import asyncio

import pytest

from pyurcmrx import (
    ActionRejectedError,
    ControlClient,
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
    fake = FakeController({0x0FE1: MACRO_RESP, 0x0FE5: bytes.fromhex("000700fdff01") + (7).to_bytes(4, "little")})
    async with ControlClient("127.0.0.1", await serve(fake)) as client:
        with pytest.raises(ProtocolError):
            await client.get_net_status()
        assert await client.get_file_time() == 7
    assert fake.connections == 2
