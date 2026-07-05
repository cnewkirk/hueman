"""Per-room activity judging for the rhythm engine (pet discounting).

The bridge's MotionAware areas report *presence*, not *identity*; in a
one-human-plus-pets household the discriminator is movement *pattern*:

* a light being changed is always a human (pets do not use switches);
* motion is human when a *different* room was active within the progression
  window (room-to-room movement) or a light change happened nearby;
* solo single-room motion is discounted as a pet.

Known blind spot, accepted by design: a human sitting nearly still in one
room for a long time degrades to "pet" judgments — but the sleep-onset vote
that consumes this signal also requires lights-out and TV-off, so a reader
with the lamp on is never mistaken for an empty house.

Every judgment carries the rule that produced it so the daemon can log an
evidence trail (see the design spec's "explainable and measured" rule).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .config import RhythmPresence


@dataclass(frozen=True)
class ActivityEvent:
    """One activity observation.

    Attributes:
        room: Room name; ``""`` when unattributable (e.g. a manual override
            detected on the driven zone).
        kind: ``"motion"`` (a motion-area fired) or ``"light_change"`` (a
            human manipulated lighting).
        ts: Epoch seconds.
    """

    room: str
    kind: str
    ts: float


@dataclass(frozen=True)
class Judgment:
    """The tracker's verdict on one event, with the rule that decided it.

    Attributes:
        event: The judged event.
        human: Whether the event counts as human activity.
        rule: ``"light-change"``, ``"progression"``, ``"light-change-context"``,
            or ``"solo-motion"`` (the discount).
    """

    event: ActivityEvent
    human: bool
    rule: str


@dataclass(frozen=True)
class PresenceSummary:
    """Aggregated view the rhythm engine consumes each tick.

    Attributes:
        quiet_s: Seconds since the last *human-judged* activity (pet blips do
            not reset it). Very large when nothing human was ever seen.
        last_active_room: Room of the most recent human activity, if any.
        recent_rooms: Distinct rooms with motion inside the confirm window.
        recent_motion_count: Motion events inside the confirm window
            (human- and pet-judged alike; wake confirmation applies its own
            bedroom/light-change requirement on top).
        recent_light_change: Whether a light change occurred in the window.
    """

    quiet_s: float
    last_active_room: str | None
    recent_rooms: tuple[str, ...]
    recent_motion_count: int
    recent_light_change: bool


class PresenceTracker:
    """Judges activity events and answers "how quiet has the house been?".

    Args:
        spec: Presence tunables (windows and thresholds).
    """

    #: Retain at most this many recent events; windows are minutes long, so
    #: this is generous while keeping memory flat over months of uptime.
    _MAX_EVENTS = 512

    def __init__(self, spec: RhythmPresence) -> None:
        """Start with an empty event history and no human activity seen."""
        self._spec = spec
        self._events: deque[ActivityEvent] = deque(maxlen=self._MAX_EVENTS)
        self._last_human_ts: float | None = None
        self._last_human_room: str | None = None

    def feed(self, event: ActivityEvent) -> Judgment:
        """Judge one event, record it, and return the verdict with its rule."""
        judgment = self._judge(event)
        self._events.append(event)
        if judgment.human:
            self._last_human_ts = event.ts
            if event.room:
                self._last_human_room = event.room
        return judgment

    def _judge(self, event: ActivityEvent) -> Judgment:
        """Apply the pet-discounting rules (see the module docstring)."""
        if event.kind == "light_change":
            return Judgment(event, human=True, rule="light-change")
        window_start = event.ts - self._spec.pet_progression_min * 60.0
        for prior in reversed(self._events):
            if prior.ts < window_start:
                break
            if prior.kind == "light_change":
                return Judgment(event, human=True, rule="light-change-context")
            if prior.kind == "motion" and prior.room and prior.room != event.room:
                return Judgment(event, human=True, rule="progression")
        return Judgment(event, human=False, rule="solo-motion")

    def summary(self, now: float) -> PresenceSummary:
        """Aggregate the confirm-window state for the engine's tick."""
        window_start = now - self._spec.wake_confirm_window_min * 60.0
        rooms: list[str] = []
        motion_count = 0
        light_change = False
        for ev in self._events:
            if ev.ts < window_start:
                continue
            if ev.kind == "motion":
                motion_count += 1
                if ev.room and ev.room not in rooms:
                    rooms.append(ev.room)
            elif ev.kind == "light_change":
                light_change = True
        quiet_s = (now - self._last_human_ts) if self._last_human_ts is not None else 1e9
        return PresenceSummary(
            quiet_s=quiet_s,
            last_active_room=self._last_human_room,
            recent_rooms=tuple(rooms),
            recent_motion_count=motion_count,
            recent_light_change=light_change,
        )
