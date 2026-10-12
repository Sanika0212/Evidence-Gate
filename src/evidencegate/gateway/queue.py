"""Hand flagged audits to the triage worker through a queue (SQS, or ElasticMQ locally)."""

from __future__ import annotations

import json
from typing import Any, Protocol

from evidencegate.telemetry import QUEUE_PUBLISHED, get_logger

log = get_logger(__name__)


class Publisher(Protocol):
    def publish(self, message: dict[str, Any]) -> None: ...


class MemoryPublisher:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def publish(self, message: dict[str, Any]) -> None:
        self.messages.append(message)
        QUEUE_PUBLISHED.inc()


class SqsPublisher:
    def __init__(self, client: Any, queue_name: str) -> None:
        self.client = client
        self.queue_name = queue_name
        self._url: str | None = None

    @property
    def url(self) -> str:
        if self._url is None:
            self._url = str(self.client.get_queue_url(QueueName=self.queue_name)["QueueUrl"])
        return self._url

    def publish(self, message: dict[str, Any]) -> None:
        try:
            self.client.send_message(QueueUrl=self.url, MessageBody=json.dumps(message))
            QUEUE_PUBLISHED.inc()
        except Exception as exc:  # triage is best-effort; the decision is already served
            log.error("triage_publish_failed", error=type(exc).__name__)
