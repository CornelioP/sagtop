"""Claude Code transcripts: ~/.claude/projects/<proj>/<sid>.jsonl and <sid>/subagents/."""

from __future__ import annotations

import json
import os
from collections import defaultdict

from .model import Node, clip
from .util import iso_to_epoch, tool_preview

# Bulky records we never need; skipped before JSON decoding.
SKIP = (
    b'{"type":"api-request',
    b'{"type":"file-history',
    b'{"type":"queue-operation',
    b'{"type":"frame-link',
    b'{"type":"artifact-',
)


_TOOL_RESULT = b'"content":[{"tool_use_id"'
_ATTACHMENT = b'"attachment":{'
_TOOL_RESULT_STAND_IN = {"type": "user", "toolResult": True}


def quick(head: bytes):
    """Recognise bulky lines whose content we never use, from their first bytes."""
    if _TOOL_RESULT in head and b'"type":"user"' in head:
        return _TOOL_RESULT_STAND_IN  # only marks the turn as in progress
    if _ATTACHMENT in head:
        return {}
    return None


def _load_json(path: str):
    try:
        with open(path) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


class WorkflowParser:
    """A Workflow run: journal.jsonl gives live progress, <wf>.json the final record."""

    skip: tuple[bytes, ...] = ()
    quick = None

    def __init__(self, node: Node):
        self.node = node
        self.started_agents: set[str] = set()
        self.done_agents: set[str] = set()
        self.final = False
        self.named = False
        self.phase = ""

    def reset(self) -> None:
        self.started_agents.clear()
        self.done_agents.clear()
        self.node.tool_events.clear()
        self._sync()

    def feed(self, records: list[dict]) -> None:
        for rec in records:
            t, aid = rec.get("type"), rec.get("agentId")
            if t == "started" and aid:
                self.started_agents.add(aid)
                self.phase = rec.get("phase") or self.phase
                self.node.tool_events.append({"t": 0, "name": rec.get("phase") or "agent", "arg": rec.get("label") or aid})
            elif t == "result" and aid:
                self.done_agents.add(aid)
        self._sync()

    def _sync(self) -> None:
        n = self.node
        n.tools = len(self.started_agents)
        if not self.final:
            n.in_turn = bool(self.started_agents - self.done_agents) or not self.started_agents
        if not self.named:
            wid = n.id.rsplit("/", 1)[1]
            n.title = f"{wid} · {self.phase}" if self.phase else wid

    def apply_meta(self, meta_path: str) -> None:
        rec = _load_json(meta_path)
        if not rec:
            return
        n = self.node
        if rec.get("workflowName"):
            n.title, self.named = rec["workflowName"], True
        n.prompt = clip(rec.get("summary") or "")
        n.model = rec.get("defaultModel") or n.model
        n.tokens_out = int(rec.get("totalTokens") or 0)
        n.started = (rec.get("startTime") or 0) / 1000 or n.started
        status = rec.get("status")
        self.final = status in ("completed", "failed", "error", "cancelled", "stopped")
        n.finished = self.final
        n.in_turn = not self.final
        n.errored = status in ("failed", "error")


def session_id(sid: str) -> str:
    return f"claude:{sid}"


def agent_id(sid: str, aid: str) -> str:
    return f"claude:{sid}/{aid}"


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _is_real_prompt(rec: dict) -> bool:
    if rec.get("isMeta") or rec.get("type") != "user":
        return False
    content = rec.get("message", {}).get("content")
    if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return False
    text = _text_of(content).lstrip()
    return bool(text) and not text.startswith("<")


class ClaudeParser:
    """Folds one Claude transcript (session or subagent) into a Node."""

    skip = SKIP
    quick = staticmethod(quick)

    def __init__(self, node: Node):
        self.node = node
        self._reset_state()

    def _reset_state(self) -> None:
        self.prompt_children: dict[str, int] = defaultdict(int)
        self.seen_msg_ids: set[str] = set()
        self.ai_title = ""

    def apply_meta(self, meta_path: str) -> None:
        """Subagent .meta.json: description, parent agent, fork flag, completion."""
        meta = _load_json(meta_path)
        if not meta:
            return
        node = self.node
        node.title = meta.get("description") or meta.get("name") or node.title
        if meta.get("parentAgentId"):
            sid = node.id.split(":", 1)[1].split("/", 1)[0]
            node.parent = agent_id(sid, meta["parentAgentId"])
        node.kind = "fork" if meta.get("isFork") else "subagent"
        node.edge = "forked" if meta.get("isFork") else "spawned"
        node.finished = bool(meta.get("finished") or meta.get("stoppedByUser"))
        if meta.get("model") and not node.model:
            node.model = meta["model"]

    def reset(self) -> None:
        n = self.node
        n.tokens_out = n.ctx = n.tools = n.branches = 0
        n.in_turn = n.errored = False
        n.prompt = n.last_text = n.model = ""
        n.tool_events.clear()
        self._reset_state()

    def feed(self, records: list[dict]) -> None:
        n = self.node
        for rec in records:
            t = rec.get("type")
            if t == "ai-title":
                self.ai_title = rec.get("aiTitle") or self.ai_title
                if n.kind == "session":
                    n.title = self.ai_title
                continue
            if t not in ("user", "assistant", "system"):
                continue
            ts = iso_to_epoch(rec.get("timestamp"))
            if ts and not n.started:
                n.started = ts
            if rec.get("cwd") and not n.cwd:
                n.cwd = rec["cwd"]
            if t == "user":
                self._user(rec)
            elif t == "assistant":
                self._assistant(rec)
            elif rec.get("subtype") == "turn_duration":
                n.in_turn = False

    def _user(self, rec: dict) -> None:
        n = self.node
        n.in_turn = True
        if _is_real_prompt(rec):
            text = _text_of(rec["message"].get("content"))
            if not n.prompt:
                n.prompt = clip(text)
                if not n.title:
                    n.title = clip(text.splitlines()[0], 120)
            parent = rec.get("parentUuid")
            if parent:
                self.prompt_children[parent] += 1
                if self.prompt_children[parent] >= 2:  # each extra prompt on the same parent = a rewind branch
                    n.branches += 1
            n.errored = False

    def _assistant(self, rec: dict) -> None:
        n = self.node
        msg = rec.get("message") or {}
        model = msg.get("model")
        if model and model != "<synthetic>":
            n.model = model
        n.errored = bool(rec.get("isApiErrorMessage"))
        mid = msg.get("id")
        usage = msg.get("usage") or {}
        if usage and mid not in self.seen_msg_ids:
            self.seen_msg_ids.add(mid)
            n.tokens_out += usage.get("output_tokens") or 0
            ctx = sum(usage.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            if ctx:
                n.ctx = ctx
        for block in msg.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                n.tools += 1
                n.tool_events.append(
                    {"t": iso_to_epoch(rec.get("timestamp")), "name": block.get("name", "?"), "arg": tool_preview(block.get("input"))}
                )
            elif block.get("type") == "text" and block.get("text", "").strip():
                n.last_text = clip(block["text"], 1500)
        if msg.get("stop_reason") == "end_turn" and n.kind != "session":
            n.in_turn = False


def discover(root: str, dirs: set[str] | None = None):
    """Yields (node_id, kind, path, parent_id, meta_path) for every transcript.

    Every directory whose contents can produce or change a node is added to `dirs`.
    """
    seen = dirs if dirs is not None else set()
    try:
        projects = list(os.scandir(root))
    except OSError:
        return
    seen.add(root)
    for proj in projects:
        if not proj.is_dir():
            continue
        try:
            entries = list(os.scandir(proj.path))
        except OSError:
            continue
        seen.add(proj.path)
        for e in entries:
            if e.name.endswith(".jsonl") and e.is_file():
                yield session_id(e.name[:-6]), "session", e.path, None, None
            elif e.is_dir():
                yield from _session_dir(e.path, e.name, seen)


def _session_dir(path: str, sid: str, seen: set[str]):
    seen.add(path)  # subagents/ and workflows/ get created in here
    records = os.path.join(path, "workflows")
    if os.path.isdir(records):
        seen.add(records)  # wf_<id>.json run records land here
    sub = os.path.join(path, "subagents")
    try:
        agents = list(os.scandir(sub))
    except OSError:
        return
    seen.add(sub)
    yield from _agents(agents, session_id(sid), sid)
    wf_root = os.path.join(sub, "workflows")
    try:
        runs = [w for w in os.scandir(wf_root) if w.is_dir()]
    except OSError:
        return
    seen.add(wf_root)
    for w in runs:
        wid = agent_id(sid, w.name)
        yield wid, "workflow", os.path.join(w.path, "journal.jsonl"), session_id(sid), os.path.join(records, w.name + ".json")
        try:
            agents = list(os.scandir(w.path))
        except OSError:
            continue
        seen.add(w.path)
        yield from _agents(agents, wid, sid)


def _agents(entries, parent: str, sid: str):
    for a in entries:
        if a.name.startswith("agent-") and a.name.endswith(".jsonl"):
            yield agent_id(sid, a.name[6:-6]), "subagent", a.path, parent, a.path[:-6] + ".meta.json"
