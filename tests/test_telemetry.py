import asyncio
import json
import shutil
import socket
import subprocess

import aiomqtt
import pytest

from pyurcmrx import (
    HdaZoneStatus,
    LinkStatus,
    OtherTelemetry,
    RoomZone,
    RoomZoneStatus,
    TelemetryParseError,
    VariableStatus,
    parse_telemetry,
    subscribe,
)

# Bodies are the spec §00 §5.2.2 examples.
HDA_BODY = {
    "volume": {"type": "variable", "min": "0", "max": "100", "level": "45"},
    "mute": "off",
    "power": "on",
    "treble": "0",
    "bass": "0",
    "balance": "0",
    "lipsync": "0",
    "thermal": "normal",
    "peak": "not_clipping",
    "amp": "normal",
    "roomid": 1,
    "roomname": "Living Room",
    "roomindex": 0,
    "hdaid": 3,
    "zoneid": "z1lr",
}


def envelope(topic: str, body) -> bytes:
    return json.dumps({"header": {"version": "0.0.0.0", "seq": 104, "time_stamp": 1663794348, "tree": topic}, "body": body}).encode()


def test_room_zone_status():
    topic = "tcsystem/global/room_event/zone_status/room_id_1"
    body = [{"room_id": 1, "zone_power": 1, "cur_vol": 45, "min_vol": 0, "max_vol": 100, "mute": 0, "fixed": 0}]
    t = parse_telemetry(topic, envelope(topic, body))
    assert (t.seq, t.timestamp) == (104, 1663794348)
    assert t.event == RoomZoneStatus(1, (RoomZone(1, True, 45, 0, 100, False, False),))


def test_hda_zone_converts_string_fields():
    topic = "tcsystem/global/hda/zone/3/0/"
    event = parse_telemetry(topic, envelope(topic, HDA_BODY)).event
    assert isinstance(event, HdaZoneStatus)
    assert (event.device_id, event.zone_idx, event.power, event.mute, event.volume, event.max_volume) == (3, 0, True, False, 45, 100)
    assert (event.fixed, event.room_id, event.room_name, event.zone_port) == (False, 1, "Living Room", "z1lr")


def test_hda_zone_topic_requires_trailing_slash():
    topic = "tcsystem/global/hda/zone/3/0"
    assert isinstance(parse_telemetry(topic, envelope(topic, HDA_BODY)).event, OtherTelemetry)


def test_variable_and_link_status():
    topic = "tcsystem/global/variable_status/variable_id_7"
    assert parse_telemetry(topic, envelope(topic, {"variable_type": 0, "value": "1"})).event == VariableStatus(7, 0, "1")
    topic = "tcsystem/global/room_event/link_status/link_id_1"
    body = {"link_id": 1, "roomid": 1, "roomname": "Living Room", "linked_rooms": [1, 2]}
    assert parse_telemetry(topic, envelope(topic, body)).event == LinkStatus(1, 1, "Living Room", (1, 2))


def test_unmodelled_topic_passes_body_through():
    topic = "tcsystem/global/hda/input/3/1/"
    assert parse_telemetry(topic, envelope(topic, {"level": "0"})).event == OtherTelemetry({"level": "0"})


def test_empty_payload_is_a_retained_clear():
    assert parse_telemetry("tcsystem/global/variable_status/variable_id_7", b"") is None


@pytest.mark.parametrize(
    "topic, body, message",
    [
        ("tcsystem/global/hda/zone/3/0/", {**HDA_BODY, "power": "maybe"}, "body.power: expected 'on' or 'off'"),
        ("tcsystem/global/hda/zone/3/0/", {**HDA_BODY, "volume": {**HDA_BODY["volume"], "level": "loud"}}, "body.volume.level"),
        ("tcsystem/global/room_event/zone_status/room_id_1", {"cur_vol": 1}, "body is not an array"),
        ("tcsystem/global/room_event/zone_status/room_id_1", [{"room_id": 1}], "body[0]: missing 'zone_power'"),
    ],
)
def test_malformed_bodies_raise(topic, body, message):
    with pytest.raises(TelemetryParseError, match=message.replace("[", r"\[").replace("]", r"\]")):
        parse_telemetry(topic, envelope(topic, body))


def test_invalid_json_raises():
    with pytest.raises(TelemetryParseError, match="invalid JSON"):
        parse_telemetry("tcsystem/x", b"{")


@pytest.fixture
def broker():
    exe = shutil.which("mosquitto") or shutil.which("/opt/homebrew/sbin/mosquitto")
    if exe is None:
        pytest.skip("mosquitto not installed")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([exe, "-p", str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    yield port
    proc.terminate()
    proc.wait(5)


async def _publish(port: int, messages: list[tuple[str, bytes, bool]]) -> None:
    for _ in range(50):  # wait for the broker to accept connections
        try:
            async with aiomqtt.Client("127.0.0.1", port=port) as client:
                for topic, payload, retain in messages:
                    await client.publish(topic, payload, retain=retain)
            return
        except aiomqtt.MqttError:
            await asyncio.sleep(0.05)
    raise RuntimeError("broker did not start")


async def test_subscribe_delivers_retained_state_and_skips_malformed(broker):
    hda = "tcsystem/global/hda/zone/3/0/"
    var = "tcsystem/global/variable_status/variable_id_7"
    # Retained state published before the subscriber connects must still arrive.
    await _publish(broker, [(hda, envelope(hda, HDA_BODY), True)])

    async def first_two():
        received = []
        async for telemetry in subscribe("127.0.0.1", broker):
            received.append(telemetry.event)
            if len(received) == 2:
                return received

    task = asyncio.create_task(first_two())
    await asyncio.sleep(0.3)
    await _publish(broker, [(var, b"not json", False), (var, envelope(var, {"variable_type": 1, "value": "5"}), False)])
    hda_event, var_event = await asyncio.wait_for(task, 5)
    assert isinstance(hda_event, HdaZoneStatus) and hda_event.volume == 45
    assert var_event == VariableStatus(7, 1, "5")
