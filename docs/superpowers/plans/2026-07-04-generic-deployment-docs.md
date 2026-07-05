# Generic Deployment Docs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a platform-neutral deployment runbook (`deploy/README.md`) with real docker-compose and systemd artifacts, and slim the Synology runbook to platform deltas.

**Architecture:** One invariants-first generic doc owns the full deployment story; three thin method sections (docker run / compose / bare-metal systemd) reference it; the Synology doc becomes a deltas page layered on top. Spec: `docs/superpowers/specs/2026-07-04-generic-deployment-docs-design.md`.

**Tech Stack:** Markdown, docker compose (file syntax v2, no `version:` key), systemd unit syntax.

## Global Constraints

- **Public repo deny-list** (split spec in the private ops repo): no homelab identifiers — hostnames, private LAN IPs (RFC 5737 doc IPs are fine), bridge id, bridge/NAS names, real usernames, VLAN names; `/volume1` only with `<share>` placeholders. Run the Task 4 grep before push.
- **Path convention** (use verbatim everywhere): `/opt/hueman` = git checkout; `/srv/hueman` = host data dir for container methods; `/var/lib/hueman` = data dir for bare-metal (deliberate FHS difference).
- **Facts that must stay accurate:** key delivery is `HUE_APPLICATION_KEY` env var ONLY; daemon CWD is `/data` in the container and paths in config resolve relative to CWD (defaults `logs/circadian.log`, `.hue-circadian-resume` — `hueman/config.py` `CircadianDaemonSpec`); Dockerfile entrypoint is `hueman -c /data/hue.yaml circadian run`; missing `circadian_daemon:` block = immediate exit; a read-only pin mount cannot trust-on-first-use; Python 3.10+.
- No code changes; `python3 -m pytest -q` must stay green (307+ pass).
- Work happens in the `deploy-docs` worktree (`~/git/github/hueman/.claude/worktrees/deploy-docs`), branch `deploy-docs`, already based on origin/main with the spec committed.

---

### Task 1: Compose and systemd artifacts

**Files:**
- Create: `deploy/docker-compose.yml`
- Create: `deploy/systemd/hueman-circadian.service`

**Interfaces:**
- Produces: the two files `deploy/README.md` (Task 2) links to, at exactly these paths.

- [ ] **Step 1: Write `deploy/docker-compose.yml`**

```yaml
# Circadian daemon via docker compose. Read deploy/README.md first — every
# mount/env choice here is explained in "What every deployment needs".
# Edit the /srv/hueman paths and the env_file location, then:  docker compose up -d
services:
  hue-circadian:
    container_name: hue-circadian
    build: ..                       # build from this checkout; no published image
    restart: always
    network_mode: host              # long-lived SSE stream to the bridge; avoids NAT drops
    volumes:
      - /srv/hueman/hue.yaml:/data/hue.yaml:ro
      - /srv/hueman/.hue-pin.json:/data/.hue-pin.json:ro   # pre-pin with `hueman auth` first
      - /srv/hueman/logs:/data/logs
      # TV-bias control-file trigger only: a directory the signaller (e.g. Home
      # Assistant) also mounts. Paths inside must match your hue.yaml.
      # - /srv/hueman/tv-signal:/data/tvsig
    env_file:
      - /srv/hueman/hueman.env      # single line: HUE_APPLICATION_KEY=<key>  (chmod 600)
```

- [ ] **Step 2: Validate the compose file**

Run: `cd deploy && docker compose config -q && cd ..` (if Docker is unavailable: `python3 -c "import yaml,sys; yaml.safe_load(open('deploy/docker-compose.yml'))"`)
Expected: exit 0, no output. (`docker compose config` may warn the env_file is missing on the dev machine — acceptable; the YAML-parse fallback is the gate.)

- [ ] **Step 3: Write `deploy/systemd/hueman-circadian.service`**

```ini
# Circadian daemon as a bare-metal systemd service (no Docker).
# Read deploy/README.md ("Bare metal: venv + systemd") for the setup steps.
# Install: copy to /etc/systemd/system/, edit paths, then
#   systemctl daemon-reload && systemctl enable --now hueman-circadian
[Unit]
Description=hueman circadian daemon
After=network-online.target
Wants=network-online.target

[Service]
User=hueman
# Holds hue.yaml, .hue-pin.json, and logs/ — the daemon resolves its
# relative paths (log file, resume file) from this directory.
WorkingDirectory=/var/lib/hueman
# Single line: HUE_APPLICATION_KEY=<key>   (chmod 600, owned by root)
EnvironmentFile=/etc/hueman/hueman.env
ExecStart=/opt/hueman/.venv/bin/hueman -c hue.yaml circadian run
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 4: Commit**

```bash
git add deploy/docker-compose.yml deploy/systemd/hueman-circadian.service
git commit -m "deploy: add copyable docker-compose and systemd unit examples"
```

### Task 2: `deploy/README.md` — the generic runbook

**Files:**
- Create: `deploy/README.md`

**Interfaces:**
- Consumes: `deploy/docker-compose.yml` and `deploy/systemd/hueman-circadian.service` from Task 1 (linked by relative path).
- Produces: the invariants page that Task 3's Synology doc and Task 4's README pointers reference as `deploy/README.md`.

- [ ] **Step 1: Write `deploy/README.md`**

````markdown
# Deploying the circadian daemon

`hueman circadian run` is a foreground process; deploying it means keeping it
running next to your bridge. Three interchangeable ways below — plain
`docker run`, docker compose, and bare metal under systemd. Synology NAS
users: read this page first, then the deltas in
[`synology/README-docker.md`](synology/README-docker.md).

There is no published container image — every method builds or installs from
a clone of this repo.

## What every deployment needs

**A `hue.yaml` with a `circadian_daemon:` block.** Without one the daemon
exits immediately — which under `restart: always` / `Restart=always` is a
crash loop. At minimum the block needs `zone:` (the zone the daemon drives);
all tunables live in `CircadianDaemonSpec` in `hueman/config.py`.

**The application key, as the `HUE_APPLICATION_KEY` environment variable —
nothing else works.** No code path reads a key file at runtime. Keep the key
in a mode-600 file and have the *host* deliver it as an env var: an
`env_file:`/`EnvironmentFile=` line (preferred — keeps it out of
`docker inspect` and shell history), or `-e HUE_APPLICATION_KEY="$(cat
/srv/hueman/.hue-key)"` inline. Missing key = every API call returns 401 =
crash loop.

**A pre-pinned `.hue-pin.json`.** The default TLS mode records the bridge
certificate's fingerprint on first connect — but the deployments below mount
the pin read-only, so trust-on-first-use cannot happen in place. Run
`hueman auth` (or any command, e.g. `hueman inventory`) once from an
interactive shell to create the pin, then copy it to the data dir.

**The data directory.** The daemon resolves relative paths from its working
directory — `/data` inside the container, `WorkingDirectory=` under systemd.
It needs: `hue.yaml` (read-only is fine), `.hue-pin.json` (read-only), and a
writable `logs/` (the log file defaults to `logs/circadian.log`; the
manual-resume control file `.hue-circadian-resume` also lands here).

**A route to the bridge.** The daemon holds a long-lived SSE (server-sent
events) stream to the bridge on your LAN. In containers use
`network_mode: host` — NAT'd bridge networks tend to silently drop idle SSE
connections. Bare metal just needs LAN reachability.

**Supervision.** Use `restart: always` / `Restart=always`. One caveat: a
manual override (the daemon suspends when you dim or switch the driven zone
yourself) is **not** persisted across a restart — after a restart the daemon
resumes driving the zone on its next in-window tick.

### Verifying a deployment

A healthy start logs, within the first minute:

```
hueman circadian daemon driving '<your zone>'; Ctrl-C to stop.
... INFO circadian daemon driving '<your zone>' (grouped_light ...); interval=60s transition=75s hand_off=...
... INFO drive '<your zone>' -> 91% / 263 mirek (75s fade)
```

then a `drive` line roughly every 60 s while inside the sunrise→hand-off
window. **Container clocks default to UTC** — log timestamps are probably not
your local time; check before concluding the curve is wrong.

## Docker run

```sh
git clone https://github.com/cnewkirk/hueman /opt/hueman
docker build -t hueman:latest /opt/hueman

docker run -d \
  --name hue-circadian \
  --restart always \
  --network host \
  -v /srv/hueman/hue.yaml:/data/hue.yaml:ro \
  -v /srv/hueman/.hue-pin.json:/data/.hue-pin.json:ro \
  -v /srv/hueman/logs:/data/logs \
  --env-file /srv/hueman/hueman.env \
  hueman:latest
```

`/srv/hueman/hueman.env` is one line, `HUE_APPLICATION_KEY=<key>`, chmod 600.

## Docker compose

[`docker-compose.yml`](docker-compose.yml) in this directory is the same
deployment as above. Edit the `/srv/hueman` paths and the `env_file:`
location, then:

```sh
cd /opt/hueman/deploy && docker compose up -d
```

## Bare metal: venv + systemd

For boxes without Docker (a Raspberry Pi, a small home server). Python 3.10+.

```sh
git clone https://github.com/cnewkirk/hueman /opt/hueman
python3 -m venv /opt/hueman/.venv
/opt/hueman/.venv/bin/pip install /opt/hueman

useradd --system --home-dir /var/lib/hueman --create-home hueman
mkdir -p /var/lib/hueman/logs /etc/hueman
# put hue.yaml + a pre-pinned .hue-pin.json in /var/lib/hueman,
# and HUE_APPLICATION_KEY=<key> in /etc/hueman/hueman.env (chmod 600)
chown -R hueman:hueman /var/lib/hueman

cp /opt/hueman/deploy/systemd/hueman-circadian.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now hueman-circadian
journalctl -u hueman-circadian -f   # verify (see "Verifying a deployment")
```

The unit file ([`systemd/hueman-circadian.service`](systemd/hueman-circadian.service))
sets `WorkingDirectory=/var/lib/hueman`, so the daemon's relative paths (log
file, resume file) land there.

## TV-bias signalling (optional)

If you use the `bias.triggers.control_file` trigger (see the README's TV-bias
section), the daemon and whatever signals it (e.g. a Home Assistant
container) must both see **the same directory** — the signaller touches
`.tv-on`/`.tv-off` there and the daemon polls them. In the container methods
that means one extra shared volume (the commented `tvsig` mount in the
compose file) whose container-side path matches the file paths in your
`hue.yaml`. Omit the shared mount and bias silently never follows the TV.
The `sse` trigger (bridge scene recall) needs no shared storage.

## No daemon? Daily `apply` instead

If you only use the native `circadian_scene:` mode, there is no resident
process to run — but the generated timeslots are anchored to the sun at the
moment `apply` runs, so schedule `hueman apply --yes` daily with any
scheduler (cron, systemd timer). [`synology/README.md`](synology/README.md)
is a worked example of that approach.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Container/service exits immediately | No `circadian_daemon:` block, bad config, or a missing mount | Read the logs — the parse error names the problem |
| Crash loop, logs show 401 | `HUE_APPLICATION_KEY` not delivered | Check the env file path and contents |
| `TLS pin mismatch` on connect | Bridge replaced or cert rotated | Delete and re-create `.hue-pin.json` from an interactive shell, redeploy |
| SSE stream drops / reconnects | Bridge reboot or LAN blip | Self-heals; if constant, check `network_mode: host` |
| `BLOCKED` on apply | Zone/scene name not on the bridge | Run `hueman plan` from your dev machine |
````

- [ ] **Step 2: Verify links resolve**

Run: `ls deploy/docker-compose.yml deploy/systemd/hueman-circadian.service deploy/synology/README-docker.md deploy/synology/README.md`
Expected: all four paths listed (no error).

- [ ] **Step 3: Commit**

```bash
git add deploy/README.md
git commit -m "deploy: platform-neutral runbook (docker run / compose / systemd)"
```

### Task 3: Slim the Synology doc to platform deltas

**Files:**
- Modify: `deploy/synology/README-docker.md` (full replacement; currently 183 lines)

**Interfaces:**
- Consumes: `deploy/README.md` (Task 2) as `../README.md`.

- [ ] **Step 1: Replace the file's entire contents with:**

````markdown
# Synology notes — circadian daemon in Container Manager

**Read [`../README.md`](../README.md) first.** Everything about the
deployment itself — required mounts, key delivery, TLS pinning, the
`circadian_daemon:` block, verification, troubleshooting — lives there and
is not repeated here. This page is only what is *different* on a Synology
NAS (DSM with Container Manager).

Substitute `<share>` with your share name (e.g. `docker`).

## Layout

Keep everything under one folder on the share:

```
/volume1/<share>/hueman/
  hue.yaml            # config (mounted ro)
  .hue-key            # application key, chmod 600 (read by the host shell)
  .hue-pin.json       # TLS pin, chmod 600 (pre-pinned; mounted ro)
  logs/               # writable mount
  build/              # plain build context — see below
```

Use `/volume1/<share>/hueman` wherever the generic doc says `/srv/hueman`.

## Docker over SSH needs the full path (and sudo)

Container Manager's Docker Engine is real Docker, but `docker` is not on the
non-interactive PATH and needs root:

```sh
sudo /usr/local/bin/docker ps
```

Every `docker …` command in the generic doc becomes
`sudo /usr/local/bin/docker …` in an SSH session.

## The build context is synced, not cloned

Don't keep a git clone on the NAS. Sync the build inputs from your dev
machine into a plain `build/` folder, then build there:

```sh
# dev machine, from the repo checkout:
rsync -r --delete hueman/ <user>@<nas>:/volume1/<share>/hueman/build/hueman/
rsync pyproject.toml Dockerfile README.md <user>@<nas>:/volume1/<share>/hueman/build/

# NAS:
sudo /usr/local/bin/docker build -t hueman:latest /volume1/<share>/hueman/build
```

DSM ships with the SFTP subsystem disabled, so plain `scp` fails
("subsystem request failed"); use `rsync` as above, or `scp -O`.

Alternatively, skip building on the NAS entirely: Container Manager → Image →
Import lets you load an image `.tar` exported from your dev machine.

## Relationship to the re-anchor cron

[`README.md`](README.md) in this directory documents the older daemon-less
setup: a DSM Task Scheduler cron running `hueman apply` daily. The daemon
supersedes it (it re-anchors continuously). Don't run both — once the
container is confirmed stable, disable the scheduled task.
````

- [ ] **Step 2: Confirm nothing else links to removed anchors**

Run: `grep -rn "README-docker" README.md CLAUDE.md examples/ docs/ deploy/ | grep -v superpowers`
Expected: only plain-file links (no `#section` anchors into the removed content). Fix any that exist.

- [ ] **Step 3: Commit**

```bash
git add deploy/synology/README-docker.md
git commit -m "deploy: slim Synology runbook to platform deltas"
```

### Task 4: README pointers, final verification, PR

**Files:**
- Modify: `README.md:135-136` (Run-the-daemon step), `README.md:178-179` (re-anchor mention), `README.md:227-228` (Limitations mention)

**Interfaces:**
- Consumes: `deploy/README.md` (Task 2).

- [ ] **Step 1: Flip the three README pointers**

`README.md:135-136` — replace:
```markdown
7. **Run the daemon** (foreground; use Docker/systemd to keep it up — a Synology
   Docker runbook lives in `deploy/synology/README-docker.md`):
```
with:
```markdown
7. **Run the daemon** (foreground; deployment runbook — docker run, compose,
   or systemd — in `deploy/README.md`, Synology notes in `deploy/synology/`):
```

`README.md:178-179` — replace:
```markdown
the daemon, schedule a daily `apply` (cron wrapper in `deploy/synology/`,
superseded by the Docker daemon runbook when the daemon is in use).
```
with:
```markdown
the daemon, schedule a daily `apply` (see "No daemon?" in `deploy/README.md`;
superseded by the daemon when the daemon is in use).
```

`README.md:227-228` — replace:
```markdown
- The daemon runs in the foreground; pair it with Docker (`--restart always`),
  `systemd`, or similar. On a manual override it suspends until a power-cycle of
```
with:
```markdown
- The daemon runs in the foreground; pair it with Docker (`--restart always`),
  `systemd`, or similar — see `deploy/README.md`. On a manual override it
  suspends until a power-cycle of
```
(keep the rest of that bullet's lines unchanged).

- [ ] **Step 2: Full verification**

```bash
python3 -m pytest -q                       # expect: 307+ passed
python3 -c "import yaml; yaml.safe_load(open('deploy/docker-compose.yml'))"
# Deny-list grep: run the pattern set from the split spec (private ops repo)
# over deploy/ and README.md. The pattern list itself must never be committed
# to this public repo.
```
Expected: tests green; YAML parses; deny-list grep returns nothing (exit 1).

- [ ] **Step 3: Commit and PR**

```bash
git add README.md
git commit -m "docs: point README at the generic deployment runbook"
git push -u origin deploy-docs
gh pr create --title "deploy: platform-neutral runbook + compose/systemd examples, Synology doc slimmed to deltas" \
  --body "Implements docs/superpowers/specs/2026-07-04-generic-deployment-docs-design.md"
```
Then merge per repo convention (squash) and pull main.
