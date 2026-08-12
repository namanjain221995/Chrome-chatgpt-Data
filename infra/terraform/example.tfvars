# Copy to terraform.tfvars and fill in. Never commit a filled-in tfvars file:
# .gitignore excludes *.tfvars for exactly this reason.

aws_region   = "us-east-1"
project_name = "techsara-chat-archive"
environment  = "production"

# --- Compute ---------------------------------------------------------------
instance_type         = "t3a.large" # 2 vCPU / 8 GiB
instance_architecture = "x86_64"    # arm64 + t4g.large also supported
root_volume_gb        = 30
data_volume_gb        = 100

# --- Networking ------------------------------------------------------------
# Leave empty to use the default VPC and its first subnet.
vpc_id    = ""
subnet_id = ""
# Leave empty: administration goes through SSM Session Manager, not SSH.
admin_ingress_cidrs = []

# --- DNS -------------------------------------------------------------------
domain_name     = "archive.example.com"
route53_zone_id = "" # set only if the zone already exists in this account

# --- Storage ---------------------------------------------------------------
s3_bucket_name = "" # empty derives <account>-<region>-techsara-chat-archive
s3_use_kms     = false
# Fill in after the first extension build so direct-to-S3 uploads are allowed.
extension_origins = []

backup_retention_days     = 90
raw_retention_days        = 365
quarantine_retention_days = 30

# --- Monitoring ------------------------------------------------------------
enable_cloudwatch_logs = true
alarm_email            = "" # confirm the SNS subscription by email

# --- Application -----------------------------------------------------------
allowed_email_domains      = ["example.com"]
managed_workspace_label    = "TechSara's Workspace"
container_image_repository = "ghcr.io/example-org/techsara-chat-archive-backend"
container_image_tag        = "replace-with-git-sha"
