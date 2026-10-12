"""Public datasets used for the demo corpus, seed calibration and evaluation.

All are downloaded from the Hugging Face Hub's Parquet exports and cached under
``data/``. Nothing private is used anywhere.

=================  ==========================================================  ========
name               source                                                      licence
=================  ==========================================================  ========
bioasq corpus      rag-datasets/rag-mini-bioasq (BioASQ 11b subset), 40,221    CC BY 2.5
                   PubMed abstracts and 4,719 questions with gold passage ids
medhallu           UTAustin-AIHealth/MedHallu: PubMedQA questions with a       see card
                   grounded answer and a hallucinated answer each
ragtruth           wandb/RAGTruth-processed: answers from six LLMs with         see card
                   human span-level hallucination labels
=================  ==========================================================  ========
"""

from __future__ import annotations

import ast
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

HF = "https://huggingface.co/datasets/{repo}/resolve/refs%2Fconvert%2Fparquet/{path}"
FILES = {
    "bioasq-corpus": ("rag-datasets/rag-mini-bioasq", "text-corpus/passages/0000.parquet"),
    "bioasq-questions": (
        "rag-datasets/rag-mini-bioasq",
        "question-answer-passages/test/0000.parquet",
    ),
    "medhallu-labeled": ("UTAustin-AIHealth/MedHallu", "pqa_labeled/train/0000.parquet"),
    "medhallu-artificial": ("UTAustin-AIHealth/MedHallu", "pqa_artificial/train/0000.parquet"),
    "ragtruth-train": ("wandb/RAGTruth-processed", "default/train/0000.parquet"),
    "ragtruth-test": ("wandb/RAGTruth-processed", "default/test/0000.parquet"),
}


def data_dir() -> Path:
    return Path(os.environ.get("EG_DATA_DIR", "data"))


def fetch(name: str) -> Path:
    """Download one Parquet file (once) and return its local path."""
    repo, path = FILES[name]
    dest = data_dir() / f"{name}.parquet"
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    with httpx.stream(
        "GET", HF.format(repo=repo, path=path), follow_redirects=True, timeout=120
    ) as r:
        r.raise_for_status()
        with tmp.open("wb") as fh:
            for chunk in r.iter_bytes(1 << 20):
                fh.write(chunk)
    tmp.rename(dest)
    return dest


def read(name: str) -> Any:
    import pandas as pd

    df = pd.read_parquet(fetch(name))
    # Some exports store a column (such as BioASQ's passage id) as the index.
    return df.reset_index() if df.index.name else df


def bioasq_corpus() -> Iterator[tuple[str, str, str]]:
    """``(id, text, source)`` for each BioASQ passage with text."""
    df = read("bioasq-corpus")
    for row in df.itertuples(index=False):
        text = str(row.passage or "").strip()
        if text and text.lower() != "nan":
            yield str(row.id), " ".join(text.split()), "bioasq"


@dataclass
class RetrievalQuestion:
    id: str
    question: str
    answer: str
    relevant: set[str]


def bioasq_questions() -> list[RetrievalQuestion]:
    df = read("bioasq-questions")
    out = []
    for row in df.itertuples(index=False):
        try:
            ids = {str(i) for i in ast.literal_eval(str(row.relevant_passage_ids))}
        except (ValueError, SyntaxError):
            continue
        if ids:
            out.append(RetrievalQuestion(str(row.id), str(row.question), str(row.answer), ids))
    return out


@dataclass
class LabelledAnswer:
    """An answer with passages and a gold label: is it supported by the passages?"""

    id: str
    question: str
    passages: list[str]
    answer: str
    unsupported: bool
    category: str = ""
    difficulty: str = ""


def medhallu(config: str = "labeled") -> list[LabelledAnswer]:
    """Each MedHallu row gives two answers: the ground truth and a hallucinated one."""
    df = read(f"medhallu-{config}")
    out = []
    for i, row in enumerate(df.to_dict("records")):
        knowledge = [
            str(k) for k in (row.get("Knowledge") if row.get("Knowledge") is not None else [])
        ]
        q = str(row["Question"])
        out.append(
            LabelledAnswer(f"{config}-{i}-gt", q, knowledge, str(row["Ground Truth"]), False)
        )
        out.append(
            LabelledAnswer(
                f"{config}-{i}-h",
                q,
                knowledge,
                str(row["Hallucinated Answer"]),
                True,
                str(row.get("Category of Hallucination") or ""),
                str(row.get("Difficulty Level") or ""),
            )
        )
    return out
