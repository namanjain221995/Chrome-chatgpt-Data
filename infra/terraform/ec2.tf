# =============================================================================
# The single EC2 instance, its security group, its encrypted volumes and its
# Elastic IP.
#
# Security group intent:
#   443/tcp  from the internet  - the only service anyone talks to
#    80/tcp  from the internet  - ACME challenge and the redirect to HTTPS
#    22/tcp  only if admin_ingress_cidrs is non-empty (default: no rule at all)
#   5432, 5050, 9000 - never open; PostgreSQL, pgAdmin and MinIO stay on the
#                      private Docker network
# =============================================================================

data "aws_vpc" "selected" {
  id      = var.vpc_id != "" ? var.vpc_id : null
  default = var.vpc_id == "" ? true : null
}

data "aws_subnets" "selected" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.selected.id]
  }
}

locals {
  subnet_id = var.subnet_id != "" ? var.subnet_id : tolist(data.aws_subnets.selected.ids)[0]
}

data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name = "name"
    values = [
      "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-${var.instance_architecture == "arm64" ? "arm64" : "amd64"}-server-*",
    ]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }

  filter {
    name   = "state"
    values = ["available"]
  }
}

resource "aws_security_group" "instance" {
  name        = "${var.project_name}-instance"
  description = "Public HTTPS for the archive API; everything else stays private"
  vpc_id      = data.aws_vpc.selected.id

  tags = {
    Name = "${var.project_name}-instance"
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "https" {
  security_group_id = aws_security_group.instance.id
  description       = "HTTPS from the internet (Caddy)"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "https_v6" {
  security_group_id = aws_security_group.instance.id
  description       = "HTTPS from the internet over IPv6"
  cidr_ipv6         = "::/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "http_redirect" {
  security_group_id = aws_security_group.instance.id
  description       = "HTTP for the ACME challenge and the HTTPS redirect only"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
}

# Created only when an operator explicitly supplies admin CIDRs. The documented
# path is SSM Session Manager, which needs no inbound rule.
resource "aws_vpc_security_group_ingress_rule" "ssh" {
  for_each = toset(var.admin_ingress_cidrs)

  security_group_id = aws_security_group.instance.id
  description       = "Optional SSH from an administrator network"
  cidr_ipv4         = each.value
  from_port         = 22
  to_port           = 22
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.instance.id
  description       = "Outbound to S3, SSM, the container registry and ACME"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_instance" "archive" {
  ami           = data.aws_ami.ubuntu.id
  instance_type = var.instance_type
  subnet_id     = local.subnet_id

  iam_instance_profile   = aws_iam_instance_profile.instance.name
  vpc_security_group_ids = [aws_security_group.instance.id]
  monitoring             = var.enable_detailed_monitoring

  # IMDSv2 only: blocks the SSRF-to-credential-theft path.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "enabled"
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.root_volume_gb
    encrypted             = true
    kms_key_id            = var.ebs_kms_key_id != "" ? var.ebs_kms_key_id : null
    delete_on_termination = true

    tags = {
      Name = "${var.project_name}-root"
    }
  }

  user_data                   = local.user_data
  user_data_replace_on_change = false

  tags = {
    Name    = var.project_name
    Backup  = "daily"
    Restore = "documented in docs/DISASTER_RECOVERY.md"
  }

  lifecycle {
    # Replacing the instance would orphan the data volume; AMI refreshes are
    # handled by unattended-upgrades and a documented rebuild procedure.
    ignore_changes = [ami, user_data]
  }
}

# Separate volume so the instance can be rebuilt without touching the data.
resource "aws_ebs_volume" "data" {
  availability_zone = aws_instance.archive.availability_zone
  size              = var.data_volume_gb
  type              = "gp3"
  iops              = var.data_volume_iops
  throughput        = var.data_volume_throughput
  encrypted         = true
  kms_key_id        = var.ebs_kms_key_id != "" ? var.ebs_kms_key_id : null

  tags = {
    Name    = "${var.project_name}-data"
    Backup  = "daily"
    Content = "postgresql,backups,caddy-state"
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_volume_attachment" "data" {
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.data.id
  instance_id = aws_instance.archive.id

  # Never force-detach a live database volume.
  force_detach = false
  skip_destroy = true
}

resource "aws_eip" "archive" {
  domain   = "vpc"
  instance = aws_instance.archive.id

  tags = {
    Name = "${var.project_name}-eip"
  }
}

# Optional DNS, only when an existing zone is supplied.
resource "aws_route53_record" "archive" {
  count = var.route53_zone_id != "" && var.domain_name != "" ? 1 : 0

  zone_id = var.route53_zone_id
  name    = var.domain_name
  type    = "A"
  ttl     = 300
  records = [aws_eip.archive.public_ip]
}
