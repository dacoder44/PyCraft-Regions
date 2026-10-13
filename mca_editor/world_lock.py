"""Hold Minecraft's modern session.lock using OS byte-range locks.

Do not use flock: Java FileChannel locks are POSIX record locks, not flock.
Legacy timestamp locks are refused. Locks are advisory on POSIX; all writers
must cooperate. Windows locks the first byte, overlapping Java's whole-file lock.
"""

import os
import threading
from contextlib import contextmanager
from pathlib import Path

from .exceptions import WorldBusyError

_GUARD = threading.Lock()
_HELD: set[Path] = set()
MODERN_LOCK_MARKER = "☃".encode()


@contextmanager
def session_lock(world_directory: Path):
    path = (world_directory / "session.lock").resolve()
    with _GUARD:
        if path in _HELD:
            raise WorldBusyError(f"World is already locked by this editor: {world_directory}")
        _HELD.add(path)
    stream = None
    try:
        try:
            # Never invent a session.lock for an unidentified or legacy world.
            stream = path.open("r+b", buffering=0)
            if stream.read(3) != MODERN_LOCK_MARKER:
                raise WorldBusyError(
                    "Legacy/unrecognized session.lock; managed writes require modern Java world locks"
                )
            stream.seek(0)
            if os.name == "posix":
                import fcntl

                fcntl.lockf(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                raise WorldBusyError("Unsupported OS session-lock implementation")
            identity = os.fstat(stream.fileno())
            current = path.stat()
            if (identity.st_dev, identity.st_ino) != (current.st_dev, current.st_ino):
                raise WorldBusyError("session.lock was replaced during acquisition")
        except OSError as exc:
            raise WorldBusyError(
                f"Cannot exclusively lock {world_directory}; server may still be running"
            ) from exc
        # Do not mislabel I/O errors inside the caller's transaction as lock errors.
        yield
    finally:
        try:
            if stream is not None:
                # Closing releases byte-range locks on both POSIX and Windows.
                stream.close()
        finally:
            with _GUARD:
                _HELD.discard(path)
