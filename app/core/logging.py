from __future__ import annotations

import logging
from typing import Optional

from .log_buffer import LogBuffer


def configure_logging(level: int = logging.INFO, buffer_size: int = 500) -> LogBuffer:
    """Configure application-wide logging and return the in-memory buffer."""
    log_buffer = LogBuffer(maxlen=buffer_size)

    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    logging.getLogger().addHandler(log_buffer.handler)
    return log_buffer


def set_log_level(level: Optional[int]) -> None:
    if level is not None:
        logging.getLogger().setLevel(level)

