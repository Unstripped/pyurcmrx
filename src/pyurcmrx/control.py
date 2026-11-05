"""Runtime control protocol over TCP 51510 (spec §52).

Framing: `type:u8 | length:u16le | opcode:u16le | payload`, where `length` counts the
opcode plus payload (requests) or opcode + status + body (responses). The wire size is
therefore `length + 3`. This follows the §52 field tables and every §52 test vector;
the §52 §2.1 prose ("length + 1") contradicts both and is pending investigator review.
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


class Opcode(IntEnum):
    ROOMS_CMD_ROOM_POWER_OFF = 0x000D
    ROOMS_CMD_HOUSE_OFF = 0x000E
    ROOMS_CMD_ROOM_POWER_ON = 0x0012
    MACRO_ACTION = 0x07DA
    ACTION_ACK = 0x07DB
    VARIABLE_TABLE = 0x0898
    VARIABLE_CMD_GET_TABLE = 0x089A
    GET_NET_STATUS = 0x0FE1
    GET_FILE_TIME = 0x0FE5
    QUERY_REPLY = 0xFFFD


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

    Responses carry no request identifier (GET_NET_STATUS and GET_FILE_TIME both reply
    0xFFFD), so requests are strictly one-at-a-time. The controller drops idle sessions
    after 10 s (spec §52 §4.1); the client reconnects on the next request instead of
    sending an unspecified keepalive.
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
        """Drop the session synchronously (safe inside cancellation handling)."""
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

    async def _connect(self) -> None:
        if self._writer is not None and not self._writer.is_closing() and not self._reader.at_eof():
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

    async def request(self, opcode: int, payload: bytes, expect: int) -> Response:
        frame = encode_request(opcode, payload)
        async with self._lock:
            # Any interruption (error, timeout, cancellation, wrong reply) leaves the stream
            # position unknown; drop the session so a late reply cannot be taken as the
            # answer to the next request (all actions share ACK opcode 0x07DB).
            try:
                async with asyncio.timeout(self.timeout):
                    await self._connect()
                    self._writer.write(frame)
                    await self._writer.drain()
                    response = await self._read_response()
                if response.opcode != expect:
                    raise ProtocolError(f"expected opcode 0x{expect:04X}, got 0x{response.opcode:04X}")
            except BaseException:
                self._reset()
                raise
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
        response = await self.request(Opcode.GET_NET_STATUS, b"", Opcode.QUERY_REPLY)
        return response.ok

    async def get_file_time(self) -> int:
        response = await self.request(Opcode.GET_FILE_TIME, b"", Opcode.QUERY_REPLY)
        if len(response.body) < 4:
            raise MalformedFrameError("GET_FILE_TIME body shorter than 4 bytes")
        return struct.unpack_from("<I", response.body, 0)[0]

    async def get_variables(self) -> dict[int, int]:
        response = await self.request(Opcode.VARIABLE_CMD_GET_TABLE, b"", Opcode.VARIABLE_TABLE)
        body = response.body
        if len(body) < 4:
            raise MalformedFrameError("variable table body shorter than 4-byte count")
        (count,) = struct.unpack_from("<I", body, 0)
        if len(body) < 4 + 5 * count:
            raise MalformedFrameError(f"variable table declares {count} entries, body has {len(body)} bytes")
        return {var_id: value for var_id, value in struct.iter_unpack("<IB", body[4 : 4 + 5 * count])}

    async def macro_action(self, room_id: int, device_id: int, macro_id: int, flags: int = 0) -> int:
        """Trigger a macro or button action; returns the controller's queue handle."""
        return await self._action(Opcode.MACRO_ACTION, struct.pack("<IIII", room_id, device_id, macro_id, flags))

    async def room_power_on(self, room_id: int) -> int:
        return await self._action(Opcode.ROOMS_CMD_ROOM_POWER_ON, struct.pack("<I", room_id))

    async def room_power_off(self, room_id: int) -> int:
        return await self._action(Opcode.ROOMS_CMD_ROOM_POWER_OFF, struct.pack("<I", room_id))

    async def house_off(self) -> int:
        return await self._action(Opcode.ROOMS_CMD_HOUSE_OFF, b"")
