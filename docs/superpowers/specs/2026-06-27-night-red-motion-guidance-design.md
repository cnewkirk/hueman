# Night-time soft-red motion guidance — design

Date: 2026-06-27 · Bridge: Hue Bridge Pro (MotionAware) · Branch: `night-red-motion-guidance`

## Goal

At night, motion should bring up **very soft, low, deep-red** light across the
apartment to guide the user (e.g. to the bathroom) without being jarring, then
fade back off. Delivered **natively on the bridge** (no daemon), managed
declaratively by `hue-iac` (plan/apply). Red preserves dark-adaptation and avoids
melatonin suppression.

## MotionAware ground truth (researched + bridge-verified)

- **Sensing is per-room and physical, not one whole-house grid.** A motion *area*
  needs **≥3 lights in one room** positioned to cover that room's 3D space; the
  shared Zigbee mesh enables comms but sensing rides the signal path between nearby
  bulbs. Up to **4 areas** per Bridge Pro. (philips-hue.com / hueblog.com)
- Only the **Living room** has a grid: `motion_area_configuration` **"Main Room"**
  (service `convenience_area_motion` rid `91c99d18-…`, 4 participants). Bedroom (2)
  and Entryway (1) are below the 3-bulb minimum; **0 spare candidate bulbs**. The
  apartment layout makes the single living-room grid sufficient for the common path.
- **Sensing ≠ response.** One motion area senses in its room but can light **up to
  3 rooms OR 1 zone** on motion. To light all the apartment's bulbs we therefore
  use **1 zone** (a 4th room would exceed the "3 rooms" cap).
- A native automation already does ~80% of this: `behavior_instance` "Main Room"
  (script "Hue Accessories" `67d9395b-…`), enabled, dark-gated, controlling 3 rooms,
  with a **23:00 night timeslot** that recalls "Nightlight" scenes and all-offs
  after 10 min. We tune this rather than build new.
- The active **"Golden hours" smart_scene** has 22:34/23:00 steps that turn lights
  on — they overlap the night window and are trimmed.

## Behaviour (the contract)

- **Active window:** 22:34 → sunrise (night timeslot `start_time` + the existing
  daylight gate, so it never fires in daylight).
- **Baseline overnight:** off.
- **On motion** (living-room grid): recall ONE soft deep-red scene on the
  whole-apartment guidelight zone (target xy ≈ 0.675, 0.31 @ **3%**). NOTE (shipped):
  scenes use the bridge's default transition; a true slow fade (scene `dynamics`/
  recall duration) is a deferred nice-to-have, not yet implemented.
- **On no motion for 3 minutes:** all off (fade out).
- **Lights affected (response):** a single zone **"Night Guide"** containing **all
  apartment bulbs** (Bedroom + Living room + Living Room Corner + Entryway).
- **Daytime/evening:** the 07:00/17:00 motion slots keep their cool/warm look but
  are **retargeted to the whole zone** (all 12 bulbs incl. Bedroom) — a single
  automation has one global `where`. Golden hours runs until 22:34.

## Components (each small, testable, one purpose)

1. **Guidelight zone (existing `AreaReconciler`).** Declare a zone **"Night Guide"**
   with all guidelight lights in `hue.yaml` `areas.zones`; the existing area
   reconciler creates/maintains it. No new I/O code.
2. **Red-scene builder (pure).** `f(zone_light_rids, color, brightness) -> scene
   create/update body`: a CLIP `scene` on the zone with one action per light
   (on, dimming=brightness, color=xy from `payload.hex_to_xy`). Unit-tested.
3. **Night-automation transform (pure).** `f(existing_behavior_instance_config,
   night_spec, zone_scene_rid) -> new_config`: returns the real current config with
   **only** the night timeslot rewritten — `start_time` 22:34, `on_motion.recall_single`
   = [the red zone scene], `on_no_motion.after` = 3 min, and `where` = [the Night
   Guide zone]. The 07:00 and 17:00 timeslots keep their start times + auto-off but are
   retargeted to the zone day/evening scenes (one global `where`). `recall_single`
   / `where` stay index-aligned (one entry each). Unit-tested against the captured
   real config fixture.
4. **Smart-scene prune (extend `SmartSceneReconciler`).** Make the config schedule
   authoritative: re-time listed scenes **and prune** timeslots whose scene is not
   listed. Trimming Golden hours = drop `Sleepy`/`Nighttime` from its `hue.yaml`
   schedule → apply removes those timeslots (keeps ≥1).
5. **`NightMotionReconciler` (I/O).** Orchestrates: ensure the red zone scene exists
   (create/update), **back up** the current behavior_instance config to
   `.hue-backup/<id>-<applied>.json`, then write the transformed config. Emits
   `Change`s; idempotent (NOOP when already converged).
6. **Config (`hue.yaml`):**
   ```yaml
   areas:
     zones:
       - name: Night Guide
         lights: [ <all apartment light names> ]
   night_motion:
     automation: "Main Room"     # behavior_instance to tune (by name)
     zone: "Night Guide"         # response target (1 zone — respects the cap)
     start: "22:34"              # end is sunrise via the daylight gate
     timeout: 3m
     color: { hex: "#ff1400" }   # deep red → xy via payload.py
     brightness: 3               # percent
   ```
   Plus: drop `Sleepy`/`Nighttime` from the Golden hours `smart_scenes` schedule.

## Data flow

`hue.yaml` → config parse → `BridgeState` (resolves the zone, the behavior_instance,
its motion_service, and zone light rids) → reconcilers emit `Change`s (`plan`) →
`apply`: AreaReconciler ensures the zone; NightMotionReconciler POSTs the red scene,
backs up + PUTs the behavior_instance; SmartSceneReconciler PUTs the trimmed Golden
hours. The bridge then runs everything natively; no resident process.

## Error handling & risks

- **behavior_instance schema is bridge-validated and finicky.** Mitigations: the
  transform only edits the night timeslot + where, a full backup is written before any
  PUT, and apply re-PUTs the automation only when its wiring actually changes (a pure
  scene-look edit updates the scenes alone). On rejection, restore from backup and iterate.
- **Idempotency:** plan diffs would-be config/scene/zone vs live; re-apply is NOOP once converged.
- **Reversibility:** backup restores the automation; Golden hours trim is re-addable;
  the zone + red scene can be deleted.

## Testing

- Unit (pure, captured real fixtures): red-scene body builder; night-automation
  transform (night timeslot rewritten, 07:00/17:00 preserved, where=zone,
  recall/where alignment); smart_scene prune (drops unlisted, re-times rest).
- Live verification after apply: read back the behavior_instance (night start 22:34,
  red zone-scene rid, 3-min timeout, where=Night Guide), the red scene, the zone
  membership, and the trimmed Golden hours; a manual night motion check confirms behaviour.

## Out of scope (deferred / next)

- **NEXT PRIORITY after this:** daily sun re-anchor as a **Docker container on the
  Synology** (Container Manager) running `hue-iac apply`, key/pin mounted, DST-aware.
- Wiring MotionAware into the `watch` daemon (this design is native, not daemon).
- Additional sensing areas / bedroom-entryway sensing (hardware can't; not attempted).
