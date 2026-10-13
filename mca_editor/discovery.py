"""Best-effort local dedicated-server discovery, without executing processes.

Process visibility is OS/permission dependent. Results are candidates inferred
from Java command lines and server.properties, not proof a world has finished
loading. Client and integrated single-player JVMs are deliberately excluded.
"""

import re
from dataclasses import dataclass
from pathlib import Path

import psutil

from .exceptions import DiscoveryError


@dataclass(frozen=True)
class ServerProcess:
    pid: int
    create_time: float
    server_directory: Path
    world_path: Path | None
    properties_path: Path
    rcon_port: int | None
    evidence: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class DiscoveryResult:
    servers: tuple[ServerProcess, ...]
    warnings: tuple[str, ...]


def _unescape(text: str) -> str:
    def replace(match: re.Match) -> str:
        value = match.group(1)
        if value.startswith("u"):
            return chr(int(value[1:], 16))
        return {"t": "\t", "r": "\r", "n": "\n", "f": "\f"}.get(value, value)

    return re.sub(r"\\(u[0-9a-fA-F]{4}|.)", replace, text)


def read_properties(path: Path) -> dict[str, str]:
    """Read Java properties, including escaped delimiters and continuations.

    The returned dictionary may include credentials; never log it. Discovery
    exposes only the world path and RCON port, never the password.
    """
    raw = path.read_bytes()
    if len(raw) > 1024 * 1024:
        raise DiscoveryError("server.properties exceeds the 1 MiB safety limit")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    logical = []
    pending = ""
    for line in text.splitlines():
        line = line.lstrip()
        if not pending and (not line or line.startswith(("#", "!"))):
            continue
        line = pending + line
        slash_count = len(line) - len(line.rstrip("\\"))
        if slash_count % 2:
            pending = line[:-1]
        else:
            logical.append(line)
            pending = ""
    if pending:
        logical.append(pending)
    result = {}
    for line in logical:
        separator = re.search(r"(?<!\\)(?:\\\\)*([\s=:])", line)
        if separator is None:
            key, value = line, ""
        else:
            index = separator.end(1) - 1
            key, value = line[:index], line[index:]
            value = value.lstrip()
            if value.startswith(("=", ":")):
                value = value[1:]
            value = value.lstrip()
        result[_unescape(key)] = _unescape(value)
    return result


def _option(args: list[str], names: tuple[str, ...]) -> str | None:
    for index, arg in enumerate(args):
        if arg in names:
            if index + 1 == len(args):
                raise DiscoveryError(f"Missing value for server option {arg}")
            return args[index + 1]
        for name in names:
            if arg.startswith(name + "="):
                return arg[len(name) + 1 :]
    return None


def _path(base: Path, value: str) -> Path:
    path = Path(value)
    return (path if path.is_absolute() else base / path).resolve()


def _inspect(process: psutil.Process) -> ServerProcess | None:
    name = process.name().lower()
    if name not in ("java", "java.exe", "javaw", "javaw.exe"):
        return None
    args = process.cmdline()
    if not args:
        return None
    joined = " ".join(args).lower()
    if any(
        marker in joined
        for marker in (
            "net.minecraft.client",
            "knotclient",
            "--accesstoken",
            "--gamedir",
            "--username",
            "--launchTarget forgeclient".lower(),
        )
    ):
        return None
    directory = Path(process.cwd()).resolve()
    # Java argfiles can conceal configuration overrides. Do not infer a world
    # from them. The user can bind an explicit world after inspecting the setup.
    hidden_arguments = any(arg.startswith("@") for arg in args[1:])
    config = _option(args, ("--config", "-c")) or "server.properties"
    properties_path = _path(directory, config)
    if not properties_path.is_file():
        return None
    server_hint = any(
        marker in joined
        for marker in (
            "net.minecraft.server",
            "knotserver",
            "forgeserver",
            "neoforgeserver",
            "org.bukkit.craftbukkit",
            "paperclip",
            "nogui",
        )
    )
    if not (server_hint or "-jar" in args or hidden_arguments):
        return None
    properties = read_properties(properties_path)
    warnings = []
    world_path = None
    if hidden_arguments:
        warnings.append(
            "Java @argfile hides launch settings; world path needs explicit confirmation"
        )
    else:
        universe = _path(directory, _option(args, ("--universe", "--world-dir", "-W")) or ".")
        world_name = _option(args, ("--world", "-w")) or properties.get("level-name", "world")
        world_path = _path(universe, world_name)
        # Bukkit can redirect the world container in YAML. Do not guess at YAML.
        bukkit_config = _path(directory, _option(args, ("--bukkit-settings", "-b")) or "bukkit.yml")
        if bukkit_config.exists() and re.search(
            r"^\s*world-container\s*:", bukkit_config.read_text(errors="replace"), re.MULTILINE
        ):
            warnings.append("Bukkit world-container setting needs explicit world-path confirmation")
            world_path = None
    if world_path is not None and not (world_path / "level.dat").is_file():
        warnings.append(
            "Inferred world has no level.dat yet; may not be loaded or may use custom settings"
        )
        world_path = None
    port = None
    if properties.get("enable-rcon", "false").lower() == "true":
        try:
            port = int(properties.get("rcon.port", "25575"))
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            warnings.append("Invalid configured RCON port")
            port = None
    return ServerProcess(
        process.pid,
        process.create_time(),
        directory,
        world_path,
        properties_path,
        port,
        "Running Java process with dedicated-server launch hints and server.properties",
        tuple(warnings),
    )


def discover_servers() -> DiscoveryResult:
    """Find visible LOCAL dedicated servers; never stop or contact any server.

    No full-disk scan, remote-server scan, or client termination is performed.
    Warnings expose incomplete process visibility instead of claiming none run.
    """
    servers = []
    warnings = []
    for process in psutil.process_iter():
        try:
            server = _inspect(process)
            if server is not None:
                servers.append(server)
        except psutil.NoSuchProcess:
            continue
        except (psutil.AccessDenied, OSError, DiscoveryError, ValueError):
            warnings.append(f"Could not inspect process {process.pid}; discovery may be incomplete")
    return DiscoveryResult(tuple(sorted(servers, key=lambda s: s.pid)), tuple(warnings))
