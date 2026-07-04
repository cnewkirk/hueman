"""Indexed snapshot of the live bridge, with name-to-id resolution.

The CLIP API addresses everything by opaque ``rid`` UUIDs, but humans (and the
IaC config) refer to rooms, zones and sensors by name. :class:`BridgeState`
loads the resource types the reconcilers and runtime need and builds the lookups
that bridge that gap, so the rest of the code never hand-rolls a search through
raw API payloads.
"""

from __future__ import annotations

from dataclasses import dataclass

from .client import HueClient
from .errors import ConfigError


@dataclass(frozen=True)
class Group:
    """A room or zone and the members it contains.

    Attributes:
        rid: The group's resource id.
        rtype: Either ``"room"`` or ``"zone"``.
        name: The human-facing group name.
        grouped_light_rid: The group's ``grouped_light`` service id, if any.
        light_rids: Light-service ids belonging to the group.
        device_rids: Member device ids (rooms list devices; zones leave this empty).
            Includes non-light accessories such as motion sensors and switches.
        light_device_rids: The subset of ``device_rids`` that own a light service.
            Membership reconciliation compares against this so accessories in the
            room are never mistaken for light drift (or evicted on apply).
    """

    rid: str
    rtype: str
    name: str
    grouped_light_rid: str | None
    light_rids: tuple[str, ...]
    device_rids: tuple[str, ...]
    light_device_rids: tuple[str, ...] = ()


@dataclass(frozen=True)
class LightRef:
    """A controllable light, addressable as both a device and a light service.

    Attributes:
        name: The light's device name as shown in the Hue app.
        device_rid: The owning device id (used for room membership).
        light_rid: The light service id (used for zone membership and scenes).
        room_name: The room the light currently belongs to, if any.
    """

    name: str
    device_rid: str
    light_rid: str
    room_name: str | None


@dataclass(frozen=True)
class MotionSensor:
    """A motion sensor device and its relevant service ids.

    Attributes:
        name: The device name as shown in the Hue app.
        motion_rid: The ``motion`` service id (sensitivity lives here).
        light_level_rid: The ``light_level`` service id, if present.
    """

    name: str
    motion_rid: str | None
    light_level_rid: str | None


@dataclass(frozen=True)
class MotionArea:
    """A Hue MotionAware motion area (a grid of bulbs sensing as one zone).

    MotionAware (Bridge Pro) does not create legacy ``motion`` device services;
    it exposes a ``motion_area_configuration`` whose ``convenience_area_motion``
    / ``security_area_motion`` services carry the motion state. Indexing these
    keeps ``inventory`` honest on bridges with no legacy PIR sensors.

    Attributes:
        name: The area name (e.g. "Main Room").
        rid: The ``motion_area_configuration`` id.
        room_name: The room the area is scoped to, if resolvable.
        motion: ``True`` if any of the area's motion services currently reports motion.
        sensitivity / sensitivity_max: Configured sensitivity, if reported.
        participant_count: Number of bulbs participating in the sensing grid.
        service_rids: The ``*_area_motion`` service ids (for SSE event routing).
    """

    name: str
    rid: str
    room_name: str | None
    motion: bool
    sensitivity: int | None
    sensitivity_max: int | None
    participant_count: int
    service_rids: tuple[str, ...]


class BridgeState:
    """Loads and indexes the bridge resources the tool operates on.

    Args:
        client: An authenticated :class:`~hue_iac.client.HueClient`.
    """

    def __init__(self, client: HueClient) -> None:
        self._client = client
        self._groups_by_name: dict[str, Group] = {}
        self._sensors_by_name: dict[str, MotionSensor] = {}
        self._scenes_by_name: dict[str, dict] = {}
        self._lights_by_name: dict[str, LightRef] = {}
        self._device_name_by_rid: dict[str, str] = {}
        self._motion_areas: list[MotionArea] = []
        self._smart_scenes_by_name: dict[str, dict] = {}
        self._scene_name_by_rid: dict[str, str] = {}
        self._behavior_instances_by_name: dict[str, dict] = {}
        self._loaded = False

    def load(self) -> "BridgeState":
        """Fetch and index rooms, zones, lights, sensors and scenes.

        Returns:
            This instance, to allow ``BridgeState(client).load()`` chaining.
        """
        devices = self._client.get_resources("device")
        device_to_light = self._index_device_lights(devices)
        device_room = self._index_groups(device_to_light)
        self._index_lights(devices, device_to_light, device_room)
        self._index_sensors(devices)
        self._index_motion_areas()
        self._index_scenes()
        self._index_smart_scenes()
        self._index_behavior_instances()
        self._loaded = True
        return self

    # -- indexing helpers --------------------------------------------------- #
    def _index_device_lights(self, devices: list[dict]) -> dict[str, str]:
        """Map each device id to its light-service id (if it owns one)."""
        device_to_light: dict[str, str] = {}
        for device in devices:
            self._device_name_by_rid[device["id"]] = device.get("metadata", {}).get("name", "")
            for service in device.get("services", []):
                if service.get("rtype") == "light":
                    device_to_light[device["id"]] = service["rid"]
        return device_to_light

    def _index_groups(self, device_to_light: dict[str, str]) -> dict[str, str]:
        """Build the room/zone index and return a device-id -> room-name map."""
        device_room: dict[str, str] = {}
        for rtype in ("room", "zone"):
            for group in self._client.get_resources(rtype):
                grouped_light_rid: str | None = None
                for service in group.get("services", []):
                    if service.get("rtype") == "grouped_light":
                        grouped_light_rid = service["rid"]
                light_rids, device_rids, light_device_rids = self._resolve_group_members(
                    group, rtype, device_to_light
                )
                name = group.get("metadata", {}).get("name", "")
                if rtype == "room":
                    for device_rid in device_rids:
                        device_room[device_rid] = name
                self._groups_by_name[name] = Group(
                    rid=group["id"],
                    rtype=rtype,
                    name=name,
                    grouped_light_rid=grouped_light_rid,
                    light_rids=tuple(light_rids),
                    device_rids=tuple(device_rids),
                    light_device_rids=tuple(light_device_rids),
                )
        return device_room

    def _index_lights(
        self,
        devices: list[dict],
        device_to_light: dict[str, str],
        device_room: dict[str, str],
    ) -> None:
        """Index every light device by name, recording its current room."""
        for device in devices:
            device_rid = device["id"]
            light_rid = device_to_light.get(device_rid)
            if light_rid is None:
                continue
            name = device.get("metadata", {}).get("name", "")
            self._lights_by_name[name] = LightRef(
                name=name,
                device_rid=device_rid,
                light_rid=light_rid,
                room_name=device_room.get(device_rid),
            )

    @staticmethod
    def _resolve_group_members(
        group: dict, rtype: str, device_to_light: dict[str, str]
    ) -> tuple[list[str], list[str], list[str]]:
        """Return ``(light_service_ids, device_ids, light_device_ids)`` for ``group``.

        Rooms list member devices (each resolved to its light service); zones
        list light services directly and own no devices. ``light_device_ids`` is
        the subset of room devices that actually own a light, so callers can tell
        controllable lights apart from accessories (sensors, switches).
        """
        lights: list[str] = []
        device_rids: list[str] = []
        light_device_rids: list[str] = []
        for child in group.get("children", []):
            child_type = child.get("rtype")
            if rtype == "room" and child_type == "device":
                device_rids.append(child["rid"])
                light_rid = device_to_light.get(child["rid"])
                if light_rid is not None:
                    lights.append(light_rid)
                    light_device_rids.append(child["rid"])
            elif child_type == "light":
                lights.append(child["rid"])
        return lights, device_rids, light_device_rids

    def _index_sensors(self, devices: list[dict]) -> None:
        """Build the motion-sensor index keyed by device name."""
        for device in devices:
            motion_rid: str | None = None
            light_level_rid: str | None = None
            for service in device.get("services", []):
                if service.get("rtype") == "motion":
                    motion_rid = service["rid"]
                elif service.get("rtype") == "light_level":
                    light_level_rid = service["rid"]
            if motion_rid is not None:
                name = device.get("metadata", {}).get("name", "")
                self._sensors_by_name[name] = MotionSensor(
                    name=name, motion_rid=motion_rid, light_level_rid=light_level_rid
                )

    def _index_motion_areas(self) -> None:
        """Index Hue MotionAware areas (bulb-grid motion sensing, Bridge Pro).

        Motion state lives on the area's ``convenience_area_motion`` /
        ``security_area_motion`` services rather than on a device ``motion``
        service, so the legacy sensor index never sees it.
        """
        services: dict[str, dict] = {}
        for rtype in ("convenience_area_motion", "security_area_motion"):
            for service in self._client.get_resources(rtype):
                services[service["id"]] = service
        room_name_by_rid = {group.rid: group.name for group in self._groups_by_name.values()}

        for cfg in self._client.get_resources("motion_area_configuration"):
            service_rids = [s["rid"] for s in cfg.get("services", [])]
            motion = False
            sensitivity: int | None = None
            sensitivity_max: int | None = None
            for rid in service_rids:
                service = services.get(rid)
                if service is None:
                    continue
                if service.get("motion", {}).get("motion"):
                    motion = True
                sens = service.get("sensitivity", {})
                if "sensitivity" in sens:
                    sensitivity = sens.get("sensitivity")
                    sensitivity_max = sens.get("sensitivity_max")
            self._motion_areas.append(
                MotionArea(
                    name=cfg.get("name", ""),
                    rid=cfg["id"],
                    room_name=room_name_by_rid.get(cfg.get("group", {}).get("rid")),
                    motion=motion,
                    sensitivity=sensitivity,
                    sensitivity_max=sensitivity_max,
                    participant_count=len(cfg.get("participants", [])),
                    service_rids=tuple(service_rids),
                )
            )

    def _index_scenes(self) -> None:
        """Index every scene by name and id (the raw payload is kept for diffing)."""
        for scene in self._client.get_resources("scene"):
            name = scene.get("metadata", {}).get("name", "")
            self._scenes_by_name[name] = scene
            self._scene_name_by_rid[scene["id"]] = name

    def _index_smart_scenes(self) -> None:
        """Index Hue smart scenes (native time-of-day scene cycles) by name."""
        for smart_scene in self._client.get_resources("smart_scene"):
            name = smart_scene.get("metadata", {}).get("name", "")
            self._smart_scenes_by_name[name] = smart_scene

    def _index_behavior_instances(self) -> None:
        """Index native automations (behavior_instance) by name."""
        for inst in self._client.get_resources("behavior_instance"):
            name = inst.get("metadata", {}).get("name", "")
            self._behavior_instances_by_name[name] = inst

    # -- lookups ------------------------------------------------------------ #
    def group(self, name: str) -> Group:
        """Return the room/zone named ``name`` or raise :class:`ConfigError`."""
        if name not in self._groups_by_name:
            raise ConfigError(f"no room or zone named {name!r} on the bridge")
        return self._groups_by_name[name]

    def sensor(self, name: str) -> MotionSensor:
        """Return the motion sensor named ``name`` or raise :class:`ConfigError`."""
        if name not in self._sensors_by_name:
            raise ConfigError(f"no motion sensor named {name!r} on the bridge")
        return self._sensors_by_name[name]

    def group_optional(self, name: str) -> Group | None:
        """Return the room/zone named ``name`` or ``None`` if it does not exist."""
        return self._groups_by_name.get(name)

    def scene(self, name: str) -> dict | None:
        """Return the raw scene payload named ``name``, or ``None``."""
        return self._scenes_by_name.get(name)

    def scene_name_for(self, rid: str) -> str | None:
        """Return the scene name for a scene resource id, or ``None``."""
        return self._scene_name_by_rid.get(rid)

    def smart_scene(self, name: str) -> dict | None:
        """Return the raw smart-scene payload named ``name``, or ``None``."""
        return self._smart_scenes_by_name.get(name)

    def behavior_instance(self, name: str) -> dict | None:
        """Return the raw behavior_instance (native automation) named ``name``."""
        return self._behavior_instances_by_name.get(name)

    def light(self, name: str) -> LightRef:
        """Return the light named ``name`` or raise :class:`ConfigError`."""
        if name not in self._lights_by_name:
            raise ConfigError(f"no light named {name!r} on the bridge")
        return self._lights_by_name[name]

    def device_name(self, device_rid: str) -> str:
        """Return the device name for ``device_rid`` (empty string if unknown)."""
        return self._device_name_by_rid.get(device_rid, "")

    @property
    def group_names(self) -> tuple[str, ...]:
        """Return all known room/zone names."""
        return tuple(self._groups_by_name)

    @property
    def all_light_names(self) -> tuple[str, ...]:
        """Return the names of every controllable light on the bridge."""
        return tuple(self._lights_by_name)

    @property
    def groups(self) -> tuple[Group, ...]:
        """Return every indexed room and zone."""
        return tuple(self._groups_by_name.values())

    @property
    def lights(self) -> tuple[LightRef, ...]:
        """Return every indexed light."""
        return tuple(self._lights_by_name.values())

    @property
    def sensors(self) -> tuple[MotionSensor, ...]:
        """Return every indexed (legacy PIR) motion sensor."""
        return tuple(self._sensors_by_name.values())

    @property
    def motion_areas(self) -> tuple[MotionArea, ...]:
        """Return every indexed Hue MotionAware area (bulb-grid sensing)."""
        return tuple(self._motion_areas)

    @property
    def smart_scenes(self) -> tuple[dict, ...]:
        """Return every indexed smart-scene payload."""
        return tuple(self._smart_scenes_by_name.values())
