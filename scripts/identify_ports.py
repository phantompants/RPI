#!/usr/bin/env python3
"""Live USB port / drive identification helper.

Run this, then plug in one drive at a time. For each partition that
appears it prints the devnode, size, model, filesystem, its udev
physical-path (ID_PATH), and - crucially - which configured storage bay
and usb_ports role that path matches in main_config.yaml. Use it to
(re)discover port_path values when a drive stops being recognised or when
you rearrange the physical layout.

Usage (on the Pi):
    sudo /opt/mediabackup/.venv/bin/python3 scripts/identify_ports.py

Ctrl+C to quit. Read-only: it never mounts or writes anything.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pyudev  # noqa: E402

from src import config as config_module  # noqa: E402
from src.hotplug_monitor import _device_id_path  # noqa: E402


def _load_bays_and_ports():
    try:
        cfg = config_module.load_config()
    except Exception as exc:  # pragma: no cover - convenience script
        print(f"(could not load config: {exc})")
        return [], []
    bays = cfg.get("web", {}).get("storage_bays", [])
    ports = (cfg.get("usb_ports") or {}).get("ports", [])
    return bays, ports


def _matches(id_path: str, bays, ports) -> str:
    if not id_path:
        return "no ID_PATH (can't match)"
    hits = []
    for bay in bays:
        m = bay.get("match")
        if m and m in id_path:
            hits.append(f"bay '{bay['name']}' (match={m})")
    for port in ports:
        p = port.get("port_path")
        if p and p in id_path:
            hits.append(f"role '{port['role']}' (port_path={p})")
    return " | ".join(hits) if hits else "NO CONFIGURED BAY/ROLE MATCHES THIS PATH"


def _describe(device: pyudev.Device, bays, ports) -> None:
    devnode = device.device_node
    parent = device.find_parent("block", device_type="disk")
    model = (parent.get("ID_MODEL") if parent else None) or device.get("ID_MODEL") or "?"
    fs = device.get("ID_FS_TYPE") or "(none)"
    try:
        sectors = device.attributes.asint("size")
        size = f"{sectors * 512 / 1e9:.1f} GB"
    except (KeyError, ValueError):
        size = "?"
    id_path = _device_id_path(device)

    print("\n" + "=" * 72)
    print(f"  devnode : {devnode}")
    print(f"  model   : {model.replace('_', ' ')}")
    print(f"  size    : {size}")
    print(f"  fs type : {fs}")
    print(f"  ID_PATH : {id_path}")
    print(f"  matches : {_matches(id_path or '', bays, ports)}")
    print("=" * 72)


def main() -> int:
    bays, ports = _load_bays_and_ports()
    context = pyudev.Context()

    print("Live port identifier. Plug in ONE drive at a time; Ctrl+C to quit.")
    print(f"Loaded {len(bays)} configured storage bays and {len(ports)} usb_ports roles.\n")
    print("Devices already connected:")
    found = False
    for device in context.list_devices(subsystem="block", DEVTYPE="partition"):
        if device.get("ID_BUS") == "usb" or (device.find_parent("block", device_type="disk") or device).get("ID_BUS") == "usb":
            _describe(device, bays, ports)
            found = True
    if not found:
        print("  (none on USB yet)")

    print("\nNow watching for insertions...")
    monitor = pyudev.Monitor.from_netlink(context)
    monitor.filter_by(subsystem="block", device_type="partition")
    for action, device in monitor:
        if action == "add":
            _describe(device, bays, ports)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nDone.")
