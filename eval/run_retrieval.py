"""Does hybrid retrieval with reranking find better evidence? BioASQ, the service's own store.

    python eval/run_retrieval.py --n 500  (needs the BioASQ corpus ingested into Postgres)

Every mode runs against the same Postgres store the retriever service uses (Postgres
full-text search, pgvector HNSW over arctic-embed-xs, fusion, MiniLM cross-encoder),
plus an in-memory BM25 over the same 40,221 passages as a reference for the lexical leg.
Metrics: Recall@5 and @10 (share of a question's gold passages found), nDCG@10, MRR@10,
and per-query latency.
"""

from __future__ import annotations

import argparse
import math
import os
import random
import time

import numpy as np
from common import write

from evidencegate import datasets
from evidencegate.embedding import load_embedder, load_reranker
from evidencegate.retrieval import Hit, Mode, PostgresStore, Retriever


def metrics(ranked: list[str], gold: set[str]) -> dict[str, float]:
    dcg = sum(1 / math.log2(i + 2) for i, d in enumerate(ranked[:10]) if d in gold)
    ideal = sum(1 / math.log2(i + 2) for i in range(min(10, len(gold))))
    rr = next((1 / (i + 1) for i, d in enumerate(ranked[:10]) if d in gold), 0.0)
    return {
        "recall@5": len(gold & set(ranked[:5])) / len(gold),
        "recall@10": len(gold & set(ranked[:10])) / len(gold),
        "ndcg@10": dcg / ideal if ideal else 0.0,
        "mrr@10": rr,
    }


class BM25Reference:
    def __init__(self) -> None:
        from rank_bm25 import BM25Okapi

        rows = list(datasets.bioasq_corpus())
        self.ids = [r[0] for r in rows]
        self.bm25 = BM25Okapi([r[1].lower().split() for r in rows])

    def search(self, query: str, k: int) -> list[str]:
        scores = self.bm25.get_scores(query.lower().split())
        return [self.ids[i] for i in np.argsort(-scores)[:k]]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=500)
    parser.add_argument("--tenant", default="demo")
    args = parser.parse_args()
    from psycopg_pool import ConnectionPool

    pool = ConnectionPool(
        os.environ.get(
            "EG_DATABASE_URL", "postgresql://evidencegate:evidencegate@localhost:5432/evidencegate"
        ),
        min_size=1,
        max_size=2,
        open=True,
    )
    store = PostgresStore(pool, 384)
    n_passages = store.count(args.tenant)
    retriever = Retriever(
        store,
        load_embedder("snowflake/snowflake-arctic-embed-xs", "models"),
        load_reranker("Xenova/ms-marco-MiniLM-L-6-v2", "models"),
    )
    questions = datasets.bioasq_questions()
    random.Random(0).shuffle(questions)
    questions = questions[: args.n]
    bm25 = BM25Reference()
    systems = {"bm25 (in-memory reference)": None, **{m.value: m for m in Mode}}
    results = []
    for name, mode in systems.items():
        per_q, lat = [], []
        for q in questions:
            started = time.perf_counter()
            if mode is None:
                ranked = bm25.search(q.question, 10)
            else:
                hits: list[Hit] = retriever.retrieve(args.tenant, q.question, 10, mode)
                ranked = [h.id for h in hits]
            lat.append(time.perf_counter() - started)
            per_q.append(metrics(ranked, q.relevant))
        row = {k: round(float(np.mean([m[k] for m in per_q])), 3) for k in per_q[0]}
        row.update(
            system=name,
            p50_ms=round(float(np.percentile(lat, 50)) * 1000, 1),
            p95_ms=round(float(np.percentile(lat, 95)) * 1000, 1),
        )
        results.append(row)
        print(row, flush=True)
    print(
        "wrote",
        write(
            "retrieval", {"questions": len(questions), "passages": n_passages, "results": results}
        ),
    )


if __name__ == "__main__":
    main()
