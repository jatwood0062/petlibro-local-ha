"""Broker tests: real sockets on localhost, plain asyncio (no pytest-asyncio needed)."""
from __future__ import annotations

import asyncio
import time

from helpers import load

broker = load("broker")
SERIAL, OTHER = "AF01ABCDEF123", "AF01ZZZZZZ999"
MODEL = "PLAF103"


def t(serial: str, channel: str, direction: str, model: str = MODEL) -> str:
    return f"dl/{model}/{serial}/device/{channel}/{direction}"


def s(text: str) -> bytes:
    raw = text.encode()
    return len(raw).to_bytes(2, "big") + raw


def pkt(first: int, body: bytes) -> bytes:
    return bytes([first]) + broker.encode_length(len(body)) + body


def connect(client_id: str = SERIAL, level: int = 4, keepalive: int = 60, extra: bytes = b"", flags: int = 0x02):
    name = b"MQTT" if level == 4 else b"MQIsdp"
    body = s(name.decode()) + bytes([level, flags]) + keepalive.to_bytes(2, "big") + s(client_id) + extra
    return pkt(0x10, body)


def subscribe(*filters: str, pid: int = 1) -> bytes:
    return pkt(0x82, pid.to_bytes(2, "big") + b"".join(s(f) + b"\x01" for f in filters))


def publish(topic: str, payload: bytes, qos: int = 0, pid: int = 7) -> bytes:
    body = s(topic) + (pid.to_bytes(2, "big") if qos else b"") + payload
    return pkt(0x30 | qos << 1, body)


class Client:
    def __init__(self, reader, writer):
        self.r, self.w = reader, writer

    @classmethod
    async def open(cls, port: int) -> Client:
        return cls(*await asyncio.open_connection("127.0.0.1", port))

    async def send(self, data: bytes) -> None:
        self.w.write(data)
        await self.w.drain()

    async def recv(self, n: int, timeout: float = 2.0) -> bytes:
        return await asyncio.wait_for(self.r.readexactly(n), timeout)

    async def handshake(self, **kw) -> bytes:
        await self.send(connect(**kw))
        return await self.recv(4)

    async def closed(self, timeout: float = 3.0) -> bool:
        """True when the broker closes the connection (discarding anything still pending)."""
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                if not await asyncio.wait_for(self.r.read(1024), max(0.01, deadline - time.monotonic())):
                    return True
        except (asyncio.TimeoutError, ConnectionError):
            return isinstance(self.r.exception(), ConnectionError) if self.r.exception() else False
        return False

    def close(self) -> None:
        self.w.close()


class Harness:
    def __init__(self, **kw):
        self.published: list[tuple[str, str, bytes]] = []
        self.connected: list[tuple[str, str]] = []
        self.gone: list[str] = []
        kw.setdefault("peer_ok", lambda ip: True)
        self.broker = broker.Broker(
            lambda c, tp, p: self.published.append((c, tp, p)),
            lambda c, ip: self.connected.append((c, ip)),
            self.gone.append,
            host="127.0.0.1", port=0, **kw)

    async def __aenter__(self) -> Harness:
        await self.broker.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.broker.stop()

    async def client(self) -> Client:
        return await Client.open(self.broker.port)


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 20))


def test_handshake_311_and_31():
    async def go():
        async with Harness() as h:
            for level in (4, 3):
                c = await h.client()
                assert await c.handshake(level=level) == b"\x20\x02\x00\x00"
                c.close()
            assert [c for c, _ in h.connected] == [SERIAL, SERIAL]
    run(go())


def test_connect_with_will_and_credentials_is_parsed_and_ignored():
    async def go():
        async with Harness() as h:
            c = await h.client()
            extra = s("will/topic") + s("bye") + s("user") + s("pass")
            await c.send(connect(extra=extra, flags=0xC6))
            assert await c.recv(4) == b"\x20\x02\x00\x00"
            await c.send(b"\xc0\x00")
            assert await c.recv(2) == b"\xd0\x00"
    run(go())


def test_bad_protocol_level_and_client_id_refused():
    async def go():
        async with Harness() as h:
            c = await h.client()
            await c.send(pkt(0x10, s("MQTT") + bytes([5, 2, 0, 60]) + s(SERIAL)))
            assert await c.recv(4) == b"\x20\x02\x00\x01"
            assert await c.closed()
            for bad in ("", "laptop-1", "AF", "AF 01", "x" * 80):
                c = await h.client()
                await c.send(connect(client_id=bad))
                assert await c.recv(4) == b"\x20\x02\x00\x02"
                assert await c.closed()
            assert h.connected == []
    run(go())


def test_subscribe_publish_routing():
    async def go():
        async with Harness() as h:
            c = await h.client()
            await c.handshake()
            await c.send(subscribe(t(SERIAL, "service", "sub"), t(SERIAL, "event", "sub")))
            assert await c.recv(6) == b"\x90\x04\x00\x01\x00\x00"
            assert await h.broker.publish(SERIAL, t(SERIAL, "service", "sub"), b'{"cmd":"X"}')
            head = await c.recv(2)
            assert head[0] == 0x30
            body = await c.recv(head[1])
            topic_len = int.from_bytes(body[:2], "big")
            assert body[2:2 + topic_len].decode() == t(SERIAL, "service", "sub")
            assert body[2 + topic_len:] == b'{"cmd":"X"}'
            assert not await h.broker.publish(SERIAL, t(SERIAL, "ntp", "sub"), b"{}")  # not subscribed
            assert not await h.broker.publish("AF01NOTHERE1", t("AF01NOTHERE1", "ntp", "sub"), b"{}")

            await c.send(publish(t(SERIAL, "event", "post"), b'{"cmd":"HEARTBEAT"}'))
            await c.send(b"\xc0\x00")
            assert await c.recv(2) == b"\xd0\x00"
            assert h.published == [(SERIAL, t(SERIAL, "event", "post"), b'{"cmd":"HEARTBEAT"}')]

            await c.send(pkt(0xA2, b"\x00\x02" + s(t(SERIAL, "event", "sub"))))
            assert await c.recv(4) == b"\xb0\x02\x00\x02"
            assert not await h.broker.publish(SERIAL, t(SERIAL, "event", "sub"), b"{}")
    run(go())


def test_qos1_publish_is_acked_and_qos2_closes():
    async def go():
        async with Harness() as h:
            c = await h.client()
            await c.handshake()
            await c.send(publish(t(SERIAL, "event", "post"), b"{}", qos=1, pid=0x1234))
            assert await c.recv(4) == b"\x40\x02\x12\x34"
            assert len(h.published) == 1
            await c.send(publish(t(SERIAL, "event", "post"), b"{}", qos=2))
            assert await c.closed()
    run(go())


def test_cross_device_isolation():
    async def go():
        async with Harness() as h:
            a, b = await h.client(), await h.client()
            await a.handshake(client_id=SERIAL)
            await b.handshake(client_id=OTHER)
            await b.send(subscribe(t(OTHER, "service", "sub")))
            await b.recv(5)
            # A publishes into B's namespace: acked (QoS 1) but dropped
            await a.send(publish(t(OTHER, "event", "post"), b"{}", qos=1))
            assert await a.recv(4) == b"\x40\x02\x00\x07"
            # A tries to listen to B, to everything and across models
            await a.send(subscribe(t(OTHER, "service", "sub"), "dl/#", "#", "dl/+/+/device/#",
                                   f"dl/{MODEL}/+/device/#", "$SYS/#"))
            head = await a.recv(2)
            assert (await a.recv(head[1]))[2:] == bytes([0x80] * 6)
            await a.send(b"\xc0\x00")
            assert await a.recv(2) == b"\xd0\x00"
            # the server sending to B is never visible to A
            assert await h.broker.publish(OTHER, t(OTHER, "service", "sub"), b"{}")
            assert h.published == []
            assert (await b.recv(2))[0] == 0x30
    run(go())


def test_suback_codes_for_foreign_filters():
    async def go():
        async with Harness() as h:
            a = await h.client()
            await a.handshake()
            filters = [t(OTHER, "service", "sub"), "dl/#", "#", f"dl/{MODEL}/+/device/#", t(SERIAL, "+", "sub"),
                       t(SERIAL, "service", "sub", "PLAF103").replace("PLAF103", "BAD")]
            await a.send(subscribe(*filters))
            head = await a.recv(2)
            body = await a.recv(head[1])
            assert body[2:] == bytes([0x80, 0x80, 0x80, 0x80, 0x00, 0x80])
    run(go())


def test_model_is_fixed_per_connection():
    async def go():
        async with Harness() as h:
            c = await h.client()
            await c.handshake()
            await c.send(publish(t(SERIAL, "event", "post"), b"1"))
            await c.send(publish(t(SERIAL, "event", "post", "PLAF301"), b"2"))
            await c.send(b"\xc0\x00")
            await c.recv(2)
            assert [p for _, _, p in h.published] == [b"1"]
    run(go())


def test_non_lan_peer_rejected():
    async def go():
        async with Harness(peer_ok=broker.is_lan_ipv4) as h:  # loopback is not a LAN address
            c = await h.client()
            await c.send(connect())
            assert await c.closed()
            assert h.connected == []
        assert not broker.is_lan_ipv4("8.8.8.8") and broker.is_lan_ipv4("192.168.4.20")
    run(go())


def test_garbage_and_oversized_packets_close_cleanly():
    async def go():
        async with Harness() as h:
            cases = [
                b"\xff\xff\xff\xff\xff\xff",                                   # bad remaining length
                b"\x10\xff\xff\x7f",                                           # CONNECT, 2 MiB announced
                b"\x30\x00",                                                   # PUBLISH before CONNECT
                b"\x00\x00",                                                   # reserved packet type
                pkt(0x10, s("MQTT") + bytes([4, 2, 0, 60]) + b"\x00\x03\xff\xfe\xfd"),  # invalid UTF-8 id
                pkt(0x10, s("MQTT")[:3]),                                      # truncated
                pkt(0x10, s("MQTT") + bytes([4, 2, 0, 60]) + s(SERIAL) + b"x"),  # trailing bytes
            ]
            for data in cases:
                c = await h.client()
                await c.send(data)
                assert await c.closed(), data
            # after CONNECT: oversized, bad subscribe, bad ping, unknown type
            for data in (b"\x30\xff\xff\x7f", b"\x82\x00", b"\xc0\x01\x00", b"\xe1\x00", b"\xf0\x00"):
                c = await h.client()
                await c.handshake()
                await c.send(data)
                assert await c.closed(), data
            assert not h.broker.clients
            # still serving
            c = await h.client()
            assert await c.handshake() == b"\x20\x02\x00\x00"
    run(go())


def test_oversized_publish_body_closes():
    async def go():
        async with Harness() as h:
            c = await h.client()
            await c.handshake()
            await c.send(pkt(0x30, s(t(SERIAL, "event", "post")) + b"x" * (broker.MAX_PACKET + 1)))
            assert await c.closed()
            assert h.published == []
    run(go())


def test_keepalive_timeout_is_one_and_a_half_times():
    async def go():
        async with Harness() as h:
            c = await h.client()
            await c.handshake(keepalive=1)
            start = time.monotonic()
            assert await c.closed(5)
            assert 1.2 < time.monotonic() - start < 3.5
            assert h.gone == [SERIAL]
    run(go())


def test_pings_keep_connection_alive():
    async def go():
        async with Harness() as h:
            c = await h.client()
            await c.handshake(keepalive=1)
            for _ in range(4):
                await asyncio.sleep(0.6)
                await c.send(b"\xc0\x00")
                assert await c.recv(2) == b"\xd0\x00"
    run(go())


def test_connect_must_arrive_in_time():
    async def go():
        async with Harness(connect_timeout=0.3) as h:
            c = await h.client()
            assert await c.closed(2)
    run(go())


def test_new_connection_replaces_old():
    async def go():
        async with Harness() as h:
            a, b = await h.client(), await h.client()
            await a.handshake()
            await b.handshake()
            assert await a.closed()
            await b.send(subscribe(t(SERIAL, "service", "sub")))
            await b.recv(5)
            assert await h.broker.publish(SERIAL, t(SERIAL, "service", "sub"), b"{}")
            assert h.broker.clients == [SERIAL] and h.gone == []
    run(go())


def test_connection_limit():
    async def go():
        async with Harness() as h:
            idle = [await h.client() for _ in range(broker.MAX_CONNECTIONS)]
            await asyncio.sleep(0.2)
            extra = await h.client()
            assert await extra.closed()
            for c in idle:
                c.close()
    run(go())


def test_rate_limit_closes_flooder():
    async def go():
        async with Harness() as h:
            c = await h.client()
            await c.handshake()
            await c.send(b"\xc0\x00" * 400)
            assert await c.closed()
    run(go())


def test_callback_errors_do_not_drop_connection():
    async def go():
        b = broker.Broker(lambda *a: 1 / 0, host="127.0.0.1", port=0, peer_ok=lambda ip: True)
        await b.start()
        try:
            c = await Client.open(b.port)
            await c.handshake()
            await c.send(publish(t(SERIAL, "event", "post"), b"{}"))
            await c.send(b"\xc0\x00")
            assert await c.recv(2) == b"\xd0\x00"
        finally:
            await b.stop()
    run(go())


def test_disconnect_packet_and_cleanup():
    async def go():
        async with Harness() as h:
            c = await h.client()
            await c.handshake()
            await c.send(b"\xe0\x00")
            assert await c.closed()
            await asyncio.sleep(0.1)
            assert h.gone == [SERIAL] and not h.broker.is_connected(SERIAL)
    run(go())


def test_topic_matches():
    m = broker.topic_matches
    assert m("a/b", "a/b") and not m("a/b", "a/c") and not m("a/b", "a/b/c")
    assert m("a/+/c", "a/x/c") and not m("a/+/c", "a/x/y/c")
    assert m("a/#", "a/b/c")
    assert m("a/b/#", "a/b/c/d")
