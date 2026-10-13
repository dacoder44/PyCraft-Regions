"""Small, scriptable CLI. Editing defaults to a separate output file."""

import argparse
import json
import sys
from pathlib import Path

from .discovery import discover_servers
from .exceptions import MCAError
from .region import Region
from .world import World


def _corner(text: str) -> tuple[int, int, int]:
    try:
        values = tuple(int(v) for v in text.split(","))
        if len(values) != 3:
            raise ValueError
        return values
    except ValueError:
        raise argparse.ArgumentTypeError("Expected X,Y,Z (three integers)") from None


def _property(text: str) -> tuple[str, str]:
    key, separator, value = text.partition("=")
    if not separator or not key or not value:
        raise argparse.ArgumentTypeError("Expected property NAME=VALUE")
    return key, value


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Inspect and safely edit Minecraft Java Anvil regions"
    )
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "discover-servers", help="List visible local dedicated-server candidates (read-only)"
    )
    world_info = commands.add_parser(
        "world-info", help="Index dimension region files without stopping a server"
    )
    world_info.add_argument("directory", type=Path)
    world_info.add_argument(
        "--eager", action="store_true", help="Parse every indexed region, not just file names"
    )
    for name in ("info", "chunks", "validate", "get-block", "set-block", "fill"):
        command = commands.add_parser(name)
        command.add_argument("file", type=Path)
        command.add_argument(
            "--dimension",
            default="minecraft:overworld",
            choices=("minecraft:overworld", "minecraft:the_nether", "minecraft:the_end"),
        )
        command.add_argument(
            "--region",
            nargs=2,
            type=int,
            metavar=("RX", "RZ"),
            help="Explicit region coordinates for unnamed/empty files",
        )
        if name in ("get-block", "set-block"):
            for axis in "xyz":
                command.add_argument(f"--{axis}", required=True, type=int)
        if name == "fill":
            command.add_argument("--from", dest="start", required=True, type=_corner)
            command.add_argument("--to", dest="end", required=True, type=_corner)
        if name in ("set-block", "fill"):
            command.add_argument("--block", required=True)
            command.add_argument(
                "--property", action="append", type=_property, default=[], metavar="NAME=VALUE"
            )
            output = command.add_mutually_exclusive_group()
            output.add_argument("--output", type=Path)
            output.add_argument(
                "--in-place",
                action="store_true",
                help="Replace input atomically and create a .bak backup",
            )
            command.add_argument(
                "--force",
                action="store_true",
                help="Allow replacement of an existing separate output",
            )
            command.add_argument(
                "--preserve-metadata",
                action="store_true",
                help="Retain potentially stale lighting/heightmaps",
            )
            command.add_argument(
                "--remove-block-entities",
                action="store_true",
                help="Remove block entities at changed positions only",
            )
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "discover-servers":
            discovery = discover_servers()
            print(
                json.dumps(
                    {
                        "servers": [
                            {
                                "pid": server.pid,
                                "create_time": server.create_time,
                                "server_directory": str(server.server_directory),
                                "world_path": str(server.world_path) if server.world_path else None,
                                "rcon_port": server.rcon_port,
                                "evidence": server.evidence,
                                "warnings": server.warnings,
                            }
                            for server in discovery.servers
                        ],
                        "warnings": discovery.warnings,
                    },
                    indent=2,
                )
            )
            return 0
        if args.command == "world-info":
            world = World(args.directory).load(eager=args.eager)
            print(
                json.dumps(
                    {
                        "world": str(world.path),
                        "snapshot_only": True,
                        "regions": [
                            {
                                "dimension": f.dimension,
                                "x": f.region_x,
                                "z": f.region_z,
                                "path": str(f.path),
                                "supported_dimension": f.supported_dimension,
                            }
                            for f in world.region_files
                        ],
                    },
                    indent=2,
                )
            )
            return 0
        region = Region(
            args.file,
            region_coords=tuple(args.region) if args.region else None,
            dimension=args.dimension,
            metadata_policy="preserve"
            if getattr(args, "preserve_metadata", False)
            else "invalidate",
            block_entity_policy="remove"
            if getattr(args, "remove_block_entities", False)
            else "error",
        ).load()
        if args.command == "info":
            print(
                json.dumps(
                    {
                        "file": str(args.file),
                        "region": region.region_coords,
                        "chunks": region.chunk_count,
                        "dimension": region.dimension,
                        "versions": sorted({c.version.name for c in region.iter_chunks()}),
                    },
                    indent=2,
                )
            )
        elif args.command == "chunks":
            for chunk in region.iter_chunks():
                slot = chunk.x % 32 + 32 * (chunk.z % 32)
                location = region.locations[slot]
                print(
                    json.dumps(
                        {
                            "x": chunk.x,
                            "z": chunk.z,
                            "DataVersion": chunk.data_version,
                            "status": chunk.status,
                            "sections": [s.y for s in chunk.sections],
                            "offset": location.offset,
                            "sectors": location.sectors,
                            "timestamp": location.timestamp,
                        }
                    )
                )
        elif args.command == "validate":
            print(
                f"Valid: {region.chunk_count} chunks; headers, allocations, compression, NBT and block palettes checked"
            )
        elif args.command == "get-block":
            block = region.get_block(args.x, args.y, args.z)
            print(json.dumps({"name": block.name, "properties": dict(block.properties)}))
        else:
            target = (
                args.file
                if args.in_place
                else (args.output or args.file.with_name(args.file.stem + ".edited.mca"))
            )
            if not args.in_place:
                if target.resolve() == args.file.resolve() or (
                    target.exists() and target.samefile(args.file)
                ):
                    raise ValueError("Use --in-place to replace the input (with backup)")
                if target.exists() and not args.force:
                    raise ValueError(
                        f"Output already exists: {target}; use --force or another --output"
                    )
            properties = dict(args.property)
            if len(properties) != len(args.property):
                raise ValueError("Duplicate --property keys")
            if args.command == "fill":
                count = region.fill(args.start, args.end, args.block, properties)
            else:
                region.set_block(args.x, args.y, args.z, args.block, properties)
                count = 1
            region.save(target, backup=args.in_place)
            print(f"Saved {count} requested block positions to {target}")
        return 0
    except (MCAError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
