"""Synthetic Claude/Codex session trees that keep changing: `python3 -m sagtop --demo`.

Everything here is invented; it exists so sagtop can be tried (and screenshotted)
without exposing real transcripts.
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
import uuid
from datetime import datetime, timezone

MIN = 60
HOUR = 3600


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


def _append(path: str, records: list[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as fh:
        for r in records:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")


def _age(path: str, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


class _Claude:
    """Writes one Claude transcript (session or agent)."""

    def __init__(self, path: str, cwd: str, model: str = "claude-opus-5-5"):
        self.path, self.cwd, self.model = path, cwd, model
        self.last = None
        self.n = 0

    def _rec(self, t, kind, **kw):
        r = {"parentUuid": self.last, "isSidechain": False, "type": kind, "uuid": str(uuid.uuid4()),
             "timestamp": _iso(t), "cwd": self.cwd}
        r.update(kw)
        self.last = r["uuid"]
        return r

    def prompt(self, text, t, parent=None):
        r = self._rec(t, "user", message={"role": "user", "content": text})
        if parent is not None:
            r["parentUuid"] = parent
        _append(self.path, [r])
        return r["uuid"]

    def tool(self, name, arg, t, out=None):
        self.n += 1
        key = {"Bash": "command", "Read": "file_path", "Edit": "file_path", "Grep": "pattern", "Agent": "description"}.get(name, "query")
        a = self._rec(t, "assistant", message={
            "model": self.model, "id": f"msg_{uuid.uuid4().hex[:12]}", "role": "assistant", "stop_reason": "tool_use",
            "content": ([{"type": "text", "text": f"Next I'll run {name.lower()} on this."}] if self.n % 3 == 0 else [])
            + [{"type": "tool_use", "id": f"toolu_{self.n}", "name": name, "input": {key: arg}}],
            "usage": {"input_tokens": 12, "cache_read_input_tokens": 18_000 + 900 * self.n,
                      "cache_creation_input_tokens": 400, "output_tokens": out or random.randint(80, 900)}})
        res = self._rec(t + 1, "user", message={"role": "user", "content": [
            {"tool_use_id": f"toolu_{self.n}", "type": "tool_result", "content": "ok"}]})
        _append(self.path, [a, res])

    def say(self, text, t, error=False):
        r = self._rec(t, "assistant", message={
            "model": self.model, "id": f"msg_{uuid.uuid4().hex[:12]}", "role": "assistant", "stop_reason": "end_turn",
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 10, "cache_read_input_tokens": 30_000, "cache_creation_input_tokens": 0, "output_tokens": 420}})
        if error:
            r["isApiErrorMessage"] = True
        _append(self.path, [r, self._rec(t + 1, "system", subtype="turn_duration", durationMs=42_000)])

    def title(self, text):
        _append(self.path, [{"type": "ai-title", "aiTitle": text}])


class _Codex:
    def __init__(self, path: str, tid: str, start: float, cwd: str, **meta):
        self.path, self.tid = path, tid
        _append(path, [
            {"timestamp": _iso(start), "type": "session_meta", "payload": {"id": tid, "timestamp": _iso(start), "cwd": cwd, **meta}},
            {"timestamp": _iso(start), "type": "turn_context", "payload": {"model": "gpt-6-astra", "cwd": cwd}},
        ])
        self.out = 0

    def event(self, t, ptype, **kw):
        _append(self.path, [{"timestamp": _iso(t), "type": "event_msg", "payload": {"type": ptype, **kw}}])

    def prompt(self, text, t):
        self.event(t, "task_started")
        self.event(t, "item_completed", item={"type": "UserMessage", "content": [{"type": "text", "text": text}]})

    def run(self, cmd, t):
        self.out += random.randint(200, 1500)
        self.event(t, "item_completed", item={"type": "CommandExecution", "command": ["/usr/bin/bash", "-lc", cmd]})
        self.event(t, "token_count", info={"total_token_usage": {"output_tokens": self.out},
                                           "last_token_usage": {"input_tokens": 40_000 + self.out * 3}})

    def done(self, text, t):
        self.event(t, "item_completed", item={"type": "AgentMessage", "content": [{"type": "Text", "text": text}]})
        self.event(t, "task_complete", last_agent_message=text)


def _tid() -> str:
    u = str(uuid.uuid4())
    return "01a1" + u[4:]


class Demo:
    """Builds the tree under `root` and, once started, keeps the 'running' parts busy."""

    def __init__(self, root: str):
        self.root = root
        self.claude_dir = os.path.join(root, "claude", "projects")
        self.codex_dir = os.path.join(root, "codex")
        self.live: list = []
        self._stop = threading.Event()
        self._build()
        self._settle_meta()

    def _settle_meta(self) -> None:
        """A .meta.json counts as activity; give each the age of its transcript."""
        for dirpath, _dirs, files in os.walk(self.claude_dir):
            for f in files:
                if f.endswith(".meta.json"):
                    transcript = os.path.join(dirpath, f[: -len(".meta.json")] + ".jsonl")
                    if os.path.exists(transcript):
                        m = os.stat(transcript).st_mtime
                        os.utime(os.path.join(dirpath, f), (m, m))

    # ------------------------------------------------------------------ helpers
    def _proj(self, name):
        return os.path.join(self.claude_dir, f"-home-dev-src-{name}")

    def _session(self, proj, cwd, model="claude-opus-5-5"):
        sid = str(uuid.uuid4())
        return sid, _Claude(os.path.join(self._proj(proj), f"{sid}.jsonl"), cwd, model)

    def _agent(self, proj, sid, aid, meta, cwd, workflow=None, model="claude-opus-5-5"):
        base = os.path.join(self._proj(proj), sid, "subagents")
        if workflow:
            base = os.path.join(base, "workflows", workflow)
        os.makedirs(base, exist_ok=True)
        with open(os.path.join(base, f"agent-{aid}.meta.json"), "w") as fh:
            json.dump(meta, fh)
        return _Claude(os.path.join(base, f"agent-{aid}.jsonl"), cwd, model)

    def _codex_path(self, tid, t):
        d = datetime.fromtimestamp(t)
        return os.path.join(self.codex_dir, "sessions", d.strftime("%Y/%m/%d"), f"rollout-{d.strftime('%Y-%m-%dT%H-%M-%S')}-{tid}.jsonl")

    # -------------------------------------------------------------------- tree
    def _build(self) -> None:
        now = time.time()
        os.makedirs(os.path.join(self.codex_dir, "sessions"), exist_ok=True)

        # 1. busy Claude session: subagents, a nested agent and a review workflow
        cwd = "/home/dev/src/uploader"
        sid, s = self._session("uploader", cwd)
        self.main_sid = sid
        s.prompt("Add retry with exponential backoff to the upload client, with tests and a review", now - 40 * MIN)
        s.title("Retry logic for the upload client")
        for i, (n, a) in enumerate([("Grep", "def upload"), ("Read", "src/uploader/client.py"), ("Agent", "Explore upload client"),
                                    ("Edit", "src/uploader/client.py"), ("Bash", "pytest tests/test_client.py -q")]):
            s.tool(n, a, now - 38 * MIN + i * 60)
        explore = self._agent("uploader", sid, "a1explore", {"description": "Explore upload client", "spawnDepth": 1, "finished": True}, cwd, model="claude-sonnet-5-5")
        explore.prompt("Map how uploads are sent and where errors surface", now - 36 * MIN)
        for n, a in [("Grep", "requests.post"), ("Read", "src/uploader/transport.py"), ("Read", "src/uploader/errors.py")]:
            explore.tool(n, a, now - 35 * MIN)
        explore.say("Uploads go through Transport.send; 5xx and timeouts raise UploadError.", now - 33 * MIN)
        _age(explore.path, 33 * MIN)
        tests = self._agent("uploader", sid, "a2tests", {"description": "Write failing retry tests", "spawnDepth": 1}, cwd)
        tests.prompt("Write tests that fail until retry exists", now - 20 * MIN)
        tests.tool("Edit", "tests/test_retry.py", now - 19 * MIN)
        review = self._agent("uploader", sid, "a3review", {"description": "Review retry patch", "spawnDepth": 1}, cwd)
        review.prompt("Review the retry patch for correctness", now - 12 * MIN)
        review.tool("Bash", "git diff main -- src/uploader", now - 11 * MIN)
        nested = self._agent("uploader", sid, "a4math", {"description": "Check backoff math", "spawnDepth": 2, "parentAgentId": "a3review"}, cwd, model="claude-sonnet-5-5")
        nested.prompt("Verify the jittered backoff never exceeds the 30 s cap", now - 10 * MIN)
        nested.tool("Bash", "python -c 'from uploader.retry import delays; print(max(delays(10)))'", now - 9 * MIN)

        wf = "wf_review-482"
        journal = os.path.join(self._proj("uploader"), sid, "subagents", "workflows", wf, "journal.jsonl")
        _append(journal, [{"type": "launched"}])
        self.workflow = (journal, [])
        for j, label in enumerate(["review:correctness", "review:security", "review:performance", "verify:timeouts"]):
            aid = f"w{j}{label.split(':')[1][:5]}"
            agent = self._agent("uploader", sid, aid, {"description": label, "agentType": "workflow-subagent", "finished": j < 2}, cwd, workflow=wf)
            agent.prompt(f"{label} of the retry patch", now - 8 * MIN)
            agent.tool("Read", "src/uploader/retry.py", now - 7 * MIN)
            _append(journal, [{"type": "started", "agentId": aid, "label": label, "phase": label.split(":")[0].title()}])
            if j < 2:
                agent.say("No blocking issues.", now - 5 * MIN)
                _append(journal, [{"type": "result", "agentId": aid, "result": {"ok": True}}])
                _age(agent.path, 5 * MIN)
            else:
                self.live.append(agent)
        self.live += [s, tests, review, nested]

        # 2. Codex session with subagents and a fork
        cwd = "/home/dev/src/ci-config"
        t0 = now - 55 * MIN
        root_tid = _tid()
        cx = _Codex(self._codex_path(root_tid, t0), root_tid, t0, cwd, thread_source="user")
        cx.prompt("Move CI to the new runner pool and keep cache hit rate above 80%", t0)
        for c in ["rg -n 'runs-on' .gitlab-ci.yml", "git log -5 --oneline", "make lint"]:
            cx.run(c, t0 + 60)
        idx = os.path.join(self.codex_dir, "session_index.jsonl")
        _append(idx, [{"id": root_tid, "thread_name": "Migrate CI to the new runner pool"}])
        for nick, path, cmd, busy in [("Bohr", "/root/runner_audit", "rg -n tags: .gitlab-ci.yml", True),
                                      ("Curie", "/root/cache_keys", "rg -n 'cache:' -A3 .gitlab-ci.yml", False)]:
            tid = _tid()
            sub = _Codex(self._codex_path(tid, t0 + 120), tid, t0 + 120, cwd, thread_source="subagent",
                         parent_thread_id=root_tid, agent_nickname=nick, agent_path=path)
            sub.prompt(f"{path.split('/')[-1].replace('_', ' ')} for the runner migration", t0 + 120)
            sub.run(cmd, t0 + 180)
            if busy:
                self.live.append(sub)
            else:
                sub.done("Cache keys are runner-independent; nothing to change.", t0 + 600)
                _age(sub.path, 45 * MIN)
        self.live.append(cx)
        fork_tid = _tid()
        fork = _Codex(self._codex_path(fork_tid, now - 30 * MIN), fork_tid, now - 30 * MIN, cwd, thread_source="user", forked_from_id=root_tid)
        _append(idx, [{"id": fork_tid, "thread_name": "Migrate CI (try podman runners)"}])
        fork.prompt("Same, but try podman-based runners", now - 30 * MIN)
        fork.run("podman info", now - 29 * MIN)
        fork.done("Podman runners work but cold builds are 2x slower.", now - 22 * MIN)
        _age(fork.path, 22 * MIN)

        # 3. idle session with rewinds
        sid, s = self._session("dashboard", "/home/dev/src/dashboard")
        first = s.prompt("Why is the weekly report query slow?", now - 3 * HOUR)
        s.tool("Bash", "EXPLAIN ANALYZE SELECT ...", now - 3 * HOUR + 60)
        anchor = s.last
        s.prompt("Try an index on (team_id, week)", now - 2.5 * HOUR, parent=anchor)
        s.prompt("Try a materialized view instead", now - 2.4 * HOUR, parent=anchor)
        s.prompt("Compare both and pick one", now - 2.3 * HOUR, parent=anchor)
        s.say("The composite index wins: 2.1 s -> 40 ms.", now - 20 * MIN)
        s.title("Profile slow dashboard query")
        _age(s.path, 14 * MIN)
        del first

        # 4. errored session
        sid, s = self._session("gateway", "/home/dev/src/gateway")
        s.prompt("Fix the flaky websocket reconnect test", now - 26 * MIN)
        s.tool("Bash", "pytest tests/test_ws.py -x --count 20", now - 25 * MIN)
        s.say("API Error: overloaded", now - 24 * MIN, error=True)
        s.title("Fix flaky websocket test")
        _age(s.path, 24 * MIN)

        # 5. finished work from earlier
        for proj, title, ago, agents in [("uploader", "Release notes for v2.3", 5 * HOUR, 1),
                                         ("gateway", "Rename legacy config flags", 9 * HOUR, 3)]:
            sid, s = self._session(proj, f"/home/dev/src/{proj}", model="claude-sonnet-5-5")
            s.prompt(title, now - ago)
            s.tool("Bash", "git log v2.2..HEAD --oneline", now - ago + 60)
            s.say("Done.", now - ago + 600)
            s.title(title)
            for k in range(agents):
                a = self._agent(proj, sid, f"d{k}", {"description": f"Update call sites ({k + 1}/{agents})", "finished": True}, f"/home/dev/src/{proj}", model="claude-sonnet-5-5")
                a.prompt("Update call sites", now - ago + 120)
                a.tool("Edit", f"src/{proj}/config.py", now - ago + 180)
                a.say("Updated.", now - ago + 300)
                _age(a.path, ago - 300)
            _age(s.path, ago - 600)

    # -------------------------------------------------------------------- live
    def _tick(self, step: int) -> None:
        now = time.time()
        for w in random.sample(self.live, k=max(1, len(self.live) // 2)):
            if isinstance(w, _Claude):
                w.tool(*random.choice([("Bash", "pytest -q"), ("Read", "src/uploader/retry.py"), ("Grep", "backoff"),
                                       ("Edit", "src/uploader/retry.py")]), now)
            else:
                w.run(random.choice(["make test", "rg -n runner .gitlab-ci.yml", "git diff --stat"]), now)
        if step == 4:  # a new subagent appears a few seconds in
            cwd = "/home/dev/src/uploader"
            bench = self._agent("uploader", self.main_sid, "a5bench", {"description": "Benchmark retry overhead", "spawnDepth": 1}, cwd)
            bench.prompt("Measure the overhead retry adds on the happy path", now)
            bench.tool("Bash", "python -m pytest benchmarks/ -q", now)
            self.live.append(bench)

    def run(self, interval: float = 1.0) -> None:
        step = 0
        while not self._stop.wait(interval):
            step += 1
            try:
                self._tick(step)
            except OSError:
                pass

    def start(self) -> threading.Thread:
        th = threading.Thread(target=self.run, name="sagtop-demo", daemon=True)
        th.start()
        return th

    def stop(self) -> None:
        self._stop.set()
