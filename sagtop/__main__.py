"""python3 -m sagtop [--port 7878] [--host 127.0.0.1] [--window 24h] [--demo]"""

import argparse
import os
import tempfile

from .engine import WINDOWS, Engine
from .server import serve

G, D, R = "\033[32m", "\033[2m", "\033[0m"


def default_dirs(env=os.environ) -> tuple[str, str]:
    """Where Claude Code and Codex keep sessions, honouring their own overrides."""
    claude_home = env.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    codex_home = env.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    return os.path.join(claude_home, "projects"), codex_home


def main() -> None:
    claude_dir, codex_dir = default_dirs()
    ap = argparse.ArgumentParser(prog="sagtop", description="Live graph of Claude Code and Codex sessions")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7878)
    ap.add_argument("--window", default="24h", choices=list(WINDOWS))
    ap.add_argument("--claude-dir", default=claude_dir, help="default: $CLAUDE_CONFIG_DIR/projects or ~/.claude/projects")
    ap.add_argument("--codex-dir", default=codex_dir, help="default: $CODEX_HOME or ~/.codex")
    ap.add_argument("--demo", action="store_true", help="watch invented sessions instead of yours")
    args = ap.parse_args()

    if args.demo:
        from .demo import Demo

        demo = Demo(tempfile.mkdtemp(prefix="sagtop-demo-"))
        demo.start()
        args.claude_dir, args.codex_dir = demo.claude_dir, demo.codex_dir

    engine = Engine(args.claude_dir, args.codex_dir, window=args.window)
    engine.tick()  # first snapshot is complete before the first browser request
    engine.start()
    httpd = serve(engine, args.host, args.port)
    print(f"{G}sagtop{R} {D}▸{R} http://{args.host}:{args.port}  {D}({len(engine.published)} nodes, "
          f"cold scan {engine.last_tick_ms:.0f} ms, window {args.window}){R}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        engine.stop()


if __name__ == "__main__":
    main()
