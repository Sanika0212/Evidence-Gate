from __future__ import annotations

import os
import random
from typing import Any

import pytest

from evidencegate.calibration import Calibrator, MemoryCalibrationStore
from evidencegate.config import Settings
from evidencegate.embedding import HashEmbedder, LexicalReranker
from evidencegate.gateway.app import Backends, Clients, create_app
from evidencegate.gateway.audit_log import MemoryAuditSink
from evidencegate.gateway.auth import NoAuth
from evidencegate.gateway.pii import RegexRedactor
from evidencegate.gateway.queue import MemoryPublisher
from evidencegate.gateway.ratelimit import MemoryRateLimiter
from evidencegate.gateway.review import MemoryReviewStore, MemoryTenants
from evidencegate.grpc_services import CalibratorServicer, CheckerServicer, RetrieverServicer
from evidencegate.onnx_checker import load_checker
from evidencegate.retrieval import MemoryStore, Retriever

CORPUS = [
    (
        "p1",
        "Aspirin irreversibly inhibits cyclooxygenase by acetylating a serine residue, "
        "which blocks thromboxane synthesis in platelets.",
        "pharm",
    ),
    (
        "p2",
        "Metformin is the first-line medication for type 2 diabetes in most adults.",
        "endo",
    ),
    (
        "p3",
        "Colorectal cancer screening is recommended for adults aged 45 to 75 years.",
        "uspstf",
    ),
    ("p4", "The Eiffel Tower is 330 metres tall and was completed in 1889.", "misc"),
]


def pytest_collection_modifyitems(config: Any, items: list[Any]) -> None:
    skip_services = pytest.mark.skip(reason="set EG_TEST_SERVICES=1 with services running")
    skip_models = pytest.mark.skip(reason="set EG_TEST_MODELS=1 with exported models")
    for item in items:
        if "services" in item.keywords and os.environ.get("EG_TEST_SERVICES") != "1":
            item.add_marker(skip_services)
        if "models" in item.keywords and os.environ.get("EG_TEST_MODELS") != "1":
            item.add_marker(skip_models)


@pytest.fixture
def retriever() -> Retriever:
    store = MemoryStore(HashEmbedder(64))
    store.add("demo", CORPUS)
    return Retriever(store, HashEmbedder(64), LexicalReranker())


@pytest.fixture
def calibrator() -> Calibrator:
    return Calibrator(MemoryCalibrationStore(0.9), audit_rate=0.5)


@pytest.fixture
def stack(retriever: Retriever, calibrator: Calibrator) -> dict[str, Any]:
    checker = load_checker("lexical")
    clients = Clients(
        RetrieverServicer(retriever, 3),
        CheckerServicer(checker),
        CalibratorServicer(calibrator),
        direct=True,
    )
    backends = Backends(
        clients=clients,
        verifier=NoAuth("demo"),
        limiter=MemoryRateLimiter(per_minute=6000, burst=1000),
        redactor=RegexRedactor(),
        audit_sink=MemoryAuditSink(),
        publisher=MemoryPublisher(),
        reviews=MemoryReviewStore(),
        tenants=MemoryTenants(),
        audit_rate=0.5,
        rng=random.Random(0),
    )
    settings = Settings(review_token="secret-token", demo=True, retrieve_k=3)
    return {"app": create_app(settings, backends), "backends": backends, "calibrator": calibrator}
