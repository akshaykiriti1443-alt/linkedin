"""The desktop path of the journey: members_worker + the REAL Claude Code CLI (`claude -p`, this machine's login).

    REELVAULT_HOME=DIR python journey_desktop.py --server http://localhost:9301 --code XXXX-XXXX [--max-jobs 6]

Pairs exactly like the desktop app (members_worker.pair with the code the web UI shows), then runs Worker.tick()
until no job is left (or --max-jobs). Prints one JSON line per job and a final SUMMARY line.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", required=True)
    ap.add_argument("--code", required=True)
    ap.add_argument("--max-jobs", type=int, default=6)
    ap.add_argument("--idle-s", type=int, default=90)
    a = ap.parse_args()
    from reelvault import members_worker as mw
    from reelvault.brain import find_claude

    out = {"claude": find_claude(), "paired": None, "jobs": []}
    out["paired"] = mw.pair(a.server, a.code, "Journey PC")
    w = mw.Worker()
    idle_until = time.time() + a.idle_s
    while len(out["jobs"]) < a.max_jobs and time.time() < idle_until:
        t0 = time.time()
        rec = w.tick()
        if rec is None:
            time.sleep(5)
            continue
        rec = {k: v for k, v in rec.items() if k in ("kind", "ok", "schema_file", "model", "error", "job_id")}
        rec["seconds"] = round(time.time() - t0, 1)
        print(json.dumps(rec, default=str), flush=True)
        out["jobs"].append(rec)
        idle_until = time.time() + a.idle_s
    print("SUMMARY " + json.dumps(out, default=str), flush=True)
    return 0 if out["jobs"] and all(j.get("ok") for j in out["jobs"]) else 1


if __name__ == "__main__":
    sys.exit(main())
