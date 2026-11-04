"""Alert engine: transitions, reminders, quiet hours, derived conditions, settings."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from helpers import load

alerts = load("alerts")
CHI = ZoneInfo("America/Chicago")
T0 = datetime(2026, 11, 3, 14, 0, tzinfo=CHI)  # Tuesday afternoon


def ts(dt: datetime) -> float:
    return dt.timestamp()


def feeder(**data):
    return {"serial": "AF1", "name": "Granary", "kind": "feeder", "online": True, "offline_since": None,
            "battery": False, "data": data, "feeds_today": 2, "next_feed": None}


def fountain(**data):
    return {"serial": "WF1", "name": "Dockstream", "kind": "fountain", "online": True, "offline_since": None,
            "battery": True, "data": data, "feeds_today": 0, "next_feed": None}


def settings(**over):
    s = alerts.normalize_settings(None)
    s["targets"] = ["service:mobile_app_jake"]
    for key, val in over.items():
        if key in s["alerts"]:
            s["alerts"][key].update(val)
        else:
            s[key] = val
    return s


class Run:
    """Evaluates repeatedly at advancing times, collecting actions."""

    def __init__(self, cfg=None, start=T0):
        self.cfg, self.state, self.now = cfg or settings(), {}, start

    def go(self, devices, minutes=0.0):
        self.now += timedelta(minutes=minutes)
        return alerts.evaluate(self.cfg, self.state, devices, ts(self.now), self.now)


def kinds(actions):
    return [(a.type, a.state, a.push, a.persist, a.dismiss, a.event) for a in actions]


# --- food low ---
def test_food_low_fires_once_then_reminds_and_resolves():
    run = Run()
    assert run.go([feeder(food_present=True)]) == []
    first = run.go([feeder(food_present=False)], 1)
    assert kinds(first) == [("food_low", "active", True, True, False, True)]
    assert "Granary" in first[0].title and "Refill" in first[0].message
    assert first[0].tag == "petlibro_lan_food_low_AF1"
    assert run.go([feeder(food_present=False)], 60) == []  # no repeat inside the cooldown
    again = run.go([feeder(food_present=False)], 11 * 60)  # 12 h 1 min after the first
    assert kinds(again) == [("food_low", "active", True, True, False, False)] and again[0].reminder
    assert run.go([feeder(food_present=True)], 1) == []  # needs two minutes of clear first
    done = run.go([feeder(food_present=True)], 3)
    assert kinds(done) == [("food_low", "resolved", True, False, True, True)]
    assert run.go([feeder(food_present=True)], 5) == []
    assert run.state["alerts"] == {}


def test_flapping_back_before_resolve_delay_does_not_notify():
    run = Run()
    run.go([feeder(food_present=False)])
    assert run.go([feeder(food_present=True)], 1) == []
    assert run.go([feeder(food_present=False)], 1) == []  # still the same alert, no second message


def test_unknown_data_changes_nothing():
    run = Run()
    run.go([feeder(food_present=False)])
    assert run.go([feeder()], 30) == []
    assert run.state["alerts"]["food_low:AF1"]["active"]


def test_disabled_alert_is_silent_and_dismisses():
    run = Run()
    run.go([feeder(food_present=False)])
    run.cfg["alerts"]["food_low"]["enabled"] = False
    out = run.go([feeder(food_present=False)], 1)
    assert [(a.dismiss, a.push, a.event) for a in out] == [(True, False, False)]
    assert run.go([feeder(food_present=False)], 1) == []


def test_remind_hours_zero_never_reminds():
    run = Run(settings(food_low={"remind_hours": 0}))
    run.go([feeder(food_present=False)])
    assert run.go([feeder(food_present=False)], 24 * 60) == []


# --- quiet hours ---
def test_in_quiet_wraps_midnight():
    q = {"enabled": True, "start": "22:00", "end": "07:00"}
    at = lambda h, m=0: datetime(2026, 11, 3, h, m, tzinfo=CHI)
    assert alerts.in_quiet(q, at(23)) and alerts.in_quiet(q, at(3)) and alerts.in_quiet(q, at(22))
    assert not alerts.in_quiet(q, at(7)) and not alerts.in_quiet(q, at(12))
    assert not alerts.in_quiet({**q, "enabled": False}, at(23))
    assert not alerts.in_quiet({**q, "start": "08:00", "end": "08:00"}, at(8))
    day = {"enabled": True, "start": "09:00", "end": "17:00"}
    assert alerts.in_quiet(day, at(12)) and not alerts.in_quiet(day, at(18))


def test_quiet_hours_delay_non_urgent_but_not_urgent():
    cfg = settings(quiet={"enabled": True, "start": "22:00", "end": "07:00"})
    run = Run(cfg, datetime(2026, 11, 3, 23, 0, tzinfo=CHI))
    low_battery = fountain(battery=10, battery_state="CHARGED")
    held = run.go([low_battery])
    # persistent notification and event right away, push held back
    assert kinds(held) == [("battery_low", "active", False, True, False, True)]
    assert run.go([low_battery], 120) == []  # still quiet at 01:00
    released = run.go([low_battery], 6 * 60 + 5)  # 07:05
    assert kinds(released) == [("battery_low", "active", True, True, False, False)]
    # urgent alerts go through at night
    run2 = Run(cfg, datetime(2026, 11, 3, 23, 0, tzinfo=CHI))
    assert kinds(run2.go([feeder(feed_blocked=True)])) == [("feed_failed", "active", True, True, False, True)]


def test_resolution_during_quiet_hours_is_not_pushed_but_dismissed():
    cfg = settings(quiet={"enabled": True, "start": "22:00", "end": "07:00"})
    run = Run(cfg, datetime(2026, 11, 3, 21, 0, tzinfo=CHI))
    run.go([fountain(battery=10)])
    run.now = datetime(2026, 11, 3, 22, 30, tzinfo=CHI)
    run.go([fountain(battery=50)])
    out = run.go([fountain(battery=50)], 3)
    assert kinds(out) == [("battery_low", "resolved", False, False, True, True)]


# --- water low ---
def test_water_threshold_sources():
    assert alerts.water_threshold({"low_water": 1649}) == (1649.0, "device")
    assert alerts.water_threshold({"low_water": 1649}, 900) == (900.0, "user")
    assert alerts.water_threshold({}) == (500.0, "default")
    assert alerts.water_threshold({"low_water": 3}) == (500.0, "default")  # implausible as grams
    assert alerts.water_threshold({"low_water": True}) == (500.0, "default")
    assert alerts.water_threshold({"low_water": 99999}) == (500.0, "default")


def test_water_low_derivation():
    assert alerts.water_low({"water_weight": 1000, "low_water": 1649}) is True
    assert alerts.water_low({"water_weight": 2285, "low_water": 1649}) is False
    assert alerts.water_low({"water_weight": 2285, "low_water": 1649}, 2500) is True
    assert alerts.water_low({"low_water": 1649}) is None
    assert alerts.water_low({"water_weight": 400}) is True  # default 500 g
    assert alerts.water_low({"water_weight": 0, "low_water": 1649}) is True


def test_water_low_alert_with_hysteresis():
    run = Run(settings(water_low={"threshold_g": 1000}))
    assert run.go([fountain(water_weight=1500)]) == []
    assert kinds(run.go([fountain(water_weight=900)], 1))[0][:2] == ("water_low", "active")
    assert run.go([fountain(water_weight=1020)], 5) == []  # above the threshold but inside the 5 % band
    assert run.state["alerts"]["water_low:WF1"]["clear_since"] is None
    run.go([fountain(water_weight=1100)], 1)
    out = run.go([fountain(water_weight=1100)], 3)
    assert kinds(out)[0][:2] == ("water_low", "resolved")


# --- filter ---
def test_filter_days_math():
    now = ts(T0)
    due = int((now + 2.5 * 86400) * 1000)
    assert alerts.filter_days_left({"filter_due_ms": due}, now) == pytest.approx(2.5)
    assert alerts.filter_days_left({"filter_due_ms": due - 5 * 86400_000}, now) == pytest.approx(-2.5)
    assert alerts.filter_days_left({}, now) is None


def test_filter_due_soon_then_overdue_then_daily_at_nine():
    run = Run(start=datetime(2026, 11, 3, 14, 0, tzinfo=CHI))
    due = int(ts(run.now + timedelta(days=2, hours=1)) * 1000)
    out = run.go([fountain(filter_due_ms=due)])
    assert kinds(out) == [("filter_due", "active", True, True, False, True)]
    assert "due in 3 days" in out[0].message or "due in 2 days" in out[0].message
    assert run.go([fountain(filter_due_ms=due)], 60 * 24) == []  # "soon" is only sent once
    out = run.go([fountain(filter_due_ms=due)], 60 * 24 + 65)  # Thu 15:05, just past due: a new alert
    assert kinds(out) == [("filter_due", "active", True, True, False, True)] and "overdue" in out[0].title
    # same day: nothing more; next day before 09:00: nothing; at 09:00: one reminder
    run.now = datetime(2026, 11, 5, 20, 0, tzinfo=CHI)
    assert run.go([fountain(filter_due_ms=due)]) == []
    run.now = datetime(2026, 11, 6, 8, 59, tzinfo=CHI)
    assert run.go([fountain(filter_due_ms=due)]) == []
    out = run.go([fountain(filter_due_ms=due)], 2)
    assert kinds(out) == [("filter_due", "active", True, True, False, False)] and out[0].reminder
    assert run.go([fountain(filter_due_ms=due)], 30) == []


def test_filter_reset_resolves_with_message():
    run = Run()
    old = int(ts(run.now - timedelta(days=30)) * 1000)
    assert run.go([fountain(filter_due_ms=old)])
    new = int(ts(run.now + timedelta(days=30)) * 1000)
    run.go([fountain(filter_due_ms=new)], 1)
    out = run.go([fountain(filter_due_ms=new)], 3)
    assert kinds(out) == [("filter_due", "resolved", True, False, True, True)]
    assert "reset" in out[0].message


def test_filter_notify_days_before_setting():
    run = Run(settings(filter_due={"days_before": 7}))
    due = int(ts(run.now + timedelta(days=6)) * 1000)
    assert run.go([fountain(filter_due_ms=due)])
    run2 = Run(settings(filter_due={"days_before": 3}))
    assert run2.go([fountain(filter_due_ms=due)]) == []


# --- battery ---
def test_battery_threshold_hysteresis_and_charging():
    run = Run(settings(battery_low={"percent": 20}))
    assert run.go([fountain(battery=21)]) == []
    assert kinds(run.go([fountain(battery=19)], 1))[0][:2] == ("battery_low", "active")
    run.go([fountain(battery=23)], 5)  # below 25: still active
    assert run.state["alerts"]["battery_low:WF1"]["clear_since"] is None
    run.go([fountain(battery=26)], 1)
    assert kinds(run.go([fountain(battery=26)], 3))[0][:2] == ("battery_low", "resolved")
    run2 = Run()
    assert run2.go([fountain(battery=5, battery_state="CHARGING")]) == []  # being charged: no alert


def test_battery_alert_ignores_devices_without_a_battery():
    dev = feeder(battery=0)  # mains Granary reports electricQuantity 0
    assert Run().go([dev]) == []


# --- offline ---
def test_offline_after_n_minutes_and_back_online():
    run = Run(settings(offline={"minutes": 30}))
    gone = ts(run.now)
    dev = lambda online: {**feeder(), "online": online, "offline_since": None if online else gone}
    assert run.go([dev(False)], 10) == []
    out = run.go([dev(False)], 25)
    assert kinds(out) == [("offline", "active", True, True, False, True)]
    assert "35 minutes" in out[0].message
    assert run.go([dev(False)], 60) == []
    assert run.go([dev(True)], 1) == []
    out = run.go([dev(True)], 3)
    assert kinds(out) == [("offline", "resolved", True, False, True, True)] and "back online" in out[0].message


def test_offline_device_does_not_raise_other_alerts():
    dev = {**fountain(battery=3, water_weight=1), "online": False, "offline_since": ts(T0)}
    assert Run().go([dev], 1) == []


# --- feed failed ---
def test_feed_failed_transitions():
    run = Run()
    assert run.go([feeder(feed_blocked=False)]) == []
    out = run.go([feeder(feed_blocked=True)], 1)
    assert kinds(out) == [("feed_failed", "active", True, True, False, True)] and "jam" in out[0].message
    assert run.go([feeder(feed_blocked=True)], 24 * 60) == []  # no re-reminders
    run.go([feeder(feed_blocked=False)], 1)
    assert kinds(run.go([feeder(feed_blocked=False)], 3))[0][:2] == ("feed_failed", "resolved")


# --- persistent toggle, daily summary ---
def test_persistent_flag_is_carried():
    run = Run(settings(persistent=False))
    out = run.go([feeder(food_present=False)])
    assert out[0].persist is False and out[0].push is True


def test_daily_summary_once_per_day():
    cfg = settings(daily_summary={"enabled": True, "time": "20:00"})
    run = Run(cfg, datetime(2026, 11, 3, 19, 0, tzinfo=CHI))
    devs = [feeder(food_present=True), fountain(water_weight=2285, battery=80, low_water=1649)]
    assert run.go(devs) == []
    out = run.go(devs, 61)
    assert len(out) == 1 and out[0].state == "summary" and out[0].push and not out[0].persist
    assert "2 feeds today" in out[0].message and "water 2285 g" in out[0].message and "battery 80%" in out[0].message
    assert run.go(devs, 30) == []
    run.now = datetime(2026, 11, 4, 20, 5, tzinfo=CHI)
    assert len(run.go(devs)) == 1


def test_daily_summary_enabled_after_its_time_waits_for_tomorrow():
    cfg = settings(daily_summary={"enabled": True, "time": "08:00"})
    run = Run(cfg, datetime(2026, 11, 3, 14, 0, tzinfo=CHI))
    assert run.go([feeder()]) == []
    assert run.go([feeder()], 60) == []


def test_daily_summary_held_by_quiet_hours_then_sent():
    cfg = settings(daily_summary={"enabled": True, "time": "22:30"},
                   quiet={"enabled": True, "start": "22:00", "end": "23:00"})
    run = Run(cfg, datetime(2026, 11, 3, 21, 0, tzinfo=CHI))
    run.go([feeder()])
    assert run.go([feeder()], 100) == []  # 22:40, quiet
    assert len(run.go([feeder()], 25)) == 1  # 23:05


# --- settings ---
def test_normalize_defaults_and_roundtrip():
    s = alerts.normalize_settings(None)
    assert s["persistent"] is True and s["alerts"]["offline"]["minutes"] == 30
    assert s["alerts"]["battery_low"]["percent"] == 20 and s["alerts"]["filter_due"]["days_before"] == 3
    assert alerts.normalize_settings(copy.deepcopy(s)) == s
    out = alerts.normalize_settings({"targets": ["service:a", "service:a", "entity:notify.b"],
                                     "alerts": {"water_low": {"threshold_g": 1200}, "unknown": {"x": 1}}})
    assert out["targets"] == ["service:a", "entity:notify.b"] and out["alerts"]["water_low"]["threshold_g"] == 1200.0
    assert alerts.normalize_settings({"alerts": {"water_low": {"threshold_g": None}}})["alerts"]["water_low"]["threshold_g"] is None


@pytest.mark.parametrize("bad", [
    [], {"targets": ["notify.x"]}, {"targets": "service:x"}, {"targets": ["service:Bad Name"]},
    {"persistent": 1}, {"quiet": {"start": "25:00"}}, {"quiet": 5}, {"alerts": {"offline": {"minutes": 1}}},
    {"alerts": {"offline": {"minutes": True}}}, {"alerts": {"battery_low": {"percent": 100}}},
    {"alerts": {"water_low": {"threshold_g": 10}}}, {"alerts": {"water_low": {"threshold_g": "x"}}},
    {"alerts": {"food_low": {"enabled": "yes"}}}, {"alerts": {"daily_summary": {"time": "8:00"}}},
    {"alerts": {"filter_due": {"days_before": -1}}}, {"alerts": []},
])
def test_normalize_rejects(bad):
    with pytest.raises(ValueError):
        alerts.normalize_settings(bad)


def test_active_alerts_listing():
    run = Run()
    run.go([feeder(food_present=False)])
    out = alerts.active_alerts(run.state, {"AF1": "Granary"})
    assert [(a["type"], a["device"], a["serial"]) for a in out] == [("food_low", "Granary", "AF1")]
