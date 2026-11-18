"""Exception hierarchy for pyurcmrx."""


class UrcMrxError(Exception):
    """Base class for all pyurcmrx errors."""


class MalformedFrameError(UrcMrxError):
    """A frame received from the controller does not match the spec layout."""


class ProtocolError(UrcMrxError):
    """The controller answered with an unexpected opcode or closed the session."""


class ActionRejectedError(UrcMrxError):
    """The controller answered a request with a negative reply opcode (spec §52 §2.2, §2.3).

    `0xFFFE` for any request, or `0x012D` for the amplifier-zone commands. The status byte
    never decides the outcome.
    """


class AddressRejectedError(ActionRejectedError):
    """The controller did not perform a request because another connection from this client
    address was registered, and retrying did not get through (spec §52 §4 rules 10-11).
    """


class ProjectParseError(UrcMrxError):
    """`core_model.json` is not valid per spec §53 §3.4."""


class CatalogUnavailableError(UrcMrxError):
    """The staged `core_model.json` could not be found, fetched, or verified."""


class TelemetryParseError(UrcMrxError):
    """An MQTT telemetry message does not match spec §00 §5.2."""
