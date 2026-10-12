"""Export the claim checkers to ONNX (full precision and dynamic int8) and check parity.

    uv run --with "optimum[onnxruntime]>=1.23" --with "torch>=2.4" --with "transformers>=4.45" \
        --with sentencepiece --with protobuf python scripts/export_models.py --out models

Writes ``<out>/<name>`` (int8) and ``<out>/<name>-fp32`` for each checker, each with
``model.onnx``, ``tokenizer.json``, ``config.json`` and ``meta.json``. Parity: the
exported models score a fixed set of premise/claim pairs and the script reports the
largest probability difference from the PyTorch model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import time
from pathlib import Path

import numpy as np

CHECKERS = {
    "minicheck": ("lytang/MiniCheck-RoBERTa-Large", "minicheck"),
    "nli": ("cross-encoder/nli-deberta-v3-base", "nli"),
}

PAIRS = [
    (
        "The Eiffel Tower is 330 metres tall and was completed in 1889.",
        "The tower was completed in 1889.",
    ),
    (
        "The Eiffel Tower is 330 metres tall and was completed in 1889.",
        "The tower is 500 metres tall.",
    ),
    (
        "Metformin is first-line therapy for type 2 diabetes in most adults.",
        "Metformin is recommended first for type 2 diabetes.",
    ),
    (
        "Metformin is first-line therapy for type 2 diabetes in most adults.",
        "Insulin is always the first treatment for type 2 diabetes.",
    ),
    (
        "Screening for colorectal cancer is recommended for adults aged 45 to 75.",
        "Adults over 90 should start colorectal screening.",
    ),
    ("Aspirin reduced recurrent stroke risk in the trial cohort.", "The trial studied aspirin."),
]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def softmax(x: np.ndarray) -> np.ndarray:
    e = np.exp(x - x.max(axis=-1, keepdims=True))
    return e / e.sum(axis=-1, keepdims=True)


def torch_probs(model_id: str) -> np.ndarray:
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    tok = AutoTokenizer.from_pretrained(model_id)
    model = AutoModelForSequenceClassification.from_pretrained(model_id).eval()
    enc = tok(
        [p for p, _ in PAIRS],
        [c for _, c in PAIRS],
        truncation="only_first",
        max_length=512,
        padding=True,
        return_tensors="pt",
    )
    with torch.inference_mode():
        return softmax(model(**enc).logits.float().numpy())


def onnx_probs(model_dir: Path) -> np.ndarray:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from evidencegate.onnx_checker import OnnxPairClassifier

    clf = OnnxPairClassifier(model_dir)
    return softmax(clf._forward(PAIRS))


def export(name: str, model_id: str, kind: str, out: Path) -> dict[str, object]:
    from onnxruntime.quantization import QuantType, quantize_dynamic
    from optimum.onnxruntime import ORTModelForSequenceClassification
    from transformers import AutoTokenizer

    fp32 = out / f"{name}-fp32"
    int8 = out / name
    for d in (fp32, int8):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
    started = time.time()
    model = ORTModelForSequenceClassification.from_pretrained(model_id, export=True)
    model.save_pretrained(fp32)
    AutoTokenizer.from_pretrained(model_id).save_pretrained(fp32)
    if not (fp32 / "tokenizer.json").exists():
        raise SystemExit(f"{model_id}: no fast tokenizer (tokenizer.json) was written")
    # Per-channel int8 weights on MatMul only. Per-tensor quantization (the default) breaks
    # DeBERTa-v3: its NLI probabilities moved by up to 0.99.
    quantize_dynamic(
        fp32 / "model.onnx",
        int8 / "model.onnx",
        weight_type=QuantType.QInt8,
        op_types_to_quantize=["MatMul"],
        per_channel=True,
        reduce_range=True,
    )
    for f in ("tokenizer.json", "config.json"):
        shutil.copy(fp32 / f, int8 / f)
    # Keep only what the runtime reads.
    for d in (fp32, int8):
        for f in d.iterdir():
            if f.name not in {"model.onnx", "tokenizer.json", "config.json"}:
                f.unlink() if f.is_file() else shutil.rmtree(f)
    ref = torch_probs(model_id)
    report: dict[str, object] = {
        "source": model_id,
        "export_seconds": round(time.time() - started, 1),
    }
    for precision, d in (("fp32", fp32), ("int8", int8)):
        meta = {
            "kind": kind,
            "source": model_id,
            "precision": precision,
            "max_length": 512,
            "sha256": sha256(d / "model.onnx"),
            "bytes": (d / "model.onnx").stat().st_size,
            "exported": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        (d / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
        probs = onnx_probs(d)
        meta["max_abs_prob_diff_vs_torch"] = float(np.abs(probs - ref).max())
        (d / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
        report[precision] = {k: meta[k] for k in ("bytes", "max_abs_prob_diff_vs_torch")}
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="models")
    parser.add_argument("--only", choices=sorted(CHECKERS), action="append")
    args = parser.parse_args()
    out = Path(args.out)
    reports = {}
    for name, (model_id, kind) in CHECKERS.items():
        if args.only and name not in args.only:
            continue
        reports[name] = export(name, model_id, kind, out)
        print(name, json.dumps(reports[name]))
    (out / "export-report.json").write_text(json.dumps(reports, indent=2) + "\n")


if __name__ == "__main__":
    main()
