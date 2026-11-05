"""Parsers for the controller's project catalog files (spec §30).

`mcs.dat` lists controllable devices with per-zone function IDs; `mcs_info.dat` lists
base stations, client seats, and device classification. `build_catalog` joins them by
device ID. How a client obtains these files from a running controller is not yet
specified (investigator question Q-001).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .errors import ProjectParseError

DB_TYPE_STANDARD = 0
DB_TYPE_MULTI_ZONE = 1


@dataclass(frozen=True, slots=True)
class Function:
    id: int
    name: str


@dataclass(frozen=True, slots=True)
class Zone:
    idx: int
    funcs: tuple[Function, ...]


@dataclass(frozen=True, slots=True)
class ConnectedDevice:
    id: int
    name: str
    db_type: int
    zones: tuple[Zone, ...]

    @property
    def multi_zone(self) -> bool:
        return self.db_type == DB_TYPE_MULTI_ZONE


@dataclass(frozen=True, slots=True)
class DeviceModel:
    """Parsed `mcs.dat`."""

    devices: tuple[ConnectedDevice, ...]
    editor_ver: str
    editor_type: str


@dataclass(frozen=True, slots=True)
class Station:
    obj_id: int
    name: str
    model: str


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    obj_id: int
    name: str
    model: str
    transport: str | None
    category: str | None
    brand: str | None
    db_model: str | None
    twoway_vfd: str | None


@dataclass(frozen=True, slots=True)
class SystemInfo:
    """Parsed `mcs_info.dat`."""

    base_stations: tuple[Station, ...]
    clients: tuple[Station, ...]
    devices: tuple[DeviceInfo, ...]
    editor_ver: str
    editor_type: str
    download_date: str
    file_indicator: int


@dataclass(frozen=True, slots=True)
class CatalogDevice:
    """One controllable device: `mcs.dat` functions joined with `mcs_info.dat` metadata."""

    device: ConnectedDevice
    info: DeviceInfo | None  # None when the ID is missing from mcs_info.dat (spec §30 §7.4)


def _load(data: bytes | str, filename: str) -> dict[str, Any]:
    try:
        root = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        raise ProjectParseError(f"{filename}: invalid JSON: {err}") from err
    if not isinstance(root, dict):
        raise ProjectParseError(f"{filename}: root is not an object")
    return root


def _field(obj: Any, key: str, kind: type | tuple[type, ...], where: str) -> Any:
    if not isinstance(obj, dict) or key not in obj:
        raise ProjectParseError(f"{where}: missing required field {key!r}")
    value = obj[key]
    # bool is an int subclass; reject it where an integer is required.
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise ProjectParseError(f"{where}.{key}: expected {getattr(kind, '__name__', kind)}, got {type(value).__name__}")
    return value


def _optional_str(obj: dict[str, Any] | None, key: str) -> str | None:
    if obj is None:
        return None
    value = obj.get(key)
    return value if isinstance(value, str) else None


def parse_mcs(data: bytes | str) -> DeviceModel:
    root = _load(data, "mcs.dat")
    devices = []
    for i, raw in enumerate(_field(root, "con_devs", list, "mcs.dat")):
        where = f"mcs.dat.con_devs[{i}]"
        db = _field(raw, "db", dict, where)
        zones = []
        for j, raw_zone in enumerate(_field(db, "zones", list, f"{where}.db")):
            zwhere = f"{where}.db.zones[{j}]"
            funcs = tuple(
                Function(_field(f, "id", int, f"{zwhere}.funcs[{k}]"), _field(f, "name", str, f"{zwhere}.funcs[{k}]"))
                for k, f in enumerate(_field(raw_zone, "funcs", list, zwhere))
            )
            zones.append(Zone(_field(raw_zone, "idx", int, zwhere), funcs))
        devices.append(
            ConnectedDevice(
                id=_field(raw, "id", int, where),
                name=_field(raw, "name", str, where),
                db_type=_field(db, "type", int, f"{where}.db"),
                zones=tuple(zones),
            )
        )
    return DeviceModel(
        devices=tuple(devices),
        editor_ver=_field(root, "editor_ver", str, "mcs.dat"),
        editor_type=_field(root, "editor_type", str, "mcs.dat"),
    )


def _stations(root: dict[str, Any], key: str) -> tuple[Station, ...]:
    return tuple(
        Station(
            obj_id=_field(raw, "obj_id", int, f"mcs_info.dat.{key}[{i}]"),
            name=_field(raw, "name", str, f"mcs_info.dat.{key}[{i}]"),
            model=_field(raw, "model", str, f"mcs_info.dat.{key}[{i}]"),
        )
        for i, raw in enumerate(_field(root, key, list, "mcs_info.dat"))
    )


def parse_mcs_info(data: bytes | str) -> SystemInfo:
    root = _load(data, "mcs_info.dat")
    devices = []
    for i, raw in enumerate(_field(root, "devices", list, "mcs_info.dat")):
        where = f"mcs_info.dat.devices[{i}]"
        db = raw.get("db") if isinstance(raw, dict) else None
        db = db if isinstance(db, dict) else None
        twoway = raw.get("twoway") if isinstance(raw, dict) else None
        twoway = twoway if isinstance(twoway, dict) else None
        devices.append(
            DeviceInfo(
                obj_id=_field(raw, "obj_id", int, where),
                name=_field(raw, "name", str, where),
                model=_field(raw, "model", str, where),
                transport=_optional_str(db, "type"),
                category=_optional_str(db, "category"),
                brand=_optional_str(db, "brand"),
                db_model=_optional_str(db, "model"),
                twoway_vfd=_optional_str(twoway, "vfd"),
            )
        )
    return SystemInfo(
        base_stations=_stations(root, "bstations"),
        clients=_stations(root, "clients"),
        devices=tuple(devices),
        editor_ver=_field(root, "editor_ver", str, "mcs_info.dat"),
        editor_type=_field(root, "editor_type", str, "mcs_info.dat"),
        download_date=_field(root, "download_date", str, "mcs_info.dat"),
        file_indicator=_field(root, "file_indicator", int, "mcs_info.dat"),
    )


def build_catalog(model: DeviceModel, info: SystemInfo) -> tuple[CatalogDevice, ...]:
    by_id = {d.obj_id: d for d in info.devices}
    return tuple(CatalogDevice(device, by_id.get(device.id)) for device in model.devices)
