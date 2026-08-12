variable "aws_region" {
  description = "AWS region for every resource in this stack."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Short project slug used to name resources."
  type        = string
  default     = "techsara-chat-archive"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,32}$", var.project_name))
    error_message = "project_name must be lowercase alphanumeric with hyphens, 3-33 characters."
  }
}

variable "environment" {
  description = "Deployment environment name."
  type        = string
  default     = "production"

  validation {
    condition     = contains(["production", "staging", "development"], var.environment)
    error_message = "environment must be production, staging or development."
  }
}

# ---------------------------------------------------------------------------
# Networking
# ---------------------------------------------------------------------------

variable "vpc_id" {
  description = "Existing VPC id. Leave empty to use the account's default VPC."
  type        = string
  default     = ""
}

variable "subnet_id" {
  description = "Public subnet id for the instance. Leave empty to pick a default-VPC subnet."
  type        = string
  default     = ""
}

variable "admin_ingress_cidrs" {
  description = <<-EOT
    Optional CIDRs allowed to reach TCP 22.

    Leave empty (the default) so no SSH ingress rule is created at all:
    administration is expected to go through SSM Session Manager, which needs
    no inbound port.
  EOT
  type        = list(string)
  default     = []

  validation {
    condition     = !contains(var.admin_ingress_cidrs, "0.0.0.0/0")
    error_message = "Refusing to open SSH to the whole internet. Use SSM Session Manager."
  }
}

# ---------------------------------------------------------------------------
# Compute
# ---------------------------------------------------------------------------

variable "instance_type" {
  description = "EC2 instance type. t3a.large = 2 vCPU / 8 GiB, the documented baseline."
  type        = string
  default     = "t3a.large"
}

variable "instance_architecture" {
  description = "x86_64 for broadest container compatibility, or arm64 for t4g instances."
  type        = string
  default     = "x86_64"

  validation {
    condition     = contains(["x86_64", "arm64"], var.instance_architecture)
    error_message = "instance_architecture must be x86_64 or arm64."
  }
}

variable "root_volume_gb" {
  description = "Encrypted gp3 root volume size in GiB."
  type        = number
  default     = 30

  validation {
    condition     = var.root_volume_gb >= 20
    error_message = "root_volume_gb must be at least 20 GiB."
  }
}

variable "data_volume_gb" {
  description = "Encrypted gp3 data volume for PostgreSQL, backups and Caddy state."
  type        = number
  default     = 100

  validation {
    condition     = var.data_volume_gb >= 50
    error_message = "data_volume_gb must be at least 50 GiB."
  }
}

variable "data_volume_iops" {
  description = "Provisioned IOPS for the gp3 data volume."
  type        = number
  default     = 3000
}

variable "data_volume_throughput" {
  description = "Provisioned throughput (MiB/s) for the gp3 data volume."
  type        = number
  default     = 125
}

variable "ebs_kms_key_id" {
  description = "Optional customer-managed KMS key for EBS. Empty uses the AWS-managed key."
  type        = string
  default     = ""
}

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

variable "s3_bucket_name" {
  description = "Archive bucket name. Empty derives account- and region-scoped name."
  type        = string
  default     = ""
}

variable "s3_use_kms" {
  description = "Use SSE-KMS instead of SSE-S3. Costs more; enable when policy requires it."
  type        = bool
  default     = false
}

variable "s3_kms_key_arn" {
  description = "Existing KMS key ARN for the bucket when s3_use_kms is true."
  type        = string
  default     = ""
}

variable "extension_origins" {
  description = <<-EOT
    Exact chrome-extension:// origins allowed to PUT attachments directly to S3.
    Fill in after the first extension build, e.g.
    ["chrome-extension://abcdefghijklmnopabcdefghijklmnop"].
  EOT
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([
      for origin in var.extension_origins : can(regex("^chrome-extension://[a-p]{32}$", origin))
    ])
    error_message = "Each origin must look like chrome-extension:// followed by a 32-character id."
  }
}

variable "raw_retention_days" {
  description = "Days before raw capture objects transition to cheaper storage."
  type        = number
  default     = 365
}

variable "backup_retention_days" {
  description = "Days to keep PostgreSQL backup objects before expiry."
  type        = number
  default     = 90
}

variable "quarantine_retention_days" {
  description = "Days to keep unverified attachment uploads in the quarantine prefix."
  type        = number
  default     = 30
}

variable "export_retention_days" {
  description = "Days to keep generated export bundles."
  type        = number
  default     = 180
}

# ---------------------------------------------------------------------------
# DNS and monitoring
# ---------------------------------------------------------------------------

variable "domain_name" {
  description = "Public hostname for the archive, e.g. archive.example.com."
  type        = string
  default     = ""
}

variable "route53_zone_id" {
  description = "Existing hosted zone id. A record is created only when both this and domain_name are set."
  type        = string
  default     = ""
}

variable "enable_cloudwatch_logs" {
  description = "Allow the instance to ship logs to CloudWatch Logs."
  type        = bool
  default     = true
}

variable "cloudwatch_log_retention_days" {
  description = "Retention for the instance log group."
  type        = number
  default     = 30
}

variable "alarm_email" {
  description = "Optional address subscribed to the alarm topic. Confirm the subscription by email."
  type        = string
  default     = ""
}

variable "enable_detailed_monitoring" {
  description = "Enable 1-minute EC2 metrics (small additional cost)."
  type        = bool
  default     = false
}

# ---------------------------------------------------------------------------
# Application configuration surfaced through SSM
# ---------------------------------------------------------------------------

variable "allowed_email_domains" {
  description = "Company email domains permitted to sign in."
  type        = list(string)
  default     = []
}

variable "managed_workspace_label" {
  description = "Exact ChatGPT workspace label to treat as the managed company workspace."
  type        = string
  default     = ""
}

variable "container_image_repository" {
  description = "Container image repository, e.g. ghcr.io/org/techsara-chat-archive-backend."
  type        = string
  default     = ""
}

variable "container_image_tag" {
  description = "Immutable image tag to deploy (a git SHA)."
  type        = string
  default     = "latest"
}
