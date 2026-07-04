# Synology daily re-anchor cron + DST-aware solar offset — design

Date: 2026-06-27 · Bridge: Hue Bridge Pro · Branch: `synology-reanchor-cron`

## Goal

Run `hue-iac apply` **once a day on the Synology NAS** so the natively-delivered
circadian cycle and the night sunrise gate keep tracking the real sun through the
seasons — without a resident daemon, and **correct year-round across DST**. Today the
re-anchor only stays accurate if someone remembers to flip `location.tz_offset_hours`
from `-7` (PDT) to `-8` (PST) every November; this effort removes that footgun and
schedules the apply.

Two parts: (1) make the tool itself DST-aware so the offset is derived per-date; (2)
deploy + schedule the daily apply on the NAS as a native Python venv driven by DSM
Task Scheduler.

## Ground truth (read from the repo + live LAN probe)

- The package is pure Python (`requests`, `PyYAML`), console script `hue-iac`,
  `requires-python >= 3.10`. No Docker/deploy scaffolding exists yet.
- `apply` needs, at runtime: `hue.yaml`, `HUE_APPLICATION_KEY` (from `.hue-key`), the TLS
  pin `.hue-pin.json`, write access to `.hue-backup/`, and bridge reachability. The pin
  and backup paths in `hue.yaml` are **relative** — resolved against the process CWD.
- `tz_offset_hours` is a **fixed float**. It is consumed once, at `sun.py:153`
  (`solar_noon_min = 720 - 4*lon - eq_of_time + tz_offset_hours*60`). `Location.parse`
  (`config.py:142`) requires it. There is no `zoneinfo`/DST logic anywhere.
- `SolarCalculator` is constructed in five places: `reconcile.py:381`, `reconcile.py:619`
  (the apply path), `cli.py:263` (preview), and `engine.py` / `watch.py` (the live
  daemon). The daily cron only exercises the **reconcile/apply** path.
- Live probe (from a LAN host, 2026-06-27): `hue-bridge.<your-domain>` resolves
  via CNAME to `192.0.2.2` and answers by name and by IP. So the existing `hue.yaml`
  host works as-is on the LAN; no IP/hosts workaround is needed.

**Decision (chosen):** make the *tool* DST-aware via stdlib `zoneinfo`, rather than
patch the offset in a cron wrapper or flip the config by hand. It is small, lives in the
pure layer (unit-tested), and fixes both the NAS cron and local Mac runs permanently.

## Part 1 — DST-aware solar offset (the contract)

- A `location:` block MAY declare `tz: <IANA name>` (e.g. `America/Los_Angeles`). When
  present, the solar math uses the **UTC offset valid on the date being anchored** — `-8`
  in winter, `-7` in summer — so sun-anchored timeslots never drift across a DST boundary.
- When `tz` is absent, behaviour is **exactly as today**: the fixed `tz_offset_hours` is
  used. Fully backward-compatible; existing configs and tests are unaffected.
- The change is scoped to the **apply/preview** path. `engine`/`watch` (the live daemon,
  *not* deployed here) keep the fixed-offset behaviour — a documented limitation, not a
  silent half-measure.

### Components (each small, one purpose, testable in isolation)

1. **`config.py` — `Location`.** Add optional field `tz: str | None = None`. `parse()`:
   - reads optional `tz` (string); validates it resolves via `ZoneInfo(tz)`, raising a
     clear `ConfigError` (e.g. "unknown timezone 'Foo/Bar'") on `ZoneInfoNotFoundError`;
   - makes `tz_offset_hours` **optional when `tz` is given**, back-filling it with the
     offset for the config-load date so every existing consumer (`engine`, `watch`, any
     `loc.tz_offset_hours` reader) still receives a usable float;
   - keeps the current `ConfigError` when **neither** `tz` nor `tz_offset_hours` is set.

2. **`sun.py` — `SolarCalculator`.** `__init__` gains `tz: str | None = None` (stored).
   New private `_offset_for(date) -> float`: returns the `zoneinfo`-derived offset for
   that date when `tz` is set (`ZoneInfo(tz).utcoffset(datetime(y, m, d, 12)).total_seconds()/3600`),
   else the fixed `tz_offset_hours`. Line 153 uses `self._offset_for(date)` in place of
   the stored constant — the only behavioural edit to the math.

3. **Callers pass the tz through (apply/preview only).** `reconcile.py:381`,
   `reconcile.py:619`, `cli.py:263` construct `SolarCalculator(loc.lat, loc.lon,
   loc.tz_offset_hours, tz=loc.tz)`. `engine.py`/`watch.py` are left as-is.

4. **Dependency.** Add `tzdata` to `[project].dependencies` so `ZoneInfo` resolves even on
   a host lacking system tz data. Pure-data, cheap; NAS + Mac already have system tzdata,
   but this makes the tool self-contained and removes a deployment assumption.

5. **`hue.yaml` (live, untracked).** Add `tz: America/Los_Angeles` under `location:`;
   keep `tz_offset_hours: -7` as the harmless fallback. Replace the now-obsolete
   "switch to -8 in November" comment with a note that `tz` drives DST automatically.
   Applied on both the Mac copy and the NAS copy.

## Part 2 — NAS install layout (native venv)

One self-contained directory (`$HUE_IAC_HOME`, exact path confirmed at install — e.g.
`/volume1/<share>/hue-iac`). The wrapper `cd`s into it so the config's **relative** pin
and backup paths resolve:

```
$HUE_IAC_HOME/
  src/              # git clone of hue-lac (pip install -e ./src; `git pull` to update)
  .venv/            # python3 -m venv; installs requests, PyYAML, tzdata
  hue.yaml          # NAS config — includes `tz: America/Los_Angeles`
  .hue-key          # application key, mode 600        (scp'd from the Mac)
  .hue-pin.json     # TLS pin                          (scp'd from the Mac → deterministic
                    #                                   trust; no blind re-TOFU on the NAS)
  .hue-backup/      # writable; write-once pre-apply backups
  bin/re-anchor.sh  # wrapper (versioned in the repo at deploy/synology/)
  logs/re-anchor.log
```

Install steps (I run them over SSH): `git clone` → `python3 -m venv .venv` →
`.venv/bin/pip install -e ./src` (pulls `requests`/`PyYAML`/`tzdata` from PyPI; NAS needs
outbound internet) → `scp` the three secret/config files, `chmod 600` the key and pin →
copy in `bin/re-anchor.sh`.

## Part 3 — Wrapper script (`deploy/synology/re-anchor.sh`, versioned in the repo)

POSIX `/bin/sh` (DSM busybox-`ash` safe — no bashisms):

- `set -eu`; resolve and `cd "$HUE_IAC_HOME"` (so relative paths resolve).
- Size-cap the log (~1 MB → rotate to `.1`) to bound growth on the NAS.
- `export HUE_APPLICATION_KEY="$(cat .hue-key)"`.
- Run `.venv/bin/hue-iac -c hue.yaml apply --yes`, appending stdout+stderr to the log with
  ISO timestamps around it.
- **Propagate the non-zero exit code** so DSM Task Scheduler can email on failure.
- **No `--ignore-unassigned`.** Steady-state apply must be clean; a `BLOCKED` change is a
  real problem and should fail loudly, never be bypassed. (`--ignore-unassigned` exists
  only for the cold-start two-pass, which does not apply here — the zone/automation are
  already live.)

The script carries no secrets, so it is safe to version in the repo.

## Part 4 — DSM Task Scheduler (the one GUI step, done by the user)

Control Panel → Task Scheduler → Create → Scheduled Task → User-defined script:

- **User:** `root` (simplest for file perms).
- **Schedule:** Daily, **~03:30 local.** DSM fires at the system-timezone wall-clock and is
  itself DST-correct. 03:30 sits inside the night-motion window, where the daily re-anchor
  is a **NOOP for `night_motion`** (its 22:34→sunrise wiring is unchanged) and only re-times
  the *future* daytime circadian timeslots — so no active guidance is disrupted.
- **Command:** `sh $HUE_IAC_HOME/bin/re-anchor.sh`.
- **Notification:** enable email → "only when the script terminates abnormally."

Exact field values are handed over as a runbook in `deploy/synology/README.md`.

## Data flow

```
DSM Task Scheduler (daily 03:30, system tz)
        │  sh bin/re-anchor.sh
        ▼
re-anchor.sh: cd $HUE_IAC_HOME; export HUE_APPLICATION_KEY=$(cat .hue-key)
        │  .venv/bin/hue-iac -c hue.yaml apply --yes
        ▼
hue-iac apply → config.py (Location w/ tz) → reconcile.py builds
        SolarCalculator(lat, lon, offset, tz=America/Los_Angeles)
        │  sun_times(today) uses the DST-correct offset for today
        ▼
CircadianSceneReconciler re-times the "Golden hours" smart_scene to today's sun;
NightMotionReconciler is NOOP → PUT only the circadian timeslots → the Hue Bridge
        │  exit code → re-anchor.sh → DSM (email iff non-zero)
        ▼
logs/re-anchor.log
```

## Error handling & verification

Failure modes, all of which surface as a **non-zero exit → DSM email**:
- **Bridge unreachable** (NAS off the LAN / DNS down) → `apply` errors out.
- **Pin mismatch** (bridge cert rotated) → TLS pin check fails; rotate `.hue-pin.json`.
- **Unexpected `BLOCKED`** (e.g. a zone went missing) → `apply` refuses, prints the real
  reason; the wrapper does not bypass it.

Verification at install, in order, before declaring done (evidence before assertion):

1. `… validate` → ok.
2. `… plan` → connects (proves NAS→bridge net **and** the copied pin trust); expect NOOP
   or only timeslot-time deltas, and **zero BLOCKED**.
3. `bin/re-anchor.sh` run manually → exit 0; log shows the apply result.
4. `… plan` again → NOOP (idempotent).
5. `… preview --date 2026-12-15` vs `--date 2026-07-15` → sunrise shifts ≈1 h, confirming
   `zoneinfo` DST derivation works **on the NAS**.
6. User creates the Task Scheduler entry, then presses its **Run** button once → confirms
   it fires under DSM's minimal env. The wrapper calls `.venv/bin/hue-iac` explicitly, so
   `PATH` is irrelevant.

## Testing (Part 1, TDD, pure layer)

- **`test_sun.py`:** `SolarCalculator(lat, lon, <ignored>, tz="America/Los_Angeles")` —
  `sun_times(date(2026,1,15))` matches a fixed `-8` calculator; `sun_times(date(2026,7,15))`
  matches a fixed `-7` calculator; the two differ by ≈60 min in solar noon. No `tz` →
  identical to today. Polar/edge cases unaffected (offset only shifts solar noon).
- **`test_config.py`:** `location` with `tz` only → parses, `tz_offset_hours` back-filled to
  a float; with both → both retained and `tz` drives derivation; bad tz name → `ConfigError`;
  neither `tz` nor `tz_offset_hours` → existing `ConfigError` preserved.
- Existing 110 tests must stay green (backward compatibility).

## Sequencing & Git

1. On `synology-reanchor-cron`: TDD the Part-1 change (`config.py`, `sun.py`, the three
   callers, `pyproject.toml`); add `deploy/synology/{re-anchor.sh,README.md}`; doc note in
   `CLAUDE.md`/`README.md` (DST + the cron).
2. Full `pytest` green (110 + new).
3. Update the live `hue.yaml` on the Mac with `tz:`; verify a real Mac `apply` still works.
4. **PR to `main`** (verify personal `gh`/remote/noreply email per CLAUDE.md first).
5. Deploy to the NAS from that code; run the verification list; user schedules it.

Prerequisites needed at implementation time (not before): **NAS hostname/IP + confirmed
key-based SSH for me**, and the NAS having outbound internet for `pip install`.

## Out of scope (deferred / next)

- **DST-awareness for `engine`/`watch`.** The live daemon keeps the fixed offset; it is not
  part of this native, no-daemon deployment. Revisit if `watch` is ever deployed.
- **Gentle night-red motion fade** (scene `dynamics`/recall duration) — still the prior
  deferred item.
- **Timestamped backups.** Write-once already preserves the genuine pre-first-apply original.
