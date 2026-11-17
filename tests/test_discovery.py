import asyncio
import errno
import logging
import socket
from ipaddress import IPv4Address

import ifaddr
import pytest

from pyurcmrx import DISCOVERY_PORT, MalformedFrameError, build_probe, discover, parse_find_ack, subnet_broadcast_addresses
from pyurcmrx import discovery

# Spec §50 §4.1 wildcard request and §4.2 sanitized example response, verbatim.
FIND_VECTOR = bytes.fromhex("01 50 C3 FF 0F")
FIND_ACK_VECTOR = bytes.fromhex(
    "01 53 C3 08 80 02 00 00 00 00 01 C0 00 02 2B 00"
    "00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"
    "00 00 00 00 00 00 00 00 00 00 00 00 00 4D 52 58"
    "2D 31 35 00 00 00 00"
)


def make_ack(seq: int = 1, mac: bytes = bytes.fromhex("020000000001")) -> bytes:
    """The §4.2 vector with a different sequence number and MAC."""
    return bytes([seq]) + FIND_ACK_VECTOR[1:5] + mac + FIND_ACK_VECTOR[11:]


def test_probe_matches_spec_vector():
    assert build_probe(1) == FIND_VECTOR
    assert build_probe(1) != bytes.fromhex("01 C3 50 FF 0F")  # byte-swapped form controllers ignore


def test_routed_probe_appends_reply_address():
    assert build_probe(1, reply_ip=IPv4Address("192.0.2.10")) == FIND_VECTOR + bytes.fromhex("C0 00 02 0A")


def test_parse_ack_spec_vector():
    c = parse_find_ack(FIND_ACK_VECTOR)
    assert (c.seq, c.model_id, c.mac, c.ip) == (1, 0x8008, "02:00:00:00:00:01", IPv4Address("192.0.2.43"))
    assert (c.description, c.model_name) == ("", "MRX-15")


def test_parse_ack_trims_padding_and_model_name_whitespace():
    data = FIND_ACK_VECTOR[:0x0F] + b"Main Rack".ljust(30, b"\0") + b"MRX-15 \n\0\0"
    c = parse_find_ack(data)
    assert (c.description, c.model_name) == ("Main Rack", "MRX-15")


@pytest.mark.parametrize(
    "data",
    [FIND_VECTOR, FIND_ACK_VECTOR[:-1], FIND_ACK_VECTOR + bytes(40), FIND_ACK_VECTOR[:1] + b"\xc3\x53" + FIND_ACK_VECTOR[3:]],
    ids=["own_probe", "54_bytes", "95_bytes", "byte_swapped_opcode"],
)
def test_parse_rejects_non_ack(data):
    with pytest.raises(MalformedFrameError):
        parse_find_ack(data)


async def start_responder() -> tuple[asyncio.DatagramTransport, list[tuple[str, int]]]:
    """A controller stand-in that answers each probe on the probe's source port (spec §50 §2.3)."""
    sources: list[tuple[str, int]] = []

    class Responder(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport

        def datagram_received(self, data, addr):
            sources.append(addr)
            seq = data[0]
            self.transport.sendto(make_ack(seq), addr)
            self.transport.sendto(make_ack(seq), addr)  # duplicate reply
            self.transport.sendto(make_ack(seq + 1, mac=bytes(6)), addr)  # stale sequence

    transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(Responder, local_addr=("127.0.0.1", 0))
    return transport, sources


async def test_discover_reads_replies_on_ephemeral_source_port_and_dedupes_by_mac():
    transport, sources = await start_responder()
    try:
        port = transport.get_extra_info("sockname")[1]
        found = await discover(0.2, seq=5, target="127.0.0.1", target_port=port)
    finally:
        transport.close()
    assert [c.mac for c in found] == ["02:00:00:00:00:01"]
    [(_, source_port)] = sources
    assert source_port not in (0, DISCOVERY_PORT)


async def test_discover_defaults_to_subnet_broadcast_addresses(monkeypatch):
    transport, sources = await start_responder()
    monkeypatch.setattr(discovery, "subnet_broadcast_addresses", lambda: ["127.0.0.1"])
    try:
        found = await discover(0.2, target_port=transport.get_extra_info("sockname")[1])
    finally:
        transport.close()
    assert len(sources) == 1
    assert [c.mac for c in found] == ["02:00:00:00:00:01"]


def failing_protocol(failures: int) -> type[discovery._DiscoveryProtocol]:
    class Failing(discovery._DiscoveryProtocol):
        def connection_made(self, transport):
            # Lands after sendto returns, as when a queued datagram fails on a later iteration.
            for _ in range(failures):
                asyncio.get_running_loop().call_later(0.01, self.error_received, OSError(65, "No route to host"))

    return Failing


async def test_discover_survives_a_failed_interface(monkeypatch, caplog):
    transport, sources = await start_responder()
    monkeypatch.setattr(discovery, "subnet_broadcast_addresses", lambda: ["127.0.0.1", "127.0.0.1"])
    monkeypatch.setattr(discovery, "_DiscoveryProtocol", failing_protocol(1))
    try:
        with caplog.at_level(logging.WARNING, logger="pyurcmrx.discovery"):
            found = await discover(0.2, target_port=transport.get_extra_info("sockname")[1])
    finally:
        transport.close()
    assert [c.mac for c in found] == ["02:00:00:00:00:01"]
    assert [r.getMessage() for r in caplog.records] == [
        "a queued discovery probe failed (target unknown): [Errno 65] No route to host"
    ]


async def test_discover_reports_unreachable_interface_and_returns_the_others(monkeypatch, caplog):
    """The LAN broadcast fails at once (as with EHOSTUNREACH on en0) while another interface works."""
    unreachable = "192.0.2.255"

    class Socket(socket.socket):
        def sendto(self, data, *args):
            if args[-1][0] == unreachable:
                raise OSError(errno.EHOSTUNREACH, "No route to host")
            return super().sendto(data, *args)

    transport, sources = await start_responder()
    port = transport.get_extra_info("sockname")[1]
    monkeypatch.setattr(discovery, "subnet_broadcast_addresses", lambda: [unreachable, "127.0.0.1"])
    monkeypatch.setattr(discovery.socket, "socket", Socket)
    try:
        with caplog.at_level(logging.WARNING, logger="pyurcmrx.discovery"):
            found = await discover(0.2, target_port=port)
    finally:
        transport.close()
    assert [c.mac for c in found] == ["02:00:00:00:00:01"]
    assert len(sources) == 1
    [record] = caplog.records
    assert record.levelno == logging.WARNING
    reason = f"[Errno {errno.EHOSTUNREACH}] No route to host"
    assert record.getMessage() == f"discovery probe to {unreachable}:{port} failed: {reason}"


async def test_discover_raises_when_every_interface_fails(monkeypatch):
    monkeypatch.setattr(discovery, "subnet_broadcast_addresses", lambda: ["127.0.0.1", "127.0.0.1"])
    monkeypatch.setattr(discovery, "_DiscoveryProtocol", failing_protocol(2))
    with pytest.raises(OSError, match="No route to host"):
        await discover(0.05, target_port=9)


async def test_discover_raises_when_probe_cannot_be_sent():
    with pytest.raises(OSError):
        await discover(0.1, target="127.0.0.1", target_port=0)


async def test_discover_raises_when_queued_probe_fails_later(monkeypatch):
    monkeypatch.setattr(discovery, "_DiscoveryProtocol", failing_protocol(1))
    with pytest.raises(OSError, match="No route to host"):
        await discover(0.05, target="127.0.0.1", target_port=9)


async def test_discover_raises_without_broadcast_interface(monkeypatch):
    monkeypatch.setattr(discovery, "subnet_broadcast_addresses", lambda: [])
    with pytest.raises(OSError):
        await discover(0.05)


async def test_discover_releases_fixed_port_before_returning():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    for _ in range(2):
        assert await discover(0.05, target="127.0.0.1", target_port=port, bind_port=port) == []


def test_subnet_broadcast_addresses_skip_loopback_point_to_point_and_host_routes(monkeypatch):
    broadcast, loopback, point_to_point = 0x2, 0x8, 0x10
    adapters = {
        "lo0": (loopback, [ifaddr.IP("127.0.0.1", 8, "lo0"), ifaddr.IP(("::1", 0, 0), 128, "lo0")]),
        "en0": (broadcast, [ifaddr.IP("192.168.60.17", 24, "en0"), ifaddr.IP(("fe80::1", 0, 4), 64, "en0")]),
        "en1": (broadcast, [ifaddr.IP("192.168.139.3", 23, "en1"), ifaddr.IP("192.168.60.18", 24, "en1")]),
        "utun0": (point_to_point, [ifaddr.IP("10.8.0.2", 24, "utun0")]),
        "tap0": (broadcast, [ifaddr.IP("10.9.0.1", 32, "tap0"), ifaddr.IP("10.9.1.0", 31, "tap0")]),
        "wifi": (None, [ifaddr.IP("10.0.0.5", 8, "wifi"), ifaddr.IP("127.0.0.2", 8, "wifi")]),  # flags unavailable
    }
    monkeypatch.setattr(
        ifaddr, "get_adapters", lambda: [ifaddr.Adapter(name, name, ips) for name, (_, ips) in adapters.items()]
    )
    monkeypatch.setattr(discovery, "_interface_flags", lambda sock, name: adapters[name][0])
    assert subnet_broadcast_addresses() == ["192.168.60.255", "192.168.139.255", "10.255.255.255"]
