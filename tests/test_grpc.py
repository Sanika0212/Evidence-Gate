"""The three services over real gRPC, on ephemeral ports."""

from __future__ import annotations

import socket
from collections.abc import Iterator
from typing import Any

import grpc
import pytest
from grpc_health.v1 import health_pb2, health_pb2_grpc

from evidencegate.calibration import Calibrator
from evidencegate.gateway.app import Clients
from evidencegate.grpc_services import (
    CalibratorServicer,
    CheckerServicer,
    RetrieverServicer,
    build_server,
    channel,
)
from evidencegate.onnx_checker import load_checker
from evidencegate.retrieval import Retriever
from evidencegate.v1 import (
    calibrator_pb2,
    calibrator_pb2_grpc,
    checker_pb2,
    checker_pb2_grpc,
    common_pb2,
    retriever_pb2,
    retriever_pb2_grpc,
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def servers(retriever: Retriever, calibrator: Calibrator) -> Iterator[dict[str, Any]]:
    ports = {name: free_port() for name in ("retriever", "checker", "calibrator")}
    started = [
        build_server(
            ports["retriever"],
            [
                (
                    retriever_pb2_grpc.add_RetrieverServiceServicer_to_server,
                    RetrieverServicer(retriever, 3),
                )
            ],
        ),
        build_server(
            ports["checker"],
            [
                (
                    checker_pb2_grpc.add_CheckerServiceServicer_to_server,
                    CheckerServicer(load_checker("lexical")),
                )
            ],
        ),
        build_server(
            ports["calibrator"],
            [
                (
                    calibrator_pb2_grpc.add_CalibratorServiceServicer_to_server,
                    CalibratorServicer(calibrator),
                )
            ],
        ),
    ]
    for s in started:
        s.start()
    yield {name: f"127.0.0.1:{p}" for name, p in ports.items()}
    for s in started:
        s.stop(grace=None)


def test_health_checks_report_serving(servers: dict[str, str]) -> None:
    for addr in servers.values():
        stub = health_pb2_grpc.HealthStub(grpc.insecure_channel(addr))
        resp = stub.Check(health_pb2.HealthCheckRequest(service=""), timeout=5)
        assert resp.status == health_pb2.HealthCheckResponse.SERVING


def test_end_to_end_over_grpc(servers: dict[str, str]) -> None:
    clients = Clients(
        retriever_pb2_grpc.RetrieverServiceStub(channel(servers["retriever"])),
        checker_pb2_grpc.CheckerServiceStub(channel(servers["checker"])),
        calibrator_pb2_grpc.CalibratorServiceStub(channel(servers["calibrator"])),
        timeout=10,
    )
    passages = clients.retrieve("demo", "aspirin cyclooxygenase platelets", 2)
    assert passages[0].id == "p1"
    t = clients.thresholds("demo")
    assert t.thresholds.support == 0.5
    assert t.version == 0
    resp = clients.audit(
        checker_pb2.AuditRequest(
            answer="Aspirin inhibits cyclooxygenase. Aspirin is made from unicorn horns.",
            passages=passages,
            thresholds=t.thresholds,
            diagnose=True,
        )
    )
    verdicts = [c.verdict for c in resp.claims]
    assert checker_pb2.VERDICT_SUPPORTED in verdicts
    assert any(
        c.verdict != checker_pb2.VERDICT_SUPPORTED and c.diagnosis.cause for c in resp.claims
    )
    assert resp.audit_json
    clients.record_scores("demo", [c.support for c in resp.claims])
    clients.add_label("demo", 0.2, True, True, "review")
    fit = calibrator_pb2_grpc.CalibratorServiceStub(channel(servers["calibrator"])).Fit(
        calibrator_pb2.FitRequest(tenant="demo", target_recall=0.9, min_unsupported=1), timeout=10
    )
    assert fit.fitted is False  # one label cannot certify 90% recall
    drift = calibrator_pb2_grpc.CalibratorServiceStub(channel(servers["calibrator"])).Drift(
        calibrator_pb2.DriftRequest(tenant="demo"), timeout=10
    )
    assert drift.live_n == len(resp.claims)


def test_retrieval_modes_over_grpc(servers: dict[str, str]) -> None:
    stub = retriever_pb2_grpc.RetrieverServiceStub(channel(servers["retriever"]))
    for mode in (
        retriever_pb2.RETRIEVAL_MODE_LEXICAL,
        retriever_pb2.RETRIEVAL_MODE_DENSE,
        retriever_pb2.RETRIEVAL_MODE_HYBRID,
        retriever_pb2.RETRIEVAL_MODE_HYBRID_RERANK,
    ):
        resp = stub.Retrieve(
            retriever_pb2.RetrieveRequest(
                tenant="demo", query="metformin diabetes", k=1, mode=mode
            ),
            timeout=10,
        )
        assert resp.mode == mode
        assert resp.passages[0].id == "p2"
    assert isinstance(resp.passages[0], common_pb2.Passage)
