"""Cloud (rclone) and NAS (rsync/ssh) sync manager.

Runs on a configurable interval and can also be triggered on demand from the
web dashboard. Progress for each sync run is reported through `state.py` the
same way ingest/export jobs are, so the dashboard can show it consistently.
"""
from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
import uuid
from typing import Any, Optional

from src import state

logger = logging.getLogger("mediabackup.sync")

RCLONE_STATS_RE = re.compile(
    r"Transferred:\s+([\d.]+\s*\w+)\s*/\s*([\d.]+\s*\w+),\s*(\d+)%,\s*([\d.]+\s*\w+/s),\s*ETA\s+(\S+)"
)


def _parse_size_to_bytes(text: str) -> int:
    text = text.strip()
    match = re.match(r"([\d.]+)\s*([A-Za-z]*)", text)
    if not match:
        return 0
    value, unit = float(match.group(1)), match.group(2).lower()
    multipliers = {"": 1, "b": 1, "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3,
                   "kb": 1000, "mb": 1000 ** 2, "gb": 1000 ** 3, "tib": 1024 ** 4}
    return int(value * multipliers.get(unit, 1))


def run_rclone_sync(remote: dict[str, Any], config: dict[str, Any], job_id: Optional[str] = None) -> bool:
    cloud_cfg = config["cloud_sync"]
    job_id = job_id or f"cloud-sync-{remote['name']}-{uuid.uuid4().hex[:8]}"
    state.start_job(job_id, "cloud_sync", extra={"remote": remote["name"]})
    progress = state.JobProgress(job_id)

    cmd = [
        "rclone", "sync", remote["source"], remote["remote_path"],
        "--config", cloud_cfg["rclone_config_path"],
        "--stats", "2s", "--stats-one-line", "-v",
    ]
    bwlimit = cloud_cfg.get("bandwidth_limit")
    if bwlimit:
        cmd += ["--bwlimit", bwlimit]

    logger.info("Starting rclone sync: %s -> %s", remote["source"], remote["remote_path"])
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:  # type: ignore[union-attr]
            match = RCLONE_STATS_RE.search(line)
            if match:
                done_s, total_s, _pct, speed_s, eta_s = match.groups()
                bytes_done = _parse_size_to_bytes(done_s)
                bytes_total = _parse_size_to_bytes(total_s)
                progress.update(bytes_done, bytes_total, speed_text=speed_s, eta_text=eta_s)
        returncode = proc.wait()
        if returncode == 0:
            progress.finish("success")
            logger.info("rclone sync for %s completed successfully", remote["name"])
            return True
        progress.finish("error", error=f"rclone exited with code {returncode}")
        logger.error("rclone sync for %s failed with code %s", remote["name"], returncode)
        return False
    except FileNotFoundError:
        progress.finish("error", error="rclone binary not found")
        logger.error("rclone is not installed or not on PATH")
        return False
    except Exception as exc:
        progress.finish("error", error=str(exc))
        logger.exception("rclone sync for %s raised an exception", remote["name"])
        return False


def run_nas_sync(config: dict[str, Any], job_id: Optional[str] = None) -> bool:
    nas_cfg = config["nas_sync"]
    if not nas_cfg.get("enabled"):
        return False

    job_id = job_id or f"nas-sync-{uuid.uuid4().hex[:8]}"
    state.start_job(job_id, "nas_sync")
    progress = state.JobProgress(job_id)

    destination = f"{nas_cfg['user']}@{nas_cfg['host']}:{nas_cfg['remote_path']}"
    cmd = ["rsync", "-avz", "--info=progress2", nas_cfg["source"] + "/", destination]

    logger.info("Starting NAS sync: %s -> %s", nas_cfg["source"], destination)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            progress.finish("success")
            logger.info("NAS sync completed successfully")
            return True
        progress.finish("error", error=result.stderr[-500:])
        logger.error("NAS sync failed: %s", result.stderr[-500:])
        return False
    except FileNotFoundError:
        progress.finish("error", error="rsync binary not found")
        return False
    except Exception as exc:
        progress.finish("error", error=str(exc))
        logger.exception("NAS sync raised an exception")
        return False


def sync_all(config: dict[str, Any]) -> None:
    cloud_cfg = config["cloud_sync"]
    any_cloud_success = False
    if cloud_cfg.get("enabled"):
        for remote in cloud_cfg.get("remotes", []):
            if remote.get("enabled"):
                if run_rclone_sync(remote, config):
                    any_cloud_success = True
    run_nas_sync(config)
    if any_cloud_success:
        # Clear the 'unsynced changes' flag so the daily trigger won't
        # re-run tonight unless new media arrives after this point.
        state.mark_cloud_sync()


class SyncScheduler:
    """Background thread that periodically runs `sync_all` and can also be
    triggered on demand (e.g. from the web dashboard's "Sync Now" button)."""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="sync-scheduler", daemon=True)
        self._thread.start()
        logger.info("Sync scheduler started (interval=%s minutes)",
                    self.config["cloud_sync"].get("schedule_minutes", 60))

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def trigger_now(self) -> None:
        threading.Thread(target=sync_all, args=(self.config,), name="sync-now", daemon=True).start()

    def _loop(self) -> None:
        interval_seconds = max(self.config["cloud_sync"].get("schedule_minutes", 60), 1) * 60
        while not self._stop_event.wait(interval_seconds):
            try:
                sync_all(self.config)
            except Exception:
                logger.exception("Scheduled sync run failed")
