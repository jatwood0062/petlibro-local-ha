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
from . import calibration as callib
from . import plans as planlib
from . import protocol
from .broker import Broker
from .const import (
    AVAILABILITY_TIMEOUT,
    CAPTURE_TIMEOUT,
    CLOUD_HOST,
    COMMAND_TIMEOUT,
    CONSUMPTION_SAVE,
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
    SIGNAL_DEVICE_REMOVED,
    SIGNAL_NEW_DEVICE,
    SIGNAL_UPDATE,
    STORE_VERSION,
)
from .hosts import classify_dns, is_lan_ipv4, parse_forward, resolve_ipv4
from .models import FEEDER, FOUNTAIN, ModelInfo, mask_serial, model_info
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
        self.blocked: dict[str, dict[str, Any]] = {}  # serials the broker refuses: {model, name, ts}
        self.broker = Broker(
            self._on_publish, self._on_connect, self._on_disconnect,
            port=entry.options.get(CONF_PORT, DEFAULT_PORT),
            tls_forward=parse_forward(entry.options.get(CONF_TLS_FORWARD, "")),
            blocked=lambda serial: serial in self.blocked, on_blocked=self._on_blocked,
        )
        self.calibration: dict[str, dict[str, float]] = {}  # fountains: empty_g / full_g (+ timestamps)
        self.consumption: dict[str, callib.Consumption] = {}
        self._connecting: dict[str, dict[str, Any]] = {}  # connected, nothing published yet: {ip, ts, model}
        self._blocked_seen: dict[str, tuple[str, float]] = {}  # last refused attempt: (ip, epoch s)
        self._water_waiters: dict[str, list[asyncio.Future]] = {}
        self._consumption_saved = 0.0
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
        self.blocked = callib.load_blocked(saved.get("blocked"))
        self.calibration = {s: c for s, c in callib.load_calibrations(saved.get("calibration")).items() if s in self.devices}
        self.consumption = {s: c for s, c in callib.load_consumption(saved.get("consumption")).items() if s in self.devices}
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
        return {
            "devices": {s: {"model": d.model} for s, d in self.devices.items()}, "plans": self.plans,
            "blocked": self.blocked, "calibration": self.calibration,
            "consumption": {s: c.to_dict() for s, c in self.consumption.items()},
        }

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
        else:  # the Devices tab shows it while it introduces itself
            self._connecting[serial] = {"ip": ip, "ts": time.time(), "model": None}
            async_dispatcher_send(self.hass, SIGNAL_ANY)

    def _on_disconnect(self, serial: str) -> None:
        _LOGGER.debug("PetLibro device %s disconnected", serial)
        if self._connecting.pop(serial, None) is not None:
            async_dispatcher_send(self.hass, SIGNAL_ANY)
        if serial in self._online:
            self._online.discard(serial)
            self._signal(serial)

    def _on_blocked(self, serial: str, ip: str) -> None:
        _LOGGER.debug("Refused blocked PetLibro device %s from %s", _mask(serial), ip)
        self._blocked_seen[serial] = (ip, time.time())
        async_dispatcher_send(self.hass, SIGNAL_ANY)

    def _on_publish(self, serial: str, topic: str, payload: bytes) -> None:
        _LOGGER.debug("RX %s: %s", topic, payload[:RAW_LOG_LIMIT])
        parsed = protocol.parse_topic(topic)
        if parsed is None or parsed.direction != "post" or parsed.serial != serial:
            return
        if (msg := protocol.decode(payload)) is None:
            _LOGGER.debug("Ignoring non-JSON message on %s", topic)
            return
        if serial in self.blocked:
            return
        dev = self.devices.get(serial)
        if dev is None:
            if model_info(parsed.model, serial) is None:
                _LOGGER.warning("Ignoring unknown PetLibro device %s (model %s)", _mask(serial), parsed.model)
                if serial in self._connecting:
                    self._connecting[serial]["model"] = parsed.model
                return
            dev = self.devices[serial] = DeviceState(serial, parsed.model)
            self._connecting.pop(serial, None)
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
            if msg.get("cmd") == "WATER_CAP_EVENT":
                self._on_water(serial, protocol.water_weights(msg), local_today)
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

    # --- water: readings, calibration, consumption ---
    def _on_water(self, serial: str, weights: list[float], today) -> None:
        if not weights:
            return
        for fut in self._water_waiters.get(serial, []):
            if not fut.done():
                fut.set_result(weights)
        tracker = self.consumption.setdefault(serial, callib.Consumption())
        mono = time.monotonic()
        if tracker.update(weights, today) or mono - self._consumption_saved >= CONSUMPTION_SAVE:
            self._consumption_saved = mono
            self._save()

    def _fountain(self, serial: str) -> ModelInfo:
        info = self.info(serial)
        if info.kind != FOUNTAIN:
            raise ServiceValidationError("Only fountains have a water level")
        return info

    def usable_cal(self, serial: str) -> dict[str, float] | None:
        cal = self.calibration.get(serial)
        return cal if callib.usable(cal) else None

    async def async_capture_water(self, serial: str, which: str) -> dict[str, Any]:
        """Record the fountain's current weight as the empty or the full reading.

        Waits for a reading that arrives after the call (up to CAPTURE_TIMEOUT) and averages its samples.
        """
        self._fountain(serial)
        if serial not in self._online:
            raise HomeAssistantError(f"{_mask(serial)} is not connected")
        fut = asyncio.get_running_loop().create_future()
        waiters = self._water_waiters.setdefault(serial, [])
        waiters.append(fut)
        try:
            weights = await asyncio.wait_for(fut, CAPTURE_TIMEOUT)
        except (asyncio.TimeoutError, TimeoutError) as err:
            raise HomeAssistantError(
                f"No new water reading arrived within {CAPTURE_TIMEOUT} seconds. Check that the fountain is online.") from err
        finally:
            if fut in waiters:
                waiters.remove(fut)
            if not waiters:
                self._water_waiters.pop(serial, None)
        grams = sum(weights) / len(weights)
        dropped = False
        try:
            if which == "empty":
                cal, dropped = callib.set_empty(self.calibration.get(serial), grams, time.time())
            else:
                cal = callib.set_full(self.calibration.get(serial), grams, time.time())
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        self.calibration[serial] = cal
        self._save()
        self._signal(serial)
        return {"grams": round(grams, 1), "dropped_full": dropped}

    def set_calibration(self, serial: str, empty_g: float, full_g: float) -> None:
        self._fountain(serial)
        try:
            self.calibration[serial] = callib.set_both(empty_g, full_g, time.time())
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        self._save()
        self._signal(serial)

    def reset_calibration(self, serial: str) -> None:
        self._fountain(serial)
        self.calibration.pop(serial, None)
        self._save()
        self._signal(serial)

    async def async_zero_scale(self, serial: str) -> None:
        self._fountain(serial)
        await self.async_command(serial, protocol.weight_clear())

    def water_info(self, serial: str) -> dict[str, Any]:
        """Level, volume, use and threshold of a fountain (the entities and the app share this)."""
        d = self.devices[serial].data
        cal = self.usable_cal(serial)
        wcfg = self.alerts.water_cfg()
        check = alertlib.water_check(d, wcfg.get("threshold_g"), cal, wcfg.get("threshold_pct"))
        weight = d.get("water_weight")
        weight = weight if isinstance(weight, (int, float)) and not isinstance(weight, bool) else None
        if check is not None:
            limit, unit, source, low = check.limit, check.unit, check.source, check.low
        else:
            limit, source = alertlib.water_threshold(d, wcfg.get("threshold_g"))
            unit, low = "g", None
        today = dt_util.now().date()
        tracker = self.consumption.get(serial)
        avg = tracker.average_daily(today) if tracker else None
        ml = callib.remaining_ml(weight, cal) if cal and weight is not None else None
        return {
            "calibrated": cal is not None, "level_pct": check.level if check else None, "remaining_ml": ml,
            "used_today_ml": tracker.used_today(today) if tracker and weight is not None else None,
            "avg_daily_ml": None if avg is None else round(avg),
            "days_left": callib.days_left(ml, avg),
            "low": low, "threshold": limit, "threshold_unit": unit, "threshold_source": source,
        }

    # --- devices: rename, remove, block, DNS check ---
    def rename_device(self, serial: str, name: str) -> None:
        """Set the device registry name the user sees ("" returns to the default name)."""
        try:
            clean = callib.clean_name(name)
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        registry = dr.async_get(self.hass)
        if (device := registry.async_get_device(identifiers={(DOMAIN, serial)})) is None:
            raise ServiceValidationError("This device is not in the device registry yet")
        registry.async_update_device(device.id, name_by_user=clean or None)
        self.signal_all()

    def forget_device(self, serial: str) -> None:
        """Disconnect a device and delete everything stored about it (not its registry entry, not the block list)."""
        self.broker.disconnect(serial)
        for fut in self._water_waiters.pop(serial, []):
            if not fut.done():
                fut.set_exception(HomeAssistantError("The device was removed"))
        self._online.discard(serial)
        for table in (self.devices, self.plans, self.feed_portions, self.calibration, self.consumption,
                      self._plan_sig, self._plan_synced, self._plan_sent, self._plan_tries, self._last_poll,
                      self._ips, self._connecting):
            table.pop(serial, None)
        self.alerts.forget_device(serial)
        self._save()
        async_dispatcher_send(self.hass, SIGNAL_DEVICE_REMOVED, serial)
        async_dispatcher_send(self.hass, SIGNAL_ANY)

    async def async_remove_device(self, serial: str, block: bool = False) -> None:
        """Remove a device from Home Assistant; with ``block`` the broker also refuses its serial from now on."""
        known = serial in self.devices
        if block:
            self.blocked[serial] = {
                "model": self.devices[serial].model if known else None,
                "name": self.device_name(serial)[:callib.NAME_MAX] if known else "", "ts": time.time()}
        if known:
            self.forget_device(serial)
        else:
            self.broker.disconnect(serial)
            self._connecting.pop(serial, None)
            self._save()
            async_dispatcher_send(self.hass, SIGNAL_ANY)
        registry = dr.async_get(self.hass)
        if (device := registry.async_get_device(identifiers={(DOMAIN, serial)})) is not None:
            registry.async_remove_device(device.id)  # takes the entities with it

    def unblock_device(self, serial: str) -> None:
        if self.blocked.pop(serial, None) is None:
            raise ServiceValidationError("That device is not blocked")
        self._blocked_seen.pop(serial, None)
        self._save()
        async_dispatcher_send(self.hass, SIGNAL_ANY)

    async def _local_ips(self) -> list[str]:
        """This host's private IPv4 addresses, the default adapter's first."""
        ips: list[str] = []
        try:
            from homeassistant.components import network  # noqa: PLC0415 - optional at import time

            adapters = sorted(await network.async_get_adapters(self.hass), key=lambda a: not a.get("default"))
            ips = [ip["address"] for a in adapters for ip in a.get("ipv4", []) if isinstance(ip.get("address"), str)]
            source = await network.async_get_source_ip(self.hass)
            if isinstance(source, str):
                ips.append(source)
        except Exception:  # noqa: BLE001 - the check then reports that it cannot tell
            _LOGGER.debug("Could not list this host's addresses", exc_info=True)
        return [ip for ip in dict.fromkeys(ips) if is_lan_ipv4(ip)]

    async def async_dns_check(self) -> dict[str, Any]:
        """Does ``mqtt.us.petlibro.com`` resolve (from Home Assistant's resolver) to this host?"""
        local = await self._local_ips()
        try:
            resolved, error = await asyncio.wait_for(self.hass.async_add_executor_job(resolve_ipv4, CLOUD_HOST), 8)
        except (asyncio.TimeoutError, TimeoutError):
            resolved, error = [], "the lookup timed out"
        status = classify_dns(resolved, local)
        return {"host": CLOUD_HOST, "status": status, "resolved": resolved, "local_ips": local, "error": error,
                "port": self.broker.port}

    def devices_admin(self) -> dict[str, Any]:
        """What only the Devices tab needs: refused serials, devices that are connecting, the port."""
        return {
            "blocked": [{
                "id": serial, "serial_masked": mask_serial(serial), "model": b.get("model"), "name": b.get("name", ""),
                "ts": int(b["ts"] * 1000) if b.get("ts") else None,
                "attempt_ts": int(seen[1] * 1000) if (seen := self._blocked_seen.get(serial)) else None,
            } for serial, b in self.blocked.items()],
            "connecting": [{
                "serial_masked": mask_serial(serial), "ip": c["ip"], "ts": int(c["ts"] * 1000), "model": c["model"],
            } for serial, c in self._connecting.items()],
            "port": self.broker.port, "cloud_host": CLOUD_HOST,
        }

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
                "calibration": self.usable_cal(serial),
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
        if admin:  # the Devices tab
            out.update(serial_masked=mask_serial(serial), ip=dev.ip, firmware=d.get("software_version"),
                       hardware=d.get("hardware_version"))
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
            water = self.water_info(serial)
            days = alertlib.filter_days_left(d, now)
            out.update(
                pump_on=d.get("pump_on"), charging=alertlib.charging(d), plugged_in=alertlib.plugged_in(d),
                battery_state=d.get("battery_state"), water_weight=d.get("water_weight"),
                water_low=water["low"], water_threshold=water["threshold"], water_threshold_unit=water["threshold_unit"],
                water_threshold_source=water["threshold_source"], calibrated=water["calibrated"],
                calibration=self.calibration.get(serial), water_level_pct=water["level_pct"],
                water_ml=water["remaining_ml"], water_used_ml=water["used_today_ml"],
                water_avg_daily_ml=water["avg_daily_ml"], water_days_left=water["days_left"],
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
            "blocked": len(self.blocked),
            "devices": {
                s: {"model": d.model, "ip": d.ip, "online": s in self._online, "data": dict(d.data),
                    "feeds_today": d.feeds_today, "plan": self.plans.get(s), "calibration": self.calibration.get(s),
                    "consumption": self.consumption[s].to_dict() if s in self.consumption else None}
                for s, d in self.devices.items()
            },
        }
