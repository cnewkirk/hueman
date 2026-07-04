# Night-motion evening no-op timeslot — design

**Date:** 2026-07-03
**Status:** approved
**Owner:** night_motion (`hue_iac/nightmotion.py`, `config.py`, `NightMotionReconciler`)

## Problem

`night_only` mode emits two MotionAware timeslots — `night_start` (22:34) and a
`00:00` clone — so the soft-red guidance covers the whole night (a single slot
does not wrap past midnight; see the 2026-06-29 gotcha). But with two slots the
bridge's wrap semantics make the `00:00` slot govern **00:00 → 22:34**, i.e.
essentially all day. The automation's daylight gate hides this while the sun is
up; once it is dark outside — well before the 22:34 hand-off on summer evenings —
the slot is live in prime living-room hours:

- 3 minutes without motion → `all_off` kills the Night Guide zone
  (daemon logs `zone turned off -> suspended`);
- the next motion → recall of the dim-red night scene, which the circadian
  daemon then fades back to the curve over 75 s.

Confirmed live 2026-07-03 (and present in logs nightly since the two-slot fix
landed): zone off at 21:17:02 PDT, red-scene recalls at 21:17:42 and 21:28:48
(bridge `scene.status.last_recall`), all before night start. User-visible
off → red → slow-back-to-normal churn every evening between ~21:00 and 22:34.

## Goal

Night guidance active only from `night_start` until sunrise. Dark evenings
before `night_start` get **no** recalls and **no** `all_off` — the circadian
daemon and manual control own the evening, as they did before the two-slot fix.

## Approaches considered

1. **Third no-op morning timeslot** (chosen) — bound the wrapped slot's reach
   with an actionless slot starting in the morning.
2. Daemon toggles the automation's `enabled` flag on a schedule — rejected:
   night guidance is native by design, and the `enabled` toggle is known to
   return bogus errors and does not flush the MotionAware action cache.
3. Move `night_start` to sunset (accept red + `all_off` all evening) —
   rejected: that formalizes the disruption instead of fixing it.

## Design

`night_only` emits **three** timeslots, in chronological order (the bridge
expects it):

| start | actions | governs |
|---|---|---|
| `00:00` | recall night scene; `all_off` after `timeout` | 00:00 → `day_start` (dark part = 00:00 → sunrise via the daylight gate) |
| `day_start` (default 08:00) | none — no-op | `day_start` → `night_start` (nothing happens, even when dark) |
| `night_start` (22:34) | recall night scene; `all_off` after `timeout` | `night_start` → 00:00 |

### Config (`config.py`)

`NightMotionSpec` gains `day_start`, parsed exactly like `start` (`"HH:MM"`),
default `08:00`. Validation: `00:00 < day_start < start` (clock minutes; the
no-op slot must sit strictly between the midnight clone and the night slot —
`00:00` exactly would collide with the clone). Default rationale: 08:00 is after the latest sunrise of the
year at the deployment latitude, so the `00:00` slot still covers dark winter
mornings and the daylight gate trims the post-sunrise remainder.

### Pure layer (`nightmotion.py`)

`transform_automation(..., night_only=True)` currently returns
`[midnight clone, night slot]`. It will return
`[midnight clone, no-op slot, night slot]` with the no-op slot built as:

```json
{"start_time": {"time": {"hour": 8, "minute": 0}, "type": "time"}}
```

— no `on_motion`, no `on_no_motion`. The bridge publishes no timeslot schema
(`behavior_script.configuration_schema` is empty), so this shape is rung 1 of
an **encoding ladder** verified live during rollout:

1. slot with no action keys at all (preferred);
2. `"on_motion": {"recall_single": []}`;
3. `"on_motion": {}`.

**Rollout outcome (2026-07-03, probed live):** rungs 1-3 are all rejected —
the schema requires `on_motion` with `recall_single` minItems 1 on every
timeslot. The schema's explicit no-op action is the string `"do_nothing"`
(the Hue app's "Do nothing"), discovered by enum probing; so the accepted
no-op slot is `"on_motion": {"recall_single": [{"action": "do_nothing"}]}`,
which round-trips verbatim on GET. That is the shape the code emits.

Whichever rung the bridge both **accepts on PUT** and **returns unchanged on
GET** (dict-equal) is the shape the code keeps. Round-trip identity is required because
`NightMotionReconciler.plan` decides wiring drift with an exact `==` between
the transform output and the live configuration — a normalized-by-the-bridge
shape would re-plan as UPDATE forever.

New parameter: `transform_automation(..., day_start: tuple[int, int] = (8, 0))`.
Docstring gains the wrap explanation (the 00:00 slot otherwise governs
00:00 → night_start).

### Reconciler

No code change. The added slot makes `wiring_ok` false → one `UPDATE` → PUT of
the instance `configuration` (existing write-once backup first). That re-PUT is
also the MotionAware scene-action cache flush, per the 2026-07-03 gotcha. A
second `plan` must report `NOOP`.

### Out of scope

- `full` mode is unchanged (it has day/evening slots and no wrap gap).
- The daylight gate, motion service, source, and scene looks are untouched.

## Testing

Unit (`tests/test_nightmotion.py`):

- `night_only` emits exactly 3 slots, chronologically ordered, with the
  expected starts (00:00 / `day_start` / `night_start`).
- The no-op slot carries no `on_motion` / `on_no_motion` keys.
- Both red slots still recall the night scene and carry the `all_off` timeout.
- Transform is idempotent: feeding its own output back in yields an equal config.
- `day_start` default is (8, 0); custom values flow through.
- Config parse: `day_start` accepted, default applied, `day_start >= start`
  rejected with a clear `ConfigError`.

Live verification (rollout):

1. `hue-iac plan` shows one `night_motion` UPDATE (retarget summary).
2. `apply`; GET the instance and confirm the stored config equals the transform
   output (round-trip identity, encoding ladder rung recorded in the PR).
3. Re-`plan` → `NOOP`.
4. Behavioral: before 22:34, >3 min without motion must NOT turn the zone off
   and motion must NOT recall red; after 22:34 red guidance works; after
   midnight red guidance still works (next morning's log check).

## Rollout

Laptop cannot currently reach the bridge directly; run `plan`/`apply` from the
NAS if that persists. No daemon image rebuild needed (daemon untouched). Update
the CLAUDE.md night_motion notes (two slots → three) in the same PR.
