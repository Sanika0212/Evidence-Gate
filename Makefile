.PHONY: install models proto lint test test-services build compose-up compose-down seed ingest \
        kind-up kind-down load eval figures aws-up aws-down aws-deploy

PY ?= .venv/bin/python
IMAGE ?= evidencegate:dev
COMPOSE = docker compose -f deploy/compose/docker-compose.yml

install:            ## dev environment
	uv venv --python 3.12 && uv pip install -e ".[serve,eval,viz]" --group dev

models:             ## export the checkers to ONNX (once)
	uv run --no-project --python 3.12 --with "optimum[onnxruntime]>=1.23" --with "torch>=2.4" \
	  --with "transformers>=4.45,<5" --with sentencepiece --with protobuf --with tokenizers \
	  --with numpy --with structlog --with prometheus-client --with opentelemetry-api \
	  python scripts/export_models.py --out models

proto:              ## regenerate gRPC stubs
	. .venv/bin/activate && scripts/gen_proto.sh

lint:
	.venv/bin/ruff check src tests scripts eval && .venv/bin/ruff format --check src tests scripts eval
	.venv/bin/mypy
	buf lint

test:
	.venv/bin/pytest

test-services:      ## needs `make compose-up`
	EG_TEST_SERVICES=1 .venv/bin/pytest -m services

build:
	docker build -t $(IMAGE) .

compose-up: build   ## local stack (SeaweedFS for S3, ElasticMQ for SQS)
	$(COMPOSE) up -d

compose-down:
	$(COMPOSE) down

ingest:             ## load the BioASQ corpus for the demo tenant
	.venv/bin/evidencegate ingest --tenant demo --dataset bioasq

seed:               ## seed the demo tenant's calibration from SciFact expert labels
	.venv/bin/evidencegate seed --tenant demo --dataset scifact --target 0.9

kind-up: build      ## local Kubernetes with Prometheus, Grafana, Argo Rollouts (ARGOCD=1 for GitOps)
	scripts/kind-up.sh

kind-down:
	scripts/kind-down.sh

load:               ## k6 ramp against the kind ingress
	k6 run -e BASE=http://localhost:8090 -e TOKEN_URL=http://localhost:8081/oauth2/token \
	  --summary-export runs/load/summary.json loadtest/k6/check.js

eval:               ## every benchmark in the README
	EG_ONNX_THREADS=4 $(PY) eval/run_detection.py
	$(PY) eval/run_guarantee.py
	$(PY) eval/run_shift.py
	$(PY) eval/run_retrieval.py --n 500

figures:
	$(PY) scripts/make_figures.py

aws-up:             ## everything in AWS from a clean account (costs money; see README)
	cd deploy/terraform/aws && terraform init && terraform apply

aws-deploy:         ## push the image and install the chart on EKS
	scripts/aws-deploy.sh

aws-down:           ## tear it all down
	cd deploy/terraform/aws && terraform destroy
