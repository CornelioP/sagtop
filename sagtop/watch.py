"""Linux inotify through ctypes: lets the engine skip polling when nothing changed.

`Inotify.create()` returns None where inotify is unavailable (macOS, BSD, a full
watch table); the engine then falls back to polling.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import struct
import sys

IN_MODIFY = 0x002
IN_ATTRIB = 0x004
IN_CLOSE_WRITE = 0x008
IN_MOVED_FROM = 0x040
IN_MOVED_TO = 0x080
IN_CREATE = 0x100
IN_DELETE = 0x200
IN_DELETE_SELF = 0x400
IN_MOVE_SELF = 0x800
IN_Q_OVERFLOW = 0x4000
IN_IGNORED = 0x8000
IN_ONLYDIR = 0x01000000
IN_ISDIR = 0x40000000
IN_NONBLOCK = 0o4000
IN_CLOEXEC = 0o2000000

CONTENT = IN_MODIFY | IN_ATTRIB | IN_CLOSE_WRITE
STRUCTURE = IN_CREATE | IN_DELETE | IN_MOVED_FROM | IN_MOVED_TO | IN_DELETE_SELF | IN_MOVE_SELF
MASK = CONTENT | STRUCTURE | IN_ONLYDIR
_HDR = struct.Struct("iIII")


class Inotify:
    def __init__(self, fd: int, libc):
        self.fd = fd
        self._libc = libc
        self.wd_to_dir: dict[int, str] = {}
        self.dir_to_wd: dict[str, int] = {}

    @classmethod
    def create(cls) -> "Inotify | None":
        if not sys.platform.startswith("linux"):
            return None
        try:
            libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
            fd = libc.inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
        except (OSError, AttributeError):
            return None
        return cls(fd, libc) if fd >= 0 else None

    def watch(self, directory: str) -> bool:
        """Adds a watch; False if the directory is gone or the watch table is full."""
        if directory in self.dir_to_wd:
            return True
        wd = self._libc.inotify_add_watch(self.fd, os.fsencode(directory), MASK)
        if wd < 0:
            return False
        self.wd_to_dir[wd] = directory
        self.dir_to_wd[directory] = wd
        return True

    def read(self) -> tuple[list[tuple[str, int]], bool]:
        """Drains pending events: ([(path, mask)], overflowed)."""
        events: list[tuple[str, int]] = []
        overflow = False
        while True:
            try:
                buf = os.read(self.fd, 1 << 16)
            except BlockingIOError:
                break
            if not buf:
                break
            pos = 0
            while pos < len(buf):
                wd, mask, _cookie, length = _HDR.unpack_from(buf, pos)
                pos += _HDR.size
                name = buf[pos : pos + length].rstrip(b"\0")
                pos += length
                if mask & IN_Q_OVERFLOW:
                    overflow = True
                    continue
                directory = self.wd_to_dir.get(wd)
                if directory is None:
                    continue
                if mask & IN_IGNORED:  # watch removed (directory deleted)
                    self.wd_to_dir.pop(wd, None)
                    self.dir_to_wd.pop(directory, None)
                    continue
                path = os.path.join(directory, os.fsdecode(name)) if name else directory
                events.append((path, mask))
        return events, overflow

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass
