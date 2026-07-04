"""Tests for the live MotionController: lux wiring, echo/override, reconnection.

These never touch a bridge. A canned BridgeState is built from a FakeClient, and
the SSE stream is replaced with an injectable fake factory so the reconnect loop
runs deterministically.
"""

from __future__ import annotations

import pytest
import requests

from hueman.config import Config
from hueman.engine import Action, Phase, TargetState
from hueman.errors import AuthError, BridgeError
from hueman.state import BridgeState
from hueman.watch import BridgeEvent, MotionController

GL = "gl1"        # grouped_light rid for the Office room
MOT = "mot1"      # motion service rid
LL = "ll1"        # light_level service rid


class FakeClient:
    def __init__(self, resources: dict) -> None:
        self._resources = resources
        self.updates: list[tuple[str, str, dict]] = []

    def get_resources(self, rtype: str) -> list[dict]:
        return self._resources.get(rtype, [])

    def update_resource(self, rtype: str, rid: str, body: dict) -> None:
        self.updates.append((rtype, rid, body))


class FakeStream:
    """Stands in for HueEventStream: yields canned events or raises on open."""

    def __init__(self, events=None, error=None) -> None:
        self._events = events or []
        self._error = error

    def events(self):
        if self._error is not None:
            raise self._error
        for event in self._events:
            yield event


class FakeFactory:
    def __init__(self, streams) -> None:
        self._streams = list(streams)
        self.calls = 0

    def __call__(self) -> FakeStream:
        self.calls += 1
        if self._streams:
            return self._streams.pop(0)
        return FakeStream()  # empty stream ends cleanly


def _build(threshold_lux: int | None = None):
    devices = [
        {
            "id": "devA",
            "metadata": {"name": "Lamp A"},
            "services": [{"rtype": "light", "rid": "litA"}],
        },
        {
            "id": "devS",
            "metadata": {"name": "Office sensor"},
            "services": [
                {"rtype": "motion", "rid": MOT},
                {"rtype": "light_level", "rid": LL},
            ],
        },
    ]
    rooms = [
        {
            "id": "roomO",
            "metadata": {"name": "Office"},
            "children": [{"rtype": "device", "rid": "devA"}, {"rtype": "device", "rid": "devS"}],
            "services": [{"rtype": "grouped_light", "rid": GL}],
        }
    ]
    client = FakeClient({"device": devices, "room": rooms, "zone": [], "scene": [], "motion": []})
    state = BridgeState(client).load()  # type: ignore[arg-type]

    policy = {
        "name": "Office",
        "sensor": "Office sensor",
        "rooms": ["Office"],
        "timeslots": [{"name": "day", "start": "00:00", "on_motion": "circadian", "timeout": "10s"}],
    }
    if threshold_lux is not None:
        policy["light_level"] = {"threshold_lux": threshold_lux}
    doc = {
        "bridge": {"host": "192.0.2.10", "application_key": "k"},
        "location": {"lat": 40.7, "lon": -74.0, "tz_offset_hours": -5},
        "motion_policies": [policy],
    }
    config = Config.parse(doc)
    return client, state, config


def _controller(client, state, config, **kw) -> MotionController:
    clock = kw.pop("clock", lambda: 1_000_000.0)
    return MotionController(client, state, config, clock=clock, **kw)  # type: ignore[arg-type]


def _lux_raw(lux: float) -> int:
    """Inverse of the controller's lux decode (raw = 10000*log10(lux)+1)."""
    import math

    return int(round(10000 * math.log10(lux) + 1))


def _gl_writes(client) -> list[dict]:
    return [body for rtype, rid, body in client.updates if rid == GL]


# -- lux gate, end to end through the controller ----------------------------- #
def test_light_level_event_then_motion_is_suppressed_when_bright() -> None:
    client, state, config = _build(threshold_lux=12)
    ctrl = _controller(client, state, config)
    ctrl._handle_event(BridgeEvent("light_level", LL, {"light": {"light_level": _lux_raw(100)}}))
    before = len(_gl_writes(client))
    ctrl._handle_event(BridgeEvent("motion", MOT, {"motion": {"motion": True}}))
    assert len(_gl_writes(client)) == before  # bright room -> motion writes nothing


def test_light_level_event_then_motion_turns_on_when_dark() -> None:
    client, state, config = _build(threshold_lux=12)
    ctrl = _controller(client, state, config)
    ctrl._handle_event(BridgeEvent("light_level", LL, {"light": {"light_level": _lux_raw(1)}}))
    ctrl._handle_event(BridgeEvent("motion", MOT, {"motion": {"motion": True}}))
    writes = _gl_writes(client)
    assert writes and writes[-1].get("on", {}).get("on") is True


# -- echo buffer / manual override ------------------------------------------- #
def test_self_write_is_not_treated_as_manual_override() -> None:
    client, state, config = _build()
    ctrl = _controller(client, state, config)
    ctrl._apply(Action("Office", TargetState(on=True, brightness=50.0, mirek=300), "self"))
    ctrl._handle_event(BridgeEvent("grouped_light", GL, {"on": {"on": True}}))  # the echo
    assert ctrl._engine_for_area("Office").phase_of("Office") is not Phase.OVERRIDDEN


def test_unrecognised_grouped_light_change_pauses_automation() -> None:
    client, state, config = _build()
    ctrl = _controller(client, state, config)
    ctrl._handle_event(BridgeEvent("grouped_light", GL, {"on": {"on": True}}))  # no prior echo
    assert ctrl._engine_for_area("Office").phase_of("Office") is Phase.OVERRIDDEN


# -- robustness: bad events, reconnection, auth ------------------------------ #
def test_malformed_event_does_not_raise() -> None:
    client, state, config = _build()
    ctrl = _controller(client, state, config)
    # on: null is a present-but-None field that naive .get(...) chaining trips on.
    ctrl._safe_handle_event(BridgeEvent("grouped_light", GL, {"on": None}))
    # a well-formed event after the bad one must still be handled
    ctrl._safe_handle_event(BridgeEvent("grouped_light", GL, {"on": {"on": True}}))
    assert ctrl._engine_for_area("Office").phase_of("Office") is Phase.OVERRIDDEN


def test_reconnects_after_stream_error() -> None:
    client, state, config = _build()
    sleeps: list[float] = []
    factory = FakeFactory([
        FakeStream(error=requests.exceptions.ConnectionError("boom")),  # 1st: drop
        FakeStream(events=[]),                                          # 2nd: clean
    ])
    ctrl = _controller(
        client, state, config, stream_factory=factory, sleep=lambda s: sleeps.append(s)
    )
    ctrl.run(tick_interval=1000.0, max_reconnects=2)
    assert factory.calls == 2          # it retried after the drop
    assert sleeps                       # backoff slept before reconnecting


def test_converges_to_standby_once_not_on_every_reconnect() -> None:
    """Re-converging on each reconnect would force occupied/overridden rooms off."""
    client, state, config = _build()
    factory = FakeFactory([
        FakeStream(error=requests.exceptions.ConnectionError("boom")),
        FakeStream(error=requests.exceptions.ConnectionError("boom2")),
        FakeStream(events=[]),
    ])
    ctrl = _controller(client, state, config, stream_factory=factory, sleep=lambda s: None)
    ctrl.run(tick_interval=1000.0, max_reconnects=3)
    off_writes = [b for _, rid, b in client.updates if rid == GL and b == {"on": {"on": False}}]
    assert len(off_writes) == 1  # startup converge only; reconnects must not re-force standby


def test_auth_failure_is_fatal_not_retried() -> None:
    client, state, config = _build()
    factory = FakeFactory([FakeStream(error=AuthError("bad key"))])
    ctrl = _controller(
        client, state, config, stream_factory=factory, sleep=lambda s: None
    )
    with pytest.raises(AuthError):
        ctrl.run(tick_interval=1000.0, max_reconnects=5)
    assert factory.calls == 1          # did not keep retrying an auth failure
