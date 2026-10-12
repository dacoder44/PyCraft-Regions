"""All public chunk coordinates are absolute, never region-local."""

from .exceptions import CoordinateError


def integers(*values: int) -> None:
    if any(type(v) is not int for v in values):
        raise CoordinateError("Coordinates must be integers (not bools or floats)")


def world_to_chunk(x: int, z: int) -> tuple[int, int]:
    integers(x, z)
    return x // 16, z // 16


def chunk_to_region(x: int, z: int) -> tuple[int, int]:
    integers(x, z)
    return x // 32, z // 32


def world_to_section(y: int) -> tuple[int, int]:
    integers(y)
    return divmod(y, 16)


def local_index(x: int, y: int, z: int) -> int:
    integers(x, y, z)
    if not all(0 <= v < 16 for v in (x, y, z)):
        raise CoordinateError("Section-local coordinates must be in 0..15")
    return (y * 16 + z) * 16 + x


def slot_index(chunk_x: int, chunk_z: int) -> int:
    integers(chunk_x, chunk_z)
    return chunk_x % 32 + 32 * (chunk_z % 32)
