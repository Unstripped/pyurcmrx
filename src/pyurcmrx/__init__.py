"""Async LAN client for URC Total Control MRX controllers.

Clean-room implementation of https://github.com/Unstripped/open-control-spec.
"""

from .checksum import rotating_checksum
from .control import (
    CONTROL_PORT,
    ActionFlags,
    ControlClient,
    Opcode,
    ProjectIdentity,
    Response,
    ResponseDecoder,
    encode_request,
)
from .discovery import DISCOVERY_PORT, DiscoveredController, build_probe, discover, parse_find_ack
from .errors import (
    ActionRejectedError,
    CatalogUnavailableError,
    MalformedFrameError,
    ProjectParseError,
    ProtocolError,
    TelemetryParseError,
    UrcMrxError,
)
from .project import Button, CoreModel, Device, FunctionRef, Room, decode_function_code, parse_core_model
from .staging import fetch_core_model, select_core_model
from .telemetry import (
    MQTT_PORT,
    HdaZoneStatus,
    LinkStatus,
    OtherTelemetry,
    RoomZone,
    RoomZoneStatus,
    Telemetry,
    VariableStatus,
    parse_telemetry,
    subscribe,
)

__all__ = [
    "CONTROL_PORT",
    "DISCOVERY_PORT",
    "MQTT_PORT",
    "ActionFlags",
    "ActionRejectedError",
    "Button",
    "CatalogUnavailableError",
    "ControlClient",
    "CoreModel",
    "Device",
    "DiscoveredController",
    "FunctionRef",
    "HdaZoneStatus",
    "LinkStatus",
    "MalformedFrameError",
    "Opcode",
    "OtherTelemetry",
    "ProjectIdentity",
    "ProjectParseError",
    "ProtocolError",
    "Response",
    "ResponseDecoder",
    "Room",
    "RoomZone",
    "RoomZoneStatus",
    "Telemetry",
    "TelemetryParseError",
    "UrcMrxError",
    "VariableStatus",
    "build_probe",
    "decode_function_code",
    "discover",
    "encode_request",
    "fetch_core_model",
    "parse_core_model",
    "parse_find_ack",
    "parse_telemetry",
    "rotating_checksum",
    "select_core_model",
    "subscribe",
]
