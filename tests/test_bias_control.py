"""Tests for the pure TV bias-hold decision core and trigger aggregator."""

from __future__ import annotations

from hue_iac.bias_control import (
    BiasDrive,
    BiasHold,
    BiasOff,
    TriggerAggregator,
    bias_actions,
    unknown_bias_lights,
)
from hue_iac.circadian_control import DriveTo
from hue_iac.config import BiasLight, BiasSpec, Color, LightState


def _spec(*lights: tuple[str, str]) -> BiasSpec:
    """Build a BiasSpec from (name, idle) pairs with a dummy hold look."""
    bl = tuple(
        BiasLight(
            name=name,
            look=LightState(on=True, brightness=28.0, color=Color(mode="ct", mirek=153)),
            idle=idle,
        )
        for name, idle in lights
    )
    return BiasSpec(
        lights=bl, transition_ms=2_000, sse_on=None, sse_off=None,
        file_on=None, file_off=None,
        probe_enabled=False, probe_host=None, probe_mode="tcp", probe_port=3001,
        probe_interval_ms=5000, probe_debounce_ms=5000,
    )


CURVE = DriveTo(brightness=40.0, mirek=300, transition_ms=75_000)


def test_tv_on_holds_every_light() -> None:
    """TV on -> every bias light holds its look, regardless of idle/window."""
    spec = _spec(("Play bars", "off"), ("Couch", "circadian"))
    actions = bias_actions(spec, tv_on=True, in_window=False, curve=None,
                           transition_ms=75_000, fade_off_ms=90_000)
    assert all(isinstance(a, BiasHold) for a in actions)
    assert {a.light for a in actions} == {"Play bars", "Couch"}
    assert actions[0].look.color.mirek == 153


def test_tv_off_in_window_circadian_drives_off_idles() -> None:
    """TV off, in window: idle=circadian follows the curve; idle=off goes off."""
    spec = _spec(("Play bars", "off"), ("Couch", "circadian"))
    actions = {a.light: a for a in bias_actions(
        spec, tv_on=False, in_window=True, curve=CURVE,
        transition_ms=75_000, fade_off_ms=90_000)}
    assert isinstance(actions["Play bars"], BiasOff)
    assert actions["Play bars"].transition_ms == 90_000
    assert isinstance(actions["Couch"], BiasDrive)
    assert actions["Couch"].brightness == 40.0 and actions["Couch"].mirek == 300
    assert actions["Couch"].transition_ms == 75_000


def test_tv_off_out_of_window_all_off() -> None:
    """TV off, out of window: even circadian-idle lights go off."""
    spec = _spec(("Couch", "circadian"))
    actions = bias_actions(spec, tv_on=False, in_window=False, curve=None,
                           transition_ms=75_000, fade_off_ms=90_000)
    assert isinstance(actions[0], BiasOff)


def test_circadian_idle_without_curve_goes_off() -> None:
    """In window but no curve sample available -> circadian idle falls back to off."""
    spec = _spec(("Couch", "circadian"))
    actions = bias_actions(spec, tv_on=False, in_window=True, curve=None,
                           transition_ms=75_000, fade_off_ms=90_000)
    assert isinstance(actions[0], BiasOff)


def _spec_edge(*lights: tuple[str, str], edge_ms: int = 2_000) -> BiasSpec:
    """_spec plus an explicit edge transition (BiasSpec.transition_ms)."""
    base = _spec(*lights)
    return BiasSpec(
        lights=base.lights, transition_ms=edge_ms, sse_on=None, sse_off=None,
        file_on=None, file_off=None,
        probe_enabled=False, probe_host=None, probe_mode="tcp", probe_port=3001,
        probe_interval_ms=5000, probe_debounce_ms=5000,
    )


def test_edge_uses_short_transition_for_all_actions() -> None:
    """On a TV-state flip every action fades over spec.transition_ms, not the
    steady-state 75s/90s (which produced the observed minute-of-nothing)."""
    spec = _spec_edge(("Play bars", "off"), ("Couch", "circadian"), edge_ms=2_000)
    on_actions = bias_actions(spec, tv_on=True, in_window=True, curve=CURVE,
                              transition_ms=75_000, fade_off_ms=90_000, edge=True)
    assert all(a.transition_ms == 2_000 for a in on_actions)          # holds snap in
    off_actions = {a.light: a for a in bias_actions(
        spec, tv_on=False, in_window=True, curve=CURVE,
        transition_ms=75_000, fade_off_ms=90_000, edge=True)}
    assert off_actions["Couch"].transition_ms == 2_000                # drive snaps to curve
    assert off_actions["Play bars"].transition_ms == 2_000            # off snaps off


def test_non_edge_keeps_steady_state_fades() -> None:
    """Steady-state ticks keep the long fades: hold/drive re-PUT at 75s, off at 90s."""
    spec = _spec_edge(("Play bars", "off"), ("Couch", "circadian"))
    hold = bias_actions(spec, tv_on=True, in_window=True, curve=CURVE,
                        transition_ms=75_000, fade_off_ms=90_000, edge=False)
    assert all(a.transition_ms == 75_000 for a in hold)
    idle = {a.light: a for a in bias_actions(
        spec, tv_on=False, in_window=True, curve=CURVE,
        transition_ms=75_000, fade_off_ms=90_000, edge=False)}
    assert idle["Couch"].transition_ms == 75_000
    assert idle["Play bars"].transition_ms == 90_000


def test_aggregator_reports_last_change_source() -> None:
    """The aggregator remembers which source caused the last raw flip, for logging."""
    agg = TriggerAggregator(debounce_ms=0)
    agg.on(0.0, "probe")
    assert agg.last_source == "probe"
    agg.on(1.0, "file")                    # raw already on: no flip, source unchanged
    assert agg.last_source == "probe"
    agg.off(2.0, "file")                   # probe still on -> raw stays on
    assert agg.last_source == "probe"
    agg.off(3.0, "probe")                  # raw flips off
    assert agg.last_source == "probe"


def test_aggregator_immediate_no_debounce() -> None:
    agg = TriggerAggregator(debounce_ms=0)
    assert agg.tv_on(0.0) is False
    agg.on(1.0)
    assert agg.tv_on(1.0) is True
    agg.off(2.0)
    assert agg.tv_on(2.0) is False


def test_aggregator_debounce_suppresses_bounce() -> None:
    """A quick on/off bounce inside the debounce window never flips the state."""
    agg = TriggerAggregator(debounce_ms=5000)
    agg.on(0.0)
    assert agg.tv_on(1.0) is False        # only 1s stable, < 5s
    agg.off(2.0)                          # bounced back before committing
    assert agg.tv_on(3.0) is False
    assert agg.tv_on(100.0) is False      # never committed to on


def test_aggregator_debounce_commits_after_window() -> None:
    agg = TriggerAggregator(debounce_ms=5000)
    agg.on(0.0)
    assert agg.tv_on(4.9) is False
    assert agg.tv_on(5.0) is True         # held stable >= debounce


def test_unknown_bias_lights_flags_names_absent_from_bridge() -> None:
    """Bias light names not present on the bridge are reported, in declared order."""
    spec = _spec(("Couch", "circadian"), ("Play bars", "off"), ("Tree Left", "circadian"))
    assert unknown_bias_lights(spec, ["Couch", "Side Table"]) == ["Play bars", "Tree Left"]


def test_unknown_bias_lights_empty_when_all_present() -> None:
    """No unknowns when every bias light resolves to a real bridge light."""
    spec = _spec(("Couch", "circadian"))
    assert unknown_bias_lights(spec, ["Couch", "Other"]) == []


def test_aggregator_active_is_alias_of_tv_on() -> None:
    agg = TriggerAggregator(debounce_ms=0)
    assert agg.active(0.0) is False
    agg.on(1.0)
    assert agg.active(1.0) is True and agg.tv_on(1.0) is True
    agg.off(2.0)
    assert agg.active(2.0) is False


def test_aggregator_reset_clears_every_source() -> None:
    # The consumer can end an episode unilaterally (e.g. security max-duration):
    # reset drops ALL sources' on-signals, whatever armed it.
    agg = TriggerAggregator(debounce_ms=0)
    agg.on(1.0, "file")
    agg.on(1.5, "sse")
    agg.reset(2.0)
    assert agg.active(2.0) is False
    agg.on(3.0, "file")                  # a fresh arm after reset still works
    assert agg.active(3.0) is True
