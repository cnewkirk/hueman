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
