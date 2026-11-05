import asyncio

import pytest

from pyurcmrx import (
    ActionFlags,
    ActionRejectedError,
    ControlClient,
    MalformedFrameError,
    Opcode,
    ProjectIdentity,
    ProtocolError,
    ResponseDecoder,
    encode_request,
)

# Spec §52 §5 test vectors.
NET_STATUS_REQ = bytes.fromhex("020200e10f")
NET_STATUS_RESP = bytes.fromhex("000300fdff01")
MACRO_REQ = bytes.fromhex("021200da07" + "00" * 16)
MACRO_RESP = bytes.fromhex("010500db07010102")


def test_encode_requests_match_spec_vectors():
    assert encode_request(Opcode.GET_NET_STATUS) == NET_STATUS_REQ
    assert encode_request(Opcode.MACRO_ACTION, bytes(16)) == MACRO_REQ


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
    """Replies to each complete request with a canned response, keyed by opcode."""

    def __init__(self, replies: dict[int, bytes], close_after: int | None = None, delays: dict[int, float] | None = None):
        self.replies = replies
        self.close_after = close_after
        self.delays = delays or {}
        self.requests: list[bytes] = []
        self.connections = 0

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.connections += 1
        served = 0
        while True:
            try:
                header = await reader.readexactly(3)
                rest = await reader.readexactly(int.from_bytes(header[1:3], "little"))
                self.requests.append(header + rest)
                opcode = int.from_bytes(rest[:2], "little")
                await asyncio.sleep(self.delays.get(opcode, 0))
                writer.write(self.replies[opcode])
                await writer.drain()
            except (asyncio.IncompleteReadError, ConnectionError):
                break
            served += 1
            if self.close_after is not None and served >= self.close_after:
                break
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
    async with ControlClient("127.0.0.1", await serve(fake)) as client:
        assert await client.get_net_status() is True
        assert await client.macro_action(0, 0, 0) == 0x0201
    assert fake.requests == [NET_STATUS_REQ, MACRO_REQ]


async def test_client_reconnects_after_controller_drops_session(serve):
    fake = FakeController({0x0FE1: NET_STATUS_RESP}, close_after=1)
    async with ControlClient("127.0.0.1", await serve(fake)) as client:
        assert await client.get_net_status()
        await asyncio.sleep(0.05)  # let the FIN arrive, as after the 10 s idle timeout
        assert await client.get_net_status()
    assert fake.connections == 2


async def test_rejected_action_raises(serve):
    fake = FakeController({0x0012: bytes.fromhex("010500db07000000")})
    async with ControlClient("127.0.0.1", await serve(fake)) as client:
        with pytest.raises(ActionRejectedError):
            await client.room_power_on(99)


async def test_variable_table_decodes_records(serve):
    body = (2).to_bytes(4, "little") + (1).to_bytes(4, "little") + b"\x07" + (9).to_bytes(4, "little") + b"\x00"
    reply = b"\x00" + (3 + len(body)).to_bytes(2, "little") + (0x0898).to_bytes(2, "little") + b"\x01" + body
    fake = FakeController({0x089A: reply})
    async with ControlClient("127.0.0.1", await serve(fake)) as client:
        assert await client.get_variables() == {1: 7, 9: 0}


async def test_cancelled_request_reply_cannot_answer_next_request(serve):
    # Both actions ACK with 0x07DB; the slow one is a rejection that arrives late.
    rejected = bytes.fromhex("010500db07000000")
    fake = FakeController({0x0012: rejected, 0x000D: MACRO_RESP}, delays={0x0012: 0.1})
    async with ControlClient("127.0.0.1", await serve(fake)) as client:
        task = asyncio.create_task(client.room_power_on(1))
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.15)  # the late rejection is now on the wire
        assert await client.room_power_off(1) == 0x0201
    assert fake.connections == 2


async def test_unexpected_opcode_drops_session(serve):
    fake = FakeController({0x0FE1: MACRO_RESP, 0x0FE6: file_time_reply(7, bytes(range(32)))})
    async with ControlClient("127.0.0.1", await serve(fake)) as client:
        with pytest.raises(ProtocolError):
            await client.get_net_status()
        assert await client.get_file_time() == ProjectIdentity(7, bytes(range(32)))
    assert fake.connections == 2


def file_time_reply(timestamp: int, token: bytes, trailing: bytes = b"") -> bytes:
    body = timestamp.to_bytes(4, "little") + token + trailing
    return b"\x00" + (3 + len(body)).to_bytes(2, "little") + (0xFFFD).to_bytes(2, "little") + b"\x01" + body


@pytest.mark.parametrize("trailing", [b"", bytes(12)], ids=["resp_length_39", "resp_length_51"])
async def test_file_time_reads_timestamp_and_project_token(serve, trailing):
    token = bytes.fromhex("ab" * 32)
    fake = FakeController({0x0FE6: file_time_reply(1730548800, token, trailing)})
    async with ControlClient("127.0.0.1", await serve(fake)) as client:
        assert await client.get_file_time() == ProjectIdentity(1730548800, token)
    assert fake.requests == [bytes.fromhex("020200e60f")]


async def test_short_file_time_body_is_malformed(serve):
    fake = FakeController({0x0FE6: file_time_reply(1, bytes(31))})
    async with ControlClient("127.0.0.1", await serve(fake)) as client:
        with pytest.raises(MalformedFrameError):
            await client.get_file_time()


async def test_keepalive_holds_idle_session_open(serve):
    fake = FakeController({0x0FE1: NET_STATUS_RESP, 0x000E: MACRO_RESP})
    async with ControlClient("127.0.0.1", await serve(fake), keepalive=0.05) as client:
        await client.house_off()
        await asyncio.sleep(0.18)
        assert await client.house_off() == 0x0201
    probes = fake.requests.count(NET_STATUS_REQ)
    assert 2 <= probes <= 4
    assert fake.connections == 1


async def test_flags_are_encoded_in_macro_action(serve):
    fake = FakeController({0x07DA: MACRO_RESP})
    async with ControlClient("127.0.0.1", await serve(fake)) as client:
        await client.macro_action(0, 7, 275, ActionFlags.HOLD)
    assert fake.requests == [bytes.fromhex("021200da07" "00000000" "07000000" "13010000" "03000000")]
