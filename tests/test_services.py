"""Stores against real Postgres (pgvector) and Redis. Run with EG_TEST_SERVICES=1."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any

import numpy as np
import pytest

from evidencegate.calibration import Calibrator, Label, PostgresCalibrationStore
from evidencegate.embedding import HashEmbedder
from evidencegate.gateway.review import PostgresReviewStore, PostgresTenants, ReviewItem
from evidencegate.retrieval import Mode, PostgresStore, Retriever
from evidencegate.triage import PostgresResultStore, TriageResult

pytestmark = pytest.mark.services


@pytest.fixture(scope="module")
def pool() -> Iterator[Any]:
    from psycopg_pool import ConnectionPool

    url = os.environ.get(
        "EG_DATABASE_URL", "postgresql://evidencegate:evidencegate@localhost:5432/evidencegate"
    )
    p = ConnectionPool(url, min_size=1, max_size=4, open=True)
    yield p
    p.close()


def test_postgres_retrieval_modes(pool: Any) -> None:
    store = PostgresStore(pool, 384)
    store.migrate()
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    rows = [
        ("a", "Aspirin irreversibly inhibits cyclooxygenase in platelets.", "x"),
        ("b", "Metformin is first-line therapy for type 2 diabetes.", "x"),
        ("c", "The Eiffel Tower was completed in 1889.", "x"),
    ]
    emb = HashEmbedder(384)
    assert store.add(tenant, rows, emb.embed([t for _, t, _ in rows])) == 3
    assert store.count(tenant) == 3
    r = Retriever(store, emb, None)
    for mode in (Mode.LEXICAL, Mode.DENSE, Mode.HYBRID):
        assert r.retrieve(tenant, "which drug treats type 2 diabetes", 1, mode)[0].id == "b"
    assert store.lexical(tenant, "!!!", 3) == []
    # Upsert replaces text without duplicating.
    store.add(
        tenant, [("a", "Aspirin blocks COX enzymes.", "y")], emb.embed(["Aspirin blocks COX"])
    )
    assert store.count(tenant) == 3


def test_postgres_calibration_store(pool: Any) -> None:
    store = PostgresCalibrationStore(pool, 0.9)
    store.migrate()
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    assert store.target_recall(tenant) == 0.9
    assert store.latest(tenant) is None
    rng = np.random.default_rng(0)
    store.add_labels(tenant, [Label(float(s), True) for s in rng.beta(2, 5, 120)])
    store.add_labels(tenant, [Label(float(s), False) for s in rng.beta(5, 2, 120)])
    cal = Calibrator(store)
    fitted, _ = cal.fit(tenant, 0.85)
    assert fitted is not None
    assert fitted.version == 1
    again, _ = cal.fit(tenant, 0.9)
    assert again is not None
    assert again.version == 2
    latest = store.latest(tenant)
    assert latest is not None
    assert latest.version == 2
    assert latest.target_recall == 0.9
    store.record_scores(tenant, [0.1, 0.5, 0.9])
    assert sorted(store.live_scores(tenant, 10)) == [0.1, 0.5, 0.9]
    store.prune_live_scores(keep=2)
    assert len(store.live_scores(tenant, 10)) == 2
    assert len(store.labels(tenant, since=0)) == 240


def test_postgres_review_tenants_and_triage(pool: Any) -> None:
    reviews = PostgresReviewStore(pool)
    reviews.migrate()
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    item = ReviewItem(tenant, "audit-1", "claim", "evidence", 0.2, True, "review")
    reviews.add([item])
    assert [i.id for i in reviews.pending(tenant)] == [item.id]
    assert reviews.set_label(item.id, "unsupported") is not None
    assert reviews.set_label(item.id, "supported") is None
    got = reviews.get(item.id)
    assert got is not None
    assert got.label == "unsupported"
    assert reviews.pending(tenant) == []
    tenants = PostgresTenants(pool)
    tenants.register(f"client-{tenant}", tenant)
    assert tenants.tenant_for(f"client-{tenant}") == tenant
    assert tenants.policy(tenant).block_on_contradicted
    results = PostgresResultStore(pool)
    results.migrate()
    r = TriageResult(f"audit-{tenant}", tenant, 0, "retrieval_miss", None, None, None, "none")
    results.upsert([r, r])
    with pool.connection() as conn:
        n = conn.execute(
            "SELECT count(*) FROM triage.results WHERE tenant = %s", (tenant,)
        ).fetchone()
    assert n is not None
    assert n[0] == 1


def test_redis_rate_limiter() -> None:
    import redis

    from evidencegate.gateway.ratelimit import RedisRateLimiter

    client = redis.Redis.from_url(os.environ.get("EG_REDIS_URL", "redis://localhost:6379/0"))
    lim = RedisRateLimiter(client, per_minute=60, burst=2)
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    assert lim.allow(tenant)[0]
    assert lim.allow(tenant)[0]
    ok, retry = lim.allow(tenant)
    assert not ok
    assert 0 < retry <= 1.0
