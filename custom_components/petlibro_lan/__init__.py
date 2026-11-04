"""PetLibro Local: feeders and water fountains over a local MQTT broker."""
from __future__ import annotations

import logging
import os

import voluptuous as vol

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store
from homeassistant.loader import async_get_integration

from . import calibration, websocket
from .const import (
    ALERT_STORE_KEY,
    DOMAIN,
    FRONTEND_URL,
    PANEL_ELEMENT,
    PANEL_URL_PATH,
    SERVICE_FEED,
    SERVICE_SET_PLAN,
    STORE_VERSION,
)
from .hub import PetlibroHub

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["binary_sensor", "button", "number", "sensor", "switch"]

PLAN_ENTRY = vol.Schema({
    vol.Required("time"): cv.time,
    vol.Required("portions"): vol.All(vol.Coerce(int), vol.Range(min=1, max=12)),
    vol.Optional("days"): vol.All(cv.ensure_list, [vol.All(vol.Coerce(int), vol.Range(min=1, max=7))]),
    vol.Optional("label"): vol.All(cv.string, vol.Length(max=40)),
    vol.Optional("enabled"): cv.boolean,
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
    # the app's JavaScript ships with the integration; the sidebar entry itself comes and goes with the config entry
    folder = os.path.join(os.path.dirname(__file__), "frontend")
    await hass.http.async_register_static_paths([StaticPathConfig(FRONTEND_URL, folder, False)])
    websocket.async_register(hass)

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
        await _register_panel(hass)
    except BaseException:
        await hub.async_stop()
        raise
    entry.async_on_unload(entry.add_update_listener(_reload))
    return True


async def _register_panel(hass: HomeAssistant) -> None:
    """The "Pets" sidebar app (visible to everyone; every change in it needs an admin)."""
    integration = await async_get_integration(hass, DOMAIN)
    frontend.async_remove_panel(hass, PANEL_URL_PATH, warn_if_unknown=False)
    await panel_custom.async_register_panel(
        hass, frontend_url_path=PANEL_URL_PATH, webcomponent_name=PANEL_ELEMENT, sidebar_title="Pets",
        sidebar_icon="mdi:paw", module_url=f"{FRONTEND_URL}/{PANEL_ELEMENT}.js?v={integration.version}",
        require_admin=False)


async def _reload(hass: HomeAssistant, entry: PetlibroConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_entry(hass: HomeAssistant, entry: PetlibroConfigEntry) -> None:
    await Store(hass, STORE_VERSION, DOMAIN).async_remove()
    await Store(hass, STORE_VERSION, ALERT_STORE_KEY).async_remove()


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: PetlibroConfigEntry, device_entry: dr.DeviceEntry
) -> bool:
    """The "Delete" button on a device's page: forget the device and everything stored about it.

    A device that is still redirected to Home Assistant reappears the next time it connects
    (block it from the Pets app's Devices tab to prevent that).
    """
    hub = getattr(entry, "runtime_data", None)
    for domain, serial in device_entry.identifiers:
        if domain != DOMAIN:
            continue
        if isinstance(hub, PetlibroHub):
            hub.forget_device(serial)  # the device registry entry itself is removed by the caller
        else:  # the entry is not loaded: clean the store directly so the device does not come back on load
            store = Store(hass, STORE_VERSION, DOMAIN)
            if isinstance(saved := await store.async_load(), dict):
                await store.async_save(calibration.purge_serial(saved, serial))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PetlibroConfigEntry) -> bool:
    if ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        frontend.async_remove_panel(hass, PANEL_URL_PATH, warn_if_unknown=False)
        await entry.runtime_data.async_stop()
    return ok
