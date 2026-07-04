"""Declarative reconciliation: diff desired config against the live bridge.

This is the Terraform-style core. Each :class:`Reconciler` compares the desired
state expressed in the config against what the bridge currently reports and
produces a list of :class:`Change` objects (the "plan"). Applying a plan simply
executes each non-no-op change. Planning never mutates the bridge, so ``plan``
is always safe to run.

The only persistent bridge resource the motion policies own is the motion
sensor's *sensitivity*; the live colour/timing behaviour is enforced at runtime
by :mod:`hue_iac.watch`. Keeping sensitivity here means a single ``apply`` fully
provisions the declarative surface, and the reconciler base class leaves room
for additional resource kinds (such as scenes) without changing the CLI.
"""

from __future__ import annotations

import abc
import copy
import datetime as _dt
import json
import os
from dataclasses import dataclass
from enum import Enum

from .circadian_scene import circadian_timeslots
from .client import HueClient
from .config import Area, Config, LightState, MotionPolicy, SceneSpec, SmartSceneSpec
from .errors import HueIacError
from .nightmotion import scene_actions_match, scene_body, transform_automation
from .payload import ColorConverter
from .state import BridgeState, Group
from .sun import SolarCalculator

#: A smart_scene recurs every day of the week.
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

#: Motion sensitivity is configured on a 0..3 scale (low/medium/high/max) and
#: scaled onto each sensor's own ``sensitivity_max`` at apply time.
_SENSITIVITY_LEVELS = 3


class ChangeType(Enum):
    """The kind of action a :class:`Change` represents."""

    CREATE = "create"
    UPDATE = "update"
    NOOP = "noop"
    BLOCKED = "blocked"  # drift that needs a human decision; never auto-applied


@dataclass(frozen=True)
class Change:
    """A single planned modification.

    Attributes:
        resource: Short label for the resource kind, for example ``"motion.sensitivity"``.
        name: The human-facing resource name the change targets.
        change_type: Whether this creates, updates, or is a no-op.
        summary: One-line, human-readable description of the diff.
    """

    resource: str
    name: str
    change_type: ChangeType
    summary: str

    @property
    def is_noop(self) -> bool:
        """Return ``True`` when applying this change would do nothing."""
        return self.change_type is ChangeType.NOOP

    @property
    def is_blocked(self) -> bool:
        """Return ``True`` when this change needs a human and cannot auto-apply."""
        return self.change_type is ChangeType.BLOCKED

    @property
    def is_actionable(self) -> bool:
        """Return ``True`` when applying this change would alter the bridge."""
        return self.change_type in (ChangeType.CREATE, ChangeType.UPDATE)


class Reconciler(abc.ABC):
    """Base class for resource-specific planners/appliers.

    Args:
        client: An authenticated bridge client.
        state: A loaded :class:`~hue_iac.state.BridgeState`.
        config: The parsed IaC configuration.
    """

    def __init__(self, client: HueClient, state: BridgeState, config: Config) -> None:
        self._client = client
        self._state = state
        self._config = config

    @abc.abstractmethod
    def plan(self) -> list[Change]:
        """Return the changes needed to converge this resource kind."""

    @abc.abstractmethod
    def apply(self, change: Change) -> None:
        """Apply a single change produced by :meth:`plan`."""

    @abc.abstractmethod
    def owns(self, change: Change) -> bool:
        """Return ``True`` if this reconciler produced and can apply ``change``."""


class SensitivityReconciler(Reconciler):
    """Reconciles each motion sensor's sensitivity with its policy."""

    def plan(self) -> list[Change]:
        """Diff desired sensitivity against each sensor's current value."""
        changes: list[Change] = []
        for policy in self._config.motion_policies:
            change = self._plan_policy(policy)
            if change is not None:
                changes.append(change)
        return changes

    def _plan_policy(self, policy: MotionPolicy) -> Change | None:
        """Return the sensitivity change for one policy, or ``None`` to skip."""
        if policy.sensitivity is None:
            return None
        sensor = self._state.sensor(policy.sensor)
        if sensor.motion_rid is None:
            return None
        current, maximum = self._read_sensitivity(sensor.motion_rid)
        desired = self._scale(policy.sensitivity, maximum)
        if current == desired:
            return Change(
                resource="motion.sensitivity",
                name=policy.sensor,
                change_type=ChangeType.NOOP,
                summary=f"sensitivity already {desired}/{maximum}",
            )
        return Change(
            resource="motion.sensitivity",
            name=policy.sensor,
            change_type=ChangeType.UPDATE,
            summary=f"sensitivity {current} -> {desired} (of {maximum})",
        )

    def owns(self, change: Change) -> bool:
        """Return ``True`` for sensitivity changes."""
        return change.resource.startswith("motion.")

    def apply(self, change: Change) -> None:
        """Write the desired sensitivity for the sensor named in ``change``."""
        policy = self._policy_for_sensor(change.name)
        sensor = self._state.sensor(change.name)
        if policy.sensitivity is None or sensor.motion_rid is None:
            return
        _, maximum = self._read_sensitivity(sensor.motion_rid)
        desired = self._scale(policy.sensitivity, maximum)
        self._client.update_resource(
            "motion", sensor.motion_rid, {"sensitivity": {"sensitivity": desired}}
        )

    # -- helpers ------------------------------------------------------------ #
    def _read_sensitivity(self, motion_rid: str) -> tuple[int, int]:
        """Return the sensor's ``(current, maximum)`` sensitivity values."""
        resource = self._client.get_resource("motion", motion_rid) or {}
        sensitivity = resource.get("sensitivity", {})
        current = int(sensitivity.get("sensitivity", 0))
        maximum = int(sensitivity.get("sensitivity_max", _SENSITIVITY_LEVELS))
        return current, maximum

    @staticmethod
    def _scale(level: int, maximum: int) -> int:
        """Scale a 0..3 policy level onto the sensor's 0..maximum range."""
        if maximum <= 0:
            return 0
        return round(level / _SENSITIVITY_LEVELS * maximum)

    def _policy_for_sensor(self, sensor_name: str) -> MotionPolicy:
        """Return the policy that drives ``sensor_name``."""
        for policy in self._config.motion_policies:
            if policy.sensor == sensor_name:
                return policy
        raise KeyError(sensor_name)


class AreaReconciler(Reconciler):
    """Reconciles room/zone membership so every light has a declared home.

    Implements the "no lights left behind" guarantee: declared areas are created
    or corrected to contain exactly their declared lights, and any light on the
    bridge that no area claims is surfaced as a blocked change when
    ``require_all_lights_assigned`` is set.
    """

    def plan(self) -> list[Change]:
        """Diff declared area membership and detect unassigned lights."""
        changes: list[Change] = []
        for area in self._config.areas:
            changes.append(self._plan_area(area))
        changes.extend(self._plan_unassigned())
        return changes

    def _plan_area(self, area: Area) -> Change:
        """Return the membership change (or no-op) for one declared area.

        Two membership-safety guards turn what used to be a crash or a silent
        wipe into a human-visible ``BLOCKED`` change:

        * a light name the bridge does not know (a typo, or a fixture that has
          not been created yet) blocks *only that area* instead of raising and
          aborting the whole plan; and
        * an area that resolves to no lights is never created empty, and never
          empties a live group on apply.
        """
        resource = f"area.{area.kind}"
        known = set(self._state.all_light_names)
        unknown = [name for name in area.lights if name not in known]
        if unknown:
            return Change(
                resource=resource,
                name=area.name,
                change_type=ChangeType.BLOCKED,
                summary=f"unknown light(s): {', '.join(unknown)}",
            )
        desired = self._desired_member_rids(area)
        group = self._state.group_optional(area.name)
        if group is None:
            if not desired:
                return Change(
                    resource=resource,
                    name=area.name,
                    change_type=ChangeType.BLOCKED,
                    summary=f"refuses to create {area.name!r} with no lights",
                )
            return Change(
                resource=resource,
                name=area.name,
                change_type=ChangeType.CREATE,
                summary=f"create {area.kind} with {len(desired)} light(s)",
            )
        current = set(group.light_device_rids if area.kind == "room" else group.light_rids)
        if current == set(desired):
            return Change(resource, area.name, ChangeType.NOOP, f"{len(desired)} light(s) assigned")
        if not desired:
            return Change(
                resource=resource,
                name=area.name,
                change_type=ChangeType.BLOCKED,
                summary=(
                    f"refuses to empty {area.name!r}: would remove all {len(current)} light(s) "
                    f"(declare its lights, or delete the {area.kind} on the bridge)"
                ),
            )
        return Change(
            resource=resource,
            name=area.name,
            change_type=ChangeType.UPDATE,
            summary=self._membership_summary(area, current, set(desired)),
        )

    def _plan_unassigned(self) -> list[Change]:
        """Return a blocked change for each light no area claims."""
        if not self._config.require_all_lights_assigned:
            return []
        declared: set[str] = set()
        for area in self._config.areas:
            if area.kind == "room":  # rooms define a light's single home
                declared.update(area.lights)
        blocked: list[Change] = []
        for light_name in self._state.all_light_names:
            if light_name not in declared:
                blocked.append(
                    Change(
                        resource="area.unassigned",
                        name=light_name,
                        change_type=ChangeType.BLOCKED,
                        summary="light is not assigned to any declared room",
                    )
                )
        return blocked

    def owns(self, change: Change) -> bool:
        """Return ``True`` for area membership changes (never the blocked ones)."""
        return change.resource in ("area.room", "area.zone")

    def apply(self, change: Change) -> None:
        """Create or correct the membership of the area named in ``change``."""
        if not change.is_actionable:  # never act on a NOOP/BLOCKED (e.g. an empty-membership PUT)
            return
        area = self._area_by_name(change.name, change.resource)
        if change.change_type is ChangeType.CREATE:
            body = {"metadata": self._metadata(area), "children": self._desired_children(area)}
            self._client.create_resource(area.kind, body)
            return
        group = self._state.group_optional(area.name)
        if group is not None:
            # Preserve the room's non-light accessories (sensors, switches) so a
            # light-membership correction never evicts them from the room.
            children = self._desired_children(area, preserve_from=group)
            self._client.update_resource(area.kind, group.rid, {"children": children})

    # -- helpers ------------------------------------------------------------ #
    def _desired_member_rids(self, area: Area) -> list[str]:
        """Resolve the light/device ids the area should contain."""
        rids: list[str] = []
        for light_name in area.lights:
            light = self._state.light(light_name)
            rids.append(light.device_rid if area.kind == "room" else light.light_rid)
        return rids

    def _desired_children(self, area: Area, preserve_from: Group | None = None) -> list[dict]:
        """Build the CLIP ``children`` list for the area.

        For a room update, ``preserve_from`` carries the live group so any
        non-light accessory devices already in the room (motion sensors, dials)
        are retained alongside the declared lights rather than rewritten away.
        """
        rtype = "device" if area.kind == "room" else "light"
        children: list[dict] = [
            {"rid": rid, "rtype": rtype} for rid in self._desired_member_rids(area)
        ]
        if preserve_from is not None and area.kind == "room":
            light_devices = set(preserve_from.light_device_rids)
            for rid in preserve_from.device_rids:
                if rid not in light_devices:  # an accessory, not a controllable light
                    children.append({"rid": rid, "rtype": "device"})
        return children

    def _metadata(self, area: Area) -> dict:
        """Build the metadata block for creating an area."""
        metadata: dict = {"name": area.name}
        if area.archetype is not None:
            metadata["archetype"] = area.archetype
        return metadata

    def _membership_summary(self, area: Area, current: set[str], desired: set[str]) -> str:
        """Describe how an area's membership will change, by light name."""
        additions = self._names_for(area, desired - current)
        removals = self._names_for(area, current - desired)
        parts: list[str] = []
        if additions:
            parts.append(f"+{', '.join(sorted(additions))}")
        if removals:
            parts.append(f"-{', '.join(sorted(removals))}")
        return "; ".join(parts) if parts else "membership changed"

    def _names_for(self, area: Area, rids: set[str]) -> list[str]:
        """Map member ids back to light names for human-readable summaries."""
        if area.kind == "room":
            names = []
            for rid in rids:
                names.append(self._state.device_name(rid) or rid)
            return names
        # Zones reference light services; recover names via the declared set.
        light_rid_to_name = {}
        for light_name in area.lights:
            light_rid_to_name[self._state.light(light_name).light_rid] = light_name
        names = []
        for rid in rids:
            names.append(light_rid_to_name.get(rid, rid))
        return names

    def _area_by_name(self, name: str, resource: str) -> Area:
        """Return the declared area matching ``name`` and resource kind."""
        kind = "room" if resource == "area.room" else "zone"
        for area in self._config.areas:
            if area.name == name and area.kind == kind:
                return area
        raise KeyError(name)


def _hhmm(minute_of_day: int) -> str:
    return f"{minute_of_day // 60:02d}:{minute_of_day % 60:02d}"


def _slot_minute(tslot: dict) -> int:
    """Return a timeslot's start time as minute-of-day (0 if unset)."""
    time = tslot.get("start_time", {}).get("time", {})
    return int(time.get("hour", 0)) * 60 + int(time.get("minute", 0))


def _write_backup(backup_dir: str, resource: dict) -> None:
    """Persist ``resource`` to ``{backup_dir}/{id}-preapply.json`` before a destructive write.

    Write-once: an existing backup is never overwritten, so repeated applies (a
    daily cron) keep the genuine pre-first-apply original rather than replacing it
    with machine-generated state. A falsy ``backup_dir`` disables backups.
    """
    if not backup_dir:
        return
    os.makedirs(backup_dir, exist_ok=True)
    path = os.path.join(backup_dir, f"{resource['id']}-preapply.json")
    if os.path.exists(path):
        return
    with open(path, "w") as handle:
        json.dump(resource, handle, indent=2)


class SmartSceneReconciler(Reconciler):
    """Re-times a bridge ``smart_scene`` so its transitions track the real sun.

    The bridge runs the native time-of-day scene cycle on its own; this
    reconciler only rewrites *when* each scene becomes active, anchoring the
    timeslots to the location's real sunrise/sunset (so re-running ``apply`` —
    e.g. daily — keeps the cycle in sync with the seasons, no daemon required).
    Scene targets and structure are preserved; only ``start_time`` changes.
    """

    def __init__(
        self, client: HueClient, state: BridgeState, config: Config,
        for_date: _dt.date | None = None, backup_dir: str = ".hue-backup",
    ) -> None:
        super().__init__(client, state, config)
        self._for_date = for_date
        self._backup_dir = backup_dir

    def _sun_times(self):
        loc = self._config.location
        day = self._for_date or _dt.date.today()
        return SolarCalculator(loc.lat, loc.lon, loc.tz_offset_hours, tz=loc.tz).sun_times(day)

    def plan(self) -> list[Change]:
        """Diff each configured smart scene's timing against the real sun."""
        if not self._config.smart_scenes:
            return []
        sun = self._sun_times()
        return [self._plan_spec(spec, sun) for spec in self._config.smart_scenes]

    def _plan_spec(self, spec: SmartSceneSpec, sun) -> Change:
        smart_scene = self._state.smart_scene(spec.name)
        if smart_scene is None:
            return Change(
                resource="smart_scene",
                name=spec.name,
                change_type=ChangeType.BLOCKED,
                summary="no smart_scene with this name on the bridge",
            )
        schedule = dict(spec.schedule)
        diffs: list[str] = []
        pruned: list[str] = []
        preserved = 0
        emptied_week = False
        for week in smart_scene.get("week_timeslots", []):
            slots = week.get("timeslots", [])
            week_kept = 0
            for tslot in slots:
                scene_name = self._state.scene_name_for(tslot.get("target", {}).get("rid"))
                if scene_name is None:
                    preserved += 1   # unresolvable rid -> keep untouched, never silently drop
                    week_kept += 1
                    continue
                anchor = schedule.get(scene_name)
                if anchor is None:
                    pruned.append(scene_name)  # not in schedule -> remove the timeslot
                    continue
                week_kept += 1
                desired = anchor.resolve(sun.sunrise_min, sun.sunset_min)
                current = _slot_minute(tslot)
                if current != desired:
                    diffs.append(f"{scene_name} {_hhmm(current)}->{_hhmm(desired)}")
            if slots and week_kept == 0:
                emptied_week = True
        if emptied_week:
            return Change(
                "smart_scene", spec.name, ChangeType.BLOCKED,
                "pruning every timeslot would empty the smart scene; "
                "keep at least one scheduled scene",
            )
        if not diffs and not pruned:
            summary = "schedule already sun-anchored"
            if preserved:
                summary += f"; {preserved} unrecognized timeslot(s) preserved"
            return Change("smart_scene", spec.name, ChangeType.NOOP, summary)
        summary = "; ".join(diffs)
        if pruned:
            summary = (summary + "; " if summary else "") + f"remove {', '.join(pruned)}"
        if preserved:
            summary += f"; preserve {preserved} unrecognized"
        return Change("smart_scene", spec.name, ChangeType.UPDATE, summary)

    def owns(self, change: Change) -> bool:
        return change.resource == "smart_scene"

    def apply(self, change: Change) -> None:
        if not change.is_actionable:
            return
        spec = next((s for s in self._config.smart_scenes if s.name == change.name), None)
        smart_scene = self._state.smart_scene(change.name) if spec else None
        if spec is None or smart_scene is None:
            return
        sun = self._sun_times()
        schedule = dict(spec.schedule)
        weeks = copy.deepcopy(smart_scene.get("week_timeslots", []))
        for week in weeks:
            slots = week.get("timeslots", [])
            kept: list[dict] = []
            for tslot in slots:
                scene_name = self._state.scene_name_for(tslot.get("target", {}).get("rid"))
                if scene_name is None:
                    kept.append(tslot)  # unrecognized rid -> preserve untouched, never drop
                    continue
                anchor = schedule.get(scene_name)
                if anchor is None:
                    continue  # intentionally pruned: scene dropped from the schedule
                minute = anchor.resolve(sun.sunrise_min, sun.sunset_min)
                tslot["start_time"] = {
                    "kind": "time",
                    "time": {"hour": minute // 60, "minute": minute % 60, "second": 0},
                }
                kept.append(tslot)
            if slots and not kept:  # defense-in-depth; plan blocks this first
                raise HueIacError(
                    f"refusing to apply: pruning would leave smart scene "
                    f"{change.name!r} with no timeslots"
                )
            kept.sort(key=_slot_minute)  # the bridge expects chronological order
            week["timeslots"] = kept
        _write_backup(self._backup_dir, smart_scene)
        self._client.update_resource("smart_scene", smart_scene["id"], {"week_timeslots": weeks})


class NightMotionReconciler(Reconciler):
    """Configures native night-time soft-red motion guidance on a zone.

    Ensures the day/evening/night zone scenes exist and rewrites the MotionAware
    ``behavior_instance`` so every timeslot targets the guidelight zone, with the
    night timeslot recalling the red scene and switching off quickly. The current
    automation config is backed up before any write. No daemon: the bridge runs it.
    """

    def __init__(
        self, client: HueClient, state: BridgeState, config: Config,
        backup_dir: str = ".hue-backup",
    ) -> None:
        super().__init__(client, state, config)
        self._backup_dir = backup_dir

    def _roles(self):
        nm = self._config.night_motion
        return (("Day", nm.day), ("Evening", nm.evening), ("Night", nm.night))

    def _scene_name(self, role: str) -> str:
        return f"{self._config.night_motion.zone} — {role}"

    def _transform(self, inst: dict, zone_rid: str, rids: dict[str, str]) -> dict:
        nm = self._config.night_motion
        return transform_automation(
            inst["configuration"], zone_rid=zone_rid,
            day_scene=rids["Day"], evening_scene=rids["Evening"], night_scene=rids["Night"],
            night_start=nm.start, night_off_min=nm.timeout_min,
            night_only=(nm.mode == "night_only"),
            day_start=nm.day_start,
        )

    def plan(self) -> list[Change]:
        nm = self._config.night_motion
        if nm is None:
            return []
        inst = self._state.behavior_instance(nm.automation)
        if inst is None:
            return [Change("night_motion", nm.automation, ChangeType.BLOCKED,
                           f"automation '{nm.automation}' not found on the bridge")]
        zone = self._state.group_optional(nm.zone)
        if zone is None:
            return [Change("night_motion", nm.zone, ChangeType.BLOCKED,
                           f"zone '{nm.zone}' not found — apply its areas.zones entry first")]
        scenes: dict[str, dict] = {}
        for role, _ in self._roles():
            sc = self._state.scene(self._scene_name(role))
            if sc and sc.get("group", {}).get("rid") == zone.rid:
                scenes[role] = sc
        missing = 3 - len(scenes)
        # Drift = an existing zone scene whose stored look no longer matches the config.
        drifted = [role for role, look in self._roles()
                   if role in scenes and not self._scene_matches(scenes[role], zone, look)]
        rids = {role: sc["id"] for role, sc in scenes.items()}
        try:
            if missing == 0:
                wiring_ok = self._transform(inst, zone.rid, rids) == inst.get("configuration")
            else:
                # Scenes still to create: dry-run the transform anyway so an
                # untransformable automation (e.g. a sun-anchored timeslot) blocks
                # here instead of crashing apply after the scenes are created.
                self._transform(inst, zone.rid, {role: "pending" for role, _ in self._roles()})
                wiring_ok = False
        except ValueError as e:
            return [Change("night_motion", nm.automation, ChangeType.BLOCKED, str(e))]
        if missing == 0 and not drifted and wiring_ok:
            return [Change("night_motion", nm.automation, ChangeType.NOOP,
                           "night guidance already configured")]
        night = f"{nm.start[0]:02d}:{nm.start[1]:02d}"
        parts: list[str] = []
        if missing:
            parts.append(f"create {missing} '{nm.zone}' scene(s)")
        if drifted:
            parts.append(f"update {len(drifted)} scene look(s) ({', '.join(drifted)})")
        if missing == 0 and not wiring_ok:
            parts.append(f"retarget '{nm.automation}' to '{nm.zone}'")
        summary = "; ".join(parts) + (
            f" — night {night} red @ {nm.night.brightness}%, off {nm.timeout_min}m")
        return [Change("night_motion", nm.automation, ChangeType.UPDATE, summary)]

    def _scene_matches(self, scene: dict, zone: Group, look) -> bool:
        """Return ``True`` if a live zone scene's actions match the desired look."""
        desired = scene_body(
            scene.get("metadata", {}).get("name", ""), zone.rid, list(zone.light_rids),
            mirek=look.mirek, hex=look.hex, brightness=look.brightness,
        )
        return scene_actions_match(scene.get("actions", []), desired["actions"])

    def owns(self, change: Change) -> bool:
        return change.resource == "night_motion"

    def apply(self, change: Change) -> None:
        if not change.is_actionable:
            return
        nm = self._config.night_motion
        inst = self._state.behavior_instance(nm.automation)
        zone = self._state.group_optional(nm.zone)
        if inst is None or zone is None:
            return
        rids = {role: self._ensure_scene(self._scene_name(role), zone, look)
                for role, look in self._roles()}
        new_cfg = self._transform(inst, zone.rid, rids)
        if new_cfg == inst.get("configuration"):
            return  # scenes ensured; automation wiring unchanged -> no re-PUT/backup needed
        self._backup(inst)
        self._client.update_resource("behavior_instance", inst["id"], {"configuration": new_cfg})

    def _ensure_scene(self, name: str, zone: Group, look) -> str:
        body = scene_body(
            name, zone.rid, list(zone.light_rids),
            mirek=look.mirek, hex=look.hex, brightness=look.brightness,
        )
        existing = self._state.scene(name)
        if existing and existing.get("group", {}).get("rid") == zone.rid:
            self._client.update_resource(
                "scene", existing["id"], {"metadata": body["metadata"], "actions": body["actions"]}
            )
            return existing["id"]
        return self._client.create_resource("scene", body)

    def _backup(self, inst: dict) -> None:
        _write_backup(self._backup_dir, inst)


class CircadianSceneReconciler(Reconciler):
    """Generates a smooth, sun-anchored circadian ``smart_scene`` from the curve.

    Samples :mod:`hue_iac.circadian` at the day's knee times (via
    :func:`hue_iac.circadian_scene.circadian_timeslots`) into up to six zone scenes
    and wires them to a ``smart_scene`` whose long ``transition_duration`` makes the
    bridge fade continuously between them — a native, daemon-free circadian cycle
    that re-anchors to the real sun on every apply. Built on the same scene/backup
    primitives as the other reconcilers; idempotent once converged.
    """

    def __init__(
        self, client: HueClient, state: BridgeState, config: Config,
        for_date: _dt.date | None = None, backup_dir: str = ".hue-backup",
    ) -> None:
        super().__init__(client, state, config)
        self._for_date = for_date
        self._backup_dir = backup_dir

    # -- helpers ------------------------------------------------------------ #
    def _solar(self) -> SolarCalculator:
        loc = self._config.location
        return SolarCalculator(loc.lat, loc.lon, loc.tz_offset_hours, tz=loc.tz)

    def _day(self) -> _dt.date:
        return self._for_date or _dt.date.today()

    def _sun_times(self):
        return self._solar().sun_times(self._day())

    def _steps(self):
        cs = self._config.circadian_scene
        return circadian_timeslots(
            self._config.circadian, self._solar(), self._day(), hand_off_min=cs.hand_off_min
        )

    def _transition_ms(self) -> int:
        cs = self._config.circadian_scene
        if cs.transition_ms is not None:
            return cs.transition_ms
        return int(self._config.circadian.ramp_minutes * 60_000)  # "ramp" -> the ramp width

    def _scene_name(self, index: int) -> str:
        return f"{self._config.circadian_scene.smart_scene} — {index + 1}"

    def _smart_scene_body(self, zone: Group, steps, rids: list[str]) -> dict:
        slots = [{
            "start_time": {"kind": "time",
                           "time": {"hour": s.minute // 60, "minute": s.minute % 60, "second": 0}},
            "target": {"rid": rid, "rtype": "scene"},
        } for s, rid in zip(steps, rids)]
        return {
            "metadata": {"name": self._config.circadian_scene.smart_scene},
            "group": {"rid": zone.rid, "rtype": "zone"},
            "week_timeslots": [{"timeslots": slots, "recurrence": list(_WEEKDAYS)}],
            "transition_duration": self._transition_ms(),
        }

    def _scenes_converged(self, zone: Group, steps) -> bool:
        for i, step in enumerate(steps):
            sc = self._state.scene(self._scene_name(i))
            if sc is None or sc.get("group", {}).get("rid") != zone.rid:
                return False
            desired = scene_body(self._scene_name(i), zone.rid, list(zone.light_rids),
                                 mirek=step.mirek, brightness=step.brightness, on=step.on)
            if not scene_actions_match(sc.get("actions", []), desired["actions"]):
                return False
        return True

    def _smart_scene_converged(self, zone: Group, steps) -> bool:
        ss = self._state.smart_scene(self._config.circadian_scene.smart_scene)
        if ss is None or ss.get("group", {}).get("rid") != zone.rid:
            return False
        if ss.get("transition_duration") != self._transition_ms():
            return False
        live = ss.get("week_timeslots", [{}])[0].get("timeslots", [])
        if len(live) != len(steps):
            return False
        live_pairs = sorted(
            (_slot_minute(t), self._state.scene_name_for(t.get("target", {}).get("rid")))
            for t in live)
        desired_pairs = sorted((s.minute, self._scene_name(i)) for i, s in enumerate(steps))
        return live_pairs == desired_pairs

    # -- plan / apply ------------------------------------------------------- #
    def plan(self) -> list[Change]:
        cs = self._config.circadian_scene
        if cs is None:
            return []
        zone = self._state.group_optional(cs.zone)
        if zone is None:
            return [Change("circadian_scene", cs.zone, ChangeType.BLOCKED,
                           f"zone '{cs.zone}' not found — apply its areas.zones entry first")]
        steps = self._steps()
        if not steps:
            return [Change("circadian_scene", cs.smart_scene, ChangeType.BLOCKED,
                           "the circadian curve produced no timeslots")]
        if self._scenes_converged(zone, steps) and self._smart_scene_converged(zone, steps):
            return [Change("circadian_scene", cs.smart_scene, ChangeType.NOOP,
                           "circadian cycle already sun-anchored")]
        mins = round(self._transition_ms() / 60_000)
        return [Change("circadian_scene", cs.smart_scene, ChangeType.UPDATE,
                       f"generate {len(steps)}-step circadian cycle on '{cs.zone}' ({mins}m fades)")]

    def owns(self, change: Change) -> bool:
        return change.resource == "circadian_scene"

    def apply(self, change: Change) -> None:
        if not change.is_actionable:
            return
        cs = self._config.circadian_scene
        zone = self._state.group_optional(cs.zone)
        if zone is None:
            return
        steps = self._steps()
        if not steps:
            raise HueIacError("refusing to apply: the circadian curve produced no timeslots")
        rids = [self._ensure_scene(self._scene_name(i), zone, step)
                for i, step in enumerate(steps)]
        body = self._smart_scene_body(zone, steps, rids)
        ss = self._state.smart_scene(cs.smart_scene)
        if ss is None:
            self._client.create_resource("smart_scene", body)
            return
        _write_backup(self._backup_dir, ss)
        if ss.get("group", {}).get("rid") != zone.rid:
            # a smart_scene's group is immutable -> replace it on the target zone
            self._client.delete_resource("smart_scene", ss["id"])
            self._client.create_resource("smart_scene", body)
        else:
            self._client.update_resource("smart_scene", ss["id"], {
                "week_timeslots": body["week_timeslots"],
                "transition_duration": body["transition_duration"]})

    def _ensure_scene(self, name: str, zone: Group, step) -> str:
        body = scene_body(name, zone.rid, list(zone.light_rids),
                          mirek=step.mirek, brightness=step.brightness, on=step.on)
        existing = self._state.scene(name)
        if existing and existing.get("group", {}).get("rid") == zone.rid:
            self._client.update_resource(
                "scene", existing["id"], {"metadata": body["metadata"], "actions": body["actions"]})
            return existing["id"]
        return self._client.create_resource("scene", body)


class SceneReconciler(Reconciler):
    """Ensures a bridge ``scene`` matches a declared :class:`SceneSpec`.

    Unlike :class:`SmartSceneReconciler` (which only re-times an existing native
    cycle), this owns a scene end-to-end: it creates the scene the first time and
    rewrites its per-light actions on drift. The scene is scoped to the spec's
    room/zone, so an external trigger (a Home Assistant automation) can recall it
    by name. If the target zone does not exist yet, the change is BLOCKED — create
    the zone first, then re-apply (the cold-start two-pass).
    """

    def plan(self) -> list[Change]:
        return [self._plan_spec(spec) for spec in self._config.scenes]

    def _plan_spec(self, spec: SceneSpec) -> Change:
        group = self._state.group_optional(spec.zone)
        if group is None:
            return Change(
                resource="scene",
                name=spec.name,
                change_type=ChangeType.BLOCKED,
                summary=f"zone {spec.zone!r} not found (create it first, then re-apply)",
            )
        zone_rids = set(group.light_rids)
        outside = [name for name, _ in spec.lights
                   if self._state.light(name).light_rid not in zone_rids]
        if outside:
            return Change("scene", spec.name, ChangeType.BLOCKED,
                          f"lights not in zone {spec.zone!r}: {', '.join(outside)}")
        desired = self._desired_actions(spec)
        existing = self._state.scene(spec.name)
        if existing is None:
            return Change("scene", spec.name, ChangeType.CREATE,
                          f"create scene in {spec.zone!r} with {len(desired)} light(s)")
        if self._actions_match(existing.get("actions", []), desired):
            return Change("scene", spec.name, ChangeType.NOOP, "scene already matches")
        return Change("scene", spec.name, ChangeType.UPDATE,
                      f"update {len(desired)} light action(s)")

    def owns(self, change: Change) -> bool:
        return change.resource == "scene"

    def apply(self, change: Change) -> None:
        if not change.is_actionable:
            return
        spec = next((s for s in self._config.scenes if s.name == change.name), None)
        if spec is None:
            return
        group = self._state.group_optional(spec.zone)
        if group is None:
            return
        actions = self._desired_actions(spec)
        existing = self._state.scene(spec.name)
        if existing is None:
            self._client.create_resource("scene", {
                "metadata": {"name": spec.name},
                "group": {"rid": group.rid, "rtype": group.rtype},
                "actions": actions,
            })
        else:
            self._client.update_resource("scene", existing["id"], {"actions": actions})

    # -- helpers ------------------------------------------------------------ #
    def _desired_actions(self, spec: SceneSpec) -> list[dict]:
        """Resolve each light name to a CLIP scene action (target + action body)."""
        actions: list[dict] = []
        for light_name, state in spec.lights:
            light = self._state.light(light_name)  # raises ConfigError if unknown
            actions.append({
                "target": {"rid": light.light_rid, "rtype": "light"},
                "action": self._action_body(state),
            })
        return actions

    @staticmethod
    def _action_body(state: LightState) -> dict:
        """Build a scene ``action`` body from a :class:`LightState`."""
        if not state.on:
            return {"on": {"on": False}}
        body: dict = {"on": {"on": True}}
        if state.brightness is not None:
            body["dimming"] = {"brightness": round(state.brightness, 1)}
        if state.color is not None:
            if state.color.mode == "xy" and state.color.hex is not None:
                x, y = ColorConverter.hex_to_xy(state.color.hex)
                body["color"] = {"xy": {"x": x, "y": y}}
            elif state.color.mode == "ct" and state.color.mirek is not None:
                body["color_temperature"] = {"mirek": state.color.mirek}
        return body

    @classmethod
    def _actions_match(cls, existing: list[dict], desired: list[dict]) -> bool:
        return cls._normalize(existing) == cls._normalize(desired)

    @staticmethod
    def _normalize(actions: list[dict]) -> dict:
        """Reduce a scene's actions to a comparable {rid: (on, bri, color)} map."""
        out: dict = {}
        for a in actions:
            rid = a.get("target", {}).get("rid")
            if rid is None:
                continue
            act = a.get("action", {})
            on = bool(act.get("on", {}).get("on", True))
            bri = act.get("dimming", {}).get("brightness")
            bri = None if bri is None else round(float(bri), 1)
            if "color" in act:
                xy = act["color"].get("xy", {})
                color = ("xy", round(float(xy.get("x", 0)), 4), round(float(xy.get("y", 0)), 4))
            elif "color_temperature" in act:
                mirek = act["color_temperature"].get("mirek")
                color = ("ct", int(mirek)) if mirek is not None else None
            else:
                color = None
            out[rid] = (on, bri, color)
        return out


class Planner:
    """Aggregates all reconcilers into one plan/apply surface.

    Args:
        client: An authenticated bridge client.
        state: A loaded :class:`~hue_iac.state.BridgeState`.
        config: The parsed IaC configuration.
    """

    def __init__(self, client: HueClient, state: BridgeState, config: Config) -> None:
        # Order matters: areas are reconciled before sensitivity so lights have
        # a home before anything else references them.
        self._reconcilers: list[Reconciler] = [
            AreaReconciler(client, state, config),
            SensitivityReconciler(client, state, config),
            SmartSceneReconciler(client, state, config),
            CircadianSceneReconciler(client, state, config),
            NightMotionReconciler(client, state, config),
            SceneReconciler(client, state, config),
        ]

    def plan(self) -> list[Change]:
        """Collect the plans from every reconciler."""
        changes: list[Change] = []
        for reconciler in self._reconcilers:
            changes.extend(reconciler.plan())
        return changes

    def apply(self, changes: list[Change]) -> int:
        """Apply every actionable change and return how many were applied."""
        applied = 0
        for reconciler in self._reconcilers:
            for change in changes:
                if not change.is_actionable:
                    continue
                if reconciler.owns(change):
                    reconciler.apply(change)
                    applied += 1
        return applied
