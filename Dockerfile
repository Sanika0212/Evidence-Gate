# One image, one command per service:
#   evidencegate serve gateway | checker | retriever | calibrator | devauth
#   evidencegate worker triage | evidencegate recalibrate --all
# Model weights are not baked in: they are mounted at /models (Compose) or synced from
# object storage by an init container (Kubernetes).

FROM python:3.12-slim AS build
# The spaCy model behind PII redaction (en_core_web_sm is 13 MB but misses some names).
ARG SPACY_MODEL=en_core_web_lg
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv
WORKDIR /src
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
RUN uv venv /opt/venv && \
    VIRTUAL_ENV=/opt/venv uv pip install --no-cache ".[serve]" && \
    VIRTUAL_ENV=/opt/venv uv pip install --no-cache \
      "${SPACY_MODEL} @ https://github.com/explosion/spacy-models/releases/download/${SPACY_MODEL}-3.8.0/${SPACY_MODEL}-3.8.0-py3-none-any.whl"

FROM python:3.12-slim
LABEL org.opencontainers.image.title="evidencegate" \
      org.opencontainers.image.source="https://github.com/Sanika0212/Evidence-Gate" \
      org.opencontainers.image.licenses="Apache-2.0"
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --home /app app
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    EG_MODELS_DIR=/models \
    HF_HOME=/tmp/hf \
    OMP_NUM_THREADS=2
WORKDIR /app
USER 10001
ENTRYPOINT ["evidencegate"]
CMD ["serve", "gateway"]
