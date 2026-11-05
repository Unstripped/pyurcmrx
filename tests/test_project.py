import json
import re
from pathlib import Path

import pytest

from pyurcmrx import FunctionRef, ProjectParseError, decode_function_code, parse_core_model

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("name", ["core_model_spec53.json", "core_model_spec30.json"])
def test_spec_minimal_core_model(name):
    model = parse_core_model((FIXTURES / name).read_bytes())
    assert model.file_indicator == 1730548800
    (room,) = model.rooms
    assert (room.id, room.name) == (1, "Living Room")
    (device,) = room.devices
    assert (device.id, device.name, device.device_type) == (2, "Living Room TV", "DBMODEL")
    assert [(b.label, b.page, b.functions) for b in device.buttons] == [
        ("Vol+", None, (FunctionRef(2, 275),)),
        ("Vol-", None, (FunctionRef(2, 276),)),
        ("Mute", None, (FunctionRef(2, 277),)),
        # Spec vector's Power slot decodes to device 3 / function 278 (kept as authored).
        ("Power", None, (FunctionRef(3, 278),)),
        ("CH +", "channel", (FunctionRef(2, 279),)),
        ("CH -", "channel", (FunctionRef(2, 280),)),
    ]


def test_decode_function_code_spec_example():
    assert decode_function_code(131347) == FunctionRef(2, 275)
    with pytest.raises(ValueError):
        decode_function_code(1 << 32)


def _model(device_obj: dict) -> str:
    room = {"id": 4, "info": {"obj": {"name": "Den"}}, "devices": [{"obj": device_obj}]}
    return json.dumps({"data": {"rooms": [room]}})


def test_duplicate_and_hidden_buttons_are_dropped_and_multi_step_macros_kept():
    device = {
        "id": 9,
        "name": "HDA",
        "hard_btns": [{"hbtn_id": 275, "macro": [{"type": 33, "code": (9 << 16) | 275}]}],
        "pages": [
            {
                "name": "audio",
                "buttons": [
                    {"name": "Louder", "macro": [{"type": 33, "code": (9 << 16) | 275}]},
                    {"name": "Hidden", "hidden": 2, "macro": [{"type": 33, "code": (9 << 16) | 1}]},
                    {"name": "Nav only", "macro": [{"type": 54, "room": 4}]},
                    {"name": "", "macro": [{"type": 33, "code": (9 << 16) | 1001}, {"type": 33, "code": (9 << 16) | 277}]},
                ],
            }
        ],
    }
    (room,) = parse_core_model(_model(device)).rooms
    assert [(b.label, b.functions) for b in room.devices[0].buttons] == [
        ("Vol+", (FunctionRef(9, 275),)),
        ("Function 1001", (FunctionRef(9, 1001), FunctionRef(9, 277))),
    ]


@pytest.mark.parametrize(
    "doc, message",
    [
        ("not json", "invalid JSON"),
        ("[]", "core_model.json: missing required field 'data'"),
        ('{"data":{}}', "data: missing required field 'rooms'"),
        (_model({"name": "x"}), "data.rooms[0].devices[0].obj: missing required field 'id'"),
        (_model({"id": True, "name": "x"}), "data.rooms[0].devices[0].obj.id: expected int"),
        (
            _model({"id": 1, "name": "x", "hard_btns": [{"macro": [{"type": 33, "code": "5"}]}]}),
            "hard_btns[0].macro[0].code: expected int",
        ),
    ],
)
def test_invalid_core_model_reports_location(doc, message):
    with pytest.raises(ProjectParseError, match=re.escape(message)):
        parse_core_model(doc)
