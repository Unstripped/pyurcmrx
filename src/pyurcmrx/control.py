"""Runtime control protocol over TCP 51510 (spec §52 v0.4.0).

Framing: `type:u8 | length:u16le | opcode:u16le | payload`, where `length` counts the
opcode plus payload (requests) or opcode + status + body (responses); wire size is
`length + 3`. Each connection is half-duplex: one request, then its response, and the
controller never sends unsolicited frames on this socket (spec §52 §2.2).

Connection lifetime depends on the opcode (spec §52 §4): only GET_FILE_TIME is
documented as leaving the connection reusable. Every other request is treated as
terminal, so the client closes the connection after its response and opens a new one
for the next request.
"""

from __future__ import annotations

import asyncio
import struct
from asyncio import StreamWriter
from dataclasses import dataclass
from enum import IntEnum

from .errors import ActionRejectedError, MalformedFrameError, ProtocolError

CONTROL_PORT = 51510
FRAME_STANDARD = 0x02
HEADER_SIZE = 3
STATUS_SUCCESS = 0x01
FILE_TIME_BODY_SIZE = 48  # 4-byte local timestamp + 44-byte time-zone rule


class Opcode(IntEnum):
    ROOMS_CMD_ROOM_POWER_OFF = 0x000D
    ROOMS_CMD_HOUSE_OFF = 0x000E
    ROOMS_CMD_ROOM_POWER_ON = 0x0012
    MACRO_ACTION = 0x07DA
    ACTION_ACK = 0x07DB
    ACTION_AVR_SET_DRAG_VOLUME = 0x07ED
    VARIABLE_TABLE = 0x0898
    VARIABLE_CMD_GET_TABLE = 0x089A
    GET_NET_STATUS = 0x0FE1
    GET_FILE_TIME = 0x0FE6
    QUERY_REPLY = 0xFFFD
    QUERY_REPLY_OFFLINE = 0xFFFE  # GET_NET_STATUS reply when the controller is not online


# Requests after which the controller keeps the connection open (spec §52 §4.3).
REUSABLE_OPCODES = frozenset({Opcode.GET_FILE_TIME})


class ActionFlags(IntEnum):
    """MACRO_ACTION `flags` (spec §52 §3.1)."""

    SINGLE = 0x0000
    PRESS = 0x0001  # button press / macro start
    RELEASE = 0x0002  # button release / macro stop; ends repeats and volume ramps
    HOLD = 0x0003  # button hold / repeat


@dataclass(frozen=True, slots=True)
class ControllerTime:
    """GET_FILE_TIME reply (spec §52 §3.9).

    `local_timestamp` is a Unix-style timestamp already adjusted by the controller's
    time-zone and daylight-saving offsets. `time_zone_rule` is the controller's opaque
    44-byte time-zone rule block, kept verbatim.
    """

    local_timestamp: int
    time_zone_rule: bytes


@dataclass(frozen=True, slots=True)
class Response:
    resp_type: int
    opcode: int
    status: int
    body: bytes

    @property
    def ok(self) -> bool:
        return self.status == STATUS_SUCCESS


def encode_request(opcode: int, payload: bytes = b"", frame_type: int = FRAME_STANDARD) -> bytes:
    length = 2 + len(payload)
    if length > 0xFFFF:
        raise ValueError(f"payload too large: {len(payload)} bytes")
    return struct.pack("<BHH", frame_type, length, opcode) + payload


class ResponseDecoder:
    """Incremental decoder: tolerates responses split across or coalesced within reads."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[Response]:
        self._buffer += data
        responses: list[Response] = []
        while len(self._buffer) >= HEADER_SIZE:
            resp_type, length = struct.unpack_from("<BH", self._buffer, 0)
            if length < 3:
                raise MalformedFrameError(f"response length {length} < 3 (opcode + status)")
            end = HEADER_SIZE + length
            if len(self._buffer) < end:
                break
            (opcode,) = struct.unpack_from("<H", self._buffer, 3)
            responses.append(Response(resp_type, opcode, self._buffer[5], bytes(self._buffer[6:end])))
            del self._buffer[:end]
        return responses


class ControlClient:
    """Serialized request/response client for one controller.

    Requests are strictly one-at-a-time. Each request runs on a fresh connection, except
    that a connection left open by a reusable request (GET_FILE_TIME) carries the next one.
    """

    def __init__(self, host: str, port: int = CONTROL_PORT, timeout: float = 5.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._decoder = ResponseDecoder()
        self._pending: list[Response] = []
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> ControlClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def _reset(self) -> StreamWriter | None:
        """Drop the connection synchronously (safe inside cancellation handling)."""
        writer, self._reader, self._writer = self._writer, None, None
        self._decoder = ResponseDecoder()
        self._pending.clear()
        if writer is not None:
            writer.close()
        return writer

    async def close(self) -> None:
        writer = self._reset()
        if writer is not None:
            try:
                await writer.wait_closed()
            except OSError:
                pass

    def _connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing() and not self._reader.at_eof()

    async def _connect(self) -> None:
        if self._connected():
            return
        await self.close()
        self._reader, self._writer = await asyncio.open_connection(self.host, self.port)

    async def _read_response(self) -> Response:
        while not self._pending:
            data = await self._reader.read(4096)
            if not data:
                raise ProtocolError("controller closed the connection")
            self._pending.extend(self._decoder.feed(data))
        return self._pending.pop(0)

    async def request(self, opcode: int, payload: bytes, *expect: int) -> Response:
        """Send one request and return its response, whose opcode must be in `expect`.

        Any interruption (error, timeout, cancellation, wrong reply) leaves the stream
        position unknown, so the connection is dropped: a late reply must never be taken
        as the answer to the next request (all actions share ACK opcode 0x07DB).
        """
        frame = encode_request(opcode, payload)
        async with self._lock:
            try:
                async with asyncio.timeout(self.timeout):
                    await self._connect()
                    self._writer.write(frame)
                    await self._writer.drain()
                    response = await self._read_response()
                if response.opcode not in expect:
                    wanted = " or ".join(f"0x{e:04X}" for e in expect)
                    raise ProtocolError(f"expected opcode {wanted}, got 0x{response.opcode:04X}")
            except BaseException:
                self._reset()
                raise
            if opcode not in REUSABLE_OPCODES:
                await self.close()
            return response

    async def _action(self, opcode: int, payload: bytes) -> int:
        response = await self.request(opcode, payload, Opcode.ACTION_ACK)
        if not response.ok:
            raise ActionRejectedError(f"controller rejected opcode 0x{opcode:04X}")
        if len(response.body) < 2:
            raise MalformedFrameError("action ACK body shorter than 2-byte queue handle")
        (queue_handle,) = struct.unpack_from("<H", response.body, 0)
        return queue_handle

    async def get_net_status(self) -> bool:
        """True when the controller reports itself online (spec §52 §3.7)."""
        response = await self.request(Opcode.GET_NET_STATUS, b"", Opcode.QUERY_REPLY, Opcode.QUERY_REPLY_OFFLINE)
        return response.ok and response.opcode == Opcode.QUERY_REPLY

    async def get_file_time(self) -> ControllerTime:
        """Read the controller's local time and time-zone rule (spec §52 §3.9)."""
        response = await self.request(Opcode.GET_FILE_TIME, b"", Opcode.QUERY_REPLY)
        if len(response.body) != FILE_TIME_BODY_SIZE:
            raise MalformedFrameError(
                f"GET_FILE_TIME body is {len(response.body)} bytes, need {FILE_TIME_BODY_SIZE}"
            )
        (local_timestamp,) = struct.unpack_from("<I", response.body, 0)
        return ControllerTime(local_timestamp, response.body[4:])

    async def get_variables(self) -> dict[int, int]:
        response = await self.request(Opcode.VARIABLE_CMD_GET_TABLE, b"", Opcode.VARIABLE_TABLE)
        body = response.body
        if len(body) < 4:
            raise MalformedFrameError("variable table body shorter than 4-byte count")
        (count,) = struct.unpack_from("<I", body, 0)
        if len(body) < 4 + 5 * count:
            raise MalformedFrameError(f"variable table declares {count} entries, body has {len(body)} bytes")
        return {var_id: value for var_id, value in struct.iter_unpack("<IB", body[4 : 4 + 5 * count])}

    async def macro_action(
        self, room_id: int, device_id: int, macro_id: int, flags: int = ActionFlags.SINGLE
    ) -> int:
        """Trigger an action; returns the controller's queue handle.

        With `device_id != 0`, `macro_id` is the device function ID (`mcs.dat` `funcs[].id`)
        and `room_id = 0` lets the controller resolve the room. With `device_id = 0`,
        `macro_id` is a room macro ID or trigger code.
        """
        return await self._action(Opcode.MACRO_ACTION, struct.pack("<IIII", room_id, device_id, macro_id, flags))

    async def set_volume(
        self,
        device_id: int,
        level: int,
        *,
        button_id: int = 275,
        min_value: int = 0,
        max_value: int = 100,
        mute: bool = False,
    ) -> int:
        """Set an absolute volume (0..100) and mute state (spec §52 §3.2, `0x07ED`).

        The target is `trigger_code = (device_id << 16) | button_id`; the spec vector uses
        the device's Vol+ function (275) as `button_id`.
        """
        if not 0 <= device_id <= 0xFFFF or not 0 <= button_id <= 0xFFFF:
            raise ValueError("device_id and button_id must fit in 16 bits")
        if not 0 <= min_value <= level <= max_value <= 100:
            raise ValueError(f"need 0 <= min_value <= level <= max_value <= 100, got {min_value}, {level}, {max_value}")
        payload = struct.pack("<IIIIBH", (device_id << 16) | button_id, min_value, max_value, level, int(mute), 0)
        return await self._action(Opcode.ACTION_AVR_SET_DRAG_VOLUME, payload)

    async def room_power_on(self, room_id: int) -> int:
        return await self._action(Opcode.ROOMS_CMD_ROOM_POWER_ON, struct.pack("<I", room_id))

    async def room_power_off(self, room_id: int) -> int:
        return await self._action(Opcode.ROOMS_CMD_ROOM_POWER_OFF, struct.pack("<I", room_id))

    async def house_off(self) -> int:
        return await self._action(Opcode.ROOMS_CMD_HOUSE_OFF, b"")
