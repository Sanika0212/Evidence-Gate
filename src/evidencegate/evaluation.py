"""Scoring public labelled sets with a checker, shared by ``evidencegate seed`` and ``eval/``.

Two granularities:

1. **Claims (RAGTruth).** Every answer sentence is labelled unsupported if a human
   annotated span overlaps it; reuses Groundsight's RAGTruth loader and scorer.
2. **Answers (MedHallu).** Each question has a grounded answer and a hallucinated one.
   An answer's score is the support of its weakest claim, so "the answer is flagged"
   is the same event as "some claim is below the threshold".
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from evidencegate.datasets import LabelledAnswer, medhallu


@dataclass
class Scored:
    id: str
    support: float
    unsupported: bool
    group: str = ""
    n_claims: int = 1


class EmbeddingChecker:
    """The common shortcut: cosine similarity of claim and passage embeddings, mapped to [0, 1]."""

    three_way = False

    def __init__(self, embedder: Any, name: str = "embedding-similarity") -> None:
        self.embedder = embedder
        self.name = name
        self._cache: dict[str, np.ndarray] = {}

    def _vec(self, texts: Sequence[str]) -> list[np.ndarray]:
        todo = [t for t in dict.fromkeys(texts) if t not in self._cache]
        if todo:
            for t, v in zip(todo, self.embedder.embed(todo), strict=True):
                self._cache[t] = v
        return [self._cache[t] for t in texts]

    def score(self, pairs: Sequence[tuple[str, str]]) -> list[tuple[float, float]]:
        premises = self._vec([p for p, _ in pairs])
        claims = self._vec([c for _, c in pairs])
        return [(float((1 + float(a @ b)) / 2), 0.0) for a, b in zip(premises, claims, strict=True)]


def score_labelled_answers(
    items: Sequence[LabelledAnswer], checker: Any, *, progress: bool = False
) -> list[Scored]:
    import time

    from groundsight import audit

    out = []
    started = time.time()
    for n, item in enumerate(items, start=1):
        if progress and n % 100 == 0:
            print(f"  {n}/{len(items)} answers, {n / (time.time() - started):.2f}/s", flush=True)
        result = audit(
            item.answer,
            item.passages or [""],
            question=item.question,
            checker=checker,
            diagnose=False,
            evidence=False,
        )
        supports = [c.support for c in result.claims]
        out.append(
            Scored(
                item.id,
                min(supports) if supports else 0.0,
                item.unsupported,
                item.category or ("grounded" if not item.unsupported else ""),
                len(supports),
            )
        )
    return out


def score_ragtruth(
    split: str, checker: Any, per_task: int | None, seed: int = 0, *, progress: bool = False
) -> list[Scored]:
    from groundsight.bench.detect import examples, load_split, score

    rows = score(examples(load_split(split), per_task, seed), checker, progress=progress)
    return [Scored(f"{r.example}:{r.start}", r.support, r.label, r.task) for r in rows]


def sample_pairs(items: list[LabelledAnswer], limit: int, seed: int) -> list[LabelledAnswer]:
    """Keep grounded/hallucinated answer pairs together when sampling."""
    if not limit or limit >= len(items):
        return items
    pairs = [items[i : i + 2] for i in range(0, len(items), 2)]
    random.Random(seed).shuffle(pairs)
    return [x for pair in pairs[: max(1, limit // 2)] for x in pair]


def score_answers(
    dataset: str, checker: Any, *, limit: int = 0, seed: int = 0, progress: bool = False
) -> list[Scored]:
    if dataset.startswith("medhallu-"):
        items = sample_pairs(medhallu(dataset.split("-", 1)[1]), limit, seed)
        return score_labelled_answers(items, checker, progress=progress)
    if dataset.startswith("ragtruth-"):
        per_task = limit // 3 if limit else None
        return score_ragtruth(dataset.split("-", 1)[1], checker, per_task, seed, progress=progress)
    raise ValueError(f"unknown dataset {dataset!r}")


def auroc(scores: Sequence[float], unsupported: Sequence[bool]) -> float:
    """AUROC of ``-support`` as a detector of unsupported (ties count half)."""
    s = -np.asarray(scores, dtype=float)
    y = np.asarray(unsupported, dtype=bool)
    pos, neg = s[y], s[~y]
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    order = np.argsort(np.concatenate([pos, neg]), kind="mergesort")
    ranks = np.empty(len(order))
    allv = np.concatenate([pos, neg])[order]
    i = 0
    while i < len(allv):
        j = i
        while j + 1 < len(allv) and allv[j + 1] == allv[i]:
            j += 1
        ranks[order[i : j + 1]] = (i + j) / 2 + 1
        i = j + 1
    return float((ranks[: len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))
