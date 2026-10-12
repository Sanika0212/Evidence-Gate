# --------------------------------------------------------------------------- S3

resource "aws_s3_bucket" "audit" {
  bucket              = "${var.name}-audit-${local.account_id}"
  object_lock_enabled = true
  force_destroy       = true # governance-mode retention can be bypassed for teardown
}

resource "aws_s3_bucket_object_lock_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id
  rule {
    default_retention {
      mode = "GOVERNANCE"
      days = var.audit_retention_days
    }
  }
}

resource "aws_s3_bucket" "models" {
  bucket        = "${var.name}-models-${local.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_versioning" "buckets" {
  for_each = { audit = aws_s3_bucket.audit.id, models = aws_s3_bucket.models.id }
  bucket   = each.value
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "buckets" {
  for_each = { audit = aws_s3_bucket.audit.id, models = aws_s3_bucket.models.id }
  bucket   = each.value
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.main.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "buckets" {
  for_each                = { audit = aws_s3_bucket.audit.id, models = aws_s3_bucket.models.id }
  bucket                  = each.value
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Deny any request that is not over TLS (transmission security).
resource "aws_s3_bucket_policy" "tls_only" {
  for_each = { audit = aws_s3_bucket.audit, models = aws_s3_bucket.models }
  bucket   = each.value.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [each.value.arn, "${each.value.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
  depends_on = [aws_s3_bucket_public_access_block.buckets]
}

# --------------------------------------------------------------------------- SQS

resource "aws_sqs_queue" "triage_dlq" {
  name                      = "evidencegate-triage-dlq"
  message_retention_seconds = 1209600
  kms_master_key_id         = aws_kms_key.main.id
}

resource "aws_sqs_queue" "triage" {
  name                       = "evidencegate-triage"
  visibility_timeout_seconds = 120
  receive_wait_time_seconds  = 10
  kms_master_key_id          = aws_kms_key.main.id
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.triage_dlq.arn
    maxReceiveCount     = 5
  })
}

# --------------------------------------------------------------------------- Cognito (OAuth2 client credentials)

resource "aws_cognito_user_pool" "main" {
  name = var.name
}

resource "random_id" "domain" {
  byte_length = 4
}

resource "aws_cognito_user_pool_domain" "main" {
  domain       = "${var.name}-${random_id.domain.hex}"
  user_pool_id = aws_cognito_user_pool.main.id
}

resource "aws_cognito_resource_server" "api" {
  identifier   = "evidencegate"
  name         = "Evidence Gate API"
  user_pool_id = aws_cognito_user_pool.main.id
  scope {
    scope_name        = "check"
    scope_description = "Check answers"
  }
  scope {
    scope_name        = "label"
    scope_description = "Submit reviewer labels"
  }
}

resource "aws_cognito_user_pool_client" "demo" {
  name                                 = "demo-tenant"
  user_pool_id                         = aws_cognito_user_pool.main.id
  generate_secret                      = true
  allowed_oauth_flows                  = ["client_credentials"]
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_scopes                 = [for s in ["check", "label"] : "${aws_cognito_resource_server.api.identifier}/${s}"]
  access_token_validity                = 1
  token_validity_units {
    access_token = "hours"
  }
}

# --------------------------------------------------------------------------- Secrets Manager

resource "random_password" "db" {
  length  = 32
  special = false
}

resource "random_password" "review" {
  length  = 32
  special = false
}

resource "aws_secretsmanager_secret" "app" {
  name                    = "evidencegate/app"
  kms_key_id              = aws_kms_key.main.arn
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "app" {
  secret_id = aws_secretsmanager_secret.app.id
  secret_string = jsonencode({
    databaseUrl = "postgresql://evidencegate:${random_password.db.result}@evidence-gate-postgres:5432/evidencegate"
    reviewToken = random_password.review.result
  })
}

# --------------------------------------------------------------------------- cost guard

resource "aws_budgets_budget" "monthly" {
  name         = "${var.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"
  cost_filter {
    name   = "TagKeyValue"
    values = ["user:project$evidence-gate"]
  }
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_email]
  }
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_email]
  }
}
