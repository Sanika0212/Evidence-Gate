# One role per service account (IRSA), each with only the actions that service performs.

locals {
  kms_use = {
    Effect   = "Allow"
    Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
    Resource = [aws_kms_key.main.arn]
  }
  models_read = {
    Effect   = "Allow"
    Action   = ["s3:GetObject", "s3:ListBucket"]
    Resource = [aws_s3_bucket.models.arn, "${aws_s3_bucket.models.arn}/*"]
  }
  roles = {
    gateway = {
      service_account = "evidence-gate-gateway"
      statements = [
        { Effect = "Allow", Action = ["s3:PutObject"], Resource = ["${aws_s3_bucket.audit.arn}/decisions/*"] },
        { Effect = "Allow", Action = ["sqs:SendMessage", "sqs:GetQueueUrl"], Resource = [aws_sqs_queue.triage.arn] },
        local.kms_use,
      ]
    }
    checker = {
      service_account = "evidence-gate-checker"
      statements      = [local.models_read, local.kms_use]
    }
    retriever = {
      service_account = "evidence-gate-retriever"
      statements      = [local.models_read, local.kms_use]
    }
    triage = {
      service_account = "evidence-gate-triage"
      statements = concat(
        [
          { Effect = "Allow", Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueUrl", "sqs:ChangeMessageVisibility"], Resource = [aws_sqs_queue.triage.arn] },
          local.models_read,
          local.kms_use,
        ],
        length(var.bedrock_model_ids) == 0 ? [] : [{
          Effect   = "Allow"
          Action   = ["bedrock:InvokeModel", "bedrock:Converse"]
          Resource = [for m in var.bedrock_model_ids : "arn:aws:bedrock:${var.region}::foundation-model/${m}"]
        }]
      )
    }
  }
}

data "aws_iam_policy_document" "assume" {
  for_each = local.roles
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [module.eks.oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${module.eks.oidc_provider}:sub"
      values   = ["system:serviceaccount:${local.namespace}:${each.value.service_account}"]
    }
    condition {
      test     = "StringEquals"
      variable = "${module.eks.oidc_provider}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "service" {
  for_each           = local.roles
  name               = "${var.name}-${each.key}"
  assume_role_policy = data.aws_iam_policy_document.assume[each.key].json
}

resource "aws_iam_role_policy" "service" {
  for_each = local.roles
  name     = "least-privilege"
  role     = aws_iam_role.service[each.key].id
  policy   = jsonencode({ Version = "2012-10-17", Statement = each.value.statements })
}

# External Secrets reads exactly one secret.
module "external_secrets_irsa" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.44"

  role_name                             = "${var.name}-external-secrets"
  attach_external_secrets_policy        = true
  external_secrets_secrets_manager_arns = [aws_secretsmanager_secret.app.arn]
  external_secrets_kms_key_arns         = [aws_kms_key.main.arn]
  oidc_providers = {
    main = {
      provider_arn               = module.eks.oidc_provider_arn
      namespace_service_accounts = ["external-secrets:external-secrets"]
    }
  }
}

module "lb_controller_irsa" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.44"

  role_name                              = "${var.name}-lb-controller"
  attach_load_balancer_controller_policy = true
  oidc_providers = {
    main = {
      provider_arn               = module.eks.oidc_provider_arn
      namespace_service_accounts = ["kube-system:aws-load-balancer-controller"]
    }
  }
}
