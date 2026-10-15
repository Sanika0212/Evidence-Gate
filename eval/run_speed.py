"""How fast is each exported checker? Pairs per second, int8 vs fp32, on this machine's CPU.

    python eval/run_speed.py

Scores the same 128 (passage window, claim) pairs from SciFact with each model, at 1 and
4 ONNX threads, after a warm-up pass and with the score cache cleared between repeats.
Run it on an otherwise idle machine: the timings in run_detection.py overlapped other
jobs and are not comparable.
"""

from __future__ import annotations

import os
import platform
import random
import time

from common import write

from evidencegate.datasets import scifact
from evidencegate.onnx_checker import OnnxPairClassifier

MODELS = ["minicheck", "minicheck-fp32", "nli", "nli-fp32"]
THREADS = (1, 4)
REPEATS = 3


def pairs(n: int = 128, seed: int = 0) -> list[tuple[str, str]]:
    from groundsight.text import windows

    items = random.Random(seed).sample(scifact(), n)
    out = []
    for item in items:
        first = windows(item.passages[0], max_words=250)[0]
        out.append((first.text, item.answer))
    return out


def main() -> None:
    data = pairs()
    results = []
    for name in MODELS:
        for threads in THREADS:
            model = OnnxPairClassifier(os.path.join("models", name), threads=threads)
            model.score(data[:16])  # load and warm up
            times = []
            for _ in range(REPEATS):
                model._cache.clear()
                started = time.perf_counter()
                model.score(data)
                times.append(time.perf_counter() - started)
            best = min(times)
            size = os.path.getsize(os.path.join("models", name, "model.onnx"))
            results.append(
                {
                    "model": name,
                    "threads": threads,
                    "pairs": len(data),
                    "pairs_per_second": round(len(data) / best, 2),
                    "ms_per_pair": round(1000 * best / len(data), 1),
                    "model_mb": round(size / 1e6),
                }
            )
            print(results[-1], flush=True)
    print(
        "wrote",
        write(
            "speed",
            {"machine": f"{platform.machine()} {platform.processor()}", "results": results},
        ),
    )


if __name__ == "__main__":
    main()
