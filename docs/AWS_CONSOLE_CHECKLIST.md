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

- [ ] EC2 instance: Amazon Linux 2023 x86_64, `t3a.large`, with the key pair
      whose private half is stored in `EC2_SSH_PRIVATE_KEY`.
- [ ] Root EBS: 30 GiB gp3, encrypted, Delete on termination enabled.
- [ ] Data EBS: 100 GiB gp3, encrypted, same Availability Zone, Delete on
      termination disabled, tag `Backup=daily`.
- [ ] IAM instance profile is attached and IMDSv2 is required.
- [ ] Elastic IP is associated with the instance.
- [ ] Security group inbound rules contain no application ports at all: no 80,
      443, 5432, 5050, 8000 or 8443. Public traffic arrives through the
      Cloudflare Tunnel, which is outbound-only, and nothing on the host
      listens on those ports -- an open rule there is pure attack surface.
- [ ] The only inbound rule is TCP 22, for administration and for the GitHub
      Actions deployment.
- [ ] Port 22 hardening: `sudo sshd -T | grep -E '^(passwordauthentication|permitrootlogin)'`
      reports `passwordauthentication no`. Restricting the source range is
      preferable but is not achievable with GitHub-hosted runners, which come
      from a large rotating pool; see docs/SECURITY.md for the trade-off and
      the alternatives.
- [ ] Systems Manager → Fleet Manager shows the node Online (used for
      Parameter Store access and break-glass sessions, not for deployment).
- [ ] SSH from the controlled source works and the host-key fingerprint
      matches the EC2 system log.
- [ ] `lsblk -f` shows the data volume mounted at
      `/srv/techsara-chat-archive`.

## Application and edge

- [ ] Docker Engine, Compose plugin v2.24+, AWS CLI, git and curl are
      installed; no host web server or reverse proxy is required.
- [ ] Private repository is at `/opt/techsara-chat-archive`.
- [ ] A named Cloudflare Tunnel `techsara-chatgpt-production` exists and shows
      Healthy with active connections.
- [ ] The public hostname routes to `http://api:8000`, and its DNS record is a
      Proxied CNAME to `<tunnel-id>.cfargotunnel.com`.
- [ ] The tunnel token is a SecureString in SSM; the rendered
      `/srv/techsara-chat-archive/secrets/cloudflared.env` is `400 root:root`.
- [ ] Cloudflare encryption mode is Full (strict); Always Use HTTPS is on.
- [ ] Only the intended hostname is accepted; a wrong Host header is rejected.
- [ ] Compose reports PostgreSQL, API, worker, cloudflared and backup
      healthy/running.
- [ ] No application port is published on the host: `ss -lntp` shows nothing on
      `0.0.0.0:8000`, `0.0.0.0:5432` or `0.0.0.0:5050`; optional pgAdmin is
      `127.0.0.1:5050` only under the `admin` profile.

## Operational readiness

- [ ] `/health/live` and `/health/ready` pass through Cloudflare.
- [ ] EC2 role identity can `HeadBucket` on `techsara-chatgpt`.
- [ ] A nightly logical backup and manifest exist under approved prefixes.
- [ ] A restore into a disposable database has passed.
- [ ] Extension ZIP hash matches CI; managed policy uses the permanent ID.
- [ ] Pilot sequence is 5 → 25 → 75 → 150 → 250 users with a stop/go review.
- [ ] Host/disk/backup alerts have an owned, tested notification destination.
