# GitHub secrets, variables and AWS SSM parameters

Three separate stores, with a strict rule about which holds what:

| Store | Holds | Why |
| --- | --- | --- |
| **GitHub repository secrets** | how to reach the instance | GitHub needs these to open an SSH session, and nothing else. |
| **GitHub repository variables** | non-sensitive pinning data | Readable in the UI, which is what you want for a host fingerprint. |
| **AWS SSM Parameter Store** | every production application secret | Read on the instance with the EC2 instance role. GitHub never sees them. |

## GitHub repository secrets — exactly four

These already exist in the repository. Do not rename them, and do not create
environment-scoped duplicates.

| Name | Value | Notes |
| --- | --- | --- |
| `EC2_HOST` | Instance hostname or public IP | Used as `HostName` in the generated SSH config. |
| `EC2_USER` | `ec2-user` | Amazon Linux 2023 default login. |
| `EC2_SSH_PORT` | `22` | Any port works; `known_hosts` entries are written in `[host]:port` form when it is not 22. |
| `EC2_SSH_PRIVATE_KEY` | OpenSSH private key, full PEM including the header and footer lines | Never printed. The composite action validates it with `ssh-keygen -y` and fails if it is malformed. |

Nothing else is required for a normal deployment. In particular, **do not add**:

```text
AWS_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY
AWS_DEPLOY_ROLE_ARN
POSTGRES_PASSWORD
CLOUDFLARE_TUNNEL_TOKEN
OPENAI_API_KEY
GHCR_TOKEN
```

If one of these is present from an earlier iteration, delete it.

## GitHub repository variables — optional

| Name | Value | Purpose |
| --- | --- | --- |
| `EC2_SSH_HOST_KEY` | The instance's SSH host public key | Pins the host key so the deployment cannot be redirected to a different machine. |

A host **public** key is not a secret, so a repository *variable* is the right
home for it: it is visible in the GitHub UI and can be compared against the
instance at any time. The workflows read `vars.EC2_SSH_HOST_KEY` first and fall
back to a secret of the same name if you would rather keep it there.

### Obtaining and verifying the host key

Both steps matter. Step 1 is convenient; step 2 is what makes it trustworthy.

1. Read the key from the instance itself, over a session you already trust
   (AWS Systems Manager Session Manager, or an SSH session whose fingerprint
   you have already checked):

   ```bash
   sudo cat /etc/ssh/ssh_host_ed25519_key.pub
   # ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA... root@ip-10-0-0-10
   ```

   Take the first two fields only: `ssh-ed25519 AAAAC3Nza...`. Paste that as
   the value of `EC2_SSH_HOST_KEY`. The workflow prepends the hostname (and the
   port, when it is not 22) to build the `known_hosts` line.

2. Independently confirm the fingerprint. On the instance:

   ```bash
   ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
   # 256 SHA256:xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx no comment (ED25519)
   ```

   Compare it with the fingerprint the *Test EC2 connection* workflow prints in
   its log, and with the host-key fingerprints EC2 writes to the instance's
   system log on first boot (EC2 console → Instance → Monitor and
   troubleshoot → Get system log).

### What happens when it is not set

The workflow does **not** fall back to `StrictHostKeyChecking=no`. It runs
`ssh-keyscan` once, writes the result to `known_hosts`, uses it with
`StrictHostKeyChecking yes` for that run, prints the fingerprints, and emits a
GitHub warning annotation telling you to pin the key. That is trust on first
use: acceptable for the initial bring-up, and it should not be left in place.

## AWS SSM Parameter Store

Prefix: `/techsara-chat-archive/`. Region: `us-east-1`. Read on the instance by
`scripts/fetch_ssm_secrets.sh` with `--with-decryption`, using the EC2 instance
role. Values are never printed and never leave the host.

Create them with `scripts/put_secrets.sh` (prompts, never echoes) or in the
AWS console.

### Required — deployment fails without these

| Parameter | Type | Notes |
| --- | --- | --- |
| `/techsara-chat-archive/postgres_password` | SecureString | 40+ random characters. |
| `/techsara-chat-archive/postgres_user` | String | e.g. `techsara_app`. |
| `/techsara-chat-archive/postgres_db` | String | e.g. `techsara_chat_archive`. |
| `/techsara-chat-archive/jwt_secret` | SecureString | `openssl rand -base64 48`. |
| `/techsara-chat-archive/config_signing_key` | SecureString | `openssl rand -base64 48`. |
| `/techsara-chat-archive/cloudflare_tunnel_token` | SecureString | From the Cloudflare tunnel. See [CLOUDFLARE_TUNNEL_SETUP.md](CLOUDFLARE_TUNNEL_SETUP.md). |
| `/techsara-chat-archive/public_base_url` | String | `https://archive.<company-domain>`. Must be `https://`. `ARCHIVE_HOSTNAME` is derived from it. |
| `/techsara-chat-archive/oidc_client_id` | String | Google Workspace OAuth client id. |
| `/techsara-chat-archive/oidc_required_hd` | String | Required Google hosted domain. |
| `/techsara-chat-archive/allowed_email_domains` | String | Comma-separated employee domains. |
| `/techsara-chat-archive/managed_workspace_label` | String | Exact ChatGPT workspace label. |

### Optional — sensible defaults when absent

| Parameter | Type | Default |
| --- | --- | --- |
| `oidc_issuer` | String | `https://accounts.google.com` |
| `oidc_client_secret` | SecureString | empty |
| `openai_compliance_api_key` | SecureString | empty (poller stays off) |
| `openai_workspace_id` | String | empty |
| `pgadmin_email` | String | placeholder; set before using the admin profile |
| `pgadmin_password` | SecureString | empty; set before using the admin profile |
| `extension_ids` | String | empty (CORS allowlist stays closed) |
| `admin_origins` | String | empty |
| `managed_workspace_ids` | String | empty |
| `postgres_max_connections` | String | `120` |
| `database_pool_size` | String | `12` |
| `database_max_overflow` | String | `4` |
| `database_pool_timeout_seconds` | String | `30` |
| `database_pool_recycle_seconds` | String | `1800` |
| `api_workers` | String | `3` |
| `worker_concurrency` | String | `2` |
| `log_level` | String | `INFO` |
| `s3_encryption_mode` | String | `SSE-S3` |
| `s3_kms_key_id` | String | empty |
| `s3_health_cache_seconds` | String | `60` |
| `backup_interval_seconds` | String | `86400` |
| `backup_retention_days` | String | `90` |
| `raw_retention_days` | String | `365` |
| `cloudflared_metrics_port` | String | `2000` |
| `pgadmin_port` | String | `5050` |

### Capture gates — absent means false

| Parameter | Type | Default |
| --- | --- | --- |
| `browser_content_capture_enabled` | String | `false` |
| `openai_written_authorization_confirmed` | String | `false` |
| `kill_switch_enabled` | String | `false` |
| `training_export_enabled` | String | `false` |
| `compliance_poll_enabled` | String | `false` |
| `auto_archive_current_open_chat` | String | `true` |
| `attachment_capture_enabled` | String | `true` |

`fetch_ssm_secrets.sh` refuses to write anything other than the literal
`true`/`false` for the first three, so a typo fails the deployment instead of
quietly enabling capture. Browser message-content capture activates only when
`browser_content_capture_enabled` **and**
`openai_written_authorization_confirmed` are both `true` and the kill switch is
off.

## GitHub environment

`Settings → Environments → production`

* **Deployment branches:** *Selected branches* → `main` only.
* **Required reviewers:** optional. Adding one turns every push to `main` into
  a gated release; the deploy job waits for approval before the SSH step.
* **Environment secrets:** none needed. The four secrets above are repository
  secrets and are available to the deploy job as-is.
* **Environment variables:** none needed.

## Rotation

| Item | How |
| --- | --- |
| `EC2_SSH_PRIVATE_KEY` | Generate a new keypair, append the public key to `~ec2-user/.ssh/authorized_keys`, update the GitHub secret, run *Test EC2 connection*, then remove the old public key. |
| Host key (`EC2_SSH_HOST_KEY`) | Only changes if the instance is rebuilt. Re-read it as above and update the variable. |
| Any SSM secret | `scripts/put_secrets.sh`, then re-run the deploy workflow so containers pick up the regenerated files. |
| `cloudflare_tunnel_token` | Rotate the tunnel token in the Cloudflare dashboard, update SSM, redeploy. |

## If a secret is exposed

Treat it as compromised the moment it appears in a commit, a log or a shared
screen. Rotate first, investigate second:

1. Rotate the value at its source (Cloudflare, Google, AWS, PostgreSQL).
2. Update SSM (or the GitHub secret) with the new value.
3. Redeploy so every container reads the new material.
4. Record what was exposed, when, and what was rotated in
   [SECURITY_REVIEW.md](SECURITY_REVIEW.md).

Rewriting git history does not un-expose a secret that was pushed. Rotate.
