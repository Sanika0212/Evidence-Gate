"""Run k6 at fixed concurrency levels and summarise throughput and latency per level.

    python loadtest/run_steps.py --levels 2 4 8 16 32 --duration 60s \
        --base http://localhost:8090 --token-url http://localhost:8081/oauth2/token \
        --where "kind on an Apple M4 laptop (Colima VM, 4 vCPU)"

Writes runs/load/results.json for scripts/make_figures.py.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).parent / "k6" / "check.js"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--levels", type=int, nargs="+", default=[2, 4, 8, 16, 32])
    p.add_argument("--duration", default="60s")
    p.add_argument("--base", default="http://localhost:8090")
    p.add_argument("--token-url", default="http://localhost:8081/oauth2/token")
    p.add_argument("--where", default="")
    args = p.parse_args()
    steps = []
    for vus in args.levels:
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as fh:
            summary = fh.name
        subprocess.run(
            [
                "k6",
                "run",
                "--quiet",
                "--summary-export",
                summary,
                "-e",
                f"BASE={args.base}",
                "-e",
                f"TOKEN_URL={args.token_url}",
                "-e",
                f"STAGES={vus}:10s,{vus}:{args.duration}",
                str(SCRIPT),
            ],
            check=False,
        )
        m = json.loads(Path(summary).read_text())["metrics"]
        dur = m["http_req_duration{endpoint:check}"]
        reqs = m["http_reqs"]
        steps.append(
            {
                "vus": vus,
                "rps": round(reqs["rate"], 2),
                "p50_ms": round(dur["med"], 1),
                "p95_ms": round(dur["p(95)"], 1),
                "p99_ms": round(dur["p(99)"], 1),
                "error_rate": round(m["http_req_failed"]["value"], 4),
                "requests": reqs["count"],
            }
        )
        print(steps[-1], flush=True)
    out = Path("runs/load/results.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"where": args.where, "duration": args.duration, "steps": steps}, indent=2)
    )
    print("wrote", out)


if __name__ == "__main__":
    main()
