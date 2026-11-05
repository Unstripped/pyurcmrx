"""Project catalog from `core_model.json` (spec §53 §3.4, §30 §1.1).

`core_model.json` is the only project description a credential-free LAN client can read
(anonymous FTP, `updateroot/`). It lists rooms, the devices assigned to each room, and
UI buttons whose macros name device functions as `code = (device_id << 16) | func_id`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .errors import ProjectParseError

STEP_DEVICE_FUNCTION = 33  # {"type": 33, "code": (device_id << 16) | func_id}
STEP_ROOM_JUMP = 54  # {"type": 54, "room": room_id}: UI navigation only

# Unlabelled mobile hard-button slots (spec §53 §3.4.6).
HARD_BUTTON_LABELS = {275: "Vol+", 276: "Vol-", 277: "Mute", 4624: "Power"}


@dataclass(frozen=True, slots=True)
class FunctionRef:
    device_id: int
    func_id: int


@dataclass(frozen=True, slots=True)
class Button:
    label: str
    hbtn_id: int | None
    page: str | None  # page name for page buttons, None for hard buttons
    functions: tuple[FunctionRef, ...]  # type-33 steps in macro order


@dataclass(frozen=True, slots=True)
class Device:
    id: int  # equals mcs.dat con_devs[].id and the MQTT hda/zone {device_id}
    name: str
    model_name: str | None
    device_type: str | None
    buttons: tuple[Button, ...]


@dataclass(frozen=True, slots=True)
class Room:
    id: int  # room_id for TCP 51510 and MQTT zone_status
    name: str
    devices: tuple[Device, ...]


@dataclass(frozen=True, slots=True)
class CoreModel:
    system_name: str | None
    file_indicator: int | None
    rooms: tuple[Room, ...]


def decode_function_code(code: int) -> FunctionRef:
    if not 0 <= code <= 0xFFFFFFFF:
        raise ValueError(f"function code out of uint32 range: {code}")
    return FunctionRef(code >> 16, code & 0xFFFF)


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


def _button(raw: Any, page: str | None, where: str) -> Button | None:
    """Return the button, or None when it is hidden or triggers no device function."""
    if not isinstance(raw, dict) or raw.get("hidden"):
        return None
    functions = []
    for k, step in enumerate(raw.get("macro") or []):
        if isinstance(step, dict) and step.get("type") == STEP_DEVICE_FUNCTION:
            code = _field(step, "code", int, f"{where}.macro[{k}]")
            try:
                functions.append(decode_function_code(code))
            except ValueError as err:
                raise ProjectParseError(f"{where}.macro[{k}].code: {err}") from err
    if not functions:
        return None
    hbtn_id = _optional(raw, "hbtn_id", int)
    name = (_optional(raw, "name", str) or "").strip()
    label = name or HARD_BUTTON_LABELS.get(hbtn_id) or f"Function {functions[0].func_id}"
    return Button(label, hbtn_id, page, tuple(functions))


def _device(raw: Any, where: str) -> Device:
    obj = _field(raw, "obj", dict, where)
    where = f"{where}.obj"
    candidates = [
        _button(b, None, f"{where}.hard_btns[{i}]") for i, b in enumerate(_optional(obj, "hard_btns", list) or [])
    ]
    for p, page in enumerate(_optional(obj, "pages", list) or []):
        page_name = _optional(page, "name", str)
        for i, b in enumerate(_optional(page, "buttons", list) or []):
            candidates.append(_button(b, page_name, f"{where}.pages[{p}].buttons[{i}]"))
    # The same function often appears as a hard button and on a page; keep the first.
    buttons: dict[tuple[FunctionRef, ...], Button] = {}
    for button in candidates:
        if button is not None:
            buttons.setdefault(button.functions, button)
    return Device(
        id=_field(obj, "id", int, where),
        name=_field(obj, "name", str, where),
        model_name=_optional(obj, "model_name", str),
        device_type=_optional(obj, "device_type", str),
        buttons=tuple(buttons.values()),
    )


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
