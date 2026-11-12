import os
import unittest

from sagtop.__main__ import default_dirs
from sagtop.engine import Engine


class DefaultDirsTest(unittest.TestCase):
    def test_falls_back_to_home(self):
        claude, codex = default_dirs({})
        self.assertEqual(claude, os.path.expanduser("~/.claude/projects"))
        self.assertEqual(codex, os.path.expanduser("~/.codex"))

    def test_honours_tool_overrides(self):
        claude, codex = default_dirs({"CLAUDE_CONFIG_DIR": "/srv/cc", "CODEX_HOME": "/srv/cx"})
        self.assertEqual((claude, codex), ("/srv/cc/projects", "/srv/cx"))

    def test_empty_override_is_ignored(self):
        self.assertEqual(default_dirs({"CLAUDE_CONFIG_DIR": ""})[0], os.path.expanduser("~/.claude/projects"))


class SnapshotReportsDirsTest(unittest.TestCase):
    def test_snapshot_names_watched_dirs_and_home(self):
        snap = Engine("/x/projects", "/y/codex").snapshot()
        self.assertEqual((snap["claude_dir"], snap["codex_dir"]), ("/x/projects", "/y/codex"))
        self.assertEqual(snap["home"], os.path.expanduser("~"))


if __name__ == "__main__":
    unittest.main()
