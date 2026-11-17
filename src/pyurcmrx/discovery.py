"""Controller discovery over UDP 61510 (spec §50)."""

from __future__ import annotations

import asyncio
import socket
import struct
from dataclasses import dataclass
from ipaddress import IPv4Address

from .errors import MalformedFrameError

DISCOVERY_PORT = 61510
BROADCAST_ADDRESS = "255.255.255.255"
OPCODE_FIND = 0xC350
OPCODE_FIND_ACK = 0xC353
MODEL_WILDCARD = 0x0FFF
FIND_ACK_SIZE = 0x3F + 32  # last field: 32-byte description at 0x3F


@dataclass(frozen=True, slots=True)
class DiscoveredController:
    seq: int
    model_id: int
    mac: str
    ip: IPv4Address
    netmask: IPv4Address
    gateway: IPv4Address
    dns: IPv4Address
    model_name: str
    description: str


def build_probe(seq: int, model_id: int = MODEL_WILDCARD, requester_ip: IPv4Address | None = None) -> bytes:
    """Build a PACKET_FIND probe: 5 bytes, or 9 bytes with the requester's IPv4 address."""
    probe = struct.pack(">BH", seq & 0xFF, OPCODE_FIND) + struct.pack("<H", model_id)
    if requester_ip is not None:
        probe += requester_ip.packed
    return probe


def _cstring(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("utf-8", errors="replace")


def parse_find_ack(data: bytes) -> DiscoveredController:
    """Parse a PACKET_FIND_ACK datagram (spec §50 §2.2)."""
    if len(data) < FIND_ACK_SIZE:
        raise MalformedFrameError(f"PACKET_FIND_ACK too short: {len(data)} bytes, need {FIND_ACK_SIZE}")
    seq, opcode = struct.unpack_from(">BH", data, 0)
    if opcode != OPCODE_FIND_ACK:
        raise MalformedFrameError(f"not a PACKET_FIND_ACK: opcode 0x{opcode:04X}")
    (model_id,) = struct.unpack_from("<H", data, 0x03)
    return DiscoveredController(
        seq=seq,
        model_id=model_id,
        mac=":".join(f"{b:02X}" for b in data[0x09:0x0F]),
        ip=IPv4Address(data[0x0F:0x13]),
        netmask=IPv4Address(data[0x13:0x17]),
        gateway=IPv4Address(data[0x17:0x1B]),
        dns=IPv4Address(data[0x1B:0x1F]),
        model_name=_cstring(data[0x1F:0x3F]),
        description=_cstring(data[0x3F:0x5F]),
    )


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, seq: int) -> None:
        self._seq = seq
        self.found: dict[str, DiscoveredController] = {}
        self.send_error: OSError | None = None
        self.closed: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        # Our own broadcast probe loops back on the shared port; it fails the opcode check.
        try:
            controller = parse_find_ack(data)
        except MalformedFrameError:
            return
        if controller.seq == self._seq:
            self.found[controller.mac] = controller

    def error_received(self, exc: OSError) -> None:
        # asyncio reports a failed sendto here instead of raising it.
        if self.send_error is None:
            self.send_error = exc

    def connection_lost(self, exc: Exception | None) -> None:
        if not self.closed.done():
            self.closed.set_result(None)


async def discover(
    timeout: float = 2.0,
    *,
    seq: int = 1,
    target: str = BROADCAST_ADDRESS,
    target_port: int = DISCOVERY_PORT,
    bind_port: int = DISCOVERY_PORT,
) -> list[DiscoveredController]:
    """Broadcast a probe and collect PACKET_FIND_ACK replies for `timeout` seconds.

    Controllers reply to UDP 61510 on the requester (spec §50 §2.2), so the socket
    binds that port by default. Results are de-duplicated by MAC address. Raises
    OSError when the probe cannot be sent (e.g. no route to the target), rather
    than reporting that no controller answered.
    """
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("0.0.0.0", bind_port))
        sock.setblocking(False)
    except OSError:
        sock.close()
        raise
    transport, protocol = await loop.create_datagram_endpoint(lambda: _DiscoveryProtocol(seq), sock=sock)
    try:
        transport.sendto(build_probe(seq), (target, target_port))
        if protocol.send_error is not None:
            raise protocol.send_error
        await asyncio.sleep(timeout)
    finally:
        # close() releases the socket on a later loop iteration; wait so the port is free on return.
        transport.close()
        await protocol.closed
    return list(protocol.found.values())
