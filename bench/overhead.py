"""Measure sagtop's own overhead: cold start, steady-state CPU and memory.

    python3 bench/overhead.py [--window 24h] [--seconds 30] [--runs 3] [-- extra sagtop args]

Starts `python3 -m sagtop` on a free port, waits for the first snapshot, then samples
/proc/<pid>/stat and /proc/<pid>/status. CPU is utime+stime of the server process
(all threads) divided by wall time; RSS is sampled every second.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import statistics
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TICK = os.sysconf("SC_CLK_TCK")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def cpu_seconds(pid: int) -> float:
    with open(f"/proc/{pid}/stat") as fh:
        fields = fh.read().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / TICK  # utime, stime


def rss_kb(pid: int) -> int:
    with open(f"/proc/{pid}/status") as fh:
        for line in fh:
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    return 0


def one_run(cmd: list[str], seconds: float) -> dict:
    port = free_port()
    t0 = time.perf_counter()
    proc = subprocess.Popen(cmd + ["--port", str(port)], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        while True:  # ready = first snapshot served
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/snapshot", timeout=1) as r:
                    snap = json.load(r)
                break
            except OSError:
                if proc.poll() is not None:
                    raise RuntimeError(proc.stderr.read().decode())
                time.sleep(0.01)
        ready = time.perf_counter() - t0
        cpu_ready = cpu_seconds(proc.pid)
        rss_samples = []
        c0, w0 = cpu_seconds(proc.pid), time.perf_counter()
        end = w0 + seconds
        while time.perf_counter() < end:
            time.sleep(1)
            rss_samples.append(rss_kb(proc.pid))
        c1, w1 = cpu_seconds(proc.pid), time.perf_counter()
        return {
            "nodes": len(snap["nodes"]),
            "ready_s": ready,
            "cpu_to_ready_s": cpu_ready,
            "steady_cpu_pct": 100 * (c1 - c0) / (w1 - w0),
            "rss_mb": max(rss_samples) / 1024,
        }
    finally:
        proc.terminate()
        proc.wait(5)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", default="24h")
    ap.add_argument("--seconds", type=float, default=30)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--cmd", default=f"{sys.executable} -m sagtop", help="server command (port is appended)")
    ap.add_argument("extra", nargs="*")
    a = ap.parse_args()
    cmd = a.cmd.split() + ["--window", a.window] + a.extra
    runs = [one_run(cmd, a.seconds) for _ in range(a.runs)]
    keys = ["nodes", "ready_s", "cpu_to_ready_s", "steady_cpu_pct", "rss_mb"]
    print(f"cmd: {' '.join(cmd)}  window={a.window}  {a.runs} runs x {a.seconds:.0f}s")
    for k in keys:
        vals = [r[k] for r in runs]
        spread = f"  (min {min(vals):.3g}, max {max(vals):.3g})" if len(vals) > 1 else ""
        print(f"  {k:16} median {statistics.median(vals):.3g}{spread}")


if __name__ == "__main__":
    main()
