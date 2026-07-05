"""Simulated-day tests for the rhythm phase engine."""
import datetime as dt
from zoneinfo import ZoneInfo

from hueman.config import RhythmSpec
from hueman.presence import PresenceSummary
from hueman.rhythm_control import AnchorStore, RhythmEngine, SignalState

TZ = "America/New_York"


def _spec(**kw):
    base = {"bedroom": "Bedroom"}
    base.update(kw)
    return RhythmSpec.parse(base)


def _epoch(day, hh, mm):
    """Epoch seconds for 2026-07-<day> hh:mm in the test tz (a Wednesday=1st)."""
    return dt.datetime(2026, 7, day, hh, mm, tzinfo=ZoneInfo(TZ)).timestamp()


QUIET = PresenceSummary(quiet_s=3600, last_active_room="Bedroom",
                        recent_rooms=(), recent_motion_count=0,
                        recent_light_change=False)
ACTIVE = PresenceSummary(quiet_s=5, last_active_room="Kitchen",
                         recent_rooms=("Kitchen", "Hallway"),
                         recent_motion_count=4, recent_light_change=False)
BEDROOM_BURST = PresenceSummary(quiet_s=5, last_active_room="Bedroom",
                                recent_rooms=("Bedroom",),
                                recent_motion_count=3,
                                recent_light_change=False)


def _sig(alarm=None, charging=False, tv=False, zone_on=True, sunset=20 * 60 + 30):
    return SignalState(next_alarm_epoch=alarm, phone_charging=charging,
                       tv_on=tv, zone_on=zone_on, sunset_min=float(sunset))


def test_starts_in_daylight_and_reports_no_change_on_steady_state():
    eng = RhythmEngine(_spec(), AnchorStore(), tz=TZ)
    d1 = eng.tick(_epoch(1, 12, 0), ACTIVE, _sig())
    assert d1.phase == RhythmEngine.DAYLIGHT and d1.changed is True  # first tick sets phase
    d2 = eng.tick(_epoch(1, 12, 1), ACTIVE, _sig())
    assert d2.changed is False


def test_evening_at_sunset_and_wind_down_before_bed_target():
    eng = RhythmEngine(_spec(), AnchorStore(), tz=TZ)
    eng.tick(_epoch(1, 12, 0), ACTIVE, _sig())
    d = eng.tick(_epoch(1, 20, 31), ACTIVE, _sig())            # past sunset 20:30
    assert d.phase == RhythmEngine.EVENING and d.changed
    d = eng.tick(_epoch(1, 21, 31), ACTIVE, _sig())            # 23:00 - 90m = 21:30
    assert d.phase == RhythmEngine.WIND_DOWN and d.changed
    assert d.evidence["bed_anchor_min"] == 23 * 60


def test_night_at_bed_anchor_then_sleep_when_vote_passes():
    eng = RhythmEngine(_spec(), AnchorStore(), tz=TZ)
    eng.tick(_epoch(1, 22, 0), ACTIVE, _sig())
    d = eng.tick(_epoch(1, 23, 1), ACTIVE, _sig())
    assert d.phase == RhythmEngine.NIGHT
    # vote fails while the TV is on or the zone is lit or the house is active
    d = eng.tick(_epoch(1, 23, 30), QUIET, _sig(tv=True, zone_on=False))
    assert d.phase == RhythmEngine.NIGHT
    d = eng.tick(_epoch(1, 23, 40), QUIET, _sig(zone_on=True))
    assert d.phase == RhythmEngine.NIGHT
    # quiet + tv off + zone off + last room bedroom -> SLEEP, onset recorded
    store = AnchorStore()
    eng2 = RhythmEngine(_spec(), store, tz=TZ)
    eng2.tick(_epoch(1, 23, 1), ACTIVE, _sig())
    d = eng2.tick(_epoch(1, 23, 50), QUIET, _sig(zone_on=False))
    assert d.phase == RhythmEngine.SLEEP and d.changed
    assert d.reason == "sleep-vote"
    assert store.median("sleep_onset", "weekday") is not None


def test_sleep_vote_needs_quiet():
    eng = RhythmEngine(_spec(), AnchorStore(), tz=TZ)
    eng.tick(_epoch(1, 23, 1), ACTIVE, _sig())
    d = eng.tick(_epoch(1, 23, 50), ACTIVE, _sig(zone_on=False))
    assert d.phase == RhythmEngine.NIGHT


def test_dawn_from_alarm_then_wake_on_bedroom_burst_records_anchor():
    store = AnchorStore()
    eng = RhythmEngine(_spec(), store, tz=TZ)
    eng.tick(_epoch(1, 23, 1), ACTIVE, _sig())
    eng.tick(_epoch(1, 23, 50), QUIET, _sig(zone_on=False))     # -> SLEEP
    alarm = _epoch(2, 6, 45)
    d = eng.tick(_epoch(2, 6, 25), QUIET, _sig(alarm=alarm, zone_on=False))
    assert d.phase == RhythmEngine.DAWN                          # 6:45 - 25m = 6:20
    assert d.evidence["wake_anchor_min"] == 6 * 60 + 45
    d = eng.tick(_epoch(2, 6, 50), BEDROOM_BURST, _sig(alarm=alarm, zone_on=False))
    assert d.phase == RhythmEngine.MORNING and d.reason == "wake-detected"
    assert store.median("wake", "weekday") == 6 * 60 + 50


def test_wake_without_bedroom_or_light_change_is_not_wake():
    """Rule 2: a cat patrol (multi-room, no bedroom, no lights) stays SLEEP."""
    eng = RhythmEngine(_spec(), AnchorStore(), tz=TZ)
    eng.tick(_epoch(1, 23, 1), ACTIVE, _sig())
    eng.tick(_epoch(1, 23, 50), QUIET, _sig(zone_on=False))     # -> SLEEP
    cat = PresenceSummary(quiet_s=5, last_active_room="Hallway",
                          recent_rooms=("Hallway", "Kitchen"),
                          recent_motion_count=5, recent_light_change=False)
    d = eng.tick(_epoch(2, 3, 0), cat, _sig(zone_on=False))
    assert d.phase == RhythmEngine.SLEEP


def test_morning_lasts_morning_min_then_daylight():
    eng = RhythmEngine(_spec(), AnchorStore(), tz=TZ)
    eng.tick(_epoch(1, 23, 1), ACTIVE, _sig())
    eng.tick(_epoch(1, 23, 50), QUIET, _sig(zone_on=False))
    eng.tick(_epoch(2, 6, 50), BEDROOM_BURST, _sig(zone_on=False))  # MORNING
    d = eng.tick(_epoch(2, 7, 55), ACTIVE, _sig())
    assert d.phase == RhythmEngine.DAYLIGHT


def test_weekend_day_class_used_for_records():
    store = AnchorStore()
    eng = RhythmEngine(_spec(), store, tz=TZ)
    # 2026-07-03 is a Friday; sleep onset recorded late Friday belongs to
    # Saturday's (weekend) class — the night is classed by the morning it ends.
    eng.tick(_epoch(3, 23, 1), ACTIVE, _sig())
    eng.tick(_epoch(3, 23, 50), QUIET, _sig(zone_on=False))
    assert store.median("sleep_onset", "weekend") is not None
    assert store.median("sleep_onset", "weekday") is None


def test_weekend_learned_wake_is_capped_by_drift_rule():
    """A learned weekend wake later than weekday+cap is clamped to the cap."""
    store = AnchorStore()
    store.record("wake", "weekday", 420, "2026-07-01")   # 07:00
    store.record("wake", "weekend", 600, "2026-07-04")   # 10:00 observed
    eng = RhythmEngine(_spec(), store, tz=TZ)
    # 2026-07-04 is a Saturday; day_class at Saturday noon is weekend
    d = eng.tick(_epoch(4, 12, 0), ACTIVE, _sig())
    # cap = weekday 420 + 90 drift = 510 (08:30), so 600 clamps to 510
    assert d.evidence["wake_anchor_min"] == 510
    assert d.evidence["wake_anchor_src"] == "learned-weekend"


def test_snapshot_is_json_serialisable_and_carries_anchors():
    import json
    eng = RhythmEngine(_spec(), AnchorStore(), tz=TZ)
    eng.tick(_epoch(1, 12, 0), ACTIVE, _sig())
    snap = eng.snapshot(_epoch(1, 12, 1))
    json.dumps(snap)
    assert snap["phase"] == RhythmEngine.DAYLIGHT
    assert "bed_anchor_min" in snap and "wake_anchor_min" in snap
