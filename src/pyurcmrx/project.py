"""Project catalog from `core_model.json` (spec §53 §3.4, §30 §1.1).

`core_model.json` is the only project description a credential-free LAN client can read
(anonymous FTP, `updateroot/`). It lists rooms, each room's menu, the device objects shown in
each room, and the buttons on both. A button's macro lists steps in execution order
(§53 §3.4.4); this library implements the two step types that reach the controller:

- type 33 triggers a controller-side button: its `code` is sent as the `MACRO_ACTION`
  (`0x07DA`) `trigger_code` (§52 §3.1);
- type 54 turns a room off: `ROOMS_CMD_ROOM_POWER_OFF` (`0x000D`) with `room_id = room`.

Type 4 (jump) is client-local navigation. This library has no screens, so it ignores jumps,
and it skips every other step type, as clients MUST (§53 §3.4.4).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .errors import ProjectParseError

STEP_JUMP = 4  # {"type": 4, "j_type": ..., "target": ...}: client-local UI navigation
STEP_BUTTON_TRIGGER = 33  # {"type": 33, "code": (object_id << 16) | button_id}
STEP_ROOM_POWER_OFF = 54  # {"type": 54, "room": room_id}

# Unlabelled hard-button slots (spec §53 §3.4.6). Jump-only slots never reach the catalog.
HARD_BUTTON_LABELS = {275: "Vol+", 276: "Vol-", 277: "Mute", 4624: "Power"}


@dataclass(frozen=True, slots=True)
class ButtonTrigger:
    """A controller-side button; send `trigger_code` with `MACRO_ACTION` (spec §52 §3.1).

    The code is opaque to clients (§53 §3.4.4): `object_id` is a project UI object id, not
    a connected-device id, and `button_id` is only by convention a device function id.
    """

    trigger_code: int

    @property
    def object_id(self) -> int:
        return self.trigger_code >> 16

    @property
    def button_id(self) -> int:
        return self.trigger_code & 0xFFFF


@dataclass(frozen=True, slots=True)
class RoomPowerOff:
    """Send `ROOMS_CMD_ROOM_POWER_OFF` (`0x000D`) with this room id (spec §53 §3.4.4)."""

    room_id: int


MacroStep = ButtonTrigger | RoomPowerOff


@dataclass(frozen=True, slots=True)
class Button:
    label: str
    hbtn_id: int | None
    page: str | None  # page or menu name for on-screen buttons, None for hard buttons
    # The steps a client sends, in macro order; jumps and unknown step types are skipped.
    steps: tuple[MacroStep, ...]


@dataclass(frozen=True, slots=True)
class Device:
    id: int  # project-wide UI object id (obj.id), not an mcs.dat connected-device id
    name: str
    model_name: str | None
    device_type: str | None
    # obj.keyboard.con_dev_id: the connected device this object controls (§53 §3.4.3, [INFERRED]).
    con_dev_id: int | None
    buttons: tuple[Button, ...]


@dataclass(frozen=True, slots=True)
class Room:
    id: int  # room_id for TCP 51510 room power (0x000D/0x0012) and MQTT zone_status
    name: str
    menu: tuple[Button, ...]  # room menu (menu_devs) hard buttons and tiles
    devices: tuple[Device, ...]


@dataclass(frozen=True, slots=True)
class CoreModel:
    system_name: str | None
    file_indicator: int | None
    rooms: tuple[Room, ...]


def _field(obj: Any, key: str, kind: type, where: str) -> Any:
    if not isinstance(obj, dict) or key not in obj:
        raise ProjectParseError(f"{where}: missing required field {key!r}")
    value = obj[key]
    # bool is an int subclass; reject it where an integer is required.
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise ProjectParseError(f"{where}.{key}: expected {kind.__name__}, got {type(value).__name__}")
    return value


def _optional(obj: Any, key: str, kind: type) -> Any:
    value = obj.get(key) if isinstance(obj, dict) else None
    if isinstance(value, kind) and not (kind is int and isinstance(value, bool)):
        return value
    return None


def _step(step: Any, where: str) -> MacroStep | None:
    step_type = _optional(step, "type", int)
    if step_type == STEP_BUTTON_TRIGGER:
        code = _field(step, "code", int, where)
        if not 0 <= code <= 0xFFFFFFFF:
            raise ProjectParseError(f"{where}.code: trigger code out of uint32 range: {code}")
        return ButtonTrigger(code)
    if step_type == STEP_ROOM_POWER_OFF:
        return RoomPowerOff(_field(step, "room", int, where))
    return None  # STEP_JUMP and unknown types


def _button(raw: Any, page: str | None, where: str) -> Button | None:
    """Return the button, or None when it is hidden or sends nothing to the controller."""
    if not isinstance(raw, dict) or raw.get("hidden"):
        return None
    steps = tuple(
        step
        for k, raw_step in enumerate(raw.get("macro") or [])
        if (step := _step(raw_step, f"{where}.macro[{k}]")) is not None
    )
    if not steps:
        return None
    hbtn_id = _optional(raw, "hbtn_id", int)
    name = (_optional(raw, "name", str) or "").strip()
    first = steps[0]
    fallback = f"Button {first.button_id}" if isinstance(first, ButtonTrigger) else "Room off"
    return Button(name or HARD_BUTTON_LABELS.get(hbtn_id) or fallback, hbtn_id, page, steps)


def _buttons(candidates: list[Button | None]) -> tuple[Button, ...]:
    # The same action often appears as a hard button and on a page; keep the first.
    # Deduplicate on the full step sequence so different macros never collapse.
    buttons: dict[tuple[MacroStep, ...], Button] = {}
    for button in candidates:
        if button is not None:
            buttons.setdefault(button.steps, button)
    return tuple(buttons.values())


def _hard_buttons(obj: dict, where: str) -> list[Button | None]:
    return [_button(b, None, f"{where}.hard_btns[{i}]") for i, b in enumerate(_optional(obj, "hard_btns", list) or [])]


def _device(raw: Any, where: str) -> Device:
    obj = _field(raw, "obj", dict, where)
    where = f"{where}.obj"
    candidates = _hard_buttons(obj, where)
    for p, page in enumerate(_optional(obj, "pages", list) or []):
        page_name = _optional(page, "name", str)
        for i, b in enumerate(_optional(page, "buttons", list) or []):
            candidates.append(_button(b, page_name, f"{where}.pages[{p}].buttons[{i}]"))
    return Device(
        id=_field(obj, "id", int, where),
        name=_field(obj, "name", str, where),
        model_name=_optional(obj, "model_name", str),
        device_type=_optional(obj, "device_type", str),
        con_dev_id=_optional(_optional(obj, "keyboard", dict), "con_dev_id", int),
        buttons=_buttons(candidates),
    )


def _menu(raw_room: dict, where: str) -> tuple[Button, ...]:
    candidates: list[Button | None] = []
    for m, menu in enumerate(_optional(_optional(raw_room, "menu_devs", dict), "obj", list) or []):
        menu_where = f"{where}.menu_devs.obj[{m}]"
        candidates += _hard_buttons(menu, menu_where)
        menu_name = _optional(menu, "name", str)
        for i, b in enumerate(_optional(menu, "menu_buttons", list) or []):
            candidates.append(_button(b, menu_name, f"{menu_where}.menu_buttons[{i}]"))
    return _buttons(candidates)


def parse_core_model(data: bytes | str) -> CoreModel:
    try:
        root = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        raise ProjectParseError(f"core_model.json: invalid JSON: {err}") from err
    body = _field(root, "data", dict, "core_model.json")
    rooms = []
    for r, raw_room in enumerate(_field(body, "rooms", list, "data")):
        where = f"data.rooms[{r}]"
        info = _field(_field(raw_room, "info", dict, where), "obj", dict, f"{where}.info")
        rooms.append(
            Room(
                id=_field(raw_room, "id", int, where),
                name=_field(info, "name", str, f"{where}.info.obj"),
                menu=_menu(raw_room, where),
                devices=tuple(
                    _device(d, f"{where}.devices[{i}]")
                    for i, d in enumerate(_field(raw_room, "devices", list, where))
                ),
            )
        )
    config = _optional(_optional(body, "config", dict), "obj", dict)
    return CoreModel(
        system_name=_optional(config, "system_name", str),
        file_indicator=_optional(config, "file_indicator", int),
        rooms=tuple(rooms),
    )
