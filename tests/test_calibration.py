from __future__ import annotations

import math

import numpy as np
import pytest
from groundsight.calibrate import conformal_threshold
from hypothesis import given, settings
from hypothesis import strategies as st

from evidencegate.calibration import (
    Calibrator,
    Label,
    MemoryCalibrationStore,
    drift,
    fit,
    ks,
    psi,
    weighted_conformal_threshold,
    weighted_recall,
)


@given(
    st.lists(st.floats(0, 1, allow_nan=False), min_size=5, max_size=200),
    st.floats(0.5, 0.95),
)
@settings(max_examples=60, deadline=None)
def test_unit_weights_match_split_conformal(scores: list[float], target: float) -> None:
    assert weighted_conformal_threshold(scores, [1.0] * len(scores), target) == conformal_threshold(
        scores, target
    )


def test_empty_or_too_small_calibration_flags_everything() -> None:
    assert weighted_conformal_threshold([], [], 0.9) == math.inf
    assert fit([Label(0.1, True)] * 5, 0.9, min_unsupported=10) is None


def test_guarantee_holds_on_fresh_draws() -> None:
    """Over many calibration draws, recall on new unsupported claims meets the target."""
    rng = np.random.default_rng(0)
    target, achieved = 0.9, []
    for _ in range(300):
        unsupported = rng.beta(2, 5, size=200)  # unsupported claims score low
        supported = rng.beta(5, 2, size=200)
        labels = [Label(float(s), True) for s in unsupported] + [
            Label(float(s), False) for s in supported
        ]
        fitted = fit(labels, target, min_unsupported=30)
        assert fitted is not None
        fresh = rng.beta(2, 5, size=2000)
        achieved.append(float((fresh < fitted.support).mean()))
    assert np.mean(achieved) >= target - 0.005
    assert 0 < fitted.false_alarm_rate < 1


def test_weighting_removes_review_selection_bias() -> None:
    """Reviewers see every flagged claim but only a 5% sample of unflagged ones."""
    rng = np.random.default_rng(1)
    support = rng.beta(2, 4, size=20_000)
    threshold, audit_rate = 0.25, 0.05
    flagged = support < threshold
    true_recall = flagged.mean()
    labels = []
    for s, f in zip(support, flagged, strict=True):
        if f:
            labels.append(Label(float(s), True, True, "review", 1.0))
        elif rng.random() < audit_rate:
            labels.append(Label(float(s), True, False, "audit", 1 / audit_rate))
    naive = np.mean([lab.flagged for lab in labels])
    est, lo, hi, _, _ = weighted_recall(labels)
    assert naive > true_recall + 0.3  # the naive estimate is badly biased upward
    assert abs(est - true_recall) < 0.03
    assert lo <= true_recall <= hi


def test_psi_and_ks_detect_shift() -> None:
    rng = np.random.default_rng(2)
    base = rng.beta(2, 5, 3000)
    same = rng.beta(2, 5, 3000)
    shifted = rng.beta(4, 3, 3000)
    assert psi(base, same) < 0.05
    assert ks(base, same) < 0.05
    assert psi(base, shifted) > 0.25
    assert ks(base, shifted) > 0.2
    assert psi([], [0.1]) == 0.0
    assert ks([], [0.1]) == 0.0


def test_drift_report_reasons() -> None:
    rng = np.random.default_rng(3)
    calib = list(rng.beta(2, 5, 1000))
    ok = drift(calib, list(rng.beta(2, 5, 500)), [], 0.9)
    assert not ok.needs_recalibration
    assert ok.reason == "within tolerance"
    missed = [Label(0.6, True, False, "audit", 1.0) for _ in range(40)]
    bad = drift(calib, list(rng.beta(4, 3, 500)), missed, 0.9)
    assert bad.needs_recalibration
    assert "PSI" in bad.reason
    assert "recall" in bad.reason


def test_calibrator_lifecycle() -> None:
    cal = Calibrator(MemoryCalibrationStore(0.9), audit_rate=0.1)
    default = cal.thresholds("t")
    assert default.target_recall == 0.0
    assert default.calibration.startswith("default")
    fitted, reason = cal.fit("t", 0.9)
    assert fitted is None
    assert "need at least" in reason
    rng = np.random.default_rng(4)
    for s in rng.beta(2, 5, 200):
        cal.add_label("t", float(s), True, False, "seed")
    for s in rng.beta(5, 2, 200):
        cal.add_label("t", float(s), False, False, "seed")
    label = cal.add_label("t", 0.7, True, False, "audit")
    assert label.weight == pytest.approx(10.0)
    fitted, reason = cal.fit("t", 0.95)
    assert fitted is not None
    assert fitted.version == 1
    assert reason == "fitted"
    assert cal.thresholds("t").support == fitted.support
    assert cal.store.target_recall("t") == 0.95
    cal.store.record_scores("t", list(rng.beta(2, 5, 300)))
    report = cal.drift("t")
    assert report.live_n == 300
