"""Optional real-world fixture integration tests; never edit cached inputs."""

import hashlib
import json
import os
from io import BytesIO
from pathlib import Path

import nbtlib
import pytest

from mca_editor import Block, Region
from mca_editor.compression import decompress

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "tests/real_fixtures.json").read_text())
CACHE = Path(os.environ.get("MCA_REAL_FIXTURES", ROOT / ".cache/real"))


@pytest.mark.parametrize("fixture", MANIFEST["files"], ids=lambda f: f["path"].split("/")[2])
def test_real_region_edit_save_reopen(fixture, tmp_path):
    path = CACHE / Path(fixture["path"]).relative_to("test/fixtures")
    if not path.exists():
        pytest.skip(
            "Run python scripts/fetch_real_fixtures.py to enable real-world integration tests"
        )
    original_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    assert original_hash == fixture["sha256"]
    r = Region(path).load()
    # Prefer a real multi-palette, terrain-containing section to exercise packing.
    chunk, section = next(
        (c, s)
        for c in r.iter_chunks()
        for s in c.sections
        if len(s.palette) > 16 and c.min_y <= s.y * 16 < c.max_y
    )
    slot = chunk.x % 32 + 32 * (chunk.z % 32)
    before = [section.get_block(x, y, z) for y in range(16) for z in range(16) for x in range(16)]
    original_other_sections = {s.y: s.nbt for s in chunk.sections if s.y != section.y}
    original_records = {i: rec.record for i, rec in r._records.items() if i != slot}
    x, y, z = chunk.x * 16 + 12, section.y * 16, chunk.z * 16
    r.set_block(x, y, z, "minecraft:diamond_block")
    output = tmp_path / path.name
    r.save(output)
    loaded = Region(output).load()
    assert loaded.get_block(x, y, z) == Block("minecraft:diamond_block")
    edited = loaded.get_section(chunk.x, chunk.z, section.y)
    after = [edited.get_block(x, y, z) for y in range(16) for z in range(16) for x in range(16)]
    before[12] = Block("minecraft:diamond_block")
    assert after == before
    for s in loaded.get_chunk(chunk.x, chunk.z).sections:
        if s.y != section.y:
            assert s.nbt == original_other_sections[s.y]
    for i, record in original_records.items():
        assert loaded._records[i].record == record
    # Independent NBT implementation can parse and reserialize the written chunk.
    record = loaded._records[slot]
    raw = decompress(record.record[5:], record.codec)
    external = nbtlib.File.parse(BytesIO(raw))
    assert int(external["DataVersion"]) == chunk.data_version
    serialized = BytesIO()
    external.write(serialized)
    assert serialized.getvalue() == raw
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original_hash
