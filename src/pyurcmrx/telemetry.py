"""MQTT state telemetry from the controller's broker (spec §00 §5.2, v0.5.1).

The broker (TCP 1883, anonymous) is publish-only from the controller: it reports state
but never accepts commands. Every message is a JSON envelope `{"header": {...}, "body":
...}` published at QoS 0. Retained values were observed for `zone_status`, `hda/zone`,
`hda/input` and `hda/ampstatus`, but no complete startup snapshot is guaranteed: the
broker keeps no persistence, a retained value may be stale, and a topic without one has
unknown state until the controller publishes an update.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import aiomqtt

from .errors import TelemetryParseError

_LOGGER = logging.getLogger(__name__)

MQTT_PORT = 1883
SUBSCRIPTION = "tcsystem/#"

_ROOM_ZONE = re.compile(r"^tcsystem/global/room_event/zone_status/room_id_(\d+)$")
_HDA_ZONE = re.compile(r"^tcsystem/global/hda/zone/(\d+)/(\d+)/$")
_HDA_INPUT = re.compile(r"^tcsystem/global/hda/input/(\d+)/(\d+)/$")
_HDA_AMP = re.compile(r"^tcsystem/global/hda/ampstatus/(\d+)/(\d+)/$")
_VARIABLE = re.compile(r"^tcsystem/global/variable_status/variable_id_(\d+)$")
_LINK = re.compile(r"^tcsystem/global/room_event/link_status/link_id_(\d+)$")


@dataclass(frozen=True, slots=True)
class RoomZoneStatus:
    """Room-level audio state; the room identity comes from the topic suffix.

    Volume units and ranges are device-dependent and not established by the spec.
    """

    room_id: int
    power: bool
    volume: int
    min_volume: int
    max_volume: int
    mute: bool
    fixed: bool  # fixed line-level output


@dataclass(frozen=True, slots=True)
class HdaZoneStatus:
    """Amplifier-zone detail; a room-level view of the same change arrives as `RoomZoneStatus`.

    No precedence between the two is specified. The topic `zone` is not `room_index`, and
    its mapping to amplifier zones is not established. Fields the spec lists without
    semantics (tone, source, ducking, ...) are only in `body`.
    """

    hda: int  # topic {hda}; observed equal to the amplifier's mcs.dat con_devs[].id
    zone: int  # topic {zone}
    power: bool | None  # None for a value other than "on"/"off"
    mute: bool | None
    volume: int  # units and ranges unverified
    min_volume: int
    max_volume: int
    fixed: bool | None  # volume.type "fixed"; None for a value other than "variable"/"fixed"
    amp_peak: str  # "normal" observed
    amp_thermal: str
    room_id: int  # body roomid; observed equal to core_model data.rooms[].id
    room_index: str  # meaning unresolved
    hda_id: str  # body hdaid
    zone_id: str  # body zoneid, e.g. "z1lr"
    body: dict[str, Any] = field(repr=False, hash=False)


@dataclass(frozen=True, slots=True)
class HdaInputStatus:
    hda: int  # topic {hda}
    input: int  # topic {input}
    audio_sense: str  # "not_sensing" observed
    hda_id: str  # body hdaid
    input_port: str  # body inputport; observed equal to the topic {input}
    body: dict[str, Any] = field(repr=False, hash=False)


@dataclass(frozen=True, slots=True)
class HdaAmpStatus:
    """Amplifier channel health; optional, so clients must not depend on receiving it.

    The topic `zone` does not map to the `HdaZoneStatus` zone in any established way.
    """

    hda: int  # topic {hda}
    zone: int  # topic {zone}
    left_peak: str  # "normal" observed
    left_thermal: str
    right_peak: str
    right_thermal: str
    hda_id: str  # body hdaid
    zone_id: str  # body zoneid; observed equal to the topic {zone}
    body: dict[str, Any] = field(repr=False, hash=False)


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
    """A topic this library does not model (paging, intercom, camera popups, ...)."""

    body: Any


Event = (
    RoomZoneStatus | HdaZoneStatus | HdaInputStatus | HdaAmpStatus | VariableStatus | LinkStatus | OtherTelemetry
)


@dataclass(frozen=True, slots=True)
class Telemetry:
    topic: str
    seq: int | None  # observed always 0; never use it for ordering or deduplication
    timestamp: int | None  # publication time in seconds; observed as local wall-clock, not UTC
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


def _text(obj: Any, key: str, where: str) -> str:
    """An HDA scalar: observed as a string, but other representations may occur."""
    value = _get(obj, key, where)
    if isinstance(value, str):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    raise TelemetryParseError(f"{where}.{key}: expected string, got {value!r}")


def _flag(obj: Any, key: str, where: str) -> bool:
    value = _int(obj, key, where)
    if value not in (0, 1):
        raise TelemetryParseError(f"{where}.{key}: expected 0 or 1, got {value}")
    return bool(value)


def _on_off(obj: Any, key: str, where: str) -> bool | None:
    return {"on": True, "off": False}.get(_text(obj, key, where))


def _object(body: Any, where: str) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise TelemetryParseError(f"{where}: body is not an object")
    return body


def _room_zone(room_id: int, body: Any) -> RoomZoneStatus:
    return RoomZoneStatus(
        room_id=room_id,
        power=_flag(body, "zone_power", "body"),
        volume=_int(body, "cur_vol", "body"),
        min_volume=_int(body, "min_vol", "body"),
        max_volume=_int(body, "max_vol", "body"),
        mute=_flag(body, "mute", "body"),
        fixed=_flag(body, "fixed", "body"),
    )


def _hda_zone(hda: int, zone: int, body: dict[str, Any]) -> HdaZoneStatus:
    volume = _get(body, "volume", "body")
    amp = _get(body, "amp", "body")
    return HdaZoneStatus(
        hda=hda,
        zone=zone,
        power=_on_off(body, "power", "body"),
        mute=_on_off(body, "mute", "body"),
        volume=_int(volume, "level", "body.volume"),
        min_volume=_int(volume, "min", "body.volume"),
        max_volume=_int(volume, "max", "body.volume"),
        fixed={"fixed": True, "variable": False}.get(_text(volume, "type", "body.volume")),
        amp_peak=_text(amp, "peak", "body.amp"),
        amp_thermal=_text(amp, "thermal", "body.amp"),
        room_id=_int(body, "roomid", "body"),
        room_index=_text(body, "roomindex", "body"),
        hda_id=_text(body, "hdaid", "body"),
        zone_id=_text(body, "zoneid", "body"),
        body=body,
    )


def _hda_input(hda: int, input_: int, body: dict[str, Any]) -> HdaInputStatus:
    return HdaInputStatus(
        hda=hda,
        input=input_,
        audio_sense=_text(body, "audiosense", "body"),
        hda_id=_text(body, "hdaid", "body"),
        input_port=_text(body, "inputport", "body"),
        body=body,
    )


def _hda_amp(hda: int, zone: int, body: dict[str, Any]) -> HdaAmpStatus:
    return HdaAmpStatus(
        hda=hda,
        zone=zone,
        left_peak=_text(body, "leftpeak", "body"),
        left_thermal=_text(body, "leftthermal", "body"),
        right_peak=_text(body, "rightpeak", "body"),
        right_thermal=_text(body, "rightthermal", "body"),
        hda_id=_text(body, "hdaid", "body"),
        zone_id=_text(body, "zoneid", "body"),
        body=body,
    )


def _event(topic: str, body: Any) -> Event:
    if m := _ROOM_ZONE.match(topic):
        return _room_zone(int(m[1]), _object(body, topic))
    if m := _HDA_ZONE.match(topic):
        return _hda_zone(int(m[1]), int(m[2]), _object(body, topic))
    if m := _HDA_INPUT.match(topic):
        return _hda_input(int(m[1]), int(m[2]), _object(body, topic))
    if m := _HDA_AMP.match(topic):
        return _hda_amp(int(m[1]), int(m[2]), _object(body, topic))
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
        seq=seq if isinstance(seq, int) and not isinstance(seq, bool) else None,
        timestamp=timestamp if isinstance(timestamp, int) and not isinstance(timestamp, bool) else None,
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
