"""Controller discovery over UDP 61510 (spec §50 v0.5.0)."""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
import sys
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Interface

import ifaddr

from .errors import MalformedFrameError

DISCOVERY_PORT = 61510
OPCODE_DISCOVERY_PROBE = 0xC350
OPCODE_DISCOVERY_REPLY = 0xC353
MODEL_WILDCARD = 0x0FFF
DISCOVERY_REPLY_SIZE = 55

_LOGGER = logging.getLogger(__name__)

# Interface flags (identical on Linux and the BSDs) and the SIOCGIFFLAGS request that reads them.
_IFF_BROADCAST = 0x2
_IFF_LOOPBACK = 0x8
_IFF_POINTOPOINT = 0x10
if sys.platform == "linux":
    _SIOCGIFFLAGS: int | None = 0x8913
elif sys.platform == "darwin" or "bsd" in sys.platform:
    _SIOCGIFFLAGS = 0xC0206911  # _IOWR('i', 17, struct ifreq)
else:
    _SIOCGIFFLAGS = None


@dataclass(frozen=True, slots=True)
class DiscoveredController:
    seq: int
    model_id: int
    mac: str
    ip: IPv4Address
    description: str
    model_name: str


def build_probe(seq: int, model_id: int = MODEL_WILDCARD, reply_ip: IPv4Address | None = None) -> bytes:
    """Build a DISCOVERY_PROBE probe (spec §50 §2.1).

    5 bytes, answered with a broadcast to 255.255.255.255; or 9 bytes when `reply_ip` is
    given, answered with a unicast to that address (spec §50 §2.3). A `model_id` whose low
    12 bits are 0xFFF is a wildcard; any other value must equal the controller's model
    identifier exactly, otherwise the controller ignores the probe.
    """
    probe = struct.pack("<BHH", seq & 0xFF, OPCODE_DISCOVERY_PROBE, model_id)
    if reply_ip is not None:
        probe += reply_ip.packed
    return probe


def _text_field(field: bytes) -> str:
    """Decode a DISCOVERY_REPLY text field (spec §50 §2.2.1).

    Read up to the first 0x00 (or the field end), remove trailing 0x0A, 0x0D and ASCII
    whitespace, then decode as UTF-8 with replacement; the encoding of 0x80-0xFF is not
    established.
    """
    return field.split(b"\0", 1)[0].rstrip(b" \t\n\r\x0b\x0c").decode("utf-8", errors="replace")


def parse_discovery_reply(data: bytes) -> DiscoveredController:
    """Parse a 55-byte DISCOVERY_REPLY datagram (spec §50 §2.2)."""
    if len(data) != DISCOVERY_REPLY_SIZE:
        raise MalformedFrameError(f"DISCOVERY_REPLY is {len(data)} bytes, need {DISCOVERY_REPLY_SIZE}")
    seq, opcode, model_id = struct.unpack_from("<BHH", data, 0)
    if opcode != OPCODE_DISCOVERY_REPLY:
        raise MalformedFrameError(f"not a DISCOVERY_REPLY: opcode 0x{opcode:04X}")
    return DiscoveredController(
        seq=seq,
        model_id=model_id,
        mac=":".join(f"{b:02X}" for b in data[0x05:0x0B]),
        ip=IPv4Address(data[0x0B:0x0F]),
        description=_text_field(data[0x0F:0x2D]),
        model_name=_text_field(data[0x2D:0x37]),
    )


def _interface_flags(sock: socket.socket, name: str) -> int | None:
    if _SIOCGIFFLAGS is None:
        return None
    import fcntl

    # struct ifreq: 16-byte name, then the flags short; padded to the largest ifreq size.
    ifreq = struct.pack("16sH", name.encode(), 0).ljust(40, b"\0")
    try:
        return struct.unpack_from("H", fcntl.ioctl(sock.fileno(), _SIOCGIFFLAGS, ifreq), 16)[0]
    except OSError:
        return None


def subnet_broadcast_addresses() -> list[str]:
    """Directed broadcast address of every local IPv4 broadcast interface.

    Loopback and point-to-point interfaces, and networks too small to have a broadcast
    address (/31, /32), are skipped.
    """
    addresses: list[str] = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for adapter in ifaddr.get_adapters():
            flags = _interface_flags(sock, adapter.name)
            if flags is not None and (not flags & _IFF_BROADCAST or flags & (_IFF_LOOPBACK | _IFF_POINTOPOINT)):
                continue
            for ip in adapter.ips:
                if not isinstance(ip.ip, str) or ip.network_prefix >= 31:
                    continue
                interface = IPv4Interface(f"{ip.ip}/{ip.network_prefix}")
                broadcast = str(interface.network.broadcast_address)
                if not interface.is_loopback and broadcast not in addresses:
                    addresses.append(broadcast)
    return addresses


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, seq: int) -> None:
        self._seq = seq
        self.found: dict[str, DiscoveredController] = {}
        self.send_errors: list[OSError] = []
        self.closed: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            controller = parse_discovery_reply(data)
        except MalformedFrameError:
            return
        if controller.seq == self._seq:
            self.found[controller.mac] = controller

    def error_received(self, exc: OSError) -> None:
        # asyncio reports a failed sendto here instead of raising it: synchronously inside
        # transport.sendto() or, for a queued datagram, on a later loop iteration.
        self.send_errors.append(exc)

    def connection_lost(self, exc: Exception | None) -> None:
        if not self.closed.done():
            self.closed.set_result(None)


async def discover(
    timeout: float = 2.0,
    *,
    seq: int = 1,
    target: str | None = None,
    target_port: int = DISCOVERY_PORT,
    bind_port: int = 0,
) -> list[DiscoveredController]:
    """Send a 5-byte DISCOVERY_PROBE and collect DISCOVERY_REPLY replies for `timeout` seconds.

    The probe goes to `target` (a controller's unicast address, a subnet-directed
    broadcast address, or 255.255.255.255; all confirmed, spec §50 §2.3, §4.1), or by
    default to the subnet broadcast address of every local IPv4 interface, as §2.3
    recommends for multi-homed clients instead of one limited-broadcast probe.
    Controllers answer to the probe's UDP source port (spec §50 §2.3), so replies are
    read on the socket that sent it; `bind_port = 0` picks an ephemeral port. Results
    are de-duplicated by MAC address.

    A target whose probe cannot be sent is logged at WARNING with its address and the
    OSError, and the remaining targets are still probed, so controllers on working
    interfaces are returned. A failure that asyncio reports after queueing the datagram
    carries no address and is logged at WARNING as unattributed. Raises OSError when
    every probe failed (e.g. no route to any target), rather than reporting that no
    controller answered.
    """
    targets = [target] if target is not None else subnet_broadcast_addresses()
    if not targets:
        raise OSError("no IPv4 interface with a broadcast address; pass a target")
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
        probe = build_probe(seq)
        for address in targets:
            # An immediate failure lands in send_errors before sendto() returns, and nothing
            # else can append to it until the next await, so new entries belong to `address`.
            errors_before = len(protocol.send_errors)
            transport.sendto(probe, (address, target_port))
            for exc in protocol.send_errors[errors_before:]:
                _LOGGER.warning("discovery probe to %s:%d failed: %s", address, target_port, exc)
        attributed = len(protocol.send_errors)
        if attributed < len(targets):
            await asyncio.sleep(timeout)
        for exc in protocol.send_errors[attributed:]:
            _LOGGER.warning("a queued discovery probe failed (target unknown): %s", exc)
        if len(protocol.send_errors) >= len(targets):
            raise protocol.send_errors[0]
    finally:
        # close() releases the socket on a later loop iteration; wait so the port is free on return.
        transport.close()
        await protocol.closed
    return list(protocol.found.values())
