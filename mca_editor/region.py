"""Strict Anvil header validation and atomic sector-rebuilding saves."""

import hashlib
import os
import re
import shutil
import struct
import tempfile
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from .block import Block
from .chunk import Chunk
from .compression import check_codec, compress, decompress
from .coordinates import chunk_to_region, integers, slot_index, world_to_chunk
from .exceptions import (
    CoordinateError,
    FormatError,
    MCAError,
    MissingChunkError,
    SaveError,
    UnsupportedFormatError,
)
from .nbt_utils import Document, Tag
from .section import Section

SECTOR_BYTES = 4096
HEADER_BYTES = 8192


@dataclass(frozen=True)
class ChunkLocation:
    offset: int
    sectors: int
    timestamp: int


@dataclass
class _Record:
    location: ChunkLocation
    codec: int
    record: bytes  # length field + codec + compressed payload (not sector padding)
    chunk: Chunk


def _digest(path: Path) -> bytes:
    with path.open("rb") as stream:
        h = hashlib.sha256()
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(data)
        return h.digest()


class Region:
    def __init__(
        self,
        path: str | os.PathLike,
        *,
        region_coords: tuple[int, int] | None = None,
        dimension: str = "minecraft:overworld",
        metadata_policy: str = "invalidate",
        block_entity_policy: str = "error",
    ):
        self.path = Path(path)
        if region_coords is not None:
            if len(region_coords) != 2:
                raise CoordinateError("region_coords must be an (x, z) pair")
            integers(*region_coords)
        match = re.match(r"^r\.(-?\d+)\.(-?\d+)(?:\.|$)", self.path.name)
        self.region_coords = (
            region_coords
            if region_coords is not None
            else (tuple(map(int, match.groups())) if match else None)
        )
        if dimension not in ("minecraft:overworld", "minecraft:the_nether", "minecraft:the_end"):
            raise CoordinateError(
                f"Unsupported dimension {dimension!r}; custom heights are not supported"
            )
        if metadata_policy not in ("invalidate", "preserve") or block_entity_policy not in (
            "error",
            "remove",
        ):
            raise ValueError("Invalid metadata_policy or block_entity_policy")
        self.dimension = dimension
        self.metadata_policy = metadata_policy
        self.block_entity_policy = block_entity_policy
        self._records: dict[int, _Record] = {}
        self._timestamps = [0] * 1024
        self._loaded = False
        self._source_digest: bytes | None = None

    def load(self) -> "Region":
        """Validate all allocated records and chunks; return self for chaining.

        Failure leaves an already-loaded object unchanged. Unsupported chunks
        are not silently skipped, even when only another chunk would be edited.
        """
        records = {}
        with self.path.open("rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            if size < HEADER_BYTES or size % SECTOR_BYTES:
                raise FormatError("Region must contain an 8 KiB header and whole 4 KiB sectors")
            header = stream.read(HEADER_BYTES)
            timestamps = list(struct.unpack(">1024I", header[SECTOR_BYTES:]))
            occupied = {0, 1}
            locations = {}
            for slot in range(1024):
                value = int.from_bytes(header[slot * 4 : slot * 4 + 4], "big")
                offset, count = value >> 8, value & 255
                if not value:
                    continue
                if offset < 2 or not count or (offset + count) * SECTOR_BYTES > size:
                    raise FormatError(
                        f"Invalid sector allocation in slot {slot}: offset={offset}, sectors={count}"
                    )
                sectors = set(range(offset, offset + count))
                if sectors & occupied:
                    raise FormatError(f"Overlapping chunk allocation in slot {slot}")
                occupied.update(sectors)
                locations[slot] = ChunkLocation(offset, count, timestamps[slot])
            coords = self.region_coords
            for slot, location in locations.items():
                stream.seek(location.offset * SECTOR_BYTES)
                length_raw = stream.read(4)
                length = int.from_bytes(length_raw, "big")
                if not 1 <= length <= location.sectors * SECTOR_BYTES - 4:
                    raise FormatError(f"Invalid chunk record length {length} in slot {slot}")
                data = stream.read(length)
                if len(data) != length:
                    raise FormatError(f"Truncated chunk in slot {slot}")
                codec = data[0]
                try:
                    check_codec(codec)
                    document = Document.parse(decompress(data[1:], codec))
                    chunk = Chunk(
                        document,
                        dimension=self.dimension,
                        metadata_policy=self.metadata_policy,
                        block_entity_policy=self.block_entity_policy,
                    )
                    actual_coords = chunk_to_region(chunk.x, chunk.z)
                    if coords is None:
                        coords = actual_coords
                    if actual_coords != coords or slot_index(chunk.x, chunk.z) != slot:
                        raise FormatError(
                            f"Chunk coordinates ({chunk.x}, {chunk.z}) disagree with region {coords}, slot {slot}"
                        )
                    records[slot] = _Record(location, codec, length_raw + data, chunk)
                except MCAError as exc:
                    raise type(exc)(f"Slot {slot}: {exc}") from exc
            # Hash the exact open file, not a possibly replaced path.
            stream.seek(0)
            h = hashlib.sha256()
            for data in iter(lambda: stream.read(1024 * 1024), b""):
                h.update(data)
        self._records, self._timestamps = records, timestamps
        self.region_coords, self._source_digest, self._loaded = coords, h.digest(), True
        return self

    def _require_loaded(self) -> None:
        if not self._loaded:
            raise MCAError("Call Region.load() before accessing or saving chunks")

    def _check_chunk(self, chunk_x: int, chunk_z: int) -> int:
        self._require_loaded()
        integers(chunk_x, chunk_z)
        if self.region_coords is None:
            raise CoordinateError(
                "Empty region has no inferable coordinates; provide region_coords=(x, z)"
            )
        if chunk_to_region(chunk_x, chunk_z) != self.region_coords:
            raise CoordinateError(
                f"Chunk ({chunk_x}, {chunk_z}) is outside region {self.region_coords}"
            )
        return slot_index(chunk_x, chunk_z)

    @property
    def chunk_count(self) -> int:
        self._require_loaded()
        return len(self._records)

    @property
    def timestamps(self) -> tuple[int, ...]:
        self._require_loaded()
        return tuple(self._timestamps)

    @property
    def locations(self) -> dict[int, ChunkLocation]:
        self._require_loaded()
        return {slot: record.location for slot, record in self._records.items()}

    def iter_chunks(self) -> Iterator[Chunk]:
        self._require_loaded()
        for slot in sorted(self._records):
            yield self._records[slot].chunk

    def get_chunk(self, chunk_x: int, chunk_z: int) -> Chunk:
        slot = self._check_chunk(chunk_x, chunk_z)
        if slot not in self._records:
            raise MissingChunkError(
                f"No chunk at ({chunk_x}, {chunk_z}); chunk generation is not supported"
            )
        return self._records[slot].chunk

    def get_section(self, chunk_x: int, chunk_z: int, section_y: int) -> Section:
        return self.get_chunk(chunk_x, chunk_z).get_section(section_y)

    def create_section(
        self, chunk_x: int, chunk_z: int, section_y: int, *, biome: str | None = None
    ) -> Section:
        return self.get_chunk(chunk_x, chunk_z).create_section(section_y, biome=biome)

    def get_block(self, x: int, y: int, z: int) -> Block:
        integers(x, y, z)
        return self.get_chunk(*world_to_chunk(x, z)).get_block(x % 16, y, z % 16)

    def get_block_data(self, x: int, y: int, z: int) -> Tag:
        """Detached palette-entry NBT, including unknown palette-entry tags."""
        integers(x, y, z)
        return self.get_chunk(*world_to_chunk(x, z)).get_block_data(x % 16, y, z % 16)

    def set_block(
        self, x: int, y: int, z: int, block_name: str, properties: Mapping[str, str] | None = None
    ) -> None:
        integers(x, y, z)
        self.get_chunk(*world_to_chunk(x, z)).set_block(x % 16, y, z % 16, block_name, properties)

    def _cuboid(
        self, start: tuple[int, int, int], end: tuple[int, int, int]
    ) -> Iterator[tuple[int, int, int]]:
        if len(start) != 3 or len(end) != 3:
            raise CoordinateError("Cuboid corners must each have three coordinates")
        integers(*start, *end)
        if any(a > b for a, b in zip(start, end)):
            raise CoordinateError("Cuboid start must be <= end on each axis (bounds are inclusive)")
        for cx in range(start[0] // 16, end[0] // 16 + 1):
            for cz in range(start[2] // 16, end[2] // 16 + 1):
                chunk = self.get_chunk(cx, cz)
                chunk._check_y(start[1])
                chunk._check_y(end[1])
        for y in range(start[1], end[1] + 1):
            for z in range(start[2], end[2] + 1):
                for x in range(start[0], end[0] + 1):
                    yield x, y, z

    def iter_blocks(
        self, start: tuple[int, int, int], end: tuple[int, int, int]
    ) -> Iterator[tuple[tuple[int, int, int], Block]]:
        """Inclusive cuboid, yielding ((world_x, world_y, world_z), Block)."""
        for xyz in self._cuboid(start, end):
            yield xyz, self.get_block(*xyz)

    def fill(
        self,
        start: tuple[int, int, int],
        end: tuple[int, int, int],
        block_name: str,
        properties: Mapping[str, str] | None = None,
    ) -> int:
        """Preflight all targets before editing; never crosses this region's boundary."""
        block = Block(block_name, {} if properties is None else properties)
        for x, y, z in self._cuboid(start, end):
            self.get_chunk(x // 16, z // 16)._edit_target(x % 16, y, z % 16, block)
        count = 0
        for xyz in self._cuboid(start, end):
            self.set_block(*xyz, block_name, properties)
            count += 1
        return count

    def save(self, path: str | os.PathLike | None = None, *, backup: bool = False) -> Path:
        """Write, fsync, fully reparse, then atomically replace the destination.

        Unmodified compressed records and timestamps are reused verbatim. Sector
        padding and allocation order may change. backup=True creates <target>.bak
        without overwriting an existing backup. The loaded source path is not
        changed by a save-as; in-memory edits remain dirty for subsequent saves.
        """
        self._require_loaded()
        target = self.path if path is None else Path(path)
        target = target.absolute()
        temporary: Path | None = None
        backup_created = False
        backup_path = Path(str(target) + ".bak")
        try:
            named_coords = re.match(r"^r\.(-?\d+)\.(-?\d+)(?:\.|$)", target.name)
            if (
                named_coords
                and self.region_coords is not None
                and tuple(map(int, named_coords.groups())) != self.region_coords
            ):
                raise SaveError(
                    f"Destination filename disagrees with region coordinates {self.region_coords}"
                )
            if target.is_symlink():
                raise SaveError("Refusing to replace a symlink destination")
            replaces_source_path = target.resolve() == self.path.resolve()
            in_place = replaces_source_path or (
                target.exists() and self.path.exists() and os.path.samefile(target, self.path)
            )
            if in_place and _digest(self.path) != self._source_digest:
                raise SaveError("Source changed on disk since load; reload before overwriting")
            before = _digest(target) if target.exists() else None
            fd, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
            temporary = Path(name)
            expected = {}
            with os.fdopen(fd, "w+b") as stream:
                stream.write(bytes(HEADER_BYTES))
                header = bytearray(HEADER_BYTES)
                struct.pack_into(">1024I", header, SECTOR_BYTES, *self._timestamps)
                offset = 2
                now = min(int(time.time()), 0xFFFFFFFF)
                for slot, record in sorted(self._records.items()):
                    payload = record.record
                    timestamp = record.location.timestamp
                    if record.chunk.modified:
                        compressed = compress(record.chunk.serialize(), record.codec)
                        payload = (
                            struct.pack(">I", len(compressed) + 1)
                            + bytes([record.codec])
                            + compressed
                        )
                        timestamp = now
                    sectors = (len(payload) + SECTOR_BYTES - 1) // SECTOR_BYTES
                    if sectors > 255:
                        raise UnsupportedFormatError(
                            f"Chunk slot {slot} requires {sectors} sectors; oversized external chunks are unsupported"
                        )
                    if offset > 0xFFFFFF or offset + sectors > 0x1000000:
                        raise SaveError("Region exceeds 24-bit sector offset limit")
                    struct.pack_into(">I", header, slot * 4, (offset << 8) | sectors)
                    struct.pack_into(">I", header, SECTOR_BYTES + slot * 4, timestamp)
                    stream.write(payload)
                    stream.write(bytes(sectors * SECTOR_BYTES - len(payload)))
                    expected[slot] = payload
                    offset += sectors
                stream.seek(0)
                stream.write(header)
                stream.flush()
                os.fsync(stream.fileno())
            verified = Region(
                temporary,
                region_coords=self.region_coords,
                dimension=self.dimension,
                metadata_policy=self.metadata_policy,
                block_entity_policy=self.block_entity_policy,
            ).load()
            if set(verified._records) != set(expected) or any(
                verified._records[s].record != p for s, p in expected.items()
            ):
                raise SaveError("Written region failed record verification")
            if target.exists():
                os.chmod(temporary, target.stat().st_mode & 0o777)
            if (_digest(target) if target.exists() else None) != before:
                raise SaveError("Destination changed during save; refusing to replace it")
            if backup and target.exists():
                # Exclusive create: an existing backup is never overwritten.
                with backup_path.open("xb") as out:
                    backup_created = True
                    with target.open("rb") as source:
                        shutil.copyfileobj(source, out)
                    out.flush()
                    os.fsync(out.fileno())
            saved_digest = _digest(temporary)
            os.replace(temporary, target)  # Commit point; same filesystem.
            temporary = None
            if replaces_source_path:
                self._source_digest = saved_digest
            return target
        except (OSError, MCAError, ValueError) as exc:
            if backup_created:
                backup_path.unlink(missing_ok=True)
            if isinstance(exc, MCAError):
                raise
            raise SaveError(f"Could not safely save {target}: {exc}") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
