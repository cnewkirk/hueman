# Daemon-native TV bias hold — Design

- **Date:** 2026-06-28
- **Status:** awaiting user review
- **Branch:** `daemon-tv-bias` (off `origin/main` @ the PR #6 daemon merge)
- **Bridge:** Hue Bridge Pro (MotionAware) — see `CLAUDE.md`
- **Supersedes the delivery half of:** `2026-06-27-tv-bias-lighting-design.md` (the
  scene + `SceneReconciler` + HA-`scene.turn_on` approach). The *intent* there is
  unchanged; only the mechanism changes to fit the circadian **daemon** (PR #6).

## 1. Problem & intent

When the LG OLED TV is on, the viewing-area lights should hold a steady "TV mode"
bias look, **while the rest of the apartment keeps winding down on the circadian
curve.** When the TV turns off, the viewing-area lights **return to what they
would have been doing** — the live circadian look by day, off at night. Verbatim:
*"if i was watching something and up late, it would continue to be in 'tv mode'
but all the other stuff would continue to wind down around me."*

## 2. Why the original TV-bias design no longer fits (the daemon mismatch)

The original design (D3) **carved the viewing lights out of night-red but kept
them in circadian** — possible only because circadian *was* a native `smart_scene`
(a light could be in the circadian scene set *and* a separate "TV Viewing" zone).

PR #6 replaced that with a **persistent daemon** that:
- drives **one `grouped_light`** for the whole "Night Guide" zone, every tick, and
  is **explicitly single-zone** (multiple zones were out of scope);
- **suspends on any change to that zone it did not make** (settle-and-compare).

So a "TV Mode" look on the couch strip (which lives in "Night Guide") would trip
the override detector and **suspend the entire zone — freezing the rest of the
apartment**, the opposite of the requirement. And pulling the viewing lights out
of the zone to avoid that would strip them of circadian entirely. The two halves
can't both hold under a single whole-zone driver. (Confirmed against the live
`hue.yaml`, `circadian_daemon.py`, and `circadian_control.py` on `main`.)

## 3. Decisions (locked during brainstorming)

| # | Decision | Rationale |
|---|----------|-----------|
| D1 | **Full fidelity:** the rest of the apartment keeps moving on the curve while the TV is on. | The verbatim requirement. Rules out a whole-zone suspend. |
| D2 | **Daemon-native bias hold:** the daemon owns the bias look and holds the viewing lights there; no bridge "TV Mode" scene. | User choice ("most integrated"). HA shrinks to a pure on/off signal. |
| D3 | **Per-light driving for the bias set.** | Lets the Play bars idle **off** while couch/tree idle on **circadian** — impossible with one grouped_light for the set. |
| D4 | **Pluggable, configurable triggers:** SSE bridge-trigger, shared control-file, and self-probe are all configurable; any enabled subset is OR-combined. `probe` ships `enabled: false`. | User choice ("leave the door open for all"). |
| D5 | **Light split:** remove the viewing lights from the "Night Guide" zone. | One config edit shrinks the daemon's main grouped_light *and* removes them from night-red's reach (no mid-movie flash — the old D3 win, now structural and free). |

## 4. Goals / non-goals

**Goals**
- TV on → viewing lights hold the bias look; the rest of the apartment keeps driving the curve.
- TV off → viewing lights resume their idle behavior (per-light: circadian-in-window, or off).
- Works late at night: TV on after hand-off → bias holds while the main set is off / night-red.
- Triggers are configurable; no mechanism is hard-wired.

**Non-goals**
- Dynamic content-sync (Sync Box territory). Steady bias only.
- Absorbing TV detection into the daemon (the `probe` source is offered but off by default; HA stays the detector unless the user enables `probe`).
- Honoring a *manual* tweak of the viewing lights *while the TV is on* (v1 re-asserts the bias look each tick — see §9).
- Multi-zone *circadian* in general (we add a bias **set**, not a second curve-driven zone).

## 5. Behavior contract

Per-light `idle` is what makes full fidelity work:

| Fixture (`idle`) | TV **on** | TV **off**, in window (sunrise→22:34) | TV **off**, night |
|---|---|---|---|
| Play bars (`idle: off`) | bias look | **off** | off |
| Couch / Tree (`idle: circadian`) | bias look | **follow the curve** (same sample as main) | off (fade at hand-off) |
| Rest of "Night Guide" | curve (untouched) | curve | off / night-red |

## 6. Architecture (preserves the pure / I-O split)

### Pure layer (unit-tested, no clock/IO)
- **Bias decision** — a pure function/small controller: given `(tv_on, in_window,
  per-light cfg, current curve sample)` → a per-light action map of
  `BiasHold(look)` · `DriveTo(curve)` · `Off`. The curve sample is the *same*
  `CircadianController` output the main set uses, so "idle: circadian" tracks the
  main set exactly.
- **Trigger aggregator** — pure: folds on/off edges from N sources into a single
  `tv_on` bool with optional debounce (injected `now`). "Any enabled source on" →
  on; precedence/edge rules are explicit and tested.

### I/O layer (thin) — extends `circadian_daemon.py`
- **Source adapters** behind one interface:
  - `sse` — reuses the existing SSE routing; resolves `on_trigger`/`off_trigger`
    (scene/button name→rid, exactly like the existing `resume_trigger`) and emits
    on/off when those events arrive.
  - `control_file` — reuses the existing per-tick control-file poll: `on_file`
    present → on, `off_file` present → off, then unlink (idempotent).
  - `probe` — a small reachability thread (ICMP `ping`, or a TCP connect to e.g.
    webOS :3001) with `interval` + `debounce`; emits on/off. **`enabled: false`
    by default.**
- **Per-light bias writes** — each tick, after the main grouped_light write, the
  daemon resolves each bias light name→`light_rid` (via `BridgeState`) and PUTs
  its action to `/clip/v2/resource/light/<rid>`. The body reuses the existing
  `payload` builder (the on/dimming/color(_temperature)/`dynamics.duration` body
  is resource-agnostic; we generalize `GroupedLightCommand.build` to serve both
  `grouped_light` and `light`, or add a thin sibling).
- The **main set is unchanged** (same controller, same grouped_light, same
  override/resume) — it just covers fewer lights after the split.

## 7. Configuration (`hue.yaml`, all tunable, defaults applied at parse time)

New `bias:` block under `circadian_daemon:` → a frozen `BiasSpec` on
`CircadianDaemonSpec` (mirrors the existing config pattern; no magic numbers):

```yaml
circadian_daemon:
  zone: "Night Guide"            # main set — now EXCLUDES the viewing lights
  # ...existing daemon keys unchanged...
  bias:
    lights:                      # per-fixture: hold look + idle behavior
      "Play bars":        { look: { mirek: 153, brightness: 28 }, idle: off }
      "Couch Lightstrip": { look: { hex: "1a0a00", brightness: 5 }, idle: circadian }
      "Tree Left":        { look: { mirek: 400, brightness: 18 }, idle: circadian }
      "Tree Right":       { look: { mirek: 400, brightness: 18 }, idle: circadian }
    triggers:                    # all optional; enabled sources OR-combine into tv_on
      sse:          { on_trigger: "TV On", off_trigger: "TV Off" }  # scene/button name or rid
      control_file: { on_file: ".tv-on", off_file: ".tv-off" }
      probe:        { enabled: false, host: 192.0.2.15, mode: tcp, port: 3001,
                      interval: 5s, debounce: 5s }
```

Validation: `bias.lights` non-empty; each `look` is a static colour (reject
`circadian` colour, as `SceneSpec` already does) + brightness 0–100; `idle ∈
{circadian, off}`; every trigger sub-block optional. If `bias` is present but no
source is enabled, the daemon logs a startup warning and bias simply never engages
(valid dormant state).

Also in `hue.yaml` (a deploy edit, applied via the existing `AreaReconciler`):
remove the viewing lights from the `Night Guide` zone membership.

## 8. Data flow

```
TV power ──(detector: HA ping, or daemon probe)──► trigger source ──► aggregator ──► tv_on
  on-edge  ⇒ bias set → hold each light's `look` (per-light, with transition)
  off-edge ⇒ bias set → idle: circadian sample if (idle==circadian and in_window) else off
  main set ⇒ ALWAYS the curve / hand-off, unaffected by tv_on
```

HA's job (if used) is one line per edge: recall the `on_trigger`/`off_trigger`
(SSE source) or touch the `on_file`/`off_file` (control-file source). The daemon
owns the look.

## 9. Failure modes & reversibility

| Mode | Handling |
|------|----------|
| All trigger sources quiet | `tv_on` holds last state; bias holds or idles accordingly. |
| Probe error / host flaps | Treat as last-known; `debounce` smooths flaps; never crashes the tick. |
| SSE drop / bridge unreachable | Existing reconnect/backoff; per-light write failures logged + retried next tick (as the main set already does). |
| Manual tweak of a viewing light while TV on | v1: daemon re-asserts the bias look next tick (documented non-goal). |
| Zone-membership edit | Applied via existing `apply` (write-once backup); re-adding the lights fully reverts. |

## 10. Testing

- **Pure:** bias action map across `tv_on × in/out-window × idle∈{circadian,off} ×
  per-fixture`; trigger aggregation + debounce + precedence; config parse/validate
  (valid looks, rejected circadian colour, idle enum, optional triggers).
- **Mocked-IO:** each source adapter flips `tv_on`; per-light writes issued to the
  right rids with the right bodies; main grouped_light write unchanged; no write
  when nothing changed.
- **Live checklist:** TV on/off in daytime (viewing holds bias / rejoins curve,
  rest keeps moving); TV on after 22:34 (bias holds, main off/night-red, no red
  flash on viewing lights); each enabled trigger source exercised.

## 11. Deliverables (file plan)

- `config.py` — `BiasSpec` (+ per-light `BiasLight`) frozen dataclasses; `bias`
  field on `CircadianDaemonSpec`; parse/validate.
- `circadian_control.py` (or a sibling `bias_control.py`) — the pure bias decision
  + trigger aggregator.
- `circadian_daemon.py` — source adapters (sse/control_file/probe), per-light bias
  writes each tick, `tv_on` wiring.
- `payload.py` — generalize the body builder for per-light writes.
- `tests/` — pure + mocked-IO coverage above.
- `hue.yaml` (live, untracked deploy edit) — `bias:` block + remove viewing lights
  from `Night Guide`.
- `homeassistant/` — trim automations to the chosen signal (recall trigger scene
  *or* touch control file); update README.
- `CLAUDE.md` — document daemon-native bias hold + the pluggable trigger sources.

## 12. Open items (resolved at implementation, not blockers)

- **O1** — Pin exact Hue light names (Play bars, Tree L/R) and add the Play bars to
  the IaC via `inventory`.
- **O2** — Tune per-fixture `look` (mirek/hex/brightness) on the real bridge.
- **O3** — Decide the default shipped trigger source (SSE vs control-file) for the
  live `hue.yaml`; `probe` stays off until a host is set.
- **O4** — Confirm couch/tree should idle on circadian (vs off) — assumed yes per D-table.
