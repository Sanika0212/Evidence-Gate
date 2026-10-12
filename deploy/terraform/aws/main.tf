data "aws_caller_identity" "current" {}
data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs        = slice(data.aws_availability_zones.available.names, 0, 2)
  account_id = data.aws_caller_identity.current.account_id
  namespace  = "evidence-gate"
}

# --------------------------------------------------------------------------- network

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.13"

  name                    = var.name
  cidr                    = "10.42.0.0/16"
  azs                     = local.azs
  public_subnets          = ["10.42.0.0/20", "10.42.16.0/20"]
  private_subnets         = ["10.42.64.0/20", "10.42.80.0/20"]
  enable_nat_gateway      = var.use_nat_gateway
  single_nat_gateway      = true
  map_public_ip_on_launch = !var.use_nat_gateway
  enable_dns_hostnames    = true

  public_subnet_tags  = { "kubernetes.io/role/elb" = 1 }
  private_subnet_tags = { "kubernetes.io/role/internal-elb" = 1 }
}

# --------------------------------------------------------------------------- cluster

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.24"

  cluster_name                             = var.name
  cluster_version                          = var.kubernetes_version
  cluster_endpoint_public_access           = true
  enable_cluster_creator_admin_permissions = true
  enable_irsa                              = true

  vpc_id     = module.vpc.vpc_id
  subnet_ids = var.use_nat_gateway ? module.vpc.private_subnets : module.vpc.public_subnets

  cluster_addons = {
    coredns                = {}
    kube-proxy             = {}
    vpc-cni                = { before_compute = true, configuration_values = jsonencode({ enableNetworkPolicy = "true" }) }
    eks-pod-identity-agent = {}
    aws-ebs-csi-driver     = { service_account_role_arn = module.ebs_csi_irsa.iam_role_arn }
  }

  eks_managed_node_groups = {
    spot = {
      capacity_type  = "SPOT"
      instance_types = var.node_instance_types
      min_size       = 1
      max_size       = 4
      desired_size   = var.node_desired
      disk_size      = 40
      labels         = { workload = "evidence-gate" }
    }
  }
}

module "ebs_csi_irsa" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.44"

  role_name             = "${var.name}-ebs-csi"
  attach_ebs_csi_policy = true
  oidc_providers = {
    main = {
      provider_arn               = module.eks.oidc_provider_arn
      namespace_service_accounts = ["kube-system:ebs-csi-controller-sa"]
    }
  }
}

# --------------------------------------------------------------------------- registry

resource "aws_ecr_repository" "app" {
  name                 = "evidencegate"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true
  image_scanning_configuration {
    scan_on_push = true
  }
  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.main.arn
  }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 10 most recent images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 10 }
      action       = { type = "expire" }
    }]
  })
}

# --------------------------------------------------------------------------- encryption

resource "aws_kms_key" "main" {
  description             = "${var.name}: audit records, models, queue messages and secrets"
  enable_key_rotation     = true
  deletion_window_in_days = 7
}

resource "aws_kms_alias" "main" {
  name          = "alias/${var.name}"
  target_key_id = aws_kms_key.main.key_id
}
