import json
import re
from pathlib import Path

import pytest

from pyurcmrx import Button, ButtonTrigger, ProjectParseError, RoomPowerOff, parse_core_model

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    "name, system_name",
    [("core_model_spec53.json", "Minimal Interop"), ("core_model_spec30.json", "Minimal System")],
)
def test_spec_minimal_core_model(name, system_name):
    model = parse_core_model((FIXTURES / name).read_bytes())
    assert (model.system_name, model.file_indicator) == (system_name, 1730548800)
    (room,) = model.rooms
    assert (room.id, room.name) == (1, "Living Room")
    # Hard button 4625 only jumps to the room main menu (client-local) and is not listed.
    assert room.menu == (
        Button("Power", 4624, None, (RoomPowerOff(1),)),
        Button("Living Room TV", None, "Main", (ButtonTrigger(67585),)),
    )
    (device,) = room.devices
    assert (device.id, device.name, device.device_type, device.con_dev_id) == (2, "Living Room TV", "DBMODEL", 2)
    assert [(b.label, b.hbtn_id, b.page, b.steps) for b in device.buttons] == [
        ("Vol+", 275, None, (ButtonTrigger(131347),)),
        ("Vol-", 276, None, (ButtonTrigger(131348),)),
        ("Mute", 277, None, (ButtonTrigger(131349),)),
        ("Power", 4624, None, (RoomPowerOff(1),)),
        ("CH +", 279, "channel", (ButtonTrigger(131351),)),
        ("CH -", 280, "channel", (ButtonTrigger(131352),)),
    ]


@pytest.mark.parametrize(
    "code, object_id, button_id",
    [
        (131347, 2, 275),  # §53 §3.4.4: object 2, button 275 (Vol+)
        (459029, 7, 277),  # §30 §1.1: Mute on reference object 7
        (67585, 1, 2049),  # §53 §3.4.7: room 1 menu tile
    ],
)
def test_trigger_code_halves(code, object_id, button_id):
    trigger = ButtonTrigger(code)
    assert (trigger.trigger_code, trigger.object_id, trigger.button_id) == (code, object_id, button_id)


def _model(device_obj: dict, **room: object) -> str:
    raw_room = {"id": 4, "info": {"obj": {"name": "Den"}}, "devices": [{"obj": device_obj}], **room}
    return json.dumps({"data": {"rooms": [raw_room]}})


def test_duplicate_hidden_and_jump_only_buttons_are_dropped_and_multi_step_macros_kept():
    device = {
        "id": 9,
        "name": "TV",
        "hard_btns": [{"hbtn_id": 275, "macro": [{"type": 33, "code": (9 << 16) | 275}]}],
        "pages": [
            {
                "name": "audio",
                "buttons": [
                    {"name": "Louder", "macro": [{"type": 33, "code": (9 << 16) | 275}]},
                    {"name": "Hidden", "hidden": 2, "macro": [{"type": 33, "code": (9 << 16) | 1}]},
                    {"name": "Nav only", "macro": [{"type": 4, "j_type": 0, "target": 7}]},
                    {"name": "", "macro": [{"type": 33, "code": (9 << 16) | 530}, {"type": 54, "room": 4}]},
                ],
            }
        ],
    }
    (room,) = parse_core_model(_model(device)).rooms
    assert room.menu == ()
    assert [(b.label, b.steps) for b in room.devices[0].buttons] == [
        ("Vol+", (ButtonTrigger((9 << 16) | 275),)),
        ("Button 530", (ButtonTrigger((9 << 16) | 530), RoomPowerOff(4))),
    ]


def test_jump_and_unknown_step_types_are_skipped_and_the_macro_continues():
    device = {
        "id": 9,
        "name": "TV",
        "pages": [
            {
                "name": "audio",
                "buttons": [
                    {
                        "name": "Power then input",
                        "macro": [
                            {"type": 33, "code": (9 << 16) | 530},
                            {"type": 1, "ms": 1000},
                            {"type": 4, "j_type": 0, "target": 9},
                            "junk",
                            {"type": 33, "code": (9 << 16) | 277},
                        ],
                    },
                ],
            }
        ],
    }
    (button,) = parse_core_model(_model(device)).rooms[0].devices[0].buttons
    assert button.steps == (ButtonTrigger((9 << 16) | 530), ButtonTrigger((9 << 16) | 277))


def test_room_menu_merges_menus_and_drops_placeholders():
    placeholder = {"id": -1, "name": "", "hidden": 1, "image": "", "macro": []}
    menu_devs = {
        "obj": [
            {
                "id": 20485,
                "name": "Main",
                "hard_btns": [
                    {"id": -1, "hbtn_id": 2304, "macro": [{"type": 4, "j_type": 0, "target": 8}]},
                    {"id": -1, "hbtn_id": 4624, "macro": [{"type": 54, "room": 4}]},
                ],
                "menu_buttons": [{"id": 2049, "name": "Watch TV", "macro": [{"type": 33, "code": (3 << 16) | 2049}]}]
                + [placeholder] * 5,
            },
            {"id": 20486, "name": "More", "menu_buttons": [{"id": 2050, "name": "Music", "macro": [{"type": 33, "code": (3 << 16) | 2050}]}]},
        ],
        "chk_obj": 0,
    }
    (room,) = parse_core_model(_model({"id": 9, "name": "TV"}, menu_devs=menu_devs)).rooms
    assert room.menu == (
        Button("Power", 4624, None, (RoomPowerOff(4),)),
        Button("Watch TV", None, "Main", (ButtonTrigger(198657),)),
        Button("Music", None, "More", (ButtonTrigger(198658),)),
    )


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
        (
            _model({"id": 1, "name": "x", "hard_btns": [{"macro": [{"type": 33, "code": 1 << 32}]}]}),
            "hard_btns[0].macro[0].code: trigger code out of uint32 range",
        ),
        (
            _model({"id": 1, "name": "x", "hard_btns": [{"macro": [{"type": 54}]}]}),
            "hard_btns[0].macro[0]: missing required field 'room'",
        ),
        (
            _model({"id": 1, "name": "x"}, menu_devs={"obj": [{"menu_buttons": [{"macro": [{"type": 54, "room": "1"}]}]}]}),
            "data.rooms[0].menu_devs.obj[0].menu_buttons[0].macro[0].room: expected int",
        ),
    ],
)
def test_invalid_core_model_reports_location(doc, message):
    with pytest.raises(ProjectParseError, match=re.escape(message)):
        parse_core_model(doc)
