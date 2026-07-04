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
    brightness: float
    mirek: int
    transition_ms: int


@dataclass(frozen=True)
class FadeOff:
    transition_ms: int


@dataclass(frozen=True)
class Hold:
    reason: str


class CircadianController:
    """State machine deciding the circadian daemon's per-tick action."""

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
        self._spec = spec
        self._curve = CircadianCurve(circadian)
        self._solar = solar
        self._tz_offset_hours = tz_offset_hours
        self._tz = tz
        self._mode = self.NIGHT_IDLE
        self._was_in_window = False

    @property
    def mode(self) -> str:
        return self._mode

    # -- public accessors (used by the bias subsystem, independent of mode) -- #
    def in_window(self, now: float) -> bool:
        """Return whether ``now`` is within the active drive window."""
        return self._in_window(now)

    def drive_to(self, now: float) -> DriveTo:
        """Return the curve sample (brightness/mirek/transition) for ``now``."""
        return self._drive_to(now)

    # -- time helpers ------------------------------------------------------- #
    def _local_dt(self, now: float) -> _dt.datetime:
        tz = ZoneInfo(self._tz) if self._tz else _dt.timezone(
            _dt.timedelta(hours=self._tz_offset_hours))
        return _dt.datetime.fromtimestamp(now, tz)

    def _minute_of_day(self, now: float) -> float:
        d = self._local_dt(now)
        return d.hour * 60 + d.minute + d.second / 60.0

    def _window(self, date: _dt.date) -> tuple[int, int]:
        sun = self._solar.sun_times(date)
        start = self._spec.start.resolve(sun.sunrise_min, sun.sunset_min)
        return start, self._spec.hand_off_min

    def _in_window(self, now: float) -> bool:
        date = self._local_dt(now).date()
        start, hand_off = self._window(date)
        minute = self._minute_of_day(now)
        return start <= minute < hand_off

    # -- target ------------------------------------------------------------- #
    def _drive_to(self, now: float) -> DriveTo:
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
        if self._spec.detect_override:
            self._mode = self.SUSPENDED

    def on_resume(self, now: float) -> None:
        self._mode = self.DRIVING

    # -- per-tick decision -------------------------------------------------- #
    def tick(self, now: float) -> DriveTo | FadeOff | Hold:
        in_window = self._in_window(now)
        window_opened = in_window and not self._was_in_window
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
        return FadeOff(transition_ms=self._spec.fade_off_ms)
