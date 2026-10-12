# Cluster add-ons the chart relies on. The application itself is deployed by Argo CD or
# `helm upgrade --install` with the generated values file (see outputs.tf).

resource "helm_release" "lb_controller" {
  count      = var.install_addons ? 1 : 0
  name       = "aws-load-balancer-controller"
  repository = "https://aws.github.io/eks-charts"
  chart      = "aws-load-balancer-controller"
  version    = "1.9.2"
  namespace  = "kube-system"
  values = [yamlencode({
    clusterName = module.eks.cluster_name
    region      = var.region
    vpcId       = module.vpc.vpc_id
    serviceAccount = {
      name        = "aws-load-balancer-controller"
      annotations = { "eks.amazonaws.com/role-arn" = module.lb_controller_irsa.iam_role_arn }
    }
  })]
}

resource "helm_release" "external_secrets" {
  count            = var.install_addons ? 1 : 0
  name             = "external-secrets"
  repository       = "https://charts.external-secrets.io"
  chart            = "external-secrets"
  version          = "0.10.4"
  namespace        = "external-secrets"
  create_namespace = true
  values = [yamlencode({
    serviceAccount = {
      name        = "external-secrets"
      annotations = { "eks.amazonaws.com/role-arn" = module.external_secrets_irsa.iam_role_arn }
    }
  })]
}

resource "helm_release" "argo_rollouts" {
  count            = var.install_addons ? 1 : 0
  name             = "argo-rollouts"
  repository       = "https://argoproj.github.io/argo-helm"
  chart            = "argo-rollouts"
  version          = "2.37.7"
  namespace        = "argo-rollouts"
  create_namespace = true
}

resource "helm_release" "argo_cd" {
  count            = var.install_addons ? 1 : 0
  name             = "argo-cd"
  repository       = "https://argoproj.github.io/argo-helm"
  chart            = "argo-cd"
  version          = "7.6.12"
  namespace        = "argocd"
  create_namespace = true
  values           = [yamlencode({ configs = { params = { "server.insecure" = true } } })]
}

resource "helm_release" "prometheus" {
  count            = var.install_addons ? 1 : 0
  name             = "prometheus"
  repository       = "https://prometheus-community.github.io/helm-charts"
  chart            = "prometheus"
  version          = "25.27.0"
  namespace        = "observability"
  create_namespace = true
  values = [
    file("${path.module}/../../observability/prometheus-values.yaml"),
    yamlencode({ serverFiles = { "recording_rules.yml" = yamldecode(file("${path.module}/../../observability/prometheus-rules.yml")) } }),
  ]
}

resource "helm_release" "grafana" {
  count            = var.install_addons ? 1 : 0
  name             = "grafana"
  repository       = "https://grafana.github.io/helm-charts"
  chart            = "grafana"
  version          = "8.5.8"
  namespace        = "observability"
  create_namespace = true
  values = [
    file("${path.module}/../../observability/grafana-values.yaml"),
    yamlencode({ dashboards = { evidencegate = { evidencegate = { json = file("${path.module}/../../observability/grafana/dashboards/evidencegate.json") } } } }),
  ]
}
