"""The gateway: ``POST /v1/check`` and the review workflow.

A check runs: authenticate, rate-limit, retrieve evidence if none was sent, fetch the
tenant's thresholds, check every claim, decide (pass, flag or block), then record the
side effects: live scores for drift, review items, a redacted audit record and, for
flagged answers, a triage job. The decision is returned before triage runs.
"""

from __future__ import annotations

import random
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import grpc
from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from evidencegate._version import __version__
from evidencegate.config import Settings
from evidencegate.gateway.audit_log import AuditSink, sha256
from evidencegate.gateway.auth import AuthError, Principal
from evidencegate.gateway.pii import redact_audit, scoped
from evidencegate.gateway.queue import Publisher
from evidencegate.gateway.review import ReviewItem, ReviewStore
from evidencegate.telemetry import (
    CLAIMS,
    DECISIONS,
    RATE_LIMITED,
    REQUEST_LATENCY,
    REQUESTS,
    SUPPORT_SCORE,
    get_logger,
)
from evidencegate.v1 import (
    calibrator_pb2,
    checker_pb2,
    common_pb2,
    retriever_pb2,
)

log = get_logger(__name__)
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
VERDICT_NAMES = {
    checker_pb2.VERDICT_SUPPORTED: "supported",
    checker_pb2.VERDICT_UNSUPPORTED: "unsupported",
    checker_pb2.VERDICT_CONTRADICTED: "contradicted",
}


# --------------------------------------------------------------------------- API models


class PassageIn(BaseModel):
    id: str
    text: str
    source: str = ""


class CheckRequest(BaseModel):
    answer: str = Field(min_length=1, max_length=20_000)
    question: str | None = Field(default=None, max_length=4_000)
    passages: list[PassageIn] | None = Field(default=None, max_length=50)
    reference: str | None = Field(default=None, max_length=20_000)
    k: int = Field(default=0, ge=0, le=20)


class FeedbackRequest(BaseModel):
    item_id: str
    label: str = Field(pattern="^(supported|unsupported)$")


# --------------------------------------------------------------------------- backends


class Clients:
    """Calls the three internal services, over gRPC or directly on servicer objects."""

    def __init__(
        self,
        retriever: Any,
        checker: Any,
        calibrator: Any,
        *,
        timeout: float = 30.0,
        direct: bool = False,
    ) -> None:
        self.retriever = retriever
        self.checker = checker
        self.calibrator = calibrator
        self.timeout = timeout
        self.direct = direct

    def _call(self, method: Callable[..., Any], request: Any) -> Any:
        return method(request, None) if self.direct else method(request, timeout=self.timeout)

    def retrieve(self, tenant: str, query: str, k: int) -> list[common_pb2.Passage]:
        resp = self._call(
            self.retriever.Retrieve, retriever_pb2.RetrieveRequest(tenant=tenant, query=query, k=k)
        )
        return list(resp.passages)

    def thresholds(self, tenant: str) -> calibrator_pb2.GetThresholdsResponse:
        resp: calibrator_pb2.GetThresholdsResponse = self._call(
            self.calibrator.GetThresholds, calibrator_pb2.GetThresholdsRequest(tenant=tenant)
        )
        return resp

    def audit(self, request: checker_pb2.AuditRequest) -> checker_pb2.AuditResponse:
        resp: checker_pb2.AuditResponse = self._call(self.checker.Audit, request)
        return resp

    def record_scores(self, tenant: str, scores: list[float]) -> None:
        self._call(
            self.calibrator.RecordScores,
            calibrator_pb2.RecordScoresRequest(tenant=tenant, support=scores),
        )

    def add_label(
        self, tenant: str, support: float, unsupported: bool, flagged: bool, source: str
    ) -> None:
        self._call(
            self.calibrator.AddLabel,
            calibrator_pb2.AddLabelRequest(
                tenant=tenant,
                support=support,
                unsupported=unsupported,
                flagged=flagged,
                source=source,
            ),
        )


@dataclass
class Backends:
    clients: Clients
    verifier: Any
    limiter: Any
    redactor: Any
    audit_sink: AuditSink
    publisher: Publisher
    reviews: ReviewStore
    tenants: Any
    audit_rate: float = 0.02
    rng: random.Random | None = None


def build_backends(settings: Settings) -> Backends:  # pragma: no cover - wiring for real services
    import boto3
    import redis
    from psycopg_pool import ConnectionPool

    from evidencegate.gateway.audit_log import S3AuditSink
    from evidencegate.gateway.auth import JWTVerifier, NoAuth
    from evidencegate.gateway.pii import load_redactor
    from evidencegate.gateway.queue import SqsPublisher
    from evidencegate.gateway.ratelimit import RedisRateLimiter
    from evidencegate.gateway.review import PostgresReviewStore, PostgresTenants
    from evidencegate.grpc_services import channel
    from evidencegate.v1 import calibrator_pb2_grpc, checker_pb2_grpc, retriever_pb2_grpc

    pool = ConnectionPool(settings.database_url, min_size=1, max_size=10, open=True)
    reviews = PostgresReviewStore(pool)
    reviews.migrate()
    tenants = PostgresTenants(pool)
    verifier: Any = (
        NoAuth()
        if settings.auth_mode == "none"
        else JWTVerifier(settings.jwks_url, settings.jwt_issuer, settings.jwt_audience, tenants)
    )
    s3 = boto3.client(
        "s3", endpoint_url=settings.s3_endpoint or None, region_name=settings.aws_region
    )
    sqs = boto3.client(
        "sqs", endpoint_url=settings.sqs_endpoint or None, region_name=settings.aws_region
    )
    lb = settings.grpc_lb_policy
    clients = Clients(
        retriever_pb2_grpc.RetrieverServiceStub(channel(settings.retriever_addr, lb)),
        checker_pb2_grpc.CheckerServiceStub(channel(settings.checker_addr, lb)),
        calibrator_pb2_grpc.CalibratorServiceStub(channel(settings.calibrator_addr, lb)),
        timeout=settings.grpc_timeout_s,
    )
    return Backends(
        clients=clients,
        verifier=verifier,
        limiter=RedisRateLimiter(
            redis.Redis.from_url(settings.redis_url),
            settings.rate_limit_per_minute,
            settings.rate_limit_burst,
        ),
        redactor=load_redactor(settings.redactor),
        audit_sink=S3AuditSink(s3, settings.audit_bucket),
        publisher=SqsPublisher(sqs, settings.triage_queue),
        reviews=reviews,
        tenants=tenants,
        audit_rate=settings.audit_sample_rate,
    )


# --------------------------------------------------------------------------- app


def create_app(settings: Settings, backends: Backends | None = None) -> FastAPI:
    app = FastAPI(title="Evidence Gate", version=__version__, docs_url="/docs", redoc_url=None)
    state: dict[str, Backends] = {}
    lock = threading.Lock()

    def be() -> Backends:
        # Built once: concurrent first requests used to build it in parallel, loading the
        # spaCy model several times over and running the pod out of memory.
        if "b" not in state:
            with lock:
                if "b" not in state:
                    state["b"] = backends or build_backends(settings)
        return state["b"]

    # `evidencegate serve gateway` calls this before accepting traffic, so the model is
    # loaded before the pod reports ready and the first request is not slow.
    app.state.warm_up = be

    rng = (backends.rng if backends and backends.rng else None) or random.Random()

    @app.middleware("http")
    async def metrics_mw(request: Request, call_next: Any) -> Any:
        started = time.perf_counter()
        status = "500"  # an exception that escapes every handler is still a failed request
        try:
            response = await call_next(request)
            status = str(response.status_code)
            return response
        finally:
            route = request.scope.get("route")
            path = getattr(route, "path", None) or "unmatched"
            REQUEST_LATENCY.labels(route=path).observe(time.perf_counter() - started)
            REQUESTS.labels(route=path, status=status).inc()

    @app.exception_handler(AuthError)
    async def auth_error(_: Request, exc: AuthError) -> JSONResponse:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status)

    @app.exception_handler(grpc.RpcError)
    async def backend_error(_: Request, exc: grpc.RpcError) -> JSONResponse:
        # A backend service failed (after the client's own retry): answer 503 so callers
        # can retry and the availability SLO counts it, instead of an uncounted traceback.
        code = exc.code().name if hasattr(exc, "code") else "UNKNOWN"
        log.warning("backend_error", code=code)
        return JSONResponse(
            {"detail": "a backend service is unavailable", "code": code},
            status_code=503,
            headers={"Retry-After": "1"},
        )

    def principal(authorization: str | None = Header(default=None)) -> Principal:
        result: Principal = be().verifier.verify(authorization)
        return result

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/readyz")
    def readyz() -> dict[str, str]:
        be()
        return {"status": "ready"}

    @app.get("/metrics")
    def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.post("/v1/check")
    def check(req: CheckRequest, who: Principal = Depends(principal)) -> dict[str, Any]:
        who.require("check")
        return run_check(req, who.tenant, who.client_id)

    def run_check(req: CheckRequest, tenant: str, client_id: str) -> dict[str, Any]:
        b = be()
        allowed, retry = b.limiter.allow(tenant)
        if not allowed:
            RATE_LIMITED.labels(tenant=tenant).inc()
            raise HTTPException(
                429, "rate limit exceeded", headers={"Retry-After": f"{max(1, round(retry))}"}
            )
        timings: dict[str, float] = {}
        started = time.perf_counter()
        retrieved = False
        if req.passages:
            passages = [
                common_pb2.Passage(id=p.id, text=p.text, source=p.source) for p in req.passages
            ]
        else:
            t0 = time.perf_counter()
            passages = b.clients.retrieve(
                tenant, req.question or req.answer, req.k or settings.retrieve_k
            )
            timings["retrieve_ms"] = (time.perf_counter() - t0) * 1000
            retrieved = True
        t0 = time.perf_counter()
        fitted = b.clients.thresholds(tenant)
        timings["thresholds_ms"] = (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        result = b.clients.audit(
            checker_pb2.AuditRequest(
                answer=req.answer,
                passages=passages,
                question=req.question or "",
                reference=req.reference or "",
                thresholds=fitted.thresholds,
                diagnose=True,
            )
        )
        timings["check_ms"] = (time.perf_counter() - t0) * 1000
        claims = [claim_out(c) for c in result.claims]
        flagged = [c for c in claims if c["flagged"]]
        contradicted = any(c["verdict"] == "contradicted" for c in claims)
        policy = b.tenants.policy(tenant)
        decision = (
            "pass"
            if not flagged
            else ("block" if contradicted and policy.block_on_contradicted else "flag")
        )
        audit_id = uuid.uuid4().hex
        timings["total_ms"] = (time.perf_counter() - started) * 1000

        for c in claims:
            CLAIMS.labels(tenant=tenant, verdict=c["verdict"]).inc()
            SUPPORT_SCORE.labels(tenant=tenant).observe(c["support"])
        DECISIONS.labels(tenant=tenant, decision=decision).inc()
        side_effects(
            b, tenant, client_id, audit_id, req, claims, decision, fitted, result, passages, timings
        )

        return {
            "id": audit_id,
            "decision": decision,
            "claims": claims,
            "passages": [
                {"id": p.id, "text": p.text, "score": p.score, "source": p.source} for p in passages
            ]
            if retrieved
            else [],
            "guarantee": {
                "target_recall": fitted.thresholds.target_recall or None,
                "threshold": fitted.thresholds.support,
                "false_alarm_rate": fitted.false_alarm_rate
                if fitted.thresholds.target_recall
                else None,
                "calibration": fitted.thresholds.calibration,
                "version": fitted.version,
                "calibrated_on_unsupported": fitted.n_unsupported,
            },
            "checker": result.checker,
            "latency_ms": {k: round(v, 1) for k, v in timings.items()},
        }

    def side_effects(
        b: Backends,
        tenant: str,
        client_id: str,
        audit_id: str,
        req: CheckRequest,
        claims: list[dict[str, Any]],
        decision: str,
        fitted: calibrator_pb2.GetThresholdsResponse,
        result: checker_pb2.AuditResponse,
        passages: list[common_pb2.Passage],
        timings: dict[str, float],
    ) -> None:
        try:
            b.clients.record_scores(tenant, [c["support"] for c in claims])
        except Exception as exc:
            log.error("record_scores_failed", error=type(exc).__name__)
        redactor = scoped(b.redactor, [req.question or "", req.answer, *(p.text for p in passages)])
        items = []
        for c in claims:
            sampled = c["flagged"] or rng.random() < b.audit_rate
            if not sampled:
                continue
            claim_text, _ = redactor.redact(c["text"])
            evidence, _ = redactor.redact((c.get("evidence") or {}).get("text", ""))
            items.append(
                ReviewItem(
                    tenant=tenant,
                    audit_id=audit_id,
                    claim=claim_text,
                    evidence=evidence,
                    support=c["support"],
                    flagged=c["flagged"],
                    source="review" if c["flagged"] else "audit",
                )
            )
        b.reviews.add(items)
        question, q_counts = redactor.redact(req.question or "")
        answer, a_counts = redactor.redact(req.answer)
        b.audit_sink.write(
            {
                "id": audit_id,
                "ts": time.time(),
                "tenant": tenant,
                "client_id": client_id,
                "decision": decision,
                "verdicts": {
                    v: sum(c["verdict"] == v for c in claims)
                    for v in ("supported", "unsupported", "contradicted")
                },
                "question_redacted": question,
                "answer_redacted": answer,
                "answer_sha256": sha256(req.answer),
                "pii_redacted": dict(q_counts + a_counts),
                "passage_ids": [p.id for p in passages],
                "thresholds": {
                    "support": fitted.thresholds.support,
                    "target_recall": fitted.thresholds.target_recall,
                    "version": fitted.version,
                },
                "checker": result.checker,
                "latency_ms": round(timings.get("total_ms", 0.0), 1),
            }
        )
        if decision != "pass":
            # The triage worker sends this to a language model: redact it first.
            audit_redacted, _ = redact_audit(result.audit_json, redactor)
            b.publisher.publish(
                {
                    "audit_id": audit_id,
                    "tenant": tenant,
                    "flagged": [c["index"] for c in claims if c["flagged"]],
                    "audit": audit_redacted,
                }
            )
        log.info(
            "checked",
            audit_id=audit_id,
            tenant=tenant,
            decision=decision,
            claims=len(claims),
            flagged=sum(c["flagged"] for c in claims),
            total_ms=round(timings.get("total_ms", 0.0), 1),
        )

    @app.get("/v1/thresholds")
    def thresholds(who: Principal = Depends(principal)) -> dict[str, Any]:
        f = be().clients.thresholds(who.tenant)
        return {
            "tenant": who.tenant,
            "threshold": f.thresholds.support,
            "target_recall": f.thresholds.target_recall or None,
            "false_alarm_rate": f.false_alarm_rate,
            "calibration": f.thresholds.calibration,
            "version": f.version,
        }

    @app.post("/v1/feedback")
    def feedback(req: FeedbackRequest, who: Principal = Depends(principal)) -> dict[str, str]:
        who.require("label")
        return label_item(req.item_id, req.label, tenant=who.tenant)

    def label_item(item_id: str, label: str, tenant: str | None) -> dict[str, str]:
        b = be()
        item = b.reviews.get(item_id)
        if item is None or (tenant is not None and item.tenant != tenant):
            raise HTTPException(404, "no such review item")
        updated = b.reviews.set_label(item_id, label)
        if updated is None:
            raise HTTPException(409, "already labelled")
        b.clients.add_label(
            updated.tenant, updated.support, label == "unsupported", updated.flagged, updated.source
        )
        return {"id": item_id, "label": label}

    # ----------------------------------------------------------- review page (HTML)

    def review_auth(token: str | None) -> None:
        if not token or token != settings.review_token:
            raise HTTPException(401, "review token required")

    @app.get("/review", response_class=HTMLResponse)
    def review_page(request: Request, token: str | None = None, tenant: str | None = None) -> Any:
        review_auth(token)
        items = be().reviews.pending(tenant, limit=50)
        return TEMPLATES.TemplateResponse(
            request, "review.html", {"items": items, "token": token, "tenant": tenant or ""}
        )

    @app.post("/review/{item_id}")
    def review_submit(
        item_id: str, label: str = Form(...), token: str = Form(...), tenant: str = Form("")
    ) -> Any:
        review_auth(token)
        if label not in {"supported", "unsupported"}:
            raise HTTPException(400, "label must be supported or unsupported")
        label_item(item_id, label, tenant=None)
        return RedirectResponse(f"/review?token={token}&tenant={tenant}", status_code=303)

    # ----------------------------------------------------------- demo page

    if settings.demo:

        @app.get("/", response_class=HTMLResponse)
        def demo_page(request: Request) -> Any:
            return TEMPLATES.TemplateResponse(request, "demo.html", {"version": __version__})

        @app.post("/demo/check")
        def demo_check(req: CheckRequest) -> dict[str, Any]:
            return run_check(req, settings.demo_tenant, "demo-page")

    return app


def claim_out(c: checker_pb2.ClaimResult) -> dict[str, Any]:
    verdict = VERDICT_NAMES.get(c.verdict, "unsupported")
    out: dict[str, Any] = {
        "index": c.index,
        "text": c.text,
        "verdict": verdict,
        "flagged": verdict != "supported",
        "support": round(c.support, 4),
        "contradiction": round(c.contradiction, 4),
    }
    if c.HasField("evidence"):
        out["evidence"] = {"passage_id": c.evidence.passage_id, "text": c.evidence.text}
    if c.HasField("diagnosis"):
        out["cause"] = c.diagnosis.cause
        out["reason"] = c.diagnosis.reason
        out["fix"] = c.diagnosis.fix
    return out
