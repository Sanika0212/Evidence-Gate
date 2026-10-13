"""gRPC servers for the checker, retriever and calibrator, plus the gateway's clients.

Each server registers the standard gRPC health service, so Kubernetes probes it with
its native ``grpc`` probe, and is instrumented with OpenTelemetry.
"""

from __future__ import annotations

import time
from concurrent import futures
from typing import Any

import grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

from evidencegate.calibration import Calibrator, FittedThresholds
from evidencegate.retrieval import Mode, Retriever
from evidencegate.telemetry import GRPC_ERRORS, STAGE_LATENCY, THRESHOLD, get_logger
from evidencegate.v1 import (
    calibrator_pb2,
    calibrator_pb2_grpc,
    checker_pb2,
    checker_pb2_grpc,
    common_pb2,
    retriever_pb2,
    retriever_pb2_grpc,
)

log = get_logger(__name__)

VERDICTS = {
    "supported": checker_pb2.VERDICT_SUPPORTED,
    "unsupported": checker_pb2.VERDICT_UNSUPPORTED,
    "contradicted": checker_pb2.VERDICT_CONTRADICTED,
}
MODES = {
    retriever_pb2.RETRIEVAL_MODE_LEXICAL: Mode.LEXICAL,
    retriever_pb2.RETRIEVAL_MODE_DENSE: Mode.DENSE,
    retriever_pb2.RETRIEVAL_MODE_HYBRID: Mode.HYBRID,
    retriever_pb2.RETRIEVAL_MODE_HYBRID_RERANK: Mode.HYBRID_RERANK,
}
MODE_ENUM = {v: k for k, v in MODES.items()}


# --------------------------------------------------------------------------- checker


def thresholds_from_pb(t: common_pb2.Thresholds) -> Any:
    from groundsight.model import Thresholds

    return Thresholds(
        support=t.support or 0.5,
        contradiction=t.contradiction or 0.5,
        contradiction_overlap=t.contradiction_overlap or 0.3,
        contradiction_overrides_support=t.contradiction_overrides_support,
        target_recall=t.target_recall or None,
        calibration=t.calibration or None,
    )


def thresholds_to_pb(f: FittedThresholds) -> common_pb2.Thresholds:
    return common_pb2.Thresholds(
        support=f.support,
        contradiction=f.contradiction,
        contradiction_overlap=0.3,
        target_recall=f.target_recall,
        calibration=f.calibration,
    )


class CheckerServicer(checker_pb2_grpc.CheckerServiceServicer):
    def __init__(self, checker: Any, *, fault_rate: float = 0.0) -> None:
        self.checker = checker
        # Test hook for canary drills: fail this share of requests on purpose.
        self.fault_rate = fault_rate

    def Audit(self, request: checker_pb2.AuditRequest, context: Any) -> checker_pb2.AuditResponse:
        import random

        if self.fault_rate and random.random() < self.fault_rate:
            GRPC_ERRORS.labels(service="checker").inc()
            if context is not None:
                context.abort(grpc.StatusCode.UNAVAILABLE, "injected fault (EG_FAULT_RATE)")
            raise RuntimeError("injected fault")
        try:
            return self._audit(request)
        except Exception:
            GRPC_ERRORS.labels(service="checker").inc()
            raise

    def _audit(self, request: checker_pb2.AuditRequest) -> checker_pb2.AuditResponse:
        import json

        from groundsight import audit as run_audit

        started = time.perf_counter()
        result = run_audit(
            request.answer,
            [
                {"id": p.id, "text": p.text, "score": p.score, "source": p.source}
                for p in request.passages
            ],
            question=request.question or None,
            reference=request.reference or None,
            checker=self.checker,
            thresholds=thresholds_from_pb(request.thresholds),
            diagnose=request.diagnose,
        )
        STAGE_LATENCY.labels(stage="checker_audit").observe(time.perf_counter() - started)
        claims = []
        for c in result.claims:
            msg = checker_pb2.ClaimResult(
                index=c.index,
                text=c.text,
                start=c.start,
                end=c.end,
                support=c.support,
                contradiction=c.contradiction,
                verdict=VERDICTS[c.verdict.value],
                relevance=c.relevance,
            )
            if c.evidence is not None:
                msg.evidence.CopyFrom(
                    checker_pb2.Evidence(
                        passage_id=c.evidence.chunk_id,
                        text=c.evidence.text,
                        support=c.evidence.support,
                        contradiction=c.evidence.contradiction,
                    )
                )
            if c.diagnosis is not None:
                msg.diagnosis.CopyFrom(
                    checker_pb2.Diagnosis(
                        cause=c.diagnosis.cause.value,
                        reason=c.diagnosis.reason,
                        fix=c.diagnosis.fix,
                        passage_ids=c.diagnosis.chunk_ids,
                    )
                )
            claims.append(msg)
        return checker_pb2.AuditResponse(
            claims=claims,
            checker=result.checker,
            groundedness=result.groundedness,
            audit_json=json.dumps(result.to_dict()),
        )


# --------------------------------------------------------------------------- retriever


class RetrieverServicer(retriever_pb2_grpc.RetrieverServiceServicer):
    def __init__(
        self, retriever: Retriever, default_k: int = 5, default_mode: Mode = Mode.HYBRID
    ) -> None:
        self.retriever = retriever
        self.default_k = default_k
        self.default_mode = default_mode

    def Retrieve(
        self, request: retriever_pb2.RetrieveRequest, context: Any
    ) -> retriever_pb2.RetrieveResponse:
        mode = MODES.get(request.mode, self.default_mode)
        hits = self.retriever.retrieve(
            request.tenant, request.query, request.k or self.default_k, mode
        )
        return retriever_pb2.RetrieveResponse(
            passages=[
                common_pb2.Passage(id=h.id, text=h.text, score=h.score, source=h.source)
                for h in hits
            ],
            mode=MODE_ENUM[mode],
        )


# --------------------------------------------------------------------------- calibrator


def fitted_to_pb(f: FittedThresholds) -> calibrator_pb2.GetThresholdsResponse:
    return calibrator_pb2.GetThresholdsResponse(
        thresholds=thresholds_to_pb(f),
        version=f.version,
        n_unsupported=f.n_unsupported,
        n_supported=f.n_supported,
        false_alarm_rate=f.false_alarm_rate,
        fitted_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(f.fitted_at)),
    )


class CalibratorServicer(calibrator_pb2_grpc.CalibratorServiceServicer):
    def __init__(self, calibrator: Calibrator) -> None:
        self.calibrator = calibrator

    def GetThresholds(self, request: Any, context: Any) -> calibrator_pb2.GetThresholdsResponse:
        f = self.calibrator.thresholds(request.tenant)
        THRESHOLD.labels(tenant=request.tenant).set(f.support)
        return fitted_to_pb(f)

    def RecordScores(self, request: Any, context: Any) -> calibrator_pb2.RecordScoresResponse:
        self.calibrator.store.record_scores(request.tenant, list(request.support))
        return calibrator_pb2.RecordScoresResponse()

    def AddLabel(self, request: Any, context: Any) -> calibrator_pb2.AddLabelResponse:
        self.calibrator.add_label(
            request.tenant,
            request.support,
            request.unsupported,
            request.flagged,
            request.source or "review",
        )
        return calibrator_pb2.AddLabelResponse()

    def Fit(self, request: Any, context: Any) -> calibrator_pb2.FitResponse:
        fitted, reason = self.calibrator.fit(
            request.tenant, request.target_recall, request.min_unsupported or 30
        )
        if fitted is None:
            return calibrator_pb2.FitResponse(fitted=False, reason=reason)
        log.info(
            "thresholds_fitted",
            tenant=request.tenant,
            version=fitted.version,
            support=fitted.support,
        )
        return calibrator_pb2.FitResponse(result=fitted_to_pb(fitted), fitted=True, reason=reason)

    def Drift(self, request: Any, context: Any) -> calibrator_pb2.DriftResponse:
        r = self.calibrator.drift(request.tenant, request.window or 2000)
        return calibrator_pb2.DriftResponse(
            psi=r.psi,
            ks=r.ks,
            live_n=r.live_n,
            labelled_unsupported=r.labelled_unsupported,
            labelled_flagged=r.labelled_flagged,
            recall_estimate=r.recall_estimate,
            recall_lower=r.recall_lower,
            needs_recalibration=r.needs_recalibration,
            reason=r.reason,
        )


# --------------------------------------------------------------------------- serving


def build_server(port: int, servicers: list[tuple[Any, Any]], *, workers: int = 8) -> grpc.Server:
    """A gRPC server with the given ``(add_fn, servicer)`` pairs and a health service."""
    interceptors = []
    try:
        from opentelemetry.instrumentation.grpc import server_interceptor

        interceptors.append(server_interceptor())
    except ImportError:  # pragma: no cover - instrumentation is in the serve extra
        pass
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=workers),
        interceptors=interceptors,
        options=[("grpc.max_receive_message_length", 32 * 1024 * 1024)],
    )
    for add, servicer in servicers:
        add(servicer, server)
    health_servicer = health.HealthServicer()
    health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)
    health_servicer.set("", health_pb2.HealthCheckResponse.SERVING)
    server.add_insecure_port(f"[::]:{port}")
    return server


def channel(addr: str) -> grpc.Channel:
    """A plaintext channel; NetworkPolicies keep internal traffic inside the namespace.

    Client spans come from ``instrument_clients`` (called once at service start-up).
    """
    return grpc.insecure_channel(
        addr, options=[("grpc.max_receive_message_length", 32 * 1024 * 1024)]
    )


def instrument_clients() -> None:
    try:
        from opentelemetry.instrumentation.grpc import GrpcInstrumentorClient

        GrpcInstrumentorClient().instrument()
    except ImportError:  # pragma: no cover
        pass
