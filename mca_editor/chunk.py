"""Chunk schema dispatch and edits that retain unrelated NBT."""

from collections.abc import Mapping
from copy import deepcopy

from .block import AIR, ID_PATTERN, Block
from .coordinates import integers
from .exceptions import (
    CoordinateError,
    FormatError,
    MissingSectionError,
    UnsafeEditError,
    UnsupportedFormatError,
)
from .nbt_utils import Byte, Compound, Document, ListTag, String, Tag, compound_list, require
from .section import Section
from .versions import Version

HEIGHTMAP_TYPES = {
    "WORLD_SURFACE_WG",
    "WORLD_SURFACE",
    "OCEAN_FLOOR_WG",
    "OCEAN_FLOOR",
    "MOTION_BLOCKING",
    "MOTION_BLOCKING_NO_LEAVES",
}


class Chunk:
    def __init__(
        self,
        document: Document,
        *,
        dimension: str = "minecraft:overworld",
        metadata_policy: str = "invalidate",
        block_entity_policy: str = "error",
    ):
        self._document = document
        self.version = Version(require(document.root, "DataVersion", 3).value)
        self._level = document.root if self.version.modern else require(document.root, "Level", 10)
        self.x = require(self._level, "xPos", 3).value
        self.z = require(self._level, "zPos", 3).value
        self.min_y, self.max_y = self.version.bounds(dimension)
        if self.version.modern and "yPos" in self._level.value:
            stored_min = require(self._level, "yPos", 3).value
            if stored_min != self.min_y // 16:
                raise UnsupportedFormatError(
                    f"Chunk yPos={stored_min} disagrees with {dimension} minimum section "
                    f"{self.min_y // 16}; select the correct dimension (custom heights are unsupported)"
                )
        if "Heightmaps" in self._level.value:
            require(self._level, "Heightmaps", 10)
        entity_key = "block_entities" if self.version.modern else "TileEntities"
        if entity_key in self._level.value:
            for entity in compound_list(require(self._level, entity_key, 9)):
                for axis in ("x", "y", "z"):
                    require(entity, axis, 3)
        if metadata_policy not in ("invalidate", "preserve"):
            raise ValueError("metadata_policy must be 'invalidate' or 'preserve'")
        if block_entity_policy not in ("error", "remove"):
            raise ValueError("block_entity_policy must be 'error' or 'remove'")
        self.metadata_policy = metadata_policy
        self.block_entity_policy = block_entity_policy
        self._section_key = "sections" if self.version.modern else "Sections"
        self._sections: dict[int, Section] = {}
        for tag in compound_list(require(self._level, self._section_key, 9)):
            section = Section(tag, self.version)
            if section.has_block_states and not self.min_y <= section.y * 16 < self.max_y:
                raise UnsupportedFormatError(
                    f"Block section Y={section.y} is outside {dimension} build height; "
                    "custom dimension heights are unsupported"
                )
            if section.y in self._sections:
                raise FormatError(f"Duplicate section Y={section.y} in chunk ({self.x}, {self.z})")
            self._sections[section.y] = section
        self.modified = False

    @property
    def data_version(self) -> int:
        return self.version.data_version

    @property
    def status(self) -> str | None:
        tag = self._level.value.get("Status")
        if tag is not None and tag.id != 8:
            raise FormatError("Chunk Status must be a string")
        return None if tag is None else tag.value

    @property
    def sections(self) -> tuple[Section, ...]:
        return tuple(self._sections[y] for y in sorted(self._sections))

    @property
    def nbt(self) -> Tag:
        """Detached snapshot, including unknown tags and original numeric types."""
        self._flush()
        return deepcopy(self._document.root)

    def _flush(self) -> None:
        for section in self._sections.values():
            section._flush()

    def serialize(self) -> bytes:
        self._flush()
        return self._document.serialize()

    def _check_y(self, y: int) -> None:
        integers(y)
        if not self.min_y <= y < self.max_y:
            raise CoordinateError(
                f"Y={y} outside dimension bounds [{self.min_y}, {self.max_y}) for {self.version.name}"
            )

    def _check_local(self, x: int, y: int, z: int) -> None:
        integers(x, y, z)
        self._check_y(y)
        if not (0 <= x < 16 and 0 <= z < 16):
            raise CoordinateError("Chunk-local X/Z must be in 0..15; Y is absolute")

    def get_section(self, section_y: int) -> Section:
        integers(section_y)
        try:
            return self._sections[section_y]
        except KeyError:
            raise MissingSectionError(
                f"No section Y={section_y} in chunk ({self.x}, {self.z}); call create_section explicitly"
            ) from None

    def create_section(self, section_y: int, *, biome: str | None = None) -> Section:
        """Explicitly create an all-air section in an existing chunk.

        Modern sections require an explicit single biome rather than guessing.
        No chunk terrain, entities, or structure metadata are generated.
        """
        integers(section_y)
        self._check_y(section_y * 16)
        if section_y in self._sections:
            raise UnsafeEditError(f"Section {section_y} already exists")
        if self.version.modern and (biome is None or not ID_PATTERN.fullmatch(biome)):
            raise UnsafeEditError("Creating a modern section requires an explicit namespaced biome")
        tag = Compound({"Y": Byte(section_y)})
        if self.version.modern:
            tag.value["biomes"] = Compound({"palette": ListTag([String(biome)], 8)})
        section = Section(tag, self.version)
        section.dirty = True  # Serialize a valid singleton air palette.
        section._flush()
        self._sections[section_y] = section
        sections_tag = require(self._level, self._section_key, 9)
        sections_tag.subtype = 10
        sections_tag.value.append(tag)
        self.modified = True
        self._invalidate_metadata()
        return section

    def get_block(self, x: int, y: int, z: int) -> Block:
        """Chunk-local X/Z; world Y. An absent section is implicit air."""
        self._check_local(x, y, z)
        section = self._sections.get(y // 16)
        return AIR if section is None else section.get_block(x, y % 16, z)

    def get_block_data(self, x: int, y: int, z: int) -> Tag:
        self._check_local(x, y, z)
        section = self._sections.get(y // 16)
        return AIR.to_nbt() if section is None else section.get_block_data(x, y % 16, z)

    def _edit_target(self, x: int, y: int, z: int, block: Block) -> tuple[Section, list[Tag]]:
        self._check_local(x, y, z)
        section = self.get_section(y // 16)
        matching = []
        key = "block_entities" if self.version.modern else "TileEntities"
        if key in self._level.value:
            for entity in compound_list(require(self._level, key, 9)):
                position = tuple(require(entity, c, 3).value for c in ("x", "y", "z"))
                if position == (self.x * 16 + x, y, self.z * 16 + z):
                    matching.append(entity)
        if matching and self.block_entity_policy == "error" and self.get_block(x, y, z) != block:
            raise UnsafeEditError(
                "Block has a block entity; use block_entity_policy='remove' to remove only the affected entity"
            )
        return section, matching

    def set_block(
        self, x: int, y: int, z: int, block_name: str, properties: Mapping[str, str] | None = None
    ) -> None:
        block = Block(block_name, {} if properties is None else properties)
        section, entities = self._edit_target(x, y, z, block)
        if section.get_block(x, y % 16, z) == block:
            return
        if entities:
            key = "block_entities" if self.version.modern else "TileEntities"
            ids = {id(entity) for entity in entities}
            tag = self._level.value[key]
            tag.value[:] = [entity for entity in tag.value if id(entity) not in ids]
        section._set_block(x, y % 16, z, block)
        self.modified = True
        self._invalidate_metadata()

    def _invalidate_metadata(self) -> None:
        if self.metadata_policy == "preserve":
            return
        # Preserve unknown heightmap keys. Minecraft primes absent recognized
        # heightmaps from block states when loading the chunk.
        heightmaps = self._level.value.get("Heightmaps")
        if heightmaps is not None:
            if heightmaps.id != 10:
                raise FormatError("Heightmaps must be a compound")
            for key in HEIGHTMAP_TYPES:
                heightmaps.value.pop(key, None)
        self._level.value["isLightOn"] = Byte(0)
        if "LightPopulated" in self._level.value:
            self._level.value["LightPopulated"] = Byte(0)
