variable "region" {
  type    = string
  default = "us-east-1"
}

variable "name" {
  type    = string
  default = "evidence-gate"
}

variable "kubernetes_version" {
  type    = string
  default = "1.31"
}

variable "node_instance_types" {
  description = "Spot instance types for the node group (any of them may be chosen)."
  type        = list(string)
  default     = ["c6i.xlarge", "c6a.xlarge", "c5.xlarge", "m6i.xlarge"]
}

variable "node_desired" {
  type    = number
  default = 2
}

variable "use_nat_gateway" {
  description = "A NAT gateway costs about $33/month; without it nodes run in public subnets behind security groups."
  type        = bool
  default     = false
}

variable "audit_retention_days" {
  description = "Object Lock (governance mode) retention for audit records."
  type        = number
  default     = 1
}

variable "budget_email" {
  description = "Where AWS Budgets sends the monthly cost alert."
  type        = string
}

variable "monthly_budget_usd" {
  type    = number
  default = 25
}

variable "bedrock_model_ids" {
  description = "Bedrock models the triage worker may call (empty disables Bedrock access)."
  type        = list(string)
  default     = []
}

variable "install_addons" {
  description = "Install the cluster add-ons with Helm (ALB controller, External Secrets, Argo Rollouts, Prometheus)."
  type        = bool
  default     = true
}
