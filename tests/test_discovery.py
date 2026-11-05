import asyncio
from ipaddress import IPv4Address

import pytest

from pyurcmrx import MalformedFrameError, build_probe, discover, parse_find_ack


def make_ack(seq: int = 1, mac: bytes = bytes.fromhex("001FB8001122")) -> bytes:
    """PACKET_FIND_ACK laid out per spec §50 Table 2.2."""
    return (
        bytes([seq])
        + bytes.fromhex("C353")
        + (0x8000).to_bytes(2, "little")
        + bytes(4)
        + mac
        + IPv4Address("192.168.1.100").packed
        + IPv4Address("255.255.255.0").packed
        + IPv4Address("192.168.1.1").packed
        + IPv4Address("192.168.1.1").packed
        + b"MRX-15".ljust(32, b"\0")
        + b"MRX-15 Master".ljust(32, b"\0")
    )


def test_probe_matches_spec_vector():
    assert build_probe(1) == bytes.fromhex("01C350FF0F")


def test_probe_with_requester_ip_is_nine_bytes():
    assert build_probe(1, requester_ip=IPv4Address("192.168.1.20")) == bytes.fromhex("01C350FF0FC0A80114")


def test_parse_ack_fields():
    c = parse_find_ack(make_ack())
    assert (c.seq, c.model_id, c.mac) == (1, 0x8000, "00:1F:B8:00:11:22")
    assert (c.ip, c.netmask, c.gateway) == (IPv4Address("192.168.1.100"), IPv4Address("255.255.255.0"), IPv4Address("192.168.1.1"))
    assert (c.model_name, c.description) == ("MRX-15", "MRX-15 Master")


def test_parse_rejects_own_probe_and_truncated_ack():
    with pytest.raises(MalformedFrameError):
        parse_find_ack(build_probe(1))
    with pytest.raises(MalformedFrameError):
        parse_find_ack(make_ack()[:-1])


async def test_discover_collects_matching_seq_and_dedupes_by_mac():
    class Responder(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport

        def datagram_received(self, data, addr):
            seq = data[0]
            self.transport.sendto(make_ack(seq), addr)
            self.transport.sendto(make_ack(seq), addr)  # duplicate reply
            self.transport.sendto(make_ack(seq + 1, mac=bytes(6)), addr)  # stale sequence

    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(Responder, local_addr=("127.0.0.1", 0))
    try:
        port = transport.get_extra_info("sockname")[1]
        found = await discover(0.2, seq=5, target="127.0.0.1", target_port=port, bind_port=0)
    finally:
        transport.close()
    assert [c.mac for c in found] == ["00:1F:B8:00:11:22"]
