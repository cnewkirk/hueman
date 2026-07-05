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
    if isinstance(value, str) and value in ("sunrise", "sunset"):
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


def _require(d: dict[str, Any], key: str, ctx: str) -> Any:
    """Return ``d[key]``, raising :class:`ConfigError` naming ``ctx`` if absent."""
    if key not in d:
        raise ConfigError(f"{ctx}: missing required key '{key}'")
    return d[key]


def _as_dict(value: Any, ctx: str) -> dict[str, Any]:
    """Coerce a YAML node to a mapping: ``None`` becomes ``{}``, non-dicts raise."""
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
    """Where the lights live, for local sunrise/sunset computation.

    Attributes:
        lat: Latitude in decimal degrees, north positive.
        lon: Longitude in decimal degrees, east positive.
        tz_offset_hours: Local UTC offset in hours (fixed, or derived from ``tz``).
        tz: Optional IANA timezone name; when set, sun math stays DST-correct.
    """

    lat: float
    lon: float
    tz_offset_hours: float
    tz: str | None = None

    @classmethod
    def parse(cls, d: dict[str, Any], ctx: str) -> "Location":
        """Parse the ``location:`` block.

        Validates lat/lon are numbers in range and that at least one of
        ``tz`` (a known IANA name) or ``tz_offset_hours`` is given; raises
        :class:`ConfigError` otherwise. A ``tz`` name wins for DST-correctness,
        with the offset derived for today as a fallback value.
        """
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
    def parse(cls, d: dict[str, Any], ctx: str) -> "TlsConfig":
        """Parse the ``bridge.tls:`` block; ``mode: cacert`` requires a ``cacert`` path."""
        mode = d.get("mode", "pin")
        if mode not in ("pin", "cacert", "insecure"):
            raise ConfigError(f"{ctx}: tls.mode must be pin|cacert|insecure, got {mode!r}")
        if mode == "cacert" and not d.get("cacert"):
            raise ConfigError(f"{ctx}: tls.mode 'cacert' requires a 'cacert' path")
        return cls(mode=mode, cacert=d.get("cacert"), pin_file=d.get("pin_file", ".hue-pin.json"))


@dataclass(frozen=True)
class Bridge:
    """How to reach and authenticate to the Hue bridge.

    Attributes:
        host: Bridge hostname or IP.
        application_key: CLIP API key, or ``None`` if not resolvable yet
            (commands that talk to the bridge fail later with a clear message).
        tls: How to trust the bridge's self-signed certificate.
    """

    host: str
    application_key: str | None
    tls: TlsConfig

    @classmethod
    def parse(cls, d: dict[str, Any], ctx: str) -> "Bridge":
        """Parse the ``bridge:`` block.

        ``host`` may come from the file or ``$HUE_BRIDGE_HOST`` (one is
        required). The application key is resolved env-first (the env var named
        by ``application_key_env``, default ``HUE_APPLICATION_KEY``) so secrets
        stay out of the file; an inline ``application_key`` is the fallback.
        """
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
        """Parse a colour node into a :class:`Color`.

        Accepts the string ``"circadian"``, or a mapping with one of ``mirek``
        (validated against the Hue range), ``kelvin`` (converted to mirek and
        clamped), or ``hex`` (6-digit RGB) — checked in that order, first match
        wins. Raises :class:`ConfigError` for anything else.
        """
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
        """Parse a light-state mapping into a :class:`LightState`.

        All of ``on``/``brightness``/``color`` are optional. Validates
        ``brightness`` is 0-100 when present; ``color`` is delegated to
        :meth:`Color.parse`.
        """
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
    def parse(cls, d: dict[str, Any], ctx: str) -> "Timeslot":
        """Parse one entry of a motion policy's ``timeslots:`` list.

        Requires ``name``, ``start``, ``on_motion`` and ``timeout``. The
        ``on_motion: circadian`` shorthand expands to an on-state with
        circadian colour; a missing ``standby`` means "fully off".
        """
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
    """Warn before lights-out: dim for ``duration_ms``, restoring on new motion.

    Attributes:
        duration_ms: How long the dimmed warning phase lasts.
        recovery: Whether motion during the dim phase restores the on-state.
    """

    duration_ms: int
    recovery: bool = True

    @classmethod
    def parse(cls, value: Any, ctx: str) -> "DimBeforeOff":
        """Parse a ``dim_before_off:`` mapping; ``duration`` is required."""
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
        """Parse a ``manual_override:`` mapping (all keys optional, ``None`` ok)."""
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
        """Parse a ``light_level:`` mapping; ``threshold_lux`` is required."""
        d = _as_dict(value, ctx)
        return cls(threshold_lux=int(_require(d, "threshold_lux", ctx)))


_SENSITIVITY = {"low": 0, "medium": 1, "high": 2, "max": 3}


@dataclass(frozen=True)
class MotionPolicy:
    """One motion-sensor automation: sensor + target areas + per-band behaviour.

    Attributes:
        name: Policy name (unique across the config).
        sensor: Device name of the motion sensor.
        areas: Room/zone names this policy controls.
        enabled: Whether the automation is active on the bridge.
        timeslots: Time-of-day bands, each with its own look and timeout.
        sensitivity: Sensor sensitivity 0-3, or ``None`` to leave it alone.
        light_level: Optional lux gate (only run when darker than threshold).
        dim_before_off: Optional dim-warning phase before turning off.
        manual_override: How the automation yields to manual scene changes.
    """

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
    def parse(cls, d: dict[str, Any], ctx: str) -> "MotionPolicy":
        """Parse one entry of the ``motion_policies:`` list.

        Requires ``name``, ``sensor``, at least one of ``rooms``/``zones``/
        ``areas`` (a bare string is accepted as a one-element list), and a
        non-empty ``timeslots`` list. ``sensitivity`` accepts the named levels
        ``low``/``medium``/``high``/``max`` or a raw integer.
        """
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
    def parse(cls, d: dict[str, Any], kind: str, ctx: str) -> "Area":
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
    def parse(cls, d: dict[str, Any], ctx: str) -> "SmartSceneSpec":
        """Parse one entry of the ``smart_scenes:`` list.

        Requires ``name`` and a non-empty ``schedule`` mapping of scene name to
        anchor expression (``sunrise``/``sunset±offset`` or ``HH:MM``).
        """
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
        """Parse a scene-look mapping into a :class:`SceneLook`.

        Requires ``brightness`` (0-100) plus a colour as ``hex`` or
        ``mirek``/``kelvin`` (kelvin is converted to mirek and clamped to the
        Hue range).
        """
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
        """Parse the ``night_motion:`` block.

        Requires ``automation``, ``zone``, ``start``, ``timeout`` and the three
        ``day``/``evening``/``night`` looks. ``start`` and ``day_start`` must be
        clock times (not sun anchors), ``timeout`` at least one minute, ``mode``
        one of ``full``/``night_only``, and ``day_start`` strictly between
        00:00 and ``start`` (it delimits the actionless daytime slot).
        """
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
        """Parse the ``circadian_scene:`` block.

        Requires ``smart_scene`` and ``zone``. ``transition`` defaults to
        ``"ramp"`` (mapped to ``transition_ms=None``); ``hand_off`` defaults to
        22:34 and must be a clock time, not a sun anchor.
        """
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
        """Parse one entry of the ``bias.lights:`` mapping (keyed by light name).

        The ``look`` must carry an explicit ``brightness`` (0-100) and a static
        colour — ``circadian`` is rejected because a hold has to be a fixed
        target. ``idle`` must be ``circadian`` or ``off``; a YAML-1.1 bare
        ``off`` (parsed as ``False``) is accepted as the string ``"off"``.
        """
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
        """Parse the ``circadian_daemon.bias:`` block.

        Requires a non-empty ``lights`` mapping; the ``triggers`` sub-blocks
        (``sse``, ``control_file``, ``probe``) are all optional. Validates
        ``probe.mode`` is ``tcp`` or ``icmp`` and parses the various durations.
        """
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
        """Parse the ``circadian_daemon:`` block.

        Only ``zone`` is required; everything else has a live-tested default.
        ``hand_off`` must be a clock time (not a sun anchor). Durations under
        ``manual_override``/``retry`` and the top level are parsed to ms, and
        an optional ``bias`` block is delegated to :meth:`BiasSpec.parse`.
        """
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
    def parse(cls, d: dict[str, Any], ctx: str) -> "SceneSpec":
        """Parse one entry of the ``scenes:`` list.

        Requires ``name``, ``zone`` and a non-empty ``lights`` mapping of light
        name to state. Rejects ``circadian`` colours — a bridge scene is a
        static look and cannot track the curve.
        """
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
        """Parse the ``security:`` block.

        Requires at least one entry in ``groups`` (a bare string is accepted).
        Validates the alert look (colour hex, brightness 0-100, ``breathe_hz``
        in (0, 2]) and the chaos safety caps: ``frame_interval`` >= 50 ms,
        ``min_flash_interval`` >= 334 ms (the photosensitive-seizure floor),
        and ``max_duration`` longer than the alert phase.
        """
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


def _clock_minute(value: Any, ctx: str) -> int:
    """Parse a strict ``HH:MM`` clock time to minutes after midnight.

    Unlike :func:`parse_time_ref` consumers that accept sun anchors, callers
    of this helper need a fixed wall-clock minute; ``sunrise``/``sunset`` are
    rejected with a :class:`ConfigError` naming ``ctx``.
    """
    ref = parse_time_ref(value, ctx=ctx)
    if ref in ("sunrise", "sunset"):
        raise ConfigError(f"{ctx} must be a clock time like '22:45'")
    hh, mm = ref.split(":")
    return int(hh) * 60 + int(mm)


def _dur_min(value: Any, ctx: str) -> int:
    """Parse a duration (``"90m"``, ``"2h"``) to whole minutes."""
    return parse_duration(value, ctx=ctx) // 60_000


@dataclass(frozen=True)
class RhythmSignals:
    """External phone-derived signal files for the rhythm engine.

    Both are optional; the engine degrades to learned/default anchors when a
    file is unset or absent. Files live on a shared mount written by an
    external automation (e.g. Home Assistant), mirroring the TV bias
    control-file channel.

    Attributes:
        next_alarm_file: Path to a file whose content is the epoch-seconds
            timestamp of the phone's next alarm (empty/``0`` = no alarm set).
            Re-read every tick; never consumed.
        charging_file: Path whose *existence* means the phone is charging.
            Never consumed.
    """

    next_alarm_file: str | None = None
    charging_file: str | None = None

    @classmethod
    def parse(cls, value: Any, ctx: str) -> "RhythmSignals":
        """Parse the ``rhythm.signals`` block (both paths optional)."""
        d = _as_dict(value, ctx)
        alarm = d.get("next_alarm_file")
        charging = d.get("charging_file")
        return cls(
            next_alarm_file=str(alarm) if alarm else None,
            charging_file=str(charging) if charging else None,
        )


@dataclass(frozen=True)
class RhythmPresence:
    """Presence-inference tunables (pet discounting and wake confirmation).

    Attributes:
        quiet_min: House-wide human quiet (minutes) required before the
            sleep-onset vote can pass.
        wake_confirm_events: Motion events within the confirm window needed
            to call a wake "sustained" (a single pet blip cannot fake it).
        wake_confirm_window_min: The sliding window (minutes) for the two
            attributes above and for ``recent_rooms``.
        pet_progression_min: Motion in one room counts as human when a
            *different* room was active within this many minutes
            (room-to-room progression); solo single-room motion is
            discounted as a pet.
    """

    quiet_min: int = 30
    wake_confirm_events: int = 3
    wake_confirm_window_min: int = 10
    pet_progression_min: int = 5

    @classmethod
    def parse(cls, value: Any, ctx: str) -> "RhythmPresence":
        """Parse the ``rhythm.presence`` block (all fields defaulted)."""
        d = _as_dict(value, ctx)
        return cls(
            quiet_min=_dur_min(d.get("quiet", "30m"), f"{ctx}.quiet"),
            wake_confirm_events=int(d.get("wake_confirm_events", 3)),
            wake_confirm_window_min=_dur_min(
                d.get("wake_confirm_window", "10m"), f"{ctx}.wake_confirm_window"),
            pet_progression_min=_dur_min(
                d.get("pet_progression", "5m"), f"{ctx}.pet_progression"),
        )


@dataclass(frozen=True)
class RhythmSpec:
    """The rhythm engine: closed-loop day-phase inference (and, in later
    stages, shepherding actuation).

    Stage 1 ships ``stage: "observe"`` only — the engine infers and logs but
    never writes to the bridge. See the design spec (ops repo,
    ``docs/superpowers/specs/2026-07-04-rhythm-engine-design.md``).

    Attributes:
        stage: Rollout stage; only ``"observe"`` is implemented (parse accepts
            the future ``"mornings"``/``"full"`` so a config can be staged, but
            the daemon refuses to start them until those stages ship).
        bedroom: Room name whose motion area anchors sleep/wake inference.
        bed_target_min: Chosen bed-time target, minutes after midnight.
        wake_default_min: Fallback wake anchor when no alarm and no history.
        weekend_drift_cap_min: Max minutes weekend anchors may lag weekday's.
        wind_down_lead_min: Wind-down phase starts this long before bed anchor.
        dawn_lead_min: Dawn phase starts this long before the wake anchor.
        dawn_max_advance_min: Cap on snooze-through compensation (unused in
            observe; parsed now so staged configs validate).
        morning_min: Duration of the ``morning`` phase after wake.
        state_file: JSON file for learned anchors + the live snapshot.
        signals: External phone signal files.
        presence: Presence-inference tunables.
    """

    bedroom: str
    stage: str = "observe"
    bed_target_min: int = 23 * 60
    wake_default_min: int = 7 * 60
    weekend_drift_cap_min: int = 90
    wind_down_lead_min: int = 90
    dawn_lead_min: int = 25
    dawn_max_advance_min: int = 20
    morning_min: int = 60
    state_file: str = "rhythm-state.json"
    signals: RhythmSignals = RhythmSignals()
    presence: RhythmPresence = RhythmPresence()

    @classmethod
    def parse(cls, value: Any, ctx: str = "rhythm") -> "RhythmSpec":
        """Parse the ``rhythm:`` block. Only ``bedroom`` is required."""
        d = _as_dict(value, ctx)
        stage = str(d.get("stage", "observe"))
        if stage not in ("observe", "mornings", "full"):
            raise ConfigError(
                f"{ctx}.stage must be 'observe', 'mornings' or 'full', got {stage!r}")
        bedroom = d.get("bedroom")
        if not bedroom or not isinstance(bedroom, str):
            raise ConfigError(f"{ctx}.bedroom is required (the room name whose "
                              "motion area anchors sleep/wake inference)")
        return cls(
            bedroom=bedroom,
            stage=stage,
            bed_target_min=_clock_minute(d.get("bed_target", "23:00"), f"{ctx}.bed_target"),
            wake_default_min=_clock_minute(d.get("wake_default", "07:00"), f"{ctx}.wake_default"),
            weekend_drift_cap_min=_dur_min(d.get("weekend_drift_cap", "90m"),
                                           f"{ctx}.weekend_drift_cap"),
            wind_down_lead_min=_dur_min(d.get("wind_down_lead", "90m"), f"{ctx}.wind_down_lead"),
            dawn_lead_min=_dur_min(d.get("dawn_lead", "25m"), f"{ctx}.dawn_lead"),
            dawn_max_advance_min=_dur_min(d.get("dawn_max_advance", "20m"),
                                          f"{ctx}.dawn_max_advance"),
            morning_min=_dur_min(d.get("morning", "60m"), f"{ctx}.morning"),
            state_file=str(d.get("state_file", "rhythm-state.json")),
            signals=RhythmSignals.parse(d.get("signals", {}), f"{ctx}.signals"),
            presence=RhythmPresence.parse(d.get("presence", {}), f"{ctx}.presence"),
        )


@dataclass(frozen=True)
class Config:
    """The fully validated desired state — the root of the config tree.

    One instance corresponds to one YAML document; every feature block is
    already validated, so downstream code (plan/apply/daemon) never re-checks
    shapes. Optional blocks are ``None``/empty when absent from the file.

    Attributes:
        require_all_lights_assigned: When true, ``plan`` emits a blocked change
            for every bridge light not assigned to any declared room.
        marker: Metadata tag identifying managed resources (see ``DEFAULT_MARKER``).
    """

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
    rhythm: RhythmSpec | None = None
    require_all_lights_assigned: bool = True
    marker: str = DEFAULT_MARKER

    @classmethod
    def parse(cls, doc: dict[str, Any]) -> "Config":
        """Parse a whole YAML document into a :class:`Config`.

        Requires the ``bridge`` and ``location`` blocks; everything else is
        optional. Beyond delegating each block to its own parser, this enforces
        the cross-cutting invariants: unique names within ``motion_policies``/
        ``areas``/``smart_scenes``/``scenes`` and single-room membership per
        light. Raises :class:`ConfigError` on the first violation.
        """
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
        rhythm = (
            RhythmSpec.parse(doc["rhythm"], "rhythm") if "rhythm" in doc else None
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
            rhythm=rhythm,
            require_all_lights_assigned=bool(doc.get("require_all_lights_assigned", True)),
            marker=str(doc.get("marker", DEFAULT_MARKER)),
        )


def _parse_circadian(d: dict[str, Any]) -> CircadianParams:
    """Parse the ``circadian:`` block as overrides on the default curve params.

    Unknown keys are ignored except ``night_start``, which is rejected loudly
    (it used to parse but do nothing). Range violations surface as
    :class:`ConfigError` via ``CircadianParams``' own ``__post_init__`` checks.
    """
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


def _parse_areas(d: dict[str, Any]) -> tuple[Area, ...]:
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
    """Raise :class:`ConfigError` if any name repeats within its section."""
    seen: set[str] = set()
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
