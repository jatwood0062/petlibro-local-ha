"""Diagnostics with serials, MACs and addresses redacted."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import PetlibroConfigEntry

TO_REDACT = {"ip", "mac", "serial", "uuid", "username", "password", "connected"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: PetlibroConfigEntry) -> dict[str, Any]:
    snap = entry.runtime_data.snapshot()
    names = {serial: f"device_{i}" for i, serial in enumerate(snap["devices"], 1)}
    snap["devices"] = {names[s]: d for s, d in snap["devices"].items()}
    return {"options": dict(entry.options), "hub": async_redact_data(snap, TO_REDACT)}
