"""Deterministic synthetic fixtures, not a terrain/world generator.

These construct real binary Anvil records but intentionally do not provide a
complete playable world (level.dat, world generation, structures, etc.).
"""

import struct
from pathlib import Path

from .block import AIR, Block
from .compression import compress
from .coordinates import slot_index
from .nbt_utils import Byte, Compound, Document, Int, ListTag, LongArray, String, Tag
from .packed_array import bit_width, pack
from .versions import Version


def make_chunk(
    x: int = 0,
    z: int = 0,
    *,
    data_version: int = 3465,
    section_y: int = 4,
    blocks: list[Block] | None = None,
    indices: list[int] | None = None,
) -> Document:
    version = Version(data_version)
    blocks = [AIR] if blocks is None else blocks
    indices = [0] * 4096 if indices is None else indices
    palette = ListTag([b.to_nbt() for b in blocks])
    packed = LongArray(pack(indices, bit_width(len(blocks)), padded=version.padded))
    section = Compound({"Y": Byte(section_y), "fixture_section_tag": String("preserve me")})
    if version.modern:
        storage = Compound({"palette": palette})
        if len(blocks) > 1:
            storage.value["data"] = packed
        section.value.update(
            {
                "block_states": storage,
                "biomes": Compound({"palette": ListTag([String("minecraft:plains")], 8)}),
            }
        )
    else:
        section.value.update({"Palette": palette, "BlockStates": packed})
    level = Compound(
        {
            "xPos": Int(x),
            "zPos": Int(z),
            "Status": String("minecraft:full" if version.modern else "full"),
            "LastUpdate": Tag(4, 123456789),
            "InhabitedTime": Tag(4, 42),
            "sections" if version.modern else "Sections": ListTag([section]),
            "block_entities" if version.modern else "TileEntities": ListTag([]),
            "Heightmaps": Compound(),
            "isLightOn": Byte(0),
            "fixture_unknown": Compound({"bytes": Tag(7, b"\0\xff\x80"), "short": Tag(2, -123)}),
        }
    )
    if version.modern:
        level.value.update({"DataVersion": Int(data_version), "yPos": Int(-4)})
        return Document(level)
    return Document(Compound({"DataVersion": Int(data_version), "Level": level}))


def write_region(
    path: str | Path, chunks: list[Document], *, codec: int = 2, timestamp: int = 1_600_000_000
) -> Path:
    """Write fresh synthetic fixtures only; refuse to overwrite any file."""
    path = Path(path)
    header = bytearray(8192)
    body = bytearray()
    occupied = set()
    for document in chunks:
        root = document.root
        level = root.value.get("Level", root)
        slot = slot_index(level.value["xPos"].value, level.value["zPos"].value)
        if slot in occupied:
            raise ValueError("Duplicate fixture slot")
        occupied.add(slot)
        data = compress(document.serialize(), codec)
        record = struct.pack(">I", len(data) + 1) + bytes([codec]) + data
        sectors = (len(record) + 4095) // 4096
        if sectors > 255:
            raise ValueError("Synthetic chunk too large")
        offset = 2 + len(body) // 4096
        struct.pack_into(">I", header, slot * 4, (offset << 8) | sectors)
        struct.pack_into(">I", header, 4096 + slot * 4, timestamp)
        body.extend(record)
        body.extend(bytes(sectors * 4096 - len(record)))
    with path.open("xb") as stream:
        stream.write(header)
        stream.write(body)
    return path
