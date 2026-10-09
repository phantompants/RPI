"""Wi-Fi scanning, connecting, and saved-network management via NetworkManager.

Shells out to `nmcli` rather than talking to NetworkManager's D-Bus API
directly, since it's already present by default on Raspberry Pi OS and
gives a stable, scriptable interface. Modifying connections requires root,
which the daemon already runs as (see systemd/mediabackup.service).
"""
from __future__ import annotations

import logging
import re
import subprocess
from typing import Any, Optional

logger = logging.getLogger("mediabackup.wifi")

NMCLI = "nmcli"
_UNESCAPED_COLON_RE = re.compile(r"(?<!\\):")


class WifiError(RuntimeError):
    pass


def _run(args: list[str], timeout: int = 15) -> str:
    try:
        result = subprocess.run([NMCLI, *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise WifiError("nmcli is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise WifiError(f"nmcli timed out running: {' '.join(args)}") from exc
    if result.returncode != 0:
        raise WifiError(result.stderr.strip() or f"nmcli failed running: {' '.join(args)}")
    return result.stdout


def _unescape(value: str) -> str:
    return value.replace("\\:", ":").replace("\\\\", "\\")


def parse_terse(output: str, field_count: int) -> list[list[str]]:
    """Parse `nmcli -t` colon-separated output, honouring nmcli's escaping
    of literal colons/backslashes within field values."""
    rows = []
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = [_unescape(p) for p in _UNESCAPED_COLON_RE.split(line)]
        if len(parts) >= field_count:
            rows.append(parts[:field_count])
    return rows


def scan_networks() -> list[dict[str, Any]]:
    output = _run(["-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list", "--rescan", "yes"],
                  timeout=20)
    networks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for in_use, ssid, signal, security in parse_terse(output, 4):
        if not ssid or ssid in seen:
            continue
        seen.add(ssid)
        networks.append({
            "ssid": ssid,
            "signal": int(signal) if signal.isdigit() else None,
            "security": security or "Open",
            "in_use": in_use.strip() == "*",
        })
    return sorted(networks, key=lambda n: n["signal"] or 0, reverse=True)


def list_saved_connections() -> list[dict[str, Any]]:
    output = _run(["-t", "-f", "NAME,TYPE", "connection", "show"])
    return [
        {"name": name}
        for name, conn_type in parse_terse(output, 2)
        if "wireless" in conn_type.lower() or conn_type.lower() == "wifi"
    ]


def current_status() -> dict[str, Any]:
    output = _run(["-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"])
    for device, dev_type, dev_state, connection in parse_terse(output, 4):
        if dev_type == "wifi" and dev_state.startswith("connected"):
            ip_address = None
            try:
                ip_output = _run(["-t", "-f", "IP4.ADDRESS", "device", "show", device])
                first_line = ip_output.strip().splitlines()[0] if ip_output.strip() else ""
                if first_line:
                    ip_address = first_line.split(":", 1)[-1].split("/")[0]
            except WifiError:
                pass
            return {"connected": True, "ssid": connection, "device": device, "ip_address": ip_address}
    return {"connected": False, "ssid": None, "device": None, "ip_address": None}


def connect(ssid: str, password: Optional[str] = None) -> dict[str, Any]:
    args = ["device", "wifi", "connect", ssid]
    if password:
        args += ["password", password]
    try:
        _run(args, timeout=30)
        logger.info("Connected to Wi-Fi network %s", ssid)
        return {"ok": True}
    except WifiError as exc:
        logger.warning("Failed to connect to %s: %s", ssid, exc)
        return {"ok": False, "error": str(exc)}


def forget(name: str) -> dict[str, Any]:
    try:
        _run(["connection", "delete", name])
        logger.info("Removed saved Wi-Fi connection %s", name)
        return {"ok": True}
    except WifiError as exc:
        return {"ok": False, "error": str(exc)}
