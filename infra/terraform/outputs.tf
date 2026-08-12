output "instance_id" {
  description = "EC2 instance id. Use with: aws ssm start-session --target <id>"
  value       = aws_instance.archive.id
}

output "public_ip" {
  description = "Elastic IP. Point the archive DNS record at this address."
  value       = aws_eip.archive.public_ip
}

output "archive_bucket" {
  description = "Archive S3 bucket name (set as S3_BUCKET on the instance)."
  value       = aws_s3_bucket.archive.id
}

output "access_log_bucket" {
  description = "Bucket receiving S3 server access logs."
  value       = aws_s3_bucket.access_logs.id
}

output "instance_role_arn" {
  description = "IAM role the instance assumes."
  value       = aws_iam_role.instance.arn
}

output "data_volume_id" {
  description = "Encrypted EBS data volume. Snapshot this with AWS Backup or DLM."
  value       = aws_ebs_volume.data.id
}

output "security_group_id" {
  description = "Instance security group."
  value       = aws_security_group.instance.id
}

output "alarm_topic_arn" {
  description = "SNS topic that receives CloudWatch alarms."
  value       = aws_sns_topic.alarms.arn
}

output "ssm_parameter_prefix" {
  description = "Prefix for this deployment's SSM parameters."
  value       = local.parameter_prefix
}

output "deploy_document_name" {
  description = "SSM document that performs a deployment."
  value       = aws_ssm_document.deploy.name
}

output "session_manager_command" {
  description = "Open an administrative shell without any inbound SSH."
  value       = "aws ssm start-session --target ${aws_instance.archive.id} --region ${var.aws_region}"
}

output "pgadmin_port_forward_command" {
  description = "Reach the private pgAdmin from a workstation with no public exposure."
  value = join(" ", [
    "aws ssm start-session --target ${aws_instance.archive.id}",
    "--document-name AWS-StartPortForwardingSession",
    "--parameters '{\"portNumber\":[\"5050\"],\"localPortNumber\":[\"5050\"]}'",
    "--region ${var.aws_region}",
  ])
}

output "dns_record" {
  description = "Route 53 record created, when a hosted zone was supplied."
  value       = length(aws_route53_record.archive) > 0 ? aws_route53_record.archive[0].fqdn : "not managed by terraform"
}

output "next_steps" {
  description = "What an operator must do after apply."
  value = join("\n", [
    "1. Write secrets:      scripts/put_secrets.sh --project ${var.project_name} --region ${var.aws_region}",
    "2. Point DNS at:       ${aws_eip.archive.public_ip}",
    "3. Copy the bundle:    scripts/deploy_bundle.sh",
    "4. Deploy:             aws ssm start-session --target ${aws_instance.archive.id}",
    "5. Verify health:      curl https://${var.domain_name != "" ? var.domain_name : "<domain>"}/health/ready",
  ])
}
