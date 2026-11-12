import collections
import tempfile
import unittest

from sagtop.demo import Demo
from sagtop.engine import Engine


class DemoTest(unittest.TestCase):
    def test_demo_tree_covers_every_kind_and_status(self):
        with tempfile.TemporaryDirectory() as root:
            demo = Demo(root)
            for step in range(1, 6):
                demo._tick(step)
            e = Engine(demo.claude_dir, demo.codex_dir)
            e.tick()
            nodes = e.published.values()
            kinds = collections.Counter((n["tool"], n["kind"]) for n in nodes)
            statuses = {n["status"] for n in nodes}
            for k in [("claude", "session"), ("claude", "subagent"), ("claude", "workflow"), ("codex", "session"), ("codex", "subagent"), ("codex", "fork")]:
                self.assertIn(k, kinds)
            self.assertEqual(statuses, {"running", "idle", "done", "error"})
            self.assertTrue(any(n["title"] == "Benchmark retry overhead" for n in nodes), "live spawn happened")
            self.assertTrue(any(n["branches"] == 2 for n in nodes), "rewinds present")
            self.assertEqual(e.errors, 0)


if __name__ == "__main__":
    unittest.main()
