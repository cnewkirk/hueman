"""Declarative configuration model and loader.

This is the user-facing "language" of hueman. A YAML document is parsed into a
tree of frozen dataclasses with eager validation, so an invalid config fails at
``validate``/``plan`` time with a precise message rather than mid-apply against
the live bridge.

Design goals, in priority order:
  1. Declarative & idempotent  - the file is the desired state, full stop.
  2. Readable                  - a non-programmer can follow what a room does.
  3. Safe by default           - secrets stay out of the file; only resources
                                 tagged as managed are ever touched on apply.
"""

from __future__ import annotations

import datetime as _dt
import math
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfoNotFoundError

import yaml

from .circadian import MIREK_MAX, MIREK_MIN, CircadianParams
from .errors import ConfigError
from .sun import zone_offset_hours

# Resources we create are tagged with this marker in their Hue metadata name so
# plan/apply can identify "ours" and prune managed resources dropped from config.
DEFAULT_MARKER = "[iac]"

_DURATION_RE = re.compile(r"^\s*(\d+)\s*(ms|s|m|h)\s*$", re.IGNORECASE)
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")
_ANCHOR_RE = re.compile(r"^(sunrise|sunset)(?:\s*([+-])\s*(\d+)\s*(h|m))?$", re.IGNORECASE)


def parse_duration(value: Any, *, ctx: str) -> int:
    """Parse ``"90s"`` / ``"2m"`` / ``"1h"`` / ``"500ms"`` into milliseconds."""
    # bool subclasses int, so YAML `interval: yes` would otherwise parse as 1s.
    if isinstance(value, bool):
        raise ConfigError(f"{ctx}: duration must be a number or string like '90s', got {value!r}")
    if isinstance(value, (int, float)):
        if value < 0:
            raise ConfigError(f"{ctx}: duration must not be negative, got {value!r}")
        return int(value * 1000)  # bare number means seconds
    if not isinstance(value, str):
        raise ConfigError(f"{ctx}: duration must be a string like '90s', got {value!r}")
    m = _DURATION_RE.match(value)
    if not m:
        raise ConfigError(f"{ctx}: invalid duration {value!r} (use ms/s/m/h, e.g. '90s')")
    n, unit = int(m.group(1)), m.group(2).lower()
    return n * {"ms": 1, "s": 1000, "m": 60_000, "h": 3_600_000}[unit]


def parse_time_ref(value: Any, *, ctx: str) -> str:
    """Validate a time reference: ``HH:MM``, ``sunrise``, or ``sunset``.

    Returned verbatim; resolution to a concrete minute happens later against
    the day's :class:`~hueman.sun.SunTimes` so the same config works all year.
    """
    if value in ("sunrise", "sunset"):
        return value
    if isinstance(value, str) and _TIME_RE.match(value):
        return value
    raise ConfigError(f"{ctx}: invalid time {value!r} (use 'HH:MM', 'sunrise', or 'sunset')")


@dataclass(frozen=True)
class Anchor:
    """A schedule time expressed relative to the sun or as a fixed clock time.

    Attributes:
        base: ``"sunrise"``, ``"sunset"``, or ``"clock"``.
        value: For ``clock``, minutes after midnight. For sun bases, a signed
            offset in minutes from sunrise/sunset (e.g. -120 = two hours before).
    """

    base: str
    value: int

    def resolve(self, sunrise_min: float, sunset_min: float) -> int:
        """Resolve to a concrete minute-of-day (clamped to 0..1439)."""
        if self.base == "clock":
            minutes: float = float(self.value)
        else:
            anchor = sunrise_min if self.base == "sunrise" else sunset_min
            if not math.isfinite(anchor):  # polar day/night fallback
                anchor = 6 * 60 if self.base == "sunrise" else 20 * 60
            minutes = anchor + self.value
        return int(max(0, min(1439, round(minutes))))


def parse_anchor(value: Any, *, ctx: str) -> Anchor:
    """Parse ``"sunrise"`` / ``"sunset-2h"`` / ``"sunset+90m"`` / ``"23:30"``."""
    if isinstance(value, str):
        v = value.strip()
        if _TIME_RE.match(v):
            hour, minute = v.split(":")
            return Anchor("clock", int(hour) * 60 + int(minute))
        m = _ANCHOR_RE.match(v)
        if m:
            offset = 0
            if m.group(2):
                magnitude = int(m.group(3)) * (60 if m.group(4).lower() == "h" else 1)
                offset = magnitude if m.group(2) == "+" else -magnitude
            return Anchor(m.group(1).lower(), offset)
    raise ConfigError(
        f"{ctx}: invalid anchor {value!r} (use 'sunrise'/'sunset' with optional "
        "'+30m'/'-2h', or a clock time 'HH:MM')"
    )


def _require(d: dict, key: str, ctx: str) -> Any:
    if key not in d:
        raise ConfigError(f"{ctx}: missing required key '{key}'")
    return d[key]


def _as_dict(value: Any, ctx: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{ctx}: expected a mapping, got {type(value).__name__}")
    return value


# --------------------------------------------------------------------------- #
# Leaf value objects
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Location:
    lat: float
    lon: float
    tz_offset_hours: float
    tz: str | None = None

    @classmethod
    def parse(cls, d: dict, ctx: str) -> "Location":
        try:
            lat = float(_require(d, "lat", ctx))
            lon = float(_require(d, "lon", ctx))
        except (TypeError, ValueError) as e:
            raise ConfigError(f"{ctx}: lat/lon must be numbers ({e})")
        if not -90 <= lat <= 90 or not -180 <= lon <= 180:
            raise ConfigError(f"{ctx}: lat/lon out of range")

        tz_name = d.get("tz")
        derived: float | None = None
        if tz_name is not None:
            if not isinstance(tz_name, str):
                raise ConfigError(
                    f"{ctx}: 'tz' must be an IANA name string (e.g. 'America/Los_Angeles')"
                )
            try:
                derived = zone_offset_hours(tz_name, _dt.date.today())
            except (ZoneInfoNotFoundError, ValueError):
                raise ConfigError(f"{ctx}: unknown timezone {tz_name!r}")

        raw = d.get("tz_offset_hours")
        if raw is None:
            if derived is None:
                raise ConfigError(
                    f"{ctx}: 'tz_offset_hours' or 'tz' is required "
                    "(e.g. -5, or 'America/Los_Angeles')"
                )
            offset = derived
        else:
            offset = float(raw)
        return cls(lat=lat, lon=lon, tz_offset_hours=offset, tz=tz_name)


@dataclass(frozen=True)
class TlsConfig:
    """How to trust the bridge's self-signed certificate."""

    mode: str = "pin"  # pin (trust-on-first-use) | cacert | insecure
    cacert: str | None = None
    pin_file: str = ".hue-pin.json"

    @classmethod
    def parse(cls, d: dict, ctx: str) -> "TlsConfig":
        mode = d.get("mode", "pin")
        if mode not in ("pin", "cacert", "insecure"):
            raise ConfigError(f"{ctx}: tls.mode must be pin|cacert|insecure, got {mode!r}")
        if mode == "cacert" and not d.get("cacert"):
            raise ConfigError(f"{ctx}: tls.mode 'cacert' requires a 'cacert' path")
        return cls(mode=mode, cacert=d.get("cacert"), pin_file=d.get("pin_file", ".hue-pin.json"))


@dataclass(frozen=True)
class Bridge:
    host: str
    application_key: str | None
    tls: TlsConfig

    @classmethod
    def parse(cls, d: dict, ctx: str) -> "Bridge":
        host = d.get("host") or os.environ.get("HUE_BRIDGE_HOST")
        if not host:
            raise ConfigError(f"{ctx}: 'host' is required (or set $HUE_BRIDGE_HOST)")
        # Prefer env indirection so the application key never lands in the file.
        key_env = d.get("application_key_env", "HUE_APPLICATION_KEY")
        key = os.environ.get(key_env) if key_env else None
        if not key and d.get("application_key"):
            key = str(d["application_key"])
        return cls(host=str(host), application_key=key, tls=TlsConfig.parse(_as_dict(d.get("tls"), ctx), ctx))


@dataclass(frozen=True)
class Color:
    """A target colour, expressed one of three ways (mutually exclusive)."""

    mode: str                    # "circadian" | "ct" | "xy"
    mirek: int | None = None
    hex: str | None = None

    @classmethod
    def parse(cls, value: Any, ctx: str) -> "Color":
        if value == "circadian":
            return cls(mode="circadian")
        d = _as_dict(value, ctx)
        if "mirek" in d:
            try:
                mirek = int(d["mirek"])
            except (TypeError, ValueError):
                raise ConfigError(f"{ctx}: mirek must be an integer, got {d['mirek']!r}")
            if not MIREK_MIN <= mirek <= MIREK_MAX:
                raise ConfigError(f"{ctx}: mirek must be {MIREK_MIN}-{MIREK_MAX}")
            return cls(mode="ct", mirek=mirek)
        if "kelvin" in d:
            try:
                kelvin = int(d["kelvin"])
            except (TypeError, ValueError):
                raise ConfigError(f"{ctx}: kelvin must be an integer, got {d['kelvin']!r}")
            if kelvin <= 0:
                raise ConfigError(f"{ctx}: kelvin must be positive, got {kelvin}")
            mirek = round(1_000_000 / kelvin)
            mirek = max(MIREK_MIN, min(MIREK_MAX, mirek))
            return cls(mode="ct", mirek=mirek)
        if "hex" in d:
            if not _HEX_RE.match(str(d["hex"])):
                raise ConfigError(f"{ctx}: hex must be a 6-digit colour like '#ff2200'")
            return cls(mode="xy", hex=str(d["hex"]).lstrip("#"))
        raise ConfigError(f"{ctx}: color needs one of 'circadian', mirek/kelvin, or hex")


@dataclass(frozen=True)
class LightState:
    """A desired on/colour/brightness state for a motion response or standby."""

    on: bool = True
    brightness: float | None = None   # 0-100; None -> use circadian/default
    color: Color | None = None

    @classmethod
    def parse(cls, value: Any, ctx: str) -> "LightState":
        d = _as_dict(value, ctx)
        on = bool(d.get("on", True))
        bri = d.get("brightness")
        if bri is not None and not 0 <= float(bri) <= 100:
            raise ConfigError(f"{ctx}: brightness must be 0-100")
        color = Color.parse(d["color"], f"{ctx}.color") if "color" in d else None
        return cls(on=on, brightness=None if bri is None else float(bri), color=color)


@dataclass(frozen=True)
class Timeslot:
    """One time-of-day band of a motion policy."""

    name: str
    start: str                       # HH:MM | sunrise | sunset
    on_motion: LightState
    timeout_ms: int
    standby: LightState | None       # what "off" looks like in this band (None => fully off)

    @classmethod
    def parse(cls, d: dict, ctx: str) -> "Timeslot":
        name = _require(d, "name", ctx)
        ctx = f"{ctx}[{name}]"
        on_motion = (
            LightState(on=True, color=Color(mode="circadian"))
            if d.get("on_motion") == "circadian"
            else LightState.parse(_require(d, "on_motion", ctx), f"{ctx}.on_motion")
        )
        standby = LightState.parse(d["standby"], f"{ctx}.standby") if "standby" in d else None
        return cls(
            name=str(name),
            start=parse_time_ref(_require(d, "start", ctx), ctx=f"{ctx}.start"),
            on_motion=on_motion,
            timeout_ms=parse_duration(_require(d, "timeout", ctx), ctx=f"{ctx}.timeout"),
            standby=standby,
        )


@dataclass(frozen=True)
class DimBeforeOff:
    duration_ms: int
    recovery: bool = True

    @classmethod
    def parse(cls, value: Any, ctx: str) -> "DimBeforeOff":
        d = _as_dict(value, ctx)
        return cls(
            duration_ms=parse_duration(_require(d, "duration", ctx), ctx=f"{ctx}.duration"),
            recovery=bool(d.get("recovery", True)),
        )


@dataclass(frozen=True)
class ManualOverride:
    """How motion automation yields to a human who set a scene by hand."""

    respect: bool = True
    pause_ms: int = 3_600_000        # how long a manual change pauses motion
    resume_on_off: bool = True       # turning the group fully off re-arms motion early

    @classmethod
    def parse(cls, value: Any, ctx: str) -> "ManualOverride":
        d = _as_dict(value, ctx)
        return cls(
            respect=bool(d.get("respect", True)),
            pause_ms=parse_duration(d.get("pause", "1h"), ctx=f"{ctx}.pause"),
            resume_on_off=bool(d.get("resume_on_off", True)),
        )


@dataclass(frozen=True)
class LightLevel:
    """Only run the automation when the room is darker than this lux threshold."""

    threshold_lux: int

    @classmethod
    def parse(cls, value: Any, ctx: str) -> "LightLevel":
        d = _as_dict(value, ctx)
        return cls(threshold_lux=int(_require(d, "threshold_lux", ctx)))


_SENSITIVITY = {"low": 0, "medium": 1, "high": 2, "max": 3}


@dataclass(frozen=True)
class MotionPolicy:
    name: str
    sensor: str                      # device name of the motion sensor
    areas: tuple[str, ...]           # room/zone names this policy controls
    enabled: bool
    timeslots: tuple[Timeslot, ...]
    sensitivity: int | None
    light_level: LightLevel | None
    dim_before_off: DimBeforeOff | None
    manual_override: ManualOverride

    @classmethod
    def parse(cls, d: dict, ctx: str) -> "MotionPolicy":
        name = _require(d, "name", ctx)
        ctx = f"{ctx}[{name}]"
        areas = d.get("rooms") or d.get("areas") or d.get("zones")
        if not areas:
            raise ConfigError(f"{ctx}: at least one of 'rooms'/'zones'/'areas' is required")
        if isinstance(areas, str):
            areas = [areas]
        slots = [Timeslot.parse(_as_dict(s, ctx), f"{ctx}.timeslots") for s in _require(d, "timeslots", ctx)]
        if not slots:
            raise ConfigError(f"{ctx}: 'timeslots' must list at least one slot")
        sens = d.get("sensitivity")
        if isinstance(sens, str):
            if sens not in _SENSITIVITY:
                raise ConfigError(f"{ctx}: sensitivity must be one of {list(_SENSITIVITY)}")
            sens = _SENSITIVITY[sens]
        return cls(
            name=str(name),
            sensor=str(_require(d, "sensor", ctx)),
            areas=tuple(str(a) for a in areas),
            enabled=bool(d.get("enabled", True)),
            timeslots=tuple(slots),
            sensitivity=None if sens is None else int(sens),
            light_level=LightLevel.parse(d["light_level"], f"{ctx}.light_level") if "light_level" in d else None,
            dim_before_off=DimBeforeOff.parse(d["dim_before_off"], f"{ctx}.dim_before_off")
            if "dim_before_off" in d
            else None,
            manual_override=ManualOverride.parse(d.get("manual_override"), f"{ctx}.manual_override"),
        )


@dataclass(frozen=True)
class Area:
    """A declared room or zone and the lights assigned to it.

    Attributes:
        name: The area name (must match, or will create, a bridge group).
        kind: Either ``"room"`` or ``"zone"``.
        lights: Names of the lights assigned to this area.
        archetype: Optional Hue room archetype (for example ``"office"``).
    """

    name: str
    kind: str
    lights: tuple[str, ...]
    archetype: str | None = None

    @classmethod
    def parse(cls, d: dict, kind: str, ctx: str) -> "Area":
        """Parse one area entry of the given ``kind``."""
        name = _require(d, "name", ctx)
        ctx = f"{ctx}[{name}]"
        lights = d.get("lights") or []
        if isinstance(lights, str):
            lights = [lights]
        if not isinstance(lights, list):
            raise ConfigError(f"{ctx}: 'lights' must be a list of light names")
        return cls(
            name=str(name),
            kind=kind,
            lights=tuple(str(light) for light in lights),
            archetype=d.get("type") or d.get("archetype"),
        )


@dataclass(frozen=True)
class SmartSceneSpec:
    """Desired sun-anchored timing for a bridge ``smart_scene``.

    ``schedule`` maps each scene name in the smart scene to the :class:`Anchor`
    at which that scene should become active, so ``apply`` can re-time the
    bridge's native time-of-day cycle to real sunrise/sunset.
    """

    name: str
    schedule: tuple[tuple[str, Anchor], ...]

    @classmethod
    def parse(cls, d: dict, ctx: str) -> "SmartSceneSpec":
        name = _require(d, "name", ctx)
        ctx = f"{ctx}[{name}]"
        raw = _as_dict(_require(d, "schedule", ctx), f"{ctx}.schedule")
        if not raw:
            raise ConfigError(f"{ctx}: 'schedule' must map at least one scene name to an anchor")
        schedule = tuple(
            (str(scene), parse_anchor(anchor, ctx=f"{ctx}.schedule[{scene}]"))
            for scene, anchor in raw.items()
        )
        return cls(name=str(name), schedule=schedule)


@dataclass(frozen=True)
class SceneLook:
    """A uniform colour+brightness applied to every light of a zone scene."""

    brightness: float
    mirek: int | None = None
    hex: str | None = None

    @classmethod
    def parse(cls, value: Any, ctx: str) -> "SceneLook":
        d = _as_dict(value, ctx)
        try:
            bri = float(_require(d, "brightness", ctx))
        except (TypeError, ValueError) as e:
            raise ConfigError(f"{ctx}.brightness: must be a number ({e})")
        if not 0 <= bri <= 100:
            raise ConfigError(f"{ctx}.brightness must be 0-100")
        if "hex" in d:
            if not _HEX_RE.match(str(d["hex"])):
                raise ConfigError(f"{ctx}.hex must be a 6-digit colour like '#ff1400'")
            return cls(brightness=bri, hex=str(d["hex"]).lstrip("#"))
        if "mirek" in d or "kelvin" in d:
            try:
                mirek = int(d["mirek"]) if "mirek" in d else round(1_000_000 / int(d["kelvin"]))
            except (TypeError, ValueError, ZeroDivisionError) as e:
                raise ConfigError(f"{ctx}: invalid mirek/kelvin ({e})")
            mirek = max(MIREK_MIN, min(MIREK_MAX, mirek))
            return cls(brightness=bri, mirek=mirek)
        raise ConfigError(f"{ctx}: needs a colour ('hex' or 'mirek'/'kelvin')")


@dataclass(frozen=True)
class NightMotionSpec:
    """Night-time soft-red motion guidance on a whole-apartment zone.

    Tunes a MotionAware ``behavior_instance`` so motion at night recalls a soft
    deep-red scene across ``zone``; the day/evening looks are preserved (as zone
    scenes) but retargeted to the zone.

    In ``night_only`` mode the automation is active only ``start`` -> ``day_start``
    (wrapping midnight); ``day_start`` -> ``start`` is covered by an actionless
    timeslot so dark evenings before the hand-off get no recalls and no all_off.
    """

    automation: str
    zone: str
    start: tuple[int, int]
    timeout_min: int
    day: SceneLook
    evening: SceneLook
    night: SceneLook
    mode: str = "full"
    day_start: tuple[int, int] = (8, 0)

    @classmethod
    def parse(cls, value: Any, ctx: str = "night_motion") -> "NightMotionSpec":
        d = _as_dict(value, ctx)
        start = parse_time_ref(_require(d, "start", ctx), ctx=f"{ctx}.start")
        if start in ("sunrise", "sunset"):
            raise ConfigError(f"{ctx}.start must be a clock time like '22:34'")
        hh, mm = start.split(":")
        timeout_min = parse_duration(_require(d, "timeout", ctx), ctx=f"{ctx}.timeout") // 60_000
        if timeout_min < 1:
            raise ConfigError(f"{ctx}.timeout must be at least 1 minute")
        mode = str(d.get("mode", "full"))
        if mode not in ("full", "night_only"):
            raise ConfigError(f"{ctx}.mode must be 'full' or 'night_only'")
        day_start_raw = parse_time_ref(d.get("day_start", "08:00"), ctx=f"{ctx}.day_start")
        if day_start_raw in ("sunrise", "sunset"):
            raise ConfigError(f"{ctx}.day_start must be a clock time like '08:00'")
        dh, dm = day_start_raw.split(":")
        day_start = (int(dh), int(dm))
        if not ((0, 0) < day_start < (int(hh), int(mm))):
            raise ConfigError(
                f"{ctx}.day_start must be after 00:00 and before {ctx}.start "
                "(the no-op slot sits between the midnight clone and the night slot)"
            )
        return cls(
            automation=str(_require(d, "automation", ctx)),
            zone=str(_require(d, "zone", ctx)),
            start=(int(hh), int(mm)),
            timeout_min=timeout_min,
            day=SceneLook.parse(_require(d, "day", ctx), f"{ctx}.day"),
            evening=SceneLook.parse(_require(d, "evening", ctx), f"{ctx}.evening"),
            night=SceneLook.parse(_require(d, "night", ctx), f"{ctx}.night"),
            mode=mode,
            day_start=day_start,
        )


@dataclass(frozen=True)
class CircadianSceneSpec:
    """A generated, sun-anchored circadian ``smart_scene``.

    The *look* comes from the top-level ``circadian`` params; this block only says
    which smart scene to own, which zone to drive, how long each cross-fade runs,
    and when the day cycle hands off to ``night_motion``.

    Attributes:
        smart_scene: Name of the smart scene this owns (created/replaced).
        zone: Zone the generated scenes and smart scene target.
        transition_ms: Cross-fade length in ms, or ``None`` for ``"ramp"`` (use the
            circadian ramp width, so dawn/dusk render as gentle gradients).
        hand_off_min: Clock minute of the last (wind-down) timeslot; after it,
            ``night_motion`` owns the lights.
    """

    smart_scene: str
    zone: str
    transition_ms: int | None
    hand_off_min: int

    @classmethod
    def parse(cls, value: Any, ctx: str = "circadian_scene") -> "CircadianSceneSpec":
        d = _as_dict(value, ctx)
        transition = d.get("transition", "ramp")
        transition_ms = None if transition == "ramp" else parse_duration(
            transition, ctx=f"{ctx}.transition"
        )
        hand_off = parse_time_ref(d.get("hand_off", "22:34"), ctx=f"{ctx}.hand_off")
        if hand_off in ("sunrise", "sunset"):
            raise ConfigError(f"{ctx}.hand_off must be a clock time like '22:34'")
        hh, mm = hand_off.split(":")
        return cls(
            smart_scene=str(_require(d, "smart_scene", ctx)),
            zone=str(_require(d, "zone", ctx)),
            transition_ms=transition_ms,
            hand_off_min=int(hh) * 60 + int(mm),
        )


def _opt_str(value: Any) -> str | None:
    """Return ``str(value)`` or ``None`` when ``value`` is ``None``."""
    return None if value is None else str(value)


@dataclass(frozen=True)
class BiasLight:
    """One viewing-area fixture in the daemon's TV bias-hold set.

    Attributes:
        name: The Hue light name the daemon drives directly (per-light).
        look: The static hold look (always on; a concrete colour, never circadian).
        idle: What the light does when the TV is off — ``"circadian"`` (follow the
            curve while in the daemon's active window) or ``"off"``.
    """

    name: str
    look: LightState
    idle: str

    @classmethod
    def parse(cls, name: str, value: Any, ctx: str) -> "BiasLight":
        d = _as_dict(value, ctx)
        look_d = _as_dict(_require(d, "look", ctx), f"{ctx}.look")
        if "brightness" not in look_d:
            raise ConfigError(f"{ctx}.look: 'brightness' is required for a bias look")
        try:
            bri = float(look_d["brightness"])
        except (TypeError, ValueError):
            raise ConfigError(
                f"{ctx}.look: brightness must be a number, got {look_d['brightness']!r}")
        if not 0 <= bri <= 100:
            raise ConfigError(f"{ctx}.look: brightness must be 0-100")
        color = Color.parse(look_d, f"{ctx}.look")  # requires mirek/kelvin/hex
        if color.mode == "circadian":
            raise ConfigError(f"{ctx}.look: a bias look must be a static colour, not 'circadian'")
        # YAML 1.1 coerces an unquoted ``idle: off`` to the boolean ``False``;
        # accept that as the string "off" so the value needn't be quoted.
        raw_idle = d.get("idle", "circadian")
        idle = "off" if raw_idle is False else str(raw_idle)
        if idle not in ("circadian", "off"):
            raise ConfigError(f"{ctx}.idle must be 'circadian' or 'off'")
        return cls(name=str(name), look=LightState(on=True, brightness=bri, color=color), idle=idle)


@dataclass(frozen=True)
class BiasSpec:
    """Daemon-native TV bias hold: a per-light set plus pluggable on/off triggers.

    When any enabled trigger source reports the TV on, the daemon holds each
    :class:`BiasLight` at its ``look``; otherwise each light follows its ``idle``
    behaviour. Triggers are OR-combined; all are optional (a spec with none is a
    valid dormant state). ``probe`` is the daemon's own reachability check and is
    disabled by default.

    ``transition_ms`` is the *edge* fade — used when the committed TV state
    flips. Deliberately short (default 2s): reusing the steady-state 75s/90s
    fades on a mode flip reads as "a minute of nothing, then an abrupt change".
    """

    lights: tuple[BiasLight, ...]
    transition_ms: int
    sse_on: str | None
    sse_off: str | None
    file_on: str | None
    file_off: str | None
    probe_enabled: bool
    probe_host: str | None
    probe_mode: str          # "tcp" | "icmp"
    probe_port: int
    probe_interval_ms: int
    probe_debounce_ms: int

    @classmethod
    def parse(cls, value: Any, ctx: str = "circadian_daemon.bias") -> "BiasSpec":
        d = _as_dict(value, ctx)
        raw_lights = _as_dict(_require(d, "lights", ctx), f"{ctx}.lights")
        if not raw_lights:
            raise ConfigError(f"{ctx}.lights: at least one light is required")
        lights = tuple(
            BiasLight.parse(name, spec, f"{ctx}.lights[{name}]")
            for name, spec in raw_lights.items()
        )
        triggers = _as_dict(d.get("triggers"), f"{ctx}.triggers")
        sse = _as_dict(triggers.get("sse"), f"{ctx}.triggers.sse")
        cf = _as_dict(triggers.get("control_file"), f"{ctx}.triggers.control_file")
        probe = _as_dict(triggers.get("probe"), f"{ctx}.triggers.probe")
        mode = str(probe.get("mode", "tcp"))
        if mode not in ("tcp", "icmp"):
            raise ConfigError(f"{ctx}.triggers.probe.mode must be 'tcp' or 'icmp'")
        return cls(
            lights=lights,
            transition_ms=parse_duration(d.get("transition", "2s"), ctx=f"{ctx}.transition"),
            sse_on=_opt_str(sse.get("on_trigger")),
            sse_off=_opt_str(sse.get("off_trigger")),
            file_on=_opt_str(cf.get("on_file")),
            file_off=_opt_str(cf.get("off_file")),
            probe_enabled=bool(probe.get("enabled", False)),
            probe_host=_opt_str(probe.get("host")),
            probe_mode=mode,
            probe_port=int(probe.get("port", 3001)),
            probe_interval_ms=parse_duration(probe.get("interval", "5s"), ctx=f"{ctx}.triggers.probe.interval"),
            probe_debounce_ms=parse_duration(probe.get("debounce", "5s"), ctx=f"{ctx}.triggers.probe.debounce"),
        )


@dataclass(frozen=True)
class CircadianDaemonSpec:
    """Tunables for the persistent circadian daemon (all from YAML, with defaults)."""

    zone: str
    start: Anchor
    hand_off_min: int
    interval_ms: int
    transition_ms: int
    fade_off_ms: int
    detect_override: bool
    echo_ttl_ms: int
    resume_on_power_cycle: bool
    resume_trigger: str | None
    control_file: str
    daily_safety_resume: bool
    brightness_floor: float | None
    brightness_ceiling: float | None
    retry_on_error_ms: int
    sse_backoff_max_ms: int
    log_path: str
    log_level: str
    # Settle-and-compare override detection (replaces the brightness-echo
    # mechanism). A grouped_light brightness is only judged once it has *held*
    # for ``settle_window_ms`` (stable within ``settle_epsilon``); a settled value
    # more than ``override_band`` percent from the daemon's commanded target is a
    # human override. Defaulted (and last) so existing test helpers keep working.
    override_band: float = 8.0       # percent; settled bri within this of target = self
    settle_window_ms: int = 2500     # brightness must hold this long to count as settled
    settle_epsilon: float = 0.75     # percent; |new-prev| <= this counts as "same value"
    # Daemon-native TV bias hold (optional). Defaulted (and last) so existing
    # test helpers / for_test paths keep working unchanged.
    bias: "BiasSpec | None" = None

    @classmethod
    def parse(cls, value: Any, ctx: str = "circadian_daemon") -> "CircadianDaemonSpec":
        d = _as_dict(value, ctx)
        mo = _as_dict(d.get("manual_override"), f"{ctx}.manual_override")
        retry = _as_dict(d.get("retry"), f"{ctx}.retry")
        log = _as_dict(d.get("log"), f"{ctx}.log")
        hand_off = parse_time_ref(d.get("hand_off", "22:34"), ctx=f"{ctx}.hand_off")
        if hand_off in ("sunrise", "sunset"):
            raise ConfigError(f"{ctx}.hand_off must be a clock time like '22:34'")
        hh, mm = hand_off.split(":")
        floor = d.get("brightness_floor")
        ceil = d.get("brightness_ceiling")
        return cls(
            zone=str(_require(d, "zone", ctx)),
            start=parse_anchor(d.get("start", "sunrise"), ctx=f"{ctx}.start"),
            hand_off_min=int(hh) * 60 + int(mm),
            interval_ms=parse_duration(d.get("interval", "60s"), ctx=f"{ctx}.interval"),
            transition_ms=parse_duration(d.get("transition", "75s"), ctx=f"{ctx}.transition"),
            fade_off_ms=parse_duration(d.get("fade_off", "90s"), ctx=f"{ctx}.fade_off"),
            detect_override=bool(mo.get("detect", True)),
            echo_ttl_ms=parse_duration(mo.get("echo_ttl", "4s"), ctx=f"{ctx}.manual_override.echo_ttl"),
            resume_on_power_cycle=bool(mo.get("resume_on_power_cycle", True)),
            resume_trigger=None if mo.get("resume_trigger") is None else str(mo["resume_trigger"]),
            control_file=str(mo.get("control_file", ".hue-circadian-resume")),
            daily_safety_resume=bool(mo.get("daily_safety_resume", True)),
            brightness_floor=None if floor is None else float(floor),
            brightness_ceiling=None if ceil is None else float(ceil),
            retry_on_error_ms=parse_duration(retry.get("on_error", "30s"), ctx=f"{ctx}.retry.on_error"),
            sse_backoff_max_ms=parse_duration(retry.get("sse_backoff_max", "60s"), ctx=f"{ctx}.retry.sse_backoff_max"),
            log_path=str(log.get("path", "logs/circadian.log")),
            log_level=str(log.get("level", "info")),
            override_band=float(mo.get("override_band", 8.0)),
            settle_window_ms=parse_duration(mo.get("settle_window", "2500ms"), ctx=f"{ctx}.manual_override.settle_window"),
            settle_epsilon=float(mo.get("settle_epsilon", 0.75)),
            bias=BiasSpec.parse(d["bias"], f"{ctx}.bias") if d.get("bias") else None,
        )


@dataclass(frozen=True)
class SceneSpec:
    """Desired bridge ``scene``: a per-light look scoped to one room/zone.

    ``lights`` maps each light name to the :class:`LightState` it holds in this
    scene, so ``apply`` can create/update a real bridge scene that an external
    trigger (e.g. a Home Assistant automation) recalls by name.
    """

    name: str
    zone: str
    lights: tuple[tuple[str, LightState], ...]

    @classmethod
    def parse(cls, d: dict, ctx: str) -> "SceneSpec":
        name = _require(d, "name", ctx)
        ctx = f"{ctx}[{name}]"
        zone = str(_require(d, "zone", ctx))
        raw = _as_dict(_require(d, "lights", ctx), f"{ctx}.lights")
        if not raw:
            raise ConfigError(f"{ctx}: 'lights' must map at least one light name to a state")
        lights = []
        for light_name, raw_state in raw.items():
            state = LightState.parse(raw_state, f"{ctx}.lights[{light_name}]")
            if state.color is not None and state.color.mode == "circadian":
                raise ConfigError(
                    f"{ctx}.lights[{light_name}]: a scene is a static look; "
                    f"'circadian' colour is not allowed"
                )
            lights.append((str(light_name), state))
        return cls(name=str(name), zone=zone, lights=tuple(lights))


@dataclass(frozen=True)
class SecuritySpec:
    """Daemon-native security mode: an escalating ALERT -> CHAOS whole-home show.

    Triggered manually (OR-combined sse / control-file sources; no arming), it
    preempts circadian + bias. The CHAOS phase is safety-capped: ``min_flash_interval``
    floors how fast a group's brightness may toggle, keeping luminance flashing
    out of the 3-30 Hz photosensitive-seizure band. Sound is decoupled — the daemon
    only writes a phase cue to ``cue_file`` / POSTs ``cue_webhook``.
    """

    groups: tuple[str, ...]
    alert_seconds: int
    alert_color: str            # 6 hex digits, no leading '#'
    alert_min_brightness: float
    alert_breathe_hz: float
    frame_interval_ms: int
    min_flash_interval_ms: int
    max_duration_ms: int
    sse_on: str | None
    sse_off: str | None
    file_on: str | None
    file_off: str | None
    cue_file: str | None
    cue_webhook: str | None
    poll_interval_ms: int
    lights_per_frame: int

    @classmethod
    def parse(cls, value: Any, ctx: str = "security") -> "SecuritySpec":
        d = _as_dict(value, ctx)
        raw_groups = d.get("groups") or []
        if isinstance(raw_groups, str):
            raw_groups = [raw_groups]
        if not isinstance(raw_groups, list) or not raw_groups:
            raise ConfigError(f"{ctx}.groups: at least one group name is required")
        groups = tuple(str(g) for g in raw_groups)

        alert = _as_dict(d.get("alert"), f"{ctx}.alert")
        alert_seconds = int(alert.get("seconds", 10))
        if alert_seconds <= 0:
            raise ConfigError(f"{ctx}.alert.seconds must be > 0")
        color = str(alert.get("color", "#ff0000"))
        if not _HEX_RE.match(color):
            raise ConfigError(f"{ctx}.alert.color must be a 6-digit colour like '#ff0000'")
        min_b = float(alert.get("min_brightness", 40))
        if not 0 <= min_b <= 100:
            raise ConfigError(f"{ctx}.alert.min_brightness must be 0-100")
        breathe_hz = float(alert.get("breathe_hz", 0.5))
        if not 0 < breathe_hz <= 2:
            raise ConfigError(f"{ctx}.alert.breathe_hz must be in (0, 2]")

        chaos = _as_dict(d.get("chaos"), f"{ctx}.chaos")
        frame_ms = parse_duration(chaos.get("frame_interval", "250ms"), ctx=f"{ctx}.chaos.frame_interval")
        if frame_ms < 50:
            raise ConfigError(f"{ctx}.chaos.frame_interval must be >= 50ms")
        flash_ms = parse_duration(chaos.get("min_flash_interval", "350ms"), ctx=f"{ctx}.chaos.min_flash_interval")
        if flash_ms < 334:
            raise ConfigError(
                f"{ctx}.chaos.min_flash_interval must be >= 334ms "
                "(keeps flashing below the 3 Hz photosensitive-seizure band)"
            )
        lights_per_frame = int(chaos.get("lights_per_frame", 3))
        if lights_per_frame < 1:
            raise ConfigError(f"{ctx}.chaos.lights_per_frame must be >= 1")
        max_ms = parse_duration(d.get("max_duration", "10m"), ctx=f"{ctx}.max_duration")
        if max_ms <= alert_seconds * 1000:
            raise ConfigError(f"{ctx}.max_duration must exceed alert.seconds")

        triggers = _as_dict(d.get("triggers"), f"{ctx}.triggers")
        sse = _as_dict(triggers.get("sse"), f"{ctx}.triggers.sse")
        cf = _as_dict(triggers.get("control_file"), f"{ctx}.triggers.control_file")
        poll_ms = parse_duration(triggers.get("poll_interval", "1s"), ctx=f"{ctx}.triggers.poll_interval")
        sound = _as_dict(d.get("sound"), f"{ctx}.sound")
        return cls(
            groups=groups,
            alert_seconds=alert_seconds,
            alert_color=color.lstrip("#"),
            alert_min_brightness=min_b,
            alert_breathe_hz=breathe_hz,
            frame_interval_ms=frame_ms,
            min_flash_interval_ms=flash_ms,
            max_duration_ms=max_ms,
            sse_on=_opt_str(sse.get("on_trigger")),
            sse_off=_opt_str(sse.get("off_trigger")),
            file_on=_opt_str(cf.get("on_file")),
            file_off=_opt_str(cf.get("off_file")),
            cue_file=_opt_str(sound.get("cue_file")),
            cue_webhook=_opt_str(sound.get("webhook")),
            poll_interval_ms=poll_ms,
            lights_per_frame=lights_per_frame,
        )


@dataclass(frozen=True)
class Config:
    bridge: Bridge
    location: Location
    circadian: CircadianParams
    motion_policies: tuple[MotionPolicy, ...]
    areas: tuple[Area, ...] = ()
    smart_scenes: tuple[SmartSceneSpec, ...] = ()
    scenes: tuple[SceneSpec, ...] = ()
    night_motion: NightMotionSpec | None = None
    circadian_scene: CircadianSceneSpec | None = None
    circadian_daemon: CircadianDaemonSpec | None = None
    security: SecuritySpec | None = None
    require_all_lights_assigned: bool = True
    marker: str = DEFAULT_MARKER

    @classmethod
    def parse(cls, doc: dict) -> "Config":
        if not isinstance(doc, dict):
            raise ConfigError("top-level config must be a mapping")
        bridge = Bridge.parse(_as_dict(_require(doc, "bridge", "config"), "bridge"), "bridge")
        location = Location.parse(_as_dict(_require(doc, "location", "config"), "location"), "location")
        circ = _parse_circadian(_as_dict(doc.get("circadian"), "circadian"))
        policies = [
            MotionPolicy.parse(_as_dict(p, "motion_policies"), "motion_policies")
            for p in (doc.get("motion_policies") or [])
        ]
        _check_unique([p.name for p in policies], "motion_policies", "name")
        areas = _parse_areas(_as_dict(doc.get("areas"), "areas"))
        _check_unique([a.name for a in areas], "areas", "name")
        _check_single_room_membership(areas)
        smart_scenes = tuple(
            SmartSceneSpec.parse(_as_dict(s, "smart_scenes"), "smart_scenes")
            for s in (doc.get("smart_scenes") or [])
        )
        _check_unique([s.name for s in smart_scenes], "smart_scenes", "name")
        scenes = tuple(
            SceneSpec.parse(_as_dict(s, "scenes"), "scenes")
            for s in (doc.get("scenes") or [])
        )
        _check_unique([s.name for s in scenes], "scenes", "name")
        night_motion = (
            NightMotionSpec.parse(doc["night_motion"]) if doc.get("night_motion") else None
        )
        circadian_scene = (
            CircadianSceneSpec.parse(doc["circadian_scene"]) if doc.get("circadian_scene") else None
        )
        circadian_daemon = (
            CircadianDaemonSpec.parse(doc["circadian_daemon"]) if doc.get("circadian_daemon") is not None else None
        )
        security = (
            SecuritySpec.parse(doc["security"]) if doc.get("security") else None
        )
        return cls(
            bridge=bridge,
            location=location,
            circadian=circ,
            motion_policies=tuple(policies),
            areas=areas,
            smart_scenes=smart_scenes,
            scenes=scenes,
            night_motion=night_motion,
            circadian_scene=circadian_scene,
            circadian_daemon=circadian_daemon,
            security=security,
            require_all_lights_assigned=bool(doc.get("require_all_lights_assigned", True)),
            marker=str(doc.get("marker", DEFAULT_MARKER)),
        )


def _parse_circadian(d: dict) -> CircadianParams:
    base = CircadianParams()
    kw: dict[str, Any] = {}
    for f in (
        "day_mirek", "evening_mirek", "night_mirek",
        "day_brightness", "evening_brightness", "night_brightness",
        "ramp_minutes",
    ):
        if f in d:
            kw[f] = d[f]
    if "night_start" in d:
        # Was a parsed-but-dead knob: the elevation-driven curve derives night
        # from the sun, so setting it silently did nothing. Fail loudly instead.
        raise ConfigError(
            "circadian.night_start is no longer supported (night is derived from "
            "solar elevation); remove it from the config")
    try:
        return replace(base, **kw)
    except (ValueError, TypeError) as e:
        raise ConfigError(f"circadian: {e}")


def _parse_areas(d: dict) -> tuple[Area, ...]:
    """Parse the ``areas`` section into room and zone entries."""
    areas: list[Area] = []
    for entry in d.get("rooms") or []:
        areas.append(Area.parse(_as_dict(entry, "areas.rooms"), "room", "areas.rooms"))
    for entry in d.get("zones") or []:
        areas.append(Area.parse(_as_dict(entry, "areas.zones"), "zone", "areas.zones"))
    return tuple(areas)


def _check_single_room_membership(areas: tuple[Area, ...]) -> None:
    """Enforce the Hue rule that a light belongs to at most one room.

    Lights may appear in any number of zones, which overlap freely.
    """
    seen_room: dict[str, str] = {}
    for area in areas:
        if area.kind != "room":
            continue
        for light in area.lights:
            if light in seen_room:
                raise ConfigError(
                    f"light {light!r} assigned to two rooms "
                    f"({seen_room[light]!r} and {area.name!r}); a light can be in only one room"
                )
            seen_room[light] = area.name


def _check_unique(names: list[str], ctx: str, field_name: str) -> None:
    seen = set()
    for n in names:
        if n in seen:
            raise ConfigError(f"{ctx}: duplicate {field_name} {n!r}")
        seen.add(n)


def load_config(path: str | Path) -> Config:
    """Read and fully validate a YAML config file."""
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        doc = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: YAML parse error: {e}")
    return Config.parse(doc or {})
