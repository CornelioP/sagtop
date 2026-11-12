"""Watches session directories, folds new records into nodes, publishes diffs.

On Linux the engine is driven by inotify: a tick reads the change events and
touches only the files they name, and the loop sleeps until the next event or
the next time-based status change. A full stat sweep still runs every
FULL_RESCAN_S (and after an event-queue overflow) as a safety net. Elsewhere,
or if inotify is unavailable, every tick is a full sweep (polling).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import multiprocessing
import os
import select
import sys
import threading
import time
import traceback
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass

from . import claude, codex
from .model import Node
from .tail import Tail
from .watch import IN_ISDIR, STRUCTURE, Inotify

WINDOWS = {"1h": 3600, "24h": 86400, "7d": 7 * 86400, "all": float("inf")}
EVENT_BUFFER = 20000
FULL_RESCAN_S = 60.0  # safety sweep when event-driven
DEBOUNCE_S = 0.2  # batch bursts of writes into one tick
TRIM_AFTER_BYTES = 32 << 20  # hand freed heap back to the OS after big parses
PARALLEL_MIN_BYTES = 1 << 30  # below ~1 GB the serial parse is cheaper overall and fast enough
PARALLEL_MAX_WORKERS = 4  # 8 shaves ~0.6 s more off a full load but costs ~25% more CPU
_TRACKED_SUFFIXES = (".jsonl", ".json")
INF = float("inf")


@dataclass
class Tracked:
    node: Node
    parser: object
    tail: Tail
    meta_path: str | None = None
    sig: tuple = ()  # (mtime_ns, size, ino) last seen by stat
    parsed_sig: tuple = ()  # sig at the last parse
    meta_mtime: float = 0.0  # meta file mtime at the last apply
    mtime: float = -1.0  # max(transcript, meta) mtime; -1 = never stat'ed / missing


class Engine:
    def __init__(self, claude_root: str, codex_root: str, window: str = "24h", interval: float = 1.0, watch: bool = True,
                 parallel: bool = True):
        self.claude_root = claude_root
        self.codex_root = codex_root
        self.window = window
        self.interval = interval
        self.files: dict[str, Tracked] = {}
        self.by_path: dict[str, str] = {}  # transcript / meta / record path -> node id
        self.codex_names: dict[str, str] = {}
        self._names_path = os.path.join(codex_root, "session_index.jsonl")
        self._names_mtime = 0.0
        self.lock = threading.Lock()  # guards node mutation vs. detail reads, and published state
        self.cond = threading.Condition(self.lock)
        self.seq = 0
        self.events: deque[tuple[int, dict]] = deque(maxlen=EVENT_BUFFER)
        self.published: dict[str, dict] = {}
        self.included: set[str] = set()
        self.due: dict[str, float] = {}  # node id -> next time-based status change
        self.ready = False
        self.last_tick_ms = 0.0
        self.errors = 0
        self.watcher = Inotify.create() if watch else None
        self.parallel = parallel
        self._need_discover = True
        self._need_full = True
        self._last_full = 0.0
        self._parsed_bytes = 0
        self._stop = threading.Event()

    # ---------------------------------------------------------------- discovery
    def _discover(self) -> set[str]:
        """Re-lists the trees; returns ids of new or replaced nodes."""
        dirs: set[str] = set()
        found: dict[str, tuple] = {}
        for nid, kind, path, parent, meta in claude.discover(self.claude_root, dirs):
            found[nid] = ("claude", kind, path, parent, meta)
        for nid, path in codex.discover(self.codex_root, dirs):
            prev = found.get(nid)
            # an archived copy and a live copy can coexist; prefer the newer file
            if prev is None or _mtime(path) > _mtime(prev[2]):
                found[nid] = ("codex", "session", path, None, None)
        fresh_dirs: list[str] = []
        if self.watcher is not None:
            fresh_dirs = [d for d in dirs if d not in self.watcher.dir_to_wd]
            for d in fresh_dirs:
                if not self.watcher.watch(d):  # watch table full: fall back to polling
                    print(f"sagtop: cannot watch {d}; falling back to polling", file=sys.stderr)
                    self.watcher.close()
                    self.watcher = None
                    break
        added: set[str] = set()
        if self.watcher is not None and fresh_dirs:
            # files may have landed before the watch existed: recheck what lives there
            prefixes = tuple(d.rstrip("/") + "/" for d in fresh_dirs)
            added |= {nid for path, nid in self.by_path.items() if path.startswith(prefixes)}
        for nid in list(self.files):
            if nid not in found:
                self._forget(nid)
        for nid, spec in found.items():
            tr = self.files.get(nid)
            if tr is None or tr.node.path != spec[2]:
                if tr is not None:
                    self._forget(nid)
                self.files[nid] = tr = self._track(nid, spec)
                self.by_path[tr.node.path] = nid
                if tr.meta_path:
                    self.by_path[tr.meta_path] = nid
                added.add(nid)
        return added

    def _forget(self, nid: str) -> None:
        tr = self.files.pop(nid)
        self.by_path.pop(tr.node.path, None)
        if tr.meta_path:
            self.by_path.pop(tr.meta_path, None)

    def _track(self, nid: str, spec: tuple) -> Tracked:
        tool, kind, path, parent, meta = spec
        node = Node(id=nid, tool=tool, kind=kind, parent=parent, edge="spawned" if parent else None, path=path)
        if kind == "workflow":
            parser = claude.WorkflowParser(node)
        elif tool == "claude":
            parser = claude.ClaudeParser(node)
        else:
            parser = codex.CodexParser(node, self.codex_names)
        return Tracked(node=node, parser=parser, tail=Tail(path, parser.skip, parser.quick), meta_path=meta)

    # -------------------------------------------------------------------- stat
    def _stat(self, tr: Tracked) -> None:
        try:
            st = os.stat(tr.node.path)
        except OSError:
            tr.sig, tr.mtime = (), -1.0
            return
        tr.sig = (st.st_mtime_ns, st.st_size, st.st_ino)
        tr.mtime = max(st.st_mtime, _mtime(tr.meta_path))
        tr.node.last = tr.mtime

    # ------------------------------------------------------------------- parse
    def _update(self, tr: Tracked) -> None:
        start = tr.tail.offset
        previous_failure = tr.node.failure
        _parse_tracked(tr, self.lock)
        self._parsed_bytes += max(0, tr.tail.offset - start)
        self._note_failure(tr, previous_failure)

    def _note_failure(self, tr: Tracked, previous: str) -> None:
        if tr.node.failure and tr.node.failure != previous:  # log once per distinct failure
            self.errors += 1
            print(f"sagtop: {tr.node.path}: {tr.node.failure}", file=sys.stderr)

    def _parse_cold_in_parallel(self, nids: set[str]) -> None:
        """First parse of a large backlog: spread whole files over worker processes.

        Workers run the same _parse_tracked on pickled copies; results replace the
        originals. Any pool failure falls back to the serial path for what is left.
        """
        cold = [self.files[n] for n in nids if self.files[n].parsed_sig != self.files[n].sig and self.files[n].tail.offset == 0]
        total = sum(tr.sig[1] for tr in cold if tr.sig)
        workers = min(PARALLEL_MAX_WORKERS, (os.cpu_count() or 1) - 1, len(cold))
        if total < PARALLEL_MIN_BYTES or workers < 2:
            return
        buckets: list[list[Tracked]] = [[] for _ in range(workers)]
        loads = [0] * workers
        for tr in sorted(cold, key=lambda t: -t.sig[1]):  # biggest first onto the lightest bucket
            i = loads.index(min(loads))
            buckets[i].append(tr)
            loads[i] += tr.sig[1]
        try:
            ctx = multiprocessing.get_context("forkserver" if "forkserver" in multiprocessing.get_all_start_methods() else "spawn")
            with ProcessPoolExecutor(workers, mp_context=ctx) as pool:
                for done in pool.map(_parse_bucket, buckets):
                    with self.lock:
                        for tr in done:
                            if isinstance(tr.parser, codex.CodexParser):
                                tr.parser.names = self.codex_names  # re-share the live name index
                                tr.parser.refresh_title()
                            self.files[tr.node.id] = tr
                    for tr in done:
                        self._note_failure(tr, "")
            self._parsed_bytes += total
        except Exception as exc:  # e.g. no /dev/shm or process limit: the serial path still works
            print(f"sagtop: parallel parse unavailable ({type(exc).__name__}: {exc}); parsing serially", file=sys.stderr)

    def _refresh_names(self) -> set[str]:
        m = _mtime(self._names_path)
        if not m or m == self._names_mtime:
            return set()
        codex.load_names(self._names_path, self.codex_names)
        self._names_mtime = m
        touched = set()
        with self.lock:
            for nid, tr in self.files.items():
                if isinstance(tr.parser, codex.CodexParser) and tr.parsed_sig:
                    tr.parser.refresh_title()
                    touched.add(nid)
        return touched

    # -------------------------------------------------------------------- tick
    def tick(self) -> None:
        t0 = time.perf_counter()
        now = time.time()
        touched: set[str] = set()

        if self.watcher is not None:
            events, overflow = self.watcher.read()
            if overflow:
                self._need_full = self._need_discover = True
            for path, mask in events:
                nid = self.by_path.get(path)
                if nid is not None:
                    touched.add(nid)
                if mask & STRUCTURE and (
                    nid is not None or mask & IN_ISDIR or path.endswith(_TRACKED_SUFFIXES) or path in self.watcher.dir_to_wd
                ):
                    self._need_discover = True  # a tracked file or directory appeared, vanished or moved

        full = self.watcher is None or self._need_full or now - self._last_full >= FULL_RESCAN_S
        if self._need_discover or full:
            touched |= self._discover()
            self._need_discover = False
        if full:
            touched |= self.files.keys()
            self._last_full, self._need_full = now, False
        touched |= self._refresh_names()

        for nid in touched:
            tr = self.files.get(nid)
            if tr is not None:
                self._stat(tr)

        horizon = now - WINDOWS.get(self.window, WINDOWS["24h"])
        included = {nid for nid, tr in self.files.items() if tr.mtime >= 0 and tr.mtime >= horizon}
        if self.parallel:
            self._parse_cold_in_parallel(included)
        for nid in included:
            self._refresh(nid, touched)
        frontier = list(included)  # pull in ancestors (known only after parsing) so children keep their parent
        while frontier:
            parent = self.files[frontier.pop()].node.parent
            if parent and parent not in included and parent in self.files and self.files[parent].mtime >= 0:
                included.add(parent)
                self._refresh(parent, touched)
                frontier.append(parent)

        self._publish(included, touched, now)
        if self._parsed_bytes >= TRIM_AFTER_BYTES:
            _malloc_trim()
            self._parsed_bytes = 0
        self.last_tick_ms = (time.perf_counter() - t0) * 1000
        self.ready = True

    def _refresh(self, nid: str, touched: set[str]) -> None:
        tr = self.files[nid]
        if tr.parsed_sig != tr.sig or (nid in touched and tr.meta_path and _mtime(tr.meta_path) != tr.meta_mtime):
            self._update(tr)

    def _publish(self, included: set[str], touched: set[str], now: float) -> None:
        entered, left = included - self.included, self.included - included
        dirty = (touched & included) | entered | {nid for nid, t in self.due.items() if t <= now}
        if entered or left:  # a child's drawn parent depends on whether the parent is shown
            moved = entered | left
            dirty |= {nid for nid in included if self.files[nid].node.parent in moved}
        with self.cond:
            changed = False
            for nid in left:
                self.published.pop(nid, None)
                self.due.pop(nid, None)
                self._emit({"op": "remove", "id": nid})
                changed = True
            for nid in dirty & included:
                node = self.files[nid].node
                s = node.summary(now)
                if s["parent"] not in included:
                    s["parent"] = None  # parent file missing: draw as a root
                self.due[nid] = node.next_status_change(now)
                if self.published.get(nid) != s:
                    self.published[nid] = s
                    self._emit({"op": "upsert", "node": s})
                    changed = True
            self.included = included
            if changed:
                self.cond.notify_all()

    def _emit(self, ev: dict) -> None:
        self.seq += 1
        ev["seq"] = self.seq
        self.events.append((self.seq, ev))

    def _next_wake(self, now: float) -> float:
        """Seconds until something can change without a file event."""
        wake = self._last_full + FULL_RESCAN_S
        wake = min(wake, min(self.due.values(), default=INF))
        span = WINDOWS.get(self.window, INF)
        if span != INF and self.included:  # the oldest shown node leaves the window
            wake = min(wake, min(self.files[n].mtime for n in self.included if n in self.files) + span)
        return max(0.05, min(wake - now, FULL_RESCAN_S))

    # ---------------------------------------------------------------- readers
    def snapshot(self) -> dict:
        with self.lock:
            return {
                "seq": self.seq,
                "window": self.window,
                "ready": self.ready,
                "tick_ms": round(self.last_tick_ms, 1),
                "claude_dir": self.claude_root,
                "codex_dir": self.codex_root,
                "home": os.path.expanduser("~"),
                "nodes": list(self.published.values()),
            }

    def detail(self, nid: str) -> dict | None:
        with self.lock:
            tr = self.files.get(nid)
            return tr.node.detail() if tr else None

    def events_since(self, since: int, timeout: float) -> tuple[list[dict] | None, int]:
        """Blocks up to `timeout` for events newer than `since`. None means the
        client fell out of the ring buffer and must reload the snapshot."""
        with self.cond:
            if self.seq <= since:
                self.cond.wait(timeout)
            if since and self.events and since < self.events[0][0] - 1:
                return None, self.seq
            return [ev for s, ev in self.events if s > since], self.seq

    def set_window(self, window: str) -> bool:
        if window not in WINDOWS:
            return False
        self.window = window
        self._wake()
        return True

    # ------------------------------------------------------------------- loop
    def run(self) -> None:
        self._wake_r, self._wake_w = os.pipe()
        try:
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception:
                    self.errors += 1
                    traceback.print_exc()
                if self.watcher is None:
                    self._sleep(self.interval)
                    continue
                if self._sleep(self._next_wake(time.time()), self.watcher.fd):
                    self._stop.wait(DEBOUNCE_S)  # let a burst of writes land together
        finally:
            os.close(self._wake_r)
            os.close(self._wake_w)

    def _sleep(self, timeout: float, fd: int | None = None) -> bool:
        """Waits for the timeout, a file event (True) or a wake-up (stop / window change)."""
        fds = [self._wake_r] + ([fd] if fd is not None else [])
        ready, _, _ = select.select(fds, [], [], timeout)
        if self._wake_r in ready:
            os.read(self._wake_r, 64)
        return fd is not None and fd in ready

    def _wake(self) -> None:
        w = getattr(self, "_wake_w", None)
        if w is not None:
            try:
                os.write(w, b"x")
            except OSError:
                pass

    def start(self) -> threading.Thread:
        th = threading.Thread(target=self.run, name="sagtop-engine", daemon=True)
        th.start()
        return th

    def stop(self) -> None:
        self._stop.set()
        self._wake()


def _parse_tracked(tr: Tracked, lock=None) -> None:
    """Folds new bytes (and a changed meta file) into tr. Never raises: a failure
    is recorded on the node and retried only when the file changes again."""
    guard = lock if lock is not None else nullcontext()
    try:
        if tr.meta_path:
            mm = _mtime(tr.meta_path)
            if mm and mm != tr.meta_mtime:
                with guard:
                    tr.parser.apply_meta(tr.meta_path)
                tr.meta_mtime = mm
        if tr.sig != tr.parsed_sig:
            for records, reset in tr.tail.batches():
                with guard:  # short holds: readers interleave with big parses
                    if reset:
                        tr.parser.reset()
                    tr.parser.feed(records)
            with guard:
                tr.node.bad_lines = tr.tail.bad_lines
                tr.node.failure = ""
            tr.parsed_sig = tr.sig
        if not tr.node.started:
            tr.node.started = tr.mtime  # no timestamp in the data: first seen
    except Exception as exc:  # one bad file must never stop the loop
        tr.node.failure = f"{type(exc).__name__}: {exc}"
        tr.parsed_sig = tr.sig


def _parse_bucket(bucket: list[Tracked]) -> list[Tracked]:
    for tr in bucket:
        _parse_tracked(tr)
    _malloc_trim()
    return bucket


def _mtime(path: str | None) -> float:
    if not path:
        return 0.0
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0.0


def _malloc_trim() -> None:
    try:
        ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass  # not glibc
