output "cluster_name" {
  value = module.eks.cluster_name
}

output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}

output "audit_bucket" {
  value = aws_s3_bucket.audit.bucket
}

output "models_bucket" {
  value = aws_s3_bucket.models.bucket
}

output "triage_queue_url" {
  value = aws_sqs_queue.triage.url
}

output "cognito_token_url" {
  value = "https://${aws_cognito_user_pool_domain.main.domain}.auth.${var.region}.amazoncognito.com/oauth2/token"
}

output "cognito_demo_client_id" {
  value = aws_cognito_user_pool_client.demo.id
}

output "cognito_demo_client_secret" {
  value     = aws_cognito_user_pool_client.demo.client_secret
  sensitive = true
}

# Account-specific Helm values, layered on top of values-aws.yaml.
resource "local_file" "helm_values" {
  filename = "${path.module}/../../helm/evidence-gate/values-aws.generated.yaml"
  content = yamlencode({
    image = { repository = aws_ecr_repository.app.repository_url }
    config = {
      awsRegion    = var.region
      auditBucket  = aws_s3_bucket.audit.bucket
      modelsBucket = aws_s3_bucket.models.bucket
      jwksUrl      = "https://cognito-idp.${var.region}.amazonaws.com/${aws_cognito_user_pool.main.id}/.well-known/jwks.json"
      jwtIssuer    = "https://cognito-idp.${var.region}.amazonaws.com/${aws_cognito_user_pool.main.id}"
    }
    gateway      = { serviceAccountAnnotations = { "eks.amazonaws.com/role-arn" = aws_iam_role.service["gateway"].arn } }
    checker      = { serviceAccountAnnotations = { "eks.amazonaws.com/role-arn" = aws_iam_role.service["checker"].arn } }
    retriever    = { serviceAccountAnnotations = { "eks.amazonaws.com/role-arn" = aws_iam_role.service["retriever"].arn } }
    triageWorker = { serviceAccountAnnotations = { "eks.amazonaws.com/role-arn" = aws_iam_role.service["triage"].arn } }
  })
}

output "region" {
  value = var.region
}
