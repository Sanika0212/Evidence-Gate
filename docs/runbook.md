# Runbook

## Local stack (Docker Compose)

1. `make models` once, to export the checkers to ONNX.
2. `make compose-up`, then `make ingest` (BioASQ corpus) and `make seed` (demo tenant's
   seed calibration from SciFact's expert labels). `evidencegate seed --scores
   runs/scores/scifact-minicheck-int8.json` reuses scores cached by `eval/` instead of
   scoring again.
3. Open http://localhost:8080 for the demo page. For the API, get a token with
   `evidencegate token` and send `POST /v1/check` with `Authorization: Bearer <token>`.
4. Review queue: http://localhost:8080/review?token=local-review-token

## Local Kubernetes (kind)

`make kind-up` creates the cluster, installs ingress-nginx, metrics-server, Prometheus,
Grafana and Argo Rollouts, and installs the chart with `values-kind.yaml`. The gateway is
at http://localhost:8090; get tokens through
`kubectl -n evidence-gate port-forward svc/evidence-gate-devauth 8081:8081`.
`ARGOCD=1 make kind-up` installs Argo CD and lets it sync the chart from GitHub instead.

1. **Size.** `values-kind.yaml` fits a 4 vCPU, 8 GB Docker VM: requests match measured
   use and each autoscaler stops at 2. A canary needs one extra checker pod, so release a
   new checker at 1 replica and scale up afterwards.
2. **Corpus.** Instead of `make ingest`, restore a dump of the retriever schema:
   `kubectl -n evidence-gate port-forward svc/evidence-gate-postgres 15432:5432` and
   `pg_restore -h localhost -p 15432 -U evidencegate -d evidencegate --no-owner --clean
   --if-exists -n retriever retriever.dump`.
3. **Helm 4.** The scripts pass `--wait=legacy` (the default watcher does not understand
   Argo Rollouts) and `--force-conflicts` (the Rollouts controller co-owns the canary
   steps under server-side apply). If a canary pause never ends, the Rollouts controller's
   queue is stuck: `kubectl -n argo-rollouts rollout restart deploy/argo-rollouts`.

## Load test

`python loadtest/run_steps.py --levels 1 2 4 8 --duration 90s --prometheus
http://localhost:9090` runs k6 at each level and records throughput, latency and how many
audits each checker pod served. Every request is new to the checker's score cache
(`--cached` sends identical requests instead). Pin replicas first so the autoscalers do
not change the setup mid-run: `--set checker.hpa.enabled=false --set checker.replicas=2`.

## Canary drill

`python scripts/canary_drill.py --fault-rate 0.3` does all of this and writes
`runs/canary/results.json`:

1. Steady traffic from k6.
2. A bad checker: the same release with `checker.faultRate=0.3`, so 30% of its calls fail.
3. The rollout's analysis compares the canary's error rate with 2%. With no traffic
   router, Argo Rollouts splits traffic by pod count: with one stable replica the canary
   pod takes about half of the calls (the gateway round-robins over all ready pods).
4. The analysis fails, the rollout aborts and the stable pods take all traffic again.
5. The script restores `checker.faultRate=0`.

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
