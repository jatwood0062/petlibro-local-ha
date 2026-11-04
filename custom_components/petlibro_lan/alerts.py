"""Alert engine: settings, derived conditions and transition logic; no Home Assistant imports.

``evaluate`` is called on every device update and on a one-minute tick. It looks at a snapshot of
the devices, compares each condition with what it saw last time (``state``, JSON friendly and
persisted by the caller) and returns the notifications to deliver as ``Action`` objects. Alerts fire
on transitions (and on re-reminders), never on every evaluation.

Assumptions (also in the README):
* Food low is the inverse of ``surplusGrain`` (true = food present).
* Water low is ``water_weight`` (grams, from WATER_CAP_EVENT) below a threshold: the user's own
  threshold if set, else the device's ``lowWater`` when that is a plausible gram value
  (100-5000), else 500 g. The device's unit for ``lowWater`` is not confirmed.
* The filter is due on ``filterNextReplacementTimestamp`` (epoch ms).
"""
from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
TARGET_RE = re.compile(r"^(?:service:[a-z0-9_]{1,128}|entity:notify\.[a-z0-9_]{1,128})$")
MAX_TARGETS = 20

ALERT_TYPES = ("food_low", "water_low", "filter_due", "offline", "feed_failed", "battery_low")
URGENT = frozenset({"feed_failed", "food_low", "water_low"})  # not held back by quiet hours

DEFAULT_WATER_LOW_G = 500
WATER_PLAUSIBLE = (100, 5000)  # grams a fountain's own low-water value must fall in to be trusted
WATER_HYSTERESIS = 1.05  # a low-water alert clears 5 % above its threshold
BATTERY_HYSTERESIS = 5  # percentage points
RESOLVE_DELAY = 120  # seconds a condition must stay clear before the alert is resolved
SUMMARY_MAX_LATE = 6 * 3600

DEFAULT_SETTINGS: dict[str, Any] = {
    "targets": [],  # "service:mobile_app_x" (notify.mobile_app_x) or "entity:notify.something"
    "persistent": True,
    "quiet": {"enabled": False, "start": "22:00", "end": "07:00"},
    "alerts": {
        "food_low": {"enabled": True, "remind_hours": 12},
        "water_low": {"enabled": True, "threshold_g": None, "remind_hours": 12},
        "filter_due": {"enabled": True, "days_before": 3, "remind_hour": 9},
        "offline": {"enabled": True, "minutes": 30, "remind_hours": 24},
        "feed_failed": {"enabled": True},
        "battery_low": {"enabled": True, "percent": 20, "remind_hours": 24},
        "daily_summary": {"enabled": False, "time": "20:00"},
    },
}

_INT_RANGES = {  # (alert, key): (min, max)
    ("food_low", "remind_hours"): (0, 168), ("water_low", "remind_hours"): (0, 168),
    ("offline", "remind_hours"): (0, 168), ("battery_low", "remind_hours"): (0, 168),
    ("filter_due", "days_before"): (0, 60), ("filter_due", "remind_hour"): (0, 23),
    ("offline", "minutes"): (5, 1440), ("battery_low", "percent"): (1, 99),
}


def _int(value: Any, lo: int, hi: int, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value) or not lo <= value <= hi:
        raise ValueError(f"{what} must be a whole number from {lo} to {hi}")
    return int(value)


def _hhmm(value: Any, what: str) -> str:
    if not isinstance(value, str) or not HHMM.match(value):
        raise ValueError(f"{what} must be HH:MM")
    return value


def normalize_settings(raw: Any) -> dict[str, Any]:
    """Defaults merged with ``raw``, validated and clamped; ValueError when something is invalid."""
    out = copy.deepcopy(DEFAULT_SETTINGS)
    if raw is None:
        return out
    if not isinstance(raw, dict):
        raise ValueError("settings must be an object")
    if "targets" in raw:
        targets = raw["targets"]
        if (not isinstance(targets, list) or len(targets) > MAX_TARGETS
                or any(not isinstance(t, str) or not TARGET_RE.match(t) for t in targets)):
            raise ValueError("targets must be a list of notify services or entities")
        out["targets"] = list(dict.fromkeys(targets))
    if "persistent" in raw:
        if not isinstance(raw["persistent"], bool):
            raise ValueError("persistent must be true or false")
        out["persistent"] = raw["persistent"]
    if isinstance(raw.get("quiet"), dict):
        q = raw["quiet"]
        if "enabled" in q:
            if not isinstance(q["enabled"], bool):
                raise ValueError("quiet.enabled must be true or false")
            out["quiet"]["enabled"] = q["enabled"]
        for key in ("start", "end"):
            if key in q:
                out["quiet"][key] = _hhmm(q[key], f"quiet {key}")
    elif "quiet" in raw:
        raise ValueError("quiet must be an object")
    alerts = raw.get("alerts", {})
    if not isinstance(alerts, dict):
        raise ValueError("alerts must be an object")
    for name, cfg in alerts.items():
        if name not in out["alerts"]:
            continue
        if not isinstance(cfg, dict):
            raise ValueError(f"alerts.{name} must be an object")
        dest = out["alerts"][name]
        if "enabled" in cfg:
            if not isinstance(cfg["enabled"], bool):
                raise ValueError(f"{name}.enabled must be true or false")
            dest["enabled"] = cfg["enabled"]
        for key, (lo, hi) in ((k, v) for (a, k), v in _INT_RANGES.items() if a == name):
            if key in cfg:
                dest[key] = _int(cfg[key], lo, hi, f"{name} {key}")
        if name == "water_low" and "threshold_g" in cfg:
            val = cfg["threshold_g"]
            if val is None:
                dest["threshold_g"] = None
            elif isinstance(val, bool) or not isinstance(val, (int, float)) or not 50 <= val <= 20000:
                raise ValueError("water threshold must be 50-20000 grams, or empty for the device's own")
            else:
                dest["threshold_g"] = round(float(val), 1)
        if name == "daily_summary" and "time" in cfg:
            dest["time"] = _hhmm(cfg["time"], "summary time")
    return out


# --- derived values (shared with the entities and the app) -----------------------------
def food_low(data: dict[str, Any]) -> bool | None:
    return None if "food_present" not in data else not data["food_present"]


def device_water_threshold(data: dict[str, Any]) -> float | None:
    """The device's own low-water value if it looks like grams, else None."""
    val = data.get("low_water")
    if isinstance(val, (int, float)) and not isinstance(val, bool) and WATER_PLAUSIBLE[0] <= val <= WATER_PLAUSIBLE[1]:
        return float(val)
    return None


def water_threshold(data: dict[str, Any], override: float | None = None) -> tuple[float, str]:
    """(grams, source) with source "user", "device" or "default"."""
    if override is not None:
        return float(override), "user"
    if (own := device_water_threshold(data)) is not None:
        return own, "device"
    return float(DEFAULT_WATER_LOW_G), "default"


def water_low(data: dict[str, Any], override: float | None = None) -> bool | None:
    weight = data.get("water_weight")
    if not isinstance(weight, (int, float)) or isinstance(weight, bool):
        return None
    return weight < water_threshold(data, override)[0]


def filter_days_left(data: dict[str, Any], now: float) -> float | None:
    due = data.get("filter_due_ms")
    if not isinstance(due, (int, float)) or isinstance(due, bool):
        return None
    return (due / 1000 - now) / 86400


def charging(data: dict[str, Any]) -> bool | None:
    state = data.get("battery_state")
    return None if not isinstance(state, str) else state.upper() == "CHARGING"


def plugged_in(data: dict[str, Any]) -> bool | None:
    state = data.get("battery_state")
    return None if not isinstance(state, str) else state.upper() in ("CHARGING", "CHARGED")


def in_quiet(quiet: dict[str, Any], local_now: datetime) -> bool:
    if not quiet.get("enabled"):
        return False
    start, end = (int(quiet[k][:2]) * 60 + int(quiet[k][3:]) for k in ("start", "end"))
    cur = local_now.hour * 60 + local_now.minute
    if start == end:
        return False
    return start <= cur < end if start < end else cur >= start or cur < end


# --- conditions ---------------------------------------------------------------------------
@dataclass
class Cond:
    bad: bool
    title: str
    message: str
    resolved: str = ""
    phase: str = ""


Device = dict[str, Any]
# device snapshot: serial, name, kind, online, offline_since (epoch s or None), battery (bool: has a battery),
# data (flat state), feeds_today, next_feed ((epoch ms, portions) or None)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _cond_food(dev: Device, cfg: dict, st: dict | None, now: float, local: datetime) -> Cond | None:
    if dev["kind"] != "feeder" or not dev["online"] or (low := food_low(dev["data"])) is None:
        return None
    n = dev["name"]
    return Cond(low, f"{n}: food low", f"{n} is running low on food. Refill the hopper.", f"{n} has food again.")


def _cond_water(dev: Device, cfg: dict, st: dict | None, now: float, local: datetime) -> Cond | None:
    data = dev["data"]
    if dev["kind"] != "fountain" or not dev["online"] or not isinstance(data.get("water_weight"), (int, float)):
        return None
    limit, _src = water_threshold(data, cfg.get("threshold_g"))
    weight = data["water_weight"]
    bad = weight < limit * (WATER_HYSTERESIS if st and st["active"] else 1)
    n = dev["name"]
    return Cond(bad, f"{n}: water low",
                f"{n} water is low ({weight:.0f} g, alert below {limit:.0f} g). Refill the reservoir.",
                f"{n} water is back to {weight:.0f} g.")


def _cond_filter(dev: Device, cfg: dict, st: dict | None, now: float, local: datetime) -> Cond | None:
    if dev["kind"] != "fountain" or (days := filter_days_left(dev["data"], now)) is None:
        return None
    n = dev["name"]
    resolved = f"{n} filter was reset; next replacement in {_plural(max(0, round(days)), 'day')}."
    if days <= 0:
        late = int(math.floor(-days))
        when = "due today" if late == 0 else f"overdue by {_plural(late, 'day')}"
        return Cond(True, f"{n}: filter overdue", f"{n} filter is {when}. Replace it, then press Reset filter in the Pets app.",
                    resolved, "overdue")
    left = math.ceil(days)
    when_due = datetime.fromtimestamp(dev["data"]["filter_due_ms"] / 1000, tz=local.tzinfo)
    due = f"{when_due:%b} {when_due.day}"
    return Cond(days <= cfg["days_before"], f"{n}: filter due soon",
                f"{n} filter is due in {_plural(left, 'day')} ({due}). Order a replacement.", resolved, "soon")


def _cond_offline(dev: Device, cfg: dict, st: dict | None, now: float, local: datetime) -> Cond | None:
    n = dev["name"]
    if dev["online"]:
        return Cond(False, f"{n}: offline", "", f"{n} is back online.")
    since = dev.get("offline_since")
    if since is None:
        return None
    minutes = int((now - since) // 60)
    return Cond(now - since >= cfg["minutes"] * 60, f"{n}: offline",
                f"{n} has been offline for {minutes} minutes. Check its power and Wi-Fi.", f"{n} is back online.")


def _cond_feed(dev: Device, cfg: dict, st: dict | None, now: float, local: datetime) -> Cond | None:
    if dev["kind"] != "feeder" or not dev["online"] or (blocked := dev["data"].get("feed_blocked")) is None:
        return None
    n = dev["name"]
    return Cond(bool(blocked), f"{n}: feed failed",
                f"{n} dispensed fewer portions than requested or reported a blocked dispenser. Check for a jam.",
                f"{n} is feeding normally again.")


def _cond_battery(dev: Device, cfg: dict, st: dict | None, now: float, local: datetime) -> Cond | None:
    data = dev["data"]
    pct = data.get("battery")
    if not dev.get("battery") or not dev["online"] or not isinstance(pct, (int, float)):
        return None
    n = dev["name"]
    limit = cfg["percent"] + (BATTERY_HYSTERESIS if st and st["active"] else 0)
    bad = pct < limit and not charging(data)
    return Cond(bad, f"{n}: battery at {pct:.0f}%", f"{n} battery is at {pct:.0f}%. Charge it soon.",
                f"{n} battery is at {pct:.0f}%.")


CONDITIONS: dict[str, Callable[..., Cond | None]] = {
    "food_low": _cond_food, "water_low": _cond_water, "filter_due": _cond_filter,
    "offline": _cond_offline, "feed_failed": _cond_feed, "battery_low": _cond_battery,
}


# --- the engine ---------------------------------------------------------------------------
@dataclass
class Action:
    """One thing to do: push to the notify targets, create/dismiss the persistent notification, fire the event."""

    type: str
    serial: str | None
    device: str
    title: str
    message: str
    state: str  # "active", "resolved" or "summary"
    tag: str
    push: bool = False
    persist: bool = False
    dismiss: bool = False
    event: bool = False
    reminder: bool = False


def tag_for(alert: str, serial: str | None) -> str:
    return f"petlibro_lan_{alert}" + (f"_{serial}" if serial else "")


def _remind_due(alert: str, cfg: dict, phase: str, st: dict, now: float, local: datetime) -> bool:
    last = st.get("last_notified")
    if last is None:
        return False
    if alert == "filter_due":
        if phase != "overdue":
            return False
        was = datetime.fromtimestamp(last, tz=local.tzinfo).date()
        return was < local.date() and local.hour >= cfg["remind_hour"]
    hours = cfg.get("remind_hours", 0)
    return hours > 0 and now - last >= hours * 3600


def evaluate(settings: dict[str, Any], state: dict[str, Any], devices: list[Device], now: float,
             local: datetime) -> list[Action]:
    """Compare every enabled condition with ``state`` (updated in place) and return what to deliver."""
    actions: list[Action] = []
    book: dict[str, dict] = state.setdefault("alerts", {})
    quiet = in_quiet(settings["quiet"], local)
    persistent = settings["persistent"]
    for dev in devices:
        serial = dev["serial"]
        for alert in ALERT_TYPES:
            cfg = settings["alerts"][alert]
            key = f"{alert}:{serial}"
            st = book.get(key)
            tag = tag_for(alert, serial)
            if not cfg["enabled"]:
                if st:  # switched off while active: quietly clear it
                    del book[key]
                    actions.append(Action(alert, serial, dev["name"], "", "", "resolved", tag, dismiss=True))
                continue
            cond = CONDITIONS[alert](dev, cfg, st, now, local)
            if cond is None:
                continue
            urgent = alert in URGENT
            hold = quiet and not urgent
            if cond.bad:
                reminder = False
                if st is None or not st["active"]:
                    st = book[key] = {"active": True, "phase": cond.phase, "since": now, "pending": True,
                                      "last_notified": None, "clear_since": None}
                    new = True
                elif st["phase"] != cond.phase:  # soon -> overdue is a new alert
                    st.update(phase=cond.phase, since=now, pending=True, last_notified=None)
                    new = True
                else:
                    new = False
                    if not st["pending"] and _remind_due(alert, cfg, cond.phase, st, now, local):
                        st["pending"], reminder = True, True
                st["clear_since"] = None
                st["title"], st["message"] = cond.title, cond.message
                if st["pending"] and (new or not hold):
                    actions.append(Action(alert, serial, dev["name"], cond.title, cond.message, "active", tag,
                                          push=not hold, persist=persistent, event=new, reminder=reminder))
                    if not hold:
                        st["pending"], st["last_notified"] = False, now
            elif st and st["active"]:
                if st.get("clear_since") is None:
                    st["clear_since"] = now
                if now - st["clear_since"] >= RESOLVE_DELAY:
                    pushed = st.get("last_notified") is not None
                    del book[key]
                    actions.append(Action(alert, serial, dev["name"], cond.title, cond.resolved, "resolved", tag,
                                          push=pushed and not quiet, dismiss=True, event=True))
    actions += _summary(settings, state, devices, now, local, quiet)
    return actions


def _summary(settings: dict, state: dict, devices: list[Device], now: float, local: datetime, quiet: bool) -> list[Action]:
    cfg = settings["alerts"]["daily_summary"]
    if not cfg["enabled"]:
        state.pop("summary", None)
        return []
    at = local.replace(hour=int(cfg["time"][:2]), minute=int(cfg["time"][3:]), second=0, microsecond=0)
    today = local.date().isoformat()
    ss = state.get("summary")
    if not isinstance(ss, dict):  # just enabled: do not send today's summary late
        ss = state["summary"] = {"date": today, "sent": local >= at}
    elif ss.get("date") != today:
        ss.update(date=today, sent=False)
    if ss["sent"] or local < at:
        return []
    if local - at > timedelta(seconds=SUMMARY_MAX_LATE):
        ss["sent"] = True
        return []
    if quiet:
        return []
    ss["sent"] = True
    return [Action("daily_summary", None, "PetLibro", "Pets daily summary", summary_text(devices, now, local, settings),
                   "summary", tag_for("daily_summary", None), push=True, event=True)]


def summary_text(devices: list[Device], now: float, local: datetime, settings: dict[str, Any]) -> str:
    lines = []
    override = settings["alerts"]["water_low"].get("threshold_g")
    for dev in devices:
        d, n = dev["data"], dev["name"]
        if not dev["online"]:
            lines.append(f"{n}: offline")
        elif dev["kind"] == "feeder":
            food = {True: "food low", False: "food OK", None: "food unknown"}[food_low(d)]
            part = f"{n}: {_plural(dev.get('feeds_today', 0), 'feed')} today, {food}"
            if nxt := dev.get("next_feed"):
                at = datetime.fromtimestamp(nxt[0] / 1000, tz=local.tzinfo).strftime("%H:%M")
                part += f", next {at} ({_plural(nxt[1], 'portion')})"
            lines.append(part)
        else:
            bits = []
            if isinstance(d.get("water_weight"), (int, float)):
                bits.append(f"water {d['water_weight']:.0f} g" + (" (low)" if water_low(d, override) else ""))
            if (days := filter_days_left(d, now)) is not None:
                bits.append("filter overdue" if days <= 0 else f"filter in {_plural(math.ceil(days), 'day')}")
            if dev.get("battery") and isinstance(d.get("battery"), (int, float)):
                bits.append(f"battery {d['battery']:.0f}%")
            lines.append(f"{n}: " + (", ".join(bits) or "no data yet"))
    return "\n".join(lines) or "No PetLibro devices yet."


def active_alerts(state: dict[str, Any], names: dict[str, str]) -> list[dict[str, Any]]:
    """The alerts currently active, for the app."""
    out = []
    for key, st in (state.get("alerts") or {}).items():
        alert, _, serial = key.partition(":")
        if st.get("active"):
            out.append({"type": alert, "serial": serial, "device": names.get(serial, serial[-4:]),
                        "since": st.get("since"), "phase": st.get("phase", ""), "title": st.get("title", ""),
                        "message": st.get("message", "")})
    return sorted(out, key=lambda a: a["since"] or 0)
