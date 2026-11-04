"""Buttons: feed now, reset filter, zero scale."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import protocol
from .entity import FEEDER, FOUNTAIN, PetlibroEntity, setup_entities


class FeedNow(PetlibroEntity, ButtonEntity):
    def __init__(self, hub, serial) -> None:
        super().__init__(hub, serial, "feed_now")

    async def async_press(self) -> None:
        await self.hub.async_feed(self.serial, self.hub.feed_portions.get(self.serial, 1))


class ResetFilter(PetlibroEntity, ButtonEntity):
    def __init__(self, hub, serial) -> None:
        super().__init__(hub, serial, "reset_filter")

    async def async_press(self) -> None:
        await self.hub.async_reset_filter(self.serial)


class ZeroScale(PetlibroEntity, ButtonEntity):
    _attr_entity_registry_enabled_default = False

    def __init__(self, hub, serial) -> None:
        super().__init__(hub, serial, "zero_scale")

    async def async_press(self) -> None:
        await self.hub.async_command(self.serial, protocol.weight_clear())


def _make(hub, serial, kind):
    if kind == FEEDER:
        return [FeedNow(hub, serial)] if hub.info(serial).max_portions else []
    if kind == FOUNTAIN:
        return [ResetFilter(hub, serial), ZeroScale(hub, serial)]
    return []


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    setup_entities(hass, entry, add, _make)
