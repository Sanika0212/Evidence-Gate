"""Command line: every service, job and one-off task is a subcommand.

evidencegate serve gateway|checker|retriever|calibrator|devauth [--port N]
evidencegate worker triage
evidencegate migrate
evidencegate ingest --tenant demo --dataset bioasq
evidencegate seed --tenant demo --dataset scifact --target 0.9
evidencegate recalibrate --all
evidencegate drift --tenant demo
evidencegate token --url http://localhost:8081 --client-id demo-client --secret demo-secret
"""

from __future__ import annotations

import argparse
import json
import signal
import time
from pathlib import Path
from typing import Any

from evidencegate.config import Settings
from evidencegate.telemetry import configure, get_logger

DEFAULT_PORTS = {
    "gateway": 8080,
    "devauth": 8081,
    "retriever": 50051,
    "checker": 50052,
    "calibrator": 50053,
}


def _pool(settings: Settings) -> Any:
    from psycopg_pool import ConnectionPool

    return ConnectionPool(settings.database_url, min_size=1, max_size=10, open=True)


def _wait_forever(server: Any) -> None:
    stop = {"flag": False}

    def handle(*_: Any) -> None:
        stop["flag"] = True
        server.stop(grace=10)

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)
    server.wait_for_termination()


def serve(args: argparse.Namespace, settings: Settings) -> None:
    log = get_logger("evidencegate.serve")
    port = args.port or DEFAULT_PORTS[args.service]
    if args.service in ("gateway", "devauth"):
        import uvicorn

        if args.service == "gateway":
            from evidencegate.gateway.app import create_app
            from evidencegate.grpc_services import instrument_clients

            instrument_clients()
            app = create_app(settings)
            started = time.perf_counter()
            app.state.warm_up()
            log.info("gateway_ready", warmup_s=round(time.perf_counter() - started, 2))
            try:
                from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

                FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz,metrics")
            except ImportError:  # pragma: no cover
                pass
        else:
            from evidencegate.devauth import create_app as create_devauth

            app = create_devauth(settings.jwt_issuer, settings.jwt_audience)
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning", access_log=False)
        return

    import os

    from evidencegate import grpc_services as gs
    from evidencegate.v1 import calibrator_pb2_grpc, checker_pb2_grpc, retriever_pb2_grpc

    if args.service == "checker":
        from evidencegate.onnx_checker import load_checker

        checker = load_checker(settings.checker, settings.models_dir)
        started = time.perf_counter()
        checker.score([("Warm-up passage about aspirin.", "Aspirin is a drug.")])
        log.info(
            "checker_ready",
            checker=settings.checker,
            warmup_s=round(time.perf_counter() - started, 2),
        )
        server = gs.build_server(
            port,
            [
                (
                    checker_pb2_grpc.add_CheckerServiceServicer_to_server,
                    gs.CheckerServicer(
                        checker,
                        fault_rate=float(os.environ.get("EG_FAULT_RATE", "0") or 0),
                        concurrency=settings.checker_concurrency,
                    ),
                )
            ],
            # Requests waiting for an inference slot hold a worker thread; keep spare
            # threads so health checks are always answered.
            workers=settings.checker_concurrency + 8,
        )
    elif args.service == "retriever":
        from evidencegate.embedding import load_embedder, load_reranker
        from evidencegate.retrieval import Mode, PostgresStore, Retriever

        store = PostgresStore(_pool(settings), settings.embedding_dim)
        store.migrate()
        retriever = Retriever(
            store,
            load_embedder(settings.embedder, settings.models_dir, settings.embedding_dim),
            load_reranker(settings.reranker, settings.models_dir),
        )
        server = gs.build_server(
            port,
            [
                (
                    retriever_pb2_grpc.add_RetrieverServiceServicer_to_server,
                    gs.RetrieverServicer(
                        retriever, settings.retrieve_k, Mode(settings.retrieval_mode)
                    ),
                )
            ],
        )
    else:
        from evidencegate.calibration import Calibrator, PostgresCalibrationStore

        cal_store = PostgresCalibrationStore(_pool(settings), settings.default_target_recall)
        cal_store.migrate()
        calibrator = Calibrator(cal_store, audit_rate=settings.audit_sample_rate)
        server = gs.build_server(
            port,
            [
                (
                    calibrator_pb2_grpc.add_CalibratorServiceServicer_to_server,
                    gs.CalibratorServicer(calibrator),
                )
            ],
        )
    from prometheus_client import start_http_server

    metrics_port = int(os.environ.get("EG_METRICS_PORT", "9100"))
    start_http_server(metrics_port)
    server.start()
    log.info("serving", service=args.service, port=port, metrics_port=metrics_port)
    _wait_forever(server)


def worker(args: argparse.Namespace, settings: Settings) -> None:
    import os

    import boto3

    from evidencegate.onnx_checker import load_checker
    from evidencegate.triage import PostgresResultStore, TriageWorker, load_generator

    store = PostgresResultStore(_pool(settings))
    store.migrate()
    sqs = boto3.client(
        "sqs", endpoint_url=settings.sqs_endpoint or None, region_name=settings.aws_region
    )
    generator = load_generator(os.environ.get("EG_TRIAGE_GENERATOR", "none"), settings.aws_region)
    checker = load_checker(settings.checker, settings.models_dir) if generator is not None else None
    from prometheus_client import start_http_server

    start_http_server(int(os.environ.get("EG_METRICS_PORT", "9100")))
    w = TriageWorker(
        sqs,
        settings.triage_queue,
        store,
        generator,
        checker,
        samples=int(os.environ.get("EG_TRIAGE_SAMPLES", "3")),
    )
    signal.signal(signal.SIGTERM, lambda *_: setattr(w, "running", False))
    get_logger("evidencegate.worker").info(
        "worker_started", generator=getattr(generator, "name", "none")
    )
    w.run()


def migrate(_: argparse.Namespace, settings: Settings) -> None:
    from evidencegate.calibration import PostgresCalibrationStore
    from evidencegate.gateway.review import PostgresReviewStore
    from evidencegate.retrieval import PostgresStore
    from evidencegate.triage import PostgresResultStore

    pool = _pool(settings)
    PostgresStore(pool, settings.embedding_dim).migrate()
    PostgresCalibrationStore(pool).migrate()
    PostgresReviewStore(pool).migrate()
    PostgresResultStore(pool).migrate()
    print("migrated")


def ingest(args: argparse.Namespace, settings: Settings) -> None:
    from evidencegate import datasets
    from evidencegate.embedding import load_embedder
    from evidencegate.retrieval import PostgresStore

    store = PostgresStore(_pool(settings), settings.embedding_dim)
    store.migrate()
    embedder = load_embedder(settings.embedder, settings.models_dir, settings.embedding_dim)
    if args.jsonl:
        with open(args.jsonl) as fh:
            rows = [
                (str(r["id"]), str(r["text"]), str(r.get("source", "")))
                for r in map(json.loads, fh)
            ]
    else:
        rows = list(datasets.bioasq_corpus())
    if args.limit:
        rows = rows[: args.limit]
    # Similar lengths per batch waste less padding.
    rows.sort(key=lambda r: len(r[1]))
    started, total = time.perf_counter(), 0
    for i in range(0, len(rows), args.batch):
        chunk = rows[i : i + args.batch]
        total += store.add(args.tenant, chunk, embedder.embed([t for _, t, _ in chunk]))
        print(f"\r{total}/{len(rows)} passages", end="", flush=True)
    store.refresh_stats(args.tenant)
    print(f"\ningested {total} passages for {args.tenant} in {time.perf_counter() - started:.0f}s")


def reindex(args: argparse.Namespace, settings: Settings) -> None:
    from evidencegate.retrieval import PostgresStore

    store = PostgresStore(_pool(settings), settings.embedding_dim)
    store.migrate()
    started = time.perf_counter()
    store.reindex(args.tenant)
    print(f"reindexed {store.count(args.tenant)} passages in {time.perf_counter() - started:.0f}s")


def seed(args: argparse.Namespace, settings: Settings) -> None:
    """Score a public labelled set with the checker and store it as the tenant's seed labels."""
    from evidencegate.calibration import Calibrator, Label, PostgresCalibrationStore
    from evidencegate.evaluation import score_answers
    from evidencegate.onnx_checker import load_checker

    store = PostgresCalibrationStore(_pool(settings), settings.default_target_recall)
    store.migrate()
    if args.scores:
        # Scores cached by eval/ with the served checker's support model.
        from evidencegate.evaluation import Scored

        scored = [Scored(**r) for r in json.loads(Path(args.scores).read_text())]
    else:
        checker = load_checker(settings.checker, settings.models_dir)
        scored = score_answers(args.dataset, checker, limit=args.limit, seed=args.seed)
    labels = [Label(s.support, s.unsupported, False, "seed", 1.0) for s in scored]
    store.add_labels(args.tenant, labels)
    store.set_target_recall(args.tenant, args.target)
    fitted, reason = Calibrator(store).fit(args.tenant, args.target, min_unsupported=30)
    print(
        json.dumps(
            {
                "labels": len(labels),
                "fitted": fitted is not None,
                "reason": reason,
                "threshold": getattr(fitted, "support", None),
                "false_alarm_rate": getattr(fitted, "false_alarm_rate", None),
            }
        )
    )


def _calibrator_stub(settings: Settings) -> Any:
    from evidencegate.grpc_services import channel
    from evidencegate.v1 import calibrator_pb2_grpc

    return calibrator_pb2_grpc.CalibratorServiceStub(channel(settings.calibrator_addr))


def recalibrate(args: argparse.Namespace, settings: Settings) -> None:
    """The scheduled job: check drift per tenant and refit when it asks for it."""
    from evidencegate.v1 import calibrator_pb2

    stub = _calibrator_stub(settings)
    tenants = args.tenant or []
    if args.all:
        pool = _pool(settings)
        with pool.connection() as conn:
            tenants = [
                r[0]
                for r in conn.execute("SELECT DISTINCT tenant FROM calibrator.labels").fetchall()
            ]
    log = get_logger("evidencegate.recalibrate")
    for tenant in tenants:
        # The calibrator exports the drift gauges when it answers this call.
        d = stub.Drift(calibrator_pb2.DriftRequest(tenant=tenant, window=2000), timeout=60)
        if d.needs_recalibration or args.force:
            f = stub.Fit(
                calibrator_pb2.FitRequest(tenant=tenant, min_unsupported=args.min_unsupported),
                timeout=60,
            )
            log.info(
                "recalibration",
                tenant=tenant,
                reason=d.reason,
                fitted=f.fitted,
                detail=f.reason,
                threshold=f.result.thresholds.support if f.fitted else None,
            )
        else:
            log.info(
                "drift_ok", tenant=tenant, psi=round(d.psi, 3), recall=round(d.recall_estimate, 3)
            )


def drift(args: argparse.Namespace, settings: Settings) -> None:
    from google.protobuf.json_format import MessageToDict

    from evidencegate.v1 import calibrator_pb2

    d = _calibrator_stub(settings).Drift(
        calibrator_pb2.DriftRequest(tenant=args.tenant[0], window=args.window), timeout=60
    )
    print(json.dumps(MessageToDict(d, always_print_fields_with_no_presence=True), indent=2))


def token(args: argparse.Namespace, _: Settings) -> None:
    import httpx

    r = httpx.post(
        f"{args.url.rstrip('/')}/oauth2/token",
        data={"grant_type": "client_credentials", "scope": args.scope},
        auth=(args.client_id, args.secret),
        timeout=10,
    )
    r.raise_for_status()
    print(r.json()["access_token"])


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="evidencegate", description="Evidence Gate services and jobs"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("serve", help="run a service")
    p.add_argument("service", choices=sorted(DEFAULT_PORTS))
    p.add_argument("--port", type=int, default=0)
    p = sub.add_parser("worker", help="run a queue worker")
    p.add_argument("kind", choices=["triage"])
    sub.add_parser("migrate", help="create or update database schemas")
    p = sub.add_parser("ingest", help="load a corpus into the retriever")
    p.add_argument("--tenant", default="demo")
    p.add_argument("--dataset", default="bioasq", choices=["bioasq"])
    p.add_argument("--jsonl", help="instead of a dataset: JSON lines with id, text, source")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--batch", type=int, default=256)
    p = sub.add_parser("reindex", help="rebuild a tenant's BM25 postings and statistics")
    p.add_argument("--tenant", default="demo")
    p = sub.add_parser("seed", help="seed a tenant's calibration from a public labelled set")
    p.add_argument("--tenant", default="demo")
    p.add_argument("--dataset", default="scifact")
    p.add_argument("--target", type=float, default=0.9)
    p.add_argument("--limit", type=int, default=2000)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--scores", help="seed from cached scores (runs/scores/*.json) instead")
    p = sub.add_parser("recalibrate", help="drift check and refit (the CronJob)")
    p.add_argument("--tenant", action="append")
    p.add_argument("--all", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--min-unsupported", type=int, default=30)
    p = sub.add_parser("drift", help="print a tenant's drift report")
    p.add_argument("--tenant", action="append", required=True)
    p.add_argument("--window", type=int, default=2000)
    p = sub.add_parser("token", help="get a token from the development issuer")
    p.add_argument("--url", default="http://localhost:8081")
    p.add_argument("--client-id", default="demo-client")
    p.add_argument("--secret", default="demo-secret")
    p.add_argument("--scope", default="check label")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    name = (
        f"evidencegate-{getattr(args, 'service', None) or getattr(args, 'kind', None) or args.cmd}"
    )
    configure(name, level=settings.log_level, json_logs=settings.log_json)
    handlers = {
        "serve": serve,
        "worker": worker,
        "migrate": migrate,
        "ingest": ingest,
        "reindex": reindex,
        "seed": seed,
        "recalibrate": recalibrate,
        "drift": drift,
        "token": token,
    }
    handlers[args.cmd](args, settings)


if __name__ == "__main__":  # pragma: no cover
    main()
