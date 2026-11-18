import asyncio
from datetime import UTC, datetime

import pytest

from pyurcmrx import (
    ALL_AMPLIFIERS,
    ActionRejectedError,
    AddressRejectedError,
    ControlClient,
    ControllerTime,
    LevelReport,
    MalformedFrameError,
    MuteAction,
    Opcode,
    ProtocolError,
    Response,
    ResponseDecoder,
    RoomAudioControl,
    encode_request,
    encode_trigger_code,
)

# Spec §52 §5 test vectors, verbatim.
NET_STATUS_REQ = bytes.fromhex("02 02 00 e1 0f")  # §5.1
NET_STATUS_RESP = bytes.fromhex("00 03 00 fd ff 01")
NET_STATUS_OFFLINE_RESP = bytes.fromhex("00 03 00 fe ff 01")
MACRO_REQ = bytes.fromhex("02 07 00 da 07 00 00 00 00 00")  # §5.2
MACRO_ACK = bytes.fromhex("01 05 00 db 07 01 01 02")
MACRO_DONE = bytes.fromhex("00 03 00 dc 07 01")
MACRO_RESP = MACRO_ACK + MACRO_DONE
ACTION_START = bytes.fromhex("00 04 00 dd 07 01 01")  # §3.1.2
ACTION_END = bytes.fromhex("00 04 00 dd 07 01 00")
LEVEL_REPORT_RESERVED = bytes.fromhex("02 00 00 00 01 00 00")  # §3.1.3, observed in every report
VOLUME_REQ = bytes.fromhex("02 15 00 ed 07 00 13 01 02 00 00 00 00 00 64 00 00 00 2d 00 00 00 00 00")  # §5.3
VOLUME_RESP = bytes.fromhex("00 14 00 ee 07 01 13 01 02 00 00 00 00 00 64 00 00 00 2d 00 00 00 00")
SHORT_VOLUME_REQ = bytes.fromhex("02 13 00 ed 07 00 13 01 02 00 00 00 00 00 64 00 00 00 2d 00 00 00")  # §5.4
SHORT_VOLUME_RESP = bytes.fromhex("00 14 00 ee 07 01 13 01 02 00 00 00 00 00 64 00 00 00 2d 00 00 00 00")
FILE_TIME_REQ = bytes.fromhex("02 02 00 e6 0f")  # §5.6
FILE_TIME_RESP = bytes.fromhex(  # §5.7
    "00 33 00 FD FF 01 F0 6B FC 6A 2C 01 00 00 00 00"
    "0B 00 00 00 01 00 02 00 00 00 00 00 00 00 00 00"
    "00 00 00 00 03 00 00 00 02 00 02 00 00 00 00 00"
    "00 00 C4 FF FF FF"
)
FIRST_REQUEST_DEADLINE_RESP = bytes.fromhex("01 03 00 fe ff 01")  # §5.8
# §5.9: room 4 -> amplifier 7, zone-output index 2 (z3lr, slot 4); trigger 0x00090113.
ROOM_AUDIO_LEVEL_REQ = bytes.fromhex("02 0a 00 a2 00 00 04 00 00 00 00 28 00")
ROOM_AUDIO_MUTE_ON_REQ = bytes.fromhex("02 0a 00 a2 00 00 04 00 00 00 01 01 00")
ROOM_AUDIO_STEP_UP_REQ = bytes.fromhex("02 0a 00 a2 00 00 04 00 00 00 02 00 00")
ROOM_AUDIO_STEP_DOWN_REQ = bytes.fromhex("02 0a 00 a2 00 00 04 00 00 00 03 00 00")
AUDIO_SET_OK = bytes.fromhex("01 03 00 03 00 01")
TRIGGER_LEVEL_REQ = bytes.fromhex("02 14 00 1b 00 00 13 01 09 00 00 00 00 00 64 00 00 00 28 00 00 00 00")
ZONE_MUTE_ON_REQ = bytes.fromhex("02 09 00 98 00 00 07 00 00 00 02 01")  # §5.10
ZONE_MUTE_OFF_REQ = bytes.fromhex("02 09 00 98 00 00 07 00 00 00 02 00")
ROOM_MUTE_REQ = bytes.fromhex("02 05 00 06 00 00 04 01")
ROOM_MUTE_EXT_REQ = bytes.fromhex("02 08 00 7e 00 00 04 00 00 00 01")
MUTE_OK = bytes.fromhex("00 03 00 03 00 02")  # status 0x02 means success here
ZONE_POWER_ON_REQ = bytes.fromhex("02 10 00 42 01 00 01 00 00 00 07 00 00 00 04 00 00 00 01")  # §5.11
ZONE_POWER_OFF_REQ = ZONE_POWER_ON_REQ[:-1] + b"\x00"
AMP_OK = bytes.fromhex("01 03 00 2c 01 01")
ZONES_MUTE_REQ = bytes.fromhex(  # §5.12
    "02 18 00 4a 01 00 01 01 00 00 00 07 00 00 00 02 00 00 00 04 00 00 00 06 00 00 00"
)
ZONE_SOURCE_REQ = bytes.fromhex(  # §5.13
    "02 30 00 32 01 00 01 00 00 00 01 07 00 00 00 07 00 00 00 7a 33 6c 72"
    "00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"
    "00 00 00 00"
)
JSON_VOLUME_REQ = bytes.fromhex("02 43 00 2e 01 00 07 00 00 00 3a 00") + (  # §5.14
    b'{"type":"set","zone":{"id":"z3lr","ctrl":{"volume":"40"}}}'
)
JSON_MUTE_REQ = bytes.fromhex("02 41 00 2e 01 00 07 00 00 00 38 00") + (
    b'{"type":"set","zone":{"id":"z3lr","ctrl":{"mute":"on"}}}'
)
HOUSE_OFF_REQ = bytes.fromhex("02 02 00 0E 00")  # §5.15
HOUSE_OFF_ACK = bytes.fromhex("00 05 00 DB 07 01 01 02")
HOUSE_OFF_DONE = bytes.fromhex("00 03 00 0F 00 01")
POWER_MENU_REQ = bytes.fromhex("02 06 00 12 00 04 00 00 00")
POWER_MENU_RESP = bytes.fromhex("00 0B 00 14 00 01 00 00 00 00 00 00 00 00")
VOLUME_PRESS_REQ = bytes.fromhex("02 07 00 DA 07 00 13 01 09 00")  # §5.16
ADDRESS_REJECTION = bytes.fromhex("01 03 00 FE FF 01")
ROOM_POWER_OFF_RESP = bytes.fromhex("00 03 00 0F 00 01")  # §3.3
UNKNOWN_ROOM_RESP = bytes.fromhex("00 03 00 FE FF 02")  # §3.11.1
VARIABLES_REQ = encode_request(Opcode.VARIABLE_TABLE_QUERY)
EMPTY_VARIABLE_TABLE = bytes.fromhex("00 07 00 98 08 01 00 00 00 00")


def level_report(level: int, mute: int = 0, body_tail: bytes = LEVEL_REPORT_RESERVED) -> bytes:
    # §3.1.3: `00 0E 00 E1 07 01` + min_level 0, max_level 100, level, mute, 7 reserved bytes.
    body = bytes([0x00, 0x64, level, mute]) + body_tail
    return b"\x00" + (3 + len(body)).to_bytes(2, "little") + (0x07E1).to_bytes(2, "little") + b"\x01" + body


def file_time_reply(rule_size: int) -> bytes:
    body = bytes(4 + rule_size)
    return b"\x00" + (3 + len(body)).to_bytes(2, "little") + (0xFFFD).to_bytes(2, "little") + b"\x01" + body


def test_encode_requests_match_spec_vectors():
    assert encode_request(Opcode.NETWORK_STATUS_QUERY) == NET_STATUS_REQ
    assert encode_request(Opcode.MACRO_TRIGGER, bytes(5)) == MACRO_REQ
    assert encode_request(Opcode.CONTROLLER_TIME_QUERY) == FILE_TIME_REQ
    assert encode_request(Opcode.HOUSE_OFF) == HOUSE_OFF_REQ


def test_encode_trigger_code_matches_spec_example():
    # Spec §52 §5.3 / §53 §3.4.4: object 2, button 275.
    assert encode_trigger_code(2, 275) == 0x00020113 == 131347
    assert encode_trigger_code(9, 275) == 0x00090113  # §5.9


@pytest.mark.parametrize(("object_id", "button_id"), [(1 << 16, 0), (0, 1 << 16), (-1, 0)])
def test_encode_trigger_code_rejects_out_of_range(object_id, button_id):
    with pytest.raises(ValueError):
        encode_trigger_code(object_id, button_id)


def test_decoder_handles_spec_fragmented_read():
    # Spec §52 §5.5: header first, then opcode and status.
    decoder = ResponseDecoder()
    assert decoder.feed(bytes.fromhex("00 03 00")) == []
    [response] = decoder.feed(bytes.fromhex("fd ff 01"))
    assert (response.resp_type, response.opcode, response.status, response.body) == (0x00, 0xFFFD, 0x01, b"")


def test_decoder_splits_two_frame_macro_reply_byte_by_byte():
    # Spec §52 §5.2: the 14-byte reply is two frames.
    decoder = ResponseDecoder()
    out = []
    for b in MACRO_RESP:
        out += decoder.feed(bytes([b]))
    assert [(r.resp_type, r.opcode, r.status, r.body) for r in out] == [
        (0x01, 0x07DB, 0x01, b"\x01\x02"),
        (0x00, 0x07DC, 0x01, b""),
    ]


def test_decoder_splits_coalesced_responses_and_keeps_remainder():
    decoder = ResponseDecoder()
    out = decoder.feed(NET_STATUS_RESP + MACRO_RESP + MACRO_ACK[:4])
    assert [r.opcode for r in out] == [0xFFFD, 0x07DB, 0x07DC]
    assert [r.opcode for r in decoder.feed(MACRO_ACK[4:])] == [0x07DB]


def test_decoder_rejects_length_below_opcode_plus_status():
    with pytest.raises(MalformedFrameError):
        ResponseDecoder().feed(bytes.fromhex("000200fdff"))


class FakeController:
    """Replies to each complete request with a canned response, keyed by opcode.

    A reply given as a list is written one chunk at a time, `chunk_delay` apart. An opcode
    without a reply is answered with silence and the connection stays open (spec §52 §4
    rule 8). After any replied request other than CONTROLLER_TIME_QUERY the controller
    answers nothing more on that connection and discards further bytes until the peer
    closes (spec §52 §4 rules 3-5).

    With `register`, connections are registered per client address as in spec §52 §4 rule
    10: while another connection is registered (or `blocked` is set, standing for a
    connection from another process), a connection's first request is answered with
    `01 03 00 FE FF 01` and not performed. A registration ends just before the final frame
    of 0x07DA, 0x07ED, 0x000D, 0x000E, 0x0FE1 and 0x089A, and otherwise when the
    connection closes.
    """

    REUSABLE = {0x0FE6}
    RELEASE_BEFORE_FINAL = {0x07DA, 0x07ED, 0x000D, 0x000E, 0x0FE1, 0x089A}

    def __init__(
        self,
        replies: dict[int, bytes | list[bytes]],
        close_after: int | None = None,
        delays: dict[int, float] | None = None,
        chunk_delay: float = 0.01,
        register: bool = False,
    ):
        self.replies = replies
        self.close_after = close_after
        self.delays = delays or {}
        self.chunk_delay = chunk_delay
        self.register = register
        self.blocked = False
        self.connections: list[list[bytes]] = []
        self.rejected: list[bytes] = []
        self.rejected_at: list[float] = []
        self.released_at: list[float] = []
        self.performed_at: list[float] = []
        self._holder: list[bytes] | None = None
        self._holder_reader: asyncio.StreamReader | None = None

    @property
    def requests(self) -> list[bytes]:
        return [request for connection in self.connections for request in connection]

    def _release(self, requests: list[bytes]) -> None:
        if self._holder is requests:
            self._holder = None
            self.released_at.append(asyncio.get_running_loop().time())

    def _registered_elsewhere(self) -> bool:
        # A registration ends when its connection closes: the holder's EOF counts as soon as
        # the stream has seen it, even if the holder's handler task has not run since.
        if self._holder is not None and self._holder_reader.at_eof():
            self._release(self._holder)
        return self.blocked or self._holder is not None

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        requests: list[bytes] = []
        self.connections.append(requests)
        loop = asyncio.get_running_loop()
        try:
            while self.close_after is None or len(requests) < self.close_after:
                header = await reader.readexactly(3)
                rest = await reader.readexactly(int.from_bytes(header[1:3], "little"))
                requests.append(header + rest)
                if self.register and len(requests) == 1:
                    if self._registered_elsewhere():
                        self.rejected.append(header + rest)
                        self.rejected_at.append(loop.time())
                        writer.write(ADDRESS_REJECTION)
                        await writer.drain()
                        while await reader.read(4096):
                            pass
                        break
                    self._holder, self._holder_reader = requests, reader
                self.performed_at.append(loop.time())
                opcode = int.from_bytes(rest[:2], "little")
                await asyncio.sleep(self.delays.get(opcode, 0))
                reply = self.replies.get(opcode)
                if reply is None:
                    continue
                chunks = reply if isinstance(reply, list) else [reply]
                for i, chunk in enumerate(chunks):
                    if i == len(chunks) - 1 and opcode in self.RELEASE_BEFORE_FINAL:
                        self._release(requests)
                    writer.write(chunk)
                    await writer.drain()
                    await asyncio.sleep(self.chunk_delay)
                if opcode not in self.REUSABLE:
                    while await reader.read(4096):
                        pass
                    break
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self._release(requests)
        writer.close()


@pytest.fixture
async def serve():
    servers = []

    async def start(fake: FakeController) -> int:
        server = await asyncio.start_server(fake.handle, "127.0.0.1", 0)
        servers.append(server)
        return server.sockets[0].getsockname()[1]

    yield start
    for server in servers:
        server.close()
        await server.wait_closed()


async def test_client_round_trips_spec_vectors(serve):
    fake = FakeController({0x0FE1: NET_STATUS_RESP, 0x07DA: MACRO_RESP, 0x07ED: VOLUME_RESP})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        assert await client.get_net_status() is True
        assert await client.macro_action(0) is None
        assert await client.set_volume(encode_trigger_code(2, 275), 45, min_value=0, max_value=100) is None
    assert fake.connections == [[NET_STATUS_REQ], [MACRO_REQ], [VOLUME_REQ]]


async def test_net_status_offline_reply(serve):
    fake = FakeController({0x0FE1: NET_STATUS_OFFLINE_RESP})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        assert await client.get_net_status() is False


@pytest.mark.parametrize(
    ("reply", "online"),
    [
        (bytes.fromhex("00 03 00 fd ff 02"), True),  # 0xFFFD is positive whatever its status
        (bytes.fromhex("00 03 00 fd ff 00"), True),
        (bytes.fromhex("00 03 00 fe ff 02"), False),  # 0xFFFE is negative whatever its status
    ],
)
async def test_net_status_ignores_reserved_status(serve, reply, online):
    # Spec §52 §2.2, §3.7: an undocumented status must not change the outcome.
    fake = FakeController({0x0FE1: reply})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        assert await client.get_net_status() is online


async def test_macro_action_trigger_code_zero_reads_both_frames_across_segments(serve):
    # Spec §52 §5.2: no action, so 0x07DC follows 0x07DB directly and no level is reported.
    fake = FakeController({0x07DA: [MACRO_ACK, MACRO_DONE], 0x0FE6: FILE_TIME_RESP})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        assert await client.macro_action(0) is None
        # The 0x07DC frame was consumed, so it cannot answer the next request.
        await client.get_file_time()
    assert fake.connections == [[MACRO_REQ], [FILE_TIME_REQ]]


async def test_macro_action_reads_action_sequence_until_done(serve):
    # Spec §52 §3.1.2: 0x07DB, 0x07DD `01`, 0x07DD `00`, 0x07DC; no level reports.
    reply = [MACRO_ACK, ACTION_START, ACTION_END, MACRO_DONE]
    fake = FakeController({0x07DA: reply, 0x0FE6: FILE_TIME_RESP})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        assert await client.macro_action(0) is None
        await client.get_file_time()
    assert fake.connections == [[MACRO_REQ], [FILE_TIME_REQ]]


async def test_macro_action_returns_last_level_report(serve):
    # Spec §52 §3.1.3: clients SHOULD take the level from the last report in the sequence.
    reply = ACTION_START + level_report(0x3F) + level_report(0x3D) + ACTION_END + MACRO_DONE
    fake = FakeController({0x07DA: MACRO_ACK + reply})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        report = await client.macro_action(0)
    assert report == LevelReport(min_level=0, max_level=100, level=0x3D, mute=False, reserved=LEVEL_REPORT_RESERVED)
    assert "reserved" not in repr(report)


async def test_macro_action_decodes_muted_report(serve):
    reply = MACRO_ACK + ACTION_START + level_report(0x3D, mute=1) + ACTION_END + MACRO_DONE
    fake = FakeController({0x07DA: reply})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        report = await client.macro_action(0)
    assert (report.level, report.mute) == (0x3D, True)


async def test_macro_action_handles_frames_split_across_segments(serve):
    sequence = MACRO_ACK + ACTION_START + level_report(0x3F) + level_report(0x3D) + ACTION_END + MACRO_DONE
    chunks = [sequence[i : i + 5] for i in range(0, len(sequence), 5)]
    fake = FakeController({0x07DA: chunks, 0x0FE6: FILE_TIME_RESP})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=2) as client:
        assert (await client.macro_action(0)).level == 0x3D
        await client.get_file_time()
    assert fake.connections == [[MACRO_REQ], [FILE_TIME_REQ]]


@pytest.mark.parametrize(
    "reply",
    [
        MACRO_ACK,
        MACRO_ACK + ACTION_START + level_report(0x3D) + ACTION_END,
    ],
    ids=["ack_only", "no_done_after_action_end"],
)
async def test_macro_action_waits_for_trigger_done(serve, reply):
    # Clients MUST keep reading until 0x07DC (spec §52 §3.1.2); neither 0x07DB nor 0x07DD ends it.
    fake = FakeController({0x07DA: reply})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=0.2) as client:
        with pytest.raises(TimeoutError):
            await client.macro_action(0)


@pytest.mark.parametrize(
    "report",
    [
        level_report(0x3D, body_tail=LEVEL_REPORT_RESERVED[:-1]),  # 10-byte body
        level_report(0x3D, body_tail=LEVEL_REPORT_RESERVED + b"\x00"),  # 12-byte body
        level_report(0x3D, mute=2),  # mute is 0 or 1
    ],
    ids=["short_body", "long_body", "mute_value"],
)
async def test_macro_action_rejects_malformed_level_report(serve, report):
    fake = FakeController({0x07DA: MACRO_ACK + ACTION_START + report + ACTION_END + MACRO_DONE})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        with pytest.raises(MalformedFrameError):
            await client.macro_action(0)


@pytest.mark.parametrize(
    ("frame", "error"),
    [
        (bytes.fromhex("00 03 00 fe ff 01"), ActionRejectedError),  # generic negative mid-sequence
        (bytes.fromhex("00 03 00 ee 07 01"), ProtocolError),  # opcode not in the sequence
        (MACRO_ACK, ProtocolError),  # a second 0x07DB
    ],
    ids=["generic_negative", "unexpected_opcode", "second_ack"],
)
async def test_macro_action_rejects_unexpected_intermediate_frame(serve, frame, error):
    fake = FakeController({0x07DA: MACRO_ACK + ACTION_START + frame + ACTION_END + MACRO_DONE})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        with pytest.raises(error):
            await client.macro_action(0)


async def test_macro_action_ignores_reserved_status_bytes(serve):
    # Spec §52 §2.2: status is per response opcode; an undocumented value changes nothing.
    odd_ack = bytes.fromhex("01 05 00 db 07 02 01 02")
    odd_progress = bytes.fromhex("00 04 00 dd 07 02 01")
    fake = FakeController({0x07DA: odd_ack + odd_progress + level_report(0x3D) + ACTION_END + MACRO_DONE})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        assert (await client.macro_action(0)).level == 0x3D


async def test_macro_action_sends_prefix_then_trigger_code(serve):
    # Without the 0x00 prefix the code's low byte is read as the prefix (spec §52 §2.1.1).
    fake = FakeController({0x07DA: MACRO_RESP})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        await client.macro_action(encode_trigger_code(9, 275))
    assert fake.requests == [VOLUME_PRESS_REQ]


@pytest.mark.parametrize("trigger_code", [-1, 1 << 32])
async def test_macro_action_rejects_out_of_range_trigger_code(trigger_code):
    with pytest.raises(ValueError):
        await ControlClient("127.0.0.1").macro_action(trigger_code)


async def test_short_volume_request_is_echoed_in_full(serve):
    # Spec §52 §5.4: no length check; the missing mute_status is read as zero.
    fake = FakeController({0x07ED: SHORT_VOLUME_RESP})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        response = await client.request(0x07ED, SHORT_VOLUME_REQ[5:], Opcode.SET_LEVEL_ABSOLUTE_REPLY)
    assert response == Response(0x00, 0x07EE, 0x01, SHORT_VOLUME_RESP[6:])
    assert fake.requests == [SHORT_VOLUME_REQ]


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        (bytes.fromhex("00 13 00 ee 07 01") + VOLUME_RESP[6:-1], MalformedFrameError),  # 16-byte body
        (VOLUME_RESP[:-1] + b"\x01", ProtocolError),  # echo differs in mute_status
        (bytes.fromhex("00 03 00 fe ff 01"), ActionRejectedError),  # generic negative, not the rejection frame
    ],
    ids=["short_body", "echo_mismatch", "generic_negative"],
)
async def test_set_volume_checks_echo_reply(serve, reply, error):
    fake = FakeController({0x07ED: reply})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        with pytest.raises(error):
            await client.set_volume(0x00020113, 45, min_value=0, max_value=100)


async def test_set_volume_echo_status_is_reserved(serve):
    fake = FakeController({0x07ED: VOLUME_RESP[:5] + b"\x02" + VOLUME_RESP[6:]})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        await client.set_volume(0x00020113, 45, min_value=0, max_value=100)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"trigger_code": 1 << 32, "level": 45},
        {"trigger_code": 0, "level": -1},
        {"trigger_code": 0, "level": 45, "max_value": 1 << 32},
    ],
)
async def test_set_volume_rejects_values_outside_uint32(kwargs):
    kwargs = {"min_value": 0, "max_value": 100} | kwargs
    with pytest.raises(ValueError):
        await ControlClient("127.0.0.1").set_volume(**kwargs)


async def test_set_volume_passes_raw_device_levels(serve):
    # Volume fields are raw device-dependent values, not a 0..100 percentage (spec §52 §3.2).
    trigger_code, level, low, high = 0x00020113, 200, 10, 250
    echoed = b"".join(v.to_bytes(4, "little") for v in (trigger_code, low, high, level)) + b"\x01"
    reply = bytes.fromhex("00 14 00 ee 07 01") + echoed
    fake = FakeController({0x07ED: reply})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        await client.set_volume(trigger_code, level, min_value=low, max_value=high, mute=True)
    assert fake.requests == [encode_request(0x07ED, b"\x00" + echoed + b"\x00")]


async def test_set_volume_rejects_button_id_bit_12():
    with pytest.raises(ValueError, match="bit 12"):
        await ControlClient("127.0.0.1").set_volume(encode_trigger_code(2, 0x1113), 45, min_value=0, max_value=100)


async def test_room_power_off_reply(serve):
    fake = FakeController({0x000D: ROOM_POWER_OFF_RESP})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        assert await client.room_power_off(1) is None
    assert fake.connections == [[bytes.fromhex("02 06 00 0d 00 01 00 00 00")]]


async def test_house_off_reads_until_macro_finished(serve):
    # Spec §52 §5.15: 0x07DB, 0x07DD pairs while the macro runs, then 0x000F.
    reply = [HOUSE_OFF_ACK, ACTION_START, ACTION_END, ACTION_START, ACTION_END, HOUSE_OFF_DONE]
    fake = FakeController({0x000E: reply, 0x0FE6: FILE_TIME_RESP})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        assert await client.house_off() is None
        await client.get_file_time()  # 0x000F was consumed and the connection closed
    assert fake.connections == [[HOUSE_OFF_REQ], [FILE_TIME_REQ]]


async def test_house_off_timeout_override(serve):
    fake = FakeController({0x000E: [HOUSE_OFF_ACK, ACTION_START, ACTION_END, HOUSE_OFF_DONE]}, chunk_delay=0.1)
    async with ControlClient("127.0.0.1", await serve(fake), timeout=0.15) as client:
        with pytest.raises(TimeoutError):
            await client.house_off()
        await client.house_off(timeout=2)


async def test_house_off_stops_at_ack_only_reply(serve):
    # 0x07DB alone does not end the sequence (spec §52 §3.4: clients MUST read until 0x000F).
    fake = FakeController({0x000E: HOUSE_OFF_ACK})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=0.2) as client:
        with pytest.raises(TimeoutError):
            await client.house_off()


async def test_power_menu_query_returns_raw_body(serve):
    # Spec §52 §5.15: both lists empty; entries are not parsed (§3.5).
    fake = FakeController({0x0012: POWER_MENU_RESP})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        assert await client.power_menu_query(4) == bytes(8)
    assert fake.requests == [POWER_MENU_REQ]


async def test_power_menu_query_keeps_short_body(serve):
    # An earlier revision saw a 4-byte body for an unknown room; the body is bounded by resp_length only.
    fake = FakeController({0x0012: bytes.fromhex("00 07 00 14 00 01 00 00 00 00")})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        assert await client.power_menu_query(99) == bytes(4)


async def test_startup_negative_is_not_an_address_rejection(serve):
    # `00 03 00 FE FF 01` (startup, spec §52 §4 rule 7) is negative but not retried.
    fake = FakeController({0x000E: NET_STATUS_OFFLINE_RESP, 0x0012: NET_STATUS_OFFLINE_RESP})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        with pytest.raises(ActionRejectedError) as house_off:
            await client.house_off()
        with pytest.raises(ActionRejectedError) as power_menu:
            await client.power_menu_query(1)
    assert not isinstance(house_off.value, AddressRejectedError)
    assert not isinstance(power_menu.value, AddressRejectedError)
    assert fake.connections == [[HOUSE_OFF_REQ], [encode_request(0x0012, bytes.fromhex("01 00 00 00"))]]


async def test_terminal_query_sequence_reconnects(serve):
    # Spec §52 §5.6: CONTROLLER_TIME_QUERY after NETWORK_STATUS_QUERY needs a new connection.
    fake = FakeController({0x0FE1: NET_STATUS_RESP, 0x0FE6: FILE_TIME_RESP})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        assert await client.get_net_status()
        await client.get_file_time()
    assert fake.connections == [[NET_STATUS_REQ], [FILE_TIME_REQ]]


async def test_variable_table_query_is_terminal(serve):
    fake = FakeController({0x089A: EMPTY_VARIABLE_TABLE})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        assert await client.get_variables() == {}
        assert await client.get_variables() == {}
    assert fake.connections == [[VARIABLES_REQ], [VARIABLES_REQ]]


async def test_file_time_leaves_connection_reusable(serve):
    house_off = [HOUSE_OFF_ACK, HOUSE_OFF_DONE]
    fake = FakeController({0x0FE6: FILE_TIME_RESP, 0x089A: EMPTY_VARIABLE_TABLE, 0x000E: house_off})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        await client.get_file_time()
        await client.get_file_time()
        await client.get_variables()
        await client.house_off()
        await client.house_off()
    # Only CONTROLLER_TIME_QUERY is reusable (spec §52 §4 rules 4-5).
    assert fake.connections == [[FILE_TIME_REQ, FILE_TIME_REQ, VARIABLES_REQ], [HOUSE_OFF_REQ], [HOUSE_OFF_REQ]]


async def test_client_reconnects_after_controller_drops_reusable_connection(serve):
    fake = FakeController({0x0FE6: FILE_TIME_RESP}, close_after=1)
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        await client.get_file_time()
        await asyncio.sleep(0.05)  # let the FIN arrive
        await client.get_file_time()
    assert fake.connections == [[FILE_TIME_REQ], [FILE_TIME_REQ]]


async def test_file_time_decodes_spec_vector(serve):
    # Spec §52 §5.7.
    assert len(FILE_TIME_RESP) == 54
    fake = FakeController({0x0FE6: FILE_TIME_RESP})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        time = await client.get_file_time()
    assert time == ControllerTime(0x6AFC6BF0, FILE_TIME_RESP[10:])
    # Local wall-clock seconds: formatted as UTC they read the controller's local time.
    assert datetime.fromtimestamp(time.local_timestamp, UTC) == datetime(2026, 11, 17, 15, 0, 0, tzinfo=UTC)
    bias = int.from_bytes(time.time_zone_rule[0x00:0x04], "little", signed=True)
    standard_bias = int.from_bytes(time.time_zone_rule[0x14:0x18], "little", signed=True)
    daylight_bias = int.from_bytes(time.time_zone_rule[0x28:0x2C], "little", signed=True)
    assert (bias, standard_bias, daylight_bias) == (300, 0, -60)
    utc = time.local_timestamp + 60 * (bias + standard_bias)
    assert datetime.fromtimestamp(utc, UTC) == datetime(2026, 11, 17, 20, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize("rule_size", [32, 43, 45], ids=["old_36_byte_body", "short", "long"])
async def test_file_time_body_must_be_48_bytes(serve, rule_size):
    fake = FakeController({0x0FE6: file_time_reply(rule_size)})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        with pytest.raises(MalformedFrameError):
            await client.get_file_time()


async def test_variable_table_decodes_records(serve):
    body = (2).to_bytes(4, "little") + (1).to_bytes(4, "little") + b"\x07" + (9).to_bytes(4, "little") + b"\x00"
    reply = b"\x00" + (3 + len(body)).to_bytes(2, "little") + (0x0898).to_bytes(2, "little") + b"\x01" + body
    fake = FakeController({0x089A: reply})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        assert await client.get_variables() == {1: 7, 9: 0}


async def test_cancelled_request_reply_cannot_answer_next_request(serve):
    # The slow request's reply arrives late; it must not be read as the next answer.
    fake = FakeController({0x0FE6: FILE_TIME_RESP, 0x0FE1: NET_STATUS_OFFLINE_RESP}, delays={0x0FE1: 0.1})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        await client.get_file_time()  # leaves the connection open
        task = asyncio.create_task(client.get_net_status())
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.15)  # the late reply is now on the wire
        assert await client.get_file_time() == ControllerTime(0x6AFC6BF0, FILE_TIME_RESP[10:])
    assert len(fake.connections) == 2


async def test_unexpected_opcode_drops_reusable_connection(serve):
    fake = FakeController({0x0FE6: MACRO_ACK, 0x000E: [HOUSE_OFF_ACK, HOUSE_OFF_DONE]})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        with pytest.raises(ProtocolError):
            await client.get_file_time()
        await client.house_off()
    assert fake.connections == [[FILE_TIME_REQ], [HOUSE_OFF_REQ]]


# Room and amplifier-zone audio commands (spec §52 §3.11, vectors §5.9-§5.14).


async def test_room_audio_vectors(serve):
    fake = FakeController({0x00A2: AUDIO_SET_OK})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        await client.set_room_audio(4, RoomAudioControl.LEVEL, 40)
        await client.set_room_audio(4, RoomAudioControl.MUTE, MuteAction.ON)
        await client.set_room_audio(4, RoomAudioControl.STEP_UP)
        await client.set_room_audio(4, RoomAudioControl.STEP_DOWN)
    # Each on its own connection (spec §52 §3.11.1 lifecycle).
    assert fake.connections == [
        [ROOM_AUDIO_LEVEL_REQ],
        [ROOM_AUDIO_MUTE_ON_REQ],
        [ROOM_AUDIO_STEP_UP_REQ],
        [ROOM_AUDIO_STEP_DOWN_REQ],
    ]


async def test_trigger_level_vector(serve):
    fake = FakeController({0x001B: AUDIO_SET_OK})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        await client.set_trigger_level(0x00090113, 40, min_value=0, max_value=100)
    assert fake.requests == [TRIGGER_LEVEL_REQ]


async def test_mute_success_is_status_0x02(serve):
    # Spec §52 §5.10: `00 03 00 03 00 02` is success; status 0x02 does not make it a failure.
    fake = FakeController({0x0098: MUTE_OK, 0x0006: MUTE_OK, 0x007E: MUTE_OK})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        await client.set_zone_mute(7, 2, MuteAction.ON)
        await client.set_zone_mute(7, 2, MuteAction.OFF)
        await client.set_room_mute(4, MuteAction.ON)
        await client.set_room_mute_ext(4, MuteAction.ON)
    assert fake.connections == [[ZONE_MUTE_ON_REQ], [ZONE_MUTE_OFF_REQ], [ROOM_MUTE_REQ], [ROOM_MUTE_EXT_REQ]]


async def test_amp_zone_power_vector(serve):
    fake = FakeController({0x0142: AMP_OK})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        await client.set_amp_zone_power([(7, 4, True)])
        await client.set_amp_zone_power([(7, 4, False)])
    assert fake.connections == [[ZONE_POWER_ON_REQ], [ZONE_POWER_OFF_REQ]]


async def test_amp_zone_power_packs_every_entry(serve):
    fake = FakeController({0x0142: AMP_OK})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        await client.set_amp_zone_power([(7, 4, True), (8, 15, False)])
    payload = bytes.fromhex("00 02 00 00 00 07 00 00 00 04 00 00 00 01 08 00 00 00 0f 00 00 00 00")
    assert fake.requests == [encode_request(0x0142, payload)]


async def test_amp_zones_mute_vector(serve):
    fake = FakeController({0x014A: AMP_OK})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        await client.set_amp_zones_mute(True, {7: [4, 6]})
    assert fake.requests == [ZONES_MUTE_REQ]


async def test_amp_all_mute_layout(serve):
    # Spec §52 §3.11.7: prefix + mute; [STATIC], no vector.
    fake = FakeController({0x0144: AMP_OK})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        await client.set_amp_all_mute(MuteAction.TOGGLE)
    assert fake.requests == [bytes.fromhex("02 04 00 44 01 00 02")]


async def test_amp_zone_source_vector(serve):
    fake = FakeController({0x0132: AMP_OK})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        await client.set_amp_zone_source(1, 7, [(7, "z3lr")])
    assert fake.requests == [ZONE_SOURCE_REQ]


async def test_amp_json_vectors(serve):
    fake = FakeController({0x012E: AMP_OK})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        await client.send_amp_json(7, {"type": "set", "zone": {"id": "z3lr", "ctrl": {"volume": "40"}}})
        await client.send_amp_json(7, {"type": "set", "zone": {"id": "z3lr", "ctrl": {"mute": "on"}}})
        await client.send_amp_json(ALL_AMPLIFIERS, {"type": "set"})
    assert fake.connections == [
        [JSON_VOLUME_REQ],
        [JSON_MUTE_REQ],
        [bytes.fromhex("02 17 00 2e 01 00 ff ff ff ff 0e 00") + b'{"type":"set"}'],
    ]


@pytest.mark.parametrize(
    ("call", "reply"),
    [
        (lambda c: c.set_room_audio(4, RoomAudioControl.LEVEL, 40), FIRST_REQUEST_DEADLINE_RESP),
        (lambda c: c.set_trigger_level(0x00090113, 40), FIRST_REQUEST_DEADLINE_RESP),
        (lambda c: c.set_zone_mute(7, 2, MuteAction.ON), UNKNOWN_ROOM_RESP),
        (lambda c: c.set_room_mute(99, MuteAction.ON), UNKNOWN_ROOM_RESP),
        (lambda c: c.set_room_mute_ext(99, MuteAction.ON), UNKNOWN_ROOM_RESP),
        (lambda c: c.set_amp_zone_power([(7, 4, True)]), bytes.fromhex("01 03 00 2d 01 01")),
        (lambda c: c.set_amp_zones_mute(True, {7: [4]}), bytes.fromhex("01 03 00 2d 01 01")),
        (lambda c: c.set_amp_zone_source(1, 7, [(7, "z3lr")]), bytes.fromhex("01 03 00 2d 01 01")),
        (lambda c: c.send_amp_json(7, {"type": "set"}), bytes.fromhex("01 03 00 2d 01 01")),
        (lambda c: c.set_amp_all_mute(MuteAction.ON), bytes.fromhex("01 03 00 fe ff 01")),
    ],
    ids=[
        "room_audio",
        "trigger_level",
        "zone_mute",
        "room_mute",
        "room_mute_ext",
        "zone_power_012d",
        "zones_mute_012d",
        "zone_source_012d",
        "json_012d",
        "all_mute_fffe",
    ],
)
async def test_audio_failure_replies_raise_without_retry(serve, call, reply):
    # Spec §52 §3.11.1: 0xFFFE or 0x012D is failure; §3.11 commands are never retried.
    audio_opcodes = (0x00A2, 0x001B, 0x0098, 0x0006, 0x007E, 0x0142, 0x014A, 0x0132, 0x012E, 0x0144)
    fake = FakeController(dict.fromkeys(audio_opcodes, reply))
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1, retry_delay=0.01) as client:
        with pytest.raises(ActionRejectedError) as raised:
            await call(client)
    assert not isinstance(raised.value, AddressRejectedError)
    assert len(fake.connections) == 1


@pytest.mark.parametrize(
    ("call", "opcode", "reply"),
    [
        (lambda c: c.set_room_audio(4, RoomAudioControl.LEVEL, 40), 0x00A2, AMP_OK),
        (lambda c: c.set_zone_mute(7, 2, MuteAction.ON), 0x0098, AMP_OK),
        (lambda c: c.set_amp_zone_power([(7, 4, True)]), 0x0142, AUDIO_SET_OK),
        (lambda c: c.send_amp_json(7, {"type": "set"}), 0x012E, MUTE_OK),
    ],
)
async def test_audio_success_opcode_is_per_command(serve, call, opcode, reply):
    # Success is decided by the opcode documented for the request (spec §52 §3.11.1).
    fake = FakeController({opcode: reply})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        with pytest.raises(ProtocolError):
            await call(client)


async def test_room_and_zone_set_level_layouts(serve):
    # Spec §52 §3.11.10: prefix, then room_id or con_dev_id + zone_index, then value.
    fake = FakeController({})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=0.1) as client:
        assert await client.set_room_volume(3, 45) is None
        assert await client.set_zone_volume(2, 0, 45) is None
    assert fake.connections == [
        [bytes.fromhex("02 08 00 7d 00 00 03 00 00 00 2d")],
        [bytes.fromhex("02 09 00 97 00 00 02 00 00 00 00 2d")],
    ]


async def test_room_and_zone_set_level_unsupported_device_raises(serve):
    # `00 03 00 FE FF 02` for an amplifier or unknown room (spec §52 §3.11.10).
    fake = FakeController({0x007D: UNKNOWN_ROOM_RESP, 0x0097: UNKNOWN_ROOM_RESP})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        with pytest.raises(ActionRejectedError):
            await client.set_room_volume(4, 45)
        with pytest.raises(ActionRejectedError):
            await client.set_zone_volume(7, 2, 45)


async def test_room_set_level_returns_unverified_reply(serve):
    reply = bytes.fromhex("00 03 00 03 00 02")  # hypothetical: the success reply is not established
    fake = FakeController({0x007D: reply}, close_after=1)
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        assert await client.set_room_volume(3, 45) == Response(0x00, 0x0003, 0x02, b"")


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.set_room_volume(1 << 32, 0),
        lambda c: c.set_room_volume(0, 256),
        lambda c: c.set_zone_volume(0, 256, 0),
        lambda c: c.set_room_audio(1 << 32, RoomAudioControl.LEVEL, 0),
        lambda c: c.set_room_audio(4, RoomAudioControl.LEVEL, 1 << 16),
        lambda c: c.set_room_audio(4, RoomAudioControl.MUTE, 3),
        lambda c: c.set_room_audio(4, RoomAudioControl.STEP_UP, 1),
        lambda c: c.set_room_audio(4, 4, 0),
        lambda c: c.set_trigger_level(1 << 32, 40),
        lambda c: c.set_trigger_level(0x00090113, -1),
        lambda c: c.set_room_mute(256, MuteAction.ON),
        lambda c: c.set_room_mute(4, 3),
        lambda c: c.set_room_mute_ext(1 << 32, MuteAction.ON),
        lambda c: c.set_zone_mute(7, 24, MuteAction.ON),
        lambda c: c.set_zone_mute(-1, 0, MuteAction.ON),
        lambda c: c.set_amp_zone_power([]),
        lambda c: c.set_amp_zone_power([(7, 16, True)]),
        lambda c: c.set_amp_zones_mute(True, {}),
        lambda c: c.set_amp_zones_mute(True, {7: []}),
        lambda c: c.set_amp_zones_mute(True, {7: [16]}),
        lambda c: c.set_amp_all_mute(3),
        lambda c: c.set_amp_zone_source(0, 7, [(7, "z3lr")]),
        lambda c: c.set_amp_zone_source(256, 7, [(7, "z3lr")]),
        lambda c: c.set_amp_zone_source(1, 7, []),
        lambda c: c.set_amp_zone_source(1, 7, [(7, "")]),
        lambda c: c.set_amp_zone_source(1, 7, [(7, "z" * 33)]),
        lambda c: c.set_amp_zone_source(1, 7, [(7, "zé")]),
        lambda c: c.set_amp_zone_source(1, 7, [(7, "z3\x00")]),
        lambda c: c.send_amp_json(1 << 32, {"type": "set"}),
    ],
)
async def test_audio_commands_reject_out_of_range(call):
    with pytest.raises(ValueError):
        await call(ControlClient("127.0.0.1"))


# One registered connection per client address (spec §52 §4 rules 10-11, vector §5.16).


async def test_clients_of_one_address_are_serialized(serve):
    # Without serialization the second press would be rejected while the first action runs.
    reply = [MACRO_ACK, ACTION_START, level_report(0x3D), ACTION_END, MACRO_DONE]
    fake = FakeController({0x07DA: reply}, chunk_delay=0.05, register=True)
    port = await serve(fake)
    async with ControlClient("127.0.0.1", port, timeout=2) as a, ControlClient("127.0.0.1", port, timeout=2) as b:
        reports = await asyncio.gather(a.macro_action(0), b.macro_action(0))
    assert [r.level for r in reports] == [0x3D, 0x3D]
    assert fake.rejected == []
    assert fake.connections == [[MACRO_REQ], [MACRO_REQ]]
    assert fake.performed_at[1] >= fake.released_at[0]


async def test_idle_reusable_connection_is_closed_before_another_client_connects(serve):
    # Clients MUST close a 0x0FE6 connection before issuing a request elsewhere (§4 rules 5, 11).
    fake = FakeController({0x0FE6: FILE_TIME_RESP, 0x0FE1: NET_STATUS_RESP}, register=True)
    port = await serve(fake)
    async with (
        ControlClient("127.0.0.1", port, timeout=1) as a,
        ControlClient("127.0.0.1", port, timeout=1, retry_window=0) as b,
    ):
        await a.get_file_time()  # parked open
        assert await b.get_net_status() is True
        await a.get_file_time()  # a reconnects
    assert fake.rejected == []
    assert fake.connections == [[FILE_TIME_REQ], [NET_STATUS_REQ], [FILE_TIME_REQ]]


async def test_address_rejection_is_retried_until_the_action_ends(serve):
    # Spec §52 §5.16: connection A (another process) runs a volume press; B is rejected
    # immediately, closes, and retries on new connections until A's action has finished.
    fake = FakeController(
        {0x07DA: [MACRO_ACK, ACTION_START, ACTION_END, MACRO_DONE], 0x0FE1: NET_STATUS_RESP},
        chunk_delay=0.1,
        register=True,
    )
    port = await serve(fake)
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(VOLUME_PRESS_REQ)
    assert await reader.readexactly(len(MACRO_ACK)) == MACRO_ACK
    async with ControlClient("127.0.0.1", port, timeout=1, retry_delay=0.02, retry_max_delay=0.08) as b:
        assert await b.get_net_status() is True
    writer.close()
    assert fake.rejected and set(fake.rejected) == {NET_STATUS_REQ}
    assert fake.connections[-1] == [NET_STATUS_REQ]
    assert all(connection == [NET_STATUS_REQ] for connection in fake.connections[1:])
    assert fake.performed_at[-1] >= fake.released_at[0]


async def test_address_rejection_backoff_doubles_then_gives_up(serve):
    fake = FakeController({0x0FE1: NET_STATUS_RESP}, register=True)
    fake.blocked = True
    port = await serve(fake)
    client = ControlClient("127.0.0.1", port, timeout=1, retry_delay=0.05, retry_max_delay=0.1, retry_window=0.3)
    with pytest.raises(AddressRejectedError):
        await client.get_net_status()
    # Attempts at about 0, 0.05, 0.15 and 0.25 s; the next would start after 0.3 s.
    assert fake.rejected == [NET_STATUS_REQ] * 4
    gaps = [later - earlier for earlier, later in zip(fake.rejected_at, fake.rejected_at[1:])]
    assert gaps[0] >= 0.05 and gaps[1] >= 0.1 and 0.1 <= gaps[2] < 0.19
    assert isinstance(AddressRejectedError("x"), ActionRejectedError)


@pytest.mark.parametrize(
    ("call", "opcode"),
    [
        (lambda c: c.macro_action(0), 0x07DA),
        (lambda c: c.set_volume(0x00020113, 45, min_value=0, max_value=100), 0x07ED),
        (lambda c: c.room_power_off(1), 0x000D),
        (lambda c: c.house_off(), 0x000E),
        (lambda c: c.power_menu_query(4), 0x0012),
        (lambda c: c.get_variables(), 0x089A),
        (lambda c: c.get_net_status(), 0x0FE1),
        (lambda c: c.get_file_time(), 0x0FE6),
    ],
)
async def test_address_rejection_frame_for_each_documented_opcode(serve, call, opcode):
    # For these opcodes `01 03 00 FE FF 01` can only be the address rejection (spec §52 §2.3).
    fake = FakeController({opcode: ADDRESS_REJECTION})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1, retry_window=0) as client:
        with pytest.raises(AddressRejectedError):
            await call(client)
    assert len(fake.connections) == 1


async def test_rejection_frame_on_reused_connection_is_not_retried(serve):
    # Only a connection's first request can be rejected for the address (spec §52 §4 rule 10).
    fake = FakeController({0x0FE6: FILE_TIME_RESP, 0x089A: ADDRESS_REJECTION})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1, retry_delay=0.01) as client:
        await client.get_file_time()
        with pytest.raises(ActionRejectedError) as raised:
            await client.get_variables()
    assert not isinstance(raised.value, AddressRejectedError)
    assert fake.connections == [[FILE_TIME_REQ, VARIABLES_REQ]]


async def test_no_retry_after_a_normal_reply(serve):
    fake = FakeController({0x0FE1: NET_STATUS_OFFLINE_RESP}, register=True)
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1, retry_delay=0.01) as client:
        assert await client.get_net_status() is False
    assert fake.connections == [[NET_STATUS_REQ]]
