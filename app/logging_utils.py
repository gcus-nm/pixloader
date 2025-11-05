from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Deque, Dict, List

LOG_BUFFER_SIZE = 500


class LogBufferHandler(logging.Handler):
    """Captures log records in a fixed-size deque for viewer consumption."""

    def __init__(self, buffer: Deque[Dict[str, str]]) -> None:
        super().__init__()
        self._buffer = buffer
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401 - override
        message = self.format(record)
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "name": record.name,
            "message": message,
        }
        with self._lock:
            self._buffer.append(payload)


class LogBuffer:
    """Thread-safe accessor for the in-memory log buffer."""

    def __init__(self, maxlen: int = LOG_BUFFER_SIZE) -> None:
        self._buffer: Deque[Dict[str, str]] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    @property
    def handler(self) -> logging.Handler:
        return LogBufferHandler(self._buffer)

    def snapshot(self, limit: int = 100) -> List[Dict[str, str]]:
        limit = max(1, min(limit, LOG_BUFFER_SIZE))
        with self._lock:
            # copy from right (newest) backwards
            items = list(self._buffer)[-limit:]
        return items
