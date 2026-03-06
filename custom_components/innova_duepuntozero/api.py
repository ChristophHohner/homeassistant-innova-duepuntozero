"""Innova Duepuntozero API Client – REST Login + gRPC device control.

Login uses HTTPS/REST via aiohttp (already bundled with Home Assistant).
Device control uses gRPC, implemented directly over a raw TLS socket with
the h2 library.  This avoids native C extensions (grpcio) that cannot be
compiled inside the HA OS container.

gRPC wire format over HTTP/2:
  - Request/response body = 5-byte framing header + protobuf payload
    - byte 0   : compression flag (0 = uncompressed)
    - bytes 1-4: big-endian uint32 payload length
  - Content-Type: application/grpc
"""
from __future__ import annotations

import asyncio
import logging
import socket
import ssl
import struct
import threading
from dataclasses import dataclass

import aiohttp
import h2.config
import h2.connection
import h2.events

_LOGGER = logging.getLogger(__name__)

REST_BASE = "https://api.innova.solutiontech.tech/api"
GRPC_HOST = "grpc.innova.solutiontech.tech"
GRPC_PORT = 443

# DuepuntozeroValueType constants (from APK reverse engineering)
TYPE_POWER_STATE = 1
TYPE_SETPOINT = 2
TYPE_OPERATION_MODE = 3
TYPE_FAN_SPEED = 4
TYPE_FLAP = 5

# DuepuntozeroEventType constants (from APK reverse engineering)
# These are the type values used in SubscribeToDeviceEvents responses,
# distinct from the DuepuntozeroValueType constants used in SetDeviceValue.
EVENT_MANUAL_MODE     = 249
EVENT_FLAP            = 250
EVENT_FAN_SPEED       = 251
EVENT_OPERATION_MODE  = 252
EVENT_ROOM_TEMP       = 253
EVENT_SETPOINT        = 254
EVENT_POWER_STATE     = 255

# DuepuntozeroFanSpeed constants (from APK reverse engineering)
FAN_AUTO   = 0
FAN_MIN    = 1
FAN_MEDIUM = 2
FAN_MAX    = 3

# DuepuntozeroOperationMode constants (from APK reverse engineering)
MODE_AUTO = 0
MODE_HEAT = 1
MODE_COOL = 2
MODE_FAN = 3
MODE_DRY = 4

# Temperatures are transmitted as integers x 10 (e.g. 220 = 22.0 degC)
TEMP_FACTOR = 10


# ---------------------------------------------------------------------------
# Minimal protobuf encoder/decoder
# We avoid grpcio-generated pb2 files and handle serialization manually.
# The protocol is simple enough that this is more maintainable than adding
# protoc as a build step.
# ---------------------------------------------------------------------------

def _encode_varint(value: int) -> bytes:
    """Encode a non-negative integer as a protobuf varint."""
    result = bytearray()
    while True:
        bits = value & 0x7F
        value >>= 7
        if value:
            result.append(0x80 | bits)
        else:
            result.append(bits)
            break
    return bytes(result)


def _encode_int32_field(field_number: int, value: int) -> bytes:
    """Encode a protobuf int32 field (wire type 0).

    Always encodes the field even when value is 0 - we are sending commands
    where 0 is meaningful (e.g. turn off = PowerState 0, Auto = OperationMode 0)
    and must not be omitted.
    """
    tag = (field_number << 3) | 0  # wire type 0 = varint
    return _encode_varint(tag) + _encode_varint(value)


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Read a varint from data starting at pos, return (value, new_pos)."""
    result = 0
    shift = 0
    while pos < len(data):
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
    raise ValueError("Truncated varint in protobuf data")


def _parse_fields(data: bytes) -> dict[int, list]:
    """Parse a protobuf message into {field_number: [raw_values]}.

    Supports wire types 0 (varint) and 2 (length-delimited).
    Unknown wire types are skipped so that new server fields do not break parsing.
    """
    fields: dict[int, list] = {}
    pos = 0
    while pos < len(data):
        tag, pos = _read_varint(data, pos)
        field_number = tag >> 3
        wire_type = tag & 0x07

        if wire_type == 0:  # varint
            value, pos = _read_varint(data, pos)
        elif wire_type == 1:  # 64-bit - skip
            pos += 8
            continue
        elif wire_type == 2:  # length-delimited
            length, pos = _read_varint(data, pos)
            value = data[pos: pos + length]
            pos += length
        elif wire_type == 5:  # 32-bit - skip
            pos += 4
            continue
        else:
            _LOGGER.debug("Unsupported protobuf wire type %d, stopping parse", wire_type)
            break

        fields.setdefault(field_number, []).append(value)
    return fields


def _get_int(fields: dict, field_number: int, default: int = 0) -> int:
    vals = fields.get(field_number)
    return int(vals[0]) if vals else default


def _get_bool(fields: dict, field_number: int) -> bool:
    return bool(_get_int(fields, field_number))


def _get_bytes(fields: dict, field_number: int) -> bytes | None:
    vals = fields.get(field_number)
    return bytes(vals[0]) if vals else None


# ---------------------------------------------------------------------------
# gRPC framing
# ---------------------------------------------------------------------------

def _grpc_encode(payload: bytes) -> bytes:
    """Wrap a protobuf payload in a gRPC data frame."""
    return struct.pack(">BI", 0, len(payload)) + payload


def _grpc_decode(data: bytes) -> bytes:
    """Extract the protobuf payload from a gRPC response frame.

    An empty body is valid - it represents an Empty protobuf message,
    as returned by SetDeviceValue.
    """
    if len(data) == 0:
        return b""
    if len(data) < 5:
        raise InnovaApiError(f"gRPC response too short ({len(data)} bytes)")
    compressed, length = struct.unpack(">BI", data[:5])
    if compressed:
        raise InnovaApiError("Compressed gRPC responses are not supported")
    return data[5: 5 + length]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

def _bytes_to_int(data: bytes) -> int:
    """Convert a big-endian byte string to int, as PrimitiveUtils.toInt() does."""
    result = 0
    for b in data:
        result = (result << 8) | b
    return result


@dataclass
class DeviceStatus:
    """Parsed status of a Duepuntozero device."""

    power_state: bool
    room_temperature: float   # degC
    setpoint: float           # degC
    setpoint_min: float       # degC
    setpoint_max: float       # degC
    setpoint_step: float      # degC
    operation_mode: int       # One of the MODE_* constants
    fan_speed: int            # One of the FAN_* constants
    flap: bool

    def apply_event(self, event_type: int, event_value: bytes) -> bool:
        """Apply an incoming event to this status object.

        Returns True if a known field was updated, False if the event type
        is unknown and a full poll is needed instead.
        """
        value = _bytes_to_int(event_value) if event_value else 0
        if event_type == EVENT_POWER_STATE:
            self.power_state = bool(value)
        elif event_type == EVENT_SETPOINT:
            self.setpoint = value / TEMP_FACTOR
        elif event_type == EVENT_OPERATION_MODE:
            self.operation_mode = value
        elif event_type == EVENT_FAN_SPEED:
            self.fan_speed = value
        elif event_type == EVENT_ROOM_TEMP:
            self.room_temperature = value / TEMP_FACTOR
        elif event_type == EVENT_FLAP:
            self.flap = bool(value)
        elif event_type == EVENT_MANUAL_MODE:
            pass  # manual mode not exposed in HA, silently ignore
        else:
            return False
        return True


def parse_device_status_response(data: bytes) -> DeviceStatus | None:
    """Parse the raw bytes of a GetDeviceStatusResponse message."""
    try:
        top = _parse_fields(data)

        main_bytes = _get_bytes(top, 2)  # field 2 = MainStatus
        if not main_bytes:
            _LOGGER.warning("GetDeviceStatusResponse contains no main_status")
            return None

        main = _parse_fields(main_bytes)

        duepunto_bytes = _get_bytes(main, 4)  # field 4 = DuepuntozeroStatus (oneof)
        if not duepunto_bytes:
            _LOGGER.warning("main_status contains no duepuntozero_status")
            return None

        d = _parse_fields(duepunto_bytes)

        # Field 3 = SetpointStatus (nested message)
        setpoint_val, setpoint_min, setpoint_max, setpoint_step = 220, 160, 310, 5
        sp_bytes = _get_bytes(d, 3)
        if sp_bytes:
            sp = _parse_fields(sp_bytes)
            setpoint_val  = _get_int(sp, 1, setpoint_val)
            setpoint_min  = _get_int(sp, 2, setpoint_min)
            setpoint_max  = _get_int(sp, 3, setpoint_max)
            setpoint_step = _get_int(sp, 4, setpoint_step)

        return DeviceStatus(
            power_state=_get_bool(d, 2),
            room_temperature=_get_int(d, 4) / TEMP_FACTOR,
            setpoint=setpoint_val / TEMP_FACTOR,
            setpoint_min=setpoint_min / TEMP_FACTOR,
            setpoint_max=setpoint_max / TEMP_FACTOR,
            setpoint_step=setpoint_step / TEMP_FACTOR,
            operation_mode=_get_int(d, 5),
            fan_speed=_get_int(d, 6),
            flap=_get_bool(d, 7),
        )
    except Exception:
        _LOGGER.exception("Failed to parse GetDeviceStatusResponse")
        return None


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

class InnovaApiError(Exception):
    """Raised when the Innova API returns an unexpected response."""


class _TokenExpiredError(Exception):
    """Internal: token was rejected, re-login required."""


class InnovaClient:
    """Client for the Innova Duepuntozero cloud API.

    Login is performed via HTTPS REST; device control calls use gRPC
    over a raw TLS+HTTP/2 socket.
    """

    def __init__(self, email: str, password: str, mac_address: str) -> None:
        self._email = email
        self._password = password
        self._mac_address = mac_address
        self._token: str | None = None

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def async_ensure_logged_in(self) -> None:
        """Ensure a valid token exists, logging in only if necessary."""
        if self._token is not None:
            return
        await self._async_login()

    async def _async_login(self) -> None:
        """Obtain a fresh bearer token from the REST login endpoint."""
        try:
            async with aiohttp.ClientSession() as session:
                resp = await session.post(
                    f"{REST_BASE}/users/login",
                    json={"email": self._email, "password": self._password},
                )
                if resp.status != 200:
                    raise InnovaApiError(f"Login failed: HTTP {resp.status}")
                body = await resp.json()
        except aiohttp.ClientError as err:
            raise InnovaApiError(f"Network error during login: {err}") from err

        token = body.get("token")
        if not token:
            raise InnovaApiError("Login response did not contain a token")
        self._token = token
        _LOGGER.debug("Innova login successful")

    # ------------------------------------------------------------------
    # gRPC transport
    # ------------------------------------------------------------------

    def _call_unary(self, method: str, payload: bytes) -> bytes:
        """Execute a synchronous unary gRPC call, return raw protobuf bytes."""
        if self._token is None:
            raise InnovaApiError("Not logged in - call async_ensure_logged_in first")

        framed = _grpc_encode(payload)
        ctx = ssl.create_default_context()
        ctx.set_alpn_protocols(["h2"])

        headers = [
            (":method", "POST"),
            (":path", method),
            (":scheme", "https"),
            (":authority", f"{GRPC_HOST}:{GRPC_PORT}"),
            ("content-type", "application/grpc"),
            ("te", "trailers"),
            ("authorization", f"Bearer {self._token}"),
            ("mac_address", self._mac_address),
        ]

        conn = h2.connection.H2Connection(
            config=h2.config.H2Configuration(client_side=True, header_encoding="utf-8")
        )
        response_data = bytearray()
        response_headers: dict[str, str] = {}

        with socket.create_connection((GRPC_HOST, GRPC_PORT)) as raw_sock:
            with ctx.wrap_socket(raw_sock, server_hostname=GRPC_HOST) as sock:
                conn.initiate_connection()
                sock.sendall(conn.data_to_send(65535))

                stream_id = conn.get_next_available_stream_id()
                conn.send_headers(stream_id, headers)
                conn.send_data(stream_id, framed, end_stream=True)
                sock.sendall(conn.data_to_send(65535))

                while True:
                    data = sock.recv(65535)
                    if not data:
                        break
                    events = conn.receive_data(data)
                    sock.sendall(conn.data_to_send(65535))

                    done = False
                    for event in events:
                        if isinstance(event, h2.events.ResponseReceived):
                            response_headers = {
                                k.decode() if isinstance(k, bytes) else k:
                                v.decode() if isinstance(v, bytes) else v
                                for k, v in event.headers
                            }
                        elif isinstance(event, h2.events.DataReceived):
                            response_data.extend(event.data)
                            conn.acknowledge_received_data(
                                event.flow_controlled_length, event.stream_id
                            )
                        elif isinstance(event, h2.events.TrailersReceived):
                            for k, v in event.headers:
                                k = k.decode() if isinstance(k, bytes) else k
                                v = v.decode() if isinstance(v, bytes) else v
                                response_headers[k] = v
                            done = True
                        elif isinstance(event, h2.events.StreamEnded):
                            done = True

                    if done:
                        conn.close_connection()
                        sock.sendall(conn.data_to_send(65535))
                        break

        _LOGGER.debug("gRPC %s -> headers=%s, content=%s",
                      method, response_headers, bytes(response_data).hex())

        if response_headers.get(":status", "200") != "200":
            raise InnovaApiError(
                f"gRPC call {method} returned HTTP {response_headers.get(':status')}"
            )
        grpc_status = response_headers.get("grpc-status")
        if grpc_status is not None and grpc_status != "0":
            grpc_message = response_headers.get("grpc-message", "")
            if grpc_status == "16":  # UNAUTHENTICATED
                self._token = None
                raise _TokenExpiredError()
            raise InnovaApiError(
                f"gRPC call {method} failed with status {grpc_status}: {grpc_message}"
            )

        return _grpc_decode(bytes(response_data))

    def _stream_device_events(
        self,
        on_event: callable,
        stop_event: threading.Event,
    ) -> None:
        """Open a SubscribeToDeviceEvents stream and call on_event for each message.

        Runs synchronously – intended to be called in an executor thread.
        Returns when stop_event is set or the server closes the stream.
        Each call to on_event receives (event_type: int, event_value: bytes).
        """
        if self._token is None:
            raise InnovaApiError("Not logged in")

        ctx = ssl.create_default_context()
        ctx.set_alpn_protocols(["h2"])

        headers = [
            (":method", "POST"),
            (":path", "/device_controls.Controls/SubscribeToDeviceEvents"),
            (":scheme", "https"),
            (":authority", f"{GRPC_HOST}:{GRPC_PORT}"),
            ("content-type", "application/grpc"),
            ("te", "trailers"),
            ("authorization", f"Bearer {self._token}"),
            ("mac_address", self._mac_address),
        ]

        conn = h2.connection.H2Connection(
            config=h2.config.H2Configuration(client_side=True, header_encoding="utf-8")
        )

        with socket.create_connection((GRPC_HOST, GRPC_PORT)) as raw_sock:
            raw_sock.settimeout(60.0)  # detect dead connections
            with ctx.wrap_socket(raw_sock, server_hostname=GRPC_HOST) as sock:
                conn.initiate_connection()
                sock.sendall(conn.data_to_send(65535))

                stream_id = conn.get_next_available_stream_id()
                conn.send_headers(stream_id, headers)
                conn.send_data(stream_id, _grpc_encode(b""), end_stream=True)
                sock.sendall(conn.data_to_send(65535))

                buf = bytearray()

                while not stop_event.is_set():
                    try:
                        data = sock.recv(65535)
                    except socket.timeout:
                        continue
                    if not data:
                        break

                    events = conn.receive_data(data)
                    sock.sendall(conn.data_to_send(65535))

                    for event in events:
                        if isinstance(event, h2.events.DataReceived):
                            buf.extend(event.data)
                            conn.acknowledge_received_data(
                                event.flow_controlled_length, event.stream_id
                            )
                            # Parse all complete gRPC frames from the buffer
                            while len(buf) >= 5:
                                _, length = struct.unpack(">BI", buf[:5])
                                if len(buf) < 5 + length:
                                    break  # wait for more data
                                frame = bytes(buf[5: 5 + length])
                                buf = buf[5 + length:]
                                fields = _parse_fields(frame)
                                event_type = _get_int(fields, 1)
                                event_value = _get_bytes(fields, 2) or b""
                                on_event(event_type, event_value)

                        elif isinstance(event, (h2.events.StreamEnded, h2.events.StreamReset)):
                            return  # server closed stream

                conn.close_connection()
                sock.sendall(conn.data_to_send(65535))

    async def _async_call_unary(self, method: str, payload: bytes) -> bytes:
        """Call _call_unary in executor, re-logging in once if the token expired."""
        try:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._call_unary, method, payload
            )
        except _TokenExpiredError:
            _LOGGER.debug("Token expired, re-logging in")
            await self._async_login()
            return await asyncio.get_event_loop().run_in_executor(
                None, self._call_unary, method, payload
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def async_get_device_status(self) -> DeviceStatus | None:
        """Fetch and parse the current device status."""
        raw = await self._async_call_unary(
            "/device_controls.Controls/GetDeviceStatus", b""
        )
        return parse_device_status_response(raw)

    async def async_set_device_value(self, type_: int, value: int) -> None:
        """Send a SetDeviceValue command."""
        payload = _encode_int32_field(1, type_) + _encode_int32_field(2, value)
        await self._async_call_unary("/device_controls.Controls/SetDeviceValue", payload)

    async def async_turn_on(self) -> None:
        """Turn the device on."""
        await self.async_set_device_value(TYPE_POWER_STATE, 1)

    async def async_turn_off(self) -> None:
        """Turn the device off."""
        await self.async_set_device_value(TYPE_POWER_STATE, 0)

    async def async_set_temperature(self, temperature: float) -> None:
        """Set the target temperature in degC."""
        await self.async_set_device_value(TYPE_SETPOINT, round(temperature * TEMP_FACTOR))

    async def async_set_operation_mode(self, mode: int) -> None:
        """Set the operation mode (one of the MODE_* constants)."""
        await self.async_set_device_value(TYPE_OPERATION_MODE, mode)

    async def async_set_fan_speed(self, speed: int) -> None:
        """Set the fan speed."""
        await self.async_set_device_value(TYPE_FAN_SPEED, speed)
