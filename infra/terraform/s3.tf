# =============================================================================
# Archive bucket.
#
# One private bucket with clear prefixes:
#   raw/          immutable capture events and compliance records
#   normalized/   conversation snapshots
#   attachments/  quarantine -> clean -> curated
#   exports/      curated JSONL bundles
#   backups/      PostgreSQL dumps and their manifests
#
# Controls: block all public access, bucket-owner-enforced ownership,
# versioning, default encryption, TLS-only policy, lifecycle tiers, and CORS
# limited to the exact extension origins with PUT only.
# =============================================================================

locals {
  bucket_name = coalesce(
    var.s3_bucket_name != "" ? var.s3_bucket_name : null,
    "${data.aws_caller_identity.current.account_id}-${var.aws_region}-${var.project_name}"
  )
}

resource "aws_s3_bucket" "archive" {
  bucket = local.bucket_name

  tags = {
    Name        = local.bucket_name
    Description = "TechSara ChatGPT session archive: raw events, snapshots, attachments, exports, backups"
  }
}

resource "aws_s3_bucket_public_access_block" "archive" {
  bucket = aws_s3_bucket.archive.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "archive" {
  bucket = aws_s3_bucket.archive.id

  rule {
    # ACLs are disabled entirely; access is granted only by policy and IAM.
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_versioning" "archive" {
  bucket = aws_s3_bucket.archive.id

  versioning_configuration {
    # Versioning is what makes an accidental overwrite or delete recoverable.
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "archive" {
  bucket = aws_s3_bucket.archive.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = var.s3_use_kms ? "aws:kms" : "AES256"
      kms_master_key_id = var.s3_use_kms ? var.s3_kms_key_arn : null
    }
    # Cuts KMS request cost dramatically for many small objects.
    bucket_key_enabled = var.s3_use_kms
  }
}

resource "aws_s3_bucket_policy" "archive" {
  bucket = aws_s3_bucket.archive.id
  policy = data.aws_iam_policy_document.bucket_policy.json

  depends_on = [aws_s3_bucket_public_access_block.archive]
}

data "aws_iam_policy_document" "bucket_policy" {
  statement {
    sid    = "DenyNonTlsRequests"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.archive.arn,
      "${aws_s3_bucket.archive.arn}/*",
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  statement {
    sid    = "DenyUnencryptedObjectUploads"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.archive.arn}/*"]

    condition {
      test     = "StringNotEquals"
      variable = "s3:x-amz-server-side-encryption"
      values   = var.s3_use_kms ? ["aws:kms"] : ["AES256"]
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "archive" {
  bucket = aws_s3_bucket.archive.id

  # Unverified uploads: short-lived by design.
  rule {
    id     = "expire-quarantine"
    status = "Enabled"

    filter {
      prefix = "attachments/quarantine/"
    }

    expiration {
      days = var.quarantine_retention_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 3
    }
  }

  # Raw capture events: cheap cold storage after the operational window.
  rule {
    id     = "tier-raw-events"
    status = "Enabled"

    filter {
      prefix = "raw/"
    }

    transition {
      days          = 90
      storage_class = "STANDARD_IA"
    }

    transition {
      days          = 180
      storage_class = "GLACIER_IR"
    }

    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }

  rule {
    id     = "tier-normalized-snapshots"
    status = "Enabled"

    filter {
      prefix = "normalized/"
    }

    transition {
      days          = 120
      storage_class = "STANDARD_IA"
    }

    noncurrent_version_expiration {
      noncurrent_days = 180
    }
  }

  rule {
    id     = "expire-backups"
    status = "Enabled"

    filter {
      prefix = "backups/"
    }

    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }

    expiration {
      days = var.backup_retention_days
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }

  rule {
    id     = "expire-exports"
    status = "Enabled"

    filter {
      prefix = "exports/"
    }

    expiration {
      days = var.export_retention_days
    }
  }

  rule {
    id     = "cleanup-multipart"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# CORS exists solely so the extension can PUT attachment bytes straight to S3
# with a presigned URL. No GET, no wildcard origin, minimal exposed headers.
resource "aws_s3_bucket_cors_configuration" "archive" {
  count  = length(var.extension_origins) > 0 ? 1 : 0
  bucket = aws_s3_bucket.archive.id

  cors_rule {
    allowed_methods = ["PUT"]
    allowed_origins = var.extension_origins
    allowed_headers = [
      "content-type",
      "content-length",
      "x-amz-server-side-encryption",
      "x-amz-checksum-sha256",
      "x-amz-content-sha256",
      "x-amz-date",
      "authorization",
    ]
    expose_headers  = ["ETag", "x-amz-version-id"]
    max_age_seconds = 3000
  }
}

# S3 access logging into a dedicated prefix of the same bucket would create a
# recursive write loop, so a separate log bucket is used.
resource "aws_s3_bucket" "access_logs" {
  bucket = "${local.bucket_name}-logs"

  tags = {
    Name        = "${local.bucket_name}-logs"
    Description = "S3 server access logs for the archive bucket"
  }
}

resource "aws_s3_bucket_public_access_block" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id

  rule {
    object_ownership = "BucketOwnerPreferred"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id

  rule {
    id     = "expire-access-logs"
    status = "Enabled"

    filter {}

    expiration {
      days = 90
    }
  }
}

resource "aws_s3_bucket_logging" "archive" {
  bucket = aws_s3_bucket.archive.id

  target_bucket = aws_s3_bucket.access_logs.id
  target_prefix = "s3-access/"

  depends_on = [aws_s3_bucket_ownership_controls.access_logs]
}
