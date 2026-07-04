"""Tests for the motion/timing decision engine.

These cover the behaviours the user specifically asked for: circadian on-motion
colour, dim-before-off with recovery, short dead-of-night timeouts with a
nightlight standby, the lux gate, and honoured manual overrides.
"""

from __future__ import annotations

from hue_iac.engine import Phase, PolicyEngine
from tests.conftest import epoch_at, make_config

LOCATION = (40.7128, -74.0060, -5.0)


def _engine(policy_overrides: dict | None = None) -> PolicyEngine:
    policy = {
        "name": "Office",
        "sensor": "Office sensor",
        "rooms": ["Office"],
        "timeslots": [
            {"name": "day", "start": "07:00", "on_motion": "circadian", "timeout": "10s"},
            {
                "name": "night",
                "start": "23:00",
                "on_motion": {"brightness": 8, "color": {"hex": "#ff2a00"}},
                "timeout": "90s",
                "standby": {"on": True, "brightness": 2, "color": {"hex": "#ff0000"}},
            },
        ],
    }
    if policy_overrides:
        policy.update(policy_overrides)
    config = make_config(policy)
    return PolicyEngine(config.motion_policies[0], LOCATION, config.circadian)


def test_motion_during_day_uses_circadian_color() -> None:
    """Daytime motion turns lights on with a resolved colour temperature."""
    engine = _engine()
    actions = engine.on_motion("Office", present=True, ts=epoch_at(13))
    assert len(actions) == 1
    assert actions[0].state.on is True
    assert actions[0].state.mirek is not None  # circadian resolved to a mirek
    assert engine.phase_of("Office") is Phase.ACTIVE


def test_dim_before_off_then_standby() -> None:
    """After the timeout the engine dims, then drops to standby."""
    engine = _engine({"dim_before_off": {"duration": "5s", "recovery": True}})
    start = epoch_at(13)
    engine.on_motion("Office", present=True, ts=start)

    dim_actions = engine.tick(start + 11)  # past the 10s timeout
    assert engine.phase_of("Office") is Phase.DIMMING
    assert dim_actions[0].state.on is True

    off_actions = engine.tick(start + 17)  # past the 5s dim window
    assert engine.phase_of("Office") is Phase.STANDBY
    assert off_actions[0].state.on is False  # daytime standby defaults to off


def test_motion_during_dim_recovers() -> None:
    """Motion during the dim warning restores full brightness immediately."""
    engine = _engine({"dim_before_off": {"duration": "5s", "recovery": True}})
    start = epoch_at(13)
    engine.on_motion("Office", present=True, ts=start)
    engine.tick(start + 11)
    assert engine.phase_of("Office") is Phase.DIMMING

    recover = engine.on_motion("Office", present=True, ts=start + 12)
    assert engine.phase_of("Office") is Phase.ACTIVE
    assert recover and recover[0].state.on is True


def test_motion_during_dim_without_recovery_is_ignored() -> None:
    """With recovery disabled, motion during the dim warning is ignored."""
    engine = _engine({"dim_before_off": {"duration": "5s", "recovery": False}})
    start = epoch_at(13)
    engine.on_motion("Office", present=True, ts=start)
    engine.tick(start + 11)
    assert engine.phase_of("Office") is Phase.DIMMING

    assert engine.on_motion("Office", present=True, ts=start + 12) == []
    assert engine.phase_of("Office") is Phase.DIMMING  # dim runs its course
    engine.tick(start + 17)
    assert engine.phase_of("Office") is Phase.STANDBY


def test_repeated_motion_while_active_does_not_re_emit() -> None:
    """Sustained motion extends the timer without re-issuing commands."""
    engine = _engine()
    start = epoch_at(13)
    first = engine.on_motion("Office", present=True, ts=start)
    again = engine.on_motion("Office", present=True, ts=start + 2)
    assert first and again == []


def test_night_motion_is_soft_red_with_nightlight_standby() -> None:
    """At night motion gives soft red, and standby is the dim red nightlight."""
    engine = _engine()
    night = epoch_at(2)  # 02:00 -> night slot (wraps from 23:00)
    on_actions = engine.on_motion("Office", present=True, ts=night)
    assert on_actions[0].state.hex == "ff2a00"
    assert on_actions[0].state.brightness == 8

    standby = engine.tick(night + 100)  # past the 90s timeout
    assert engine.phase_of("Office") is Phase.STANDBY
    assert standby[0].state.on is True            # nightlight stays on
    assert standby[0].state.hex == "ff0000"
    assert standby[0].state.brightness == 2


def test_lux_gate_suppresses_when_bright() -> None:
    """The lux threshold blocks switching on when the room is already bright."""
    engine = _engine({"light_level": {"threshold_lux": 50}})
    assert engine.on_motion("Office", present=True, ts=epoch_at(13), lux=100) == []
    assert engine.on_motion("Office", present=True, ts=epoch_at(13), lux=5)


def test_lux_from_separate_report_suppresses_subsequent_motion() -> None:
    """Lux arrives on its own light_level report; the next motion must honour it.

    This mirrors the real runtime wiring: motion events carry no lux, so the
    gate only works if the engine remembers the last reported ambient level.
    """
    engine = _engine({"light_level": {"threshold_lux": 50}})
    ts = epoch_at(13)
    engine.on_motion("Office", present=False, ts=ts, lux=100)  # ambient reading only
    assert engine.on_motion("Office", present=True, ts=ts) == []  # remembered lux suppresses


def test_bright_ambient_does_not_cut_off_an_occupied_room() -> None:
    """The lux gate blocks the initial turn-on only, never sustained presence.

    Regression: a room lit by its own lights raises ambient lux above the
    threshold; ongoing motion must still refresh the idle timer so the room is
    not timed off while someone is in it.
    """
    engine = _engine({"light_level": {"threshold_lux": 50}})
    start = epoch_at(13)  # day slot, 10s timeout
    engine.on_motion("Office", present=True, ts=start)  # dark room -> ON, ACTIVE
    assert engine.phase_of("Office") is Phase.ACTIVE
    engine.on_motion("Office", present=False, ts=start + 2, lux=120)  # own light brightens room
    engine.on_motion("Office", present=True, ts=start + 8)  # still occupied (motion carries no lux)
    assert engine.tick(start + 12) == []  # must NOT time off; presence refreshed the timer
    assert engine.phase_of("Office") is Phase.ACTIVE


def test_manual_override_pauses_then_resumes() -> None:
    """A manual change pauses motion until the override window elapses."""
    engine = _engine({"manual_override": {"respect": True, "pause": "1h"}})
    start = epoch_at(13)
    engine.on_manual_override("Office", ts=start)
    assert engine.phase_of("Office") is Phase.OVERRIDDEN
    assert engine.on_motion("Office", present=True, ts=start + 60) == []  # honoured

    after = engine.on_motion("Office", present=True, ts=start + 3601)  # past 1h
    assert after and after[0].state.on is True


def test_manual_off_rearms_motion() -> None:
    """Turning the room fully off re-arms motion before the window ends."""
    engine = _engine({"manual_override": {"respect": True, "pause": "1h", "resume_on_off": True}})
    start = epoch_at(13)
    engine.on_manual_override("Office", ts=start)
    engine.on_manual_off("Office", ts=start + 30)
    assert engine.phase_of("Office") is Phase.STANDBY
    assert engine.on_motion("Office", present=True, ts=start + 31)  # responds again
