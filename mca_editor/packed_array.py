"""Anvil palette indices, least significant bits first in signed big-endian longs.

Unlike the network protocol, disk palettes remain local even above 256 entries.
"""

from .exceptions import FormatError

MASK64 = (1 << 64) - 1


def bit_width(palette_size: int) -> int:
    if not 1 <= palette_size <= 4096:
        raise FormatError(f"Invalid block palette size {palette_size}")
    return max(4, (palette_size - 1).bit_length())


def long_count(count: int, bits: int, padded: bool) -> int:
    if not 1 <= bits <= 32 or count < 0:
        raise ValueError("Packed arrays require 1..32 bits and a nonnegative count")
    return (count + 64 // bits - 1) // (64 // bits) if padded else (count * bits + 63) // 64


def unpack(longs: list[int], bits: int, count: int = 4096, *, padded: bool) -> list[int]:
    expected = long_count(count, bits, padded)
    if len(longs) != expected:
        raise FormatError(f"Packed array has {len(longs)} longs; expected {expected} ({bits} bits)")
    words = [v & MASK64 for v in longs]
    mask = (1 << bits) - 1
    result = []
    for i in range(count):
        word, shift = (
            (i // (64 // bits), (i % (64 // bits)) * bits) if padded else divmod(i * bits, 64)
        )
        value = words[word] >> shift
        if not padded and shift + bits > 64:
            value |= words[word + 1] << (64 - shift)
        result.append(value & mask)
    return result


def pack(values: list[int], bits: int, *, padded: bool) -> list[int]:
    words = [0] * long_count(len(values), bits, padded)
    for i, value in enumerate(values):
        if not 0 <= value < (1 << bits):
            raise FormatError(f"Palette index {value} does not fit in {bits} bits")
        word, shift = (
            (i // (64 // bits), (i % (64 // bits)) * bits) if padded else divmod(i * bits, 64)
        )
        words[word] |= (value << shift) & MASK64
        if not padded and shift + bits > 64:
            words[word + 1] |= value >> (64 - shift)
    return [v if v < 1 << 63 else v - (1 << 64) for v in words]
