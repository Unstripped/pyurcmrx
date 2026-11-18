"""MQTT state telemetry from the controller's broker (spec §00 §5.2, v0.7.0).

The broker (TCP 1883, anonymous) is publish-only from the controller: it reports state
but never accepts commands. Every message is a JSON envelope `{"header": {...}, "body":
...}` published at QoS 0. `zone_status`, `hda/zone`, `hda/input` and `hda/ampstatus` are
retained, but no complete startup snapshot is guaranteed: the broker keeps no persistence,
so the retained set holds only values published since it started, a retained value may be
stale, and a topic without one has unknown state until the controller publishes an update.

For a room whose audio is an HDA zone, `hda/zone` and `zone_status` are rendered from the
same amplifier-zone record. On an incremental change `hda/zone` is published first, then
`zone_status`, normally with the same time stamp; `zone_status` is re-published on any zone
change, even when its own fields are unchanged. A full amplifier status refresh
(registration or reconnect) publishes `hda/zone` for every room-bound slot, `hda/input` for
every input and `hda/ampstatus` for every slot, but no `zone_status`. Rooms without an HDA
zone publish only `zone_status` [STATIC].
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator, Callable
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

    The body is always one object. When several amplifier zones are bound to one room (for
    example both halves of a split stereo zone), each zone's change publishes its own object
    to the same topic and the most recent publish wins [STATIC]. For an HDA-backed room
    prefer `HdaZoneStatus`; this is the only source for rooms without an HDA zone. Volume
    units and general range are device-dependent and not established.
    """

    room_id: int
    power: bool
    volume: int
    min_volume: int
    # For an HDA-backed room, the amplifier's configured maximum, which can differ from the
    # constant `HdaZoneStatus.max_volume` [STATIC].
    max_volume: int
    mute: bool
    # Raw output-type value != 0: fixed output. Only 0 was observed; values above 1 are not
    # excluded [STATIC].
    fixed: bool


@dataclass(frozen=True, slots=True)
class HdaZoneStatus:
    """Amplifier-zone detail for one controller slot (spec §00 §5.2.2).

    The topic `zone` is a 0-based controller slot: for stereo zone n (1-based) of the
    amplifier, slot 2(n-1) carries `z<n>lr` (unsplit) or `z<n>l` (split left half), and
    slot 2(n-1)+1 carries `z<n>r` (split right half only) [STATIC]. It is not `mcs.dat`
    `zones[].idx`; identify the amplifier channel by `zone_id`. A slot is published only
    while it is bound to a room and the amplifier is online.

    For an HDA-backed room this is authoritative over `RoomZoneStatus`, which renders the
    same record. Value sets and presence rules come from firmware analysis [STATIC]; split
    ("mono") and ducking-active bodies have not been observed live. Members the spec lists
    as always present but this library does not require are None when absent; enumerated
    text maps to None for an unknown value.
    """

    hda: int  # topic {hda}: the amplifier's connected-device ID (mcs.dat con_devs[].id)
    zone: int  # topic {zone}: 0-based controller slot
    power: bool | None  # "on"/"off"
    mute: bool | None  # "on"/"off"
    volume: int  # volume.level; 0-100 observed
    min_volume: int  # volume.min: the constant 0, not the amplifier's configured limit
    max_volume: int  # volume.max: the constant 100; see RoomZoneStatus.max_volume
    fixed: bool | None  # volume.type "fixed"/"variable"
    amp_peak: str  # "normal" or "peaking"
    amp_thermal: str  # "normal" or "thermal overload"
    room_id: int  # roomid: the bound room's data.rooms[].id, the zone_status topic room_id
    # roomindex: 0-based position of the bound room in the project's room list; equal to
    # room_id only when room IDs are consecutive from 0, and never to be equated with `zone`.
    room_index: int
    hda_id: str  # hdaid, equal to the topic {hda}
    zone_id: str  # zoneid "z<n>lr", "z<n>l" or "z<n>r": the authoritative zone identity
    room_name: str | None  # roomname
    stereo: bool | None  # "stereo" (unsplit zone) or "mono" (split zone half)
    balance: int | None  # signed
    bass: int | None  # signed
    treble: int | None  # signed
    lipsync: int | None  # lip-sync delay setting
    # "Off", "Music", "Voice", "Gaming" or the custom preset's `eq_custom_name`; absent for
    # an unknown preset.
    eq: str | None
    eq_custom_name: str | None  # eqcustomname, default "Custom"
    ducking: bool | None  # ducking.status "on"/"off": a ducking event is active on this zone
    ducking_mute: bool | None  # ducking.mute "on"/"off"; only while ducking
    ducking_event_id: int | None  # ducking.eventid; only while ducking
    ducking_input_name: str | None  # ducking.inputname; only while ducking and the event is in the project
    # Zone audio source. Analog input n: the bound device's name, the owning amplifier's
    # connected-device ID and n, naming topic hda/input/{input_hda_id}/{input_port}/.
    # Microphone: its name, amplifier and port. Chime n: the sound name (if found), this
    # amplifier and n. All None for no or an unrecognised source or a failed lookup.
    input_device_name: str | None  # inputdevname
    input_hda_id: str | None  # inputhdaid
    input_port: str | None  # inputhdaport
    body: dict[str, Any] = field(repr=False, hash=False)


@dataclass(frozen=True, slots=True)
class HdaInputStatus:
    """One amplifier analog input (spec §00 §5.2.2).

    Published for every input on a full amplifier status refresh and for one input when its
    status changes, never on zone changes [STATIC]. Preamplifier/microphone state is on the
    separate `hda/mic/{hda}/{mic}/` topic, delivered as `OtherTelemetry`.
    """

    hda: int  # topic {hda}: the amplifier's connected-device ID
    input: int  # topic {input}: 1-based input number
    audio_sense: str  # "sensing_audio" or "not_sensing" (only "not_sensing" observed live)
    hda_id: str  # hdaid, equal to the topic {hda}
    input_port: str  # inputport, equal to the topic {input}
    body: dict[str, Any] = field(repr=False, hash=False)


@dataclass(frozen=True, slots=True)
class HdaAmpStatus:
    """Amplifier channel health for one controller slot (spec §00 §5.2.2).

    The topic `channel` {k} is 1-based, k = slot + 1, so it describes the same slot as the
    `HdaZoneStatus` whose `zone == slot`; for stereo zone n, k = 2n-1 is the left (or stereo
    pair) channel and k = 2n the right one [STATIC]. The left and right members of one
    message always carry the same value.

    Published only on a full amplifier status refresh, for every slot regardless of room
    binding, so it can be stale [STATIC]. For a room-bound slot prefer
    `HdaZoneStatus.amp_peak`/`amp_thermal`; this is the only source for unbound slots,
    including the right half of an unsplit stereo zone. Clients MAY consume it but MUST NOT
    require it.
    """

    hda: int  # topic {hda}: the amplifier's connected-device ID
    channel: int  # topic {k}, 1-based
    left_peak: str  # "normal" or "peaking"
    left_thermal: str  # "normal" or "thermal overload"
    right_peak: str
    right_thermal: str
    hda_id: str  # hdaid, equal to the topic {hda}
    zone_id: str  # zoneid, equal to the topic {k}
    body: dict[str, Any] = field(repr=False, hash=False)

    @property
    def slot(self) -> int:
        """The 0-based controller slot: the `HdaZoneStatus.zone` of the same channel."""
        return self.channel - 1


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
    # Not a sequence number: one publisher-wide value, 0 from start-up; after a camera-popup
    # event it MAY carry that event's local-time start in epoch seconds [STATIC]. Any integer
    # is accepted. Never use it for ordering, deduplication or gap detection.
    seq: int | None
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


def _optional[T](parse: Callable[[Any, str, str], T], obj: dict[str, Any], key: str, where: str) -> T | None:
    """A member the library does not require: None when absent, parsed when present."""
    return parse(obj, key, where) if key in obj else None


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
        fixed=_int(body, "fixed", "body") != 0,
    )


def _hda_zone(hda: int, zone: int, body: dict[str, Any]) -> HdaZoneStatus:
    volume = _get(body, "volume", "body")
    amp = _get(body, "amp", "body")
    ducking = body.get("ducking", {})
    if not isinstance(ducking, dict):
        raise TelemetryParseError(f"body.ducking: expected an object, got {ducking!r}")
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
        room_index=_int(body, "roomindex", "body"),
        hda_id=_text(body, "hdaid", "body"),
        zone_id=_text(body, "zoneid", "body"),
        room_name=_optional(_text, body, "roomname", "body"),
        stereo={"stereo": True, "mono": False}.get(_optional(_text, body, "stereo", "body")),
        balance=_optional(_int, body, "balance", "body"),
        bass=_optional(_int, body, "bass", "body"),
        treble=_optional(_int, body, "treble", "body"),
        lipsync=_optional(_int, body, "lipsync", "body"),
        eq=_optional(_text, body, "eq", "body"),
        eq_custom_name=_optional(_text, body, "eqcustomname", "body"),
        ducking=_optional(_on_off, ducking, "status", "body.ducking"),
        ducking_mute=_optional(_on_off, ducking, "mute", "body.ducking"),
        ducking_event_id=_optional(_int, ducking, "eventid", "body.ducking"),
        ducking_input_name=_optional(_text, ducking, "inputname", "body.ducking"),
        input_device_name=_optional(_text, body, "inputdevname", "body"),
        input_hda_id=_optional(_text, body, "inputhdaid", "body"),
        input_port=_optional(_text, body, "inputhdaport", "body"),
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


def _hda_amp(hda: int, channel: int, body: dict[str, Any]) -> HdaAmpStatus:
    return HdaAmpStatus(
        hda=hda,
        channel=channel,
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
