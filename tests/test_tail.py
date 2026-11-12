import os
import tempfile
import unittest

from sagtop.tail import Tail


class TailTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "t.jsonl")

    def tearDown(self):
        self.dir.cleanup()

    def write(self, data: bytes, mode="ab"):
        with open(self.path, mode) as fh:
            fh.write(data)

    def test_reads_only_new_lines(self):
        self.write(b'{"a":1}\n{"a":2}\n')
        t = Tail(self.path)
        self.assertEqual(t.read(), ([{"a": 1}, {"a": 2}], False))
        self.assertEqual(t.read(), ([], False))
        self.write(b'{"a":3}\n')
        self.assertEqual(t.read(), ([{"a": 3}], False))

    def test_partial_line_waits_for_newline(self):
        self.write(b'{"a":1}\n{"a":')
        t = Tail(self.path)
        self.assertEqual(t.read()[0], [{"a": 1}])
        self.write(b'2}\n')
        self.assertEqual(t.read()[0], [{"a": 2}])

    def test_bad_lines_are_counted_not_fatal(self):
        self.write(b'{"a":1}\nnot json\n[1,2]\n{"a":2}\n')
        t = Tail(self.path)
        self.assertEqual(t.read()[0], [{"a": 1}, {"a": 2}])
        self.assertEqual(t.bad_lines, 1)

    def test_truncation_resets(self):
        self.write(b'{"a":1}\n{"a":2}\n')
        t = Tail(self.path)
        t.read()
        self.write(b'{"b":1}\n', mode="wb")
        self.assertEqual(t.read(), ([{"b": 1}], True))

    def test_rotation_resets(self):
        self.write(b'{"a":1}\n')
        t = Tail(self.path)
        t.read()
        os.rename(self.path, self.path + ".old")
        self.write(b'{"a":1}\n{"a":2}\n', mode="wb")
        records, reset = t.read()
        self.assertTrue(reset)
        self.assertEqual(records, [{"a": 1}, {"a": 2}])

    def test_skip_and_quick_avoid_decoding(self):
        self.write(b'{"type":"api-request-blob","x":1}\n{"big":"yes"}\n{"a":1}\n')
        t = Tail(self.path, skip=(b'{"type":"api-request',), quick=lambda line: {"stand": 1} if b'"big"' in line else None)
        self.assertEqual(t.read()[0], [{"stand": 1}, {"a": 1}])


if __name__ == "__main__":
    unittest.main()
