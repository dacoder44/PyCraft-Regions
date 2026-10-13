"""Synthetic server for lifecycle tests, NOT a Minecraft implementation.

Holds a real OS world lock, accepts real RCON frames, writes a final block on
'stop', and exits. It never touches a user world; tests create all input files.
"""

import argparse
import json
import socket
import struct
from pathlib import Path

from mca_editor import Region
from mca_editor.world_lock import session_lock


def read(connection, size):
    output = b""
    while len(output) < size:
        part = connection.recv(size - len(output))
        if not part:
            raise EOFError
        output += part
    return output


def receive(connection):
    length = struct.unpack("<i", read(connection, 4))[0]
    if not 10 <= length <= 8192:
        raise ValueError("bad request")
    packet = read(connection, length)
    request_id, kind = struct.unpack("<ii", packet[:8])
    return request_id, kind, packet[8:-2].decode()


def send(connection, request_id, kind, body=""):
    payload = struct.pack("<ii", request_id, kind) + body.encode() + b"\0\0"
    data = struct.pack("<i", len(payload)) + payload
    # Deliberately fragmented TCP response: client cannot assume recv fills a frame.
    for index in range(0, len(data), 3):
        connection.sendall(data[index : index + 3])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("world", type=Path)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--ignore-stop", action="store_true")
    args = parser.parse_args()
    with session_lock(args.world), socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", args.port))
        listener.listen()
        print(json.dumps({"port": listener.getsockname()[1]}), flush=True)
        stopping = False
        while not stopping:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(10)
                try:
                    request_id, kind, text = receive(connection)
                    assert kind == 3
                    if text != "fixture-password":
                        send(connection, -1, 2)
                        continue
                    send(connection, request_id, 0)  # Optional Source-style auth prelude.
                    send(connection, request_id, 2)
                    request_id, kind, text = receive(connection)
                    assert kind == 2 and text == "stop"
                    if args.ignore_stop:
                        continue
                    region = Region(args.world / "region/r.0.0.mca").load()
                    region.set_block(1, 64, 0, "minecraft:gold_block")
                    region.save()
                    stopping = True
                    # No reply: shutdown can close the RCON socket immediately.
                except (EOFError, ConnectionError):
                    pass


if __name__ == "__main__":
    main()
