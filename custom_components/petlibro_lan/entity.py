"""Shared entity base and dynamic setup."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_NEW_DEVICE, SIGNAL_UPDATE
from .hub import PetlibroHub
from .models import FEEDER, FOUNTAIN


class PetlibroEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hub: PetlibroHub, serial: str, key: str) -> None:
        self.hub = hub
        self.serial = serial
        self._attr_translation_key = key
        self._attr_unique_id = f"{serial}_{key}"
        info = hub.info(serial)
        dev = hub.devices[serial]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            manufacturer="PetLibro",
            model=dev.model,
            model_id=dev.model,
            name=f"{info.name} {serial[-4:]}",
            sw_version=dev.data.get("software_version"),
            hw_version=dev.data.get("hardware_version"),
        )

    @property
    def data(self) -> dict[str, Any]:
        return self.hub.devices[self.serial].data

    @property
    def available(self) -> bool:
        return self.hub.is_online(self.serial)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(
            self.hass, SIGNAL_UPDATE.format(self.serial), self.async_write_ha_state))


def setup_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    add: AddEntitiesCallback,
    factory: Callable[[PetlibroHub, str, str], list[Entity]],
) -> None:
    """Add entities for known devices now and for new ones as they connect.

    ``factory(hub, serial, kind)`` returns the entities of one platform for one device.
    """
    hub: PetlibroHub = entry.runtime_data
    done: set[str] = set()

    @callback
    def add_device(serial: str) -> None:
        if serial in done or serial not in hub.devices:
            return
        done.add(serial)
        info = hub.info(serial)
        if entities := factory(hub, serial, info.kind):
            add(entities)

    for serial in list(hub.devices):
        add_device(serial)
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_DEVICE, add_device))


__all__ = ["FEEDER", "FOUNTAIN", "PetlibroEntity", "setup_entities"]
