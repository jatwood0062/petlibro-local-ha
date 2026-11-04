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
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

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
    SIGNAL_NEW_DEVICE,
    SIGNAL_UPDATE,
    STORE_VERSION,
)
from .hosts import parse_forward
from .models import FEEDER, ModelInfo, model_info
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
        try:
            await self.broker.start()
        except OSError as err:
            raise ConnectionError(f"cannot listen on port {self.broker.port}: {err}") from err
        self._unsub_tick = async_track_time_interval(self.hass, self._tick, timedelta(seconds=30))

    async def async_stop(self) -> None:
        if self._unsub_tick:
            self._unsub_tick()
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

    def _spawn(self, coro) -> None:
        task = self.hass.async_create_background_task(coro, f"{DOMAIN} task")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _signal(self, serial: str) -> None:
        async_dispatcher_send(self.hass, SIGNAL_UPDATE.format(serial))

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
        came_online = serial not in self._online
        self._online.add(serial)

        now = protocol.now_ms()
        local_today = dt_util.now().date()
        if "linkId" not in msg:
            dev.ingest(parsed.channel, msg, now, local_today)  # also the ATTR_GET_SERVICE replies
            if parsed.channel == "service":
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
