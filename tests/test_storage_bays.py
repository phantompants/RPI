from src.web_server import assemble_storage_bays

CONFIG = {
    "web": {
        "storage_bays": [
            {"name": "USB3 Lower - Main SSD", "match": "0:1.2:1.0"},
            {"name": "USB3 Upper - USB-C Hub - Micro SD", "match": "0:1.1.3:1.0-scsi-0:0:0:0"},
            {"name": "USB3 Upper - USB-C Hub - SD", "match": "0:1.1.3:1.0-scsi-0:0:0:1"},
            {"name": "USB2 Upper - USB Thumb Drive", "match": "0:1.3:1.0"},
        ]
    }
}


def test_bays_preserve_configured_order_and_report_disconnected():
    bays = assemble_storage_bays(CONFIG, {})
    assert [b["name"] for b in bays] == [b["name"] for b in CONFIG["web"]["storage_bays"]]
    assert all(b["connected"] is False for b in bays)


def test_mounted_device_fills_matching_bay(tmp_path):
    devices = {
        "/dev/sda1": {
            "status": "mounted",
            "id_path": "platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.2:1.0-scsi-0:0:0:0",
            "mount_point": str(tmp_path),
        }
    }
    bays = assemble_storage_bays(CONFIG, devices)
    main_ssd = bays[0]
    assert main_ssd["connected"] is True
    assert main_ssd["devnode"] == "/dev/sda1"
    assert main_ssd["total_bytes"] > 0
    assert all(b["connected"] is False for b in bays[1:])


def test_scsi_lun_distinguishes_card_slots(tmp_path):
    devices = {
        "/dev/sdc1": {
            "status": "mounted",
            "id_path": "platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.1.3:1.0-scsi-0:0:0:1-part1",
            "mount_point": str(tmp_path),
        }
    }
    bays = assemble_storage_bays(CONFIG, devices)
    by_name = {b["name"]: b for b in bays}
    assert by_name["USB3 Upper - USB-C Hub - SD"]["connected"] is True
    assert by_name["USB3 Upper - USB-C Hub - Micro SD"]["connected"] is False


def test_removed_devices_do_not_fill_bays(tmp_path):
    devices = {
        "/dev/sdd1": {
            "status": "removed",
            "id_path": "platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.3:1.0-scsi-0:0:0:0",
            "mount_point": str(tmp_path),
        }
    }
    bays = assemble_storage_bays(CONFIG, devices)
    assert all(b["connected"] is False for b in bays)


def test_stale_mount_point_reports_disconnected():
    devices = {
        "/dev/sda1": {
            "status": "mounted",
            "id_path": "platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.2:1.0-scsi-0:0:0:0",
            "mount_point": "/does/not/exist/anymore",
        }
    }
    bays = assemble_storage_bays(CONFIG, devices)
    assert bays[0]["connected"] is False
