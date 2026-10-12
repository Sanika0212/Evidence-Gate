#!/usr/bin/env bash
# After `make aws-up`: push the image to ECR, upload the models to S3, install the chart.
set -euo pipefail
cd "$(dirname "$0")/.."
TF="terraform -chdir=deploy/terraform/aws"
REGION=$($TF output -raw region 2>/dev/null || echo us-east-1)
REPO=$($TF output -raw ecr_repository_url)
MODELS=$($TF output -raw models_bucket)
CLUSTER=$($TF output -raw cluster_name)
TAG=${TAG:-$(git rev-parse --short HEAD)}

aws eks update-kubeconfig --name "$CLUSTER" --region "$REGION" >/dev/null
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "${REPO%%/*}"
# EKS nodes are x86_64.
docker buildx build --platform linux/amd64 -t "$REPO:$TAG" --push .

aws s3 sync models/minicheck "s3://$MODELS/minicheck" --only-show-errors
aws s3 sync models/nli "s3://$MODELS/nli" --only-show-errors
aws s3 sync models/fastembed "s3://$MODELS/fastembed" --only-show-errors

kubectl get namespace evidence-gate >/dev/null 2>&1 || kubectl create namespace evidence-gate
kubectl apply -f - <<'EOF'
apiVersion: external-secrets.io/v1beta1
kind: ClusterSecretStore
metadata: { name: aws-secrets-manager }
spec:
  provider:
    aws:
      service: SecretsManager
      region: us-east-1
      auth: { jwt: { serviceAccountRef: { name: external-secrets, namespace: external-secrets } } }
EOF
helm upgrade --install eg deploy/helm/evidence-gate -n evidence-gate \
  -f deploy/helm/evidence-gate/values-aws.yaml \
  -f deploy/helm/evidence-gate/values-aws.generated.yaml \
  --set image.tag="$TAG" --wait --timeout 20m
kubectl -n evidence-gate get pods,ingress
