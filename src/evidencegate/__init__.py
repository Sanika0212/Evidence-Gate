"""Evidence Gate: a trust gateway that checks every claim in a RAG answer against its sources.

Each tenant chooses a recall target ("flag at least 90% of unsupported claims"); the
calibrator sets the threshold by split conformal calibration on that tenant's labelled
claims, watches for drift, and recalibrates from reviewer labels.

The services:

==============  ==================================================================
gateway         REST API: auth, rate limits, PII redaction, audit log, review page
retriever       hybrid lexical + dense retrieval with a cross-encoder reranker (gRPC)
checker         claim checking with ONNX MiniCheck + NLI on top of Groundsight (gRPC)
calibrator      per-tenant conformal thresholds, labels, drift detection (gRPC)
triage-worker   replays flagged answers with the fix their cause implies (queue)
==============  ==================================================================
"""

from evidencegate._version import __version__

__all__ = ["__version__"]
