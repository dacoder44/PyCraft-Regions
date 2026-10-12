"""A validated 16³ section with a local disk palette."""

from copy import deepcopy

from .block import AIR, Block
from .coordinates import local_index
from .exceptions import FormatError
from .nbt_utils import Compound, ListTag, LongArray, Tag, compound_list, require
from .packed_array import bit_width, pack, unpack
from .versions import Version


class Section:
    def __init__(self, tag: Tag, version: Version):
        self._tag = tag
        self.version = version
        self.y = require(tag, "Y", 1).value
        self.dirty = False
        self._container = tag
        self._palette_key, self._data_key = "Palette", "BlockStates"
        if version.modern:
            self._palette_key, self._data_key = "palette", "data"
            if "block_states" not in tag.value:
                self._container = None
            else:
                self._container = require(tag, "block_states", 10)
        elif "Blocks" in tag.value or "Data" in tag.value:
            raise FormatError("Legacy numerical block storage in a palette-era chunk")
        container = self._container
        if container is None or self._palette_key not in container.value:
            if container is not None and (self._data_key in container.value or version.modern):
                raise FormatError("Block state storage has data/container but no palette")
            self._palette = [AIR.to_nbt()]
            self._blocks = [AIR]
            self._indices = [0] * 4096
            self.has_block_states = False
        else:
            self._palette = compound_list(require(container, self._palette_key, 9))
            bits = bit_width(len(self._palette))
            self._blocks = [Block.from_nbt(entry) for entry in self._palette]
            data = container.value.get(self._data_key)
            if data is None:
                if len(self._palette) != 1 or not version.modern:
                    raise FormatError(
                        "Missing packed block data (only modern singleton palettes may omit it)"
                    )
                self._indices = [0] * 4096
            else:
                if data.id != 12:
                    raise FormatError("Packed block data must be TAG_Long_Array")
                if version.modern and len(self._palette) == 1 and not data.value:
                    self._indices = [0] * 4096
                else:
                    self._indices = unpack(data.value, bits, padded=version.padded)
                if any(index >= len(self._palette) for index in self._indices):
                    raise FormatError("Packed block index outside palette")
            self.has_block_states = True
        self._lookup = {block.key: i for i, block in enumerate(self._blocks)}

    @property
    def palette(self) -> tuple[Block, ...]:
        return tuple(self._blocks)

    @property
    def bits_per_block(self) -> int:
        return 0 if self.version.modern and len(self._blocks) == 1 else bit_width(len(self._blocks))

    @property
    def nbt(self) -> Tag:
        """Detached, typed NBT snapshot. Mutating it does not edit the section."""
        self._flush()
        return deepcopy(self._tag)

    def get_block(self, x: int, y: int, z: int) -> Block:
        """Read using section-local coordinates, all in 0..15."""
        return self._blocks[self._indices[local_index(x, y, z)]]

    def get_block_data(self, x: int, y: int, z: int) -> Tag:
        return deepcopy(self._palette[self._indices[local_index(x, y, z)]])

    def _set_block(self, x: int, y: int, z: int, block: Block) -> None:
        index = local_index(x, y, z)
        if self._blocks[self._indices[index]] == block:
            return
        palette_index = self._lookup.get(block.key)
        if palette_index is None:
            if len(self._palette) == 4096:
                # Reclaim a slot that will be unused after this edit. There are
                # only 4096 cells, so at least one such slot must exist.
                used = set(self._indices[:index] + self._indices[index + 1 :])
                palette_index = next(i for i in range(4096) if i not in used)
                self._palette[palette_index] = block.to_nbt()
                self._blocks[palette_index] = block
                self._lookup = {b.key: i for i, b in enumerate(self._blocks)}
            else:
                palette_index = len(self._palette)
                self._palette.append(block.to_nbt())
                self._blocks.append(block)
                self._lookup[block.key] = palette_index
        self._indices[index] = palette_index
        self.dirty = True

    def _flush(self) -> None:
        if not self.dirty:
            return
        if self._container is None:
            self._container = Compound()
            self._tag.value["block_states"] = self._container
        self._container.value[self._palette_key] = ListTag(self._palette)
        if self.version.modern and len(self._palette) == 1:
            self._container.value.pop(self._data_key, None)
        else:
            self._container.value[self._data_key] = LongArray(
                pack(self._indices, bit_width(len(self._palette)), padded=self.version.padded)
            )
        self.has_block_states = True
        self.dirty = False
