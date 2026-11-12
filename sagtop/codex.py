"""Codex rollouts: ~/.codex/sessions/YYYY/MM/DD/rollout-*-<id>.jsonl (+ archived_sessions)."""

from __future__ import annotations

import json
import os
import re

from .model import Node, clip
from .util import iso_to_epoch, tool_preview

_ID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$")
# Record types the parser never reads (model I/O, reasoning, compaction, usage
# records); about a third of a typical rollout. Recognised from the line head.
_IGNORED_TYPES = {b"response_item", b"compacted", b"token_usage_record", b"world_state", b"inter_agent_communication_metadata"}


def quick(head: bytes):
    """Stand-in {} for records the parser ignores, decided from the first bytes."""
    i = head.find(b'"type":"')  # the record's own type: "timestamp" comes first, nested types later
    if i < 0:
        return None
    j = head.find(b'"', i + 8)
    return {} if j > 0 and head[i + 8 : j] in _IGNORED_TYPES else None


_TOOL_ITEMS = {"CommandExecution", "McpToolCall", "FileChange", "Extension", "ImageView"}


def node_id(tid: str) -> str:
    return f"codex:{tid}"


def _item_tool(item: dict) -> tuple[str, str]:
    t = item.get("type")
    if t == "CommandExecution":
        cmd = item.get("command") or []
        if isinstance(cmd, list) and len(cmd) >= 3 and cmd[1] in ("-lc", "-c"):
            cmd = cmd[2]
        return "exec", tool_preview(cmd)
    if t == "McpToolCall":
        return f"{item.get('server')}.{item.get('tool')}", tool_preview(item.get("arguments"))
    if t == "FileChange":
        return "edit", tool_preview(list((item.get("changes") or {}).keys()))
    if t == "Extension":
        return item.get("kind") or "ext", tool_preview(item.get("query") or "")
    return t.lower(), tool_preview(item.get("path") or "")


def _text(content) -> str:
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
    return str(content or "")


class CodexParser:
    skip: tuple[bytes, ...] = ()
    quick = staticmethod(quick)

    def __init__(self, node: Node, names: dict[str, str]):
        self.node = node
        self.names = names  # thread id -> thread_name from session_index.jsonl
        self.fallback_title = ""

    def reset(self) -> None:
        n = self.node
        n.tokens_out = n.ctx = n.tools = 0
        n.in_turn = n.finished = n.errored = False
        n.prompt = n.last_text = ""
        n.tool_events.clear()

    def refresh_title(self) -> None:
        tid = self.node.id.split(":", 1)[1]
        self.node.title = self.names.get(tid) or self.fallback_title or self.node.title

    def feed(self, records: list[dict]) -> None:
        n = self.node
        for rec in records:
            p = rec.get("payload")
            if not isinstance(p, dict):
                continue
            rtype = rec.get("type")
            ts = iso_to_epoch(rec.get("timestamp"))
            if ts and not n.started:
                n.started = ts
            if rtype == "session_meta":
                self._meta(p)
            elif rtype == "turn_context":
                n.model = p.get("model") or n.model
                n.cwd = n.cwd or p.get("cwd") or ""
            elif rtype == "event_msg":
                self._event(p, ts)
        self.refresh_title()

    def _meta(self, p: dict) -> None:
        n = self.node
        n.cwd = p.get("cwd") or n.cwd
        n.started = iso_to_epoch(p.get("timestamp")) or n.started
        if p.get("thread_source") == "subagent" and p.get("parent_thread_id"):
            n.kind, n.edge, n.parent = "subagent", "spawned", node_id(p["parent_thread_id"])
            nick = " ".join(x for x in (p.get("agent_nickname"), p.get("agent_path")) if x)
            self.fallback_title = nick or self.fallback_title
        elif p.get("forked_from_id"):
            n.kind, n.edge, n.parent = "fork", "forked", node_id(p["forked_from_id"])

    def _event(self, p: dict, ts: float) -> None:
        n = self.node
        t = p.get("type")
        if t == "task_started":
            n.in_turn, n.finished, n.errored = True, False, False
        elif t == "task_complete":
            n.in_turn = False
            n.finished = n.kind != "session"
            if p.get("last_agent_message"):
                n.last_text = clip(p["last_agent_message"], 1500)
        elif t == "turn_aborted":
            n.in_turn = False
        elif t == "error":
            n.errored = True
        elif t == "thread_settings_applied":
            n.model = (p.get("thread_settings") or {}).get("model") or n.model
        elif t == "token_count":
            info = p.get("info") or {}
            n.tokens_out = (info.get("total_token_usage") or {}).get("output_tokens") or n.tokens_out
            n.ctx = (info.get("last_token_usage") or {}).get("input_tokens") or n.ctx
        elif t == "item_completed":
            item = p.get("item") or {}
            it = item.get("type")
            if it == "UserMessage":
                text = _text(item.get("content"))
                if not n.prompt and text.strip() and not text.lstrip().startswith("<"):
                    n.prompt = clip(text)
                    self.fallback_title = self.fallback_title or clip(text.splitlines()[0], 120)
            elif it == "AgentMessage":
                n.last_text = clip(_text(item.get("content")), 1500) or n.last_text
            elif it in _TOOL_ITEMS:
                n.tools += 1
                name, arg = _item_tool(item)
                n.tool_events.append({"t": ts, "name": name, "arg": arg})
            elif it == "SubAgentActivity" and item.get("kind") == "started":
                n.tool_events.append({"t": ts, "name": "spawn", "arg": item.get("agent_path") or ""})


def discover(root: str, dirs: set[str] | None = None):
    """Yields (node_id, path) for every rollout under sessions/ and archived_sessions/.

    Visited directories (and `root`, for session_index.jsonl) are added to `dirs`.
    """
    seen = dirs if dirs is not None else set()
    if os.path.isdir(root):
        seen.add(root)
    for sub in ("sessions", "archived_sessions"):
        for dirpath, _dirs, files in os.walk(os.path.join(root, sub)):
            seen.add(dirpath)
            for f in files:
                m = _ID_RE.search(f)
                if m:
                    yield node_id(m.group(1)), os.path.join(dirpath, f)


def load_names(index_path: str, names: dict[str, str]) -> None:
    """session_index.jsonl is append-only; later lines win."""
    try:
        with open(index_path) as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("id") and d.get("thread_name"):
                    names[d["id"]] = d["thread_name"]
    except OSError:
        pass
