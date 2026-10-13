"""Hybrid retrieval: lexical and dense candidates fused with reciprocal rank fusion,
then reordered by a cross-encoder.

Two stores implement the same interface: :class:`PostgresStore` (BM25 over stemmed
postings plus pgvector, used by the service) and :class:`MemoryStore` (tests).
"""

from __future__ import annotations

import math
import re
import time
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

import numpy as np

from evidencegate.embedding import Embedder, Reranker
from evidencegate.telemetry import STAGE_LATENCY

_WORD = re.compile(r"[a-z0-9]+")


class Mode(StrEnum):
    LEXICAL = "lexical"
    DENSE = "dense"
    HYBRID = "hybrid"
    HYBRID_RERANK = "hybrid_rerank"


@dataclass
class Hit:
    id: str
    text: str
    score: float
    source: str = ""
    ranks: dict[str, int] = field(default_factory=dict)


class Store(Protocol):
    def lexical(self, tenant: str, query: str, n: int) -> list[Hit]: ...

    def dense(self, tenant: str, vector: np.ndarray, n: int) -> list[Hit]: ...


def rrf(rankings: Iterable[Sequence[Hit]], k: int = 60) -> list[Hit]:
    """Reciprocal rank fusion: ``score(d) = sum over rankings of 1 / (k + rank)``."""
    fused: dict[str, Hit] = {}
    for name, ranking in enumerate(rankings):
        for rank, hit in enumerate(ranking, start=1):
            entry = fused.setdefault(hit.id, Hit(hit.id, hit.text, 0.0, hit.source))
            entry.score += 1.0 / (k + rank)
            entry.ranks[str(name)] = rank
    return sorted(fused.values(), key=lambda h: h.score, reverse=True)


class Retriever:
    def __init__(
        self,
        store: Store,
        embedder: Embedder,
        reranker: Reranker | None = None,
        *,
        candidates: int = 50,
        rerank_depth: int = 20,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.reranker = reranker
        self.candidates = candidates
        self.rerank_depth = rerank_depth

    def _timed(self, stage: str, fn: Any, *args: Any) -> Any:
        started = time.perf_counter()
        try:
            return fn(*args)
        finally:
            STAGE_LATENCY.labels(stage=stage).observe(time.perf_counter() - started)

    def retrieve(
        self, tenant: str, query: str, k: int = 5, mode: Mode = Mode.HYBRID_RERANK
    ) -> list[Hit]:
        if mode == Mode.HYBRID_RERANK and self.reranker is None:
            mode = Mode.HYBRID
        lexical: list[Hit] = []
        dense: list[Hit] = []
        if mode in (Mode.LEXICAL, Mode.HYBRID, Mode.HYBRID_RERANK):
            lexical = self._timed(
                "retrieve_lexical", self.store.lexical, tenant, query, self.candidates
            )
        if mode in (Mode.DENSE, Mode.HYBRID, Mode.HYBRID_RERANK):
            vector = self._timed("embed_query", self.embedder.embed_queries, [query])[0]
            dense = self._timed("retrieve_dense", self.store.dense, tenant, vector, self.candidates)
        if mode == Mode.LEXICAL:
            return lexical[:k]
        if mode == Mode.DENSE:
            return dense[:k]
        fused = rrf([lexical, dense])
        if mode == Mode.HYBRID:
            return fused[:k]
        pool = fused[: self.rerank_depth]
        assert self.reranker is not None
        scores = self._timed("rerank", self.reranker.rerank, query, [h.text for h in pool])
        for hit, s in zip(pool, scores, strict=True):
            hit.score = float(s)
        return sorted(pool, key=lambda h: h.score, reverse=True)[:k]


# --------------------------------------------------------------------------- stores


class MemoryStore:
    """Passages in memory with BM25 scoring and exact cosine search. For tests and evals."""

    def __init__(self, embedder: Embedder, k1: float = 1.5, b: float = 0.75) -> None:
        self.embedder = embedder
        self.k1 = k1
        self.b = b
        self._rows: dict[str, list[tuple[str, str, str]]] = {}
        self._vectors: dict[str, np.ndarray] = {}
        self._tf: dict[str, list[Counter[str]]] = {}
        self._df: dict[str, Counter[str]] = {}

    def add(self, tenant: str, passages: Sequence[tuple[str, str, str]]) -> None:
        """Add ``(id, text, source)`` rows."""
        rows = self._rows.setdefault(tenant, [])
        tfs = self._tf.setdefault(tenant, [])
        df = self._df.setdefault(tenant, Counter())
        for pid, text, source in passages:
            rows.append((pid, text, source))
            tf = Counter(_WORD.findall(text.lower()))
            tfs.append(tf)
            df.update(tf.keys())
        vectors = self.embedder.embed([t for _, t, _ in passages])
        old = self._vectors.get(tenant)
        self._vectors[tenant] = vectors if old is None else np.vstack([old, vectors])

    def lexical(self, tenant: str, query: str, n: int) -> list[Hit]:
        rows, tfs, df = self._rows.get(tenant, []), self._tf.get(tenant, []), self._df.get(tenant)
        if not rows or df is None:
            return []
        terms = _WORD.findall(query.lower())
        total = len(rows)
        avg = sum(sum(tf.values()) for tf in tfs) / total
        scores = np.zeros(total)
        for t in set(terms):
            if t not in df:
                continue
            idf = math.log(1 + (total - df[t] + 0.5) / (df[t] + 0.5))
            for i, tf in enumerate(tfs):
                f = tf.get(t, 0)
                if f:
                    length = sum(tf.values())
                    scores[i] += (
                        idf
                        * f
                        * (self.k1 + 1)
                        / (f + self.k1 * (1 - self.b + self.b * length / avg))
                    )
        order = [int(i) for i in np.argsort(-scores) if scores[i] > 0][:n]
        return [Hit(rows[i][0], rows[i][1], float(scores[i]), rows[i][2]) for i in order]

    def dense(self, tenant: str, vector: np.ndarray, n: int) -> list[Hit]:
        rows, vectors = self._rows.get(tenant, []), self._vectors.get(tenant)
        if not rows or vectors is None:
            return []
        sims = vectors @ vector
        order = np.argsort(-sims)[:n]
        return [Hit(rows[i][0], rows[i][1], float(sims[i]), rows[i][2]) for i in order]


SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS retriever;
CREATE TABLE IF NOT EXISTS retriever.passages (
    tenant     text NOT NULL,
    id         text NOT NULL,
    text       text NOT NULL,
    source     text NOT NULL DEFAULT '',
    tsv        tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    embedding  vector({dim}) NOT NULL,
    PRIMARY KEY (tenant, id)
);
CREATE INDEX IF NOT EXISTS passages_tsv ON retriever.passages USING gin (tsv);
CREATE INDEX IF NOT EXISTS passages_embedding ON retriever.passages
    USING hnsw (embedding vector_cosine_ops);
-- BM25: Postgres text ranking has no inverse document frequency, so keep postings
-- (stemmed lexeme, passage, term frequency) and per-tenant statistics.
CREATE TABLE IF NOT EXISTS retriever.postings (
    tenant  text NOT NULL,
    lexeme  text NOT NULL,
    id      text NOT NULL,
    tf      integer NOT NULL,
    PRIMARY KEY (tenant, lexeme, id)
);
CREATE INDEX IF NOT EXISTS postings_id ON retriever.postings (tenant, id);
CREATE TABLE IF NOT EXISTS retriever.doclen (
    tenant text NOT NULL, id text NOT NULL, len integer NOT NULL, PRIMARY KEY (tenant, id)
);
CREATE TABLE IF NOT EXISTS retriever.lexstats (
    tenant text NOT NULL, lexeme text NOT NULL, idf double precision NOT NULL,
    PRIMARY KEY (tenant, lexeme)
);
CREATE TABLE IF NOT EXISTS retriever.corpusstats (
    tenant text PRIMARY KEY, n integer NOT NULL, avglen double precision NOT NULL
);
"""

BM25_SQL = """
WITH q AS (SELECT DISTINCT lexeme FROM unnest(to_tsvector('english', %(query)s))),
     st AS (SELECT avglen FROM retriever.corpusstats WHERE tenant = %(tenant)s),
     scored AS (
         SELECT p.id,
                sum(ls.idf * p.tf * (%(k1)s + 1)
                    / (p.tf + %(k1)s * (1 - %(b)s + %(b)s * d.len / st.avglen))) AS score
         FROM q
         JOIN retriever.postings p ON p.tenant = %(tenant)s AND p.lexeme = q.lexeme
         JOIN retriever.lexstats ls ON ls.tenant = %(tenant)s AND ls.lexeme = q.lexeme
         JOIN retriever.doclen d ON d.tenant = %(tenant)s AND d.id = p.id
         CROSS JOIN st
         GROUP BY p.id ORDER BY score DESC LIMIT %(n)s)
SELECT s.id, x.text, x.source, s.score
FROM scored s JOIN retriever.passages x ON x.tenant = %(tenant)s AND x.id = s.id
ORDER BY s.score DESC
"""


class PostgresStore:
    """Passages in Postgres: BM25 over stemmed postings and pgvector HNSW search.

    Call :meth:`refresh_stats` after loading passages (``evidencegate ingest`` does);
    until then lexical search falls back to Postgres full-text ranking.
    """

    def __init__(self, pool: Any, dim: int = 384, k1: float = 1.2, b: float = 0.75) -> None:
        self.pool = pool
        self.dim = dim
        self.k1 = k1
        self.b = b

    def migrate(self) -> None:
        with self.pool.connection() as conn:
            conn.execute("SELECT pg_advisory_lock(4201)")
            try:
                conn.execute(SCHEMA.format(dim=self.dim))
            finally:
                conn.execute("SELECT pg_advisory_unlock(4201)")

    def add(self, tenant: str, rows: Sequence[tuple[str, str, str]], vectors: np.ndarray) -> int:
        from pgvector.psycopg import register_vector

        ids = [pid for pid, _, _ in rows]
        with self.pool.connection() as conn:
            register_vector(conn)
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO retriever.passages (tenant, id, text, source, embedding) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (tenant, id) DO UPDATE SET "
                    "text = EXCLUDED.text, source = EXCLUDED.source, embedding = EXCLUDED.embedding",
                    [
                        (tenant, pid, text, source, vec)
                        for (pid, text, source), vec in zip(rows, vectors, strict=True)
                    ],
                )
                self._index(cur, tenant, ids)
        return len(rows)

    def _index(self, cur: Any, tenant: str, ids: list[str] | None) -> None:
        where = "p.tenant = %s" + (" AND p.id = ANY(%s)" if ids is not None else "")
        args: tuple[Any, ...] = (tenant, ids) if ids is not None else (tenant,)
        cur.execute("DELETE FROM retriever.postings p WHERE " + where, args)
        cur.execute(
            "INSERT INTO retriever.postings (tenant, lexeme, id, tf) "
            "SELECT p.tenant, t.lexeme, p.id, coalesce(array_length(t.positions, 1), 1) "
            "FROM retriever.passages p, unnest(p.tsv) t WHERE " + where,
            args,
        )

    def reindex(self, tenant: str) -> None:
        """Rebuild postings for every passage of a tenant, then its statistics."""
        with self.pool.connection() as conn, conn.cursor() as cur:
            self._index(cur, tenant, None)
        self.refresh_stats(tenant)

    def refresh_stats(self, tenant: str) -> None:
        """Document lengths, IDF per lexeme and average length, from the postings."""
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM retriever.doclen WHERE tenant = %s", (tenant,))
            cur.execute(
                "INSERT INTO retriever.doclen (tenant, id, len) SELECT tenant, id, sum(tf) "
                "FROM retriever.postings WHERE tenant = %s GROUP BY tenant, id",
                (tenant,),
            )
            cur.execute("DELETE FROM retriever.lexstats WHERE tenant = %s", (tenant,))
            cur.execute(
                "INSERT INTO retriever.lexstats (tenant, lexeme, idf) "
                "SELECT x.tenant, x.lexeme, ln(1 + (c.n - x.df + 0.5) / (x.df + 0.5)) "
                "FROM (SELECT tenant, lexeme, count(*) AS df FROM retriever.postings "
                "      WHERE tenant = %s GROUP BY tenant, lexeme) x, "
                "     (SELECT count(*) AS n FROM retriever.doclen WHERE tenant = %s) c",
                (tenant, tenant),
            )
            cur.execute(
                "INSERT INTO retriever.corpusstats (tenant, n, avglen) "
                "SELECT %s, count(*), coalesce(avg(len), 1) FROM retriever.doclen WHERE tenant = %s "
                "ON CONFLICT (tenant) DO UPDATE SET n = EXCLUDED.n, avglen = EXCLUDED.avglen",
                (tenant, tenant),
            )

    def count(self, tenant: str) -> int:
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT count(*) FROM retriever.passages WHERE tenant = %s", (tenant,)
            ).fetchone()
        return int(row[0]) if row else 0

    def lexical(self, tenant: str, query: str, n: int) -> list[Hit]:
        if not _WORD.search(query.lower()):
            return []
        with self.pool.connection() as conn:
            has_stats = conn.execute(
                "SELECT 1 FROM retriever.corpusstats WHERE tenant = %s", (tenant,)
            ).fetchone()
            if has_stats:
                rows = conn.execute(
                    BM25_SQL,
                    {"query": query, "tenant": tenant, "n": n, "k1": self.k1, "b": self.b},
                ).fetchall()
            else:
                terms = " | ".join(dict.fromkeys(_WORD.findall(query.lower())))
                rows = conn.execute(
                    "SELECT id, text, source, ts_rank_cd(tsv, q) AS score "
                    "FROM retriever.passages, to_tsquery('english', %s) q "
                    "WHERE tenant = %s AND tsv @@ q ORDER BY score DESC LIMIT %s",
                    (terms, tenant, n),
                ).fetchall()
        return [Hit(r[0], r[1], float(r[3]), r[2]) for r in rows]

    def dense(self, tenant: str, vector: np.ndarray, n: int) -> list[Hit]:
        from pgvector.psycopg import register_vector

        sql = (
            "SELECT id, text, source, 1 - (embedding <=> %s) AS score "
            "FROM retriever.passages WHERE tenant = %s ORDER BY embedding <=> %s LIMIT %s"
        )
        with self.pool.connection() as conn:
            register_vector(conn)
            conn.execute("SET hnsw.ef_search = 100")
            rows = conn.execute(sql, (vector, tenant, vector, n)).fetchall()
        return [Hit(r[0], r[1], float(r[3]), r[2]) for r in rows]
