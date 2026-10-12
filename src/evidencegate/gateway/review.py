"""Review queue: every flagged claim, plus a random audit sample of unflagged claims.

The audit sample is what makes recall measurable in production: without it, nobody
ever looks at an unsupported claim the gate let through.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ReviewItem:
    tenant: str
    audit_id: str
    claim: str
    evidence: str
    support: float
    flagged: bool
    source: str  # "review" (flagged) or "audit" (random unflagged sample)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created: float = field(default_factory=time.time)
    label: str | None = None  # "supported" or "unsupported"


class ReviewStore(Protocol):
    def add(self, items: list[ReviewItem]) -> None: ...

    def pending(self, tenant: str | None, limit: int = 50) -> list[ReviewItem]: ...

    def get(self, item_id: str) -> ReviewItem | None: ...

    def set_label(self, item_id: str, label: str) -> ReviewItem | None: ...


class MemoryReviewStore:
    def __init__(self) -> None:
        self.items: dict[str, ReviewItem] = {}

    def add(self, items: list[ReviewItem]) -> None:
        for item in items:
            self.items[item.id] = item

    def pending(self, tenant: str | None, limit: int = 50) -> list[ReviewItem]:
        rows = [
            i
            for i in self.items.values()
            if i.label is None and (tenant is None or i.tenant == tenant)
        ]
        return sorted(rows, key=lambda i: i.created)[:limit]

    def get(self, item_id: str) -> ReviewItem | None:
        return self.items.get(item_id)

    def set_label(self, item_id: str, label: str) -> ReviewItem | None:
        item = self.items.get(item_id)
        if item is None or item.label is not None:
            return None
        item.label = label
        return item


SCHEMA = """
CREATE SCHEMA IF NOT EXISTS gateway;
CREATE TABLE IF NOT EXISTS gateway.review_items (
    id        text PRIMARY KEY,
    tenant    text NOT NULL,
    audit_id  text NOT NULL,
    claim     text NOT NULL,
    evidence  text NOT NULL,
    support   double precision NOT NULL,
    flagged   boolean NOT NULL,
    source    text NOT NULL,
    created   timestamptz NOT NULL DEFAULT now(),
    label     text,
    labelled  timestamptz
);
CREATE INDEX IF NOT EXISTS review_pending ON gateway.review_items (tenant, created) WHERE label IS NULL;
CREATE TABLE IF NOT EXISTS gateway.tenants (
    client_id  text PRIMARY KEY,
    tenant     text NOT NULL,
    block_on_contradicted boolean NOT NULL DEFAULT true
);
"""


class PostgresReviewStore:
    def __init__(self, pool: Any) -> None:
        self.pool = pool

    def migrate(self) -> None:
        with self.pool.connection() as conn:
            conn.execute("SELECT pg_advisory_lock(4203)")
            try:
                conn.execute(SCHEMA)
            finally:
                conn.execute("SELECT pg_advisory_unlock(4203)")

    def add(self, items: list[ReviewItem]) -> None:
        if not items:
            return
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO gateway.review_items (id, tenant, audit_id, claim, evidence, support, flagged, source) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    (
                        i.id,
                        i.tenant,
                        i.audit_id,
                        i.claim,
                        i.evidence,
                        i.support,
                        i.flagged,
                        i.source,
                    )
                    for i in items
                ],
            )

    def _row(self, r: Any) -> ReviewItem:
        return ReviewItem(
            id=r[0],
            tenant=r[1],
            audit_id=r[2],
            claim=r[3],
            evidence=r[4],
            support=float(r[5]),
            flagged=bool(r[6]),
            source=r[7],
            created=float(r[8]),
            label=r[9],
        )

    _COLS = "id, tenant, audit_id, claim, evidence, support, flagged, source, extract(epoch FROM created), label"

    def pending(self, tenant: str | None, limit: int = 50) -> list[ReviewItem]:
        sql = f"SELECT {self._COLS} FROM gateway.review_items WHERE label IS NULL"
        params: list[Any] = []
        if tenant:
            sql += " AND tenant = %s"
            params.append(tenant)
        sql += " ORDER BY created LIMIT %s"
        params.append(limit)
        with self.pool.connection() as conn:
            return [self._row(r) for r in conn.execute(sql, params).fetchall()]

    def get(self, item_id: str) -> ReviewItem | None:
        with self.pool.connection() as conn:
            r = conn.execute(
                f"SELECT {self._COLS} FROM gateway.review_items WHERE id = %s", (item_id,)
            ).fetchone()
        return self._row(r) if r else None

    def set_label(self, item_id: str, label: str) -> ReviewItem | None:
        with self.pool.connection() as conn:
            r = conn.execute(
                "UPDATE gateway.review_items SET label = %s, labelled = now() "
                f"WHERE id = %s AND label IS NULL RETURNING {self._COLS}",
                (label, item_id),
            ).fetchone()
        return self._row(r) if r else None


@dataclass
class TenantPolicy:
    tenant: str
    block_on_contradicted: bool = True


class MemoryTenants:
    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self.mapping = mapping or {}

    def tenant_for(self, client_id: str) -> str | None:
        return self.mapping.get(client_id)

    def policy(self, tenant: str) -> TenantPolicy:
        return TenantPolicy(tenant)


class PostgresTenants:
    def __init__(self, pool: Any) -> None:
        self.pool = pool

    def tenant_for(self, client_id: str) -> str | None:
        with self.pool.connection() as conn:
            r = conn.execute(
                "SELECT tenant FROM gateway.tenants WHERE client_id = %s", (client_id,)
            ).fetchone()
        return str(r[0]) if r else None

    def policy(self, tenant: str) -> TenantPolicy:
        with self.pool.connection() as conn:
            r = conn.execute(
                "SELECT block_on_contradicted FROM gateway.tenants WHERE tenant = %s LIMIT 1",
                (tenant,),
            ).fetchone()
        return TenantPolicy(tenant, bool(r[0]) if r else True)

    def register(self, client_id: str, tenant: str) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO gateway.tenants (client_id, tenant) VALUES (%s, %s) "
                "ON CONFLICT (client_id) DO UPDATE SET tenant = EXCLUDED.tenant",
                (client_id, tenant),
            )
