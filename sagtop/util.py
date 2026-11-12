from __future__ import annotations

import json
from datetime import datetime

_PREVIEW_KEYS = ("command", "cmd", "description", "file_path", "path", "pattern", "url", "query", "prompt", "skill")


def iso_to_epoch(ts) -> float:
    if not ts or not isinstance(ts, str):
        return 0.0
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def tool_preview(inp, n: int = 160) -> str:
    """One-line hint of what a tool call does."""
    if isinstance(inp, dict):
        for k in _PREVIEW_KEYS:
            v = inp.get(k)
            if v:
                text = " ".join(v) if isinstance(v, list) else str(v)
                break
        else:
            text = json.dumps(inp, ensure_ascii=False)
    elif isinstance(inp, list):
        text = " ".join(map(str, inp))
    else:
        text = str(inp or "")
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"
