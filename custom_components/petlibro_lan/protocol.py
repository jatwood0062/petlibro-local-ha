"""PetLibro wire protocol as pure functions; no Home Assistant imports.

Topics look like ``dl/<MODEL>/<SERIAL>/device/<channel>/<post|sub>``: devices publish on ``post``
and listen on ``sub``. Messages are JSON with ``cmd``, ``ts`` (ms), ``msgId`` and ``code``.
"""
from __future__ import annotations

import json
import math
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import Any

from .models import valid_model, valid_serial

CHANNELS = ("event", "heart", "ntp", "service", "system")
MAX_TRANSITION_DAYS = 800


@dataclass(frozen=True)
class Topic:
    model: str
    serial: str
    channel: str
    direction: str


def parse_topic(topic: str) -> Topic | None:
    parts = topic.split("/")
    if len(parts) != 6 or parts[0] != "dl" or parts[3] != "device":
        return None
    _, model, serial, _, channel, direction = parts
    if not (valid_model(model) and valid_serial(serial)):
        return None
    if channel not in CHANNELS or direction not in ("post", "sub"):
        return None
    return Topic(model, serial, channel, direction)


def build_topic(model: str, serial: str, channel: str, direction: str = "sub") -> str:
    return f"dl/{model}/{serial}/device/{channel}/{direction}"


def now_ms() -> int:
    return int(time.time() * 1000)


def new_msg_id() -> str:
    return uuid.uuid4().hex


def encode(msg: dict[str, Any]) -> bytes:
    return json.dumps(msg, separators=(",", ":")).encode()


def decode(payload: bytes) -> dict[str, Any] | None:
    """A JSON object with a string ``cmd``, or None."""
    try:
        msg = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(msg, dict) or not isinstance(msg.get("cmd"), str):
        return None
    return msg


# --- replies the server owes the device ----------------------------------------------
def ack(msg: dict[str, Any], now: int) -> dict[str, Any]:
    """The generic reply to an event: echo cmd, msgId and execStep."""
    out: dict[str, Any] = {"cmd": msg["cmd"], "ts": now, "msgId": msg["msgId"], "code": 0}
    if "execStep" in msg:
        out["execStep"] = msg["execStep"]
    return out


def utc_offset_seconds(tz: tzinfo, at: datetime) -> int:
    return int((at.astimezone(tz).utcoffset() or timedelta(0)).total_seconds())


def next_transitions(tz: tzinfo, now: int, count: int = 2) -> list[tuple[int, int]]:
    """Next ``count`` UTC-offset changes as (epoch ms, new offset seconds); [] without DST."""
    out: list[tuple[int, int]] = []
    prev = datetime.fromtimestamp(now / 1000, tz=tz)
    prev_off = utc_offset_seconds(tz, prev)
    for day in range(1, MAX_TRANSITION_DAYS):
        cur = prev + timedelta(days=1)
        off = utc_offset_seconds(tz, cur)
        if off != prev_off:
            lo, hi = int(prev.timestamp()), int(cur.timestamp())
            while hi - lo > 1:
                mid = (lo + hi) // 2
                if utc_offset_seconds(tz, datetime.fromtimestamp(mid, tz=tz)) == prev_off:
                    lo = mid
                else:
                    hi = mid
            out.append((hi * 1000, off))
            if len(out) >= count:
                break
        prev, prev_off = cur, off
    return out


def ntp_reply(now: int, tz: tzinfo) -> dict[str, Any]:
    """Answer to a device's NTP request: server time, zone, and the next two DST changes."""
    offset = utc_offset_seconds(tz, datetime.fromtimestamp(now / 1000, tz=tz))
    out: dict[str, Any] = {
        "cmd": "NTP", "ts": now, "code": 0, "calibrationTag": True,
        "timezone": offset // 3600, "timezoneOffsetSeconds": offset,
    }
    for name, (ts, off) in zip(("next", "secondNext"), next_transitions(tz, now)):
        out[f"{name}DSTOffsetSeconds"] = off
        out[f"{name}DSTTransitionTs"] = ts
    return out


def plan_reply(msg: dict[str, Any], now: int, wire_plans: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Reply to GET_FEEDING_PLAN_EVENT.

    ``wire_plans`` None (no plan configured in Home Assistant): a plain ack with no ``plans`` key,
    which stops the device's retries and leaves the schedule stored on the device alone. A list
    (even empty, when the user explicitly cleared the plan) is sent as the authoritative schedule.
    """
    out = ack(msg, now) if "msgId" in msg else {"cmd": msg["cmd"], "ts": now, "code": 0}
    if wire_plans is not None:
        out["plans"] = wire_plans
    return out


def respond(
    channel: str,
    msg: dict[str, Any],
    now: int,
    tz: tzinfo,
    wire_plans: list[dict[str, Any]] | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """Replies owed for a message the device published: a list of (``sub`` channel, payload).

    Heartbeats and replies to our commands get nothing: a ``*_SERVICE`` cmd is a reply whichever
    channel it arrives on (the Granary answers ATTR_GET_SERVICE on ``event/post``), and echoing it
    makes the device run the command again, in a loop. Messages carrying ``linkId`` are cloud echoes.
    """
    cmd = msg.get("cmd")
    if "linkId" in msg or not isinstance(cmd, str) or is_reply(cmd):
        return []
    if channel == "ntp":
        return [("ntp", ntp_reply(now, tz))] if cmd == "NTP" else []
    if channel == "system":
        if cmd == "BINDING" and "msgId" in msg:
            return [("system", {"cmd": "BINDING", "msgId": msg["msgId"], "code": 0})]
        return []
    if channel != "event" or not isinstance(msg.get("msgId"), str):
        return []
    if cmd == "GET_FEEDING_PLAN_EVENT":
        return [("event", plan_reply(msg, now, wire_plans))]
    return [("event", ack(msg, now))]


def is_reply(cmd: object) -> bool:
    """True for the device's answer to one of our commands (never acknowledged)."""
    return isinstance(cmd, str) and cmd.endswith("_SERVICE")


# --- commands the server sends on service/sub ----------------------------------------
def command(cmd: str, fields: dict[str, Any] | None = None, *, now: int | None = None,
            msg_id: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"cmd": cmd, **(fields or {})}
    out["ts"] = now_ms() if now is None else now
    out["msgId"] = msg_id or new_msg_id()
    return out


def attr_get(**kw: Any) -> dict[str, Any]:
    return command("ATTR_GET_SERVICE", **kw)


def attr_set(fields: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return command("ATTR_SET_SERVICE", fields, **kw)


def manual_feed(portions: int, max_portions: int = 12, **kw: Any) -> dict[str, Any]:
    if not isinstance(portions, int) or isinstance(portions, bool) or not 1 <= portions <= max_portions:
        raise ValueError(f"portions must be 1-{max_portions}")
    return command("MANUAL_FEEDING_SERVICE", {"grainNum": portions}, **kw)


def set_feeding_plan(wire_plans: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
    """Replaces the device's whole schedule; an empty list clears it."""
    return command("FEEDING_PLAN_SERVICE", {"plans": wire_plans}, **kw)


def clear_feeding_plan(**kw: Any) -> dict[str, Any]:
    return set_feeding_plan([], **kw)


def indicator_light(on: bool, **kw: Any) -> dict[str, Any]:
    return attr_set({"lightSwitch": bool(on), "lightAgingType": 1, "filterLedSwitch": True}, **kw)


def button_lock(locked: bool, key: str = "disableHardwareButton", **kw: Any) -> dict[str, Any]:
    return attr_set({key: bool(locked), "filterLedSwitch": True}, **kw)


def sound(on: bool, **kw: Any) -> dict[str, Any]:
    return attr_set({"soundSwitch": bool(on), "soundAgingType": 1, "filterLedSwitch": True}, **kw)


def pump(on: bool, **kw: Any) -> dict[str, Any]:
    """Fountain pump; the wire value is inverted (0 runs the pump)."""
    return attr_set({"waterStopSwitch": 0 if on else 1}, **kw)


def fountain_light(on: bool, **kw: Any) -> dict[str, Any]:
    return attr_set({"lightSwitch": 1 if on else 0}, **kw)


def filter_reset(life_days: int, *, now: int | None = None, **kw: Any) -> dict[str, Any]:
    """Writes a new filter due date, ``life_days`` from now."""
    base = now_ms() if now is None else now
    return attr_set({"filterNextReplacementTimestamp": base + life_days * 86_400_000}, now=base, **kw)


def weight_clear(threshold: int = 550, **kw: Any) -> dict[str, Any]:
    return command("WEIGHT_CLR_SERVICE", {"threshold": threshold}, **kw)


# --- device messages -> flat state ---------------------------------------------------
def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return None


def _num(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value


_BOOLS = {
    "surplusGrain": "food_present", "lightSwitch": "light", "disableHardwareButton": "button_lock",
    "childLockSwitch": "child_lock", "soundSwitch": "sound", "filterLedSwitch": "filter_led",
}
_NUMS = {
    "electricQuantity": "battery", "powerType": "power_type", "currentWeight": "water_weight",
    "lowWater": "low_water", "filterNextReplacementTimestamp": "filter_due_ms", "volume": "volume",
}


def parse_attrs(msg: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, name in _BOOLS.items():
        if (val := _bool(msg.get(key))) is not None:
            out[name] = val
    for key, name in _NUMS.items():
        if (val := _num(msg.get(key))) is not None:
            out[name] = val
    if (val := _num(msg.get("waterStopSwitch"))) is not None:
        out["pump_on"] = val == 0
    if isinstance(msg.get("batteryState"), str):
        out["battery_state"] = msg["batteryState"]
    return out


def water_samples(msg: dict[str, Any]) -> list[dict[str, Any]]:
    """The sample dicts of a WATER_CAP_EVENT (oldest first); tolerates a flat message without the list."""
    content = msg.get("content")
    samples = [s for s in content if isinstance(s, dict)] if isinstance(content, list) else []
    if not samples and "weight" in msg:
        samples = [msg]
    return samples


def water_weights(msg: dict[str, Any]) -> list[float]:
    """Every valid scale reading (grams) in a WATER_CAP_EVENT, oldest first."""
    return [float(w) for s in water_samples(msg) if (w := _num(s.get("weight"))) is not None]


def parse_message(channel: str, msg: dict[str, Any], now: int) -> dict[str, Any]:
    """Flat updates from one device message; merge them, never replace the state.

    ``feed_completed`` marks a finished dispense (the state tracker counts it);
    ``feed_blocked`` is inferred from fewer portions dispensed than requested.
    """
    cmd = msg.get("cmd")
    out: dict[str, Any] = {}
    if cmd == "HEARTBEAT":
        if (val := _num(msg.get("rssi"))) is not None:
            out["rssi"] = val
        if (val := _num(msg.get("ts"))) is not None:
            out["device_ts"] = val
    elif cmd in ("ATTR_PUSH_EVENT", "ATTR_GET_SERVICE"):
        out.update(parse_attrs(msg))
    elif cmd == "DEVICE_START_EVENT":
        for key, name in (("hardwareVersion", "hardware_version"), ("softwareVersion", "software_version"),
                          ("mac", "mac"), ("restartReason", "restart_reason")):
            if isinstance(msg.get(key), str):
                out[name] = msg[key]
    elif cmd == "GRAIN_OUTPUT_EVENT":
        if msg.get("execStep") == "GRAIN_END" or msg.get("finished") is True:
            actual, expect = _num(msg.get("actualGrainNum")), _num(msg.get("expectGrainNum"))
            if actual:
                out["feed_completed"] = True
                out["last_feed_portions"] = int(actual)
                out["last_feed_ms"] = now
                out["last_feed_scheduled"] = msg.get("type") == 1
                if expect is not None:
                    out["feed_blocked"] = actual < expect
    elif cmd == "WATER_CAP_EVENT":
        if samples := water_samples(msg):
            last = samples[-1]
            if (val := _num(last.get("weight"))) is not None:
                out["water_weight"] = val
            if (val := _num(last.get("weightPercent"))) is not None:
                out["water_percent"] = val
            radar = [_bool(s.get("radarDetected")) for s in samples]
            if any(r is not None for r in radar):
                out["radar_now"] = bool(radar[-1])
                if any(radar):  # a pet was at the fountain during this report
                    out["radar_seen_ms"] = now
    elif cmd == "ERROR_EVENT" and (val := _num(msg.get("errorCode"))) is not None:
        out["error_code"] = int(val)
        if val == 2032:
            out["feed_blocked"] = True
    return out
