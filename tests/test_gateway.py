from __future__ import annotations

import json
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

from evidencegate.gateway.audit_log import verify_chain
from evidencegate.gateway.ratelimit import MemoryRateLimiter

PASSAGES = [
    {
        "id": "p1",
        "text": "Aspirin irreversibly inhibits cyclooxygenase by acetylating a serine residue.",
    },
    {
        "id": "p2",
        "text": "Metformin is the first-line medication for type 2 diabetes in most adults.",
    },
]


def client(stack: dict[str, Any]) -> TestClient:
    return TestClient(stack["app"])


def test_supported_answer_passes(stack: dict[str, Any]) -> None:
    r = client(stack).post(
        "/v1/check",
        json={
            "question": "What does aspirin inhibit?",
            "answer": "Aspirin irreversibly inhibits cyclooxygenase by acetylating a serine residue.",
            "passages": PASSAGES,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["decision"] == "pass"
    assert all(c["verdict"] == "supported" for c in body["claims"])
    assert body["passages"] == []  # the caller sent passages; nothing was retrieved
    assert body["guarantee"]["target_recall"] is None  # tenant not calibrated yet
    assert stack["backends"].publisher.messages == []


def test_unsupported_claim_is_flagged_and_queued(stack: dict[str, Any]) -> None:
    r = client(stack).post(
        "/v1/check",
        json={
            "question": "What does aspirin do?",
            "answer": "Aspirin irreversibly inhibits cyclooxygenase by acetylating a serine residue. "
            "Aspirin permanently cures migraine headaches after one single dose.",
            "passages": PASSAGES,
        },
    )
    body = r.json()
    assert body["decision"] in {"flag", "block"}
    flagged = [c for c in body["claims"] if c["flagged"]]
    assert flagged
    assert "migraine" in flagged[0]["text"]
    assert flagged[0]["cause"]
    msgs = stack["backends"].publisher.messages
    assert len(msgs) == 1
    assert msgs[0]["flagged"] == [flagged[0]["index"]]
    assert json.loads(msgs[0]["audit"])["answer"].startswith("Aspirin")


def test_retrieves_evidence_when_none_is_sent(stack: dict[str, Any]) -> None:
    r = client(stack).post(
        "/v1/check",
        json={
            "question": "Which medication is first-line for type 2 diabetes?",
            "answer": "Metformin is the first-line medication for type 2 diabetes in most adults.",
        },
    )
    body = r.json()
    assert body["passages"]
    assert body["passages"][0]["id"] == "p2"
    assert body["decision"] == "pass"
    assert "retrieve_ms" in body["latency_ms"]


def test_rate_limit_returns_429(stack: dict[str, Any]) -> None:
    stack["backends"].limiter = MemoryRateLimiter(per_minute=60, burst=1)
    c = client(stack)
    payload = {"answer": "Metformin treats diabetes.", "passages": PASSAGES}
    assert c.post("/v1/check", json=payload).status_code == 200
    r = c.post("/v1/check", json=payload)
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) >= 1


def test_validation_rejects_empty_answers(stack: dict[str, Any]) -> None:
    assert client(stack).post("/v1/check", json={"answer": ""}).status_code == 422


def test_pii_never_reaches_storage(stack: dict[str, Any]) -> None:
    c = client(stack)
    r = c.post(
        "/v1/check",
        json={
            "question": "Patient MRN 12345678, phone 716-555-0142: what does aspirin inhibit?",
            "answer": "For SSN 123-45-6789 the drug cures migraine forever, email jo@example.com. "
            "Aspirin inhibits cyclooxygenase, call 716-555-0199.",
            "passages": [
                *PASSAGES,
                {"id": "p3", "text": "Call 716-555-0199 to reach the aspirin clinic."},
            ],
        },
    )
    assert r.json()["decision"] != "pass"
    backends = stack["backends"]
    sink = backends.audit_sink
    published = backends.publisher.messages
    assert published, "a flagged answer is queued for triage"
    dumped = (
        json.dumps(sink.records)
        + json.dumps([vars(i) for i in backends.reviews.items.values()])
        + json.dumps(published)
    )
    for secret in ("12345678", "716-555-0142", "123-45-6789", "jo@example.com", "716-555-0199"):
        assert secret not in dumped
    # The queued audit stays well formed: claim offsets index the redacted answer.
    audit = json.loads(published[-1]["audit"])
    for claim in audit["claims"]:
        assert audit["answer"][claim["start"] : claim["end"]] == claim["text"]
    record = sink.records[-1]
    assert record["pii_redacted"]["US_SSN"] == 1
    assert len(record["answer_sha256"]) == 64


def test_audit_records_form_a_valid_hash_chain(stack: dict[str, Any]) -> None:
    c = client(stack)
    for answer in ("Metformin treats diabetes.", "Aspirin inhibits cyclooxygenase."):
        c.post("/v1/check", json={"answer": answer, "passages": PASSAGES})
    records = stack["backends"].audit_sink.records
    assert verify_chain(records)
    tampered = [dict(r) for r in records]
    tampered[0]["decision"] = "pass" if tampered[0]["decision"] != "pass" else "block"
    assert not verify_chain(tampered)


def test_review_loop_feeds_the_calibrator(stack: dict[str, Any]) -> None:
    c = client(stack)
    c.post(
        "/v1/check",
        json={
            "answer": "Aspirin permanently cures migraine headaches after one single dose.",
            "passages": PASSAGES,
        },
    )
    assert c.get("/review").status_code == 401
    page = c.get("/review", params={"token": "secret-token"})
    assert page.status_code == 200
    assert "migraine" in page.text
    item = stack["backends"].reviews.pending(None)[0]
    r = c.post(
        f"/review/{item.id}",
        data={"label": "unsupported", "token": "secret-token", "tenant": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303
    labels = stack["calibrator"].store.labels("demo")
    assert len(labels) == 1
    assert labels[0].unsupported
    assert labels[0].source == "review"
    again = c.post("/v1/feedback", json={"item_id": item.id, "label": "supported"})
    assert again.status_code == 409


def test_feedback_api_and_thresholds(stack: dict[str, Any]) -> None:
    c = client(stack)
    rng = np.random.default_rng(0)
    cal = stack["calibrator"]
    for s in rng.beta(2, 5, 100):
        cal.add_label("demo", float(s), True, False, "seed")
    for s in rng.beta(5, 2, 100):
        cal.add_label("demo", float(s), False, False, "seed")
    cal.fit("demo", 0.9)
    t = c.get("/v1/thresholds").json()
    assert t["target_recall"] == pytest.approx(0.9)
    assert t["version"] == 1
    r = c.post("/v1/check", json={"answer": "Metformin treats diabetes.", "passages": PASSAGES})
    g = r.json()["guarantee"]
    assert g["target_recall"] == pytest.approx(0.9)
    assert g["false_alarm_rate"] is not None
    assert c.post("/v1/feedback", json={"item_id": "nope", "label": "supported"}).status_code == 404


def test_health_metrics_and_demo(stack: dict[str, Any]) -> None:
    c = client(stack)
    assert c.get("/healthz").json()["status"] == "ok"
    assert c.get("/readyz").status_code == 200
    c.post("/v1/check", json={"answer": "Metformin treats diabetes.", "passages": PASSAGES})
    metrics = c.get("/metrics").text
    assert "eg_claims_total" in metrics
    assert "eg_decisions_total" in metrics
    assert "Evidence Gate" in c.get("/").text
    demo = c.post("/demo/check", json={"answer": "Metformin is the first-line medication."})
    assert demo.status_code == 200
