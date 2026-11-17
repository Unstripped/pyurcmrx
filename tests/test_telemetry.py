import asyncio
import json
import os
import shutil
import socket
import subprocess

import aiomqtt
import pytest

from pyurcmrx import (
    HdaAmpStatus,
    HdaInputStatus,
    HdaZoneStatus,
    LinkStatus,
    OtherTelemetry,
    RoomZoneStatus,
    TelemetryParseError,
    VariableStatus,
    parse_telemetry,
    subscribe,
)

# Spec §00 §5.2.2 example bodies, verbatim.
SPEC_ROOM_BODY = '{"zone_power": 1, "cur_vol": 45, "min_vol": 0, "max_vol": 100, "mute": 0, "fixed": 0}'
SPEC_HDA_BODY = """{
  "amp": {"peak": "normal", "thermal": "normal"},
  "balance": "0", "bass": "0", "treble": "0", "lipsync": "0",
  "ducking": {"status": "off"}, "eq": "Off", "eqcustomname": "Custom",
  "hdaid": "2", "inputdevname": "Source", "inputhdaid": "2", "inputhdaport": "1",
  "mute": "off", "power": "on", "roomid": "2", "roomindex": "2",
  "roomname": "Room", "stereo": "stereo",
  "volume": {"level": "45", "max": "100", "min": "0", "type": "variable"},
  "zoneid": "z1lr"
}"""
SPEC_INPUT_BODY = '{"audiosense":"not_sensing","hdaid":"2","inputport":"1"}'
SPEC_AMP_BODY = '{"hdaid":"2","zoneid":"1","leftpeak":"normal","leftthermal":"normal","rightpeak":"normal","rightthermal":"normal"}'

# Bodies observed on a real MRX-15 (issue #6), verbatim.
OBSERVED_ROOM_BODY = '{"cur_vol":81,"fixed":0,"max_vol":100,"min_vol":0,"mute":0,"zone_power":1}'
OBSERVED_HDA_BODY = '{"amp":{"peak":"normal","thermal":"normal"},"balance":"0","bass":"0","ducking":{"status":"off"},"eq":"Off","eqcustomname":"Custom","hdaid":"2","inputdevname":"APPLE TV GYM","inputhdaid":"2","inputhdaport":"1","lipsync":"0","mute":"off","power":"on","roomid":"2","roomindex":"2","roomname":"Exercise Room","stereo":"stereo","treble":"0","volume":{"level":"81","max":"100","min":"0","type":"variable"},"zoneid":"z1lr"}'

ROOM_TOPIC = "tcsystem/global/room_event/zone_status/room_id_2"
HDA_TOPIC = "tcsystem/global/hda/zone/2/0/"


def envelope(topic: str, body: str, time_stamp: int = 1663794348) -> bytes:
    """Spec §00 §5.2.1 envelope around a JSON body text."""
    header = {"version": "0.0.0.0", "seq": 0, "time_stamp": time_stamp, "tree": topic}
    return f'{{"header": {json.dumps(header)}, "body": {body}}}'.encode()


def test_room_zone_status_takes_room_from_topic():
    t = parse_telemetry(ROOM_TOPIC, envelope(ROOM_TOPIC, SPEC_ROOM_BODY))
    assert (t.seq, t.timestamp) == (0, 1663794348)
    assert t.event == RoomZoneStatus(2, True, 45, 0, 100, False, False)


def test_room_zone_status_observed_body():
    event = parse_telemetry(ROOM_TOPIC, envelope(ROOM_TOPIC, OBSERVED_ROOM_BODY)).event
    assert event == RoomZoneStatus(2, True, 81, 0, 100, False, False)


def test_hda_zone_spec_example():
    event = parse_telemetry(HDA_TOPIC, envelope(HDA_TOPIC, SPEC_HDA_BODY)).event
    assert isinstance(event, HdaZoneStatus)
    assert (event.hda, event.zone, event.power, event.mute, event.fixed) == (2, 0, True, False, False)
    assert (event.volume, event.min_volume, event.max_volume) == (45, 0, 100)
    assert (event.amp_peak, event.amp_thermal) == ("normal", "normal")
    assert (event.room_id, event.room_index, event.hda_id, event.zone_id) == (2, "2", "2", "z1lr")
    assert event.body == json.loads(SPEC_HDA_BODY)


def test_room_and_hda_views_of_one_change_stay_separate():
    # Issue #6: one change produced both topics with the same time_stamp (cur_vol 81 / level "81").
    room = parse_telemetry(ROOM_TOPIC, envelope(ROOM_TOPIC, OBSERVED_ROOM_BODY, 1700000000))
    hda = parse_telemetry(HDA_TOPIC, envelope(HDA_TOPIC, OBSERVED_HDA_BODY, 1700000000))
    assert isinstance(room.event, RoomZoneStatus) and isinstance(hda.event, HdaZoneStatus)
    assert room.event.volume == hda.event.volume == 81
    assert hda.event.room_id == room.event.room_id == 2
    assert hda.event.body["inputdevname"] == "APPLE TV GYM"


def test_hda_zone_tolerates_unknown_values_and_extra_fields():
    body = json.loads(SPEC_HDA_BODY)
    body.update(power="standby", extra={"x": 1}, roomid=2, amp={"peak": "clipping", "thermal": "hot"})
    body["volume"] = {**body["volume"], "type": "stepped"}
    event = parse_telemetry(HDA_TOPIC, envelope(HDA_TOPIC, json.dumps(body))).event
    assert (event.power, event.fixed, event.room_id) == (None, None, 2)
    assert (event.amp_peak, event.amp_thermal) == ("clipping", "hot")


def test_hda_zone_topic_requires_trailing_slash():
    topic = "tcsystem/global/hda/zone/2/0"
    assert isinstance(parse_telemetry(topic, envelope(topic, SPEC_HDA_BODY)).event, OtherTelemetry)


def test_hda_input_status():
    topic = "tcsystem/global/hda/input/2/1/"
    event = parse_telemetry(topic, envelope(topic, SPEC_INPUT_BODY)).event
    assert event == HdaInputStatus(2, 1, "not_sensing", "2", "1", json.loads(SPEC_INPUT_BODY))


def test_hda_amp_status():
    topic = "tcsystem/global/hda/ampstatus/2/1/"
    event = parse_telemetry(topic, envelope(topic, SPEC_AMP_BODY)).event
    assert event == HdaAmpStatus(2, 1, "normal", "normal", "normal", "normal", "2", "1", json.loads(SPEC_AMP_BODY))


def test_variable_and_link_status():
    topic = "tcsystem/global/variable_status/variable_id_7"
    body = '{\n  "variable_type": 0,\n  "value": "1"\n}'
    assert parse_telemetry(topic, envelope(topic, body)).event == VariableStatus(7, 0, "1")
    topic = "tcsystem/global/room_event/link_status/link_id_1"
    body = '{\n  "link_id": 1,\n  "roomid": 1,\n  "roomname": "Living Room",\n  "linked_rooms": [1, 2]\n}'
    assert parse_telemetry(topic, envelope(topic, body)).event == LinkStatus(1, 1, "Living Room", (1, 2))


def test_unmodelled_topic_passes_body_through():
    topic = "tcsystem/global/camera_popup"
    assert parse_telemetry(topic, envelope(topic, '{"popup_status": 1}')).event == OtherTelemetry({"popup_status": 1})


def test_empty_payload_is_a_retained_clear():
    assert parse_telemetry("tcsystem/global/variable_status/variable_id_7", b"") is None


@pytest.mark.parametrize(
    "topic, body, message",
    [
        (HDA_TOPIC, {**json.loads(SPEC_HDA_BODY), "power": ["on"]}, "body.power: expected string"),
        (HDA_TOPIC, {**json.loads(SPEC_HDA_BODY), "volume": {"level": "loud", "max": "100", "min": "0", "type": "variable"}}, "body.volume.level"),
        (HDA_TOPIC, {k: v for k, v in json.loads(SPEC_HDA_BODY).items() if k != "amp"}, "body: missing 'amp'"),
        (HDA_TOPIC, [json.loads(SPEC_HDA_BODY)], "body is not an object"),
        (ROOM_TOPIC, [json.loads(SPEC_ROOM_BODY)], "body is not an object"),
        (ROOM_TOPIC, {"cur_vol": 1}, "body: missing 'zone_power'"),
        (ROOM_TOPIC, {**json.loads(SPEC_ROOM_BODY), "mute": 2}, "body.mute: expected 0 or 1"),
    ],
)
def test_malformed_bodies_raise(topic, body, message):
    with pytest.raises(TelemetryParseError, match=message.replace("[", r"\[").replace("]", r"\]")):
        parse_telemetry(topic, envelope(topic, json.dumps(body)))


def test_invalid_json_raises():
    with pytest.raises(TelemetryParseError, match="invalid JSON"):
        parse_telemetry("tcsystem/x", b"{")


@pytest.fixture
def broker():
    exe = shutil.which("mosquitto") or shutil.which("/opt/homebrew/sbin/mosquitto")
    if exe is None:
        if os.environ.get("PYURCMRX_REQUIRE_MOSQUITTO"):
            pytest.fail("mosquitto not installed but PYURCMRX_REQUIRE_MOSQUITTO is set")
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
    var = "tcsystem/global/variable_status/variable_id_7"
    # Retained state published before the subscriber connects must still arrive.
    await _publish(broker, [(HDA_TOPIC, envelope(HDA_TOPIC, SPEC_HDA_BODY), True)])

    async def first_two():
        received = []
        async for telemetry in subscribe("127.0.0.1", broker):
            received.append(telemetry.event)
            if len(received) == 2:
                return received

    task = asyncio.create_task(first_two())
    await asyncio.sleep(0.3)
    await _publish(broker, [(var, b"not json", False), (var, envelope(var, '{"variable_type": 1, "value": "5"}'), False)])
    hda_event, var_event = await asyncio.wait_for(task, 5)
    assert isinstance(hda_event, HdaZoneStatus) and hda_event.volume == 45
    assert var_event == VariableStatus(7, 1, "5")
