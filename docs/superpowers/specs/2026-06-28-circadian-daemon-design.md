# Circadian daemon — design

**Date:** 2026-06-28
**Status:** awaiting user review
**Bridge:** Hue Bridge Pro (MotionAware) · zone "Night Guide" (12 apartment bulbs)

## Problem & evidence

The goal has always been **smooth, continuous circadian transitions** as the day goes on.
The native `smart_scene` approach cannot deliver this, proven on the live bridge:

- A `smart_scene` recalls fixed scenes at ≤6 timeslots, and **Hue ignores the transition on
  a scene recall** — the lights **snap** between looks (corroborated by HA core #88894 and
  diyHue #169, and observed directly: the end-of-test smart_scene reactivation produced a
  visible "snap").
- **Per-zone `dynamics` transitions DO fade smoothly** — verified live: a single
  `grouped_light` PUT with `dynamics.duration` produced a smooth fade the user watched
  (the polled group-average *readback* is noisy mid-transition and is not a reliable
  instrument; the bulbs themselves fade cleanly).
- The proven pattern for smooth custom circadian on Hue is a **daemon** that re-sends
  per-zone state with a transition on an interval (Home Assistant Adaptive Lighting:
  ~90 s interval / ~45 s transition; hue-scheduler: scheduled updates with `tr`/`interpolate`).
- Motion was investigated and **exonerated** as the cause of earlier erratic traces
  (motion services read false throughout); a full bridge census (CLIP v2 + v1 rules/
  schedules + adaptive "Natural light") found no other hidden controller.

## Goal

Replace the snappy smart_scene with a **persistent daemon** that drives the Night Guide
zone through smooth, continuous brightness **and** color-temperature transitions, reusing
the existing pure decision layer, deployed as a Docker container on the Synology NAS.

## What this reuses vs. replaces

- **Reuses (unchanged):** `sun.py` (DST-aware solar math), `circadian.py` (solar-elevation
  curve — the natural ramp the user approved), `client.py`, `state.py`, `payload.py`, and
  the `night_motion` **night** red-on-motion guidance.
- **Replaces / retires:** the `Golden hours` smart_scene (deleted on the bridge); the
  `CircadianSceneReconciler` + `circadian_scene` config (`circadian_scene.py`,
  `CircadianSceneSpec`) — superseded by the daemon; the PR-#4 **daily re-anchor cron**
  (the daemon re-anchors continuously, so the cron is no longer needed).
- **Changes:** `night_motion` becomes **night-red-only** (day/evening motion recalls
  stripped) so nothing snaps a fixed look during the day.

> Branch/PR note: this builds on the solar-elevation curve (`natural-circadian-curve`) and
> the DST math (PR #4). The daemon supersedes the smart_scene delivery and the daily cron;
> integration/branch strategy is decided at planning time. The DST math and the curve are
> kept — only the *delivery mechanism* changes.

## Architecture (pure/IO split preserved)

### Pure layer (unit-tested, no I/O)
- `sun.py`, `circadian.py` — existing; sample `(brightness, mirek)` for a time/sun.
- **New: a daemon decision core** (a state machine, analogous to `engine.py`'s
  `PolicyEngine`). Inputs are all explicit (injected time, config, and observed events) so
  it is fully unit-testable with no clock or bridge. It owns:
  - **Mode:** `DRIVING` · `SUSPENDED` (manual override) · `NIGHT_IDLE`.
  - **Action for this tick:** `DriveTo(brightness, mirek, transition_ms)` ·
    `FadeOff(transition_ms)` · `Hold` (do nothing).
  - **Decision rules:**
    - Active window = **sunrise → hand_off** (default 22:34). Window edges come from
      `sun.py` for the date.
    - `DRIVING` inside window → `DriveTo(curve(now))` with `transition_ms ≈ interval`
      (overlapping → continuous fade).
    - `DRIVING` reaching hand_off → `FadeOff` once, then → `NIGHT_IDLE`.
    - `NIGHT_IDLE` reaching sunrise → `DRIVING`.
    - Any mode + an **external event** (a bridge change the daemon did not initiate) →
      `SUSPENDED`, `Hold`.
    - `SUSPENDED` + a **resume trigger** → `DRIVING` (re-sync to `curve(now)`).
  - **Echo buffer:** records `(rid, expected, ts)` for each write so the matching bridge
    event is recognized as *self* (not a human), with a short TTL (à la `watch.py`).

### I/O layer (thin)
- **New daemon loop module** (alongside `watch.py`). Each tick (~`interval`, default 60 s):
  reads now, asks the decision core for the action, executes it via `client.py` +
  `payload.py`, and records the echo. Concurrently it consumes the bridge **SSE event
  stream** (`/eventstream/clip/v2`) to feed external-change + resume events to the core,
  and checks the **resume control file**. Bridge/SSE errors are logged and retried (with
  backoff); the loop never crashes the process (Docker `restart=always` covers hard
  failures).
- **CLI:** `hue-iac circadian run` (run the daemon) and `hue-iac circadian resume`
  (write the resume sentinel the running daemon reads). `watch` is left as-is.

## Behaviour contract

- **Day:** from sunrise to 22:34, Night Guide continuously fades along the solar-elevation
  curve — cool/bright at solar noon, warming and dimming through the afternoon/evening.
- **Hand-off (22:34):** the daemon fades the zone **off**, then idles; `night_motion` owns
  22:34→sunrise (off baseline, soft red on motion) exactly as today.
- **Manual override:** when the daemon sees a Night Guide change it did not make (you
  recall a scene, dim, change color, etc.) it **suspends indefinitely** and stops driving.
  Detection is **event-based via SSE** (the polled group-average is unreliable mid-fade).
- **Re-engage (all four):**
  1. **Off → On** power-cycle of the zone (Hue app / switch / voice).
  2. **`hue-iac circadian resume`** command (writes the sentinel file).
  3. **Optional dedicated Hue button or scene** — if configured (`resume_trigger`), its
     event resumes the daemon.
  4. **Daily sunrise safety-resume** — a backstop so it can never stay stuck off.
- **Motion:** `Main Room` reconfigured to **night-red-only** (07:00/17:00 day/evening
  recalls removed; 22:34 red kept), backed up first and fully reversible.

## Configuration (`hue.yaml`) — everything tunable, nothing hardcoded

**Principle (hard requirement):** every operational value is read from YAML into a frozen
`CircadianDaemonSpec` dataclass (mirroring the existing `config.py` pattern). **No magic
numbers in code** — intervals, transitions, times, tolerances, retry/backoff, clamps, and
paths all come from config, each with a documented default applied at parse time. The pure
decision-core receives these as parameters (which is also what makes it unit-testable).

New `circadian_daemon:` block (defaults shown):

```yaml
circadian_daemon:
  zone: "Night Guide"          # zone the daemon drives
  start: sunrise               # active-window start (anchor: sunrise/sunset±offset or HH:MM)
  hand_off: "22:34"            # active-window end; fade to off, then idle (night_motion owns)
  interval: 60s                # tick cadence
  transition: 75s              # per-tick fade length (>= interval => continuous)
  fade_off: 90s                # transition used for the hand-off fade-to-off
  manual_override:
    detect: true               # suspend on a change the daemon didn't make
    echo_ttl: 4s               # window to recognize our own writes vs. external (SSE)
    resume_on_power_cycle: true     # zone off->on re-engages
    resume_trigger: null            # optional: a button rid or scene name that re-engages
    control_file: ".hue-circadian-resume"   # `circadian resume` writes this; daemon clears it
    daily_safety_resume: true       # re-engage at the window start each day (backstop)
  brightness_floor: null       # optional hard min % the daemon will not go below (null = curve only)
  brightness_ceiling: null     # optional hard max %
  retry:
    on_error: 30s              # wait after a failed tick / bridge error
    sse_backoff_max: 60s       # max reconnect backoff for the event stream
  log:
    path: "logs/circadian.log"
    level: info
```

- The **look** (day/evening/night mirek + brightness, ramp) stays in the existing top-level
  `circadian:` block; **location** (lat/lon/tz) in `location:` — both reused, both already
  YAML.
- Remove the `circadian_scene:` block (superseded).
- `night_motion:` gains `mode: night_only` (default for this deployment) so the reconciler
  strips the day/evening recalls; all night_motion looks/times remain YAML as today.

Anything not listed that turns out to need tuning during implementation is added to this
block with a default — it does not get hardcoded.

## Deployment (Docker on Synology)

- **Dockerfile**: slim Python base, `pip install .`, entrypoint `hue-iac circadian run`.
- **Run** (Container Manager): `--restart always`, host/LAN networking so the bridge
  (`192.0.2.2`) is reachable, and mounts for `hue.yaml`, `.hue-key`, `.hue-pin.json`
  (read-only) and a writable `logs/` volume. The resume sentinel lives on a mounted path so
  `hue-iac circadian resume` (run in the container, or a sibling) can signal it.
- **Runbook** under `deploy/synology/` (Docker variant) covering build/import, run, verify,
  and how to trigger resume.

## Error handling & reversibility

- Bridge unreachable / SSE drop → log, backoff, retry; process stays up.
- TLS via the mounted pin (`pin.py`); a missing pin fails closed (no silent TOFU in the
  container).
- `Main Room` and any deleted/changed bridge resource is **backed up** (write-once) before
  the change; restore returns the prior automation. The smart_scene deletion is reversible
  by re-running the (retained-in-git) generator if ever desired.

## Testing

- **Pure (no I/O):** the decision-core state machine — every transition
  (`DRIVING→SUSPENDED` on an external event; `SUSPENDED→DRIVING` for each of the four
  resume triggers; `DRIVING→NIGHT_IDLE` at hand_off; `NIGHT_IDLE→DRIVING` at sunrise) and
  action computation (correct `(bri, mirek, transition)` for a given time/sun), with
  injected time/events. Echo-buffer self-vs-external logic. Curve reuse covered by existing
  tests.
- **I/O (mocked):** the loop wiring against a fake client + a fake SSE event source —
  verify it issues the planned writes, records echoes, and routes events to the core.
- **Live (deploy):** watch a real smooth circadian stretch; exercise each resume trigger;
  confirm motion is night-only; confirm a manual change suspends and a re-engage resumes.

## Out of scope (deferred)

- Multiple zones (only Night Guide).
- Removing the old smart_scene/`circadian_scene` *code* (vs. just retiring its use + the
  bridge resource) — the plan decides whether to delete it now or leave it dormant.
- The legacy `watch.py`/`engine.py` motion daemon (unrelated; untouched).
- Seasonal peak-scaling of the curve (already deferred in the curve spec).
