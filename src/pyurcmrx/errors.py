"""Exception hierarchy for pyurcmrx."""


class UrcMrxError(Exception):
    """Base class for all pyurcmrx errors."""


class MalformedFrameError(UrcMrxError):
    """A frame received from the controller does not match the spec layout."""


class ProtocolError(UrcMrxError):
    """The controller answered with an unexpected opcode or closed the session."""


class ActionRejectedError(UrcMrxError):
    """The controller rejected a request: generic negative `0xFFFE`, or status other than 0x01.

    Spec §52 §2.2 and §2.3: `0xFFFE` is negative whatever its status byte, and any other
    reply with a status other than 0x01 is treated as a failure.
    """


class ProjectParseError(UrcMrxError):
    """`core_model.json` is not valid per spec §53 §3.4."""


class CatalogUnavailableError(UrcMrxError):
    """The staged `core_model.json` could not be found, fetched, or verified."""


class TelemetryParseError(UrcMrxError):
    """An MQTT telemetry message does not match spec §00 §5.2."""
