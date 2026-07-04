"""Pure helpers for the night-time soft-red motion guidance.

Kept I/O-free and unit-tested: building the CLIP ``scene`` bodies for the
guidelight zone, and transforming an existing MotionAware ``behavior_instance``
config so every timeslot targets the whole-apartment zone, with the night
timeslot rewritten to recall the soft-red scene and switch off quickly.

The reconciler in :mod:`hueman.reconcile` owns the bridge I/O; this module only
produces request bodies from inputs.
"""

from __future__ import annotations

import copy

from .payload import ColorConverter


def scene_body(
    name: str,
    group_rid: str,
    light_rids: list[str],
    *,
    mirek: int | None = None,
    hex: str | None = None,
    brightness: float = 0.0,
    on: bool = True,
    group_rtype: str = "zone",
) -> dict:
    """Build a CLIP ``scene`` body for ``group_rid`` setting every light alike.

    When ``on`` is true, exactly one of ``mirek`` (white colour temperature) or
    ``hex`` (an sRGB colour, converted to CIE xy) selects the colour and
    ``brightness`` is a percent. When ``on`` is false the scene turns every light
    off (an explicit ``{"on": {"on": False}}`` action); ``brightness``/colour are
    ignored. The off form is how the circadian day cycle hands off to
    ``night_motion`` at bedtime without leaving a competing on-step in the night
    window.
    """
    if hex is not None:
        x, y = ColorConverter.hex_to_xy(hex)
        colour = {"color": {"xy": {"x": x, "y": y}}}
    elif mirek is not None:
        colour = {"color_temperature": {"mirek": int(mirek)}}
    else:
        colour = {}
    actions = []
    for rid in light_rids:
        if on:
            action = {"on": {"on": True}, "dimming": {"brightness": round(float(brightness), 1)}}
            action.update(colour)
        else:
            action = {"on": {"on": False}}
        actions.append({"target": {"rid": rid, "rtype": "light"}, "action": action})
    return {
        "metadata": {"name": name},
        "group": {"rid": group_rid, "rtype": group_rtype},
        "actions": actions,
    }


def _colour_kind(action: dict) -> tuple:
    """Return a comparable colour signature for a scene action.

    ``("xy", x, y)`` for an sRGB/CIE colour, ``("ct", mirek)`` for a white
    colour temperature, or ``("none",)`` when neither is set. Defensive: a
    ``color`` block without ``xy`` (e.g. a gradient-only light) reads as none
    rather than raising.
    """
    color = action.get("color")
    if isinstance(color, dict) and isinstance(color.get("xy"), dict):
        xy = color["xy"]
        return ("xy", float(xy.get("x", 0.0)), float(xy.get("y", 0.0)))
    ct = action.get("color_temperature")
    if isinstance(ct, dict) and "mirek" in ct:
        return ("ct", int(ct["mirek"]))
    return ("none",)


def scene_actions_match(
    live_actions: list[dict],
    desired_actions: list[dict],
    *,
    bri_tol: float = 0.5,
    xy_tol: float = 1e-3,
) -> bool:
    """Return ``True`` if two scene action lists describe the same look.

    Matched by ``target.rid`` (order-insensitive) and tolerant of the bridge's
    own numeric rounding: ``on`` exact, ``dimming.brightness`` within ``bri_tol``
    percent, ``color_temperature.mirek`` exact, ``color.xy`` within ``xy_tol`` per
    coordinate, and the colour *kind* (xy vs ct vs none) must agree. Used to
    detect when a zone scene's stored look has drifted from the desired config.
    Defensive throughout so an unexpected action shape returns a mismatch rather
    than raising inside a planner.
    """
    live = {a.get("target", {}).get("rid"): a.get("action", {}) for a in live_actions}
    desired = {a.get("target", {}).get("rid"): a.get("action", {}) for a in desired_actions}
    if live.keys() != desired.keys():
        return False
    for rid, da in desired.items():
        la = live[rid]
        if bool(la.get("on", {}).get("on")) != bool(da.get("on", {}).get("on")):
            return False
        if not bool(da.get("on", {}).get("on")):
            continue  # an off light's brightness/colour is irrelevant — match on on-state alone
        lb = la.get("dimming", {}).get("brightness")
        db = da.get("dimming", {}).get("brightness")
        if (lb is None) != (db is None):
            return False
        if lb is not None and abs(float(lb) - float(db)) > bri_tol:
            return False
        live_colour, desired_colour = _colour_kind(la), _colour_kind(da)
        if live_colour[0] != desired_colour[0]:
            return False
        if live_colour[0] == "ct" and live_colour[1] != desired_colour[1]:
            return False
        if live_colour[0] == "xy" and (
            abs(live_colour[1] - desired_colour[1]) > xy_tol
            or abs(live_colour[2] - desired_colour[2]) > xy_tol
        ):
            return False
    return True


def _start_minute(slot: dict) -> int:
    """Clock minute of a timeslot's start; raises ValueError for sun-anchored slots.

    MotionAware timeslots may be anchored to sunrise/sunset instead of a clock
    time (no ``"time"`` mapping). Those cannot be ordered or re-timed here, so
    raise a typed, descriptive error the planner can turn into a BLOCKED change —
    never a bare ``KeyError`` out of ``plan``.
    """
    start = slot.get("start_time") or {}
    t = start.get("time")
    if not isinstance(t, dict):
        raise ValueError(
            f"sun-anchored timeslot (start_time type {start.get('type')!r}) is not "
            "supported; re-anchor it to a clock time in the Hue app first"
        )
    return t["hour"] * 60 + t.get("minute", 0)


def transform_automation(
    config: dict,
    *,
    zone_rid: str,
    day_scene: str,
    evening_scene: str,
    night_scene: str,
    night_start: tuple[int, int] = (22, 34),
    night_off_min: int = 3,
    day_start: tuple[int, int] = (8, 0),
    night_only: bool = False,
) -> dict:
    """Return ``config`` retargeted to a single zone with a red night timeslot.

    A MotionAware automation's response target (``where``) is global across all
    its timeslots, so every timeslot is pointed at the one guidelight zone and
    each recalls a single zone scene. The latest-starting timeslot is treated as
    "night": its start becomes ``night_start``, it recalls ``night_scene``, and it
    switches everything off after ``night_off_min`` minutes. The earlier
    timeslots keep their start times and any auto-off, retargeted to the day /
    evening zone scenes. The daylight gate, motion service and source are
    preserved. The input is not mutated.

    When ``night_only`` is ``True``, the day and evening timeslots are dropped and
    three timeslots are emitted in chronological order: a 00:00 clone of the night
    slot, a **no-op** slot at ``day_start`` (a single ``"do_nothing"`` action — the
    bridge requires ``on_motion`` with at least one action on every slot — and no
    ``on_no_motion``), and the night slot at ``night_start``. A single timeslot does NOT wrap past
    midnight on the bridge, so the 00:00 clone carries the small hours; the no-op
    slot bounds it so ``day_start`` -> ``night_start`` does nothing even when dark
    (without it the 00:00 slot governs the whole day and dark evenings before the
    hand-off get red recalls and all_off). Night slots recall ``night_scene``, gated
    to darkness by the preserved daylight gate. This mode is used when a circadian
    daemon owns the day cycle and the automation should only act at night.

    The no-op slot is only valid in ``night_only`` mode: feeding a config that
    already contains one (missing/empty ``on_motion.recall_single`` or a lone
    ``"do_nothing"`` action) into full mode (``night_only=False``) raises
    ``ValueError`` rather than silently retargeting the no-op or crashing. ``day_start``
    must lie strictly between midnight and ``night_start`` so the emitted timeslots
    stay chronological; a direct caller violating that also raises ``ValueError``.
    """
    cfg = copy.deepcopy(config)
    motion = cfg["motion"]
    motion["where"] = [{"group": {"rid": zone_rid, "rtype": "zone"}}]

    slots = motion["when"]["timeslots"]
    night_slot = max(slots, key=_start_minute)
    earlier = sorted((s for s in slots if s is not night_slot), key=_start_minute)
    role: dict[int, str] = {id(night_slot): night_scene}
    if earlier:
        role[id(earlier[0])] = day_scene            # earliest start = day
        for s in earlier[1:]:
            role[id(s)] = evening_scene             # any later ones = evening

    if night_only:
        if not ((0, 0) < day_start < night_start):
            raise ValueError(
                "day_start must be strictly between 00:00 and night_start so the "
                "timeslots stay chronological"
            )
        motion["when"]["timeslots"] = [night_slot]
        slots = motion["when"]["timeslots"]

    def recall(scene_rid: str) -> list[dict]:
        return [{"action": {"recall": {"rid": scene_rid, "rtype": "scene"}}}]

    for s in slots:
        actions = s.get("on_motion", {}).get("recall_single") or []
        if not actions or actions == [{"action": "do_nothing"}]:
            raise ValueError(
                "actionless timeslot (a night_only no-op) is not supported in full "
                "mode — remove it in the Hue app or keep mode: night_only"
            )
        s["on_motion"]["recall_single"] = recall(role[id(s)])
        if "on_no_motion" in s:
            s["on_no_motion"]["recall_single"] = [{"action": "all_off"}]

    night_slot["start_time"] = {"time": {"hour": night_start[0], "minute": night_start[1]}, "type": "time"}
    on_no = night_slot.setdefault("on_no_motion", {})
    on_no["after"] = {"minutes": night_off_min}
    on_no["recall_single"] = [{"action": "all_off"}]

    if night_only:
        # A lone timeslot does not wrap past midnight on the bridge (it only covers
        # night_start -> 00:00), so the red guidance needs a 00:00 clone for the small
        # hours. But with only those two slots the 00:00 slot would govern
        # 00:00 -> night_start — i.e. red recalls + all_off in dark evenings before
        # the hand-off (bit us nightly, 2026-07-03). An actionless day_start slot
        # bounds it: night coverage is night_start -> day_start, and
        # day_start -> night_start does nothing even when dark.
        midnight_slot = copy.deepcopy(night_slot)
        midnight_slot["start_time"] = {"time": {"hour": 0, "minute": 0}, "type": "time"}
        noop_slot = {
            "start_time": {
                "time": {"hour": day_start[0], "minute": day_start[1]},
                "type": "time",
            },
            # The bridge schema REQUIRES on_motion with >=1 action on every
            # timeslot (a slot without it, or with an empty recall list, is
            # rejected — probed live 2026-07-03). "do_nothing" is the schema's
            # explicit no-op action (the Hue app's "Do nothing") and round-trips
            # verbatim on GET, which the reconciler's exact-== NOOP check needs.
            "on_motion": {"recall_single": [{"action": "do_nothing"}]},
        }
        motion["when"]["timeslots"] = [midnight_slot, noop_slot, night_slot]
    return cfg
