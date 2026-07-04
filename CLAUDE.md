# CLAUDE.md

This file guides Claude Code when working in hueman (Hue lighting
infrastructure-as-code + resident circadian daemon). Live deployment
configuration lives in a separate private ops repo; nothing in this
repo references a real bridge or network.

## Commands

```bash
# Install (editable, with dev deps). Use python3 — plain `python` may not be on PATH.
python3 -m pip install -e ".[dev]"

# Run all tests
python3 -m pytest

# Run a single test file
python3 -m pytest tests/test_engine.py

# Run a single test by name
python3 -m pytest tests/test_engine.py -k "test_motion_on"

# CLI (after install)
hueman -c examples/home.yaml validate
hueman -c examples/home.yaml preview
hueman -c examples/home.yaml plan
hueman -c examples/home.yaml apply
hueman -c examples/home.yaml circadian run   # the resident daemon (foreground)
hueman -c examples/home.yaml watch --dry-run # legacy motion controller
```

Bridge-touching commands need `HUE_APPLICATION_KEY` — set it from your bridge
pairing (`hueman auth` performs trust-on-first-use pairing and TLS pinning).
The bridge host lives in the config (`bridge.host`); `HUE_BRIDGE_HOST` only
overrides it.

## MotionAware bridges — READ BEFORE claiming "no sensors"

On a **Hue Bridge Pro running MotionAware**, motion is sensed by a *grid of
bulbs* (Zigbee-signal fluctuation), **not** by legacy PIR sensors.

Consequence: `GET /clip/v2/resource/motion` returns **0** — **this does not
mean there are no sensors.** MotionAware surfaces under its own resource types:

- `motion_area_configuration` — the grid (room-scoped, `motion_area_candidate` bulbs)
- `convenience_area_motion` / `security_area_motion` — the motion *services*,
  each with `motion.motion`, `motion.motion_report.changed`, and
  `sensitivity{sensitivity, sensitivity_max}`

`state.py` indexes these (`MotionArea`, `_index_motion_areas`) and
`hueman inventory` prints them. Only the legacy `watch.py` runtime is blind to
MotionAware (it routes just `motion`/`light_level`/`grouped_light` events and
requires a legacy PIR sensor to even construct). MotionAware **sensitivity is a
live-only bridge setting**: no reconciler manages it, and a daily apply neither
reverts nor recreates it.

## Verification discipline (non-negotiable)

- **Never assert a negative ("there are no X on the bridge") from a hand-picked probe.** Enumerate the authoritative full set first: `GET /clip/v2/resource` (all types), then inspect what's actually there. A whitelist of types you *assume* exist is not evidence of absence.
- A finding that flips the whole plan is a signal to **lower confidence and widen the search**, not to assert "definitive/zero/full stop."
- If a claim depends on platform capabilities — especially features newer than the model's training (e.g. Bridge Pro, MotionAware) — **research the docs/API before asserting.**

## The two day-cycle deployment modes

**Mode 1 — the circadian daemon** (`hueman circadian run`, typically a Docker
container near the bridge): re-samples the solar curve every 60 s and drives one
grouped_light zone with long cross-fades from sunrise to `hand_off`, detects
manual overrides by settle-and-compare, and owns the TV-bias hold. Night
guidance stays native on the bridge (`night_motion:` → a MotionAware
`behavior_instance`), provisioned by `apply`.

**Mode 2 — native, daemon-less**: `CircadianSceneReconciler` (+ pure
`hueman/circadian_scene.py`) generates ≤6 sun-anchored knee scenes + a
`smart_scene` with long cross-fades; a daily cron `apply` re-anchors the
timeslot times to real sun (idempotent — the looks are curve regime-constants).

The reconcilers in `reconcile.py` (all provisioned by `apply`):
- `NightMotionReconciler` (+ pure `hueman/nightmotion.py`) tunes a MotionAware `behavior_instance` for night soft-red guidance on a guide zone, backing it up first. `plan` detects zone-scene look drift via the tolerant `scene_actions_match`; apply re-PUTs the automation only when its wiring actually changes. Sun-anchored timeslots it can't re-time surface as `BLOCKED`, not a crash.
- `CircadianSceneReconciler` — the native day-cycle generator (mode 2 above).
- `SmartSceneReconciler` re-times/prunes an *existing* (e.g. app-built) `smart_scene` to real sun. Empty-floor prune → `BLOCKED` (never PUTs an empty scene); timeslots whose scene rid is unresolved are preserved, not silently dropped; backs up before the PUT.

Bridge caps (probed live on a Bridge Pro, self-cleaning probe): a `smart_scene`
allows **max 6 timeslots** but **`transition_duration` ≥ 24h**, so native
smoothness comes from long fades, not many steps. Scenes store actions verbatim
(no store-time min-dim/gamut clamp).

Operational gotchas (all observed live on a Bridge Pro):
- **night_motion needs a TWO-pass apply** on cold start: pass 1 creates the zone (`apply --yes --ignore-unassigned`), pass 2 (fresh state) wires the automation. State is not refreshed mid-apply.
- **The MotionAware engine caches recalled scene ACTIONS at configuration load, not per recall.** After any apply that re-PUTs a scene the automation recalls (look change, member change), you MUST also re-PUT the `behavior_instance`'s own `configuration` (verbatim is fine) or the next recall applies the stale look and then recalls stop entirely — looks exactly like "MotionAware broken" while the instance reports enabled/running. An `enabled` false→true toggle does NOT flush the cache (and the enable PUT can return a bogus "instance doesn't support triggers" error). The reconciler only re-PUTs the automation on wiring changes, so scene-look-only applies need the manual kick.
- **A single MotionAware timeslot does NOT wrap past midnight** — it only governs
  `start → 00:00`, and with multiple slots the last wraps into the first, so the
  latest slot's reach extends until the earliest slot's start the next day. So
  `night_only` emits **three** timeslots: `night_start`, a `00:00` clone (covers
  the small hours), and an **actionless `day_start` slot (default 08:00)** that
  bounds the 00:00 slot — without it the 00:00 slot governs `00:00 → night_start`
  and dark evenings before the hand-off get red recalls + the auto `all_off`.
  The bridge schema requires `on_motion` with at least one action on every slot;
  the no-op is the string action `"do_nothing"`.
- **When the daemon drives the lights, a Home Assistant instance on the same
  LAN does NOT capture their state** (HA's recorder shows 0 changes while the
  daemon drives them). To inspect light state or confirm an automation fired,
  query the Hue CLIP API directly or read the daemon's log — never HA history.
- `apply` refuses on any `BLOCKED` change (unassigned lights, "zone/automation not found", an empty-floor prune), printing **each blocked change's real reason**; `--ignore-unassigned` bypasses all blocked changes (which is also what enables the two-pass cold start).
- Backups in `.hue-backup/` are **write-once** (`{id}-preapply.json`), so a daily re-apply can't overwrite the genuine pre-first-apply original.
- **DST-aware sun anchoring:** `location.tz` (IANA name, e.g. `America/Los_Angeles`) makes
  the apply/preview path AND the circadian daemon derive the UTC offset *for the date in
  question* via stdlib `zoneinfo`, so no manual `tz_offset_hours` flip at the DST
  change. Fixed `tz_offset_hours` remains the fallback; only the legacy `engine`/`watch`
  path still uses the fixed offset.

## TV-aware bias lighting — daemon-native bias hold

When the TV is on, a configured set of viewing lights holds a steady "TV mode"
bias look **while the rest of the home keeps driving the circadian curve**;
when the TV turns off each light returns to its configured idle behaviour
(`circadian` rejoins the curve, `off` goes dark). This is built **into the
circadian daemon** (`circadian_daemon.py` + pure `bias_control.py`), NOT as a
bridge scene — a single-grouped_light, suspend-on-override daemon can't hold a
sub-zone while the rest keeps moving. See
`docs/superpowers/specs/2026-06-28-daemon-tv-bias-hold-design.md` and
`2026-07-02-bias-edge-transition-design.md`.

How it works (config: `circadian_daemon.bias`):
- **Per-light** drive of the bias set; the daemon owns the look
  (`bias.lights[*].look`) and each light's `idle` (`circadian` or `off`).
- **No bias light may sit in the daemon's driven zone** or the 60 s
  grouped_light tick stomps its held look. Keep the driven zone and the bias
  set disjoint.
- **Pluggable on/off triggers** (`bias.triggers`, OR-combined, all optional):
  `probe` (the daemon TCP/ICMP-pings the TV itself — note some TVs hold their
  webOS port open in standby, defeating port probes), `control_file` (something
  touches `on_file`/`off_file` on a shared mount — e.g. a Home Assistant webOS
  integration that knows real TV power), and `sse` (a bridge scene/button
  recall the daemon sees on the event stream).
- **TV flips are edges**: they apply immediately with the short
  `bias.transition` fade and are INFO-logged with their source; the steady
  per-tick curve drive keeps the long fade. Failed edge writes (e.g. bridge
  "command queue is full") are retried on the next tick, and out-of-window
  `off` re-writes are suppressed after the first success.
- The main zone is untouched by all this; a manual override of the *main* zone
  never freezes the viewing lights (bias uses `controller.in_window/drive_to`).

## Security mode — daemon-native panic (escalating alert + capped chaos)

`security:` (top-level in the config) adds a manual panic that **preempts
circadian and bias** (`SECURITY` > bias > circadian). It runs an escalating
show built into the circadian daemon (`circadian_daemon.py` + pure
`security_control.py`):

- **Phase 1 ALERT** (`alert.seconds`): whole-home bright-red slow breathe — legible,
  so occupants instantly clock it.
- **Phase 2 CHAOS** (until disarm or `max_duration`): **per-light** hard cuts
  (`dynamics: 0`) — a rotating budget of `chaos.lights_per_frame` lights per frame, each
  jumping ~137.5° of hue per update (golden-angle: always near-complementary), brightness
  alternating out of phase. **Safety cap is luminance-only:** no light's brightness changes
  faster than `chaos.min_flash_interval` (hard floor 334 ms), keeping luminance flashing
  below the 3-30 Hz photosensitive-seizure band. The cap is a tested invariant in the pure
  layer; hue churn is deliberately NOT capped (that is where the violence lives).
- **Exit:** disarm via the same channels, or the `max_duration` cap; the exit
  consumes the arm signal (so an expiry can't re-fire the show) and one normal
  tick snaps every light back to its current circadian/bias look, with a
  settle-classification grace of one fade + settle window so the catch-up ramp
  is never misread as a manual override.
- Known limit: the CLIP REST path drops a meaningful share of frame writes at
  high frame rates (the daemon logs drop stats on exit); real per-frame control
  would need the Entertainment streaming API.

Triggers (OR-combined, no arming): `hueman security on|off` (writes the
`control_file` flags), and/or a Hue button/scene the daemon sees on SSE
(`triggers.sse`). On startup security is always OFF and stale control-files are cleared.

**Sound is decoupled (cue contract):** the daemon writes the phase string
`alert` / `chaos` / `clear` to `security.sound.cue_file` (and/or POSTs
`{"cue": <phase>}` to `sound.webhook`). It plays no audio itself — an external
adapter (e.g. Home Assistant) watches the cue file / webhook and plays the
matching sound. That adapter is out of this repo's scope.

## Architecture

The codebase is split into a **pure decision layer** (fully unit-tested, no I/O) and a **thin I/O layer** (needs a real bridge):

### Pure layer (unit-tested)

| Module | Role |
|--------|------|
| `config.py` | Parses/validates YAML into frozen dataclasses. `load_config()` is the entry point. `DEFAULT_MARKER = "[iac]"` tags bridge resources as iac-managed. |
| `sun.py` | NOAA sunrise/sunset math (`SolarCalculator`). |
| `circadian.py` | Maps time-of-day + `SunTimes` → colour temperature + brightness (`CircadianCurve`). |
| `circadian_scene.py` | Pure generator: samples `CircadianCurve` at ≤6 sun-anchored knee times into `CircadianStep`s for the generated smart-scene cycle. |
| `circadian_control.py` | Pure daemon state machine (`CircadianController`): drive window, `DriveTo`/`FadeOff`/`Hold` decisions, settle-and-compare override detection. |
| `bias_control.py` | Pure TV-bias core: `bias_actions` (per-light hold/drive/off with edge-aware fades) + `TriggerAggregator` (OR-combined, debounced trigger sources). |
| `security_control.py` | Pure security-mode core: `SecurityController` (ALERT breathe → luminance-capped CHAOS frames). |
| `engine.py` | Per-area state machine (`PolicyEngine`) for the legacy `watch` runtime. Consumes `MotionPolicy` + explicit `ts` floats; emits `Action` values. Four phases: `STANDBY`, `ACTIVE`, `DIMMING`, `OVERRIDDEN`. |
| `payload.py` | Converts engine `TargetState` → CLIP v2 request bodies (sRGB `#rrggbb` → CIE xy). |
| `nightmotion.py` | Pure night-motion helpers: builds CLIP `scene` bodies, transforms the MotionAware `behavior_instance`, and `scene_actions_match` (tolerant scene-look diff, reused by the circadian reconciler). |
| `reconcile.py` | Terraform-style planner. `Planner` runs `AreaReconciler` (room/zone membership), `SensitivityReconciler` (sensor sensitivity), `SmartSceneReconciler` (re-time an existing scene), `CircadianSceneReconciler` (generate the smooth circadian cycle), and `NightMotionReconciler` (night soft-red guidance). `Change.change_type` is one of `CREATE`, `UPDATE`, `NOOP`, `BLOCKED`. |

### I/O layer

| Module | Role |
|--------|------|
| `client.py` | CLIP API v2 HTTP client (`HueClient`). |
| `pin.py` | Trust-on-first-use TLS pinning (SHA-256 of bridge cert → `.hue-pin.json`). |
| `state.py` | Loads live bridge state; resolves resource names → ids (`BridgeState`), including MotionAware areas/services. |
| `circadian_daemon.py` | The resident daemon (`CircadianDaemon`): 60s curve ticks + SSE event loop + TV-bias triggers (probe thread / SSE / control files) + security show, all serialised under one lock. |
| `watch.py` | **Legacy** SSE event loop (`MotionController`) for `motion_policies`. Its echo-buffer override detection predates the bridge's periodic re-emission of settled values (the daemon's settle-and-compare replaced it), and it requires legacy PIR sensors. |
| `cli.py` | `argparse`-based CLI (`Cli`). Subcommands: `auth`, `validate`, `preview`, `inventory`, `plan`, `apply`, `circadian run\|resume`, `security on\|off\|status`, `watch`. |

### Data flow

```
YAML config → config.py → Config (frozen dataclasses)
                                     │
             BridgeState ◄──── client.py ◄── Hue Bridge
                                     │
reconcile.py (plan/apply) ◄──────────┤
                                     │
circadian_daemon.py ◄── SSE events + probe + control files
    │  ├─ circadian_control.py (curve ticks, override detection)
    │  ├─ bias_control.py (TV-bias decisions)
    │  └─ security_control.py (panic show frames)
    └── targets ──► payload.py ──► client.py ──► Hue Bridge

engine.py (PolicyEngine) ◄── watch.py (SSE events)   [legacy path]
```

### Test helpers (`tests/conftest.py`)

- `epoch_at(hour, minute, date=...)` — converts local wall-clock time to epoch seconds using `TZ_OFFSET = -5.0`. Default date is 2026-06-21 (summer solstice, stable sunrise/sunset).
- `make_config(policy_dict)` — wraps a single policy dict into a full `Config` for unit tests.

### Key design constraints

- `PolicyEngine` is I/O-free; time is always passed as an explicit `float` epoch seconds so tests drive it directly without mocking `time`.
- `plan()` is read-only and safe to run repeatedly; `apply()` is idempotent.
- A light can belong to exactly one room but any number of zones (validated at parse time in `config.py`).
- Bridge resources created by `apply` carry `DEFAULT_MARKER = "[iac]"` in their metadata name so they can be identified and managed across runs.
- `watch` maintains an echo buffer (`_ECHO_TTL_SECONDS = 4.0`) so `grouped_light` events caused by its own writes are not misread as manual overrides.
