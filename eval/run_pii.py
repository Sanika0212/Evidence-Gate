"""Do identifiers leak? Synthetic identifiers sent through the gateway, every sink scanned.

    python eval/run_pii.py [--requests 1000] [--redactor en_core_web_sm]

Each request carries one synthetic patient: a name, phone number, email, SSN, medical
record number, date of birth (numeric or written out), street address and IP address,
spread over the question, the answer and a passage. All are made up: names from fixed
lists, phone numbers in the fictional 555-01xx range, emails at example.org, SSNs in the
900 area (never issued), IPs in the 203.0.113.0/24 documentation range.

Requests go through the real gateway app in process, with the production redactor and
the model-free checker. About half the answers contain a claim the passages don't
support, so the review and triage-queue paths run too. Afterwards every sink is scanned:
each request's audit record, review items and queued triage message for that request's
own identifiers, and the gateway's JSON logs and OpenTelemetry spans for any of them. A
leak is one seeded identifier found verbatim.

``--without-fixes`` reproduces the first version of the redactor (no street address
pattern, no request-scoped names) for the before and after table in the README.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import random
import re
import time
from collections import Counter, defaultdict
from typing import Any

import structlog
from common import write
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from evidencegate import telemetry
from evidencegate.calibration import Calibrator, MemoryCalibrationStore
from evidencegate.config import Settings
from evidencegate.embedding import HashEmbedder, LexicalReranker
from evidencegate.gateway.app import Backends, Clients, create_app
from evidencegate.gateway.audit_log import MemoryAuditSink
from evidencegate.gateway.auth import NoAuth
from evidencegate.gateway.pii import load_redactor
from evidencegate.gateway.queue import MemoryPublisher
from evidencegate.gateway.ratelimit import MemoryRateLimiter
from evidencegate.gateway.review import MemoryReviewStore, MemoryTenants
from evidencegate.grpc_services import CalibratorServicer, CheckerServicer, RetrieverServicer
from evidencegate.onnx_checker import load_checker
from evidencegate.retrieval import MemoryStore, Retriever

FIRST = [
    "Maria",
    "Aaliyah",
    "Darnell",
    "Priya",
    "Tomasz",
    "Ngozi",
    "Hiroshi",
    "Svetlana",
    "Mateo",
    "Ingrid",
    "Kwame",
    "Rosalind",
    "Dmitri",
    "Esperanza",
    "Yusuf",
    "Annika",
    "Desmond",
    "Ifeoma",
    "Bartholomew",
    "Lucinda",
    "Ravi",
    "Mirela",
    "Thaddeus",
    "Oksana",
    "Leopold",
    "Zainab",
    "Cornelius",
    "Henrietta",
    "Anselm",
    "Philippa",
]
LAST = [
    "Gonzalez",
    "Okafor",
    "Lindqvist",
    "Hendricks",
    "Nakamura",
    "Wojcik",
    "Abernathy",
    "Castellanos",
    "Mbeki",
    "Raghunathan",
    "Fitzgerald",
    "Petrovic",
    "Adeyemi",
    "Vasquez",
    "Kowalczyk",
    "Thorne",
    "Haddad",
    "Brennan",
    "Oyelaran",
    "Delacroix",
    "Yamamoto",
    "Szabo",
    "Whitfield",
    "Iqbal",
    "Montgomery",
    "Novak",
    "Achterberg",
    "Barrientos",
    "Ferreira",
    "Lindgren",
]
STREETS = [
    "Elm",
    "Maple",
    "Linwood",
    "Hertel",
    "Delaware",
    "Bailey",
    "Kenmore",
    "Niagara",
    "Parkside",
    "Richmond",
]
SUFFIXES = ("Street", "Avenue", "Road", "Drive", "Lane")
MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]

# (passage sentence, supported claim, unsupported claim)
FACTS = [
    (
        "Metformin is the first-line medication for type 2 diabetes in most adults.",
        "was started on metformin, the first-line medication for type 2 diabetes",
        "was started on metformin, which cures type 1 diabetes within a week",
    ),
    (
        "Aspirin irreversibly inhibits cyclooxygenase by acetylating a serine residue.",
        "takes aspirin, which irreversibly inhibits cyclooxygenase",
        "takes aspirin, which reversibly activates cyclooxygenase",
    ),
    (
        "Colorectal cancer screening is recommended for adults aged 45 to 75 years.",
        "is due for colorectal cancer screening, recommended for adults aged 45 to 75",
        "is due for colorectal cancer screening, recommended only after age 85",
    ),
    (
        "Lisinopril is an ACE inhibitor used to treat high blood pressure.",
        "is on lisinopril, an ACE inhibitor for high blood pressure",
        "is on lisinopril, an antibiotic for urinary tract infections",
    ),
    (
        "Warfarin requires regular INR monitoring because of its narrow therapeutic range.",
        "needs regular INR checks while on warfarin",
        "needs no monitoring at all while on warfarin",
    ),
    (
        "Influenza vaccination is recommended every year for everyone aged 6 months and older.",
        "should get an influenza vaccine every year",
        "should get an influenza vaccine once every ten years",
    ),
]


def patient(rng: random.Random, i: int) -> dict[str, str]:
    first, last = rng.choice(FIRST), rng.choice(LAST)
    month, day, year = rng.randint(1, 12), rng.randint(1, 28), rng.randint(1935, 2005)
    return {
        "first_name": first,
        "last_name": last,
        "phone": f"716-555-{100 + i % 100:04d}",
        "email": f"{first.lower()}.{last.lower()}{i}@example.org",
        "ssn": f"9{rng.randint(10, 99)}-{rng.randint(10, 99)}-{rng.randint(1000, 9999)}",
        "mrn": f"{rng.randint(10_000_000, 99_999_999)}",
        "dob": (f"{month:02d}/{day:02d}/{year}" if i % 2 else f"{MONTHS[month - 1]} {day}, {year}"),
        "address": f"{rng.randint(10, 9999)} {rng.choice(STREETS)} {rng.choice(SUFFIXES)}",
        "ip": f"203.0.113.{rng.randint(1, 254)}",
    }


def request(rng: random.Random, i: int) -> tuple[dict[str, Any], dict[str, str], bool]:
    p = patient(rng, i)
    fact = rng.choice(FACTS)
    unsupported = i % 2 == 0
    name = f"{p['first_name']} {p['last_name']}"
    question = (
        f"Patient {name}, MRN {p['mrn']}, born {p['dob']}: what is the plan? "
        f"Logged in from {p['ip']}."
    )
    answer = (
        f"{name} {fact[2] if unsupported else fact[1]}. "
        f"Reach {p['first_name']} at {p['phone']} or {p['email']}. "
        f"Home address on file is {p['address']}."
    )
    passages = [
        {"id": f"note-{i}", "text": fact[0]},
        {
            "id": f"demo-{i}",
            "text": f"Registration: {name}, SSN {p['ssn']}, {p['address']}, phone {p['phone']}, "
            f"email {p['email']}, date of birth {p['dob']}.",
        },
    ]
    return {"question": question, "answer": answer, "passages": passages}, p, unsupported


def build(redactor_spec: str) -> tuple[Any, Backends, InMemorySpanExporter, io.StringIO]:
    logs = io.StringIO()
    telemetry.configure("gateway", json_logs=True)
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=logs))
    logging.getLogger().addHandler(logging.StreamHandler(logs))

    store = MemoryStore(HashEmbedder(64))
    store.add("demo", [(f"f{n}", f[0], "facts") for n, f in enumerate(FACTS)])
    calibrator = Calibrator(MemoryCalibrationStore(0.9), audit_rate=0.5)
    backends = Backends(
        clients=Clients(
            RetrieverServicer(Retriever(store, HashEmbedder(64), LexicalReranker()), 3),
            CheckerServicer(load_checker("lexical")),
            CalibratorServicer(calibrator),
            direct=True,
        ),
        verifier=NoAuth("demo"),
        limiter=MemoryRateLimiter(per_minute=1_000_000, burst=1_000_000),
        redactor=load_redactor(redactor_spec),
        audit_sink=MemoryAuditSink(),
        publisher=MemoryPublisher(),
        reviews=MemoryReviewStore(),
        tenants=MemoryTenants(),
        audit_rate=0.5,
        rng=random.Random(0),
    )
    app = create_app(Settings(redactor=redactor_spec), backends)
    spans = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(spans))
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
    return app, backends, spans, logs


def request_sinks(backends: Backends) -> dict[str, dict[str, str]]:
    """Each request's own records, by audit id: audit record, review items, queue message."""
    by: dict[str, dict[str, list[Any]]] = defaultdict(lambda: defaultdict(list))
    for rec in backends.audit_sink.records:  # type: ignore[attr-defined]
        by[rec["id"]]["audit store"].append(rec)
    for item in backends.reviews.items.values():  # type: ignore[attr-defined]
        by[item.audit_id]["review store"].append(vars(item))
    for msg in backends.publisher.messages:  # type: ignore[attr-defined]
        by[msg["audit_id"]]["triage queue"].append(msg)
    return {
        aid: {sink: json.dumps(rows, default=str) for sink, rows in sinks.items()}
        for aid, sinks in by.items()
    }


def shared_sinks(spans: InMemorySpanExporter, logs: io.StringIO) -> dict[str, str]:
    span_dump = [
        {
            "name": s.name,
            "attributes": dict(s.attributes or {}),
            "events": [e.name for e in s.events],
        }
        for s in spans.get_finished_spans()
    ]
    return {"logs": logs.getvalue(), "traces": json.dumps(span_dump, default=str)}


def found(value: str, text: str) -> bool:
    return value in text and bool(re.search(rf"(?<![\w.-]){re.escape(value)}(?![\w-])", text))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--requests", type=int, default=1000)
    ap.add_argument("--redactor", default="en_core_web_sm")
    ap.add_argument(
        "--without-fixes",
        action="store_true",
        help="the first version: no street address pattern, no request-scoped names",
    )
    ap.add_argument("--out", default="pii", help="results go to runs/<out>/results.json")
    args = ap.parse_args()
    if args.without_fixes:
        import evidencegate.gateway.app as gateway_app
        import evidencegate.gateway.pii as pii

        gateway_app.scoped = lambda redactor, texts: redactor  # type: ignore[assignment]
        pii._PATTERNS[:] = [p for p in pii._PATTERNS if p[0] != "STREET_ADDRESS"]

    app, backends, spans, logs = build(args.redactor)
    client = TestClient(app)
    rng = random.Random(0)
    seeded: list[tuple[str, str, str]] = []  # (audit id, kind, value)
    decisions: Counter[str] = Counter()
    started = time.time()
    for i in range(args.requests):
        body, p, _ = request(rng, i)
        r = client.post("/v1/check", json=body)
        r.raise_for_status()
        out = r.json()
        decisions[out["decision"]] += 1
        seeded += [(out["id"], kind, value) for kind, value in p.items()]
        if (i + 1) % 100 == 0:
            print(f"  {i + 1}/{args.requests} requests, {(i + 1) / (time.time() - started):.1f}/s")

    # A seeded identifier leaks if it appears verbatim in its own request's records, or
    # anywhere in the logs or traces (which never carry request text).
    own = request_sinks(backends)
    shared = shared_sinks(spans, logs)
    by_kind: dict[str, Counter[str]] = defaultdict(Counter)
    by_sink: Counter[str] = Counter()
    examples: list[dict[str, Any]] = []
    for audit_id, kind, value in seeded:
        by_kind[kind]["seeded"] += 1
        sinks = {**own.get(audit_id, {}), **shared}
        leaked_in = [name for name, text in sinks.items() if found(value, text)]
        if leaked_in:
            by_kind[kind]["leaked"] += 1
            by_sink.update(leaked_in)
            if len(examples) < 20:
                examples.append({"kind": kind, "value": value, "sinks": leaked_in})
    result = {
        "requests": args.requests,
        "decisions": dict(decisions),
        "redactor": f"Presidio ({args.redactor}) plus regex"
        if args.redactor != "regex"
        else "regex only",
        "fixes": not args.without_fixes,
        "identifiers_seeded": len(seeded),
        "identifiers_leaked": sum(c["leaked"] for c in by_kind.values()),
        "by_kind": {
            k: {"seeded": v["seeded"], "leaked": v["leaked"]} for k, v in sorted(by_kind.items())
        },
        "leaks_by_sink": dict(by_sink),
        "examples": examples,
        "seconds": round(time.time() - started, 1),
    }
    print(json.dumps({k: v for k, v in result.items() if k != "examples"}, indent=1))
    print("wrote", write(args.out, result))


if __name__ == "__main__":
    main()
