"""Switches: indicator light and button lock (feeders), pump and light (fountains)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import protocol
from .entity import FEEDER, FOUNTAIN, PetlibroEntity, setup_entities


class PetlibroSwitch(PetlibroEntity, SwitchEntity):
    """On/off attribute: ``state_key`` is the flat state key, ``build`` makes the command."""

    def __init__(self, hub, serial, key: str, state_key: str, build) -> None:
        super().__init__(hub, serial, key)
        self._state_key, self._build = state_key, build

    @property
    def is_on(self) -> bool | None:
        return self.data.get(self._state_key)

    async def _set(self, on: bool) -> None:
        await self.hub.async_set_attrs(self.serial, self._build(on), {self._state_key: on})

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)


def _make(hub, serial, kind):
    info = hub.info(serial)
    out = []
    if kind == FEEDER:
        if info.light_switch:
            out.append(PetlibroSwitch(hub, serial, "indicator_light", "light", protocol.indicator_light))
        if info.lock_key and info.max_portions:
            key = "button_lock" if info.lock_key == "disableHardwareButton" else "child_lock"
            lock = info.lock_key
            out.append(PetlibroSwitch(hub, serial, "button_lock", key, lambda on: protocol.button_lock(on, lock)))
    elif kind == FOUNTAIN:
        out.append(PetlibroSwitch(hub, serial, "pump", "pump_on", protocol.pump))
        out.append(PetlibroSwitch(hub, serial, "light", "light", protocol.fountain_light))
    return out


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    setup_entities(hass, entry, add, _make)
