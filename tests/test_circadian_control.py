from __future__ import annotations
import datetime as _dt

from hueman.circadian import CircadianParams
from hueman.circadian_control import CircadianController, DriveTo, FadeOff, Hold
from hueman.config import CircadianDaemonSpec
from hueman.config import Anchor
from hueman.sun import SolarCalculator

TZ = -7.0  # PDT, matches the test date
SOLAR = SolarCalculator(45.5152, -122.6784, TZ)  # Portland
PARAMS = CircadianParams()

def _spec(**kw):
    base = dict(zone="Night Guide", start=Anchor("sunrise", 0), hand_off_min=22 * 60 + 34,
                interval_ms=60_000, transition_ms=75_000, fade_off_ms=90_000,
                detect_override=True, echo_ttl_ms=4_000, resume_on_power_cycle=True,
                resume_trigger=None, control_file=".r", daily_safety_resume=True,
                brightness_floor=None, brightness_ceiling=None, retry_on_error_ms=30_000,
                sse_backoff_max_ms=60_000, log_path="x", log_level="info")
    base.update(kw)
    return CircadianDaemonSpec(**base)

def _epoch(h, m, day=_dt.date(2026, 6, 28)):
    # local wall-clock -> epoch using the fixed TZ offset
    tz = _dt.timezone(_dt.timedelta(hours=TZ))
    return _dt.datetime(day.year, day.month, day.day, h, m, tzinfo=tz).timestamp()

def _ctrl(**kw):
    return CircadianController(_spec(**kw), PARAMS, SOLAR, TZ)


def test_drives_curve_during_day():
    c = _ctrl()
    a = c.tick(_epoch(13, 14))  # ~solar noon
    assert isinstance(a, DriveTo)
    assert a.transition_ms == 75_000
    assert 95.0 <= a.brightness <= 100.0       # near-peak at solar noon
    assert a.mirek == PARAMS.day_mirek
    assert c.mode == "driving"


def test_fades_off_at_hand_off_then_idles():
    c = _ctrl()
    c.tick(_epoch(20, 0))                       # establish DRIVING in-window
    a = c.tick(_epoch(22, 40))                  # just past 22:34 hand_off
    assert isinstance(a, FadeOff) and a.transition_ms == 90_000
    assert c.mode == "night_idle"
    assert isinstance(c.tick(_epoch(23, 30)), Hold)   # stays idle overnight


def test_resumes_driving_at_window_start_next_day():
    c = _ctrl()
    c.tick(_epoch(23, 0))                       # night_idle (before sunrise / after hand_off)
    a = c.tick(_epoch(9, 0))                    # well after sunrise -> in window
    assert isinstance(a, DriveTo) and c.mode == "driving"


def test_external_change_suspends_and_holds():
    c = _ctrl()
    c.tick(_epoch(12, 0))
    c.on_external_change(_epoch(12, 0))
    assert c.mode == "suspended"
    assert isinstance(c.tick(_epoch(12, 1)), Hold)   # no driving while suspended


def test_on_resume_returns_to_driving_in_window():
    c = _ctrl()
    c.tick(_epoch(12, 0)); c.on_external_change(_epoch(12, 0))
    c.on_resume(_epoch(12, 5))
    a = c.tick(_epoch(12, 5))
    assert isinstance(a, DriveTo) and c.mode == "driving"


def test_daily_safety_resume_at_window_start():
    c = _ctrl(daily_safety_resume=True)
    c.tick(_epoch(12, 0)); c.on_external_change(_epoch(12, 0))   # suspended during day1
    c.tick(_epoch(23, 0))                                         # still suspended at night
    a = c.tick(_epoch(9, 0, _dt.date(2026, 6, 29)))              # next day, window open
    assert isinstance(a, DriveTo) and c.mode == "driving"


def test_no_safety_resume_when_disabled():
    c = _ctrl(daily_safety_resume=False)
    c.tick(_epoch(12, 0)); c.on_external_change(_epoch(12, 0))   # suspended during day1
    c.tick(_epoch(23, 0))                                         # reset _was_in_window at night
    a = c.tick(_epoch(9, 0, _dt.date(2026, 6, 29)))              # next day, window-open edge fires
    assert isinstance(a, Hold) and c.mode == "suspended"


def test_detect_override_off_ignores_external_change():
    c = _ctrl(detect_override=False)
    c.tick(_epoch(12, 0))                       # establish DRIVING in-window
    c.on_external_change(_epoch(12, 0))         # ignored when detection is off
    assert c.mode == "driving"
    assert isinstance(c.tick(_epoch(12, 1)), DriveTo)


def test_zoneinfo_tz_branch_drives_during_day():
    c = CircadianController(_spec(), PARAMS, SOLAR, TZ, tz="America/Los_Angeles")
    a = c.tick(_epoch(13, 14))                  # PDT == -7 in late June, matches _epoch helper
    assert isinstance(a, DriveTo) and c.mode == "driving"


def test_brightness_floor_and_ceiling_clamp():
    c = _ctrl(brightness_floor=40.0, brightness_ceiling=80.0)
    a = c.tick(_epoch(13, 14))                  # would be ~100 -> clamped to 80
    assert isinstance(a, DriveTo) and a.brightness == 80.0


def test_is_night_out_of_window_or_past_civil_dusk() -> None:
    """The home reads as night outside the window (parked at the overnight
    look) and, in window, only once the sun is at/below −6° — not during the
    dusk blend (2026-06-28 Portland: sunset ~21:03, civil dusk ~21:41 PDT)."""
    c = _ctrl()
    assert not c.is_night(_epoch(13, 0))    # broad day
    assert not c.is_night(_epoch(21, 15))   # twilight blend, still in window
    assert c.is_night(_epoch(22, 10))       # past civil dusk, window still open
    assert c.is_night(_epoch(23, 30))       # out of window
    assert c.is_night(_epoch(3, 0))         # pre-dawn, out of window


# -- overnight (midnight-crossing) windows: start after hand_off wraps -------- #

def _overnight_ctrl(**kw):
    """Dusk->dawn window: start at sunset, hand off at sunrise."""
    return _ctrl(start=Anchor("sunset", 0), hand_off_anchor=Anchor("sunrise", 0), **kw)


def test_overnight_window_drives_evening_and_small_hours():
    c = _overnight_ctrl()
    a = c.tick(_epoch(22, 0))                          # after sunset (~21:03 PDT)
    assert isinstance(a, DriveTo) and c.mode == "driving"
    a = c.tick(_epoch(3, 0, _dt.date(2026, 6, 29)))    # small hours, next date
    assert isinstance(a, DriveTo) and c.mode == "driving"


def test_overnight_window_idles_by_day():
    c = _overnight_ctrl()
    assert isinstance(c.tick(_epoch(12, 0)), Hold)
    assert c.mode == "night_idle"


def test_overnight_window_fades_off_at_sunrise():
    c = _overnight_ctrl()
    c.tick(_epoch(4, 30))                              # pre-dawn, in window (~05:25 sunrise)
    a = c.tick(_epoch(6, 0))                           # past sunrise -> close edge
    assert isinstance(a, FadeOff)
    assert c.mode == "night_idle"
    assert isinstance(c.tick(_epoch(12, 0)), Hold)     # stays off through the day


def test_overnight_window_opens_at_sunset():
    c = _overnight_ctrl()
    c.tick(_epoch(12, 0))                              # day: idle
    a = c.tick(_epoch(21, 30))                         # past sunset -> window open
    assert isinstance(a, DriveTo) and c.mode == "driving"


def test_hand_off_anchor_clock_matches_legacy_behavior():
    c = _ctrl(hand_off_anchor=Anchor("clock", 22 * 60 + 34))
    c.tick(_epoch(20, 0))                              # establish DRIVING
    a = c.tick(_epoch(22, 40))                         # just past 22:34
    assert isinstance(a, FadeOff) and c.mode == "night_idle"


def test_overnight_manual_override_holds_until_resume():
    c = _overnight_ctrl()
    c.tick(_epoch(23, 0))
    c.on_external_change(_epoch(23, 0))
    assert isinstance(c.tick(_epoch(23, 30)), Hold)    # suspended, hands off
    c.on_resume(_epoch(23, 45))
    a = c.tick(_epoch(23, 45))
    assert isinstance(a, DriveTo) and c.mode == "driving"
