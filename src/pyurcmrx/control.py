"""Runtime control protocol over TCP 51510 (spec §52 v0.5.0).

Framing: `type:u8 | length:u16le | opcode:u16le | payload`, where `length` counts the
opcode plus payload (requests) or opcode + status + body (responses); wire size is
`length + 3`. Each connection is half-duplex: one request, then its response frames, and
the controller never sends unsolicited frames on this socket (spec §52 §2.2). Some
requests produce more than one response frame (MACRO_ACTION: 0x07DB then 0x07DC), so
frames are split by `resp_length`, never assumed to be one per read or per request.

Outcome (spec §52 §2.2, §2.3): the response opcode carries the result. `0xFFFE` is a
generic negative response that may answer any request and is a failure whatever its
status byte; any status other than `0x01` is also a failure. `resp_type` says nothing
about success.

Connection lifetime depends on the opcode (spec §52 §4 rules 3-5): only GET_FILE_TIME is
documented as leaving the connection reusable. Every other request is treated as
terminal, so the client closes the connection after the final response frame and opens a
new one for the next request.
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
DRAG_VOLUME_ECHO_SIZE = 17  # 0x07EE body: request offsets 0x00-0x10 (spec §52 §3.2)


class Opcode(IntEnum):
    ROOMS_CMD_ROOM_POWER_OFF = 0x000D
    ROOMS_CMD_HOUSE_OFF = 0x000E
    ROOM_POWER_OFF_REPLY = 0x000F
    ROOMS_CMD_ROOM_POWER_ON = 0x0012
    ROOM_POWER_ON_REPLY = 0x0014
    # Room audio, extended (room-addressed) and connected-device forms (spec §52 §3.11).
    ROOM_SET_VOLUME = 0x007D
    ROOM_SET_MUTE = 0x007E
    ZONE_SET_VOLUME = 0x0097
    ZONE_SET_MUTE = 0x0098
    MACRO_ACTION = 0x07DA
    ACTION_ACK = 0x07DB
    ACTION_FOLLOW_UP = 0x07DC  # second MACRO_ACTION frame; meaning not established (spec §52 §6 item 4)
    ACTION_AVR_SET_DRAG_VOLUME = 0x07ED
    ACTION_AVR_SET_DRAG_VOLUME_REPLY = 0x07EE
    VARIABLE_TABLE = 0x0898
    VARIABLE_CMD_GET_TABLE = 0x089A
    GET_NET_STATUS = 0x0FE1
    GET_FILE_TIME = 0x0FE6
    GENERIC_POSITIVE = 0xFFFD
    GENERIC_NEGATIVE = 0xFFFE


# Requests after which the controller keeps the connection open (spec §52 §4 rule 5). Every
# other opcode is terminal (§4 rules 3 and 4).
REUSABLE_OPCODES = frozenset({Opcode.GET_FILE_TIME})


@dataclass(frozen=True, slots=True)
class ControllerTime:
    """GET_FILE_TIME reply (spec §52 §3.9).

    `local_timestamp` is the controller's local wall-clock time encoded as seconds since
    1970-01-01 00:00:00; it is not a UTC instant. Formatting it as UTC shows the
    controller's local date and time. UTC is `local_timestamp + 60 × (bias + standard_bias)`
    during standard time, or `+ 60 × (bias + daylight_bias)` during daylight-saving time,
    with those fields taken from `time_zone_rule`.

    `time_zone_rule` is the 44-byte rule block, kept verbatim: `bias` int32 LE at +0x00,
    `standard_date` 8 × uint16 LE at +0x04, `standard_bias` int32 LE at +0x14,
    `daylight_date` 8 × uint16 LE at +0x18, `daylight_bias` int32 LE at +0x28.
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
        """False for the generic negative response or any status other than 0x01 (spec §52 §2.2)."""
        return self.opcode != Opcode.GENERIC_NEGATIVE and self.status == STATUS_SUCCESS


def encode_request(opcode: int, payload: bytes = b"", frame_type: int = FRAME_STANDARD) -> bytes:
    length = 2 + len(payload)
    if length > 0xFFFF:
        raise ValueError(f"payload too large: {len(payload)} bytes")
    return struct.pack("<BHH", frame_type, length, opcode) + payload


def encode_trigger_code(object_id: int, button_id: int) -> int:
    """Pack `(object_id << 16) | button_id` (spec §52 §3.1, §53 §3.4.4).

    `object_id` is a project user-interface object identifier, not an `mcs.dat`
    connected-device identifier. Clients should send the button `code` values the project
    model publishes and not build codes for functions that no button carries.
    """
    _check_range("object_id", object_id, 16)
    _check_range("button_id", button_id, 16)
    return (object_id << 16) | button_id


def _check_range(name: str, value: int, bits: int) -> None:
    if not 0 <= value < 1 << bits:
        raise ValueError(f"{name} must fit in {bits} unsigned bits, got {value}")


def _check_response(request_opcode: int, response: Response, expect: tuple[int, ...]) -> Response:
    if response.opcode == Opcode.GENERIC_NEGATIVE and response.opcode not in expect:
        raise ActionRejectedError(
            f"controller answered opcode 0x{request_opcode:04X} with generic negative 0xFFFE"
            f" (status 0x{response.status:02X})"
        )
    if response.opcode not in expect:
        wanted = " or ".join(f"0x{e:04X}" for e in expect)
        raise ProtocolError(f"expected opcode {wanted}, got 0x{response.opcode:04X}")
    if response.status != STATUS_SUCCESS:
        raise ActionRejectedError(
            f"controller answered opcode 0x{request_opcode:04X} with status 0x{response.status:02X}"
        )
    return response


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
    The spec sets no idle limit for that connection and clients must not rely on one being
    kept forever (spec §52 §4 rule 5): a connection the controller has since closed is
    replaced on the next request, and `close()` (or `async with`) releases it when done.
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

    async def _send(self, frame: bytes) -> None:
        await self._connect()
        self._writer.write(frame)
        await self._writer.drain()

    async def _read_response(self) -> Response:
        while not self._pending:
            data = await self._reader.read(4096)
            if not data:
                raise ProtocolError("controller closed the connection")
            self._pending.extend(self._decoder.feed(data))
        return self._pending.pop(0)

    async def request(self, opcode: int, payload: bytes, *expect: int, then: tuple[int, ...] = ()) -> Response:
        """Send one request and return its first response frame, whose opcode must be in `expect`.

        `then` lists, in order, the opcodes of the further frames the request produces
        (MACRO_ACTION's 0x07DC). They are read and checked before the connection is
        released, because clients must consume every response frame (spec §52 §2.2).

        A generic negative response (0xFFFE) not listed in `expect`, or any frame whose
        status is not 0x01, raises ActionRejectedError (spec §52 §2.2, §2.3). Any other
        opcode raises ProtocolError.

        Any interruption (error, timeout, cancellation, wrong reply) leaves the stream
        position unknown, so the connection is dropped: a late reply must never be taken
        as the answer to the next request.
        """
        frame = encode_request(opcode, payload)
        async with self._lock:
            try:
                async with asyncio.timeout(self.timeout):
                    await self._send(frame)
                    response = _check_response(opcode, await self._read_response(), expect)
                    for follow_up in then:
                        _check_response(opcode, await self._read_response(), (follow_up,))
            except BaseException:
                self._reset()
                raise
            if opcode not in REUSABLE_OPCODES:
                await self.close()
            return response

    async def _request_unconfirmed(self, opcode: int, payload: bytes) -> Response | None:
        """Send a terminal request whose success reply is not established (spec §52 §3.11).

        Returns the first reply frame if one arrives within the timeout, or None if the
        controller closes the connection or stays silent. A generic negative reply or a
        status other than 0x01 raises ActionRejectedError.
        """
        frame = encode_request(opcode, payload)
        async with self._lock:
            try:
                async with asyncio.timeout(self.timeout):
                    await self._send(frame)
                try:
                    async with asyncio.timeout(self.timeout):
                        response = await self._read_response()
                except (TimeoutError, ProtocolError):  # silence or close: no reply observed
                    response = None
                if response is not None and not response.ok:
                    raise ActionRejectedError(
                        f"controller answered opcode 0x{opcode:04X} with opcode 0x{response.opcode:04X}"
                        f" (status 0x{response.status:02X})"
                    )
            except BaseException:
                self._reset()
                raise
            await self.close()
            return response

    async def get_net_status(self) -> bool:
        """True when the controller reports itself online (spec §52 §3.7).

        Online means the controller's most recent check found its wired interface up with
        an IPv4 address; it is not a test of any remote service. The controller answers
        0xFFFE (False) while the link is down, no address could be read, or before its
        first check after startup.
        """
        response = await self.request(
            Opcode.GET_NET_STATUS, b"", Opcode.GENERIC_POSITIVE, Opcode.GENERIC_NEGATIVE
        )
        return response.opcode == Opcode.GENERIC_POSITIVE

    async def get_file_time(self) -> ControllerTime:
        """Read the controller's local time and time-zone rule (spec §52 §3.9).

        The connection stays open for the next request (spec §52 §4 rule 5); close the
        client when done.
        """
        response = await self.request(Opcode.GET_FILE_TIME, b"", Opcode.GENERIC_POSITIVE)
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

    async def macro_action(self, trigger_code: int) -> None:
        """Trigger a project button by its trigger code (spec §52 §3.1).

        `trigger_code` is a button `code` from the project model (see
        `encode_trigger_code`). The controller answers 0x07DB then 0x07DC; neither says
        whether the code exists, so returning means only that the request was acknowledged.
        """
        _check_range("trigger_code", trigger_code, 32)
        await self.request(
            Opcode.MACRO_ACTION,
            struct.pack("<I", trigger_code),
            Opcode.ACTION_ACK,
            then=(Opcode.ACTION_FOLLOW_UP,),
        )

    async def set_volume(
        self,
        trigger_code: int,
        level: int,
        *,
        min_value: int,
        max_value: int,
        mute: bool = False,
    ) -> None:
        """Set an absolute level through a project volume control (spec §52 §3.2, `0x07ED`).

        `trigger_code` is `(object_id << 16) | button_id` of the project volume control;
        `min_value`, `max_value` and `level` are raw device-dependent values, not
        percentages. The 0x07EE reply echoes the request and is returned even for a trigger
        code the project lacks, so returning means only that the request was acknowledged,
        not that the level was applied.
        """
        for name, value in (
            ("trigger_code", trigger_code),
            ("level", level),
            ("min_value", min_value),
            ("max_value", max_value),
        ):
            _check_range(name, value, 32)
        payload = struct.pack("<IIIIBH", trigger_code, min_value, max_value, level, int(mute), 0)
        response = await self.request(
            Opcode.ACTION_AVR_SET_DRAG_VOLUME, payload, Opcode.ACTION_AVR_SET_DRAG_VOLUME_REPLY
        )
        if len(response.body) != DRAG_VOLUME_ECHO_SIZE:
            raise MalformedFrameError(
                f"0x07EE body is {len(response.body)} bytes, need {DRAG_VOLUME_ECHO_SIZE}"
            )
        if response.body != payload[:DRAG_VOLUME_ECHO_SIZE]:
            raise ProtocolError("0x07EE body does not echo the request")

    async def room_power_off(self, room_id: int) -> None:
        """Power a room off (spec §52 §3.3). The reply is the same for an unknown room."""
        _check_range("room_id", room_id, 32)
        await self.request(Opcode.ROOMS_CMD_ROOM_POWER_OFF, struct.pack("<I", room_id), Opcode.ROOM_POWER_OFF_REPLY)

    async def room_power_on(self, room_id: int) -> None:
        """Power a room on (spec §52 §3.5). The reply is the same for an unknown room."""
        _check_range("room_id", room_id, 32)
        await self.request(Opcode.ROOMS_CMD_ROOM_POWER_ON, struct.pack("<I", room_id), Opcode.ROOM_POWER_ON_REPLY)

    async def house_off(self) -> None:
        """Power the house off (spec §52 §3.4; reply 0x07DB is static analysis, not live-tested)."""
        await self.request(Opcode.ROOMS_CMD_HOUSE_OFF, b"", Opcode.ACTION_ACK)

    async def set_room_volume(self, room_id: int, value: int) -> Response | None:
        """Set a room's audio zone to raw level `value` (spec §52 §3.11, `0x007D`).

        `value` is the zone's raw level, not a percentage; to convert from a percentage
        the spec recommends `min_vol + (max_vol - min_vol) × percent / 100` with the zone's
        configured bounds. An unknown room raises ActionRejectedError. A successful reply
        has not been observed: the first reply frame is returned unverified, or None when
        the controller sends nothing within the timeout.
        """
        _check_range("room_id", room_id, 32)
        _check_range("value", value, 8)
        return await self._request_unconfirmed(Opcode.ROOM_SET_VOLUME, struct.pack("<IB", room_id, value))

    async def set_room_mute(self, room_id: int, mute: bool) -> Response | None:
        """Mute or unmute a room's audio zone (spec §52 §3.11, `0x007E`); replies as `set_room_volume`."""
        _check_range("room_id", room_id, 32)
        return await self._request_unconfirmed(Opcode.ROOM_SET_MUTE, struct.pack("<IB", room_id, int(mute)))

    async def set_zone_volume(self, con_dev_id: int, zone_index: int, value: int) -> Response | None:
        """Set one zone of a connected audio device to raw level `value` (spec §52 §3.11, `0x0097`).

        `con_dev_id` is the audio device identifier from `mcs.dat`. Replies are handled as
        in `set_room_volume`.
        """
        _check_range("con_dev_id", con_dev_id, 32)
        _check_range("zone_index", zone_index, 8)
        _check_range("value", value, 8)
        payload = struct.pack("<IBB", con_dev_id, zone_index, value)
        return await self._request_unconfirmed(Opcode.ZONE_SET_VOLUME, payload)

    async def set_zone_mute(self, con_dev_id: int, zone_index: int, mute: bool) -> Response | None:
        """Mute or unmute one zone of a connected audio device (spec §52 §3.11, `0x0098`)."""
        _check_range("con_dev_id", con_dev_id, 32)
        _check_range("zone_index", zone_index, 8)
        payload = struct.pack("<IBB", con_dev_id, zone_index, int(mute))
        return await self._request_unconfirmed(Opcode.ZONE_SET_MUTE, payload)
