"""USB/SD hotplug detection using pyudev.

Classifies inserted block devices as either an SD card (usually behind a USB
card reader) or an external SSD, mounts the relevant partition, and hands off
to `ingest_engine` / `export_engine`. Runs as a background thread owned by
`main.py`.

Physical USB ports can optionally be pinned to fixed roles (see the
`usb_ports` config section) so behaviour depends on which port a device is
plugged into rather than guessed from capacity - devices on unconfigured
ports still fall back to the capacity/model heuristic below.
"""
from __future__ import annotations

import datetime
import logging
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Optional

import pyudev

from src import state

logger = logging.getLogger("mediabackup.hotplug")

SD_MODEL_HINTS = ("card", "sd", "mmc", "reader", "sdhc", "sdxc")
DEFAULT_SD_SIZE_THRESHOLD_BYTES = 256 * 1024 ** 3  # 256 GB

# Short display names used in the dashboard's job cards and device list.
ROLE_DISPLAY_LABELS = {
    "sd_source": "Source SD/MicroSD",
    "primary_ssd": "Main SSD",
    "secondary_ssd_mirror": "Mirror SSD",
    "thumb_drive": "ThumbDrive Last 3 Days",
}

DeviceHook = Callable[..., None]


class DeviceClassifier:
    """Decides whether a mounted partition belongs to an SD card or an
    external SSD using udev metadata and, as a last resort, capacity."""

    def __init__(self, sd_size_threshold_bytes: int = DEFAULT_SD_SIZE_THRESHOLD_BYTES):
        self.sd_size_threshold_bytes = sd_size_threshold_bytes

    def classify(self, device: pyudev.Device) -> str:
        parent = device.find_parent("block", device_type="disk") or device

        if parent.get("ID_DRIVE_FLASH_SD") == "1":
            return "sd"

        text_fields = " ".join(
            (parent.get(key) or "") for key in ("ID_MODEL", "ID_VENDOR", "ID_FS_LABEL", "ID_MODEL_ID")
        ).lower()
        if any(hint in text_fields for hint in SD_MODEL_HINTS):
            return "sd"

        size_bytes = self._device_size_bytes(parent)
        if size_bytes is not None and size_bytes <= self.sd_size_threshold_bytes:
            return "sd"

        return "ssd"

    @staticmethod
    def _device_size_bytes(device: pyudev.Device) -> Optional[int]:
        try:
            sectors = device.attributes.asint("size")
            return sectors * 512
        except (KeyError, ValueError):
            return None


class MountError(RuntimeError):
    pass


def _already_mounted_at(devnode: str) -> Optional[str]:
    """Return the mount point `devnode` is already mounted at, if any.

    Used to skip devices the daemon shouldn't touch at all - most
    importantly the Pi's own boot/root partitions, which are mounted before
    the daemon starts and can otherwise be misclassified as an inserted SD
    card during the initial device scan.
    """
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == devnode:
                    return parts[1]
    except OSError:
        pass
    return None


def _device_id_path(device: pyudev.Device) -> Optional[str]:
    """Return the udev physical-path identifier for a device's disk, which
    is stable across reboots/reinsertions for a given physical USB port
    (unlike /dev/sdX letters, which depend on insertion order)."""
    id_path = device.get("ID_PATH")
    if id_path:
        return id_path
    parent = device.find_parent("block", device_type="disk")
    return parent.get("ID_PATH") if parent is not None else None


def match_usb_port(config: dict[str, Any], device: pyudev.Device) -> Optional[dict[str, Any]]:
    """Return the configured `usb_ports.ports` entry whose `port_path`
    matches this device's physical port, or None if port pinning is
    disabled or the device is on an unconfigured port.

    A device plugged into a hub's downstream port contains its upstream
    port's path as a prefix (e.g. a direct port "0:1.1.0" vs. a hub's
    downstream port "0:1.1.1:1.0"), so a config with both configured could
    have more than one substring match. Picking the longest match resolves
    that in favour of the more specific path regardless of list order.
    """
    usb_ports_cfg = config.get("usb_ports") or {}
    if not usb_ports_cfg.get("enabled"):
        return None
    id_path = _device_id_path(device)
    if not id_path:
        return None
    matches = [
        port for port in usb_ports_cfg.get("ports", [])
        if port.get("port_path") and port["port_path"] in id_path
    ]
    if not matches:
        return None
    return max(matches, key=lambda port: len(port["port_path"]))


def mount_partition(devnode: str, mount_point: str, read_only: bool = False) -> None:
    Path(mount_point).mkdir(parents=True, exist_ok=True)
    options = "ro" if read_only else "rw"
    cmd = ["mount", "-o", options, devnode, mount_point]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise MountError(f"mount {devnode} -> {mount_point} failed: {result.stderr.strip()}")
    logger.info("Mounted %s at %s (%s)", devnode, mount_point, options)


def unmount(mount_point: str) -> None:
    result = subprocess.run(["umount", mount_point], capture_output=True, text=True)
    if result.returncode != 0:
        logger.warning("Failed to unmount %s: %s", mount_point, result.stderr.strip())
    else:
        logger.info("Unmounted %s", mount_point)


class HotplugMonitor:
    def __init__(
        self,
        config: dict,
        on_sd_card: DeviceHook,
        on_external_ssd: DeviceHook,
        on_device_removed: Optional[Callable[[str], None]] = None,
    ):
        self.config = config
        self.on_sd_card = on_sd_card
        self.on_external_ssd = on_external_ssd
        self.on_device_removed = on_device_removed
        self.classifier = DeviceClassifier()
        self._context = pyudev.Context()
        self._observer: Optional[pyudev.MonitorObserver] = None
        self._mounted: dict[str, str] = {}  # devnode -> mount_point
        self._mounted_by_role: dict[str, tuple[str, str, pyudev.Device]] = {}  # role -> (devnode, mount_point, device)
        self._last_role_export_date: dict[str, datetime.date] = {}

    def start(self) -> None:
        monitor = pyudev.Monitor.from_netlink(self._context)
        monitor.filter_by(subsystem="block", device_type="partition")
        self._observer = pyudev.MonitorObserver(monitor, callback=self._handle_event, name="hotplug-observer")
        self._observer.start()
        logger.info("Hotplug monitor started, watching for block device partitions")
        # Run in the background: a device already plugged in at startup can
        # trigger a long-running ingest/export, and _scan_existing() must
        # not block the caller (main.py still has to start the web server).
        threading.Thread(target=self._scan_existing, name="hotplug-initial-scan", daemon=True).start()

    def stop(self) -> None:
        if self._observer:
            self._observer.stop()
            logger.info("Hotplug monitor stopped")

    def _scan_existing(self) -> None:
        # Devices returned by list_devices() are static enumeration results,
        # not live monitor events, so they have no meaningful .action -
        # dispatch straight to _handle_add instead of going through
        # _handle_event.
        for device in self._context.list_devices(subsystem="block", DEVTYPE="partition"):
            devnode = device.device_node
            if not devnode:
                continue
            try:
                self._handle_add(devnode, device)
            except Exception:
                logger.exception("Error handling initial device scan for %s", devnode)
                state.set_system_status("error")

    def _handle_event(self, device: pyudev.Device) -> None:
        # pyudev.MonitorObserver invokes its callback with just the device;
        # the action ("add"/"remove"/...) is read from device.action.
        devnode = device.device_node
        if not devnode:
            return
        action = device.action
        try:
            if action == "add":
                self._handle_add(devnode, device)
            elif action == "remove":
                self._handle_remove(devnode)
        except Exception:
            logger.exception("Error handling udev event %s for %s", action, devnode)
            state.set_system_status("error")

    def _handle_add(self, devnode: str, device: pyudev.Device) -> None:
        parent = device.find_parent("block", device_type="disk")
        bus = parent.get("ID_BUS") if parent is not None else None
        if bus is not None and bus != "usb":
            logger.debug("Skipping %s: not a USB-attached device (bus=%s)", devnode, bus)
            return

        fs_type = device.get("ID_FS_TYPE")
        if not fs_type:
            logger.debug("Skipping %s: no filesystem detected yet", devnode)
            return

        existing_mount = _already_mounted_at(devnode)
        if existing_mount and existing_mount not in self._configured_mount_points():
            # Genuinely foreign mounts (the Pi's own boot/root partitions,
            # anything hand-mounted elsewhere) are not ours to manage.
            logger.info("Skipping %s: already mounted at %s (likely internal system storage)",
                        devnode, existing_mount)
            return
        if existing_mount:
            # Mounted at one of our own mount points - a previous daemon run
            # mounted it and we restarted. Adopt it (track it, show it in the
            # dashboard, run the usual hooks - they're incremental) instead
            # of ignoring it until the user unplugs/replugs.
            logger.info("Adopting %s: already mounted at our mount point %s from a previous run",
                        devnode, existing_mount)

        port = match_usb_port(self.config, device)
        if port:
            self._handle_port_role(devnode, device, port, existing_mount)
            return

        device_class = self.classifier.classify(device)
        logger.info("Detected new partition %s classified as %s (fs=%s)", devnode, device_class, fs_type)

        paths = self.config["paths"]
        id_path = _device_id_path(device)
        if device_class == "sd":
            mount_point = existing_mount or paths["source_sd_mount"]
            if not existing_mount:
                read_only = self.config["ingest"].get("read_only_mount", True)
                mount_partition(devnode, mount_point, read_only=read_only)
            self._mounted[devnode] = mount_point
            state.update_device(devnode, kind="sd", mount_point=mount_point, status="mounted",
                                 label=ROLE_DISPLAY_LABELS["sd_source"], id_path=id_path)
            self.on_sd_card(mount_point, device)
        else:
            mount_point = existing_mount or paths["external_ssd_mount"]
            if not existing_mount:
                mount_partition(devnode, mount_point, read_only=False)
            self._mounted[devnode] = mount_point
            state.update_device(devnode, kind="ssd", mount_point=mount_point, status="mounted",
                                 label="External SSD", id_path=id_path)
            self.on_external_ssd(mount_point, device)

    def _configured_mount_points(self) -> set:
        """Every mount point this daemon itself manages - used to tell 'we
        mounted this before a restart' apart from foreign/system mounts."""
        points = {
            self.config["paths"].get("source_sd_mount"),
            self.config["paths"].get("external_ssd_mount"),
        }
        for port in (self.config.get("usb_ports") or {}).get("ports", []):
            points.add(port.get("mount_point"))
        points.discard(None)
        return points

    def _handle_port_role(self, devnode: str, device: pyudev.Device, port: dict[str, Any],
                           existing_mount: Optional[str] = None) -> None:
        role = port["role"]
        mount_point = existing_mount or port.get("mount_point") or self.config["paths"]["external_ssd_mount"]
        logger.info("Detected new partition %s matched USB port role '%s' (%s)",
                    devnode, role, port.get("label", ""))

        display_label = ROLE_DISPLAY_LABELS.get(role, role)
        id_path = _device_id_path(device)
        if role == "sd_source":
            if not existing_mount:
                read_only = self.config["ingest"].get("read_only_mount", True)
                mount_partition(devnode, mount_point, read_only=read_only)
            self._mounted[devnode] = mount_point
            state.update_device(devnode, kind="sd", role=role, mount_point=mount_point, status="mounted",
                                 label=display_label, id_path=id_path)
            self.on_sd_card(mount_point, device)
            return

        if not existing_mount:
            mount_partition(devnode, mount_point, read_only=False)
        self._mounted[devnode] = mount_point
        self._mounted_by_role[role] = (devnode, mount_point, device)
        state.update_device(devnode, kind="ssd", role=role, mount_point=mount_point, status="mounted",
                             label=display_label, id_path=id_path)

        earliest_run_time = port.get("earliest_run_time")
        if earliest_run_time:
            # Gated roles (e.g. the secondary mirror) don't export on
            # insert - a DailyTimeTrigger in main.py polls
            # is_role_mounted()/trigger_role_export() and runs it once the
            # configured time-of-day threshold passes, whether that means
            # this drive was already plugged in or is inserted later.
            logger.info("Role '%s' is gated until %s; will export automatically once that time passes",
                        role, earliest_run_time)
            return

        export_overrides = {"filter_by_age": True, "max_age_days": port["max_age_days"]} if "max_age_days" in port else None
        self._run_role_export(role, mount_point, device, export_overrides)

    def _run_role_export(self, role: str, mount_point: str, device: pyudev.Device,
                          export_overrides: Optional[dict[str, Any]] = None) -> None:
        self._last_role_export_date[role] = datetime.date.today()
        self.on_external_ssd(mount_point, device, export_overrides, ROLE_DISPLAY_LABELS.get(role, role))

    def is_role_mounted(self, role: str) -> bool:
        return role in self._mounted_by_role

    def eject(self, devnode: str) -> tuple[bool, str]:
        """Unmount a device previously mounted by this monitor so the user
        can safely unplug it. Accepts either the devnode key or, as a
        convenience, a mount point value. Fails cleanly if the device is
        busy (e.g. an ingest/export is still writing to it)."""
        mount_point = self._mounted.get(devnode)
        if mount_point is None and devnode in self._mounted.values():
            mount_point = devnode
            devnode = next(k for k, v in self._mounted.items() if v == mount_point)
        if mount_point is None:
            return False, f"{devnode} is not mounted by this daemon"

        result = subprocess.run(["umount", mount_point], capture_output=True, text=True)
        if result.returncode != 0:
            message = result.stderr.strip() or "umount failed"
            logger.warning("Eject of %s (%s) failed: %s", devnode, mount_point, message)
            return False, message

        self._mounted.pop(devnode, None)
        for role, (role_devnode, _mp, _dev) in list(self._mounted_by_role.items()):
            if role_devnode == devnode:
                del self._mounted_by_role[role]
        state.update_device(devnode, status="ejected")
        logger.info("Ejected %s (%s) - safe to unplug", devnode, mount_point)
        return True, "ejected"

    def trigger_role_export(self, role: str, force: bool = False) -> bool:
        """Run the export for `role` if its drive is currently mounted and
        it hasn't already run today. Returns True if an export was started.

        `force=True` (manual dashboard trigger) bypasses the once-per-day
        guard, which only exists so the scheduled time-gate doesn't re-run
        an export the user already kicked off by hand."""
        entry = self._mounted_by_role.get(role)
        if entry is None:
            return False
        if not force and self._last_role_export_date.get(role) == datetime.date.today():
            return False
        devnode, mount_point, device = entry
        port = next((p for p in (self.config.get("usb_ports") or {}).get("ports", []) if p.get("role") == role), {})
        export_overrides = {"filter_by_age": True, "max_age_days": port["max_age_days"]} if "max_age_days" in port else None
        self._run_role_export(role, mount_point, device, export_overrides)
        return True

    def _handle_remove(self, devnode: str) -> None:
        mount_point = self._mounted.pop(devnode, None)
        if mount_point:
            unmount(mount_point)
            state.update_device(devnode, status="removed")
        for role, (role_devnode, _mount_point, _device) in list(self._mounted_by_role.items()):
            if role_devnode == devnode:
                del self._mounted_by_role[role]
        if self.on_device_removed:
            self.on_device_removed(devnode)


def run_standalone() -> None:
    """Convenience entry point for running just the hotplug monitor for
    manual testing: `python -m src.hotplug_monitor`."""
    from src import config as config_module
    from src import export_engine, ingest_engine

    logging.basicConfig(level=logging.INFO)
    cfg = config_module.load_config()
    state.configure(cfg["paths"]["state_dir"])

    monitor = HotplugMonitor(
        cfg,
        on_sd_card=lambda mount_point, device: ingest_engine.handle_sd_card(cfg, mount_point),
        on_external_ssd=lambda mount_point, device, export_overrides=None, dest_label=None:
            export_engine.handle_external_ssd(cfg, mount_point, device, export_overrides, dest_label),
    )
    monitor.start()
    stop_event = threading.Event()
    try:
        stop_event.wait()
    except KeyboardInterrupt:
        monitor.stop()


if __name__ == "__main__":
    run_standalone()
