"""Dimension-aware world snapshots and queued, server-safe block edits.

Reads are disk snapshots, NOT live Minecraft memory. Writes reload the final
server save under session locks, stage every affected region, then commit with
backups and a recovery journal. Multi-file replacement is not crash-atomic.
"""

import json
import os
import re
import shutil
import tempfile
import threading
import uuid
import warnings
from collections.abc import Iterator, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path

from .block import Block
from .coordinates import integers
from .discovery import DiscoveryResult, discover_servers
from .exceptions import (
    CoordinateError,
    DiscoveryError,
    MissingRegionError,
    RestartError,
    ServerControlError,
    UnsafeEditError,
    UnsupportedFormatError,
    WorldBusyError,
    WorldSaveError,
)
from .region import Region, _digest
from .server import ServerController, _positive_timeout
from .world_lock import session_lock

VANILLA_DIMENSIONS = {"minecraft:overworld", "minecraft:the_nether", "minecraft:the_end"}
REGION_NAME = re.compile(r"r\.(-?\d+)\.(-?\d+)\.mca\Z")


class LiveSnapshotWarning(UserWarning):
    """A running server may have unflushed data not visible in a disk snapshot."""


@dataclass(frozen=True)
class RegionFile:
    dimension: str
    region_x: int
    region_z: int
    path: Path
    world_directory: Path

    @property
    def supported_dimension(self) -> bool:
        return self.dimension in VANILLA_DIMENSIONS


@dataclass(frozen=True)
class RegionInfo:
    file: RegionFile
    chunk_count: int
    data_versions: tuple[int, ...]


@dataclass(frozen=True)
class PendingEdit:
    dimension: str
    x: int
    y: int
    z: int
    block: Block
    expected: Block | None = None


@dataclass(frozen=True)
class SaveReport:
    files: tuple[Path, ...] = ()
    backup_path: Path | None = None
    server_stopped: bool = False
    restarted: bool = False
    restart_pid: int | None = None


def _journal(directory: Path, data: dict) -> None:
    """Atomic journal update; backups remain usable even if this update fails."""
    temporary = directory / "manifest.tmp"
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, directory / "manifest.json")


class World:
    def __init__(
        self,
        path: str | Path,
        *,
        server: ServerController | None = None,
        dimension_paths: Mapping[str, str | Path] | None = None,
        metadata_policy: str = "invalidate",
        block_entity_policy: str = "error",
    ):
        """path is the primary world directory containing level.dat.

        Optional dimension_paths maps dimension IDs to explicit region folders
        (not .mca files), replacing discovery for those dimensions. Server
        association must point to this exact world; no arbitrary PID is killed.
        """
        self.path = Path(path).resolve()
        self.server = server
        if server is not None and Path(server.world_path).resolve() != self.path:
            raise ServerControlError("Controller belongs to a different world")
        self._dimension_paths = {
            name: Path(path).resolve() for name, path in (dimension_paths or {}).items()
        }
        if metadata_policy not in ("invalidate", "preserve") or block_entity_policy not in (
            "error",
            "remove",
        ):
            raise ValueError("Invalid metadata or block entity policy")
        self.metadata_policy = metadata_policy
        self.block_entity_policy = block_entity_policy
        self._files: dict[tuple[str, int, int], RegionFile] = {}
        self._cache: dict[tuple[str, int, int], Region] = {}
        self._pending: dict[tuple[str, int, int, int], PendingEdit] = {}
        self._loaded = False
        self._mutex = threading.RLock()
        self._recovery_required = False

    @staticmethod
    def discover_servers() -> DiscoveryResult:
        return discover_servers()

    @property
    def pending_edits(self) -> tuple[PendingEdit, ...]:
        with self._mutex:
            return tuple(self._pending.values())

    @property
    def region_files(self) -> tuple[RegionFile, ...]:
        self._require_loaded()
        with self._mutex:
            return tuple(self._files[key] for key in sorted(self._files))

    def _require_loaded(self) -> None:
        if not self._loaded:
            raise DiscoveryError("Call World.load() before accessing world files")

    def _owner(self, folder: Path) -> Path:
        if folder.is_relative_to(self.path):
            return self.path
        for parent in (folder, *folder.parents):
            if (parent / "level.dat").is_file():
                return parent
        raise DiscoveryError(f"No owning world with level.dat found for {folder}")

    def _scan(self) -> dict[tuple[str, int, int], RegionFile]:
        if not (self.path / "level.dat").is_file():
            raise DiscoveryError(f"World directory has no level.dat: {self.path}")
        dimensions = {"minecraft:overworld": self.path / "region"}
        for name, folder, suffix in (
            ("minecraft:the_nether", "DIM-1", "_nether"),
            ("minecraft:the_end", "DIM1", "_the_end"),
        ):
            vanilla = self.path / folder / "region"
            split = self.path.with_name(self.path.name + suffix) / folder / "region"
            if vanilla.is_dir() and split.is_dir() and name not in self._dimension_paths:
                raise DiscoveryError(
                    f"Both vanilla and split folders exist for {name}; set dimension_paths explicitly"
                )
            dimensions[name] = vanilla if vanilla.is_dir() else split
        custom = self.path / "dimensions"
        if custom.is_dir():
            for folder in sorted(custom.rglob("region")):
                if folder.is_dir():
                    parts = folder.relative_to(custom).parts[:-1]
                    if len(parts) >= 2:
                        dimensions[f"{parts[0]}:{'/'.join(parts[1:])}"] = folder
        dimensions.update(self._dimension_paths)
        result = {}
        used_paths = set()
        for dimension, folder in dimensions.items():
            if not folder.exists():
                if dimension in self._dimension_paths:
                    raise DiscoveryError(f"Explicit dimension folder does not exist: {folder}")
                continue
            owner = self._owner(folder.resolve())
            for path in sorted(folder.glob("*.mca")):
                match = REGION_NAME.fullmatch(path.name)
                if not match:
                    continue  # backups and edited copies are not active region slots
                if path.is_symlink():
                    raise DiscoveryError(f"World API refuses symlinked region files: {path}")
                path = path.resolve()
                if path in used_paths:
                    raise DiscoveryError(f"Region mapped to multiple dimensions: {path}")
                used_paths.add(path)
                rx, rz = map(int, match.groups())
                key = dimension, rx, rz
                if key in result:
                    raise DiscoveryError(f"Duplicate region coordinates for {key}")
                result[key] = RegionFile(dimension, rx, rz, path, owner)
        return result

    def load(self, *, eager: bool = False) -> "World":
        """Refresh the index and discard cached disk snapshots, retaining edits.

        eager=True parses every region, raising on unsupported dimensions or
        formats rather than quietly skipping any. The default is lazy to avoid
        loading an entire large world into RAM. No server is stopped for reads.
        """
        with self._mutex:
            self._files = self._scan()
            self._cache.clear()
            self._loaded = True
            if self.server is not None and self.server.is_running():
                warnings.warn(
                    "Reading a running server's disk snapshot: unsaved changes are invisible; "
                    "save() will stop the server and reload before applying queued edits",
                    LiveSnapshotWarning,
                    stacklevel=2,
                )
            if eager:
                for _ in self.read_all_regions():
                    pass
        return self

    def _file(self, x: int, z: int, dimension: str) -> RegionFile:
        self._require_loaded()
        integers(x, z)
        if dimension not in VANILLA_DIMENSIONS:
            raise UnsupportedFormatError(
                f"Unsupported world dimension {dimension!r}; custom heights are not inferred"
            )
        key = dimension, x // 512, z // 512
        if key not in self._files:
            raise MissingRegionError(f"No region file for {key}")
        return self._files[key]

    def _fresh_region(self, file: RegionFile) -> Region:
        if not file.supported_dimension:
            raise UnsupportedFormatError(f"Unsupported world dimension {file.dimension!r}")
        before = _digest(file.path)
        region = Region(
            file.path,
            dimension=file.dimension,
            metadata_policy=self.metadata_policy,
            block_entity_policy=self.block_entity_policy,
        ).load()
        if before != region._source_digest or before != _digest(file.path):
            raise WorldBusyError(
                f"Region changed while being read; retry after shutdown: {file.path}"
            )
        return region

    def _read_region(self, file: RegionFile) -> Region:
        key = file.dimension, file.region_x, file.region_z
        if key not in self._cache:
            self._cache[key] = self._fresh_region(file)
        return self._cache[key]

    def read_all_regions(self) -> Iterator[RegionInfo]:
        """Parse each indexed region and return metadata (not writable handles)."""
        for file in self.region_files:
            with self._mutex:
                region = self._read_region(file)
                info = RegionInfo(
                    file,
                    region.chunk_count,
                    tuple(sorted({c.data_version for c in region.iter_chunks()})),
                )
            yield info

    def get_block(
        self,
        x: int,
        y: int,
        z: int,
        *,
        dimension: str = "minecraft:overworld",
        include_pending: bool = True,
    ) -> Block:
        integers(x, y, z)
        with self._mutex:
            file = self._file(x, z, dimension)
            edit = self._pending.get((dimension, x, y, z)) if include_pending else None
            return edit.block if edit else self._read_region(file).get_block(x, y, z)

    def get_block_data(self, x: int, y: int, z: int, *, dimension: str = "minecraft:overworld"):
        """Detached NBT from the disk snapshot, excluding queued edits."""
        integers(x, y, z)
        with self._mutex:
            return self._read_region(self._file(x, z, dimension)).get_block_data(x, y, z)

    def set_block(
        self,
        x: int,
        y: int,
        z: int,
        block_name: str,
        properties: Mapping[str, str] | None = None,
        *,
        dimension: str = "minecraft:overworld",
        expected: Block | None = None,
    ) -> None:
        """Queue an edit, never write live files. Last assignment wins.

        expected optionally rejects a conflict with the final post-shutdown block.
        Chunk/section existence and version-specific Y are checked after shutdown.
        """
        integers(x, y, z)
        block = Block(block_name, properties or {})
        if expected is not None and not isinstance(expected, Block):
            raise TypeError("expected must be a Block or None")
        with self._mutex:
            self._file(x, z, dimension)
            self._pending[dimension, x, y, z] = PendingEdit(dimension, x, y, z, block, expected)

    def fill(
        self,
        start: tuple[int, int, int],
        end: tuple[int, int, int],
        block_name: str,
        properties: Mapping[str, str] | None = None,
        *,
        dimension: str = "minecraft:overworld",
        max_blocks: int = 1_000_000,
    ) -> int:
        """Queue an inclusive cuboid, including across region boundaries."""
        if len(start) != 3 or len(end) != 3:
            raise CoordinateError("Cuboid corners must have three integer coordinates")
        integers(*start, *end, max_blocks)
        if any(a > b for a, b in zip(start, end)):
            raise CoordinateError("Cuboid start must be <= end on every axis")
        count = (end[0] - start[0] + 1) * (end[1] - start[1] + 1) * (end[2] - start[2] + 1)
        if count > max_blocks:
            raise CoordinateError(f"Fill exceeds max_blocks={max_blocks}")
        block = Block(block_name, properties or {})
        with self._mutex:
            for rx in range(start[0] // 512, end[0] // 512 + 1):
                for rz in range(start[2] // 512, end[2] // 512 + 1):
                    self._file(rx * 512, rz * 512, dimension)
            for y in range(start[1], end[1] + 1):
                for z in range(start[2], end[2] + 1):
                    for x in range(start[0], end[0] + 1):
                        self._pending[dimension, x, y, z] = PendingEdit(dimension, x, y, z, block)
        return count

    def discard_edits(self) -> None:
        with self._mutex:
            self._pending.clear()

    def _assert_stopped(self) -> None:
        if self.server is not None and self.server.is_running():
            raise WorldBusyError("Server is still running or restarted; refusing writes")

    def _commit(self, regions: list[Region], backup_root: Path) -> tuple[Path, ...]:
        """Stage all files before replacing any; best-effort rollback on error."""
        try:
            backup_root.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            raise WorldSaveError(
                "Cannot create backup directory; no region files were replaced",
                backup_path=backup_root,
            ) from exc
        entries = []
        staged = []
        committed = []
        journal = {"status": "preparing", "world": str(self.path), "files": entries}
        try:
            for index, region in enumerate(regions):
                source = region.path
                before = region._source_digest
                if _digest(source) != before:
                    raise WorldBusyError(f"Source changed before staging: {source}")
                backup = backup_root / f"{index}.mca"
                shutil.copy2(source, backup)
                with backup.open("r+b") as stream:
                    os.fsync(stream.fileno())
                if _digest(backup) != before:
                    raise WorldBusyError(f"Source changed while backing up: {source}")
                fd, name = tempfile.mkstemp(prefix=".mca-editor-", suffix=".tmp", dir=source.parent)
                os.close(fd)
                stage = Path(name)
                staged.append(stage)
                region.save(stage)  # Includes fsync, full reparse and record validation.
                os.chmod(stage, source.stat().st_mode & 0o777)
                entries.append(
                    {
                        "source": str(source),
                        "backup": backup.name,
                        "stage": str(stage),
                        "before_sha256": before.hex(),
                        "after_sha256": _digest(stage).hex(),
                        "committed": False,
                    }
                )
            journal["status"] = "staged"
            _journal(backup_root, journal)
            # Validate every original before the first replacement.
            for entry in entries:
                if _digest(Path(entry["source"])).hex() != entry["before_sha256"]:
                    raise WorldBusyError("Region changed before commit")
            for entry in entries:
                self._assert_stopped()
                source = Path(entry["source"])
                if _digest(source).hex() != entry["before_sha256"]:
                    raise WorldBusyError(f"Region changed during commit: {source}")
                os.replace(entry["stage"], source)
                committed.append(entry)
                entry["committed"] = True
                _journal(backup_root, journal)
            journal["status"] = "committed"
            _journal(backup_root, journal)
            return tuple(Path(entry["source"]) for entry in entries)
        except (Exception, KeyboardInterrupt) as exc:
            failures = []
            for entry in reversed(committed):
                restore = None
                try:
                    source = Path(entry["source"])
                    backup = backup_root / entry["backup"]
                    if _digest(source).hex() != entry["after_sha256"]:
                        raise WorldBusyError(
                            "Another writer changed a committed region; rollback refused"
                        )
                    if _digest(backup).hex() != entry["before_sha256"]:
                        raise WorldSaveError("Backup checksum changed; rollback refused")
                    fd, name = tempfile.mkstemp(
                        prefix=".mca-restore-", suffix=".tmp", dir=source.parent
                    )
                    os.close(fd)
                    restore = Path(name)
                    shutil.copy2(backup, restore)
                    with restore.open("r+b") as stream:
                        os.fsync(stream.fileno())
                    os.replace(restore, source)
                    entry["committed"] = False
                except Exception:
                    failures.append(entry["source"])
                finally:
                    if restore is not None:
                        restore.unlink(missing_ok=True)
            journal["status"] = "recovery_required" if failures else "rolled_back"
            journal["recovery_files"] = failures
            try:
                _journal(backup_root, journal)
            except OSError:
                pass  # Verified backup files are retained even if the journal fails.
            self._recovery_required = bool(failures)
            raise WorldSaveError(
                f"World save failed; {'manual recovery required' if failures else 'committed files rolled back'}. "
                f"Server will not be restarted. Backups: {backup_root}",
                backup_path=backup_root,
                recovery_required=bool(failures),
            ) from exc
        finally:
            for stage in staged:
                stage.unlink(missing_ok=True)

    def save(
        self,
        *,
        restart: bool = False,
        shutdown_timeout: float = 60.0,
        backup_directory: str | Path | None = None,
    ) -> SaveReport:
        """Gracefully stop SERVER, reload, apply queued edits, save, optionally restart.

        Backups are mandatory and uniquely named. Restart is opt-in and never
        happens on an edit/commit failure. No client process is controlled.
        Without a controller, saving is allowed only with exclusive modern locks.
        """
        _positive_timeout(shutdown_timeout)
        with self._mutex:
            self._require_loaded()
            if self._recovery_required:
                raise WorldSaveError(
                    "Resolve the previous recovery-required transaction before further saves",
                    recovery_required=True,
                )
            if not self._pending:
                return SaveReport()
            if self.server is not None and Path(self.server.world_path).resolve() != self.path:
                raise ServerControlError("Controller belongs to a different world")
            if restart:
                if self.server is None:
                    raise ServerControlError("Restart requires a configured server controller")
                self.server.validate_restart()
            stopped = self.server is not None and self.server.is_running()
            if stopped:
                self.server.stop(timeout=shutdown_timeout)
            self._assert_stopped()
            # Reload the final server output. Never serialize the earlier live cache.
            self._files = self._scan()
            self._cache.clear()
            selected = {}
            for edit in self._pending.values():
                file = self._file(edit.x, edit.z, edit.dimension)
                selected[file.dimension, file.region_x, file.region_z] = file
            owners = {self.path} | {file.world_directory for file in selected.values()}
            report = SaveReport(server_stopped=stopped)
            with ExitStack() as locks:
                for owner in sorted(owners):
                    locks.enter_context(session_lock(owner))
                self._assert_stopped()
                regions = {key: self._fresh_region(file) for key, file in selected.items()}
                for edit in self._pending.values():
                    region = regions[edit.dimension, edit.x // 512, edit.z // 512]
                    if (
                        edit.expected is not None
                        and region.get_block(edit.x, edit.y, edit.z) != edit.expected
                    ):
                        raise UnsafeEditError(
                            f"Post-shutdown block conflicts with expected state at {(edit.dimension, edit.x, edit.y, edit.z)}"
                        )
                    region.set_block(edit.x, edit.y, edit.z, edit.block.name, edit.block.properties)
                changed = [r for r in regions.values() if any(c.modified for c in r.iter_chunks())]
                if changed:
                    backup_parent = (
                        Path(backup_directory).resolve()
                        if backup_directory
                        else self.path / ".mca-editor-backups"
                    )
                    backup = backup_parent / uuid.uuid4().hex
                    files = self._commit(changed, backup)
                    report = replace(report, files=files, backup_path=backup)
                self._pending.clear()
            # Locks MUST be released before launch, otherwise Minecraft cannot open the world.
            if restart:
                try:
                    pid = self.server.restart()
                    report = replace(report, restarted=True, restart_pid=pid)
                except Exception as exc:
                    raise RestartError(
                        "Edits were saved, but server restart failed; do not reapply the edits",
                        report,
                    ) from exc
            return report
