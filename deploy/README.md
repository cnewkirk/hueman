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
