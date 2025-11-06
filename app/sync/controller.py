from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class SyncStatus:
    in_progress: bool = False
    last_started_at: Optional[datetime] = None
    last_finished_at: Optional[datetime] = None
    last_error: Optional[str] = None
    last_cycle: int = 0


class SyncController:
    """Coordinates synchronization cycle requests and exposes current status."""

    def __init__(self, interval_seconds: int) -> None:
        self._interval = max(0, interval_seconds)
        self._manual_trigger = threading.Event()
        self._status = SyncStatus()
        self._lock = threading.Lock()

    @property
    def interval(self) -> int:
        return self._interval

    def request_sync(self) -> None:
        self._manual_trigger.set()

    def mark_cycle_start(self, cycle: int) -> None:
        with self._lock:
            self._status.in_progress = True
            self._status.last_started_at = datetime.utcnow()
            self._status.last_cycle = cycle
            self._status.last_error = None

    def mark_cycle_end(self, *, error: Optional[str] = None) -> None:
        with self._lock:
            self._status.in_progress = False
            self._status.last_finished_at = datetime.utcnow()
            self._status.last_error = error

    def get_status(self) -> SyncStatus:
        with self._lock:
            return SyncStatus(
                in_progress=self._status.in_progress,
                last_started_at=self._status.last_started_at,
                last_finished_at=self._status.last_finished_at,
                last_error=self._status.last_error,
                last_cycle=self._status.last_cycle,
            )

    def wait_for_next_cycle(self, stop_event: threading.Event) -> bool:
        if stop_event.is_set():
            return False

        if self._interval <= 0:
            self._manual_trigger.wait()
            self._manual_trigger.clear()
            return not stop_event.is_set()

        triggered = self._manual_trigger.wait(timeout=self._interval)
        if triggered:
            self._manual_trigger.clear()
            return not stop_event.is_set()

        return not stop_event.is_set()

    def wait_for_manual(self, stop_event: threading.Event) -> bool:
        while not stop_event.is_set():
            if self._manual_trigger.wait(timeout=0.5):
                self._manual_trigger.clear()
                return True
        return False

