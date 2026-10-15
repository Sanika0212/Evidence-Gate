"""What happens to a general-domain threshold on biomedical claims, and how many labels
does recalibration need to restore the guarantee?

    python eval/run_shift.py              (after run_detection.py)

Calibration source: RAGTruth train (general QA, summaries, data-to-text), scored per
answer (weakest sentence). Deployments: SciFact's 1,259 claim and abstract pairs with
expert labels, and MedHallu's 2,000 answers for contrast. The RAGTruth threshold for 90%
recall is applied to each target; then the threshold is refit on k target labels drawn
from one half and measured on the other half (100 draws per k). The PSI between the
source and target score distributions is what the drift monitor would have seen.
"""

from __future__ import annotations

from collections import defaultdict

import numpy as np
from common import cached, write

from evidencegate.calibration import Label, fit, psi
from evidencegate.evaluation import Scored, score_ragtruth
from evidencegate.onnx_checker import load_checker

TARGET = 0.9
KS = (25, 50, 100, 200, 400)
TARGET_SETS = ("scifact", "medhallu")


def answer_level(rows: list[Scored]) -> list[Scored]:
    """Sentence rows to answers: weakest support; unsupported if any sentence is."""
    by: dict[str, list[Scored]] = defaultdict(list)
    for r in rows:
        by[r.id.rsplit(":", 1)[0]].append(r)
    return [
        Scored(k, min(x.support for x in v), any(x.unsupported for x in v), v[0].group, len(v))
        for k, v in by.items()
    ]


def recalibration(s: np.ndarray, y: np.ndarray) -> list[dict[str, float | int | None]]:
    rng = np.random.default_rng(0)
    curve: list[dict[str, float | int | None]] = []
    for k in KS:
        recalls, fars, fitted_n = [], [], 0
        for _ in range(100):
            idx = rng.permutation(len(s))
            pool, test = idx[: len(s) // 2], idx[len(s) // 2 :]
            labelled = pool[:k]
            f = fit([Label(float(s[i]), bool(y[i])) for i in labelled], TARGET, min_unsupported=5)
            if f is None:
                continue
            fitted_n += 1
            fl = s[test] < f.support
            recalls.append(fl[y[test]].mean())
            fars.append(fl[~y[test]].mean())
        curve.append(
            {
                "labels": k,
                "fits": fitted_n,
                "recall_mean": round(float(np.mean(recalls)), 3) if recalls else None,
                "recall_p10": round(float(np.percentile(recalls, 10)), 3) if recalls else None,
                "false_alarm_mean": round(float(np.mean(fars)), 3) if fars else None,
            }
        )
        print(curve[-1], flush=True)
    return curve


def main() -> None:
    checker = load_checker("minicheck", "models")
    source = answer_level(
        cached(
            "ragtruth-train-900-minicheck-int8",
            lambda: score_ragtruth("train", checker, 300, seed=1),
        )
    )
    src = fit([Label(r.support, r.unsupported) for r in source], TARGET)
    assert src is not None
    results = {}
    for name in TARGET_SETS:
        rows = cached(f"{name}-minicheck-int8", lambda: [])
        if not rows:
            raise SystemExit("run eval/run_detection.py first")
        s = np.array([r.support for r in rows])
        y = np.array([r.unsupported for r in rows])
        flagged = s < src.support
        transfer = {
            "source_threshold": round(src.support, 4),
            "source_false_alarm": round(src.false_alarm_rate, 3),
            "recall_on_target": round(float(flagged[y].mean()), 3),
            "false_alarm_on_target": round(float(flagged[~y].mean()), 3),
            "psi_source_vs_target": round(psi([r.support for r in source], s), 3),
            "n": len(s),
        }
        print(name, transfer, flush=True)
        results[name] = {"transfer": transfer, "recalibration": recalibration(s, y)}
    print(
        "wrote",
        write("shift", {"target_recall": TARGET, "source_answers": len(source), "sets": results}),
    )


if __name__ == "__main__":
    main()
