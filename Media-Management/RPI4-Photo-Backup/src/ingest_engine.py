"""SD card ingestion: identification, incremental mirroring, and the
EXIF/GPX-driven processed pass into the Processed_Media catalog.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import exifread
import gpxpy

from src import state

logger = logging.getLogger("mediabackup.ingest")

MANIFEST_NAME = ".processed_manifest.json"


# --------------------------------------------------------------------------
# Card identification
# --------------------------------------------------------------------------

def get_card_id(mount_path: str, device=None) -> str:
    """Return a stable identifier for an SD card.

    Prefers the filesystem UUID reported by udev (PARTUUID/ID_FS_UUID) since
    it survives reformatting the mirror destination. Falls back to a hash of
    the card's top-level file structure (names + sizes) when no UUID is
    available (e.g. some cheap card reader/controller combos hide it).
    """
    if device is not None:
        for key in ("ID_FS_UUID", "ID_PART_TABLE_UUID"):
            value = device.get(key)
            if value:
                return value.replace("-", "").lower()[:16]

    return _structural_card_id(mount_path)


def _structural_card_id(mount_path: str) -> str:
    hasher = hashlib.sha1()
    root = Path(mount_path)
    entries = []
    for entry in sorted(root.rglob("*")):
        try:
            stat = entry.stat()
        except OSError:
            continue
        entries.append(f"{entry.relative_to(root)}:{stat.st_size}")
        if len(entries) >= 500:  # cap cost on cards with huge file counts
            break
    hasher.update("\n".join(entries).encode("utf-8", errors="ignore"))
    return hasher.hexdigest()[:16]


# --------------------------------------------------------------------------
# Incremental mirror copy
# --------------------------------------------------------------------------

def _iter_source_files(source: Path):
    for dirpath, _dirnames, filenames in os.walk(source):
        for filename in filenames:
            yield Path(dirpath) / filename


def _needs_copy(src_stat: os.stat_result, dest: Path) -> bool:
    if not dest.exists():
        return True
    dest_stat = dest.stat()
    return src_stat.st_size != dest_stat.st_size or int(src_stat.st_mtime) > int(dest_stat.st_mtime)


def incremental_mirror_copy(source: str, dest: str, job_id: Optional[str] = None,
                             labels: Optional[dict[str, str]] = None) -> dict[str, int]:
    """Copy new/modified files from `source` into `dest`, preserving the
    relative directory structure, skipping files already mirrored."""
    source_path = Path(source)
    dest_path = Path(dest)
    dest_path.mkdir(parents=True, exist_ok=True)

    files = list(_iter_source_files(source_path))
    total_bytes = sum(f.stat().st_size for f in files)

    job_id = job_id or f"mirror-{uuid.uuid4().hex[:8]}"
    state.start_job(job_id, "sd_mirror", total_bytes=total_bytes,
                    extra={"files_total": len(files), **(labels or {})})
    progress = state.JobProgress(job_id)

    bytes_done = 0
    files_done = 0
    files_copied = 0
    files_skipped = 0

    try:
        for src_file in files:
            rel = src_file.relative_to(source_path)
            dest_file = dest_path / rel
            src_stat = src_file.stat()

            if _needs_copy(src_stat, dest_file):
                dest_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_file, dest_file)
                files_copied += 1
            else:
                files_skipped += 1

            bytes_done += src_stat.st_size
            files_done += 1
            progress.update(bytes_done, total_bytes, files_done, len(files))

        progress.update(bytes_done, total_bytes, files_done, len(files), force=True)
        progress.finish("success")
    except Exception as exc:
        progress.finish("error", error=str(exc))
        raise

    logger.info(
        "Mirror copy complete: %d copied, %d skipped (already mirrored), %d bytes",
        files_copied, files_skipped, bytes_done,
    )
    return {"files_copied": files_copied, "files_skipped": files_skipped, "bytes_done": bytes_done}


# --------------------------------------------------------------------------
# EXIF / GPX metadata extraction
# --------------------------------------------------------------------------

def _dms_to_decimal(dms, ref: str) -> float:
    degrees = float(dms.values[0].num) / float(dms.values[0].den)
    minutes = float(dms.values[1].num) / float(dms.values[1].den)
    seconds = float(dms.values[2].num) / float(dms.values[2].den)
    decimal = degrees + minutes / 60.0 + seconds / 3600.0
    if ref in ("S", "W"):
        decimal = -decimal
    return decimal


def extract_exif(file_path: Path) -> dict[str, Any]:
    """Return {'datetime': datetime|None, 'gps': (lat, lon)|None, 'camera': str|None}."""
    result: dict[str, Any] = {"datetime": None, "gps": None, "camera": None}
    try:
        with open(file_path, "rb") as fh:
            tags = exifread.process_file(fh, details=False)
    except Exception:
        return result

    dt_tag = tags.get("EXIF DateTimeOriginal") or tags.get("Image DateTime")
    if dt_tag:
        try:
            result["datetime"] = datetime.strptime(str(dt_tag), "%Y:%m:%d %H:%M:%S")
        except ValueError:
            pass

    lat = tags.get("GPS GPSLatitude")
    lat_ref = tags.get("GPS GPSLatitudeRef")
    lon = tags.get("GPS GPSLongitude")
    lon_ref = tags.get("GPS GPSLongitudeRef")
    if lat and lon and lat_ref and lon_ref:
        try:
            result["gps"] = (
                _dms_to_decimal(lat, str(lat_ref)),
                _dms_to_decimal(lon, str(lon_ref)),
            )
        except (AttributeError, ZeroDivisionError, IndexError):
            pass

    make = tags.get("Image Make")
    model = tags.get("Image Model")
    if make or model:
        result["camera"] = " ".join(str(t) for t in (make, model) if t).strip()

    return result


def gpx_time_to_local_naive(when: datetime) -> datetime:
    """Convert a GPX point timestamp to naive local time.

    GPX files record timestamps in UTC (timezone-aware), but camera EXIF
    times are naive local time (whatever the camera clock shows). Comparing
    them raw would be off by the UTC offset - e.g. 10-11 hours in Sydney -
    making every match miss the tolerance window. Convert aware timestamps
    to the system's local timezone first; naive ones (rare, nonstandard GPX)
    are assumed to already be local.
    """
    if when.tzinfo is not None:
        return when.astimezone().replace(tzinfo=None)
    return when


def gpx_point_dates(gpx_path: Path) -> tuple[Optional[datetime], set]:
    """Return (first point's local time, set of local dates covered) for a
    GPX file, or (None, empty set) if it can't be parsed or has no
    timestamped points. Used to name GPX files by recording start and to
    copy multi-day logs into every export day-folder they span."""
    try:
        with open(gpx_path, "r", encoding="utf-8") as fh:
            gpx = gpxpy.parse(fh)
    except Exception:
        return None, set()
    times = [
        gpx_time_to_local_naive(point.time)
        for track in gpx.tracks
        for segment in track.segments
        for point in segment.points
        if point.time
    ]
    if not times:
        return None, set()
    return min(times), {t.date() for t in times}


class GpxTrack:
    """Sorted (timestamp, lat, lon) points loaded from every .gpx file found
    under a directory, used to fill in GPS coordinates for media files whose
    EXIF has none (e.g. action cameras logging position separately)."""

    def __init__(self, points: list[tuple[datetime, float, float]]):
        self._points = sorted(points, key=lambda p: p[0])

    @classmethod
    def load_from_dir(cls, directory: Path) -> "GpxTrack":
        points: list[tuple[datetime, float, float]] = []
        for gpx_file in directory.rglob("*.gpx"):
            try:
                with open(gpx_file, "r", encoding="utf-8") as fh:
                    gpx = gpxpy.parse(fh)
            except Exception:
                logger.warning("Failed to parse GPX file %s", gpx_file)
                continue
            for track in gpx.tracks:
                for segment in track.segments:
                    for point in segment.points:
                        if point.time:
                            points.append((gpx_time_to_local_naive(point.time), point.latitude, point.longitude))
        return cls(points)

    def match(self, when: datetime, tolerance_seconds: int) -> Optional[tuple[float, float]]:
        if not self._points:
            return None
        best = min(self._points, key=lambda p: abs((p[0] - when).total_seconds()))
        if abs((best[0] - when).total_seconds()) <= tolerance_seconds:
            return (best[1], best[2])
        return None

    def __bool__(self) -> bool:
        return bool(self._points)


# --------------------------------------------------------------------------
# Processed pass: rename + catalog into Processed_Media
# --------------------------------------------------------------------------

def _load_manifest(processed_dir: Path) -> dict[str, Any]:
    manifest_path = processed_dir / MANIFEST_NAME
    if manifest_path.is_file():
        try:
            with open(manifest_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_manifest(processed_dir: Path, manifest: dict[str, Any]) -> None:
    manifest_path = processed_dir / MANIFEST_NAME
    tmp = manifest_path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
    tmp.replace(manifest_path)


def _build_filename(pattern: str, when: Optional[datetime], original_name: str) -> str:
    if when is None:
        when = datetime.fromtimestamp(0)
    return pattern.format(
        date=when.strftime("%Y%m%d"),
        time=when.strftime("%H%M%S"),
        original_name=original_name,
    )


def process_media(mirror_dir: str, processed_dir: str, config: dict, card_id: str,
                   job_id: Optional[str] = None, labels: Optional[dict[str, str]] = None) -> dict[str, int]:
    """Read the mirrored raw copy, extract EXIF/GPX metadata, rename files to
    `YYYYMMDD-HHMMSS-originalfilename.ext`, and catalog them (flat) into
    Processed_Media, skipping files already processed for this card."""
    mirror_path = Path(mirror_dir)
    processed_path = Path(processed_dir)
    processed_path.mkdir(parents=True, exist_ok=True)

    ingest_cfg = config["ingest"]
    extensions = {ext.lower() for ext in ingest_cfg.get("media_extensions", [])}
    pattern = ingest_cfg.get("rename_pattern", "{date}-{time}-{original_name}")
    tolerance = ingest_cfg.get("gpx_match_tolerance_seconds", 120)

    manifest = _load_manifest(processed_path)
    manifest.setdefault(card_id, {})
    card_manifest = manifest[card_id]

    gpx_track = GpxTrack.load_from_dir(mirror_path)

    candidates = [f for f in _iter_source_files(mirror_path) if f.suffix.lower() in extensions]
    total_bytes = sum(f.stat().st_size for f in candidates)

    job_id = job_id or f"process-{uuid.uuid4().hex[:8]}"
    state.start_job(job_id, "sd_process", total_bytes=total_bytes,
                    extra={"files_total": len(candidates), **(labels or {})})
    progress = state.JobProgress(job_id)

    bytes_done = 0
    files_done = 0
    files_written = 0
    files_skipped = 0

    try:
        for src_file in candidates:
            rel = str(src_file.relative_to(mirror_path))
            src_stat = src_file.stat()

            if rel in card_manifest:
                bytes_done += src_stat.st_size
                files_done += 1
                files_skipped += 1
                progress.update(bytes_done, total_bytes, files_done, len(candidates))
                continue

            exif = extract_exif(src_file)
            when = exif["datetime"]
            gps = exif["gps"]
            if gps is None and gpx_track and when is not None:
                gps = gpx_track.match(when, tolerance)
            if when is None and src_file.suffix.lower() == ".gpx":
                # GPX logs have no EXIF; name them by the recording's start.
                when, _dates = gpx_point_dates(src_file)
            if when is None:
                # No EXIF capture time (screenshots, sidecar files, some
                # video containers) - fall back to the file's modification
                # time rather than the epoch, so names still sort sensibly.
                when = datetime.fromtimestamp(src_stat.st_mtime)

            dest_name = _build_filename(pattern, when, src_file.name)
            dest_file = processed_path / dest_name
            counter = 1
            while dest_file.exists():
                dest_file = processed_path / f"{Path(dest_name).stem}_{counter}{Path(dest_name).suffix}"
                counter += 1

            shutil.copy2(src_file, dest_file)
            card_manifest[rel] = dest_file.name
            manifest.setdefault("_metadata", {})[dest_file.name] = {
                "datetime": when.isoformat() if when else None,
                "gps": {"lat": gps[0], "lon": gps[1]} if gps else None,
                "camera": exif["camera"],
                "card_id": card_id,
                "source_relpath": rel,
                "size": src_stat.st_size,
            }
            files_written += 1

            bytes_done += src_stat.st_size
            files_done += 1
            progress.update(bytes_done, total_bytes, files_done, len(candidates))

        progress.update(bytes_done, total_bytes, files_done, len(candidates), force=True)
        progress.finish("success")
    except Exception as exc:
        progress.finish("error", error=str(exc))
        raise
    finally:
        _save_manifest(processed_path, manifest)

    logger.info("Processed pass complete for card %s: %d written, %d already processed",
                card_id, files_written, files_skipped)
    return {"files_written": files_written, "files_skipped": files_skipped, "bytes_done": bytes_done}


# --------------------------------------------------------------------------
# Orchestration hook called by hotplug_monitor
# --------------------------------------------------------------------------

def internal_storage_ready(config: dict) -> bool:
    """True when the internal working storage is a real mounted drive.

    paths.internal_ssd points at a mount point (e.g. /mnt/port_primary_ssd).
    If the drive isn't mounted, that path is just an empty directory on the
    boot card's root filesystem - writing gigabytes of media there fills the
    OS partition, so every write job must check this first. Set
    paths.require_internal_mount to false only for tests/dev machines where
    the path is a plain directory on purpose.
    """
    if not config["paths"].get("require_internal_mount", True):
        return True
    return Path(config["paths"]["internal_ssd"]).is_mount()


def handle_sd_card(config: dict, mount_point: str, device=None) -> None:
    # Keep device-state updates keyed by devnode when we have one, matching
    # the entries hotplug_monitor creates, so the dashboard shows one row
    # per physical device rather than a second mount-point-keyed duplicate.
    device_key = getattr(device, "device_node", None) or mount_point

    if not internal_storage_ready(config):
        logger.error(
            "Refusing to ingest %s: internal storage %s is not a mounted drive - "
            "plug in the Main SSD first (writing now would fill the OS boot card)",
            mount_point, config["paths"]["internal_ssd"],
        )
        state.set_system_status("error")
        state.update_device(device_key, status="blocked_no_main_ssd")
        return

    state.set_system_status("ingesting")
    try:
        card_id = get_card_id(mount_point, device)
        mirror_dir = str(Path(config["paths"]["sd_mirror_root"]) / f"SD_Mirror_{card_id}")
        processed_dir = config["paths"]["processed_media"]

        logger.info("Starting ingest for card %s (mount=%s)", card_id, mount_point)
        mirror_stats = incremental_mirror_copy(
            mount_point, mirror_dir,
            labels={"source_label": "Source SD/MicroSD", "dest_label": "Main SSD (raw mirror)"},
        )
        process_stats = process_media(
            mirror_dir, processed_dir, config, card_id,
            labels={"source_label": "Main SSD (raw mirror)", "dest_label": "Main SSD (Processed Media)"},
        )

        if process_stats.get("files_written", 0) > 0:
            # New media in Processed_Media - flag it so tonight's cloud sync
            # actually runs (it only fires on days with changes).
            state.mark_change()

        state.set_system_status("idle")
        state.update_device(device_key, status="safe_to_eject", card_id=card_id,
                             mirror_stats=mirror_stats, process_stats=process_stats)
        logger.info("Ingest complete for card %s, safe to eject", card_id)
    except Exception:
        logger.exception("Ingest failed for %s", mount_point)
        state.set_system_status("error")
        raise
