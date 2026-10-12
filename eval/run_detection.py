"""Does the gate catch unsupported content? AUROC per checker on medical and general data.

    python eval/run_detection.py

Sets:
1. MedHallu, expert-labelled split: 1,000 PubMedQA questions, each with a grounded and a
   hallucinated answer (2,000 answers). An answer's score is its weakest claim's support.
2. RAGTruth test: 300 answers (100 per task) with human span labels, scored per sentence.

Checkers: the service's ONNX MiniCheck (int8 and fp32), ONNX DeBERTa NLI (int8;
support = entailment probability), embedding similarity (the common RAG shortcut,
using the retriever's arctic-embed-xs), and word overlap. Each AUROC has a 95%
bootstrap interval. Throughput is measured on this machine's CPU.
"""

from __future__ import annotations

import time

from common import bootstrap_auroc, cached, write

from evidencegate.embedding import load_embedder
from evidencegate.evaluation import EmbeddingChecker, score_answers
from evidencegate.onnx_checker import load_checker

CHECKERS = {
    "minicheck-int8": lambda: load_checker("minicheck", "models"),
    "minicheck-fp32": lambda: load_checker("minicheck-fp32", "models"),
    "nli-int8": lambda: load_checker("nli", "models"),
    "embedding-similarity": lambda: EmbeddingChecker(
        load_embedder("snowflake/snowflake-arctic-embed-xs", "models")
    ),
    "word-overlap": lambda: load_checker("lexical"),
}
# (set, dataset, answers, checkers). The fp32 model only runs on a 600-answer subset:
# its role is to show int8 loses nothing, measured on the same answers.
JOBS = [
    (
        "medhallu",
        "medhallu-labeled",
        0,
        ["minicheck-int8", "nli-int8", "embedding-similarity", "word-overlap"],
    ),
    ("medhallu-600", "medhallu-labeled", 600, ["minicheck-int8", "minicheck-fp32"]),
    ("ragtruth", "ragtruth-test", 300, ["minicheck-int8", "embedding-similarity", "word-overlap"]),
]


def main() -> None:
    results = []
    for set_name, dataset, limit, names in JOBS:
        for name in names:
            checker = CHECKERS[name]()
            started = time.time()
            rows = cached(
                f"{set_name}-{name}",
                lambda c=checker, d=dataset, n=limit: score_answers(d, c, limit=n),
            )
            elapsed = time.time() - started
            auc, lo, hi = bootstrap_auroc(rows)
            results.append(
                {
                    "set": set_name,
                    "checker": name,
                    "items": len(rows),
                    "unsupported": sum(r.unsupported for r in rows),
                    "auroc": round(auc, 3),
                    "ci95": [round(lo, 3), round(hi, 3)],
                    "seconds": round(elapsed, 1),
                }
            )
            print(
                f"{set_name:9s} {name:22s} AUROC {auc:.3f} [{lo:.3f}, {hi:.3f}]  n={len(rows)}",
                flush=True,
            )
    print("wrote", write("detection", results))


if __name__ == "__main__":
    main()
