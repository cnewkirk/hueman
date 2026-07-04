"""Tests for declarative area/light assignment and the orphan guard."""

from __future__ import annotations

import pytest

from hueman.config import Config
from hueman.errors import ConfigError
from hueman.reconcile import AreaReconciler, ChangeType
from hueman.state import BridgeState


class FakeClient:
    """Minimal stand-in for HueClient backed by canned resource lists."""

    def __init__(self, resources: dict[str, list[dict]]) -> None:
        self._resources = resources
        self.updates: list[tuple[str, str, dict]] = []
        self.creates: list[tuple[str, dict]] = []

    def get_resources(self, rtype: str) -> list[dict]:
        return self._resources.get(rtype, [])

    def update_resource(self, rtype: str, rid: str, body: dict) -> None:
        self.updates.append((rtype, rid, body))

    def create_resource(self, rtype: str, body: dict) -> str:
        self.creates.append((rtype, body))
        return "new-rid"


def _device(name: str, device_rid: str, light_rid: str) -> dict:
    return {
        "id": device_rid,
        "metadata": {"name": name},
        "services": [{"rtype": "light", "rid": light_rid}],
    }


def _sensor_device(name: str, device_rid: str) -> dict:
    """A non-light accessory (e.g. a motion sensor) that can be a room child."""
    return {
        "id": device_rid,
        "metadata": {"name": name},
        "services": [{"rtype": "motion", "rid": f"{device_rid}-motion"}],
    }


def _room(name: str, rid: str, device_rids: list[str]) -> dict:
    children = [{"rtype": "device", "rid": rid} for rid in device_rids]
    return {"id": rid, "metadata": {"name": name}, "children": children, "services": []}


def _bridge_with(devices: list[dict], rooms: list[dict]) -> tuple[FakeClient, BridgeState]:
    client = FakeClient({"device": devices, "room": rooms, "zone": [], "scene": [], "motion": []})
    state = BridgeState(client).load()  # type: ignore[arg-type]
    return client, state


def _zone(name: str, rid: str, light_rids: list[str]) -> dict:
    """A bridge zone whose children reference light services directly."""
    children = [{"rtype": "light", "rid": r} for r in light_rids]
    return {"id": rid, "metadata": {"name": name}, "children": children, "services": []}


def _bridge(
    devices: list[dict],
    rooms: list[dict] | None = None,
    zones: list[dict] | None = None,
) -> tuple[FakeClient, BridgeState]:
    client = FakeClient(
        {"device": devices, "room": rooms or [], "zone": zones or [], "scene": [], "motion": []}
    )
    state = BridgeState(client).load()  # type: ignore[arg-type]
    return client, state


def _config(areas: dict, require: bool = True) -> Config:
    doc = {
        "bridge": {"host": "192.0.2.10", "application_key": "k"},
        "location": {"lat": 0, "lon": 0, "tz_offset_hours": 0},
        "require_all_lights_assigned": require,
        "areas": areas,
        "motion_policies": [],
    }
    return Config.parse(doc)


def test_unassigned_light_is_blocked() -> None:
    """A light in no declared room is reported as blocked drift."""
    devices = [_device("Lamp A", "devA", "litA"), _device("Lamp B", "devB", "litB")]
    rooms = [_room("Office", "roomO", ["devA", "devB"])]
    client, state = _bridge_with(devices, rooms)
    config = _config({"rooms": [{"name": "Office", "lights": ["Lamp A"]}]})

    changes = AreaReconciler(client, state, config).plan()  # type: ignore[arg-type]
    blocked = [c for c in changes if c.change_type is ChangeType.BLOCKED]
    assert [c.name for c in blocked] == ["Lamp B"]


def test_membership_drift_produces_update() -> None:
    """Declaring a light into a room it is not yet in plans an update."""
    devices = [_device("Lamp A", "devA", "litA"), _device("Lamp B", "devB", "litB")]
    rooms = [_room("Office", "roomO", ["devA"])]
    client, state = _bridge_with(devices, rooms)
    config = _config({"rooms": [{"name": "Office", "lights": ["Lamp A", "Lamp B"]}]}, require=False)

    reconciler = AreaReconciler(client, state, config)  # type: ignore[arg-type]
    changes = reconciler.plan()
    update = next(c for c in changes if c.change_type is ChangeType.UPDATE)
    assert update.name == "Office"

    reconciler.apply(update)
    assert client.updates
    rtype, rid, body = client.updates[0]
    assert rtype == "room" and rid == "roomO"
    assert {child["rid"] for child in body["children"]} == {"devA", "devB"}


def test_missing_room_is_created() -> None:
    """Declaring a room the bridge lacks plans (and applies) a create."""
    devices = [_device("Lamp A", "devA", "litA")]
    client, state = _bridge_with(devices, rooms=[])
    config = _config({"rooms": [{"name": "Office", "type": "office", "lights": ["Lamp A"]}]})

    reconciler = AreaReconciler(client, state, config)  # type: ignore[arg-type]
    create = next(c for c in reconciler.plan() if c.change_type is ChangeType.CREATE)
    reconciler.apply(create)
    assert client.creates
    rtype, body = client.creates[0]
    assert rtype == "room"
    assert body["metadata"] == {"name": "Office", "archetype": "office"}


def test_room_with_nonlight_device_is_noop_not_update() -> None:
    """A motion sensor living in the room must not be read as membership drift."""
    devices = [_device("Lamp A", "devA", "litA"), _sensor_device("Office sensor", "devS")]
    rooms = [_room("Office", "roomO", ["devA", "devS"])]
    client, state = _bridge_with(devices, rooms)
    config = _config({"rooms": [{"name": "Office", "lights": ["Lamp A"]}]}, require=False)

    changes = AreaReconciler(client, state, config).plan()  # type: ignore[arg-type]
    office = next(c for c in changes if c.name == "Office")
    assert office.change_type is ChangeType.NOOP


def test_apply_preserves_nonlight_device_children() -> None:
    """Correcting light membership must not evict the room's sensor/switch."""
    devices = [
        _device("Lamp A", "devA", "litA"),
        _device("Lamp B", "devB", "litB"),
        _sensor_device("Office sensor", "devS"),
    ]
    rooms = [_room("Office", "roomO", ["devA", "devS"])]  # add Lamp B; keep the sensor
    client, state = _bridge_with(devices, rooms)
    config = _config(
        {"rooms": [{"name": "Office", "lights": ["Lamp A", "Lamp B"]}]}, require=False
    )

    reconciler = AreaReconciler(client, state, config)  # type: ignore[arg-type]
    update = next(c for c in reconciler.plan() if c.change_type is ChangeType.UPDATE)
    reconciler.apply(update)

    _, _, body = client.updates[0]
    assert {child["rid"] for child in body["children"]} == {"devA", "devB", "devS"}


def test_light_in_two_rooms_rejected_at_parse() -> None:
    """The single-room rule is enforced during config validation."""
    with pytest.raises(ConfigError):
        _config(
            {
                "rooms": [
                    {"name": "Office", "lights": ["Shared"]},
                    {"name": "Reception", "lights": ["Shared"]},
                ]
            }
        )


def test_empty_zone_membership_is_blocked_not_emptied() -> None:
    """An area declared with no lights must never silently empty a live group."""
    devices = [_device("Lamp A", "devA", "litA"), _device("Lamp B", "devB", "litB")]
    zones = [_zone("Night Guide", "zoneNG", ["litA", "litB"])]
    client, state = _bridge(devices, zones=zones)
    config = _config({"zones": [{"name": "Night Guide", "type": "home", "lights": []}]}, require=False)

    reconciler = AreaReconciler(client, state, config)  # type: ignore[arg-type]
    change = next(c for c in reconciler.plan() if c.name == "Night Guide")
    assert change.change_type is ChangeType.BLOCKED
    assert not change.is_actionable
    # The destructive empty-children PUT must never be issued.
    reconciler.apply(change)
    assert client.updates == []


def test_unknown_light_in_area_is_blocked_not_raised() -> None:
    """A typo'd/nonexistent light name blocks just that area instead of crashing plan()."""
    devices = [_device("Lamp A", "devA", "litA")]
    zones = [_zone("Living", "zoneL", ["litA"])]
    client, state = _bridge(devices, zones=zones)
    config = _config(
        {"zones": [{"name": "Living", "type": "home", "lights": ["Lamp A", "Ghost Light"]}]},
        require=False,
    )

    reconciler = AreaReconciler(client, state, config)  # type: ignore[arg-type]
    changes = reconciler.plan()  # must not raise ConfigError
    change = next(c for c in changes if c.name == "Living")
    assert change.change_type is ChangeType.BLOCKED
    assert "Ghost Light" in change.summary


def test_empty_area_is_not_created() -> None:
    """An area with no resolvable lights is blocked rather than created empty."""
    devices = [_device("Lamp A", "devA", "litA")]
    client, state = _bridge(devices)
    config = _config({"zones": [{"name": "Empty Zone", "type": "home", "lights": []}]}, require=False)

    change = next(
        c for c in AreaReconciler(client, state, config).plan() if c.name == "Empty Zone"  # type: ignore[arg-type]
    )
    assert change.change_type is ChangeType.BLOCKED
