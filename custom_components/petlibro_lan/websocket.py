"""Websocket API of the Pets app. Reads are open to every user; every change needs an admin."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import DOMAIN, SIGNAL_ANY, SIGNAL_NEW_DEVICE
from .hub import PetlibroHub
from .models import FEEDER

PUSH_DELAY = 1.0  # seconds: device updates are batched for the subscribed app
CLEAR_WORD = "CLEAR"

TIME = vol.Match(r"^([01]\d|2[0-3]):([0-5]\d)$")
SERIAL = vol.All(str, vol.Length(min=3, max=64))
MEAL = vol.Schema({
    vol.Required("time"): TIME,
    vol.Required("portions"): vol.All(int, vol.Range(min=1, max=12)),
    vol.Optional("days"): vol.All([vol.All(int, vol.Range(min=1, max=7))], vol.Length(min=1, max=7)),
    vol.Optional("label"): vol.All(str, vol.Length(max=40)),
    vol.Optional("enabled"): bool,
})
_HOURS = vol.All(int, vol.Range(min=0, max=168))
SETTINGS = vol.Schema({
    vol.Optional("targets"): vol.All([vol.All(str, vol.Length(max=200))], vol.Length(max=20)),
    vol.Optional("persistent"): bool,
    vol.Optional("quiet"): vol.Schema({
        vol.Optional("enabled"): bool, vol.Optional("start"): TIME, vol.Optional("end"): TIME}),
    vol.Optional("alerts"): vol.Schema({
        vol.Optional("food_low"): vol.Schema({vol.Optional("enabled"): bool, vol.Optional("remind_hours"): _HOURS}),
        vol.Optional("water_low"): vol.Schema({
            vol.Optional("enabled"): bool, vol.Optional("remind_hours"): _HOURS,
            vol.Optional("threshold_g"): vol.Any(None, vol.All(vol.Coerce(float), vol.Range(min=50, max=20000)))}),
        vol.Optional("filter_due"): vol.Schema({
            vol.Optional("enabled"): bool, vol.Optional("days_before"): vol.All(int, vol.Range(min=0, max=60)),
            vol.Optional("remind_hour"): vol.All(int, vol.Range(min=0, max=23))}),
        vol.Optional("offline"): vol.Schema({
            vol.Optional("enabled"): bool, vol.Optional("minutes"): vol.All(int, vol.Range(min=5, max=1440)),
            vol.Optional("remind_hours"): _HOURS}),
        vol.Optional("feed_failed"): vol.Schema({vol.Optional("enabled"): bool}),
        vol.Optional("battery_low"): vol.Schema({
            vol.Optional("enabled"): bool, vol.Optional("percent"): vol.All(int, vol.Range(min=1, max=99)),
            vol.Optional("remind_hours"): _HOURS}),
        vol.Optional("daily_summary"): vol.Schema({vol.Optional("enabled"): bool, vol.Optional("time"): TIME}),
    }),
})


def async_register(hass: HomeAssistant) -> None:
    for command in (ws_state, ws_subscribe, ws_get_schedule, ws_set_schedule, ws_clear_schedule, ws_get_alerts,
                    ws_set_alerts, ws_targets, ws_test, ws_command):
        websocket_api.async_register_command(hass, command)


def _hub(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> PetlibroHub | None:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries or not isinstance(hub := getattr(entries[0], "runtime_data", None), PetlibroHub):
        connection.send_error(msg["id"], "not_loaded", "PetLibro Local is not set up")
        return None
    return hub


def _serial(hub: PetlibroHub, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> str | None:
    if msg["serial"] not in hub.devices:
        connection.send_error(msg["id"], "not_found", "Unknown device")
        return None
    return msg["serial"]


def _state(hub: PetlibroHub, admin: bool) -> dict[str, Any]:
    return {
        "devices": [hub.view(s, admin) for s in hub.devices],
        "active_alerts": hub.alerts.active() if admin else [],
        "time_zone": str(hub.tz),
        "admin": admin,
    }


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/state"})
@callback
def ws_state(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    if (hub := _hub(hass, connection, msg)) is not None:
        connection.send_result(msg["id"], _state(hub, connection.user.is_admin))


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/subscribe"})
@callback
def ws_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """The current state now and again, batched, whenever a device changes."""
    if (hub := _hub(hass, connection, msg)) is None:
        return
    pending = None

    @callback
    def send() -> None:
        nonlocal pending
        pending = None
        if hass.config_entries.async_loaded_entries(DOMAIN):
            connection.send_message(websocket_api.event_message(msg["id"], _state(hub, connection.user.is_admin)))

    @callback
    def changed(*_: Any) -> None:
        nonlocal pending
        if pending is None:
            pending = hass.loop.call_later(PUSH_DELAY, send)

    unsubs = [async_dispatcher_connect(hass, SIGNAL_ANY, changed), async_dispatcher_connect(hass, SIGNAL_NEW_DEVICE, changed)]

    @callback
    def unsubscribe() -> None:
        for unsub in unsubs:
            unsub()
        if pending is not None:
            pending.cancel()

    connection.subscriptions[msg["id"]] = unsubscribe
    connection.send_result(msg["id"])
    connection.send_message(websocket_api.event_message(msg["id"], _state(hub, connection.user.is_admin)))


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/get_schedule"})
@callback
def ws_get_schedule(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    if (hub := _hub(hass, connection, msg)) is None:
        return
    feeders = [s for s in hub.devices if hub.info(s).kind == FEEDER and hub.info(s).max_portions]
    connection.send_result(msg["id"], {"feeders": [hub.schedule_view(s, connection.user.is_admin) for s in feeders]})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/set_schedule",
    vol.Required("serial"): SERIAL,
    vol.Required("meals"): vol.All([MEAL], vol.Length(min=1, max=24)),
    # the first save replaces whatever the PetLibro app stored on the feeder: the caller must say it knows
    vol.Optional("replace_ack", default=False): bool,
})
@websocket_api.async_response
async def ws_set_schedule(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    if (hub := _hub(hass, connection, msg)) is None or (serial := _serial(hub, connection, msg)) is None:
        return
    if serial not in hub.plans and not msg["replace_ack"]:
        connection.send_error(msg["id"], "needs_ack",
                              "Saving replaces the schedule stored on the feeder; confirm that first")
        return
    if not any(m.get("enabled", True) for m in msg["meals"]):
        connection.send_error(msg["id"], "empty", "Enable at least one meal, or use Clear schedule")
        return
    try:
        await hub.async_set_plan(serial, msg["meals"])
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "invalid", str(err))
        return
    connection.send_result(msg["id"], hub.schedule_view(serial))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/clear_schedule",
    vol.Required("serial"): SERIAL,
    vol.Required("confirm"): str,
})
@websocket_api.async_response
async def ws_clear_schedule(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """The only path that sends an empty plan to a feeder."""
    if (hub := _hub(hass, connection, msg)) is None or (serial := _serial(hub, connection, msg)) is None:
        return
    if msg["confirm"] != CLEAR_WORD:
        connection.send_error(msg["id"], "not_confirmed", f"Type {CLEAR_WORD} to confirm")
        return
    try:
        await hub.async_set_plan(serial, [])
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "invalid", str(err))
        return
    connection.send_result(msg["id"], hub.schedule_view(serial))


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/get_alerts"})
@callback
def ws_get_alerts(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    if (hub := _hub(hass, connection, msg)) is None:
        return
    connection.send_result(msg["id"], {
        "settings": hub.alerts.settings, "active": hub.alerts.active(),
        "devices": [{"id": s, "name": hub.device_name(s), "kind": hub.info(s).kind,
                     "device_low_water": hub.devices[s].data.get("low_water"),
                     "water_weight": hub.devices[s].data.get("water_weight")} for s in hub.devices],
    })


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/set_alerts", vol.Required("settings"): SETTINGS})
@callback
def ws_set_alerts(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    if (hub := _hub(hass, connection, msg)) is None:
        return
    try:
        settings = hub.alerts.async_set_settings(msg["settings"])
    except ValueError as err:
        connection.send_error(msg["id"], "invalid", str(err))
        return
    connection.send_result(msg["id"], {"settings": settings, "active": hub.alerts.active()})


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/notify_targets"})
@callback
def ws_targets(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    if (hub := _hub(hass, connection, msg)) is not None:
        connection.send_result(msg["id"], {"targets": hub.alerts.targets()})


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/test_notification"})
@websocket_api.async_response
async def ws_test(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    if (hub := _hub(hass, connection, msg)) is not None:
        connection.send_result(msg["id"], {"results": await hub.alerts.async_test()})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/command",
    vol.Required("serial"): SERIAL,
    vol.Required("action"): vol.In(["feed", "pump", "light", "reset_filter"]),
    vol.Optional("portions"): vol.All(int, vol.Range(min=1, max=12)),
    vol.Optional("on"): bool,
})
@websocket_api.async_response
async def ws_command(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    if (hub := _hub(hass, connection, msg)) is None or (serial := _serial(hub, connection, msg)) is None:
        return
    action = msg["action"]
    try:
        if action == "feed":
            if "portions" not in msg:
                raise vol.Invalid("portions is required")
            await hub.async_feed(serial, msg["portions"])
        elif action in ("pump", "light"):
            if "on" not in msg:
                raise vol.Invalid("on is required")
            await (hub.async_set_pump if action == "pump" else hub.async_set_light)(serial, msg["on"])
        else:
            await hub.async_reset_filter(serial)
    except vol.Invalid as err:
        connection.send_error(msg["id"], "invalid_format", str(err))
        return
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "failed", str(err))
        return
    connection.send_result(msg["id"], hub.view(serial))
