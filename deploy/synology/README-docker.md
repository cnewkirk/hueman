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

## TV-bias shared mount needs world-writable permissions

If you use the TV-bias control-file trigger (see "TV-bias signalling" in the
generic doc), Container Manager tends to run the two containers under
mismatched UIDs — create the shared signal directory world-writable once:

```sh
mkdir -p /volume1/<share>/homeassistant/tv-signal
chmod 777 /volume1/<share>/homeassistant/tv-signal
```

## Relationship to the re-anchor cron

[`README.md`](README.md) in this directory documents the older daemon-less
setup: a DSM Task Scheduler cron running `hueman apply` daily. The daemon
supersedes it (it re-anchors continuously). Don't run both — once the
container is confirmed stable, disable the scheduled task.
