"""A background thread that fires an action once per calendar day, once a
local time-of-day threshold has passed, and only while a guard condition
holds - e.g. "run the mirror export, but only after 20:00 and only while
the mirror drive is actually plugged in" or "sync to the cloud, but only
after 22:00 and only if cloud sync is enabled".
"""
from __future__ import annotations

import datetime
import logging
import threading
from typing import Callable, Optional

logger = logging.getLogger("mediabackup.scheduled_jobs")


def should_fire(now: datetime.datetime, at_time: datetime.time,
                last_run_date: Optional[datetime.date], guard_ok: bool) -> bool:
    """Pure decision logic, split out from the polling loop so it can be
    unit tested without real threads/clocks."""
    if now.time() < at_time:
        return False
    if last_run_date == now.date():
        return False
    return guard_ok


class DailyTimeTrigger:
    def __init__(self, name: str, at_time: str, action: Callable[[], None],
                 guard: Optional[Callable[[], bool]] = None, poll_seconds: int = 30):
        self.name = name
        self.at_time = datetime.datetime.strptime(at_time, "%H:%M").time()
        self.action = action
        self.guard = guard or (lambda: True)
        self.poll_seconds = poll_seconds
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_run_date: Optional[datetime.date] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name=f"daily-trigger-{self.name}", daemon=True)
        self._thread.start()
        logger.info("Daily trigger '%s' scheduled for %s", self.name, self.at_time.strftime("%H:%M"))

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop_event.wait(self.poll_seconds):
            now = datetime.datetime.now()
            if not should_fire(now, self.at_time, self._last_run_date, self.guard()):
                continue
            self._last_run_date = now.date()
            try:
                logger.info("Running scheduled job '%s' (%s threshold reached)", self.name, self.at_time)
                self.action()
            except Exception:
                logger.exception("Scheduled job '%s' raised an exception", self.name)
