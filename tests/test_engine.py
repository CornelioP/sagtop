import http.client
import json
import os
import socket
import threading
import time
import unittest

from sagtop.engine import Engine
from sagtop.model import Node
from sagtop.server import serve

from .helpers import Tree, c_assistant, c_user, write_jsonl, x_meta

DAY = 86400


def age(path, seconds):
    t = time.time() - seconds
    os.utime(path, (t, t))


class StatusTest(unittest.TestCase):
    def test_status_rules(self):
        now = 10_000.0
        n = Node(id="x", tool="claude", last=now - 5)
        self.assertEqual(n.status(now), "running")
        n.last = now - 60
        self.assertEqual(n.status(now), "idle")
        n.in_turn = True
        self.assertEqual(n.status(now), "running")
        n.last = now - 600
        self.assertEqual(n.status(now), "idle")  # stale mid-turn: crashed or waiting
        n.last = now - 3 * 3600
        self.assertEqual(n.status(now), "done")
        n.last, n.finished = now - 30, True
        self.assertEqual(n.status(now), "done")
        n.errored = True
        self.assertEqual(n.status(now), "error")

    def test_next_status_change(self):
        n = Node(id="x", tool="claude", last=1000.0)
        self.assertEqual(n.next_status_change(1001), 1010)
        self.assertEqual(n.next_status_change(1010), 1000 + 1800)
        n.in_turn = True
        self.assertEqual(n.next_status_change(1011), 1120)
        n.finished = True
        self.assertEqual(n.next_status_change(1011), float("inf"))
        n.finished, n.errored = False, True
        self.assertEqual(n.next_status_change(1001), float("inf"))


class EngineTest(unittest.TestCase):
    watch = True

    def setUp(self):
        self.t = Tree()
        self.e = Engine(self.t.claude, self.t.codex, window="24h", watch=self.watch)

    def tearDown(self):
        self.t.cleanup()

    def test_window_and_ancestors(self):
        old, new = self.t.session("old"), self.t.session("new")
        write_jsonl(old, [c_user("old session")])
        write_jsonl(new, [c_user("new session")])
        age(old, 3 * DAY)
        # a fresh codex subagent whose parent rollout is a week old
        parent_tid, child_tid = "01a10000-0000-7000-8000-00000000000a", "01a10000-0000-7000-8000-00000000000b"
        write_jsonl(self.t.rollout(parent_tid), [x_meta(parent_tid)])
        age(self.t.rollout(parent_tid), 7 * DAY)
        write_jsonl(self.t.rollout(child_tid), [x_meta(child_tid, thread_source="subagent", parent_thread_id=parent_tid)])
        self.e.tick()
        ids = set(self.e.published)
        self.assertIn("claude:new", ids)
        self.assertNotIn("claude:old", ids)
        self.assertIn(f"codex:{parent_tid}", ids, "ancestor pulled in despite being outside the window")
        self.assertEqual(self.e.published[f"codex:{child_tid}"]["parent"], f"codex:{parent_tid}")

        self.e.set_window("7d")
        self.e.tick()
        self.assertIn("claude:old", self.e.published)
        self.assertFalse(self.e.set_window("bogus"))

    def test_diffs_and_buffer_reset(self):
        path = self.t.session("s")
        u = c_user("hello")
        write_jsonl(path, [u])
        self.e.tick()
        seq = self.e.seq
        events, _ = self.e.events_since(0, 0)
        self.assertEqual([ev["op"] for ev in events], ["upsert"])
        self.e.tick()  # nothing changed except maybe status
        write_jsonl(path, [c_assistant(u["uuid"], tool=("Bash", {"command": "make"}))], mode="a")
        self.e.tick()
        events, _ = self.e.events_since(seq, 0)
        self.assertEqual(events[-1]["node"]["tools"], 1)
        # a client far behind the ring buffer gets told to reload
        self.e.events.clear()
        self.e._emit({"op": "noop"})
        self.e._emit({"op": "noop"})
        self.assertEqual(self.e.events_since(1, 0)[0], None)

    def test_missing_parent_drawn_as_root_and_bad_file_isolated(self):
        write_jsonl(self.t.agent("gone", "a1", {"description": "orphan"}), [c_user("x")])
        good = self.t.session("ok")
        write_jsonl(good, [c_user("fine")])
        self.e.tick()
        self.assertIsNone(self.e.published["claude:gone/a1"]["parent"])
        self.assertIn("claude:ok", self.e.published)

    def test_parser_crash_is_isolated(self):
        bad, good = self.t.session("bad"), self.t.session("good")
        write_jsonl(bad, [c_user("x")])
        write_jsonl(good, [c_user("y")])
        self.e.tick()
        tr = self.e.files["claude:bad"]

        def boom(_records):
            raise RuntimeError("parser exploded")

        tr.parser.feed = boom
        write_jsonl(bad, [c_user("more")], mode="a")
        write_jsonl(good, [c_user("more")], mode="a")
        self.e.tick()
        self.e.tick()
        self.assertEqual(self.e.published["claude:bad"]["failure"], "RuntimeError: parser exploded")
        self.assertEqual(self.e.errors, 1, "logged once, not every tick")
        self.assertEqual(self.e.published["claude:good"]["failure"], "")

    def test_file_removed(self):
        path = self.t.session("tmp")
        write_jsonl(path, [c_user("x")])
        self.e.tick()
        os.remove(path)
        self.e.tick()
        self.assertNotIn("claude:tmp", self.e.published)
        events, _ = self.e.events_since(0, 0)
        self.assertEqual(events[-1], {"op": "remove", "id": "claude:tmp", "seq": self.e.seq})


class PollingEngineTest(EngineTest):
    """Same behaviour without inotify (macOS, BSD, exhausted watch table)."""

    watch = False


class EventDrivenTest(unittest.TestCase):
    def setUp(self):
        self.t = Tree()
        self.e = Engine(self.t.claude, self.t.codex)
        if self.e.watcher is None:
            self.skipTest("inotify unavailable")

    def tearDown(self):
        self.t.cleanup()

    def test_new_directories_are_picked_up_without_a_sweep(self):
        write_jsonl(self.t.session("s"), [c_user("root")])
        self.e.tick()
        sweep = self.e._last_full
        write_jsonl(self.t.agent("s", "a1", {"description": "late agent"}), [c_user("x")])
        wf = os.path.join(self.t.proj, "s", "subagents", "workflows", "wf_1")
        write_jsonl(os.path.join(wf, "journal.jsonl"), [{"type": "launched"}])
        write_jsonl(self.t.agent("s", "w1", {"description": "wf agent"}, workflow="wf_1"), [c_user("x")])
        self.e.tick()
        self.assertEqual(self.e._last_full, sweep, "no full sweep needed")
        self.assertEqual(self.e.published["claude:s/a1"]["title"], "late agent")
        self.assertEqual(self.e.published["claude:s/w1"]["parent"], "claude:s/wf_1")

    def test_idle_tick_touches_nothing(self):
        write_jsonl(self.t.session("s"), [c_user("root")])
        self.e.tick()
        seq = self.e.seq
        self.e._stat = None  # any stat would now raise
        self.e.tick()
        self.assertEqual(self.e.seq, seq)

    def test_status_changes_with_time_alone(self):
        write_jsonl(self.t.session("s"), [c_user("root")])
        self.e.tick()
        self.assertEqual(self.e.published["claude:s"]["status"], "running")
        seen = []
        for _ in range(3):  # follow the scheduled status changes, no file events
            due = self.e.due["claude:s"]
            self.e._publish(self.e.included, set(), due + 0.001)
            seen.append(self.e.published["claude:s"]["status"])
        self.assertEqual(seen, ["idle", "done", "done"])  # mid-turn: running -> idle at 120 s -> done at 30 min


class LiveServerTest(unittest.TestCase):
    """End to end: a write to a session file reaches an SSE client within 2 s."""

    def setUp(self):
        self.t = Tree()
        self.path = self.t.session("live")
        self.root = c_user("live session")
        write_jsonl(self.path, [self.root])
        self.e = Engine(self.t.claude, self.t.codex, interval=0.2)
        self.e.tick()
        self.e.start()
        self.httpd = serve(self.e, "127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.e.stop()
        self.t.cleanup()

    def get(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", path)
        try:
            r = c.getresponse()
            return r.status, r.read()
        finally:
            c.close()

    def test_snapshot_detail_static(self):
        status, body = self.get("/api/snapshot")
        snap = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual([n["id"] for n in snap["nodes"]], ["claude:live"])
        status, body = self.get("/api/node/claude:live")
        self.assertEqual(json.loads(body)["prompt"], "live session")
        self.assertEqual(self.get("/api/node/nope")[0], 404)
        self.assertEqual(self.get("/")[0], 200)
        self.assertEqual(self.get("/vendor/cytoscape.min.js")[0], 200)

    def test_static_rejects_traversal(self):
        s = socket.create_connection(("127.0.0.1", self.port))
        s.sendall(b"GET /../engine.py HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        data = s.recv(4096)
        s.close()
        self.assertIn(b"404", data.split(b"\r\n")[0])

    def test_append_reaches_sse_client(self):
        _, body = self.get("/api/snapshot")
        since = json.loads(body)["seq"]
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        sock.sendall(f"GET /api/events?since={since} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        time.sleep(0.1)
        t0 = time.monotonic()
        write_jsonl(self.path, [c_assistant(self.root["uuid"], tool=("Bash", {"command": "pytest"}))], mode="a")
        buf = b""
        while b'"tools":1' not in buf and time.monotonic() - t0 < 3:
            buf += sock.recv(65536)
        latency = time.monotonic() - t0
        sock.close()
        self.assertIn(b"event: batch", buf)
        self.assertIn(b'"tools":1', buf)
        self.assertLess(latency, 2.0)


if __name__ == "__main__":
    unittest.main()


class ParallelColdParseTest(unittest.TestCase):
    def test_parallel_matches_serial(self):
        import tempfile

        from sagtop import engine as eng
        from sagtop.demo import Demo

        with tempfile.TemporaryDirectory() as root:
            demo = Demo(root)
            serial = Engine(demo.claude_dir, demo.codex_dir, watch=False, parallel=False)
            serial.tick()
            old = eng.PARALLEL_MIN_BYTES
            eng.PARALLEL_MIN_BYTES = 0  # force the worker pool even for a small tree
            try:
                par = Engine(demo.claude_dir, demo.codex_dir, watch=False, parallel=True)
                par.tick()
            finally:
                eng.PARALLEL_MIN_BYTES = old
            self.assertEqual(par.published, serial.published)
            some = next(iter(par.files))
            self.assertEqual(par.detail(some), serial.detail(some))
            # parsed copies keep tailing incrementally afterwards
            demo._tick(1)
            serial.tick()
            par.tick()
            self.assertEqual({k: v["tools"] for k, v in par.published.items()}, {k: v["tools"] for k, v in serial.published.items()})
