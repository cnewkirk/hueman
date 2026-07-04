"""Tests for the pure night-motion helpers (scene body + automation transform)."""

from __future__ import annotations

import json
import pathlib
from dataclasses import replace

import pytest

from hueman.config import Config
from hueman.errors import ConfigError
from hueman.nightmotion import scene_actions_match, scene_body, transform_automation
from hueman.reconcile import ChangeType, NightMotionReconciler
from hueman.state import BridgeState

FIXTURE = json.loads(
    (pathlib.Path(__file__).parent / "fixtures" / "main_room_behavior.json").read_text()
)


# -- scene_body -------------------------------------------------------------- #
def test_scene_body_red_xy_low_brightness():
    body = scene_body("Night Guide — Night", "ZONE", ["l1", "l2"], hex="#ff1400", brightness=3.0)
    assert body["metadata"]["name"] == "Night Guide — Night"
    assert body["group"] == {"rid": "ZONE", "rtype": "zone"}
    assert len(body["actions"]) == 2
    act = body["actions"][0]["action"]
    assert act["on"] == {"on": True}
    assert act["dimming"] == {"brightness": 3.0}
    assert "xy" in act["color"] and act["color"]["xy"]["x"] > 0.6  # deep red
    assert "color_temperature" not in act


def test_scene_body_color_temperature_variant():
    body = scene_body("Night Guide — Day", "ZONE", ["l1"], mirek=233, brightness=100.0)
    act = body["actions"][0]["action"]
    assert act["color_temperature"] == {"mirek": 233}
    assert "color" not in act
    assert act["dimming"] == {"brightness": 100.0}


def test_scene_body_off_turns_every_light_off():
    """on=False produces a pure off action — no dimming/colour — for the hand-off."""
    body = scene_body("Golden hours — 6", "ZONE", ["l1", "l2"], on=False)
    assert len(body["actions"]) == 2
    for entry in body["actions"]:
        assert entry["action"] == {"on": {"on": False}}


# -- scene_actions_match ----------------------------------------------------- #
def _act(rid, *, on=True, bri=None, xy=None, mirek=None):
    """Build one scene action (target + action) for matcher tests."""
    action = {"on": {"on": on}}
    if bri is not None:
        action["dimming"] = {"brightness": bri}
    if xy is not None:
        action["color"] = {"xy": {"x": xy[0], "y": xy[1]}}
    if mirek is not None:
        action["color_temperature"] = {"mirek": mirek}
    return {"target": {"rid": rid, "rtype": "light"}, "action": action}


def test_scene_actions_match_identical():
    live = [_act("l1", bri=3.0, xy=(0.69, 0.30)), _act("l2", bri=3.0, xy=(0.69, 0.30))]
    desired = [_act("l1", bri=3.0, xy=(0.69, 0.30)), _act("l2", bri=3.0, xy=(0.69, 0.30))]
    assert scene_actions_match(live, desired) is True


def test_scene_actions_match_order_insensitive():
    live = [_act("l2", bri=3.0, xy=(0.69, 0.30)), _act("l1", bri=3.0, xy=(0.69, 0.30))]
    desired = [_act("l1", bri=3.0, xy=(0.69, 0.30)), _act("l2", bri=3.0, xy=(0.69, 0.30))]
    assert scene_actions_match(live, desired) is True


def test_scene_actions_match_brightness_drift_detected():
    live = [_act("l1", bri=3.0, xy=(0.69, 0.30))]
    desired = [_act("l1", bri=5.0, xy=(0.69, 0.30))]
    assert scene_actions_match(live, desired) is False


def test_scene_actions_match_brightness_within_tolerance():
    # 3.0 vs 3.16 (percent/8-bit quantization) is inside the 0.5 tolerance.
    live = [_act("l1", bri=3.16, xy=(0.69, 0.30))]
    desired = [_act("l1", bri=3.0, xy=(0.69, 0.30))]
    assert scene_actions_match(live, desired) is True


def test_scene_actions_match_xy_within_tolerance():
    live = [_act("l1", bri=3.0, xy=(0.6971, 0.3022))]
    desired = [_act("l1", bri=3.0, xy=(0.6975, 0.3025))]  # < 1e-3 per coordinate
    assert scene_actions_match(live, desired) is True


def test_scene_actions_match_xy_drift_detected():
    live = [_act("l1", bri=3.0, xy=(0.69, 0.30))]
    desired = [_act("l1", bri=3.0, xy=(0.55, 0.40))]  # a real hue change
    assert scene_actions_match(live, desired) is False


def test_scene_actions_match_rid_set_mismatch():
    live = [_act("l1", bri=3.0, xy=(0.69, 0.30))]
    desired = [_act("l1", bri=3.0, xy=(0.69, 0.30)), _act("l2", bri=3.0, xy=(0.69, 0.30))]
    assert scene_actions_match(live, desired) is False


def test_scene_actions_match_colour_kind_mismatch():
    live = [_act("l1", bri=100.0, mirek=233)]
    desired = [_act("l1", bri=100.0, xy=(0.45, 0.40))]
    assert scene_actions_match(live, desired) is False


def test_scene_actions_match_mirek_exact():
    assert scene_actions_match([_act("l1", bri=100.0, mirek=233)],
                               [_act("l1", bri=100.0, mirek=346)]) is False
    assert scene_actions_match([_act("l1", bri=100.0, mirek=233)],
                               [_act("l1", bri=100.0, mirek=233)]) is True


def test_scene_actions_match_int_float_coercion():
    # The bridge may echo ints (brightness 3, xy 0); the matcher must coerce.
    live = [_act("l1", bri=3, xy=(0.69, 0.30))]
    desired = [_act("l1", bri=3.0, xy=(0.69, 0.30))]
    assert scene_actions_match(live, desired) is True


def test_scene_actions_match_off_ignores_dimming_and_colour():
    # Two 'off' looks match even if the bridge retained stale dimming/colour, so a
    # daily re-apply of the off hand-off scene stays idempotent (no false drift).
    live = [{"target": {"rid": "l1", "rtype": "light"},
             "action": {"on": {"on": False}, "dimming": {"brightness": 42.0},
                        "color_temperature": {"mirek": 300}}}]
    desired = scene_body("x", "Z", ["l1"], on=False)["actions"]
    assert scene_actions_match(live, desired) is True


def test_scene_actions_match_off_vs_on_is_mismatch():
    live = [_act("l1", on=True, bri=10.0)]
    desired = scene_body("x", "Z", ["l1"], on=False)["actions"]
    assert scene_actions_match(live, desired) is False


def test_scene_actions_match_defensive_against_missing_keys():
    # A live action missing dimming/colour must not raise (returns mismatch).
    live = [{"target": {"rid": "l1"}, "action": {"on": {"on": True}}}]
    desired = [_act("l1", bri=3.0, xy=(0.69, 0.30))]
    assert scene_actions_match(live, desired) is False


def test_scene_actions_match_real_fixture_round_trips():
    """The matcher returns True against actually bridge-stored scene actions."""
    fix = json.loads(
        (pathlib.Path(__file__).parent / "fixtures" / "night_guide_night_scene.json").read_text()
    )
    rids = [a["target"]["rid"] for a in fix["actions"]]
    desired = scene_body(fix["metadata"]["name"], fix["group"]["rid"], rids,
                         hex="ff1400", brightness=3.0)
    assert scene_actions_match(fix["actions"], desired["actions"]) is True


# -- transform_automation ---------------------------------------------------- #
def _transformed():
    return transform_automation(
        FIXTURE["configuration"],
        zone_rid="ZONE",
        day_scene="DAY",
        evening_scene="EVE",
        night_scene="RED",
        night_start=(22, 34),
        night_off_min=3,
    )


def test_transform_points_where_at_single_zone():
    cfg = _transformed()
    assert cfg["motion"]["where"] == [{"group": {"rid": "ZONE", "rtype": "zone"}}]


def test_transform_night_timeslot_rewritten():
    cfg = _transformed()
    slots = {s["start_time"]["time"]["hour"]: s for s in cfg["motion"]["when"]["timeslots"]}
    # the former 23:00 night slot now starts 22:34, recalls RED, off after 3 min
    night = slots[22]
    assert night["start_time"]["time"] == {"hour": 22, "minute": 34}
    assert [a["action"]["recall"]["rid"] for a in night["on_motion"]["recall_single"]] == ["RED"]
    assert night["on_no_motion"]["after"] == {"minutes": 3}
    assert night["on_no_motion"]["recall_single"] == [{"action": "all_off"}]


def test_transform_preserves_day_and_evening_slots_retargeted():
    cfg = _transformed()
    slots = {s["start_time"]["time"]["hour"]: s for s in cfg["motion"]["when"]["timeslots"]}
    # day 07:00 keeps its start + no-auto-off, retargeted to the DAY zone scene
    day = slots[7]
    assert [a["action"]["recall"]["rid"] for a in day["on_motion"]["recall_single"]] == ["DAY"]
    assert "on_no_motion" not in day
    # evening 17:00 keeps its 15-min off, retargeted to EVE, all_off single
    eve = slots[17]
    assert [a["action"]["recall"]["rid"] for a in eve["on_motion"]["recall_single"]] == ["EVE"]
    assert eve["on_no_motion"]["after"] == {"minutes": 15}
    assert eve["on_no_motion"]["recall_single"] == [{"action": "all_off"}]


def test_transform_preserves_gate_sensor_and_source():
    cfg = _transformed()
    assert cfg["light_level"] == FIXTURE["configuration"]["light_level"]  # daylight gate intact
    assert cfg["motion"]["motion_service"] == FIXTURE["configuration"]["motion"]["motion_service"]
    assert cfg["source"] == FIXTURE["configuration"]["source"]


def test_transform_does_not_mutate_input():
    before = json.dumps(FIXTURE["configuration"])
    _transformed()
    assert json.dumps(FIXTURE["configuration"]) == before


# -- NightMotionReconciler --------------------------------------------------- #
class _RecFakeClient:
    def __init__(self, resources):
        self._r = resources
        self.updates = []
        self.creates = []

    def get_resources(self, rtype):
        return self._r.get(rtype, [])

    def update_resource(self, rtype, rid, body):
        self.updates.append((rtype, rid, body))

    def create_resource(self, rtype, body):
        self.creates.append((rtype, body))
        if rtype == "scene":
            return "rid-" + body["metadata"]["name"].split("—")[-1].strip()
        return "new-rid"


def _night_cfg_doc(*, mode: str | None = None):
    """Single source of truth for the night_motion config doc.

    Both `_night_cfg()` and `_night_cfg_with_mode()` build from this so they can't
    silently diverge when a field is added. When `mode` is given it's injected so
    the result still flows through `Config.parse` (and its mode validation).
    """
    nm = {
        "automation": "Main Room", "zone": "Night Guide", "start": "22:34", "timeout": "3m",
        "day": {"mirek": 233, "brightness": 100},
        "evening": {"mirek": 346, "brightness": 100},
        "night": {"hex": "#ff1400", "brightness": 3},
    }
    if mode is not None:
        nm["mode"] = mode
    return {
        "bridge": {"host": "x", "application_key": "k"},
        "location": {"lat": 45.5, "lon": -122.7, "tz_offset_hours": -7},
        "motion_policies": [],
        "night_motion": nm,
    }


def _night_cfg():
    return Config.parse(_night_cfg_doc())


def _night_state(with_zone=True):
    resources = {"device": [], "room": [], "zone": [], "scene": [], "motion": [],
                 "behavior_instance": [json.loads(json.dumps(FIXTURE))]}
    if with_zone:
        resources["zone"] = [{
            "id": "zNG", "metadata": {"name": "Night Guide"},
            "children": [{"rtype": "light", "rid": "litA"}, {"rtype": "light", "rid": "litB"}],
            "services": [],
        }]
    client = _RecFakeClient(resources)
    return client, BridgeState(client).load()


def test_night_motion_creates_scenes_and_retargets(tmp_path):
    client, state = _night_state()
    rec = NightMotionReconciler(client, state, _night_cfg(), backup_dir=str(tmp_path))
    change = rec.plan()[0]
    assert change.change_type is ChangeType.UPDATE
    rec.apply(change)

    scene_creates = [b for (t, b) in client.creates if t == "scene"]
    assert {b["metadata"]["name"] for b in scene_creates} == {
        "Night Guide — Day", "Night Guide — Evening", "Night Guide — Night"}
    assert all(b["group"] == {"rid": "zNG", "rtype": "zone"} and len(b["actions"]) == 2
               for b in scene_creates)

    bi = [(rid, b) for (t, rid, b) in client.updates if t == "behavior_instance"]
    assert bi
    put = bi[0][1]["configuration"]
    assert put["motion"]["where"] == [{"group": {"rid": "zNG", "rtype": "zone"}}]
    slots = {s["start_time"]["time"]["hour"]: s for s in put["motion"]["when"]["timeslots"]}
    assert slots[22]["on_motion"]["recall_single"][0]["action"]["recall"]["rid"] == "rid-Night"
    assert any(p.name.endswith("preapply.json") for p in tmp_path.iterdir())  # backup written


def test_night_motion_blocked_without_zone(tmp_path):
    client, state = _night_state(with_zone=False)
    rec = NightMotionReconciler(client, state, _night_cfg(), backup_dir=str(tmp_path))
    assert rec.plan()[0].change_type is ChangeType.BLOCKED


# -- drift detection (MUST-FIX 2) -------------------------------------------- #
def _scene_payload(role, sid, zone_rid, light_rids, look):
    body = scene_body(f"Night Guide — {role}", zone_rid, light_rids,
                      mirek=look.mirek, hex=look.hex, brightness=look.brightness)
    return {"id": sid, "metadata": {"name": f"Night Guide — {role}"},
            "group": {"rid": zone_rid, "rtype": "zone"}, "actions": body["actions"]}


def _converged_state():
    """A bridge already fully converged to _night_cfg() -> plan should be NOOP."""
    cfg = _night_cfg()
    nm = cfg.night_motion
    zone_rid, lights = "zNG", ["litA", "litB"]
    rids = {"Day": "scDay", "Evening": "scEve", "Night": "scNight"}
    looks = {"Day": nm.day, "Evening": nm.evening, "Night": nm.night}
    scenes = [_scene_payload(r, rids[r], zone_rid, lights, looks[r])
              for r in ("Day", "Evening", "Night")]
    inst_cfg = transform_automation(
        FIXTURE["configuration"], zone_rid=zone_rid,
        day_scene=rids["Day"], evening_scene=rids["Evening"], night_scene=rids["Night"],
        night_start=nm.start, night_off_min=nm.timeout_min)
    inst = {"id": "bi1", "metadata": {"name": "Main Room"}, "configuration": inst_cfg}
    resources = {"device": [], "room": [], "motion": [],
                 "zone": [{"id": zone_rid, "metadata": {"name": "Night Guide"},
                           "children": [{"rtype": "light", "rid": "litA"},
                                        {"rtype": "light", "rid": "litB"}], "services": []}],
                 "scene": scenes, "behavior_instance": [inst]}
    client = _RecFakeClient(resources)
    return client, BridgeState(client).load(), cfg


def test_night_motion_converged_is_noop(tmp_path):
    client, state, cfg = _converged_state()
    rec = NightMotionReconciler(client, state, cfg, backup_dir=str(tmp_path))
    assert rec.plan()[0].change_type is ChangeType.NOOP


def test_night_motion_scene_look_drift_is_update(tmp_path):
    client, state, cfg = _converged_state()
    nm = cfg.night_motion
    drifted_cfg = replace(cfg, night_motion=replace(
        nm, night=replace(nm.night, brightness=8.0)))  # edit YAML look; bridge still says 3%
    rec = NightMotionReconciler(client, state, drifted_cfg, backup_dir=str(tmp_path))
    change = rec.plan()[0]
    assert change.change_type is ChangeType.UPDATE
    assert "look" in change.summary.lower()


def test_night_motion_apply_skips_unchanged_automation(tmp_path):
    client, state, cfg = _converged_state()
    nm = cfg.night_motion
    drifted_cfg = replace(cfg, night_motion=replace(
        nm, night=replace(nm.night, brightness=8.0)))  # look-only change, wiring identical
    rec = NightMotionReconciler(client, state, drifted_cfg, backup_dir=str(tmp_path))
    rec.apply(rec.plan()[0])
    assert any(u[0] == "scene" for u in client.updates)          # scene look rewritten
    assert not any(u[0] == "behavior_instance" for u in client.updates)  # wiring not re-PUT
    assert list(tmp_path.iterdir()) == []                         # no backup when wiring unchanged


# -- night_only mode --------------------------------------------------------- #
def _night_cfg_with_mode(mode: str):
    # Compose from the shared doc and flow through Config.parse, so this can't
    # diverge from _night_cfg() and the parse-time mode validation still runs
    # (dataclasses.replace would bypass that validation).
    return Config.parse(_night_cfg_doc(mode=mode))


def test_transform_night_only_emits_three_slots_wrapping_midnight():
    # night_only drops day/evening and emits THREE slots: a 00:00 clone and the
    # night_start slot carry the red guidance; an actionless day_start slot bounds
    # the 00:00 slot's wrap so dark evenings before night_start do nothing. (A lone
    # slot does not wrap past midnight; with only 00:00 + night_start the 00:00
    # slot governs 00:00 -> night_start, i.e. red recalls + all_off from sunset.)
    cfg = transform_automation(
        FIXTURE["configuration"], zone_rid="ZONE",
        day_scene="DAY", evening_scene="EVE", night_scene="RED",
        night_start=(22, 34), night_off_min=3, night_only=True)
    slots = cfg["motion"]["when"]["timeslots"]
    starts = [(s["start_time"]["time"]["hour"], s["start_time"]["time"].get("minute", 0))
              for s in slots]
    assert starts == [(0, 0), (8, 0), (22, 34)]  # chronological — the bridge expects it
    red = [slots[0], slots[2]]
    for s in red:  # both night slots recall RED and switch off after night_off_min
        assert [a["action"]["recall"]["rid"] for a in s["on_motion"]["recall_single"]] == ["RED"]
        assert s["on_no_motion"]["after"] == {"minutes": 3}
        assert s["on_no_motion"]["recall_single"] == [{"action": "all_off"}]
    assert cfg["motion"]["where"] == [{"group": {"rid": "ZONE", "rtype": "zone"}}]


def test_transform_night_only_noop_slot_has_no_actions():
    cfg = transform_automation(
        FIXTURE["configuration"], zone_rid="ZONE",
        day_scene="DAY", evening_scene="EVE", night_scene="RED",
        night_start=(22, 34), night_off_min=3, night_only=True)
    noop = cfg["motion"]["when"]["timeslots"][1]
    # The bridge schema requires on_motion with >=1 action on every timeslot
    # (probed live 2026-07-03: a slot without it, or with an empty recall list,
    # is rejected). "do_nothing" is the schema's explicit no-op action and
    # round-trips verbatim, so this exact shape is load-bearing for the
    # reconciler's exact-== NOOP check.
    assert noop == {
        "start_time": {"time": {"hour": 8, "minute": 0}, "type": "time"},
        "on_motion": {"recall_single": [{"action": "do_nothing"}]},
    }
    assert "on_no_motion" not in noop


def test_transform_night_only_custom_day_start():
    cfg = transform_automation(
        FIXTURE["configuration"], zone_rid="ZONE",
        day_scene="DAY", evening_scene="EVE", night_scene="RED",
        night_start=(22, 34), night_off_min=3, night_only=True, day_start=(9, 15))
    noop = cfg["motion"]["when"]["timeslots"][1]
    assert noop["start_time"]["time"] == {"hour": 9, "minute": 15}


def test_transform_night_only_round_trips_own_output():
    # The reconciler NOOPs on exact ==, so feeding the transform its own output
    # must be a fixed point — otherwise every plan would re-report UPDATE.
    kwargs = dict(zone_rid="ZONE", day_scene="DAY", evening_scene="EVE",
                  night_scene="RED", night_start=(22, 34), night_off_min=3,
                  night_only=True)
    once = transform_automation(FIXTURE["configuration"], **kwargs)
    twice = transform_automation(once, **kwargs)
    assert twice == once


def test_night_motion_spec_mode_parsed():
    cfg = _night_cfg_with_mode("night_only")
    assert cfg.night_motion.mode == "night_only"


def test_night_motion_mode_invalid_raises():
    with pytest.raises(ConfigError, match="mode must be"):
        _night_cfg_with_mode("invalid")


def test_night_motion_day_start_defaults_to_0800():
    cfg = _night_cfg_with_mode("night_only")
    assert cfg.night_motion.day_start == (8, 0)


def test_night_motion_day_start_custom():
    doc = _night_cfg_doc(mode="night_only")
    doc["night_motion"]["day_start"] = "09:15"
    cfg = Config.parse(doc)
    assert cfg.night_motion.day_start == (9, 15)


def test_night_motion_day_start_must_precede_start():
    doc = _night_cfg_doc(mode="night_only")
    doc["night_motion"]["day_start"] = "23:00"  # after the 22:34 night start
    with pytest.raises(ConfigError, match="day_start"):
        Config.parse(doc)


def test_night_motion_day_start_midnight_rejected():
    doc = _night_cfg_doc(mode="night_only")
    doc["night_motion"]["day_start"] = "00:00"  # would collide with the midnight clone
    with pytest.raises(ConfigError, match="day_start"):
        Config.parse(doc)


def test_night_motion_day_start_sun_anchor_rejected():
    doc = _night_cfg_doc(mode="night_only")
    doc["night_motion"]["day_start"] = "sunrise"
    with pytest.raises(ConfigError, match="day_start"):
        Config.parse(doc)


# -- sun-anchored timeslots must not crash the planner (2026-07-02 review) ---- #
def _sun_anchored_fixture():
    """FIXTURE with one timeslot re-anchored to sunset (no clock 'time' mapping)."""
    fx = json.loads(json.dumps(FIXTURE))
    fx["configuration"]["motion"]["when"]["timeslots"][0]["start_time"] = {
        "type": "sunset"
    }
    return fx


def test_transform_rejects_sun_anchored_timeslot_with_clear_error():
    """A sunrise/sunset-anchored slot can't be ordered by clock minute; the
    transform must raise a typed, descriptive error -- not a bare KeyError."""
    fx = _sun_anchored_fixture()
    with pytest.raises(ValueError, match="sun-anchored"):
        transform_automation(
            fx["configuration"], zone_rid="Z", day_scene="d",
            evening_scene="e", night_scene="n",
        )


def test_plan_is_blocked_not_crashed_on_sun_anchored_timeslot(tmp_path):
    """The planner surfaces a sun-anchored timeslot as BLOCKED (with a reason),
    never as an exception out of plan()."""
    resources = {"device": [], "room": [], "scene": [], "motion": [],
                 "behavior_instance": [_sun_anchored_fixture()],
                 "zone": [{
                     "id": "zNG", "metadata": {"name": "Night Guide"},
                     "children": [{"rtype": "light", "rid": "litA"}],
                     "services": [],
                 }]}
    client = _RecFakeClient(resources)
    state = BridgeState(client).load()
    rec = NightMotionReconciler(client, state, _night_cfg(), backup_dir=str(tmp_path))
    change = rec.plan()[0]
    assert change.change_type is ChangeType.BLOCKED
    assert "sun-anchored" in change.summary


# -- actionless slot in full mode + day_start ordering (review findings) ----- #
def test_transform_full_mode_rejects_actionless_slot():
    """A live 3-slot night_only shape fed back through full mode must raise a
    descriptive ValueError, not a bare KeyError('on_motion')."""
    night_only_cfg = transform_automation(
        FIXTURE["configuration"], zone_rid="ZONE",
        day_scene="DAY", evening_scene="EVE", night_scene="RED",
        night_start=(22, 34), night_off_min=3, night_only=True)
    with pytest.raises(ValueError, match="full mode"):
        transform_automation(
            night_only_cfg, zone_rid="ZONE",
            day_scene="DAY", evening_scene="EVE", night_scene="RED",
            night_start=(22, 34), night_off_min=3, night_only=False)


def test_plan_is_blocked_not_crashed_on_actionless_slot_in_full_mode(tmp_path):
    """Someone applies night_only, then flips mode back to full: plan() must
    surface this as BLOCKED, never crash with a bare KeyError."""
    night_only_cfg = transform_automation(
        FIXTURE["configuration"], zone_rid="zNG",
        day_scene="d", evening_scene="e", night_scene="n",
        night_start=(22, 34), night_off_min=3, night_only=True)
    inst = {"id": "bi1", "metadata": {"name": "Main Room"}, "configuration": night_only_cfg}
    resources = {"device": [], "room": [], "scene": [], "motion": [],
                 "behavior_instance": [inst],
                 "zone": [{
                     "id": "zNG", "metadata": {"name": "Night Guide"},
                     "children": [{"rtype": "light", "rid": "litA"}],
                     "services": [],
                 }]}
    client = _RecFakeClient(resources)
    state = BridgeState(client).load()
    rec = NightMotionReconciler(client, state, _night_cfg(), backup_dir=str(tmp_path))
    change = rec.plan()[0]
    assert change.change_type is ChangeType.BLOCKED
    assert "full mode" in change.summary


def test_transform_night_only_day_start_after_night_start_raises():
    """A direct caller with night_start before the default day_start would emit
    non-chronological slots; transform_automation must guard this itself."""
    with pytest.raises(ValueError, match="day_start"):
        transform_automation(
            FIXTURE["configuration"], zone_rid="ZONE",
            day_scene="DAY", evening_scene="EVE", night_scene="RED",
            night_start=(7, 0), night_off_min=3, night_only=True)


def test_night_motion_reconciler_passes_day_start_through(tmp_path):
    # Use a NON-default day_start: with the transform's own default (8, 0) this
    # test would pass even without the reconciler pass-through.
    doc = _night_cfg_doc(mode="night_only")
    doc["night_motion"]["day_start"] = "09:15"
    client, state = _night_state()
    rec = NightMotionReconciler(client, state, Config.parse(doc), backup_dir=str(tmp_path))
    rec.apply(rec.plan()[0])
    bi = [(rid, b) for (t, rid, b) in client.updates if t == "behavior_instance"]
    assert bi
    slots = bi[0][1]["configuration"]["motion"]["when"]["timeslots"]
    starts = [(s["start_time"]["time"]["hour"], s["start_time"]["time"].get("minute", 0))
              for s in slots]
    assert starts == [(0, 0), (9, 15), (22, 34)]
    assert slots[1]["on_motion"] == {"recall_single": [{"action": "do_nothing"}]}
    assert "on_no_motion" not in slots[1]
