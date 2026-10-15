"""Settings, read from ``EG_*`` environment variables.

Every service reads the same settings object and uses the part it needs, so one
ConfigMap configures the whole deployment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from typing import Any


def _env(name: str, default: str) -> str:
    return os.environ.get(f"EG_{name.upper()}", default)


@dataclass(frozen=True)
class Settings:
    # Service addresses (host:port for gRPC, URLs for HTTP).
    retriever_addr: str = "localhost:50051"
    checker_addr: str = "localhost:50052"
    calibrator_addr: str = "localhost:50053"
    grpc_timeout_s: float = 30.0
    checker_concurrency: int = 2  # inferences a checker pod runs at once; the rest queue
    grpc_lb_policy: str = "round_robin"  # or "pick_first" (one connection, one pod)

    # Storage.
    database_url: str = "postgresql://evidencegate:evidencegate@localhost:5432/evidencegate"
    redis_url: str = "redis://localhost:6379/0"
    s3_endpoint: str = ""  # empty uses AWS; set for MinIO
    audit_bucket: str = "evidencegate-audit"
    models_bucket: str = "evidencegate-models"
    sqs_endpoint: str = ""  # empty uses AWS; set for ElasticMQ
    triage_queue: str = "evidencegate-triage"
    aws_region: str = "us-east-1"

    # Models.
    models_dir: str = "models"
    checker: str = "minicheck+nli"  # or "lexical" for tests and CI
    embedder: str = "snowflake/snowflake-arctic-embed-xs"  # or "hash" for tests and CI
    reranker: str = "Xenova/ms-marco-MiniLM-L-6-v2"  # or "lexical" / "none"
    embedding_dim: int = 384

    # Gateway behaviour.
    auth_mode: str = "jwt"  # "jwt" or "none" (local development only)
    jwks_url: str = "http://localhost:8081/.well-known/jwks.json"
    jwt_issuer: str = "evidencegate-dev"
    jwt_audience: str = "evidencegate"
    rate_limit_per_minute: int = 120
    rate_limit_burst: int = 30
    audit_sample_rate: float = 0.02  # share of unflagged claims sent to review as a recall audit
    default_target_recall: float = 0.9
    review_token: str = "change-me"
    retrieve_k: int = 5
    retrieval_mode: str = "hybrid"  # lexical, dense, hybrid or hybrid_rerank
    # A spaCy model for Presidio, or "regex". The large model leaks no identifiers in
    # eval/run_pii.py where the small one leaks some names (see the README).
    redactor: str = "en_core_web_lg"
    demo: bool = False  # serve the demo page at / (no auth; a fixed demo tenant)
    demo_tenant: str = "demo"

    # Telemetry.
    service_name: str = "evidencegate"
    log_level: str = "INFO"
    log_json: bool = True

    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls, **overrides: Any) -> Settings:
        values: dict[str, Any] = {}
        for f in fields(cls):
            if f.name == "extra":
                continue
            raw = _env(f.name, "")
            if raw == "":
                continue
            default = f.default
            if isinstance(default, bool):
                values[f.name] = raw.lower() in {"1", "true", "yes", "on"}
            elif isinstance(default, int):
                values[f.name] = int(raw)
            elif isinstance(default, float):
                values[f.name] = float(raw)
            else:
                values[f.name] = raw
        values.update(overrides)
        return cls(**values)
