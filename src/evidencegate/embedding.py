"""Dense embeddings and cross-encoder reranking (ONNX via fastembed), plus model-free
stand-ins for tests and CI.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

import numpy as np

_WORD = re.compile(r"[a-z0-9]+")


class Embedder(Protocol):
    dim: int

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        """Unit-norm passage vectors, shape ``(len(texts), dim)``."""
        ...

    def embed_queries(self, texts: Sequence[str]) -> np.ndarray:
        """Unit-norm query vectors (some models expect an instruction prefix on queries)."""
        ...


# Query instructions from the model cards; fastembed does not add them.
_SEARCH = "Represent this sentence for searching relevant passages: "
QUERY_PREFIX = {
    "snowflake/snowflake-arctic-embed-xs": _SEARCH,
    "snowflake/snowflake-arctic-embed-s": _SEARCH,
    "BAAI/bge-small-en-v1.5": _SEARCH,
}


class Reranker(Protocol):
    name: str

    def rerank(self, query: str, texts: Sequence[str]) -> list[float]:
        """Relevance of each text to the query (higher is more relevant)."""
        ...


class HashEmbedder:
    """Signed feature hashing of words: deterministic, no model files. Tests only."""

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        return self._hash(texts)

    def embed_queries(self, texts: Sequence[str]) -> np.ndarray:
        return self._hash(texts)

    def _hash(self, texts: Sequence[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            for word in _WORD.findall(text.lower()):
                digest = hashlib.blake2b(word.encode(), digest_size=8).digest()
                h = int.from_bytes(digest, "little")
                out[row, h % self.dim] += 1.0 if (h >> 63) & 1 else -1.0
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return np.asarray(out / norms, dtype=np.float32)


class FastEmbedder:
    """A fastembed (ONNX) sentence embedder such as ``BAAI/bge-small-en-v1.5``."""

    def __init__(self, model_name: str, cache_dir: str | os.PathLike[str] | None = None) -> None:
        from fastembed import TextEmbedding

        self.model_name = model_name
        self._model: Any = TextEmbedding(
            model_name=model_name, cache_dir=str(cache_dir) if cache_dir else None
        )
        self.dim = int(self._model.embedding_size)
        self.query_prefix = QUERY_PREFIX.get(model_name, "")

    def embed_queries(self, texts: Sequence[str]) -> np.ndarray:
        return self.embed([self.query_prefix + t for t in texts])

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        vectors = np.asarray(list(self._model.embed(list(texts), batch_size=64)), dtype=np.float32)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return np.asarray(vectors / norms, dtype=np.float32)


class LexicalReranker:
    """Content-word overlap between query and text. Tests only."""

    name = "lexical"

    def rerank(self, query: str, texts: Sequence[str]) -> list[float]:
        q = set(_WORD.findall(query.lower()))
        return [len(q & set(_WORD.findall(t.lower()))) / (len(q) or 1) for t in texts]


class FastReranker:
    """A fastembed (ONNX) cross-encoder such as ``Xenova/ms-marco-MiniLM-L-6-v2``."""

    def __init__(self, model_name: str, cache_dir: str | os.PathLike[str] | None = None) -> None:
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        self.name = model_name
        self._model: Any = TextCrossEncoder(
            model_name=model_name, cache_dir=str(cache_dir) if cache_dir else None
        )

    def rerank(self, query: str, texts: Sequence[str]) -> list[float]:
        return [float(s) for s in self._model.rerank(query, list(texts), batch_size=32)]


def load_embedder(spec: str, models_dir: str | os.PathLike[str] = "models", dim: int = 384) -> Any:
    if spec == "hash":
        return HashEmbedder(dim)
    return FastEmbedder(spec, Path(models_dir) / "fastembed")


def load_reranker(spec: str, models_dir: str | os.PathLike[str] = "models") -> Any:
    if spec == "none":
        return None
    if spec == "lexical":
        return LexicalReranker()
    return FastReranker(spec, Path(models_dir) / "fastembed")
