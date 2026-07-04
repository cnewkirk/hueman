"""Tests for the declarative scenes surface (config + reconciler)."""

from __future__ import annotations

import pytest

from hue_iac.config import Color, Config, LightState
from hue_iac.errors import ConfigError
from hue_iac.reconcile import ChangeType, Planner, SceneReconciler
from hue_iac.state import BridgeState


def _doc(scenes):
    return {
        "bridge": {"host": "192.0.2.10", "application_key": "k"},
        "location": {"lat": 45.5, "lon": -122.6, "tz_offset_hours": -7},
        "motion_policies": [],
        "scenes": scenes,
    }


TV_SCENE = {
    "name": "TV Mode",
    "zone": "TV Viewing",
    "lights": {
        "Play bar left": {"on": True, "brightness": 27, "color": {"mirek": 153}},
        "Couch Lightstrip": {"on": True, "brightness": 5, "color": {"hex": "#ff8030"}},
    },
}


def test_scene_spec_parses_per_light_states():
    cfg = Config.parse(_doc([TV_SCENE]))
    assert len(cfg.scenes) == 1
    spec = cfg.scenes[0]
    assert spec.name == "TV Mode"
    assert spec.zone == "TV Viewing"
    states = dict(spec.lights)
    assert states["Play bar left"] == LightState(
        on=True, brightness=27.0, color=Color(mode="ct", mirek=153)
    )
    assert states["Couch Lightstrip"].color.mode == "xy"


def test_scene_missing_zone_raises():
    with pytest.raises(ConfigError):
        Config.parse(_doc([{"name": "X", "lights": {"A": {"brightness": 5}}}]))


def test_scene_missing_lights_raises():
    with pytest.raises(ConfigError):
        Config.parse(_doc([{"name": "X", "zone": "Z", "lights": {}}]))


def test_duplicate_scene_name_raises():
    with pytest.raises(ConfigError):
        Config.parse(_doc([
            {"name": "Dup", "zone": "Z", "lights": {"A": {"brightness": 5}}},
            {"name": "Dup", "zone": "Z", "lights": {"B": {"brightness": 5}}},
        ]))


class _FakeClient:
    def __init__(self, resources):
        self._r = resources
        self.created = []
        self.updated = []

    def get_resources(self, rtype):
        return self._r.get(rtype, [])

    def create_resource(self, rtype, body):
        self.created.append((rtype, body))
        return "new-rid"

    def update_resource(self, rtype, rid, body):
        self.updated.append((rtype, rid, body))


def _device(name, dev_id, light_id):
    return {
        "id": dev_id,
        "metadata": {"name": name},
        "services": [{"rtype": "light", "rid": light_id}],
    }


def _state_with_tv_zone(scene=None):
    devices = [
        _device("Play bar left", "dPBL", "lPBL"),
        _device("Couch Lightstrip", "dCLS", "lCLS"),
        _device("Bedroom lamp", "dBED", "lBED"),   # exists on bridge but NOT in TV Viewing zone
    ]
    zone = {
        "id": "zTV",
        "metadata": {"name": "TV Viewing"},
        "services": [{"rtype": "grouped_light", "rid": "glTV"}],
        "children": [{"rid": "lPBL", "rtype": "light"}, {"rid": "lCLS", "rtype": "light"}],
    }
    resources = {
        "device": devices, "room": [], "zone": [zone],
        "convenience_area_motion": [], "security_area_motion": [],
        "motion_area_configuration": [], "smart_scene": [],
        "scene": [scene] if scene else [],
    }
    client = _FakeClient(resources)
    return client, BridgeState(client).load()  # type: ignore[arg-type]


def _cfg(scenes):
    return Config.parse({
        "bridge": {"host": "192.0.2.10", "application_key": "k"},
        "location": {"lat": 45.5, "lon": -122.6, "tz_offset_hours": -7},
        "motion_policies": [],
        "scenes": scenes,
    })


_TV = {
    "name": "TV Mode",
    "zone": "TV Viewing",
    "lights": {
        "Play bar left": {"on": True, "brightness": 27, "color": {"mirek": 153}},
        "Couch Lightstrip": {"on": True, "brightness": 5, "color": {"mirek": 400}},
    },
}


def _existing_scene(pbl_brightness):
    return {
        "id": "scTV",
        "metadata": {"name": "TV Mode"},
        "group": {"rid": "zTV", "rtype": "zone"},
        "actions": [
            {"target": {"rid": "lPBL", "rtype": "light"},
             "action": {"on": {"on": True}, "dimming": {"brightness": pbl_brightness},
                        "color_temperature": {"mirek": 153}}},
            {"target": {"rid": "lCLS", "rtype": "light"},
             "action": {"on": {"on": True}, "dimming": {"brightness": 5.0},
                        "color_temperature": {"mirek": 400}}},
        ],
    }


def test_scene_create_when_absent():
    client, state = _state_with_tv_zone()
    change = SceneReconciler(client, state, _cfg([_TV])).plan()[0]
    assert change.change_type is ChangeType.CREATE


def test_scene_blocked_when_zone_missing():
    client, state = _state_with_tv_zone()
    cfg = _cfg([{**_TV, "zone": "Nope"}])
    change = SceneReconciler(client, state, cfg).plan()[0]
    assert change.change_type is ChangeType.BLOCKED


def test_scene_noop_when_actions_match():
    client, state = _state_with_tv_zone(scene=_existing_scene(27.0))
    change = SceneReconciler(client, state, _cfg([_TV])).plan()[0]
    assert change.change_type is ChangeType.NOOP


def test_scene_update_when_brightness_differs():
    client, state = _state_with_tv_zone(scene=_existing_scene(99.0))
    change = SceneReconciler(client, state, _cfg([_TV])).plan()[0]
    assert change.change_type is ChangeType.UPDATE


def test_apply_create_posts_full_scene_body():
    client, state = _state_with_tv_zone()
    rec = SceneReconciler(client, state, _cfg([_TV]))
    rec.apply(rec.plan()[0])
    assert len(client.created) == 1
    rtype, body = client.created[0]
    assert rtype == "scene"
    assert body["metadata"]["name"] == "TV Mode"
    assert body["group"] == {"rid": "zTV", "rtype": "zone"}
    acts = {a["target"]["rid"]: a["action"] for a in body["actions"]}
    assert acts["lPBL"]["color_temperature"]["mirek"] == 153
    assert acts["lPBL"]["dimming"]["brightness"] == 27.0
    assert acts["lCLS"]["color_temperature"]["mirek"] == 400


def test_apply_update_puts_actions_only():
    client, state = _state_with_tv_zone(scene=_existing_scene(99.0))
    rec = SceneReconciler(client, state, _cfg([_TV]))
    rec.apply(rec.plan()[0])
    assert client.created == []
    rtype, rid, body = client.updated[0]
    assert (rtype, rid) == ("scene", "scTV")
    acts = {a["target"]["rid"]: a["action"] for a in body["actions"]}
    assert acts["lPBL"]["dimming"]["brightness"] == 27.0


def test_apply_noop_writes_nothing():
    client, state = _state_with_tv_zone(scene=_existing_scene(27.0))
    rec = SceneReconciler(client, state, _cfg([_TV]))
    rec.apply(rec.plan()[0])
    assert client.created == [] and client.updated == []


def test_planner_includes_scene_changes():
    client, state = _state_with_tv_zone()
    kinds = {c.resource for c in Planner(client, state, _cfg([_TV])).plan()}
    assert "scene" in kinds


# ---------------------------------------------------------------------------
# New tests (TDD)
# ---------------------------------------------------------------------------

def test_scene_blocked_when_light_not_in_zone():
    """A scene referencing a light that exists but is NOT in the zone → BLOCKED."""
    client, state = _state_with_tv_zone()
    cfg = _cfg([{
        "name": "Bad Scene",
        "zone": "TV Viewing",
        "lights": {"Bedroom lamp": {"brightness": 30}},
    }])
    change = SceneReconciler(client, state, cfg).plan()[0]
    assert change.change_type is ChangeType.BLOCKED
    assert "Bedroom lamp" in change.summary


def test_scene_rejects_circadian_color():
    """A scene light with color: circadian → ConfigError at parse time."""
    with pytest.raises(ConfigError, match="circadian"):
        Config.parse(_doc([{
            "name": "Bad",
            "zone": "Z",
            "lights": {"A": {"color": "circadian"}},
        }]))


def test_apply_off_light_action():
    """A scene with on: false produces action == {\"on\": {\"on\": False}} only."""
    off_scene = {
        "name": "Off Scene",
        "zone": "TV Viewing",
        "lights": {
            "Play bar left": {"on": False},
        },
    }
    client, state = _state_with_tv_zone()
    rec = SceneReconciler(client, state, _cfg([off_scene]))
    rec.apply(rec.plan()[0])
    assert len(client.created) == 1
    _, body = client.created[0]
    acts = {a["target"]["rid"]: a["action"] for a in body["actions"]}
    assert acts["lPBL"] == {"on": {"on": False}}


def test_apply_update_body_has_only_actions_key():
    """UPDATE sends exactly {\"actions\": ...} — no extra top-level keys."""
    client, state = _state_with_tv_zone(scene=_existing_scene(99.0))
    rec = SceneReconciler(client, state, _cfg([_TV]))
    rec.apply(rec.plan()[0])
    _, _, body = client.updated[0]
    assert set(body) == {"actions"}


def test_apply_hex_color_produces_xy():
    """A scene light with hex color produces a color.xy action with float x/y."""
    hex_scene = {
        "name": "Hex Scene",
        "zone": "TV Viewing",
        "lights": {
            "Play bar left": {"on": True, "color": {"hex": "#ff6600"}},
        },
    }
    client, state = _state_with_tv_zone()
    rec = SceneReconciler(client, state, _cfg([hex_scene]))
    rec.apply(rec.plan()[0])
    assert len(client.created) == 1
    _, body = client.created[0]
    acts = {a["target"]["rid"]: a["action"] for a in body["actions"]}
    color = acts["lPBL"].get("color")
    assert color is not None, "expected 'color' key in action"
    xy = color.get("xy", {})
    assert isinstance(xy.get("x"), float) and isinstance(xy.get("y"), float)
