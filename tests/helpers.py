"""Builders for synthetic Claude/Codex trees shaped like the real on-disk formats."""

from __future__ import annotations

import json
import os
import tempfile
import uuid


def write_jsonl(path: str, records: list[dict], mode: str = "w") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, mode) as fh:
        for r in records:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")


def c_user(text, parent=None, ts="2026-11-04T10:00:00.000Z", **kw):
    u = str(uuid.uuid4())
    rec = {"parentUuid": parent, "isSidechain": False, "type": "user",
           "message": {"role": "user", "content": text}, "uuid": u, "timestamp": ts, "cwd": "/home/me/repo"}
    rec.update(kw)
    return rec


def c_tool_result(parent, ts="2026-11-04T10:00:02.000Z"):
    return {"parentUuid": parent, "isSidechain": False, "type": "user",
            "message": {"role": "user", "content": [{"tool_use_id": "toolu_1", "type": "tool_result", "content": "ok"}]},
            "uuid": str(uuid.uuid4()), "timestamp": ts}


def c_assistant(parent, mid="msg_1", text=None, tool=None, out=10, ts="2026-11-04T10:00:01.000Z", stop=None, **kw):
    content = []
    if text:
        content.append({"type": "text", "text": text})
    if tool:
        content.append({"type": "tool_use", "id": "toolu_1", "name": tool[0], "input": tool[1]})
    rec = {"parentUuid": parent, "isSidechain": False, "type": "assistant", "uuid": str(uuid.uuid4()), "timestamp": ts,
           "message": {"model": "claude-opus-5-5", "id": mid, "role": "assistant", "content": content, "stop_reason": stop,
                       "usage": {"input_tokens": 5, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 0, "output_tokens": out}}}
    rec.update(kw)
    return rec


def c_turn_end(parent):
    return {"parentUuid": parent, "type": "system", "subtype": "turn_duration", "durationMs": 10, "uuid": str(uuid.uuid4()),
            "timestamp": "2026-11-04T10:00:03.000Z"}


def x_meta(tid, **kw):
    p = {"id": tid, "session_id": tid, "timestamp": "2026-11-04T09:00:00.000Z", "cwd": "/home/me/repo", "thread_source": "user"}
    p.update(kw)
    return {"timestamp": "2026-11-04T09:00:00.000Z", "type": "session_meta", "payload": p}


def x_event(ptype, ts="2026-11-04T09:00:01.000Z", **kw):
    p = {"type": ptype}
    p.update(kw)
    return {"timestamp": ts, "type": "event_msg", "payload": p}


class Tree:
    """A temp ~/.claude/projects + ~/.codex pair."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.claude = os.path.join(self.tmp.name, "claude", "projects")
        self.codex = os.path.join(self.tmp.name, "codex")
        self.proj = os.path.join(self.claude, "-home-me-repo")
        os.makedirs(self.proj)
        os.makedirs(os.path.join(self.codex, "sessions", "2026", "11", "04"))

    def session(self, sid):
        return os.path.join(self.proj, f"{sid}.jsonl")

    def agent(self, sid, aid, meta=None, workflow=None):
        base = os.path.join(self.proj, sid, "subagents")
        if workflow:
            base = os.path.join(base, "workflows", workflow)
        path = os.path.join(base, f"agent-{aid}.jsonl")
        if meta is not None:
            os.makedirs(base, exist_ok=True)
            with open(path[:-6] + ".meta.json", "w") as fh:
                json.dump(meta, fh)
        return path

    def rollout(self, tid):
        return os.path.join(self.codex, "sessions", "2026", "11", "04", f"rollout-2026-11-04T09-00-00-{tid}.jsonl")

    def cleanup(self):
        self.tmp.cleanup()
