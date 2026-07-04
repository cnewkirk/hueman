# hue-iac

Declarative, Terraform-style management of Philips Hue lighting — a sun-anchored
circadian day cycle, TV-aware bias lighting, night motion guidance, and honoured
manual overrides.

You describe the desired state of your lights in a YAML file; `hue-iac` diffs it
against the bridge (`plan`), converges it (`apply`), and — for the behaviours a
bridge can't run natively — ships a small resident **circadian daemon** that
drives a zone smoothly along the solar curve and holds a TV-bias look while you
watch. Built for the **Hue Bridge Pro** and its local **CLIP API v2**.

```
hue-iac validate         # parse + validate the config (no bridge needed)
hue-iac preview          # print the circadian colour curve for a day
hue-iac plan             # show what apply would change (read-only)
hue-iac apply            # converge the bridge's declarative state
hue-iac circadian run    # run the circadian + TV-bias daemon (foreground)
hue-iac circadian resume # clear a manual-override suspension out-of-band
hue-iac watch            # legacy live motion controller (see Limitations)
```

## The three layers

1. **`apply` provisions native bridge state** — rooms/zones, sensor sensitivity,
   a generated sun-anchored `smart_scene` day cycle (`circadian_scene:`),
   re-timed hand-built smart scenes (`smart_scenes:`), and a MotionAware
   night-red guidance automation (`night_motion:`). The bridge runs all of this
   itself; `apply` is idempotent and safe to re-run daily.
2. **The circadian daemon** (`circadian run`) is the live runtime for what the
   bridge cannot express: it re-samples the solar-elevation curve every minute
   and drives one zone through a continuous colour/brightness drift (75 s
   cross-fades over 60 s ticks — no visible steps), detects manual overrides by
   settle-and-compare and steps back until you resume, and holds a **TV bias**
   look on the viewing lights while the TV is on (detected by a TCP probe of the
   TV, an SSE scene trigger, or a control file — OR-combined, debounced), snapping
   them back to the room's curve seconds after it turns off.
3. **`watch`** is the older per-sensor motion runtime (`motion_policies:`). It
   predates the daemon and MotionAware; see Limitations before using it.

## Architecture

The decision logic is isolated from all I/O so it is deterministic and fully
unit-tested; the network surface is thin and replaceable.

| Module | Responsibility |
| --- | --- |
| `config.py` | Parse + validate the YAML into typed, frozen dataclasses |
| `sun.py` | NOAA sunrise/sunset + solar elevation for a location (`SolarCalculator`) |
| `circadian.py` | Solar elevation → colour-temperature/brightness curve (`CircadianCurve`) |
| `circadian_scene.py` | Samples the curve at ≤6 sun knee-times into the generated smart-scene steps |
| `circadian_control.py` | Pure daemon state machine: drive window, settle-and-compare override detection (`CircadianController`) |
| `bias_control.py` | Pure TV-bias core: per-light hold/drive/off decisions + debounced trigger aggregation |
| `engine.py` | Pure motion/timing state machine for `watch` (`PolicyEngine`) |
| `payload.py` | Decision output → CLIP request bodies, incl. sRGB → CIE xy |
| `nightmotion.py` | Pure night-motion/scene helpers: scene bodies, automation transform, tolerant scene-look diff |
| `reconcile.py` | Terraform-style plan/apply: `Planner` + area, sensitivity, smart-scene, circadian-scene, and night-motion reconcilers |
| `state.py` | Index the live bridge (incl. MotionAware areas); resolve names → ids |
| `client.py` | CLIP API v2 client |
| `pin.py` | Trust-on-first-use TLS certificate pinning |
| `circadian_daemon.py` | The resident daemon: curve ticks, SSE events, TV-bias triggers/probe |
| `watch.py` | Legacy live runtime: bridge event stream → `PolicyEngine` → commands |
| `cli.py` | The `hue-iac` command-line interface |

## Install

```bash
cd hue-iac
python3 -m pip install -e .        # installs the `hue-iac` console script
# or just: python3 -m pip install requests pyyaml
```

Requires Python 3.10+. Run the tests with `python3 -m pytest`.

## First-time setup

1. **Find your bridge IP** (Hue app → Settings → My Hue System → your bridge),
   and put it in the config `bridge.host` or `export HUE_BRIDGE_HOST=...`.
2. **Pair** — press the bridge's physical link button, then:
   ```bash
   hue-iac auth
   ```
   It prints an application key. Store it as an environment variable so it never
   lands in the config file:
   ```bash
   export HUE_APPLICATION_KEY=<the key it printed>
   ```
3. **Validate and preview** (no bridge calls):
   ```bash
   hue-iac -c examples/home.yaml validate
   hue-iac -c examples/home.yaml preview
   ```
4. **Plan and apply**:
   ```bash
   hue-iac -c examples/home.yaml plan
   hue-iac -c examples/home.yaml apply
   ```
5. **Run the daemon** (foreground; use Docker/systemd to keep it up — a Synology
   Docker runbook lives in `deploy/synology/README-docker.md`):
   ```bash
   hue-iac -c examples/home.yaml circadian run
   ```

## TLS

The bridge serves a self-signed certificate. `hue-iac` defaults to
`tls.mode: pin` — trust-on-first-use: it records the bridge certificate's
SHA-256 fingerprint in `.hue-pin.json` on first connect and enforces it on
**every** connection the session opens (including the long-lived event stream),
so a man-in-the-middle on your LAN is caught. If you replace the bridge, delete
`.hue-pin.json` to re-pin deliberately. Alternatives are `tls.mode: cacert`
(verify against a CA bundle you supply) and `tls.mode: insecure` (no
verification — not recommended).

## No lights left behind

Declare your rooms, zones, and which lights belong to each under `areas:`. With
`require_all_lights_assigned: true` (the default), `plan` flags any light on the
bridge that isn't in a declared room, and `apply` refuses until you assign it
(or pass `--ignore-unassigned`). A light lives in exactly one room (a Hue
constraint, validated at parse time) but may appear in any number of overlapping
zones.

```yaml
areas:
  rooms:
    - name: Office
      type: office
      lights: [Office desk lamp, Office ceiling 1, Office ceiling 2]
  zones:
    - name: Night pathway
      lights: [Secretary desk lamp, Office ceiling 1]
```

### Keeping it anchored (DST + seasons)

Set `location.tz` to an IANA zone (e.g. `America/Los_Angeles`) and the sun math
tracks DST automatically — no twice-a-year `tz_offset_hours` edit. The daemon
re-derives the curve from the sun continuously; the *native* smart-scene path
(`circadian_scene:`) re-anchors whenever `apply` runs, so if you use it without
the daemon, schedule a daily `apply` (cron wrapper in `deploy/synology/`,
superseded by the Docker daemon runbook when the daemon is in use).

## Config reference

See [`examples/home.yaml`](examples/home.yaml) for a complete, commented example.
Key sections:

- `bridge` — host and TLS; the application key comes from `$HUE_APPLICATION_KEY`.
- `location` — `lat`, `lon`, and `tz` (IANA) or `tz_offset_hours`; drives sunrise/sunset.
- `circadian` — anchor points for the colour curve (drives the daemon, `preview`,
  and `circadian_scene`).
- `areas` — declarative room/zone light assignment.
- `circadian_daemon` — the resident runtime: `zone`, drive window (`start`,
  `hand_off`), `interval`/`transition`/`fade_off`, manual-override handling, and
  the `bias:` block (per-light TV looks + `triggers:` probe/sse/control-file,
  with a short `transition` edge fade for TV on/off flips).
- `circadian_scene` — generate a smooth, sun-anchored circadian `smart_scene`
  from the `circadian` curve (native alternative to the daemon for the day cycle).
- `smart_scenes` — re-time an existing bridge `smart_scene` to the real sun.
- `night_motion` — night soft-red motion guidance via a MotionAware automation
  (`mode: night_only` when the daemon owns the day; three timeslots are emitted —
  night start, a 00:00 clone for the small hours, and an actionless `day_start`
  slot that keeps the wrapped night slot from governing dark evenings).
- `motion_policies` — per-sensor policies for the legacy `watch` runtime.

## Limitations and notes

- The pure decision layer (engine, circadian, daemon controller, bias, sun,
  config, reconcile, nightmotion) is covered by ~240 unit tests. `client`,
  `pin`, and the daemon's I/O shell talk to a real bridge and should be
  exercised on your network.
- **`watch` is legacy and known-inadequate against current bridge firmware**:
  its echo-buffer override detection predates the bridge's periodic re-emission
  of settled `grouped_light` values, which it can misread as manual overrides,
  and it only understands legacy PIR `motion` sensors — not Bridge Pro
  MotionAware. The daemon's settle-and-compare detection is the current design.
  `watch --dry-run` logs intended commands without sending them.
- The daemon runs in the foreground; pair it with Docker (`--restart always`),
  `systemd`, or similar. On a manual override it suspends until a power-cycle of
  the zone, `hue-iac circadian resume`, or the daily safety resume.
