"""Day-phase inference for the rhythm engine (pure logic, no I/O).

Stage 1 (observe): given presence summaries and external signals, decide the
current day phase, detect sleep onset and wake, and maintain *learned anchors*
(recent observed wake / sleep-onset minutes per weekday/weekend class). The
engine never performs I/O; the daemon supplies inputs and persists the store.

Every decision carries a reason and an evidence mapping so the daemon can log
an auditable trail — unexplainable automation in a home is indistinguishable
from malfunction.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from .config import RhythmSpec
from .presence import PresenceSummary

#: Newest observations kept per (kind, day-class); two weeks of weekdays.
_MAX_SAMPLES = 14


class AnchorStore:
    """Recent observed wake / sleep-onset minutes, per weekday/weekend class.

    One sample per calendar date (a re-observation replaces the earlier one);
    at most 14 newest per (kind, class). Values are minutes after midnight.
    Serialisable to a small JSON document the daemon persists; a corrupted or
    missing document silently yields an empty store (learning restarts, the
    engine falls back to config defaults — see the safety rails in the spec).
    """

    def __init__(self) -> None:
        """Start empty; populate via :meth:`record` or :meth:`from_json`."""
        # {day_class: {kind: [{"date": iso, "minute": int}, ...]}}
        self._samples: dict[str, dict[str, list[dict[str, Any]]]] = {}

    def record(self, kind: str, day_class: str, minute: int, date_iso: str) -> None:
        """Record one observation, replacing the same date, keeping newest 14."""
        bucket = self._samples.setdefault(day_class, {}).setdefault(kind, [])
        bucket[:] = [s for s in bucket if s["date"] != date_iso]
        bucket.append({"date": date_iso, "minute": int(minute)})
        bucket.sort(key=lambda s: str(s["date"]))
        del bucket[:-_MAX_SAMPLES]

    def median(self, kind: str, day_class: str) -> int | None:
        """Median observed minute for the pair, or ``None`` with no samples.

        The midpoint of an even sample count rounds to the nearest minute.
        """
        bucket = self._samples.get(day_class, {}).get(kind, [])
        if not bucket:
            return None
        minutes: list[int] = [int(s["minute"]) for s in bucket]
        return round(median(minutes))

    def to_json(self) -> dict[str, Any]:
        """Serialise to the persisted document shape."""
        return {"anchors": self._samples}

    @classmethod
    def from_json(cls, doc: dict[str, Any]) -> AnchorStore:
        """Rebuild from :meth:`to_json` output; malformed input yields empty."""
        store = cls()
        anchors = doc.get("anchors")
        if not isinstance(anchors, dict):
            return store
        for day_class, kinds in anchors.items():
            if not isinstance(kinds, dict):
                continue
            for kind, samples in kinds.items():
                if not isinstance(samples, list):
                    continue
                for s in samples:
                    if (isinstance(s, dict)
                            and isinstance(s.get("date"), str)
                            and isinstance(s.get("minute"), int)):
                        store.record(str(kind), str(day_class), s["minute"], s["date"])
        return store


@dataclass(frozen=True)
class SignalState:
    """External signals the daemon hands the engine on each tick.

    Attributes:
        next_alarm_epoch: Epoch seconds of the phone's next alarm; ``None``
            when unknown/none set.
        phone_charging: Whether the phone is on the charger (bedtime proxy).
        tv_on: Committed TV state from the bias aggregator (``False`` when
            bias is not configured).
        zone_on: Whether the daemon last commanded its driven zone on — a
            lights-out proxy for the sleep vote.
        sunset_min: Today's sunset as minutes after local midnight (evening
            phase anchor); the daemon computes it from the solar calculator.
    """

    next_alarm_epoch: float | None
    phone_charging: bool
    tv_on: bool
    zone_on: bool
    sunset_min: float


@dataclass(frozen=True)
class PhaseDecision:
    """One tick's verdict: the phase, whether it changed, and the evidence.

    Attributes:
        phase: The (possibly unchanged) current phase constant.
        changed: True when this tick moved to a new phase.
        reason: Short machine-greppable cause, e.g. ``"sleep-vote"``.
        evidence: JSON-serialisable inputs behind the decision (anchors,
            presence numbers, signal states) — the log's audit trail.
    """

    phase: str
    changed: bool
    reason: str
    evidence: dict[str, Any]


def _noon_shifted(minute: float) -> float:
    """Map minute-of-day onto a noon-origin axis so evening>night comparisons
    survive midnight (23:30 -> 690, 00:30 -> 750, 11:59 -> 1439)."""
    return (minute - 720.0) % 1440.0


class RhythmEngine:
    """Anchored, negotiated day-phase state machine (observe stage).

    Phases move forward only (one step per tick) through
    ``dawn → morning → daylight → evening → wind_down → night → sleep`` with
    two event-driven exceptions: the sleep vote can end an evening early
    (``wind_down``/``night`` → ``sleep``) and confirmed wake evidence ends
    ``dawn``/``sleep`` (→ ``morning``). All thresholds come from the spec;
    learned anchors refine them but never move outside spec bounds.

    Args:
        spec: The parsed ``rhythm:`` block.
        store: Learned anchors (the daemon owns persistence).
        tz: IANA timezone for wall-clock and day-class decisions.
    """

    DAWN = "dawn"
    MORNING = "morning"
    DAYLIGHT = "daylight"
    EVENING = "evening"
    WIND_DOWN = "wind_down"
    NIGHT = "night"
    SLEEP = "sleep"

    def __init__(self, spec: RhythmSpec, store: AnchorStore, *, tz: str) -> None:
        """Wire the spec and store; phase is decided on the first tick."""
        self._spec = spec
        self._store = store
        self._tz = ZoneInfo(tz)
        self._phase: str | None = None
        self._morning_started_min: float | None = None
        self._last_evidence: dict[str, Any] = {}

    @property
    def phase(self) -> str:
        """The current phase (``daylight`` before the first tick)."""
        return self._phase or self.DAYLIGHT

    @property
    def store(self) -> AnchorStore:
        """The learned-anchor store (the daemon persists it)."""
        return self._store

    # -- clock helpers ---------------------------------------------------- #
    def _local(self, now: float) -> _dt.datetime:
        """Local wall-clock datetime for epoch ``now``."""
        return _dt.datetime.fromtimestamp(now, tz=self._tz)

    @staticmethod
    def _minute(local: _dt.datetime) -> float:
        """Minutes after local midnight."""
        return local.hour * 60.0 + local.minute + local.second / 60.0

    def _day_class(self, local: _dt.datetime) -> str:
        """``"weekend"`` for Sat/Sun mornings, else ``"weekday"``.

        A *night* is classed by the morning it ends: an onset observed before
        04:00 belongs to that same date's class, a late-evening onset to the
        next day's.
        """
        date = local.date() if local.hour < 4 else local.date() + _dt.timedelta(days=1)
        return "weekend" if date.weekday() >= 5 else "weekday"

    def _wake_day_class(self, local: _dt.datetime) -> str:
        """Day class of a wake observation (the date it happened)."""
        return "weekend" if local.weekday() >= 5 else "weekday"

    # -- anchors ----------------------------------------------------------- #
    def _wake_anchor_min(self, now: float, signals: SignalState) -> tuple[float, str]:
        """Tomorrow-or-today's wake anchor minute and its source label.

        Preference order: a real alarm within the next 18 h; the learned
        median for the applicable day class (weekend capped at the weekday
        anchor + drift cap); the config default.
        """
        if signals.next_alarm_epoch is not None and 0 < signals.next_alarm_epoch - now <= 18 * 3600:
            alarm_local = self._local(signals.next_alarm_epoch)
            return self._minute(alarm_local), "alarm"
        local = self._local(now)
        day_class = self._day_class(local)
        learned = self._store.median("wake", day_class)
        if learned is not None:
            if day_class == "weekend":
                weekday_base = self._store.median("wake", "weekday")
                base = float(weekday_base if weekday_base is not None
                             else self._spec.wake_default_min)
                learned = int(min(learned, base + self._spec.weekend_drift_cap_min))
            return float(learned), f"learned-{day_class}"
        return float(self._spec.wake_default_min), "default"

    # -- votes ------------------------------------------------------------- #
    def _sleep_vote(self, presence: PresenceSummary, signals: SignalState) -> tuple[bool, dict[str, Any]]:
        """The sleep-onset confidence vote and its evidence."""
        checks = {
            "quiet": presence.quiet_s >= self._spec.presence.quiet_min * 60.0,
            "tv_off": not signals.tv_on,
            "zone_off": not signals.zone_on,
            "bedroom_or_charging": (
                presence.last_active_room == self._spec.bedroom or signals.phone_charging),
        }
        return all(checks.values()), {"sleep_vote": checks, "quiet_s": round(presence.quiet_s)}

    def _wake_evidence(self, presence: PresenceSummary) -> tuple[bool, dict[str, Any]]:
        """Sustained-wake test: enough motion AND (bedroom involved OR lights).

        The bedroom/light requirement is pet rule 2 — a multi-room cat patrol
        that never enters the bedroom and touches no light is not a wake.
        """
        sustained = (
            presence.recent_motion_count >= self._spec.presence.wake_confirm_events
            or len(presence.recent_rooms) >= 2
        )
        anchored = (self._spec.bedroom in presence.recent_rooms
                    or presence.recent_light_change)
        return sustained and anchored, {
            "motion_count": presence.recent_motion_count,
            "rooms": list(presence.recent_rooms),
            "light_change": presence.recent_light_change,
            "bedroom_involved": self._spec.bedroom in presence.recent_rooms,
        }

    # -- tick ---------------------------------------------------------------- #
    def tick(self, now: float, presence: PresenceSummary, signals: SignalState) -> PhaseDecision:
        """Advance at most one phase; return the decision with evidence."""
        local = self._local(now)
        minute = self._minute(local)
        wake_anchor, wake_src = self._wake_anchor_min(now, signals)
        bed_anchor = float(self._spec.bed_target_min)
        evidence: dict[str, Any] = {
            "minute": round(minute), "wake_anchor_min": int(wake_anchor),
            "wake_anchor_src": wake_src, "bed_anchor_min": int(bed_anchor),
            "sunset_min": round(signals.sunset_min),
        }
        prev = self._phase
        phase, reason = self._decide(minute, now, local, wake_anchor, bed_anchor,
                                     presence, signals, evidence)
        changed = phase != prev
        self._phase = phase
        if changed:
            self._last_evidence = dict(evidence)
        return PhaseDecision(phase=phase, changed=changed, reason=reason, evidence=evidence)

    def _decide(
        self, minute: float, now: float, local: _dt.datetime,
        wake_anchor: float, bed_anchor: float,
        presence: PresenceSummary, signals: SignalState, evidence: dict[str, Any],
    ) -> tuple[str, str]:
        """The transition table; mutates ``evidence`` with vote details."""
        spec = self._spec
        wind_down_start = _noon_shifted(bed_anchor - spec.wind_down_lead_min)
        night_start = _noon_shifted(bed_anchor)
        m_shift = _noon_shifted(minute)

        if self._phase is None:  # first tick: seed from wall clock
            if m_shift >= night_start:
                return self.NIGHT, "seed"
            if m_shift >= wind_down_start:
                return self.WIND_DOWN, "seed"
            if m_shift >= _noon_shifted(signals.sunset_min):
                return self.EVENING, "seed"
            if minute < wake_anchor:
                return self.NIGHT, "seed"          # pre-dawn restart: assume night
            return self.DAYLIGHT, "seed"

        if self._phase == self.SLEEP:
            woke, wake_ev = self._wake_evidence(presence)
            evidence.update(wake_ev)
            in_dawn_window = wake_anchor - spec.dawn_lead_min <= minute < wake_anchor
            if woke:
                self._record_wake(local, minute)
                self._morning_started_min = minute
                return self.MORNING, "wake-detected"
            if in_dawn_window:
                return self.DAWN, "dawn-window"
            return self.SLEEP, "hold"

        if self._phase == self.DAWN:
            woke, wake_ev = self._wake_evidence(presence)
            evidence.update(wake_ev)
            if woke:
                self._record_wake(local, minute)
                self._morning_started_min = minute
                return self.MORNING, "wake-detected"
            if minute >= wake_anchor + 120:  # missed detection failsafe
                self._morning_started_min = minute
                return self.MORNING, "wake-assumed-late"
            if minute >= wake_anchor:
                evidence["snoozed_through"] = True   # alarm passed, no motion yet
            return self.DAWN, "hold"

        if self._phase == self.MORNING:
            started = self._morning_started_min if self._morning_started_min is not None else minute
            if minute >= started + spec.morning_min:
                return self.DAYLIGHT, "morning-elapsed"
            return self.MORNING, "hold"

        if self._phase == self.DAYLIGHT:
            if m_shift >= wind_down_start:
                return self.WIND_DOWN, "wind-down-lead"
            if m_shift >= _noon_shifted(signals.sunset_min):
                return self.EVENING, "sunset"
            return self.DAYLIGHT, "hold"

        if self._phase == self.EVENING:
            if m_shift >= wind_down_start:
                return self.WIND_DOWN, "wind-down-lead"
            return self.EVENING, "hold"

        if self._phase in (self.WIND_DOWN, self.NIGHT):
            asleep, vote_ev = self._sleep_vote(presence, signals)
            evidence.update(vote_ev)
            if asleep:
                self._record_sleep_onset(local, minute)
                return self.SLEEP, "sleep-vote"
            if self._phase == self.WIND_DOWN and m_shift >= night_start:
                return self.NIGHT, "bed-anchor"
            return self._phase, "hold"

        return self.DAYLIGHT, "reset"  # unreachable; defensive

    # -- records ------------------------------------------------------------- #
    def _record_wake(self, local: _dt.datetime, minute: float) -> None:
        """Log today's observed wake into the learned store."""
        self._store.record("wake", self._wake_day_class(local), int(minute),
                           local.date().isoformat())

    def _record_sleep_onset(self, local: _dt.datetime, minute: float) -> None:
        """Log tonight's observed sleep onset (classed by the morning it ends)."""
        self._store.record("sleep_onset", self._day_class(local), int(minute),
                           local.date().isoformat())

    def snapshot(self, now: float) -> dict[str, Any]:
        """JSON-serialisable state for persistence and ``hueman rhythm``."""
        local = self._local(now)
        return {
            "phase": self.phase,
            "as_of": local.isoformat(timespec="seconds"),
            "bed_anchor_min": self._spec.bed_target_min,
            "wake_anchor_min": int(self._wake_anchor_min(
                now, SignalState(None, False, False, False, 0.0))[0]),
            "learned": {
                "wake_weekday": self._store.median("wake", "weekday"),
                "wake_weekend": self._store.median("wake", "weekend"),
                "sleep_onset_weekday": self._store.median("sleep_onset", "weekday"),
                "sleep_onset_weekend": self._store.median("sleep_onset", "weekend"),
            },
            "last_change_evidence": self._last_evidence,
        }
