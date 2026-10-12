"""Helpers shared by the evaluation scripts: cached scoring, bootstrap intervals, output."""

from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from evidencegate.evaluation import Scored, auroc

RUNS = Path("runs")


def cached(name: str, fn: Any) -> list[Scored]:
    """Score once and keep the per-item scores in ``runs/scores/<name>.json``."""
    path = RUNS / "scores" / f"{name}.json"
    if path.exists():
        return [Scored(**r) for r in json.loads(path.read_text())]
    started = time.time()
    rows = fn()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(r) for r in rows]))
    print(f"  scored {name}: {len(rows)} items in {time.time() - started:.0f}s", flush=True)
    return rows


def bootstrap_auroc(rows: list[Scored], n: int = 1000, seed: int = 0) -> tuple[float, float, float]:
    s = np.array([r.support for r in rows])
    y = np.array([r.unsupported for r in rows])
    rng = np.random.default_rng(seed)
    stats = []
    for _ in range(n):
        idx = rng.integers(0, len(s), len(s))
        if y[idx].all() or not y[idx].any():
            continue
        stats.append(auroc(s[idx], y[idx]))
    return auroc(s, y), float(np.percentile(stats, 2.5)), float(np.percentile(stats, 97.5))


def write(name: str, payload: Any) -> Path:
    path = RUNS / name / "results.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))
    return path
