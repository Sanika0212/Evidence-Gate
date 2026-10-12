"""Per-tenant thresholds with a recall guarantee, and drift detection.

**Fitting.** A tenant asks for a recall target, for example "flag at least 90% of
unsupported claims". The threshold is the split conformal quantile of the support
scores of labelled unsupported claims (Groundsight's :func:`conformal_threshold`): if
new unsupported claims are exchangeable with the labelled ones, each is flagged with
probability at least the target.

**Selection bias in reviewer labels.** In production, labels come from a review queue
that holds every flagged claim plus a small random sample of unflagged ones (the
audit sample). Unsupported claims that slipped through are therefore under-represented
by a known factor. Every label carries an inverse-probability weight (1 for flagged
claims and seed labels, ``1 / audit_rate`` for audited ones), and both the recall
estimate and the refit use weighted quantiles, which removes the bias in expectation.

**Drift.** Two signals, either of which asks for recalibration:

1. Recall measured on recent weighted labels is clearly below target (the upper end
   of its 95% interval is under the target).
2. The live support scores no longer look like the calibration scores (population
   stability index above 0.25, the conventional "significant shift" level).
"""

from __future__ import annotations

import math
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np
from groundsight.calibrate import conformal_threshold
from groundsight.metrics import wilson_interval

PSI_SHIFT = 0.25


@dataclass
class Label:
    support: float
    unsupported: bool
    flagged: bool = False
    source: str = "seed"  # "seed", "review" or "audit"
    weight: float = 1.0
    created: float = field(default_factory=time.time)


@dataclass
class FittedThresholds:
    support: float
    target_recall: float
    n_unsupported: int
    n_supported: int
    false_alarm_rate: float
    calibration: str
    version: int = 0
    fitted_at: float = field(default_factory=time.time)
    contradiction: float = 0.5


@dataclass
class DriftReport:
    psi: float
    ks: float
    live_n: int
    labelled_unsupported: int
    labelled_flagged: int
    recall_estimate: float
    recall_lower: float
    recall_upper: float
    needs_recalibration: bool
    reason: str


def weighted_conformal_threshold(
    scores: Sequence[float], weights: Sequence[float], target_recall: float
) -> float:
    """Smallest ``t`` such that the weighted share of ``scores < t`` reaches the target.

    With unit weights this is exactly the split conformal threshold. The finite-sample
    correction adds the weight a new point would carry (the mean weight) to the total.
    """
    s = np.asarray(scores, dtype=float)
    w = np.asarray(weights, dtype=float)
    if len(s) == 0:
        return math.inf
    if np.allclose(w, w[0]):
        return float(conformal_threshold(s, target_recall))
    order = np.argsort(s)
    s, w = s[order], w[order]
    total = w.sum() + w.mean()
    cum = np.cumsum(w) / total
    idx = int(np.searchsorted(cum, target_recall, side="left"))
    if idx >= len(s):
        return math.inf
    return float(np.nextafter(s[idx], math.inf))


def fit(
    labels: Sequence[Label],
    target_recall: float,
    *,
    min_unsupported: int = 30,
    calibration: str = "labels",
) -> FittedThresholds | None:
    """Fit the support threshold for ``target_recall``; ``None`` if there are too few labels."""
    pos = [lab for lab in labels if lab.unsupported]
    neg = [lab for lab in labels if not lab.unsupported]
    if len(pos) < min_unsupported:
        return None
    threshold = weighted_conformal_threshold(
        [p.support for p in pos], [p.weight for p in pos], target_recall
    )
    if not math.isfinite(threshold):
        return None
    neg_w = np.array([n.weight for n in neg], dtype=float)
    neg_flag = np.array([n.support < threshold for n in neg], dtype=float)
    far = float((neg_w * neg_flag).sum() / neg_w.sum()) if len(neg) else 0.0
    return FittedThresholds(
        support=threshold,
        target_recall=target_recall,
        n_unsupported=len(pos),
        n_supported=len(neg),
        false_alarm_rate=far,
        calibration=calibration,
    )


def psi(expected: Sequence[float], actual: Sequence[float], bins: int = 10) -> float:
    """Population stability index over quantile bins of ``expected``."""
    e = np.asarray(expected, dtype=float)
    a = np.asarray(actual, dtype=float)
    if len(e) < bins or len(a) == 0:
        return 0.0
    edges = np.unique(np.quantile(e, np.linspace(0, 1, bins + 1)))
    edges[0], edges[-1] = -np.inf, np.inf
    pe = np.histogram(e, edges)[0] / len(e)
    pa = np.histogram(a, edges)[0] / len(a)
    pe = np.clip(pe, 1e-4, None)
    pa = np.clip(pa, 1e-4, None)
    return float(np.sum((pa - pe) * np.log(pa / pe)))


def ks(expected: Sequence[float], actual: Sequence[float]) -> float:
    """Two-sample Kolmogorov-Smirnov statistic (largest gap between the two ECDFs)."""
    e = np.sort(np.asarray(expected, dtype=float))
    a = np.sort(np.asarray(actual, dtype=float))
    if len(e) == 0 or len(a) == 0:
        return 0.0
    grid = np.concatenate([e, a])
    cdf_e = np.searchsorted(e, grid, side="right") / len(e)
    cdf_a = np.searchsorted(a, grid, side="right") / len(a)
    return float(np.max(np.abs(cdf_e - cdf_a)))


def weighted_recall(labels: Sequence[Label]) -> tuple[float, float, float, int, int]:
    """Recall of flagging on labelled unsupported claims, inverse-probability weighted.

    Returns ``(estimate, lower, upper, n_unsupported, n_flagged)``; the interval is a
    Wilson interval on the effective (Kish) sample size.
    """
    pos = [lab for lab in labels if lab.unsupported]
    if not pos:
        return 0.0, 0.0, 1.0, 0, 0
    w = np.array([p.weight for p in pos], dtype=float)
    hit = np.array([p.flagged for p in pos], dtype=float)
    est = float((w * hit).sum() / w.sum())
    n_eff = float(w.sum() ** 2 / (w**2).sum())
    k = round(est * n_eff)
    lo, hi = wilson_interval(k, max(1, round(n_eff)))
    return est, lo, hi, len(pos), int(hit.sum())


def drift(
    calibration_scores: Sequence[float],
    live_scores: Sequence[float],
    recent_labels: Sequence[Label],
    target_recall: float,
    *,
    min_labels: int = 20,
) -> DriftReport:
    p = psi(calibration_scores, live_scores)
    k = ks(calibration_scores, live_scores)
    est, lo, hi, n_pos, n_flag = weighted_recall(recent_labels)
    reasons = []
    if n_pos >= min_labels and hi < target_recall:
        reasons.append(
            f"recall on reviewer labels is below target ({est:.2f}, 95% CI up to {hi:.2f})"
        )
    if p > PSI_SHIFT:
        reasons.append(f"live support scores shifted (PSI {p:.2f} > {PSI_SHIFT})")
    return DriftReport(
        psi=p,
        ks=k,
        live_n=len(live_scores),
        labelled_unsupported=n_pos,
        labelled_flagged=n_flag,
        recall_estimate=est,
        recall_lower=lo,
        recall_upper=hi,
        needs_recalibration=bool(reasons),
        reason="; ".join(reasons) or "within tolerance",
    )


# --------------------------------------------------------------------------- stores


class CalibrationStore(Protocol):
    def target_recall(self, tenant: str) -> float: ...

    def set_target_recall(self, tenant: str, target: float) -> None: ...

    def add_label(self, tenant: str, label: Label) -> None: ...

    def labels(self, tenant: str, since: float | None = None) -> list[Label]: ...

    def latest(self, tenant: str) -> FittedThresholds | None: ...

    def save(self, tenant: str, fitted: FittedThresholds) -> FittedThresholds: ...

    def record_scores(self, tenant: str, scores: Sequence[float]) -> None: ...

    def live_scores(self, tenant: str, limit: int) -> list[float]: ...


class MemoryCalibrationStore:
    def __init__(self, default_target: float = 0.9) -> None:
        self.default_target = default_target
        self._targets: dict[str, float] = {}
        self._labels: dict[str, list[Label]] = {}
        self._fits: dict[str, list[FittedThresholds]] = {}
        self._live: dict[str, list[float]] = {}

    def target_recall(self, tenant: str) -> float:
        return self._targets.get(tenant, self.default_target)

    def set_target_recall(self, tenant: str, target: float) -> None:
        self._targets[tenant] = target

    def add_label(self, tenant: str, label: Label) -> None:
        self._labels.setdefault(tenant, []).append(label)

    def labels(self, tenant: str, since: float | None = None) -> list[Label]:
        rows = self._labels.get(tenant, [])
        return [r for r in rows if since is None or r.created >= since]

    def latest(self, tenant: str) -> FittedThresholds | None:
        fits = self._fits.get(tenant)
        return fits[-1] if fits else None

    def save(self, tenant: str, fitted: FittedThresholds) -> FittedThresholds:
        fits = self._fits.setdefault(tenant, [])
        fitted.version = len(fits) + 1
        fits.append(fitted)
        return fitted

    def record_scores(self, tenant: str, scores: Sequence[float]) -> None:
        self._live.setdefault(tenant, []).extend(float(s) for s in scores)

    def live_scores(self, tenant: str, limit: int) -> list[float]:
        return self._live.get(tenant, [])[-limit:]


SCHEMA = """
CREATE SCHEMA IF NOT EXISTS calibrator;
CREATE TABLE IF NOT EXISTS calibrator.tenants (
    tenant         text PRIMARY KEY,
    target_recall  double precision NOT NULL
);
CREATE TABLE IF NOT EXISTS calibrator.labels (
    id          bigserial PRIMARY KEY,
    tenant      text NOT NULL,
    support     double precision NOT NULL,
    unsupported boolean NOT NULL,
    flagged     boolean NOT NULL,
    source      text NOT NULL,
    weight      double precision NOT NULL,
    created     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS labels_tenant ON calibrator.labels (tenant, created);
CREATE TABLE IF NOT EXISTS calibrator.thresholds (
    tenant           text NOT NULL,
    version          integer NOT NULL,
    support          double precision NOT NULL,
    contradiction    double precision NOT NULL,
    target_recall    double precision NOT NULL,
    n_unsupported    integer NOT NULL,
    n_supported      integer NOT NULL,
    false_alarm_rate double precision NOT NULL,
    calibration      text NOT NULL,
    fitted_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant, version)
);
CREATE TABLE IF NOT EXISTS calibrator.live_scores (
    id       bigserial PRIMARY KEY,
    tenant   text NOT NULL,
    support  double precision NOT NULL,
    created  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS live_scores_tenant ON calibrator.live_scores (tenant, id DESC);
"""


class PostgresCalibrationStore:
    def __init__(self, pool: Any, default_target: float = 0.9) -> None:
        self.pool = pool
        self.default_target = default_target

    def migrate(self) -> None:
        with self.pool.connection() as conn:
            conn.execute("SELECT pg_advisory_lock(4202)")
            try:
                conn.execute(SCHEMA)
            finally:
                conn.execute("SELECT pg_advisory_unlock(4202)")

    def target_recall(self, tenant: str) -> float:
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT target_recall FROM calibrator.tenants WHERE tenant = %s", (tenant,)
            ).fetchone()
        return float(row[0]) if row else self.default_target

    def set_target_recall(self, tenant: str, target: float) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO calibrator.tenants (tenant, target_recall) VALUES (%s, %s) "
                "ON CONFLICT (tenant) DO UPDATE SET target_recall = EXCLUDED.target_recall",
                (tenant, target),
            )

    def add_label(self, tenant: str, label: Label) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO calibrator.labels (tenant, support, unsupported, flagged, source, weight, created) "
                "VALUES (%s, %s, %s, %s, %s, %s, to_timestamp(%s))",
                (
                    tenant,
                    label.support,
                    label.unsupported,
                    label.flagged,
                    label.source,
                    label.weight,
                    label.created,
                ),
            )

    def add_labels(self, tenant: str, labels: Sequence[Label]) -> None:
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO calibrator.labels (tenant, support, unsupported, flagged, source, weight, created) "
                "VALUES (%s, %s, %s, %s, %s, %s, to_timestamp(%s))",
                [
                    (tenant, x.support, x.unsupported, x.flagged, x.source, x.weight, x.created)
                    for x in labels
                ],
            )

    def labels(self, tenant: str, since: float | None = None) -> list[Label]:
        sql = (
            "SELECT support, unsupported, flagged, source, weight, extract(epoch FROM created) "
            "FROM calibrator.labels WHERE tenant = %s"
        )
        params: list[Any] = [tenant]
        if since is not None:
            sql += " AND created >= to_timestamp(%s)"
            params.append(since)
        with self.pool.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            Label(float(r[0]), bool(r[1]), bool(r[2]), str(r[3]), float(r[4]), float(r[5]))
            for r in rows
        ]

    def latest(self, tenant: str) -> FittedThresholds | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT support, target_recall, n_unsupported, n_supported, false_alarm_rate, "
                "calibration, version, extract(epoch FROM fitted_at), contradiction "
                "FROM calibrator.thresholds WHERE tenant = %s ORDER BY version DESC LIMIT 1",
                (tenant,),
            ).fetchone()
        if not row:
            return None
        return FittedThresholds(
            support=float(row[0]),
            target_recall=float(row[1]),
            n_unsupported=int(row[2]),
            n_supported=int(row[3]),
            false_alarm_rate=float(row[4]),
            calibration=str(row[5]),
            version=int(row[6]),
            fitted_at=float(row[7]),
            contradiction=float(row[8]),
        )

    def save(self, tenant: str, fitted: FittedThresholds) -> FittedThresholds:
        with self.pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO calibrator.thresholds (tenant, version, support, contradiction, target_recall, "
                "n_unsupported, n_supported, false_alarm_rate, calibration) "
                "SELECT %s, coalesce(max(version), 0) + 1, %s, %s, %s, %s, %s, %s, %s "
                "FROM calibrator.thresholds WHERE tenant = %s RETURNING version",
                (
                    tenant,
                    fitted.support,
                    fitted.contradiction,
                    fitted.target_recall,
                    fitted.n_unsupported,
                    fitted.n_supported,
                    fitted.false_alarm_rate,
                    fitted.calibration,
                    tenant,
                ),
            ).fetchone()
        fitted.version = int(row[0]) if row else 0
        return fitted

    def record_scores(self, tenant: str, scores: Sequence[float]) -> None:
        if not scores:
            return
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO calibrator.live_scores (tenant, support) VALUES (%s, %s)",
                [(tenant, float(s)) for s in scores],
            )

    def live_scores(self, tenant: str, limit: int) -> list[float]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT support FROM calibrator.live_scores WHERE tenant = %s ORDER BY id DESC LIMIT %s",
                (tenant, limit),
            ).fetchall()
        return [float(r[0]) for r in rows]

    def prune_live_scores(self, keep: int = 50_000) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "DELETE FROM calibrator.live_scores s USING ("
                "  SELECT id FROM (SELECT id, row_number() OVER (PARTITION BY tenant ORDER BY id DESC) rn"
                "  FROM calibrator.live_scores) x WHERE rn > %s) old WHERE s.id = old.id",
                (keep,),
            )


class Calibrator:
    """The calibrator's behaviour, independent of transport (gRPC) and storage."""

    def __init__(
        self, store: CalibrationStore, *, audit_rate: float = 0.02, default_support: float = 0.5
    ) -> None:
        self.store = store
        self.audit_rate = audit_rate
        self.default_support = default_support

    def thresholds(self, tenant: str) -> FittedThresholds:
        latest = self.store.latest(tenant)
        if latest is not None:
            return latest
        return FittedThresholds(
            support=self.default_support,
            target_recall=0.0,
            n_unsupported=0,
            n_supported=0,
            false_alarm_rate=0.0,
            calibration="default (not calibrated)",
        )

    def add_label(
        self, tenant: str, support: float, unsupported: bool, flagged: bool, source: str
    ) -> Label:
        weight = 1.0 / self.audit_rate if source == "audit" and self.audit_rate > 0 else 1.0
        label = Label(support, unsupported, flagged, source, weight)
        self.store.add_label(tenant, label)
        return label

    def fit(
        self, tenant: str, target_recall: float = 0.0, min_unsupported: int = 30
    ) -> tuple[FittedThresholds | None, str]:
        target = target_recall or self.store.target_recall(tenant)
        if target_recall:
            self.store.set_target_recall(tenant, target_recall)
        labels = self.store.labels(tenant)
        sources = sorted({lab.source for lab in labels})
        fitted = fit(
            labels,
            target,
            min_unsupported=min_unsupported,
            calibration=f"conformal on {'+'.join(sources) or 'no'} labels",
        )
        if fitted is None:
            n = sum(lab.unsupported for lab in labels)
            return None, f"need at least {min_unsupported} labelled unsupported claims, have {n}"
        return self.store.save(tenant, fitted), "fitted"

    def drift(
        self, tenant: str, window: int = 2000, label_window_s: float = 7 * 86400
    ) -> DriftReport:
        labels = self.store.labels(tenant)
        calib_scores = [lab.support for lab in labels if lab.source == "seed"] or [
            lab.support for lab in labels
        ]
        live = self.store.live_scores(tenant, window)
        recent = [
            lab
            for lab in labels
            if lab.source != "seed" and lab.created >= time.time() - label_window_s
        ]
        target = self.thresholds(tenant).target_recall or self.store.target_recall(tenant)
        return drift(calib_scores, live, recent, target)
