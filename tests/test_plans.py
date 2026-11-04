"""Feeding plan tests: validation, UTC conversion across DST, next feed."""
from __future__ import annotations

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import pytest

from helpers import load

plans = load("plans")
CHICAGO = ZoneInfo("America/Chicago")
LA = ZoneInfo("America/Los_Angeles")


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def test_normalize():
    out = plans.normalize([{"time": "18:00", "portions": 2}, {"time": time(7, 30), "portions": 1, "days": [5, 1, 1]}], 12)
    assert out == [{"time": "07:30", "portions": 1, "days": [1, 5]},
                   {"time": "18:00", "portions": 2, "days": [1, 2, 3, 4, 5, 6, 7]}]
    assert plans.normalize([], 12) == []


@pytest.mark.parametrize("bad", [
    "x", [1], [{"time": "7:30", "portions": 1}], [{"time": "24:00", "portions": 1}], [{"time": "07:30", "portions": 0}],
    [{"time": "07:30", "portions": 13}], [{"time": "07:30", "portions": True}], [{"time": "07:30", "portions": 1, "days": []}],
    [{"time": "07:30", "portions": 1, "days": [8]}], [{"time": "07:30", "portions": 1, "days": "1"}],
    [{"time": "07:30", "portions": 1}] * 25])
def test_normalize_rejects(bad):
    with pytest.raises(ValueError):
        plans.normalize(bad, 12)


def test_portion_limit_depends_on_model():
    with pytest.raises(ValueError):
        plans.normalize([{"time": "07:30", "portions": 6}], 5)


def test_wire_format_winter():
    stored = plans.normalize([{"time": "07:00", "portions": 2}], 12)
    wire = plans.to_wire(stored, CHICAGO, utc(2026, 1, 14, 12), 1234)  # CST, UTC-6
    assert wire == [{"planId": 1, "executionTime": "13:00", "repeatDay": [1, 2, 3, 4, 5, 6, 7], "enableAudio": False,
                     "audioTimes": 0, "grainNum": 2, "syncTime": 1234}]


def test_dst_changes_the_utc_time():
    stored = plans.normalize([{"time": "07:00", "portions": 2}], 12)
    winter = plans.to_wire(stored, CHICAGO, utc(2026, 3, 1, 12), 0)
    summer = plans.to_wire(stored, CHICAGO, utc(2026, 3, 10, 12), 0)
    assert winter[0]["executionTime"] == "13:00" and summer[0]["executionTime"] == "12:00"
    assert plans.signature(winter) != plans.signature(summer)


def test_week_straddling_the_transition_is_split():
    # Mon 2026-03-02: Sun 03-08 07:00 is already CDT (12:00Z); Mon-Sat are CST (13:00Z)
    stored = plans.normalize([{"time": "07:00", "portions": 2}], 12)
    wire = plans.to_wire(stored, CHICAGO, utc(2026, 3, 2, 12), 0)
    assert [(p["executionTime"], p["repeatDay"]) for p in wire] == [("12:00", [7]), ("13:00", [1, 2, 3, 4, 5, 6])]


def test_fall_back():
    stored = plans.normalize([{"time": "07:00", "portions": 1}], 12)
    before = plans.to_wire(stored, CHICAGO, utc(2026, 10, 28, 12), 0)  # Wed, CDT (UTC-5); Sun 11-01 07:00 is CST
    after = plans.to_wire(stored, CHICAGO, utc(2026, 11, 3, 12), 0)
    assert {p["executionTime"] for p in before} == {"12:00", "13:00"}
    assert [p["executionTime"] for p in after] == ["13:00"]


def test_weekday_moves_with_utc_date():
    # Pacific 20:00 is 04:00 UTC the next day; Monday local = Tuesday UTC, Sunday local = Monday UTC
    stored = plans.normalize([{"time": "20:00", "portions": 1, "days": [1, 7]}], 12)
    wire = plans.to_wire(stored, LA, utc(2026, 1, 14, 12), 0)
    assert wire[0]["executionTime"] == "04:00" and wire[0]["repeatDay"] == [1, 2]
    stored = plans.normalize([{"time": "05:00", "portions": 1, "days": [1]}], 12)  # 13:00Z same day
    assert plans.to_wire(stored, LA, utc(2026, 1, 14, 12), 0)[0]["repeatDay"] == [1]
    stored = plans.normalize([{"time": "20:00", "portions": 1, "days": [7]}], 12)
    assert plans.to_wire(stored, LA, utc(2026, 1, 14, 12), 0)[0]["repeatDay"] == [1]


def test_entries_with_same_time_and_portions_merge_and_others_stay_separate():
    stored = plans.normalize([{"time": "07:00", "portions": 1, "days": [1]}, {"time": "07:00", "portions": 1, "days": [3]},
                              {"time": "07:00", "portions": 2, "days": [5]}], 12)
    wire = plans.to_wire(stored, timezone.utc, utc(2026, 1, 14, 12), 0)
    assert [(p["planId"], p["grainNum"], p["repeatDay"]) for p in wire] == [(1, 1, [1, 3]), (2, 2, [5])]


def test_empty_plan_gives_empty_wire():
    assert plans.to_wire([], CHICAGO, utc(2026, 1, 14), 0) == []


def test_next_feed():
    stored = plans.normalize([{"time": "07:00", "portions": 2}, {"time": "18:00", "portions": 3}], 12)
    # 2026-01-14 14:00Z = 08:00 CST -> next is 18:00 local = 00:00Z next day
    assert plans.next_feed(stored, CHICAGO, utc(2026, 1, 14, 14)) == (utc(2026, 1, 15, 0), 3)
    assert plans.next_feed(stored, CHICAGO, utc(2026, 1, 15, 0, 30)) == (utc(2026, 1, 15, 13), 2)
    assert plans.next_feed([], CHICAGO, utc(2026, 1, 14)) is None
    weekdays = plans.normalize([{"time": "07:00", "portions": 1, "days": [1]}], 12)  # Mondays
    at, _ = plans.next_feed(weekdays, CHICAGO, utc(2026, 1, 14, 14))  # Wednesday
    assert at == utc(2026, 1, 19, 13)


# --- label / enabled (schedule editor) and the round trip ---
def test_label_and_enabled_roundtrip():
    raw = [{"time": "07:30", "portions": 2, "label": " Breakfast ", "days": [1, 2]},
           {"time": "18:00", "portions": 1, "enabled": False, "label": ""},
           {"time": "12:00", "portions": 1, "enabled": True}]
    out = plans.normalize(raw, 12)
    assert out == [{"time": "07:30", "portions": 2, "days": [1, 2], "label": "Breakfast"},
                   {"time": "12:00", "portions": 1, "days": [1, 2, 3, 4, 5, 6, 7]},
                   {"time": "18:00", "portions": 1, "days": [1, 2, 3, 4, 5, 6, 7], "enabled": False}]
    assert plans.normalize(out, 12) == out  # what the editor loads can be saved again unchanged


@pytest.mark.parametrize("bad", [
    {"label": 5}, {"label": "x" * 41}, {"label": "a\nb"}, {"enabled": "no"}, {"enabled": 1}])
def test_label_enabled_rejected(bad):
    with pytest.raises(ValueError):
        plans.normalize([{"time": "07:30", "portions": 1, **bad}], 12)


def test_disabled_meals_never_reach_the_device_or_next_feed():
    stored = plans.normalize([{"time": "07:00", "portions": 1}, {"time": "08:00", "portions": 2, "enabled": False}], 12)
    wire = plans.to_wire(stored, CHICAGO, utc(2026, 1, 14, 12), 0)
    assert [p["grainNum"] for p in wire] == [1]
    at, portions = plans.next_feed(stored, CHICAGO, utc(2026, 1, 14, 12))
    assert portions == 1
    off = plans.normalize([{"time": "07:00", "portions": 1, "enabled": False}], 12)
    assert plans.to_wire(off, CHICAGO, utc(2026, 1, 14), 0) == [] and plans.next_feed(off, CHICAGO, utc(2026, 1, 14)) is None


def test_upcoming_three():
    stored = plans.normalize([{"time": "07:00", "portions": 2, "label": "Breakfast"}, {"time": "18:00", "portions": 3}], 12)
    # 14:00Z = 08:00 CST: 18:00 today, 07:00 tomorrow, 18:00 tomorrow
    out = plans.upcoming(stored, CHICAGO, utc(2026, 1, 14, 14))
    assert out == [(utc(2026, 1, 15, 0), 3, ""), (utc(2026, 1, 15, 13), 2, "Breakfast"), (utc(2026, 1, 16, 0), 3, "")]
    assert plans.upcoming([], CHICAGO, utc(2026, 1, 14)) == []
    weekly = plans.normalize([{"time": "07:00", "portions": 1, "days": [1]}], 12)
    assert len(plans.upcoming(weekly, CHICAGO, utc(2026, 1, 14, 14))) == 1
