"""Create a new synthetic region, edit, save, reopen and assert the result."""

import argparse
from pathlib import Path

from mca_editor import Region
from mca_editor.fixtures import make_chunk, write_region


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="A NEW directory, never an existing world")
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=False)
    original = write_region(args.directory / "r.0.0.mca", [make_chunk(), make_chunk(1, 0)])
    region = Region(original).load()
    print("Before:", region.get_block(0, 64, 0))
    region.set_block(0, 64, 0, "minecraft:diamond_block")
    region.set_block(1, 64, 0, "minecraft:oak_log", {"axis": "y"})
    output = args.directory / "r.0.0.modified.mca"
    region.save(output)
    reopened = Region(output).load()
    assert reopened.get_block(0, 64, 0).name == "minecraft:diamond_block"
    assert reopened.get_block(1, 64, 0).properties == {"axis": "y"}
    assert reopened.get_block(2, 64, 0).name == "minecraft:air"
    assert reopened.get_block(16, 64, 0).name == "minecraft:air"
    print("After reopening:", reopened.get_block(0, 64, 0))
    print("Verified properties:", dict(reopened.get_block(1, 64, 0).properties))
    print("Saved:", output)


if __name__ == "__main__":
    main()
