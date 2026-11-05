"""Exception hierarchy for pyurcmrx."""


class UrcMrxError(Exception):
    """Base class for all pyurcmrx errors."""


class MalformedFrameError(UrcMrxError):
    """A frame received from the controller does not match the spec layout."""


class ProtocolError(UrcMrxError):
    """The controller answered with an unexpected opcode or closed the session."""


class ActionRejectedError(UrcMrxError):
    """The controller acknowledged an action with status 0x00 (spec §52 §4.3)."""


class ProjectParseError(UrcMrxError):
    """`core_model.json` is not valid per spec §53 §3.4."""


class CatalogUnavailableError(UrcMrxError):
    """The staged `core_model.json` could not be found, fetched, or verified."""


class TelemetryParseError(UrcMrxError):
    """An MQTT telemetry message does not match spec §00 §5.2."""
