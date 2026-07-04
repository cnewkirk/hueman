"""Tests for the solar calculator."""

from __future__ import annotations

import datetime as _dt

import pytest

from hue_iac.sun import SolarCalculator, zone_offset_hours


def test_summer_solstice_nyc_sunrise_sunset_reasonable() -> None:
    """NYC summer solstice sunrise/sunset land in the expected windows."""
    calc = SolarCalculator(lat=40.7128, lon=-74.0060, tz_offset_hours=-4)  # EDT
    sun = calc.sun_times(_dt.date(2026, 6, 21))
    # Sunrise around 05:25, sunset around 20:31 local (EDT) for NYC.
    assert 5 * 60 <= sun.sunrise_min <= 5 * 60 + 45
    assert 20 * 60 <= sun.sunset_min <= 20 * 60 + 45
    assert sun.sunset_min - sun.sunrise_min > 14 * 60  # long summer day


def test_polar_night_flagged() -> None:
    """Far north in midwinter is flagged as polar night."""
    calc = SolarCalculator(lat=78.0, lon=15.0, tz_offset_hours=1)
    sun = calc.sun_times(_dt.date(2026, 12, 21))
    assert sun.is_polar_night
    assert not sun.is_polar_day


def test_polar_day_flagged() -> None:
    """Far north in midsummer is flagged as polar day."""
    calc = SolarCalculator(lat=78.0, lon=15.0, tz_offset_hours=1)
    sun = calc.sun_times(_dt.date(2026, 6, 21))
    assert sun.is_polar_day
    assert not sun.is_polar_night


def test_zone_offset_hours_tracks_dst() -> None:
    """America/Los_Angeles is -8 in winter (PST) and -7 in summer (PDT)."""
    assert zone_offset_hours("America/Los_Angeles", _dt.date(2026, 1, 15)) == -8.0
    assert zone_offset_hours("America/Los_Angeles", _dt.date(2026, 7, 15)) == -7.0


def test_tz_aware_calculator_matches_fixed_offset_per_season() -> None:
    """With a tz name, sun_times uses the date's real offset (PST winter, PDT summer)."""
    lat, lon = 45.5152, -122.6784  # Portland, OR
    tz_calc = SolarCalculator(lat, lon, tz_offset_hours=0.0, tz="America/Los_Angeles")
    pst = SolarCalculator(lat, lon, tz_offset_hours=-8)
    pdt = SolarCalculator(lat, lon, tz_offset_hours=-7)
    jan, jul = _dt.date(2026, 1, 15), _dt.date(2026, 7, 15)
    # Winter anchors to PST (-8), summer to PDT (-7): the tz path picks the date's offset.
    assert tz_calc.sun_times(jan).sunrise_min == pytest.approx(pst.sun_times(jan).sunrise_min)
    assert tz_calc.sun_times(jul).sunrise_min == pytest.approx(pdt.sun_times(jul).sunrise_min)
    # And the fixed -8 calc would be ~60 min off in summer, proving the offset really moved.
    assert abs(tz_calc.sun_times(jul).sunrise_min - pst.sun_times(jul).sunrise_min) == pytest.approx(60.0, abs=2.0)


def test_no_tz_uses_the_fixed_offset_unchanged() -> None:
    """Omitting tz reproduces today's behaviour exactly."""
    lat, lon, d = 40.7128, -74.0060, _dt.date(2026, 3, 1)
    assert (
        SolarCalculator(lat, lon, tz_offset_hours=-5).sun_times(d).sunrise_min
        == SolarCalculator(lat, lon, tz_offset_hours=-5, tz=None).sun_times(d).sunrise_min
    )


def test_noon_elevation_nyc_summer_solstice() -> None:
    """Noon elevation = 90 - |lat - declination|; ~72.7° for NYC at the solstice."""
    calc = SolarCalculator(40.7128, -74.0060, -4)
    assert calc.noon_elevation(_dt.date(2026, 6, 21)) == pytest.approx(72.7, abs=0.6)


def test_elevation_peaks_at_solar_noon() -> None:
    calc = SolarCalculator(40.7128, -74.0060, -4)
    d = _dt.date(2026, 6, 21)
    sun = calc.sun_times(d)
    noon_min = (sun.sunrise_min + sun.sunset_min) / 2.0
    assert calc.solar_elevation(d, noon_min) == pytest.approx(calc.noon_elevation(d), abs=0.2)


def test_elevation_near_zero_at_sunrise_and_sunset() -> None:
    calc = SolarCalculator(40.7128, -74.0060, -4)
    d = _dt.date(2026, 6, 21)
    sun = calc.sun_times(d)
    assert abs(calc.solar_elevation(d, sun.sunrise_min)) < 1.5
    assert abs(calc.solar_elevation(d, sun.sunset_min)) < 1.5


def test_elevation_is_dst_aware_with_tz() -> None:
    """At a fixed clock minute, the tz path uses the date's real offset (PDT in July)."""
    d = _dt.date(2026, 7, 15)
    tz_calc = SolarCalculator(45.5152, -122.6784, tz_offset_hours=0.0, tz="America/Los_Angeles")
    pdt = SolarCalculator(45.5152, -122.6784, tz_offset_hours=-7)
    assert tz_calc.solar_elevation(d, 12 * 60) == pytest.approx(pdt.solar_elevation(d, 12 * 60), abs=0.01)
