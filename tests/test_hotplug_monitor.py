from src.hotplug_monitor import DeviceClassifier, HotplugMonitor, _already_mounted_at, match_usb_port


class FakeAttributes:
    def __init__(self, size_sectors=None):
        self._size_sectors = size_sectors

    def asint(self, key):
        if key == "size" and self._size_sectors is not None:
            return self._size_sectors
        raise KeyError(key)


class FakeDevice:
    def __init__(self, properties=None, size_sectors=None, parent=None, device_node=None, action=None):
        self._properties = properties or {}
        self.attributes = FakeAttributes(size_sectors)
        self._parent = parent
        self.device_node = device_node
        self.action = action

    def get(self, key):
        return self._properties.get(key)

    def find_parent(self, subsystem, device_type=None):
        return self._parent


def test_classifies_sd_via_udev_flash_property():
    parent = FakeDevice(properties={"ID_DRIVE_FLASH_SD": "1"})
    device = FakeDevice(parent=parent)
    assert DeviceClassifier().classify(device) == "sd"


def test_classifies_sd_via_model_name_hint():
    parent = FakeDevice(properties={"ID_MODEL": "USB_SD_Card_Reader"})
    device = FakeDevice(parent=parent)
    assert DeviceClassifier().classify(device) == "sd"


def test_classifies_sd_via_small_capacity():
    # 32GB in 512-byte sectors, well under the default 256GB threshold.
    parent = FakeDevice(properties={}, size_sectors=32 * 1024 ** 3 // 512)
    device = FakeDevice(parent=parent)
    assert DeviceClassifier().classify(device) == "sd"


def test_classifies_ssd_via_large_capacity():
    # 1TB in 512-byte sectors, above the default 256GB threshold.
    parent = FakeDevice(properties={}, size_sectors=1024 ** 4 // 512)
    device = FakeDevice(parent=parent)
    assert DeviceClassifier().classify(device) == "ssd"


def test_classifies_ssd_when_no_hints_available():
    parent = FakeDevice(properties={})
    device = FakeDevice(parent=parent)
    assert DeviceClassifier().classify(device) == "ssd"


def test_custom_threshold_is_respected():
    parent = FakeDevice(properties={}, size_sectors=100 * 1024 ** 3 // 512)  # 100GB
    device = FakeDevice(parent=parent)
    classifier = DeviceClassifier(sd_size_threshold_bytes=50 * 1024 ** 3)
    assert classifier.classify(device) == "ssd"


def test_already_mounted_at_finds_existing_mount():
    # "proc" is always mounted at /proc on any running Linux system.
    assert _already_mounted_at("proc") == "/proc"


def test_already_mounted_at_returns_none_for_unmounted_device():
    assert _already_mounted_at("/dev/definitely-not-a-real-device") is None


USB_PORTS_CONFIG = {
    "usb_ports": {
        "enabled": True,
        "ports": [
            {"role": "sd_source", "port_path": "usb-0:1.1", "mount_point": "/mnt/port_sd"},
            {"role": "primary_ssd", "port_path": "usb-0:1.2", "mount_point": "/mnt/port_primary"},
        ],
    }
}


def test_match_usb_port_finds_configured_port():
    device = FakeDevice(properties={"ID_PATH": "platform-xhci-hcd.0-usb-0:1.2:1.0-scsi-0:0:0:0"})
    port = match_usb_port(USB_PORTS_CONFIG, device)
    assert port is not None
    assert port["role"] == "primary_ssd"


def test_match_usb_port_falls_back_to_parent_id_path():
    parent = FakeDevice(properties={"ID_PATH": "platform-xhci-hcd.0-usb-0:1.1:1.0"})
    device = FakeDevice(properties={}, parent=parent)
    port = match_usb_port(USB_PORTS_CONFIG, device)
    assert port is not None
    assert port["role"] == "sd_source"


def test_match_usb_port_returns_none_when_disabled():
    device = FakeDevice(properties={"ID_PATH": "platform-xhci-hcd.0-usb-0:1.1:1.0"})
    disabled_config = {"usb_ports": {"enabled": False, "ports": USB_PORTS_CONFIG["usb_ports"]["ports"]}}
    assert match_usb_port(disabled_config, device) is None


def test_match_usb_port_returns_none_for_unconfigured_port():
    device = FakeDevice(properties={"ID_PATH": "platform-xhci-hcd.0-usb-0:1.9:1.0"})
    assert match_usb_port(USB_PORTS_CONFIG, device) is None


def test_match_usb_port_prefers_more_specific_hub_downstream_path():
    # A device behind a powered hub on port "0:1.1" has an ID_PATH that
    # contains the direct port's path as a prefix ("0:1.1.1:1.0" vs.
    # "0:1.1.0"), regardless of which order the two roles are configured in.
    config = {
        "usb_ports": {
            "enabled": True,
            "ports": [
                {"role": "sd_source", "port_path": "0:1.1.0"},
                {"role": "secondary_ssd_mirror", "port_path": "0:1.1.1:1.0"},
                {"role": "thumb_drive", "port_path": "0:1.1.3:1.0"},
            ],
        }
    }
    mirror_device = FakeDevice(properties={
        "ID_PATH": "platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.1.1:1.0-scsi-0:0:0:0"
    })
    thumb_device = FakeDevice(properties={
        "ID_PATH": "platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.1.3:1.0-scsi-0:0:0:0"
    })
    reader_device = FakeDevice(properties={
        "ID_PATH": "platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.1.0-scsi-0:0:0:0"
    })

    assert match_usb_port(config, mirror_device)["role"] == "secondary_ssd_mirror"
    assert match_usb_port(config, thumb_device)["role"] == "thumb_drive"
    assert match_usb_port(config, reader_device)["role"] == "sd_source"


def _monitor_with_recorded_exports():
    calls = []
    monitor = HotplugMonitor(
        {"paths": {"external_ssd_mount": "/mnt/external_ssd"}, "usb_ports": USB_PORTS_CONFIG["usb_ports"]},
        on_sd_card=lambda mount_point, device: None,
        on_external_ssd=lambda mount_point, device, export_overrides=None, dest_label=None: calls.append(
            (mount_point, export_overrides, dest_label)
        ),
    )
    return monitor, calls


def test_is_role_mounted_reflects_tracked_state():
    monitor, _calls = _monitor_with_recorded_exports()
    assert monitor.is_role_mounted("secondary_ssd_mirror") is False
    monitor._mounted_by_role["secondary_ssd_mirror"] = ("/dev/sdc1", "/mnt/port_secondary", FakeDevice())
    assert monitor.is_role_mounted("secondary_ssd_mirror") is True


def test_trigger_role_export_runs_once_per_day():
    monitor, calls = _monitor_with_recorded_exports()
    monitor._mounted_by_role["secondary_ssd_mirror"] = ("/dev/sdc1", "/mnt/port_secondary", FakeDevice())

    assert monitor.trigger_role_export("secondary_ssd_mirror") is True
    assert calls == [("/mnt/port_secondary", None, "Mirror SSD")]

    # Second call the same day should be a no-op even though still mounted.
    assert monitor.trigger_role_export("secondary_ssd_mirror") is False
    assert len(calls) == 1

    # ...unless forced (manual dashboard trigger bypasses the daily guard).
    assert monitor.trigger_role_export("secondary_ssd_mirror", force=True) is True
    assert len(calls) == 2


def test_trigger_role_export_returns_false_when_not_mounted():
    monitor, calls = _monitor_with_recorded_exports()
    assert monitor.trigger_role_export("secondary_ssd_mirror") is False
    assert calls == []


def test_trigger_role_export_applies_max_age_override():
    calls = []
    monitor = HotplugMonitor(
        {"paths": {"external_ssd_mount": "/mnt/external_ssd"}, "usb_ports": {
            "enabled": True,
            "ports": [{"role": "thumb_drive", "port_path": "usb-0:1.4", "max_age_days": 3}],
        }},
        on_sd_card=lambda mount_point, device: None,
        on_external_ssd=lambda mount_point, device, export_overrides=None, dest_label=None: calls.append(
            (export_overrides, dest_label)
        ),
    )
    monitor._mounted_by_role["thumb_drive"] = ("/dev/sdd1", "/mnt/port_thumb", FakeDevice())

    assert monitor.trigger_role_export("thumb_drive") is True
    assert calls == [({"filter_by_age": True, "max_age_days": 3}, "ThumbDrive Last 3 Days")]


def test_handle_event_takes_a_single_device_argument():
    # pyudev.MonitorObserver calls its callback as callback(device) - a
    # single positional argument, with the action read from device.action.
    # A callback expecting callback(action, device) crashes the observer
    # thread on the very first live hotplug event.
    removed = []
    monitor = HotplugMonitor(
        {"paths": {"external_ssd_mount": "/mnt/external_ssd"}},
        on_sd_card=lambda mount_point, device: None,
        on_external_ssd=lambda mount_point, device, export_overrides=None, dest_label=None: None,
        on_device_removed=lambda devnode: removed.append(devnode),
    )
    device = FakeDevice(device_node="/dev/sdz1", action="remove")

    monitor._handle_event(device)  # must not raise TypeError

    assert removed == ["/dev/sdz1"]


def test_eject_unknown_device_fails_cleanly():
    monitor, _calls = _monitor_with_recorded_exports()
    ok, message = monitor.eject("/dev/not-mounted")
    assert ok is False
    assert "not mounted" in message


def test_adopts_device_already_mounted_at_our_mount_point(monkeypatch, tmp_path):
    # After a daemon restart, drives the previous run mounted are still
    # mounted at our configured mount points. They must be adopted (tracked,
    # hooks run) rather than skipped as "internal system storage".
    from src import hotplug_monitor as hm
    from src import state
    state.configure(tmp_path / "state")

    exports = []
    monitor = HotplugMonitor(
        {
            "paths": {"source_sd_mount": "/mnt/source_sd", "external_ssd_mount": "/mnt/external_ssd"},
            "ingest": {"read_only_mount": True},
            "usb_ports": {
                "enabled": True,
                "ports": [{"role": "primary_ssd", "port_path": "0:1.2:1.0",
                           "mount_point": "/mnt/port_primary_ssd"}],
            },
        },
        on_sd_card=lambda mount_point, device: None,
        on_external_ssd=lambda mount_point, device, export_overrides=None, dest_label=None:
            exports.append((mount_point, dest_label)),
    )

    monkeypatch.setattr(hm, "_already_mounted_at", lambda devnode: "/mnt/port_primary_ssd")
    monkeypatch.setattr(hm, "mount_partition",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not remount")))

    device = FakeDevice(
        properties={"ID_PATH": "platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.2:1.0-scsi-0:0:0:0",
                    "ID_FS_TYPE": "exfat"},
        device_node="/dev/sda1",
    )
    monitor._handle_add("/dev/sda1", device)

    assert monitor.is_role_mounted("primary_ssd")
    assert exports == [("/mnt/port_primary_ssd", "Main SSD")]


def test_still_skips_foreign_mounts(monkeypatch, tmp_path):
    from src import hotplug_monitor as hm
    from src import state
    state.configure(tmp_path / "state")

    monitor, calls = _monitor_with_recorded_exports()
    monitor.config["paths"]["source_sd_mount"] = "/mnt/source_sd"

    monkeypatch.setattr(hm, "_already_mounted_at", lambda devnode: "/boot/firmware")
    device = FakeDevice(properties={"ID_FS_TYPE": "vfat"}, device_node="/dev/mmcblk0p1")

    monitor._handle_add("/dev/mmcblk0p1", device)

    assert calls == []
    assert not monitor._mounted
