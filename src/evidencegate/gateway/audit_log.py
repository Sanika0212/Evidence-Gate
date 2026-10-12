"""Tamper-evident audit trail of every gate decision.

Records are hash-chained (each carries the hash of the previous record from the same
replica) and written in batches as JSON Lines objects to S3. In AWS the bucket has
Object Lock enabled, so written objects cannot be changed or deleted during their
retention period. Records hold redacted text and a SHA-256 of the original answer, so
an auditor can prove which answer a decision was about without storing it.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from typing import Any, Protocol

from evidencegate.telemetry import get_logger

log = get_logger(__name__)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class AuditSink(Protocol):
    def write(self, record: dict[str, Any]) -> dict[str, Any]: ...

    def flush(self) -> None: ...


class _Chain:
    def __init__(self) -> None:
        self.prev = "0" * 64
        self.lock = threading.Lock()
        self.replica = uuid.uuid4().hex[:12]

    def link(self, record: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            body = {**record, "replica": self.replica, "prev_hash": self.prev}
            digest = sha256(json.dumps(body, sort_keys=True, separators=(",", ":")))
            body["hash"] = digest
            self.prev = digest
            return body


def verify_chain(records: list[dict[str, Any]]) -> bool:
    """True if every record's hash is correct and links to the previous one."""
    prev = "0" * 64
    for r in records:
        body = {k: v for k, v in r.items() if k != "hash"}
        if body.get("prev_hash") != prev:
            return False
        if sha256(json.dumps(body, sort_keys=True, separators=(",", ":"))) != r.get("hash"):
            return False
        prev = r["hash"]
    return True


class MemoryAuditSink:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self._chain = _Chain()

    def write(self, record: dict[str, Any]) -> dict[str, Any]:
        linked = self._chain.link(record)
        self.records.append(linked)
        return linked

    def flush(self) -> None:
        return None


class S3AuditSink:
    def __init__(
        self, client: Any, bucket: str, *, batch: int = 200, interval_s: float = 10.0
    ) -> None:
        self.client = client
        self.bucket = bucket
        self.batch = batch
        self.interval_s = interval_s
        self._chain = _Chain()
        self._buffer: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._last = time.time()

    def write(self, record: dict[str, Any]) -> dict[str, Any]:
        linked = self._chain.link(record)
        with self._lock:
            self._buffer.append(linked)
            due = len(self._buffer) >= self.batch or time.time() - self._last >= self.interval_s
        if due:
            self.flush()
        return linked

    def flush(self) -> None:
        with self._lock:
            rows, self._buffer = self._buffer, []
            self._last = time.time()
        if not rows:
            return
        now = time.gmtime()
        key = (
            f"decisions/{time.strftime('%Y/%m/%d/%H', now)}/"
            f"{self._chain.replica}-{int(time.time() * 1000)}.jsonl"
        )
        body = "\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n"
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=body.encode(),
                ContentType="application/x-ndjson",
                ChecksumAlgorithm="SHA256",
            )
        except Exception as exc:  # keep serving; the records go back in the buffer
            log.error("audit_flush_failed", error=type(exc).__name__, records=len(rows))
            with self._lock:
                self._buffer = rows + self._buffer
