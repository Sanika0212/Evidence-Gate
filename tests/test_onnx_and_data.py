"""The ONNX checker on a tiny generated model, dataset loaders on local Parquet, telemetry."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from evidencegate import datasets
from evidencegate.onnx_checker import OnnxPairClassifier, load_checker


def tiny_model(directory: Path, kind: str, labels: dict[int, str]) -> Path:
    """A real ONNX sequence classifier: mean of token ids times a weight row per label."""
    onnx = pytest.importorskip("onnx")
    from onnx import TensorProto, helper
    from tokenizers import Tokenizer, models, pre_tokenizers

    directory.mkdir(parents=True, exist_ok=True)
    n = len(labels)
    weights = np.linspace(-1.0, 1.0, n, dtype=np.float32).reshape(1, n)
    graph = helper.make_graph(
        [
            helper.make_node("Cast", ["input_ids"], ["ids_f"], to=TensorProto.FLOAT),
            helper.make_node("Cast", ["attention_mask"], ["mask_f"], to=TensorProto.FLOAT),
            helper.make_node("Mul", ["ids_f", "mask_f"], ["masked"]),
            helper.make_node("ReduceMean", ["masked"], ["mean"], axes=[1], keepdims=1),
            helper.make_node("Div", ["mean", "scale"], ["scaled"]),
            helper.make_node("MatMul", ["scaled", "w"], ["logits"]),
        ],
        "tiny",
        [
            helper.make_tensor_value_info("input_ids", TensorProto.INT64, ["b", "s"]),
            helper.make_tensor_value_info("attention_mask", TensorProto.INT64, ["b", "s"]),
        ],
        [helper.make_tensor_value_info("logits", TensorProto.FLOAT, ["b", n])],
        [
            helper.make_tensor("w", TensorProto.FLOAT, [1, n], weights.flatten().tolist()),
            helper.make_tensor("scale", TensorProto.FLOAT, [], [10.0]),
        ],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 8
    onnx.save(model, directory / "model.onnx")
    vocab = {"[PAD]": 0, "[UNK]": 1} | {
        w: i + 2
        for i, w in enumerate(
            ["aspirin", "inhibits", "cox", "the", "moon", "is", "made", "of", "cheese"]
        )
    }
    tok = Tokenizer(models.WordLevel(vocab, unk_token="[UNK]"))
    tok.pre_tokenizer = pre_tokenizers.Whitespace()
    tok.save(str(directory / "tokenizer.json"))
    (directory / "config.json").write_text(
        json.dumps({"id2label": {str(k): v for k, v in labels.items()}})
    )
    (directory / "meta.json").write_text(json.dumps({"kind": kind, "max_length": 8}))
    return directory


def test_onnx_minicheck_and_nli(tmp_path: Path) -> None:
    mc = OnnxPairClassifier(
        tiny_model(tmp_path / "minicheck", "minicheck", {0: "unsupported", 1: "supported"}),
        batch_size=2,
    )
    pairs = [
        ("aspirin inhibits cox", "aspirin inhibits cox"),
        ("the moon", "is made of cheese cheese cheese"),
        ("x", "y"),
    ]
    scores = mc.score(pairs)
    assert len(scores) == 3
    assert all(0 <= s <= 1 and c == 0.0 for s, c in scores)
    assert scores[1][0] != scores[2][0]
    assert mc.score(pairs[:1]) == scores[:1]  # cached
    assert not mc.three_way
    assert mc.name == "minicheck"
    nli = OnnxPairClassifier(
        tiny_model(tmp_path / "nli", "nli", {0: "contradiction", 1: "entailment", 2: "neutral"})
    )
    (s, c) = nli.score([("aspirin", "aspirin inhibits cox")])[0]
    assert nli.three_way
    assert 0 < s < 1
    assert 0 < c < 1
    combined = load_checker("minicheck+nli", tmp_path)
    assert combined.score([("aspirin", "cox")])[0][1] > 0


def test_onnx_rejects_bad_models(tmp_path: Path) -> None:
    d = tiny_model(tmp_path / "bad", "nli", {0: "yes", 1: "no"})
    with pytest.raises(ValueError, match="entailment"):
        OnnxPairClassifier(d)
    (d / "meta.json").write_text(json.dumps({"kind": "other"}))
    with pytest.raises(ValueError, match="unknown checker kind"):
        OnnxPairClassifier(d)


@pytest.fixture
def local_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import pandas as pd

    monkeypatch.setenv("EG_DATA_DIR", str(tmp_path))
    pd.DataFrame(
        {"passage": ["Aspirin inhibits COX.", "nan", "  Metformin  treats diabetes. "]},
        index=pd.Index([7, 8, 9], name="id"),
    ).to_parquet(tmp_path / "bioasq-corpus.parquet")
    pd.DataFrame(
        {
            "question": ["q1", "q2"],
            "answer": ["a1", "a2"],
            "relevant_passage_ids": ["[7, 9]", "not a list"],
            "id": [1, 2],
        }
    ).to_parquet(tmp_path / "bioasq-questions.parquet")
    pd.DataFrame(
        {
            "Question": ["Does aspirin inhibit COX?"],
            "Knowledge": [["Aspirin inhibits COX."]],
            "Ground Truth": ["Yes, aspirin inhibits COX."],
            "Difficulty Level": ["hard"],
            "Hallucinated Answer": ["No, aspirin activates COX."],
            "Category of Hallucination": ["Misinterpretation"],
        }
    ).to_parquet(tmp_path / "medhallu-labeled.parquet")
    return tmp_path


def test_dataset_loaders(local_data: Path) -> None:
    corpus = list(datasets.bioasq_corpus())
    assert corpus == [
        ("7", "Aspirin inhibits COX.", "bioasq"),
        ("9", "Metformin treats diabetes.", "bioasq"),
    ]
    qs = datasets.bioasq_questions()
    assert len(qs) == 1
    assert qs[0].relevant == {"7", "9"}
    med = datasets.medhallu("labeled")
    assert [m.unsupported for m in med] == [False, True]
    assert med[1].category == "Misinterpretation"
    assert med[1].difficulty == "hard"


def test_fetch_downloads_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EG_DATA_DIR", str(tmp_path))
    calls: list[str] = []

    class Resp:
        def __enter__(self) -> Resp:
            return self

        def __exit__(self, *a: Any) -> None:
            return None

        def raise_for_status(self) -> None:
            return None

        def iter_bytes(self, n: int) -> list[bytes]:
            return [b"PAR1"]

    def stream(method: str, url: str, **kw: Any) -> Resp:
        calls.append(url)
        return Resp()

    monkeypatch.setattr(datasets.httpx, "stream", stream)
    p = datasets.fetch("bioasq-corpus")
    assert p.read_bytes() == b"PAR1"
    datasets.fetch("bioasq-corpus")
    assert len(calls) == 1
    assert "rag-mini-bioasq" in calls[0]


def test_telemetry_configure_with_tracing(monkeypatch: pytest.MonkeyPatch) -> None:
    from opentelemetry.exporter.otlp.proto.grpc import trace_exporter
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from evidencegate import telemetry

    exported = InMemorySpanExporter()
    monkeypatch.setattr(trace_exporter, "OTLPSpanExporter", lambda: exported)
    monkeypatch.setattr(telemetry, "_configured", False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    telemetry.configure("evidencegate-test", level="DEBUG", json_logs=False)
    telemetry.configure("ignored")  # idempotent
    log = telemetry.get_logger("t")
    with telemetry.tracer().start_as_current_span("unit"):
        log.info("inside_span", n=1)
    assert telemetry._configured
