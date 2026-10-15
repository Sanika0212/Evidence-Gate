"""PII and PHI redaction before anything is written to logs, traces or the audit store.

Checks run on the original text (redaction would change what the claims say); only
storage sees redacted text. :class:`PresidioRedactor` uses Microsoft Presidio with a
spaCy model (large by default) plus medical record number and street address patterns;
:class:`RegexRedactor` covers structured identifiers only and needs no model (tests and
CI). :func:`scoped` makes a redactor for one request: a name recognised anywhere in the
request is redacted everywhere in it, which catches the bare first names and unusual
surnames the model misses in some sentences but not others (``eval/run_pii.py``).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from itertools import pairwise
from typing import Any, Protocol

from evidencegate.telemetry import PII_REDACTED

# HIPAA Safe Harbor identifiers that appear in free text.
ENTITIES = [
    "PERSON",
    "PHONE_NUMBER",
    "EMAIL_ADDRESS",
    "US_SSN",
    "DATE_TIME",
    "LOCATION",
    "IP_ADDRESS",
    "URL",
    "US_DRIVER_LICENSE",
    "MEDICAL_LICENSE",
    "MRN",
]


class Redactor(Protocol):
    def redact(self, text: str) -> tuple[str, Counter[str]]: ...


_PATTERNS = [
    ("EMAIL_ADDRESS", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("US_SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("MRN", re.compile(r"\b(?:MRN|medical record(?: number)?)[\s:#]*\d{5,12}\b", re.I)),
    (
        "PHONE_NUMBER",
        re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)"),
    ),
    ("DATE_TIME", re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b|\b\d{4}-\d{2}-\d{2}\b")),
    ("IP_ADDRESS", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    (
        "STREET_ADDRESS",
        re.compile(
            r"\b\d{1,6}\s+(?:[A-Z][a-z]+\.?\s+){1,3}(?:Street|St|Avenue|Ave|Road|Rd|"
            r"Boulevard|Blvd|Lane|Ln|Drive|Dr|Court|Ct|Way|Place|Pl|Terrace|Parkway|Pkwy)\b"
        ),
    ),
]


class RegexRedactor:
    def redact(self, text: str) -> tuple[str, Counter[str]]:
        counts: Counter[str] = Counter()
        for entity, pattern in _PATTERNS:
            text, n = pattern.subn(f"<{entity}>", text)
            if n:
                counts[entity] += n
        for entity, n in counts.items():
            PII_REDACTED.labels(entity=entity).inc(n)
        return text, counts


class PresidioRedactor:
    def __init__(self, spacy_model: str = "en_core_web_lg", score_threshold: float = 0.4) -> None:
        from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer
        from presidio_analyzer.nlp_engine import NlpEngineProvider
        from presidio_anonymizer import AnonymizerEngine

        nlp = NlpEngineProvider(
            nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": "en", "model_name": spacy_model}],
                # spaCy labels that are not identifiers (otherwise logged on every call).
                "ner_model_configuration": {
                    "labels_to_ignore": [
                        "CARDINAL",
                        "ORDINAL",
                        "QUANTITY",
                        "PERCENT",
                        "MONEY",
                        "PRODUCT",
                        "FAC",
                        "WORK_OF_ART",
                        "LAW",
                        "LANGUAGE",
                        "EVENT",
                    ]
                },
            }
        ).create_engine()
        self._analyzer: Any = AnalyzerEngine(nlp_engine=nlp, supported_languages=["en"])
        self._analyzer.registry.add_recognizer(
            PatternRecognizer(
                supported_entity="MRN",
                patterns=[
                    Pattern("mrn", r"\b(?:MRN|medical record(?: number)?)[\s:#]*\d{5,12}\b", 0.85)
                ],
            )
        )
        self._anonymizer: Any = AnonymizerEngine()
        self.score_threshold = score_threshold

    def redact(self, text: str) -> tuple[str, Counter[str]]:
        if not text:
            return text, Counter()
        # Structured identifiers first, so a street number is not read as a date and
        # the patterns catch what the model misses.
        text, counts = RegexRedactor().redact(text)
        results = self._analyzer.analyze(
            text=text, entities=ENTITIES, language="en", score_threshold=self.score_threshold
        )
        found = Counter(r.entity_type for r in results)
        from presidio_anonymizer.entities import OperatorConfig

        operators = {e: OperatorConfig("replace", {"new_value": f"<{e}>"}) for e in found}
        redacted = self._anonymizer.anonymize(
            text=text, analyzer_results=results, operators=operators
        ).text
        for entity, n in found.items():
            PII_REDACTED.labels(entity=entity).inc(n)
        counts.update(found)
        return redacted, counts

    def find_names(self, texts: list[str]) -> set[str]:
        """Capitalised tokens of every PERSON span Presidio finds in ``texts``."""
        names: set[str] = set()
        for text in texts:
            if not text:
                continue
            for r in self._analyzer.analyze(
                text=text, entities=["PERSON"], language="en", score_threshold=self.score_threshold
            ):
                # The model often tags only the first name: take the capitalised words
                # that follow the span directly as well ("Rosalind" -> "Rosalind Hendricks").
                following = _FOLLOWING.match(text, r.end)
                span = text[r.start : following.end() if following else r.end]
                names.update(
                    tok
                    for tok in re.findall(r"[A-Z][\w'-]+", span)
                    if len(tok) >= 3 and tok not in _NOT_NAMES
                )
        return names


_FOLLOWING = re.compile(r"(?:[ ]+[A-Z][\w'-]+){1,2}")
# Words the model sometimes includes in a PERSON span.
_NOT_NAMES = {"Patient", "Doctor", "Nurse", "Reach", "Registration", "Home", "Contact", "Logged"}


class _ScopedRedactor:
    """Wraps a redactor and also replaces the names already found in this request."""

    def __init__(self, inner: Redactor, names: set[str]) -> None:
        self.inner = inner
        self.pattern = re.compile(
            r"\b(?:" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + r")\b"
        )

    def redact(self, text: str) -> tuple[str, Counter[str]]:
        text, counts = self.inner.redact(text)
        text, n = self.pattern.subn("<PERSON>", text)
        if n:
            counts["PERSON"] += n
            PII_REDACTED.labels(entity="PERSON").inc(n)
        return text, counts


def scoped(redactor: Any, texts: list[str]) -> Redactor:
    """A redactor for one request: names found in any of ``texts`` are redacted in all."""
    find = getattr(redactor, "find_names", None)
    names = find(texts) if find else set()
    return _ScopedRedactor(redactor, names) if names else redactor


def redact_audit(audit_json: str, redactor: Redactor) -> tuple[str, Counter[str]]:
    """Redact every free-text field of a Groundsight audit before it leaves the gateway.

    The audit goes to the triage queue and from there to a language model, so the answer,
    question, passages, claims, evidence and diagnoses are all redacted. Claims are
    redacted one at a time and the answer is rebuilt from them, so claim offsets still
    index the answer; evidence offsets are found again in the redacted passage.
    """
    d = json.loads(audit_json)
    total: Counter[str] = Counter()

    def r(text: str | None) -> str | None:
        if not text:
            return text
        out, counts = redactor.redact(text)
        total.update(counts)
        return out

    answer = d.get("answer") or ""
    claims = sorted(d.get("claims") or [], key=lambda c: c.get("start", 0))
    aligned = all(answer[c["start"] : c["end"]] == c["text"] for c in claims) and all(
        a["end"] <= b["start"] for a, b in pairwise(claims)
    )
    if aligned:
        parts: list[str] = []
        cursor = 0
        for c in claims:
            parts.append(r(answer[cursor : c["start"]]) or "")
            cursor = c["end"]
            c["text"] = r(c["text"]) or ""
            c["start"] = sum(map(len, parts))
            c["end"] = c["start"] + len(c["text"])
            parts.append(c["text"])
        parts.append(r(answer[cursor:]) or "")
        d["answer"] = "".join(parts)
    else:
        d["answer"] = r(answer) or ""
        for c in claims:
            c["text"] = r(c["text"])
            at = d["answer"].find(c["text"] or "")
            c["start"], c["end"] = (at, at + len(c["text"] or "")) if at >= 0 else (0, 0)

    chunks = {}
    for chunk in d.get("chunks") or []:
        chunk["text"] = r(chunk.get("text"))
        chunks[chunk.get("id")] = chunk["text"] or ""
    for c in claims:
        ev = c.get("evidence")
        if ev:
            ev["text"] = r(ev.get("text"))
            at = chunks.get(ev.get("chunk_id"), "").find(ev["text"] or "")
            ev["start"], ev["end"] = (at, at + len(ev["text"] or "")) if at >= 0 else (0, 0)
        diagnosis = c.get("diagnosis")
        if isinstance(diagnosis, dict):
            for key in ("reason", "fix"):
                diagnosis[key] = r(diagnosis.get(key))
    d["question"] = r(d.get("question"))
    d["reference"] = r(d.get("reference"))
    return json.dumps(d), total


def load_redactor(spec: str) -> Any:
    return RegexRedactor() if spec == "regex" else PresidioRedactor(spec)
