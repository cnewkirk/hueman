"""Tests for CLI helpers that don't need a live bridge."""

from __future__ import annotations

import pytest

from hue_iac.cli import Cli
from hue_iac.errors import HueIacError
from hue_iac.reconcile import Change, ChangeType


def test_circadian_resume_writes_control_file(tmp_path):
    import hue_iac.cli as climod
    cfgfile = tmp_path / "hue.yaml"
    ctrl = tmp_path / ".resume"
    cfgfile.write_text(
        "bridge: {host: x, application_key: k}\n"
        "location: {lat: 45.5, lon: -122.7, tz_offset_hours: -7}\n"
        "motion_policies: []\n"
        f"circadian_daemon: {{zone: 'Night Guide', manual_override: {{control_file: '{ctrl}'}}}}\n")
    rc = climod.Cli().run(["-c", str(cfgfile), "circadian", "resume"])
    assert rc == 0
    assert ctrl.exists()


def test_circadian_parser_has_run_and_resume():
    from hue_iac.cli import Cli
    args = Cli()._build_parser().parse_args(["circadian", "run"])
    assert args.command == "circadian" and args.circadian_cmd == "run"


def test_blocked_refusal_lists_each_real_reason():
    """The refusal message reports every blocked change accurately, not as 'unassigned lights'."""
    blocked = [
        Change("area.unassigned", "Lamp", ChangeType.BLOCKED,
               "light is not assigned to any declared room"),
        Change("smart_scene", "Golden hours", ChangeType.BLOCKED,
               "pruning every timeslot would empty the smart scene; keep at least one scheduled scene"),
        Change("night_motion", "Night Guide", ChangeType.BLOCKED,
               "zone 'Night Guide' not found — apply its areas.zones entry first"),
    ]
    msg = Cli._format_blocked(blocked)

    # Each blocked change shows its own resource, name and reason.
    assert "Lamp" in msg and "not assigned" in msg
    assert "Golden hours" in msg and "empty the smart scene" in msg
    assert "night_motion" in msg and "Night Guide" in msg
    # The escape hatch is still advertised.
    assert "--ignore-unassigned" in msg
    # The old misleading blanket message is gone.
    assert "light(s) are not assigned to any area" not in msg


def _write_cfg(tmp_path, security_block):
    cfg = tmp_path / "hue.yaml"
    cfg.write_text(
        "bridge:\n"
        "  host: 192.0.2.10\n"
        "  application_key: test-key\n"
        "location:\n"
        "  lat: 45.5\n"
        "  lon: -122.6\n"
        "  tz_offset_hours: -7\n"
        "motion_policies: []\n"
        + security_block
    )
    return cfg


def test_security_on_writes_on_file(tmp_path):
    on_file = tmp_path / ".sec-on"
    cfg = _write_cfg(tmp_path,
        "security:\n"
        "  groups:\n"
        "    - Night Guide\n"
        "  triggers:\n"
        "    control_file:\n"
        f"      on_file: {on_file}\n"
        f"      off_file: {tmp_path / '.sec-off'}\n")
    rc = Cli().run(["-c", str(cfg), "security", "on"])
    assert rc == 0 and on_file.exists()


def test_security_off_writes_off_file(tmp_path):
    off_file = tmp_path / ".sec-off"
    cfg = _write_cfg(tmp_path,
        "security:\n"
        "  groups:\n"
        "    - Night Guide\n"
        "  triggers:\n"
        "    control_file:\n"
        f"      on_file: {tmp_path / '.sec-on'}\n"
        f"      off_file: {off_file}\n")
    rc = Cli().run(["-c", str(cfg), "security", "off"])
    assert rc == 0 and off_file.exists()


def test_security_on_without_on_file_errors(tmp_path):
    cfg = _write_cfg(tmp_path, "security:\n  groups:\n    - Night Guide\n")
    rc = Cli().run(["-c", str(cfg), "security", "on"])
    assert rc == 1   # HueIacError -> caught by run() -> exit 1


def test_security_without_block_errors(tmp_path):
    cfg = _write_cfg(tmp_path, "")
    rc = Cli().run(["-c", str(cfg), "security", "status"])
    assert rc == 1


def test_security_off_without_off_file_errors(tmp_path):
    """security off fails (exit 1) when no control_file.off_file is configured."""
    cfg = _write_cfg(tmp_path,
        "security:\n"
        "  groups:\n"
        "    - Night Guide\n")
    rc = Cli().run(["-c", str(cfg), "security", "off"])
    assert rc == 1


def test_security_status_prints_config(tmp_path, capsys):
    """security status prints the configured groups and exits 0."""
    on_file = tmp_path / ".sec-on"
    off_file = tmp_path / ".sec-off"
    cfg = _write_cfg(tmp_path,
        "security:\n"
        "  groups:\n"
        "    - Night Guide\n"
        "    - TV Viewing\n"
        "  triggers:\n"
        "    control_file:\n"
        f"      on_file: {on_file}\n"
        f"      off_file: {off_file}\n")
    rc = Cli().run(["-c", str(cfg), "security", "status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Night Guide" in out
