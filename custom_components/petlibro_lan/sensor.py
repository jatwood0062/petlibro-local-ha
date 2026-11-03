"""Sensors: battery, Wi-Fi, feeding history and plan, water, filter, IP."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfMass, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .entity import FEEDER, FOUNTAIN, PetlibroEntity, setup_entities

POWER_SOURCES = {2: "battery", 3: "mains"}


def _ts(ms: Any) -> datetime | None:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc) if isinstance(ms, (int, float)) else None


def _filter_days(d: dict[str, Any]) -> float | None:
    if not isinstance(due := d.get("filter_due_ms"), (int, float)):
        return None
    return round((due / 1000 - datetime.now(timezone.utc).timestamp()) / 86400, 1)


@dataclass(frozen=True, kw_only=True)
class Desc(SensorEntityDescription):
    value: Callable[[dict[str, Any]], Any]


COMMON = (
    Desc(key="rssi", device_class=SensorDeviceClass.SIGNAL_STRENGTH, native_unit_of_measurement="dBm",
         state_class=SensorStateClass.MEASUREMENT, entity_category=EntityCategory.DIAGNOSTIC,
         value=lambda d: d.get("rssi")),
)
BATTERY = Desc(key="battery", device_class=SensorDeviceClass.BATTERY, native_unit_of_measurement=PERCENTAGE,
               state_class=SensorStateClass.MEASUREMENT, value=lambda d: d.get("battery"))
FEEDER_SENSORS = (
    Desc(key="power_source", device_class=SensorDeviceClass.ENUM, options=["battery", "mains", "unknown"],
         entity_category=EntityCategory.DIAGNOSTIC,
         value=lambda d: POWER_SOURCES.get(d["power_type"], "unknown") if "power_type" in d else None),
    Desc(key="last_feed_time", device_class=SensorDeviceClass.TIMESTAMP, value=lambda d: _ts(d.get("last_feed_ms"))),
    Desc(key="last_feed_portions", state_class=SensorStateClass.MEASUREMENT,
         value=lambda d: d.get("last_feed_portions")),
)
FOUNTAIN_SENSORS = (
    Desc(key="water_weight", device_class=SensorDeviceClass.WEIGHT, native_unit_of_measurement=UnitOfMass.GRAMS,
         state_class=SensorStateClass.MEASUREMENT, value=lambda d: d.get("water_weight")),
    Desc(key="water_level", native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
         value=lambda d: d.get("water_percent")),
    Desc(key="filter_days", native_unit_of_measurement=UnitOfTime.DAYS, value=_filter_days),
)


class PetlibroSensor(PetlibroEntity, SensorEntity):
    def __init__(self, hub, serial, desc: Desc) -> None:
        super().__init__(hub, serial, desc.key)
        self.entity_description = desc

    @property
    def native_value(self) -> Any:
        return self.entity_description.value(self.data)


class FeedsToday(PetlibroEntity, SensorEntity):
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, hub, serial) -> None:
        super().__init__(hub, serial, "feeds_today")

    @property
    def native_value(self) -> int:
        dev = self.hub.devices[self.serial]
        return dev.feeds_today if dev.feeds_day == dt_util.now().date() else 0


class FeedingPlan(PetlibroEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, hub, serial) -> None:
        super().__init__(hub, serial, "feeding_plan")

    @property
    def native_value(self) -> datetime | None:
        nxt = self.hub.next_feed(self.serial)
        return nxt[0] if nxt else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        nxt = self.hub.next_feed(self.serial)
        return {"plans": self.hub.plans.get(self.serial), "next_portions": nxt[1] if nxt else None}


class IpAddress(PetlibroEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, hub, serial) -> None:
        super().__init__(hub, serial, "ip_address")

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> str | None:
        return self.hub.devices[self.serial].ip


def _make(hub, serial, kind):
    info = hub.info(serial)
    out: list = [IpAddress(hub, serial)]
    descs = list(COMMON)
    if kind == FEEDER:
        descs += FEEDER_SENSORS + (BATTERY,)
        if info.max_portions:
            out += [FeedsToday(hub, serial), FeedingPlan(hub, serial)]
    elif kind == FOUNTAIN:
        descs += FOUNTAIN_SENSORS
        if info.battery:
            descs.append(BATTERY)
    return out + [PetlibroSensor(hub, serial, d) for d in descs]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    setup_entities(hass, entry, add, _make)
