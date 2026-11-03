"""A minimal MQTT broker for PetLibro devices; pure asyncio, no Home Assistant imports.

The devices talk plain MQTT (3.1 or 3.1.1) to ``mqtt.us.petlibro.com:1883``. This broker serves
exactly one conversation per device: the device and the integration. Home Assistant is the only
other party, through the callbacks and ``publish``; clients never see each other's traffic.

Security: peers must be private / link-local IPv4; at most 16 connections; packets up to 64 KiB;
CONNECT within 10 s; idle clients are dropped at 1.5x their keepalive; message rate is limited;
client ids must look like PetLibro serials; a client can only publish to and subscribe under its
own ``dl/<model>/<serial>/`` prefix (anything else is dropped). Credentials are not checked: the
devices use static ones that mean nothing locally. Parsing is strict and never raises out of a
connection task. Only QoS 0 and 1 are supported (QoS 2 closes the connection); nothing is retained.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .hosts import is_lan_ipv4
from .models import valid_model, valid_serial

_LOGGER = logging.getLogger(__name__)

MAX_CONNECTIONS = 16
MAX_PACKET = 64 * 1024
CONNECT_TIMEOUT = 10.0
PACKET_TIMEOUT = 10.0  # the rest of a packet must follow its first byte this quickly
WRITE_TIMEOUT = 5.0
DEFAULT_IDLE = 300.0
IDLE_FACTOR = 1.5
MAX_FILTERS = 16
RATE_PER_SEC = 20.0
RATE_BURST = 60.0

CONNECT, CONNACK, PUBLISH, PUBACK = 1, 2, 3, 4
SUBSCRIBE, SUBACK, UNSUBSCRIBE, UNSUBACK = 8, 9, 10, 11
PINGREQ, PINGRESP, DISCONNECT = 12, 13, 14
RC_OK, RC_VERSION, RC_ID = 0, 1, 2


class MqttError(Exception):
    """A malformed or unsupported packet; the connection is closed."""


class ConnectRefused(MqttError):
    def __init__(self, rc: int) -> None:
        super().__init__(f"connect refused ({rc})")
        self.rc = rc


@dataclass
class Connect:
    level: int
    keepalive: int
    client_id: str


class RateLimiter:
    """Token bucket."""

    def __init__(self, rate: float = RATE_PER_SEC, burst: float = RATE_BURST) -> None:
        self.rate, self.burst = rate, burst
        self.tokens = burst
        self.stamp = time.monotonic()

    def allow(self) -> bool:
        now = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.stamp) * self.rate)
        self.stamp = now
        if self.tokens < 1:
            return False
        self.tokens -= 1
        return True


# --- parsing --------------------------------------------------------------------------
def encode_length(n: int) -> bytes:
    out = bytearray()
    while True:
        n, digit = divmod(n, 128)
        out.append(digit | (0x80 if n else 0))
        if not n:
            return bytes(out)


def read_string(buf: bytes, pos: int) -> tuple[str, int]:
    """A length-prefixed UTF-8 string; MqttError when truncated, invalid or containing NUL."""
    if pos + 2 > len(buf):
        raise MqttError("truncated string length")
    end = pos + 2 + int.from_bytes(buf[pos:pos + 2], "big")
    if end > len(buf):
        raise MqttError("truncated string")
    try:
        text = buf[pos + 2:end].decode("utf-8")
    except UnicodeDecodeError as err:
        raise MqttError("invalid UTF-8") from err
    if "\x00" in text:
        raise MqttError("NUL in string")
    return text, end


def read_bytes(buf: bytes, pos: int) -> tuple[bytes, int]:
    if pos + 2 > len(buf):
        raise MqttError("truncated field length")
    end = pos + 2 + int.from_bytes(buf[pos:pos + 2], "big")
    if end > len(buf):
        raise MqttError("truncated field")
    return buf[pos + 2:end], end


def parse_connect(body: bytes) -> Connect:
    name, pos = read_string(body, 0)
    if pos + 4 > len(body):
        raise MqttError("truncated CONNECT")
    level, flags = body[pos], body[pos + 1]
    keepalive = int.from_bytes(body[pos + 2:pos + 4], "big")
    pos += 4
    if (name, level) not in (("MQIsdp", 3), ("MQTT", 4)):
        raise ConnectRefused(RC_VERSION)
    if level == 4 and flags & 1:
        raise MqttError("reserved CONNECT flag set")
    client_id, pos = read_string(body, pos)
    if flags & 0x04:  # will: parsed and ignored
        _, pos = read_string(body, pos)
        _, pos = read_bytes(body, pos)
    if flags & 0x80:
        _, pos = read_string(body, pos)
    if flags & 0x40:
        _, pos = read_bytes(body, pos)
    if pos != len(body):
        raise MqttError("trailing bytes in CONNECT")
    return Connect(level, keepalive, client_id)


def topic_matches(topic_filter: str, topic: str) -> bool:
    f, t = topic_filter.split("/"), topic.split("/")
    for i, part in enumerate(f):
        if part == "#":
            return i == len(f) - 1
        if i >= len(t) or (part != "+" and part != t[i]):
            return False
    return len(f) == len(t)


class _Conn:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, ip: str) -> None:
        self.reader, self.writer, self.ip = reader, writer, ip
        self.client_id = ""
        self.model: str | None = None
        self.filters: list[str] = []
        self.limiter = RateLimiter()
        self.lock = asyncio.Lock()

    async def send(self, data: bytes) -> None:
        async with self.lock:
            self.writer.write(data)
            await asyncio.wait_for(self.writer.drain(), WRITE_TIMEOUT)

    def own_topic(self, topic: str, *, is_filter: bool) -> bool:
        """True when the topic (or filter) lies under this client's ``dl/<model>/<serial>/`` prefix."""
        parts = topic.split("/")
        if len(parts) < 4 or parts[0] != "dl" or parts[2] != self.client_id or not valid_model(parts[1]):
            return False
        if self.model not in (None, parts[1]):
            return False
        rest = parts[3:]
        for i, part in enumerate(rest):
            if ("+" in part or "#" in part) and not (is_filter and part in ("+", "#")):
                return False
            if part == "#" and i != len(rest) - 1:
                return False
        self.model = parts[1]
        return True

    def close(self) -> None:
        try:
            self.writer.close()
        except Exception:  # noqa: BLE001
            pass


class Broker:
    """Accepts device connections and hands their publishes to ``on_publish``."""

    def __init__(
        self,
        on_publish: Callable[[str, str, bytes], None],
        on_connect: Callable[[str, str], None] | None = None,
        on_disconnect: Callable[[str], None] | None = None,
        *,
        host: str = "0.0.0.0",
        port: int = 1883,
        peer_ok: Callable[[str], bool] = is_lan_ipv4,
        client_ok: Callable[[str], bool] = valid_serial,
        connect_timeout: float = CONNECT_TIMEOUT,
        default_idle: float = DEFAULT_IDLE,
    ) -> None:
        self._on_publish, self._on_connect, self._on_disconnect = on_publish, on_connect, on_disconnect
        self.host, self.port = host, port
        self._peer_ok, self._client_ok = peer_ok, client_ok
        self._connect_timeout, self._default_idle = connect_timeout, default_idle
        self._server: asyncio.AbstractServer | None = None
        self._conns: dict[str, _Conn] = {}
        self._active = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        _LOGGER.debug("MQTT broker listening on %s:%s", self.host, self.port)

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
        for conn in list(self._conns.values()):
            conn.close()
        self._conns.clear()
        if server is not None:
            try:  # Python 3.12+ waits for open connections here, so they are closed first
                await asyncio.wait_for(server.wait_closed(), 5)
            except (asyncio.TimeoutError, TimeoutError):
                _LOGGER.debug("Timed out waiting for the broker to close")

    def is_connected(self, client_id: str) -> bool:
        return client_id in self._conns

    @property
    def clients(self) -> list[str]:
        return list(self._conns)

    async def publish(self, client_id: str, topic: str, payload: bytes) -> bool:
        """Send a QoS 0 message to a connected client that subscribed to the topic."""
        conn = self._conns.get(client_id)
        if conn is None or not conn.own_topic(topic, is_filter=False):
            return False
        if not any(topic_matches(f, topic) for f in conn.filters):
            return False
        raw = topic.encode()
        body = len(raw).to_bytes(2, "big") + raw + payload
        try:
            await conn.send(bytes([PUBLISH << 4]) + encode_length(len(body)) + body)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Write to %s failed; closing", client_id, exc_info=True)
            conn.close()
            return False
        return True

    # --- connection handling ---
    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        ip = peer[0] if peer else ""
        conn = _Conn(reader, writer, ip)
        if not self._peer_ok(ip):
            _LOGGER.debug("Refused connection from non-LAN peer %s", ip)
            conn.close()
            return
        if self._active >= MAX_CONNECTIONS:
            _LOGGER.warning("Refused connection from %s: too many connections", ip)
            conn.close()
            return
        self._active += 1
        try:
            await self._session(conn)
        except asyncio.CancelledError:
            raise
        except (asyncio.TimeoutError, TimeoutError):
            _LOGGER.debug("Connection from %s timed out", ip)
        except (MqttError, asyncio.IncompleteReadError, ConnectionError, OSError) as err:
            _LOGGER.debug("Connection from %s closed: %s", ip, err)
        except Exception:  # noqa: BLE001  nothing may escape a connection task
            _LOGGER.debug("Connection from %s failed", ip, exc_info=True)
        finally:
            self._active -= 1
            conn.close()
            if conn.client_id and self._conns.get(conn.client_id) is conn:
                del self._conns[conn.client_id]
                self._notify(self._on_disconnect, conn.client_id)

    def _notify(self, fn: Callable[..., Any] | None, *args: Any) -> None:
        if fn is None:
            return
        try:
            fn(*args)
        except Exception:  # noqa: BLE001  a callback bug must not drop the connection
            _LOGGER.exception("MQTT callback failed")

    @staticmethod
    async def _read_packet(reader: asyncio.StreamReader, first_timeout: float) -> tuple[int, int, bytes]:
        first = (await asyncio.wait_for(reader.readexactly(1), first_timeout))[0]
        length, mult = 0, 1
        for _ in range(4):
            byte = (await asyncio.wait_for(reader.readexactly(1), PACKET_TIMEOUT))[0]
            length += (byte & 0x7F) * mult
            if not byte & 0x80:
                break
            mult *= 128
        else:
            raise MqttError("remaining length too long")
        if length > MAX_PACKET:
            raise MqttError("packet too large")
        body = await asyncio.wait_for(reader.readexactly(length), PACKET_TIMEOUT) if length else b""
        return first >> 4, first & 0x0F, body

    async def _session(self, conn: _Conn) -> None:
        ptype, flags, body = await self._read_packet(conn.reader, self._connect_timeout)
        if ptype != CONNECT or flags:
            raise MqttError("first packet must be CONNECT")
        try:
            hello = parse_connect(body)
            if not hello.client_id or not self._client_ok(hello.client_id):
                raise ConnectRefused(RC_ID)
        except ConnectRefused as err:
            await conn.send(bytes([CONNACK << 4, 2, 0, err.rc]))
            raise
        conn.client_id = hello.client_id
        if (old := self._conns.get(conn.client_id)) is not None:
            _LOGGER.debug("Client %s reconnected; replacing the old connection", conn.client_id)
            old.close()
        self._conns[conn.client_id] = conn
        await conn.send(bytes([CONNACK << 4, 2, 0, RC_OK]))
        self._notify(self._on_connect, conn.client_id, conn.ip)
        idle = hello.keepalive * IDLE_FACTOR if hello.keepalive else self._default_idle
        while True:
            ptype, flags, body = await self._read_packet(conn.reader, idle)
            if not conn.limiter.allow():
                raise MqttError("rate limit exceeded")
            if self._conns.get(conn.client_id) is not conn:
                return  # replaced by a newer connection
            if ptype == PUBLISH:
                await self._publish(conn, flags, body)
            elif ptype == SUBSCRIBE:
                await self._subscribe(conn, flags, body)
            elif ptype == UNSUBSCRIBE:
                await self._unsubscribe(conn, flags, body)
            elif ptype == PINGREQ:
                if body or flags:
                    raise MqttError("bad PINGREQ")
                await conn.send(bytes([PINGRESP << 4, 0]))
            elif ptype == DISCONNECT:
                return
            elif ptype == PUBACK:
                continue
            else:
                raise MqttError(f"unsupported packet type {ptype}")

    async def _publish(self, conn: _Conn, flags: int, body: bytes) -> None:
        qos = (flags >> 1) & 3
        if qos > 1:
            raise MqttError("QoS 2 is not supported")
        topic, pos = read_string(body, 0)
        packet_id = 0
        if qos:
            if pos + 2 > len(body):
                raise MqttError("truncated packet id")
            packet_id = int.from_bytes(body[pos:pos + 2], "big")
            pos += 2
        payload = body[pos:]
        if qos:
            await conn.send(bytes([PUBACK << 4, 2]) + packet_id.to_bytes(2, "big"))
        if not conn.own_topic(topic, is_filter=False):
            _LOGGER.debug("Dropped publish from %s to foreign topic %r", conn.client_id, topic[:80])
            return
        self._notify(self._on_publish, conn.client_id, topic, payload)

    async def _subscribe(self, conn: _Conn, flags: int, body: bytes) -> None:
        if flags != 2 or len(body) < 2:
            raise MqttError("bad SUBSCRIBE")
        packet_id, pos, codes = body[:2], 2, bytearray()
        if pos >= len(body):
            raise MqttError("SUBSCRIBE without topics")
        while pos < len(body):
            topic_filter, pos = read_string(body, pos)
            if pos >= len(body):
                raise MqttError("truncated SUBSCRIBE")
            if body[pos] & 0xFC or body[pos] == 3:
                raise MqttError("bad requested QoS")
            pos += 1
            ok = (topic_filter and conn.own_topic(topic_filter, is_filter=True)
                  and (topic_filter in conn.filters or len(conn.filters) < MAX_FILTERS))
            if ok:
                if topic_filter not in conn.filters:
                    conn.filters.append(topic_filter)
                codes.append(0)  # messages to devices are sent at QoS 0
            else:
                _LOGGER.debug("Refused subscription of %s to %r", conn.client_id, topic_filter[:80])
                codes.append(0x80)
        await conn.send(bytes([SUBACK << 4]) + encode_length(2 + len(codes)) + packet_id + bytes(codes))

    async def _unsubscribe(self, conn: _Conn, flags: int, body: bytes) -> None:
        if flags != 2 or len(body) < 2:
            raise MqttError("bad UNSUBSCRIBE")
        packet_id, pos = body[:2], 2
        if pos >= len(body):
            raise MqttError("UNSUBSCRIBE without topics")
        while pos < len(body):
            topic_filter, pos = read_string(body, pos)
            if topic_filter in conn.filters:
                conn.filters.remove(topic_filter)
        await conn.send(bytes([UNSUBACK << 4, 2]) + packet_id)
