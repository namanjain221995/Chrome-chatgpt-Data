# AWS console checklist

For administrators who prefer the console to Terraform. Follow in order; each
step names what you should see when it worked.

Terraform (`infra/terraform/`) does all of this in one `apply` and is the
recommended path. Use this checklist when console access is the only option, or
to verify what Terraform built.

---

## 1. S3 bucket

**S3 → Create bucket**

- [ ] Name: `<account-id>-<region>-techsara-chat-archive`
- [ ] Region: the one region you will use for everything
- [ ] **Block all public access: ON** (all four checkboxes)
- [ ] Bucket Versioning: **Enable**
- [ ] Default encryption: **SSE-S3** (or SSE-KMS if policy requires)
- [ ] Object Ownership: **ACLs disabled (Bucket owner enforced)**

**Properties → Lifecycle rules**

- [ ] `expire-quarantine` — prefix `attachments/quarantine/`, expire after 30 days
- [ ] `tier-raw-events` — prefix `raw/`, IA at 90 days, Glacier IR at 180
- [ ] `expire-backups` — prefix `backups/`, IA at 30 days, expire at 90
- [ ] `expire-exports` — prefix `exports/`, expire at 180 days
- [ ] Abort incomplete multipart uploads after 7 days

**Permissions → Bucket policy** — deny non-TLS:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "DenyNonTlsRequests",
    "Effect": "Deny",
    "Principal": "*",
    "Action": "s3:*",
    "Resource": ["arn:aws:s3:::BUCKET", "arn:aws:s3:::BUCKET/*"],
    "Condition": { "Bool": { "aws:SecureTransport": "false" } }
  }]
}
```

**Permissions → CORS** (after the extension is built):

```json
[{
  "AllowedMethods": ["PUT"],
  "AllowedOrigins": ["chrome-extension://YOUR_EXTENSION_ID"],
  "AllowedHeaders": ["content-type","content-length","x-amz-server-side-encryption",
                     "x-amz-checksum-sha256","x-amz-content-sha256","x-amz-date","authorization"],
  "ExposeHeaders": ["ETag","x-amz-version-id"],
  "MaxAgeSeconds": 3000
}]
```

✅ **Check:** the bucket shows "Bucket and objects not public".

---

## 2. IAM role

**IAM → Roles → Create role → AWS service → EC2**

- [ ] Attach the AWS-managed policy **AmazonSSMManagedInstanceCore**
- [ ] Create an inline policy for S3, scoped to the archive prefixes, with
      `s3:PutObject`, `s3:GetObject`, `s3:GetObjectVersion`,
      `s3:AbortMultipartUpload`, `s3:ListMultipartUploadParts` on
      `arn:aws:s3:::BUCKET/{raw,normalized,attachments,exports,backups}/*`,
      plus `s3:ListBucket` on the bucket
- [ ] **Do not** grant `s3:DeleteObject`. Deletion is a deliberate,
      audited action, and lifecycle rules handle expiry.
- [ ] Create an inline policy for SSM: `ssm:GetParameter`,
      `ssm:GetParameters`, `ssm:GetParametersByPath` on
      `arn:aws:ssm:REGION:ACCOUNT:parameter/techsara-chat-archive/*`, plus
      `kms:Decrypt` on `alias/aws/ssm`
- [ ] Optional CloudWatch policy: `logs:CreateLogStream`, `logs:PutLogEvents`,
      `logs:DescribeLogStreams`, and `cloudwatch:PutMetricData` restricted to
      the `TechSara/ChatArchive` and `CWAgent` namespaces
- [ ] Name it `techsara-chat-archive-instance`

✅ **Check:** the role's trust policy names `ec2.amazonaws.com`.

---

## 3. Security group

**VPC → Security groups → Create**

| Direction | Port | Source | Purpose |
| --- | --- | --- | --- |
| Inbound | 443 | `0.0.0.0/0` and `::/0` | HTTPS |
| Inbound | 80 | `0.0.0.0/0` | ACME challenge and redirect only |
| Outbound | all | `0.0.0.0/0` | S3, SSM, registry, ACME |

- [ ] **No inbound 22.** Administration is through SSM Session Manager.
- [ ] **No inbound 5432, 5050 or 9000.** Those stay on the Docker network.

✅ **Check:** the inbound list has exactly two rules.

---

## 4. EC2 instance

**EC2 → Launch instance**

- [ ] AMI: Ubuntu Server 24.04 LTS
- [ ] Type: `t3a.large` (2 vCPU, 8 GiB)
- [ ] Key pair: **Proceed without a key pair** — SSM does not need one
- [ ] Network: your VPC, a public subnet, auto-assign public IP enabled
- [ ] Security group: the one from step 3
- [ ] Storage: 30 GiB gp3, **Encrypted**
- [ ] Advanced → IAM instance profile: the role from step 2
- [ ] Advanced → Metadata version: **V2 only (token required)**, hop limit 1
- [ ] Advanced → User data: paste the cloud-init from
      `infra/terraform/user_data.tf`

✅ **Check:** after ~3 minutes the instance appears under
**Systems Manager → Fleet Manager**.

---

## 5. Data volume

**EC2 → Volumes → Create volume**

- [ ] 100 GiB, gp3, 3000 IOPS, 125 MiB/s
- [ ] Same availability zone as the instance
- [ ] **Encrypted**
- [ ] Tag `Backup=daily`
- [ ] Attach to the instance as `/dev/sdf`

✅ **Check:** on the instance, `lsblk` shows the volume, and
`sudo /usr/local/sbin/mount-data-volume.sh` mounts it at
`/srv/techsara-chat-archive`.

---

## 6. Elastic IP

**EC2 → Elastic IPs → Allocate → Associate** with the instance.

✅ **Check:** the address is stable across a stop/start.

---

## 7. SSM parameters

**Systems Manager → Parameter Store**

Non-secret, type **String**, under `/techsara-chat-archive/`:

- [ ] `environment` = `production`
- [ ] `aws_region`, `s3_bucket`, `public_base_url`, `caddy_domain`, `caddy_email`
- [ ] `allowed_email_domains`, `managed_workspace_label`
- [ ] `image_repository`, `image_tag`
- [ ] `postgres_db` = `techsara_chat_archive`, `postgres_user` = `techsara_app`
- [ ] `oidc_issuer`, `oidc_client_id`, `oidc_required_hd`
- [ ] `browser_content_capture_enabled` = `false` (until authorized)
- [ ] `openai_written_authorization_confirmed` = `false` (until authorized)

Secret, type **SecureString** — use `scripts/put_secrets.sh` rather than typing
these into a browser:

- [ ] `postgres_password`, `jwt_secret`, `config_signing_key`
- [ ] `oidc_client_secret`, `openai_compliance_api_key`, `pgadmin_password`

✅ **Check:**
`aws ssm get-parameters-by-path --path /techsara-chat-archive --query 'Parameters[].Name'`

---

## 8. CloudWatch

**CloudWatch → Alarms → Create alarm**

- [ ] `StatusCheckFailed` > 0 for 2 periods of 60 s
- [ ] `CPUUtilization` > 80% for 3 periods of 300 s
- [ ] `CPUCreditBalance` < 30 (t-class only)
- [ ] `CWAgent disk_used_percent` > 85% on `/srv/techsara-chat-archive`
- [ ] `TechSara/ChatArchive BackupSucceeded` < 1 in 26 hours, treating missing
      data as **breaching**

**SNS → Topics** — create `techsara-chat-archive-alarms`, subscribe your
address, and **confirm the subscription email**. An unconfirmed subscription
sends nothing.

**Log group** `/aws/ec2/techsara-chat-archive`, retention 30 days.

---

## 9. Route 53

**Route 53 → Hosted zone → Create record**

- [ ] Type A, name `archive`, value the Elastic IP, TTL 300

✅ **Check:** `dig +short archive.example.com` returns the Elastic IP. Caddy
cannot obtain a certificate until it does.

---

## 10. Final verification

- [ ] `aws ssm start-session --target <id>` opens a shell with no SSH
- [ ] `curl https://archive.example.com/health/ready` returns `{"status":"ok"}`
- [ ] `curl https://archive.example.com/api/v1/config` shows
      `capture_active: false`
- [ ] The bucket shows "not public"
- [ ] `nmap` from outside shows only 80 and 443 open
- [ ] The first nightly backup appears under `backups/postgres/`
- [ ] A test restore succeeds (`verify_backup.sh --full-restore`)

Then, and only then, follow step 8 of
[AWS_STEP_BY_STEP.md](../docs/AWS_STEP_BY_STEP.md) to enable capture.
