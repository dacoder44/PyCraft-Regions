import json
from pathlib import Path

import pytest

from mca_editor import (
    Block,
    DiscoveryError,
    LiveSnapshotWarning,
    MissingRegionError,
    Region,
    RestartError,
    ServerControlError,
    UnsafeEditError,
    UnsupportedFormatError,
    World,
    WorldBusyError,
    WorldSaveError,
)
from mca_editor.fixtures import make_chunk, write_region
from mca_editor.nbt_utils import Int
from mca_editor.world_lock import MODERN_LOCK_MARKER, session_lock


@pytest.fixture
def world_dir(tmp_path):
    root = tmp_path / "world"
    root.mkdir()
    (root / "level.dat").write_bytes(b"synthetic world marker")
    (root / "session.lock").write_bytes(MODERN_LOCK_MARKER)
    (root / "region").mkdir()
    write_region(root / "region" / "r.-1.0.mca", [make_chunk(-1, 0)])
    write_region(root / "region" / "r.0.0.mca", [make_chunk(0, 0)])
    return root


class Controller:
    def __init__(self, root, *, running=True, on_stop=None):
        self.world_path = root
        self.running = running
        self.on_stop = on_stop
        self.events = []
        self.restartable = True

    def is_running(self):
        return self.running

    def stop(self, timeout=60):
        self.events.append("stop")
        if self.on_stop:
            self.on_stop()
        self.running = False

    def validate_restart(self):
        self.events.append("validate_restart")
        if not self.restartable:
            raise ServerControlError("No start command")

    def restart(self):
        # Assert locks were released before invoking the launcher.
        with session_lock(self.world_path):
            pass
        self.events.append("restart")
        self.running = True
        return 9876


def test_discovery_index_reads_and_negative_cross_region_edits(world_dir):
    world = World(world_dir).load(eager=True)
    assert len(world.region_files) == len(list(world.read_all_regions())) == 2
    before = {f.path: f.path.read_bytes() for f in world.region_files}
    assert world.get_block(-1, 64, 0).name == "minecraft:air"
    assert world.fill((-1, 64, 0), (1, 65, 0), "minecraft:oak_log", {"axis": "y"}) == 6
    assert world.get_block(-1, 64, 0).properties == {"axis": "y"}
    assert world.get_block(-1, 64, 0, include_pending=False).name == "minecraft:air"
    assert world.get_block_data(-1, 64, 0).value["Name"].value == "minecraft:air"
    assert all(path.read_bytes() == data for path, data in before.items())
    report = world.save()
    assert len(report.files) == 2
    assert not world.pending_edits
    manifest = json.loads((report.backup_path / "manifest.json").read_text())
    assert manifest["status"] == "committed"
    for entry in manifest["files"]:
        assert (report.backup_path / entry["backup"]).read_bytes() == before[Path(entry["source"])]
    reopened = World(world_dir).load()
    for x in (-1, 0, 1):
        for y in (64, 65):
            assert reopened.get_block(x, y, 0) == Block("minecraft:oak_log", {"axis": "y"})
    assert reopened.get_block(2, 64, 0).name == "minecraft:air"
    assert world.get_block(-1, 64, 0, include_pending=False).properties == {"axis": "y"}


def test_shutdown_final_save_reloaded_and_optional_restart(world_dir):
    path = world_dir / "region" / "r.0.0.mca"

    def final_server_save():
        region = Region(path).load()
        region.set_block(1, 64, 0, "minecraft:gold_block")
        region.save()

    server = Controller(world_dir, on_stop=final_server_save)
    with pytest.warns(LiveSnapshotWarning):
        world = World(world_dir, server=server).load()
    assert world.get_block(1, 64, 0).name == "minecraft:air"  # cached live disk view
    world.set_block(0, 64, 0, "minecraft:diamond_block")
    report = world.save(restart=True)
    assert server.events == ["validate_restart", "stop", "restart"]
    assert report.restarted and report.server_stopped and report.restart_pid == 9876
    reopened = Region(path).load()
    assert reopened.get_block(0, 64, 0).name == "minecraft:diamond_block"
    assert reopened.get_block(1, 64, 0).name == "minecraft:gold_block"


def test_shutdown_only_default_and_unique_backups(world_dir):
    server = Controller(world_dir)
    with pytest.warns(LiveSnapshotWarning):
        world = World(world_dir, server=server).load()
    world.set_block(0, 64, 0, "minecraft:stone")
    first = world.save()
    assert server.events == ["stop"] and not first.restarted
    assert (
        Region(world_dir / "region/r.0.0.mca").load().get_block(0, 64, 0).name == "minecraft:stone"
    )
    world.set_block(1, 64, 0, "minecraft:diamond_block")
    second = world.save()
    assert first.backup_path != second.backup_path
    assert (
        Region(world_dir / "region/r.0.0.mca").load().get_block(1, 64, 0).name
        == "minecraft:diamond_block"
    )


def test_expected_state_conflict_leaves_final_server_data(world_dir):
    path = world_dir / "region/r.0.0.mca"

    def final_save():
        region = Region(path).load()
        region.set_block(0, 64, 0, "minecraft:stone")
        region.save()

    server = Controller(world_dir, on_stop=final_save)
    with pytest.warns(LiveSnapshotWarning):
        world = World(world_dir, server=server).load()
    world.set_block(0, 64, 0, "minecraft:diamond_block", expected=Block("minecraft:air"))
    with pytest.raises(UnsafeEditError, match="conflicts"):
        world.save(restart=True)
    assert "restart" not in server.events
    assert len(world.pending_edits) == 1
    assert Region(path).load().get_block(0, 64, 0).name == "minecraft:stone"


@pytest.mark.parametrize("failure", ["timeout", "still_running", "no_command", "wrong_world"])
def test_unsafe_lifecycle_refuses_writes(world_dir, failure):
    server = Controller(world_dir)
    with pytest.warns(LiveSnapshotWarning):
        world = World(world_dir, server=server).load()
    world.set_block(0, 64, 0, "minecraft:stone")
    original = (world_dir / "region/r.0.0.mca").read_bytes()
    if failure == "timeout":

        def fail(timeout):
            raise ServerControlError("shutdown timed out")

        server.stop = fail
    elif failure == "still_running":
        server.stop = lambda timeout: None
    elif failure == "no_command":
        server.restartable = False
    else:
        server.world_path = world_dir.parent
    with pytest.raises((ServerControlError, WorldBusyError)):
        world.save(restart=True)
    assert "restart" not in server.events
    assert (world_dir / "region/r.0.0.mca").read_bytes() == original
    assert Region(world_dir / "region/r.0.0.mca").load().get_block(0, 64, 0).name == "minecraft:air"


def test_restart_failure_is_distinct_from_save_failure(world_dir):
    server = Controller(world_dir, running=False)
    world = World(world_dir, server=server).load()
    world.set_block(0, 64, 0, "minecraft:stone")

    def fail():
        raise ServerControlError("bad executable")

    server.restart = fail
    with pytest.raises(RestartError) as error:
        world.save(restart=True)
    assert error.value.report.files and error.value.report.backup_path
    assert not world.pending_edits
    assert (
        Region(world_dir / "region/r.0.0.mca").load().get_block(0, 64, 0).name == "minecraft:stone"
    )


@pytest.mark.parametrize("failure", ["stage", "commit", "rollback"])
def test_multiregion_failure_rollback_and_recovery(world_dir, monkeypatch, failure):
    import mca_editor.world as module

    server = Controller(world_dir, running=False)
    world = World(world_dir, server=server).load()
    before = {f.path: f.path.read_bytes() for f in world.region_files}
    world.set_block(-1, 64, 0, "minecraft:stone")
    world.set_block(0, 64, 0, "minecraft:stone")
    real_save, real_replace = Region.save, module.os.replace
    first, second = world_dir / "region/r.-1.0.mca", world_dir / "region/r.0.0.mca"
    if failure == "stage":

        def fail_save(region, *args, **kwargs):
            if region.path == second:
                raise OSError("injected staging failure")
            return real_save(region, *args, **kwargs)

        monkeypatch.setattr(Region, "save", fail_save)
    else:

        def fail_replace(source, target):
            if Path(target) == second:
                raise OSError("injected second-file commit failure")
            if (
                failure == "rollback"
                and Path(target) == first
                and Path(source).name.startswith(".mca-restore-")
            ):
                raise OSError("injected rollback failure")
            return real_replace(source, target)

        monkeypatch.setattr(module.os, "replace", fail_replace)
    with pytest.raises(WorldSaveError) as error:
        world.save(restart=True)
    assert "restart" not in server.events
    assert len(world.pending_edits) == 2
    report = json.loads((error.value.backup_path / "manifest.json").read_text())
    if failure == "rollback":
        assert error.value.recovery_required and report["status"] == "recovery_required"
        assert Region(first).load().get_block(-1, 64, 0).name == "minecraft:stone"
        assert Region(second).load().get_block(0, 64, 0).name == "minecraft:air"
        with pytest.raises(WorldSaveError, match="previous"):
            world.save()
    else:
        assert not error.value.recovery_required and report["status"] == "rolled_back"
        assert all(path.read_bytes() == data for path, data in before.items())
        assert Region(first).load().get_block(-1, 64, 0).name == "minecraft:air"
    assert not list((world_dir / "region").glob(".mca-*.tmp"))


@pytest.mark.parametrize("layout", ["vanilla", "paper"])
def test_dimension_index_and_edits(world_dir, layout):
    for dimension, suffix, name in (
        ("DIM-1", "_nether", "minecraft:the_nether"),
        ("DIM1", "_the_end", "minecraft:the_end"),
    ):
        owner = world_dir if layout == "vanilla" else world_dir.with_name(world_dir.name + suffix)
        owner.mkdir(exist_ok=True)
        (owner / "level.dat").touch()
        (owner / "session.lock").write_bytes(MODERN_LOCK_MARKER)
        region_dir = owner / dimension / "region"
        region_dir.mkdir(parents=True)
        doc = make_chunk()
        doc.root.value["yPos"] = Int(0)
        write_region(region_dir / "r.0.0.mca", [doc])
    world = World(world_dir).load(eager=True)
    assert len(world.region_files) == 4
    world.set_block(0, 64, 0, "minecraft:netherrack", dimension="minecraft:the_nether")
    world.set_block(0, 64, 0, "minecraft:end_stone", dimension="minecraft:the_end")
    world.save()
    reopened = World(world_dir).load()
    assert (
        reopened.get_block(0, 64, 0, dimension="minecraft:the_nether").name
        == "minecraft:netherrack"
    )
    assert reopened.get_block(0, 64, 0, dimension="minecraft:the_end").name == "minecraft:end_stone"
    assert reopened.get_block(0, 64, 0).name == "minecraft:air"


def test_custom_dimension_indexed_but_not_silently_parsed(world_dir):
    folder = world_dir / "dimensions/example/moon/region"
    folder.mkdir(parents=True)
    write_region(folder / "r.0.0.mca", [make_chunk()])
    world = World(world_dir).load()
    assert any(
        f.dimension == "example:moon" and not f.supported_dimension for f in world.region_files
    )
    with pytest.raises(UnsupportedFormatError):
        world.get_block(0, 64, 0, dimension="example:moon")
    with pytest.raises(UnsupportedFormatError):
        World(world_dir).load(eager=True)


def test_ambiguous_dimensions_require_explicit_paths(world_dir):
    vanilla = world_dir / "DIM-1/region"
    split = world_dir.with_name("world_nether") / "DIM-1/region"
    vanilla.mkdir(parents=True)
    split.mkdir(parents=True)
    with pytest.raises(DiscoveryError, match="Both"):
        World(world_dir).load()
    World(world_dir, dimension_paths={"minecraft:the_nether": vanilla}).load()


def test_missing_region_fill_does_not_queue_partial_edits(world_dir):
    world = World(world_dir).load()
    with pytest.raises(MissingRegionError):
        world.fill((0, 64, 0), (512, 64, 0), "minecraft:stone")
    assert not world.pending_edits
    world.set_block(0, 64, 0, "minecraft:stone")
    world.discard_edits()
    assert not world.save().files
    assert Region(world_dir / "region/r.0.0.mca").load().get_block(0, 64, 0).name == "minecraft:air"


def test_session_lock_guard_and_legacy_refusal(world_dir):
    world = World(world_dir).load()
    world.set_block(0, 64, 0, "minecraft:stone")
    with session_lock(world_dir):
        with pytest.raises(WorldBusyError):
            world.save()
    (world_dir / "session.lock").write_bytes(b"legacy!!")
    with pytest.raises(WorldBusyError, match="Legacy"):
        world.save()
    assert Region(world_dir / "region/r.0.0.mca").load().get_block(0, 64, 0).name == "minecraft:air"


def test_read_detects_file_changed_during_snapshot(world_dir, monkeypatch):
    world = World(world_dir).load()
    original_load = Region.load

    def change_after_read(region):
        result = original_load(region)
        data = bytearray(region.path.read_bytes())
        data[4096] ^= 1  # Valid but changed header timestamp.
        region.path.write_bytes(data)
        return result

    monkeypatch.setattr(Region, "load", change_after_read)
    with pytest.raises(WorldBusyError, match="changed"):
        world.get_block(0, 64, 0)


def test_noop_save_does_not_stop_server(world_dir):
    server = Controller(world_dir)
    with pytest.warns(LiveSnapshotWarning):
        world = World(world_dir, server=server).load()
    report = world.save(restart=True)
    assert not report.server_stopped and not report.restarted
    assert not server.events and server.running


def test_section_preflight_failure_does_not_write_any_region(world_dir):
    from mca_editor import MissingSectionError

    world = World(world_dir).load()
    before = {f.path: f.path.read_bytes() for f in world.region_files}
    world.set_block(-1, 64, 0, "minecraft:stone")
    world.set_block(0, 80, 0, "minecraft:stone")  # This existing chunk has no section 5.
    with pytest.raises(MissingSectionError):
        world.save()
    assert all(path.read_bytes() == data for path, data in before.items())
    assert (
        Region(world_dir / "region/r.-1.0.mca").load().get_block(-1, 64, 0).name == "minecraft:air"
    )


def test_session_lock_does_not_swallow_body_error(world_dir):
    with pytest.raises(OSError, match="disk full"):
        with session_lock(world_dir):
            raise OSError("disk full")
    with session_lock(world_dir):
        pass  # cleanup released the OS lock and in-process guard


def test_world_cli_index_only_does_not_modify_files(world_dir, capsys):
    from mca_editor.cli import main

    before = (world_dir / "region/r.0.0.mca").read_bytes()
    assert main(["world-info", str(world_dir), "--eager"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["snapshot_only"] and len(result["regions"]) == 2
    assert (world_dir / "region/r.0.0.mca").read_bytes() == before
