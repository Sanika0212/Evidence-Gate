"""Every CLI command, against real Postgres (services job) and in-process gRPC servers."""

from __future__ import annotations

import json
import os
import socket
import uuid
from pathlib import Path
from typing import Any

import pytest

from evidencegate import cli

pytestmark = pytest.mark.services

DB = os.environ.get(
    "EG_DATABASE_URL", "postgresql://evidencegate:evidencegate@localhost:5432/evidencegate"
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    values = {
        "EG_DATABASE_URL": DB,
        "EG_CHECKER": "lexical",
        "EG_EMBEDDER": "hash",
        "EG_RERANKER": "lexical",
        "EG_MODELS_DIR": str(tmp_path),
        "EG_LOG_JSON": "false",
        "EG_METRICS_PORT": str(free_port()),
    }
    for k, v in values.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(cli, "configure", lambda *a, **k: None)
    return values


def test_migrate_and_ingest_jsonl(env: dict[str, str], tmp_path: Path, capsys: Any) -> None:
    cli.main(["migrate"])
    corpus = tmp_path / "c.jsonl"
    corpus.write_text(
        "\n".join(
            json.dumps({"id": f"d{i}", "text": f"passage {i} about aspirin"}) for i in range(5)
        )
    )
    tenant = f"cli-{uuid.uuid4().hex[:6]}"
    cli.main(["ingest", "--tenant", tenant, "--jsonl", str(corpus), "--batch", "2", "--limit", "4"])
    out = capsys.readouterr().out
    assert "migrated" in out
    assert f"ingested 4 passages for {tenant}" in out


@pytest.mark.parametrize("service", ["checker", "retriever", "calibrator"])
def test_serve_grpc_services(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, service: str
) -> None:
    started: list[Any] = []

    def stop(server: Any) -> None:
        started.append(server)
        server.stop(grace=None)

    monkeypatch.setattr(cli, "_wait_forever", stop)
    cli.main(["serve", service, "--port", str(free_port())])
    assert len(started) == 1


def test_serve_http_services(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    apps: list[Any] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: apps.append(app))
    monkeypatch.setenv("EG_AUTH_MODE", "none")
    cli.main(["serve", "devauth"])
    cli.main(["serve", "gateway", "--port", "18080"])
    assert [a.title for a in apps] == ["Evidence Gate dev issuer", "Evidence Gate"]


def test_worker_starts_and_stops(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    import boto3

    from evidencegate import triage

    class SQS:
        def get_queue_url(self, QueueName: str) -> dict[str, str]:
            return {"QueueUrl": "http://q"}

    monkeypatch.setattr(boto3, "client", lambda *a, **k: SQS())
    ran: list[bool] = []
    monkeypatch.setattr(triage.TriageWorker, "run", lambda self, **k: ran.append(True))
    cli.main(["worker", "triage"])
    assert ran == [True]


def test_recalibrate_and_drift_over_grpc(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    import numpy as np
    from psycopg_pool import ConnectionPool

    from evidencegate.calibration import Calibrator, Label, PostgresCalibrationStore
    from evidencegate.grpc_services import CalibratorServicer, build_server
    from evidencegate.v1 import calibrator_pb2_grpc

    pool = ConnectionPool(DB, min_size=1, max_size=2, open=True)
    store = PostgresCalibrationStore(pool)
    store.migrate()
    tenant = f"cli-{uuid.uuid4().hex[:6]}"
    rng = np.random.default_rng(0)
    store.add_labels(tenant, [Label(float(s), True) for s in rng.beta(2, 5, 60)])
    store.add_labels(tenant, [Label(float(s), False) for s in rng.beta(5, 2, 60)])
    port = free_port()
    server = build_server(
        port,
        [
            (
                calibrator_pb2_grpc.add_CalibratorServiceServicer_to_server,
                CalibratorServicer(Calibrator(store)),
            )
        ],
    )
    server.start()
    monkeypatch.setenv("EG_CALIBRATOR_ADDR", f"127.0.0.1:{port}")
    try:
        cli.main(["recalibrate", "--tenant", tenant, "--force"])
        assert store.latest(tenant) is not None
        cli.main(["recalibrate", "--all"])
        capsys.readouterr()
        cli.main(["drift", "--tenant", tenant])
        out = capsys.readouterr().out
        report = json.loads(out[out.index("{\n") :])
        assert "psi" in report
        assert report["needsRecalibration"] in (True, False)
    finally:
        server.stop(grace=None)
        pool.close()


def test_token_command(env: dict[str, str], monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    import httpx

    class Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"access_token": "abc"}

    monkeypatch.setattr(httpx, "post", lambda *a, **k: Resp())
    cli.main(["token"])
    assert capsys.readouterr().out.strip() == "abc"
