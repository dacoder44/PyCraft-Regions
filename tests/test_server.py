import json
import socket
import struct
import subprocess
import sys
import threading
from pathlib import Path
from queue import Queue

import psutil
import pytest

from mca_editor import (
    LiveSnapshotWarning,
    RconServer,
    Region,
    ServerControlError,
    ServerProcess,
    World,
    WorldBusyError,
)
from mca_editor.fixtures import make_chunk, write_region
from mca_editor.server import _RconConnection
from mca_editor.world_lock import MODERN_LOCK_MARKER

HELPER = Path(__file__).parent / "helpers/rcon_process.py"


@pytest.fixture
def server_factory(tmp_path):
    children = []
    controllers = []

    def start(*, ignore_stop=False):
        root = tmp_path / f"world{len(children)}"
        (root / "region").mkdir(parents=True)
        (root / "level.dat").touch()
        (root / "session.lock").write_bytes(MODERN_LOCK_MARKER)
        write_region(root / "region/r.0.0.mca", [make_chunk()])
        command = [sys.executable, str(HELPER.resolve()), str(root)]
        if ignore_stop:
            command.append("--ignore-stop")
        child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        children.append(child)
        # Bounded readiness wait: failed subprocess startup cannot hang the suite.
        output = Queue()
        reader = threading.Thread(target=lambda: output.put(child.stdout.readline()), daemon=True)
        reader.start()
        line = output.get(timeout=10)
        assert line, child.stderr.read()
        port = json.loads(line)["port"]
        process = psutil.Process(child.pid)
        info = ServerProcess(
            child.pid,
            process.create_time(),
            tmp_path,
            root,
            tmp_path / "server.properties",
            port,
            "synthetic integration fixture",
        )
        controller = RconServer(
            info,
            password="fixture-password",
            start_command=[sys.executable, str(HELPER.resolve()), str(root), "--port", str(port)],
        )
        controllers.append(controller)
        return child, info, controller, root

    yield start
    # Cleanup only test-owned synthetic processes; production never calls kill.
    for controller in controllers:
        if controller._started_process is not None:
            process = controller._started_process
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)
    for child in children:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=10)
        child.stdout.close()
        child.stderr.close()


def test_real_socket_stop_flush_edit_and_lock_release(server_factory):
    child, info, controller, root = server_factory()
    with pytest.warns(LiveSnapshotWarning):
        world = World(root, server=controller).load()
    assert world.get_block(1, 64, 0).name == "minecraft:air"
    world.set_block(0, 64, 0, "minecraft:diamond_block")
    report = world.save(shutdown_timeout=10)
    assert report.server_stopped and not report.restarted
    assert not controller.is_running()
    child.wait(timeout=5)
    reopened = Region(root / "region/r.0.0.mca").load()
    assert reopened.get_block(0, 64, 0).name == "minecraft:diamond_block"
    assert reopened.get_block(1, 64, 0).name == "minecraft:gold_block"


def test_real_process_restart_explicit_command_and_log(server_factory):
    _, _, controller, root = server_factory()
    with pytest.warns(LiveSnapshotWarning):
        world = World(root, server=controller).load()
    world.set_block(0, 64, 0, "minecraft:diamond_block")
    report = world.save(restart=True, shutdown_timeout=10)
    assert report.restarted and controller.pid == report.restart_pid
    assert controller.is_running()
    # Restart promises a spawned process, not network/Minecraft readiness.
    assert controller.log_path.exists()
    assert (
        Region(root / "region/r.0.0.mca").load().get_block(0, 64, 0).name
        == "minecraft:diamond_block"
    )


def test_live_lock_blocks_uncontrolled_world_write(server_factory):
    child, _, _, root = server_factory()
    world = World(root).load()
    world.set_block(0, 64, 0, "minecraft:stone")
    before = (root / "region/r.0.0.mca").read_bytes()
    with pytest.raises(WorldBusyError):
        world.save()
    assert child.poll() is None
    assert (root / "region/r.0.0.mca").read_bytes() == before
    assert Region(root / "region/r.0.0.mca").load().get_block(0, 64, 0).name == "minecraft:air"


def test_timeout_never_kills_or_writes(server_factory):
    child, _, controller, root = server_factory(ignore_stop=True)
    with pytest.warns(LiveSnapshotWarning):
        world = World(root, server=controller).load()
    world.set_block(0, 64, 0, "minecraft:stone")
    with pytest.raises(ServerControlError, match="timed out"):
        world.save(shutdown_timeout=0.5)
    assert child.poll() is None
    assert Region(root / "region/r.0.0.mca").load().get_block(0, 64, 0).name == "minecraft:air"
    assert not (root / ".mca-editor-backups").exists()


def test_bad_auth_leaves_server_and_files_alone(server_factory):
    child, info, _, root = server_factory()
    controller = RconServer(info, password="wrong-password")
    with pytest.warns(LiveSnapshotWarning):
        world = World(root, server=controller).load()
    world.set_block(0, 64, 0, "minecraft:stone")
    with pytest.raises(ServerControlError, match="authentication failed"):
        world.save()
    assert child.poll() is None
    assert Region(root / "region/r.0.0.mca").load().get_block(0, 64, 0).name == "minecraft:air"


def test_wrong_listener_owner_and_pid_reuse_refused(server_factory):
    child, info, _, root = server_factory()
    own_process = psutil.Process()
    wrong = ServerProcess(
        own_process.pid,
        own_process.create_time(),
        info.server_directory,
        root,
        info.properties_path,
        info.rcon_port,
        "wrong process fixture",
    )
    controller = RconServer(wrong, password="fixture-password")
    with pytest.raises(ServerControlError, match="not owned"):
        controller.stop(timeout=1)
    reused = ServerProcess(
        info.pid,
        info.create_time - 1,
        info.server_directory,
        root,
        info.properties_path,
        info.rcon_port,
        "reused PID fixture",
    )
    controller = RconServer(reused, password="fixture-password")
    assert not controller.is_running()
    controller.stop(timeout=1)  # Must not send anything to a reused PID.
    assert child.poll() is None


def test_config_validation_and_no_password_in_repr(server_factory):
    _, info, controller, root = server_factory()
    assert "fixture-password" not in repr(controller)
    with pytest.raises(ValueError, match="loopback"):
        RconServer(info, password="secret", host="192.0.2.1")
    with pytest.raises(ValueError, match="argument sequence"):
        RconServer(info, password="secret", start_command="java -jar server.jar")
    with pytest.raises(ServerControlError, match="already running"):
        controller.restart()
    for timeout in (0, -1, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            controller.stop(timeout)


@pytest.mark.parametrize(
    "payload",
    [
        struct.pack("<i", -1),
        struct.pack("<i", 9),
        struct.pack("<i", 5_000_000),
        struct.pack("<iii", 10, 1, 2) + b"!!",
    ],
)
def test_malformed_rcon_packets(payload):
    import time

    first, second = socket.socketpair()
    with first, second:
        second.sendall(payload)
        connection = _RconConnection(first, time.monotonic() + 1)
        with pytest.raises(ServerControlError, match="Malformed"):
            connection.receive()


def test_missing_restart_executable_refuses_before_shutdown(server_factory):
    child, info, _, root = server_factory()
    controller = RconServer(
        info, password="fixture-password", start_command=["/nonexistent/mca-test-java"]
    )
    with pytest.warns(LiveSnapshotWarning):
        world = World(root, server=controller).load()
    world.set_block(0, 64, 0, "minecraft:stone")
    with pytest.raises(ServerControlError, match="executable"):
        world.save(restart=True)
    assert child.poll() is None
    assert Region(root / "region/r.0.0.mca").load().get_block(0, 64, 0).name == "minecraft:air"
