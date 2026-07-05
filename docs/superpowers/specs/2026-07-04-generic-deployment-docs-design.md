# Generic (non-Synology) deployment docs — design

**Date:** 2026-07-04
**Status:** approved
**Owner:** `deploy/` docs; touches `README.md` pointer and `deploy/synology/README-docker.md`

## Problem

All deployment knowledge lives in `deploy/synology/README-docker.md`, written
Synology-first (`/volume1/<share>` paths, `sudo /usr/local/bin/docker`,
Container Manager, SFTP-off workarounds). The invariants an outside user needs
— mounts, key delivery, TLS pinning, the `circadian_daemon:` block, host
networking, verification — are all platform-neutral but only discoverable by
mentally stripping the Synology parts. There is no docker-compose example and
no systemd path for no-Docker boxes. The repo is public; this is the first
thing an external adopter hits after `apply` works.

## Decisions (settled with the user)

- **systemd variant = bare-metal venv daemon** (`hueman circadian run` from a
  pip/venv install, no Docker) — a real third path, not a unit that wraps
  `docker run`.
- **Synology doc slims to platform deltas** layered on the generic doc. The
  private ops repo's concrete copy (real paths) stays standalone and untouched.
- **Build-from-source only.** Compose uses `build:`; no registry image, no CI
  publish. GHCR publishing is noted as a possible follow-up, not done.
- **Daemon-only, plus one paragraph** pointing daemon-less (`circadian_scene:`
  + daily `apply`) users at `deploy/synology/README.md` as the worked example.

## Deliverables

### 1. `deploy/README.md` (new) — the generic runbook

Structure:

1. **Intro map** — one paragraph: three deployment flavors below; Synology
   quirks live in `synology/README-docker.md`; read the invariants first.
2. **What every deployment needs** (invariants, written once):
   - `hue.yaml` **must contain a `circadian_daemon:` block** — without it the
     daemon exits immediately, which is a crash-loop under any supervisor.
     Point at `hueman/config.py` (`CircadianDaemonSpec`) for tunables.
   - **Key delivery is the `HUE_APPLICATION_KEY` env var only** — no code path
     reads a key file at runtime. Show the host-shell-reads-file pattern and
     the env-file hardening alternative (keeps the key out of `docker inspect`
     and shell history).
   - **`.hue-pin.json` must be pre-pinned** (run `hueman auth` once from a
     host shell) — a read-only mount cannot trust-on-first-use.
   - **The three mounts** — `/data/hue.yaml` (ro), `/data/.hue-pin.json` (ro),
     `/data/logs` (rw) — and that the daemon resolves relative paths from
     `/data` (its working directory).
   - **Network** — the daemon holds a long-lived SSE stream to the bridge on
     your LAN; `network_mode: host` recommended for containers (NAT keepalive
     problems otherwise). Bare-metal just needs a route to the bridge.
   - **Supervision** — `--restart always` / `Restart=always`; caveat: a manual
     override (SUSPENDED) is not persisted across a restart.
   - **Verify** — the exact healthy-start log lines (`circadian daemon driving
     '<zone>'`, `drive '<zone>' -> …` ticks); warning that container clocks
     default to UTC so log timestamps are not local time.
3. **Method: docker run** — canonical command with generic paths; build first
   (`docker build -t hueman:latest .`). Path convention (used consistently
   across the doc): `/opt/hueman` = the git checkout, `/srv/hueman` = the host
   data dir for the container methods, `/var/lib/hueman` = the data dir for
   the bare-metal method (FHS service-state home; deliberate difference).
4. **Method: docker compose** — `docker compose up -d` against the real file
   (deliverable 2); shows only what to edit (paths, env file).
5. **Method: bare metal (venv + systemd)** — `python3 -m venv` + `pip install .`
   from a clone; unit file (deliverable 3); data dir layout mirrors `/data`
   (config, pin, `logs/` under `WorkingDirectory`).
6. **TV-bias signalling (optional)** — the shared control-file directory
   pattern: the signaller (e.g. a Home Assistant container) and the daemon
   must both see one shared directory; the `sse` trigger alternative needs no
   shared mount. Do not re-explain the feature; link the README section.
7. **Daemon-less alternative** — one paragraph: schedule `hueman apply` daily
   with any scheduler to keep native `circadian_scene:` timeslots sun-anchored;
   `deploy/synology/README.md` is the worked example.
8. **Troubleshooting table** (generic): container exits immediately (bad
   config / missing mount), 401 crash-loop (key env var missing), TLS pin
   mismatch (bridge cert rotated → re-pin), SSE drops (auto-reconnects),
   `BLOCKED` on apply (zone/scene not found — run `hueman plan`).

### 2. `deploy/docker-compose.yml` (new)

Real, copyable file: one service `hue-circadian`; `build:` context `..`
(repo root); `network_mode: host`; `restart: always`; the three volumes plus a
commented-out tvsig volume; `env_file:` for `HUE_APPLICATION_KEY`. Comments
kept to one line each pointing at the README invariants.

### 3. `deploy/systemd/hueman-circadian.service` (new)

Real, copyable unit: `Description`, `After=network-online.target` /
`Wants=network-online.target`; `User=hueman` (non-root);
`WorkingDirectory=/var/lib/hueman` (holds `hue.yaml`, `.hue-pin.json`,
`logs/`); `EnvironmentFile=/etc/hueman/hueman.env` (the key);
`ExecStart=/opt/hueman/.venv/bin/hueman -c hue.yaml circadian run`;
`Restart=always`, `RestartSec=5`; `[Install] WantedBy=multi-user.target`.
Paths are the doc's generic convention; users edit them.

### 4. `deploy/synology/README-docker.md` — slim to deltas

Opens with "read `../README.md` first; this page is only what's different on
Synology." Keeps: `/volume1/<share>` layout convention; `sudo
/usr/local/bin/docker` (not on the non-interactive PATH); Container Manager
image-import alternative; source sync is rsync into a plain `build/` context,
not a git clone (and `scp` needs `-O` — SFTP subsystem disabled); the
relationship-to-the-re-anchor-cron note. Deletes everything the generic doc
now owns (invariants, run command rationale, tvsig explanation, verification,
failure modes). The legacy cron `deploy/synology/README.md` is untouched.

### 5. `README.md` pointer

The "Run the daemon" step and the Limitations mention flip from "a Synology
Docker runbook lives in `deploy/synology/README-docker.md`" to pointing at
`deploy/README.md` (with Synology named as a platform-notes subpage).

## Non-goals

- No registry image, no CI publish workflow (follow-up candidate: GHCR).
- No generic cron content for the daemon-less mode beyond the one paragraph.
- No changes to the ops repo's concrete Synology runbook.
- No code changes.

## Verification

- `docker compose config` lints the compose file (plain YAML parse if Docker
  is unavailable on the dev machine).
- Fresh-eyes read of the systemd unit against the daemon's real CWD/relative-
  path behavior (`log.path`, pin, resume file all resolve from CWD).
- Deny-list grep (split spec) over the diff before push — public repo.
- `python3 -m pytest -q` still green (docs-only, should be untouched).
