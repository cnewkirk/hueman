"""Pure generator for the smooth-circadian smart scene.

Samples the sun-elevation circadian curve (:mod:`hueman.circadian`) at a handful
of afternoon-weighted *knee* times so a native ``smart_scene`` — capped by the
bridge at six timeslots — can fade continuously through the day instead of
snapping between a few hand-built looks. I/O-free: :mod:`hueman.reconcile`
owns the bridge writes.

Knees are weighted toward the afternoon decline: sunrise, solar noon, two
afternoon points, sunset, and the wind-down hand-off. The bridge fades between
them over a long ``transition_duration``, reproducing the natural sun-tracking
curve as a continuous gradient.
"""

from __future__ import annotations

from dataclasses import dataclass

from .circadian import CircadianCurve, CircadianParams
from .sun import SolarCalculator

#: A ``smart_scene`` accepts at most this many timeslots (bridge schema cap of 6,
#: confirmed live: ``maxItems`` on ``week_timeslots[0].timeslots``).
MAX_TIMESLOTS = 6


@dataclass(frozen=True)
class CircadianStep:
    """One generated timeslot: its start minute-of-day and the look it holds.

    ``on`` is false only for the hand-off step, which turns the zone off so the
    night-motion automation owns the night window cleanly (no competing on-step).
    """

    minute: int
    mirek: int
    brightness: float
    on: bool = True


def circadian_timeslots(
    params: CircadianParams,
    solar: SolarCalculator,
    date,
    *,
    hand_off_min: int,
) -> list[CircadianStep]:
    """Return up to :data:`MAX_TIMESLOTS` sun-anchored circadian steps for ``date``.

    Knees are weighted toward the afternoon decline: sunrise, solar noon, two
    afternoon points, and sunset (each sampled from the elevation-driven
    :meth:`CircadianCurve.state_at`), plus a final ``hand_off_min`` knee that turns
    the zone **off**. The off hand-off is deliberate: it lets ``night_motion`` own
    the whole 22:34→sunrise window (off baseline + red on motion) with no competing
    on-step — the overlap the night-red design trims. Clamped to a valid day,
    sorted, de-duplicated by minute, capped at six. Polar days (no sunrise/sunset)
    fall back to a single midday step.
    """
    curve = CircadianCurve(params)
    sun = solar.sun_times(date)
    noon_elev = solar.noon_elevation(date)

    if sun.is_polar_day or sun.is_polar_night:
        midday = 12 * 60
        state = curve.state_at(solar.solar_elevation(date, midday), noon_elev)
        return [CircadianStep(midday, state.mirek, state.brightness)]

    sunrise, sunset = sun.sunrise_min, sun.sunset_min
    solar_noon = (sunrise + sunset) / 2.0
    afternoon = sunset - solar_noon
    day_candidates = [
        sunrise,                          # day begins (evening look at the horizon)
        solar_noon,                       # peak day look
        solar_noon + afternoon / 3.0,     # early afternoon
        solar_noon + 2.0 * afternoon / 3.0,  # late afternoon
        sunset,                           # evening look at the horizon
    ]

    # The off hand-off must be the FINAL slot — an on-knee sorted after it would
    # re-light the zone after bedtime — so day knees at/after hand_off are dropped
    # (an early hand_off simply ends the day cycle early).
    hand_off = int(max(0, min(1439, round(float(hand_off_min)))))
    steps: dict[int, CircadianStep] = {}
    for raw in day_candidates:
        minute = int(max(0, min(1439, round(raw))))
        if minute >= hand_off:
            continue
        state = curve.state_at(solar.solar_elevation(date, minute), noon_elev)
        steps[minute] = CircadianStep(minute, state.mirek, state.brightness)

    # Hand-off: the day cycle goes OFF at hand_off so night_motion owns the night
    # window cleanly.
    steps[hand_off] = CircadianStep(hand_off, params.night_mirek, 0.0, on=False)

    return [steps[m] for m in sorted(steps)][:MAX_TIMESLOTS]
