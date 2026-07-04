"""Tests for the pure circadian-scene timeslot generator."""

from __future__ import annotations

import datetime as _dt
import pytest

from hue_iac.circadian import CircadianCurve, CircadianParams
from hue_iac.circadian_scene import MAX_TIMESLOTS, circadian_timeslots
from hue_iac.config import Config
from hue_iac.nightmotion import scene_body
from hue_iac.reconcile import ChangeType, CircadianSceneReconciler
from hue_iac.state import BridgeState
from hue_iac.sun import SolarCalculator

SOLAR = SolarCalculator(40.7128, -74.0060, -4)  # NYC, EDT
PARAMS = CircadianParams()     # day 233/100, evening 370/60, night 454/15
HAND_OFF = 22 * 60 + 34        # 22:34
DAY = _dt.date(2026, 6, 21)    # solstice; stable sunrise/sunset


def _steps():
    return circadian_timeslots(PARAMS, SOLAR, DAY, hand_off_min=HAND_OFF)


def test_generates_at_most_six_unique_chronological_steps():
    steps = _steps()
    assert 1 <= len(steps) <= MAX_TIMESLOTS
    minutes = [s.minute for s in steps]
    assert minutes == sorted(minutes)
    assert len(set(minutes)) == len(minutes)


def test_peak_step_is_the_day_look():
    peak = max(_steps(), key=lambda s: s.brightness)
    assert (peak.mirek, peak.brightness) == (PARAMS.day_mirek, PARAMS.day_brightness)


def test_afternoon_steps_decline_strictly():
    sun = SOLAR.sun_times(DAY)
    solar_noon = (sun.sunrise_min + sun.sunset_min) / 2.0
    bris = [s.brightness for s in _steps() if solar_noon <= s.minute <= sun.sunset_min]
    assert len(bris) >= 3
    assert all(bris[i] > bris[i + 1] for i in range(len(bris) - 1))


def test_every_step_look_matches_the_curve():
    curve = CircadianCurve(PARAMS)
    noon = SOLAR.noon_elevation(DAY)
    for step in _steps():
        if not step.on:
            continue  # the hand-off step is an explicit off, not a curve sample
        want = curve.state_at(SOLAR.solar_elevation(DAY, step.minute), noon)
        assert (step.mirek, step.brightness) == (want.mirek, want.brightness)


def test_hand_off_knee_is_off():
    """The day cycle's final (hand-off) knee turns the zone OFF so night_motion
    owns the whole night window with no competing on-step."""
    steps = _steps()
    hand_off = max(steps, key=lambda s: s.minute)
    assert hand_off.minute == HAND_OFF            # 22:34 is the latest knee
    assert hand_off.on is False                   # ...and it is off
    assert all(s.on for s in steps if s.minute != hand_off.minute)  # every day knee stays on


def test_polar_day_falls_back_to_a_single_step():
    polar = SolarCalculator(78.0, 15.0, 1)  # Svalbard
    steps = circadian_timeslots(PARAMS, polar, _dt.date(2026, 6, 21), hand_off_min=HAND_OFF)
    assert len(steps) == 1


def test_steps_clamp_into_a_valid_day():
    for step in _steps():
        assert 0 <= step.minute <= 1439
        assert 0.0 <= step.brightness <= 100.0


# --------------------------------------------------------------------------- #
# CircadianSceneReconciler
# --------------------------------------------------------------------------- #
class _FakeClient:
    def __init__(self, resources):
        self._r = resources
        self.updates = []
        self.creates = []
        self.deletes = []

    def get_resources(self, rtype):
        return self._r.get(rtype, [])

    def update_resource(self, rtype, rid, body):
        self.updates.append((rtype, rid, body))

    def create_resource(self, rtype, body):
        self.creates.append((rtype, body))
        return f"new-{rtype}-{len(self.creates)}"

    def delete_resource(self, rtype, rid):
        self.deletes.append((rtype, rid))


def _zone(rid, name, light_rids):
    return {"id": rid, "metadata": {"name": name}, "services": [],
            "children": [{"rtype": "light", "rid": r} for r in light_rids]}


def _cs_cfg(**overrides):
    block = {"smart_scene": "Golden hours", "zone": "Night Guide",
             "transition": "ramp", "hand_off": "22:34"}
    block.update(overrides)
    return Config.parse({
        "bridge": {"host": "x", "application_key": "k"},
        "location": {"lat": 45.5, "lon": -122.7, "tz_offset_hours": -7},
        "motion_policies": [],
        "circadian_scene": block,
    })


def _ramp_ms(cfg):
    return int(cfg.circadian.ramp_minutes * 60_000)


def _converged(cfg, zone_rid, lights):
    """Build the scenes + smart_scene exactly as apply would, so plan -> NOOP."""
    solar = SolarCalculator(cfg.location.lat, cfg.location.lon, cfg.location.tz_offset_hours)
    steps = circadian_timeslots(cfg.circadian, solar, DAY, hand_off_min=cfg.circadian_scene.hand_off_min)
    scenes, rids = [], []
    for i, step in enumerate(steps):
        name = f"{cfg.circadian_scene.smart_scene} — {i + 1}"
        body = scene_body(name, zone_rid, lights, mirek=step.mirek, brightness=step.brightness, on=step.on)
        scenes.append({"id": f"sc{i}", "metadata": {"name": name},
                       "group": {"rid": zone_rid, "rtype": "zone"}, "actions": body["actions"]})
        rids.append(f"sc{i}")
    ss = {"id": "ssGH", "metadata": {"name": cfg.circadian_scene.smart_scene},
          "group": {"rid": zone_rid, "rtype": "zone"}, "transition_duration": _ramp_ms(cfg),
          "week_timeslots": [{"timeslots": [
              {"start_time": {"kind": "time",
                              "time": {"hour": s.minute // 60, "minute": s.minute % 60, "second": 0}},
               "target": {"rid": rids[i], "rtype": "scene"}} for i, s in enumerate(steps)],
              "recurrence": ["monday", "tuesday", "wednesday", "thursday", "friday",
                             "saturday", "sunday"]}]}
    return scenes, ss, steps


def test_circadian_create_from_scratch(tmp_path):
    cfg = _cs_cfg()
    client = _FakeClient({"device": [], "room": [], "motion": [], "scene": [], "smart_scene": [],
                          "zone": [_zone("zNG", "Night Guide", ["litA", "litB"])]})
    rec = CircadianSceneReconciler(client, BridgeState(client).load(), cfg,
                                   for_date=DAY, backup_dir=str(tmp_path))
    change = rec.plan()[0]
    assert change.change_type is ChangeType.UPDATE
    rec.apply(change)

    scene_creates = [b for (t, b) in client.creates if t == "scene"]
    ss_creates = [b for (t, b) in client.creates if t == "smart_scene"]
    assert 1 <= len(scene_creates) <= MAX_TIMESLOTS
    assert len(ss_creates) == 1
    body = ss_creates[0]
    assert body["group"] == {"rid": "zNG", "rtype": "zone"}
    assert body["transition_duration"] == _ramp_ms(cfg)
    slots = body["week_timeslots"][0]["timeslots"]
    assert len(slots) == len(scene_creates) <= MAX_TIMESLOTS


def test_circadian_converged_is_noop(tmp_path):
    cfg = _cs_cfg()
    scenes, ss, _ = _converged(cfg, "zNG", ["litA", "litB"])
    client = _FakeClient({"device": [], "room": [], "motion": [], "scene": scenes,
                          "smart_scene": [ss], "zone": [_zone("zNG", "Night Guide", ["litA", "litB"])]})
    rec = CircadianSceneReconciler(client, BridgeState(client).load(), cfg,
                                   for_date=DAY, backup_dir=str(tmp_path))
    assert rec.plan()[0].change_type is ChangeType.NOOP


def test_circadian_scene_look_drift_is_update(tmp_path):
    cfg = _cs_cfg()
    scenes, ss, _ = _converged(cfg, "zNG", ["litA", "litB"])
    scenes[0]["actions"][0]["action"]["dimming"]["brightness"] = 77.0  # tamper one look
    client = _FakeClient({"device": [], "room": [], "motion": [], "scene": scenes,
                          "smart_scene": [ss], "zone": [_zone("zNG", "Night Guide", ["litA", "litB"])]})
    rec = CircadianSceneReconciler(client, BridgeState(client).load(), cfg,
                                   for_date=DAY, backup_dir=str(tmp_path))
    assert rec.plan()[0].change_type is ChangeType.UPDATE


def test_circadian_blocked_without_zone(tmp_path):
    cfg = _cs_cfg()
    client = _FakeClient({"device": [], "room": [], "motion": [], "scene": [], "smart_scene": [],
                          "zone": []})
    rec = CircadianSceneReconciler(client, BridgeState(client).load(), cfg,
                                   for_date=DAY, backup_dir=str(tmp_path))
    assert rec.plan()[0].change_type is ChangeType.BLOCKED


def test_circadian_recreates_smart_scene_on_group_change(tmp_path):
    cfg = _cs_cfg()
    scenes, ss, _ = _converged(cfg, "zNG", ["litA", "litB"])
    ss["group"] = {"rid": "OTHER", "rtype": "zone"}  # existing smart scene on a different group
    client = _FakeClient({"device": [], "room": [], "motion": [], "scene": scenes,
                          "smart_scene": [ss], "zone": [_zone("zNG", "Night Guide", ["litA", "litB"])]})
    rec = CircadianSceneReconciler(client, BridgeState(client).load(), cfg,
                                   for_date=DAY, backup_dir=str(tmp_path))
    rec.apply(rec.plan()[0])
    assert ("smart_scene", "ssGH") in client.deletes              # old one removed
    assert any(t == "smart_scene" for (t, _b) in client.creates)  # recreated on the zone


def test_reconciler_sun_times_are_dst_aware_when_tz_set(tmp_path):
    """With location.tz, the reconciler anchors to the DST-correct offset per date."""
    cfg = Config.parse({
        "bridge": {"host": "x", "application_key": "k"},
        "location": {"lat": 45.5152, "lon": -122.6784, "tz": "America/Los_Angeles"},
        "motion_policies": [],
        "circadian_scene": {"smart_scene": "Golden hours", "zone": "Night Guide",
                            "transition": "ramp", "hand_off": "22:34"},
    })
    client = _FakeClient({"device": [], "room": [], "motion": [], "scene": [], "smart_scene": [],
                          "zone": [_zone("zNG", "Night Guide", ["litA", "litB"])]})
    state = BridgeState(client).load()
    jan, jul = _dt.date(2026, 1, 15), _dt.date(2026, 7, 15)
    win = CircadianSceneReconciler(client, state, cfg, for_date=jan, backup_dir=str(tmp_path))._sun_times()
    summer = CircadianSceneReconciler(client, state, cfg, for_date=jul, backup_dir=str(tmp_path))._sun_times()
    pst = SolarCalculator(cfg.location.lat, cfg.location.lon, -8).sun_times(jan)
    pdt = SolarCalculator(cfg.location.lat, cfg.location.lon, -7).sun_times(jul)
    assert win.sunrise_min == pytest.approx(pst.sunrise_min)
    assert summer.sunrise_min == pytest.approx(pdt.sunrise_min)


def test_early_hand_off_drops_later_day_knees():
    """The off hand-off must be the FINAL slot. A hand_off before sunset (long
    summer days, or a user picking 20:00) must drop the day knees at/after it —
    otherwise sorted() places an on-knee after the off step and the smart scene
    re-lights the zone after bedtime."""
    early = 13 * 60                        # 13:00, well before the NYC solstice sunset
    steps = circadian_timeslots(PARAMS, SOLAR, DAY, hand_off_min=early)
    assert steps[-1].minute == early and steps[-1].on is False
    assert all(s.on and s.minute < early for s in steps[:-1])
    assert len(steps) >= 2                 # sunrise/noon knees before 13:00 survive
