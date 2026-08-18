"""FastAPI backend for the local dashboard: storage gauges, live job
progress, system health, config toggles, and on-demand trigger buttons.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

import psutil
import yaml
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

from src import config as config_module
from src import export_engine, file_browser, ingest_engine, state, wifi_manager

logger = logging.getLogger("mediabackup.web")

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


class ConfigToggles(BaseModel):
    cloud_sync_enabled: Optional[bool] = None
    export_filter_by_age: Optional[bool] = None
    gpx_match_tolerance_seconds: Optional[int] = None


class WifiConnectRequest(BaseModel):
    ssid: str
    password: Optional[str] = None


class WifiForgetRequest(BaseModel):
    name: str


class CloudSyncRemote(BaseModel):
    name: str
    source: str
    remote_path: str
    enabled: bool = True


class CloudSyncSettings(BaseModel):
    enabled: Optional[bool] = None
    daily_start_time: Optional[str] = None
    schedule_minutes: Optional[int] = None
    bandwidth_limit: Optional[str] = None


class EjectRequest(BaseModel):
    devnode: str


def _disk_gauge(path: str) -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(path)
        return {
            "path": path,
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "percent_used": round(usage.used / usage.total * 100, 1) if usage.total else 0,
            "available": True,
        }
    except FileNotFoundError:
        return {"path": path, "available": False}


def _cpu_temperature_celsius() -> Optional[float]:
    thermal_zone = Path("/sys/class/thermal/thermal_zone0/temp")
    if thermal_zone.is_file():
        try:
            return int(thermal_zone.read_text().strip()) / 1000.0
        except (ValueError, OSError):
            return None
    return None


ACTIVE_DEVICE_STATUSES = ("mounted", "safe_to_eject", "export_complete")


def assemble_storage_bays(config: dict[str, Any], devices: dict[str, Any]) -> list[dict[str, Any]]:
    """Map currently-connected devices onto the configured physical bays
    (web.storage_bays), preserving the configured display order. A bay is
    'connected' when some active device's udev ID_PATH contains the bay's
    `match` fragment; its gauge reads that device's mount point."""
    bays = []
    for bay in config.get("web", {}).get("storage_bays", []):
        match = bay.get("match") or ""
        entry: dict[str, Any] = {"name": bay["name"], "connected": False}
        if match:
            for devnode, dev in devices.items():
                if dev.get("status") not in ACTIVE_DEVICE_STATUSES:
                    continue
                if match in (dev.get("id_path") or ""):
                    entry["connected"] = True
                    entry["devnode"] = devnode
                    entry["status"] = dev.get("status")
                    if dev.get("mount_point"):
                        entry.update(_disk_gauge(dev["mount_point"]))
                        entry["connected"] = entry.pop("available", True)
                    break
        bays.append(entry)
    return bays


def _persist_config(cfg: dict[str, Any]) -> None:
    with open(config_module.DEFAULT_CONFIG_PATH, "w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh, sort_keys=False)


def tail_log(config: dict[str, Any], lines: int = 300) -> str:
    """Return the last `lines` lines of the daemon log file."""
    log_path = Path(config["paths"].get("log_dir", "logs")) / "mediabackup.log"
    if not log_path.is_file():
        return f"(no log file yet at {log_path})"
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            return "".join(collections.deque(fh, maxlen=max(1, min(lines, 5000))))
    except OSError as exc:
        return f"(could not read {log_path}: {exc})"


def usb_diagnostics() -> dict[str, Any]:
    """Gather read-only USB/block-device info for troubleshooting a drive
    that isn't being recognised. Every command is best-effort; missing
    tools just yield an empty section rather than failing the whole call."""
    def run(cmd: list[str]) -> str:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            return (result.stdout or result.stderr).strip()
        except (FileNotFoundError, subprocess.SubprocessError) as exc:
            return f"({' '.join(cmd)} unavailable: {exc})"

    return {
        "lsblk": run(["lsblk", "-o", "NAME,SIZE,FSTYPE,LABEL,MOUNTPOINT,MODEL"]),
        "by_path": run(["ls", "-la", "/dev/disk/by-path/"]),
        "lsusb": run(["lsusb"]),
        "dmesg_tail": run(["dmesg", "--time-format", "iso"]).splitlines()[-25:],
    }


def _system_health() -> dict[str, Any]:
    root_usage = shutil.disk_usage("/")
    return {
        "cpu_percent": psutil.cpu_percent(interval=None),
        "cpu_temp_celsius": _cpu_temperature_celsius(),
        "ram_percent": psutil.virtual_memory().percent,
        "ram_used_bytes": psutil.virtual_memory().used,
        "ram_total_bytes": psutil.virtual_memory().total,
        "sd_card_used_bytes": root_usage.used,
        "sd_card_total_bytes": root_usage.total,
    }


def create_app(config: dict[str, Any], sync_scheduler=None, hardware_feedback=None, monitor=None) -> FastAPI:
    app = FastAPI(title="Portable Media Backup & Cloud Sync")
    app.state.config = config
    app.state.sync_scheduler = sync_scheduler
    app.state.hardware_feedback = hardware_feedback
    app.state.monitor = monitor

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        index_path = TEMPLATES_DIR / "index.html"
        return index_path.read_text(encoding="utf-8")

    @app.get("/api/status")
    async def status() -> JSONResponse:
        snapshot = state.snapshot()
        return JSONResponse({
            "storage": {"bays": assemble_storage_bays(app.state.config, snapshot.get("devices", {}))},
            "health": _system_health(),
            "jobs_and_devices": snapshot,
        })

    @app.get("/api/config")
    async def get_config() -> JSONResponse:
        return JSONResponse(app.state.config)

    @app.post("/api/config")
    async def update_config(toggles: ConfigToggles) -> JSONResponse:
        cfg = app.state.config
        if toggles.cloud_sync_enabled is not None:
            cfg["cloud_sync"]["enabled"] = toggles.cloud_sync_enabled
        if toggles.export_filter_by_age is not None:
            cfg["export"]["filter_by_age"] = toggles.export_filter_by_age
        if toggles.gpx_match_tolerance_seconds is not None:
            cfg["ingest"]["gpx_match_tolerance_seconds"] = toggles.gpx_match_tolerance_seconds

        _persist_config(cfg)
        return JSONResponse({"ok": True, "config": cfg})

    @app.get("/api/cloud_sync")
    async def get_cloud_sync() -> JSONResponse:
        return JSONResponse(app.state.config["cloud_sync"])

    @app.post("/api/cloud_sync/settings")
    async def update_cloud_sync_settings(settings: CloudSyncSettings) -> JSONResponse:
        cloud_cfg = app.state.config["cloud_sync"]
        if settings.enabled is not None:
            cloud_cfg["enabled"] = settings.enabled
        if settings.daily_start_time is not None:
            cloud_cfg["daily_start_time"] = settings.daily_start_time
        if settings.schedule_minutes is not None:
            cloud_cfg["schedule_minutes"] = settings.schedule_minutes
        if settings.bandwidth_limit is not None:
            cloud_cfg["bandwidth_limit"] = settings.bandwidth_limit
        _persist_config(app.state.config)
        return JSONResponse({"ok": True, "cloud_sync": cloud_cfg})

    @app.post("/api/cloud_sync/remotes")
    async def upsert_cloud_sync_remote(remote: CloudSyncRemote) -> JSONResponse:
        remotes = app.state.config["cloud_sync"]["remotes"]
        existing = next((r for r in remotes if r["name"] == remote.name), None)
        if existing is not None:
            existing.update(remote.model_dump())
        else:
            remotes.append(remote.model_dump())
        _persist_config(app.state.config)
        return JSONResponse({"ok": True, "remotes": remotes})

    @app.delete("/api/cloud_sync/remotes/{name}")
    async def delete_cloud_sync_remote(name: str) -> JSONResponse:
        remotes = app.state.config["cloud_sync"]["remotes"]
        remaining = [r for r in remotes if r["name"] != name]
        if len(remaining) == len(remotes):
            return JSONResponse({"ok": False, "error": f"no remote named '{name}'"}, status_code=404)
        remotes[:] = remaining
        _persist_config(app.state.config)
        return JSONResponse({"ok": True, "remotes": remotes})

    @app.get("/api/wifi/status")
    async def wifi_status() -> JSONResponse:
        try:
            return JSONResponse(await asyncio.get_event_loop().run_in_executor(None, wifi_manager.current_status))
        except wifi_manager.WifiError as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    @app.get("/api/wifi/scan")
    async def wifi_scan() -> JSONResponse:
        try:
            networks = await asyncio.get_event_loop().run_in_executor(None, wifi_manager.scan_networks)
            return JSONResponse({"ok": True, "networks": networks})
        except wifi_manager.WifiError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @app.get("/api/wifi/saved")
    async def wifi_saved() -> JSONResponse:
        try:
            saved = await asyncio.get_event_loop().run_in_executor(None, wifi_manager.list_saved_connections)
            return JSONResponse({"ok": True, "saved": saved})
        except wifi_manager.WifiError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @app.post("/api/wifi/connect")
    async def wifi_connect(req: WifiConnectRequest) -> JSONResponse:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, wifi_manager.connect, req.ssid, req.password)
        return JSONResponse(result, status_code=200 if result.get("ok") else 502)

    @app.post("/api/wifi/forget")
    async def wifi_forget(req: WifiForgetRequest) -> JSONResponse:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, wifi_manager.forget, req.name)
        return JSONResponse(result, status_code=200 if result.get("ok") else 502)

    @app.get("/api/files/roots")
    async def files_roots() -> JSONResponse:
        return JSONResponse({"roots": file_browser.get_roots(app.state.config)})

    @app.get("/api/files/list")
    async def files_list(root: str, path: str = "") -> JSONResponse:
        try:
            return JSONResponse(file_browser.list_dir(app.state.config, root, path))
        except file_browser.FileBrowserError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @app.get("/api/files/download")
    async def files_download(root: str, path: str):
        try:
            target = file_browser.resolve_download_path(app.state.config, root, path)
        except file_browser.FileBrowserError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return FileResponse(target, filename=target.name)

    @app.get("/api/logs", response_class=PlainTextResponse)
    async def get_logs(lines: int = 300) -> str:
        return tail_log(app.state.config, lines)

    @app.get("/api/usb/diagnostics")
    async def usb_diag() -> JSONResponse:
        data = await asyncio.get_event_loop().run_in_executor(None, usb_diagnostics)
        return JSONResponse(data)

    @app.post("/api/eject")
    async def eject(req: EjectRequest) -> JSONResponse:
        if app.state.monitor is None:
            return JSONResponse({"ok": False, "error": "hotplug monitor not running"}, status_code=503)
        ok, message = await asyncio.get_event_loop().run_in_executor(None, app.state.monitor.eject, req.devnode)
        if ok:
            return JSONResponse({"ok": True})
        return JSONResponse({"ok": False, "error": message}, status_code=409)

    @app.post("/api/trigger/sync")
    async def trigger_sync() -> JSONResponse:
        if app.state.sync_scheduler is None:
            return JSONResponse({"ok": False, "error": "sync scheduler not running"}, status_code=503)
        app.state.sync_scheduler.trigger_now()
        return JSONResponse({"ok": True})

    @app.post("/api/trigger/mirror")
    async def trigger_mirror() -> JSONResponse:
        if app.state.monitor is None:
            return JSONResponse({"ok": False, "error": "hotplug monitor not running"}, status_code=503)
        if not app.state.monitor.is_role_mounted("secondary_ssd_mirror"):
            return JSONResponse({"ok": False, "error": "Mirror SSD is not connected"}, status_code=409)
        # The export itself can run for minutes - kick it off in the
        # background and return immediately; progress shows up in the
        # jobs list like any other export.
        asyncio.get_event_loop().run_in_executor(
            None, app.state.monitor.trigger_role_export, "secondary_ssd_mirror", True
        )
        return JSONResponse({"ok": True})

    @app.post("/api/trigger/ingest")
    async def trigger_ingest() -> JSONResponse:
        cfg = app.state.config
        mount_point = cfg["paths"]["source_sd_mount"]
        if not Path(mount_point).is_mount():
            return JSONResponse({"ok": False, "error": f"no SD card mounted at {mount_point}"}, status_code=409)
        asyncio.get_event_loop().run_in_executor(None, ingest_engine.handle_sd_card, cfg, mount_point)
        return JSONResponse({"ok": True})

    @app.post("/api/trigger/export")
    async def trigger_export() -> JSONResponse:
        cfg = app.state.config
        mount_point = cfg["paths"]["external_ssd_mount"]
        if not Path(mount_point).is_mount():
            return JSONResponse({"ok": False, "error": f"no external SSD mounted at {mount_point}"}, status_code=409)
        asyncio.get_event_loop().run_in_executor(None, export_engine.handle_external_ssd, cfg, mount_point)
        return JSONResponse({"ok": True})

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            while True:
                snapshot = state.snapshot()
                await websocket.send_json({
                    "storage": {"bays": assemble_storage_bays(app.state.config, snapshot.get("devices", {}))},
                    "health": _system_health(),
                    "jobs_and_devices": snapshot,
                })
                await asyncio.sleep(1.0)
        except WebSocketDisconnect:
            logger.debug("Dashboard websocket client disconnected")

    return app
