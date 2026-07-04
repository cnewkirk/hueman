# TV-Aware Bias Lighting — Design

- **Date:** 2026-06-27
- **Status:** Approved (brainstorming), pending spec review → implementation plan
- **Branch:** `worktree-tv-bias-lighting` (off `origin/main`)
- **Bridge:** Bridge Pro (MotionAware) — see `CLAUDE.md`

## 1. Problem & intent

When the LG OLED TV is on, the user wants the lights around the viewing area to
drop into a steady "TV mode" tuned for watching — a bias backlight behind the TV
plus glare reduction on the nearby fixtures — **while the rest of the apartment
keeps winding down on its own** (native circadian by day, soft-red motion
guidance at night). When the TV turns off, the viewing-area lights return to
**what they would have been doing had the TV never been on** (the time-of-day
default), not a blunt "off".

Verbatim driving requirement: *"if i was watching something and up late, it
would continue to be in 'tv mode' but all the other stuff would continue to wind
down around me."*

## 2. Key finding that shapes the design (researched, not assumed)

A Hue Bridge Pro **cannot natively know the TV is on** and hold a steady scene:
the entertainment subsystem and the automation engine are decoupled, and a plain
bias light (steady backlight) is explicitly *not* what a Hue Play HDMI Sync Box
does (the Sync Box only does dynamic content-sync). Therefore TV-on detection
**requires an external watcher** — there is no pure-native, no-daemon path. This
is the first feature in this repo that is **not** "native on the bridge, no
daemon," and that is called out honestly in the docs (§10).

Sources consulted during brainstorming: Signify CLIP v2 docs, `aiohue`
(`entertainment_configuration` = `status: active/inactive` + `active_streamer`),
`aiowebostv` (webOS `getPowerState`, and "off" surfacing as the NIC dropping),
HA `webostv`/`hue`/`ping` integrations, Pulse-Eight libCEC.

## 3. Decisions (locked during brainstorming)

| # | Decision | Rationale |
|---|----------|-----------|
| D1 | **Detection: Home Assistant in a container on the Synology**, using the built-in **`ping` binary_sensor** against the TV's IP. | The user's idea: a TV in deep standby tears down its NIC, so "unreachable" ≈ "off". Config-only, no webOS pairing/`client-key`, nothing to rot on a firmware update. More robust for the *off* edge than `webostv` (which inherits webOS standby flakiness). |
| D2 | **Bias look is a tiered "TV Mode" scene**, not a uniform zone. | The viewing area has fixtures with different jobs (see §5). |
| D3 | **Carve viewing-area lights out of night-red only; keep them in circadian.** | Night-red is motion-triggered and frequent — it would flash the bias light red mid-movie. Removing those lights from the `Night Guide` zone fixes that *structurally*. Keeping them in circadian preserves a daytime default to return to (D4). |
| D4 | **TV-off → return to native default** (circadian by day, dark at night; Play bars off). | The user's refined requirement: the counterfactual "what it would have been". |
| D5 | **The "TV Mode" look is a bridge `scene` authored in IaC** (new `scenes:` config surface + a `SceneReconciler`). | Keeps the look in git, makes it tappable in the Hue app, and reduces HA to a one-line `scene.turn_on`. Composes with the release logic (HA already does scene recalls). |
| D6 | **Runtime: HA container, `network_mode: host`.** | Required for in-container ICMP; also the recommended HA-Docker mode and what makes bridge/TV discovery work. |

## 4. Goals / non-goals

**Goals**
- TV on → viewing-area lights enter the tiered "TV Mode" look.
- TV off → viewing-area lights resume their time-of-day native default.
- The rest of the apartment is never touched by this feature.
- No mid-movie red flash from the night-red automation.
- The "look" is version-controlled IaC; HA holds only the trigger.

**Non-goals**
- Dynamic content-sync (Sync Box territory). This is *steady* bias lighting.
- Detecting *what* is on screen, or which HDMI input is active.
- Making the bridge natively TV-aware (established impossible — §2).
- Waking the TV (we only observe it).

## 5. The lighting model

### 5.1 Fixtures and their TV-Mode targets (starting values; tunable on-bridge)

| Fixture (physical) | Role in TV mode | Target (starting point) |
|--------------------|-----------------|--------------------------|
| **Play bars** (behind TV) | Bias reference | ~6500 K (~153 mirek), low brightness (~25–30%) |
| **Couch lightstrip** (behind couch, in eyeline) | Glare kill | Very low (~3–8%), warm |
| **Light tree — Left** (near TV) | Glare/distraction down | Dimmed low (~15–20%), warm |
| **Light tree — Right** (near TV) | Glare/distraction down | Dimmed low (~15–20%), warm |

Exact Hue resource names (esp. the Play bars and which fixtures are the "tree
L/R") are **pinned by running `hue-iac inventory` against the live bridge**, not guessed —
per the repo's verification discipline. Brightness/mirek values are tunable.

### 5.2 Zones (managed by the existing `AreaReconciler`, via `areas.zones`)

- **New `TV Viewing` zone** = { Play bars, Couch lightstrip, Tree Left, Tree
  Right }. This is the group HA targets.
- **`Night Guide` zone edited**: remove Couch lightstrip + Tree L/R (and never
  add the Play bars). Result: the "Main Room" night-red automation, which targets
  `Night Guide`, can no longer reach the viewing area → **no mid-movie flash,
  structurally**. Night-red still covers the rest of the apartment.

### 5.3 Per-fixture native default (the "return to" target, D4)

| Fixture | Daytime default | Night default (22:34→sunrise) |
|---------|-----------------|--------------------------------|
| Play bars | off (TV-dedicated) | off |
| Couch lightstrip | circadian (Golden hours look) | off |
| Tree L/R | circadian (Golden hours look) | off |

> **Open item (O1):** confirm via `inventory` whether the Couch lightstrip + Tree
> L/R are currently members of the Golden hours circadian scenes. If not, decide
> at implementation whether to add them (so a daytime default exists) or define
> their non-TV default explicitly. The *principle* — "resume native behavior" —
> is fixed; the exact membership is the implementation detail.

## 6. Architecture

Two components, one clean seam. **Source-of-truth lives in git; HA is only the
live trigger.**

```
┌─────────────────────────── this repo (hue-iac, declarative) ───────────────────────────┐
│  hue.yaml (live, untracked):  areas.zones: TV Viewing (+ Night Guide edit)              │
│                               scenes: "TV Mode" (per-fixture targets)                    │
│  config.py:    SceneSpec dataclass + `scenes:` parsing                                    │
│  reconcile.py: SceneReconciler  → ensures the bridge `scene` "TV Mode" (CREATE/UPDATE)   │
│  AreaReconciler (existing) → ensures the TV Viewing zone + Night Guide membership        │
│  → `hue-iac apply` makes the bridge match. Look + zones are versioned, reconciled.       │
└──────────────────────────────────────────────────────────────────────────────────────────┘
                                          │  (bridge scene "TV Mode" exists, scoped to TV Viewing)
                                          ▼
┌─────────────────────────── Home Assistant (container on Synology) ────────────────────────┐
│  binary_sensor.ping  → TV IP, count=N, scan_interval≈5s                                    │
│  automation A: sensor → on  (for: 5s)  ⇒ scene.turn_on "TV Mode"                            │
│  automation B: sensor → off (for: 5s)  ⇒ release (see §7)                                   │
│  network_mode: host  (ICMP + discovery)                                                     │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

HA only acts on the **on/off edges**, so a manual tweak to the bias mid-movie
survives until the TV next toggles.

## 7. Data flow & release logic

```
LG OLED power ──(NIC up/down)──► HA ping binary_sensor ──(for:5s debounce)──► HA automation

 on-edge  ⇒ scene.turn_on "TV Mode"            (Play bars→bias, couch→mellow, tree→dim)
 off-edge ⇒ RELEASE:
              Play bars            → off
              Couch + Tree L/R     → if daytime (sunrise..22:34): recall current circadian look
                                     else (night):                off
```

- **"Recall current circadian look"**: the bridge's Golden hours holds the live
  daytime value; on release HA re-applies it so the lights snap back immediately
  instead of waiting for the next sun-anchored transition. Exact HA mechanism
  (recall the Golden hours smart_scene vs. activate the current sub-scene) is
  pinned at implementation — **Open item (O2)**.
- The day/night cutoff reuses the existing `22:34 / sunrise` boundary from the
  night_motion config.

## 8. Failure modes

| Mode | Handling |
|------|----------|
| Wi-Fi packet drop | `for: 5s` debounce + small ping `count` per check. |
| TV stays pingable in standby (Quick Start+ / WoL) | **30-second standby test** (§9) before shipping. If it fails: switch the probe from ICMP to a **TCP connect to webOS :3001**, which closes in standby even when the NIC stays up. |
| HA down / restarting | Lights hold last state; bridge automations unaffected. No bias, no chaos. |
| Bridge unreachable from HA | HA logs the failed call; native automations unaffected. |
| TV IP changes | DHCP reservation for the TV (setup step). |
| Daytime circadian transition lands mid-movie | v1 accepts it (rare, gentle, daytime-only; evening/night viewing is rock-solid because night-red is carved out and there are no circadian steps after bedtime). Optional future "re-assert" watcher in HA if daytime bias must be bulletproof — **out of scope for v1**. |

## 9. Testing

**Pure layer (unit, no bridge):**
- `SceneSpec` parsing in `config.py` (valid/invalid, per-fixture state shapes).
- `SceneReconciler.plan()` → CREATE / UPDATE / NOOP / BLOCKED, following the
  existing `reconcile` + `tests/conftest.py` (`make_config`) patterns.

**On-bridge validation (manual checklist):**
1. **30-sec standby test:** `ping` the TV, power it off with the remote, confirm
   replies stop within a few seconds. (Decides ICMP vs. TCP probe.)
2. `hue-iac apply` creates the `TV Viewing` zone, edits `Night Guide`, and
   creates the `TV Mode` scene; re-running `plan` is a NOOP (idempotent).
3. Turn the TV on → viewing area enters TV Mode; rest of apartment unchanged.
4. At night, with the TV on, walk through a non-viewing room → hallway/bedroom
   red-guide fires as normal; viewing area does **not** flinch.
5. Turn the TV off in the evening → couch/tree return to the warm circadian look,
   Play bars go off. Turn it off late at night → viewing area goes dark.

**HA config:** validated by HA's own config check; automations dry-run via the
trace UI.

## 10. Deliverables (file plan)

In this repo (the PR):
- `config.py` — `SceneSpec` frozen dataclass; `scenes: tuple[SceneSpec, ...] = ()`
  on `Config`; YAML parsing + validation.
- `reconcile.py` — `SceneReconciler(Reconciler)`; wired into `Planner`. Reuses the
  marker (`DEFAULT_MARKER = "[iac]"`) and `payload.py` colour conversion.
- `tests/` — unit tests for the above.
- `homeassistant/` (new) —
  - `docker-compose.yml` (HA container, `network_mode: host`, restart policy),
  - `config/configuration.yaml` snippet (ping binary_sensor; hue integration note),
  - `config/automations.yaml` (automations A + B),
  - `README.md` — Synology Container Manager setup, DHCP reservation, the 30-sec
    standby test, ICMP/host-networking note + TCP-probe fallback, Hue pairing.
- `CLAUDE.md` — note the new feature **and the honest architectural shift**: this
  is the first feature requiring an external runtime (HA), not pure-native.

In the user's **untracked live `hue.yaml`** (a local deploy step, not part of the
code PR): the `TV Viewing` zone, the `Night Guide` membership edit, and the
`scenes: TV Mode` block.

## 11. Open items (resolved at implementation, not blockers)

- **O1** — Confirm Couch/Tree circadian membership via `inventory`; finalize their
  daytime default (§5.3).
- **O2** — Pin the exact HA "recall circadian on release" mechanism (§7).
- **O3** — Pin exact Hue resource names (Play bars, Tree L/R) via `inventory`.
- **O4** — Tune the TV-Mode brightness/mirek values on the real bridge.
- **O5** — Decide ICMP vs. TCP probe from the 30-sec standby test result.

## 12. Two-pass / operational notes

- `hue-iac apply` refuses on any `BLOCKED` change with a misleading "lights not
  assigned" message; `--ignore-unassigned` bypasses it. Cold-start ordering
  (zone must exist before the scene references it) may need the same two-pass
  pattern the night_motion feature uses — confirm during implementation.
- The Play bars are new to the IaC; add them so `inventory`/membership is complete.
