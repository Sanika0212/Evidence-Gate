from __future__ import annotations

import numpy as np

from evidencegate.embedding import HashEmbedder, LexicalReranker, load_embedder, load_reranker
from evidencegate.retrieval import Hit, MemoryStore, Mode, Retriever, rrf


def test_rrf_rewards_agreement() -> None:
    a = [Hit("x", "", 0), Hit("y", "", 0), Hit("z", "", 0)]
    b = [Hit("y", "", 0), Hit("w", "", 0)]
    fused = rrf([a, b])
    assert fused[0].id == "y"
    assert {h.id for h in fused} == {"x", "y", "z", "w"}
    assert fused[0].ranks == {"0": 2, "1": 1}


def test_hash_embedder_is_deterministic_and_normalised() -> None:
    e = HashEmbedder(32)
    v = e.embed(["aspirin blocks cyclooxygenase", "aspirin blocks cyclooxygenase", ""])
    assert np.allclose(v[0], v[1])
    assert abs(np.linalg.norm(v[0]) - 1) < 1e-6
    assert np.linalg.norm(v[2]) == 0


def test_every_mode_finds_the_relevant_passage(retriever: Retriever) -> None:
    for mode in Mode:
        hits = retriever.retrieve("demo", "which enzyme does aspirin inhibit in platelets", 2, mode)
        assert hits, mode
        assert hits[0].id == "p1", mode


def test_unknown_tenant_returns_nothing(retriever: Retriever) -> None:
    assert retriever.retrieve("other", "aspirin", 3, Mode.HYBRID_RERANK) == []


def test_rerank_falls_back_to_hybrid_without_a_reranker() -> None:
    store = MemoryStore(HashEmbedder(64))
    store.add("t", [("a", "metformin diabetes", ""), ("b", "tower paris", "")])
    r = Retriever(store, HashEmbedder(64), None)
    assert r.retrieve("t", "metformin", 1, Mode.HYBRID_RERANK)[0].id == "a"


def test_lexical_reranker_and_loaders() -> None:
    assert LexicalReranker().rerank("aspirin platelets", ["aspirin and platelets", "tower"]) == [
        1.0,
        0.0,
    ]
    assert isinstance(load_embedder("hash", dim=16), HashEmbedder)
    assert load_reranker("none") is None
    assert isinstance(load_reranker("lexical"), LexicalReranker)
