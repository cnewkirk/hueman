"""The motion/timing decision engine — pure, deterministic, testable.

This is the brain that turns sensor events into light commands. It is kept free
of any I/O: it consumes already-classified events (motion, ambient light level,
a manual-override signal, and clock ticks) and emits :class:`Action` values that
the runtime (:mod:`hueman.watch`) executes against the bridge. Time is passed
in as an explicit epoch-seconds float so tests drive it directly.

Why a custom engine instead of the bridge's built-in motion behaviour? Three of
the user's requirements can't be expressed by the native sensor automation:

  * **Continuous circadian colour** — the on-motion colour is recomputed from
    the live sun position every activation, not snapped to a fixed scene.
  * **Honoured manual overrides** — recalling a scene by hand pauses the
    automation for the room for a configurable window.
  * **Precise night behaviour** — a distinct standby state (e.g. a dim red
    nightlight) plus short dead-of-night timeouts.

One :class:`PolicyEngine` instance is created per controlled area.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from enum import Enum

from .circadian import CircadianCurve, CircadianParams
from .config import LightState, MotionPolicy, Timeslot
from .sun import SolarCalculator, SunTimes


class Phase(Enum):
    """Lifecycle state of one controlled area's automation."""

    STANDBY = "standby"        # no motion; lights off or at nightlight state
    ACTIVE = "active"          # motion seen recently; lights at on-motion state
    DIMMING = "dimming"        # warning dim before turning off
    OVERRIDDEN = "overridden"  # human took manual control; automation paused


@dataclass(frozen=True)
class TargetState:
    """A concrete light command, colour already resolved to mirek/xy/none."""

    on: bool
    brightness: float | None = None
    mirek: int | None = None
    hex: str | None = None

    @classmethod
    def off(cls) -> "TargetState":
        """Return the canonical all-off command."""
        return cls(on=False)


@dataclass(frozen=True)
class Action:
    """An instruction to apply ``state`` to ``area`` (a room/zone name)."""

    area: str
    state: TargetState
    reason: str  # human-readable, for logs/dry-run


@dataclass
class _AreaRuntime:
    """Mutable per-area bookkeeping for the state machine.

    Attributes:
        phase: The area's current :class:`Phase`.
        last_motion_ts: Epoch seconds of the most recent motion detection.
        dim_started_ts: Epoch seconds at which the dim warning began.
        override_until_ts: Epoch seconds until which manual override holds.
        last_lux: Most recent ambient light level reported for the area, kept so
            the brightness gate works even though motion events carry no lux.
    """

    phase: Phase = Phase.STANDBY
    last_motion_ts: float = 0.0
    dim_started_ts: float = 0.0
    override_until_ts: float = 0.0
    last_lux: int | None = None


def _start_minute(scored_slot: tuple[float, "Timeslot"]) -> float:
    """Return the resolved start minute from a ``(minute, slot)`` pair.

    Used as an explicit sort key in place of an inline lambda.
    """
    return scored_slot[0]


class PolicyEngine:
    """Drives one motion policy across the areas it controls.

    Args:
        policy: The validated motion policy this engine enforces.
        location: A ``(latitude, longitude, tz_offset_hours)`` triple used to
            anchor circadian colour and ``sunrise``/``sunset`` timeslots.
        circadian: Circadian curve parameters shared across the deployment.
    """

    def __init__(
        self,
        policy: MotionPolicy,
        location: tuple[float, float, float],
        circadian: CircadianParams = CircadianParams(),
    ) -> None:
        """Initialise per-area runtimes and the solar/circadian helpers."""
        self.policy = policy
        latitude, longitude, tz_offset_hours = location
        self._tz_offset_hours = tz_offset_hours
        self._solar = SolarCalculator(latitude, longitude, tz_offset_hours)
        self._curve = CircadianCurve(circadian)
        self._areas: dict[str, _AreaRuntime] = {}
        for area in policy.areas:
            self._areas[area] = _AreaRuntime()
        self._sun_cache: dict[_dt.date, SunTimes] = {}

    # -- time helpers ------------------------------------------------------- #
    def _local_dt(self, ts: float) -> _dt.datetime:
        """Convert epoch seconds to a timezone-aware local datetime."""
        tz = _dt.timezone(_dt.timedelta(hours=self._tz_offset_hours))
        return _dt.datetime.fromtimestamp(ts, tz)

    def _sun_for(self, day: _dt.date) -> SunTimes:
        """Return cached sunrise/sunset for ``day``, computing it on first use."""
        if day not in self._sun_cache:
            self._sun_cache[day] = self._solar.sun_times(day)
        return self._sun_cache[day]

    def _minute_of_day(self, ts: float) -> float:
        """Return ``ts`` as fractional local minutes after midnight."""
        d = self._local_dt(ts)
        return d.hour * 60 + d.minute + d.second / 60.0

    def _resolve_time_ref(self, ref: str, sun: SunTimes) -> float:
        """Resolve a slot start (``"sunrise"``, ``"sunset"`` or ``"HH:MM"``) to minutes.

        Polar-day sentinels are clamped to the day's edges so every slot still
        gets a finite, sortable start minute.
        """
        if ref == "sunrise":
            return sun.sunrise_min if not sun.is_polar_day else 0.0
        if ref == "sunset":
            return sun.sunset_min if not sun.is_polar_day else 24 * 60.0
        h, m = ref.split(":")
        return int(h) * 60 + int(m)

    def current_timeslot(self, ts: float) -> Timeslot:
        """Return the timeslot in effect at ``ts``.

        The active slot is the one whose resolved start time is the latest at or
        before the current minute. Selection wraps around midnight, so a slot
        starting at 23:00 also covers the small hours until the next morning
        slot begins.

        Args:
            ts: Epoch seconds of the moment to evaluate.

        Returns:
            The :class:`~hueman.config.Timeslot` currently in effect.
        """
        sun = self._sun_for(self._local_dt(ts).date())
        now = self._minute_of_day(ts)
        scored: list[tuple[float, Timeslot]] = []
        for slot in self.policy.timeslots:
            scored.append((self._resolve_time_ref(slot.start, sun), slot))
        scored.sort(key=_start_minute)

        chosen = scored[-1][1]  # default to the last slot for the wrap-around
        for start_min, slot in scored:
            if start_min <= now:
                chosen = slot
        return chosen

    # -- colour resolution -------------------------------------------------- #
    def _resolve(self, st: LightState, ts: float) -> TargetState:
        """Resolve a configured :class:`LightState` into a concrete command at ``ts``.

        ``circadian`` colour is recomputed from the live sun position (and may
        also supply the brightness when the config leaves it unset); ``ct`` and
        ``xy`` pass through as fixed mirek/hex values.
        """
        if not st.on:
            return TargetState.off()
        bri = st.brightness
        mirek = None
        hexv = None
        if st.color is not None:
            if st.color.mode == "circadian":
                day = self._local_dt(ts).date()
                minute = self._minute_of_day(ts)
                elevation = self._solar.solar_elevation(day, minute)
                circadian_state = self._curve.state_at(elevation, self._solar.noon_elevation(day))
                mirek = circadian_state.mirek
                if bri is None:
                    bri = circadian_state.brightness
            elif st.color.mode == "ct":
                mirek = st.color.mirek
            elif st.color.mode == "xy":
                hexv = st.color.hex
        return TargetState(on=True, brightness=bri, mirek=mirek, hex=hexv)

    def _standby_target(self, slot: Timeslot, ts: float) -> TargetState:
        """Return the slot's standby command — off unless a nightlight is configured."""
        if slot.standby is None:
            return TargetState.off()
        return self._resolve(slot.standby, ts)

    # -- event handlers ----------------------------------------------------- #
    def on_manual_override(self, area: str, ts: float) -> list[Action]:
        """A human changed this area's lights directly; pause automation."""
        if area not in self._areas or not self.policy.manual_override.respect:
            return []
        rt = self._areas[area]
        rt.phase = Phase.OVERRIDDEN
        rt.override_until_ts = ts + self.policy.manual_override.pause_ms / 1000.0
        return []  # we deliberately do not fight the human

    def on_manual_off(self, area: str, ts: float) -> list[Action]:
        """Human turned the area fully off; optionally re-arm motion early."""
        if area not in self._areas:
            return []
        rt = self._areas[area]
        if rt.phase == Phase.OVERRIDDEN and self.policy.manual_override.resume_on_off:
            rt.phase = Phase.STANDBY
            rt.override_until_ts = 0.0
        return []

    def on_motion(self, area: str, present: bool, ts: float, lux: int | None = None) -> list[Action]:
        """Handle a motion-sensor report for ``area``.

        Presence switches the area to its on-motion state for the active
        timeslot (subject to the override and lux gates); an absence report
        records nothing immediately and is aged out by :meth:`tick`.

        Args:
            area: The room or zone the sensor covers.
            present: ``True`` for motion detected, ``False`` for cleared.
            ts: Epoch seconds of the report.
            lux: Ambient light level, when available, for the brightness gate.

        Returns:
            The actions to apply, possibly empty.
        """
        if area not in self._areas:
            return []
        rt = self._areas[area]
        self._expire_override(rt, ts)
        if lux is not None:
            # Ambient readings arrive on their own (presence-less) reports; keep
            # the latest so a subsequent motion event can consult the lux gate.
            rt.last_lux = lux

        if not present:
            # Motion cleared: nothing immediate; the tick handler ages it out.
            return []
        if rt.phase == Phase.OVERRIDDEN:
            return []  # honour the override
        if not self.policy.enabled:
            return []
        dim = self.policy.dim_before_off
        if rt.phase == Phase.DIMMING and dim is not None and not dim.recovery:
            # Override disabled recovery: let the dim run its course to standby
            # and deliberately do not extend the idle timer.
            return []

        if rt.phase == Phase.ACTIVE:
            # Already on; sustained presence only extends the idle timer. The lux
            # gate must NOT apply here, or a room lit by its own lights (high
            # ambient) would stop refreshing and time off while still occupied.
            rt.last_motion_ts = ts
            return []
        if rt.phase == Phase.STANDBY and self._too_bright(rt.last_lux):
            return []  # don't switch a dark, idle room on when it's already bright

        # Turn on (from standby) or recover (from a dim warning).
        rt.last_motion_ts = ts
        rt.phase = Phase.ACTIVE
        slot = self.current_timeslot(ts)
        return [Action(area, self._resolve(slot.on_motion, ts), f"motion -> {slot.name}")]

    def tick(self, ts: float) -> list[Action]:
        """Advance every area's timers and emit due transitions.

        Drives the idle-timeout -> dim -> standby progression. Recovery back to
        active when motion returns during the dim warning is handled eagerly in
        :meth:`on_motion`; this method only ages timers forward. Should be
        called periodically (for example once per second) by the runtime.

        Args:
            ts: Epoch seconds of this tick.

        Returns:
            The actions to apply this tick, possibly empty.
        """
        actions: list[Action] = []
        for area, rt in self._areas.items():
            self._expire_override(rt, ts)
            if rt.phase in (Phase.STANDBY, Phase.OVERRIDDEN):
                continue
            slot = self.current_timeslot(ts)
            idle = ts - rt.last_motion_ts
            timeout = slot.timeout_ms / 1000.0
            dim = self.policy.dim_before_off

            if rt.phase == Phase.ACTIVE and idle >= timeout:
                if dim is not None:
                    rt.phase = Phase.DIMMING
                    rt.dim_started_ts = ts
                    actions.append(Action(area, self._dim_target(slot, ts), "idle timeout -> dim warning"))
                else:
                    rt.phase = Phase.STANDBY
                    actions.append(Action(area, self._standby_target(slot, ts), "idle timeout -> standby"))
            elif rt.phase == Phase.DIMMING:
                dim_elapsed = dim.duration_ms / 1000.0 if dim is not None else 0.0
                if ts - rt.dim_started_ts >= dim_elapsed:
                    rt.phase = Phase.STANDBY
                    actions.append(Action(area, self._standby_target(slot, ts), "dim elapsed -> standby"))
        return actions

    def enter_standby_all(self, ts: float) -> list[Action]:
        """Force every area to its standby state (used on startup to converge)."""
        actions = []
        for area, rt in self._areas.items():
            rt.phase = Phase.STANDBY
            slot = self.current_timeslot(ts)
            actions.append(Action(area, self._standby_target(slot, ts), "startup -> standby"))
        return actions

    # -- internals ---------------------------------------------------------- #
    def _dim_target(self, slot: Timeslot, ts: float) -> TargetState:
        """Return the dim-warning command: the on-motion look at 30% brightness.

        The floor of 1% keeps the warning visible even for very dim slots.
        """
        base = self._resolve(slot.on_motion, ts)
        if not base.on:
            return base
        bri = base.brightness if base.brightness is not None else 100.0
        return TargetState(on=True, brightness=max(1.0, bri * 0.3), mirek=base.mirek, hex=base.hex)

    def _too_bright(self, lux: int | None) -> bool:
        """Return ``True`` when the lux gate should suppress switching on.

        Unknown lux (no reading yet, or no gate configured) never suppresses.
        """
        ll = self.policy.light_level
        if ll is None or lux is None:
            return False
        return lux >= ll.threshold_lux

    @staticmethod
    def _expire_override(rt: _AreaRuntime, ts: float) -> None:
        """Drop an elapsed manual override, returning the area to standby."""
        if rt.phase == Phase.OVERRIDDEN and ts >= rt.override_until_ts:
            rt.phase = Phase.STANDBY
            rt.override_until_ts = 0.0

    # -- introspection (for status/dry-run) -------------------------------- #
    def phase_of(self, area: str) -> Phase:
        """Return ``area``'s current :class:`Phase` (raises ``KeyError`` if unknown)."""
        return self._areas[area].phase
