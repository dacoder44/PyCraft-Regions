"""Bounded, strict Anvil compression codecs (1=gzip, 2=zlib, 3=raw)."""

import gzip
import zlib

from .exceptions import FormatError, UnsupportedFormatError
from .nbt_utils import MAX_NBT_BYTES


def check_codec(codec: int) -> None:
    if codec & 128:
        raise UnsupportedFormatError(
            "External .mcc chunk record detected; external storage is not supported"
        )
    if codec not in (1, 2, 3):
        raise UnsupportedFormatError(
            f"Unsupported chunk compression {codec} (supported: gzip=1, zlib=2, raw=3; LZ4=4 is unsupported)"
        )


def decompress(data: bytes, codec: int) -> bytes:
    check_codec(codec)
    if codec == 3:
        if len(data) > MAX_NBT_BYTES:
            raise FormatError("Uncompressed chunk exceeds safety limit")
        return data
    try:
        decoder = zlib.decompressobj(31 if codec == 1 else 15)
        result = decoder.decompress(data, MAX_NBT_BYTES + 1)
        if len(result) > MAX_NBT_BYTES or decoder.unconsumed_tail:
            raise FormatError("Decompressed chunk exceeds 64 MiB safety limit")
        if not decoder.eof or decoder.unused_data:
            raise FormatError("Truncated compression stream or trailing compressed data")
        return result
    except zlib.error as exc:
        raise FormatError(f"Invalid compressed chunk: {exc}") from exc


def compress(data: bytes, codec: int) -> bytes:
    check_codec(codec)
    if codec == 1:
        return gzip.compress(data, mtime=0)
    return zlib.compress(data) if codec == 2 else data
