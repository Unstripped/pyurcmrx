"""Runtime control protocol over TCP 51510 (spec §52 v0.10.0).

Framing: `type:u8 | length:u16le | opcode:u16le | payload`, where `length` counts the
opcode plus payload (requests) or opcode + status + body (responses); wire size is
`length + 3`. Each connection is half-duplex: one request, then its response frames, and
the controller never sends unsolicited frames on this socket (spec §52 §2.2). Some
requests produce more than one response frame (MACRO_TRIGGER: 0x07DB, then any number of
0x07DD and 0x07E1 frames, then 0x07DC), so frames are split by `resp_length`, never
assumed to be one per read or per request.

Outcome (spec §52 §2.2, §2.3): the response opcode carries the result. Each request lists
the reply opcodes that mean success; `0xFFFE` (and, for the amplifier-zone commands,
`0x012D`) is a failure. The status byte is defined per response opcode, never decides the
outcome, and an undocumented status is treated as reserved: the room and zone mute
commands, for example, report success as `0x0003` with status `0x02`. `resp_type` says
nothing about success either.

Connection lifetime depends on the opcode (spec §52 §4 rules 3-5): only
CONTROLLER_TIME_QUERY is documented as leaving the connection reusable. Every other
request is treated as terminal, so the client closes the connection after the final
response frame and opens a new one for the next request.

One connection per address (spec §52 §4 rules 10 and 11): the controller registers each
connection under the client's IPv4 address, and while one is registered it answers the
first request of any other connection from that address with `01 03 00 FE FF 01` without
performing it. All `ControlClient` instances in one event loop that target the same
`(host, port)` therefore share one connection slot: requests are serialized across them,
and an idle reusable connection held by one is closed before another opens a connection.
Other processes or hosts behind the same address are not coordinated; their connections
are seen only as address rejections, which are retried as described in `ControlClient`.
"""

from __future__ import annotations

import asyncio
import json
import struct
import weakref
from asyncio import StreamWriter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from .errors import ActionRejectedError, AddressRejectedError, MalformedFrameError, ProtocolError

CONTROL_PORT = 51510
FRAME_STANDARD = 0x02
HEADER_SIZE = 3
FILE_TIME_BODY_SIZE = 48  # 4-byte local timestamp + 44-byte time-zone rule
SET_LEVEL_ABSOLUTE_ECHO_SIZE = 17  # 0x07EE body: request payload offsets 0x01-0x11 (spec §52 §3.2)
LEVEL_REPORT_BODY_SIZE = 11  # 0x07E1 body (spec §52 §3.1.3)
# MACRO_TRIGGER, SET_LEVEL_ABSOLUTE and the §3.11 audio payloads start with this byte (spec
# §52 §2.1.1, §3.11.1); other values select a session-establishment path instead.
REQUEST_PREFIX = 0x00
SET_LEVEL_ABSOLUTE_RESERVED_BUTTON_BIT = 0x1000  # must not be set in button_id (spec §52 §3.2)
ZONE_INDEX_MAX = 23  # zone-output index range of a 24-output amplifier (spec §52 §3.11.2)
SLOT_MAX = 15  # controller slot range (spec §52 §3.11.2)
ZONE_ID_SIZE = 32  # NUL-padded zone-ID field of AMP_ZONE_SOURCE_SET (spec §52 §3.11.8)
# `hda_id` of AMP_JSON_COMMAND addressing every amplifier (spec §52 §3.11.9, [STATIC]).
ALL_AMPLIFIERS = 0xFFFFFFFF
# First byte of the address-rejection frame `01 03 00 FE FF 01` (spec §52 §2.3, §4 rule 10).
ADDRESS_REJECTION_RESP_TYPE = 0x01


class Opcode(IntEnum):
    # Success reply to ROOM_AUDIO_SET, TRIGGER_LEVEL_SET and the mute commands (spec §52
    # §3.11.1); the spec gives it no name.
    AUDIO_SET_REPLY = 0x0003
    ROOM_MUTE_SET = 0x0006
    ROOM_POWER_OFF = 0x000D
    HOUSE_OFF = 0x000E
    # Final frame of ROOM_POWER_OFF and HOUSE_OFF (spec §52 §3.3, §3.4); unnamed in the spec.
    ROOM_POWER_OFF_REPLY = 0x000F
    POWER_MENU_QUERY = 0x0012
    POWER_MENU_REPLY = 0x0014  # unnamed in the spec (§3.5)
    TRIGGER_LEVEL_SET = 0x001B
    # Set-level forms that never reach an amplifier zone (spec §52 §3.11.10); unnamed in the spec.
    ROOM_SET_VOLUME = 0x007D
    ROOM_MUTE_SET_EXT = 0x007E
    ZONE_SET_VOLUME = 0x0097
    ZONE_MUTE_SET = 0x0098
    ROOM_AUDIO_SET = 0x00A2
    # Success and failure replies of the amplifier-zone commands (spec §52 §3.11.1); unnamed
    # in the spec. AMP_COMMAND_FAILED has never been observed.
    AMP_COMMAND_REPLY = 0x012C
    AMP_COMMAND_FAILED = 0x012D
    AMP_JSON_COMMAND = 0x012E
    AMP_ZONE_SOURCE_SET = 0x0132
    AMP_ZONE_POWER_SET = 0x0142
    AMP_ALL_MUTE_SET = 0x0144
    AMP_ZONES_MUTE_SET = 0x014A
    MACRO_TRIGGER = 0x07DA
    TRIGGER_ACK = 0x07DB
    # Last MACRO_TRIGGER frame, always sent; follows TRIGGER_ACK directly when the trigger
    # code resolves to no action (spec §52 §3.1.2).
    TRIGGER_DONE = 0x07DC
    # Sent when the triggered action starts (body `01`) and when it ends (body `00`);
    # whether other bodies exist is not established (spec §52 §3.1.2, §6 item 2).
    ACTION_PROGRESS = 0x07DD
    # Zone level after each volume or mute command of the action (spec §52 §3.1.3).
    LEVEL_REPORT = 0x07E1
    SET_LEVEL_ABSOLUTE = 0x07ED
    SET_LEVEL_ABSOLUTE_REPLY = 0x07EE
    VARIABLE_TABLE = 0x0898
    VARIABLE_TABLE_QUERY = 0x089A
    NETWORK_STATUS_QUERY = 0x0FE1
    CONTROLLER_TIME_QUERY = 0x0FE6
    GENERIC_POSITIVE = 0xFFFD
    GENERIC_NEGATIVE = 0xFFFE


class MuteAction(IntEnum):
    """`mute` value of ROOM_AUDIO_SET, the mute commands and AMP_ALL_MUTE_SET (spec §52 §3.11)."""

    OFF = 0
    ON = 1
    TOGGLE = 2


class RoomAudioControl(IntEnum):
    """`control` field of ROOM_AUDIO_SET (spec §52 §3.11.3)."""

    LEVEL = 0  # `value` is the raw zone level
    MUTE = 1  # `value` is a MuteAction
    STEP_UP = 2  # `value` is ignored and sent as 0
    STEP_DOWN = 3  # `value` is ignored and sent as 0


# Requests after which the controller keeps the connection open (spec §52 §4 rule 5). Every
# other opcode is terminal (§4 rules 3 and 4).
REUSABLE_OPCODES = frozenset({Opcode.CONTROLLER_TIME_QUERY})

# Requests for which a `01 03 00 FE FF 01` arriving right after the first request of a
# connection can only be the address rejection (spec §52 §2.3, §4 rule 10). For the §3.11
# commands the same frame is also a command failure, so it is never retried for them.
ADDRESS_REJECTION_OPCODES = frozenset(
    {
        Opcode.MACRO_TRIGGER,
        Opcode.SET_LEVEL_ABSOLUTE,
        Opcode.ROOM_POWER_OFF,
        Opcode.HOUSE_OFF,
        Opcode.POWER_MENU_QUERY,
        Opcode.VARIABLE_TABLE_QUERY,
        Opcode.NETWORK_STATUS_QUERY,
        Opcode.CONTROLLER_TIME_QUERY,
    }
)

# Negative reply opcodes per request (spec §52 §2.3, §3.11.1); every other request has only
# the generic negative response.
_AMP_NEGATIVE = (Opcode.GENERIC_NEGATIVE, Opcode.AMP_COMMAND_FAILED)
_NEGATIVE_REPLIES: dict[int, tuple[int, ...]] = {
    Opcode.AMP_ZONE_POWER_SET: _AMP_NEGATIVE,
    Opcode.AMP_ZONES_MUTE_SET: _AMP_NEGATIVE,
    Opcode.AMP_ALL_MUTE_SET: _AMP_NEGATIVE,
    Opcode.AMP_ZONE_SOURCE_SET: _AMP_NEGATIVE,
    Opcode.AMP_JSON_COMMAND: _AMP_NEGATIVE,
}


@dataclass(frozen=True, slots=True)
class ControllerTime:
    """CONTROLLER_TIME_QUERY reply (spec §52 §3.9).

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
class LevelReport:
    """LEVEL_REPORT (0x07E1) body: a zone's level after one command of an action (spec §52 §3.1.3).

    Sent during actions that command a multi-zone amplifier; none was sent for a
    networked speaker. `level` matched the level the amplifier reported; `min_level` and
    `max_level` (observed 0 and 100) are consistent with the zone's bounds but not
    established as such. `reserved` holds body bytes 0x04-0x0A verbatim (observed
    `02 00 00 00 01 00 00`); their meaning is not established.
    """

    min_level: int
    max_level: int
    level: int
    mute: bool
    reserved: bytes = field(repr=False)


def _parse_level_report(body: bytes) -> LevelReport:
    if len(body) != LEVEL_REPORT_BODY_SIZE:
        raise MalformedFrameError(f"0x07E1 body is {len(body)} bytes, need {LEVEL_REPORT_BODY_SIZE}")
    min_level, max_level, level, mute = body[:4]
    if mute not in (0, 1):
        raise MalformedFrameError(f"0x07E1 mute byte is 0x{mute:02X}, need 0x00 or 0x01")
    return LevelReport(min_level, max_level, level, bool(mute), body[4:])


@dataclass(frozen=True, slots=True)
class Response:
    """One response frame. Whether it is a success depends on the request (spec §52 §2.2)."""

    resp_type: int
    opcode: int
    status: int
    body: bytes


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


def _check_bounds(name: str, value: int, low: int, high: int) -> None:
    if not low <= value <= high:
        raise ValueError(f"{name} must be {low}-{high}, got {value}")


def _is_address_rejection(request_opcode: int, response: Response) -> bool:
    """True for `01 03 00 FE FF 01` answering the first request of a connection (spec §52 §2.3).

    Only for the requests in ADDRESS_REJECTION_OPCODES, where that frame has no other
    source once the request has been sent at connect time; the first byte alone does not
    identify it.
    """
    return (
        request_opcode in ADDRESS_REJECTION_OPCODES
        and response.opcode == Opcode.GENERIC_NEGATIVE
        and response.resp_type == ADDRESS_REJECTION_RESP_TYPE
        and not response.body
    )


def _check_response(request_opcode: int, response: Response, expect: tuple[int, ...]) -> Response:
    """Accept `response` when its opcode is one `expect` lists; the status byte is not consulted."""
    if response.opcode in expect:
        return response
    if response.opcode in _NEGATIVE_REPLIES.get(request_opcode, (Opcode.GENERIC_NEGATIVE,)):
        raise ActionRejectedError(
            f"controller answered opcode 0x{request_opcode:04X} with negative reply 0x{response.opcode:04X}"
            f" (resp_type 0x{response.resp_type:02X}, status 0x{response.status:02X})"
        )
    wanted = " or ".join(f"0x{e:04X}" for e in expect)
    raise ProtocolError(f"expected opcode {wanted}, got 0x{response.opcode:04X}")


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


class _AddressSlot:
    """The one connection this process may have open to a controller (spec §52 §4 rule 11).

    `lock` is held for each request, from connecting until its last frame is read and the
    connection is closed or parked; `idle` is the client whose reusable connection is
    parked open, which must be closed before any other connection is opened (§4 rule 5).
    """

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.idle: ControlClient | None = None


_address_slots: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[tuple[str, int], _AddressSlot]] = (
    weakref.WeakKeyDictionary()
)


def _address_slot(host: str, port: int) -> _AddressSlot:
    """The running loop's slot for `(host, port)`; the key is the address as given, not resolved."""
    slots = _address_slots.setdefault(asyncio.get_running_loop(), {})
    return slots.setdefault((host, port), _AddressSlot())


class ControlClient:
    """Request/response client for one controller.

    Requests are strictly one-at-a-time, and so are the requests of every other
    `ControlClient` for the same `(host, port)` in the same event loop: the controller
    allows one registered connection per client address (spec §52 §4 rules 10-11). Each
    request runs on a fresh connection, except that a connection left open by a reusable
    request (CONTROLLER_TIME_QUERY) carries this client's next request. That connection
    has no idle limit and blocks every other connection from this address while it is
    open, so it is closed before another client opens a connection, and `close()` (or
    `async with`) should release it as soon as it is no longer needed (§4 rule 5).

    Address rejection (spec §52 §4 rules 10-11): when another connection from this address
    is registered (another process, or an action this client stopped reading), the
    controller answers the first request with `01 03 00 FE FF 01` and does not perform it.
    For the requests where that frame can only mean this (ADDRESS_REJECTION_OPCODES), the
    client closes the connection and retries on a new one after `retry_delay` seconds,
    doubling the delay up to `retry_max_delay`, and gives up with AddressRejectedError
    once the next attempt would start more than `retry_window` seconds after the first:
    the spec's MAY-retry with backoff from 250 ms to 2 s, and its SHOULD-give-up after
    about 15 s. `retry_window=0` disables retries. A request that received a normal reply,
    or any §3.11 audio command, is never retried.

    `timeout` bounds each request, from connecting to its last frame; the §3.11 commands
    need at least 3 s when an amplifier does not answer (spec §52 §3.11.1).
    """

    def __init__(
        self,
        host: str,
        port: int = CONTROL_PORT,
        timeout: float = 5.0,
        *,
        retry_delay: float = 0.25,
        retry_max_delay: float = 2.0,
        retry_window: float = 15.0,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.retry_delay = retry_delay
        self.retry_max_delay = retry_max_delay
        self.retry_window = retry_window
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._slot: _AddressSlot | None = None
        self._decoder = ResponseDecoder()
        self._pending: list[Response] = []

    async def __aenter__(self) -> ControlClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def _reset(self) -> StreamWriter | None:
        """Drop the connection synchronously (safe inside cancellation handling)."""
        writer, self._reader, self._writer = self._writer, None, None
        if self._slot is not None and self._slot.idle is self:
            self._slot.idle = None
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

    async def _connect(self, slot: _AddressSlot) -> bool:
        """Ensure an open connection; True when a new one was opened. Caller holds `slot.lock`."""
        if self._connected():
            return False
        await self.close()
        if slot.idle is not None and slot.idle is not self:
            await slot.idle.close()  # one connection per address (spec §52 §4 rules 5 and 11)
        self._slot = slot
        self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
        return True

    async def _send(self, frame: bytes) -> None:
        self._writer.write(frame)
        await self._writer.drain()

    async def _read_response(self) -> Response:
        while not self._pending:
            data = await self._reader.read(4096)
            if not data:
                raise ProtocolError("controller closed the connection")
            self._pending.extend(self._decoder.feed(data))
        return self._pending.pop(0)

    async def request(self, opcode: int, payload: bytes, *expect: int, timeout: float | None = None) -> Response:
        """Send one request and return its single response frame, whose opcode must be in `expect`.

        Errors, retries and connection handling are as in `request_sequence`.
        """
        [response] = await self.request_sequence(opcode, payload, *expect, timeout=timeout)
        return response

    async def request_sequence(
        self,
        opcode: int,
        payload: bytes,
        *expect: int,
        until: int | None = None,
        between: tuple[int, ...] = (),
        timeout: float | None = None,
    ) -> list[Response]:
        """Send one request and return its response frames in order.

        The first frame's opcode must be in `expect`. With `until`, frames are then read
        until one whose opcode is `until`, which is the last frame returned; every frame
        before it must have an opcode in `between`, in any order and number
        (MACRO_TRIGGER: 0x07DD and 0x07E1 until 0x07DC). All frames are read within one
        timeout (`timeout`, default the client's) and before the connection is released,
        because clients must consume every response frame (spec §52 §2.2).

        Success is decided by response opcode alone (spec §52 §2.2): a listed opcode is
        accepted whatever its status byte. A negative reply not listed in `expect`
        (0xFFFE, or 0x012D for the amplifier-zone commands) raises ActionRejectedError;
        an address rejection that outlasts the retry window raises AddressRejectedError
        (see the class docstring). Any other unexpected opcode raises ProtocolError.

        Any interruption (error, timeout, cancellation, wrong reply) leaves the stream
        position unknown, so the connection is dropped: a late reply must never be taken
        as the answer to the next request.
        """
        loop = asyncio.get_running_loop()
        give_up_at = loop.time() + self.retry_window
        delay = self.retry_delay
        while True:
            try:
                return await self._exchange(opcode, payload, expect, until, between, timeout)
            except AddressRejectedError:
                if loop.time() + delay > give_up_at:
                    raise
            await asyncio.sleep(delay)
            delay = min(2 * delay, self.retry_max_delay)

    async def _exchange(
        self,
        opcode: int,
        payload: bytes,
        expect: tuple[int, ...],
        until: int | None,
        between: tuple[int, ...],
        timeout: float | None,
    ) -> list[Response]:
        frame = encode_request(opcode, payload)
        following = between if until is None else (*between, until)
        slot = _address_slot(self.host, self.port)
        async with slot.lock:
            try:
                async with asyncio.timeout(self.timeout if timeout is None else timeout):
                    fresh = await self._connect(slot)
                    await self._send(frame)  # immediately after connecting (spec §52 §4 rule 6)
                    first = await self._read_response()
                    if fresh and _is_address_rejection(opcode, first):
                        raise AddressRejectedError(
                            f"controller did not perform opcode 0x{opcode:04X}: another connection"
                            " from this address is registered"
                        )
                    responses = [_check_response(opcode, first, expect)]
                    while until is not None and responses[-1].opcode != until:
                        responses.append(_check_response(opcode, await self._read_response(), following))
            except BaseException:
                self._reset()
                raise
            if opcode in REUSABLE_OPCODES:
                slot.idle = self
            else:
                await self.close()
            return responses

    async def _request_unconfirmed(self, opcode: int, payload: bytes) -> Response | None:
        """Send a terminal request whose success reply is not established (spec §52 §3.11.10).

        Returns the first reply frame if one arrives within the timeout, or None if the
        controller closes the connection or stays silent. A generic negative reply raises
        ActionRejectedError. Never retried: the request is not in ADDRESS_REJECTION_OPCODES.
        """
        frame = encode_request(opcode, payload)
        slot = _address_slot(self.host, self.port)
        async with slot.lock:
            try:
                async with asyncio.timeout(self.timeout):
                    await self._connect(slot)
                    await self._send(frame)
                try:
                    async with asyncio.timeout(self.timeout):
                        response = await self._read_response()
                except (TimeoutError, ProtocolError):  # silence or close: no reply observed
                    response = None
                if response is not None and response.opcode == Opcode.GENERIC_NEGATIVE:
                    raise ActionRejectedError(
                        f"controller answered opcode 0x{opcode:04X} with negative reply 0xFFFE"
                        f" (resp_type 0x{response.resp_type:02X}, status 0x{response.status:02X})"
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
        first check after startup. Both replies are taken whatever their status byte. An
        address rejection (`01 03 00 FE FF 01`, spec §2.3) is not an offline reply: it is
        retried as described in the class docstring.
        """
        response = await self.request(
            Opcode.NETWORK_STATUS_QUERY, b"", Opcode.GENERIC_POSITIVE, Opcode.GENERIC_NEGATIVE
        )
        return response.opcode == Opcode.GENERIC_POSITIVE

    async def get_file_time(self) -> ControllerTime:
        """Read the controller's local time and time-zone rule (spec §52 §3.9).

        The connection stays open for this client's next request (spec §52 §4 rule 5) and
        blocks other connections from this address while open; close the client when done.
        """
        response = await self.request(Opcode.CONTROLLER_TIME_QUERY, b"", Opcode.GENERIC_POSITIVE)
        if len(response.body) != FILE_TIME_BODY_SIZE:
            raise MalformedFrameError(
                f"CONTROLLER_TIME_QUERY body is {len(response.body)} bytes, need {FILE_TIME_BODY_SIZE}"
            )
        (local_timestamp,) = struct.unpack_from("<I", response.body, 0)
        return ControllerTime(local_timestamp, response.body[4:])

    async def get_variables(self) -> dict[int, int]:
        response = await self.request(Opcode.VARIABLE_TABLE_QUERY, b"", Opcode.VARIABLE_TABLE)
        body = response.body
        if len(body) < 4:
            raise MalformedFrameError("variable table body shorter than 4-byte count")
        (count,) = struct.unpack_from("<I", body, 0)
        if len(body) < 4 + 5 * count:
            raise MalformedFrameError(f"variable table declares {count} entries, body has {len(body)} bytes")
        return {var_id: value for var_id, value in struct.iter_unpack("<IB", body[4 : 4 + 5 * count])}

    async def macro_action(self, trigger_code: int) -> LevelReport | None:
        """Trigger a project button by its trigger code (spec §52 §3.1, MACRO_TRIGGER).

        `trigger_code` is a button `code` from the project model (see
        `encode_trigger_code`). It is sent after the 0x00 prefix byte (spec §52 §2.1.1).
        The controller answers 0x07DB, then, when the code resolves to an action, 0x07DD
        `01`, level reports (0x07E1) and 0x07DD `00`, and always 0x07DC last; every frame
        is read until 0x07DC (spec §52 §3.1.2). For valid buttons 0x07DC arrived up to
        about 1.45 s after the request, within the default 5 s timeout. No frame says
        whether the code exists or the device carried the action out, so returning means
        only that the request was acknowledged. Nothing is written to the connection during
        the action, since any byte releases the press (spec §52 §3.1.4); closing it early
        would neither cancel the action nor release the client address (§4 rule 10).

        A volume button is a press the controller holds for up to 1000 ms, sending the
        device's volume command repeatedly; the send count and the size of each step
        depend on controller and device timing and must not be assumed fixed (spec §52
        §3.1.4). For a sustained change send discrete presses one after another; for an
        amplifier zone prefer `set_room_audio` or `set_trigger_level`. Read the resulting
        level from the return value instead: the last level report of the sequence (spec
        §52 §3.1.3), or None when none arrived (no action, or a device that sends none,
        such as a networked speaker).
        """
        _check_range("trigger_code", trigger_code, 32)
        responses = await self.request_sequence(
            Opcode.MACRO_TRIGGER,
            struct.pack("<BI", REQUEST_PREFIX, trigger_code),
            Opcode.TRIGGER_ACK,
            until=Opcode.TRIGGER_DONE,
            between=(Opcode.ACTION_PROGRESS, Opcode.LEVEL_REPORT),
        )
        reports = [_parse_level_report(r.body) for r in responses if r.opcode == Opcode.LEVEL_REPORT]
        return reports[-1] if reports else None

    async def set_volume(
        self,
        trigger_code: int,
        level: int,
        *,
        min_value: int,
        max_value: int,
        mute: bool = False,
    ) -> None:
        """Set an absolute level through a trigger's connected-device binding (spec §52 §3.2, `0x07ED`).

        `trigger_code` is `(object_id << 16) | button_id`; bit 12 of `button_id` must be
        clear. The controller resolves it to a connected-device binding (room menu, device
        object, module device, then the button's macro) and acts only when the bound
        device runs a qualifying two-way script or defines a command with ID
        `0x1100 + level`; otherwise nothing is sent. It never reaches a multi-zone amplifier
        zone: use `set_trigger_level` or `set_room_audio` there. No device has been
        observed to accept it, and neither `core_model.json` nor the reply shows whether a
        trigger does, so use it only with a trigger the user has confirmed against the
        device.

        `min_value`, `max_value` and `level` are raw device-dependent values, not
        percentages; only `trigger_code` and `level` affect the device. The 0x07EE reply
        echoes request payload offsets 0x01-0x11 whatever the outcome, even for a trigger
        code the project lacks: it confirms framing only, not that the level was applied.
        """
        for name, value in (
            ("trigger_code", trigger_code),
            ("level", level),
            ("min_value", min_value),
            ("max_value", max_value),
        ):
            _check_range(name, value, 32)
        if trigger_code & SET_LEVEL_ABSOLUTE_RESERVED_BUTTON_BIT:
            raise ValueError("button_id bit 12 (0x1000) must not be set for 0x07ED (spec §52 §3.2)")
        payload = struct.pack(
            "<BIIIIBB", REQUEST_PREFIX, trigger_code, min_value, max_value, level, int(mute), 0
        )
        response = await self.request(Opcode.SET_LEVEL_ABSOLUTE, payload, Opcode.SET_LEVEL_ABSOLUTE_REPLY)
        if len(response.body) != SET_LEVEL_ABSOLUTE_ECHO_SIZE:
            raise MalformedFrameError(
                f"0x07EE body is {len(response.body)} bytes, need {SET_LEVEL_ABSOLUTE_ECHO_SIZE}"
            )
        if response.body != payload[1 : 1 + SET_LEVEL_ABSOLUTE_ECHO_SIZE]:
            raise ProtocolError("0x07EE body does not echo the request")

    async def room_power_off(self, room_id: int) -> None:
        """Power a room off (spec §52 §3.3). The reply is the same for an unknown room."""
        _check_range("room_id", room_id, 32)
        await self.request(Opcode.ROOM_POWER_OFF, struct.pack("<I", room_id), Opcode.ROOM_POWER_OFF_REPLY)

    async def house_off(self, *, timeout: float | None = None) -> None:
        """Run the project's system-off macro (spec §52 §3.4, `0x000E`).

        Reads 0x07DB, any number of 0x07DD frames while the macro steps run, and 0x000F
        when it has finished. The run time depends on the project's macro (3.77 s in the
        reference), so `timeout` (default the client's) may need raising for long macros.
        """
        await self.request_sequence(
            Opcode.HOUSE_OFF,
            b"",
            Opcode.TRIGGER_ACK,
            until=Opcode.ROOM_POWER_OFF_REPLY,
            between=(Opcode.ACTION_PROGRESS,),
            timeout=timeout,
        )

    async def power_menu_query(self, room_id: int) -> bytes:
        """Read the global and one room's power-menu lists, raw (spec §52 §3.5, `0x0012`).

        Read-only: it switches nothing on, and no room power-on opcode exists (a room is
        switched on by triggering one of its activity buttons with `macro_action`). The
        returned body is `global_count` u32, the global entries, `room_count` u32 and the
        room's entries; the entry layout is not established, so the body is returned
        unparsed (`00 00 00 00 00 00 00 00` with both lists empty). Which payload offset
        the controller reads `room_id` from is not established; the 4-byte form is sent.
        """
        _check_range("room_id", room_id, 32)
        response = await self.request(Opcode.POWER_MENU_QUERY, struct.pack("<I", room_id), Opcode.POWER_MENU_REPLY)
        return response.body

    # Room and amplifier-zone audio commands (spec §52 §3.11). Each is terminal; success
    # means the controller built and sent the amplifier command, not that the amplifier
    # applied it (§3.11.1): read the resulting state from MQTT zone telemetry, which is
    # published only for zones bound to a room. A failure raises ActionRejectedError and is
    # never retried, because the address rejection and the 0x00A2/0x001B failure share one
    # frame.

    async def set_room_audio(self, room_id: int, control: RoomAudioControl, value: int = 0) -> None:
        """Set level, mute or one step on a room's audio zone (spec §52 §3.11.3, ROOM_AUDIO_SET `0x00A2`).

        The room's audio routing selects the amplifier zone. `value` is the raw zone level
        (0-65535) for LEVEL, a MuteAction for MUTE (TOGGLE is [STATIC]), and must be 0 for
        STEP_UP and STEP_DOWN, which move the zone by the amplifier's own step. To convert
        from a percentage the spec recommends `min_vol + (max_vol - min_vol) × percent / 100`
        with the zone's configured bounds.
        """
        _check_range("room_id", room_id, 32)
        control = RoomAudioControl(control)
        if control is RoomAudioControl.LEVEL:
            _check_range("value", value, 16)
        elif control is RoomAudioControl.MUTE:
            value = MuteAction(value)
        elif value != 0:
            raise ValueError(f"value must be 0 for {control.name}, got {value}")
        payload = struct.pack("<BIBH", REQUEST_PREFIX, room_id, control, value)
        await self.request(Opcode.ROOM_AUDIO_SET, payload, Opcode.AUDIO_SET_REPLY)

    async def set_trigger_level(
        self, trigger_code: int, level: int, *, min_value: int = 0, max_value: int = 0
    ) -> None:
        """Set an absolute level on the zone a trigger binds (spec §52 §3.11.4, TRIGGER_LEVEL_SET `0x001B`).

        `trigger_code` is resolved to a binding as for `set_volume` (spec §52 §3.2 step 1);
        room-menu, device-object and module-device volume triggers reached an amplifier
        zone. For an amplifier, audio switch or similar device (sub-types 0x0100, 0x0103,
        0x0104, 0x0105) `level` is set directly, and only its low byte reaches an
        amplifier; any other device gets the `set_volume` device action. `min_value` and
        `max_value` are sent but ignored by the controller.
        """
        for name, value in (
            ("trigger_code", trigger_code),
            ("level", level),
            ("min_value", min_value),
            ("max_value", max_value),
        ):
            _check_range(name, value, 32)
        payload = struct.pack("<BIIIIB", REQUEST_PREFIX, trigger_code, min_value, max_value, level, 0)
        await self.request(Opcode.TRIGGER_LEVEL_SET, payload, Opcode.AUDIO_SET_REPLY)

    async def set_room_mute(self, room: int, mute: MuteAction) -> None:
        """Mute a room's audio zone (spec §52 §3.11.5, ROOM_MUTE_SET `0x0006`).

        `room` is a room ID of at most 255; use `set_room_mute_ext` for larger IDs. OFF and
        TOGGLE are [STATIC]. An unknown room raises ActionRejectedError.
        """
        _check_range("room", room, 8)
        await self.request(
            Opcode.ROOM_MUTE_SET, struct.pack("<BBB", REQUEST_PREFIX, room, MuteAction(mute)), Opcode.AUDIO_SET_REPLY
        )

    async def set_room_mute_ext(self, room_id: int, mute: MuteAction) -> None:
        """Mute a room's audio zone by 32-bit room ID (spec §52 §3.11.5, ROOM_MUTE_SET_EXT `0x007E`).

        OFF and TOGGLE are [STATIC]. An unknown room raises ActionRejectedError.
        """
        _check_range("room_id", room_id, 32)
        payload = struct.pack("<BIB", REQUEST_PREFIX, room_id, MuteAction(mute))
        await self.request(Opcode.ROOM_MUTE_SET_EXT, payload, Opcode.AUDIO_SET_REPLY)

    async def set_zone_mute(self, con_dev_id: int, zone_index: int, mute: MuteAction) -> None:
        """Mute one amplifier zone by zone-output index (spec §52 §3.11.5, ZONE_MUTE_SET `0x0098`).

        `con_dev_id` is the amplifier's connected-device ID (`mcs.dat` `con_devs[].id`);
        `zone_index` is its 0-based zone-output index (0-23). Needs no room binding, so it
        reaches a zone bound to no room; TOGGLE is [STATIC].
        """
        _check_range("con_dev_id", con_dev_id, 32)
        _check_bounds("zone_index", zone_index, 0, ZONE_INDEX_MAX)
        payload = struct.pack("<BIBB", REQUEST_PREFIX, con_dev_id, zone_index, MuteAction(mute))
        await self.request(Opcode.ZONE_MUTE_SET, payload, Opcode.AUDIO_SET_REPLY)

    async def set_amp_zone_power(self, entries: Sequence[tuple[int, int, bool]]) -> None:
        """Switch amplifier zones on or off (spec §52 §3.11.6, AMP_ZONE_POWER_SET `0x0142`).

        Each entry is `(hda_id, slot, on)`: the amplifier's connected-device ID, the
        controller slot (0-15: `2(n-1)` for `z<n>lr` or `z<n>l`, `2(n-1)+1` for `z<n>r`),
        and the power state. At least one entry is required.
        """
        if not entries:
            raise ValueError("at least one entry is required")
        payload = struct.pack("<BI", REQUEST_PREFIX, len(entries))
        for hda_id, slot, on in entries:
            _check_range("hda_id", hda_id, 32)
            _check_bounds("slot", slot, 0, SLOT_MAX)
            payload += struct.pack("<IIB", hda_id, slot, int(on))
        await self.request(Opcode.AMP_ZONE_POWER_SET, payload, Opcode.AMP_COMMAND_REPLY)

    async def set_amp_zones_mute(self, mute: bool, slots: Mapping[int, Sequence[int]]) -> None:
        """Mute or unmute amplifier zones by slot (spec §52 §3.11.7, AMP_ZONES_MUTE_SET `0x014A`).

        `slots` maps each amplifier's connected-device ID to its controller slots (0-15),
        sent in mapping order; at least one amplifier with at least one slot is required.
        """
        if not slots:
            raise ValueError("at least one amplifier is required")
        payload = struct.pack("<BBI", REQUEST_PREFIX, int(mute), len(slots))
        for hda_id, hda_slots in slots.items():
            _check_range("hda_id", hda_id, 32)
            if not hda_slots:
                raise ValueError(f"amplifier {hda_id} needs at least one slot")
            for slot in hda_slots:
                _check_bounds("slot", slot, 0, SLOT_MAX)
            payload += struct.pack(f"<II{len(hda_slots)}I", hda_id, len(hda_slots), *hda_slots)
        await self.request(Opcode.AMP_ZONES_MUTE_SET, payload, Opcode.AMP_COMMAND_REPLY)

    async def set_amp_all_mute(self, mute: MuteAction) -> None:
        """Mute every zone of the project's master amplifier (spec §52 §3.11.7, AMP_ALL_MUTE_SET `0x0144`).

        [STATIC] only: the layout and replies come from firmware analysis and the command
        has not been exercised against a live controller.
        """
        payload = struct.pack("<BB", REQUEST_PREFIX, MuteAction(mute))
        await self.request(Opcode.AMP_ALL_MUTE_SET, payload, Opcode.AMP_COMMAND_REPLY)

    async def set_amp_zone_source(
        self, input_port: int, source_hda_id: int, targets: Sequence[tuple[int, str]]
    ) -> None:
        """Switch zones on and route them to an amplifier input (spec §52 §3.11.8, AMP_ZONE_SOURCE_SET `0x0132`).

        `input_port` is the 1-based input of amplifier `source_hda_id`; each target is
        `(hda_id, zone_id)` with the amplifier's zone-ID string (`z<n>lr`, `z<n>l`,
        `z<n>r`; ASCII, at most 32 bytes). At least one target is required.

        Only select an input port that has a source device bound in the project: for an
        unbound port the amplifier ignores the command and the controller still answers
        success after about 2 s, so this call cannot detect it. Verify the result from
        zone telemetry.
        """
        _check_bounds("input_port", input_port, 1, 0xFF)
        _check_range("source_hda_id", source_hda_id, 32)
        if not targets:
            raise ValueError("at least one target is required")
        payload = struct.pack("<BIBI", REQUEST_PREFIX, len(targets), input_port, source_hda_id)
        for hda_id, zone_id in targets:
            _check_range("hda_id", hda_id, 32)
            encoded = zone_id.encode("ascii")
            if not 1 <= len(encoded) <= ZONE_ID_SIZE or b"\x00" in encoded:
                raise ValueError(f"zone_id must be 1-{ZONE_ID_SIZE} ASCII characters without NUL, got {zone_id!r}")
            payload += struct.pack(f"<I{ZONE_ID_SIZE}s", hda_id, encoded)
        await self.request(Opcode.AMP_ZONE_SOURCE_SET, payload, Opcode.AMP_COMMAND_REPLY)

    async def send_amp_json(self, hda_id: int, command: Mapping[str, Any]) -> None:
        """Pass a JSON command to an amplifier (spec §52 §3.11.9, AMP_JSON_COMMAND `0x012E`).

        `command` is serialised compactly as UTF-8, for example
        `{"type":"set","zone":{"id":"z3lr","ctrl":{"volume":"40"}}}`; the amplifier's JSON
        grammar is outside the spec, and the controller re-serialises the object, so key
        order is not preserved. `hda_id` is the amplifier's connected-device ID, or
        ALL_AMPLIFIERS ([STATIC]). This is the only command that can set an absolute level
        on a zone no room or trigger binding reaches; the mute form is confirmed, the volume
        form inferred.
        """
        _check_range("hda_id", hda_id, 32)
        encoded = json.dumps(command, separators=(",", ":"), ensure_ascii=False).encode()
        _check_range("json_length", len(encoded), 16)
        payload = struct.pack("<BIH", REQUEST_PREFIX, hda_id, len(encoded)) + encoded
        await self.request(Opcode.AMP_JSON_COMMAND, payload, Opcode.AMP_COMMAND_REPLY)

    async def set_room_volume(self, room_id: int, value: int) -> Response | None:
        """Set a room's audio zone level with `0x007D` (spec §52 §3.11.10). Not for amplifier zones.

        The controller supports it only when the room's zone belongs to a connected device
        of sub-type 0x0100 or 0x0103 ([STATIC]); for any other device, including an
        amplifier, nothing is sent and the reply is `00 03 00 FE FF 02`, which raises
        ActionRejectedError. Its success reply is not established, so the first reply frame
        is returned unverified, or None when the controller sends nothing within the
        timeout. For an amplifier zone use `set_room_audio`.
        """
        _check_range("room_id", room_id, 32)
        _check_range("value", value, 8)
        payload = struct.pack("<BIB", REQUEST_PREFIX, room_id, value)
        return await self._request_unconfirmed(Opcode.ROOM_SET_VOLUME, payload)

    async def set_zone_volume(self, con_dev_id: int, zone_index: int, value: int) -> Response | None:
        """Set one zone of a connected device with `0x0097` (spec §52 §3.11.10). Not for amplifier zones.

        Supported only for connected devices of sub-type 0x0100 or 0x0103 ([STATIC]);
        replies are handled as in `set_room_volume`. For an amplifier zone use
        `send_amp_json` or `set_room_audio`.
        """
        _check_range("con_dev_id", con_dev_id, 32)
        _check_range("zone_index", zone_index, 8)
        _check_range("value", value, 8)
        payload = struct.pack("<BIBB", REQUEST_PREFIX, con_dev_id, zone_index, value)
        return await self._request_unconfirmed(Opcode.ZONE_SET_VOLUME, payload)
