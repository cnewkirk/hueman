"""Tests for presence judging (pet discounting, quiet tracking)."""
from hueman.config import RhythmPresence
from hueman.presence import ActivityEvent, PresenceTracker

SPEC = RhythmPresence()  # quiet 30m, confirm 3 in 10m, progression 5m
T0 = 1_000_000.0


def _motion(room, ts):
    return ActivityEvent(room=room, kind="motion", ts=ts)


def test_solo_single_room_motion_is_discounted_as_pet():
    tr = PresenceTracker(SPEC)
    j = tr.feed(_motion("Kitchen", T0))
    assert j.human is False and j.rule == "solo-motion"
    assert tr.summary(T0 + 60).quiet_s >= 60  # did not reset human-quiet


def test_room_progression_is_human():
    tr = PresenceTracker(SPEC)
    tr.feed(_motion("Kitchen", T0))
    j = tr.feed(_motion("Hallway", T0 + 120))  # different room within 5m
    assert j.human is True and j.rule == "progression"
    assert tr.summary(T0 + 130).quiet_s == 10.0
    assert tr.summary(T0 + 130).last_active_room == "Hallway"


def test_progression_window_expires():
    tr = PresenceTracker(SPEC)
    tr.feed(_motion("Kitchen", T0))
    j = tr.feed(_motion("Hallway", T0 + 6 * 60))  # beyond 5m window
    assert j.human is False and j.rule == "solo-motion"


def test_light_change_is_always_human():
    tr = PresenceTracker(SPEC)
    j = tr.feed(ActivityEvent(room="", kind="light_change", ts=T0))
    assert j.human is True and j.rule == "light-change"
    assert tr.summary(T0 + 5).quiet_s == 5.0


def test_motion_near_light_change_is_human():
    tr = PresenceTracker(SPEC)
    tr.feed(ActivityEvent(room="", kind="light_change", ts=T0))
    j = tr.feed(_motion("Kitchen", T0 + 60))
    assert j.human is True and j.rule == "light-change-context"


def test_summary_windows():
    tr = PresenceTracker(SPEC)
    tr.feed(_motion("Bedroom", T0))
    tr.feed(_motion("Hallway", T0 + 30))
    tr.feed(_motion("Kitchen", T0 + 60))
    s = tr.summary(T0 + 90)
    assert s.recent_motion_count == 3
    assert set(s.recent_rooms) == {"Bedroom", "Hallway", "Kitchen"}
    assert s.recent_light_change is False
    # 11 minutes later the confirm window (10m) has emptied
    s2 = tr.summary(T0 + 60 + 11 * 60)
    assert s2.recent_motion_count == 0 and s2.recent_rooms == ()


def test_roomless_motion_cannot_be_progression():
    """Motion with no room attribution never proves room-to-room movement."""
    tr = PresenceTracker(SPEC)
    tr.feed(_motion("Kitchen", T0))
    j = tr.feed(_motion("", T0 + 60))
    assert j.human is False and j.rule == "solo-motion"


def test_quiet_is_since_last_human_not_last_pet():
    tr = PresenceTracker(SPEC)
    tr.feed(_motion("Bedroom", T0))
    tr.feed(_motion("Hallway", T0 + 10))          # human via progression
    tr.feed(_motion("Kitchen", T0 + 20 * 60))      # solo — pet
    assert tr.summary(T0 + 21 * 60).quiet_s == 21 * 60 - 10
