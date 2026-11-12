# sagtop — live graph of Claude Code and Codex sessions

Date: 2026-11-04. Status: approved in chat, implemented in v0.1.

Measured on this machine: cold start 0.8 s for the 24h window (139 nodes, ~310 MB
read), 7 s for all history (1,382 nodes); steady-state tick ~15 ms; new
nodes reach the browser within one 1 s poll.

## Goal

Watch Claude Code and Codex sessions live in a browser, as a graph: one node per
session, subagent, or fork; edges = spawned / forked. Read-only over
`~/.claude/projects` and `~/.codex/{sessions,archived_sessions}`. No hooks, no
change to agent configuration.

Success: start a session that spawns subagents and see parent and children
appear and change state within ~2 s.

## Run

`python3 -m sagtop` → `http://127.0.0.1:7878`. Remote: `ssh -L 7878:localhost:7878 <box>`.
Python 3.12 stdlib only. Cytoscape.js and JetBrains Mono vendored in `sagtop/static/vendor`.

## Data model

`Node`: `id`, `tool` (claude|codex), `kind` (session|subagent|fork|workflow), `parent`,
`edge` (spawned|forked), `title`, `cwd`, `model`, `status`, `started`, `last`,
`tokens_out`, `ctx`, `tools`, `branches`, `depth`, `bad_lines`.

Status: `running` (mid-turn and written < 120 s ago, or written < 10 s ago),
`idle` (otherwise, written < 30 min ago), `done` (older, or subagent `finished`),
`error` (last assistant record was an API error).

## Sources

Claude
- `projects/<p>/<sid>.jsonl` → session `claude:<sid>`. Title from `ai-title`,
  else first real user prompt. Model, output tokens, context size from assistant
  `message.usage`. Turn end = `system/turn_duration`.
- `projects/<p>/<sid>/subagents/agent-<aid>.jsonl` + `.meta.json` → subagent
  `claude:<sid>/<aid>`; parent = `parentAgentId` when present, else the session.
  `isFork` → kind fork. `finished`/`stoppedByUser` → done.
- `<sid>/subagents/workflows/wf_<id>/` → workflow node `claude:<sid>/wf_<id>`
  (child of the session) with its `agent-*.jsonl` as children. Live progress from
  `journal.jsonl` (`started`/`result`); name, status and tokens from the run
  record `<sid>/workflows/wf_<id>.json` once it exists. (Found during
  implementation: ~440 of 768 subagent transcripts were workflow agents.)
- In-session branches: a parent message with more than one real user prompt as
  child counts as a rewind/edit branch (`branches`).
- Claude does not record session-level forks (verified: no fork field and no
  shared message uuids across 1,274 sessions), so none are drawn.

Codex
- `sessions/YYYY/MM/DD/rollout-*-<id>.jsonl`, `archived_sessions/*.jsonl` →
  `codex:<id>`. `session_meta`: `thread_source=subagent` → spawned child of
  `parent_thread_id`; else `forked_from_id` → forked. Title from
  `session_index.jsonl` thread name, else `agent_nickname agent_path`, else
  first user message. Status from `task_started` / `task_complete` /
  `turn_aborted`. Tokens from `token_count`.

## Live engine

- Poll every 1 s: scan dirs, `stat` files. Only files with mtime in the window
  (1h|24h|7d|all; default 24h) are parsed, plus ancestors of included nodes.
- Incremental tail from a stored byte offset; partial trailing lines buffered;
  bad JSON lines skipped and counted; truncation or inode change → reparse.
- A failing file never stops the loop; its error is surfaced on its node.
- The window is server-wide (single-user tool).
- SSE `/api/events?since=<seq>`: `upsert`/`remove` events with a monotonically
  increasing seq kept in a ring buffer; a client that fell behind gets `reset`
  and refetches `/api/snapshot`. 15 s heartbeat.

## UI

Black background, phosphor green, amber running, red error, JetBrains Mono,
optional scanlines. Deterministic left-to-right tree layout (own implementation,
replacing dagre): trees newest-first, children by start time, trees packed into
columns shaped like the viewport; auto-fit until the user pans/zooms; animated moves, right-angle edges,
dashed fork edges, blinking `█` on running nodes. htop-style top bar with
counts, window, events/s, link state; tmux-style key bar.

Keys: `j/k` next/prev, `h` parent, `l` child, `Enter` detail panel, `/` filter
(`status:` `tool:` `kind:` + free text), `f` follow newest running, `t` cycle
window, `g` fit, `s` scanlines, `?` help, `Esc` close.

## Testing

Unit tests for both parsers on trimmed fixtures, tailer edge cases (partial
line, truncation, rotation), an engine test that appends to a fake session and
expects an SSE upsert within 2 s, and a timed cold start on real data.

## Out of scope (v1)

Claude Code hooks, cost in USD, editing or resuming sessions from the UI.
