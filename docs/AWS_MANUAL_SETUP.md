# Manual AWS setup and EC2 deployment

This runbook creates no infrastructure automatically. An AWS administrator uses
the Console, and the CLI commands verify what was created. Replace angle-bracket
placeholders locally. Never paste credentials or private keys into commands.

Official references: [S3 security](https://docs.aws.amazon.com/AmazonS3/latest/userguide/security-best-practices.html),
[Session Manager](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager.html),
and [mounting EBS](https://docs.aws.amazon.com/ebs/latest/userguide/ebs-using-volumes.html).

## 1. Confirm AWS region `us-east-1`

Console: choose **US East (N. Virginia)** in the top navigation and keep it
selected. CLI checkpoint:

```bash
export AWS_REGION=us-east-1
export AWS_DEFAULT_REGION=us-east-1
aws sts get-caller-identity
aws configure get region
```

Expected: the intended account/role and `us-east-1`. Stop if either is wrong.

## 2. Secure the existing S3 bucket

Console → S3 → `techsara-chatgpt`:

1. Confirm Region is US East (N. Virginia); do not create another bucket.
2. Permissions → Block public access → enable all four settings.
3. Permissions → Object Ownership → Bucket owner enforced.
4. Properties → Bucket Versioning → Enable.
5. Properties → Default encryption → SSE-S3; choose SSE-KMS only with an
   approved key and add its permissions to the instance role.
6. Add a bucket policy statement that denies non-TLS requests:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "DenyInsecureTransport",
    "Effect": "Deny",
    "Principal": "*",
    "Action": "s3:*",
    "Resource": [
      "arn:aws:s3:::techsara-chatgpt",
      "arn:aws:s3:::techsara-chatgpt/*"
    ],
    "Condition": {"Bool": {"aws:SecureTransport": "false"}}
  }]
}
```

7. Management → Lifecycle rules: abort incomplete multipart uploads after 7
   days; expire quarantine/rejected temporary objects per policy; transition or
   expire `raw/`, `exports/`, `backups/postgres/`, and `backups/manifests/`
   according to the approved retention schedule. Preserve legal holds.
8. Configure prefixes: `raw/`, `normalized/`, `attachments/quarantine/`,
   `attachments/clean/`, `attachments/rejected/`, `exports/`,
   `backups/postgres/`, `backups/manifests/`, and `deploy/`.

CLI checkpoint:

```bash
aws s3api get-bucket-location --bucket techsara-chatgpt
aws s3api get-public-access-block --bucket techsara-chatgpt
aws s3api get-bucket-ownership-controls --bucket techsara-chatgpt
aws s3api get-bucket-versioning --bucket techsara-chatgpt
aws s3api get-bucket-encryption --bucket techsara-chatgpt
aws s3api get-bucket-policy-status --bucket techsara-chatgpt
```

Expected screenshot/checkpoint: “Bucket and objects not public,” Versioning
Enabled, ACLs disabled, and default encryption shown. Rollback only the new
lifecycle rules if their retention is wrong; never disable public-access blocks.

## 3. Create a least-privilege IAM policy for the exact S3 bucket

IAM → Policies → Create policy → JSON. Name it `TechSaraChatArchiveS3`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ListApprovedPrefixes",
      "Effect": "Allow",
      "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
      "Resource": "arn:aws:s3:::techsara-chatgpt",
      "Condition": {"StringLike": {"s3:prefix": [
        "raw/*", "normalized/*", "attachments/*", "exports/*",
        "backups/*", "deploy/*"
      ]}}
    },
    {
      "Sid": "ReadWriteArchiveObjects",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject", "s3:GetObjectVersion", "s3:PutObject",
        "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"
      ],
      "Resource": [
        "arn:aws:s3:::techsara-chatgpt/raw/*",
        "arn:aws:s3:::techsara-chatgpt/normalized/*",
        "arn:aws:s3:::techsara-chatgpt/attachments/*",
        "arn:aws:s3:::techsara-chatgpt/exports/*",
        "arn:aws:s3:::techsara-chatgpt/backups/*",
        "arn:aws:s3:::techsara-chatgpt/deploy/*"
      ]
    }
  ]
}
```

If SSE-KMS is selected, add only `kms:Encrypt`, `kms:Decrypt`, and
`kms:GenerateDataKey` for the exact key ARN. CLI checkpoint:

```bash
aws iam get-policy --policy-arn <policy-arn>
aws iam get-policy-version --policy-arn <policy-arn> --version-id <default-version>
```

Rollback: detach and delete only this new policy if review rejects it.

## 4. Create an EC2 role with S3 and Systems Manager access

IAM → Roles → Create role → AWS service → EC2. Attach
`AmazonSSMManagedInstanceCore`, `TechSaraChatArchiveS3`, and a small inline
policy allowing `ssm:GetParameter`, `ssm:GetParameters`, and
`ssm:GetParametersByPath` on
`arn:aws:ssm:us-east-1:<account-id>:parameter/techsara-chat-archive/*`.
Name it `techsara-chat-archive-ec2`.

```bash
aws iam get-role --role-name techsara-chat-archive-ec2
aws iam list-attached-role-policies --role-name techsara-chat-archive-ec2
```

Expected: trust principal `ec2.amazonaws.com` and both attached policies.

## 5. Launch Ubuntu 24.04 LTS x86_64 on `t3a.large`

EC2 → Launch instance. Select Canonical Ubuntu Server 24.04 LTS x86_64,
`t3a.large`, a public subnet, and **Proceed without a key pair**. Set metadata
options to IMDSv2 required and hop limit 1. Tag `Name=techsara-chat-archive`.

Checkpoint screenshot: AMI, architecture, instance type, subnet, metadata V2
required, and no key pair. Rollback: terminate only before data is attached.

## 6. Configure encrypted root and data EBS volumes

In the launch wizard set root to 30 GiB gp3, encrypted. EC2 → Volumes → Create
100 GiB gp3, encrypted, same Availability Zone, 3000 IOPS / 125 MiB/s. Set
Delete on termination **off** for data, tag `Backup=daily`, and attach as
`/dev/sdf` (Nitro will expose an NVMe name).

```bash
aws ec2 describe-volumes --volume-ids <data-volume-id> \
  --query 'Volumes[0].{AZ:AvailabilityZone,Encrypted:Encrypted,Size:Size,State:State}'
```

Expected: 100 GiB, encrypted, in-use, same AZ. Never format until step 12.

## 7. Attach the EC2 role

At launch choose the `techsara-chat-archive-ec2` IAM instance profile, or EC2
→ instance → Security → Modify IAM role after launch.

```bash
aws ec2 describe-iam-instance-profile-associations \
  --filters Name=instance-id,Values=<instance-id>
```

## 8. Allocate and associate an Elastic IP

EC2 → Elastic IP addresses → Allocate → Associate with the instance. Record it
in the change ticket.

```bash
aws ec2 describe-addresses --allocation-ids <allocation-id>
```

Rollback: disassociate before releasing; do not release an address referenced
by DNS.

## 9. Create the Cloudflare-only HTTPS security group

VPC → Security groups → Create. Add inbound TCP 443 rules for Cloudflare's
currently published IPv4 ranges from <https://www.cloudflare.com/ips/>. Do not
add a general internet source and do not open port 80; Cloudflare performs the
HTTP-to-HTTPS redirect at its edge. Add IPv6 source ranges only if you also
configure a reviewed IPv6 origin path. Outbound: HTTPS and required DNS/NTP, or
the approved account default.

```bash
aws ec2 describe-security-groups --group-ids <security-group-id> \
  --query 'SecurityGroups[0].IpPermissions'
```

## 10. Keep every non-TLS port closed publicly

The inbound list must contain no rules for 22, 80, 5050, 5432, 8000, or 8443.
Check both IPv4 and IPv6. From an external approved scanner, confirm direct
origin access is blocked. Roll back any rule that exposes a private port
immediately.

## 11. Connect through AWS Systems Manager Session Manager

Systems Manager → Fleet Manager must show the node Online. EC2 → Connect →
Session Manager → Connect, or:

```bash
aws ssm start-session --target <instance-id> --region us-east-1
```

Expected: an `ssm-user` shell without an inbound management port.

## 12. Format and mount the data EBS volume safely and idempotently

Inside the session, run `lsblk -f` and identify the unmounted 100 GiB device.
Nitro names can differ from `/dev/sdf`; never assume. Then:

```bash
sudo -i
cd /opt/techsara-chat-archive
./scripts/bootstrap_ec2_host.sh --data-device /dev/nvme1n1
findmnt /srv/techsara-chat-archive
ls -ld /srv/techsara-chat-archive/{postgres,backups,secrets,tls,pgadmin}
```

The script formats only an unformatted block device, refuses non-ext4 data,
uses the UUID in `/etc/fstab`, and is safe to rerun. If the device already
contains expected data, mount it without formatting and verify its UUID first.

## 13. Install Docker Engine and Compose

The bootstrap runs `scripts/install_docker.sh`. Verify:

```bash
sudo docker version
sudo docker compose version
sudo systemctl is-enabled docker
```

Expected: server and client versions plus Compose v2. Rollback packages only
after stopping the archive and preserving `/srv`.

## 14. Confirm the host has no web server

The bootstrap installs Docker, Compose, AWS CLI, OpenSSL, `curl`, and `jq`; it
does not install a host web server. Confirm the only planned public listener is
the API container's TLS port from `compose.prod.yaml`. Do not install an
additional proxy or publish the container's internal port 8443.

## 15. Clone the private GitHub repository

Use a short-lived GitHub credential through the approved credential helper or
a read-only deploy key, then remove it from the host:

```bash
sudo install -d -o root -g root -m 0750 /opt/techsara-chat-archive
sudo git clone https://github.com/<owner>/<repository>.git /opt/techsara-chat-archive
cd /opt/techsara-chat-archive
sudo git checkout <green-commit-sha>
git status --short
```

Expected: detached/checked-out approved SHA and clean status.

## 16. Create SSM SecureString parameters and fetch them

Parameter Store path `/techsara-chat-archive/`. Create String values for:
`image_repository`, `image_tag`, `public_base_url`, `archive_hostname`,
`postgres_db`, `postgres_user`, `allowed_email_domains`, `oidc_issuer`,
`oidc_client_id`, `oidc_required_hd`, `managed_workspace_label`,
`pgadmin_email`, plus optional extension/compliance/retention settings. Set
`browser_content_capture_enabled`, `openai_written_authorization_confirmed`,
`training_export_enabled`, and `compliance_poll_enabled` to `false`.

Create SecureString values using the interactive helper:

```bash
./scripts/put_secrets.sh --project techsara-chat-archive --region us-east-1 --generate
sudo ./scripts/fetch_ssm_secrets.sh
sudo stat -c '%a %U:%G %n' /srv/techsara-chat-archive/secrets/*
```

Expected: secret files are root-owned `440` with only the consuming service's
numeric group, the directory is root-owned `750`, and `.env` is `600`.

## 17. Configure Cloudflare proxied DNS and Origin CA

Follow [CLOUDFLARE_DNS_AND_TLS.md](CLOUDFLARE_DNS_AND_TLS.md): create the proxied
A record and create a certificate for the exact hostname. DNS-only mode does
not supply Full (strict) or the trusted client-address header. Never store the
private key in SSM, Git, Compose, or a ticket.

## 18. Install and validate direct API TLS

```bash
sudo ./scripts/install_origin_tls.sh \
  --cert-file /root/origin-input.pem --key-file /root/origin-input.key
sudo stat -c '%a %U:%G %n' /srv/techsara-chat-archive/tls/*
```

Set Cloudflare SSL/TLS mode Full (strict) and enable its edge HTTPS redirect.
Expected after the API starts: origin certificate validation succeeds;
unexpected Host headers are rejected by the application.

## 19. Start PostgreSQL

```bash
cd /opt/techsara-chat-archive
sudo docker compose -f compose.prod.yaml pull postgres
sudo docker compose -f compose.prod.yaml up -d postgres
sudo docker compose -f compose.prod.yaml exec -T postgres pg_isready
```

## 20. Run Alembic migrations

```bash
sudo docker compose -f compose.prod.yaml run --rm migrate
sudo docker compose -f compose.prod.yaml exec -T postgres \
  psql -U techsara_app -d techsara_chat_archive -c 'select * from alembic_version;'
```

On migration failure, do not start the API. Preserve logs and restore the
pre-change backup into a new database; never improvise a destructive downgrade.

## 21. Start API, worker, backup, and optional poller

```bash
sudo ./scripts/deploy_ec2.sh
sudo docker compose -f compose.prod.yaml ps
# Only after OpenAI authorization and configuration:
sudo docker compose -f compose.prod.yaml --profile compliance up -d compliance-poller
```

## 22. Verify health, S3, database, and background jobs

```bash
sudo ./scripts/verify_deployment.sh
sudo docker compose -f compose.prod.yaml logs --tail 100 api worker backup
sudo ss -lntp
```

Expected: public health is OK; role identity resolves; `HeadBucket` succeeds;
database is ready; only TLS port 443 is public; jobs transition without logging
content.

## 23. Build and configure the extension

On a trusted build host run `make extension-zip`, verify its SHA against CI,
publish privately, obtain the permanent extension ID, update S3 CORS for exactly
`chrome-extension://<id>`, set `extension_ids` in SSM, deploy Chrome managed
storage, and refetch/redeploy. See [CHROME_ENTERPRISE_DEPLOYMENT.md](CHROME_ENTERPRISE_DEPLOYMENT.md).

## 24. Run staged pilots: 5, 25, 75, 150, 250

At each stage hold for an agreed observation window. Review API latency/error
rate, database connections, queue depth/age, S3 errors, disk, backup freshness,
extension offline queue, privacy reports, and support load. Stop rollout if a
capture boundary, data-loss, or security control fails. Capacity is a measured
claim, not inferred from instance size.

## 25. Configure nightly backups and restore tests

The `backup` service runs every 24 hours and uploads dump + manifest under
`backups/`. Enable the supplied systemd timer only if the container loop is
disabled. Weekly:

```bash
sudo docker compose -f compose.prod.yaml exec -T backup \
  /bin/sh /opt/scripts/verify_backup.sh --full-restore
```

Record size, checksum, age, restored row counts, revision, operator, and ticket.

## 26. Configure CloudWatch or host monitoring

Install/configure the CloudWatch Agent manually or use the approved host agent.
Alert on EC2 status, CPU sustained above 80%, memory/swap pressure, data volume
above 80/90%, PostgreSQL container restarts, readiness failures, queue age,
archive job failures, backup older than 30 hours, and certificate expiry. Test
the notification route. Monitoring must not ship prompts, responses, tokens,
authorization headers, cookies, or presigned URLs.

## Deployment rollback summary

Application failure: set the preceding immutable `IMAGE_TAG` in SSM and rerun
`scripts/deploy_ec2.sh`. Database failure: restore the last verified dump into a
new database and switch only after validation. Edge failure: restore the prior
root-owned certificate pair and restart the API container. Instance failure:
launch a replacement in the same AZ or attach the preserved data volume to a
reviewed replacement, then repeat steps 7–22. Capture gates remain false during
every rollback.
