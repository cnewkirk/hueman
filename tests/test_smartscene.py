"""Tests for sun-anchored smart_scene config + reconciliation."""

from __future__ import annotations

import datetime as _dt

import pytest

from hue_iac.config import Anchor, Config
from hue_iac.errors import ConfigError, HueIacError
from hue_iac.reconcile import Change, ChangeType, SmartSceneReconciler
from hue_iac.state import BridgeState
from hue_iac.sun import SolarCalculator


def _doc(smart_scenes):
    return {
        "bridge": {"host": "192.0.2.10", "application_key": "k"},
        "location": {"lat": 40.7128, "lon": -74.0060, "tz_offset_hours": -5},
        "motion_policies": [],
        "smart_scenes": smart_scenes,
    }


def test_smart_scene_schedule_parses_sun_anchors():
    cfg = Config.parse(_doc([
        {"name": "Golden hours", "schedule": {
            "Arise": "sunrise",
            "Unwind": "sunset",
            "Sleepy": "sunset+90m",
            "Storybook": "sunset-2h",
            "Nighttime": "23:30",
        }},
    ]))
    assert len(cfg.smart_scenes) == 1
    spec = cfg.smart_scenes[0]
    assert spec.name == "Golden hours"
    sched = dict(spec.schedule)
    assert sched["Arise"] == Anchor("sunrise", 0)
    assert sched["Unwind"] == Anchor("sunset", 0)
    assert sched["Sleepy"] == Anchor("sunset", 90)
    assert sched["Storybook"] == Anchor("sunset", -120)
    assert sched["Nighttime"] == Anchor("clock", 23 * 60 + 30)


def test_invalid_anchor_raises():
    with pytest.raises(ConfigError):
        Config.parse(_doc([{"name": "X", "schedule": {"A": "noon"}}]))


def test_anchor_resolves_against_suntimes():
    # sunrise 06:00 (=360 min), sunset 20:00 (=1200 min)
    assert Anchor("sunrise", 0).resolve(360.0, 1200.0) == 360
    assert Anchor("sunset", 90).resolve(360.0, 1200.0) == 1200 + 90
    assert Anchor("sunset", -120).resolve(360.0, 1200.0) == 1200 - 120
    assert Anchor("clock", 23 * 60 + 30).resolve(360.0, 1200.0) == 23 * 60 + 30
    # clamps into a single day
    assert 0 <= Anchor("sunrise", -1000).resolve(60.0, 1200.0) <= 1439


# --------------------------------------------------------------------------- #
# Reconciler
# --------------------------------------------------------------------------- #
class _FakeClient:
    def __init__(self, resources):
        self._r = resources
        self.updates = []

    def get_resources(self, rtype):
        return self._r.get(rtype, [])

    def update_resource(self, rtype, rid, body):
        self.updates.append((rtype, rid, body))


def _tslot(hour, minute, scene_rid):
    return {
        "start_time": {"kind": "time", "time": {"hour": hour, "minute": minute, "second": 0}},
        "target": {"rid": scene_rid, "rtype": "scene"},
    }


def _state_with_smartscene():
    scenes = [
        {"id": "scArise", "metadata": {"name": "Arise"}},
        {"id": "scUnwind", "metadata": {"name": "Unwind"}},
        {"id": "scNight", "metadata": {"name": "Nighttime"}},
    ]
    ss = {
        "id": "ss1",
        "metadata": {"name": "Golden hours"},
        "group": {"rid": "zAll", "rtype": "zone"},
        "week_timeslots": [{"timeslots": [
            _tslot(7, 0, "scArise"),
            _tslot(20, 0, "scUnwind"),
            _tslot(0, 0, "scNight"),
        ]}],
    }
    client = _FakeClient({"device": [], "room": [], "zone": [], "motion": [],
                          "scene": scenes, "smart_scene": [ss]})
    return client, BridgeState(client).load()  # type: ignore[arg-type]


DAY = _dt.date(2026, 6, 21)


def test_state_indexes_smart_scenes_and_scene_names():
    _, state = _state_with_smartscene()
    assert state.smart_scene("Golden hours")["id"] == "ss1"
    assert state.scene_name_for("scArise") == "Arise"


def test_reconciler_retimes_timeslots_to_real_sun(tmp_path):
    client, state = _state_with_smartscene()
    cfg = Config.parse(_doc([{"name": "Golden hours", "schedule": {
        "Arise": "sunrise", "Unwind": "sunset", "Nighttime": "23:30"}}]))
    rec = SmartSceneReconciler(client, state, cfg, for_date=DAY, backup_dir=str(tmp_path))

    change = rec.plan()[0]
    assert change.change_type is ChangeType.UPDATE  # 07:00 != sunrise on the solstice
    rec.apply(change)

    _, rid, body = client.updates[0]
    assert rid == "ss1"
    tslots = body["week_timeslots"][0]["timeslots"]
    sun = SolarCalculator(cfg.location.lat, cfg.location.lon, cfg.location.tz_offset_hours).sun_times(DAY)
    sr = int(round(sun.sunrise_min))
    arise = next(t for t in tslots if t["target"]["rid"] == "scArise")["start_time"]["time"]
    assert arise["hour"] * 60 + arise["minute"] == sr
    night = next(t for t in tslots if t["target"]["rid"] == "scNight")["start_time"]["time"]
    assert (night["hour"], night["minute"]) == (23, 30)  # clock anchor preserved


def test_reconciler_noop_when_times_already_match():
    client, state = _state_with_smartscene()
    # clock anchors equal to the existing 07:00 / 20:00 / 00:00 times
    cfg = Config.parse(_doc([{"name": "Golden hours", "schedule": {
        "Arise": "07:00", "Unwind": "20:00", "Nighttime": "00:00"}}]))
    rec = SmartSceneReconciler(client, state, cfg, for_date=DAY)
    assert rec.plan()[0].change_type is ChangeType.NOOP
    rec.apply(rec.plan()[0])
    assert client.updates == []  # NOOP must not write


def test_missing_smart_scene_is_blocked():
    client, state = _state_with_smartscene()
    cfg = Config.parse(_doc([{"name": "Nope", "schedule": {"A": "sunrise"}}]))
    rec = SmartSceneReconciler(client, state, cfg, for_date=DAY)
    assert rec.plan()[0].change_type is ChangeType.BLOCKED


def test_reconciler_prunes_timeslots_not_in_schedule(tmp_path):
    """Dropping a scene from the config schedule removes its timeslot on apply."""
    client, state = _state_with_smartscene()
    cfg = Config.parse(_doc([{"name": "Golden hours", "schedule": {
        "Arise": "07:00", "Unwind": "20:00"}}]))  # Nighttime omitted -> prune it
    rec = SmartSceneReconciler(client, state, cfg, for_date=DAY, backup_dir=str(tmp_path))
    change = rec.plan()[0]
    assert change.change_type is ChangeType.UPDATE
    rec.apply(change)
    _, _, body = client.updates[0]
    scene_rids = {t["target"]["rid"] for t in body["week_timeslots"][0]["timeslots"]}
    assert scene_rids == {"scArise", "scUnwind"}  # scNight pruned


# --------------------------------------------------------------------------- #
# Prune safety: empty-floor guard, unresolved-rid preservation, backup
# --------------------------------------------------------------------------- #
def _state_with_ghost():
    """A smart_scene with one timeslot whose scene rid is NOT indexed."""
    scenes = [
        {"id": "scArise", "metadata": {"name": "Arise"}},
        {"id": "scUnwind", "metadata": {"name": "Unwind"}},
    ]  # deliberately no scene named/iding "scGhost"
    ss = {
        "id": "ssG",
        "metadata": {"name": "Golden hours"},
        "group": {"rid": "zAll", "rtype": "zone"},
        "week_timeslots": [{"timeslots": [
            _tslot(7, 0, "scArise"),
            _tslot(20, 0, "scUnwind"),
            _tslot(0, 0, "scGhost"),   # unresolvable: rid not in the scene index
        ]}],
    }
    client = _FakeClient({"device": [], "room": [], "zone": [], "motion": [],
                          "scene": scenes, "smart_scene": [ss]})
    return client, BridgeState(client).load()


def test_unresolved_timeslot_preserved_is_noop(tmp_path):
    client, state = _state_with_ghost()
    # Anchors equal the existing 07:00/20:00 -> no real diff; ghost is preserved.
    cfg = Config.parse(_doc([{"name": "Golden hours", "schedule": {
        "Arise": "07:00", "Unwind": "20:00"}}]))
    rec = SmartSceneReconciler(client, state, cfg, for_date=DAY, backup_dir=str(tmp_path))
    change = rec.plan()[0]
    assert change.change_type is ChangeType.NOOP
    assert "unrecognized" in change.summary
    rec.apply(change)
    assert client.updates == []  # NOOP must not write


def test_unresolved_timeslot_kept_on_apply(tmp_path):
    client, state = _state_with_ghost()
    # Force a real re-time of Arise (sunrise != 07:00 on the solstice) -> UPDATE.
    cfg = Config.parse(_doc([{"name": "Golden hours", "schedule": {
        "Arise": "sunrise", "Unwind": "20:00"}}]))
    rec = SmartSceneReconciler(client, state, cfg, for_date=DAY, backup_dir=str(tmp_path))
    change = rec.plan()[0]
    assert change.change_type is ChangeType.UPDATE
    rec.apply(change)
    _, _, body = client.updates[0]
    scene_rids = {t["target"]["rid"] for t in body["week_timeslots"][0]["timeslots"]}
    assert "scGhost" in scene_rids  # unresolvable timeslot preserved, never dropped


def test_empty_floor_is_blocked(tmp_path):
    client, state = _state_with_smartscene()  # live: Arise/Unwind/Nighttime
    # Schedule names a scene matching NO live timeslot -> every slot would prune.
    cfg = Config.parse(_doc([{"name": "Golden hours", "schedule": {"Daytime": "07:00"}}]))
    rec = SmartSceneReconciler(client, state, cfg, for_date=DAY, backup_dir=str(tmp_path))
    change = rec.plan()[0]
    assert change.change_type is ChangeType.BLOCKED
    rec.apply(change)
    assert client.updates == []  # BLOCKED is not actionable -> no write


def test_apply_refuses_to_empty_smart_scene(tmp_path):
    client, state = _state_with_smartscene()
    cfg = Config.parse(_doc([{"name": "Golden hours", "schedule": {"Daytime": "07:00"}}]))
    rec = SmartSceneReconciler(client, state, cfg, for_date=DAY, backup_dir=str(tmp_path))
    # Bypass plan's BLOCKED with a hand-crafted actionable change to hit the guard.
    forced = Change("smart_scene", "Golden hours", ChangeType.UPDATE, "forced")
    with pytest.raises(HueIacError):
        rec.apply(forced)
    assert client.updates == []


def test_backup_written_before_put(tmp_path):
    client, state = _state_with_smartscene()
    cfg = Config.parse(_doc([{"name": "Golden hours", "schedule": {
        "Arise": "sunrise", "Unwind": "20:00", "Nighttime": "00:00"}}]))
    rec = SmartSceneReconciler(client, state, cfg, for_date=DAY, backup_dir=str(tmp_path))
    rec.apply(rec.plan()[0])
    assert any(p.name == "ss1-preapply.json" for p in tmp_path.iterdir())


def test_backup_is_write_once(tmp_path):
    client, state = _state_with_smartscene()
    cfg = Config.parse(_doc([{"name": "Golden hours", "schedule": {
        "Arise": "sunrise", "Unwind": "20:00", "Nighttime": "00:00"}}]))
    backup = tmp_path / "ss1-preapply.json"
    backup.write_text("ORIGINAL")  # a prior apply already saved the true original
    rec = SmartSceneReconciler(client, state, cfg, for_date=DAY, backup_dir=str(tmp_path))
    rec.apply(rec.plan()[0])
    assert backup.read_text() == "ORIGINAL"  # write-once: never overwrites
