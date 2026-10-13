"""Public exception hierarchy; unsafe operations fail rather than guess."""


class MCAError(Exception):
    """Base error for format, coordinate and editing failures."""


class FormatError(MCAError):
    """Malformed region, NBT, chunk or packed array."""


class UnsupportedFormatError(MCAError):
    """Recognized but unsupported version or storage format."""


class CoordinateError(MCAError, ValueError):
    """Coordinate outside the selected region or dimension."""


class MissingChunkError(MCAError, KeyError):
    """Region slot is empty; this library does not generate chunks."""


class MissingSectionError(MCAError, KeyError):
    """Explicit section creation is required before editing."""


class UnsafeEditError(MCAError):
    """Edit needs an explicit metadata decision."""


class SaveError(MCAError):
    """Save failed without replacing the destination."""


class DiscoveryError(MCAError):
    """A server/world configuration cannot be inferred safely."""


class ServerControlError(MCAError):
    """Shutdown, process identity, RCON, or startup could not be verified."""


class WorldBusyError(MCAError):
    """World is locked, changing, or still in use; no safe write is possible."""


class MissingRegionError(MCAError, KeyError):
    """The world has no region file for the requested coordinates."""


class WorldSaveError(SaveError):
    """World transaction failed; inspect backup_path and recovery_required."""

    def __init__(self, message, *, backup_path=None, recovery_required=False):
        super().__init__(message)
        self.backup_path = backup_path
        self.recovery_required = recovery_required


class RestartError(ServerControlError):
    """Edits committed, but restarting failed. report describes the saved files."""

    def __init__(self, message, report):
        super().__init__(message)
        self.report = report
