"""Strict Java big-endian NBT, all 12 payload types and modified UTF-8.

Compound insertion order, numeric widths, list element types (including empty
lists), array types and the root name are retained. Duplicate compound keys are
rejected because a dictionary could not preserve them losslessly.
"""

import struct
from dataclasses import dataclass
from io import BytesIO
from typing import Any

from .exceptions import FormatError

MAX_NBT_BYTES = 64 * 1024 * 1024
MAX_DEPTH = 128
MAX_TAGS = 2_000_000


@dataclass
class Tag:
    id: int
    value: Any
    subtype: int = 0  # TAG_List element type, even when empty


def Byte(value: int) -> Tag:
    return Tag(1, value)


def Int(value: int) -> Tag:
    return Tag(3, value)


def String(value: str) -> Tag:
    return Tag(8, value)


def ListTag(values: list[Tag], subtype: int = 10) -> Tag:
    return Tag(9, values, subtype)


def Compound(value: dict[str, Tag] | None = None) -> Tag:
    return Tag(10, {} if value is None else value)


def LongArray(value: list[int]) -> Tag:
    return Tag(12, value)


def require(compound: Tag, name: str, tag_id: int) -> Tag:
    if compound.id != 10:
        raise FormatError("Expected compound tag")
    tag = compound.value.get(name)
    if tag is None or tag.id != tag_id:
        raise FormatError(f"Tag {name!r} must have NBT type {tag_id}")
    return tag


def compound_list(tag: Tag) -> list[Tag]:
    if tag.id != 9 or (tag.subtype != 10 and (tag.value or tag.subtype != 0)):
        raise FormatError("Expected list of compounds")
    return tag.value


def _encode_string(text: str) -> bytes:
    units = text.encode("utf-16-be", "surrogatepass")
    output = bytearray()
    for i in range(0, len(units), 2):
        c = (units[i] << 8) | units[i + 1]
        if 0 < c < 128:
            output.append(c)
        elif c < 2048:
            output.extend((0xC0 | (c >> 6), 0x80 | (c & 63)))
        else:
            output.extend((0xE0 | (c >> 12), 0x80 | ((c >> 6) & 63), 0x80 | (c & 63)))
    if len(output) > 65535:
        raise FormatError("NBT string exceeds 65535 encoded bytes")
    return struct.pack(">H", len(output)) + output


def _decode_string(data: bytes) -> str:
    # Modified UTF-8 encodes NUL as C0 80 and supplementary characters as
    # two three-byte surrogate code units, as Java DataInput.readUTF does.
    if any(byte >= 0xF0 for byte in data):
        raise FormatError("Four-byte UTF-8 is invalid in Java modified UTF-8 strings")
    text = data.replace(b"\xc0\x80", b"\0").decode("utf-8", "surrogatepass")
    return text.encode("utf-16-be", "surrogatepass").decode("utf-16-be", "surrogatepass")


SCALARS = {1: ">b", 2: ">h", 3: ">i", 4: ">q", 5: ">f", 6: ">d"}


class _Reader:
    def __init__(self, data: bytes):
        self.stream = BytesIO(data)
        self.remaining_tags = MAX_TAGS
        self.size = len(data)

    def read(self, count: int) -> bytes:
        if count < 0 or count > self.size - self.stream.tell():
            raise FormatError("Truncated NBT payload")
        return self.stream.read(count)

    def number(self, fmt: str) -> Any:
        return struct.unpack(fmt, self.read(struct.calcsize(fmt)))[0]

    def string(self) -> str:
        return _decode_string(self.read(self.number(">H")))

    def payload(self, tag_id: int, depth: int = 0) -> Tag:
        self.remaining_tags -= 1
        if depth > MAX_DEPTH or self.remaining_tags < 0:
            raise FormatError("NBT nesting or tag-count safety limit exceeded")
        if tag_id in SCALARS:
            return Tag(tag_id, self.number(SCALARS[tag_id]))
        if tag_id == 8:
            return String(self.string())
        if tag_id in (7, 11, 12):
            count = self.number(">i")
            if count < 0:
                raise FormatError("Negative NBT array length")
            width = {7: 1, 11: 4, 12: 8}[tag_id]
            raw = self.read(count * width)
            return Tag(
                tag_id,
                raw
                if tag_id == 7
                else [v[0] for v in struct.iter_unpack(">i" if tag_id == 11 else ">q", raw)],
            )
        if tag_id == 9:
            subtype, count = self.number(">B"), self.number(">i")
            if count < 0 or subtype > 12 or (subtype == 0 and count) or count > self.remaining_tags:
                raise FormatError("Invalid NBT list type or length")
            return ListTag([self.payload(subtype, depth + 1) for _ in range(count)], subtype)
        if tag_id == 10:
            children: dict[str, Tag] = {}
            while True:
                child_id = self.number(">B")
                if child_id == 0:
                    return Compound(children)
                name = self.string()
                if name in children:
                    raise FormatError(f"Duplicate NBT compound key {name!r}")
                children[name] = self.payload(child_id, depth + 1)
        raise FormatError(f"Unknown NBT tag type {tag_id}")


@dataclass
class Document:
    root: Tag
    name: str = ""

    @classmethod
    def parse(cls, data: bytes) -> "Document":
        if len(data) > MAX_NBT_BYTES:
            raise FormatError("NBT exceeds 64 MiB safety limit")
        try:
            reader = _Reader(data)
            if reader.number(">B") != 10:
                raise FormatError("Chunk NBT root must be TAG_Compound")
            name = reader.string()
            root = reader.payload(10)
            if reader.stream.tell() != len(data):
                raise FormatError("Trailing bytes after root NBT compound")
            return cls(root, name)
        except (UnicodeError, struct.error, RecursionError) as exc:
            raise FormatError(f"Malformed NBT: {exc}") from exc

    def serialize(self) -> bytes:
        stream = BytesIO()
        budget = MAX_TAGS

        def write_payload(tag: Tag, depth: int = 0) -> None:
            nonlocal budget
            budget -= 1
            if depth > MAX_DEPTH or budget < 0 or stream.tell() > MAX_NBT_BYTES:
                raise FormatError("NBT serialization safety limit exceeded")
            if tag.id in SCALARS:
                stream.write(struct.pack(SCALARS[tag.id], tag.value))
            elif tag.id == 8:
                stream.write(_encode_string(tag.value))
            elif tag.id in (7, 11, 12):
                stream.write(struct.pack(">i", len(tag.value)))
                if tag.id == 7:
                    stream.write(tag.value)
                else:
                    fmt = ">i" if tag.id == 11 else ">q"
                    for number in tag.value:
                        stream.write(struct.pack(fmt, number))
            elif tag.id == 9:
                if not 0 <= tag.subtype <= 12 or (tag.subtype == 0 and tag.value):
                    raise FormatError("Invalid list subtype")
                stream.write(struct.pack(">Bi", tag.subtype, len(tag.value)))
                for child in tag.value:
                    if child.id != tag.subtype:
                        raise FormatError("NBT list contains mixed tag types")
                    write_payload(child, depth + 1)
            elif tag.id == 10:
                for name, child in tag.value.items():
                    stream.write(struct.pack(">B", child.id))
                    stream.write(_encode_string(name))
                    write_payload(child, depth + 1)
                stream.write(b"\0")
            else:
                raise FormatError(f"Cannot serialize NBT tag type {tag.id}")

        try:
            if self.root.id != 10:
                raise FormatError("Root must be a compound")
            stream.write(b"\x0a" + _encode_string(self.name))
            write_payload(self.root)
            data = stream.getvalue()
            if len(data) > MAX_NBT_BYTES:
                raise FormatError("NBT exceeds 64 MiB safety limit")
            return data
        except (struct.error, OverflowError, TypeError, UnicodeError, RecursionError) as exc:
            raise FormatError(f"Cannot serialize NBT: {exc}") from exc
