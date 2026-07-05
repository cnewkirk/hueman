"""Tests for the ``rhythm:`` config block."""
import pytest

from hueman.config import Config, RhythmSpec
from hueman.errors import ConfigError

BASE = {
    "bridge": {"host": "bridge.local"},
    "location": {"lat": 40.0, "lon": -75.0, "tz": "America/New_York"},
    "circadian": {"day": {"brightness": 90, "kelvin": 5000},
                  "evening": {"brightness": 40, "kelvin": 2700},
                  "night": {"brightness": 15, "kelvin": 2200}},
}


def _cfg(rhythm):
    doc = dict(BASE)
    doc["rhythm"] = rhythm
    return Config.parse(doc)


def test_rhythm_absent_is_none():
    assert Config.parse(dict(BASE)).rhythm is None


def test_rhythm_minimal_defaults():
    spec = _cfg({"bedroom": "Bedroom"}).rhythm
    assert spec is not None
    assert spec.stage == "observe"
    assert spec.bedroom == "Bedroom"
    assert spec.bed_target_min == 23 * 60
    assert spec.wake_default_min == 7 * 60
    assert spec.weekend_drift_cap_min == 90
    assert spec.wind_down_lead_min == 90
    assert spec.dawn_lead_min == 25
    assert spec.morning_min == 60
    assert spec.state_file == "rhythm-state.json"
    assert spec.signals.next_alarm_file is None
    assert spec.presence.quiet_min == 30
    assert spec.presence.wake_confirm_events == 3
    assert spec.presence.wake_confirm_window_min == 10
    assert spec.presence.pet_progression_min == 5


def test_rhythm_full_block():
    spec = _cfg({
        "stage": "observe",
        "bedroom": "Bedroom",
        "bed_target": "22:45",
        "wake_default": "06:30",
        "weekend_drift_cap": "2h",
        "wind_down_lead": "75m",
        "dawn_lead": "20m",
        "dawn_max_advance": "15m",
        "morning": "45m",
        "state_file": "/data/rhythm.json",
        "signals": {
            "next_alarm_file": "/mnt/sig/.next-alarm",
            "charging_file": "/mnt/sig/.phone-charging",
        },
        "presence": {
            "quiet": "20m",
            "wake_confirm_events": 4,
            "wake_confirm_window": "8m",
            "pet_progression": "3m",
        },
    }).rhythm
    assert spec.bed_target_min == 22 * 60 + 45
    assert spec.wake_default_min == 6 * 60 + 30
    assert spec.weekend_drift_cap_min == 120
    assert spec.wind_down_lead_min == 75
    assert spec.dawn_lead_min == 20
    assert spec.dawn_max_advance_min == 15
    assert spec.morning_min == 45
    assert spec.state_file == "/data/rhythm.json"
    assert spec.signals.next_alarm_file == "/mnt/sig/.next-alarm"
    assert spec.presence.quiet_min == 20
    assert spec.presence.wake_confirm_events == 4
    assert spec.presence.wake_confirm_window_min == 8
    assert spec.presence.pet_progression_min == 3


def test_rhythm_requires_bedroom():
    with pytest.raises(ConfigError, match="rhythm.bedroom"):
        _cfg({})


def test_rhythm_rejects_unknown_stage():
    with pytest.raises(ConfigError, match="rhythm.stage"):
        _cfg({"bedroom": "Bedroom", "stage": "shepherd"})


def test_rhythm_rejects_sun_anchored_bed_target():
    with pytest.raises(ConfigError, match="rhythm.bed_target"):
        _cfg({"bedroom": "Bedroom", "bed_target": "sunset"})
