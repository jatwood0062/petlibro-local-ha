"""Binary sensors: connectivity, food low, dispenser blocked, water low, filter due."""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import alerts
from .hub import PetlibroHub
from .entity import FEEDER, FOUNTAIN, PetlibroEntity, setup_entities


def _food_low(d: dict[str, Any], hub: PetlibroHub) -> bool | None:
    return alerts.food_low(d)


def _water_low(d: dict[str, Any], hub: PetlibroHub) -> bool | None:
    """Weight below the user's threshold from the app, else the device's own plausible one, else 500 g."""
    return alerts.water_low(d, hub.alerts.water_override())


def _filter_due(d: dict[str, Any], hub: PetlibroHub) -> bool | None:
    days = alerts.filter_days_left(d, time.time())
    return None if days is None else days <= 0


def _blocked(d: dict[str, Any], hub: PetlibroHub) -> bool | None:
    return d.get("feed_blocked")


@dataclass(frozen=True, kw_only=True)
class Desc(BinarySensorEntityDescription):
    value: Callable[[dict[str, Any], PetlibroHub], bool | None]


FEEDER_SENSORS = (
    Desc(key="food_low", device_class=BinarySensorDeviceClass.PROBLEM, value=_food_low),
    Desc(key="dispenser_blocked", device_class=BinarySensorDeviceClass.PROBLEM, value=_blocked),
)
FOUNTAIN_SENSORS = (
    Desc(key="water_low", device_class=BinarySensorDeviceClass.PROBLEM, value=_water_low),
    Desc(key="filter_due", device_class=BinarySensorDeviceClass.PROBLEM, value=_filter_due),
)


class PetlibroBinarySensor(PetlibroEntity, BinarySensorEntity):
    def __init__(self, hub, serial, desc: Desc) -> None:
        super().__init__(hub, serial, desc.key)
        self.entity_description = desc

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value(self.data, self.hub)


class Connectivity(PetlibroEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hub, serial) -> None:
        super().__init__(hub, serial, "connectivity")

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return self.hub.is_online(self.serial)


def _make(hub, serial, kind):
    out: list = [Connectivity(hub, serial)]
    descs = FEEDER_SENSORS if kind == FEEDER else FOUNTAIN_SENSORS if kind == FOUNTAIN else ()
    return out + [PetlibroBinarySensor(hub, serial, d) for d in descs]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    setup_entities(hass, entry, add, _make)
