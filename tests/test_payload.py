"""Tests for colour conversion and CLIP body construction."""

from __future__ import annotations

from hue_iac.engine import TargetState
from hue_iac.payload import ColorConverter, GroupedLightCommand


def test_red_hex_maps_to_red_corner_of_gamut() -> None:
    """Pure red sits at the high-x, low-y corner of CIE space."""
    x, y = ColorConverter.hex_to_xy("#ff0000")
    assert x > 0.6
    assert y < 0.35


def test_black_is_safe() -> None:
    """A zero colour does not divide by zero."""
    assert ColorConverter.hex_to_xy("#000000") == (0.0, 0.0)


def test_off_body_is_minimal() -> None:
    """An off target produces only the on=false field."""
    body = GroupedLightCommand.build(TargetState.off())
    assert body == {"on": {"on": False}}


def test_ct_body_has_mirek_and_brightness() -> None:
    """A colour-temperature target carries dimming and mirek."""
    body = GroupedLightCommand.build(TargetState(on=True, brightness=42.0, mirek=300))
    assert body["on"] == {"on": True}
    assert body["dimming"] == {"brightness": 42.0}
    assert body["color_temperature"] == {"mirek": 300}
    assert "color" not in body


def test_hex_body_prefers_xy_over_mirek() -> None:
    """When a hex colour is present it wins over any mirek value."""
    body = GroupedLightCommand.build(TargetState(on=True, brightness=20.0, hex="ff1500"))
    assert "color" in body
    assert "color_temperature" not in body


def test_build_adds_dynamics_duration_when_transition_set() -> None:
    body = GroupedLightCommand.build(
        TargetState(on=True, brightness=50.0, mirek=300, hex=None), transition_ms=75000)
    assert body["on"] == {"on": True}
    assert body["dimming"] == {"brightness": 50.0}
    assert body["color_temperature"] == {"mirek": 300}
    assert body["dynamics"] == {"duration": 75000}


def test_build_off_with_transition_fades_off() -> None:
    body = GroupedLightCommand.build(TargetState.off(), transition_ms=90000)
    assert body == {"on": {"on": False}, "dynamics": {"duration": 90000}}


def test_build_without_transition_unchanged() -> None:
    body = GroupedLightCommand.build(TargetState(on=True, brightness=50.0, mirek=300, hex=None))
    assert "dynamics" not in body


def test_light_command_matches_grouped_body() -> None:
    """Per-light writes reuse the same body shape as grouped_light."""
    from hue_iac.payload import LightCommand

    t = TargetState(on=True, brightness=28.0, mirek=153, hex=None)
    assert LightCommand.build(t, transition_ms=500) == GroupedLightCommand.build(t, transition_ms=500)
    assert LightCommand.build(t, transition_ms=500) == {
        "on": {"on": True},
        "dimming": {"brightness": 28.0},
        "color_temperature": {"mirek": 153},
        "dynamics": {"duration": 500},
    }


def test_light_command_hex_and_off() -> None:
    from hue_iac.payload import LightCommand

    assert "color" in LightCommand.build(TargetState(on=True, brightness=5.0, hex="1a0a00"))
    assert LightCommand.build(TargetState.off(), transition_ms=90000) == {
        "on": {"on": False},
        "dynamics": {"duration": 90000},
    }
