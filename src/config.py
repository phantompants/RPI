"""Shared YAML configuration loader for the media backup daemon."""
from __future__ import annotations

import datetime
import os
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = Path(
    os.environ.get("MEDIABACKUP_CONFIG", PROJECT_ROOT / "config" / "main_config.yaml")
)

REQUIRED_SECTIONS = ("paths", "ingest", "export", "cloud_sync", "nas_sync", "alerts", "web")


class ConfigError(Exception):
    pass


def _validate_hh_mm(value: Any, field_name: str) -> None:
    try:
        datetime.datetime.strptime(value, "%H:%M")
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{field_name} must be in HH:MM 24-hour format, got {value!r}") from exc


def load_config(path: Path | str | None = None) -> dict[str, Any]:
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not config_path.is_file():
        raise ConfigError(f"Config file not found: {config_path}")
    with open(config_path, "r", encoding="utf-8") as fh:
        try:
            data = yaml.safe_load(fh) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"Failed to parse YAML config {config_path}: {exc}") from exc
    validate_config(data)
    return data


def validate_config(data: dict[str, Any]) -> None:
    missing = [section for section in REQUIRED_SECTIONS if section not in data]
    if missing:
        raise ConfigError(f"Config is missing required section(s): {', '.join(missing)}")

    paths = data["paths"]
    for key in ("internal_ssd", "processed_media", "source_sd_mount", "external_ssd_mount"):
        if key not in paths:
            raise ConfigError(f"Config paths section is missing '{key}'")

    media_extensions = data["ingest"].get("media_extensions")
    if not isinstance(media_extensions, list):
        raise ConfigError("ingest.media_extensions must be a list")
    for ext in media_extensions:
        if not isinstance(ext, str) or not ext.startswith("."):
            raise ConfigError(
                f"ingest.media_extensions entries must be strings starting with '.', got {ext!r} "
                "(hint: numeric-looking extensions like .360 must be quoted in YAML)"
            )

    port = data.get("web", {}).get("port")
    if not isinstance(port, int) or not (0 < port < 65536):
        raise ConfigError(f"web.port must be a valid TCP port, got {port!r}")

    daily_start_time = data.get("cloud_sync", {}).get("daily_start_time")
    if daily_start_time:
        _validate_hh_mm(daily_start_time, "cloud_sync.daily_start_time")

    usb_ports = data.get("usb_ports")
    if usb_ports and usb_ports.get("enabled"):
        for entry in usb_ports.get("ports", []):
            if not entry.get("port_path"):
                raise ConfigError(
                    f"usb_ports.enabled is true but port '{entry.get('role')}' has no port_path set"
                )
            if entry.get("earliest_run_time"):
                _validate_hh_mm(entry["earliest_run_time"], f"usb_ports port '{entry.get('role')}'.earliest_run_time")


def ensure_directories(data: dict[str, Any]) -> None:
    """Create all directories referenced by the config that don't exist yet."""
    paths = data["paths"]
    for key in ("processed_media", "state_dir", "log_dir"):
        value = paths.get(key)
        if value:
            Path(value).mkdir(parents=True, exist_ok=True)
