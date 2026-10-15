"""Canary drill: ship a checker that fails 30% of requests and time the automatic rollback.

    python scripts/canary_drill.py --namespace evidence-gate --fault-rate 0.3

Keeps steady traffic flowing (k6 at 4 users), upgrades the release with
``checker.faultRate`` set, polls the Argo Rollouts status until the rollout aborts or
becomes healthy, then restores the good version. Writes runs/canary/results.json.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path


def sh(*cmd: str) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def rollout(ns: str) -> dict[str, object]:
    out = json.loads(
        sh("kubectl", "-n", ns, "get", "rollout", "evidence-gate-checker", "-o", "json")
    )
    status = out.get("status", {})
    return {
        "phase": status.get("phase"),
        "aborted": bool(status.get("abort")),
        "message": status.get("message", ""),
        "step": status.get("currentStepIndex"),
        "canary_weight": status.get("canary", {})
        .get("weights", {})
        .get("canary", {})
        .get("weight"),
    }


def helm(ns: str, values: str, fault: str, tag: str) -> None:
    # Helm 4 applies server-side; the chart owns the Rollout fields the controller touches.
    v4 = sh("helm", "version", "--short").startswith("v4")
    sh(
        "helm",
        "upgrade",
        "eg",
        "deploy/helm/evidence-gate",
        "-n",
        ns,
        "-f",
        values,
        "--set",
        f"checker.faultRate={fault}",
        "--set",
        f"image.tag={tag}",
        *(["--force-conflicts"] if v4 else []),
    )


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--namespace", default="evidence-gate")
    p.add_argument("--values", default="deploy/helm/evidence-gate/values-kind.yaml")
    p.add_argument("--fault-rate", default="0.3")
    p.add_argument("--tag", default="dev")
    p.add_argument("--timeout", type=int, default=900)
    args = p.parse_args()
    traffic = subprocess.Popen(
        ["k6", "run", "--quiet", "-e", "STAGES=4:20m", "loadtest/k6/check.js"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(30)
        started = time.time()
        helm(args.namespace, args.values, args.fault_rate, args.tag)
        timeline = []
        while time.time() - started < args.timeout:
            state = rollout(args.namespace)
            state["t"] = round(time.time() - started, 1)
            if not timeline or {k: v for k, v in state.items() if k != "t"} != {
                k: v for k, v in timeline[-1].items() if k != "t"
            }:
                timeline.append(state)
                print(state, flush=True)
            if state["aborted"] or (state["phase"] == "Healthy" and state["t"] > 30):
                break
            time.sleep(5)
        result = {
            "fault_rate": float(args.fault_rate),
            "outcome": "aborted" if timeline[-1]["aborted"] else str(timeline[-1]["phase"]),
            "seconds_to_outcome": timeline[-1]["t"],
            "max_canary_weight": max((s["canary_weight"] or 0) for s in timeline),
            "timeline": timeline,
        }
        out = Path("runs/canary/results.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2))
        print("wrote", out)
    finally:
        helm(args.namespace, args.values, "0", args.tag)
        traffic.terminate()


if __name__ == "__main__":
    main()
