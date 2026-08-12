# AWS Console checklist

Use region `us-east-1` throughout. This checklist is the visual companion to
[AWS_MANUAL_SETUP.md](AWS_MANUAL_SETUP.md); the longer guide contains policies,
CLI verification, expected output, and rollback.

## Existing S3 bucket

- [ ] S3 → `techsara-chatgpt` → Properties: Region is US East (N. Virginia).
- [ ] Permissions: all four Block Public Access controls are on.
- [ ] Permissions: Object Ownership is Bucket owner enforced (ACLs disabled).
- [ ] Properties: Versioning is Enabled.
- [ ] Properties: Default encryption is SSE-S3, or approved SSE-KMS.
- [ ] Permissions: bucket policy denies requests where SecureTransport is false.
- [ ] Management: lifecycle rules cover temporary uploads, exports, raw data,
      database backups, manifests, and incomplete multipart uploads.
- [ ] Permissions: CORS is absent until the permanent extension ID is known.

## IAM and Systems Manager

- [ ] IAM → Policies: `TechSaraChatArchiveS3` is scoped to the exact bucket and
      approved prefixes; there is no wildcard bucket resource.
- [ ] IAM → Roles: `techsara-chat-archive-ec2` trusts EC2.
- [ ] The role has `TechSaraChatArchiveS3` and
      `AmazonSSMManagedInstanceCore` attached.
- [ ] The role can read only `/techsara-chat-archive/*` SSM parameters.
- [ ] Parameter Store contains non-secret String values and secret SecureString
      values; both capture gates and training export are `false`.

## EC2, storage, and networking

- [ ] EC2 instance: Ubuntu 24.04 LTS x86_64, `t3a.large`, no key pair.
- [ ] Root EBS: 30 GiB gp3, encrypted, Delete on termination enabled.
- [ ] Data EBS: 100 GiB gp3, encrypted, same Availability Zone, Delete on
      termination disabled, tag `Backup=daily`.
- [ ] IAM instance profile is attached and IMDSv2 is required.
- [ ] Elastic IP is associated with the instance.
- [ ] Security group inbound rules contain only TCP 443 from Cloudflare's
      published ranges; there are no rules for 22, 80, 5432, 5050, 8000, or 8443.
- [ ] Systems Manager → Fleet Manager shows the node Online.
- [ ] Session Manager opens a shell without SSH.
- [ ] `lsblk -f` shows the data volume mounted at
      `/srv/techsara-chat-archive`.

## Application and edge

- [ ] Docker Engine, Compose plugin, AWS CLI, OpenSSL, and curl are installed;
      no host web server is required.
- [ ] Private repository is at `/opt/techsara-chat-archive`.
- [ ] Cloudflare A record points to the Elastic IP and is Proxied.
- [ ] Origin CA files are root-owned mode `0440`, group 10001; neither is in the
      repository and only the API container mounts them.
- [ ] Cloudflare encryption mode is Full (strict).
- [ ] Direct API TLS passes and only the intended hostname is accepted.
- [ ] Compose reports PostgreSQL, API, worker, and backup healthy/running.
- [ ] PostgreSQL has no published port; FastAPI publishes only TLS `443`; optional
      pgAdmin is `127.0.0.1:5050` only under the `admin` profile.

## Operational readiness

- [ ] `/health/live` and `/health/ready` pass through Cloudflare.
- [ ] EC2 role identity can `HeadBucket` on `techsara-chatgpt`.
- [ ] A nightly logical backup and manifest exist under approved prefixes.
- [ ] A restore into a disposable database has passed.
- [ ] Extension ZIP hash matches CI; managed policy uses the permanent ID.
- [ ] Pilot sequence is 5 → 25 → 75 → 150 → 250 users with a stop/go review.
- [ ] Host/disk/backup alerts have an owned, tested notification destination.
