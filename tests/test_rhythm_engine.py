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
#: What PresenceTracker.summary() yields right after a daemon restart:
#: no human activity ever judged.
COLD = PresenceSummary(quiet_s=1e9, last_active_room=None,
                       recent_rooms=(), recent_motion_count=0,
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


def test_morning_restart_seeds_daylight_not_night():
    """A daemon restart mid-morning must not strand the day in NIGHT."""
    eng = RhythmEngine(_spec(), AnchorStore(), tz=TZ)
    d = eng.tick(_epoch(1, 9, 30), ACTIVE, _sig())
    assert d.phase == RhythmEngine.DAYLIGHT and d.reason == "seed"


def test_seed_hour_sweep_is_sane():
    """Seeding at every hour of a Wednesday lands in a sane phase.

    Defaults: wake anchor 07:00, sunset fixture 20:30, wind-down 21:30
    (bed 23:00 - 90m lead), night 23:00.
    """
    expected = {}
    for h in range(24):
        if h < 7:
            expected[h] = RhythmEngine.NIGHT
        elif h < 21:
            expected[h] = RhythmEngine.DAYLIGHT
        elif h == 21:
            expected[h] = RhythmEngine.EVENING
        elif h == 22:
            expected[h] = RhythmEngine.WIND_DOWN
        else:
            expected[h] = RhythmEngine.NIGHT
    for h in range(24):
        eng = RhythmEngine(_spec(), AnchorStore(), tz=TZ)
        d = eng.tick(_epoch(1, h, 0), QUIET, _sig(zone_on=False))
        assert d.phase == expected[h], f"hour {h}: {d.phase} != {expected[h]}"


def test_cold_start_with_charging_records_no_onset():
    """A restart during actual sleep must not fabricate a sleep onset.

    Cold presence (no human event ever judged) + phone on charger + zone off
    would pass the old vote; the human-seen gate blocks it.
    """
    store = AnchorStore()
    eng = RhythmEngine(_spec(), store, tz=TZ)
    d = eng.tick(_epoch(1, 6, 0), COLD, _sig(charging=True, zone_on=False))
    assert d.phase == RhythmEngine.NIGHT          # seeded pre-wake-anchor
    d = eng.tick(_epoch(1, 6, 1), COLD, _sig(charging=True, zone_on=False))
    assert d.phase == RhythmEngine.NIGHT
    assert store.median("sleep_onset", "weekday") is None
    assert store.median("sleep_onset", "weekend") is None


def test_overnight_restart_wakes_from_night():
    """Seeded NIGHT (restart during sleep) hands off to MORNING on wake."""
    store = AnchorStore()
    eng = RhythmEngine(_spec(), store, tz=TZ)
    d = eng.tick(_epoch(1, 2, 0), QUIET, _sig(zone_on=False))
    assert d.phase == RhythmEngine.NIGHT
    d = eng.tick(_epoch(1, 6, 50), BEDROOM_BURST, _sig(zone_on=False))
    assert d.phase == RhythmEngine.MORNING and d.reason == "wake-detected"
    assert store.median("wake", "weekday") == 6 * 60 + 50


def test_anchor_day_class_boundaries():
    """The wake anchor is classed by the upcoming morning, not the onset rule.

    Friday 06:00 -> today's (weekday) wake; Sunday 06:00 -> weekend; Friday
    22:00 -> Saturday morning, so weekend. 2026-07-03 is a Friday.
    """
    def _store():
        s = AnchorStore()
        s.record("wake", "weekday", 420, "2026-06-29")   # 07:00
        s.record("wake", "weekend", 500, "2026-06-28")   # 08:20
        return s

    d = RhythmEngine(_spec(), _store(), tz=TZ).tick(_epoch(3, 6, 0), ACTIVE, _sig())
    assert d.evidence["wake_anchor_min"] == 420
    assert d.evidence["wake_anchor_src"] == "learned-weekday"

    d = RhythmEngine(_spec(), _store(), tz=TZ).tick(_epoch(5, 6, 0), ACTIVE, _sig())
    # cap = weekday 420 + 90 drift = 510; 500 is under the cap -> unclamped
    assert d.evidence["wake_anchor_min"] == 500
    assert d.evidence["wake_anchor_src"] == "learned-weekend"

    d = RhythmEngine(_spec(), _store(), tz=TZ).tick(_epoch(3, 22, 0), ACTIVE, _sig())
    assert d.evidence["wake_anchor_min"] == 500
    assert d.evidence["wake_anchor_src"] == "learned-weekend"


def test_snapshot_is_json_serialisable_and_carries_anchors():
    import json
    eng = RhythmEngine(_spec(), AnchorStore(), tz=TZ)
    eng.tick(_epoch(1, 12, 0), ACTIVE, _sig())
    snap = eng.snapshot(_epoch(1, 12, 1))
    json.dumps(snap)
    assert snap["phase"] == RhythmEngine.DAYLIGHT
    assert "bed_anchor_min" in snap and "wake_anchor_min" in snap
