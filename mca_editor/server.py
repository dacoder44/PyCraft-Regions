"""Local dedicated-server lifecycle control. Never kill a JVM or touch a client.

RCON is unencrypted; only loopback endpoints owned by the selected process are
accepted. Start commands are explicit trusted configuration, never reconstructed
from discovery (which might include secrets or client launcher arguments).
"""

import ipaddress
import math
import os
import shutil
import socket
import struct
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

import psutil

from .discovery import ServerProcess
from .exceptions import ServerControlError


class ServerController(Protocol):
    """Adapter contract for RCON or an application's service-manager integration.

    A stop implementation MUST wait for its server and writer processes to exit,
    without force-killing them. World additionally requires modern session locks.
    """

    world_path: Path

    def is_running(self) -> bool: ...
    def stop(self, timeout: float = 60.0) -> None: ...
    def validate_restart(self) -> None: ...
    def restart(self) -> int: ...


def _positive_timeout(timeout: float) -> None:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Timeout must be finite and positive")


class _RconConnection:
    def __init__(self, connection: socket.socket, deadline: float):
        self.connection = connection
        self.deadline = deadline

    def _timeout(self) -> None:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ServerControlError("RCON request timed out")
        self.connection.settimeout(remaining)

    def send(self, request_id: int, kind: int, text: str) -> None:
        data = text.encode("utf-8")
        if b"\0" in data or len(data) > 4096:
            raise ServerControlError("Invalid RCON payload")
        self._timeout()
        packet = struct.pack("<ii", request_id, kind) + data + b"\0\0"
        self.connection.sendall(struct.pack("<i", len(packet)) + packet)

    def _read(self, size: int) -> bytes:
        result = bytearray()
        while len(result) < size:
            self._timeout()
            data = self.connection.recv(size - len(result))
            if not data:
                raise ServerControlError("RCON connection closed before authentication completed")
            result.extend(data)
        return bytes(result)

    def receive(self) -> tuple[int, int, bytes]:
        length = struct.unpack("<i", self._read(4))[0]
        if not 10 <= length <= 4 * 1024 * 1024:
            raise ServerControlError("Malformed RCON packet length")
        payload = self._read(length)
        if payload[-2:] != b"\0\0":
            raise ServerControlError("Malformed RCON packet terminator")
        request_id, kind = struct.unpack("<ii", payload[:8])
        return request_id, kind, payload[8:-2]

    def authenticate(self, password: str) -> None:
        self.send(1, 3, password)
        # Source-style servers may precede AUTH_RESPONSE with RESPONSE_VALUE.
        for _ in range(2):
            request_id, kind, _ = self.receive()
            if request_id == -1:
                raise ServerControlError("RCON authentication failed")
            if request_id != 1:
                raise ServerControlError("Unexpected RCON authentication request ID")
            if kind == 2:
                return
            if kind != 0:
                break
        raise ServerControlError("Expected RCON authentication response")


class RconServer:
    def __init__(
        self,
        server: ServerProcess,
        *,
        password: str,
        host: str = "127.0.0.1",
        port: int | None = None,
        world_path: str | Path | None = None,
        start_command: Sequence[str] | None = None,
        log_path: str | Path | None = None,
    ):
        """Bind a discovered PID and world. Explicit world_path confirms overrides.

        Passwords are never included in reprs, logs, or subprocess arguments.
        Commands must launch the server in the foreground (no shell/daemonizer).
        Disable supervisor auto-restart before using this adapter.
        """
        address = ipaddress.ip_address(host)
        if not address.is_loopback:
            raise ValueError("RconServer only accepts literal loopback addresses")
        if not isinstance(password, str) or not password or "\0" in password:
            raise ValueError("A nonempty RCON password is required")
        chosen_world = world_path if world_path is not None else server.world_path
        if chosen_world is None:
            raise ServerControlError(
                "Discovery could not identify the world; confirm world_path explicitly"
            )
        self.world_path = Path(chosen_world).resolve()
        if not (self.world_path / "level.dat").is_file():
            raise ServerControlError("Confirmed world must contain level.dat")
        self.server_directory = server.server_directory.resolve()
        self.host = host
        self.port = port if port is not None else server.rcon_port
        if self.port is None or not 1 <= self.port <= 65535:
            raise ServerControlError("RCON must be enabled; supply a valid local RCON port")
        self._password = password
        self._pid = server.pid
        self._create_time = server.create_time
        if isinstance(start_command, (str, bytes)):
            raise ValueError("start_command must be an argument sequence, not a shell string")
        self._start_command = tuple(start_command) if start_command is not None else None
        self.log_path = (
            Path(log_path) if log_path else self.server_directory / "mca-editor-server.log"
        )
        self._started_process: subprocess.Popen | None = None

    @property
    def pid(self) -> int:
        return self._pid

    def _process(self) -> psutil.Process | None:
        try:
            process = psutil.Process(self._pid)
            if process.create_time() != self._create_time:
                return None  # PID reused; never operate on the new occupant.
            if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                return None
            return process
        except psutil.NoSuchProcess:
            return None
        except psutil.AccessDenied as exc:
            raise ServerControlError("Cannot verify selected server process identity") from exc

    def is_running(self) -> bool:
        return self._process() is not None

    def _verify_listener(self, process: psutil.Process) -> None:
        try:
            host = ipaddress.ip_address(self.host)
            for connection in process.net_connections(kind="tcp"):
                if connection.status != psutil.CONN_LISTEN or not connection.laddr:
                    continue
                address = ipaddress.ip_address(connection.laddr.ip)
                if connection.laddr.port == self.port and (
                    address == host or (address.is_unspecified and address.version == host.version)
                ):
                    return
        except (psutil.Error, OSError) as exc:
            raise ServerControlError(
                "Cannot verify RCON listener ownership; refusing shutdown"
            ) from exc
        raise ServerControlError(
            "RCON port is not owned by the selected server PID; refusing shutdown"
        )

    def stop(self, timeout: float = 60.0) -> None:
        _positive_timeout(timeout)
        process = self._process()
        if process is None:
            return
        deadline = time.monotonic() + timeout
        try:
            children = process.children(recursive=True)
            self._verify_listener(process)
            with socket.create_connection((self.host, self.port), timeout=min(timeout, 10)) as sock:
                client = _RconConnection(sock, deadline)
                client.authenticate(self._password)
                # Recheck after auth, before sending any state-changing command.
                current = self._process()
                if current is None:
                    raise ServerControlError("Server exited during RCON authentication")
                self._verify_listener(current)
                client.send(2, 2, "stop")
                # A stop may close RCON without a reply; process exit, not its
                # response text, is our completion condition.
            process.wait(timeout=max(0, deadline - time.monotonic()))
            _, alive = psutil.wait_procs(children, timeout=max(0, deadline - time.monotonic()))
            if any(p.is_running() and p.status() != psutil.STATUS_ZOMBIE for p in alive):
                raise ServerControlError("Server descendants remain alive; refusing disk writes")
            if self.is_running():
                raise ServerControlError("Server is still running after shutdown request")
        except ServerControlError:
            raise
        except (OSError, psutil.Error) as exc:
            raise ServerControlError(
                "Graceful server shutdown failed or timed out; no force-kill was attempted"
            ) from exc

    def validate_restart(self) -> None:
        if not self._start_command or any(
            not isinstance(arg, str) or not arg or "\0" in arg for arg in self._start_command
        ):
            raise ServerControlError(
                "Restart requires an explicit nonempty start_command argument list"
            )
        if not self.server_directory.is_dir():
            raise ServerControlError("Server working directory does not exist")
        executable = self._start_command[0]
        if not Path(executable).is_absolute() and ("/" in executable or "\\" in executable):
            executable = str(self.server_directory / executable)
        if shutil.which(executable) is None:
            raise ServerControlError(
                "Configured server executable was not found or is not executable"
            )

    def restart(self) -> int:
        """Launch the configured server, not a client. Returns PID, not readiness.

        stdout/stderr append to log_path; no unread pipe can stall the server.
        The caller must only restart after releasing world locks and committing.
        """
        self.validate_restart()
        if self.is_running():
            raise ServerControlError("Selected server is already running")
        try:
            options = (
                {"start_new_session": True}
                if os.name != "nt"
                else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            )
            with self.log_path.open("ab") as log:
                process = subprocess.Popen(
                    self._start_command,
                    cwd=self.server_directory,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    shell=False,
                    **options,
                )
            # Retain the child handle so a subsequent stop/restart can reap it.
            self._started_process = process
            identity = psutil.Process(process.pid)
            self._pid, self._create_time = process.pid, identity.create_time()
            if process.poll() is not None:
                raise ServerControlError("Server launch exited immediately; inspect its log")
            return process.pid
        except (OSError, psutil.Error) as exc:
            raise ServerControlError(
                "Server could not be launched; inspect the configured command/log"
            ) from exc
