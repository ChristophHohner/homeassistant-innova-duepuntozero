"""Innova Duepuntozero API Client - v2 (services.app.AppService)."""
from __future__ import annotations

import asyncio
import logging
import socket
import ssl
import struct
import threading
import time
import uuid
from dataclasses import dataclass, replace

import aiohttp
import h2.config
import h2.connection
import h2.events

_LOGGER = logging.getLogger(__name__)

BRAND = "diffusapp"          # "innova" for the official app
REST_BASE = f"https://v2.api.{BRAND}.solutiontech.tech/app"
GRPC_HOST = f"v2.grpc.{BRAND}.solutiontech.tech"
GRPC_PORT = 443

M_SEND_DEVICE = "/services.app.AppService/SendDevice"
M_SUBSCRIBE = "/services.app.AppService/SubscribeEvents"

MODE_AUTO, MODE_HEAT, MODE_COOL, MODE_DRY, MODE_FAN = 1, 2, 3, 4, 5
FAN_AUTO, FAN_MIN, FAN_MEDIUM, FAN_MAX, FAN_BOOST = 1, 2, 3, 4, 5
TYPE_POWER_STATE, TYPE_SETPOINT = 1, 2
TYPE_OPERATION_MODE, TYPE_FAN_SPEED, TYPE_FLAP = 3, 4, 5


def _varint(v: int) -> bytes:
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(0x80 | b)
        else:
            out.append(b)
            return bytes(out)


def _f_varint(n: int, v: int) -> bytes:
    return _varint(n << 3) + _varint(v)


def _f_float(n: int, v: float) -> bytes:
    return _varint((n << 3) | 5) + struct.pack("<f", v)


def _f_bytes(n: int, v: bytes) -> bytes:
    return _varint((n << 3) | 2) + _varint(len(v)) + v


def _read_varint(d: bytes, p: int) -> tuple[int, int]:
    r = s = 0
    while p < len(d):
        b = d[p]
        p += 1
        r |= (b & 0x7F) << s
        if not b & 0x80:
            return r, p
        s += 7
    raise ValueError("truncated varint")


def _parse(d: bytes) -> dict[int, list]:
    out: dict[int, list] = {}
    p = 0
    while p < len(d):
        tag, p = _read_varint(d, p)
        n, wt = tag >> 3, tag & 7
        if wt == 0:
            v, p = _read_varint(d, p)
        elif wt == 1:
            v, p = d[p:p + 8], p + 8
        elif wt == 2:
            ln, p = _read_varint(d, p)
            v, p = d[p:p + ln], p + ln
        elif wt == 5:
            v, p = d[p:p + 4], p + 4
        else:
            break
        out.setdefault(n, []).append(v)
    return out


def _i(f: dict, n: int, d: int = 0) -> int:
    v = f.get(n)
    return int(v[0]) if v and isinstance(v[0], int) else d


def _b(f: dict, n: int) -> bool:
    return bool(_i(f, n))


def _flt(f: dict, n: int, d: float = 0.0) -> float:
    v = f.get(n)
    if not v or not isinstance(v[0], (bytes, bytearray)) or len(v[0]) != 4:
        return d
    return struct.unpack("<f", bytes(v[0]))[0]


def _msg(f: dict, n: int) -> bytes | None:
    v = f.get(n)
    if not v or not isinstance(v[0], (bytes, bytearray)):
        return None
    return bytes(v[0])


def _frame(p: bytes) -> bytes:
    return struct.pack(">BI", 0, len(p)) + p


def _unframe(d: bytes) -> bytes:
    if len(d) < 5:
        return b""
    c, ln = struct.unpack(">BI", d[:5])
    if c:
        raise InnovaApiError("compressed gRPC not supported")
    return d[5:5 + ln]


@dataclass
class DeviceStatus:
    power_state: bool
    room_temperature: float
    setpoint: float
    setpoint_min: float
    setpoint_max: float
    setpoint_step: float
    operation_mode: int
    fan_speed: int
    flap: bool
    humidity: float = 0.0
    alarms: int = 0

    def apply_event(self, event_type: int, event_value: bytes) -> bool:
        return False


def _merge_ac_state(data: bytes, cur: DeviceStatus | None) -> DeviceStatus:
    """Merge a partial AC update into a copy of the current state."""
    d = _parse(data)
    new = (DeviceStatus(False, 0.0, 22.0, 16.0, 31.0, 0.5, 0, 0, False)
           if cur is None else replace(cur))
    if 1 in d:
        new.alarms = _i(d, 1, new.alarms)
    if 2 in d:
        new.power_state = _b(d, 2)
    if 4 in d:
        t = _flt(d, 4, new.room_temperature)
        if t:
            new.room_temperature = t
    if 5 in d:
        new.operation_mode = _i(d, 5, new.operation_mode)
    if 6 in d:
        new.fan_speed = _i(d, 6, new.fan_speed)
    if 7 in d:
        new.flap = _b(d, 7)
    if 10 in d:
        h = _flt(d, 10, new.humidity)
        if h:
            new.humidity = h
    sp = _msg(d, 3)
    if sp:
        s = _parse(sp)
        if 1 in s:
            new.setpoint = _flt(s, 1, new.setpoint)
        if 2 in s:
            new.setpoint_min = _flt(s, 2, new.setpoint_min)
        if 3 in s:
            new.setpoint_max = _flt(s, 3, new.setpoint_max)
        if 4 in s:
            new.setpoint_step = _flt(s, 4, new.setpoint_step) or 0.5
    return new


def _extract_ac(frame: bytes, want_mac: bytes,
                cur: DeviceStatus | None = None) -> DeviceStatus | None:
    top = _parse(frame)
    dev = _msg(top, 1)
    if not dev:
        return None
    d = _parse(dev)
    mac = _msg(d, 1)
    if mac and want_mac and mac != want_mac:
        return None
    ev = _msg(d, 3)
    if not ev:
        return None
    de = _msg(_parse(ev), 3)
    if not de:
        return None
    ac = _msg(_parse(de), 2)
    if not ac:
        return None
    return _merge_ac_state(ac, cur)


class InnovaApiError(Exception):
    """Raised when the Innova API returns an unexpected response."""


class _TokenExpiredError(Exception):
    pass


class InnovaClient:
    def __init__(self, email: str, password: str, mac_address: str) -> None:
        self._email = email
        self._password = password
        self._mac_address = mac_address
        self._mac = bytes.fromhex(mac_address.replace(":", "").replace("-", ""))
        self._token: str | None = None
        self._home_id: bytes | None = None
        self._node_id = 0
        self._status: DeviceStatus | None = None
        self._external_cb = None
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._stop_boot = threading.Event()

    async def async_ensure_logged_in(self) -> None:
        if self._token is None:
            await self._async_login()

    async def _async_login(self) -> None:
        try:
            async with aiohttp.ClientSession() as s:
                r = await s.post(f"{REST_BASE}/users/login",
                                 json={"email": self._email,
                                       "password": self._password})
                if r.status != 200:
                    raise InnovaApiError(f"Login failed: HTTP {r.status}")
                body = await r.json()
                tok = body.get("token") or body.get("accessToken")
                if not tok:
                    raise InnovaApiError("no token in login response")
                self._token = tok
                hr = await s.get(f"{REST_BASE}/homes",
                                 headers={"Authorization": f"Bearer {tok}"})
                if hr.status != 200:
                    raise InnovaApiError(f"/homes failed: HTTP {hr.status}")
                raw = await hr.json()
                homes = raw.get("value", raw) if isinstance(raw, dict) else raw
        except aiohttp.ClientError as err:
            raise InnovaApiError(f"network error: {err}") from err

        target = self._mac_address.replace(":", "").lower()
        for home in homes:
            for dev in home.get("devices", []):
                if (dev.get("macAddress") or "").replace(":", "").lower() == target:
                    self._home_id = uuid.UUID(home["id"]).bytes
                    self._node_id = dev.get("nodeId") or 0
                    _LOGGER.debug("matched device, node=%s", self._node_id)
                    return
        raise InnovaApiError(f"device {self._mac_address} not found in any home")

    # ---------------- unary ----------------

    def _call(self, method: str, payload: bytes) -> bytes:
        if not self._token:
            raise InnovaApiError("not logged in")
        ctx = ssl.create_default_context()
        ctx.set_alpn_protocols(["h2"])
        conn = h2.connection.H2Connection(
            config=h2.config.H2Configuration(client_side=True,
                                             header_encoding="utf-8"))
        body = bytearray()
        hdrs: dict[str, str] = {}
        with socket.create_connection((GRPC_HOST, GRPC_PORT), timeout=20) as raw:
            with ctx.wrap_socket(raw, server_hostname=GRPC_HOST) as sock:
                conn.initiate_connection()
                sock.sendall(conn.data_to_send(65535))
                sid = conn.get_next_available_stream_id()
                conn.send_headers(sid, [
                    (":method", "POST"), (":path", method), (":scheme", "https"),
                    (":authority", f"{GRPC_HOST}:{GRPC_PORT}"),
                    ("content-type", "application/grpc"), ("te", "trailers"),
                    ("authorization", f"Bearer {self._token}")])
                conn.send_data(sid, _frame(payload), end_stream=True)
                sock.sendall(conn.data_to_send(65535))
                done = False
                while not done:
                    data = sock.recv(65535)
                    if not data:
                        break
                    for ev in conn.receive_data(data):
                        if isinstance(ev, h2.events.ResponseReceived):
                            hdrs.update(dict(ev.headers))
                        elif isinstance(ev, h2.events.DataReceived):
                            body.extend(ev.data)
                            conn.acknowledge_received_data(
                                ev.flow_controlled_length, ev.stream_id)
                        elif isinstance(ev, h2.events.TrailersReceived):
                            hdrs.update(dict(ev.headers))
                            done = True
                        elif isinstance(ev, h2.events.StreamEnded):
                            done = True
                    sock.sendall(conn.data_to_send(65535))
        st = hdrs.get("grpc-status")
        if st == "16":
            self._token = None
            raise _TokenExpiredError()
        if st not in (None, "0"):
            raise InnovaApiError(
                f"gRPC {method} status {st}: {hdrs.get('grpc-message', '')}")
        return _unframe(bytes(body))

    async def _async_call(self, method: str, payload: bytes) -> bytes:
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(None, self._call, method, payload)
        except _TokenExpiredError:
            await self._async_login()
            return await loop.run_in_executor(None, self._call, method, payload)

    # ---------------- event stream ----------------

    def _run_stream(self, stop: threading.Event) -> None:
        if not self._token or not self._home_id:
            raise InnovaApiError("not ready")
        ctx = ssl.create_default_context()
        ctx.set_alpn_protocols(["h2"])
        conn = h2.connection.H2Connection(
            config=h2.config.H2Configuration(client_side=True,
                                             header_encoding="utf-8"))
        buf = bytearray()
        with socket.create_connection((GRPC_HOST, GRPC_PORT), timeout=120) as raw:
            with ctx.wrap_socket(raw, server_hostname=GRPC_HOST) as sock:
                conn.initiate_connection()
                sock.sendall(conn.data_to_send(65535))
                sid = conn.get_next_available_stream_id()
                conn.send_headers(sid, [
                    (":method", "POST"), (":path", M_SUBSCRIBE),
                    (":scheme", "https"),
                    (":authority", f"{GRPC_HOST}:{GRPC_PORT}"),
                    ("content-type", "application/grpc"), ("te", "trailers"),
                    ("authorization", f"Bearer {self._token}")])
                conn.send_data(sid, _frame(_f_bytes(1, self._home_id)),
                               end_stream=True)
                sock.sendall(conn.data_to_send(65535))
                _LOGGER.debug("event stream opened")
                while not stop.is_set() and not self._stop.is_set():
                    try:
                        data = sock.recv(65535)
                    except socket.timeout:
                        continue
                    if not data:
                        return
                    for ev in conn.receive_data(data):
                        if isinstance(ev, h2.events.DataReceived):
                            buf.extend(ev.data)
                            conn.acknowledge_received_data(
                                ev.flow_controlled_length, ev.stream_id)
                            while len(buf) >= 5:
                                _, ln = struct.unpack(">BI", buf[:5])
                                if len(buf) < 5 + ln:
                                    break
                                msg = bytes(buf[5:5 + ln])
                                del buf[:5 + ln]
                                self._handle_frame(msg)
                        elif isinstance(ev, (h2.events.StreamEnded,
                                             h2.events.StreamReset)):
                            return
                    sock.sendall(conn.data_to_send(65535))

    def _handle_frame(self, msg: bytes) -> None:
        with self._lock:
            cur = self._status
        try:
            st = _extract_ac(msg, self._mac, cur)
        except Exception as e:  # noqa: BLE001
            _LOGGER.debug("parse failed: %s", e)
            return
        if not st:
            return

        with self._lock:
            self._status = st
        _LOGGER.debug("state: %s", st)
        self._notify(st)

    def _stream_device_events(self, on_event=None, stop_event=None) -> None:
        """Entry point used by __init__.py; also stops the bootstrap stream."""
        if on_event is not None:
            self._external_cb = on_event
        self._stop_boot.set()           # only one subscription at a time
        while not self._stop.is_set():
            if stop_event is not None and stop_event.is_set():
                return
            try:
                self._run_stream(stop_event or self._stop)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("stream error: %s", err)
            if stop_event is not None:
                if stop_event.wait(10):
                    return
            elif self._stop.wait(10):
                return

    def _bootstrap_loop(self) -> None:
        """Short-lived stream used before __init__.py starts its own."""
        end = time.monotonic() + 60
        while (not self._stop_boot.is_set() and not self._stop.is_set()
               and time.monotonic() < end):
            try:
                self._run_stream(self._stop_boot)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("bootstrap stream error: %s", err)
            if self._stop_boot.wait(5):
                return
        _LOGGER.debug("bootstrap stream finished")

    def _ensure_stream(self) -> None:
        if self._stop_boot.is_set():
            return                      # external stream already running
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._bootstrap_loop,
                                        name="innova-v2-boot", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._stop_boot.set()

    # ---------------- public API ----------------

    def _notify(self, st: DeviceStatus) -> None:
        if self._external_cb:
            try:
                self._external_cb(st)
            except Exception:  # noqa: BLE001
                pass

    async def _async_kick(self) -> None:
        """Nudge the device: change the setpoint, then restore it."""
        try:
            with self._lock:
                sp = self._status.setpoint if self._status else 22.0
            other = sp + 0.5 if sp < 30.0 else sp - 0.5

            async def _send(v: float) -> None:
                p = _f_bytes(1, self._mac)
                if self._node_id:
                    p += _f_varint(2, self._node_id)
                p += _f_bytes(3, _f_bytes(3, _f_bytes(1, _f_float(2, v))))
                await self._async_call(M_SEND_DEVICE, p)

            await _send(other)
            await asyncio.sleep(1.5)
            await _send(sp)
            _LOGGER.debug("kick sent (%.1f -> %.1f)", other, sp)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("kick failed: %s", err)

    async def async_get_device_status(self) -> DeviceStatus | None:
        await self.async_ensure_logged_in()
        self._ensure_stream()
        with self._lock:
            if self._status:
                return self._status

        def _wait(seconds: float):
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                with self._lock:
                    if self._status:
                        return self._status
                time.sleep(0.3)
            return None

        loop = asyncio.get_event_loop()
        st = await loop.run_in_executor(None, _wait, 6)
        if st is None:
            await self._async_kick()
            st = await loop.run_in_executor(None, _wait, 15)
        if st is None:
            _LOGGER.warning("no state received from event stream yet")
        return st

    async def _async_set(self, field: bytes, optimistic: dict | None = None) -> None:
        """Send a command and apply it locally at once for instant feedback."""
        await self.async_ensure_logged_in()
        payload = _f_bytes(1, self._mac)
        if self._node_id:
            payload += _f_varint(2, self._node_id)
        payload += _f_bytes(3, _f_bytes(3, _f_bytes(1, field)))
        await self._async_call(M_SEND_DEVICE, payload)
        if not optimistic:
            return
        with self._lock:
            base = self._status or DeviceStatus(
                False, 0.0, 22.0, 16.0, 31.0, 0.5, 0, 0, False)
            st = replace(base, **optimistic)
            self._status = st
        self._notify(st)

    async def async_set_device_value(self, type_: int, value) -> None:
        if type_ == TYPE_SETPOINT:
            await self.async_set_temperature(float(value))
        elif type_ == TYPE_POWER_STATE:
            if value:
                await self.async_turn_on()
            else:
                await self.async_turn_off()
        elif type_ == TYPE_OPERATION_MODE:
            await self.async_set_operation_mode(int(value))
        elif type_ == TYPE_FAN_SPEED:
            await self.async_set_fan_speed(int(value))
        elif type_ == TYPE_FLAP:
            await self._async_set(_f_varint(5, 1 if value else 0),
                                  {"flap": bool(value)})
        else:
            raise InnovaApiError(f"unknown value type {type_}")

    async def async_turn_on(self) -> None:
        await self._async_set(_f_varint(1, 1), {"power_state": True})

    async def async_turn_off(self) -> None:
        await self._async_set(_f_varint(1, 0), {"power_state": False})

    async def async_set_temperature(self, temperature: float) -> None:
        await self._async_set(_f_float(2, float(temperature)),
                              {"setpoint": float(temperature)})

    async def async_set_operation_mode(self, mode: int) -> None:
        await self._async_set(_f_varint(3, int(mode)),
                              {"operation_mode": int(mode)})

    async def async_set_fan_speed(self, speed: int) -> None:
        await self._async_set(_f_varint(4, int(speed)),
                              {"fan_speed": int(speed)})
