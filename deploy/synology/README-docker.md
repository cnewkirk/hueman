# hue-iac circadian daemon — Synology Container Manager runbook

This document covers running the circadian daemon as a Docker container on the NAS
via Synology Container Manager. The container replaces the re-anchor cron approach:
**the daily `re-anchor.sh` cron (documented in `README.md`) is superseded by this
daemon.** The cron job is retired once this container is confirmed running.

---

## Prerequisites

- Synology NAS with Container Manager (Docker Engine) installed.
- Files already copied to the NAS share (same share used by the re-anchor setup):
  - `hue.yaml` — config with `bridge.host`, `location.tz`, **and a `circadian_daemon:`
    block** (see the requirement below)
  - `.hue-key` — bridge application key (chmod 600); read by the **host shell** to
    populate `$HUE_APPLICATION_KEY`, **not** mounted into the container
  - `.hue-pin.json` — TLS pin (chmod 600); **must be pre-pinned** — the read-only
    container mount cannot perform trust-on-first-use (run `hue-iac auth` once from a
    host shell to create it if starting fresh)

> **`hue.yaml` MUST contain a `circadian_daemon:` block.** `hue-iac circadian run`
> raises `HueIacError("no 'circadian_daemon' block in config")` and the container exits
> immediately (crash-loops under `--restart always`) if it is missing. At minimum the
> block needs `zone:` (the apartment zone the daemon drives). The daemon tunables also
> live here — notably `manual_override.control_file` (the resume-signal file, default
> `.hue-circadian-resume`) and `log.path` (default `logs/circadian.log`, which lands
> under the mounted `/data/logs`). See `hue_iac/config.py` (`CircadianDaemonSpec`) for the
> full set of keys and their defaults. The live `hue.yaml` gets this block added during
> deploy; if you are starting from scratch, add it before building.
>
> **TV bias hold (`circadian_daemon.bias`):** if you enable the **control-file**
> trigger source (`bias.triggers.control_file`), its `on_file`/`off_file` must land
> on a path both this container and the signaller (e.g. a Home Assistant container)
> can write/read — put them under the shared `/data` mount and have HA write there.
> The `sse` source needs no shared mount (HA just recalls a bridge scene/button);
> the `probe` source needs the container to reach the TV's IP (host/LAN networking).

Throughout this document, substitute `<share>` with the actual share name
(e.g. `docker` or the share already used in the re-anchor runbook).

---

## Step 1 — Build the image on the NAS

The NAS keeps a plain **build context** at `$HUE_IAC_HOME/build/` (Dockerfile,
`hue_iac/`, `pyproject.toml`, `README.md`) — **not a git clone** (the live deploy
has no `src/` checkout; source is copied in from the dev machine). From the Mac,
sync the current `main` into it, then build over SSH:

```sh
# On the Mac, from a clean checkout of origin/main:
rsync -r --delete hue_iac/ <user>@<nas>:/volume1/<share>/hue-iac/build/hue_iac/
rsync pyproject.toml Dockerfile <user>@<nas>:/volume1/<share>/hue-iac/build/
# (scp also works but needs -O — Synology has SFTP disabled.)

# On the NAS (docker is not on the non-interactive PATH — use the full path):
sudo /usr/local/bin/docker build -t hue-iac:latest /volume1/<share>/hue-iac/build
```

Alternatively, import a pre-built tarball via Container Manager UI:
Container Manager → Image → Import → select the `.tar` file exported from the Mac.

---

## Step 2 — Start the daemon

```sh
HUE_IAC_HOME=/volume1/<share>/hue-iac

docker run -d \
  --name hue-circadian \
  --restart always \
  --network host \
  -v "$HUE_IAC_HOME/hue.yaml":/data/hue.yaml:ro \
  -v "$HUE_IAC_HOME/.hue-pin.json":/data/.hue-pin.json:ro \
  -v "$HUE_IAC_HOME/logs":/data/logs \
  -v /volume1/<share>/homeassistant/tv-signal:/data/tvsig \
  -e HUE_APPLICATION_KEY="$(cat $HUE_IAC_HOME/.hue-key)" \
  hue-iac:latest
```

**Why the `tv-signal` mount (REQUIRED for TV-bias):** TV detection runs through
Home Assistant's webOS integration, which reads real TV power and pokes
`.tv-on`/`.tv-off` control-files that `circadian_daemon.bias.triggers.control_file`
consumes (see the TV-bias section below and `homeassistant/README.md`). Both
containers must see the same directory: HA writes it via its `/config` mount
(`/volume1/<share>/homeassistant/tv-signal`), the daemon reads it at `/data/tvsig`
(matching the paths in `hue.yaml`). **Omit this mount and TV-bias silently stops
following the TV** — the daemon never sees HA's signals. Create it once with
`mkdir -p /volume1/<share>/homeassistant/tv-signal && chmod 777 …`.

**Why `--network host`:** the bridge is at `192.0.2.2` on the LAN. Host networking
ensures the container can reach that address and keep an SSE stream open without NAT
interference.

**Key delivery:** the key is read **only** from the `HUE_APPLICATION_KEY` environment
variable — no code path in `hue_iac/` reads a mounted key file at runtime (the only
`.hue-key` reference is the `auth` command printing an `export HUE_APPLICATION_KEY=...`
line). The `-e HUE_APPLICATION_KEY="$(cat $HUE_IAC_HOME/.hue-key)"` flag has the **host
shell** read the file and pass its contents as the env var; keep it exactly as shown.
Dropping this flag leaves the container with no key → every API call returns 401 →
`--restart always` enters a crash loop.

> **Security note:** the inline `-e` value is visible via `docker inspect hue-circadian`
> and your shell history. To harden, use `--env-file` instead: create a file (chmod 600)
> containing a single line `HUE_APPLICATION_KEY=<key>` and replace
> `-e HUE_APPLICATION_KEY="$(cat ...)"` with `--env-file /path/to/.hue-env`. This still
> delivers the key via the env var but keeps it out of `docker inspect` and shell history.

**`--restart always`:** the daemon restarts automatically after a crash or a NAS reboot.
No cron job or watchdog is needed.

> **Manual-override note:** a manual override (the daemon enters SUSPENDED) is **not**
> persisted across a container restart or NAS reboot. After restart the daemon comes back
> idle and resumes driving the zone on the next in-window tick, reclaiming a zone the user
> had previously taken over. This is by design; be aware if you rely on manual control
> surviving a reboot.

---

## Step 3 — Verify the daemon is running

```sh
# Tail live logs (Ctrl-C to stop)
docker logs -f hue-circadian

# Check the last 50 lines
docker logs --tail 50 hue-circadian

# Confirm the container is up and its restart policy
docker inspect hue-circadian | grep -E '"Status"|"RestartPolicy"' | head -6
```

Expected: log lines showing the daemon subscribing to the bridge SSE stream and the
first circadian scene applied.

---

## Step 4 — Trigger a manual circadian resume

If the daemon is paused (e.g. you ran `hue-iac circadian pause` for debugging) and you
want to resume without restarting the container:

```sh
docker exec hue-circadian hue-iac -c /data/hue.yaml circadian resume
```

---

## Step 5 — Update the image

Same as Step 1: re-sync the source from the Mac into `build/`, rebuild, then
recreate the container:

```sh
# Mac: rsync hue_iac/ pyproject.toml Dockerfile into …/hue-iac/build/  (Step 1)
# NAS:
sudo /usr/local/bin/docker build -t hue-iac:latest /volume1/<share>/hue-iac/build
sudo /usr/local/bin/docker rm -f hue-circadian
# Re-run the docker run command from Step 2
```

---

## Failure modes

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Container exits immediately | Bad config / missing file mount | `docker logs hue-circadian` for the error |
| SSE stream drops / reconnects | Bridge rebooted or LAN blip | Daemon auto-reconnects; monitor logs |
| TLS pin mismatch | Bridge cert rotated | Refresh `.hue-pin.json` from the Mac, restart container |
| `BLOCKED` on scene apply | Zone or scene not found | Run `hue-iac plan` on the Mac to diagnose |

---

## Relationship to the re-anchor cron

The daily `re-anchor.sh` cron (`README.md`) calls `hue-iac apply` once a day to keep
the circadian smart-scene timed to the real sun. This Docker daemon runs `hue-iac
circadian run` continuously, reacting to SSE events in real time — it includes its own
daily re-anchor logic. Once this daemon is confirmed stable, disable or delete
the Task Scheduler job created in `README.md` to avoid double-applying.
