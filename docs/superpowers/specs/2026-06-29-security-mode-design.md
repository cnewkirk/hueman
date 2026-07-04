# Security mode — Design

- **Date:** 2026-06-29
- **Status:** implemented; chaos reworked 2026-07-02 (see addendum at end)
- **Branch:** `feature/security-mode` (off `origin/main` @ `020d5d9`, the PR #12 config-sync)
- **Bridge:** Hue Bridge Pro (MotionAware) — see `CLAUDE.md`
- **Builds on:** the persistent circadian daemon (`circadian_daemon.py`) and its
  daemon-native bias hold (`bias_control.py`, `2026-06-28-daemon-tv-bias-hold-design.md`).
  Security mode reuses the daemon's pure/I-O split, `TriggerAggregator`, and
  per-resource write path.

## 1. Problem & intent

A single, deliberately-triggered "something is wrong" mode for the apartment that
does two things at once:

- **(a) Alert the occupants** — make it instantly, unmistakably clear that
  something unexpected is happening.
- **(b) Disorient an intruder** — drive the lights (and, via a decoupled cue,
  sound) into a chaotic, patternless state that is hard to think or move through.

These are served in sequence: a short, *legible* alert first (so the people who
live here immediately understand), escalating into *capped chaos* if it is not
disarmed. The Hue bridge does no audio, so sound is handled outside this codebase
(HA → Sonos) and the daemon only emits per-phase cues.

## 2. Decisions (locked during brainstorming)

| # | Decision | Rationale |
|---|----------|-----------|
| D1 | **Escalating sequence:** Phase 1 `ALERT` (legible) → Phase 2 `CHAOS` (disorienting), if not disarmed within `alert.seconds`. | Serves (a) then (b) with one mode. User choice. |
| D2 | **Sound is decoupled:** the daemon writes a per-phase *cue* (`alert`/`chaos`/`clear`) to a file (or optional webhook); the existing **HA → Sonos** stack plays audio. | The bridge/codebase is lights-only. Zero daemon→audio coupling; lights ship now, audio is a thin HA-side adapter. User choice. |
| D3 | **Manual, pluggable trigger:** fire/disarm via CLI, a control-file, and/or a Hue button/scene seen on the SSE stream, OR-combined (`TriggerAggregator`). **No arm/disarm state.** | Zero false positives; deliberate panic action. Automated detectors (HA/motion) can be added later as additional sources without redesign. User choice. |
| D4 | **Aggressive but safety-capped chaos:** full color-thrash + out-of-phase zone alternation + brightness swings, but **all luminance flashing kept out of the ~3–30 Hz photosensitive-seizure band** by a hard, tested cap. | Occupants are present (that is goal *a*). Disorientation comes from color churn + patternless alternation, not dangerous strobe. User choice; cap configurable but floored. |
| D5 | **Disarm + hard max-duration failsafe:** stop via the same channels it started, or auto-stop at `max_duration`; then **snap lights back to normal** circadian/bias. | A stuck panic trigger strobing indefinitely is its own hazard. User choice. |
| D6 | **Daemon-native (one process owns the lights), REST transport.** Security is a top-priority state that preempts bias and circadian. | Same reason bias became daemon-native (PR #7): a separate process would fight the daemon's 60 s writes. REST `grouped_light` is enough because the safety cap removes the need for high-Hz strobe; Hue Entertainment/DTLS streaming is deferred. User choice (Approach A). |

## 3. Goals / non-goals

**Goals**
- A deliberately-triggered mode that runs `ALERT` → `CHAOS` across the whole apartment.
- Legible alert phase (occupants clock it instantly); disorienting-but-safe chaos phase.
- Manual triggers, OR-combined and configurable; disarm via the same channels.
- Hard `max_duration` failsafe; clean snap-back to normal on exit.
- Per-phase sound cues emitted for an external (HA) audio adapter.
- Fully unit-tested pure core, including the safety cap as a tested invariant.

**Non-goals**
- **Automated intrusion detection / arming.** Triggers are pluggable so an HA or
  MotionAware detector can be added later, but v1 is manual only.
- **Playing the sound.** The daemon emits cues; HA → Sonos is a separate, out-of-repo adapter.
- **Hue Entertainment / DTLS high-Hz streaming** ("brutal mode") — deferred; REST for v1.
- **Per-light effects / content sync.** Security drives a few `grouped_light`s, not 12 lights.
- **Honoring a manual light tweak mid-show.** Only the disarm channels or the cap stop it (by design).

## 4. Behavior contract

| Phase | When | Lights | Cue |
|---|---|---|---|
| `ALERT` | trigger fires → `alert.seconds` | All groups synchronized: bright red, slow breathe (`alert.min_brightness`↔100 at `alert.breathe_hz`, default 0.5 Hz). Legible "alarm." | `alert` |
| `CHAOS` | after `ALERT`, until disarm or `max_duration` | Per group: hue thrashes every frame; brightness (the luminance driver) alternates bright↔dim **only on a per-group schedule ≥ `min_flash_interval` apart**, groups offset out of phase. Patternless, busy, never a dangerous strobe. | `chaos` |
| `CLEAR` | disarm channel or `max_duration` | Daemon forces an immediate normal re-evaluation (circadian + bias write once) → every light snaps to its current correct look. No leftover red. | `clear` |

Priority while active: **`SECURITY` > bias > circadian.** A manual override of any
light mid-show does **not** disarm (security ignores the circadian override
detector); only `off_file` / `off_trigger` / `security off` / the cap stop it.

## 5. Architecture (preserves the pure / I-O split)

### Pure layer (unit-tested, no clock / no I/O) — `security_control.py`

- **`SecurityController`** — deterministic decision core, seeded for reproducibility:
  - `phase_at(elapsed_ms) -> "alert" | "chaos"` — `alert` while `elapsed_ms < alert_ms`, else `chaos`.
  - `is_expired(elapsed_ms) -> bool` — `elapsed_ms >= max_duration_ms` (the failsafe).
  - `frame_at(elapsed_ms, frame_index) -> SecurityFrame` — the per-frame light state.
- **`SecurityFrame`** — `{ phase, targets: list[GroupTarget], cue: str | None }`,
  where `GroupTarget = (group_name, TargetState)` reusing `engine.TargetState`
  (on / brightness / hex). `cue` is set only on the first frame of a phase.
- **Safety cap is enforced *here* and is the central tested invariant:**
  the controller derives `flash_period_frames = max(1, ceil(min_flash_interval_ms /
  frame_interval_ms))` and toggles each group's bright/dim level **only at multiples
  of that period**, with a per-group offset. Hue may change every frame; **brightness
  changes only on this schedule**, so no group's luminance flashes faster than
  `1 / min_flash_interval` Hz. With the default 350 ms that is < 3 Hz — below the
  danger band. The alert breathe is likewise floored (`breathe_hz ≤ 2`).
- **`TriggerAggregator`** — reused verbatim from `bias_control.py`: folds on/off
  edges from N sources into one debounced `security_on` (injected `now`).

The pure core knows nothing about circadian, the clock, or the bridge — only
`(elapsed, frame_index)` → frame. Restoring "normal" on exit is the daemon's job
(it re-runs its existing circadian/bias path), so the core stays fully decoupled.

### I/O layer (thin) — extends `circadian_daemon.py`

- **Two loops.** The existing **slow loop** (~60 s: circadian + bias + SSE) is
  unchanged. A new **fast loop** is entered only while `security_on`:
  ```
  start = now()
  frame_index = 0
  while security_on and not controller.is_expired(now() - start):
      frame = controller.frame_at((now()-start)*1000, frame_index)
      if frame.cue: write_cue(frame.cue)
      for (group, state) in frame.targets:
          try: client.update_resource("grouped_light", rid[group],
                                       GroupedLightCommand.build(state))
          except BridgeError: log.debug(...)        # a dropped frame must not crash the show
      frame_index += 1
      sleep(frame_interval_ms)
  # CLEAR:
  write_cue("clear"); reset_control_files()
  run_normal_tick_once()                            # snap-back: circadian + bias re-assert
  ```
- **Triggers** feed a dedicated security `TriggerAggregator`:
  - `control_file` — `on_file` present → on, `off_file` present → off, then unlink
    (idempotent). Polled in the slow loop **and** re-checked each fast-loop iteration
    so disarm-by-file works mid-show. The CLI writes these.
  - `sse` — reuses the daemon's existing SSE routing; `on_trigger` / `off_trigger`
    resolve a scene/button name → rid exactly like the existing resume/bias triggers.
    SSE events arrive on the SSE thread and flip the aggregator while the fast loop
    runs, so a physical panic button disarms mid-show. (Aggregator guarded by a lock,
    as the bias one already is.)
- **Cues** — `write_cue(name)` writes the phase string to `sound.cue_file`
  (and/or POSTs `sound.webhook`). The daemon's entire audio responsibility.
- **Startup** — security is always OFF on boot; a stale `on_file` (mtime older than
  `max_duration`) is removed; security never auto-enters from persisted state.

## 6. Configuration (`hue.yaml`, block style, defaults at parse time)

A new **top-level `security:`** block (not nested under `circadian_daemon:` — it
preempts the curve rather than using it) → a frozen `SecuritySpec` on `Config`.
Reuses `LightState`, `parse_duration`, `_as_dict`, `_opt_str` and the `BiasSpec`
trigger shape.

```yaml
security:
  groups:                 # whole-home; >=2 to alternate. Reuse existing zones.
    - Night Guide
    - TV Viewing
  alert:
    seconds: 10
    color: "#ff0000"      # bright-red slow breathe
    min_brightness: 40    # breathe floor (%); peaks at 100
    breathe_hz: 0.5       # <= 2 (validated; far below the seizure band)
  chaos:
    frame_interval: 250ms # ~4 fps over REST
    min_flash_interval: 350ms   # HARD CAP: any group's bright<->dim stays <3 Hz
  max_duration: 10m       # failsafe auto-stop
  triggers:               # OR-combined, all optional (mirrors bias)
    control_file:
      on_file: logs/.security-on
      off_file: logs/.security-off
    sse:
      on_trigger: "Panic On"    # a Hue button/scene name seen on the SSE stream
      off_trigger: "Panic Off"
    debounce: 0s          # panic fires instantly
  sound:
    cue_file: logs/.security-cue   # daemon writes: alert | chaos | clear
    # webhook: http://homeassistant:8123/api/webhook/security_cue   # optional alternative
```

**Validation** (eager, at parse time):
- `groups` non-empty; **warn** (not error) if `< 2` — alternation needs two groups.
- `alert.seconds > 0`; `0 <= alert.min_brightness <= 100`; `0 < alert.breathe_hz <= 2`.
- `alert.color` a valid 6-digit hex.
- `chaos.frame_interval >= 50ms`.
- `chaos.min_flash_interval >= 334ms` — **hard error** if lower, so the config
  cannot select into the 3 Hz danger floor.
- `max_duration > alert.seconds`.
- Every trigger sub-block optional; if none enabled, `security` is a valid dormant
  state and the daemon logs a startup warning (it can never fire).
- Group names validated against the bridge at startup (like `unknown_bias_lights`):
  all unknown names listed at once.

## 7. Data flow

```
panic ──(CLI write-file │ HA touch on_file │ Hue button on SSE)──► security TriggerAggregator ──► security_on
  on-edge  ⇒ daemon enters fast loop:
              ALERT (alert.seconds): all groups red slow-breathe ........... cue: alert
              CHAOS (until disarm/cap): per-group hue-thrash + offset,
                                        flash-capped brightness alternation .. cue: chaos
  disarm (off-file │ off_trigger │ CLI off) OR elapsed >= max_duration:
           ⇒ CLEAR: cue: clear; reset control-files; one normal tick re-asserts circadian+bias
  main/slow loop ⇒ suspended while security_on (the fast loop owns the lights)
```

HA's job, if used: one action per edge (touch `on_file`/`off_file`, or recall the
`on_trigger`/`off_trigger` scene), plus the cue→Sonos adapter that watches
`cue_file`. The daemon owns the light show.

## 8. Failure modes & reversibility

| Mode | Handling |
|------|----------|
| Transient bridge error / 429 mid-show | Fast loop swallows `BridgeError` per frame (debug-log, continue); a dropped frame is invisible. Optional small backoff if repeated. |
| Daemon crashes mid-show | Lights freeze on the last *frame* (a color, not a strobe). On restart security is OFF and a stale `on_file` is cleared → no re-fire / no loop. |
| Stuck / forgotten trigger | `max_duration` cap auto-stops and snaps back regardless of triggers. |
| All triggers quiet | `security_on` holds last state; in `CHAOS` that means it runs until the cap (by design). |
| Manual light tweak while active | Ignored (only disarm channels / cap stop it) — documented non-goal. |
| SSE drop / bridge unreachable | Existing reconnect/backoff; the fast loop's per-frame writes simply log-and-continue. |
| Misconfigured into danger band | Impossible: `min_flash_interval >= 334ms` is a hard parse error; `breathe_hz <= 2` validated. |

## 9. Testing

- **`tests/test_security_control.py`** (pure): phase boundary at `alert.seconds`;
  expiry at `max_duration`; seeded frame determinism (same seed+index → same frame);
  **the strobe-cap invariant** — over a long synthetic sequence, assert no group's
  brightness toggles bright↔dim closer than `flash_period_frames`; alert breathe
  shape + floor; cue set only on phase-entry frames.
- **`tests/test_config.py`** (additions): `SecuritySpec.parse` happy path; each
  validation error including the `min_flash_interval` floor and `breathe_hz` cap;
  bool-coercible handling; durations; dormant-no-trigger warning.
- **`tests/test_circadian_daemon.py`** (additions): trigger edge enters `SECURITY`
  and preempts circadian/bias; disarm-by-file and cap exits; snap-back runs one
  normal tick; startup clears a stale `on_file`; transient `BridgeError` per frame
  is swallowed. Uses the existing injected-`now` / fake-client harness.
- **`tests/test_cli.py`** (additions): `security on|off|status` write/clear the
  control-files and report state.
- **Live validation** (documented, run during implementation per the "probe live"
  tradition; no bridge calls during design): confirm `frame_interval` × N groups
  does not draw sustained 429s; eyeball the cap and the alert/chaos looks; verify
  `cue_file` write and HA pickup. If REST proves too slow/janky, that is the signal
  to revisit Entertainment streaming (out of v1 scope).

## 10. Deliverables (file plan)

- `hue_iac/security_control.py` **(new, pure)** — `SecurityController`,
  `SecurityFrame`, `GroupTarget`, the safety-capped generator.
- `hue_iac/config.py` — `SecuritySpec` (frozen) + `.parse()`/validation; `security`
  field on `Config`.
- `hue_iac/circadian_daemon.py` — fast loop, `SECURITY` preemption, security
  `TriggerAggregator` wiring (control-file + SSE), cue emission, snap-back, startup
  stale-file clearing.
- `hue_iac/payload.py` — reuse `GroupedLightCommand` (frames emit hex →
  `ColorConverter.hex_to_xy`). Add an `hsv→hex` helper only if the generator emits HSV.
- `hue_iac/cli.py` — `security on | off | status` (write/clear control-files;
  mirrors `circadian resume`).
- `tests/` — `test_security_control.py` (new) + additions to `test_config.py`,
  `test_circadian_daemon.py`, `test_cli.py`.
- `hue.yaml` (live deploy edit, like `bias:`) — top-level `security:` block.
- `CLAUDE.md` — document security mode + the **cue contract** (`alert`/`chaos`/`clear`)
  that HA consumes.
- `docs/superpowers/specs/2026-06-29-security-mode-design.md` — this spec.

## 11. Open items (resolved at implementation, not blockers)

- **O1** — Pick the live `security.groups` for full whole-home coverage
  (`Night Guide` + `TV Viewing` assumed; consider adding `Entry` / an `All` group).
- **O2** — Choose the shipped trigger for the live `hue.yaml`: CLI/control-file
  (zero hardware) vs a physical Hue button/scene via SSE (needs a button paired on
  the bridge). Both can ship; pick the default.
- **O3** — Tune the alert look and the chaos palette on the real bridge.
- **O4** — Validate REST fps headroom live (`frame_interval` × N groups without
  sustained 429s); if insufficient, revisit Entertainment streaming.
- **O5** — Build the HA cue→Sonos adapter (separate, out-of-repo effort) that
  watches `cue_file` and plays the matching audio.

## Addendum — 2026-07-02 chaos rework (user: "way more violent")

Live testing found chaos read as a **gradual colour drift**, not violence. Three
causes, all fixed:

1. **Frames were sent without `dynamics`**, so bulbs applied their default
   ~400 ms fade and blended every step. Chaos frames now carry
   `dynamics.duration: 0` (hard cuts); alert frames fade over one
   `frame_interval` (a smooth breathe is desirable *there*).
2. **The "fix-wave" commit had over-tightened the cap to hue+brightness.** The
   cap is restored to D4's original intent: **luminance-only**. Hue now jumps on
   every update via a golden-angle progression (~137.5° per step — always
   near-complementary, never neighbouring shades, deterministic).
3. **Two whole-group blobs became a per-light patchwork.** The daemon resolves
   the member lights of `security.groups`; chaos updates a rotating budget of
   `chaos.lights_per_frame` (default 3) individual lights per frame, so the
   whole apartment churns decorrelated while total write rate stays inside the
   bridge's REST budget. A light's brightness still flips no faster than
   `min_flash_interval` — enforced against wall time across the rotation
   schedule and covered by tests. The ALERT phase remains whole-group.

`GroupFrame` became `FrameTarget(kind, name, target)` with kind
`"group" | "light"`. If REST-budget violence is still insufficient, the next
step remains the deferred Hue Entertainment/DTLS streaming engine ("brutal
mode") — per-light frames at 25–50 Hz with the same luminance-cap invariant.
