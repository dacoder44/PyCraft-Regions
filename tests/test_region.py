import gzip
import hashlib
import struct
import zlib
from pathlib import Path

import pytest

from mca_editor import Block, FormatError, Region, SaveError, UnsupportedFormatError
from mca_editor.compression import compress, decompress
from mca_editor.fixtures import make_chunk, write_region
from mca_editor.nbt_utils import Compound, Int, ListTag, LongArray, Tag


@pytest.mark.parametrize("codec", [1, 2, 3])
def test_headers_timestamps_codecs_and_roundtrip(fixture_region, reopen, codec):
    r = fixture_region(documents=[make_chunk(), make_chunk(31, 31)], codec=codec)
    raw = r.path.read_bytes()
    assert len(raw) % 4096 == 0
    assert int.from_bytes(raw[:3], "big") == 2
    assert raw[3] == r.locations[0].sectors
    assert struct.unpack_from(">I", raw, 4096)[0] == 1_600_000_000
    assert r.timestamps[0] == r.timestamps[1023] == 1_600_000_000
    assert r.timestamps[1] == 0
    assert len(r.locations) == 2
    for record in r._records.values():
        payload = record.record[5:]
        plain = (
            gzip.decompress(payload)
            if codec == 1
            else zlib.decompress(payload)
            if codec == 2
            else payload
        )
        assert plain == record.chunk.serialize()
    untouched = r._records[1023].record
    r.set_block(0, 64, 0, "minecraft:diamond_block")
    r = reopen(r)
    assert r.get_block(0, 64, 0).name == "minecraft:diamond_block"
    assert r.get_block(511, 64, 511).name == "minecraft:air"
    assert r._records[1023].record == untouched
    assert r.timestamps[1023] == 1_600_000_000
    assert r.timestamps[0] >= 1_600_000_000


def test_empty_region_and_nonzero_empty_timestamp(tmp_path):
    data = bytearray(8192)
    struct.pack_into(">I", data, 4096, 42)
    p = tmp_path / "r.-1.2.mca"
    p.write_bytes(data)
    r = Region(p).load()
    assert r.chunk_count == 0 and r.timestamps[0] == 42
    output = r.save(tmp_path / "empty.mca")
    assert output.read_bytes() == data
    assert Region(output).load().chunk_count == 0


def test_growth_relocates_chunks_without_overlap(fixture_region, reopen):
    r = fixture_region(documents=[make_chunk(), make_chunk(1, 0)])
    original_sectors = r.locations[0].sectors
    untouched_record = r._records[1].record
    expected = []
    for index in range(400):
        block = "fixture:" + hashlib.sha256(str(index).encode()).hexdigest()
        r.set_block(index % 16, 64 + index // 256, index // 16 % 16, block)
        expected.append(block)
    r = reopen(r)
    assert r.locations[0].sectors > original_sectors
    assert r.locations[1].offset >= r.locations[0].offset + r.locations[0].sectors
    assert r._records[1].record == untouched_record
    for index, block in enumerate(expected):
        assert r.get_block(index % 16, 64 + index // 256, index // 16 % 16).name == block
    assert r.get_block(0, 79, 0).name == "minecraft:air"


def test_oversized_edit_refused_without_touching_original(tmp_path):
    doc = make_chunk()
    doc.root.value["payload"] = Tag(7, b"")
    size = 255 * 4096 - 1000
    doc.root.value["payload"].value = bytes(size - len(doc.serialize()) - 5)
    p = write_region(tmp_path / "r.0.0.mca", [doc], codec=3)
    original = p.read_bytes()
    r = Region(p).load()
    r.set_block(0, 64, 0, "minecraft:stone")
    with pytest.raises(UnsupportedFormatError, match="oversized"):
        r.save()
    assert p.read_bytes() == original
    assert Region(p).load().get_block(0, 64, 0).name == "minecraft:air"
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize("stage", ["fsync", "replace", "verify", "compress"])
def test_failed_save_leaves_original_and_output_untouched(
    fixture_region, monkeypatch, tmp_path, stage
):
    r = fixture_region()
    original = r.path.read_bytes()
    target = tmp_path / "existing.mca"
    target.write_bytes(original)
    r.set_block(0, 64, 0, "minecraft:stone")

    def fail(*args, **kwargs):
        raise OSError("Injected disk failure")

    with monkeypatch.context() as context:
        if stage in ("fsync", "replace"):
            context.setattr("mca_editor.region.os." + stage, fail)
        elif stage == "verify":
            context.setattr(Region, "load", fail)
        else:
            context.setattr("mca_editor.region.compress", fail)
        with pytest.raises(SaveError, match="Injected"):
            r.save(backup=True)
        with pytest.raises(SaveError, match="Injected"):
            r.save(target, backup=True)
    assert r.path.read_bytes() == target.read_bytes() == original
    assert not list(tmp_path.glob(".*.tmp"))
    assert not list(tmp_path.glob("*.bak"))
    assert Region(r.path).load().get_block(0, 64, 0).name == "minecraft:air"
    # Dirty data remains available for a later successful retry.
    r.save(target)
    assert Region(target).load().get_block(0, 64, 0).name == "minecraft:stone"


def test_backup_and_consecutive_in_place_saves(fixture_region):
    r = fixture_region()
    original = r.path.read_bytes()
    r.set_block(0, 64, 0, "minecraft:diamond_block")
    r.save(backup=True)
    backup = Path(str(r.path) + ".bak")
    assert backup.read_bytes() == original
    assert Region(r.path).load().get_block(0, 64, 0).name == "minecraft:diamond_block"
    r.set_block(1, 64, 0, "minecraft:stone")
    before = r.path.read_bytes()
    with pytest.raises(SaveError):
        r.save(backup=True)
    assert r.path.read_bytes() == before and backup.read_bytes() == original
    r.save()
    loaded = Region(r.path).load()
    assert loaded.get_block(0, 64, 0).name == "minecraft:diamond_block"
    assert loaded.get_block(1, 64, 0).name == "minecraft:stone"


def test_external_change_and_symlinks_refused(fixture_region, tmp_path):
    r = fixture_region()
    r.set_block(0, 64, 0, "minecraft:stone")
    link = tmp_path / "link.mca"
    link.symlink_to(r.path)
    with pytest.raises(SaveError, match="symlink"):
        r.save(link)
    data = bytearray(r.path.read_bytes())
    struct.pack_into(">I", data, 4096, 1)
    r.path.write_bytes(data)
    with pytest.raises(SaveError, match="changed on disk"):
        r.save()
    assert r.path.read_bytes() == data
    assert Region(r.path).load().get_block(0, 64, 0).name == "minecraft:air"


@pytest.mark.parametrize("length", [0, 1, 4096, 8191, 8193])
def test_bad_file_sizes(tmp_path, length):
    p = tmp_path / "bad.mca"
    p.write_bytes(bytes(length))
    with pytest.raises(FormatError):
        Region(p).load()


@pytest.mark.parametrize("offset,count", [(0, 1), (1, 1), (2, 0), (100, 1), (2, 255)])
def test_bad_locations(fixture_region, offset, count):
    r = fixture_region()
    raw = bytearray(r.path.read_bytes())
    struct.pack_into(">I", raw, 0, offset << 8 | count)
    r.path.write_bytes(raw)
    with pytest.raises(FormatError, match="allocation"):
        Region(r.path).load()


def test_overlap(fixture_region):
    r = fixture_region()
    raw = bytearray(r.path.read_bytes())
    raw[4:8] = raw[:4]
    r.path.write_bytes(raw)
    with pytest.raises(FormatError, match="Overlapping"):
        Region(r.path).load()


@pytest.mark.parametrize("length", [0, 4093, 0xFFFFFFFF])
def test_bad_chunk_length(fixture_region, length):
    r = fixture_region()
    raw = bytearray(r.path.read_bytes())
    struct.pack_into(">I", raw, 8192, length)
    r.path.write_bytes(raw)
    with pytest.raises(FormatError, match="record length"):
        Region(r.path).load()


@pytest.mark.parametrize("codec", [0, 4, 127, 128, 129, 130, 131, 255])
def test_unsupported_compression_and_external(fixture_region, codec):
    r = fixture_region()
    raw = bytearray(r.path.read_bytes())
    raw[8196] = codec
    if codec & 128:
        struct.pack_into(">I", raw, 8192, 1)  # actual external stub
    r.path.write_bytes(raw)
    with pytest.raises(UnsupportedFormatError):
        Region(r.path).load()


@pytest.mark.parametrize("dv", [1343, 2527, 2825, 3837, 3955, 99999])
def test_versions_rejected(tmp_path, dv):
    doc = make_chunk()
    doc.root.value["DataVersion"] = Int(dv)
    p = write_region(tmp_path / "r.0.0.mca", [doc])
    with pytest.raises(UnsupportedFormatError, match="DataVersion"):
        Region(p).load()


@pytest.mark.parametrize(
    "kind",
    [
        "missing_version",
        "bad_position",
        "wrong_region",
        "duplicate_section",
        "bad_palette",
        "missing_palette",
        "missing_data",
        "bad_length",
        "bad_index",
        "bad_property",
        "bad_heightmaps",
    ],
)
def test_malformed_chunk_nbt(tmp_path, kind):
    doc = make_chunk(blocks=[Block("minecraft:air"), Block("minecraft:stone")])
    section = doc.root.value["sections"].value[0]
    storage = section.value["block_states"]
    if kind == "missing_version":
        del doc.root.value["DataVersion"]
    elif kind == "bad_position":
        doc.root.value["xPos"] = Tag(1, 0)
    elif kind == "wrong_region":
        doc.root.value["xPos"] = Int(32)
    elif kind == "duplicate_section":
        doc.root.value["sections"].value.append(section)
    elif kind == "bad_palette":
        storage.value["palette"] = ListTag([])
    elif kind == "missing_palette":
        del storage.value["palette"]
    elif kind == "missing_data":
        del storage.value["data"]
    elif kind == "bad_length":
        storage.value["data"] = LongArray([0])
    elif kind == "bad_index":
        storage.value["data"].value[0] = 15
    elif kind == "bad_property":
        storage.value["palette"].value[0].value["Properties"] = Compound({"axis": Int(1)})
    elif kind == "bad_heightmaps":
        doc.root.value["Heightmaps"] = Int(0)
    p = write_region(tmp_path / "r.0.0.mca", [doc])
    with pytest.raises(FormatError):
        Region(p).load()


def test_corrupt_streams_and_limits(monkeypatch):
    for codec in (1, 2):
        data = compress(b"payload", codec)
        for broken in (data[:-1], b"bad data", data + b"trailing"):
            with pytest.raises(FormatError):
                decompress(broken, codec)
    monkeypatch.setattr("mca_editor.compression.MAX_NBT_BYTES", 32)
    for codec in (1, 2, 3):
        with pytest.raises(FormatError, match="limit"):
            decompress(compress(bytes(33), codec), codec)


def test_slot_mismatch(tmp_path):
    p = write_region(tmp_path / "r.0.0.mca", [make_chunk()])
    raw = bytearray(p.read_bytes())
    raw[4:8], raw[:4] = raw[:4], bytes(4)
    p.write_bytes(raw)
    with pytest.raises(FormatError, match="coordinates"):
        Region(p).load()


def test_save_rejects_misleading_region_filename(fixture_region, tmp_path):
    r = fixture_region()
    r.set_block(0, 64, 0, "minecraft:stone")
    target = tmp_path / "r.1.0.mca"
    with pytest.raises(SaveError, match="filename disagrees"):
        r.save(target)
    assert not target.exists()
    assert Region(r.path).load().get_block(0, 64, 0).name == "minecraft:air"


def test_save_to_hardlink_does_not_retarget_source(fixture_region, tmp_path):
    r = fixture_region()
    original = r.path.read_bytes()
    alias = tmp_path / "alias.mca"
    alias.hardlink_to(r.path)
    r.set_block(0, 64, 0, "minecraft:stone")
    r.save(alias)
    assert r.path.read_bytes() == original
    assert Region(alias).load().get_block(0, 64, 0).name == "minecraft:stone"
    r.save()
    assert Region(r.path).load().get_block(0, 64, 0).name == "minecraft:stone"
