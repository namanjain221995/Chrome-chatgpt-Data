# =============================================================================
# Instance role: least privilege, scoped to this bucket's prefixes.
#
# The instance can write archive data and backups, read what it wrote, and read
# its own SSM parameters. It cannot list other buckets, cannot delete archive
# objects (deletion is a deliberate, audited operation performed by an
# administrator), and holds no long-lived access key.
# =============================================================================

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

resource "aws_iam_role" "instance" {
  name        = "${var.project_name}-instance"
  description = "EC2 role for the TechSara ChatGPT archive instance"

  assume_role_policy = data.aws_iam_policy_document.instance_assume.json

  tags = {
    Name = "${var.project_name}-instance"
  }
}

data "aws_iam_policy_document" "instance_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_instance_profile" "instance" {
  name = "${var.project_name}-instance"
  role = aws_iam_role.instance.name
}

# --- S3: prefix-scoped read/write, no delete -------------------------------

data "aws_iam_policy_document" "s3_access" {
  statement {
    sid    = "ListOnlyArchivePrefixes"
    effect = "Allow"

    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [aws_s3_bucket.archive.arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values = [
        "raw/*",
        "normalized/*",
        "attachments/*",
        "exports/*",
        "backups/*",
        "",
      ]
    }
  }

  statement {
    sid    = "WriteArchiveObjects"
    effect = "Allow"

    actions = [
      "s3:PutObject",
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:GetObjectAttributes",
      "s3:AbortMultipartUpload",
      "s3:ListMultipartUploadParts",
    ]

    resources = [
      "${aws_s3_bucket.archive.arn}/raw/*",
      "${aws_s3_bucket.archive.arn}/normalized/*",
      "${aws_s3_bucket.archive.arn}/attachments/*",
      "${aws_s3_bucket.archive.arn}/exports/*",
      "${aws_s3_bucket.archive.arn}/backups/*",
    ]
  }

  # Promoting a verified attachment from quarantine to clean is a server-side
  # copy, which requires read on the source and write on the destination.
  statement {
    sid       = "CopyAttachmentsBetweenStages"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.archive.arn}/attachments/*"]
  }

  # Deliberately absent: s3:DeleteObject. Retention deletes are performed by an
  # administrator or by the bucket lifecycle rules, never by the application.
}

resource "aws_iam_policy" "s3_access" {
  name        = "${var.project_name}-s3-access"
  description = "Prefix-scoped access to the archive bucket (no delete)"
  policy      = data.aws_iam_policy_document.s3_access.json
}

resource "aws_iam_role_policy_attachment" "s3_access" {
  role       = aws_iam_role.instance.name
  policy_arn = aws_iam_policy.s3_access.arn
}

# --- KMS (only when the bucket uses a customer-managed key) ----------------

data "aws_iam_policy_document" "kms_access" {
  count = var.s3_use_kms && var.s3_kms_key_arn != "" ? 1 : 0

  statement {
    effect = "Allow"
    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:ReEncrypt*",
      "kms:GenerateDataKey*",
      "kms:DescribeKey",
    ]
    resources = [var.s3_kms_key_arn]
  }
}

resource "aws_iam_policy" "kms_access" {
  count       = var.s3_use_kms && var.s3_kms_key_arn != "" ? 1 : 0
  name        = "${var.project_name}-kms-access"
  description = "Use of the archive bucket KMS key"
  policy      = data.aws_iam_policy_document.kms_access[0].json
}

resource "aws_iam_role_policy_attachment" "kms_access" {
  count      = var.s3_use_kms && var.s3_kms_key_arn != "" ? 1 : 0
  role       = aws_iam_role.instance.name
  policy_arn = aws_iam_policy.kms_access[0].arn
}

# --- SSM: Session Manager access and this project's parameters -------------

resource "aws_iam_role_policy_attachment" "ssm_managed_instance" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "ssm_parameters" {
  statement {
    sid    = "ReadProjectParameters"
    effect = "Allow"

    actions = [
      "ssm:GetParameter",
      "ssm:GetParameters",
      "ssm:GetParametersByPath",
    ]

    resources = [
      "arn:${data.aws_partition.current.partition}:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.project_name}/*",
    ]
  }

  # SecureString parameters are decrypted with the account's default SSM key.
  statement {
    sid       = "DecryptSecureStrings"
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = ["arn:${data.aws_partition.current.partition}:kms:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
  }
}

resource "aws_iam_policy" "ssm_parameters" {
  name        = "${var.project_name}-ssm-parameters"
  description = "Read this project's SSM parameters"
  policy      = data.aws_iam_policy_document.ssm_parameters.json
}

resource "aws_iam_role_policy_attachment" "ssm_parameters" {
  role       = aws_iam_role.instance.name
  policy_arn = aws_iam_policy.ssm_parameters.arn
}

# --- CloudWatch ------------------------------------------------------------

data "aws_iam_policy_document" "cloudwatch" {
  count = var.enable_cloudwatch_logs ? 1 : 0

  statement {
    sid    = "WriteInstanceLogs"
    effect = "Allow"

    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]

    resources = ["${aws_cloudwatch_log_group.instance[0].arn}:*"]
  }

  statement {
    sid       = "PublishCustomMetrics"
    effect    = "Allow"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["TechSara/ChatArchive", "CWAgent"]
    }
  }
}

resource "aws_iam_policy" "cloudwatch" {
  count       = var.enable_cloudwatch_logs ? 1 : 0
  name        = "${var.project_name}-cloudwatch"
  description = "Ship instance logs and disk metrics to CloudWatch"
  policy      = data.aws_iam_policy_document.cloudwatch[0].json
}

resource "aws_iam_role_policy_attachment" "cloudwatch" {
  count      = var.enable_cloudwatch_logs ? 1 : 0
  role       = aws_iam_role.instance.name
  policy_arn = aws_iam_policy.cloudwatch[0].arn
}
