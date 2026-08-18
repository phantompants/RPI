"""System entry point: wires together the hotplug monitor, ingest/export
engines, sync scheduler, hardware feedback, and the web dashboard.

Usage:
    python3 src/main.py                 # run the full daemon
    python3 src/main.py --check-config   # validate config and exit
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path

if __package__ in (None, ""):
    # Allow `python3 src/main.py` (as used by systemd/mediabackup.service) to
    # find the `src` package, since running a script directly only puts its
    # own directory on sys.path, not the project root.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn

from src import config as config_module
from src import (
    export_engine,
    hardware_feedback,
    hotplug_monitor,
    ingest_engine,
    scheduled_jobs,
    state,
    sync_scheduler,
    web_server,
)


def setup_logging(log_dir: str) -> None:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(Path(log_dir) / "mediabackup.log"),
    ]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        handlers=handlers,
    )


def check_config(config_path: str | None) -> int:
    try:
        cfg = config_module.load_config(config_path)
    except config_module.ConfigError as exc:
        print(f"Config is INVALID: {exc}")
        return 1
    print(f"Config OK: {config_path or config_module.DEFAULT_CONFIG_PATH}")
    print(f"  paths.internal_ssd       = {cfg['paths']['internal_ssd']}")
    print(f"  paths.processed_media    = {cfg['paths']['processed_media']}")
    print(f"  web.host:port            = {cfg['web']['host']}:{cfg['web']['port']}")
    print(f"  cloud_sync.enabled       = {cfg['cloud_sync']['enabled']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Portable Media Backup & Cloud Sync daemon")
    parser.add_argument("--config", default=None, help="Path to main_config.yaml")
    parser.add_argument("--check-config", action="store_true", help="Validate config and exit")
    args = parser.parse_args()

    if args.check_config:
        return check_config(args.config)

    cfg = config_module.load_config(args.config)
    config_module.ensure_directories(cfg)
    setup_logging(cfg["paths"]["log_dir"])
    logger = logging.getLogger("mediabackup.main")

    state.configure(cfg["paths"]["state_dir"])

    hw_feedback = hardware_feedback.HardwareFeedback(cfg)
    hw_feedback.start()
    state.subscribe(hw_feedback.on_state_change)

    scheduler = sync_scheduler.SyncScheduler(cfg)
    daily_jobs: list[scheduled_jobs.DailyTimeTrigger] = []

    cloud_daily_time = cfg["cloud_sync"].get("daily_start_time")
    if cloud_daily_time:
        # Only fire on days where media actually changed, unless
        # sync_only_on_change is turned off (then it runs every day).
        only_on_change = cfg["cloud_sync"].get("sync_only_on_change", True)
        cloud_trigger = scheduled_jobs.DailyTimeTrigger(
            "cloud_sync", at_time=cloud_daily_time,
            action=lambda: sync_scheduler.sync_all(cfg),
            guard=lambda: cfg["cloud_sync"].get("enabled", False)
            and (state.has_unsynced_changes() or not only_on_change),
        )
        cloud_trigger.start()
        daily_jobs.append(cloud_trigger)
    else:
        scheduler.start()

    monitor = hotplug_monitor.HotplugMonitor(
        cfg,
        on_sd_card=lambda mount_point, device: ingest_engine.handle_sd_card(cfg, mount_point, device),
        on_external_ssd=lambda mount_point, device, export_overrides=None, dest_label=None:
            export_engine.handle_external_ssd(cfg, mount_point, device, export_overrides, dest_label),
    )
    monitor.start()

    for port in (cfg.get("usb_ports") or {}).get("ports", []):
        if cfg.get("usb_ports", {}).get("enabled") and port.get("earliest_run_time"):
            role = port["role"]
            gated_trigger = scheduled_jobs.DailyTimeTrigger(
                role, at_time=port["earliest_run_time"],
                action=lambda role=role: monitor.trigger_role_export(role),
                guard=lambda role=role: monitor.is_role_mounted(role),
            )
            gated_trigger.start()
            daily_jobs.append(gated_trigger)

    app = web_server.create_app(cfg, sync_scheduler=scheduler, hardware_feedback=hw_feedback, monitor=monitor)
    server_config = uvicorn.Config(app, host=cfg["web"]["host"], port=cfg["web"]["port"], log_level="info")
    server = uvicorn.Server(server_config)

    def handle_signal(signum, frame) -> None:
        logger.info("Received signal %s, shutting down", signum)
        server.should_exit = True

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    logger.info("Portable Media Backup & Cloud Sync daemon starting up")
    try:
        server.run()
    finally:
        monitor.stop()
        scheduler.stop()
        for job in daily_jobs:
            job.stop()
        hw_feedback.stop()
        logger.info("Daemon stopped")

    return 0


if __name__ == "__main__":
    sys.exit(main())
