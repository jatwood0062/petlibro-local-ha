"""Protocol tests: topics, acks, NTP, command builders, parsers, state tracking."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from helpers import load

protocol = load("protocol")
state = load("state")
models = load("models")
hosts = load("hosts")
CHICAGO = ZoneInfo("America/Chicago")
NOW = 1_790_000_000_000


def test_topics_roundtrip_and_rejects():
    topic = protocol.build_topic("PLAF103", "AF01ABC123", "event", "post")
    assert topic == "dl/PLAF103/AF01ABC123/device/event/post"
    t = protocol.parse_topic(topic)
    assert (t.model, t.serial, t.channel, t.direction) == ("PLAF103", "AF01ABC123", "event", "post")
    for bad in ("", "dl/PLAF103/AF01ABC123/device/event", "x/PLAF103/AF01ABC123/device/event/post",
                "dl/plaf103/AF01ABC123/device/event/post", "dl/PLAF103/bad/device/event/post",
                "dl/PLAF103/AF01ABC123/device/other/post", "dl/PLAF103/AF01ABC123/device/event/up",
                "dl/PLAF103/AF01ABC123/device/event/post/x"):
        assert protocol.parse_topic(bad) is None


def test_serials_and_models():
    assert models.valid_serial("AF01ABCDEF") and models.valid_serial("WF03-xyz_1")
    assert not models.valid_serial("../etc") and not models.valid_serial("AF") and not models.valid_serial(5)
    assert models.model_info("PLWF305").kind == "fountain"
    assert models.model_info("PLAF999", "AF99XYZ").max_portions == 0
    assert models.model_info("PLXX999", "ZZ1234") is None


def test_lan_addresses():
    assert hosts.is_lan_ipv4("192.168.4.20") and hosts.is_lan_ipv4("10.1.2.3") and hosts.is_lan_ipv4("172.20.0.1")
    for bad in ("8.8.8.8", "127.0.0.1", "0.0.0.0", "224.0.0.1", "100.64.0.1", "172.32.0.1", "::1", "", None, "1.2.3"):
        assert not hosts.is_lan_ipv4(bad)


def test_msg_id_and_timestamp():
    assert re.fullmatch(r"[0-9a-f]{32}", protocol.new_msg_id())
    assert protocol.new_msg_id() != protocol.new_msg_id()
    assert 1e12 < protocol.now_ms() < 1e14


def test_decode():
    assert protocol.decode(b'{"cmd":"X"}') == {"cmd": "X"}
    for bad in (b"", b"\xff", b"[]", b'{"cmd":1}', b"{}", b"null"):
        assert protocol.decode(bad) is None


# --- acks, one per event type ---
@pytest.mark.parametrize("cmd", ["DEVICE_START_EVENT", "ATTR_PUSH_EVENT", "DEVICE_LOG_REPORT_EVENT",
                                 "DETECTION_EVENT", "WAREHOUSE_DOOR_EVENT", "WATER_CAP_EVENT", "ERROR_EVENT"])
def test_generic_event_ack(cmd):
    (channel, reply), = protocol.respond("event", {"cmd": cmd, "msgId": "abc", "success": True}, NOW, CHICAGO)
    assert channel == "event"
    assert reply == {"cmd": cmd, "ts": NOW, "msgId": "abc", "code": 0}


def test_grain_output_ack_echoes_exec_step():
    for step in ("GRAIN_START", "GRAIN_END"):
        (_, reply), = protocol.respond("event", {"cmd": "GRAIN_OUTPUT_EVENT", "msgId": "m", "execStep": step}, NOW, CHICAGO)
        assert reply["execStep"] == step and reply["msgId"] == "m"
    (_, reply), = protocol.respond("event", {"cmd": "WET_GRAIN_OUTPUT_EVENT", "msgId": "m", "execStep": "OPEN_DOOR"}, NOW, CHICAGO)
    assert reply["execStep"] == "OPEN_DOOR"


def test_no_reply_cases():
    r = protocol.respond
    assert r("heart", {"cmd": "HEARTBEAT", "rssi": -50}, NOW, CHICAGO) == []
    assert r("service", {"cmd": "MANUAL_FEEDING_SERVICE", "msgId": "x", "code": 0}, NOW, CHICAGO) == []
    assert r("event", {"cmd": "ATTR_PUSH_EVENT"}, NOW, CHICAGO) == []  # nothing to echo
    assert r("event", {"cmd": "ATTR_PUSH_EVENT", "msgId": "x", "linkId": "y"}, NOW, CHICAGO) == []
    assert r("ntp", {"cmd": "NTP_SYNC", "msgId": "x", "code": 0}, NOW, CHICAGO) == []
    assert r("event", {"cmd": "ATTR_PUSH_EVENT", "msgId": 5}, NOW, CHICAGO) == []


def test_plan_request_without_stored_plan_never_clears():
    (_, reply), = protocol.respond("event", {"cmd": "GET_FEEDING_PLAN_EVENT", "msgId": "p"}, NOW, CHICAGO, None)
    assert reply == {"cmd": "GET_FEEDING_PLAN_EVENT", "ts": NOW, "msgId": "p", "code": 0}
    assert "plans" not in reply


def test_plan_request_with_stored_plan_and_explicit_clear():
    plans = [{"planId": 1, "executionTime": "13:00", "repeatDay": [1], "grainNum": 2}]
    (_, reply), = protocol.respond("event", {"cmd": "GET_FEEDING_PLAN_EVENT", "msgId": "p"}, NOW, CHICAGO, plans)
    assert reply["plans"] == plans and reply["msgId"] == "p"
    (_, reply), = protocol.respond("event", {"cmd": "GET_FEEDING_PLAN_EVENT", "msgId": "p"}, NOW, CHICAGO, [])
    assert reply["plans"] == []


def test_binding_reply():
    (channel, reply), = protocol.respond("system", {"cmd": "BINDING", "msgId": "b"}, NOW, CHICAGO)
    assert channel == "system" and reply == {"cmd": "BINDING", "msgId": "b", "code": 0}


def test_ntp_reply_fixed_zone_and_dst():
    utc = protocol.ntp_reply(NOW, timezone.utc)
    assert utc["cmd"] == "NTP" and utc["code"] == 0 and utc["ts"] == NOW and utc["timezone"] == 0
    assert "nextDSTTransitionTs" not in utc
    # 2026-11-03 in Chicago: CST (-6), next change is 2027-03-14 at 02:00 CST = 08:00Z, then 2027-11-07
    now = int(datetime(2026, 11, 3, 12, tzinfo=timezone.utc).timestamp() * 1000)
    reply = protocol.ntp_reply(now, CHICAGO)
    assert reply["timezone"] == -6 and reply["timezoneOffsetSeconds"] == -21600
    assert reply["nextDSTOffsetSeconds"] == -18000
    assert reply["nextDSTTransitionTs"] == int(datetime(2027, 3, 14, 8, tzinfo=timezone.utc).timestamp() * 1000)
    assert reply["secondNextDSTOffsetSeconds"] == -21600
    assert reply["secondNextDSTTransitionTs"] == int(datetime(2027, 11, 7, 7, tzinfo=timezone.utc).timestamp() * 1000)


# --- commands ---
def test_command_envelope():
    msg = protocol.manual_feed(2)
    assert msg["cmd"] == "MANUAL_FEEDING_SERVICE" and msg["grainNum"] == 2
    assert re.fullmatch(r"[0-9a-f]{32}", msg["msgId"]) and isinstance(msg["ts"], int) and msg["ts"] > 1e12
    assert json.loads(protocol.encode(msg)) == msg


def test_manual_feed_bounds():
    protocol.manual_feed(12)
    protocol.manual_feed(5, 5)
    for bad in (0, 13, -1, 1.5, "2", True):
        with pytest.raises(ValueError):
            protocol.manual_feed(bad)
    with pytest.raises(ValueError):
        protocol.manual_feed(6, 5)


def test_attribute_commands():
    kw = {"now": NOW, "msg_id": "m"}
    assert protocol.indicator_light(True, **kw) == {
        "cmd": "ATTR_SET_SERVICE", "lightSwitch": True, "lightAgingType": 1, "filterLedSwitch": True, "ts": NOW, "msgId": "m"}
    assert protocol.button_lock(True, **kw)["disableHardwareButton"] is True
    assert protocol.button_lock(False, "childLockSwitch", **kw)["childLockSwitch"] is False
    assert protocol.sound(False, **kw)["soundSwitch"] is False
    assert protocol.attr_get(**kw) == {"cmd": "ATTR_GET_SERVICE", "ts": NOW, "msgId": "m"}


def test_fountain_commands():
    assert protocol.pump(True)["waterStopSwitch"] == 0  # inverted: 0 runs the pump
    assert protocol.pump(False)["waterStopSwitch"] == 1
    assert protocol.fountain_light(True)["lightSwitch"] == 1 and protocol.fountain_light(False)["lightSwitch"] == 0
    assert type(protocol.fountain_light(True)["lightSwitch"]) is int
    reset = protocol.filter_reset(30, now=NOW)
    assert reset["filterNextReplacementTimestamp"] == NOW + 30 * 86_400_000
    assert protocol.weight_clear(now=NOW)["cmd"] == "WEIGHT_CLR_SERVICE"


def test_plan_commands():
    plans = [{"planId": 1}]
    assert protocol.set_feeding_plan(plans)["plans"] == plans
    clear = protocol.clear_feeding_plan()
    assert clear["cmd"] == "FEEDING_PLAN_SERVICE" and clear["plans"] == []


# --- parsers ---
def test_parse_attrs_feeder_and_fountain():
    feeder = protocol.parse_message("event", {"cmd": "ATTR_PUSH_EVENT", "surplusGrain": False, "electricQuantity": 80,
                                              "powerType": 3, "lightSwitch": True, "disableHardwareButton": False}, NOW)
    assert feeder == {"food_present": False, "battery": 80, "power_type": 3, "light": True, "button_lock": False}
    fountain = protocol.parse_message("event", {"cmd": "ATTR_GET_SERVICE", "waterStopSwitch": 0, "lightSwitch": 1,
                                                "currentWeight": 1800, "lowWater": 500,
                                                "filterNextReplacementTimestamp": NOW, "batteryState": "CHARGING"}, NOW)
    assert fountain["pump_on"] is True and fountain["light"] is True and fountain["water_weight"] == 1800
    assert fountain["battery_state"] == "CHARGING" and fountain["filter_due_ms"] == NOW
    assert protocol.parse_message("event", {"cmd": "ATTR_PUSH_EVENT", "waterStopSwitch": 1}, NOW) == {"pump_on": False}
    assert protocol.parse_message("event", {"cmd": "ATTR_PUSH_EVENT", "electricQuantity": "x", "volume": True}, NOW) == {}


def test_parse_start_heartbeat_water_cap_error():
    start = protocol.parse_message("event", {"cmd": "DEVICE_START_EVENT", "hardwareVersion": "1.0", "softwareVersion": "2.0.1",
                                             "mac": "AA:BB", "restartReason": "other:0x0", "pid": "PLAF103"}, NOW)
    assert start == {"hardware_version": "1.0", "software_version": "2.0.1", "mac": "AA:BB", "restart_reason": "other:0x0"}
    assert protocol.parse_message("heart", {"cmd": "HEARTBEAT", "rssi": -48, "ts": NOW - 5000}, NOW) == {"rssi": -48, "device_ts": NOW - 5000}
    cap = {"cmd": "WATER_CAP_EVENT", "content": [{"weightPercent": 90, "weight": 2000}, {"weightPercent": 70, "weight": 2285}]}
    assert protocol.parse_message("event", cap, NOW) == {"water_weight": 2285, "water_percent": 70}
    assert protocol.parse_message("event", {"cmd": "WATER_CAP_EVENT", "content": "x"}, NOW) == {}
    assert protocol.parse_message("event", {"cmd": "ERROR_EVENT", "errorCode": 2032}, NOW) == {"error_code": 2032, "feed_blocked": True}
    assert protocol.parse_message("event", {"cmd": "SOMETHING_NEW", "x": 1}, NOW) == {}


def grain(step, actual, expect=2, msg_id="g1", **extra):
    return {"cmd": "GRAIN_OUTPUT_EVENT", "msgId": msg_id, "execStep": step, "finished": step == "GRAIN_END",
            "actualGrainNum": actual, "expectGrainNum": expect, **extra}


def test_state_merges_partial_pushes_and_counts_feeds_once():
    st = state.DeviceState("AF01ABC123", "PLAF103")
    day = date(2026, 11, 3)
    st.ingest("event", {"cmd": "ATTR_PUSH_EVENT", "electricQuantity": 90, "surplusGrain": True}, NOW, day)
    st.ingest("event", {"cmd": "ATTR_PUSH_EVENT", "surplusGrain": False}, NOW, day)
    assert st.data == {"battery": 90, "food_present": False}
    st.ingest("event", grain("GRAIN_START", 0), NOW, day)
    assert st.feeds_today == 0
    st.ingest("event", grain("GRAIN_END", 2, type=2), NOW, day)
    st.ingest("event", grain("GRAIN_END", 2, type=2), NOW + 1, day)  # retransmission
    assert st.feeds_today == 1
    assert st.data["last_feed_portions"] == 2 and st.data["feed_blocked"] is False and st.data["last_feed_ms"] == NOW
    st.ingest("event", grain("GRAIN_END", 1, msg_id="g2", type=1), NOW, day)
    assert st.feeds_today == 2 and st.data["feed_blocked"] is True and st.data["last_feed_scheduled"] is True
    st.ingest("event", grain("GRAIN_END", 2, msg_id="g3"), NOW, date(2026, 11, 4))
    assert st.feeds_today == 1  # new local day


def test_state_heartbeat_drift():
    st = state.DeviceState("AF01ABC123", "PLAF103")
    st.ingest("heart", {"cmd": "HEARTBEAT", "rssi": -60, "ts": NOW - 555_000}, NOW, date(2026, 1, 1))
    assert st.data["clock_drift_s"] == -555 and "device_ts" not in st.data


def test_non_finite_numbers_are_ignored():
    assert protocol.parse_attrs({"electricQuantity": float("inf"), "currentWeight": float("nan")}) == {}


def test_command_replies_are_never_acked_on_any_channel():
    # the real Granary (fw 1.1.1) answers ATTR_GET_SERVICE on event/post; acking it looped forever
    reply = {"cmd": "ATTR_GET_SERVICE", "msgId": "5d906ae4", "code": 0, "surplusGrain": True}
    for channel in ("event", "service"):
        assert protocol.respond(channel, reply, NOW, CHICAGO) == []
    assert protocol.respond("event", {"cmd": "MANUAL_FEEDING_SERVICE", "msgId": "x"}, NOW, CHICAGO) == []
    assert protocol.respond("event", {"cmd": "DEVICE_LOG_REPORT_EVENT", "msgId": "y", "logs": []}, NOW, CHICAGO)
