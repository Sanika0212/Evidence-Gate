#!/usr/bin/env bash
# Build a local Kubernetes cluster with everything the chart relies on, then install it.
#
#   scripts/kind-up.sh            # cluster + add-ons + Evidence Gate
#   scripts/kind-down.sh          # delete the cluster
#
# Needs: docker, kind, kubectl, helm, and exported models in ./models.
set -euo pipefail
cd "$(dirname "$0")/.."
CLUSTER=evidence-gate
IMAGE=${EG_IMAGE:-evidencegate:dev}
NS=evidence-gate

if ! kind get clusters | grep -qx "$CLUSTER"; then
  sed "s#MODELS_DIR#$(pwd)/models#" deploy/kind/cluster.yaml > /tmp/eg-kind-cluster.yaml
  kind create cluster --config /tmp/eg-kind-cluster.yaml --wait 120s
fi
kubectl config use-context "kind-$CLUSTER" >/dev/null
docker image inspect "$IMAGE" >/dev/null 2>&1 || docker build -t "$IMAGE" .
kind load docker-image "$IMAGE" --name "$CLUSTER"

helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx >/dev/null
helm repo add metrics-server https://kubernetes-sigs.github.io/metrics-server/ >/dev/null
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null
helm repo add grafana https://grafana.github.io/helm-charts >/dev/null
helm repo add argo https://argoproj.github.io/argo-helm >/dev/null
helm repo update >/dev/null

echo "== ingress-nginx"
helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx -n ingress-nginx --create-namespace \
  --version 4.11.3 \
  --set controller.hostPort.enabled=true --set controller.service.type=ClusterIP \
  --set controller.nodeSelector.ingress-ready=true \
  --set-string controller.tolerations[0].key=node-role.kubernetes.io/control-plane \
  --set-string controller.tolerations[0].effect=NoSchedule \
  --set controller.resources.requests.cpu=50m --set controller.resources.requests.memory=90Mi \
  --set controller.admissionWebhooks.enabled=false --wait

echo "== metrics-server (for the autoscalers)"
helm upgrade --install metrics-server metrics-server/metrics-server -n kube-system \
  --version 3.12.2 --set 'args={--kubelet-insecure-tls}' --wait

echo "== Prometheus and Grafana (with the SLO rules and dashboard)"
python3 scripts/render_obs_values.py /tmp/eg-obs >/dev/null
helm upgrade --install prometheus prometheus-community/prometheus -n observability --create-namespace \
  --version 25.27.0 -f /tmp/eg-obs/prometheus.yaml --wait
helm upgrade --install grafana grafana/grafana -n observability --version 8.5.8 \
  -f /tmp/eg-obs/grafana.yaml --wait

echo "== Argo Rollouts"
helm upgrade --install argo-rollouts argo/argo-rollouts -n argo-rollouts --create-namespace \
  --version 2.37.7 --set controller.resources.requests.cpu=50m --wait

echo "== Evidence Gate"
kubectl get namespace "$NS" >/dev/null 2>&1 || kubectl create namespace "$NS"
helm upgrade --install eg deploy/helm/evidence-gate -n "$NS" \
  -f deploy/helm/evidence-gate/values-kind.yaml --set image.tag="${IMAGE#*:}" \
  --wait --timeout 15m
kubectl -n "$NS" get pods
echo
echo "Gateway:  http://localhost:8090/   (demo page)"
echo "Grafana:  kubectl -n observability port-forward svc/grafana 3000:80"
