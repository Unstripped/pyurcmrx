"""MQTT state telemetry from the controller's broker (spec §00 §5.2, v0.3.0).

The broker (TCP 1883, anonymous) is publish-only from the controller: it reports state
but never accepts commands. Every message is a JSON envelope `{"header": {...}, "body":
...}`. State topics are retained, so subscribing yields the last known values, but the
controller only publishes a zone after it initializes or changes.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import aiomqtt

from .errors import TelemetryParseError

_LOGGER = logging.getLogger(__name__)

MQTT_PORT = 1883
SUBSCRIPTION = "tcsystem/#"

_ROOM_ZONE = re.compile(r"^tcsystem/global/room_event/zone_status/room_id_(\d+)$")
_HDA_ZONE = re.compile(r"^tcsystem/global/hda/zone/(\d+)/(\d+)/$")
_VARIABLE = re.compile(r"^tcsystem/global/variable_status/variable_id_(\d+)$")
_LINK = re.compile(r"^tcsystem/global/room_event/link_status/link_id_(\d+)$")


@dataclass(frozen=True, slots=True)
class RoomZone:
    room_id: int
    power: bool
    volume: int  # 0..100
    min_volume: int
    max_volume: int
    mute: bool
    fixed: bool  # fixed line-level output


@dataclass(frozen=True, slots=True)
class RoomZoneStatus:
    room_id: int
    zones: tuple[RoomZone, ...]


@dataclass(frozen=True, slots=True)
class HdaZoneStatus:
    device_id: int  # core_model rooms[].devices[].obj.id
    zone_idx: int  # 0-based amplifier channel
    power: bool
    mute: bool
    volume: int  # 0..100
    min_volume: int
    max_volume: int
    fixed: bool
    treble: int  # dB, -10..10
    bass: int
    balance: int
    lipsync: int  # ms
    thermal: str
    peak: str
    amp: str
    room_id: int
    room_name: str
    zone_port: str  # e.g. "z1lr"


@dataclass(frozen=True, slots=True)
class VariableStatus:
    variable_id: int
    variable_type: int  # 0 boolean, 1 integer, 2 string
    value: str


@dataclass(frozen=True, slots=True)
class LinkStatus:
    link_id: int
    room_id: int
    room_name: str
    linked_rooms: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class OtherTelemetry:
    """A topic this library does not model (HDA inputs, paging, intercom, ...)."""

    body: Any


Event = RoomZoneStatus | HdaZoneStatus | VariableStatus | LinkStatus | OtherTelemetry


@dataclass(frozen=True, slots=True)
class Telemetry:
    topic: str
    seq: int | None
    timestamp: int | None
    event: Event


def _get(obj: Any, key: str, where: str) -> Any:
    if not isinstance(obj, dict) or key not in obj:
        raise TelemetryParseError(f"{where}: missing {key!r}")
    return obj[key]


def _int(obj: Any, key: str, where: str) -> int:
    value = _get(obj, key, where)
    if isinstance(value, bool):
        raise TelemetryParseError(f"{where}.{key}: expected integer, got bool")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)  # HDA bodies carry numbers as strings
        except ValueError:
            pass
    raise TelemetryParseError(f"{where}.{key}: expected integer, got {value!r}")


def _str(obj: Any, key: str, where: str) -> str:
    value = _get(obj, key, where)
    if not isinstance(value, str):
        raise TelemetryParseError(f"{where}.{key}: expected string, got {value!r}")
    return value


def _flag(obj: Any, key: str, where: str) -> bool:
    value = _int(obj, key, where)
    if value not in (0, 1):
        raise TelemetryParseError(f"{where}.{key}: expected 0 or 1, got {value}")
    return bool(value)


def _on_off(obj: Any, key: str, where: str) -> bool:
    value = _str(obj, key, where)
    if value not in ("on", "off"):
        raise TelemetryParseError(f"{where}.{key}: expected 'on' or 'off', got {value!r}")
    return value == "on"


def _room_zone(raw: Any, where: str) -> RoomZone:
    return RoomZone(
        room_id=_int(raw, "room_id", where),
        power=_flag(raw, "zone_power", where),
        volume=_int(raw, "cur_vol", where),
        min_volume=_int(raw, "min_vol", where),
        max_volume=_int(raw, "max_vol", where),
        mute=_flag(raw, "mute", where),
        fixed=_flag(raw, "fixed", where),
    )


def _hda_zone(device_id: int, zone_idx: int, body: Any) -> HdaZoneStatus:
    volume = _get(body, "volume", "body")
    volume_type = _str(volume, "type", "body.volume")
    if volume_type not in ("variable", "fixed"):
        raise TelemetryParseError(f"body.volume.type: expected 'variable' or 'fixed', got {volume_type!r}")
    return HdaZoneStatus(
        device_id=device_id,
        zone_idx=zone_idx,
        power=_on_off(body, "power", "body"),
        mute=_on_off(body, "mute", "body"),
        volume=_int(volume, "level", "body.volume"),
        min_volume=_int(volume, "min", "body.volume"),
        max_volume=_int(volume, "max", "body.volume"),
        fixed=volume_type == "fixed",
        treble=_int(body, "treble", "body"),
        bass=_int(body, "bass", "body"),
        balance=_int(body, "balance", "body"),
        lipsync=_int(body, "lipsync", "body"),
        thermal=_str(body, "thermal", "body"),
        peak=_str(body, "peak", "body"),
        amp=_str(body, "amp", "body"),
        room_id=_int(body, "roomid", "body"),
        room_name=_str(body, "roomname", "body"),
        zone_port=_str(body, "zoneid", "body"),
    )


def _event(topic: str, body: Any) -> Event:
    if m := _ROOM_ZONE.match(topic):
        if not isinstance(body, list):
            raise TelemetryParseError(f"{topic}: body is not an array")
        return RoomZoneStatus(int(m[1]), tuple(_room_zone(z, f"body[{i}]") for i, z in enumerate(body)))
    if m := _HDA_ZONE.match(topic):
        return _hda_zone(int(m[1]), int(m[2]), body)
    if m := _VARIABLE.match(topic):
        return VariableStatus(int(m[1]), _int(body, "variable_type", "body"), _str(body, "value", "body"))
    if m := _LINK.match(topic):
        linked = _get(body, "linked_rooms", "body")
        if not isinstance(linked, list) or not all(isinstance(r, int) and not isinstance(r, bool) for r in linked):
            raise TelemetryParseError("body.linked_rooms: expected an array of integers")
        return LinkStatus(int(m[1]), _int(body, "roomid", "body"), _str(body, "roomname", "body"), tuple(linked))
    return OtherTelemetry(body)


def parse_telemetry(topic: str, payload: bytes) -> Telemetry | None:
    """Parse one broker message; returns None for an empty payload (retained-state clear)."""
    if not payload:
        return None
    try:
        envelope = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        raise TelemetryParseError(f"{topic}: invalid JSON: {err}") from err
    header = envelope.get("header") if isinstance(envelope, dict) else None
    seq = header.get("seq") if isinstance(header, dict) else None
    timestamp = header.get("time_stamp") if isinstance(header, dict) else None
    return Telemetry(
        topic=topic,
        seq=seq if isinstance(seq, int) else None,
        timestamp=timestamp if isinstance(timestamp, int) else None,
        event=_event(topic, _get(envelope, "body", topic)),
    )


async def subscribe(host: str, port: int = MQTT_PORT) -> AsyncIterator[Telemetry]:
    """Yield parsed telemetry until the connection drops (raises `aiomqtt.MqttError`).

    Malformed messages are logged and skipped so one bad publish cannot end the stream.
    Reconnection is the caller's job.
    """
    async with aiomqtt.Client(host, port=port) as client:
        await client.subscribe(SUBSCRIPTION)
        async for message in client.messages:
            topic = message.topic.value
            try:
                telemetry = parse_telemetry(topic, bytes(message.payload or b""))
            except TelemetryParseError as err:
                _LOGGER.warning("Skipping malformed telemetry: %s", err)
                continue
            if telemetry is not None:
                yield telemetry
