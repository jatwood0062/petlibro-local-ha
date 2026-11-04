"""Fountain water-level calibration, water consumption and stored-data helpers; no Home Assistant imports.

The scale under the reservoir reports grams. A calibration is two readings, ``empty_g`` (reservoir
empty or lifted off) and ``full_g`` (filled to the MAX line); the water level is the position of the
current weight between them. One gram of water is about one millilitre.
"""
from __future__ import annotations

import math
from datetime import date
from typing import Any

from .models import valid_serial

MIN_SPAN_G = 300  # full must be at least this much heavier than empty
MAX_G = 20000
REFILL_JUMP_G = 150  # a rise of this much between two readings is a refill, not a change in use
TOP_UP_G = 30  # smaller rises than this are scale jitter; between this and the refill jump they are top-ups
DEADBAND_G = 10  # drops smaller than this (against the last reference) are scale jitter
MAX_DROP_G = 300  # a bigger drop between two readings is the reservoir being lifted off, not drinking
HISTORY_DAYS = 7
MIN_DAY_SAMPLES = 20  # a day needs this many readings to count towards the average
MIN_DAILY_USE_G = 5  # below this average a "days left" estimate is meaningless
AVERAGE_DAYS = 3
NAME_MAX = 60


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


# --- calibration -------------------------------------------------------------------------
def normalize(raw: Any) -> dict[str, float] | None:
    """The valid fields of a stored calibration (empty_g, full_g, empty_ts, full_ts); None when nothing is usable."""
    if not isinstance(raw, dict):
        return None
    out: dict[str, float] = {}
    for key in ("empty_g", "full_g"):
        val = _num(raw.get(key))
        if val is not None and 0 <= val <= MAX_G:
            out[key] = round(val, 1)
    for key in ("empty_ts", "full_ts"):
        val = _num(raw.get(key))
        if val is not None and val > 0 and key[:-3] + "_g" in out:
            out[key] = round(val, 3)
    if "empty_g" in out and "full_g" in out and out["full_g"] < out["empty_g"] + MIN_SPAN_G:
        out.pop("full_g")
        out.pop("full_ts", None)
    return out or None


def usable(cal: dict[str, Any] | None) -> bool:
    """True when both readings are present and far enough apart to calibrate the level."""
    return bool(cal) and "empty_g" in cal and "full_g" in cal and cal["full_g"] >= cal["empty_g"] + MIN_SPAN_G


def _reading(grams: Any, what: str) -> float:
    val = _num(grams)
    if val is None or not 0 <= val <= MAX_G:
        raise ValueError(f"{what} must be a weight from 0 to {MAX_G} g")
    return round(val, 1)


def check_span(empty: float, full: float) -> None:
    if full < empty + MIN_SPAN_G:
        raise ValueError(
            f"Full ({full:.0f} g) must be at least {MIN_SPAN_G} g heavier than empty ({empty:.0f} g). "
            "Check that the reservoir is filled to the MAX line.")


def set_empty(cal: dict[str, float] | None, grams: Any, now: float) -> tuple[dict[str, float], bool]:
    """Record the empty reading; returns (calibration, dropped_full). A full reading that no longer fits is dropped."""
    empty = _reading(grams, "Empty")
    out = dict(cal or {})
    out.update(empty_g=empty, empty_ts=round(now, 3))
    dropped = False
    if "full_g" in out and out["full_g"] < empty + MIN_SPAN_G:
        out.pop("full_g")
        out.pop("full_ts", None)
        dropped = True
    return out, dropped


def set_full(cal: dict[str, float] | None, grams: Any, now: float) -> dict[str, float]:
    """Record the full reading; the empty one must exist and be at least MIN_SPAN_G lighter."""
    full = _reading(grams, "Full")
    if not cal or "empty_g" not in cal:
        raise ValueError("Capture the empty reading first")
    check_span(cal["empty_g"], full)
    out = dict(cal)
    out.update(full_g=full, full_ts=round(now, 3))
    return out


def set_both(empty: Any, full: Any, now: float) -> dict[str, float]:
    e, f = _reading(empty, "Empty"), _reading(full, "Full")
    check_span(e, f)
    return {"empty_g": e, "full_g": f, "empty_ts": round(now, 3), "full_ts": round(now, 3)}


def level_percent(weight: float, cal: dict[str, Any]) -> float:
    """Water level 0-100 % between the empty and full readings (clamped, one decimal)."""
    span = cal["full_g"] - cal["empty_g"]
    return round(min(100.0, max(0.0, (weight - cal["empty_g"]) / span * 100)), 1)


def remaining_ml(weight: float, cal: dict[str, Any]) -> float:
    """Water above the empty reading; 1 g of water is about 1 mL."""
    return round(max(0.0, weight - cal["empty_g"]))


def days_left(remaining: float | None, daily: float | None) -> float | None:
    if remaining is None or daily is None or daily < MIN_DAILY_USE_G:
        return None
    return round(remaining / daily, 1)


# --- consumption ---------------------------------------------------------------------------
class Consumption:
    """Water drunk per day, from the sequence of scale readings (no Home Assistant, no clock of its own).

    Each reading is compared with a reference weight. A drop of at least ``DEADBAND_G`` counts as water
    used and moves the reference. A rise of ``REFILL_JUMP_G`` or more is a refill and a drop of
    ``MAX_DROP_G`` or more is the reservoir being lifted off: both only move the reference. Rises
    between ``TOP_UP_G`` and the refill jump are top-ups (reference moves, nothing counted); smaller
    changes are scale jitter and are ignored.
    """

    def __init__(self) -> None:
        self.day: str | None = None
        self.used_g = 0.0
        self.samples = 0
        self.ref: float | None = None
        self.history: dict[str, dict[str, float]] = {}

    @classmethod
    def from_dict(cls, raw: Any) -> Consumption:
        out = cls()
        if not isinstance(raw, dict):
            return out
        if isinstance(raw.get("day"), str) and (_iso(raw["day"]) is not None):
            out.day = raw["day"]
            out.used_g = max(0.0, _num(raw.get("used_g")) or 0.0)
            out.samples = int(max(0.0, _num(raw.get("samples")) or 0))
        out.ref = _num(raw.get("ref"))
        hist = raw.get("history")
        if isinstance(hist, dict):
            for key, item in hist.items():
                if _iso(key) is not None and isinstance(item, dict):
                    used, n = _num(item.get("used_g")), _num(item.get("samples"))
                    if used is not None and n is not None and used >= 0 and n >= 0:
                        out.history[key] = {"used_g": round(used, 1), "samples": int(n)}
        out._prune()
        return out

    def to_dict(self) -> dict[str, Any]:
        return {"day": self.day, "used_g": round(self.used_g, 1), "samples": self.samples, "ref": self.ref,
                "history": self.history}

    def _prune(self) -> None:
        for key in sorted(self.history)[:-HISTORY_DAYS]:
            del self.history[key]

    def _roll(self, today: date) -> None:
        iso = today.isoformat()
        if self.day == iso:
            return
        if self.day is not None and self.samples:
            self.history[self.day] = {"used_g": round(self.used_g, 1), "samples": self.samples}
        self.day, self.used_g, self.samples = iso, 0.0, 0
        self._prune()

    def update(self, weights: list[float], today: date) -> bool:
        """Feed readings in order; True when something worth saving changed (a refill, a day change, +10 g used)."""
        before_day, before_used = self.day, self.used_g
        important = False
        self._roll(today)
        important = self.day != before_day
        for w in weights:
            w = _num(w)
            if w is None or w < 0:
                continue
            self.samples += 1
            if self.ref is None:
                self.ref = w
                continue
            delta = w - self.ref
            if delta >= REFILL_JUMP_G or delta <= -MAX_DROP_G:
                self.ref = w
                important = True
            elif delta <= -DEADBAND_G:
                self.used_g += -delta
                self.ref = w
            elif delta >= TOP_UP_G:
                self.ref = w
        return important or self.used_g - before_used >= 10

    def used_today(self, today: date) -> float:
        return round(self.used_g) if self.day == today.isoformat() else 0

    def average_daily(self, today: date, days: int = AVERAGE_DAYS) -> float | None:
        """Mean water used on the last ``days`` complete days that had enough readings."""
        iso = today.isoformat()
        known = dict(self.history)
        if self.day is not None and self.samples:  # the last tracked day, not rolled over yet
            known[self.day] = {"used_g": self.used_g, "samples": self.samples}
        usable_days = [known[k]["used_g"] for k in sorted(known) if k < iso and known[k]["samples"] >= MIN_DAY_SAMPLES]
        usable_days = usable_days[-days:]
        return round(sum(usable_days) / len(usable_days), 1) if usable_days else None


def _iso(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


# --- stored data (hub Store) ----------------------------------------------------------------
def load_calibrations(raw: Any) -> dict[str, dict[str, float]]:
    out = {}
    if isinstance(raw, dict):
        for serial, item in raw.items():
            if valid_serial(serial) and (cal := normalize(item)) is not None:
                out[serial] = cal
    return out


def load_consumption(raw: Any) -> dict[str, Consumption]:
    if not isinstance(raw, dict):
        return {}
    return {serial: Consumption.from_dict(item) for serial, item in raw.items() if valid_serial(serial)}


def load_blocked(raw: Any) -> dict[str, dict[str, Any]]:
    """{serial: {"model": str | None, "name": str, "ts": float}}; anything malformed is dropped."""
    out: dict[str, dict[str, Any]] = {}
    if isinstance(raw, dict):
        for serial, item in raw.items():
            if not valid_serial(serial):
                continue
            item = item if isinstance(item, dict) else {}
            model = item.get("model") if isinstance(item.get("model"), str) else None
            name = item.get("name") if isinstance(item.get("name"), str) else ""
            out[serial] = {"model": model, "name": name[:NAME_MAX], "ts": _num(item.get("ts")) or 0.0}
    return out


def purge_serial(saved: dict[str, Any], serial: str) -> dict[str, Any]:
    """The stored hub data without anything belonging to ``serial`` (not the block list)."""
    out = dict(saved)
    for key in ("devices", "plans", "calibration", "consumption"):
        if isinstance(out.get(key), dict):
            out[key] = {s: v for s, v in out[key].items() if s != serial}
    return out


def clean_name(value: Any) -> str:
    """A device name from the app: trimmed, at most NAME_MAX characters; ValueError when not text."""
    if not isinstance(value, str):
        raise ValueError("The name must be text")
    name = " ".join(value.split())
    if len(name) > NAME_MAX:
        raise ValueError(f"The name can be at most {NAME_MAX} characters")
    return name
