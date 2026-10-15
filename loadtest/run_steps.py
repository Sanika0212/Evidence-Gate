"""Run k6 at fixed concurrency levels and summarise throughput and latency per level.

    python loadtest/run_steps.py --levels 2 4 8 16 32 --duration 60s \
        --base http://localhost:8090 --token-url http://localhost:8081/oauth2/token \
        --where "kind on an Apple M4 laptop (Colima VM, 4 vCPU)"

Writes runs/<out>/results.json (default runs/load/) for scripts/make_figures.py.
``--replicas`` only labels the results; pin the checker first, for example with
``helm upgrade ... --set checker.hpa.enabled=false --set checker.replicas=2``.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).parent / "k6" / "check.js"


def audits_per_pod(prometheus: str, duration: str) -> dict[str, int]:
    """Checker audits per pod over the level just run (from the pods' own metrics)."""
    import urllib.parse
    import urllib.request

    query = (
        "sum by (pod) (increase(eg_stage_duration_seconds_count"
        f'{{stage="checker_audit"}}[{duration}]))'
    )
    url = f"{prometheus}/api/v1/query?" + urllib.parse.urlencode({"query": query})
    with urllib.request.urlopen(url, timeout=10) as r:
        result = json.load(r)["data"]["result"]
    return {
        row["metric"].get("pod", "?"): round(float(row["value"][1]))
        for row in result
        if float(row["value"][1]) >= 0.5
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--levels", type=int, nargs="+", default=[2, 4, 8, 16, 32])
    p.add_argument("--duration", default="60s")
    p.add_argument("--base", default="http://localhost:8090")
    p.add_argument("--token-url", default="http://localhost:8081/oauth2/token")
    p.add_argument("--where", default="")
    p.add_argument("--replicas", type=int, default=0, help="checker replicas, recorded only")
    p.add_argument("--cached", action="store_true", help="identical requests (score cache hits)")
    p.add_argument("--out", default="load")
    p.add_argument(
        "--prometheus",
        default="",
        help="Prometheus URL: also record how many audits each checker pod served per level",
    )
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
                "-e",
                f"CACHED={1 if args.cached else 0}",
                str(SCRIPT),
            ],
            check=False,
        )
        m = json.loads(Path(summary).read_text())["metrics"]
        per_pod = audits_per_pod(args.prometheus, args.duration) if args.prometheus else None
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
                "audits_per_checker_pod": per_pod,
            }
        )
        print(steps[-1], flush=True)
    out = Path("runs") / args.out / "results.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "where": args.where,
                "checker_replicas": args.replicas or None,
                "cached": args.cached,
                "duration": args.duration,
                "steps": steps,
            },
            indent=2,
        )
    )
    print("wrote", out)


if __name__ == "__main__":
    main()
