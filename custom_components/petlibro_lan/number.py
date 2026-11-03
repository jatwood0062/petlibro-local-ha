"""Portions used by the Feed now button."""
from __future__ import annotations

from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import FEEDER, PetlibroEntity, setup_entities


class FeedPortions(PetlibroEntity, RestoreNumber):
    _attr_native_min_value = 1
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    def __init__(self, hub, serial) -> None:
        super().__init__(hub, serial, "feed_portions")
        self._attr_native_max_value = hub.info(serial).max_portions
        hub.feed_portions.setdefault(serial, 1)

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> float:
        return self.hub.feed_portions[self.serial]

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_number_data()) and last.native_value:
            self.hub.feed_portions[self.serial] = max(1, min(int(last.native_value), int(self._attr_native_max_value)))

    async def async_set_native_value(self, value: float) -> None:
        self.hub.feed_portions[self.serial] = int(value)
        self.async_write_ha_state()


def _make(hub, serial, kind):
    return [FeedPortions(hub, serial)] if kind == FEEDER and hub.info(serial).max_portions else []


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    setup_entities(hass, entry, add, _make)
