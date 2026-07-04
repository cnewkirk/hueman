# Synology daily re-anchor — runbook

Runs `hueman apply` once a day on the NAS so the bridge's circadian cycle
re-anchors to the real sun. DST is handled inside the tool (`location.tz`), so
nothing here needs a seasonal edit.

## Layout
```
$HUE_IAC_HOME/            # e.g. /volume1/<share>/hueman
  src/                    # git clone of this repo
  .venv/                  # python3 -m venv; pip install -e ./src
  hue.yaml  .hue-key  .hue-pin.json  .hue-backup/
  bin/re-anchor.sh  logs/
```

## Install (over SSH, one time)
```sh
HUE_IAC_HOME=/volume1/<share>/hueman
mkdir -p "$HUE_IAC_HOME" && cd "$HUE_IAC_HOME"
git clone https://github.com/cnewkirk/hueman.git src
python3 -m venv .venv
.venv/bin/pip install -e ./src        # pulls requests, PyYAML, tzdata
mkdir -p bin logs .hue-backup
cp src/deploy/synology/re-anchor.sh bin/ && chmod +x bin/re-anchor.sh
# From the Mac, copy secrets + config:
#   scp .hue-key .hue-pin.json hue.yaml <nas>:$HUE_IAC_HOME/
chmod 600 .hue-key .hue-pin.json
# Ensure hue.yaml's location: block has  tz: America/Los_Angeles
```

## Verify (before scheduling)
```sh
cd "$HUE_IAC_HOME"
export HUE_APPLICATION_KEY=$(cat .hue-key)
.venv/bin/hueman -c hue.yaml validate
.venv/bin/hueman -c hue.yaml plan                     # NOOP / time deltas, ZERO blocked
sh bin/re-anchor.sh && tail -n 20 logs/re-anchor.log   # exit 0, "re-anchor OK"
.venv/bin/hueman -c hue.yaml plan                     # NOOP (idempotent)
.venv/bin/hueman -c hue.yaml preview --date 2026-12-15
.venv/bin/hueman -c hue.yaml preview --date 2026-07-15  # sunrise ~1h apart => DST works
```

## Schedule (DSM GUI — the one manual step)
Control Panel → Task Scheduler → Create → Scheduled Task → User-defined script
- **User:** `root`
- **Schedule:** Daily, **03:30**
- **Run command:** `sh /volume1/<share>/hueman/bin/re-anchor.sh`
- **Notification:** email → "only when the script terminates abnormally"

Then press **Run** once and check `logs/re-anchor.log`.

## Failure modes (all → non-zero exit → DSM email)
- Bridge unreachable (NAS off-LAN / DNS) — `apply` errors.
- TLS pin mismatch (bridge cert rotated) — refresh `.hue-pin.json` from the Mac.
- Unexpected `BLOCKED` (e.g. a zone went missing) — `apply` refuses and prints why.
