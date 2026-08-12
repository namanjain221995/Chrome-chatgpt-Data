# =============================================================================
# CloudWatch: log group, alarm topic and the alarms that matter on a
# single-instance deployment.
#
# Deliberately small: an instance that fails its status check, runs hot, fills
# its disk or stops answering health checks. Alarm noise that nobody acts on is
# worse than no alarm.
# =============================================================================

resource "aws_cloudwatch_log_group" "instance" {
  count = var.enable_cloudwatch_logs ? 1 : 0

  name              = "/aws/ec2/${var.project_name}"
  retention_in_days = var.cloudwatch_log_retention_days

  tags = {
    Name = "${var.project_name}-instance-logs"
  }
}

resource "aws_sns_topic" "alarms" {
  name = "${var.project_name}-alarms"

  tags = {
    Name = "${var.project_name}-alarms"
  }
}

resource "aws_sns_topic_subscription" "alarm_email" {
  count = var.alarm_email != "" ? 1 : 0

  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

locals {
  alarm_actions = [aws_sns_topic.alarms.arn]
}

# --- Instance health -------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "status_check" {
  alarm_name        = "${var.project_name}-status-check-failed"
  alarm_description = "EC2 or system status check has failed; the archive is likely unreachable."

  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 2
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "breaching"

  dimensions = {
    InstanceId = aws_instance.archive.id
  }

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "cpu_high" {
  alarm_name        = "${var.project_name}-cpu-high"
  alarm_description = "Sustained high CPU. Check queue depth, then consider t3a.xlarge."

  namespace           = "AWS/EC2"
  metric_name         = "CPUUtilization"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 3
  threshold           = 80
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    InstanceId = aws_instance.archive.id
  }

  alarm_actions = local.alarm_actions
}

# Burstable instances stall hard when CPU credits run out; this is the early
# warning that a t3a class is no longer the right size.
resource "aws_cloudwatch_metric_alarm" "cpu_credit_low" {
  count = startswith(var.instance_type, "t3") || startswith(var.instance_type, "t4g") ? 1 : 0

  alarm_name        = "${var.project_name}-cpu-credits-low"
  alarm_description = "CPU credit balance is low; the instance will throttle. Consider a larger size."

  namespace           = "AWS/EC2"
  metric_name         = "CPUCreditBalance"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 2
  threshold           = 30
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    InstanceId = aws_instance.archive.id
  }

  alarm_actions = local.alarm_actions
}

# --- Disk (requires the CloudWatch agent on the instance) ------------------

resource "aws_cloudwatch_metric_alarm" "disk_space" {
  count = var.enable_cloudwatch_logs ? 1 : 0

  alarm_name = "${var.project_name}-data-disk-space-low"
  alarm_description = join(" ", [
    "Data volume above 85% used.",
    "PostgreSQL stops accepting writes when the volume fills.",
    "Requires the CloudWatch agent; see docs/MONITORING.md.",
  ])

  namespace           = "CWAgent"
  metric_name         = "disk_used_percent"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 2
  threshold           = 85
  comparison_operator = "GreaterThanThreshold"
  # Missing data means the agent is not reporting, which is itself worth knowing.
  treat_missing_data = "missing"

  dimensions = {
    InstanceId = aws_instance.archive.id
    path       = local.data_root
    fstype     = "xfs"
  }

  alarm_actions = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "memory_high" {
  count = var.enable_cloudwatch_logs ? 1 : 0

  alarm_name        = "${var.project_name}-memory-high"
  alarm_description = "Memory above 90%. Requires the CloudWatch agent."

  namespace           = "CWAgent"
  metric_name         = "mem_used_percent"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 3
  threshold           = 90
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "missing"

  dimensions = {
    InstanceId = aws_instance.archive.id
  }

  alarm_actions = local.alarm_actions
}

# --- Backups ---------------------------------------------------------------

# A backup that silently stops is the failure mode that hurts most, so a missed
# nightly upload alarms on missing data rather than staying quiet.
resource "aws_cloudwatch_metric_alarm" "backup_missing" {
  alarm_name        = "${var.project_name}-backup-missing"
  alarm_description = "No successful PostgreSQL backup reported in 26 hours. Check the backup service."

  namespace           = "TechSara/ChatArchive"
  metric_name         = "BackupSucceeded"
  statistic           = "Sum"
  period              = 93600 # 26 hours
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"

  alarm_actions = local.alarm_actions
}

# --- Dashboard -------------------------------------------------------------

resource "aws_cloudwatch_dashboard" "archive" {
  dashboard_name = var.project_name

  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "metric"
        width  = 12
        height = 6
        properties = {
          title  = "Instance CPU"
          region = var.aws_region
          metrics = [
            ["AWS/EC2", "CPUUtilization", "InstanceId", aws_instance.archive.id],
          ]
          period = 300
          stat   = "Average"
        }
      },
      {
        type   = "metric"
        width  = 12
        height = 6
        properties = {
          title  = "EBS data volume throughput"
          region = var.aws_region
          metrics = [
            ["AWS/EBS", "VolumeReadBytes", "VolumeId", aws_ebs_volume.data.id],
            [".", "VolumeWriteBytes", ".", "."],
          ]
          period = 300
          stat   = "Sum"
        }
      },
      {
        type   = "metric"
        width  = 12
        height = 6
        properties = {
          title  = "Archive bucket size"
          region = var.aws_region
          metrics = [
            ["AWS/S3", "BucketSizeBytes", "BucketName", aws_s3_bucket.archive.id,
            "StorageType", "StandardStorage"],
          ]
          period = 86400
          stat   = "Average"
        }
      },
    ]
  })
}
