# Discovering and editing dedicated-server worlds

`World` is the higher-level API above `Region`. It discovers region files across
dimensions, reads disk snapshots, queues block changes, and coordinates safe
server shutdown, file editing, and optional **server** restart. It never closes,
kills, launches, or reconnects a Minecraft **client**.

A client on the same computer is unaffected as a process, but disconnects when
its server stops. Players must reconnect after the server finishes restarting.

## 1. Find local dedicated servers

```python
from mca_editor import World

scan = World.discover_servers()
for server in scan.servers:
    print(server.pid, server.server_directory, server.world_path, server.rcon_port)
    for warning in server.warnings:
        print("  warning:", warning)
for warning in scan.warnings:
    print("Discovery warning:", warning)
```

Equivalent read-only CLI:

```sh
python main.py discover-servers
python main.py world-info /srv/minecraft/world
python main.py world-info /srv/minecraft/world --eager
```

Discovery uses `psutil` to inspect visible local Java processes, excludes known
client/integrated-server launch arguments, and looks for dedicated-server launch
hints and `server.properties`. It understands `level-name`, `--world`/`-w`,
`--universe`/`--world-dir`/`-W`, and `--config`/`-c`. Escaped Java properties and
continued lines are supported. RCON passwords and full process command lines are
**not** included in discovery results or CLI output.

Results are **candidates**, not proof of a fully loaded world. Inaccessible
processes produce warnings. A missing `level.dat`, Java `@argfile`, or Bukkit
`world-container` override leaves `world_path=None` rather than guessing.
Select a server explicitly; do not blindly stop the first result. If necessary,
confirm its world via the `world_path=` argument to `RconServer`.

This is not an exhaustive disk or network scan. Remote servers, Docker/WSL PID
namespaces not visible to the editor, hidden service accounts, custom launchers,
plugin-managed worlds, and integrated single-player servers are not automatically
identified reliably. Run the editor in the same environment with sufficient
permissions, or provide a correctly configured controller/world explicitly.

## 2. Configure graceful shutdown and optional startup

The bundled `RconServer` adapter sends Minecraft's **`stop` command**, which saves
and shuts down the server. It never uses task-kill, SIGTERM, or SIGKILL.

Before use, enable RCON in your server's `server.properties` and restart it
normally so that the setting takes effect:

```properties
enable-rcon=true
rcon.port=25575
# Set rcon.password privately to a strong password; do not commit it.
```

Keep the RCON port behind the local firewall. RCON itself is unencrypted. The
adapter accepts only **literal loopback** addresses (`127.0.0.1` or `::1`) and
checks that the RCON listening port belongs to the selected PID before sending
`stop`. It also checks the process creation time to defend against PID reuse.
The RCON port is distinct from the game port (usually 25565).

```python
import os
from mca_editor import RconServer, World

scan = World.discover_servers()
# Choose the intended dedicated server from the discovery result.
selected_pid = int(os.environ["MINECRAFT_SERVER_PID"])
selected = next(server for server in scan.servers if server.pid == selected_pid)

controller = RconServer(
    selected,
    password=os.environ["MINECRAFT_RCON_PASSWORD"],
    # world_path="/srv/minecraft/world",  # required if discovery is uncertain
    # Only needed if you will request restart. Must launch the SERVER directly.
    start_command=["java", "-Xmx4G", "-jar", "server.jar", "nogui"],
)
world = World(controller.world_path, server=controller).load()
```

Use the actual Java executable, jar, and arguments for your installation. On
Windows, for example, the executable may be an absolute path ending in
`java.exe`. `start_command` is an argument list passed without a shell, with the
server installation directory as its working directory. No command is guessed
from discovery. Shell strings, daemonizing wrappers, and automatic launchers
that exit before the real server starts are not supported by this adapter.
Output appends to `mca-editor-server.log` by default (`log_path=` can override it).

**Disable supervisor auto-restart** while editing. For systemd, Docker, hosting
panels, or custom console control, implement `ServerController`:

* `world_path: pathlib.Path`: exact primary world directory.
* `is_running() -> bool`: authoritative process/service state.
* `stop(timeout=60.0) -> None`: request graceful shutdown, wait for the JVM and
  writers to exit, raise on failure; never return while writers remain alive.
* `validate_restart() -> None`: verify explicit restart configuration.
* `restart() -> int`: start the server after locks have been released; return
  its PID, not a client or detached launcher PID.

An adapter is trusted code. Incorrect adapters cannot make unknown/noncooperating
writers safe. The world API independently requires exclusive modern world locks.

## 3. Index region files and read snapshots

```python
for file in world.region_files:
    print(file.dimension, file.region_x, file.region_z, file.path)

# Parse all indexed region files, not merely their names.
for info in world.read_all_regions():
    print(info.file.path, info.chunk_count, info.data_versions)

block = world.get_block(-1, 64, 0)
print(block.name, dict(block.properties))
nether = world.get_block(0, 64, 0, dimension="minecraft:the_nether")
```

`load()` indexes files and reads lazily. `load(eager=True)` parses all files up
front. `read_all_regions()` returns immutable metadata, not writable `Region`
handles that could accidentally bypass server shutdown. Disk snapshots are
cached; call `load()` again to refresh them (queued edits are retained).

Recognized layouts:

| Dimension | Vanilla | Paper/Spigot split world |
|---|---|---|
| Overworld | `world/region` | `world/region` |
| Nether | `world/DIM-1/region` | `world_nether/DIM-1/region` |
| End | `world/DIM1/region` | `world_the_end/DIM1/region` |
| Custom | `world/dimensions/<namespace>/<path>/region` | Explicit configuration may be required |

All active `r.<x>.<z>.mca` files are indexed. Edited copies/backups with other
names are not active slots. If both vanilla and split directories exist for a
dimension, discovery refuses the ambiguity. Resolve it explicitly:

```python
world = World(
    "/srv/minecraft/world",
    server=controller,
    dimension_paths={
        "minecraft:the_nether": "/srv/minecraft/world_nether/DIM-1/region",
    },
).load()
```

Custom dimension files appear in the index with `supported_dimension=False`.
Reading/editing them (including an eager load) raises rather than guessing their
height or silently skipping them. All existing parser version/format limits
still apply: release DataVersions 1.13–1.20.4 only, no LZ4/external `.mcc` chunks.

**A disk read is not a live game-memory read.** The running JVM may not have
flushed recent changes. Different regions may reflect different save times.
A bound running controller produces `LiveSnapshotWarning`. Checksums around a
read detect many concurrent-file changes and refuse an inconsistent read; they
do not create a globally atomic live snapshot. An unbound world is also only a
disk snapshot, regardless of whether a server is actually running.

## 4. Queue changes, then save

```python
from mca_editor import Block

world.set_block(0, 64, 0, "minecraft:diamond_block")
world.set_block(1, 64, 0, "minecraft:oak_log", {"axis": "y"})

# Optional optimistic conflict guard against the FINAL post-shutdown state.
world.set_block(2, 64, 0, "minecraft:stone", expected=Block("minecraft:air"))

# Inclusive cuboid; unlike Region.fill(), this may cross region boundaries.
# Every required region, chunk and section must already exist.
world.fill((-1, 64, 0), (1, 65, 0), "minecraft:stone")

print(world.pending_edits)
print(world.get_block(0, 64, 0))  # includes queued overlay
print(world.get_block(0, 64, 0, include_pending=False))  # cached disk state

report = world.save(restart=True, shutdown_timeout=60.0)
print(report.files, report.backup_path, report.server_stopped)
print(report.restarted, report.restart_pid)
```

Each `set_block` queues an immutable block state. Last assignment to a position
wins (including replacing its optional `expected` guard). `fill` defaults to a
one-million-position safety limit (`max_blocks=`). Queueing validates coordinates,
IDs, properties, and region availability; missing chunks/sections, block-entity
conflicts and version-specific bounds are validated against fresh data during
save. No section/chunk is automatically invented. `discard_edits()` clears the
queue. `get_block_data()` returns detached **disk snapshot** NBT, not the overlay.

Saving performs these steps:

1. If restarting was requested, validate launch configuration first.
2. If the selected server is running, authenticate locally and send `stop`.
3. Wait for that process and its observed descendants to exit. Timeout, bad
   credentials, permission problems, or unverifiable listener ownership abort
   the operation without writing regions. **Never force-kill.**
4. Acquire OS byte-range locks on the primary world's `session.lock` and each
   affected split world's lock. Check again that the server is stopped.
5. Reload affected region files **after shutdown**; never write stale live-read
   buffers over the server's last save. Apply only queued coordinates and check
   any `expected` state guards.
6. Preflight all edits, create verified backups, and stage/reparse all modified
   regions before replacing any original. Check source hashes before commits.
7. Replace each original atomically, with a recovery journal. On a normal failure,
   attempt rollback of already replaced files while retaining backups.
8. Clear successfully committed edits, release world locks, and **only if
   `restart=True`**, launch the configured server.

`restart=False` is the default. With no queued edits, `save()` does nothing,
including no shutdown or restart. If all queued states already match, no region
files/backups need writing, but an explicitly requested restart still occurs
after shutdown/reload. A restart result means the process was **launched**, not
that Minecraft has finished loading or is accepting players.

Without a server controller, `World(path).save()` can edit an already stopped
world only if it can acquire the same exclusive modern locks. It never infers
“offline” from a lock file merely existing or being absent.

## Safety, backup and failure policy

Backups are mandatory for changed world regions and unique per save:

```text
world/.mca-editor-backups/<unique-transaction-id>/
    manifest.json
    0.mca
    1.mca
```

`backup_directory=` selects a different backup **parent**; each transaction still
gets a unique subdirectory. The journal maps every backed-up file to its original
absolute path and before/after SHA-256 hashes. Backups contain the final server
save, not stale data from an earlier read.

* Any shutdown, locking, validation, edit or save failure leaves the server
  **stopped if it already stopped**, and never automatically restarts it. Inspect
  the failure before starting it manually.
* A normal commit failure attempts rollback; `WorldSaveError.backup_path`
  identifies the backups. If rollback also fails or another writer modified a
  committed file, `recovery_required=True` is set. Further saves on that `World`
  object are refused. Keep the server stopped and restore the verified backups
  manually before opening a new editing session.
* `RestartError.report` means **edits were already saved**, but launch failed.
  The queue is cleared; fix/retry startup, do not repeat the edits blindly.
* Replacements are atomic **per file**, not crash-atomic across the whole world.
  SIGKILL, power loss, full/broken disks, and concurrent uncooperative writers
  cannot be made transaction-safe by this API. Retain independent world backups.
  After an interrupted transaction, use the journal and actual file hashes
  (journal updates can lag one replacement) to assess recovery before restart.
* Modern Minecraft's `session.lock` contains a snowman marker and uses Java
  byte-range locks. The API uses POSIX record locks (`lockf`, **not** `flock`) or
  Windows byte-range locking. Legacy timestamp locks or missing/unrecognized
  locks are refused, not fabricated. Thus managed writes are intended for
  modern-lock worlds (normally 1.16+), even though the lower-level parser can
  read older releases. Use the offline `Region` API for legacy worlds after
  stopping all writers yourself.
* POSIX locks are advisory; other tools must honor them. Network filesystems
  may not provide reliable semantics. The editor process must not independently
  open/close the same lock file while a save is active (POSIX record-lock rule).
  Symlinked region files are rejected by the world API.
* The `Region` API and old region CLI edit commands remain explicit **offline**
  tools. They do not automatically discover/stop servers. Managed server writes
  must go through `World.save()`.
* Lighting, heightmaps, block entities, simulation/POI data, unsupported versions,
  and custom dimensions retain the limitations described in the main README.

## Tests and platform status

```sh
python -m pip install -e '.[test]'
python -m pytest -q
python -m pytest tests/test_discovery.py tests/test_world.py tests/test_server.py -q
```

The tests cover process discovery/client exclusion, Java properties, dimension
layouts, negative/cross-region edits, stale-read rebasing on final server saves,
conflict guards, real TCP RCON framing/authentication, timeout without killing,
PID reuse, listener ownership, subprocess-held OS locks, opt-in restart,
mandatory backups, staged failures, rollback, failed recovery and restart errors.

A **synthetic subprocess** speaks real RCON frames and holds an actual world lock
for lifecycle tests; it is explicitly not a Minecraft server. No user worlds,
Minecraft clients, or actual Minecraft servers are launched or stopped by tests.
Linux process/socket/locking paths were exercised here. Windows and macOS code
paths are provided but have not been verified on those OSes in this workspace;
permission failures are fail-closed. No in-game end-to-end claim is made.

Format references consulted:
[DirectoryLock](https://github.com/mahtomedi/minecraft/blob/main/src/main/java/net/minecraft/util/DirectoryLock.java)
and
[RconClient](https://github.com/mahtomedi/minecraft/blob/main/src/main/java/net/minecraft/server/rcon/thread/RconClient.java).
