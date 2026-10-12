"""Does the recall guarantee hold, and does weighting fix reviewer selection bias?

    python eval/run_guarantee.py          (after run_detection.py has cached scores)

1. **Guarantee.** MedHallu answers scored by the int8 MiniCheck checker are split at
   random into calibration and test halves, 200 times. For each target recall the
   conformal threshold is fitted on the calibration half; recall and false-alarm rate
   are measured on the test half.
2. **Review-queue simulation.** The same real scores replayed as traffic through a gate
   at a 90% threshold: reviewers label every flagged answer plus a 2% random audit of
   passed ones. Recall estimated naively from the labels is compared with the
   inverse-probability weighted estimate and with the truth.
"""

from __future__ import annotations

import numpy as np
from common import cached, write

from evidencegate.calibration import Label, fit, weighted_recall

TARGETS = (0.8, 0.9, 0.95)


def main() -> None:
    rows = cached("medhallu-minicheck-int8", lambda: [])
    if not rows:
        raise SystemExit("run eval/run_detection.py first")
    s = np.array([r.support for r in rows])
    y = np.array([r.unsupported for r in rows])
    rng = np.random.default_rng(0)
    guarantee = []
    for target in TARGETS:
        recalls, fars, thresholds = [], [], []
        for _ in range(200):
            idx = rng.permutation(len(s))
            cal, test = idx[: len(s) // 2], idx[len(s) // 2 :]
            fitted = fit([Label(float(s[i]), bool(y[i])) for i in cal], target, min_unsupported=30)
            assert fitted is not None
            flagged = s[test] < fitted.support
            recalls.append(flagged[y[test]].mean())
            fars.append(flagged[~y[test]].mean())
            thresholds.append(fitted.support)
        guarantee.append(
            {
                "target": target,
                "recall_mean": round(float(np.mean(recalls)), 3),
                "recall_p5": round(float(np.percentile(recalls, 5)), 3),
                "false_alarm_mean": round(float(np.mean(fars)), 3),
                "threshold_mean": round(float(np.mean(thresholds)), 3),
                "share_of_splits_meeting_target": round(
                    float(np.mean(np.array(recalls) >= target)), 3
                ),
            }
        )
        print(guarantee[-1], flush=True)

    # Review-queue simulation on the real scores.
    fitted = fit([Label(float(a), bool(b)) for a, b in zip(s, y, strict=True)], 0.9)
    assert fitted is not None
    sims = []
    for seed in range(200):
        r = np.random.default_rng(seed)
        order = r.permutation(len(s))
        flagged = s[order] < fitted.support
        labels = []
        for i, f in zip(order, flagged, strict=True):
            if f:
                labels.append(Label(float(s[i]), bool(y[i]), True, "review", 1.0))
            elif r.random() < 0.02:
                labels.append(Label(float(s[i]), bool(y[i]), False, "audit", 50.0))
        truth = flagged[y[order]].mean()
        naive = np.mean([lab.flagged for lab in labels if lab.unsupported])
        est, lo, hi, _, _ = weighted_recall(labels)
        sims.append((truth, naive, est, lo <= truth <= hi))
    sim = np.array(sims, dtype=float)
    review = {
        "threshold": round(fitted.support, 4),
        "true_recall": round(float(sim[:, 0].mean()), 3),
        "naive_estimate_mean": round(float(sim[:, 1].mean()), 3),
        "weighted_estimate_mean": round(float(sim[:, 2].mean()), 3),
        "weighted_abs_error_mean": round(float(np.abs(sim[:, 2] - sim[:, 0]).mean()), 3),
        "interval_coverage": round(float(sim[:, 3].mean()), 3),
        "audit_rate": 0.02,
        "replays": len(sims),
    }
    print(review)
    print(
        "wrote", write("guarantee", {"guarantee": guarantee, "review_queue": review, "n": len(s)})
    )


if __name__ == "__main__":
    main()
