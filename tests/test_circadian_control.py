from __future__ import annotations
import datetime as _dt

from hue_iac.circadian import CircadianParams
from hue_iac.circadian_control import CircadianController, DriveTo, FadeOff, Hold
from hue_iac.config import CircadianDaemonSpec
from hue_iac.config import Anchor
from hue_iac.sun import SolarCalculator

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
