# RPI4-Photo-Backup

[![CI](https://github.com/phantompants/RPI4-Photo-Backup/actions/workflows/ci.yml/badge.svg)](https://github.com/phantompants/RPI4-Photo-Backup/actions/workflows/ci.yml)

Portable Media Backup & Cloud Sync System — an RPi5-based (RPi4-compatible) daemon for
mirroring SD cards, extracting/renaming media by EXIF/GPX metadata, exporting to an
external SSD (camera-compatible or standard structured layout), and syncing to the cloud
or a NAS, with GPIO/M5Stack hardware feedback and a local web dashboard.

## How it works

**SD card ingestion.** When an SD card is inserted, `hotplug_monitor.py` classifies the
new partition (SD card reader vs. external SSD) using udev metadata and capacity, then
mounts it read-only. `ingest_engine.py` derives a stable card ID (filesystem UUID, or a
hash of the card's file structure as a fallback), incrementally mirrors new/changed files
into `SD_Mirror_[CARD_ID]/` on the internal SSD, then runs a processed pass: it reads
EXIF date/time, GPS, and camera model from each media file, falls back to matching GPX
track logs by timestamp when EXIF has no GPS fix, and renames/copies the result into
`Processed_Media/` as `YYYYMMDD-HHMMSS-originalfilename.ext`. A manifest tracks what's
already been processed so re-inserting the same card is a no-op.

**External SSD export.** When an SSD is inserted, `export_engine.py` checks for a
`.camera_emulation.yaml` marker at its root. If present, it reconstructs a
camera-compatible DCIM layout (configurable folder/prefix/counter) so the drive can be
read back like a camera's own storage. Otherwise it exports into a standard
`YYYY/YYYY-MM/YYYY-MM-DD/camera/filetype/resolution/` hierarchy driven by the EXIF/GPS
metadata captured during ingestion, optionally limited to files modified within the last
N days.

**Cloud/NAS sync.** `sync_scheduler.py` runs `rclone` against configured remotes on a
schedule and on demand, plus an optional NAS `rsync` sync, reporting live progress.

**Feedback & control.** `hardware_feedback.py` drives GPIO status LEDs (blue while
ingesting, green when safe to eject, red on error, pulsing/solid amber during SSD export)
and forwards the same status to an M5Stack over serial or websocket, with an audible
alert on job completion. `web_server.py` serves a dependency-free single-file dashboard
(`templates/index.html`) showing storage gauges, live job progress, system health, config
toggles, and manual trigger buttons, backed by a FastAPI API and websocket feed.

All modules report progress through `state.py`, a thread-safe JSON-backed store, so the
dashboard and hardware feedback stay in sync with whatever `ingest_engine`/`export_engine`/
`sync_scheduler` are doing without being directly coupled to them.

## Layout

```
config/
  main_config.yaml       # paths, ingest/export behaviour, alerts, cloud/NAS sync
  rclone.conf             # rclone remotes (template - populate with `rclone config`)
src/
  main.py                 # entry point, wires everything together
  config.py               # YAML config loader + validation
  state.py                # shared JSON-backed job/device status store
  hotplug_monitor.py      # pyudev SD/SSD hotplug detection + mounting
  ingest_engine.py        # card ID, incremental mirror copy, EXIF/GPX processed pass
  export_engine.py        # external SSD camera-emulation vs standard structured export
  sync_scheduler.py       # rclone cloud sync + NAS rsync, scheduled and on-demand
  hardware_feedback.py    # GPIO status LEDs, M5Stack serial/websocket, audible alerts
  web_server.py           # FastAPI dashboard backend
templates/
  index.html               # single-file dashboard (no external dependencies)
systemd/
  mediabackup.service      # systemd unit for running as a daemon
tests/                     # unit tests for hotplug classification + ingestion
.github/workflows/ci.yml   # config validation + test suite on every push/PR
```

## Setup (Raspberry Pi OS Lite, 64-bit Bookworm)

```bash
sudo mkdir -p /opt/mediabackup
sudo cp -r . /opt/mediabackup
cd /opt/mediabackup
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Edit `config/main_config.yaml` to match your mount points, alert hardware, and
cloud/NAS destinations. Populate `config/rclone.conf` by running `rclone config`
(or `rclone config create <name> <type> ...`) for any cloud remotes referenced
under `cloud_sync.remotes`.

Validate the config before installing the service:

```bash
python3 src/main.py --check-config
```

## Running

Directly (foreground, useful for development):

```bash
python3 src/main.py
```

As a systemd service:

```bash
sudo cp systemd/mediabackup.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mediabackup.service
sudo journalctl -u mediabackup -f
```

The dashboard is then available at `http://<pi-address>:8080/`.

## Tests

```bash
pip install -r requirements.txt pytest
python -m pytest tests/ -v
```

CI runs the same config validation and test suite on every push and pull request against
`main` (see `.github/workflows/ci.yml`).
