"""PetLibro Local: feeders and water fountains over a local MQTT broker."""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store

from .const import DOMAIN, SERVICE_FEED, SERVICE_SET_PLAN, STORE_VERSION
from .hub import PetlibroHub

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["binary_sensor", "button", "number", "sensor", "switch"]

PLAN_ENTRY = vol.Schema({
    vol.Required("time"): cv.time,
    vol.Required("portions"): vol.All(vol.Coerce(int), vol.Range(min=1, max=12)),
    vol.Optional("days"): vol.All(cv.ensure_list, [vol.All(vol.Coerce(int), vol.Range(min=1, max=7))]),
})
FEED_SCHEMA = vol.Schema({
    vol.Required("device_id"): cv.string,
    vol.Required("portions"): vol.All(vol.Coerce(int), vol.Range(min=1, max=12)),
})
PLAN_SCHEMA = vol.Schema({
    vol.Required("device_id"): cv.string,
    vol.Required("plans"): [PLAN_ENTRY],
})

CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)
PetlibroConfigEntry = ConfigEntry  # runtime_data is a PetlibroHub


def _hub(hass: HomeAssistant) -> PetlibroHub:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        raise ServiceValidationError("PetLibro Local is not set up")
    return entries[0].runtime_data


def _serial(hass: HomeAssistant, hub: PetlibroHub, device_id: str) -> str:
    device = dr.async_get(hass).async_get(device_id)
    for domain, serial in (device.identifiers if device else ()):
        if domain == DOMAIN and serial in hub.devices:
            return serial
    raise ServiceValidationError("Choose a PetLibro Local device")


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    async def feed(call: ServiceCall) -> None:
        hub = _hub(hass)
        await hub.async_feed(_serial(hass, hub, call.data["device_id"]), call.data["portions"])

    async def set_plan(call: ServiceCall) -> None:
        hub = _hub(hass)
        plans = [dict(p, time=p["time"].strftime("%H:%M")) for p in call.data["plans"]]
        await hub.async_set_plan(_serial(hass, hub, call.data["device_id"]), plans)

    hass.services.async_register(DOMAIN, SERVICE_FEED, feed, FEED_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_SET_PLAN, set_plan, PLAN_SCHEMA)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: PetlibroConfigEntry) -> bool:
    hub = PetlibroHub(hass, entry)
    try:
        await hub.async_start()
    except ConnectionError as err:
        raise ConfigEntryNotReady(str(err)) from err
    entry.runtime_data = hub
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await hub.async_stop()
        raise
    entry.async_on_unload(entry.add_update_listener(_reload))
    return True


async def _reload(hass: HomeAssistant, entry: PetlibroConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_entry(hass: HomeAssistant, entry: PetlibroConfigEntry) -> None:
    await Store(hass, STORE_VERSION, DOMAIN).async_remove()


async def async_unload_entry(hass: HomeAssistant, entry: PetlibroConfigEntry) -> bool:
    if ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.async_stop()
    return ok
