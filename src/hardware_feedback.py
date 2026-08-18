"""Status LEDs, M5Stack notifications, and audible/visual alerts.

LED colour convention:
  Blue          - ingesting an SD card
  Green         - success / safe to eject
  Red           - error
  Pulsing amber - SSD export in progress
  Solid amber   - SSD export complete

Gracefully degrades to no-op stubs when GPIO/serial hardware isn't present
(e.g. running the dashboard on a dev machine), so the rest of the daemon can
depend on this module unconditionally.
"""
from __future__ import annotations

import json
import logging
import queue
import subprocess
import threading
import time
from typing import Any, Optional

logger = logging.getLogger("mediabackup.hardware")

try:
    from gpiozero import LED, PWMLED
    GPIO_AVAILABLE = True
except (ImportError, NotImplementedError):
    GPIO_AVAILABLE = False

try:
    import serial as pyserial
    SERIAL_AVAILABLE = True
except ImportError:
    SERIAL_AVAILABLE = False

try:
    import websockets
    from websockets.sync.client import connect as ws_connect
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False


# --------------------------------------------------------------------------
# LED control
# --------------------------------------------------------------------------

class LEDController:
    def __init__(self, gpio_cfg: dict[str, Any]):
        self.enabled = gpio_cfg.get("enabled", False) and GPIO_AVAILABLE
        self._leds: dict[str, Any] = {}
        if not self.enabled:
            if gpio_cfg.get("enabled") and not GPIO_AVAILABLE:
                logger.warning("GPIO alerts enabled in config but gpiozero is unavailable; LEDs disabled")
            return

        pins = gpio_cfg.get("pins", {})
        for name in ("red", "green", "blue"):
            if name in pins:
                self._leds[name] = LED(pins[name])
        if "yellow" in pins:
            self._leds["yellow"] = PWMLED(pins["yellow"])

    def _all_off(self) -> None:
        for led in self._leds.values():
            led.off()

    def set_state(self, status: str) -> None:
        if not self.enabled:
            return
        self._all_off()
        if status == "ingesting" and "blue" in self._leds:
            self._leds["blue"].on()
        elif status in ("idle", "safe_to_eject", "success") and "green" in self._leds:
            self._leds["green"].on()
        elif status == "error" and "red" in self._leds:
            self._leds["red"].on()
        elif status == "ssd_copying" and "yellow" in self._leds:
            self._leds["yellow"].pulse()
        elif status == "export_complete" and "yellow" in self._leds:
            self._leds["yellow"].on()

    def close(self) -> None:
        for led in self._leds.values():
            try:
                led.close()
            except Exception:
                pass


# --------------------------------------------------------------------------
# M5Stack notifier (serial or websocket transport)
# --------------------------------------------------------------------------

class M5StackNotifier:
    def __init__(self, m5_cfg: dict[str, Any]):
        self.enabled = m5_cfg.get("enabled", False)
        self.transport = m5_cfg.get("transport", "serial")
        self.cfg = m5_cfg
        self._queue: "queue.Queue[dict]" = queue.Queue()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if not self.enabled:
            return
        if self.transport == "serial" and not SERIAL_AVAILABLE:
            logger.warning("M5Stack serial transport configured but pyserial is unavailable")
            return
        if self.transport == "websocket" and not WEBSOCKETS_AVAILABLE:
            logger.warning("M5Stack websocket transport configured but websockets is unavailable")
            return
        self._thread = threading.Thread(target=self._run, name="m5stack-notifier", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)

    def send(self, payload: dict[str, Any]) -> None:
        if self.enabled:
            self._queue.put(payload)

    def _run(self) -> None:
        if self.transport == "serial":
            self._run_serial()
        else:
            self._run_websocket()

    def _run_serial(self) -> None:
        port, baud = self.cfg["serial_port"], self.cfg.get("serial_baud", 115200)
        conn = None
        while not self._stop_event.is_set():
            try:
                if conn is None:
                    conn = pyserial.Serial(port, baud, timeout=1)
                    logger.info("M5Stack serial connection open on %s", port)
                payload = self._queue.get(timeout=1)
                conn.write((json.dumps(payload) + "\n").encode("utf-8"))
            except queue.Empty:
                continue
            except (OSError, pyserial.SerialException):
                logger.warning("M5Stack serial link error, retrying in 5s")
                conn = None
                time.sleep(5)

    def _run_websocket(self) -> None:
        url = self.cfg["websocket_url"]
        conn = None
        while not self._stop_event.is_set():
            try:
                if conn is None:
                    conn = ws_connect(url, open_timeout=5)
                    logger.info("M5Stack websocket connection open to %s", url)
                payload = self._queue.get(timeout=1)
                conn.send(json.dumps(payload))
            except queue.Empty:
                continue
            except Exception:
                logger.warning("M5Stack websocket error, retrying in 5s")
                conn = None
                time.sleep(5)


# --------------------------------------------------------------------------
# Audible / visual alert
# --------------------------------------------------------------------------

def trigger_alert(sound_cfg: dict[str, Any], message: str) -> None:
    logger.info("ALERT: %s", message)
    print(f"\a[ALERT] {message}")
    if sound_cfg.get("enabled") and sound_cfg.get("command"):
        try:
            subprocess.Popen(sound_cfg["command"], shell=True)
        except Exception:
            logger.warning("Failed to run alert sound command")


# --------------------------------------------------------------------------
# Coordinator: wires state.py changes to LEDs / M5Stack / alerts
# --------------------------------------------------------------------------

class HardwareFeedback:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        alerts_cfg = config["alerts"]
        self.leds = LEDController(alerts_cfg.get("gpio", {}))
        self.m5stack = M5StackNotifier(alerts_cfg.get("m5stack", {}))
        self.sound_cfg = alerts_cfg.get("sound", {})
        self._last_system_status: Optional[str] = None
        self._known_job_status: dict[str, str] = {}

    def start(self) -> None:
        self.m5stack.start()

    def stop(self) -> None:
        self.m5stack.stop()
        self.leds.close()

    def on_state_change(self, snapshot: dict[str, Any]) -> None:
        system_status = snapshot.get("system", "idle")
        if system_status != self._last_system_status:
            self.leds.set_state(system_status)
            self.m5stack.send({"type": "system_status", "status": system_status})
            self._last_system_status = system_status

        for job_id, job in snapshot.get("jobs", {}).items():
            status = job.get("status")
            if status in ("success", "error") and self._known_job_status.get(job_id) != status:
                self._known_job_status[job_id] = status
                self.m5stack.send({"type": "job_complete", "job_id": job_id, "job_type": job.get("type"),
                                    "status": status})
                if status == "success":
                    trigger_alert(self.sound_cfg, f"Job {job.get('type')} completed successfully")
                else:
                    trigger_alert(self.sound_cfg, f"Job {job.get('type')} failed: {job.get('error')}")
