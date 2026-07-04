# Natural solar-elevation circadian curve — design

**Date:** 2026-06-27
**Status:** approved (design validated live on the bridge 2026-06-27 — the proposed evening look felt right to the user)
**Branch context:** built on top of `synology-reanchor-cron` (DST-aware sun anchoring is already in place and verified)

## Problem

The current circadian curve (`hue_iac/circadian.py`) holds a **flat day plateau** at the
full day look (e.g. 4292 K / 100 %) for the entire middle of the day, then drops only
inside a narrow ramp centred on sunset. Verified live on the live bridge on
2026-06-27 (Portland, sunset 21:04 PDT): the generated `Golden hours` smart scene
held 100 % cool white from ~06:09 until the 20:19 "dusk begins" knee, and the zone read
**94.5 % brightness at 20:27** — visually identical to noon. The lights stay at full
daylight until the sun is essentially on the horizon, then fall off a cliff.

The user's goal, stated directly: **"ramp down naturally, like the sun does."** Full
brightness until the moment the sun goes down is explicitly *not* the goal.

## Goal

Replace the plateau-and-edge-ramp model with a curve driven by the sun's actual
position, so brightness and colour temperature **rise to a midday peak and then
decline smoothly all afternoon** (cooler/brighter when the sun is high, warmer/dimmer
as it drops), reaching the evening look around sunset and the night look through civil
twilight. The decline must be continuous, not a late cliff.

### Decisions locked in brainstorming

- **Constant peak, natural shape.** Each day is normalised so the curve reaches the
  configured **day look at that day's solar noon** (always 100 % midday, summer *or*
  winter). The *shape* of the rise and fall tracks the true sun; the absolute peak does
  not shrink in winter. (We can add optional seasonal peak-scaling later; out of scope
  now — YAGNI.)
- **Afternoon-weighted knees.** Given the bridge's hard 6-timeslot cap, the generated
  smart scene spends its resolution on the afternoon decline (the user's pain point).
  The morning is a single smooth sunrise→noon fade.
- **Apply-path only.** This drives the natively-delivered smart scene
  (`circadian_scene.py` → `reconcile.py`). No daemon. The daily Synology re-anchor keeps
  it tracking the seasons. The non-deployed `watch`/`engine` daemon is out of scope.

## Model

A new **daylight factor** derived from solar elevation θ (degrees above the horizon),
with θ_noon = the day's maximum elevation (at solar noon):

```
day      (θ ≥ 0):        f = clamp( sin θ / sin θ_noon , 0, 1 )
                         look = lerp(evening → day, f)
twilight (-6° ≤ θ < 0):  g = clamp( -θ / 6 , 0, 1 )
                         look = lerp(evening → night, g)
night    (θ < -6°):      look = night
```

- `sin θ` (relative irradiance, ∝ cos zenith angle) is the physically honest measure of
  "how much sun there is." It is flat-ish near noon and accelerates downward toward
  sunset — a natural decline, not a hard plateau.
- The three regimes are **continuous**: at θ = 0 both the day and twilight branches yield
  the evening look; the colour warms monotonically day → evening → night as the sun
  drops.
- `lerp` interpolates **both** brightness and mirek between the existing
  `CircadianParams` anchors (`day_*`, `evening_*`, `night_*`). No new look parameters.
- θ_noon has a closed form: `θ_noon = 90 − |lat − declination|`.

### Edge cases

- **Polar night** (`sun.is_polar_night`, θ_noon ≤ 0): constant night look (matches today's
  "evening/night fallback"; no division by a non-positive θ_noon).
- **Polar day** (`sun.is_polar_day`): θ stays > 0 all day; the factor simply never reaches
  night. Acceptable.
- **θ_noon guard:** only divide when θ_noon > 0; otherwise treat as night/twilight.

### Computed result (Portland, 2026-06-27; sunrise 05:24, solar noon 13:14, sunset 21:04)

Real values from the proposed formula against the live config anchors (day 233 mk/100 %,
evening 370 mk/60 %, night 454 mk/15 %):

| time  | sun elev | brightness | colour |
|-------|----------|-----------|--------|
| 07:00 | 14°  | 70 % | 2995 K |
| 09:00 | 35°  | 85 % | 3501 K |
| 11:00 | 55°  | 95 % | 4020 K |
| 13:14 | 68° (noon) | 100 % | 4292 K (peak) |
| 15:00 | 59°  | 97 % | 4114 K |
| 17:30 | 34°  | 84 % | 3489 K |
| 19:30 | 14°  | 70 % | 2985 K |
| 20:30 | 4°   | 63 % | 2783 K |
| 21:04 | ~0° (sunset) | 53 % | 2616 K |
| 21:40 | −6° (civil dusk) | 17 % | 2217 K |
| 22:00 | −8° | 15 % (night) | 2203 K |
| 22:34 | — | hand-off → night-red motion | |

The afternoon is **strictly non-increasing from solar noon to sunset** (verified). Note
the θ = 0 anchor is the evening look, but the listed sunset minute is already a hair into
twilight (refraction), so it reads ~53 % rather than exactly 60 % — a smooth pass-through,
not a step. Exact levels are tunable via the anchors / `sin θ` mapping after seeing it
live. In December the same curve, with no config change, reaches the cozy evening look by
~17:00.

## Components and changes

### `hue_iac/sun.py` — add solar elevation (pure)

- Extract the per-date solar intermediates (declination, equation of time, solar-noon
  minute) currently computed inside `sun_times()` into a small private helper so both
  `sun_times()` and the new elevation function share them (DRY, no behaviour change to
  `sun_times`).
- Add `SolarCalculator.solar_elevation(date, minute_of_day) -> float` (degrees):
  `sinθ = sin(lat)·sin(dec) + cos(lat)·cos(dec)·cos(H)`, where the hour angle
  `H = (minute_of_day − solar_noon_min) / 4` degrees. Reuses the existing
  DST-aware `_offset_for(date)` so elevation is correct under DST.
- Add `SolarCalculator.noon_elevation(date) -> float` returning `90 − |lat − dec|`.

### `hue_iac/circadian.py` — geometry-driven curve

- Change `CircadianCurve.state_at` to be a pure function of **sun geometry**, not clock
  time: `state_at(elevation_deg, noon_elevation_deg) -> CircadianState`, implementing the
  three-regime model above. This decouples the curve from time entirely — it answers
  "given how high the sun is, what should the light look like."
- `sample_day(...)` and any preview helper compute elevation per sampled minute via the
  `SolarCalculator` and feed it to `state_at`.
- `ramp_minutes` and `night_start_min` are **no longer used by the curve**. Keep the
  fields on `CircadianParams` (accepted-but-ignored, documented as deprecated) so
  existing configs — including the live `hue.yaml` — keep parsing unchanged. No config
  break.

### `hue_iac/circadian_scene.py` — afternoon-weighted knees

- `circadian_timeslots(...)` samples the new curve at 6 sun-relative knees:
  `sunrise, solar_noon, noon+⅓·(sunset−noon), noon+⅔·(sunset−noon), sunset, hand_off`.
  Each look comes from `state_at(elevation(knee), noon_elevation)`. Same clamp → sort →
  dedupe → cap-at-6 discipline as today. Polar fallback unchanged (single midday step).
- Because the knee looks now genuinely differ across the afternoon, the bridge's long
  cross-fades draw the smooth declining arc.

### `hue_iac/reconcile.py` — wiring only

- `CircadianSceneReconciler` already builds a `SolarCalculator` for `_sun_times()`; pass
  that same calculator (with the date) into the generator so it can compute elevation.
  No new astronomy in the reconciler.

### `hue_iac/cli.py` — preview

- `_print_curve` computes elevation per sample via the calculator and feeds `state_at`.
  Output format unchanged (time / kelvin / brightness rows), now showing the real arc.

### `hue_iac/config.py`

- No schema change. `ramp_minutes`/`night_start_min` remain valid keys (ignored by the
  new curve). The `circadian` block's look anchors are unchanged.

## Testing (test-first)

New / replaced unit tests (pure, no bridge):

- **`test_sun.py`**: `noon_elevation == 90 − |lat − dec|` for a known date; elevation ≈ 0
  at sunrise/sunset minutes; a spot value against a NOAA reference; elevation is
  DST-correct when `tz` is set. Keep all existing DST tests.
- **`test_circadian.py`** (replaces the plateau tests): `state_at(θ_noon, θ_noon)` == day
  look; `state_at(0, θ_noon)` == evening look; `state_at(−6, θ_noon)` == night look;
  brightness and mirek are **monotonic** as elevation falls from θ_noon → 0 → −6 (strictly
  no plateau across the afternoon); warming (kelvin decreases) as the sun drops; polar
  night → night look; θ_noon ≤ 0 handled without error.
- **`test_circadian_scene.py`**: afternoon knees step **strictly down** in brightness
  (noon > early-aft > late-aft > sunset); peak knee equals the day look; ≤ 6 timeslots;
  sorted, deduped; DST-aware reconciler test still passes.

Full suite must stay green (currently 120 passing) with no skips/xfails.

## Out of scope

- Seasonal peak-scaling (winter middays dimmer) — deferred; today we normalise to a
  constant 100 % peak.
- Changes to `watch`/`engine` (non-deployed daemon).
- Night-motion red guidance and the hand-off time (unchanged; 22:34 still owns
  night → sunrise).
- The separate `config.py` raw-traceback robustness gap surfaced by the adversarial
  audit — tracked separately, not part of this curve work.

## Risks

- **6-knee resolution.** A smooth arc approximated by 6 long cross-fades will be close,
  not exact. Afternoon-weighting mitigates the visible part. If the morning rise looks
  too coarse, we trade one afternoon knee for a mid-morning one (one-line change).
- **Mapping choice.** `sin θ` vs linear-in-θ changes how "fast" the afternoon dims. We
  start with `sin θ` (physical) and tune the anchors after observing it live.
