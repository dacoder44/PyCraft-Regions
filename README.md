# PyCraft Regions / `mca_editor`

A Python library and CLI for **Minecraft Java Edition Anvil (`.mca`) files**.
Reads real region headers, decompresses chunks, preserves typed NBT, decodes
version-specific palettes, edits individual block states, and writes validated
region files with rebuilt sector allocations. No runtime dependencies; Python
**3.10+**.

**Stop Minecraft/the server before editing. Work on a copy of the world.** This
is an offline block-storage editor, not a terrain generator or Minecraft's game
engine. See the compatibility and metadata policies below before using it on a
world you care about.

## Install

From this repository:

```sh
python -m venv .venv
source .venv/bin/activate                 # Windows: .venv\Scripts\activate
python -m pip install -e .               # library and mca-editor CLI
python -m pip install -e '.[test]'        # additionally install pytest + nbtlib
```

`requirements.txt` contains the test dependencies as an alternative. The actual
library uses only the Python standard library. `nbtlib` is an independent test
oracle, not the production codec.

## Python API

```python
from mca_editor import Region

region = Region("r.0.0.mca").load()
block = region.get_block(0, 64, 0)
print(block.name, dict(block.properties))

region.set_block(0, 64, 0, "minecraft:diamond_block")
region.set_block(1, 64, 0, "minecraft:oak_log", properties={"axis": "y"})
region.save("r.0.0.modified.mca")

reopened = Region("r.0.0.modified.mca").load()
assert reopened.get_block(0, 64, 0).name == "minecraft:diamond_block"
assert reopened.get_block(1, 64, 0).properties == {"axis": "y"}
```

### Inspection, iteration and filling

```python
chunk = region.get_chunk(0, 0)  # ABSOLUTE chunk coordinates
print(chunk.x, chunk.z, chunk.data_version, chunk.version.name, chunk.status)
print([(s.y, len(s.palette), s.bits_per_block) for s in chunk.sections])
section = region.get_section(0, 0, 4)  # section Y=4 covers world Y=64..79
print(section.get_block(1, 0, 0))  # section-local x/y/z in 0..15
print(chunk.get_block(1, 64, 0))  # chunk-local x/z, absolute world Y

entry = region.get_block_data(1, 64, 0)  # detached typed palette-entry NBT
print(entry.value["Name"].value)
root = chunk.nbt  # detached full typed NBT snapshot

for chunk in region.iter_chunks():
    print(chunk.x, chunk.z)
for position, block in region.iter_blocks((0, 64, 0), (10, 65, 10)):
    print(position, block.name)

# Both corners inclusive; preflights missing chunks/sections/entity conflicts.
region.fill((14, 64, 14), (17, 65, 17), "minecraft:stone")
region.save("filled.mca")
```

`Region.get_block/set_block/get_block_data/iter_blocks/fill` use **world
coordinates**. Chunk arguments to `get_chunk`, `get_section`, and
`create_section` are **absolute chunk coordinates**, not slot indices. Floor
division and modulo handle negatives: world X=-1 → chunk X=-1 → local X=15.
Section index order is `y * 256 + z * 16 + x` (X fastest).

A `Region` operates on **one file**, not the whole world. Operations can cross
chunk/section boundaries inside that region, but crossing a region boundary
raises `CoordinateError`; open the neighboring region explicitly. Missing chunks
raise `MissingChunkError` and are never fabricated. Missing sections read as
`minecraft:air`, but edits require explicit creation:

```python
# Only when the section does not already exist. Modern versions require a biome.
region.create_section(0, 0, -1, biome="minecraft:plains")
region.set_block(0, -1, 0, "minecraft:stone")
```

New sections start as air; modern sections get a singleton biome palette.
Existing biome, lighting-only, and unknown section tags are retained when adding
block storage. This does **not** create a new chunk, upgrade a protochunk, or
compute terrain/structures.

`region.locations` maps slot indices to immutable
`ChunkLocation(offset, sectors, timestamp)`; `region.timestamps` includes all
1,024 slots, even empty ones. Slot index is `chunk_x % 32 + 32 * (chunk_z % 32)`.
Offsets/timestamps report the loaded header; reload to inspect a saved file's
new allocation. `save()` leaves the in-memory editing session intact, including
dirty chunks; saving to a different path does not retarget `region.path`.

Block values and properties are immutable. NBT snapshots from `chunk.nbt`,
`section.nbt` and `get_block_data()` are detached: mutating them does not edit the
region. Public mutations go through chunk/region block methods.

### Dimensions and file names

Region files do **not** identify their dimension. The default is the vanilla
Overworld. Explicitly select other dimensions:

```python
nether = Region("DIM-1/region/r.-1.0.mca", dimension="minecraft:the_nether").load()
end = Region("DIM1/region/r.0.0.mca", dimension="minecraft:the_end").load()
```

Vanilla Overworld limits are Y=0..255 before 1.18, Y=-64..319 from 1.18.
Nether and End use Y=0..255. Nether **terrain generation** height is not the
same as its 256-block storage/build height. Custom dimension heights are not
supported. A modern `yPos` or block section inconsistent with the selected
dimension is rejected. An `.mca` alone cannot prove all dimension settings;
selecting the correct dimension remains the caller's responsibility.

Region coordinates come from `r.<rx>.<rz>.*`, or from chunk coordinates when
loading another filename such as `edited.mca`. All chunks must agree with the
region and their header slots. For an empty arbitrarily named file, supply
`Region("empty.mca", region_coords=(-1, 2))` to use coordinate-based methods.

## CLI

Use `python main.py`, `python -m mca_editor`, or installed `mca-editor`:

```sh
python main.py info r.0.0.mca
python main.py chunks r.0.0.mca
python main.py get-block r.0.0.mca --x 0 --y 64 --z 0

# Default destination: r.0.0.edited.mca (input is untouched).
python main.py set-block r.0.0.mca --x 0 --y 64 --z 0 \
    --block minecraft:diamond_block

python main.py set-block r.0.0.mca --x 1 --y 64 --z 0 \
    --block minecraft:oak_log --property axis=y --output logs.mca

python main.py fill r.0.0.mca --from 0,60,0 --to 10,64,10 \
    --block minecraft:stone --output edited.mca
python main.py validate edited.mca

# Negative cuboid arguments use '=' to avoid argparse option ambiguity.
python main.py fill r.-1.-1.mca --from=-17,-1,-1 --to=-14,0,-1 \
    --block minecraft:stone --output negative.mca

# Explicit overwrite: atomically replaces input and creates r.0.0.mca.bak.
python main.py set-block r.0.0.mca --x 0 --y 64 --z 0 \
    --block minecraft:stone --in-place
```

Use repeated `--property key=value` flags for multiple states. An existing
separate output requires `--force`. Existing `.bak` files are never overwritten.
All commands accept `--dimension` and optional `--region RX RZ`. Edits also accept
`--preserve-metadata` and `--remove-block-entities` (see below). No CLI command
silently creates sections or chunks. Exit codes: 0 success, 1 I/O/format/edit
error, 2 argument syntax error. `info`, `chunks`, and `get-block` emit JSON;
`chunks` emits one JSON object per line.

## Compatibility matrix

Support is deliberately **release-only and allowlisted**, rather than guessing
from a broad numerical range. The exact allowlist is in `mca_editor/versions.py`.
Every listed DataVersion has a synthetic save/reopen edit test.

| Java releases | Allowed DataVersions | Chunk/section storage | Packing | Verification |
|---|---|---|---|---|
| 1.13, 1.13.1, 1.13.2 | 1519, 1628, 1631 | `Level.Sections[].Palette` / `BlockStates` | Continuous, entries may span longs; min 4 bits | Synthetic each release; real 1.13.2 |
| 1.14–1.14.4 | 1952, 1957, 1963, 1968, 1976 | Same `Level` schema | Continuous | Synthetic each release; real 1.14.4 |
| 1.15–1.15.2 | 2225, 2227, 2230 | Same `Level` schema | Continuous | Synthetic each release |
| 1.16–1.16.5 | 2566, 2567, 2578, 2580, 2584, 2586 | Same `Level` schema | Padded, entries never span longs | Synthetic each release; real 1.16 |
| 1.17–1.17.1 | 2724, 2730 | Same `Level` schema | Padded | Synthetic each release; real 1.17.1 |
| 1.18–1.18.2 | 2860, 2865, 2975 | Root `sections[].block_states.{palette,data}` | Padded; singleton may omit `data`; negative Y | Synthetic each release; real 1.18.2 |
| 1.19–1.19.4 | 3105, 3117, 3120, 3218, 3337 | Same root-level schema | Padded; singleton omission | Synthetic each release; real 1.19.4 |
| 1.20–1.20.4 | 3463, 3465, 3578, 3698, 3700 | Same root-level schema | Padded; singleton omission | Synthetic each release |
| ≤1.12.2; 1.20.5+ (including 1.21+); all snapshots/pre-releases | Not allowlisted | **Rejected** | No guessed layout | Rejection tests |

This table concerns **on-disk block storage**, not Minecraft's network chunk
protocol. Disk palettes remain local for large palettes, including >256 states.
Bits per non-singleton block are `max(4, ceil(log2(palette_size)))`.
Continuous length is `ceil(4096 * bits / 64)`; padded length is
`ceil(4096 / floor(64 / bits))`. Old schemas require the long array even for
singletons. Signed NBT longs are interpreted as unsigned 64-bit bit containers.

| Region feature | Policy |
|---|---|
| 8 KiB header; 3-byte sector offsets; 1-byte counts; unsigned timestamps | Read/write, validate allocation bounds and overlaps |
| gzip (1), zlib (2), uncompressed (3) | Read/write; original codec retained |
| External flag (`compression & 0x80`), `.mcc` files | Detected and **rejected**, not followed or modified |
| LZ4 (4), unknown compression | **Rejected** |
| Chunks exceeding 255 sectors after editing | **Rejected before destination replacement** |
| Empty slots, absent sections, negative coordinates | Supported with explicit missing-chunk/section rules |
| NBT tags 1–12, named root, signed arrays, typed empty lists, Java modified UTF-8 | Read/write; unknown tags retained |
| Duplicate NBT keys, invalid lengths/types/indices, truncated payloads | **Rejected**, never silently repaired |

## Editing and metadata policy

An edit decodes the section's 4,096 indices, reuses or appends the exact block
state, and repacks using that chunk's version. Width increases do not disturb
other indices. Palette property order is canonicalized for matching. Unused
palette entries are normally retained; at the 4,096-entry limit a slot unused
after the edit is reclaimed. `air`, `cave_air`, and `void_air` remain distinct.

Unknown NBT tags, root names, numeric widths, biome containers, entities,
structures, status, ticks, and unrelated sections are preserved. **Unmodified
chunks keep their exact compressed record bytes and timestamp.** Modified
chunks preserve NBT values/types, not necessarily the original byte ordering of
new properties or floating-point NaN payload bits.

Block changes can make derived metadata stale:

* Default `metadata_policy="invalidate"`: remove **recognized heightmap entries**
  from changed chunks and set `isLightOn=0` (also `LightPopulated=0` if present).
  Minecraft can prime absent heightmaps and regenerate invalid lighting. Unknown
  heightmap keys and existing light arrays are retained. This is not an offline
  lighting solver; behavior across old releases and neighboring chunk borders
  is not verified in a running game.
* `metadata_policy="preserve"` / `--preserve-metadata`: leave all these tags
  intact, even if stale. Use this only when arranging metadata repair yourself.
* Default `block_entity_policy="error"`: refuse a **state-changing** edit at a
  position with a block entity. `"remove"` / `--remove-block-entities` explicitly
  removes only entities at changed positions; all unrelated block entities and
  their inventories are retained. No block entities are automatically created.

### Important limits

* No runtime block registry: namespaced-ID syntax and string properties are
  checked, but block existence, property legality, and version availability are
  the caller's responsibility. Use valid complete states for your target
  Minecraft version/modpack. Unknown/modded NBT data is preserved, but modded
  gameplay compatibility is not claimed.
* Editing a chest/sign/etc. does not construct its required block entity.
  Multiblock structures (beds, doors, etc.), neighbor updates, water flow,
  scheduled ticks, POI databases, and gameplay side effects are not simulated.
  External entity/POI region files are not touched.
* Protochunk status and generation metadata are retained, not promoted to full
  chunks; later terrain generation can overwrite edits in unfinished chunks.
  Prefer fully generated chunks for durable edits.
* No custom-height dimensions, chunk generation/deletion, DataVersion upgrades,
  external chunks, LZ4, or legacy numerical block decoder.
* Strict eager validation means **any unsupported or corrupt allocated chunk
  rejects the file**, even if the desired edit is in another chunk.
* Safety limits: 64 MiB decompressed NBT per chunk, 128 nesting depth, two million
  NBT tags. Region parsing retains chunks in memory; this is not a streaming
  world editor. Pathological/modded files exceeding these limits are rejected.
* The binary tests include real-world files and an independent NBT codec, but
  **no Minecraft client/server was launched for an in-game loading test**.
  “Supported” means the documented binary schema is implemented and tested,
  not a guarantee about every game's metadata or simulation behavior.

## Safe saving

`region.save()` is an explicit library in-place save; use
`region.save(backup=True)` for a non-overwriting `.bak` backup. The CLI defaults
to a separate file instead. In both cases:

1. Serialize changed chunks and reuse other compressed records.
2. Allocate new, nonoverlapping contiguous sectors in a same-directory temp
   file; rebuild both header tables and update changed-chunk timestamps.
3. Check the 255-sector count and 24-bit offset limits; write full sectors.
4. Flush and `fsync` the file; reopen it through the full parser and compare
   every written chunk record.
5. Check for detected destination changes; optionally copy/fsync an exclusive
   backup; commit with `os.replace`.

A serialization, compression, write, verification, backup or replacement failure
before the commit leaves the original/destination bytes untouched; the temporary
file is removed. No backup is overwritten. In-place saves reject source changes
since load, and symlink destinations are refused. Allocation padding, unused
sectors, and allocation order need not match the source.

This is atomic replacement on filesystems supporting `os.replace`, **not** a
multi-file transaction, file lock, or guaranteed power-loss recovery. There is
still a race with another writer after the last check; do not edit a live world.
The parent directory is not fsynced, ownership/extended attributes are not
preserved, and hard links are not updated as a group.

## Tests and reproducible demonstration

```sh
python -m pytest -q

# Optional integration corpus: ~10 MiB, checksum-pinned upstream fixtures.
python scripts/fetch_real_fixtures.py
python -m pytest -q                        # includes six real-region tests

# Must be a NEW directory; never points at or overwrites an existing world.
python scripts/demo.py .cache/my-demo
python main.py validate .cache/my-demo/r.0.0.modified.mca
```

The demo constructs real synthetic Anvil records, prints the original air block,
changes it to diamond, writes an oak-log state, saves to a separate file, reopens
it, and asserts both changes and untouched neighbors. `mca_editor.fixtures`
contains reusable `make_chunk()` and exclusive-create `write_region()` helpers.
These are parser test fixtures, **not complete playable worlds**.

Tests cover both packing layouts against an independent bit-by-bit oracle,
4–12-bit arrays, cross-long boundaries, 16→17 and other width transitions,
4,096-entry palette recycling, all claimed DataVersions, compression codecs,
NBT round trips against `nbtlib`, negative/chunk/section/region boundaries,
exact cuboid fills, metadata preservation, allocation growth, malformed input,
external/oversized rejection, backups, and injected save failures. Block edit
tests verify the result after saving and reopening, not only in memory.

Real fixtures cover 1,694 chunks across six releases. They are **not committed**;
`tests/real_fixtures.json` pins Git blob IDs and SHA-256 checksums. Integration
tests skip when absent; set `MCA_REAL_FIXTURES` to use an alternate cache root.
No test edits the input real-world fixtures. The library's synthetic tests work
offline once test dependencies are installed.

## Architecture and format references

* `region.py`: header/location validation, compression records, world-coordinate
  API, cuboid preflight, atomic save and validation.
* `chunk.py`, `versions.py`: release allowlist, old/new schema dispatch, dimension
  bounds, section lifecycle, metadata policies.
* `section.py`, `packed_array.py`, `block.py`: palettes, immutable block states,
  signed-long bit packing and resizing.
* `nbt_utils.py`: bounded full NBT codec including Java modified UTF-8.
* `compression.py`, `coordinates.py`, `exceptions.py`: focused primitives.
* `cli.py`, `fixtures.py`, `scripts/`, `tests/`: CLI, reproducible fixtures and tests.

References consulted for layout decisions (no source code copied):

* [Querz/NBT Section.java](https://github.com/Querz/NBT/blob/master/src/main/java/net/querz/mca/Section.java):
  continuous/padded transition (snapshot DataVersion 2527; snapshots themselves
  are outside this library's allowlist).
* [Minecraft ChunkSerializer](https://github.com/mahtomedi/minecraft/blob/main/src/main/java/net/minecraft/world/level/chunk/storage/ChunkSerializer.java),
  [PalettedContainer](https://github.com/mahtomedi/minecraft/blob/main/src/main/java/net/minecraft/world/level/chunk/PalettedContainer.java),
  [RegionFile](https://github.com/mahtomedi/minecraft/blob/main/src/main/java/net/minecraft/world/level/chunk/storage/RegionFile.java):
  root sections, singleton palettes, biomes, heightmap priming, external flags.
* [misode/mcmeta version data](https://github.com/misode/mcmeta/blob/summary/versions/data.json):
  release/DataVersion mappings.
* [PrismarineJS/prismarine-provider-anvil fixtures](https://github.com/PrismarineJS/prismarine-provider-anvil/tree/master/test/fixtures):
  real-world integration corpus, MIT-licensed upstream; pinned by our manifest.

Project license: [MIT](LICENSE).
