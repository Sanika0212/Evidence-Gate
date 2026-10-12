"""Structured logs, Prometheus metrics and OpenTelemetry traces, shared by every service.

Logs are JSON lines carrying the active trace id, so a log line in Grafana links to its
trace. Tracing exports over OTLP only when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set.
Text that may contain patient data never goes into logs or span attributes: callers log
ids, counts and scores.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

import structlog
from opentelemetry import trace
from prometheus_client import Counter, Gauge, Histogram

_configured = False

LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 0.75, 1, 1.5, 2.5, 5, 10, 30)

REQUESTS = Counter("eg_requests_total", "Gateway requests by route and status", ["route", "status"])
REQUEST_LATENCY = Histogram(
    "eg_request_duration_seconds", "Gateway request latency", ["route"], buckets=LATENCY_BUCKETS
)
CLAIMS = Counter("eg_claims_total", "Claims checked by verdict", ["tenant", "verdict"])
DECISIONS = Counter("eg_decisions_total", "Gate decisions", ["tenant", "decision"])
RATE_LIMITED = Counter("eg_rate_limited_total", "Requests rejected by the rate limiter", ["tenant"])
STAGE_LATENCY = Histogram(
    "eg_stage_duration_seconds",
    "Latency of each stage inside a service",
    ["stage"],
    buckets=LATENCY_BUCKETS,
)
SUPPORT_SCORE = Histogram(
    "eg_support_score",
    "Support scores of served claims",
    ["tenant"],
    buckets=tuple(i / 20 for i in range(1, 21)),
)
THRESHOLD = Gauge("eg_support_threshold", "Current support threshold", ["tenant"])
DRIFT_PSI = Gauge("eg_drift_psi", "Population stability index of live support scores", ["tenant"])
RECALL_LOWER = Gauge(
    "eg_recall_lower_bound", "Lower 95% bound on recall from reviewer labels", ["tenant"]
)
QUEUE_PUBLISHED = Counter("eg_triage_published_total", "Flagged audits sent to triage")
TRIAGE_DONE = Counter("eg_triage_processed_total", "Triage jobs by outcome", ["outcome"])
PII_REDACTED = Counter("eg_pii_entities_redacted_total", "PII entities redacted", ["entity"])
GRPC_ERRORS = Counter("eg_grpc_errors_total", "gRPC handler errors by service", ["service"])


def _add_trace_ids(_: Any, __: str, event: dict[str, Any]) -> dict[str, Any]:
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx.is_valid:
        event["trace_id"] = format(ctx.trace_id, "032x")
        event["span_id"] = format(ctx.span_id, "016x")
    return event


def configure(service_name: str, *, level: str = "INFO", json_logs: bool = True) -> None:
    """Set up logging and (if an OTLP endpoint is configured) tracing. Idempotent."""
    global _configured
    if _configured:
        return
    _configured = True
    logging.basicConfig(stream=sys.stdout, level=level.upper(), format="%(message)s")
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _add_trace_ids,
        renderer,
    ]
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )
    structlog.contextvars.bind_contextvars(service=service_name)
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        _configure_tracing(service_name)


def _configure_tracing(service_name: str) -> None:
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)


def tracer() -> trace.Tracer:
    return trace.get_tracer("evidencegate")
