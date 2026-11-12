"""Incremental JSONL reader that survives partial writes, truncation and rotation."""

from __future__ import annotations

import json
import os

CHUNK = 1 << 20
HEAD = 400  # bytes inspected by skip/quick


class Tail:
    """Reads only the bytes appended since the last call.

    `read()` returns (records, reset). `reset` is True when the file shrank or was
    replaced, meaning the caller must discard state derived from earlier records.
    """

    def __init__(self, path: str, skip: tuple[bytes, ...] = (), quick=None):
        self.path = path
        self.skip = skip  # line prefixes not worth JSON-decoding
        self.quick = quick  # bytes -> stand-in record, or None to decode normally
        self.offset = 0
        self.inode: int | None = None
        self.buf = b""
        self.bad_lines = 0

    def _decode(self, data: bytes, start: int, end: int):
        # look at the head first so skipped lines are never copied whole
        head = data[start : min(end, start + HEAD)]
        if self.skip and head.startswith(self.skip):
            return None
        if self.quick is not None:
            stand_in = self.quick(head)
            if stand_in is not None:
                return stand_in
        try:
            rec = json.loads(data[start:end])
        except ValueError:
            if data[start:end].strip():
                self.bad_lines += 1
            return None
        return rec if isinstance(rec, dict) else None

    def batches(self):
        """Yields (records, reset) per chunk of new bytes, so a large backlog is
        folded batch by batch instead of materialised at once. `reset` is True
        only on the first batch, when the file shrank or was replaced."""
        st = os.stat(self.path)
        reset = False
        if self.inode is not None and (st.st_ino != self.inode or st.st_size < self.offset):
            self.offset, self.buf, self.bad_lines, reset = 0, b"", 0, True
        self.inode = st.st_ino
        if st.st_size == self.offset:
            if reset:
                yield [], True
            return
        with open(self.path, "rb") as fh:
            fh.seek(self.offset)
            while True:
                chunk = fh.read(CHUNK)
                if not chunk:
                    break
                self.offset += len(chunk)
                data = self.buf + chunk if self.buf else chunk
                records: list[dict] = []
                pos = 0
                while True:
                    nl = data.find(b"\n", pos)
                    if nl < 0:
                        break
                    if nl > pos:
                        rec = self._decode(data, pos, nl)
                        if rec is not None:
                            records.append(rec)
                    pos = nl + 1
                self.buf = data[pos:]  # incomplete tail (or b"")
                yield records, reset
                reset = False
        if reset:  # file emptied: still report it
            yield [], True

    def read(self) -> tuple[list[dict], bool]:
        records: list[dict] = []
        reset = False
        for batch, r in self.batches():
            records.extend(batch)
            reset = reset or r
        return records, reset
