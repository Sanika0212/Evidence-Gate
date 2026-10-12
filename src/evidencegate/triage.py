"""The triage worker: confirm or reject each flagged claim's cause by replay.

It consumes the queue the gateway publishes to. For every flagged audit it runs
Groundsight's :func:`verify`: the answer is regenerated with the fix the cause implies
(drop a misleading passage, promote buried evidence, tighten the instruction) and
without it, and the cause counts as confirmed when the fix beats plain resampling.
Without a configured generator it records the rule-based diagnosis only.

Results are upserted by ``(audit_id, claim_index)``, so a redelivered message (SQS is
at-least-once) does not create duplicates. A message is deleted only after its
results are stored; poison messages move to the dead-letter queue after the
redrive limit configured on the queue.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from evidencegate.telemetry import TRIAGE_DONE, get_logger

log = get_logger(__name__)


@dataclass
class TriageResult:
    audit_id: str
    tenant: str
    claim_index: int
    cause: str
    confirmed: bool | None  # None when no replay was run
    fix_rate: float | None
    resample_rate: float | None
    intervention: str


class ResultStore(Protocol):
    def upsert(self, results: list[TriageResult]) -> None: ...


class MemoryResultStore:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, int], TriageResult] = {}

    def upsert(self, results: list[TriageResult]) -> None:
        for r in results:
            self.rows[(r.audit_id, r.claim_index)] = r


SCHEMA = """
CREATE SCHEMA IF NOT EXISTS triage;
CREATE TABLE IF NOT EXISTS triage.results (
    audit_id      text NOT NULL,
    claim_index   integer NOT NULL,
    tenant        text NOT NULL,
    cause         text NOT NULL,
    confirmed     boolean,
    fix_rate      double precision,
    resample_rate double precision,
    intervention  text NOT NULL,
    updated       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (audit_id, claim_index)
);
"""


class PostgresResultStore:
    def __init__(self, pool: Any) -> None:
        self.pool = pool

    def migrate(self) -> None:
        with self.pool.connection() as conn:
            conn.execute("SELECT pg_advisory_lock(4204)")
            try:
                conn.execute(SCHEMA)
            finally:
                conn.execute("SELECT pg_advisory_unlock(4204)")

    def upsert(self, results: list[TriageResult]) -> None:
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO triage.results (audit_id, claim_index, tenant, cause, confirmed, fix_rate, "
                "resample_rate, intervention) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (audit_id, claim_index) DO UPDATE SET cause = EXCLUDED.cause, "
                "confirmed = EXCLUDED.confirmed, fix_rate = EXCLUDED.fix_rate, "
                "resample_rate = EXCLUDED.resample_rate, intervention = EXCLUDED.intervention, updated = now()",
                [
                    (
                        r.audit_id,
                        r.claim_index,
                        r.tenant,
                        r.cause,
                        r.confirmed,
                        r.fix_rate,
                        r.resample_rate,
                        r.intervention,
                    )
                    for r in results
                ],
            )


class BedrockChat:
    """A Groundsight ``Generator`` backed by Amazon Bedrock's Converse API."""

    def __init__(
        self, model_id: str, client: Any = None, region: str = "us-east-1", max_tokens: int = 400
    ) -> None:
        if client is None:
            import boto3

            client = boto3.client("bedrock-runtime", region_name=region)
        self.client = client
        self.model_id = model_id
        self.max_tokens = max_tokens
        self.name = f"bedrock:{model_id}"
        self.input_tokens = 0
        self.output_tokens = 0

    def __call__(
        self, messages: list[dict[str, str]], *, temperature: float = 0.0, seed: int | None = None
    ) -> str:
        system = [{"text": m["content"]} for m in messages if m["role"] == "system"]
        convo = [
            {"role": m["role"], "content": [{"text": m["content"]}]}
            for m in messages
            if m["role"] != "system"
        ]
        resp = self.client.converse(
            modelId=self.model_id,
            system=system,
            messages=convo,
            inferenceConfig={"temperature": temperature, "maxTokens": self.max_tokens},
        )
        usage = resp.get("usage", {})
        self.input_tokens += int(usage.get("inputTokens", 0))
        self.output_tokens += int(usage.get("outputTokens", 0))
        return "".join(part.get("text", "") for part in resp["output"]["message"]["content"])


def load_generator(spec: str, region: str = "us-east-1") -> Any:
    """``bedrock:<model id>``, ``openai:<model>`` or ``none``."""
    if not spec or spec == "none":
        return None
    kind, _, model = spec.partition(":")
    if kind == "bedrock":
        return BedrockChat(model, region=region)
    if kind == "openai":
        from groundsight import OpenAIChat

        return OpenAIChat(model=model)
    raise ValueError(f"unknown generator {spec!r}")


def process(
    message: dict[str, Any], generator: Any, checker: Any, *, samples: int = 3
) -> list[TriageResult]:
    """Triage one flagged audit; pure apart from the generator calls."""
    from groundsight import Audit, PromptResponder, verify

    audit = Audit.from_dict(json.loads(message["audit"]))
    flagged = [int(i) for i in message.get("flagged", [])]
    results: list[TriageResult] = []
    by_index = {c.index: c for c in audit.claims}
    if generator is None:
        for i in flagged:
            claim = by_index.get(i)
            cause = claim.diagnosis.cause.value if claim and claim.diagnosis else "unknown"
            results.append(
                TriageResult(
                    message["audit_id"], message["tenant"], i, cause, None, None, None, "none"
                )
            )
        return results
    verifications = verify(
        audit, PromptResponder(generator), samples=samples, checker=checker, claims=flagged
    )
    for v in verifications:
        best = v.best
        results.append(
            TriageResult(
                audit_id=message["audit_id"],
                tenant=message["tenant"],
                claim_index=v.claim_index,
                cause=v.cause.value,
                confirmed=v.confirmed,
                fix_rate=best.rate if best else None,
                resample_rate=v.baseline.rate,
                intervention=best.intervention.label if best else "none",
            )
        )
    return results


class TriageWorker:
    def __init__(
        self,
        sqs: Any,
        queue_name: str,
        store: ResultStore,
        generator: Any,
        checker: Any,
        *,
        samples: int = 3,
        wait_s: int = 10,
        batch: int = 5,
    ) -> None:
        self.sqs = sqs
        self.queue_url = str(sqs.get_queue_url(QueueName=queue_name)["QueueUrl"])
        self.store = store
        self.generator = generator
        self.checker = checker
        self.samples = samples
        self.wait_s = wait_s
        self.batch = batch
        self.running = True

    def poll_once(self) -> int:
        resp = self.sqs.receive_message(
            QueueUrl=self.queue_url, MaxNumberOfMessages=self.batch, WaitTimeSeconds=self.wait_s
        )
        handled = 0
        for msg in resp.get("Messages", []):
            started = time.perf_counter()
            try:
                body = json.loads(msg["Body"])
                results = process(body, self.generator, self.checker, samples=self.samples)
                self.store.upsert(results)
                self.sqs.delete_message(QueueUrl=self.queue_url, ReceiptHandle=msg["ReceiptHandle"])
                outcome = "replayed" if self.generator is not None else "diagnosed"
                TRIAGE_DONE.labels(outcome=outcome).inc()
                handled += 1
                log.info(
                    "triaged",
                    audit_id=body.get("audit_id"),
                    claims=len(results),
                    seconds=round(time.perf_counter() - started, 2),
                )
            except Exception as exc:
                TRIAGE_DONE.labels(outcome="error").inc()
                log.error("triage_failed", error=type(exc).__name__)
        return handled

    def run(self, should_stop: Callable[[], bool] = lambda: False) -> None:
        while self.running and not should_stop():
            self.poll_once()
