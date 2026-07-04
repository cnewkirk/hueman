"""Tests for BridgeState indexing, incl. Hue MotionAware motion areas.

Regression guard: MotionAware surfaces motion under motion_area_configuration +
convenience_area_motion / security_area_motion, NOT the legacy `motion` service.
BridgeState must surface these so `inventory` never reports a MotionAware bridge
as having no sensors.
"""

from __future__ import annotations

from hueman.state import BridgeState


class FakeClient:
    def __init__(self, resources: dict) -> None:
        self._resources = resources

    def get_resources(self, rtype: str):
        return self._resources.get(rtype, [])


def _motionaware_bridge() -> BridgeState:
    resources = {
        "device": [],
        "room": [{"id": "roomX", "metadata": {"name": "Living room"}, "children": [], "services": []}],
        "zone": [],
        "scene": [],
        "motion": [],  # legacy PIR: none — the trap that caused the false "no sensors"
        "motion_area_configuration": [
            {
                "id": "cfg1",
                "name": "Main Room",
                "group": {"rid": "roomX", "rtype": "room"},
                "participants": [{"resource": {"rid": f"b{i}"}} for i in range(4)],
                "services": [
                    {"rid": "conv1", "rtype": "convenience_area_motion"},
                    {"rid": "sec1", "rtype": "security_area_motion"},
                ],
            }
        ],
        "convenience_area_motion": [
            {"id": "conv1", "motion": {"motion": False, "motion_valid": True},
             "sensitivity": {"sensitivity": 2, "sensitivity_max": 4}}
        ],
        "security_area_motion": [
            {"id": "sec1", "motion": {"motion": True, "motion_valid": True},
             "sensitivity": {"sensitivity": 2, "sensitivity_max": 4}}
        ],
    }
    return BridgeState(FakeClient(resources)).load()  # type: ignore[arg-type]


def test_motionaware_areas_are_indexed() -> None:
    state = _motionaware_bridge()
    areas = state.motion_areas
    assert len(areas) == 1
    area = areas[0]
    assert area.name == "Main Room"
    assert area.room_name == "Living room"
    assert area.participant_count == 4
    assert area.motion is True          # security service reports motion
    assert area.sensitivity == 2 and area.sensitivity_max == 4
    assert set(area.service_rids) == {"conv1", "sec1"}


def test_legacy_motion_index_still_empty_but_areas_present() -> None:
    """The legacy `motion` index is empty; MotionAware must NOT be conflated away."""
    state = _motionaware_bridge()
    assert state.sensors == ()          # no legacy PIR sensors
    assert state.motion_areas           # but MotionAware IS present
