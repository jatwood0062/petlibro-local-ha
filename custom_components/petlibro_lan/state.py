"""Per-device state tracker; no Home Assistant imports."""
from __future__ import annotations

from collections import deque
from datetime import date
from typing import Any

from .protocol import parse_message

DEDUPE = 64


class DeviceState:
    """Merged state of one device: partial pushes merge, retransmitted dispenses count once."""

    def __init__(self, serial: str, model: str) -> None:
        self.serial = serial
        self.model = model
        self.data: dict[str, Any] = {}
        self.ip: str | None = None
        self.last_seen: float | None = None  # monotonic
        self.last_seen_ts: float | None = None  # wall clock (epoch seconds)
        self.feeds_today = 0
        self.feeds_day: date | None = None
        self._seen: deque[tuple[str, str, str]] = deque(maxlen=DEDUPE)

    def ingest(self, channel: str, msg: dict[str, Any], now_ms: int, today: date) -> dict[str, Any]:
        """Apply one device message; returns what changed."""
        if self.feeds_day != today:
            self.feeds_day, self.feeds_today = today, 0
        if msg.get("cmd") == "GRAIN_OUTPUT_EVENT" and isinstance(msg.get("msgId"), str):
            key = (msg["cmd"], msg["msgId"], str(msg.get("execStep", "")))
            if key in self._seen:
                return {}
            self._seen.append(key)
        updates = parse_message(channel, msg, now_ms)
        if updates.pop("feed_completed", False):
            self.feeds_today += 1
        if "device_ts" in updates:
            updates["clock_drift_s"] = round((updates.pop("device_ts") - now_ms) / 1000)
        self.data.update(updates)
        return updates
