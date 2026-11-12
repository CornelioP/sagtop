"""Total CPU of one cold scan, including parallel workers.

    python3 bench/cold_cpu.py [window] [--serial]
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def proc_cpu(pid: int, children: bool = False) -> float:
    with open(f"/proc/{pid}/stat") as fh:
        f = fh.read().rsplit(")", 1)[1].split()
    ticks = int(f[11]) + int(f[12]) + ((int(f[13]) + int(f[14])) if children else 0)
    return ticks / os.sysconf("SC_CLK_TCK")


def main() -> None:
    from sagtop.__main__ import default_dirs
    from sagtop.engine import Engine

    window = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "24h"
    e = Engine(*default_dirs(), window=window, watch=False, parallel="--serial" not in sys.argv)
    c0, t0 = time.process_time(), time.perf_counter()
    e.tick()
    wall = time.perf_counter() - t0
    own = time.process_time() - c0
    workers = 0.0
    try:
        from multiprocessing import forkserver

        pid = forkserver._forkserver._forkserver_pid
        if pid:
            workers = proc_cpu(pid, children=True)  # forkserver itself + reaped workers
    except Exception:
        pass
    print(f"window={window} nodes={len(e.published)} wall={wall:.2f}s cpu_main={own:.2f}s cpu_workers={workers:.2f}s total={own + workers:.2f}s")


if __name__ == "__main__":
    main()
