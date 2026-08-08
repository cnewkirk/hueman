from __future__ import annotations

from hueman.nightguide_control import GUIDING, IDLE, NightGuideController


def test_starts_idle():
    c = NightGuideController(timeout_ms=180_000)
    assert c.state == IDLE


def test_motion_enters_guiding_and_reports_the_edge():
    c = NightGuideController(timeout_ms=180_000)
    entered = c.motion(100.0)
    assert entered is True
    assert c.state == GUIDING


def test_repeated_motion_while_guiding_is_not_a_fresh_edge():
    c = NightGuideController(timeout_ms=180_000)
    assert c.motion(100.0) is True
    assert c.motion(105.0) is False   # already guiding -> no new write needed
    assert c.state == GUIDING


def test_tick_before_timeout_does_not_end_the_episode():
    c = NightGuideController(timeout_ms=180_000)   # 3 min
    c.motion(100.0)
    assert c.tick(100.0 + 170) is False
    assert c.state == GUIDING


def test_tick_past_timeout_ends_the_episode_once():
    c = NightGuideController(timeout_ms=180_000)
    c.motion(100.0)
    assert c.tick(100.0 + 180) is True             # the exit edge
    assert c.state == IDLE
    assert c.tick(100.0 + 240) is False             # already idle -> no repeat edge


def test_repeated_motion_extends_the_timeout_from_the_last_event():
    # Someone still moving around at t=170 (just under the 180s timeout) must
    # push the deadline out from THAT motion, not the original one -- a guide
    # light must not blink off mid-trip just because it's been >3min total.
    c = NightGuideController(timeout_ms=180_000)
    c.motion(100.0)
    c.motion(270.0)                                  # motion again at t=270 (still guiding)
    assert c.tick(270.0 + 170) is False              # 170s since the LATEST motion -> not yet
    assert c.state == GUIDING
    assert c.tick(270.0 + 180) is True               # 180s since the latest motion -> ends
    assert c.state == IDLE


def test_motion_after_a_completed_episode_starts_a_fresh_one():
    c = NightGuideController(timeout_ms=180_000)
    c.motion(100.0)
    c.tick(100.0 + 180)
    assert c.state == IDLE
    assert c.motion(500.0) is True                   # fresh edge, independent episode
    assert c.state == GUIDING
