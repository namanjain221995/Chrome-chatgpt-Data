# =============================================================================
# SSM Parameter Store.
#
# Terraform creates only the NON-SECRET parameters. Secret values are written
# out of band by scripts/put_secrets.sh, because anything Terraform manages is
# stored in plaintext in state — the exact outcome we are avoiding.
#
# Secret parameter names this deployment expects (created by that script):
#   /<project>/postgres_password
#   /<project>/jwt_secret
#   /<project>/config_signing_key
#   /<project>/oidc_client_secret
#   /<project>/openai_compliance_api_key
#   /<project>/pgadmin_password
# =============================================================================

locals {
  parameter_prefix = "/${var.project_name}"

  # Non-secret runtime configuration the deploy script renders into .env.
  plain_parameters = {
    environment             = var.environment
    aws_region              = var.aws_region
    s3_bucket               = aws_s3_bucket.archive.id
    public_base_url         = var.domain_name != "" ? "https://${var.domain_name}" : ""
    caddy_domain            = var.domain_name
    allowed_email_domains   = join(",", var.allowed_email_domains)
    managed_workspace_label = var.managed_workspace_label
    image_repository        = var.container_image_repository
    image_tag               = var.container_image_tag
    data_root               = local.data_root
    s3_encryption_mode      = var.s3_use_kms ? "SSE-KMS" : "SSE-S3"
    s3_kms_key_id           = var.s3_use_kms ? var.s3_kms_key_arn : ""
    cloudwatch_log_group    = var.enable_cloudwatch_logs ? aws_cloudwatch_log_group.instance[0].name : ""
    alarm_topic_arn         = aws_sns_topic.alarms.arn
    backup_retention_days   = tostring(var.backup_retention_days)
    raw_retention_days      = tostring(var.raw_retention_days)
  }
}

resource "aws_ssm_parameter" "plain" {
  for_each = local.plain_parameters

  name  = "${local.parameter_prefix}/${each.key}"
  type  = "String"
  value = each.value != "" ? each.value : "unset"
  tier  = "Standard"

  description = "Non-secret configuration for ${var.project_name}"

  tags = {
    Name = "${local.parameter_prefix}/${each.key}"
  }
}

# Placeholders make the parameter paths discoverable and let the deploy script
# fail loudly on "REPLACE_ME" instead of silently starting with no secret. The
# real values are written by scripts/put_secrets.sh with --type SecureString;
# `ignore_changes` means Terraform never reads or overwrites them afterwards.
resource "aws_ssm_parameter" "secret_placeholders" {
  for_each = toset([
    "postgres_password",
    "jwt_secret",
    "config_signing_key",
    "oidc_client_secret",
    "openai_compliance_api_key",
    "pgadmin_password",
  ])

  name  = "${local.parameter_prefix}/${each.value}"
  type  = "SecureString"
  value = "REPLACE_ME_WITH_scripts_put_secrets_sh"
  tier  = "Standard"

  description = "Secret for ${var.project_name}; set with scripts/put_secrets.sh"

  lifecycle {
    ignore_changes = [value]
  }

  tags = {
    Name      = "${local.parameter_prefix}/${each.value}"
    Sensitive = "true"
  }
}

# A documented, reviewable way to run the deployment through Run Command rather
# than an interactive shell.
resource "aws_ssm_document" "deploy" {
  name            = "${var.project_name}-deploy"
  document_type   = "Command"
  document_format = "YAML"

  content = yamlencode({
    schemaVersion = "2.2"
    description   = "Pull images, back up, migrate and restart the TechSara archive stack"
    parameters = {
      imageTag = {
        type        = "String"
        description = "Immutable image tag (git SHA) to deploy"
        default     = var.container_image_tag
      }
    }
    mainSteps = [
      {
        action = "aws:runShellScript"
        name   = "deploy"
        inputs = {
          timeoutSeconds = "1800"
          runCommand = [
            "set -euo pipefail",
            "cd /opt/${var.project_name}",
            "export IMAGE_TAG='{{ imageTag }}'",
            "sudo -E ./scripts/deploy_ec2.sh",
          ]
        }
      },
    ]
  })

  tags = {
    Name = "${var.project_name}-deploy"
  }
}
