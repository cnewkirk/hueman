from __future__ import annotations
import datetime as _dt
import logging
import sys

import pytest
import requests

from hueman.circadian_daemon import CircadianDaemon, _LOG
from hueman.config import Config
from hueman.errors import AuthError, BridgeError
from hueman.watch import BridgeEvent


@pytest.fixture(autouse=True)
def _restore_daemon_logging():
    saved_handlers, saved_level = list(_LOG.handlers), _LOG.level
    yield
    _LOG.handlers[:] = saved_handlers
    _LOG.setLevel(saved_level)


class _FakeClient:
    def __init__(self, *, resource_to_return=None):
        self.writes = []
        self._resource_to_return = resource_to_return   # for get_resource (night-guide snapshot)
    def get_resources(self, rtype):  # for BridgeState.load if used
        return []
    def get_resource(self, rtype, rid):
        return self._resource_to_return
    def update_resource(self, rtype, rid, body):
        self.writes.append((rtype, rid, body))


class _FakeStream:
    """Stands in for HueEventStream: yields canned events or raises on open."""

    def __init__(self, events=None, error=None):
        self._events = events or []
        self._error = error

    def events(self):
        if self._error is not None:
            raise self._error
        for event in self._events:
            yield event


class _FakeFactory:
    def __init__(self, streams):
        self._streams = list(streams)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self._streams:
            return self._streams.pop(0)
        return _FakeStream()  # empty stream ends cleanly


def _run_daemon(factory, sleeps, *, clock_value=None):
    """Build a for_test daemon, then override the run-loop seams run() reads."""
    daemon = CircadianDaemon.for_test(_FakeClient(), _cfg(), grouped_light_rid="GL")
    daemon._stream_factory = factory
    daemon._sleep = lambda s: sleeps.append(s)
    if clock_value is not None:
        daemon._clock = lambda: clock_value
    return daemon


def _cfg():
    return Config.parse({
        "bridge": {"host": "x", "application_key": "k"},
        "location": {"lat": 45.5152, "lon": -122.6784, "tz_offset_hours": -7},
        "motion_policies": [],
        "circadian_daemon": {"zone": "Night Guide", "interval": "60s", "transition": "75s"},
    })


def test_tick_writes_grouped_light_with_transition(monkeypatch):
    # A daemon wired to a fake grouped_light rid issues a transition write on a daytime tick.
    daemon = CircadianDaemon.for_test(_FakeClient(), _cfg(), grouped_light_rid="GL")
    noon = _epoch(13, 14)
    daemon._tick_once(noon)            # internal single-tick entry point for tests
    assert daemon._client.writes, "expected a grouped_light write"
    rtype, rid, body = daemon._client.writes[-1]
    assert (rtype, rid) == ("grouped_light", "GL")
    assert body["dynamics"] == {"duration": 75000}
    assert body["on"] == {"on": True}


def _dim(bri):
    """A grouped_light dimming event payload (the bridge's mid-fade re-emit)."""
    return BridgeEvent("grouped_light", "GL", {"dimming": {"brightness": bri}})


def test_ramp_then_settle_at_target_does_not_suspend():
    # The live bug: a 75s fade makes the bridge emit a STREAM of brightness
    # events that ramp to, then re-emit, the commanded target. None of these must
    # suspend the daemon. settle_window defaults to 2.5s; epsilon 0.75; band 8.
    daemon = CircadianDaemon.for_test(_FakeClient(), _cfg(), grouped_light_rid="GL")
    t0 = _epoch(12, 0)
    daemon._tick_once(t0)                       # NIGHT_IDLE -> DRIVING; records cmd target
    target = daemon._cmd_brightness
    assert daemon._controller.mode == "driving"
    # ramp: each value moves > epsilon, so the settle window keeps resetting.
    for off, dt in ((-2.0, 0.3), (-1.0, 0.8), (0.0, 1.3)):
        daemon._handle_event(_dim(target + off), t0 + dt)
        assert daemon._controller.mode == "driving"   # nothing settled mid-ramp
    # now the bridge re-emits the SAME settled value across the settle window.
    daemon._handle_event(_dim(target), t0 + 1.4)              # holds (within epsilon)
    assert daemon._controller.mode == "driving"              # not stable long enough yet
    daemon._handle_event(_dim(target), t0 + 1.3 + 2.6)       # held >= settle_window -> settled
    assert daemon._controller.mode == "driving"              # within band of target -> our look


def test_settle_far_from_target_suspends():
    # A real human dim: the value moves then HOLDS far from target across the
    # settle window -> a genuine override -> suspend.
    daemon = CircadianDaemon.for_test(_FakeClient(), _cfg(), grouped_light_rid="GL")
    t0 = _epoch(12, 0)
    daemon._tick_once(t0)
    daemon._handle_event(_dim(50.0), t0 + 0.4)    # moves (from None)
    daemon._handle_event(_dim(30.0), t0 + 0.9)    # moves -> reset window at 30
    assert daemon._controller.mode == "driving"   # mid-sweep: not yet settled
    daemon._handle_event(_dim(30.0), t0 + 0.9 + 2.6)   # held 30 >= settle_window -> settled far
    assert daemon._controller.mode == "suspended"


def test_moving_values_never_suspend_even_if_far():
    # Values that keep moving (each step > epsilon) are presumed an in-progress
    # fade and are NEVER classified, even when far from target -> no premature
    # suspend mid-sweep. This is precisely what the single-event echo got wrong.
    daemon = CircadianDaemon.for_test(_FakeClient(), _cfg(), grouped_light_rid="GL")
    t0 = _epoch(12, 0)
    daemon._tick_once(t0)
    for bri, dt in ((30.0, 1.0), (50.0, 2.0), (70.0, 3.0)):   # spans > settle_window total
        daemon._handle_event(_dim(bri), t0 + dt)
        assert daemon._controller.mode == "driving"


def test_tick_defers_drive_over_a_pending_manual_change():
    # THE STOMP RACE: a human turns the zone up; before the bridge re-emits the
    # settled value long enough for the SSE path to judge it, a tick lands. The
    # tick must NOT write the curve target over the unjudged value — doing so
    # fades the human's change away and the eventual settle-at-target reads as
    # "self", erasing the override without a trace.
    daemon = CircadianDaemon.for_test(_FakeClient(), _cfg(), grouped_light_rid="GL")
    t0 = _epoch(12, 0)
    daemon._tick_once(t0)                                # driving; records cmd target
    manual = daemon._cmd_brightness - 30.0               # far beyond override_band (8)
    daemon._handle_event(_dim(manual), t0 + 30)          # the human's change (moves -> unjudged)
    daemon._tick_once(t0 + 60)                           # tick lands before any re-emit
    assert len(daemon._client.writes) == 1               # deferred: no second write
    assert daemon._controller.mode == "driving"          # judgment itself hasn't happened yet


def test_tick_classifies_settled_override_without_reemission():
    # If the bridge never re-emits the settled value, the SSE path alone would
    # never judge it. The tick must classify it itself — but only after its own
    # last fade (75s) has had time to land, so the t0+60 tick (still inside the
    # fade window) defers, and the t0+120 tick suspends.
    daemon = CircadianDaemon.for_test(_FakeClient(), _cfg(), grouped_light_rid="GL")
    t0 = _epoch(12, 0)
    daemon._tick_once(t0)
    manual = daemon._cmd_brightness - 30.0
    daemon._handle_event(_dim(manual), t0 + 30)          # the change's ONLY event
    daemon._tick_once(t0 + 60)                           # own fade not landed -> defer, no judgment
    assert daemon._controller.mode == "driving"
    daemon._tick_once(t0 + 120)                          # past fade + settle window -> judged
    assert daemon._controller.mode == "suspended"
    assert len(daemon._client.writes) == 1               # the change was never overwritten


def test_stale_mid_fade_sample_never_false_suspends_on_tick():
    # A sparse mid-fade sample of the daemon's OWN fade, never re-emitted, must
    # not be tick-judged as a human override while the fade is still running
    # (the same trap the post-security grace closed for restores). Once the fade
    # lands and the bridge emits the target, driving continues normally.
    daemon = CircadianDaemon.for_test(_FakeClient(), _cfg(), grouped_light_rid="GL")
    t0 = _epoch(12, 0)
    daemon._tick_once(t0)                                # write #1; fade until t0+75
    target = daemon._cmd_brightness
    daemon._handle_event(_dim(target - 20.0), t0 + 40)   # sparse mid-fade sample (moves)
    daemon._tick_once(t0 + 60)                           # inside own fade: not judged (defers)
    assert daemon._controller.mode == "driving"
    daemon._handle_event(_dim(target), t0 + 76)          # fade lands (moves -> resets window)
    daemon._handle_event(_dim(target), t0 + 79)          # re-emit: held >= window -> self
    assert daemon._controller.mode == "driving"
    daemon._tick_once(t0 + 120)                          # judged as self -> drives again
    assert len(daemon._client.writes) == 2


def test_tick_drives_through_pending_value_within_band():
    # An unjudged value WITHIN override_band of target (our own fade's re-emit,
    # or a sub-band nudge) must not defer the tick — the curve keeps driving.
    daemon = CircadianDaemon.for_test(_FakeClient(), _cfg(), grouped_light_rid="GL")
    t0 = _epoch(12, 0)
    daemon._tick_once(t0)
    daemon._handle_event(_dim(daemon._cmd_brightness - 2.0), t0 + 30)   # within band, unjudged
    daemon._tick_once(t0 + 60)
    assert len(daemon._client.writes) == 2               # not deferred


def test_zone_turned_off_while_driving_suspends():
    daemon = CircadianDaemon.for_test(_FakeClient(), _cfg(), grouped_light_rid="GL")
    t0 = _epoch(12, 0)
    daemon._tick_once(t0)              # driving, cmd_on True
    daemon._handle_event(BridgeEvent("grouped_light", "GL", {"on": {"on": False}}), t0 + 1.0)
    assert daemon._controller.mode == "suspended"


def test_power_cycle_off_then_on_resumes():
    daemon = CircadianDaemon.for_test(_FakeClient(), _cfg(), grouped_light_rid="GL")
    t0 = _epoch(12, 0)
    daemon._tick_once(t0)
    daemon._handle_event(BridgeEvent("grouped_light", "GL", {"on": {"on": False}}), t0 + 1.0)
    assert daemon._controller.mode == "suspended"
    daemon._handle_event(BridgeEvent("grouped_light", "GL", {"on": {"on": True}}), t0 + 2.0)
    assert daemon._controller.mode == "driving"   # off->on power cycle re-engages


# -- resume race: stale cmd reference (live 2026-08-08) --------------------- #
# THE LIVE 2026-08-08 BUG: overnight motion-triggered brightness churn
# repeatedly power-cycled the zone. Every "power-cycle -> resumed" was undone
# by the very next settle judgment, because _cmd_brightness/_cmd_on are never
# refreshed while SUSPENDED — they're still whatever the daemon last commanded
# BEFORE the override began — and classify-before-drive (_tick_once) always
# runs that judgment before the controller gets a chance to redrive and
# refresh the reference. Resume "worked" only by luck (when nothing moved
# after the original override), and flapped every time something did. All
# three resume paths (power-cycle, control-file, resume-trigger scene) shared
# the bug; `_resume_locked` closes it the same way `_restore_after_security`
# already closed the identical race for the security-exit path.
def test_power_cycle_resume_does_not_immediately_resuspend_on_stale_target():
    daemon = CircadianDaemon.for_test(_FakeClient(), _cfg(), grouped_light_rid="GL")
    t0 = _epoch(12, 0)
    daemon._tick_once(t0)                                  # driving; records the target
    stale_target = daemon._cmd_brightness
    daemon._handle_event(_dim(stale_target - 30.0), t0 + 30)
    daemon._handle_event(_dim(stale_target - 30.0), t0 + 33)   # settles -> suspended
    assert daemon._controller.mode == "suspended"
    t1 = t0 + 1000                                         # no further drive since suspension
    daemon._handle_event(BridgeEvent("grouped_light", "GL", {"on": {"on": False}}), t1)
    daemon._handle_event(BridgeEvent("grouped_light", "GL", {"on": {"on": True}}), t1 + 1)
    assert daemon._controller.mode == "driving"            # power-cycle resumed
    # the just-resumed brightness settles far from the STALE target -- without
    # the grace reset this re-suspends within one settle window, every time.
    fresh = stale_target + 40.0
    daemon._handle_event(_dim(fresh), t1 + 2)
    daemon._handle_event(_dim(fresh), t1 + 5)               # holds >= settle_window
    assert daemon._controller.mode == "driving"


def test_power_cycle_resume_still_detects_a_genuine_override_after_grace():
    # The grace is not a permanent hall pass: once the daemon has had a real
    # tick to redrive (refreshing _cmd_brightness) and the grace window has
    # elapsed, a genuinely new manual change must still suspend normally.
    daemon = CircadianDaemon.for_test(_FakeClient(), _cfg(), grouped_light_rid="GL")
    t0 = _epoch(12, 0)
    daemon._tick_once(t0)
    stale_target = daemon._cmd_brightness
    daemon._handle_event(_dim(stale_target - 30.0), t0 + 30)
    daemon._handle_event(_dim(stale_target - 30.0), t0 + 33)
    assert daemon._controller.mode == "suspended"
    t1 = t0 + 1000
    daemon._handle_event(BridgeEvent("grouped_light", "GL", {"on": {"on": False}}), t1)
    daemon._handle_event(BridgeEvent("grouped_light", "GL", {"on": {"on": True}}), t1 + 1)
    assert daemon._controller.mode == "driving"
    daemon._tick_once(t1 + 60)                              # a real tick redrives -> fresh target
    fresh_target = daemon._cmd_brightness
    assert daemon._controller.mode == "driving"
    after_grace = t1 + 60 + 75 + 2.5 + 5                    # past transition + settle_window
    daemon._handle_event(_dim(fresh_target - 25.0), after_grace)
    daemon._handle_event(_dim(fresh_target - 25.0), after_grace + 3)
    assert daemon._controller.mode == "suspended"           # a real override still lands


def test_power_cycle_resume_at_night_immediately_repaints_to_night_look():
    # THE LIVE 2026-08-08 FOLLOW-UP: the resume-race fix stopped the daemon
    # from re-suspending itself, but a resume with no immediate write left
    # the zone exactly where the override left it until the next real
    # transition -- a toggle-resume at night produced literally no visible
    # change (confirmed live: 'power-cycle -> resumed' logged, mode correct,
    # but the bulbs never moved off the override's brightness). "The toggle
    # is on" has to mean circadian visibly owns the zone right now, not
    # eventually.
    daemon = CircadianDaemon.for_test(_FakeClient(), _cfg_night_look(), grouped_light_rid="GL")
    daemon._tick_once(_epoch(22, 0))                # driving
    daemon._tick_once(_epoch(23, 0))                # hand-off -> night_look (1%), night_idle
    assert daemon._client.writes[-1][2]["dimming"] == {"brightness": 1.0}
    t = _epoch(23, 30)
    daemon._handle_event(_dim(42.0), t)             # a real manual change, far from night_look
    daemon._handle_event(_dim(42.0), t + 3)         # settles -> suspended
    assert daemon._controller.mode == "suspended"
    n = len(daemon._client.writes)                 # nothing writes between here and the toggle
    daemon._handle_event(BridgeEvent("grouped_light", "GL", {"on": {"on": False}}), t + 30)
    daemon._handle_event(BridgeEvent("grouped_light", "GL", {"on": {"on": True}}), t + 31)
    assert len(daemon._client.writes) == n + 1      # a NEW write landed on the resume event itself
    rtype, rid, body = daemon._client.writes[-1]
    assert body["dimming"] == {"brightness": 1.0}   # repainted to night_look immediately
    assert daemon._cmd_brightness == 1.0
    assert daemon._controller.mode == "driving"


def test_power_cycle_resume_in_daytime_immediately_redrives_the_curve():
    # The daytime counterpart: resume shouldn't have to wait up to one
    # `interval` for the next tick to redrive -- it drives the instant the
    # toggle lands.
    daemon = CircadianDaemon.for_test(_FakeClient(), _cfg(), grouped_light_rid="GL")
    t0 = _epoch(12, 0)
    daemon._tick_once(t0)
    daemon._handle_event(_dim(daemon._cmd_brightness - 30.0), t0 + 30)
    daemon._handle_event(_dim(daemon._cmd_brightness - 30.0), t0 + 33)
    assert daemon._controller.mode == "suspended"
    n = len(daemon._client.writes)
    daemon._handle_event(BridgeEvent("grouped_light", "GL", {"on": {"on": False}}), t0 + 60)
    daemon._handle_event(BridgeEvent("grouped_light", "GL", {"on": {"on": True}}), t0 + 61)
    assert len(daemon._client.writes) == n + 1      # a fresh drive landed on the resume event itself
    assert daemon._controller.mode == "driving"


def _cfg_manual_override(control_file):
    return Config.parse({
        "bridge": {"host": "x", "application_key": "k"},
        "location": {"lat": 45.5152, "lon": -122.6784, "tz_offset_hours": -7},
        "motion_policies": [],
        "circadian_daemon": {
            "zone": "Night Guide", "interval": "60s", "transition": "75s",
            "manual_override": {"control_file": control_file},
        },
    })


def test_control_file_resume_does_not_immediately_resuspend_on_stale_target(tmp_path):
    resume_file = tmp_path / ".hue-circadian-resume"
    daemon = CircadianDaemon.for_test(
        _FakeClient(), _cfg_manual_override(str(resume_file)), grouped_light_rid="GL")
    t0 = _epoch(12, 0)
    daemon._tick_once(t0)
    stale_target = daemon._cmd_brightness
    daemon._handle_event(_dim(stale_target - 30.0), t0 + 30)
    daemon._handle_event(_dim(stale_target - 30.0), t0 + 33)
    assert daemon._controller.mode == "suspended"
    resume_file.write_text("resume\n")
    daemon._tick_once(t0 + 1000)                            # poll_control_file -> resumed
    assert daemon._controller.mode == "driving"
    assert not resume_file.exists()                         # consumed
    fresh = stale_target + 40.0
    daemon._handle_event(_dim(fresh), t0 + 1002)
    daemon._handle_event(_dim(fresh), t0 + 1005)
    assert daemon._controller.mode == "driving"             # not immediately re-suspended


def test_resume_trigger_does_not_immediately_resuspend_on_stale_target():
    daemon = CircadianDaemon.for_test(_FakeClient(), _cfg(), grouped_light_rid="GL")
    daemon._resume_trigger_rid = "SCENE1"
    t0 = _epoch(12, 0)
    daemon._tick_once(t0)
    stale_target = daemon._cmd_brightness
    daemon._handle_event(_dim(stale_target - 30.0), t0 + 30)
    daemon._handle_event(_dim(stale_target - 30.0), t0 + 33)
    assert daemon._controller.mode == "suspended"
    t1 = t0 + 1000
    daemon._handle_event(BridgeEvent("scene", "SCENE1", {"recall": {}}), t1)
    assert daemon._controller.mode == "driving"
    fresh = stale_target + 40.0
    daemon._handle_event(_dim(fresh), t1 + 2)
    daemon._handle_event(_dim(fresh), t1 + 5)
    assert daemon._controller.mode == "driving"


def test_own_fade_off_does_not_suspend():
    # After the daemon's own hand-off fade-off, cmd_on is False; the ensuing
    # on:false from the bridge is ours and must NOT be read as a human override.
    daemon = CircadianDaemon.for_test(_FakeClient(), _cfg(), grouped_light_rid="GL")
    daemon._tick_once(_epoch(22, 0))   # in window -> driving, cmd_on True
    assert daemon._cmd_on is True
    daemon._tick_once(_epoch(23, 0))   # past hand-off (22:34) -> FadeOff, cmd_on False
    assert daemon._cmd_on is False
    daemon._handle_event(BridgeEvent("grouped_light", "GL", {"on": {"on": False}}), _epoch(23, 1))
    assert daemon._controller.mode != "suspended"


def _cfg_night_look():
    return Config.parse({
        "bridge": {"host": "x", "application_key": "k"},
        "location": {"lat": 45.5152, "lon": -122.6784, "tz_offset_hours": -7},
        "motion_policies": [],
        "circadian_daemon": {
            "zone": "Night Guide", "interval": "60s", "transition": "75s",
            "night_look": {"brightness": 1, "hex": "#ff0000"},
        },
    })


def test_hand_off_fades_to_night_look_instead_of_off():
    # With night_look configured, window close parks the zone at the static
    # look (min-brightness red) rather than fading it off, then goes night-idle.
    daemon = CircadianDaemon.for_test(_FakeClient(), _cfg_night_look(), grouped_light_rid="GL")
    daemon._tick_once(_epoch(22, 0))       # in window -> driving
    daemon._tick_once(_epoch(23, 0))       # past hand-off (22:34) -> night look
    rtype, rid, body = daemon._client.writes[-1]
    assert body["on"] == {"on": True}
    assert body["dimming"] == {"brightness": 1.0}
    assert "color" in body                 # hex -> CIE xy
    assert daemon._cmd_on is True          # the 1% look is OUR commanded state
    assert daemon._cmd_brightness == 1.0
    assert daemon._controller.mode == "night_idle"
    # night-idle holds: the next tick writes nothing over an overnight manual change.
    n = len(daemon._client.writes)
    daemon._tick_once(_epoch(23, 1))
    assert len(daemon._client.writes) == n


def test_night_power_cycle_does_not_refire_hand_off():
    # THE 2026-08-07 LIVE BUG: at night the human turns the zone on; the
    # off->on power-cycle resumes the daemon (mode DRIVING), and the next
    # out-of-window tick used to fire the hand-off fade — yanking the lights
    # they just turned on down to the night look (or off). The hand-off must
    # fire on the window-CLOSE edge only; an out-of-window resume is inert.
    daemon = CircadianDaemon.for_test(_FakeClient(), _cfg_night_look(), grouped_light_rid="GL")
    daemon._tick_once(_epoch(22, 0))               # in window -> driving
    daemon._tick_once(_epoch(23, 0))               # close edge -> night look (1% red)
    assert daemon._client.writes[-1][2]["dimming"] == {"brightness": 1.0}
    t = _epoch(23, 30)
    daemon._handle_event(BridgeEvent("grouped_light", "GL", {"on": {"on": False}}), t)
    assert daemon._controller.mode == "suspended"  # human off at night: hands off
    daemon._handle_event(BridgeEvent("grouped_light", "GL", {"on": {"on": True}}), t + 60)
    assert daemon._controller.mode == "driving"    # power-cycle resume
    n = len(daemon._client.writes)
    daemon._tick_once(t + 90)                      # out-of-window tick after resume
    assert len(daemon._client.writes) == n         # NO hand-off re-fire, nothing written
    assert daemon._controller.mode == "night_idle"
    daemon._tick_once(t + 150)                     # and it stays inert
    assert len(daemon._client.writes) == n


def test_suspended_override_survives_hand_off():
    # A zone the human took over before hand-off must NOT be yanked to the
    # night look at window close — suspended means hands off, including the edge.
    daemon = CircadianDaemon.for_test(_FakeClient(), _cfg_night_look(), grouped_light_rid="GL")
    t0 = _epoch(22, 0)
    daemon._tick_once(t0)                  # driving
    daemon._handle_event(_dim(90.0), t0 + 30)      # manual change, far from target
    daemon._handle_event(_dim(90.0), t0 + 45)      # held >= settle_window -> suspended
    assert daemon._controller.mode == "suspended"
    n = len(daemon._client.writes)
    daemon._tick_once(_epoch(23, 0))       # past hand-off: Hold, not night look
    assert len(daemon._client.writes) == n


def test_manual_on_while_parked_off_suspends_and_survives_window_open():
    # THE CONTRACT: a manual change owns the lights until an EXPLICIT action.
    # Turning the zone on at night (after our own fade-off) is a manual night
    # look; without latching SUSPENDED, the next window open would flip
    # NIGHT_IDLE -> DRIVING and silently stomp it come morning.
    daemon = CircadianDaemon.for_test(_FakeClient(), _cfg(), grouped_light_rid="GL")
    daemon._tick_once(_epoch(22, 0))       # in window -> driving
    daemon._tick_once(_epoch(23, 0))       # past hand-off -> FadeOff, cmd_on False
    daemon._handle_event(BridgeEvent("grouped_light", "GL", {"on": {"on": False}}),
                         _epoch(23, 1))    # our own fade-off event: ignored
    assert daemon._controller.mode == "night_idle"
    daemon._handle_event(BridgeEvent("grouped_light", "GL", {"on": {"on": True}}),
                         _epoch(23, 30))   # a human turned the zone on
    assert daemon._controller.mode == "suspended"
    n = len(daemon._client.writes)
    daemon._tick_once(_epoch(12, 0, day=_dt.date(2026, 6, 29)))  # next-day window open
    assert daemon._controller.mode == "suspended"   # no silent morning retake
    assert len(daemon._client.writes) == n
    # the explicit hand-back still works: zone off->on power-cycles a resume.
    t = _epoch(12, 30, day=_dt.date(2026, 6, 29))
    daemon._handle_event(BridgeEvent("grouped_light", "GL", {"on": {"on": False}}), t)
    daemon._handle_event(BridgeEvent("grouped_light", "GL", {"on": {"on": True}}), t + 1)
    assert daemon._controller.mode == "driving"
    assert len(daemon._client.writes) > n           # resume re-drove the zone


def test_in_window_override_holds_past_next_window_open_by_default():
    # daily_safety_resume now defaults OFF: a daytime override survives the
    # hand-off AND the next morning's window-open edge, until an explicit resume.
    daemon = CircadianDaemon.for_test(_FakeClient(), _cfg(), grouped_light_rid="GL")
    t0 = _epoch(12, 0)
    daemon._tick_once(t0)
    daemon._handle_event(_dim(30.0), t0 + 30)       # manual dim, far from target
    daemon._handle_event(_dim(30.0), t0 + 45)       # held >= settle_window
    assert daemon._controller.mode == "suspended"
    n = len(daemon._client.writes)
    daemon._tick_once(_epoch(23, 0))                # window close while suspended: Hold
    daemon._tick_once(_epoch(12, 0, day=_dt.date(2026, 6, 29)))  # window-OPEN edge
    assert daemon._controller.mode == "suspended"
    assert len(daemon._client.writes) == n


# -- resume_trigger --------------------------------------------------------- #
def test_resume_trigger_scene_event_resumes():
    # A scene whose rid matches the resolved resume trigger re-engages the daemon.
    # The seam (_resume_trigger_rid) is resolved from a scene name via BridgeState
    # in production; for_test skips that, so the test sets the resolved rid here.
    daemon = CircadianDaemon.for_test(_FakeClient(), _cfg(), grouped_light_rid="GL")
    daemon._resume_trigger_rid = "SCENE1"
    daemon._tick_once(_epoch(12, 0))   # NIGHT_IDLE -> DRIVING
    daemon._handle_event(BridgeEvent("grouped_light", "GL",
                         {"on": {"on": False}}), _epoch(12, 0))   # human off -> suspend
    assert daemon._controller.mode == "suspended"
    # an unrelated scene must NOT resume
    daemon._handle_event(BridgeEvent("scene", "OTHER", {"recall": {}}), _epoch(12, 1))
    assert daemon._controller.mode == "suspended"
    # the configured resume-trigger scene re-engages driving
    daemon._handle_event(BridgeEvent("scene", "SCENE1", {"recall": {}}), _epoch(12, 1))
    assert daemon._controller.mode == "driving"


# -- run() loop: reconnect, backoff reset, auth-fatal, shutdown -------------- #
def test_run_reconnects_after_stream_error_and_processes_event():
    # First connection drops (transient); run() retries and processes the event
    # from the second, clean connection.
    sleeps: list[float] = []
    factory = _FakeFactory([
        _FakeStream(error=requests.exceptions.ConnectionError("boom")),       # 1st: drop
        _FakeStream(events=[BridgeEvent("grouped_light", "GL",
                                        {"on": {"on": False}})]),             # 2nd: clean
    ])
    daemon = _run_daemon(factory, sleeps, clock_value=_epoch(12, 0))
    daemon.run(max_reconnects=2)
    assert factory.calls == 2                       # it retried after the drop
    assert sleeps                                   # backoff slept before reconnecting
    assert daemon._controller.mode == "suspended"   # the post-reconnect event was handled


def test_run_backoff_resets_on_clean_connection():
    # error -> clean(data) -> error -> clean: the sleep after the data-bearing
    # connection is smaller than the first, proving backoff reset to initial.
    sleeps: list[float] = []
    factory = _FakeFactory([
        _FakeStream(error=BridgeError("e1")),                                  # backoff doubles
        _FakeStream(events=[BridgeEvent("grouped_light", "GL", {"on": {"on": True}})]),  # resets
        _FakeStream(error=BridgeError("e2")),                                  # doubles again
        _FakeStream(events=[]),                                                # clean end
    ])
    daemon = _run_daemon(factory, sleeps, clock_value=_epoch(12, 0))
    daemon.run(max_reconnects=4)
    assert factory.calls == 4
    assert len(sleeps) >= 2
    assert sleeps[1] < sleeps[0]   # reset shrank the post-reconnect backoff


def test_run_auth_failure_is_fatal_not_retried():
    sleeps: list[float] = []
    factory = _FakeFactory([_FakeStream(error=AuthError("bad key"))])
    daemon = _run_daemon(factory, sleeps, clock_value=_epoch(12, 0))
    with pytest.raises(AuthError):
        daemon.run(max_reconnects=5)
    assert factory.calls == 1   # a rejected key is not retried


def test_stop_breaks_run_loop_and_joins_tick_thread():
    # A live connection that calls stop(): run() must exit the loop and join the
    # tick thread without hanging (the test simply returning proves the join).
    sleeps: list[float] = []
    box: list[CircadianDaemon] = []

    class _StoppingStream:
        def events(self):
            box[0].stop()   # simulate a stop signal arriving on a live stream
            return iter(())

    factory = _FakeFactory([_StoppingStream()])
    daemon = _run_daemon(factory, sleeps, clock_value=_epoch(12, 0))
    box.append(daemon)
    daemon.run()   # no max_reconnects: only stop() can end this
    assert daemon._stop_event.is_set()
    assert factory.calls == 1   # stop took effect on the first connection


def test_init_raises_bridge_error_if_zone_has_no_grouped_light():
    """__init__ fails fast when the zone exists but has no grouped_light service.

    Without this guard every write silently fails (PUT to a None rid is a no-op)
    and the daemon runs indefinitely without driving any lights.
    """
    from hueman.state import Group

    class _StateNoGl:
        def group(self, name: str) -> Group:
            return Group(
                rid="G1", rtype="zone", name=name, grouped_light_rid=None,
                light_rids=(), device_rids=()
            )

        def scene(self, name: str) -> None:
            return None

    with pytest.raises(BridgeError, match="has no grouped_light service"):
        CircadianDaemon(_FakeClient(), _StateNoGl(), _cfg())


def test_init_raises_clear_error_listing_all_unknown_bias_lights():
    """A bias light absent from the bridge fails fast at startup, naming *every* bad
    name at once, instead of crash-looping on a cryptic first-name resolution error.
    """
    from hueman.errors import ConfigError
    from hueman.state import Group, LightRef

    class _State:
        def group(self, name):
            return Group(rid="G1", rtype="zone", name=name, grouped_light_rid="GL",
                         light_rids=(), device_rids=())

        def scene(self, name):
            return None

        @property
        def all_light_names(self):
            return ("Couch",)

        def light(self, name):
            if name == "Couch":
                return LightRef(name="Couch", device_rid="dC", light_rid="lC", room_name=None)
            raise ConfigError(f"no light named {name!r} on the bridge")

    cfg = _cfg_bias(
        {"control_file": {"on_file": ".tv-on", "off_file": ".tv-off"}},
        lights={
            "Couch": {"look": {"mirek": 200, "brightness": 10}},
            "Play bars": {"look": {"mirek": 153, "brightness": 28}},
            "Tree Left": {"look": {"mirek": 400, "brightness": 18}},
        },
    )
    with pytest.raises(ConfigError) as exc:
        CircadianDaemon(_FakeClient(), _State(), cfg)
    msg = str(exc.value)
    assert "Play bars" in msg and "Tree Left" in msg


def _epoch(h, m, day=_dt.date(2026, 6, 28)):
    tz = _dt.timezone(_dt.timedelta(hours=-7))
    return _dt.datetime(day.year, day.month, day.day, h, m, tzinfo=tz).timestamp()


# -- TV bias hold: trigger sources -> tv_on --------------------------------- #
def _cfg_bias(triggers, lights=None):
    """Config whose circadian_daemon carries a bias block with given triggers."""
    return Config.parse({
        "bridge": {"host": "x", "application_key": "k"},
        "location": {"lat": 45.5152, "lon": -122.6784, "tz_offset_hours": -7},
        "motion_policies": [],
        "circadian_daemon": {
            "zone": "Night Guide", "interval": "60s", "transition": "75s",
            "bias": {
                "lights": lights or {"Couch": {"look": {"mirek": 200, "brightness": 10}}},
                "triggers": triggers,
            },
        },
    })


def test_bias_sse_triggers_flip_tv_on():
    daemon = CircadianDaemon.for_test(
        _FakeClient(),
        _cfg_bias({"sse": {"on_trigger": "TV On", "off_trigger": "TV Off"}}),
        grouped_light_rid="GL")
    daemon._bias_sse_on_rid = "SON"      # production resolves these from BridgeState
    daemon._bias_sse_off_rid = "SOFF"
    t = _epoch(12, 0)
    assert daemon._bias_aggregator.tv_on(t) is False
    daemon._handle_event(BridgeEvent("scene", "SON", {"recall": {}}), t)
    assert daemon._bias_aggregator.tv_on(t) is True
    daemon._handle_event(BridgeEvent("scene", "SOFF", {"recall": {}}), t + 1)
    assert daemon._bias_aggregator.tv_on(t + 1) is False


def test_bias_control_file_triggers(tmp_path):
    on_file, off_file = tmp_path / "tv-on", tmp_path / "tv-off"
    daemon = CircadianDaemon.for_test(
        _FakeClient(),
        _cfg_bias({"control_file": {"on_file": str(on_file), "off_file": str(off_file)}}),
        grouped_light_rid="GL")
    t = _epoch(12, 0)
    on_file.write_text("")
    daemon._poll_bias_files(t)
    assert daemon._bias_aggregator.tv_on(t) is True
    assert not on_file.exists()                       # consumed (edge-triggered)
    off_file.write_text("")
    daemon._poll_bias_files(t + 1)
    assert daemon._bias_aggregator.tv_on(t + 1) is False


def test_bias_file_tick_applies_bias_immediately(tmp_path):
    """A control-file flag applies bias on the fast file-poll cadence, not only
    on the 60s curve tick — a Home Assistant shell_command signal engages/
    releases bias in seconds even with the network probe disabled."""
    on_file, off_file = tmp_path / "tv-on", tmp_path / "tv-off"
    cfg = _cfg_bias(
        {"control_file": {"on_file": str(on_file), "off_file": str(off_file)}},
        lights={
            "Play bars": {"look": {"mirek": 153, "brightness": 28}, "idle": "off"},
            "Couch": {"look": {"hex": "1a0a00", "brightness": 5}, "idle": "circadian"},
        })
    d = CircadianDaemon.for_test(_FakeClient(), cfg, grouped_light_rid="GL")
    d._bias_rids = {"Play bars": "Lplay", "Couch": "Lcouch"}
    t = _epoch(13, 14)                              # daytime, in window
    on_file.write_text("")
    d._bias_file_tick(t)                            # consume flag + apply now
    lw = _light_writes(d)
    assert lw["Lplay"]["on"] == {"on": True}        # play bars held on (bias look)
    assert lw["Lplay"]["color_temperature"] == {"mirek": 153}
    assert not on_file.exists()                     # flag consumed
    d._client.writes.clear()
    d._bias_file_tick(t + 1)                        # no flag, no edge -> no write
    assert _light_writes(d) == {}
    off_file.write_text("")
    d._bias_file_tick(t + 2)                        # off flag -> apply off now
    assert _light_writes(d)["Lplay"]["on"] == {"on": False}   # idle=off


def test_bias_file_tick_noop_during_security(tmp_path):
    """The fast bias-file poll yields to an active security show (it owns the
    lights); the flag is left for after the show, not consumed mid-panic."""
    on_file = tmp_path / "tv-on"
    d = CircadianDaemon.for_test(
        _FakeClient(),
        _cfg_bias({"control_file": {"on_file": str(on_file)}}),
        grouped_light_rid="GL")
    d._security_active = True
    on_file.write_text("")
    d._bias_file_tick(_epoch(13, 14))
    assert on_file.exists()                         # not consumed while security owns lights
    assert _light_writes(d) == {}


def test_bias_probe_tick_feeds_aggregator(monkeypatch):
    daemon = CircadianDaemon.for_test(
        _FakeClient(),
        _cfg_bias({"probe": {"enabled": True, "host": "192.0.2.50", "debounce": "0s"}}),
        grouped_light_rid="GL")
    t = _epoch(12, 0)
    monkeypatch.setattr(daemon, "_probe_reachable", lambda: True)
    daemon._probe_tick(t)
    assert daemon._bias_aggregator.tv_on(t) is True
    monkeypatch.setattr(daemon, "_probe_reachable", lambda: False)
    daemon._probe_tick(t + 1)
    assert daemon._bias_aggregator.tv_on(t + 1) is False


def test_bias_probe_disabled_is_noop():
    daemon = CircadianDaemon.for_test(
        _FakeClient(),
        _cfg_bias({"sse": {"on_trigger": "TV On"}}),   # probe absent/disabled
        grouped_light_rid="GL")
    t = _epoch(12, 0)
    daemon._probe_tick(t)                              # no-op when disabled
    assert daemon._bias_aggregator.tv_on(t) is False


# -- TV bias hold: per-light writes each tick ------------------------------- #
def _bias_daemon():
    """for_test daemon with two bias lights (play bars idle=off, couch idle=circadian)."""
    cfg = _cfg_bias(
        {"sse": {"on_trigger": "On", "off_trigger": "Off"}},
        lights={
            "Play bars": {"look": {"mirek": 153, "brightness": 28}, "idle": "off"},
            "Couch": {"look": {"hex": "1a0a00", "brightness": 5}, "idle": "circadian"},
        })
    d = CircadianDaemon.for_test(_FakeClient(), cfg, grouped_light_rid="GL")
    d._bias_rids = {"Play bars": "Lplay", "Couch": "Lcouch"}   # production resolves via BridgeState
    return d


def _light_writes(daemon):
    return {rid: body for (rtype, rid, body) in daemon._client.writes if rtype == "light"}


def test_bias_tick_holds_when_tv_on():
    d = _bias_daemon()
    t = _epoch(13, 14)                 # daytime, in window
    d._bias_aggregator.on(t)          # TV on
    d._tick_once(t)
    lw = _light_writes(d)
    assert set(lw) == {"Lplay", "Lcouch"}
    assert lw["Lplay"]["color_temperature"] == {"mirek": 153}
    assert lw["Lplay"]["dimming"] == {"brightness": 28.0}
    assert "color" in lw["Lcouch"]                  # hex hold look
    assert lw["Lcouch"]["dimming"] == {"brightness": 5.0}
    # the main zone is still driven independently
    assert any(rt == "grouped_light" for (rt, _r, _b) in d._client.writes)


def test_bias_tick_idle_in_window():
    d = _bias_daemon()
    t = _epoch(13, 14)                 # daytime, TV off
    d._tick_once(t)                    # first apply = startup edge -> short fade
    lw = _light_writes(d)
    # play bars idle=off -> off; couch idle=circadian -> follows the curve (on)
    assert lw["Lplay"]["on"] == {"on": False}
    assert lw["Lplay"]["dynamics"] == {"duration": d._bias.transition_ms}
    assert lw["Lcouch"]["on"] == {"on": True}
    assert lw["Lcouch"]["dynamics"] == {"duration": d._bias.transition_ms}
    d._client.writes.clear()
    d._tick_once(t + 60)               # steady state: curve-following keeps 75s
    lw = _light_writes(d)
    assert "Lplay" not in lw           # off already written; not re-spammed
    assert lw["Lcouch"]["dynamics"] == {"duration": d._spec.transition_ms}


def test_bias_tick_off_out_of_window():
    d = _bias_daemon()
    t = _epoch(23, 30)                 # past hand-off 22:34 -> out of window, TV off
    d._tick_once(t)
    lw = _light_writes(d)
    assert lw["Lplay"]["on"] == {"on": False}
    assert lw["Lcouch"]["on"] == {"on": False}      # no night_look configured -> off


def test_bias_tick_off_out_of_window_joins_night_look():
    # THE LIVE 2026-08-08 REPORT: with night_look configured, an idle=circadian
    # viewing light going fully dark overnight (while the rest of the home sat
    # at a dim night_look) read as broken, not as "TV is off" -- the only thing
    # that should set a light apart from the rest of the home is the TV being
    # ON, not a separate day/night schedule of its own.
    cfg = Config.parse({
        "bridge": {"host": "x", "application_key": "k"},
        "location": {"lat": 45.5152, "lon": -122.6784, "tz_offset_hours": -7},
        "motion_policies": [],
        "circadian_daemon": {
            "zone": "Night Guide", "interval": "60s", "transition": "75s",
            "night_look": {"brightness": 1, "hex": "#ff0000"},
            "bias": {
                "lights": {
                    "Play bars": {"look": {"mirek": 153, "brightness": 28}, "idle": "off"},
                    "Couch": {"look": {"hex": "1a0a00", "brightness": 5}, "idle": "circadian"},
                },
                "triggers": {"sse": {"on_trigger": "On", "off_trigger": "Off"}},
            },
        },
    })
    d = CircadianDaemon.for_test(_FakeClient(), cfg, grouped_light_rid="GL")
    d._bias_rids = {"Play bars": "Lplay", "Couch": "Lcouch"}
    t = _epoch(23, 30)                 # past hand-off 22:34 -> out of window, TV off
    d._tick_once(t)
    lw = _light_writes(d)
    assert lw["Lplay"]["on"] == {"on": False}       # idle=off is unaffected
    assert lw["Lcouch"]["on"] == {"on": True}        # idle=circadian joins night_look instead
    assert lw["Lcouch"]["dimming"] == {"brightness": 1.0}
    assert "color" in lw["Lcouch"]


def test_no_bias_writes_when_no_bias_configured():
    daemon = CircadianDaemon.for_test(_FakeClient(), _cfg(), grouped_light_rid="GL")
    daemon._tick_once(_epoch(13, 14))
    assert all(rtype != "light" for (rtype, _r, _b) in daemon._client.writes)


def test_bias_idle_unaffected_by_main_zone_override():
    """Full-fidelity guarantee: a manual override of the MAIN zone (which suspends
    the main set) must not freeze the viewing lights — idle=circadian keeps
    following the curve, driven off the controller's window/curve, not its mode."""
    d = _bias_daemon()
    t = _epoch(13, 14)
    d._tick_once(t)                                # first in-window tick: main drives
    d._client.writes.clear()                       # ignore that tick's writes
    d._controller.on_external_change(t)            # main zone overridden -> suspended
    assert d._controller.mode == "suspended"
    d._tick_once(t)                                # window already open -> stays suspended
    lw = _light_writes(d)
    assert lw["Lcouch"]["on"] == {"on": True}      # viewing light still drives the curve
    assert not any(rt == "grouped_light" for (rt, _r, _b) in d._client.writes)  # main held


# -- TV bias hold: per-light manual-override freeze -------------------------- #
def _light_ev(rid, **data):
    return BridgeEvent("light", rid, data)


def test_bias_light_manual_dim_defers_then_freezes_that_light():
    # THE CONTRACT, per light: a human dims one viewing light; the daemon must
    # first DEFER (never write over an unjudged value), then freeze that one
    # light once the value settles — while the rest of the set keeps driving.
    d = _bias_daemon()
    t = _epoch(13, 14)
    d._bias_aggregator.on(t)                       # TV on -> hold looks
    d._tick_once(t)                                # edge apply; cmd latched (Couch 5%)
    d._handle_event(_light_ev("Lcouch", dimming={"brightness": 60.0}), t + 10)
    d._client.writes.clear()
    d._tick_once(t + 11)                           # value not yet settled -> defer
    lw = _light_writes(d)
    assert "Lcouch" not in lw                      # not written over mid-adjustment
    assert "Lplay" in lw                           # the other light still held
    d._handle_event(_light_ev("Lcouch", dimming={"brightness": 60.0}), t + 14)
    assert "Lcouch" in d._bias_overridden          # settled far from 5% -> frozen
    d._client.writes.clear()
    d._tick_once(t + 60)
    assert "Lcouch" not in _light_writes(d)        # frozen: skipped every tick


def test_bias_light_manual_off_freezes_it():
    # Turning a commanded-on viewing light OFF is a manual override too — the
    # daemon must not relight it on the next tick.
    d = _bias_daemon()
    t = _epoch(13, 14)
    d._bias_aggregator.on(t)
    d._tick_once(t)
    d._handle_event(_light_ev("Lcouch", on={"on": False}), t + 10)
    assert "Lcouch" in d._bias_overridden
    d._client.writes.clear()
    d._tick_once(t + 60)
    assert "Lcouch" not in _light_writes(d)


def test_bias_light_manual_on_while_parked_off_freezes():
    # Out of window, TV off: the set is parked off. A human turning a viewing
    # light ON owns that light — the daemon must not fade it back off.
    d = _bias_daemon()
    t = _epoch(23, 30)                             # past hand-off, TV off -> offs written
    d._tick_once(t)
    d._handle_event(_light_ev("Lplay", on={"on": True}), t + 60)
    assert "Lplay" in d._bias_overridden
    d._client.writes.clear()
    d._tick_once(t + 120)
    assert "Lplay" not in _light_writes(d)


def test_bias_light_power_cycle_rejoins_immediately():
    # The explicit per-light hand-back: toggling the frozen light off->on
    # releases it and re-drives it on the event itself, not next tick.
    d = _bias_daemon()
    t = _epoch(13, 14)
    d._bias_aggregator.on(t)
    d._tick_once(t)
    d._handle_event(_light_ev("Lcouch", on={"on": False}), t + 10)   # freeze (manual off)
    assert "Lcouch" in d._bias_overridden
    d._client.writes.clear()
    d._handle_event(_light_ev("Lcouch", on={"on": True}), t + 20)    # off->on rejoin
    assert "Lcouch" not in d._bias_overridden
    lw = _light_writes(d)
    assert lw["Lcouch"]["dimming"] == {"brightness": 5.0}            # hold look re-driven


def test_resume_releases_frozen_bias_lights():
    # "Circadian mode toggled" (resume trigger / control file / power-cycle)
    # hands the WHOLE home back: frozen viewing lights rejoin on the resume.
    d = _bias_daemon()
    t = _epoch(13, 14)
    d._bias_aggregator.on(t)
    d._tick_once(t)
    d._handle_event(_light_ev("Lcouch", on={"on": False}), t + 10)
    assert "Lcouch" in d._bias_overridden
    d._client.writes.clear()
    d._resume_locked(t + 60)
    assert not d._bias_overridden
    assert "Lcouch" in _light_writes(d)            # edge re-assert rejoined it


# -- TV bias hold: probe edge applies immediately (low-latency) -------------- #
def test_probe_edge_applies_bias_immediately(monkeypatch):
    """A probe state change applies bias on the probe cadence, not only on the
    60s curve tick: _probe_tick writes the bias lights the moment committed
    tv_on flips, and does not re-write when nothing changed."""
    cfg = _cfg_bias(
        {"probe": {"enabled": True, "host": "192.0.2.50", "debounce": "0s"}},
        lights={
            "Play bars": {"look": {"mirek": 153, "brightness": 28}, "idle": "off"},
            "Couch": {"look": {"hex": "1a0a00", "brightness": 5}, "idle": "circadian"},
        })
    d = CircadianDaemon.for_test(_FakeClient(), cfg, grouped_light_rid="GL")
    d._bias_rids = {"Play bars": "Lplay", "Couch": "Lcouch"}
    t = _epoch(13, 14)                              # daytime, in window

    monkeypatch.setattr(d, "_probe_reachable", lambda: True)
    d._probe_tick(t)                               # TV-on edge -> apply now
    lw = _light_writes(d)
    assert lw["Lplay"]["on"] == {"on": True}        # play bars held on (bias look)
    assert lw["Lplay"]["color_temperature"] == {"mirek": 153}

    d._client.writes.clear()
    d._probe_tick(t + 1)                            # still on, no edge -> no write
    assert _light_writes(d) == {}

    monkeypatch.setattr(d, "_probe_reachable", lambda: False)
    d._probe_tick(t + 2)                            # TV-off edge -> apply now
    lw = _light_writes(d)
    assert lw["Lplay"]["on"] == {"on": False}       # play bars off (idle=off)


# -- TV bias hold: edge transition, off suppression, failure retry ----------- #
def test_probe_edge_writes_use_edge_fade(monkeypatch):
    """A TV flip drives every bias light with the short bias.transition fade
    (default 2s), not the steady-state 75s/90s fades."""
    d = _bias_daemon()
    t = _epoch(13, 14)                              # daytime, in window
    d._tick_once(t)                                 # startup apply; settle initial state
    d._client.writes.clear()
    d._bias_aggregator.on(t + 1)                    # TV-on edge
    d._apply_bias_if_changed(t + 1)
    lw = _light_writes(d)
    assert lw["Lplay"]["dynamics"] == {"duration": 2_000}
    assert lw["Lcouch"]["dynamics"] == {"duration": 2_000}
    d._client.writes.clear()
    d._bias_aggregator.off(t + 2)                   # TV-off edge
    d._apply_bias_if_changed(t + 2)
    lw = _light_writes(d)
    assert lw["Lplay"]["on"] == {"on": False}
    assert lw["Lplay"]["dynamics"] == {"duration": 2_000}   # off snaps, no 90s tail
    assert lw["Lcouch"]["dynamics"] == {"duration": 2_000}  # drive snaps to curve


def test_steady_state_off_writes_are_suppressed():
    """Once a light's off has been written successfully, non-edge ticks stop
    re-PUTing it (kills the 7-writes-per-minute overnight spam)."""
    d = _bias_daemon()
    t = _epoch(23, 30)                              # out of window, TV off
    d._tick_once(t)                                 # startup edge: both lights -> off
    lw = _light_writes(d)
    assert lw["Lplay"]["on"] == {"on": False} and lw["Lcouch"]["on"] == {"on": False}
    d._client.writes.clear()
    d._tick_once(t + 60)                            # steady state: nothing to re-write
    assert _light_writes(d) == {}
    d._client.writes.clear()
    d._bias_aggregator.on(t + 120)                  # TV-on edge re-arms writes
    d._tick_once(t + 120)
    assert set(_light_writes(d)) == {"Lplay", "Lcouch"}


def test_failed_edge_writes_retry_on_next_apply(monkeypatch):
    """A bridge rejection (e.g. 'command queue is full') must not latch the edge:
    the next apply retries the writes instead of waiting for a state change."""
    d = _bias_daemon()
    t = _epoch(13, 14)
    d._tick_once(t)                                 # settle startup state
    d._client.writes.clear()
    boom = BridgeError("command queue is full")
    original = d._client.update_resource
    def flaky(rtype, rid, body):
        if rtype == "light":
            raise boom
        return original(rtype, rid, body)
    d._client.update_resource = flaky
    d._bias_aggregator.on(t + 1)                    # TV-on edge; all writes fail
    d._apply_bias_if_changed(t + 1)
    d._client.update_resource = original            # bridge queue drains
    d._client.writes.clear()
    d._apply_bias_if_changed(t + 2)                 # no new flip -- pure retry
    lw = _light_writes(d)
    assert set(lw) == {"Lplay", "Lcouch"}           # edge was not lost
    assert lw["Lplay"]["dynamics"] == {"duration": 2_000}


def test_unreachable_light_does_not_stall_edge_latch(monkeypatch):
    """An unreachable bias bulb (a housekeeper unplugged it) must NOT keep the
    edge from latching. Otherwise ``_bias_last_applied_on`` never catches up to
    ``tv_on``, every poll reads as a fresh TV flip, and the daemon re-fires the
    whole viewing set every ~2s forever (the runaway observed live 2026-07-25)."""
    d = _bias_daemon()
    t = _epoch(13, 14)
    d._tick_once(t)                                 # settle startup state (TV off)
    d._client.writes.clear()
    dead = BridgeError(
        "PUT /clip/v2/resource/light/Lcouch: device (light) Lcouch has "
        "communication issues, command (.on.on) may not have effect",
        unreachable=True,
    )
    original = d._client.update_resource
    def one_dead(rtype, rid, body):
        if rtype == "light" and rid == "Lcouch":    # the unplugged bulb
            raise dead
        return original(rtype, rid, body)
    d._client.update_resource = one_dead
    d._bias_aggregator.on(t + 1)                    # TV-on edge; Lcouch unreachable
    d._apply_bias_if_changed(t + 1)
    assert d._bias_last_applied_on is True          # latched despite the dead bulb
    assert "Lplay" in _light_writes(d)              # the reachable light still held
    # No new flip -> the next apply is a NO-OP, not another edge re-fire.
    d._client.writes.clear()
    d._apply_bias_if_changed(t + 2)
    assert _light_writes(d) == {}


def test_bias_edge_logs_flip_at_info(caplog):
    """A committed TV flip emits one INFO line naming the state and source."""
    cfg = _cfg_bias({"probe": {"enabled": True, "host": "192.0.2.50", "debounce": "0s"}})
    d = CircadianDaemon.for_test(_FakeClient(), cfg, grouped_light_rid="GL")
    d._bias_rids = {"Couch": "Lcouch"}
    caplog.set_level(logging.INFO, logger="hueman.circadian_daemon")
    t = _epoch(13, 14)
    d._tick_once(t)
    import unittest.mock as _mock
    with _mock.patch.object(d, "_probe_reachable", return_value=True):
        d._probe_tick(t + 1)
    assert any(
        r.levelno == logging.INFO and "TV on" in r.message and "probe" in r.message
        for r in caplog.records
    ), f"expected INFO 'TV on … probe'; got: {[r.message for r in caplog.records]}"


# -- observability logging tests --------------------------------------------- #

def test_tick_logs_drive_at_info(caplog):
    """A daytime tick emits an INFO record mentioning 'drive' and the zone name."""
    daemon = CircadianDaemon.for_test(_FakeClient(), _cfg(), grouped_light_rid="GL")
    caplog.set_level(logging.INFO, logger="hueman.circadian_daemon")
    daemon._tick_once(_epoch(13, 14))
    assert any(
        r.levelno == logging.INFO and "drive" in r.message and "Night Guide" in r.message
        for r in caplog.records
    ), f"expected INFO 'drive … Night Guide'; got: {[r.message for r in caplog.records]}"


def test_external_event_logs_override(caplog):
    """A brightness that SETTLES far from target logs INFO 'override' + 'suspended'."""
    daemon = CircadianDaemon.for_test(_FakeClient(), _cfg(), grouped_light_rid="GL")
    caplog.set_level(logging.INFO, logger="hueman.circadian_daemon")
    t0 = _epoch(12, 0)
    daemon._tick_once(t0)
    daemon._handle_event(_dim(30.0), t0 + 0.4)            # moves (from None)
    daemon._handle_event(_dim(30.0), t0 + 0.4 + 2.6)      # held >= settle_window -> settled far
    assert daemon._controller.mode == "suspended"
    assert any(
        r.levelno == logging.INFO and "override" in r.message and "suspended" in r.message
        for r in caplog.records
    ), f"expected INFO 'override … suspended'; got: {[r.message for r in caplog.records]}"


def test_configure_logging_adds_stdout_streamhandler():
    """_configure_logging attaches a plain StreamHandler pointing at sys.stdout."""
    daemon = CircadianDaemon.for_test(_FakeClient(), _cfg(), grouped_light_rid="GL")
    daemon._configure_logging(daemon._spec)
    assert any(
        type(h) is logging.StreamHandler and h.stream is sys.stdout
        for h in _LOG.handlers
    ), f"no stdout StreamHandler found; handlers: {_LOG.handlers}"


# -- security mode show engine ------------------------------------------------ #
def _cfg_security(triggers=None, groups=("Night Guide", "TV Viewing"), **over):
    sec = {
        "groups": list(groups),
        "alert": {"seconds": over.get("alert_seconds", 1)},
        "chaos": {"frame_interval": "250ms", "min_flash_interval": "350ms"},
        "max_duration": over.get("max_duration", "2s"),
    }
    if triggers is not None:
        sec["triggers"] = triggers
    if "sound" in over:
        sec["sound"] = over["sound"]
    return Config.parse({
        "bridge": {"host": "x", "application_key": "k"},
        "location": {"lat": 45.5152, "lon": -122.6784, "tz_offset_hours": -7},
        "motion_policies": [],
        "circadian_daemon": {"zone": "Night Guide", "interval": "60s", "transition": "75s"},
        "security": sec,
    })


def test_security_show_writes_frames_emits_cues_and_reverts(monkeypatch, tmp_path):
    cue = tmp_path / "cue"
    d = CircadianDaemon.for_test(
        _FakeClient(), _cfg_security(sound={"cue_file": str(cue)}), grouped_light_rid="GL")
    d._security_rids = {"Night Guide": "GLng", "TV Viewing": "GLtv"}
    monkeypatch.setattr(d._stop_event, "wait", lambda *a, **k: None)  # no real sleeping
    start = _epoch(2, 0)
    seq = iter(start + 0.25 * i for i in range(0, 400))
    d._clock = lambda: next(seq)
    d._security_aggregator.on(start)            # armed
    d._run_security_show(start)
    # frames went to BOTH security group rids
    gl = {rid for (rt, rid, _b) in d._client.writes if rt == "grouped_light"}
    assert {"GLng", "GLtv"} <= gl
    # cue progressed alert -> chaos -> clear; the last cue written is "clear"
    assert cue.read_text().strip() == "clear"
    # reverted to normal
    assert d._security_active is False


def test_security_disarm_file_ends_show(monkeypatch, tmp_path):
    off_file = tmp_path / "off"
    d = CircadianDaemon.for_test(
        _FakeClient(),
        _cfg_security(triggers={"control_file": {"off_file": str(off_file)}}),
        grouped_light_rid="GL")
    d._security_rids = {"Night Guide": "GLng", "TV Viewing": "GLtv"}
    monkeypatch.setattr(d._stop_event, "wait", lambda *a, **k: None)
    start = _epoch(2, 0)
    seq = iter(start + 0.25 * i for i in range(0, 400))
    d._clock = lambda: next(seq)
    d._security_aggregator.on(start)
    off_file.write_text("")                     # disarm before the max-duration cap
    d._run_security_show(start)
    assert d._security_active is False
    assert not off_file.exists()                # consumed on reset


def test_security_max_duration_expiry_clears_arm_signal(monkeypatch):
    """Expiry must consume the arm signal, or _security_loop re-engages the show
    on its next poll (observed live 2026-07-03: standdown 20:30:34, re-engaged
    20:30:36, ran a second full show)."""
    d = CircadianDaemon.for_test(_FakeClient(), _cfg_security(), grouped_light_rid="GL")
    d._security_rids = {"Night Guide": "GLng", "TV Viewing": "GLtv"}
    monkeypatch.setattr(d._stop_event, "wait", lambda *a, **k: None)
    start = _epoch(2, 0)
    seq = iter(start + 0.25 * i for i in range(0, 400))
    d._clock = lambda: next(seq)
    d._security_aggregator.on(start, "file")     # armed; never explicitly disarmed
    d._run_security_show(start)                  # exits via the 2s max-duration cap
    assert d._security_aggregator.active(start + 100) is False


def test_post_security_restore_ramp_settle_does_not_suspend():
    """The restore drive is a big catch-up ramp from wherever chaos left the zone.
    A slow mid-ramp reading that holds within epsilon across the settle window
    must NOT be classified as a human override (observed live 2026-07-03:
    'settled at 45.6% vs target 99.8% -> suspended' 42s after the disarm)."""
    d = CircadianDaemon.for_test(_FakeClient(), _cfg_security(), grouped_light_rid="GL")
    d._security_rids = {"Night Guide": "GLng", "TV Viewing": "GLtv"}
    t0 = _epoch(12, 0)                           # in-window: restore re-drives the zone
    d._restore_after_security(t0)
    assert d._controller.mode == "driving"
    d._handle_event(_dim(45.6), t0 + 30)         # mid-ramp value appears
    d._handle_event(_dim(45.6), t0 + 33)         # holds >= settle_window, far from target
    assert d._controller.mode == "driving"       # within the post-restore grace -> not an override


def test_override_detection_resumes_after_post_security_grace():
    """The grace covers one fade + settle window; a value that settles far from
    target AFTER that is a genuine human override and must still suspend."""
    d = CircadianDaemon.for_test(_FakeClient(), _cfg_security(), grouped_light_rid="GL")
    d._security_rids = {"Night Guide": "GLng", "TV Viewing": "GLtv"}
    t0 = _epoch(12, 0)
    d._restore_after_security(t0)
    after = t0 + 75 + 2.5 + 1                    # past transition (75s) + settle_window (2.5s)
    d._handle_event(_dim(50.0), after)           # moves -> resets the settle window
    d._handle_event(_dim(50.0), after + 3)       # holds far from target, grace over
    assert d._controller.mode == "suspended"


def test_security_active_suppresses_normal_tick():
    d = CircadianDaemon.for_test(_FakeClient(), _cfg_security(), grouped_light_rid="GL")
    d._security_active = True
    d._tick_once(_epoch(13, 14))                # daytime: would normally drive the curve
    assert d._client.writes == []              # security owns the lights -> no normal write


def test_security_sse_triggers_arm_and_disarm():
    d = CircadianDaemon.for_test(
        _FakeClient(),
        _cfg_security(triggers={"sse": {"on_trigger": "Panic On", "off_trigger": "Panic Off"}}),
        grouped_light_rid="GL")
    d._security_sse_on_rid = "SECON"            # production resolves these via BridgeState
    d._security_sse_off_rid = "SECOFF"
    t = _epoch(2, 0)
    assert d._security_aggregator.active(t) is False
    d._handle_event(BridgeEvent("button", "SECON", {"button": {}}), t)
    assert d._security_aggregator.active(t) is True
    d._handle_event(BridgeEvent("button", "SECOFF", {"button": {}}), t + 1)
    assert d._security_aggregator.active(t + 1) is False


def test_security_control_file_arms(tmp_path):
    on_file = tmp_path / "on"
    d = CircadianDaemon.for_test(
        _FakeClient(),
        _cfg_security(triggers={"control_file": {"on_file": str(on_file)}}),
        grouped_light_rid="GL")
    t = _epoch(2, 0)
    on_file.write_text("")
    d._poll_security_files(t)
    assert d._security_aggregator.active(t) is True
    assert not on_file.exists()                 # consumed (edge-triggered)


def test_init_raises_listing_unknown_security_groups():
    from hueman.errors import ConfigError
    from hueman.state import Group

    class _State:
        def group(self, name):
            return Group(rid="G", rtype="zone", name=name, grouped_light_rid="GL",
                         light_rids=(), device_rids=())
        def group_optional(self, name):
            return self.group(name) if name == "Night Guide" else None
        def scene(self, name):
            return None
        @property
        def group_names(self):
            return ("Night Guide",)

    cfg = _cfg_security(groups=("Night Guide", "Ghost Zone"))
    with pytest.raises(ConfigError, match="Ghost Zone"):
        CircadianDaemon(_FakeClient(), _State(), cfg)


def test_probe_suppressed_during_security_show(monkeypatch):
    """_probe_tick must not write bias lights while security mode is active.

    Without the guard the probe thread can race with the security show: a TV-state
    edge calls _apply_bias_if_changed which writes to the bias light rids concurrently
    with the security frames. The guard (first line of _probe_tick) must short-circuit
    before any aggregator update or write.
    """
    cfg = _cfg_bias(
        {"probe": {"enabled": True, "host": "192.0.2.50", "debounce": "0s"}},
        lights={
            "Play bars": {"look": {"mirek": 153, "brightness": 28}, "idle": "off"},
            "Couch": {"look": {"hex": "1a0a00", "brightness": 5}, "idle": "circadian"},
        })
    d = CircadianDaemon.for_test(_FakeClient(), cfg, grouped_light_rid="GL")
    d._bias_rids = {"Play bars": "Lplay", "Couch": "Lcouch"}   # rids set; writes would happen
    d._security_active = True                                   # security owns the lights
    monkeypatch.setattr(d, "_probe_reachable", lambda: True)    # TV would be seen as on
    d._probe_tick(_epoch(13, 14))
    assert _light_writes(d) == {}, "probe must not write bias lights during a security show"


def test_startup_clears_stale_security_on_file_keeps_fresh(tmp_path):
    """A stale on_file (older than max_duration) is removed at startup so a restart
    can't re-fire the show; a fresh flag is left for the normal poll to honour."""
    import os
    on_file, off_file = tmp_path / "on", tmp_path / "off"
    d = CircadianDaemon.for_test(
        _FakeClient(),
        _cfg_security(
            triggers={"control_file": {"on_file": str(on_file), "off_file": str(off_file)}},
            max_duration="2s"),
        grouped_light_rid="GL")
    now = _epoch(2, 0)
    on_file.write_text(""); off_file.write_text("")
    os.utime(on_file, (now - 100, now - 100))     # stale (>2s old)
    os.utime(off_file, (now - 0.5, now - 0.5))    # fresh (<2s old)
    d._clear_stale_security_files(now)
    assert not on_file.exists()                   # stale removed
    assert off_file.exists()                      # fresh kept
# -- 2026-07-02 review fixes -------------------------------------------------- #
def test_probe_tick_feeds_aggregator_under_lock(monkeypatch):
    """The probe thread must mutate the shared TriggerAggregator while holding
    the daemon lock — an unlocked read-modify-write can race the tick/SSE
    threads' tv_on() commit and bypass the probe debounce entirely."""
    d = CircadianDaemon.for_test(
        _FakeClient(),
        _cfg_bias({"probe": {"enabled": True, "host": "192.0.2.50", "debounce": "5s"}}),
        grouped_light_rid="GL")
    d._bias_rids = {"Couch": "Lcouch"}
    lock_states = []
    agg = d._bias_aggregator
    orig_on, orig_off = agg.on, agg.off

    def spy_on(now, source="default"):
        lock_states.append(d._lock.locked())
        orig_on(now, source)

    def spy_off(now, source="default"):
        lock_states.append(d._lock.locked())
        orig_off(now, source)

    monkeypatch.setattr(agg, "on", spy_on)
    monkeypatch.setattr(agg, "off", spy_off)
    monkeypatch.setattr(d, "_probe_reachable", lambda: True)
    d._probe_tick(_epoch(13, 14))
    monkeypatch.setattr(d, "_probe_reachable", lambda: False)
    d._probe_tick(_epoch(13, 15))
    assert lock_states and all(lock_states), (
        f"aggregator mutated without the daemon lock: {lock_states}")


def test_sse_bias_trigger_applies_immediately():
    """An SSE-signalled TV flip must reach the lights on the event, like the
    probe path — not wait up to a full curve tick."""
    d = CircadianDaemon.for_test(
        _FakeClient(),
        _cfg_bias({"sse": {"on_trigger": "TV On", "off_trigger": "TV Off"}}),
        grouped_light_rid="GL")
    d._bias_sse_on_rid = "SON"
    d._bias_sse_off_rid = "SOFF"
    d._bias_rids = {"Couch": "Lcouch"}
    t = _epoch(13, 14)
    d._tick_once(t)                          # settle the startup edge
    d._client.writes.clear()
    d._handle_event(BridgeEvent("scene", "SON", {"recall": {}}), t + 1)
    lw = _light_writes(d)
    assert lw.get("Lcouch", {}).get("on") == {"on": True}   # hold applied now
    d._client.writes.clear()
    d._handle_event(BridgeEvent("scene", "SOFF", {"recall": {}}), t + 2)
    lw = _light_writes(d)
    assert "Lcouch" in lw                                    # release applied now


def test_cmd_target_not_latched_when_write_fails(monkeypatch):
    """A failed grouped_light write must not update the settle detector's
    commanded target — after a bridge outage the zone still holds the OLD level,
    and comparing it against a never-applied newer target reads as a human
    override and spuriously suspends the daemon."""
    d = CircadianDaemon.for_test(_FakeClient(), _cfg(), grouped_light_rid="GL")
    noon = _epoch(13, 14)

    def boom(rtype, rid, body):
        raise BridgeError("bridge unreachable")

    original = d._client.update_resource
    monkeypatch.setattr(d._client, "update_resource", boom)
    d._tick_once(noon)
    assert d._cmd_brightness is None          # target not latched on failure
    monkeypatch.setattr(d._client, "update_resource", original)
    d._tick_once(noon + 60)
    assert d._cmd_brightness is not None      # latched once the write lands

def test_security_chaos_snaps_and_alert_breathes(monkeypatch, tmp_path):
    """Chaos frames are hard cuts (dynamics 0); alert frames fade smoothly over
    one frame interval. The old build sent no dynamics at all, so bulbs applied
    their default fade and chaos read as a gradual colour drift."""
    d = CircadianDaemon.for_test(
        _FakeClient(), _cfg_security(alert_seconds=1, max_duration="2s"),
        grouped_light_rid="GL")
    d._security_rids = {"Night Guide": "GLng", "TV Viewing": "GLtv"}
    monkeypatch.setattr(d._stop_event, "wait", lambda *a, **k: None)
    start = _epoch(2, 0)
    seq = iter(start + 0.25 * i for i in range(0, 400))
    d._clock = lambda: next(seq)
    d._security_aggregator.on(start)
    d._run_security_show(start)
    gl_writes = [(rid, b) for (rt, rid, b) in d._client.writes if rt == "grouped_light"
                 and rid in ("GLng", "GLtv")]
    alert = [b for _r, b in gl_writes if b.get("dynamics", {}).get("duration") == 250]
    chaos = [b for _r, b in gl_writes if b.get("dynamics", {}).get("duration") == 0]
    assert alert, "alert frames must fade over one frame interval"
    assert chaos, "chaos frames must be zero-duration hard cuts"
    assert all("dynamics" in b for _r, b in gl_writes), "no write may omit dynamics"


def test_security_chaos_drives_member_lights_individually(monkeypatch, tmp_path):
    """With resolved member lights, the chaos phase writes individual `light`
    resources (decorrelated patchwork), not just the two group blobs."""
    d = CircadianDaemon.for_test(
        _FakeClient(), _cfg_security(alert_seconds=1, max_duration="3s"),
        grouped_light_rid="GL")
    d._security_rids = {"Night Guide": "GLng", "TV Viewing": "GLtv"}
    d._security_light_rids = tuple(f"Lr{i}" for i in range(6))
    d._rebuild_security_controller()
    monkeypatch.setattr(d._stop_event, "wait", lambda *a, **k: None)
    start = _epoch(2, 0)
    seq = iter(start + 0.25 * i for i in range(0, 400))
    d._clock = lambda: next(seq)
    d._security_aggregator.on(start)
    d._run_security_show(start)
    light_rids = {rid for (rt, rid, _b) in d._client.writes if rt == "light"}
    assert light_rids == {f"Lr{i}" for i in range(6)}


def test_security_frame_writes_are_parallel_not_serial(monkeypatch):
    """A frame's target writes must overlap: at 175ms budgets, serial ~100ms
    HTTPS round-trips overrun every frame and stretch the whole show."""
    import time as _time

    class _SlowClient(_FakeClient):
        def update_resource(self, rtype, rid, body):
            _time.sleep(0.15)
            super().update_resource(rtype, rid, body)

    d = CircadianDaemon.for_test(
        _SlowClient(), _cfg_security(alert_seconds=1, max_duration="2s"),
        grouped_light_rid="GL")
    d._security_rids = {"Night Guide": "GLng", "TV Viewing": "GLtv"}
    d._security_light_rids = tuple(f"Lr{i}" for i in range(6))
    d._rebuild_security_controller()
    monkeypatch.setattr(d._stop_event, "wait", lambda *a, **k: None)
    start = _epoch(2, 0)
    seq = iter(start + 0.25 * i for i in range(0, 400))
    d._clock = lambda: next(seq)
    d._security_aggregator.on(start)
    t0 = _time.monotonic()
    d._run_security_show(start)
    wall = _time.monotonic() - t0
    n_writes = len(d._client.writes)
    assert n_writes >= 10
    # Serial would take ~n_writes * 0.15s; parallel must beat half of that easily.
    assert wall < n_writes * 0.15 * 0.5, (
        f"{n_writes} writes took {wall:.2f}s — looks serial")


def test_security_show_logs_write_failure_count(monkeypatch, caplog):
    """Swallowed per-frame write failures must surface as an aggregate WARNING
    at CLEAR — silent drops are how 'chaos' degrades into sparse pops."""
    import logging as _logging

    class _FlakyClient(_FakeClient):
        def update_resource(self, rtype, rid, body):
            if rtype == "grouped_light" and rid in ("GLng", "GLtv"):
                raise BridgeError("429 too many requests")
            super().update_resource(rtype, rid, body)

    d = CircadianDaemon.for_test(
        _FlakyClient(), _cfg_security(alert_seconds=1, max_duration="2s"),
        grouped_light_rid="GL")
    d._security_rids = {"Night Guide": "GLng", "TV Viewing": "GLtv"}
    caplog.set_level(_logging.INFO, logger="hueman.circadian_daemon")
    monkeypatch.setattr(d._stop_event, "wait", lambda *a, **k: None)
    start = _epoch(2, 0)
    seq = iter(start + 0.25 * i for i in range(0, 400))
    d._clock = lambda: next(seq)
    d._security_aggregator.on(start)
    d._run_security_show(start)
    assert any("dropped" in r.message and r.levelno >= _logging.WARNING
               for r in caplog.records), \
        f"no drop-count warning; got: {[r.message for r in caplog.records]}"


def test_security_clear_at_night_restores_darkness(monkeypatch):
    """Out of window the 'normal' state is DARK, but the normal tick is
    deliberately hands-off overnight — so a nighttime show must explicitly
    drive the security zones off and re-assert the bias set on CLEAR, or the
    apartment freezes in the last chaos colours."""
    d = _bias_daemon_with_security()
    monkeypatch.setattr(d._stop_event, "wait", lambda *a, **k: None)
    start = _epoch(23, 30)                        # past hand-off: out of window
    seq = iter(start + 0.25 * i for i in range(0, 400))
    d._clock = lambda: next(seq)
    d._bias_off_written.update(d._bias_rids.values())   # poisoned pre-show state
    d._bias_last_applied_on = False
    d._security_aggregator.on(start)
    d._run_security_show(start)
    gl_off = {rid for (rt, rid, b) in d._client.writes
              if rt == "grouped_light" and rid in ("GLng", "GLtv")
              and b.get("on") == {"on": False}}
    assert gl_off == {"GLng", "GLtv"}, f"zones not driven dark on clear: {gl_off}"
    light_off = {rid for (rt, rid, b) in d._client.writes
                 if rt == "light" and b.get("on") == {"on": False}}
    assert "Lcouch" in light_off, "bias set not re-asserted (off suppression stuck)"
    assert d._cmd_on is False                     # own off, not a 'human override'


def _bias_daemon_with_security():
    cfg = Config.parse({
        "bridge": {"host": "x", "application_key": "k"},
        "location": {"lat": 45.5152, "lon": -122.6784, "tz_offset_hours": -7},
        "motion_policies": [],
        "circadian_daemon": {
            "zone": "Night Guide", "interval": "60s", "transition": "75s",
            "bias": {"lights": {"Couch": {"look": {"mirek": 200, "brightness": 10}}}},
        },
        "security": {
            "groups": ["Night Guide", "TV Viewing"],
            "alert": {"seconds": 1},
            "chaos": {"frame_interval": "250ms", "min_flash_interval": "350ms"},
            "max_duration": "2s",
        },
    })
    d = CircadianDaemon.for_test(_FakeClient(), cfg, grouped_light_rid="GL")
    d._security_rids = {"Night Guide": "GLng", "TV Viewing": "GLtv"}
    d._bias_rids = {"Couch": "Lcouch"}
    return d


def test_security_clear_in_window_reasserts_bias_as_edge(monkeypatch):
    """In-window CLEAR must reset the bias edge state so the next apply
    re-drives every viewing light (the show stomped them; 'already applied'
    bookkeeping from before the show is stale)."""
    d = _bias_daemon_with_security()
    monkeypatch.setattr(d._stop_event, "wait", lambda *a, **k: None)
    start = _epoch(13, 0)                         # daytime, in window
    seq = iter(start + 0.25 * i for i in range(0, 400))
    d._clock = lambda: next(seq)
    d._bias_last_applied_on = False               # pre-show bookkeeping
    d._security_aggregator.on(start)
    d._run_security_show(start)
    assert any(
        rt == "light" and rid == "Lcouch" and b.get("on") == {"on": True}
        for (rt, rid, b) in d._client.writes[-20:]
    ), "bias not re-driven after the show"


# -- night-guide: motion-triggered path lighting ----------------------------- #
def _cfg_night_guide(*, night_look=None, timeout="3m", start="sunrise", hand_off="22:34"):
    daemon_cfg = {
        "zone": "Night Guide", "interval": "60s", "transition": "75s",
        "start": start, "hand_off": hand_off,
        "night_guide": {
            "area": "Main Room",
            "look": {"brightness": 9, "hex": "#ff1400"},
            "timeout": timeout,
        },
    }
    if night_look is not None:
        daemon_cfg["night_look"] = night_look
    return Config.parse({
        "bridge": {"host": "x", "application_key": "k"},
        "location": {"lat": 45.5152, "lon": -122.6784, "tz_offset_hours": -7},
        "motion_policies": [],
        "circadian_daemon": daemon_cfg,
    })


def _motion_event(rid, on):
    return BridgeEvent("convenience_area_motion", rid, {"motion": {"motion": on}})


def test_night_guide_motion_writes_the_guide_look_when_parked():
    d = CircadianDaemon.for_test(_FakeClient(), _cfg_night_guide(), grouped_light_rid="GL")
    d._night_guide_motion_rids = {"MOTION1"}
    t = _epoch(23, 0)                              # out of window, mode starts NIGHT_IDLE
    assert not d._controller.in_window(t)
    d._handle_event(_motion_event("MOTION1", True), t)
    assert d._client.writes, "expected a guide-look write"
    rtype, rid, body = d._client.writes[-1]
    assert (rtype, rid) == ("grouped_light", "GL")
    assert body["dimming"] == {"brightness": 9.0}
    assert d._cmd_brightness == 9.0
    assert d._cmd_on is True
    assert d._night_guide_controller.state == "guiding"


def test_night_guide_does_not_engage_while_circadian_is_driving():
    d = CircadianDaemon.for_test(_FakeClient(), _cfg_night_guide(), grouped_light_rid="GL")
    d._night_guide_motion_rids = {"MOTION1"}
    t = _epoch(13, 0)                              # daytime, well within window
    assert d._controller.in_window(t)
    d._handle_event(_motion_event("MOTION1", True), t)
    assert d._client.writes == []
    assert d._night_guide_controller.state == "idle"


def test_night_guide_ignores_motion_from_an_unrelated_area():
    d = CircadianDaemon.for_test(_FakeClient(), _cfg_night_guide(), grouped_light_rid="GL")
    d._night_guide_motion_rids = {"MOTION1"}
    d._handle_event(_motion_event("OTHER", True), _epoch(23, 0))
    assert d._client.writes == []
    assert d._night_guide_controller.state == "idle"


def test_night_guide_ignores_a_motion_false_event():
    d = CircadianDaemon.for_test(_FakeClient(), _cfg_night_guide(), grouped_light_rid="GL")
    d._night_guide_motion_rids = {"MOTION1"}
    d._handle_event(_motion_event("MOTION1", False), _epoch(23, 0))
    assert d._client.writes == []
    assert d._night_guide_controller.state == "idle"


def test_night_guide_repeated_motion_extends_without_rewriting():
    d = CircadianDaemon.for_test(_FakeClient(), _cfg_night_guide(timeout="3m"), grouped_light_rid="GL")
    d._night_guide_motion_rids = {"MOTION1"}
    t = _epoch(23, 0)
    d._handle_event(_motion_event("MOTION1", True), t)
    n = len(d._client.writes)
    d._handle_event(_motion_event("MOTION1", True), t + 170)   # still guiding -> extends, no rewrite
    assert len(d._client.writes) == n
    d._tick_once(t + 170 + 170)                    # 170s since the LATEST motion -> still guiding
    assert d._night_guide_controller.state == "guiding"
    assert len(d._client.writes) == n
    d._tick_once(t + 170 + 181)                     # past 180s since the latest motion -> restores
    assert d._night_guide_controller.state == "idle"


def test_night_guide_snapshots_a_real_manual_override_before_writing_the_guide_look():
    snapshot_body = {
        "on": {"on": True},
        "dimming": {"brightness": 42.0},
        "color_temperature": {},
        "color": {"xy": {"x": 0.4, "y": 0.4}},
    }
    d = CircadianDaemon.for_test(
        _FakeClient(resource_to_return=snapshot_body), _cfg_night_guide(), grouped_light_rid="GL")
    d._night_guide_motion_rids = {"MOTION1"}
    t0 = _epoch(21, 0)                              # daytime: establish driving + a real override
    d._tick_once(t0)
    d._handle_event(_dim(d._cmd_brightness - 30.0), t0 + 30)
    d._handle_event(_dim(d._cmd_brightness - 30.0), t0 + 33)
    assert d._controller.mode == "suspended"
    t1 = _epoch(23, 0)                              # later, past hand-off, still suspended
    d._handle_event(_motion_event("MOTION1", True), t1)
    assert d._night_guide_snapshot == snapshot_body
    rtype, rid, body = d._client.writes[-1]
    assert body["dimming"] == {"brightness": 9.0}   # the guide look, not the snapshot


def test_night_guide_restores_the_exact_snapshot_after_timeout():
    snapshot_body = {
        "on": {"on": True},
        "dimming": {"brightness": 42.0},
        "color_temperature": {},
        "color": {"xy": {"x": 0.4, "y": 0.4}},
    }
    d = CircadianDaemon.for_test(
        _FakeClient(resource_to_return=snapshot_body),
        _cfg_night_guide(timeout="1m"), grouped_light_rid="GL")
    d._night_guide_motion_rids = {"MOTION1"}
    t0 = _epoch(21, 0)
    d._tick_once(t0)
    d._handle_event(_dim(d._cmd_brightness - 30.0), t0 + 30)
    d._handle_event(_dim(d._cmd_brightness - 30.0), t0 + 33)
    assert d._controller.mode == "suspended"
    t1 = _epoch(23, 0)
    d._handle_event(_motion_event("MOTION1", True), t1)
    n = len(d._client.writes)
    d._tick_once(t1 + 61)                           # past the 1-minute timeout
    assert len(d._client.writes) == n + 1
    rtype, rid, body = d._client.writes[-1]
    assert body["dimming"] == {"brightness": 42.0}
    assert body["color"] == {"xy": {"x": 0.4, "y": 0.4}}
    assert d._night_guide_snapshot is None
    assert d._controller.mode == "suspended"        # the override is still logically in force


def test_night_guide_hands_back_to_night_look_when_nothing_was_suspended():
    d = CircadianDaemon.for_test(
        _FakeClient(),
        _cfg_night_guide(night_look={"brightness": 1, "hex": "#ff0000"}, timeout="1m"),
        grouped_light_rid="GL")
    d._night_guide_motion_rids = {"MOTION1"}
    d._tick_once(_epoch(22, 0))                     # driving
    d._tick_once(_epoch(23, 0))                      # hand-off -> night_look write, NIGHT_IDLE
    assert d._client.writes[-1][2]["dimming"] == {"brightness": 1.0}
    t1 = _epoch(23, 30)
    d._handle_event(_motion_event("MOTION1", True), t1)
    assert d._client.writes[-1][2]["dimming"] == {"brightness": 9.0}     # guide look
    d._tick_once(t1 + 61)
    rtype, rid, body = d._client.writes[-1]
    assert body["dimming"] == {"brightness": 1.0}    # back to the resting night_look
    assert d._controller.mode == "night_idle"


def test_night_guide_hands_back_to_the_curve_if_window_reopens_during_the_episode():
    # start/hand_off pinned to fixed clock times (not sunrise) so the window
    # edge is deterministic in the test, independent of solar computation.
    d = CircadianDaemon.for_test(
        _FakeClient(),
        _cfg_night_guide(timeout="3m", start="06:00", hand_off="22:00"),
        grouped_light_rid="GL")
    d._night_guide_motion_rids = {"MOTION1"}
    t = _epoch(5, 58)                                # 2 min before the window opens at 06:00
    assert not d._controller.in_window(t)
    d._handle_event(_motion_event("MOTION1", True), t)
    assert d._client.writes[-1][2]["dimming"] == {"brightness": 9.0}
    t1 = t + 3 * 60 + 5                              # past the 3-min timeout; window now open
    assert d._controller.in_window(t1)
    d._tick_once(t1)
    rtype, rid, body = d._client.writes[-1]
    assert body["dimming"]["brightness"] != 9.0      # replaced by a curve sample, not the guide look
    assert d._controller.mode == "driving"


def _cfg_rhythm_and_night_guide():
    return Config.parse({
        "bridge": {"host": "x", "application_key": "k"},
        "location": {
            "lat": 45.5152, "lon": -122.6784, "tz_offset_hours": -7,
            "tz": "America/Los_Angeles",
        },
        "motion_policies": [],
        "circadian_daemon": {
            "zone": "Night Guide", "interval": "60s", "transition": "75s",
            "night_guide": {
                "area": "Main Room",
                "look": {"brightness": 9, "hex": "#ff1400"},
                "timeout": "3m",
            },
        },
        "rhythm": {"stage": "observe", "bedroom": "Bedroom"},
    })


def test_night_guide_and_rhythm_both_react_to_the_same_motion_event(caplog):
    # Two independent consumers of one SSE event -- neither should block the
    # other now that the routing no longer gates night-guide on presence
    # being configured.
    d = CircadianDaemon.for_test(_FakeClient(), _cfg_rhythm_and_night_guide(), grouped_light_rid="GL")
    d._night_guide_motion_rids = {"MOTION1"}
    d._rhythm_motion_rooms = {"MOTION1": "Living room"}
    caplog.set_level(logging.DEBUG, logger="hueman.circadian_daemon")
    d._handle_event(_motion_event("MOTION1", True), _epoch(23, 0))
    assert d._client.writes, "night-guide should have written the guide look"
    assert d._night_guide_controller.state == "guiding"
    assert any(
        "rhythm: motion in 'Living room'" in r.message for r in caplog.records
    ), "rhythm should still have recorded the same motion event"


# -- matches-command fast classification (the 2026-08-22 play-bar TV-off lag) - #
def test_bias_command_echo_classifies_immediately_and_edge_writes_without_defer():
    # LIVE 2026-08-22: per-tick hold writes keep pushing _bias_cmd_fade_until
    # forward, so a bias light's own echo stayed "not yet judged" for as long
    # as the TV was on; the TV-off edge then deferred the night-look write for
    # minutes. A fresh value within override_band of the commanded brightness
    # must classify as "self" immediately, letting the edge write land in the
    # same tick.
    cfg = Config.parse({
        "bridge": {"host": "x", "application_key": "k"},
        "location": {"lat": 45.5152, "lon": -122.6784, "tz_offset_hours": -7},
        "motion_policies": [],
        "circadian_daemon": {
            "zone": "Night Guide", "interval": "60s", "transition": "75s",
            "night_look": {"brightness": 1, "hex": "#ff0000"},
            "bias": {
                "lights": {
                    "Couch": {"look": {"hex": "1a0a00", "brightness": 95}, "idle": "circadian"},
                },
                "triggers": {"sse": {"on_trigger": "On", "off_trigger": "Off"}},
            },
        },
    })
    d = CircadianDaemon.for_test(_FakeClient(), cfg, grouped_light_rid="GL")
    d._bias_rids = {"Couch": "Lcouch"}
    t = _epoch(23, 30)                                  # out of window: night look rules
    d._bias_aggregator.on(t)
    d._tick_once(t)                                     # TV on -> hold 95% written; cmd latched
    d._handle_event(_light_ev("Lcouch", dimming={"brightness": 94.9}), t + 5)
    assert d._bias_obs["Lcouch"][2] is True             # command echo -> classified, no settle wait
    d._tick_once(t + 60)                                # hold re-write refreshes fade_until
    d._client.writes.clear()
    d._bias_aggregator.off(t + 65)                      # TV off right after a hold write
    d._tick_once(t + 65)
    lw = _light_writes(d)
    assert lw["Lcouch"]["dimming"] == {"brightness": 1.0}   # night look written THIS tick


def test_bias_far_echo_still_waits_and_defers_at_edge():
    # The guard the fast path must not weaken: a value far from the commanded
    # brightness (a human mid-adjustment) stays unjudged until it settles, and
    # an edge arriving meanwhile still defers rather than writes over it.
    d = _bias_daemon()
    t = _epoch(13, 14)
    d._bias_aggregator.on(t)
    d._tick_once(t)                                     # Couch hold 5% written; cmd latched
    d._handle_event(_light_ev("Lcouch", dimming={"brightness": 60.0}), t + 5)
    assert d._bias_obs["Lcouch"][2] is False            # far from 5% -> NOT fast-classified


def test_zone_command_echo_classifies_immediately():
    # Zone twin: a grouped-light echo within override_band of the commanded
    # target classifies instantly (debug "self"), instead of waiting out the
    # settle window behind our own fade.
    daemon = CircadianDaemon.for_test(_FakeClient(), _cfg(), grouped_light_rid="GL")
    t0 = _epoch(12, 0)
    daemon._tick_once(t0)
    target = daemon._cmd_brightness
    daemon._handle_event(_dim(target - 1.0), t0 + 0.5)  # single echo, mid-own-fade
    assert daemon._obs_classified is True
    assert daemon._controller.mode == "driving"


def test_zone_far_value_still_waits_for_settle():
    # A far value mid-fade must stay unjudged (no premature suspend, no
    # premature classification) — the fast path applies only to command echoes.
    daemon = CircadianDaemon.for_test(_FakeClient(), _cfg(), grouped_light_rid="GL")
    t0 = _epoch(12, 0)
    daemon._tick_once(t0)
    daemon._handle_event(_dim(5.0), t0 + 0.5)           # far from any daytime target
    assert daemon._obs_classified is False
    assert daemon._controller.mode == "driving"
