"""Thread-safe JSON-backed shared state for job progress and device status.

Every long running job (ingest mirror/process pass, export pass, cloud sync)
reports its progress here so that `web_server.py` and `hardware_feedback.py`
can observe it without being directly coupled to the worker code.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

_lock = threading.RLock()
_listeners: list[Callable[[dict[str, Any]], None]] = []
_state: dict[str, Any] = {
    "jobs": {},       # job_id -> job progress dict
    "devices": {},    # device path -> last known device status
    "system": "idle",  # idle | ingesting | ssd_copying | error
    "last_change_at": 0.0,      # epoch of last new-media change (ingest wrote files)
    "last_cloud_sync_at": 0.0,  # epoch of last successful cloud sync
}
_state_file: Path | None = None


def configure(state_dir: str | Path) -> None:
    global _state_file
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    _state_file = state_dir / "status.json"
    _load()


def subscribe(callback: Callable[[dict[str, Any]], None]) -> None:
    with _lock:
        _listeners.append(callback)


def _load() -> None:
    if _state_file and _state_file.is_file():
        try:
            with open(_state_file, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            with _lock:
                _state.update(loaded)
        except (json.JSONDecodeError, OSError):
            pass


def _persist_locked() -> None:
    if not _state_file:
        return
    tmp = _state_file.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(_state, fh, indent=2, sort_keys=True)
    tmp.replace(_state_file)


def snapshot() -> dict[str, Any]:
    with _lock:
        return json.loads(json.dumps(_state))


def set_system_status(status: str) -> None:
    with _lock:
        _state["system"] = status
        _persist_locked()
    _notify()


def mark_change() -> None:
    """Record that new media landed in Processed_Media, so the daily cloud
    sync knows there's something to upload today."""
    with _lock:
        _state["last_change_at"] = time.time()
        _persist_locked()


def mark_cloud_sync() -> None:
    """Record a successful cloud sync, clearing the 'unsynced changes' state."""
    with _lock:
        _state["last_cloud_sync_at"] = time.time()
        _persist_locked()


def has_unsynced_changes() -> bool:
    """True when media changed more recently than the last successful cloud
    sync - i.e. there's something new worth uploading."""
    with _lock:
        return _state.get("last_change_at", 0.0) > _state.get("last_cloud_sync_at", 0.0)


def update_device(device_path: str, **fields: Any) -> None:
    with _lock:
        entry = _state["devices"].setdefault(device_path, {})
        entry.update(fields)
        entry["updated_at"] = time.time()
        _persist_locked()
    _notify()


def start_job(job_id: str, job_type: str, total_bytes: int = 0, extra: dict | None = None) -> None:
    with _lock:
        _state["jobs"][job_id] = {
            "type": job_type,
            "status": "running",
            "bytes_done": 0,
            "bytes_total": total_bytes,
            "files_done": 0,
            "files_total": 0,
            "speed_bps": 0.0,
            "eta_seconds": None,
            "started_at": time.time(),
            "updated_at": time.time(),
            "error": None,
            **(extra or {}),
        }
        _persist_locked()
    _notify()


class JobProgress:
    """Helper bound to a single job_id that tracks byte/file counters and
    derives speed + ETA, throttled to avoid excessive disk writes."""

    def __init__(self, job_id: str, min_interval: float = 0.5):
        self.job_id = job_id
        self.min_interval = min_interval
        self._last_write = 0.0
        self._start = time.time()

    def update(self, bytes_done: int, bytes_total: int, files_done: int = 0,
               files_total: int = 0, force: bool = False, **extra: Any) -> None:
        now = time.time()
        if not force and (now - self._last_write) < self.min_interval:
            return
        elapsed = max(now - self._start, 1e-6)
        speed = bytes_done / elapsed
        remaining = max(bytes_total - bytes_done, 0)
        eta = (remaining / speed) if speed > 0 else None
        with _lock:
            job = _state["jobs"].get(self.job_id)
            if job is None:
                return
            job.update({
                "bytes_done": bytes_done,
                "bytes_total": bytes_total,
                "files_done": files_done,
                "files_total": files_total,
                "speed_bps": speed,
                "eta_seconds": eta,
                "updated_at": now,
                **extra,
            })
            _persist_locked()
        self._last_write = now
        _notify()

    def finish(self, status: str = "success", error: str | None = None) -> None:
        with _lock:
            job = _state["jobs"].get(self.job_id)
            if job is not None:
                job["status"] = status
                job["error"] = error
                job["updated_at"] = time.time()
                _persist_locked()
        _notify()


def _notify() -> None:
    with _lock:
        snap = json.loads(json.dumps(_state))
        callbacks = list(_listeners)
    for cb in callbacks:
        try:
            cb(snap)
        except Exception:
            pass
