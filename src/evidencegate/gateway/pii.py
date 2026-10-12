"""PII and PHI redaction before anything is written to logs, traces or the audit store.

Checks run on the original text (redaction would change what the claims say); only
storage sees redacted text. :class:`PresidioRedactor` uses Microsoft Presidio with a
small spaCy model plus a medical record number pattern; :class:`RegexRedactor` covers
structured identifiers only and needs no model (tests and CI).
"""

from __future__ import annotations

import re
from collections import Counter
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
    def __init__(self, spacy_model: str = "en_core_web_sm", score_threshold: float = 0.4) -> None:
        from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer
        from presidio_analyzer.nlp_engine import NlpEngineProvider
        from presidio_anonymizer import AnonymizerEngine

        nlp = NlpEngineProvider(
            nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": "en", "model_name": spacy_model}],
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
        results = self._analyzer.analyze(
            text=text, entities=ENTITIES, language="en", score_threshold=self.score_threshold
        )
        counts = Counter(r.entity_type for r in results)
        from presidio_anonymizer.entities import OperatorConfig

        operators = {e: OperatorConfig("replace", {"new_value": f"<{e}>"}) for e in counts}
        redacted = self._anonymizer.anonymize(
            text=text, analyzer_results=results, operators=operators
        ).text
        # Structured identifiers Presidio's small model can miss.
        redacted, extra = RegexRedactor().redact(redacted)
        counts.update(extra)
        for entity, n in counts.items():
            if entity not in extra:
                PII_REDACTED.labels(entity=entity).inc(n)
        return redacted, counts


def load_redactor(spec: str) -> Any:
    return RegexRedactor() if spec == "regex" else PresidioRedactor(spec)
