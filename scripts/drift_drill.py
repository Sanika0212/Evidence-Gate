"""Drift drill: normal traffic, then a simulated retrieval outage; time the drift alert.

    python scripts/drift_drill.py --requests 150 --prometheus http://localhost:9090

1. Clears the tenant's live support scores (telemetry from earlier load tests, not labels;
   the calibration is untouched).
2. Normal traffic: SciFact claims checked against the abstracts they cite, the same kind of
   data the demo tenant was calibrated on. Runs the recalibrate job and reads the PSI.
3. Outage: the same claims checked against another claim's abstract, as if the retriever
   returned the wrong documents. Runs the recalibrate job again.
4. Polls Prometheus until the SupportScoresDrifted alert fires (the rule holds for 30
   minutes) and writes runs/drift/results.json.
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

ALERT = "SupportScoresDrifted"


def sh(*cmd: str) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def prom(base: str, path: str, **params: str) -> Any:
    url = f"{base}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.load(r)["data"]


def psi(base: str, tenant: str) -> float | None:
    rows = prom(base, "/api/v1/query", query=f'eg_drift_psi{{tenant="{tenant}"}}')["result"]
    return round(float(rows[0]["value"][1]), 3) if rows else None


def alert_state(base: str) -> str:
    alerts = prom(base, "/api/v1/alerts")["alerts"]
    states = [a["state"] for a in alerts if a["labels"].get("alertname") == ALERT]
    return "firing" if "firing" in states else ("pending" if states else "inactive")


def recalibrate(ns: str) -> None:
    job = f"drift-drill-{int(time.time())}"
    sh("kubectl", "-n", ns, "create", "job", job, "--from=cronjob/evidence-gate-recalibrate")
    sh("kubectl", "-n", ns, "wait", "--for=condition=complete", f"job/{job}", "--timeout=300s")


def send(base: str, token_url: str, bodies: list[dict[str, Any]], workers: int) -> dict[str, int]:
    token = httpx.post(
        token_url,
        data={"grant_type": "client_credentials"},
        auth=("demo-client", "demo-secret"),
        timeout=10,
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    def one(body: dict[str, Any]) -> int:
        r = httpx.post(f"{base}/v1/check", json=body, headers=headers, timeout=120)
        return r.status_code

    with ThreadPoolExecutor(workers) as pool:
        codes = list(pool.map(one, bodies))
    return {str(c): codes.count(c) for c in sorted(set(codes))}


def main() -> None:
    from evidencegate.datasets import scifact

    p = argparse.ArgumentParser()
    p.add_argument("--namespace", default="evidence-gate")
    p.add_argument("--tenant", default="demo")
    p.add_argument("--requests", type=int, default=150)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--base", default="http://localhost:8090")
    p.add_argument("--token-url", default="http://localhost:8081/oauth2/token")
    p.add_argument("--prometheus", default="http://localhost:9090")
    p.add_argument("--timeout-minutes", type=float, default=45)
    args = p.parse_args()

    items = random.Random(0).sample(scifact(), args.requests)
    normal = [
        {"answer": it.answer, "passages": [{"id": f"doc{i}", "text": it.passages[0]}]}
        for i, it in enumerate(items)
    ]
    outage = [
        {"answer": it.answer, "passages": [{"id": f"doc{i}", "text": items[i - 1].passages[0]}]}
        for i, it in enumerate(items)
    ]

    sh(
        "kubectl",
        "-n",
        args.namespace,
        "exec",
        "evidence-gate-postgres-0",
        "--",
        "psql",
        "-U",
        "evidencegate",
        "-d",
        "evidencegate",
        "-c",
        f"DELETE FROM calibrator.live_scores WHERE tenant = '{args.tenant}'",
    )
    result: dict[str, Any] = {"tenant": args.tenant, "requests_per_phase": args.requests}
    for phase, bodies in (("normal", normal), ("outage", outage)):
        started = time.time()
        codes = send(args.base, args.token_url, bodies, args.workers)
        recalibrate(args.namespace)
        time.sleep(75)  # a scrape and a rule evaluation
        result[phase] = {
            "status_codes": codes,
            "seconds": round(time.time() - started),
            "psi": psi(args.prometheus, args.tenant),
            "alert": alert_state(args.prometheus),
        }
        print(phase, result[phase], flush=True)

    started = time.time()
    timeline: list[dict[str, Any]] = []
    while time.time() - started < args.timeout_minutes * 60:
        state = alert_state(args.prometheus)
        if not timeline or timeline[-1]["alert"] != state:
            timeline.append({"alert": state, "t": round(time.time() - started)})
            print(timeline[-1], flush=True)
        if state == "firing":
            break
        time.sleep(30)
    result["alert_timeline_after_outage"] = timeline
    result["outcome"] = timeline[-1]["alert"] if timeline else "unknown"
    out = Path("runs/drift/results.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2))
    print("wrote", out)


if __name__ == "__main__":
    main()
