import asyncio
from datetime import UTC, datetime

import pytest

from pyurcmrx import (
    ActionRejectedError,
    ControlClient,
    ControllerTime,
    LevelReport,
    MalformedFrameError,
    Opcode,
    ProtocolError,
    Response,
    ResponseDecoder,
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
WRONG_LENGTH_VOLUME_REQ = bytes.fromhex("02 13 00 ed 07 13 01 02 00 00 00 00 00 64 00 00 00 2d 00 00 00 00")  # §5.4
WRONG_LENGTH_VOLUME_RESP = bytes.fromhex("01 03 00 fe ff 01")
FILE_TIME_REQ = bytes.fromhex("02 02 00 e6 0f")  # §5.6
FILE_TIME_RESP = bytes.fromhex(  # §5.7
    "00 33 00 FD FF 01 F0 6B FC 6A 2C 01 00 00 00 00"
    "0B 00 00 00 01 00 02 00 00 00 00 00 00 00 00 00"
    "00 00 00 00 03 00 00 00 02 00 02 00 00 00 00 00"
    "00 00 C4 FF FF FF"
)
FIRST_REQUEST_DEADLINE_RESP = bytes.fromhex("01 03 00 fe ff 01")  # §5.8
ROOM_POWER_OFF_RESP = bytes.fromhex("00 03 00 0F 00 01")  # §3.3
ROOM_POWER_ON_RESP = bytes.fromhex("00 07 00 14 00 01 00 00 00 00")  # §3.5, unknown room
ROOM_AUDIO_UNKNOWN_ROOM_RESP = bytes.fromhex("00 03 00 FE FF 02")  # §3.11
VARIABLES_REQ = encode_request(Opcode.VARIABLE_TABLE_QUERY)
HOUSE_OFF_REQ = encode_request(Opcode.HOUSE_OFF)
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


def test_encode_trigger_code_matches_spec_example():
    # Spec §52 §5.3 / §53 §3.4.4: object 2, button 275.
    assert encode_trigger_code(2, 275) == 0x00020113 == 131347


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


@pytest.mark.parametrize(
    ("frame", "ok"),
    [
        (NET_STATUS_RESP, True),
        (NET_STATUS_OFFLINE_RESP, False),  # 0xFFFE is negative whatever the status (§2.2)
        (FIRST_REQUEST_DEADLINE_RESP, False),  # §5.8
        (ROOM_AUDIO_UNKNOWN_ROOM_RESP, False),
        (bytes.fromhex("00 03 00 fd ff 02"), False),  # status other than 0x01 is a failure
    ],
)
def test_response_ok_follows_opcode_and_status(frame, ok):
    [response] = ResponseDecoder().feed(frame)
    assert response.ok is ok


class FakeController:
    """Replies to each complete request with a canned response, keyed by opcode.

    A reply given as a list is written one chunk at a time. An opcode without a reply is
    answered with silence and the connection stays open (spec §52 §4 rule 8). After a
    terminal request the controller answers nothing more on that connection and discards
    further bytes until the peer closes (spec §52 §4 rule 3).
    """

    TERMINAL = {0x07DA, 0x07ED, 0x0012, 0x000D, 0x0FE1, 0x089A}

    def __init__(
        self,
        replies: dict[int, bytes | list[bytes]],
        close_after: int | None = None,
        delays: dict[int, float] | None = None,
    ):
        self.replies = replies
        self.close_after = close_after
        self.delays = delays or {}
        self.connections: list[list[bytes]] = []

    @property
    def requests(self) -> list[bytes]:
        return [request for connection in self.connections for request in connection]

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        requests: list[bytes] = []
        self.connections.append(requests)
        try:
            while self.close_after is None or len(requests) < self.close_after:
                header = await reader.readexactly(3)
                rest = await reader.readexactly(int.from_bytes(header[1:3], "little"))
                requests.append(header + rest)
                opcode = int.from_bytes(rest[:2], "little")
                await asyncio.sleep(self.delays.get(opcode, 0))
                reply = self.replies.get(opcode)
                if reply is None:
                    continue
                for chunk in reply if isinstance(reply, list) else [reply]:
                    writer.write(chunk)
                    await writer.drain()
                    await asyncio.sleep(0.01)
                if opcode in self.TERMINAL:
                    while await reader.read(4096):
                        pass
                    break
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
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


async def test_net_status_with_failure_status_raises(serve):
    fake = FakeController({0x0FE1: bytes.fromhex("00 03 00 fd ff 02")})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        with pytest.raises(ActionRejectedError):
            await client.get_net_status()


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
        (bytes.fromhex("00 04 00 dd 07 02 01"), ActionRejectedError),  # status other than 0x01
        (bytes.fromhex("00 03 00 ee 07 01"), ProtocolError),  # opcode not in the sequence
        (MACRO_ACK, ProtocolError),  # a second 0x07DB
    ],
    ids=["generic_negative", "failure_status", "unexpected_opcode", "second_ack"],
)
async def test_macro_action_rejects_unexpected_intermediate_frame(serve, frame, error):
    fake = FakeController({0x07DA: MACRO_ACK + ACTION_START + frame + ACTION_END + MACRO_DONE})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        with pytest.raises(error):
            await client.macro_action(0)


async def test_macro_action_sends_prefix_then_trigger_code(serve):
    # Without the 0x00 prefix the code's low byte is read as the prefix (spec §52 §2.1.1).
    fake = FakeController({0x07DA: MACRO_RESP})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        await client.macro_action(encode_trigger_code(7, 305))
    assert fake.requests == [bytes.fromhex("02 07 00 da 07 00 31 01 07 00")]


@pytest.mark.parametrize("trigger_code", [-1, 1 << 32])
async def test_macro_action_rejects_out_of_range_trigger_code(trigger_code):
    with pytest.raises(ValueError):
        await ControlClient("127.0.0.1").macro_action(trigger_code)


async def test_wrong_length_volume_request_is_rejected(serve):
    # Spec §52 §5.4, sent through the raw request API.
    fake = FakeController({0x07ED: WRONG_LENGTH_VOLUME_RESP})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        with pytest.raises(ActionRejectedError):
            await client.request(0x07ED, WRONG_LENGTH_VOLUME_REQ[5:], Opcode.SET_LEVEL_ABSOLUTE_REPLY)
    assert fake.requests == [WRONG_LENGTH_VOLUME_REQ]


async def test_set_volume_rejects_generic_negative(serve):
    fake = FakeController({0x07ED: WRONG_LENGTH_VOLUME_RESP})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        with pytest.raises(ActionRejectedError):
            await client.set_volume(0x00020113, 45, min_value=0, max_value=100)


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        (bytes.fromhex("00 13 00 ee 07 01") + VOLUME_RESP[6:-1], MalformedFrameError),  # 16-byte body
        (VOLUME_RESP[:-1] + b"\x01", ProtocolError),  # echo differs in mute_status
        (VOLUME_RESP[:5] + b"\x02" + VOLUME_RESP[6:], ActionRejectedError),  # status 0x02
    ],
    ids=["short_body", "echo_mismatch", "failure_status"],
)
async def test_set_volume_checks_echo_reply(serve, reply, error):
    fake = FakeController({0x07ED: reply})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        with pytest.raises(error):
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


async def test_room_power_replies(serve):
    fake = FakeController({0x000D: ROOM_POWER_OFF_RESP, 0x0012: ROOM_POWER_ON_RESP, 0x000E: MACRO_ACK})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        assert await client.room_power_off(1) is None
        assert await client.room_power_on(1) is None
        assert await client.house_off() is None
    assert fake.connections == [
        [bytes.fromhex("02 06 00 0d 00 01 00 00 00")],
        [bytes.fromhex("02 06 00 12 00 01 00 00 00")],
        [HOUSE_OFF_REQ],
    ]


async def test_generic_negative_on_action_raises_rejection(serve):
    # E.g. the controller has not finished initialization (spec §52 §2.3, §4 rule 7).
    fake = FakeController({0x000E: NET_STATUS_OFFLINE_RESP, 0x0012: NET_STATUS_OFFLINE_RESP})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        with pytest.raises(ActionRejectedError):
            await client.house_off()
        with pytest.raises(ActionRejectedError):
            await client.room_power_on(1)


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
    fake = FakeController({0x0FE6: FILE_TIME_RESP, 0x089A: EMPTY_VARIABLE_TABLE, 0x000E: MACRO_ACK})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        await client.get_file_time()
        await client.get_file_time()
        await client.get_variables()
        await client.house_off()
        await client.house_off()
    # Only CONTROLLER_TIME_QUERY is reusable; house off is treated as terminal (spec §52 §4 rules 4-5).
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
    fake = FakeController({0x0FE6: MACRO_ACK, 0x000E: MACRO_ACK})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        with pytest.raises(ProtocolError):
            await client.get_file_time()
        await client.house_off()
    assert fake.connections == [[FILE_TIME_REQ], [HOUSE_OFF_REQ]]


async def test_room_audio_requests_match_spec_layouts(serve):
    # Spec §52 §3.11 layouts; success replies are unobserved, so silence returns None.
    fake = FakeController({})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=0.1) as client:
        assert await client.set_room_volume(3, 45) is None
        assert await client.set_room_mute(3, True) is None
        assert await client.set_zone_volume(2, 0, 45) is None
        assert await client.set_zone_mute(2, 5, False) is None
    assert fake.connections == [
        [bytes.fromhex("02 07 00 7d 00 03 00 00 00 2d")],
        [bytes.fromhex("02 07 00 7e 00 03 00 00 00 01")],
        [bytes.fromhex("02 08 00 97 00 02 00 00 00 00 2d")],
        [bytes.fromhex("02 08 00 98 00 02 00 00 00 05 00")],
    ]


async def test_room_audio_unknown_room_raises_rejection(serve):
    fake = FakeController({0x007D: ROOM_AUDIO_UNKNOWN_ROOM_RESP, 0x0098: ROOM_AUDIO_UNKNOWN_ROOM_RESP})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        with pytest.raises(ActionRejectedError):
            await client.set_room_volume(99, 45)
        with pytest.raises(ActionRejectedError):
            await client.set_zone_mute(2, 0, True)


async def test_room_audio_returns_unverified_reply(serve):
    reply = bytes.fromhex("00 03 00 7d 00 01")  # hypothetical: success replies are not specified
    fake = FakeController({0x007D: reply}, close_after=1)
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        assert await client.set_room_volume(3, 45) == Response(0x00, 0x007D, 0x01, b"")


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.set_room_volume(1 << 32, 0),
        lambda c: c.set_room_volume(0, 256),
        lambda c: c.set_zone_volume(0, 256, 0),
        lambda c: c.set_zone_mute(-1, 0, True),
    ],
)
async def test_room_audio_rejects_out_of_range(call):
    with pytest.raises(ValueError):
        await call(ControlClient("127.0.0.1"))
