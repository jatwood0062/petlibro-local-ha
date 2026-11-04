"""Home Assistant glue for the alert engine: persistence, delivery and the one-minute tick."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import alerts
from .const import ALERT_EVENT, ALERT_STORE_KEY, ALERT_TICK, PANEL_URL_PATH, STORE_VERSION

if TYPE_CHECKING:
    from .hub import PetlibroHub

_LOGGER = logging.getLogger(__name__)
SAVE_DELAY = 10
HIDDEN_SERVICES = {"send_message", "persistent_notification", "notify"}


class AlertManager:
    """Owns the alert settings and state, runs ``alerts.evaluate`` and delivers what it returns."""

    def __init__(self, hass: HomeAssistant, hub: PetlibroHub) -> None:
        self.hass = hass
        self.hub = hub
        self.settings: dict[str, Any] = alerts.normalize_settings(None)
        self.state: dict[str, Any] = {}
        self._store: Store = Store(hass, STORE_VERSION, ALERT_STORE_KEY)
        self._unsub = None
        self._saved = ""

    # --- lifecycle ---
    async def async_load(self) -> None:
        saved = await self._store.async_load()
        if isinstance(saved, dict):
            try:
                self.settings = alerts.normalize_settings(saved.get("settings"))
            except ValueError:
                _LOGGER.warning("Ignoring invalid stored alert settings")
            if isinstance(saved.get("state"), dict):
                self.state = saved["state"]
        self._saved = self._dump()

    def start(self) -> None:
        self._unsub = async_track_time_interval(self.hass, self._tick, timedelta(seconds=ALERT_TICK))

    async def async_stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None
        await self._store.async_save(self._data())

    async def async_remove(self) -> None:
        await self._store.async_remove()

    def _data(self) -> dict[str, Any]:
        return {"settings": self.settings, "state": self.state}

    def _dump(self) -> str:
        return json.dumps(self._data(), sort_keys=True, default=str)

    # --- evaluation ---
    async def _tick(self, _now: datetime) -> None:
        self.evaluate()

    @callback
    def evaluate(self) -> None:
        """Run the engine on the current devices; never raises (it runs inside message handling)."""
        try:
            actions = alerts.evaluate(self.settings, self.state, self.hub.alert_devices(), time.time(), dt_util.now())
        except Exception:  # noqa: BLE001 - a bug here must not break the broker
            _LOGGER.exception("Alert evaluation failed")
            return
        for action in actions:
            self._deliver(action)
        if (dumped := self._dump()) != self._saved:
            self._saved = dumped
            self._store.async_delay_save(self._data, SAVE_DELAY)

    @callback
    def async_set_settings(self, raw: Any) -> dict[str, Any]:
        self.settings = alerts.normalize_settings(raw)
        self.evaluate()
        self.hub.signal_all()
        return self.settings

    def water_override(self) -> float | None:
        return self.settings["alerts"]["water_low"].get("threshold_g")

    def water_cfg(self) -> dict[str, Any]:
        return self.settings["alerts"]["water_low"]

    @callback
    def forget_device(self, serial: str) -> None:
        """A device was removed: drop its alert state and dismiss its notifications."""
        for key in [k for k in (self.state.get("alerts") or {}) if k.endswith(f":{serial}")]:
            del self.state["alerts"][key]
        for alert in alerts.ALERT_TYPES:
            persistent_notification.async_dismiss(self.hass, alerts.tag_for(alert, serial))
        if (dumped := self._dump()) != self._saved:
            self._saved = dumped
            self._store.async_delay_save(self._data, SAVE_DELAY)

    def active(self) -> list[dict[str, Any]]:
        return alerts.active_alerts(self.state, {s: self.hub.device_name(s) for s in self.hub.devices})

    # --- delivery ---
    def _deliver(self, action: alerts.Action) -> None:
        if action.event:
            self.hass.bus.async_fire(ALERT_EVENT, {
                "type": action.type, "device": action.device, "serial": action.serial or "",
                "state": action.state, "title": action.title, "message": action.message,
                "reminder": action.reminder,
            })
        if action.dismiss:
            persistent_notification.async_dismiss(self.hass, action.tag)
        if action.persist and self.settings["persistent"]:
            persistent_notification.async_create(self.hass, action.message, action.title, action.tag)
        if action.push:
            for target in self.settings["targets"]:
                self.hub.spawn(self._send(target, action.title, action.message, action.tag))

    async def _send(self, target: str, title: str, message: str, tag: str) -> str | None:
        """Deliver to one target; returns an error text or None."""
        kind, _, name = target.partition(":")
        try:
            if kind == "service":
                data: dict[str, Any] = {"title": title, "message": message}
                if name.startswith("mobile_app_"):
                    # tag: a newer message for the same alert replaces the old one; url/clickAction open the app
                    path = f"/{PANEL_URL_PATH}"
                    data["data"] = {"tag": tag, "group": "petlibro", "url": path, "clickAction": path}
                await self.hass.services.async_call("notify", name, data, blocking=True)
            else:
                await self.hass.services.async_call(
                    "notify", "send_message", {"entity_id": name, "title": title, "message": message}, blocking=True)
        except Exception as err:  # noqa: BLE001 - one broken target must not stop the others
            _LOGGER.warning("Notification to %s failed: %s", target, err)
            return str(err) or type(err).__name__
        return None

    async def async_test(self) -> list[dict[str, Any]]:
        """Send a test message to every target (and the persistent notification); reports each result."""
        title, message = "PetLibro test notification", "If you can read this, Pets alerts reach this device."
        tag = "petlibro_lan_test"
        if self.settings["persistent"]:
            persistent_notification.async_create(self.hass, message, title, tag)
        results = []
        for target in self.settings["targets"]:
            error = await self._send(target, title, message, tag)
            results.append({"target": target, "ok": error is None, "error": error})
        return results

    # --- what can be notified ---
    def targets(self) -> list[dict[str, str]]:
        out = []
        for name in sorted(self.hass.services.async_services_for_domain("notify")):
            if name in HIDDEN_SERVICES:
                continue
            label = name.removeprefix("mobile_app_").replace("_", " ")
            out.append({"id": f"service:{name}", "kind": "service", "label": f"notify.{name}",
                        "name": label.title() if name.startswith("mobile_app_") else name.replace("_", " ").title()})
        for state in sorted(self.hass.states.async_all("notify"), key=lambda s: s.entity_id):
            out.append({"id": f"entity:{state.entity_id}", "kind": "entity", "label": state.entity_id,
                        "name": state.name})
        return out
