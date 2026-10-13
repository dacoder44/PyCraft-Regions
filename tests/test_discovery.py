import psutil
import pytest

from mca_editor.discovery import _inspect, discover_servers, read_properties


class Process:
    pid = 123

    def __init__(self, directory, args=None, name="java"):
        self.directory = directory
        self.args = args or ["java", "-jar", "server.jar", "nogui"]
        self.process_name = name

    def name(self):
        return self.process_name

    def cmdline(self):
        return self.args

    def cwd(self):
        return str(self.directory)

    def create_time(self):
        return 123.25


@pytest.fixture
def installation(tmp_path):
    (tmp_path / "world").mkdir()
    (tmp_path / "world" / "level.dat").write_bytes(b"discovery only")
    (tmp_path / "server.properties").write_text(
        "level-name=world\nenable-rcon=true\nrcon.port=25575\nrcon.password=do-not-expose\n"
    )
    return tmp_path


def test_visible_server_is_candidate_not_client(installation, monkeypatch):
    processes = [
        Process(installation),
        Process(installation, ["java", "net.minecraft.client.main.Main", "--gameDir", "foo"]),
        Process(installation, ["java", "-jar", "launcher.jar", "--accessToken", "secret"]),
        Process(installation, name="python"),
    ]
    monkeypatch.setattr(psutil, "process_iter", lambda: iter(processes))
    result = discover_servers()
    assert len(result.servers) == 1
    server = result.servers[0]
    assert server.world_path == installation / "world"
    assert server.pid == 123 and server.create_time == 123.25
    assert server.rcon_port == 25575
    assert "do-not-expose" not in repr(result)
    assert not result.warnings


def test_explicit_launch_overrides_and_escaped_properties(installation):
    universe = installation / "worlds"
    world = universe / "other world"
    world.mkdir(parents=True)
    (world / "level.dat").touch()
    process = Process(
        installation, ["java", "-jar", "server.jar", "--universe", "worlds", "--world=other world"]
    )
    assert _inspect(process).world_path == world
    properties = installation / "different.properties"
    properties.write_text("level-name=other\\ world\nrcon.port: 12345\nenable-rcon true\n")
    process.args = [
        "java",
        "-jar",
        "server.jar",
        "--universe=worlds",
        "--config",
        "different.properties",
    ]
    assert _inspect(process).world_path == world
    assert _inspect(process).rcon_port == 12345


def test_unknown_launch_settings_are_not_guessed(installation):
    server = _inspect(
        Process(installation, ["java", "@user_jvm_args.txt", "@libraries/args.txt", "nogui"])
    )
    assert server.world_path is None and server.warnings
    (installation / "bukkit.yml").write_text("settings:\n  world-container: /somewhere\n")
    server = _inspect(Process(installation))
    assert server.world_path is None and server.warnings


def test_missing_world_and_bad_port(installation):
    (installation / "server.properties").write_text(
        "level-name=missing\nenable-rcon=true\nrcon.port=bad\n"
    )
    server = _inspect(Process(installation))
    assert server.world_path is None
    assert server.rcon_port is None
    assert len(server.warnings) == 2


def test_access_denied_is_reported_without_claiming_no_servers(monkeypatch):
    class Denied:
        pid = 321

        def name(self):
            raise psutil.AccessDenied(self.pid)

    monkeypatch.setattr(psutil, "process_iter", lambda: iter([Denied()]))
    result = discover_servers()
    assert not result.servers
    assert "incomplete" in result.warnings[0]


def test_properties_parser_java_escaping(tmp_path):
    path = tmp_path / "properties"
    path.write_text(
        "# comment\n! comment\nworld=hello\\\n   world\nname\\ with\\ spaces : value\n"
        "unicode=\\u2603\ncolon\\:key=colon\\:value\nslash=hello\\\\\nflag\n"
    )
    assert read_properties(path) == {
        "world": "helloworld",
        "name with spaces": "value",
        "unicode": "☃",
        "colon:key": "colon:value",
        "slash": "hello\\",
        "flag": "",
    }


def test_discovery_cli_never_emits_credentials(installation, monkeypatch, capsys):
    from mca_editor.cli import main

    monkeypatch.setattr(psutil, "process_iter", lambda: iter([Process(installation)]))
    assert main(["discover-servers"]) == 0
    text = capsys.readouterr().out
    assert "do-not-expose" not in text and "world_path" in text
