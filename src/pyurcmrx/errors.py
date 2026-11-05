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
    """A project file (mcs.dat / mcs_info.dat) is not valid per spec §30."""
