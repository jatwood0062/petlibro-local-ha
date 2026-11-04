"""Water-level calibration math, consumption/refill detection, stored-data loading, DNS classification."""
from __future__ import annotations

from datetime import date

import pytest

from helpers import load

cal = load("calibration")
hosts = load("hosts")
models = load("models")
protocol = load("protocol")

D1, D2, D3, D4, D5 = (date(2026, 11, d) for d in range(1, 6))
GOOD = {"empty_g": 400.0, "full_g": 2400.0}


# --- level math ---
def test_level_percent_between_empty_and_full_and_clamped():
    assert cal.level_percent(400, GOOD) == 0
    assert cal.level_percent(1400, GOOD) == 50
    assert cal.level_percent(2400, GOOD) == 100
    assert cal.level_percent(1660, GOOD) == 63.0  # 1260 / 2000
    assert cal.level_percent(0, GOOD) == 0  # reservoir lifted off: below empty clamps
    assert cal.level_percent(3000, GOOD) == 100  # overfilled clamps
    assert cal.remaining_ml(1660, GOOD) == 1260
    assert cal.remaining_ml(100, GOOD) == 0


def test_validation_rules():
    assert cal.usable(GOOD) and not cal.usable({"empty_g": 400.0}) and not cal.usable(None)
    assert not cal.usable({"empty_g": 400.0, "full_g": 600.0})
    cal.check_span(400, 700)  # exactly 300 g apart is fine
    with pytest.raises(ValueError, match="at least 300"):
        cal.check_span(400, 699)
    with pytest.raises(ValueError):
        cal.set_both(1655, 1800, 1.0)
    with pytest.raises(ValueError):
        cal.set_both(-1, 2000, 1.0)
    with pytest.raises(ValueError):
        cal.set_both(0, 30000, 1.0)
    with pytest.raises(ValueError):
        cal.set_both(True, 2000, 1.0)
    with pytest.raises(ValueError):
        cal.set_both(float("nan"), 2000, 1.0)
    out = cal.set_both(0, 2400.04, 5.0)
    assert out == {"empty_g": 0.0, "full_g": 2400.0, "empty_ts": 5.0, "full_ts": 5.0}


def test_two_step_capture_flow():
    step1, dropped = cal.set_empty(None, 12.34, 100.0)
    assert step1 == {"empty_g": 12.3, "empty_ts": 100.0} and not dropped and not cal.usable(step1)
    with pytest.raises(ValueError, match="empty reading first"):
        cal.set_full(None, 2400, 1.0)
    with pytest.raises(ValueError, match="at least 300"):
        cal.set_full(step1, 200, 1.0)  # a full reading too close to empty is refused and changes nothing
    step2 = cal.set_full(step1, 2400, 200.0)
    assert cal.usable(step2) and step2["full_ts"] == 200.0 and step2["empty_ts"] == 100.0
    # a new empty reading that no longer leaves room for the old full one drops it
    again, dropped = cal.set_empty(step2, 2300, 300.0)
    assert dropped and "full_g" not in again and "full_ts" not in again
    keep, dropped = cal.set_empty(step2, 50, 300.0)
    assert not dropped and keep["full_g"] == 2400.0


def test_normalize_stored_calibration():
    assert cal.normalize(None) is None and cal.normalize("x") is None and cal.normalize({}) is None
    assert cal.normalize({"empty_g": 400, "full_g": 2400, "empty_ts": 5, "full_ts": 6, "junk": 1}) == {
        "empty_g": 400.0, "full_g": 2400.0, "empty_ts": 5.0, "full_ts": 6.0}
    assert cal.normalize({"empty_g": 400, "full_g": 500}) == {"empty_g": 400.0}  # span too small: full dropped
    assert cal.normalize({"empty_g": "x", "full_g": True}) is None
    assert cal.normalize({"empty_g": 99999}) is None
    assert cal.normalize({"full_g": 2000, "empty_ts": 5}) == {"full_g": 2000.0}  # a timestamp needs its weight


def test_days_left():
    assert cal.days_left(1200, 200) == 6.0
    assert cal.days_left(None, 200) is None and cal.days_left(1200, None) is None
    assert cal.days_left(1200, 2) is None  # nothing drunk: no estimate


# --- consumption ---
def used(weights, tracker=None, today=D1):
    t = tracker or cal.Consumption()
    t.update(weights, today)
    return t


def test_drinking_is_counted_jitter_is_not():
    t = used([2000, 2000, 2003, 1998, 2002])  # noise inside the dead band
    assert t.used_today(D1) == 0
    t.update([1980], D1)  # 20 g below the reference
    assert t.used_today(D1) == 20
    t.update([1975, 1979], D1)  # -5 and +4 against the new reference: still jitter
    assert t.used_today(D1) == 20
    t.update([1960], D1)
    assert t.used_today(D1) == 40


def test_refill_jump_is_not_negative_use_and_resets_the_reference():
    t = used([800, 790])  # 10 g used
    assert t.used_today(D1) == 10
    assert t.update([2400], D1) is True  # refill: +1610
    assert t.used_today(D1) == 10
    t.update([2390], D1)
    assert t.used_today(D1) == 20  # counting continues from the refilled weight


def test_refill_boundary_150g():
    t = used([1000])
    t.update([1149], D1)  # +149: a top-up, not a refill (and nothing used)
    t.update([1000], D1)  # then it drains again: 149 g used
    assert t.used_today(D1) == 149
    t2 = used([1000])
    t2.update([1150], D1)  # +150 is a refill
    t2.update([1140], D1)
    assert t2.used_today(D1) == 10


def test_lifting_the_reservoir_off_is_not_drinking():
    t = used([1655, 1650])  # 5 g jitter
    t.update([0], D1)  # lifted off: weight 0
    assert t.used_today(D1) == 0
    t.update([0, 0], D1)
    t.update([1655], D1)  # put back
    assert t.used_today(D1) == 0
    t.update([1630], D1)
    assert t.used_today(D1) == 25


def test_invalid_readings_are_skipped():
    t = used([1000, float("nan"), -5, None, "x", True, 980])
    assert t.used_today(D1) == 20


def test_day_rollover_keeps_history_and_resets_today():
    t = used([2000, 1800], today=D1)  # 200 g, 2 readings
    t.update([1700], D2)  # new day
    assert t.used_today(D2) == 100 and t.used_today(D1) == 0
    assert t.history["2026-11-01"] == {"used_g": 200.0, "samples": 2}
    assert t.average_daily(D2) is None  # that day had too few readings to trust


def test_average_daily_uses_last_three_complete_days_with_enough_readings():
    t = cal.Consumption()
    w = 4000
    for day, drink in ((D1, 100), (D2, 150), (D3, 200), (D4, 250)):
        readings = [w - 0.0]
        for _ in range(cal.MIN_DAY_SAMPLES):
            readings.append(w)
        w -= drink
        readings.append(w)
        t.update(readings, day)
    assert t.average_daily(D5) == pytest.approx((150 + 200 + 250) / 3, abs=0.5)  # D1 falls out of the 3-day window
    assert t.average_daily(D4) == pytest.approx((100 + 150 + 200) / 3, abs=0.5)  # today (D4) is not part of its own average
    assert cal.days_left(1200, t.average_daily(D5)) == pytest.approx(1200 / 200, abs=0.1)


def test_history_is_pruned_to_a_week():
    t = cal.Consumption()
    for i in range(12):
        t.update([1000] * 30, date(2026, 11, 1 + i))
    assert len(t.history) == cal.HISTORY_DAYS


def test_consumption_roundtrip_and_garbage():
    t = used([2000, 1900], today=D1)
    t.update([1800], D2)
    back = cal.Consumption.from_dict(t.to_dict())
    assert back.to_dict() == t.to_dict() and back.used_today(D2) == 100
    for junk in (None, "x", [], {"day": "bad", "history": 5}, {"history": {"2026-11-01": {"used_g": "x"}, "bad": {}}},
                 {"day": "2026-11-01", "used_g": -5, "samples": "z", "ref": float("nan")}):
        out = cal.Consumption.from_dict(junk)
        assert out.used_today(D1) >= 0 and out.history == {} and out.ref is None


def test_save_hint():
    t = cal.Consumption()
    assert t.update([1000], D1) is True  # first reading of a day: worth saving
    assert t.update([999], D1) is False
    assert t.update([1000] * 3, D1) is False
    assert t.update([900], D1) is True  # 100 g used


# --- stored data ---
def test_stores_from_an_older_version_load_empty():
    assert cal.load_calibrations(None) == {} and cal.load_consumption(None) == {} and cal.load_blocked(None) == {}
    assert cal.load_calibrations("x") == {} and cal.load_blocked([]) == {}


def test_load_filters_garbage():
    cals = cal.load_calibrations({"WF01ABCD": GOOD, "bad serial": GOOD, "WF02ABCD": {"empty_g": "x"}, "WF03ABCD": 5})
    assert cals == {"WF01ABCD": GOOD}
    blocked = cal.load_blocked({"AF01ABCD": {"model": "PLAF103", "name": "x" * 100, "ts": 5},
                                "nope": {}, "WF01ABCD": "junk", "AF02ABCD": {"model": 5, "ts": "z"}})
    assert set(blocked) == {"AF01ABCD", "WF01ABCD", "AF02ABCD"}
    assert blocked["AF01ABCD"] == {"model": "PLAF103", "name": "x" * 60, "ts": 5.0}
    assert blocked["WF01ABCD"] == {"model": None, "name": "", "ts": 0.0}
    cons = cal.load_consumption({"WF01ABCD": {"day": "2026-11-01", "used_g": 5, "samples": 3}, "x y": {}})
    assert list(cons) == ["WF01ABCD"] and cons["WF01ABCD"].used_today(D1) == 5


def test_purge_serial_removes_everything_stored_about_a_device_but_not_the_block_list():
    saved = {"devices": {"WF01ABCD": {"model": "PLWF116"}, "AF01ABCD": {"model": "PLAF103"}},
             "plans": {"AF01ABCD": [{"time": "07:00"}]}, "calibration": {"WF01ABCD": GOOD},
             "consumption": {"WF01ABCD": {}}, "blocked": {"WF01ABCD": {}}}
    out = cal.purge_serial(saved, "WF01ABCD")
    assert out["devices"] == {"AF01ABCD": {"model": "PLAF103"}} and out["calibration"] == {} and out["consumption"] == {}
    assert out["plans"] == saved["plans"] and out["blocked"] == {"WF01ABCD": {}}
    assert "WF01ABCD" in saved["devices"]  # the input is untouched
    assert cal.purge_serial({}, "x") == {}


def test_clean_name():
    assert cal.clean_name("  Kitchen   fountain ") == "Kitchen fountain"
    assert cal.clean_name("   ") == ""  # back to the default name
    with pytest.raises(ValueError):
        cal.clean_name("x" * 61)
    with pytest.raises(ValueError):
        cal.clean_name(5)


def test_mask_serial_shows_only_the_last_four():
    assert models.mask_serial("AF01ABCDEF1234") == "**********1234"
    assert models.mask_serial("abc") == "abc"


# --- water readings from the protocol ---
def test_water_weights_from_a_water_cap_event():
    msg = {"cmd": "WATER_CAP_EVENT", "content": [
        {"weightPercent": 50, "weight": 1655, "radarDetected": False}, {"weightPercent": 50, "weight": 1650.5},
        {"weight": "x"}, {"weightPercent": 50}, "junk", {"weight": True}]}
    assert protocol.water_weights(msg) == [1655.0, 1650.5]
    assert protocol.water_weights({"cmd": "WATER_CAP_EVENT", "weight": 1800}) == [1800.0]
    assert protocol.water_weights({"cmd": "WATER_CAP_EVENT", "content": []}) == []
    assert protocol.water_weights({"cmd": "WATER_CAP_EVENT", "content": "x"}) == []


# --- DNS classification ---
def test_classify_dns():
    here = ["192.168.4.10", "10.0.0.2"]
    assert hosts.classify_dns(["192.168.4.10"], here) == "ok"
    assert hosts.classify_dns(["34.1.2.3", "10.0.0.2"], here) == "ok"
    assert hosts.classify_dns(["192.168.4.99"], here) == "other_lan"
    assert hosts.classify_dns(["34.1.2.3"], here) == "public"
    assert hosts.classify_dns(["127.0.0.1"], here) == "public"  # loopback is not a LAN address a device can reach
    assert hosts.classify_dns([], here) == "unresolved"
    assert hosts.classify_dns(["192.168.4.10"], []) == "other_lan"  # own addresses unknown: cannot confirm


def test_resolve_ipv4_localhost():
    ips, err = hosts.resolve_ipv4("localhost")
    assert err is None and ips == ["127.0.0.1"]
