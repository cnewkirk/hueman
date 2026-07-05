"""Tests for the `hueman rhythm` status command."""
import json

from hueman.cli import main


def _write_config(tmp_path, state_file):
    cfg = tmp_path / "hue.yaml"
    cfg.write_text(
        "bridge:\n  host: bridge.local\n  application_key: k\n"
        "location:\n  lat: 40.0\n  lon: -75.0\n  tz: America/New_York\n"
        "circadian:\n"
        "  day:\n    brightness: 90\n    kelvin: 5000\n"
        "  evening:\n    brightness: 40\n    kelvin: 2700\n"
        "  night:\n    brightness: 15\n    kelvin: 2200\n"
        "rhythm:\n"
        "  bedroom: Bedroom\n"
        f"  state_file: {state_file}\n"
    )
    return cfg


def test_rhythm_status_prints_snapshot(tmp_path, capsys):
    state = tmp_path / "rhythm-state.json"
    state.write_text(json.dumps({
        "version": 1,
        "snapshot": {
            "phase": "wind_down",
            "as_of": "2026-07-05T21:45:00-04:00",
            "bed_anchor_min": 1380,
            "wake_anchor_min": 405,
            "learned": {"wake_weekday": 412, "wake_weekend": None,
                        "sleep_onset_weekday": 1395, "sleep_onset_weekend": None},
            "last_change_evidence": {"minute": 1305, "reason_detail": "wind-down-lead"},
        },
        "anchors": {"weekday": {"wake": [{"date": "2026-07-03", "minute": 412}]}},
    }))
    cfg = _write_config(tmp_path, state)
    assert main(["-c", str(cfg), "rhythm"]) == 0
    out = capsys.readouterr().out
    assert "wind_down" in out
    assert "23:00" in out            # bed anchor rendered as HH:MM
    assert "06:52" in out            # learned weekday wake 412 -> HH:MM
    assert "phase error" in out.lower()


def test_rhythm_status_without_state_file(tmp_path, capsys):
    cfg = _write_config(tmp_path, tmp_path / "missing.json")
    assert main(["-c", str(cfg), "rhythm"]) == 1
    assert "daemon has not written" in capsys.readouterr().err.lower()


def test_rhythm_status_without_rhythm_block(tmp_path, capsys):
    cfg = tmp_path / "hue.yaml"
    cfg.write_text(
        "bridge:\n  host: bridge.local\n  application_key: k\n"
        "location:\n  lat: 40.0\n  lon: -75.0\n  tz: America/New_York\n"
        "circadian:\n"
        "  day:\n    brightness: 90\n    kelvin: 5000\n"
        "  evening:\n    brightness: 40\n    kelvin: 2700\n"
        "  night:\n    brightness: 15\n    kelvin: 2200\n"
    )
    assert main(["-c", str(cfg), "rhythm"]) == 1
    assert "no 'rhythm' block" in capsys.readouterr().err.lower()


def test_rhythm_status_with_non_dict_snapshot(tmp_path, capsys):
    state = tmp_path / "rhythm-state.json"
    state.write_text('{"version": 1, "snapshot": "corrupt"}')
    cfg = _write_config(tmp_path, state)
    assert main(["-c", str(cfg), "rhythm"]) == 1
    assert "malformed" in capsys.readouterr().err.lower()


def test_rhythm_status_with_wrong_typed_minutes_degrades(tmp_path, capsys):
    state = tmp_path / "rhythm-state.json"
    state.write_text(
        '{"version": 1, "snapshot": {"phase": "night", "bed_anchor_min": "1380",'
        ' "learned": {"sleep_onset_weekday": "oops"}}}')
    cfg = _write_config(tmp_path, state)
    assert main(["-c", str(cfg), "rhythm"]) == 0
    out = capsys.readouterr().out
    assert "--:--" in out and "n/a" in out.lower()
