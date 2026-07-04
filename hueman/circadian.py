"""Circadian colour-temperature curve.

Maps the sun's elevation angle to a Hue colour temperature (in *mirek*, the
reciprocal megakelvin unit the CLIP API uses) plus a default brightness,
following the natural progression of daylight:

    deep night -> warmest and dimmest (high mirek)
    dawn       -> warming and brightening toward midday
    midday     -> coolest and brightest (low mirek)
    dusk       -> cooling back toward warm
    evening    -> warm and relaxed

The curve is driven by the sun's elevation geometry rather than clock times, so
it naturally tracks the seasons and adjusts for latitude without any ramp-width
tuning.

Mirek reference points (``mirek = 1_000_000 / kelvin``):

    153 mirek = 6500 K (cool daylight)    250 mirek = 4000 K (neutral)
    370 mirek = 2700 K (warm white)       500 mirek = 2000 K (candle/amber)
"""

from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass

from .sun import SolarCalculator

#: Lowest mirek (coolest colour) accepted by CLIP white/colour lights.
MIREK_MIN = 153
#: Highest mirek (warmest colour) accepted by CLIP white/colour lights.
MIREK_MAX = 500


@dataclass(frozen=True)
class CircadianParams:
    """Tunable anchor points for the circadian curve.

    Attributes:
        day_mirek: Colour temperature at solar noon.
        evening_mirek: Colour temperature at the horizon (sunrise/sunset).
        night_mirek: Colour temperature through civil twilight and below.
        day_brightness: Brightness percentage at solar noon.
        evening_brightness: Brightness percentage at the horizon.
        night_brightness: Brightness percentage at night.
        ramp_minutes: Width of the dawn and dusk transitions, in minutes
            (kept for the scene generator's transition_duration; not used
            by the geometry curve itself).
    """

    day_mirek: int = 233
    evening_mirek: int = 370
    night_mirek: int = 454
    day_brightness: float = 100.0
    evening_brightness: float = 60.0
    night_brightness: float = 15.0
    ramp_minutes: float = 90.0

    def __post_init__(self) -> None:
        """Validate that every mirek anchor is within the CLIP range."""
        for name in ("day_mirek", "evening_mirek", "night_mirek"):
            value = getattr(self, name)
            if not MIREK_MIN <= value <= MIREK_MAX:
                raise ValueError(
                    f"{name}={value} outside valid mirek range {MIREK_MIN}-{MIREK_MAX}"
                )


@dataclass(frozen=True)
class CircadianState:
    """A concrete light state produced by the curve at one moment.

    Attributes:
        mirek: Colour temperature in mirek.
        brightness: Brightness percentage in the range 0-100.
    """

    mirek: int
    brightness: float

    @property
    def kelvin(self) -> int:
        """Return the colour temperature converted to kelvin."""
        return round(1_000_000 / self.mirek)


class CircadianCurve:
    """Resolves circadian light states driven by the sun's elevation angle.

    Args:
        params: The anchor points to interpolate between.
    """

    def __init__(self, params: CircadianParams = CircadianParams()) -> None:
        """Store the anchor parameters the curve interpolates between."""
        self._params = params

    @property
    def params(self) -> CircadianParams:
        """Return the parameters this curve was built from."""
        return self._params

    @staticmethod
    def _lerp(start: float, end: float, fraction: float) -> float:
        """Linearly interpolate from ``start`` to ``end``, clamped to [0, 1]."""
        clamped = max(0.0, min(1.0, fraction))
        return start + (end - start) * clamped

    def state_at(self, elevation_deg: float, noon_elevation_deg: float) -> CircadianState:
        """Return the circadian state for a sun ``elevation_deg`` (degrees).

        Driven by the sun's geometry, normalised to ``noon_elevation_deg`` so the
        peak day look lands at solar noon every day:

        * day (θ ≥ 0):        f = sin θ / sin θ_noon, lerp(evening → day)
        * twilight (−6..0°):  g = −θ / 6, lerp(evening → night)
        * night (θ < −6° or θ_noon ≤ 0): the night look
        """
        params = self._params
        sin_noon = math.sin(math.radians(noon_elevation_deg))
        if noon_elevation_deg <= 0 or sin_noon <= 0:
            return CircadianState(params.night_mirek, params.night_brightness)

        if elevation_deg >= 0:
            fraction = max(0.0, min(1.0, math.sin(math.radians(elevation_deg)) / sin_noon))
            return CircadianState(
                round(self._lerp(params.evening_mirek, params.day_mirek, fraction)),
                round(self._lerp(params.evening_brightness, params.day_brightness, fraction), 1),
            )
        if elevation_deg >= -6.0:
            fraction = max(0.0, min(1.0, -elevation_deg / 6.0))
            return CircadianState(
                round(self._lerp(params.evening_mirek, params.night_mirek, fraction)),
                round(self._lerp(params.evening_brightness, params.night_brightness, fraction), 1),
            )
        return CircadianState(params.night_mirek, params.night_brightness)

    def sample_day(
        self, solar: SolarCalculator, date: _dt.date, step_minutes: int = 60
    ) -> list[tuple[int, CircadianState]]:
        """Sample the curve across ``date`` using the sun's elevation each step."""
        noon = solar.noon_elevation(date)
        samples: list[tuple[int, CircadianState]] = []
        minute = 0
        while minute < 24 * 60:
            elevation = solar.solar_elevation(date, minute)
            samples.append((minute, self.state_at(elevation, noon)))
            minute += step_minutes
        return samples
