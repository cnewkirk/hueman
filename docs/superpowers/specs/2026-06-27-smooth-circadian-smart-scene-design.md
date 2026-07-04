# Smooth circadian smart scene — design

Date: 2026-06-27 · Bridge: Hue Bridge Pro · Branch: `night-red-motion-guidance`

## Goal

Make the daytime circadian cycle change **smoothly and continuously** instead of
snapping between looks at a few discrete times ("abrupt, violent transitions at
certain time intervals"). Delivered **natively on the bridge** (no daemon),
generated and managed declaratively by `hue-iac`, anchored to the real sun.

## Ground truth (probed live, read-only + one self-cleaning write probe)

- A `smart_scene` caps at **6 timeslots** (`maxItems: 6` on `week_timeslots[0].timeslots`).
- Its top-level **`transition_duration` accepts at least 24h** (`86400000 ms` stored
  verbatim). The 60s on the existing scenes is the app default, **not** a ceiling.
- `circadian.py` (`CircadianCurve.state_at(minute, sun) -> (mirek, brightness)`) already
  models a sun-anchored curve and is **not currently wired to the bridge** (only `preview`
  uses it). It is the source of truth for the looks.
- Scenes store actions **verbatim** (confirmed for the night scenes), so the MUST-FIX 2
  `scene_actions_match` drift check is idempotent here.
- A `smart_scene` can be grouped on a **zone** ("Golden hours" is on the "All" zone).
  "Night Guide" is a 12-light zone covering the whole apartment.

**Design consequence:** smoothness comes from **6 sun-anchored points on the curve + a
`transition_duration` long enough that the bridge is always fading between them**, never
holding-then-jumping. This is how Hue's own "Natural light" (also 6 timeslots) is shaped.

## Behaviour (the contract)

- `hue-iac` generates up to **6 `[iac]`-owned scenes** on a zone, each a uniform
  colour-temperature + brightness sampled from `circadian.py` at a sun-anchored time.
- It maintains **one smart scene** of 6 timeslots at those times, targeting those scenes,
  with `transition_duration` set so dawn/dusk render as gentle gradients and the midday /
  evening plateaus hold steady (the curve is flat there, so no jump).
- **Sun-anchored & idempotent:** every `apply` recomputes from the day's sun; re-running
  (the daily cron) re-anchors and is a NOOP once converged.
- **Reversible:** the existing smart scene is backed up (write-once) before any change; the
  old hand-built scenes (Arise/Shine/Storybook/Unwind) are left on the bridge.
- **Night unchanged:** the cycle covers the **waking day** and hands off at bedtime;
  `night_motion` keeps owning 22:34→sunrise (red-on-motion) exactly as today.

## Components (each small, one purpose, testable in isolation)

1. **Config — `CircadianSceneSpec` (`config.py`).** Parses a new `circadian_scene:` block:
   ```yaml
   circadian_scene:
     smart_scene: "Golden hours"   # the smart scene this owns (by name)
     zone: "Night Guide"           # zone to drive (must be a declared/known zone)
     transition: ramp              # "ramp" (= circadian ramp_minutes) | a duration like "90m"
     hand_off: "22:34"             # last timeslot ≤ this; night_motion owns the rest
   ```
   The **look** comes from the existing top-level `circadian:` params (day/evening/night
   mirek + brightness, `ramp_minutes`, `night_start`). The current `smart_scenes:` Golden
   hours entry is removed (superseded by this).

2. **Pure generator — `hue_iac/circadian_scene.py`.** `circadian_timeslots(params, sun, *,
   hand_off_min) -> list[CircadianStep]` where each step is `(minute_of_day, mirek,
   brightness)`. Picks ≤6 sun-anchored **knee** times of the curve — `sunrise−ramp/2`
   (dawn begins), `sunrise+ramp/2` (full day), solar-noon (day plateau anchor),
   `sunset−ramp/2` (dusk begins), `sunset+ramp/2` (evening), `hand_off` (wind-down) —
   sampling `CircadianCurve.state_at` at each, then dedupes/sorts/clamps to a valid,
   chronological set of ≤6. I/O-free, fully unit-tested (golden values per the curve).

3. **Reconciler — `CircadianSceneReconciler` (`reconcile.py`).** A new `Reconciler`
   (sibling of `SmartSceneReconciler`, added to `Planner`):
   - `plan()`: compute desired steps; resolve the zone (missing → BLOCKED); for each step
     diff the desired scene (`scene_body` + `scene_actions_match`) and the smart scene's
     timeslots/`transition_duration` against live; emit CREATE/UPDATE/NOOP. Empty/garbled
     desired set → BLOCKED (never write an empty smart scene — reuses MUST-FIX 1's guard).
   - `apply()`: ensure the N scenes (create/update via `scene_body`); **back up** the live
     smart scene via the shared `_write_backup`; then write the smart scene. If it exists on
     the right zone → PUT (`week_timeslots` + `transition_duration`); if its group differs
     (old Golden hours is on "All") or it is absent → delete-and-recreate / create on the
     target zone (group is treated as immutable; verified on first apply).
   - Reuses `scene_body`, `scene_actions_match`, `_write_backup`, and the `_slot_minute` /
     empty-floor helpers added in the must-fix work — no new bridge-I/O patterns.

4. **`transition_duration` policy.** Default `transition: ramp` → `ramp_minutes` (e.g. 90
   min): the two non-flat segments (dawn, dusk) are each exactly `ramp_minutes` wide between
   their knee timeslots, so a ramp-length fade renders them as smooth gradients; the flat
   plateaus are unaffected (fading from a look to the same look is constant). Configurable to
   an explicit duration. **Verified on the bridge** after first apply (observe the actual
   transition phasing; adjust knee placement if the bridge's fade-start offset warrants it —
   firmware behaviour, not assumed).

5. **`hue.yaml` change.** Remove the `smart_scenes:` Golden-hours entry; add the
   `circadian_scene:` block above. `circadian:` params stay (they now drive the looks).

## Data flow

`hue.yaml` → `config.py` (`CircadianSceneSpec` + `CircadianParams`) → `circadian_scene.py`
(curve → ≤6 sun-anchored steps) → `CircadianSceneReconciler.plan` diffs vs `BridgeState`
(zone, scenes, smart scene) → `apply`: POST/PUT the 6 scenes, back up + write the smart
scene on the zone. The bridge then runs the continuous cycle natively; no resident process.

## Integration with `night_motion`

`night_motion` is untouched. The circadian cycle's last timeslot is at `hand_off` (≤ 22:34),
so the day cycle winds down to warm/dim before `night_motion`'s window begins. Overnight
behaviour (lights off baseline, red on motion) is exactly as today. Both can share the
"Night Guide" zone; the night motion automation overlays the smart scene via motion as it
already does.

## Error handling & reversibility

- **Missing zone** → BLOCKED ("apply its `areas.zones` entry first"), mirroring `night_motion`.
- **Empty/!valid desired set** → BLOCKED; `apply` refuses to write an empty smart scene
  (reuses the MUST-FIX 1 empty-floor guard).
- **Backup before write** (write-once) so the pre-first-apply original smart scene is
  recoverable; old hand-built scenes remain on the bridge.
- **Idempotent:** plan diffs desired scenes' actions + the smart scene's timeslots/transition
  vs live; re-apply is NOOP once converged (safe for the daily cron).
- **Two-pass cold start** still applies if the zone doesn't yet exist (create zone, then wire).

## Testing

- **Unit (pure):** `circadian_timeslots` — count ≤6, chronological, clamped, correct
  knee times and sampled looks for a known `(params, sun)`; degenerate cases (polar day/night,
  tiny ramp, `hand_off` before sunset).
- **Reconciler (fake client/state):** scenes-absent → CREATE; look edit → UPDATE (drift);
  converged → NOOP; missing zone → BLOCKED; smart scene on wrong group → delete+recreate;
  `transition_duration` drift → UPDATE; backup written once.
- **Live verification (read-only + one apply):** after apply, read back the smart scene (6
  timeslots at sun-anchored times, long `transition_duration`) and the 6 scenes; re-run
  `plan` → all NOOP (idempotent); observe an evening fade to confirm continuity; confirm
  `night_motion` still NOOP and the 22:34 handoff is clean.

## Dependencies — finish first (foundation)

This builds directly on the in-progress MUST-FIX hardening of the smart-scene/scene path:
- **MUST-FIX 2** — `NightMotionReconciler.plan` look-drift detection + apply guard (the
  `scene_actions_match` reuse this feature depends on).
- **CLI** — accurate blocked-refusal message (this feature adds new BLOCKED reasons).
- **Spec doc fix** + full `pytest` + read-only live `plan` NOOP.
These are tracked in `docs/.../soft-whistling-cray.md`; complete them, then build this.

## Out of scope (deferred / next)

- **Gentle night-red motion fade** (scene `dynamics`/recall duration on the night scene) —
  a separate, optional add-on; the day cycle is the priority here.
- **User-overridable anchor times / >6 logical steps** — the bridge caps at 6; the knee set
  is sufficient. Revisit only if the curve gains more inflection points.
- **Synology daily-re-anchor cron** — still the next operational item once this lands.
