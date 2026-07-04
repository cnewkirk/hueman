"""Shared test fixtures and helpers."""

from __future__ import annotations

import datetime as _dt

from hue_iac.config import Config

TZ_OFFSET = -5.0


def epoch_at(hour: int, minute: int = 0, *, date: _dt.date | None = None) -> float:
    """Return epoch seconds for a local wall-clock time at ``TZ_OFFSET``.

    Args:
        hour: Local hour (0-23).
        minute: Local minute.
        date: The local date; defaults to the 2026 summer solstice for stable
            sunrise/sunset.

    Returns:
        Epoch seconds corresponding to that local moment.
    """
    day = date or _dt.date(2026, 6, 21)
    tz = _dt.timezone(_dt.timedelta(hours=TZ_OFFSET))
    return _dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz).timestamp()


def make_config(policy: dict, *, circadian: dict | None = None) -> Config:
    """Build a validated :class:`Config` around a single motion policy dict."""
    doc = {
        "bridge": {"host": "192.0.2.10", "application_key": "test-key"},
        "location": {"lat": 40.7128, "lon": -74.0060, "tz_offset_hours": TZ_OFFSET},
        "motion_policies": [policy],
    }
    if circadian is not None:
        doc["circadian"] = circadian
    return Config.parse(doc)
