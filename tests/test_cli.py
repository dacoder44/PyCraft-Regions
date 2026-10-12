import json
import subprocess
import sys

from mca_editor import Region
from mca_editor.cli import main
from mca_editor.fixtures import make_chunk


def test_cli_info_chunks_get_validate(fixture_region, capsys):
    r = fixture_region()
    assert main(["info", str(r.path)]) == 0
    assert json.loads(capsys.readouterr().out)["chunks"] == 1
    assert main(["chunks", str(r.path)]) == 0
    assert json.loads(capsys.readouterr().out)["sections"] == [4]
    assert main(["get-block", str(r.path), "--x", "0", "--y", "64", "--z", "0"]) == 0
    assert json.loads(capsys.readouterr().out)["name"] == "minecraft:air"
    assert main(["validate", str(r.path)]) == 0
    assert "Valid:" in capsys.readouterr().out


def test_cli_edits_separate_by_default(fixture_region, capsys, tmp_path):
    r = fixture_region(documents=[make_chunk(), make_chunk(1, 0)])
    original = r.path.read_bytes()
    assert (
        main(
            [
                "set-block",
                str(r.path),
                "--x",
                "0",
                "--y",
                "64",
                "--z",
                "0",
                "--block",
                "minecraft:oak_log",
                "--property",
                "axis=y",
            ]
        )
        == 0
    )
    output = tmp_path / "r.0.0.edited.mca"
    assert Region(output).load().get_block(0, 64, 0).properties == {"axis": "y"}
    assert r.path.read_bytes() == original
    filled = tmp_path / "filled.mca"
    assert (
        main(
            [
                "fill",
                str(r.path),
                "--from",
                "14,64,0",
                "--to",
                "17,65,1",
                "--block",
                "minecraft:stone",
                "--output",
                str(filled),
            ]
        )
        == 0
    )
    loaded = Region(filled).load()
    for x in range(14, 18):
        assert loaded.get_block(x, 64, 0).name == "minecraft:stone"
    assert loaded.get_block(18, 64, 0).name == "minecraft:air"
    assert main(["validate", str(filled)]) == 0
    assert (
        main(
            [
                "set-block",
                str(r.path),
                "--x",
                "0",
                "--y",
                "64",
                "--z",
                "0",
                "--block",
                "minecraft:stone",
            ]
        )
        == 1
    )  # refuses output overwrite
    assert "already exists" in capsys.readouterr().err


def test_cli_in_place_backup_and_errors(fixture_region, capsys):
    r = fixture_region()
    original = r.path.read_bytes()
    base = [
        "set-block",
        str(r.path),
        "--x",
        "0",
        "--y",
        "64",
        "--z",
        "0",
        "--block",
        "minecraft:stone",
    ]
    assert main(base + ["--output", str(r.path)]) == 1
    assert "--in-place" in capsys.readouterr().err
    assert main(base + ["--in-place"]) == 0
    assert r.path.with_suffix(".mca.bak").read_bytes() == original
    assert Region(r.path).load().get_block(0, 64, 0).name == "minecraft:stone"
    assert main(["get-block", str(r.path), "--x", "512", "--y", "64", "--z", "0"]) == 1
    assert "outside region" in capsys.readouterr().err


def test_entrypoint_and_argparse_errors(fixture_region):
    r = fixture_region()
    result = subprocess.run(
        [sys.executable, "main.py", "validate", str(r.path)], text=True, capture_output=True
    )
    assert result.returncode == 0 and "Valid:" in result.stdout
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mca_editor",
            "fill",
            str(r.path),
            "--from",
            "bad",
            "--to",
            "1,2,3",
            "--block",
            "minecraft:stone",
        ],
        text=True,
        capture_output=True,
    )
    assert result.returncode == 2 and "three integers" in result.stderr
