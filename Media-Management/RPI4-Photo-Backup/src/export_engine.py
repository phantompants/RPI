"""External SSD export: reconstructs either a camera-compatible DCIM layout
or a standard date/camera/filetype/resolution hierarchy from Processed_Media.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import yaml

from src import state
from src.ingest_engine import MANIFEST_NAME, gpx_point_dates

logger = logging.getLogger("mediabackup.export")

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".dng", ".cr2", ".cr3", ".nef", ".arw", ".gpr"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".360"}


def _load_yaml(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _load_processed_metadata(processed_dir: Path) -> dict[str, Any]:
    manifest_path = processed_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        return {}
    with open(manifest_path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    return manifest.get("_metadata", {})


def _filetype_bucket(extension: str) -> str:
    ext = extension.lower()
    if ext in PHOTO_EXTENSIONS:
        return "photo"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    return "other"


def _resolution_bucket(file_path: Path) -> str:
    ext = file_path.suffix.lower()
    if ext in VIDEO_EXTENSIONS and shutil.which("ffprobe"):
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", str(file_path)],
                capture_output=True, text=True, timeout=10,
            )
            resolution = result.stdout.strip()
            if resolution:
                return resolution
        except (subprocess.SubprocessError, OSError):
            pass
    if ext in PHOTO_EXTENSIONS:
        try:
            import exifread
            with open(file_path, "rb") as fh:
                tags = exifread.process_file(fh, details=False)
            width = tags.get("EXIF ExifImageWidth") or tags.get("Image ImageWidth")
            height = tags.get("EXIF ExifImageLength") or tags.get("Image ImageLength")
            if width and height:
                return f"{width}x{height}"
        except Exception:
            pass
    return "unknown_resolution"


def _within_age_limit(file_path: Path, max_age_days: int) -> bool:
    cutoff = datetime.now() - timedelta(days=max_age_days)
    return datetime.fromtimestamp(file_path.stat().st_mtime) >= cutoff


def _iter_export_candidates(processed_dir: Path, export_cfg: dict[str, Any]):
    manifest_name = MANIFEST_NAME
    for entry in sorted(processed_dir.iterdir()):
        if not entry.is_file() or entry.name == manifest_name:
            continue
        if export_cfg.get("filter_by_age") and not _within_age_limit(entry, export_cfg.get("max_age_days", 31)):
            continue
        yield entry


# --------------------------------------------------------------------------
# Camera-compatible mode
# --------------------------------------------------------------------------

def export_camera_emulation(processed_dir: str, external_mount: str, marker_path: Path,
                             export_cfg: dict[str, Any], job_id: Optional[str] = None,
                             labels: Optional[dict[str, str]] = None) -> dict[str, int]:
    emulation_cfg = _load_yaml(marker_path).get("camera_emulation", {})
    folder = emulation_cfg.get("folder", "DCIM/100MEDIA")
    prefix = emulation_cfg.get("file_prefix", "IMG_")
    counter_digits = int(emulation_cfg.get("counter_digits", 4))
    counter = int(emulation_cfg.get("counter_start", 1))
    extension_case = emulation_cfg.get("extension_case", "upper")

    dest_root = Path(external_mount) / folder
    dest_root.mkdir(parents=True, exist_ok=True)

    processed_path = Path(processed_dir)
    candidates = list(_iter_export_candidates(processed_path, export_cfg))
    total_bytes = sum(f.stat().st_size for f in candidates)

    job_id = job_id or f"export-camera-{uuid.uuid4().hex[:8]}"
    state.start_job(job_id, "ssd_export_camera", total_bytes=total_bytes,
                    extra={"files_total": len(candidates), **(labels or {})})
    progress = state.JobProgress(job_id)

    bytes_done = 0
    files_written = 0
    try:
        for entry in candidates:
            ext = entry.suffix.upper() if extension_case == "upper" else entry.suffix.lower()
            dest_name = f"{prefix}{counter:0{counter_digits}d}{ext}"
            dest_file = dest_root / dest_name
            if not dest_file.exists():
                shutil.copy2(entry, dest_file)
                files_written += 1
            counter += 1
            bytes_done += entry.stat().st_size
            progress.update(bytes_done, total_bytes, files_written, len(candidates))

        progress.update(bytes_done, total_bytes, files_written, len(candidates), force=True)
        progress.finish("success")
    except Exception as exc:
        progress.finish("error", error=str(exc))
        raise

    logger.info("Camera-emulation export complete: %d files written to %s", files_written, dest_root)
    return {"files_written": files_written, "bytes_done": bytes_done}


# --------------------------------------------------------------------------
# Standard structured mode
# --------------------------------------------------------------------------

def export_standard(processed_dir: str, external_mount: str, export_cfg: dict[str, Any],
                     job_id: Optional[str] = None, labels: Optional[dict[str, str]] = None) -> dict[str, int]:
    processed_path = Path(processed_dir)
    metadata = _load_processed_metadata(processed_path)
    pattern = export_cfg.get(
        "standard_structure", "{year}/{year}-{month}/{year}-{month}-{day}/{camera}/{filetype}/{resolution}"
    )

    candidates = list(_iter_export_candidates(processed_path, export_cfg))
    total_bytes = sum(f.stat().st_size for f in candidates)

    job_id = job_id or f"export-standard-{uuid.uuid4().hex[:8]}"
    state.start_job(job_id, "ssd_export_standard", total_bytes=total_bytes,
                    extra={"files_total": len(candidates), **(labels or {})})
    progress = state.JobProgress(job_id)

    bytes_done = 0
    files_written = 0
    try:
        for entry in candidates:
            if entry.suffix.lower() == ".gpx":
                # A GPS log spanning several days belongs with each of those
                # days' media: copy it into every day-folder its track
                # covers, under a gps_logs bucket.
                _first, dates = gpx_point_dates(entry)
                gpx_days = sorted(dates) or [datetime.fromtimestamp(entry.stat().st_mtime).date()]
                for day in gpx_days:
                    rel_dir = pattern.format(
                        year=f"{day.year:04d}", month=f"{day.month:02d}", day=f"{day.day:02d}",
                        camera="gps_logs", filetype="gpx", resolution="track",
                    )
                    dest_dir = Path(external_mount) / rel_dir
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    dest_file = dest_dir / entry.name
                    if not dest_file.exists() or dest_file.stat().st_size != entry.stat().st_size:
                        shutil.copy2(entry, dest_file)
                        files_written += 1
                bytes_done += entry.stat().st_size
                progress.update(bytes_done, total_bytes, files_written, len(candidates))
                continue

            meta = metadata.get(entry.name, {})
            when_str = meta.get("datetime")
            when = datetime.fromisoformat(when_str) if when_str else datetime.fromtimestamp(entry.stat().st_mtime)
            camera = (meta.get("camera") or "unknown_camera").replace(" ", "_").replace("/", "-")
            filetype = _filetype_bucket(entry.suffix)
            resolution = _resolution_bucket(entry)

            rel_dir = pattern.format(
                year=when.strftime("%Y"),
                month=when.strftime("%m"),
                day=when.strftime("%d"),
                camera=camera,
                filetype=filetype,
                resolution=resolution,
            )
            dest_dir = Path(external_mount) / rel_dir
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest_file = dest_dir / entry.name

            if not dest_file.exists() or dest_file.stat().st_size != entry.stat().st_size:
                shutil.copy2(entry, dest_file)
                files_written += 1

            bytes_done += entry.stat().st_size
            progress.update(bytes_done, total_bytes, files_written, len(candidates))

        progress.update(bytes_done, total_bytes, files_written, len(candidates), force=True)
        progress.finish("success")
    except Exception as exc:
        progress.finish("error", error=str(exc))
        raise

    logger.info("Standard export complete: %d files written under %s", files_written, external_mount)
    return {"files_written": files_written, "bytes_done": bytes_done}


# --------------------------------------------------------------------------
# Orchestration hook called by hotplug_monitor
# --------------------------------------------------------------------------

def handle_external_ssd(config: dict, mount_point: str, device=None,
                         export_overrides: Optional[dict[str, Any]] = None,
                         dest_label: Optional[str] = None) -> None:
    from src.ingest_engine import internal_storage_ready

    device_key = getattr(device, "device_node", None) or mount_point
    if not internal_storage_ready(config):
        logger.error(
            "Refusing to export to %s: internal storage %s is not a mounted drive - "
            "plug in the Main SSD first (there is nothing real to export from)",
            mount_point, config["paths"]["internal_ssd"],
        )
        state.set_system_status("error")
        state.update_device(device_key, status="blocked_no_main_ssd")
        return

    state.set_system_status("ssd_copying")
    labels = {"source_label": "Main SSD (Processed Media)", "dest_label": dest_label or "External SSD"}
    try:
        export_cfg = dict(config["export"])
        if export_overrides:
            export_cfg.update(export_overrides)
        processed_dir = config["paths"]["processed_media"]
        marker_path = Path(mount_point) / export_cfg.get("camera_emulation_marker", ".camera_emulation.yaml")

        if marker_path.is_file():
            logger.info("Camera-emulation marker found at %s; using camera-compatible export", marker_path)
            stats = export_camera_emulation(processed_dir, mount_point, marker_path, export_cfg, labels=labels)
        else:
            logger.info("No camera-emulation marker; using standard structured export")
            stats = export_standard(processed_dir, mount_point, export_cfg, labels=labels)

        state.set_system_status("idle")
        state.update_device(device_key, status="export_complete", export_stats=stats)
        logger.info("Export complete for %s", mount_point)
    except Exception:
        logger.exception("Export failed for %s", mount_point)
        state.set_system_status("error")
        raise
