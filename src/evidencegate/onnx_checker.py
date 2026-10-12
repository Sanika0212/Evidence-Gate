"""Claim checkers on ONNX Runtime, implementing Groundsight's ``Checker`` protocol.

The service images carry ``onnxruntime`` and ``tokenizers`` only: no torch. Each model
directory holds ``model.onnx``, ``tokenizer.json``, ``config.json`` and ``meta.json``,
written by ``scripts/export_models.py``:

==================  ===========================================  ============
directory           source model                                 output
==================  ===========================================  ============
``minicheck``       lytang/MiniCheck-RoBERTa-Large (int8)        support
``minicheck-fp32``  same, full precision                         support
``nli``             cross-encoder/nli-deberta-v3-base (int8)     3-way
``nli-fp32``        same, full precision                         3-way
==================  ===========================================  ============

Scores match Groundsight's PyTorch checkers: MiniCheck's support is the softmax
probability of its last label; NLI returns (entailment, contradiction) probabilities.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np

from evidencegate.telemetry import STAGE_LATENCY

Pair = tuple[str, str]
Score = tuple[float, float]


def _softmax(logits: np.ndarray) -> np.ndarray:
    shifted = logits - logits.max(axis=-1, keepdims=True)
    exp = np.exp(shifted)
    return np.asarray(exp / exp.sum(axis=-1, keepdims=True))


class OnnxPairClassifier:
    """A sequence-pair classifier exported to ONNX, scored in length-sorted batches."""

    def __init__(
        self,
        model_dir: str | os.PathLike[str],
        *,
        batch_size: int = 16,
        max_length: int = 512,
        threads: int | None = None,
        cache_size: int = 50_000,
    ) -> None:
        self.model_dir = Path(model_dir)
        meta = json.loads((self.model_dir / "meta.json").read_text())
        config = json.loads((self.model_dir / "config.json").read_text())
        self.kind: str = meta["kind"]
        if self.kind not in {"minicheck", "nli"}:
            raise ValueError(f"unknown checker kind {self.kind!r} in {self.model_dir}")
        self.name = self.model_dir.name
        self.three_way = self.kind == "nli"
        self.batch_size = batch_size
        self.max_length = int(meta.get("max_length", max_length))
        self.threads = threads
        self.cache_size = cache_size
        self._cache: dict[Pair, Score] = {}
        id2label = {int(k): str(v).lower() for k, v in config.get("id2label", {}).items()}
        self._entail = next((i for i, n in id2label.items() if n.startswith("entail")), -1)
        self._contra = next((i for i, n in id2label.items() if n.startswith("contradict")), -1)
        if self.three_way and (self._entail < 0 or self._contra < 0):
            raise ValueError(f"{self.model_dir}: NLI model needs entailment and contradiction")
        self._session: Any = None
        self._tokenizer: Any = None
        self._input_names: list[str] = []

    def _load(self) -> None:
        if self._session is not None:
            return
        import onnxruntime as ort
        from tokenizers import Tokenizer

        if hasattr(ort, "disable_telemetry_events"):
            ort.disable_telemetry_events()
        options = ort.SessionOptions()
        threads = self.threads or int(os.environ.get("EG_ONNX_THREADS", "0") or 0)
        if threads:
            options.intra_op_num_threads = threads
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self._session = ort.InferenceSession(
            str(self.model_dir / "model.onnx"), options, providers=["CPUExecutionProvider"]
        )
        self._input_names = [i.name for i in self._session.get_inputs()]
        tokenizer = Tokenizer.from_file(str(self.model_dir / "tokenizer.json"))
        tokenizer.enable_truncation(max_length=self.max_length, strategy="only_first")
        pad_token = next(
            (t for t in ("<pad>", "[PAD]") if tokenizer.token_to_id(t) is not None), None
        )
        if pad_token is None:
            raise ValueError(f"{self.model_dir}: tokenizer has no pad token")
        tokenizer.enable_padding(pad_id=tokenizer.token_to_id(pad_token), pad_token=pad_token)
        self._tokenizer = tokenizer

    def _forward(self, batch: Sequence[Pair]) -> np.ndarray:
        self._load()
        encodings = self._tokenizer.encode_batch([(p, c) for p, c in batch])
        feeds: dict[str, np.ndarray] = {
            "input_ids": np.array([e.ids for e in encodings], dtype=np.int64),
            "attention_mask": np.array([e.attention_mask for e in encodings], dtype=np.int64),
        }
        if "token_type_ids" in self._input_names:
            feeds["token_type_ids"] = np.array([e.type_ids for e in encodings], dtype=np.int64)
        started = time.perf_counter()
        (logits,) = self._session.run(["logits"], {k: feeds[k] for k in self._input_names})
        STAGE_LATENCY.labels(stage=f"onnx_{self.kind}").observe(time.perf_counter() - started)
        return np.asarray(logits, dtype=np.float32)

    def _convert(self, logits: np.ndarray) -> list[Score]:
        probs = _softmax(logits)
        if self.three_way:
            return [(float(p[self._entail]), float(p[self._contra])) for p in probs]
        return [(float(p[-1]), 0.0) for p in probs]

    def score(self, pairs: Sequence[Pair]) -> list[Score]:
        todo = list(dict.fromkeys(p for p in pairs if p not in self._cache))
        # Similar lengths in a batch waste less padding.
        todo.sort(key=lambda p: len(p[0]) + len(p[1]))
        for i in range(0, len(todo), self.batch_size):
            batch = todo[i : i + self.batch_size]
            for pair, result in zip(batch, self._convert(self._forward(batch)), strict=True):
                self._cache[pair] = result
        out = [self._cache[p] for p in pairs]
        if len(self._cache) > self.cache_size:
            self._cache.clear()
        return out


def load_checker(spec: str, models_dir: str | os.PathLike[str] = "models") -> Any:
    """Build a checker from a spec such as ``"minicheck+nli"`` or ``"lexical"``.

    ``lexical`` is Groundsight's word-overlap checker: no model files, used in tests and
    CI. Any other name is a model directory under ``models_dir``.
    """
    from groundsight.checkers import Combined, LexicalChecker

    if "+" in spec:
        support, contradiction = spec.split("+", 1)
        return Combined(load_checker(support, models_dir), load_checker(contradiction, models_dir))
    if spec == "lexical":
        return LexicalChecker()
    return OnnxPairClassifier(Path(models_dir) / spec)
