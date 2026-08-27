"""Pure decision core for the circadian daemon (no clock, no I/O).

Given an injected ``now`` (epoch seconds) and external/resume events, decides the
action for this tick — drive the zone to the current curve point, fade it off at
hand-off, or hold — and tracks the daemon mode. Time and events are explicit
arguments so the whole state machine is unit-tested without a bridge, mirroring
:class:`hueman.engine.PolicyEngine`.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from .circadian import CircadianCurve, CircadianParams
from .config import Anchor, CircadianDaemonSpec
from .sun import SolarCalculator


@dataclass(frozen=True)
class DriveTo:
    """Drive the zone to this curve sample over ``transition_ms``.

    Attributes:
        brightness: Target brightness percentage (0-100), floor/ceiling applied.
        mirek: Target colour temperature in mirek.
        transition_ms: Cross-fade duration for the write, in milliseconds.
    """

    brightness: float
    mirek: int
    transition_ms: int


@dataclass(frozen=True)
class FadeOff:
    """Fade the zone off over ``transition_ms`` (the hand-off at window close)."""

    transition_ms: int


@dataclass(frozen=True)
class Hold:
    """Do nothing this tick; ``reason`` names the mode holding (for logging)."""

    reason: str


class CircadianController:
    """State machine deciding the circadian daemon's per-tick action.

    Modes: ``DRIVING`` (inside the window, following the curve), ``SUSPENDED``
    (a manual override was detected; hands off until resumed or the next
    window open), and ``NIGHT_IDLE`` (outside the window, nothing to do).
    """

    DRIVING = "driving"
    SUSPENDED = "suspended"
    NIGHT_IDLE = "night_idle"

    def __init__(
        self,
        spec: CircadianDaemonSpec,
        circadian: CircadianParams,
        solar: SolarCalculator,
        tz_offset_hours: float,
        tz: str | None = None,
    ) -> None:
        """Bind the daemon spec, curve, solar calculator and timezone.

        Args:
            spec: Daemon settings (window anchors, transitions, override policy).
            circadian: Curve parameters used to build the ``CircadianCurve``.
            solar: Calculator for the site's sun times and elevation.
            tz_offset_hours: Fixed UTC offset fallback when ``tz`` is unset.
            tz: Optional IANA timezone name; when set, local time is DST-correct.
        """
        self._spec = spec
        self._curve = CircadianCurve(circadian)
        self._solar = solar
        self._tz_offset_hours = tz_offset_hours
        self._tz = tz
        self._mode = self.NIGHT_IDLE
        self._was_in_window = False

    @property
    def mode(self) -> str:
        """Return the current daemon mode (``DRIVING``/``SUSPENDED``/``NIGHT_IDLE``)."""
        return self._mode

    # -- public accessors (used by the bias subsystem, independent of mode) -- #
    def in_window(self, now: float) -> bool:
        """Return whether ``now`` is within the active drive window."""
        return self._in_window(now)

    def drive_to(self, now: float) -> DriveTo:
        """Return the curve sample (brightness/mirek/transition) for ``now``."""
        return self._drive_to(now)

    def is_night(self, now: float) -> bool:
        """Whether the home reads as night at ``now``.

        True outside the drive window (the zone is parked at its overnight
        look) or once the curve has fully reached its night anchors (sun at or
        below −6°) — see :meth:`CircadianCurve.is_night`. The bias hold keys
        its per-light ``night_look`` variant off this, so TV viewing against a
        dim home gets the glare-cut hold while dusk's twilight blend still
        gets the full daytime one.
        """
        if not self._in_window(now):
            return True
        date = self._local_dt(now).date()
        elev = self._solar.solar_elevation(date, self._minute_of_day(now))
        return self._curve.is_night(elev, self._solar.noon_elevation(date))

    # -- time helpers ------------------------------------------------------- #
    def _local_dt(self, now: float) -> _dt.datetime:
        """Return ``now`` as a local datetime (DST-correct when a tz name is set)."""
        tz = ZoneInfo(self._tz) if self._tz else _dt.timezone(
            _dt.timedelta(hours=self._tz_offset_hours))
        return _dt.datetime.fromtimestamp(now, tz)

    def _minute_of_day(self, now: float) -> float:
        """Return ``now`` as fractional local minutes after midnight."""
        d = self._local_dt(now)
        return d.hour * 60 + d.minute + d.second / 60.0

    def _window(self, date: _dt.date) -> tuple[int, int]:
        """Return ``(start_min, hand_off_min)`` for ``date``, start sun-resolved."""
        sun = self._solar.sun_times(date)
        start = self._spec.start.resolve(sun.sunrise_min, sun.sunset_min)
        return start, self._spec.hand_off_min

    def _in_window(self, now: float) -> bool:
        """Return whether ``now`` falls in ``[start, hand_off)`` for its local date."""
        date = self._local_dt(now).date()
        start, hand_off = self._window(date)
        minute = self._minute_of_day(now)
        return start <= minute < hand_off

    # -- target ------------------------------------------------------------- #
    def _drive_to(self, now: float) -> DriveTo:
        """Sample the elevation-driven curve at ``now`` and apply floor/ceiling."""
        date = self._local_dt(now).date()
        elev = self._solar.solar_elevation(date, self._minute_of_day(now))
        noon = self._solar.noon_elevation(date)
        state = self._curve.state_at(elev, noon)
        bri = state.brightness
        if self._spec.brightness_floor is not None:
            bri = max(bri, self._spec.brightness_floor)
        if self._spec.brightness_ceiling is not None:
            bri = min(bri, self._spec.brightness_ceiling)
        return DriveTo(brightness=bri, mirek=state.mirek, transition_ms=self._spec.transition_ms)

    # -- events ------------------------------------------------------------- #
    def on_external_change(self, now: float) -> None:
        """Suspend on a detected manual override.

        A no-op if ``detect_override`` is disabled, so the daemon keeps
        driving through external writes.
        """
        if self._spec.detect_override:
            self._mode = self.SUSPENDED

    def on_resume(self, now: float) -> None:
        """Resume driving (operator/API resume), regardless of the current mode."""
        self._mode = self.DRIVING

    # -- per-tick decision -------------------------------------------------- #
    def tick(self, now: float) -> DriveTo | FadeOff | Hold:
        """Advance the state machine one tick and return the action for ``now``.

        ``SUSPENDED`` holds until an explicit resume — a manual override owns
        the zone until the human hands it back. The opt-in
        ``daily_safety_resume`` re-arms driving at the window-*open* edge
        instead (for deployments that prefer the daemon never staying disabled
        past a morning); it is off by default. ``NIGHT_IDLE``
        flips to ``DRIVING`` when the window is active. While driving, an
        in-window tick returns the curve sample; the window-*close* edge
        returns a single ``FadeOff`` and drops to ``NIGHT_IDLE``. Driving
        outside the window with no close edge (an out-of-window resume, e.g. a
        nighttime power-cycle) drops to ``NIGHT_IDLE`` silently — the hand-off
        already happened, and re-firing it would stomp whatever the human just
        turned on.
        """
        in_window = self._in_window(now)
        window_opened = in_window and not self._was_in_window
        window_closed = not in_window and self._was_in_window
        self._was_in_window = in_window

        if self._mode == self.SUSPENDED:
            if window_opened and self._spec.daily_safety_resume:
                self._mode = self.DRIVING
            else:
                return Hold("suspended")

        if self._mode == self.NIGHT_IDLE:
            if in_window:
                self._mode = self.DRIVING
            else:
                return Hold("night_idle")

        # DRIVING
        if in_window:
            return self._drive_to(now)
        self._mode = self.NIGHT_IDLE
        if window_closed:
            return FadeOff(transition_ms=self._spec.fade_off_ms)
        # DRIVING outside the window without a close edge: an out-of-window
        # resume (power-cycle or trigger at night). The hand-off already
        # happened — firing it again would stomp whatever the human just
        # turned on (observed live 2026-08-07: night power-cycle -> resume ->
        # FadeOff yanked fresh lights to the 1% night look). Hands off.
        return Hold("night_idle")
