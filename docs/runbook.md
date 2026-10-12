# Runbook

## Local stack (Docker Compose)

1. `make models` once, to export the checkers to ONNX.
2. `make compose-up`, then `make ingest` (BioASQ corpus) and `make seed` (demo tenant's
   seed calibration from MedHallu's generated split).
3. Open http://localhost:8080 for the demo page. For the API, get a token with
   `evidencegate token` and send `POST /v1/check` with `Authorization: Bearer <token>`.
4. Review queue: http://localhost:8080/review?token=local-review-token

## Local Kubernetes (kind)

`make kind-up` creates the cluster, installs ingress-nginx, metrics-server, Prometheus,
Grafana and Argo Rollouts, and installs the chart with `values-kind.yaml`. The gateway is
at http://localhost:8090.

## Canary drill

1. Start traffic: `make load` (or any steady stream of checks).
2. Ship a bad checker: `helm upgrade eg deploy/helm/evidence-gate -n evidence-gate
   -f deploy/helm/evidence-gate/values-kind.yaml --set checker.faultRate=0.3`.
3. Watch: `kubectl argo rollouts get rollout evidence-gate-checker -n evidence-gate -w`.
   The canary takes 10% of traffic; the analysis sees its error rate above 2% and the
   rollout aborts and returns all traffic to the stable pods.
4. Undo the setting with `--set checker.faultRate=0`.

## Drift and recalibration

The `evidence-gate-recalibrate` CronJob runs hourly. For each tenant it compares live
support scores with the calibration scores (PSI) and measures recall on recent
reviewer labels (weighted for the audit sample). When either is out of tolerance and
there are enough labelled unsupported claims, it refits the threshold. Manually:
`evidencegate drift --tenant demo` and `evidencegate recalibrate --tenant demo --force`.

## AWS

1. `make aws-up` (needs `budget_email`; about 15 minutes for EKS).
2. `make aws-deploy` pushes the image, uploads the models, installs the chart.
3. `make aws-down` destroys everything; then check Resource Groups for the tag
   `project=evidence-gate`.
