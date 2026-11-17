import asyncio

import pytest

from pyurcmrx import (
    ActionFlags,
    ActionRejectedError,
    ControlClient,
    ControllerTime,
    MalformedFrameError,
    Opcode,
    ProtocolError,
    ResponseDecoder,
    encode_request,
)

# Spec §52 §5 test vectors.
NET_STATUS_REQ = bytes.fromhex("020200e10f")
NET_STATUS_RESP = bytes.fromhex("000300fdff01")
MACRO_REQ = bytes.fromhex("021200da07" + "00" * 16)
MACRO_RESP = bytes.fromhex("010500db07010102")
FILE_TIME_REQ = bytes.fromhex("020200e60f")  # §5.5
VARIABLES_REQ = encode_request(Opcode.VARIABLE_CMD_GET_TABLE)
HOUSE_OFF_REQ = encode_request(Opcode.ROOMS_CMD_HOUSE_OFF)

TIME_ZONE_RULE = bytes(range(0x80, 0x80 + 44))


def file_time_reply(local_timestamp: int = 1794911025, rule: bytes = TIME_ZONE_RULE) -> bytes:
    """GET_FILE_TIME response laid out per spec §52 §3.9."""
    body = local_timestamp.to_bytes(4, "little") + rule
    return b"\x00" + (3 + len(body)).to_bytes(2, "little") + (0xFFFD).to_bytes(2, "little") + b"\x01" + body


def test_encode_requests_match_spec_vectors():
    assert encode_request(Opcode.GET_NET_STATUS) == NET_STATUS_REQ
    assert encode_request(Opcode.MACRO_ACTION, bytes(16)) == MACRO_REQ
    assert encode_request(Opcode.GET_FILE_TIME) == FILE_TIME_REQ


def test_decoder_handles_spec_fragmented_read():
    # Spec §52 §5.4: header first, then opcode and status.
    decoder = ResponseDecoder()
    assert decoder.feed(NET_STATUS_RESP[:3]) == []
    [response] = decoder.feed(NET_STATUS_RESP[3:])
    assert (response.resp_type, response.opcode, response.status, response.body) == (0x00, 0xFFFD, 0x01, b"")


def test_decoder_handles_byte_by_byte_fragments():
    decoder = ResponseDecoder()
    out = []
    for b in MACRO_RESP:
        out += decoder.feed(bytes([b]))
    assert len(out) == 1
    assert (out[0].resp_type, out[0].opcode, out[0].status, out[0].body) == (0x01, 0x07DB, 0x01, b"\x01\x02")


def test_decoder_splits_coalesced_responses_and_keeps_remainder():
    decoder = ResponseDecoder()
    out = decoder.feed(NET_STATUS_RESP + MACRO_RESP + MACRO_RESP[:4])
    assert [r.opcode for r in out] == [0xFFFD, 0x07DB]
    assert [r.opcode for r in decoder.feed(MACRO_RESP[4:])] == [0x07DB]


def test_decoder_rejects_length_below_opcode_plus_status():
    with pytest.raises(MalformedFrameError):
        ResponseDecoder().feed(bytes.fromhex("000200fdff"))


class FakeController:
    """Replies to each complete request with a canned response, keyed by opcode.

    Models spec §52 §4.2: after a terminal query the controller answers nothing more
    on that connection; it discards further bytes until the peer closes.
    """

    TERMINAL = {0x0FE1, 0x089A}

    def __init__(self, replies: dict[int, bytes], close_after: int | None = None, delays: dict[int, float] | None = None):
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
                writer.write(self.replies[opcode])
                await writer.drain()
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
    fake = FakeController({0x0FE1: NET_STATUS_RESP, 0x07DA: MACRO_RESP})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        assert await client.get_net_status() is True
        assert await client.macro_action(0, 0, 0) == 0x0201
    assert fake.connections == [[NET_STATUS_REQ], [MACRO_REQ]]


async def test_net_status_offline_reply(serve):
    fake = FakeController({0x0FE1: bytes.fromhex("000300feff01")})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        assert await client.get_net_status() is False


async def test_terminal_query_sequence_reconnects(serve):
    # Spec §52 §5.5: GET_FILE_TIME after GET_NET_STATUS needs a new connection.
    fake = FakeController({0x0FE1: NET_STATUS_RESP, 0x0FE6: file_time_reply()})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        assert await client.get_net_status()
        await client.get_file_time()
    assert fake.connections == [[NET_STATUS_REQ], [FILE_TIME_REQ]]


async def test_variable_table_query_is_terminal(serve):
    reply = b"\x00" + (7).to_bytes(2, "little") + (0x0898).to_bytes(2, "little") + b"\x01" + bytes(4)
    fake = FakeController({0x089A: reply})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        assert await client.get_variables() == {}
        assert await client.get_variables() == {}
    assert fake.connections == [[VARIABLES_REQ], [VARIABLES_REQ]]


async def test_file_time_leaves_connection_reusable(serve):
    reply = b"\x00" + (7).to_bytes(2, "little") + (0x0898).to_bytes(2, "little") + b"\x01" + bytes(4)
    fake = FakeController({0x0FE6: file_time_reply(), 0x089A: reply, 0x000E: MACRO_RESP})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        await client.get_file_time()
        await client.get_file_time()
        await client.get_variables()
        await client.house_off()
        await client.house_off()
    # Actions are not documented as reusable, so each one closes its connection (spec §52 §4.4).
    assert fake.connections == [[FILE_TIME_REQ, FILE_TIME_REQ, VARIABLES_REQ], [HOUSE_OFF_REQ], [HOUSE_OFF_REQ]]


async def test_client_reconnects_after_controller_drops_reusable_connection(serve):
    fake = FakeController({0x0FE6: file_time_reply()}, close_after=1)
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        await client.get_file_time()
        await asyncio.sleep(0.05)  # let the FIN arrive
        await client.get_file_time()
    assert fake.connections == [[FILE_TIME_REQ], [FILE_TIME_REQ]]


async def test_file_time_parses_54_byte_response(serve):
    reply = file_time_reply(1794911025)
    assert len(reply) == 54
    fake = FakeController({0x0FE6: reply})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        assert await client.get_file_time() == ControllerTime(1794911025, TIME_ZONE_RULE)


@pytest.mark.parametrize("rule_size", [32, 43, 45], ids=["old_36_byte_body", "short", "long"])
async def test_file_time_body_must_be_48_bytes(serve, rule_size):
    fake = FakeController({0x0FE6: file_time_reply(rule=bytes(rule_size))})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        with pytest.raises(MalformedFrameError):
            await client.get_file_time()


async def test_rejected_action_raises(serve):
    fake = FakeController({0x0012: bytes.fromhex("010500db07000000")})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        with pytest.raises(ActionRejectedError):
            await client.room_power_on(99)


async def test_variable_table_decodes_records(serve):
    body = (2).to_bytes(4, "little") + (1).to_bytes(4, "little") + b"\x07" + (9).to_bytes(4, "little") + b"\x00"
    reply = b"\x00" + (3 + len(body)).to_bytes(2, "little") + (0x0898).to_bytes(2, "little") + b"\x01" + body
    fake = FakeController({0x089A: reply})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        assert await client.get_variables() == {1: 7, 9: 0}


async def test_cancelled_request_reply_cannot_answer_next_request(serve):
    # Both actions ACK with 0x07DB; the slow one is a rejection that arrives late.
    rejected = bytes.fromhex("010500db07000000")
    fake = FakeController({0x0012: rejected, 0x000D: MACRO_RESP}, delays={0x0012: 0.1})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        task = asyncio.create_task(client.room_power_on(1))
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.15)  # the late rejection is now on the wire
        assert await client.room_power_off(1) == 0x0201
    assert len(fake.connections) == 2


async def test_unexpected_opcode_drops_reusable_connection(serve):
    fake = FakeController({0x0FE6: MACRO_RESP, 0x000E: MACRO_RESP})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        with pytest.raises(ProtocolError):
            await client.get_file_time()
        assert await client.house_off() == 0x0201
    assert fake.connections == [[FILE_TIME_REQ], [HOUSE_OFF_REQ]]


async def test_flags_are_encoded_in_macro_action(serve):
    fake = FakeController({0x07DA: MACRO_RESP})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        await client.macro_action(0, 7, 275, ActionFlags.HOLD)
    assert fake.requests == [bytes.fromhex("021200da07" "00000000" "07000000" "13010000" "03000000")]


async def test_set_volume_matches_spec_vector(serve):
    fake = FakeController({0x07ED: MACRO_RESP})
    async with ControlClient("127.0.0.1", await serve(fake), timeout=1) as client:
        assert await client.set_volume(2, 45) == 0x0201
    # Spec §52 §5.3 request vector, verbatim.
    assert fake.requests == [bytes.fromhex("02 15 00 ed 07 13 01 02 00 00 00 00 00 64 00 00 00 2d 00 00 00 00 00 00")]


@pytest.mark.parametrize("kwargs", [{"level": 101}, {"level": 50, "max_value": 40}, {"level": 5, "min_value": 10}])
async def test_set_volume_rejects_out_of_range(kwargs):
    with pytest.raises(ValueError):
        await ControlClient("127.0.0.1").set_volume(2, **kwargs)
