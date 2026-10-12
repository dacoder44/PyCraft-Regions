from copy import deepcopy

import pytest

from mca_editor import (
    Block,
    CoordinateError,
    MissingChunkError,
    MissingSectionError,
    Region,
    UnsafeEditError,
    UnsupportedFormatError,
)
from mca_editor.fixtures import make_chunk, write_region
from mca_editor.nbt_utils import Byte, Compound, Int, ListTag, LongArray, String, Tag
from mca_editor.versions import RELEASES


def section_blocks(section):
    return [section.get_block(x, y, z) for y in range(16) for z in range(16) for x in range(16)]


@pytest.mark.parametrize("data_version", RELEASES)
def test_every_claimed_release_singleton_edit_save_reopen(fixture_region, reopen, data_version):
    r = fixture_region(data_version=data_version)
    before = section_blocks(r.get_section(0, 0, 4))
    assert r.get_block(0, 64, 0) == Block("minecraft:air")
    r.set_block(12, 64, 0, "minecraft:oak_log", {"axis": "y"})
    r = reopen(r)
    after = section_blocks(r.get_section(0, 0, 4))
    assert r.get_block(12, 64, 0) == Block("minecraft:oak_log", {"axis": "y"})
    before[12] = after[12]
    assert before == after


@pytest.mark.parametrize("data_version", [1631, 2230, 2566, 2730, 2860, 3700])
@pytest.mark.parametrize("palette_size", [2, 16, 32, 64, 128, 256])
def test_palette_reuse_and_width_growth_preserves_every_cell(
    fixture_region, reopen, data_version, palette_size
):
    blocks = [Block("minecraft:stone", {"fixture_variant": str(i)}) for i in range(palette_size)]
    indices = [i % palette_size for i in range(4096)]
    r = fixture_region(data_version=data_version, blocks=blocks, indices=indices)
    before = section_blocks(r.get_section(0, 0, 4))
    # Entry 12 straddles the 64-bit boundary for continuous five-bit storage.
    r.set_block(12, 64, 0, "minecraft:oak_log", {"axis": "x"})
    r.set_block(15, 79, 15, blocks[0].name, blocks[0].properties)
    r.set_block(13, 64, 0, "minecraft:oak_log", {"axis": "x"})
    r = reopen(r)
    after = section_blocks(r.get_section(0, 0, 4))
    assert len(r.get_section(0, 0, 4).palette) == palette_size + 1
    for index, block in [
        (12, Block("minecraft:oak_log", {"axis": "x"})),
        (13, Block("minecraft:oak_log", {"axis": "x"})),
        (4095, blocks[0]),
    ]:
        assert after[index] == block
        before[index] = block
    assert after == before


def test_full_palette_recycles_only_unused_entry(fixture_region, reopen):
    blocks = [Block(f"fixture:block_{i}") for i in range(4096)]
    r = fixture_region(blocks=blocks, indices=list(range(4096)))
    r.set_block(0, 64, 0, "minecraft:diamond_block")
    r = reopen(r)
    expected = [Block("minecraft:diamond_block")] + blocks[1:]
    assert section_blocks(r.get_section(0, 0, 4)) == expected
    assert len(r.get_section(0, 0, 4).palette) == 4096


def test_air_variants_and_properties_canonical_reuse(fixture_region, reopen):
    r = fixture_region()
    for x, name in enumerate(("air", "cave_air", "void_air")):
        r.set_block(x, 64, 0, "minecraft:" + name)
    props = {"facing": "east", "waterlogged": "true"}
    r.set_block(3, 64, 0, "minecraft:oak_stairs", props)
    r.set_block(4, 64, 0, "minecraft:oak_stairs", dict(reversed(list(props.items()))))
    r = reopen(r)
    assert [r.get_block(i, 64, 0).name for i in range(3)] == [
        "minecraft:air",
        "minecraft:cave_air",
        "minecraft:void_air",
    ]
    assert r.get_block(3, 64, 0) == r.get_block(4, 64, 0) == Block("minecraft:oak_stairs", props)
    assert len(r.get_section(0, 0, 4).palette) == 4
    with pytest.raises(TypeError):
        r.get_block(3, 64, 0).properties["facing"] = "west"


@pytest.mark.parametrize(
    "cx,cz,x,z",
    [
        (-1, -1, -1, -1),
        (-32, -32, -512, -512),
        (-33, 0, -513, 0),
        (31, 31, 511, 511),
        (32, 0, 512, 0),
    ],
)
def test_negative_and_region_edges(fixture_region, reopen, cx, cz, x, z):
    r = fixture_region(x=cx, z=cz, filename=f"r.{cx // 32}.{cz // 32}.mca", section_y=-4)
    r.set_block(x, -64, z, "minecraft:diamond_block")
    r = reopen(r)
    assert r.get_block(x, -64, z).name == "minecraft:diamond_block"
    assert r.get_block_data(x, -64, z).value["Name"].value == "minecraft:diamond_block"
    assert r.get_chunk(cx, cz).get_block(x % 16, -64, z % 16).name == "minecraft:diamond_block"
    with pytest.raises(CoordinateError):
        r.get_block((cx // 32 + 1) * 512, -64, z)


def test_fill_across_four_chunks_exact_cuboid(fixture_region, reopen):
    docs = [make_chunk(x, z) for x in range(2) for z in range(2)]
    r = fixture_region(documents=docs)
    assert r.fill((14, 65, 14), (17, 67, 17), "minecraft:stone") == 48
    r = reopen(r)
    for chunk in r.iter_chunks():
        for y in range(64, 80):
            for z in range(chunk.z * 16, chunk.z * 16 + 16):
                for x in range(chunk.x * 16, chunk.x * 16 + 16):
                    expected = (
                        "minecraft:stone"
                        if 14 <= x <= 17 and 65 <= y <= 67 and 14 <= z <= 17
                        else "minecraft:air"
                    )
                    assert r.get_block(x, y, z).name == expected
    assert len(list(r.iter_blocks((14, 65, 14), (17, 67, 17)))) == 48


def test_fill_preflight_does_not_partially_edit(fixture_region, reopen):
    r = fixture_region()
    for end, error in [
        ((16, 64, 0), MissingChunkError),
        ((512, 64, 0), MissingChunkError),
        ((0, 80, 0), MissingSectionError),
        ((0, 320, 0), CoordinateError),
    ]:
        with pytest.raises(error):
            r.fill((0, 64, 0), end, "minecraft:stone")
        assert not r.get_chunk(0, 0).modified
    r = reopen(r)
    assert all(b.name == "minecraft:air" for b in section_blocks(r.get_section(0, 0, 4)))


@pytest.mark.parametrize("dv", [1631, 2586, 3465])
def test_create_sections_explicitly(fixture_region, reopen, dv):
    r = fixture_region(data_version=dv)
    y = -1 if dv >= 2860 else 5
    assert r.get_block(0, y * 16, 0).name == "minecraft:air"
    with pytest.raises(MissingSectionError):
        r.set_block(0, y * 16, 0, "minecraft:stone")
    if dv >= 2860:
        with pytest.raises(UnsafeEditError):
            r.create_section(0, 0, y)
    r.create_section(0, 0, y, biome="minecraft:plains")
    r.set_block(0, y * 16, 0, "minecraft:stone")
    r.create_section(0, 0, y + 2, biome="minecraft:plains")
    r = reopen(r)
    assert r.get_block(0, y * 16, 0).name == "minecraft:stone"
    assert r.get_block(1, y * 16, 0).name == "minecraft:air"
    assert r.get_block(0, (y + 2) * 16, 0).name == "minecraft:air"
    assert r.get_section(0, 0, y + 2).has_block_states


@pytest.mark.parametrize("dv", [1631, 2586, 3465])
def test_light_only_section(fixture_region, reopen, dv):
    doc = make_chunk(data_version=dv)
    level = doc.root.value.get("Level", doc.root)
    tag = level.value["sections" if dv >= 2860 else "Sections"].value[0]
    for key in ("block_states", "Palette", "BlockStates"):
        tag.value.pop(key, None)
    tag.value["SkyLight"] = Tag(7, bytes([255]) * 2048)
    r = fixture_region(documents=[doc])
    assert r.get_block(0, 64, 0).name == "minecraft:air"
    r.set_block(0, 64, 0, "minecraft:stone")
    r = reopen(r)
    assert r.get_block(0, 64, 0).name == "minecraft:stone"
    assert r.get_section(0, 0, 4).nbt.value["SkyLight"].value == bytes([255]) * 2048


@pytest.mark.parametrize("dv", [1631, 3465])
@pytest.mark.parametrize("policy", ["invalidate", "preserve"])
def test_unknown_nbt_and_metadata_policies(tmp_path, reopen, dv, policy):
    doc = make_chunk(data_version=dv)
    level = doc.root.value.get("Level", doc.root)
    level.value.update(
        {
            "Heightmaps": Compound(
                {"WORLD_SURFACE": LongArray([123]), "mod:unknown": Tag(11, [7, 8])}
            ),
            "isLightOn": Byte(1),
            "LightPopulated": Byte(1),
            "Entities": ListTag([Compound({"id": String("minecraft:pig")})]),
            "Biomes": Tag(11, [1] * 256),
            "mod:opaque": Compound({"a": Tag(5, 1.25)}),
        }
    )
    before = deepcopy(level)
    r = Region(write_region(tmp_path / "r.0.0.mca", [doc]), metadata_policy=policy).load()
    r.set_block(0, 64, 0, "minecraft:stone")
    r = reopen(r)
    assert r.get_block(0, 64, 0).name == "minecraft:stone"
    root = r.get_chunk(0, 0).nbt
    after = root.value.get("Level", root)
    for name in (
        "Entities",
        "Biomes",
        "mod:opaque",
        "fixture_unknown",
        "LastUpdate",
        "InhabitedTime",
        "Status",
    ):
        assert before.value[name] == after.value[name]
    if policy == "preserve":
        for name in ("Heightmaps", "isLightOn", "LightPopulated"):
            assert after.value[name] == before.value[name]
    else:
        assert "WORLD_SURFACE" not in after.value["Heightmaps"].value
        assert after.value["Heightmaps"].value["mod:unknown"] == Tag(11, [7, 8])
        assert after.value["isLightOn"].value == after.value["LightPopulated"].value == 0


@pytest.mark.parametrize("dv", [1631, 3465])
def test_block_entity_conflict_is_explicit(tmp_path, reopen, dv):
    doc = make_chunk(data_version=dv, blocks=[Block("minecraft:chest", {"facing": "north"})])
    level = doc.root.value.get("Level", doc.root)
    key = "block_entities" if dv >= 2860 else "TileEntities"
    entities = [
        Compound(
            {
                "id": String("minecraft:chest"),
                "x": Int(x),
                "y": Int(64),
                "z": Int(0),
                "Items": ListTag([]),
            }
        )
        for x in (0, 2)
    ]
    level.value[key] = ListTag(entities)
    path = write_region(tmp_path / "r.0.0.mca", [doc])
    r = Region(path).load()
    with pytest.raises(UnsafeEditError):
        r.fill((0, 64, 0), (1, 64, 0), "minecraft:stone")
    r = reopen(r)
    assert r.get_block(0, 64, 0).name == "minecraft:chest"
    r = Region(path, block_entity_policy="remove").load()
    r.set_block(0, 64, 0, "minecraft:stone")
    r = reopen(r)
    assert r.get_block(0, 64, 0).name == "minecraft:stone"
    root = r.get_chunk(0, 0).nbt
    assert root.value.get("Level", root).value[key].value == entities[1:]


def test_invalid_coordinate_types_and_dimensions(fixture_region):
    r = fixture_region()
    for xyz in [(True, 64, 0), (0.0, 64, 0), (0, -65, 0), (0, 320, 0)]:
        with pytest.raises(CoordinateError):
            r.get_block(*xyz)
    with pytest.raises(CoordinateError):
        Region(r.path, dimension="mod:moon")
    with pytest.raises(UnsupportedFormatError, match="dimension"):
        Region(r.path, dimension="minecraft:the_nether").load()
    with pytest.raises(ValueError):
        r.set_block(0, 64, 0, "stone")
    with pytest.raises(ValueError):
        r.set_block(0, 64, 0, "minecraft:stone", {"axis": True})


def test_fill_across_negative_chunk_and_section_boundaries(fixture_region, reopen):
    documents = []
    for cx in (-2, -1):
        doc = make_chunk(cx, -1, section_y=-1)
        doc.root.value["sections"].value.extend(
            make_chunk(cx, -1, section_y=0).root.value["sections"].value
        )
        documents.append(doc)
    r = fixture_region(documents=documents, filename="r.-1.-1.mca")
    assert r.fill((-17, -1, -1), (-14, 0, -1), "minecraft:stone") == 8
    r = reopen(r)
    for xyz, block in r.iter_blocks((-18, -2, -2), (-13, 1, -1)):
        x, y, z = xyz
        expected = (
            "minecraft:stone" if -17 <= x <= -14 and -1 <= y <= 0 and z == -1 else "minecraft:air"
        )
        assert block.name == expected


def test_fill_rejects_region_boundary_before_any_edit(fixture_region, reopen):
    r = fixture_region(x=31)
    with pytest.raises(CoordinateError, match="outside region"):
        r.fill((511, 64, 0), (512, 64, 0), "minecraft:stone")
    r = reopen(r)
    assert r.get_block(511, 64, 0).name == "minecraft:air"


def test_unknown_palette_tags_and_root_name_preserved(fixture_region, reopen):
    doc = make_chunk()
    doc.name = "named\0😀root"
    storage = doc.root.value["sections"].value[0].value["block_states"]
    storage.value["mod:storage"] = Tag(2, 3)
    storage.value["palette"].value[0].value["mod:palette"] = Tag(4, 999)
    r = fixture_region(documents=[doc])
    r.set_block(1, 64, 0, "minecraft:stone")
    r = reopen(r)
    assert r.get_block(1, 64, 0).name == "minecraft:stone"
    assert r.get_block_data(0, 64, 0).value["mod:palette"] == Tag(4, 999)
    assert r.get_section(0, 0, 4).nbt.value["block_states"].value["mod:storage"] == Tag(2, 3)
    assert r.get_chunk(0, 0)._document.name == doc.name
    detached = r.get_block_data(0, 64, 0)
    detached.value["Name"] = String("minecraft:stone")
    assert r.get_block(0, 64, 0).name == "minecraft:air"


def test_noop_preserves_original_record(fixture_region, reopen):
    r = fixture_region()
    original = r._records[0].record
    r.set_block(0, 64, 0, "minecraft:air")
    r = reopen(r)
    assert r._records[0].record == original
    assert r.get_block(0, 64, 0).name == "minecraft:air"


def test_custom_height_rejected(fixture_region):
    with pytest.raises(UnsupportedFormatError, match="custom dimension"):
        fixture_region(section_y=20)


@pytest.mark.parametrize("dimension", ["minecraft:the_nether", "minecraft:the_end"])
def test_vanilla_nonoverworld_dimensions(tmp_path, reopen, dimension):
    doc = make_chunk(section_y=15)
    doc.root.value["yPos"] = Int(0)
    path = write_region(tmp_path / "r.0.0.mca", [doc])
    r = Region(path, dimension=dimension).load()
    r.set_block(15, 255, 15, "minecraft:stone")
    r = reopen(r)
    assert r.get_block(15, 255, 15).name == "minecraft:stone"
    assert r.get_block(14, 255, 15).name == "minecraft:air"
    for y in (-1, 256):
        with pytest.raises(CoordinateError):
            r.get_block(0, y, 0)
