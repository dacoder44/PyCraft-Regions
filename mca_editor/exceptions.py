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
