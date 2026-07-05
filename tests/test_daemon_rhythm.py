"""Daemon plumbing tests for the rhythm observe stage (no bridge writes)."""
import json

import pytest

from hueman.circadian_daemon import CircadianDaemon
from hueman.config import Config
from hueman.watch import BridgeEvent


def _config(tmp_path, **rhythm_extra):
    rhythm = {
        "bedroom": "Bedroom",
        "state_file": str(tmp_path / "rhythm-state.json"),
        "signals": {
            "next_alarm_file": str(tmp_path / ".next-alarm"),
            "charging_file": str(tmp_path / ".phone-charging"),
        },
    }
    rhythm.update(rhythm_extra)
    return Config.parse({
        "bridge": {"host": "bridge.local", "application_key": "k"},
        "location": {"lat": 40.0, "lon": -75.0, "tz": "America/New_York"},
        "circadian": {"day": {"brightness": 90, "kelvin": 5000},
                      "evening": {"brightness": 40, "kelvin": 2700},
                      "night": {"brightness": 15, "kelvin": 2200}},
        "circadian_daemon": {"zone": "Main"},
        "rhythm": rhythm,
    })


class _NullClient:
    """Fails the test if the daemon writes to the bridge in observe stage."""

    class bridge:
        host = "bridge.local"
        application_key = "k"

    def update_resource(self, rtype, rid, body):
        raise AssertionError(f"observe stage wrote to the bridge: {rtype} {rid}")


def _daemon(tmp_path, **rhythm_extra):
    cfg = _config(tmp_path, **rhythm_extra)
    d = CircadianDaemon.for_test(_NullClient(), cfg, grouped_light_rid="gl-1")
    d._rhythm_motion_rooms = {"svc-bed": "Bedroom", "svc-kitchen": "Kitchen"}
    return d


def test_stage_beyond_observe_refuses_to_start(tmp_path):
    from hueman.errors import ConfigError
    with pytest.raises(ConfigError, match="rhythm.stage"):
        _daemon(tmp_path, stage="mornings")


def test_motion_event_feeds_tracker_by_room(tmp_path):
    d = _daemon(tmp_path)
    ev = BridgeEvent(rtype="convenience_area_motion", rid="svc-bed",
                     data={"motion": {"motion": True}})
    d._handle_event(ev, 1_000_000.0)
    s = d._presence.summary(1_000_000.0 + 1)
    assert s.recent_motion_count == 1
    assert s.recent_rooms == ("Bedroom",)


def test_motion_clear_and_unknown_rid_are_ignored(tmp_path):
    d = _daemon(tmp_path)
    d._handle_event(BridgeEvent(rtype="convenience_area_motion", rid="svc-bed",
                                data={"motion": {"motion": False}}), 1_000_000.0)
    d._handle_event(BridgeEvent(rtype="convenience_area_motion", rid="svc-nope",
                                data={"motion": {"motion": True}}), 1_000_000.0)
    assert d._presence.summary(1_000_001.0).recent_motion_count == 0


def test_tick_reads_signal_files_and_persists_state(tmp_path):
    d = _daemon(tmp_path)
    (tmp_path / ".next-alarm").write_text("1800000000")
    (tmp_path / ".phone-charging").write_text("")
    d._rhythm_tick(1_000_000.0)
    state = json.loads((tmp_path / "rhythm-state.json").read_text())
    assert state["version"] == 1
    assert state["snapshot"]["phase"] in (
        "dawn", "morning", "daylight", "evening", "wind_down", "night", "sleep")


def test_alarm_file_garbage_is_none(tmp_path):
    d = _daemon(tmp_path)
    (tmp_path / ".next-alarm").write_text("not-a-number")
    assert d._read_alarm_epoch() is None
    (tmp_path / ".next-alarm").write_text("0")
    assert d._read_alarm_epoch() is None


def test_rhythm_disabled_is_inert(tmp_path):
    cfg = Config.parse({
        "bridge": {"host": "bridge.local", "application_key": "k"},
        "location": {"lat": 40.0, "lon": -75.0, "tz": "America/New_York"},
        "circadian": {"day": {"brightness": 90, "kelvin": 5000},
                      "evening": {"brightness": 40, "kelvin": 2700},
                      "night": {"brightness": 15, "kelvin": 2200}},
        "circadian_daemon": {"zone": "Main"},
    })
    d = CircadianDaemon.for_test(_NullClient(), cfg, grouped_light_rid="gl-1")
    assert d._rhythm is None
    d._rhythm_tick(1_000_000.0)  # no-op, no crash, no file
