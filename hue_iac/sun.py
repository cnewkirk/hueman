"""Local sunrise/sunset computation (no network, no third-party dependencies).

Implements the NOAA solar-position equations closely enough for lighting
automation (accurate to roughly one minute for non-polar latitudes). The IaC
engine only needs sunrise and sunset as local wall-clock minutes for a given
date and location, which lets the circadian curve and the day/night timeslots
anchor to the real sun rather than to fixed clock times.

Reference:
    NOAA Solar Calculator -- https://gml.noaa.gov/grad/solcalc/calcdetails.html
"""

from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass
from zoneinfo import ZoneInfo


def zone_offset_hours(tz_name: str, date: _dt.date) -> float:
    """Return the UTC offset in hours for ``tz_name`` on ``date``.

    Sampled at local noon, so the result is the day's prevailing offset and
    never lands in the ambiguous/imaginary wall-clock hour around a DST
    transition. Lighting only needs the day's offset, so this is exact here.
    """
    noon = _dt.datetime(date.year, date.month, date.day, 12)
    return ZoneInfo(tz_name).utcoffset(noon).total_seconds() / 3600.0


@dataclass(frozen=True)
class SunTimes:
    """Sunrise and sunset for a single day.

    Attributes:
        sunrise_min: Sunrise as local minutes after midnight. ``-inf`` denotes
            polar day and ``+inf`` denotes polar night.
        sunset_min: Sunset as local minutes after midnight, with the same
            infinite sentinels reversed for the polar cases.
    """

    sunrise_min: float
    sunset_min: float

    @property
    def is_polar_day(self) -> bool:
        """Return ``True`` when the sun never sets on this day."""
        return math.isinf(self.sunrise_min) and self.sunrise_min < 0

    @property
    def is_polar_night(self) -> bool:
        """Return ``True`` when the sun never rises on this day."""
        return math.isinf(self.sunrise_min) and self.sunrise_min > 0


class SolarCalculator:
    """Compute sunrise/sunset for a fixed location.

    Bundling the location into an object keeps callers from threading
    latitude, longitude and timezone through every call and lets the engine
    hold one configured calculator per site.

    Args:
        lat: Latitude in decimal degrees, north positive.
        lon: Longitude in decimal degrees, east positive.
        tz_offset_hours: Local UTC offset in hours (for example ``-5`` for US
            Eastern Standard Time).
        tz: Optional IANA timezone name (for example ``America/Los_Angeles``).
            When set, sun_times uses the date's real UTC offset (DST-correct);
            otherwise the fixed tz_offset_hours is used.
    """

    def __init__(
        self, lat: float, lon: float, tz_offset_hours: float, tz: str | None = None
    ) -> None:
        self._lat = lat
        self._lon = lon
        self._tz_offset_hours = tz_offset_hours
        self._tz = tz

    def _offset_for(self, date: _dt.date) -> float:
        """UTC offset in hours for ``date`` — DST-correct when a tz name is set."""
        if self._tz is not None:
            return zone_offset_hours(self._tz, date)
        return self._tz_offset_hours

    @staticmethod
    def _julian_day(date: _dt.date) -> float:
        """Return the Julian Day Number at midnight UTC for ``date``."""
        year, month, day = date.year, date.month, date.day
        if month <= 2:
            year -= 1
            month += 12
        century = year // 100
        gregorian = 2 - century + century // 4
        return (
            math.floor(365.25 * (year + 4716))
            + math.floor(30.6001 * (month + 1))
            + day
            + gregorian
            - 1524.5
        )

    def _geometry(self, date: _dt.date) -> tuple[float, float]:
        """Return ``(declination_deg, equation_of_time_min)`` for ``date``.

        These are the date-only solar intermediates shared by sunrise/sunset and
        elevation; longitude, latitude and the UTC offset are applied by callers.
        """
        julian_century = (self._julian_day(date) - 2451545.0) / 36525.0

        geom_mean_long = (
            280.46646 + julian_century * (36000.76983 + julian_century * 0.0003032)
        ) % 360
        geom_mean_anom = 357.52911 + julian_century * (35999.05029 - 0.0001537 * julian_century)
        eccentricity = 0.016708634 - julian_century * (
            0.000042037 + 0.0000001267 * julian_century
        )

        sun_eq_of_centre = (
            math.sin(math.radians(geom_mean_anom))
            * (1.914602 - julian_century * (0.004817 + 0.000014 * julian_century))
            + math.sin(math.radians(2 * geom_mean_anom)) * (0.019993 - 0.000101 * julian_century)
            + math.sin(math.radians(3 * geom_mean_anom)) * 0.000289
        )
        sun_true_long = geom_mean_long + sun_eq_of_centre
        sun_app_long = (
            sun_true_long
            - 0.00569
            - 0.00478 * math.sin(math.radians(125.04 - 1934.136 * julian_century))
        )

        mean_obliquity = 23 + (
            26 + (21.448 - julian_century * (46.815 + julian_century * (0.00059 - julian_century * 0.001813))) / 60
        ) / 60
        obliquity_corr = mean_obliquity + 0.00256 * math.cos(
            math.radians(125.04 - 1934.136 * julian_century)
        )

        declination = math.degrees(
            math.asin(
                math.sin(math.radians(obliquity_corr)) * math.sin(math.radians(sun_app_long))
            )
        )

        var_y = math.tan(math.radians(obliquity_corr / 2)) ** 2
        eq_of_time = 4 * math.degrees(
            var_y * math.sin(2 * math.radians(geom_mean_long))
            - 2 * eccentricity * math.sin(math.radians(geom_mean_anom))
            + 4
            * eccentricity
            * var_y
            * math.sin(math.radians(geom_mean_anom))
            * math.cos(2 * math.radians(geom_mean_long))
            - 0.5 * var_y * var_y * math.sin(4 * math.radians(geom_mean_long))
            - 1.25 * eccentricity * eccentricity * math.sin(2 * math.radians(geom_mean_anom))
        )
        return declination, eq_of_time

    def sun_times(self, date: _dt.date) -> SunTimes:
        """Return the :class:`SunTimes` for ``date`` at this location.

        Args:
            date: The local calendar date to compute for.

        Returns:
            A :class:`SunTimes`; the polar-day/polar-night cases are flagged via
            its properties rather than raising.
        """
        declination, eq_of_time = self._geometry(date)

        # The -0.833 degree zenith offset accounts for atmospheric refraction
        # and the angular radius of the solar disc.
        cos_hour_angle = (
            math.cos(math.radians(90.833))
            / (math.cos(math.radians(self._lat)) * math.cos(math.radians(declination)))
            - math.tan(math.radians(self._lat)) * math.tan(math.radians(declination))
        )
        if cos_hour_angle < -1:
            return SunTimes(sunrise_min=float("-inf"), sunset_min=float("inf"))
        if cos_hour_angle > 1:
            return SunTimes(sunrise_min=float("inf"), sunset_min=float("-inf"))

        hour_angle = math.degrees(math.acos(cos_hour_angle))
        solar_noon_min = 720 - 4 * self._lon - eq_of_time + self._offset_for(date) * 60
        return SunTimes(
            sunrise_min=solar_noon_min - hour_angle * 4,
            sunset_min=solar_noon_min + hour_angle * 4,
        )

    def solar_elevation(self, date: _dt.date, minute_of_day: float) -> float:
        """Return the sun's altitude in degrees at ``minute_of_day`` (local clock).

        Negative below the horizon. DST-correct when this calculator has a tz name.
        """
        declination, eq_of_time = self._geometry(date)
        solar_noon_min = 720 - 4 * self._lon - eq_of_time + self._offset_for(date) * 60
        hour_angle = (minute_of_day - solar_noon_min) / 4.0  # degrees; 0 at solar noon
        sin_elev = (
            math.sin(math.radians(self._lat)) * math.sin(math.radians(declination))
            + math.cos(math.radians(self._lat))
            * math.cos(math.radians(declination))
            * math.cos(math.radians(hour_angle))
        )
        return math.degrees(math.asin(max(-1.0, min(1.0, sin_elev))))

    def noon_elevation(self, date: _dt.date) -> float:
        """Return the day's maximum elevation (at solar noon): ``90 − |lat − dec|``."""
        declination, _ = self._geometry(date)
        return 90.0 - abs(self._lat - declination)
