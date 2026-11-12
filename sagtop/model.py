"""Tool-agnostic node shared by the Claude and Codex parsers."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

RUN_FRESH_S = 10  # any write this recent means running
RUN_TURN_S = 120  # mid-turn and written this recently means running
IDLE_S = 30 * 60  # otherwise idle until this old, then done
MAX_TOOL_EVENTS = 20
MAX_TEXT = 4000


def clip(text: str, n: int = MAX_TEXT) -> str:
    text = text.strip()
    return text if len(text) <= n else text[: n - 1] + "…"


@dataclass
class Node:
    id: str
    tool: str  # claude | codex
    kind: str = "session"  # session | subagent | fork | workflow
    parent: str | None = None
    edge: str | None = None  # spawned | forked
    title: str = ""
    cwd: str = ""
    model: str = ""
    started: float = 0.0
    last: float = 0.0  # file mtime (epoch s)
    tokens_out: int = 0
    ctx: int = 0
    tools: int = 0
    branches: int = 0
    in_turn: bool = False
    finished: bool = False
    errored: bool = False
    bad_lines: int = 0
    failure: str = ""  # parser exception, surfaced in the UI
    path: str = ""
    prompt: str = ""
    last_text: str = ""
    tool_events: deque = field(default_factory=lambda: deque(maxlen=MAX_TOOL_EVENTS))

    def status(self, now: float | None = None) -> str:
        age = (now or time.time()) - self.last
        if self.errored:
            return "error"
        if self.finished and age > RUN_FRESH_S:
            return "done"
        if age < RUN_FRESH_S or (self.in_turn and age < RUN_TURN_S):
            return "running"
        return "idle" if age < IDLE_S else "done"

    def next_status_change(self, now: float) -> float:
        """When status() will next change by time alone (inf if never)."""
        if self.errored:
            return float("inf")
        if self.finished:
            marks = (self.last + RUN_FRESH_S,)
        elif self.in_turn:  # running until RUN_TURN_S (> RUN_FRESH_S), then idle
            marks = (self.last + max(RUN_FRESH_S, RUN_TURN_S), self.last + IDLE_S)
        else:
            marks = (self.last + RUN_FRESH_S, self.last + IDLE_S)
        return min((t for t in marks if t > now), default=float("inf"))

    def summary(self, now: float | None = None) -> dict:
        return {
            "id": self.id,
            "tool": self.tool,
            "kind": self.kind,
            "parent": self.parent,
            "edge": self.edge,
            "title": self.title or "(untitled)",
            "cwd": self.cwd,
            "model": self.model,
            "status": self.status(now),
            "started": self.started,
            "last": self.last,
            "tokens_out": self.tokens_out,
            "ctx": self.ctx,
            "tools": self.tools,
            "branches": self.branches,
            "bad_lines": self.bad_lines,
            "failure": self.failure,
        }

    def detail(self) -> dict:
        d = self.summary()
        d.update(
            path=self.path,
            prompt=self.prompt,
            last_text=self.last_text,
            tool_events=list(self.tool_events),
        )
        return d
