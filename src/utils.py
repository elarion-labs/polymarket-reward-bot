"""
utils.py — Shared utility functions.
"""
from __future__ import annotations

import json
import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque

# ---------------------------------------------------------------------------
# Structured logger
# ---------------------------------------------------------------------------

_log_records: Deque[str] = deque(maxlen=200)  # in-memory tail for dashboard


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        if hasattr(record, "extra"):
            data.update(record.extra)  # type: ignore[arg-type]
        line = json.dumps(data)
        _log_records.append(line)
        return line


def setup_logging(log_file: str = "bot.log") -> logging.Logger:
    logger = logging.getLogger("reward_bot")
    logger.setLevel(logging.DEBUG)

    # File handler (JSON)
    fh = logging.FileHandler(log_file)
    fh.setFormatter(_JsonFormatter())
    logger.addHandler(fh)

    # Console handler (human readable)
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(ch)

    return logger


def get_log_tail(n: int = 50) -> list[str]:
    return list(_log_records)[-n:]


# ---------------------------------------------------------------------------
# Price / size helpers
# ---------------------------------------------------------------------------

def round_price(price: float, tick: float = 0.01) -> float:
    """Round price to the nearest tick."""
    return round(round(price / tick) * tick, 10)


def round_size(size: float, min_size: float = 5.0, decimals: int = 0) -> float:
    """Round size down to min_size granularity."""
    size = max(min_size, size)
    factor = 10 ** decimals
    return int(size * factor) / factor


def cents_to_price(cents: float) -> float:
    """Convert spread in cents to price units (1 cent = 0.01)."""
    return cents / 100.0


def now_utc() -> float:
    return time.time()


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Simple rate limiter
# ---------------------------------------------------------------------------

class RateLimiter:
    """Token-bucket style rate limiter."""

    def __init__(self, calls: int, period: float):
        self._calls = calls
        self._period = period
        self._timestamps: Deque[float] = deque()

    def wait(self) -> None:
        now = time.monotonic()
        self._timestamps.append(now)
        while self._timestamps and self._timestamps[0] < now - self._period:
            self._timestamps.popleft()
        if len(self._timestamps) > self._calls:
            sleep_for = self._period - (now - self._timestamps[0])
            if sleep_for > 0:
                time.sleep(sleep_for)
