"""Tests for the solar-elevation circadian colour curve."""

from __future__ import annotations

from hueman.circadian import CircadianCurve, CircadianParams

P = CircadianParams()
NOON = 65.0  # a representative solar-noon elevation (degrees)


def _curve() -> CircadianCurve:
    return CircadianCurve(P)


def test_solar_noon_is_day_look() -> None:
    s = _curve().state_at(NOON, NOON)
    assert (s.mirek, s.brightness) == (P.day_mirek, P.day_brightness)


def test_horizon_is_evening_look() -> None:
    s = _curve().state_at(0.0, NOON)
    assert (s.mirek, s.brightness) == (P.evening_mirek, P.evening_brightness)


def test_civil_dusk_is_night_look() -> None:
    s = _curve().state_at(-6.0, NOON)
    assert (s.mirek, s.brightness) == (P.night_mirek, P.night_brightness)


def test_below_civil_twilight_is_night() -> None:
    s = _curve().state_at(-25.0, NOON)
    assert (s.mirek, s.brightness) == (P.night_mirek, P.night_brightness)


def test_brightness_declines_monotonically_as_sun_drops() -> None:
    c = _curve()
    bris = [c.state_at(e, NOON).brightness for e in (NOON, 50, 35, 20, 10, 2, 0, -3, -6)]
    assert all(bris[i] >= bris[i + 1] - 1e-9 for i in range(len(bris) - 1))


def test_colour_warms_as_sun_drops() -> None:
    c = _curve()
    assert c.state_at(10.0, NOON).kelvin < c.state_at(NOON, NOON).kelvin
    mireks = [c.state_at(e, NOON).mirek for e in (NOON, 40, 20, 0, -6)]
    assert all(mireks[i] <= mireks[i + 1] for i in range(len(mireks) - 1))


def test_polar_night_nonpositive_noon_elevation_is_night() -> None:
    s = _curve().state_at(-5.0, -10.0)
    assert (s.mirek, s.brightness) == (P.night_mirek, P.night_brightness)
