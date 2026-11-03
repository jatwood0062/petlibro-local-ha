"""Feeding plans: validation, local-time to UTC conversion and next-feed maths; no Home Assistant imports.

Plans are stored in local time. The device only understands UTC ``HH:MM`` and weekdays and does
not follow DST, so each stored entry is converted using the *next occurrence* of each weekday and
the result is compared with what was last sent to decide when to re-send.
"""
from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone, tzinfo
from typing import Any

MAX_PLANS = 24
ALL_DAYS = (1, 2, 3, 4, 5, 6, 7)
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def normalize(raw: object, max_portions: int) -> list[dict[str, Any]]:
    """Validate a list of {time, portions, days?} and return it in storage form; ValueError if bad."""
    if not isinstance(raw, list) or len(raw) > MAX_PLANS:
        raise ValueError(f"plan must be a list of at most {MAX_PLANS} entries")
    out = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each plan entry must be an object")
        text = item.get("time")
        if isinstance(text, time):
            text = text.strftime("%H:%M")
        if not isinstance(text, str) or not _TIME_RE.match(text[:5]) or len(text) not in (5, 8):
            raise ValueError(f"invalid time {text!r}, expected HH:MM")
        portions = item.get("portions")
        if isinstance(portions, bool) or not isinstance(portions, int) or not 1 <= portions <= max_portions:
            raise ValueError(f"portions must be 1-{max_portions}")
        days = item.get("days", list(ALL_DAYS))
        if (not isinstance(days, list) or not days
                or any(isinstance(d, bool) or not isinstance(d, int) or not 1 <= d <= 7 for d in days)):
            raise ValueError("days must be a list of 1 (Monday) to 7 (Sunday)")
        out.append({"time": text[:5], "portions": portions, "days": sorted(set(days))})
    return sorted(out, key=lambda p: (p["time"], p["portions"]))


def _occurrences(plans: list[dict[str, Any]], tz: tzinfo, now: datetime):
    """Yield (plan, weekday, local datetime of the next occurrence on that weekday)."""
    local_now = now.astimezone(tz)
    today = local_now.date()
    for plan in plans:
        hh, mm = int(plan["time"][:2]), int(plan["time"][3:5])
        for day in plan["days"]:
            date = today + timedelta(days=(day - 1 - today.weekday()) % 7)
            at = datetime.combine(date, time(hh, mm), tzinfo=tz)
            if at <= local_now:
                at += timedelta(days=7)
            yield plan, day, at


def to_wire(plans: list[dict[str, Any]], tz: tzinfo, now: datetime, sync_ms: int) -> list[dict[str, Any]]:
    """Device plans in UTC; weekdays move with the time when the UTC date differs from the local one."""
    groups: dict[tuple[str, int], set[int]] = {}
    for plan, _day, at in _occurrences(plans, tz, now):
        utc = at.astimezone(timezone.utc)
        groups.setdefault((utc.strftime("%H:%M"), plan["portions"]), set()).add(utc.isoweekday())
    return [
        {"planId": i, "executionTime": hhmm, "repeatDay": sorted(days), "enableAudio": False,
         "audioTimes": 0, "grainNum": portions, "syncTime": sync_ms}
        for i, ((hhmm, portions), days) in enumerate(sorted(groups.items()), 1)
    ]


def signature(wire: list[dict[str, Any]]) -> tuple:
    """What the device schedule means, ignoring ids and sync times."""
    return tuple(sorted((p["executionTime"], tuple(p["repeatDay"]), p["grainNum"]) for p in wire))


def next_feed(plans: list[dict[str, Any]], tz: tzinfo, now: datetime) -> tuple[datetime, int] | None:
    """(UTC time, portions) of the soonest planned feed, or None."""
    found = [(at, plan["portions"]) for plan, _d, at in _occurrences(plans, tz, now)]
    if not found:
        return None
    at, portions = min(found)
    return at.astimezone(timezone.utc), portions
