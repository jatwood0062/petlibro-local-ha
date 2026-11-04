"""Owns the broker, the devices and their feeding plans."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import alerts as alertlib
from . import plans as planlib
from . import protocol
from .broker import Broker
from .const import (
    AVAILABILITY_TIMEOUT,
    COMMAND_TIMEOUT,
    CONF_PORT,
    CONF_TLS_FORWARD,
    DEFAULT_PORT,
    DOMAIN,
    PLAN_MAX_TRIES,
    PLAN_RESYNC,
    PLAN_RETRY,
    POLL_INTERVAL,
    FILTER_LIFE_DAYS,
    SIGNAL_ANY,
    SIGNAL_NEW_DEVICE,
    SIGNAL_UPDATE,
    STORE_VERSION,
)
from .hosts import parse_forward
from .models import FEEDER, FOUNTAIN, ModelInfo, model_info
from .notifier import AlertManager
from .state import DeviceState

_LOGGER = logging.getLogger(__name__)
RAW_LOG_LIMIT = 600
SAVE_DELAY = 5


def _mask(serial: str) -> str:
    """Serials are not logged in full above debug level."""
    return f"...{serial[-4:]}"


class PetlibroHub:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.broker = Broker(
            self._on_publish, self._on_connect, self._on_disconnect,
            port=entry.options.get(CONF_PORT, DEFAULT_PORT),
            tls_forward=parse_forward(entry.options.get(CONF_TLS_FORWARD, "")),
        )
        self.devices: dict[str, DeviceState] = {}
        self.plans: dict[str, list[dict[str, Any]]] = {}  # only devices with a user-configured plan
        self.feed_portions: dict[str, int] = {}
        self._store: Store = Store(hass, STORE_VERSION, DOMAIN)
        self._online: set[str] = set()
        self._pending: dict[str, asyncio.Future] = {}
        self._tasks: set[asyncio.Task] = set()
        self._plan_sig: dict[str, tuple] = {}
        self._plan_synced: dict[str, float] = {}
        self._plan_sent: dict[str, float] = {}
        self._plan_tries: dict[str, int] = {}
        self._last_poll: dict[str, float] = {}
        self._ips: dict[str, str] = {}
        self._unsub_tick = None
        self.started_ts = time.time()
        self.alerts = AlertManager(hass, self)

    @property
    def tz(self):
        return dt_util.get_default_time_zone()

    async def async_start(self) -> None:
        saved = await self._store.async_load() or {}
        for serial, item in (saved.get("devices") or {}).items():
            if isinstance(item, dict) and isinstance(item.get("model"), str):
                self.devices[serial] = DeviceState(serial, item["model"])
        self.plans = {}
        for serial, stored in (saved.get("plans") or {}).items():
            if serial in self.devices and (info := self.info(serial)).max_portions:
                try:
                    self.plans[serial] = planlib.normalize(stored, info.max_portions)
                except ValueError:
                    _LOGGER.warning("Ignoring invalid stored feeding plan for %s", _mask(serial))
        await self.alerts.async_load()  # before the broker: a device update must not evaluate default settings
        try:
            await self.broker.start()
        except OSError as err:
            raise ConnectionError(f"cannot listen on port {self.broker.port}: {err}") from err
        self._unsub_tick = async_track_time_interval(self.hass, self._tick, timedelta(seconds=30))
        self.alerts.start()

    async def async_stop(self) -> None:
        if self._unsub_tick:
            self._unsub_tick()
        await self.alerts.async_stop()
        await self.broker.stop()
        for task in list(self._tasks):
            task.cancel()
        await self._store.async_save(self._snapshot())

    def _snapshot(self) -> dict[str, Any]:
        return {"devices": {s: {"model": d.model} for s, d in self.devices.items()}, "plans": self.plans}

    def _save(self) -> None:
        self._store.async_delay_save(self._snapshot, SAVE_DELAY)

    def info(self, serial: str) -> ModelInfo:
        dev = self.devices[serial]
        return model_info(dev.model, serial) or ModelInfo(dev.model, FEEDER)

    def is_online(self, serial: str) -> bool:
        return serial in self._online

    def spawn(self, coro) -> None:
        self._spawn(coro)

    def _spawn(self, coro) -> None:
        task = self.hass.async_create_background_task(coro, f"{DOMAIN} task")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _signal(self, serial: str) -> None:
        async_dispatcher_send(self.hass, SIGNAL_UPDATE.format(serial))
        async_dispatcher_send(self.hass, SIGNAL_ANY)
        self.alerts.evaluate()

    def signal_all(self) -> None:
        """Settings changed: entities that depend on them (water low) and the app refresh."""
        for serial in self.devices:
            async_dispatcher_send(self.hass, SIGNAL_UPDATE.format(serial))
        async_dispatcher_send(self.hass, SIGNAL_ANY)

    # --- broker callbacks ---
    def _on_connect(self, serial: str, ip: str) -> None:
        _LOGGER.debug("PetLibro device %s connected from %s", serial, ip)
        self._ips[serial] = ip
        if (dev := self.devices.get(serial)) is not None:
            dev.ip = ip
            self._signal(serial)

    def _on_disconnect(self, serial: str) -> None:
        _LOGGER.debug("PetLibro device %s disconnected", serial)
        if serial in self._online:
            self._online.discard(serial)
            self._signal(serial)

    def _on_publish(self, serial: str, topic: str, payload: bytes) -> None:
        _LOGGER.debug("RX %s: %s", topic, payload[:RAW_LOG_LIMIT])
        parsed = protocol.parse_topic(topic)
        if parsed is None or parsed.direction != "post" or parsed.serial != serial:
            return
        if (msg := protocol.decode(payload)) is None:
            _LOGGER.debug("Ignoring non-JSON message on %s", topic)
            return
        dev = self.devices.get(serial)
        if dev is None:
            if model_info(parsed.model, serial) is None:
                _LOGGER.warning("Ignoring unknown PetLibro device %s (model %s)", _mask(serial), parsed.model)
                return
            dev = self.devices[serial] = DeviceState(serial, parsed.model)
            dev.ip = self._ips.get(serial)
            _LOGGER.info("New PetLibro device %s (%s)", _mask(serial), parsed.model)
            self._save()
            async_dispatcher_send(self.hass, SIGNAL_NEW_DEVICE, serial)
        elif dev.model != parsed.model:
            dev.model = parsed.model
            self._save()
        dev.last_seen = time.monotonic()
        dev.last_seen_ts = time.time()
        came_online = serial not in self._online
        self._online.add(serial)

        now = protocol.now_ms()
        local_today = dt_util.now().date()
        if "linkId" not in msg:
            dev.ingest(parsed.channel, msg, now, local_today)  # also the ATTR_GET_SERVICE replies
            if parsed.channel == "service" or protocol.is_reply(msg.get("cmd")):
                self._resolve(msg)
        wire = self._wire_plan(serial)
        for channel, reply in protocol.respond(parsed.channel, msg, now, self.tz, wire):
            self._send_raw(dev, channel, reply)
        if came_online or msg.get("cmd") == "DEVICE_START_EVENT":
            self._send_raw(dev, "service", protocol.attr_get())
            self._last_poll[serial] = time.monotonic()
        self._signal(serial)

    def _resolve(self, msg: dict[str, Any]) -> None:
        msg_id = msg.get("msgId")
        fut = self._pending.get(msg_id) if isinstance(msg_id, str) else None
        if fut is not None and not fut.done():
            fut.set_result(msg)

    # --- sending ---
    def _send_raw(self, dev: DeviceState, channel: str, msg: dict[str, Any]) -> None:
        topic = protocol.build_topic(dev.model, dev.serial, channel)
        payload = protocol.encode(msg)
        _LOGGER.debug("TX %s: %s", topic, payload[:RAW_LOG_LIMIT])
        self._spawn(self.broker.publish(dev.serial, topic, payload))

    async def async_command(self, serial: str, msg: dict[str, Any]) -> dict[str, Any]:
        """Send a command and wait for the device's reply; raises HomeAssistantError on failure."""
        dev = self.devices[serial]
        if serial not in self._online or not self.broker.is_connected(serial):
            raise HomeAssistantError(f"{_mask(serial)} is not connected")
        loop = asyncio.get_running_loop()
        fut = self._pending[msg["msgId"]] = loop.create_future()
        try:
            topic = protocol.build_topic(dev.model, serial, "service")
            if not await self.broker.publish(serial, topic, protocol.encode(msg)):
                raise HomeAssistantError(f"{_mask(serial)} is not listening for commands")
            try:
                reply = await asyncio.wait_for(fut, COMMAND_TIMEOUT)
            except (asyncio.TimeoutError, TimeoutError) as err:
                raise HomeAssistantError(f"{_mask(serial)} did not answer {msg['cmd']}") from err
        finally:
            self._pending.pop(msg["msgId"], None)
        if reply.get("code", 0) != 0:
            raise HomeAssistantError(f"{_mask(serial)} rejected {msg['cmd']} (code {reply.get('code')})")
        return reply

    async def async_set_attrs(self, serial: str, msg: dict[str, Any], expect: dict[str, Any]) -> None:
        """Send an ATTR_SET and apply the confirmed values to the state."""
        await self.async_command(serial, msg)
        self.devices[serial].data.update(expect)
        self._signal(serial)

    async def async_feed(self, serial: str, portions: int) -> None:
        info = self.info(serial)
        if not info.max_portions:
            raise ServiceValidationError(f"{info.name} does not support manual feeding here")
        try:
            msg = protocol.manual_feed(portions, info.max_portions)
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        await self.async_command(serial, msg)

    async def async_set_pump(self, serial: str, on: bool) -> None:
        if self.info(serial).kind != FOUNTAIN:
            raise ServiceValidationError("Only fountains have a pump")
        await self.async_set_attrs(serial, protocol.pump(on), {"pump_on": on})

    async def async_set_light(self, serial: str, on: bool) -> None:
        info = self.info(serial)
        if info.kind == FOUNTAIN:
            msg = protocol.fountain_light(on)
        elif info.light_switch:
            msg = protocol.indicator_light(on)
        else:
            raise ServiceValidationError(f"{info.name} has no controllable light")
        await self.async_set_attrs(serial, msg, {"light": on})

    async def async_reset_filter(self, serial: str) -> None:
        if self.info(serial).kind != FOUNTAIN:
            raise ServiceValidationError("Only fountains have a filter")
        msg = protocol.filter_reset(FILTER_LIFE_DAYS)
        await self.async_set_attrs(serial, msg, {"filter_due_ms": msg["filterNextReplacementTimestamp"]})

    # --- plans ---
    def _wire_plan(self, serial: str) -> list[dict[str, Any]] | None:
        stored = self.plans.get(serial)
        if stored is None:
            return None
        return planlib.to_wire(stored, self.tz, datetime.now(timezone.utc), protocol.now_ms())

    async def async_set_plan(self, serial: str, raw: object) -> None:
        info = self.info(serial)
        if not info.max_portions:
            raise ServiceValidationError(f"{info.name} does not support feeding plans here")
        try:
            self.plans[serial] = planlib.normalize(raw, info.max_portions)
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        self._plan_sig.pop(serial, None)
        self._plan_tries[serial] = 0
        self._save()
        self._signal(serial)
        if serial in self._online:
            await self._sync_plan(serial)

    async def _sync_plan(self, serial: str) -> None:
        wire = self._wire_plan(serial)
        if wire is None:
            return
        self._plan_sent[serial] = time.monotonic()
        self._plan_tries[serial] = self._plan_tries.get(serial, 0) + 1
        try:
            await self.async_command(serial, protocol.set_feeding_plan(wire))
        except HomeAssistantError as err:
            _LOGGER.warning("Feeding plan for %s not confirmed yet: %s", _mask(serial), err)
            return
        self._plan_sig[serial] = planlib.signature(wire)
        self._plan_synced[serial] = time.monotonic()
        self._plan_tries[serial] = 0
        _LOGGER.info("Feeding plan sent to %s (%d entries)", _mask(serial), len(wire))

    def next_feed(self, serial: str) -> tuple[datetime, int] | None:
        stored = self.plans.get(serial)
        return planlib.next_feed(stored, self.tz, datetime.now(timezone.utc)) if stored else None

    # --- periodic work ---
    async def _tick(self, _now: datetime) -> None:
        mono = time.monotonic()
        for serial, dev in self.devices.items():
            if serial in self._online and (dev.last_seen is None or mono - dev.last_seen > AVAILABILITY_TIMEOUT):
                self._online.discard(serial)
                self._signal(serial)
                continue
            if serial not in self._online:
                continue
            if mono - self._last_poll.get(serial, 0) >= POLL_INTERVAL:
                self._last_poll[serial] = mono
                self._send_raw(dev, "service", protocol.attr_get())
            self._maybe_resync(serial, mono)

    def _maybe_resync(self, serial: str, mono: float) -> None:
        wire = self._wire_plan(serial)
        if wire is None or mono - self._plan_sent.get(serial, 0) < PLAN_RETRY:
            return
        changed = planlib.signature(wire) != self._plan_sig.get(serial)
        stale = mono - self._plan_synced.get(serial, 0) >= PLAN_RESYNC
        if (changed or stale) and self._plan_tries.get(serial, 0) < PLAN_MAX_TRIES:
            self._spawn(self._sync_plan(serial))

    # --- views for the alert engine and the app ---
    def device_name(self, serial: str) -> str:
        """The name the user sees: the device registry name (renamed or not)."""
        reg = next((d for d in dr.async_entries_for_config_entry(dr.async_get(self.hass), self.entry.entry_id)
                    if (DOMAIN, serial) in d.identifiers), None)
        if reg is not None and (reg.name_by_user or reg.name):
            return reg.name_by_user or reg.name
        return f"{self.info(serial).name} {serial[-4:]}"

    def alert_devices(self) -> list[dict[str, Any]]:
        out = []
        for serial, dev in self.devices.items():
            info = self.info(serial)
            online = serial in self._online
            nxt = self.next_feed(serial)
            out.append({
                "serial": serial, "name": self.device_name(serial), "kind": info.kind, "online": online,
                "offline_since": None if online else (dev.last_seen_ts or self.started_ts),
                "battery": info.battery, "data": dev.data, "feeds_today": self._feeds_today(dev),
                "next_feed": (int(nxt[0].timestamp() * 1000), nxt[1]) if nxt else None,
            })
        return out

    @staticmethod
    def _feeds_today(dev: DeviceState) -> int:
        return dev.feeds_today if dev.feeds_day == dt_util.now().date() else 0

    def view(self, serial: str, admin: bool = True) -> dict[str, Any]:
        """Everything the Overview tab shows about one device."""
        dev, info, d = self.devices[serial], self.info(serial), self.devices[serial].data
        now = time.time()
        out: dict[str, Any] = {
            "id": serial if admin else f"...{serial[-4:]}", "name": self.device_name(serial), "model": dev.model,
            "model_name": info.name, "kind": info.kind, "online": serial in self._online,
            "last_seen_ts": int(dev.last_seen_ts * 1000) if dev.last_seen_ts else None,
            "can_feed": bool(info.max_portions), "max_portions": info.max_portions,
            "can_light": info.kind == FOUNTAIN or info.light_switch,
            "light": d.get("light"), "battery": d.get("battery") if info.battery else None,
            "has_battery": info.battery, "rssi": d.get("rssi"),
        }
        if info.kind == FEEDER:
            nxt = self.next_feed(serial)
            out.update(
                food_low=alertlib.food_low(d), feed_blocked=d.get("feed_blocked"),
                last_feed_ms=d.get("last_feed_ms"), last_feed_portions=d.get("last_feed_portions"),
                last_feed_scheduled=d.get("last_feed_scheduled"), feeds_today=self._feeds_today(dev),
                plan_managed=serial in self.plans,
                next_feed=({"ts": int(nxt[0].timestamp() * 1000), "portions": nxt[1]} if nxt else None),
                feed_portions=self.feed_portions.get(serial, 1))
        elif info.kind == FOUNTAIN:
            override = self.alerts.water_override()
            limit, source = alertlib.water_threshold(d, override)
            days = alertlib.filter_days_left(d, now)
            out.update(
                pump_on=d.get("pump_on"), charging=alertlib.charging(d), plugged_in=alertlib.plugged_in(d),
                battery_state=d.get("battery_state"), water_weight=d.get("water_weight"),
                water_low=alertlib.water_low(d, override), water_threshold=limit, water_threshold_source=source,
                device_low_water=d.get("low_water"), filter_due_ms=d.get("filter_due_ms"),
                filter_days_left=None if days is None else round(days, 1), filter_overdue=None if days is None else days <= 0,
                radar_seen_ms=d.get("radar_seen_ms"), radar_now=d.get("radar_now"))
        return out

    def plan_sync_state(self, serial: str) -> str:
        """"synced", "pending" (waiting for the feeder to confirm) or "none" (no plan from Home Assistant)."""
        wire = self._wire_plan(serial)
        if wire is None:
            return "none"
        return "synced" if planlib.signature(wire) == self._plan_sig.get(serial) else "pending"

    def schedule_view(self, serial: str, admin: bool = True) -> dict[str, Any]:
        stored = self.plans.get(serial)
        upcoming = planlib.upcoming(stored, self.tz, datetime.now(timezone.utc)) if stored else []
        return {
            "id": serial if admin else f"...{serial[-4:]}", "name": self.device_name(serial), "max_portions": self.info(serial).max_portions,
            "configured": stored is not None, "meals": stored or [], "sync": self.plan_sync_state(serial),
            "online": serial in self._online, "time_zone": str(self.tz),
            "upcoming": [{"ts": int(at.timestamp() * 1000), "portions": p, "label": label} for at, p, label in upcoming],
        }

    # --- diagnostics ---
    def snapshot(self) -> dict[str, Any]:
        return {
            "port": self.broker.port,
            "connected": self.broker.clients,
            "devices": {
                s: {"model": d.model, "ip": d.ip, "online": s in self._online, "data": dict(d.data),
                    "feeds_today": d.feeds_today, "plan": self.plans.get(s)}
                for s, d in self.devices.items()
            },
        }
