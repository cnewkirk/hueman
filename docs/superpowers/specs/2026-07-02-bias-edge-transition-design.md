# TV bias edge transition — design

Date: 2026-07-02
Status: approved (live-debugged with instrumented TV cycles on 2026-07-02, ~01:56–02:08 PDT)

## Problem

Turning the TV off leaves the viewing lights "sitting in the same state for about a
minute, then shutting off abruptly" (at night), or taking >1 minute to rejoin the
circadian curve (in-window). Turning the TV on is acceptable but slower than it
needs to be.

## Evidence (instrumented live test)

A 1 s-resolution TCP watcher on the TV's `:3001` plus a raw bridge SSE capture
showed, for a TV-off at ~01:56:35 PDT:

- Port closed 01:56:50; the daemon PUT `on:false` to all 7 bias lights at
  01:57:00. **Detection is fast: ~10 s off, ~7 s on.** Detection was never the
  problem.
- The bulbs were still ramping *toward* the bright TV look when the off landed
  (the 01:56:26 curve tick had re-PUT the hold look with a fresh 75 s fade); they
  peaked at 94.86 % twenty seconds *after* the off command.
- `on:false` with `dynamics.duration=90000` (`fade_off: 90s`) is perceptually
  "hold then snap": linear dimming from high brightness is nearly invisible for
  the first two thirds, and the bulb cuts at the end. Observed as ~90 s of
  nothing, then an abrupt off.

Root causes:

1. Edge writes reuse the steady-state fades (`transition: 75s`, `fade_off: 90s`)
   which are tuned for imperceptible curve drift, not for a mode flip.
2. The 60 s curve tick re-PUTs the bias look every tick, restarting long ramps
   that fight a pending edge.
3. `_apply_bias` latches `_bias_last_applied_on` before writing; if the bridge
   rejects the writes (observed: `command queue is full` for all 7 lights at
   once), the edge is lost until the next 60 s tick.
4. Probe-driven TV flips are not logged, so detection latency was invisible.

## Fix

### 1. Edge transition (`bias.transition`, default 2s)

New `BiasSpec.transition_ms` parsed from `circadian_daemon.bias.transition`
(default `2s`). When the committed `tv_on` differs from the last *applied* value
(an edge), every bias action — `BiasHold`, `BiasDrive`, and `BiasOff` — uses
this short transition. Steady-state (non-edge) applies keep today's behaviour:
`BiasDrive` uses the daemon `transition` (75 s) so curve-following stays smooth;
`BiasHold` re-PUTs the same look (harmless, self-healing).

`bias_actions()` stays pure: it gains an `edge: bool` parameter and picks the
transition per action from the values passed in.

### 2. Stop the overnight off-spam

Out of window with the TV off, every 60 s tick currently re-PUTs `off` to all 7
viewing lights, all night (redundant Zigbee traffic; extra pressure on the queue
that has already dropped a whole bias apply). Non-edge `BiasOff` writes are
skipped once the off state has been successfully written; an edge or a failed
write re-arms them.

### 3. No edge latch on failure

`_write_light` reports success. `_apply_bias` only records the applied `tv_on`
(and "off written" state) when all its writes succeed; on any failure the next
probe tick (≤5 s) retries the full set. Writes are absolute, so retries are
idempotent.

### 4. Log committed TV flips

One INFO line when the committed `tv_on` changes, with the trigger source and
the transition being applied, e.g. `bias: TV off (probe) -> 7 lights, 2s edge
fade`. Makes detection latency directly readable from `circadian.log`.

## Config change (hue.yaml)

```yaml
circadian_daemon:
  bias:
    transition: 2s   # edge fade when the TV state flips (default 2s)
```

## Expected behaviour after fix

- TV off: lights leave TV mode ~10–12 s after power-off (5 s poll + 5 s debounce
  + 2 s fade), snapping to the curve in-window or off at night.
- TV on: lights snap to the TV look ~7–12 s after power-on.
- Steady-state circadian drift unchanged (75 s cross-fades each 60 s tick).
- No per-minute off writes overnight.

## Testing

- Pure: `bias_actions` edge vs non-edge transition selection for all three
  action types; `BiasSpec.parse` default and explicit `transition`.
- Daemon (fake client): edge apply uses the edge fade; steady-state off writes
  are suppressed after a successful off; a failed write leaves the edge
  unlatched so the next apply retries; INFO flip logging.

## Deployment

Rebuild the Docker image on the NAS from the repo and restart `hue-circadian`
(this intentionally drops the dormant, reverted webos-probe code still baked
into the running image), then live-verify with a TV on/off cycle against the
new INFO log lines. The `.bak-prewebos` config backup and the debug watchers
(`debug/tvwatch.sh`, SSE capture) are cleaned up after verification.

## Out of scope

- Night motion: live capture shows instant scene recalls on motion (3× tonight);
  monitoring continues separately.
- Daemon SSE `Read timed out; reconnecting` warnings (pre-existing, harmless to
  bias; worth a later look).
- LG standby port flap: not observed tonight (both reopen events were the user);
  the 5 s debounce is retained unchanged.
