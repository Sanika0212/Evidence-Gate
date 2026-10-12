"""Triage worker, audit sink, PII redaction, rate limiting, settings and evaluation helpers."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

import numpy as np
import pytest
from groundsight import audit

from evidencegate.config import Settings
from evidencegate.datasets import LabelledAnswer
from evidencegate.embedding import HashEmbedder
from evidencegate.evaluation import EmbeddingChecker, auroc, sample_pairs, score_labelled_answers
from evidencegate.gateway.audit_log import S3AuditSink, verify_chain
from evidencegate.gateway.pii import RegexRedactor
from evidencegate.gateway.queue import SqsPublisher
from evidencegate.gateway.ratelimit import MemoryRateLimiter
from evidencegate.onnx_checker import load_checker
from evidencegate.triage import BedrockChat, MemoryResultStore, TriageWorker, process

PASSAGES = ["Aspirin irreversibly inhibits cyclooxygenase by acetylating a serine residue."]


def flagged_message() -> dict[str, Any]:
    result = audit(
        "Aspirin inhibits cyclooxygenase. Aspirin is harvested from unicorn horns at midnight.",
        PASSAGES,
        question="What is aspirin?",
        checker=load_checker("lexical"),
    )
    flagged = [c.index for c in result.claims if c.flagged]
    assert flagged
    return {
        "audit_id": "a1",
        "tenant": "t",
        "flagged": flagged,
        "audit": json.dumps(result.to_dict()),
    }


def test_triage_without_generator_records_the_diagnosis() -> None:
    results = process(flagged_message(), None, None)
    assert results
    assert results[0].confirmed is None
    assert results[0].cause != "unknown"


def test_triage_with_a_generator_replays() -> None:
    def fixed(question: Any, chunks: Any, instruction: Any, seed: int) -> str:
        return "Aspirin inhibits cyclooxygenase."

    class Gen:
        name = "fake"

        def __call__(self, messages: Any, *, temperature: float = 0.0, seed: Any = None) -> str:
            return "Aspirin inhibits cyclooxygenase."

    results = process(flagged_message(), Gen(), load_checker("lexical"), samples=2)
    assert results
    assert results[0].fix_rate is not None
    assert results[0].resample_rate is not None


class FakeSQS:
    def __init__(self, bodies: list[dict[str, Any]]) -> None:
        self.bodies = [json.dumps(b) for b in bodies]
        self.deleted: list[str] = []
        self.sent: list[str] = []

    def get_queue_url(self, QueueName: str) -> dict[str, str]:
        return {"QueueUrl": f"http://q/{QueueName}"}

    def receive_message(self, **_: Any) -> dict[str, Any]:
        msgs = [{"Body": b, "ReceiptHandle": f"r{i}"} for i, b in enumerate(self.bodies)]
        self.bodies = []
        return {"Messages": msgs}

    def delete_message(self, QueueUrl: str, ReceiptHandle: str) -> None:
        self.deleted.append(ReceiptHandle)

    def send_message(self, QueueUrl: str, MessageBody: str) -> None:
        self.sent.append(MessageBody)


def test_worker_is_idempotent_and_survives_poison_messages() -> None:
    good = flagged_message()
    sqs = FakeSQS([good, {"audit_id": "bad"}, good])
    store = MemoryResultStore()
    worker = TriageWorker(sqs, "triage", store, None, None, wait_s=0)
    assert worker.poll_once() == 2
    assert sqs.deleted == ["r0", "r2"]  # the poison message stays for the redrive policy
    assert len(store.rows) == len(good["flagged"])  # redelivery did not duplicate
    worker.run(should_stop=lambda: True)


def test_sqs_publisher() -> None:
    sqs = FakeSQS([])
    pub = SqsPublisher(sqs, "triage")
    pub.publish({"x": 1})
    assert json.loads(sqs.sent[0]) == {"x": 1}
    assert pub.url == "http://q/triage"


def test_bedrock_chat_converse_shape() -> None:
    class Client:
        def converse(self, **kw: Any) -> dict[str, Any]:
            assert kw["system"] == [{"text": "sys"}]
            assert kw["messages"][0]["content"][0]["text"] == "hi"
            return {
                "output": {"message": {"content": [{"text": "hello"}]}},
                "usage": {"inputTokens": 3, "outputTokens": 1},
            }

    chat = BedrockChat("m", client=Client())
    out = chat([{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}])
    assert out == "hello"
    assert chat.input_tokens == 3
    assert chat.name == "bedrock:m"


class FakeS3:
    def __init__(self, fail: int = 0) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail = fail

    def put_object(self, **kw: Any) -> None:
        if self.fail:
            self.fail -= 1
            raise ConnectionError("down")
        self.objects[kw["Key"]] = kw["Body"]


def test_s3_audit_sink_batches_and_retries() -> None:
    s3 = FakeS3(fail=1)
    sink = S3AuditSink(s3, "b", batch=2, interval_s=3600)
    sink.write({"n": 1})
    sink.write({"n": 2})  # batch full: flush fails and the records are kept
    assert s3.objects == {}
    sink.write({"n": 3})
    sink.flush()
    rows = [json.loads(line) for body in s3.objects.values() for line in body.decode().splitlines()]
    assert [r["n"] for r in rows] == [1, 2, 3]
    assert verify_chain(rows)


def test_regex_redactor() -> None:
    text, counts = RegexRedactor().redact(
        "Call 716-555-0142 or jo@x.org about MRN: 99887766 seen 03/04/2025 from 10.0.0.1."
    )
    assert counts == Counter(
        {"PHONE_NUMBER": 1, "EMAIL_ADDRESS": 1, "MRN": 1, "DATE_TIME": 1, "IP_ADDRESS": 1}
    )
    assert "716" not in text
    assert "<MRN>" in text


def test_presidio_redactor_catches_names() -> None:
    pytest.importorskip("spacy")
    import spacy.util

    if not spacy.util.is_package("en_core_web_sm"):
        pytest.skip("spaCy model not installed")
    from evidencegate.gateway.pii import PresidioRedactor

    text, counts = PresidioRedactor().redact(
        "John Smith (MRN 12345678) was seen in Buffalo; SSN 123-45-6789."
    )
    assert "John Smith" not in text
    assert "12345678" not in text
    assert "123-45-6789" not in text
    assert counts["PERSON"] >= 1


def test_memory_rate_limiter_refills() -> None:
    now = [0.0]
    lim = MemoryRateLimiter(per_minute=60, burst=2, clock=lambda: now[0])
    assert lim.allow("t")[0]
    assert lim.allow("t")[0]
    ok, retry = lim.allow("t")
    assert not ok
    assert retry == pytest.approx(1.0)
    now[0] += 1.0
    assert lim.allow("t")[0]


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EG_RATE_LIMIT_PER_MINUTE", "7")
    monkeypatch.setenv("EG_DEMO", "true")
    monkeypatch.setenv("EG_AUDIT_SAMPLE_RATE", "0.1")
    monkeypatch.setenv("EG_CHECKER", "lexical")
    s = Settings.from_env(retrieve_k=9)
    assert (s.rate_limit_per_minute, s.demo, s.audit_sample_rate, s.checker, s.retrieve_k) == (
        7,
        True,
        0.1,
        "lexical",
        9,
    )


def test_auroc_matches_brute_force() -> None:
    rng = np.random.default_rng(0)
    scores = list(np.round(rng.random(60), 1))  # ties on purpose
    labels = list(rng.random(60) < 0.4)
    pos = [s for s, y in zip(scores, labels, strict=True) if y]
    neg = [s for s, y in zip(scores, labels, strict=True) if not y]
    brute = np.mean([(p < n) + 0.5 * (p == n) for p in pos for n in neg])
    assert auroc(scores, labels) == pytest.approx(brute)


def test_answer_scoring_and_embedding_baseline() -> None:
    items = [
        LabelledAnswer("1-gt", "q", PASSAGES, "Aspirin inhibits cyclooxygenase.", False),
        LabelledAnswer("1-h", "q", PASSAGES, "Aspirin activates the moon.", True),
    ]
    assert sample_pairs(items * 3, 2, 0)
    assert len(sample_pairs(items, 0, 0)) == 2
    scored = score_labelled_answers(items, load_checker("lexical"))
    assert scored[0].support > scored[1].support
    emb = EmbeddingChecker(HashEmbedder(64))
    s = emb.score([(PASSAGES[0], "aspirin cyclooxygenase"), (PASSAGES[0], "moon rocks")])
    assert s[0][0] > s[1][0]
