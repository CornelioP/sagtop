import json
import os
import unittest

from sagtop import claude, codex
from sagtop.model import Node
from sagtop.tail import Tail

from .helpers import (Tree, c_assistant, c_tool_result, c_turn_end, c_user, write_jsonl, x_event, x_meta)


def parse(parser, path):
    records, _ = Tail(path, parser.skip, parser.quick).read()
    parser.feed(records)
    return parser.node


class ClaudeSessionTest(unittest.TestCase):
    def setUp(self):
        self.t = Tree()

    def tearDown(self):
        self.t.cleanup()

    def test_session_fields(self):
        u1 = c_user("<local-command-caveat>ignored</local-command-caveat>", isMeta=True)
        u2 = c_user("Build the graph viewer\nsecond line", parent=u1["uuid"])
        a1 = c_assistant(u2["uuid"], mid="m1", tool=("Bash", {"command": "ls -la"}), out=7)
        a1b = c_assistant(a1["uuid"], mid="m1", text="split block of same message", out=7)  # same id: count once
        tr = c_tool_result(a1b["uuid"])
        a2 = c_assistant(tr["uuid"], mid="m2", text="done", out=3, stop="end_turn")
        path = self.t.session("s1")
        write_jsonl(path, [{"type": "api-request-blob", "message": "x" * 100}, u1, u2, a1, a1b, tr, a2, c_turn_end(a2["uuid"])])
        n = parse(claude.ClaudeParser(Node(id="claude:s1", tool="claude")), path)
        self.assertEqual(n.title, "Build the graph viewer")
        self.assertTrue(n.prompt.startswith("Build the graph viewer"))
        self.assertEqual(n.model, "claude-opus-5-5")
        self.assertEqual(n.tokens_out, 10)
        self.assertEqual(n.ctx, 105)
        self.assertEqual(n.tools, 1)
        self.assertEqual(list(n.tool_events)[0]["arg"], "ls -la")
        self.assertEqual(n.last_text, "done")
        self.assertFalse(n.in_turn)
        self.assertEqual(n.cwd, "/home/me/repo")

    def test_ai_title_wins_and_in_turn_tracks_tool_results(self):
        u = c_user("hello")
        a = c_assistant(u["uuid"], tool=("Read", {"file_path": "/x"}))
        path = self.t.session("s2")
        write_jsonl(path, [u, a, c_tool_result(a["uuid"]), {"type": "ai-title", "aiTitle": "Nice title"}])
        n = parse(claude.ClaudeParser(Node(id="claude:s2", tool="claude")), path)
        self.assertEqual(n.title, "Nice title")
        self.assertTrue(n.in_turn)

    def test_rewind_branches(self):
        root = c_user("first")
        a = c_assistant(root["uuid"])
        b1 = c_user("try A", parent=a["uuid"])
        b2 = c_user("try B", parent=a["uuid"])
        b3 = c_user("try C", parent=a["uuid"])
        path = self.t.session("s3")
        write_jsonl(path, [root, a, b1, b2, b3])
        n = parse(claude.ClaudeParser(Node(id="claude:s3", tool="claude")), path)
        self.assertEqual(n.branches, 2)

    def test_api_error(self):
        u = c_user("hi")
        path = self.t.session("s4")
        write_jsonl(path, [u, c_assistant(u["uuid"], isApiErrorMessage=True)])
        n = parse(claude.ClaudeParser(Node(id="claude:s4", tool="claude")), path)
        self.assertEqual(n.status(), "error")

    def test_reset_clears_counters(self):
        u = c_user("hi")
        path = self.t.session("s5")
        write_jsonl(path, [u, c_assistant(u["uuid"], tool=("Bash", {"command": "x"}))])
        p = claude.ClaudeParser(Node(id="claude:s5", tool="claude"))
        parse(p, path)
        p.reset()
        parse(p, path)
        self.assertEqual(p.node.tools, 1)
        self.assertEqual(p.node.tokens_out, 10)


class ClaudeTreeTest(unittest.TestCase):
    def setUp(self):
        self.t = Tree()

    def tearDown(self):
        self.t.cleanup()

    def test_discover_and_meta(self):
        write_jsonl(self.t.session("s1"), [c_user("root")])
        write_jsonl(self.t.agent("s1", "a1", {"description": "Explore code", "spawnDepth": 1, "finished": True}), [c_user("go")])
        write_jsonl(self.t.agent("s1", "a2", {"description": "Nested", "spawnDepth": 2, "parentAgentId": "a1"}), [c_user("go")])
        write_jsonl(self.t.agent("s1", "a3", {"description": "Forked", "isFork": True}), [c_user("go")])
        wf_dir = os.path.join(self.t.proj, "s1", "subagents", "workflows", "wf_1")
        write_jsonl(os.path.join(wf_dir, "journal.jsonl"),
                    [{"type": "launched"}, {"type": "started", "agentId": "w1", "label": "review:a", "phase": "Review"}])
        write_jsonl(self.t.agent("s1", "w1", {"description": "review:a"}, workflow="wf_1"), [c_user("go")])

        found = {nid: (kind, parent, meta) for nid, kind, _p, parent, meta in claude.discover(self.t.claude)}
        self.assertEqual(found["claude:s1"][:2], ("session", None))
        self.assertEqual(found["claude:s1/a1"][:2], ("subagent", "claude:s1"))
        self.assertEqual(found["claude:s1/wf_1"][:2], ("workflow", "claude:s1"))
        self.assertEqual(found["claude:s1/w1"][:2], ("subagent", "claude:s1/wf_1"))
        self.assertNotIn("claude:s1/journal", " ".join(found))

        def meta_node(aid):
            p = claude.ClaudeParser(Node(id=f"claude:s1/{aid}", tool="claude", kind="subagent", parent="claude:s1"))
            p.apply_meta(found[f"claude:s1/{aid}"][2])
            return p.node

        a1, a2, a3 = meta_node("a1"), meta_node("a2"), meta_node("a3")
        self.assertEqual((a1.title, a1.finished), ("Explore code", True))
        self.assertEqual(a2.parent, "claude:s1/a1")
        self.assertEqual((a3.kind, a3.edge), ("fork", "forked"))

    def test_workflow_progress_then_record(self):
        wf_dir = os.path.join(self.t.proj, "s1", "subagents", "workflows", "wf_9")
        journal = os.path.join(wf_dir, "journal.jsonl")
        write_jsonl(journal, [{"type": "launched"}, {"type": "started", "agentId": "x", "phase": "Build", "label": "b"}])
        p = claude.WorkflowParser(Node(id="claude:s1/wf_9", tool="claude", kind="workflow"))
        tail = Tail(journal)
        p.feed(tail.read()[0])
        self.assertEqual((p.node.title, p.node.tools, p.node.in_turn), ("wf_9 · Build", 1, True))
        write_jsonl(journal, [{"type": "result", "agentId": "x", "result": {}}], mode="a")
        p.feed(tail.read()[0])
        self.assertFalse(p.node.in_turn)
        record = os.path.join(self.t.proj, "s1", "workflows", "wf_9.json")
        os.makedirs(os.path.dirname(record))
        with open(record, "w") as fh:
            json.dump({"workflowName": "build-it", "status": "completed", "totalTokens": 1234, "startTime": 1_700_000_000_000}, fh)
        p.apply_meta(record)
        self.assertEqual((p.node.title, p.node.tokens_out, p.node.finished), ("build-it", 1234, True))


class CodexTest(unittest.TestCase):
    def setUp(self):
        self.t = Tree()

    def tearDown(self):
        self.t.cleanup()

    def test_subagent_session(self):
        tid = "01a12f08-7574-7e60-8e92-d7341296ef7a"
        parent = "01a1211c-096a-7a43-aee5-54adeb3616d4"
        path = self.t.rollout(tid)
        write_jsonl(path, [
            x_meta(tid, thread_source="subagent", parent_thread_id=parent, forked_from_id=parent, agent_nickname="Bohr", agent_path="/root/facts"),
            {"timestamp": "2026-11-04T09:00:00.500Z", "type": "turn_context", "payload": {"model": "gpt-6-astra", "cwd": "/w"}},
            x_event("task_started"),
            x_event("item_completed", item={"type": "UserMessage", "content": [{"type": "text", "text": "Find facts"}]}),
            x_event("item_completed", item={"type": "CommandExecution", "command": ["/usr/bin/bash", "-lc", "rg foo"]}),
            x_event("item_completed", item={"type": "McpToolCall", "server": "gdrive", "tool": "search", "arguments": {"query": "q"}}),
            x_event("token_count", info={"total_token_usage": {"output_tokens": 42}, "last_token_usage": {"input_tokens": 900}}),
            x_event("task_complete", last_agent_message="All done"),
        ])
        found = dict(codex.discover(self.t.codex))
        self.assertEqual(found[f"codex:{tid}"], path)
        n = parse(codex.CodexParser(Node(id=f"codex:{tid}", tool="codex"), {}), path)
        self.assertEqual((n.kind, n.edge, n.parent), ("subagent", "spawned", f"codex:{parent}"))
        self.assertEqual(n.title, "Bohr /root/facts")
        self.assertEqual(n.model, "gpt-6-astra")
        self.assertEqual((n.tools, n.tokens_out, n.ctx), (2, 42, 900))
        self.assertEqual([e["name"] for e in n.tool_events], ["exec", "gdrive.search"])
        self.assertEqual(list(n.tool_events)[0]["arg"], "rg foo")
        self.assertEqual((n.prompt, n.last_text, n.in_turn, n.finished), ("Find facts", "All done", False, True))

    def test_ignored_records_are_never_decoded(self):
        tid = "01a10000-0000-7000-8000-000000000003"
        path = self.t.rollout(tid)
        write_jsonl(path, [
            x_meta(tid),
            {"timestamp": "2026-11-04T09:00:01.000Z", "type": "response_item", "payload": {"type": "reasoning", "encrypted_content": "x" * 5000}},
            {"timestamp": "2026-11-04T09:00:01.000Z", "type": "compacted", "payload": {"message": ""}},
            x_event("task_started"),
        ])
        with open(path, "ab") as fh:  # an ignored type with a broken body must not count as a bad line
            fh.write(b'{"timestamp":"2026-11-04T09:00:02.000Z","type":"response_item","payload":{broken\n')
        tail = Tail(path, codex.CodexParser.skip, codex.CodexParser.quick)
        records, _ = tail.read()
        self.assertEqual([r.get("type") for r in records], ["session_meta", None, None, "event_msg", None])
        self.assertEqual(tail.bad_lines, 0)

    def test_fork_and_index_name(self):
        tid, src = "01a10000-0000-7000-8000-000000000001", "01a10000-0000-7000-8000-000000000002"
        path = self.t.rollout(tid)
        write_jsonl(path, [x_meta(tid, forked_from_id=src), x_event("task_started")])
        names = {}
        idx = os.path.join(self.t.codex, "session_index.jsonl")
        write_jsonl(idx, [{"id": tid, "thread_name": "old"}, {"id": tid, "thread_name": "Renamed thread"}])
        codex.load_names(idx, names)
        n = parse(codex.CodexParser(Node(id=f"codex:{tid}", tool="codex"), names), path)
        self.assertEqual((n.kind, n.edge, n.parent, n.title), ("fork", "forked", f"codex:{src}", "Renamed thread"))
        self.assertTrue(n.in_turn)


if __name__ == "__main__":
    unittest.main()
