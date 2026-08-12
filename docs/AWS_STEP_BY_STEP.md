# AWS deployment, step by step

Follow in order. Each step states what it produces and how to check it worked.

## 0. Before you start

| Requirement | Detail |
| --- | --- |
| AWS account | Permission to create EC2, EBS, S3, IAM, SSM, CloudWatch, SNS |
| Region | One region for everything (default `us-east-1`) |
| Domain | A hostname you control, e.g. `archive.example.com` |
| Google Workspace | Admin access to create an OAuth client |
| Written authorization | The decision to archive conversations, recorded before capture is enabled |
| Tools | `terraform` ≥ 1.6, `aws` CLI v2, `session-manager-plugin` |

```bash
aws sts get-caller-identity   # confirm the right account
```

## 1. Provision infrastructure

```bash
cd infra/terraform
cp example.tfvars terraform.tfvars
$EDITOR terraform.tfvars     # region, domain, email domains, workspace label

terraform init
terraform plan -out=tfplan   # review carefully
terraform apply tfplan
```

**Produces:** the S3 bucket, IAM role and instance profile, security group, EC2
instance, encrypted EBS data volume, Elastic IP, SSM parameters, log group, SNS
topic and CloudWatch alarms.

```bash
terraform output              # note instance_id, public_ip, archive_bucket
```

Terraform deliberately does **not** hold your secrets: anything it manages is
written to state in plaintext.

## 2. Write the secrets

```bash
cd ../..
./scripts/put_secrets.sh --project techsara-chat-archive --region us-east-1
```

Prompts for each value without echoing it. Use `--generate` to have the
machine-only secrets (database password, JWT key, config signing key, pgAdmin
password) generated for you; the OAuth client secret and the compliance API key
always come from you.

```bash
aws ssm get-parameters-by-path --path /techsara-chat-archive \
  --region us-east-1 --query 'Parameters[].Name'
```

## 3. Point DNS at the instance

Create an A record for your hostname pointing at the Elastic IP from step 1.
If Terraform manages your zone, pass `route53_zone_id` and it is created for you.

```bash
dig +short archive.example.com    # must return the Elastic IP
```

Caddy cannot obtain a certificate until this resolves.

## 4. Configure Google Workspace OIDC

In Google Cloud Console → **APIs & Services → Credentials**:

1. Create an **OAuth client ID**, type **Web application**.
2. Authorized redirect URI:
   `https://<EXTENSION_ID>.chromiumapp.org/oidc`
   (fill in after the first extension build — step 7).
3. Restrict to your organization's internal users.
4. Put the client id in SSM as `/techsara-chat-archive/oidc_client_id`, and the
   client secret through `put_secrets.sh`.

```bash
aws ssm put-parameter --name /techsara-chat-archive/oidc_client_id \
  --value "<client-id>" --type String --overwrite --region us-east-1
aws ssm put-parameter --name /techsara-chat-archive/oidc_required_hd \
  --value "example.com" --type String --overwrite --region us-east-1
```

## 5. Copy the deployment bundle

```bash
./scripts/deploy_bundle.sh                     # builds artifacts/…tar.gz
aws ssm start-session --target <instance-id>   # no SSH, no inbound port

# On the instance:
sudo mkdir -p /opt/techsara-chat-archive
sudo chown ubuntu:ubuntu /opt/techsara-chat-archive
```

Transfer the bundle by putting it in your archive bucket and pulling it down
with the instance's own role:

```bash
aws s3 cp artifacts/techsara-chat-archive-deploy-*.tar.gz \
  s3://<archive-bucket>/deploy/ --sse AES256

# On the instance:
aws s3 cp s3://<archive-bucket>/deploy/techsara-chat-archive-deploy-<tag>.tar.gz /tmp/
tar -xzf /tmp/techsara-chat-archive-deploy-*.tar.gz -C /tmp
cp -r /tmp/techsara-chat-archive-deploy-*/. /opt/techsara-chat-archive/
```

## 6. Deploy

```bash
# On the instance:
cd /opt/techsara-chat-archive
sudo IMAGE_TAG=<git-sha> ./scripts/deploy_ec2.sh

sudo cp deploy/systemd/techsara-chat-archive.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable techsara-chat-archive
```

The script renders `.env` and the 0400 secret files from SSM, pulls images,
takes a pre-migration backup, migrates, starts the stack, and rolls the
application images back if the health check fails.

```bash
curl -s https://archive.example.com/health/ready | jq
curl -s https://archive.example.com/api/v1/config | jq '.config.policy'
```

At this point `capture_active` is **false**. That is correct.

## 7. Build and publish the extension

```bash
make extension-zip        # artifacts/techsara-chatgpt-archive-extension-1.0.0.zip
```

Upload to the Chrome Web Store as a **private** listing, or host it for
force-install. See [CHROME_ENTERPRISE_DEPLOYMENT.md](CHROME_ENTERPRISE_DEPLOYMENT.md).

Once you know the extension id:

```bash
aws ssm put-parameter --name /techsara-chat-archive/extension_ids \
  --value "<extension-id>" --type String --overwrite --region us-east-1
```

Add the extension origin to Terraform so S3 accepts direct uploads:

```hcl
extension_origins = ["chrome-extension://<extension-id>"]
```

```bash
cd infra/terraform && terraform apply
```

Then redeploy so the API's CORS allowlist picks up the id.

## 8. Enable capture (a policy decision)

Only after the written authorization is recorded and employees have been given
the privacy notice:

```bash
aws ssm put-parameter --name /techsara-chat-archive/browser_content_capture_enabled \
  --value "true" --type String --overwrite --region us-east-1
aws ssm put-parameter --name /techsara-chat-archive/openai_written_authorization_confirmed \
  --value "true" --type String --overwrite --region us-east-1

aws ssm send-command --document-name techsara-chat-archive-deploy \
  --targets Key=instanceids,Values=<instance-id> \
  --parameters imageTag=<git-sha>
```

```bash
curl -s https://archive.example.com/api/v1/config | jq '.config.policy.capture_active'
# true
```

## 9. Verify the whole path

1. Sign in through the extension popup with a company account.
2. Open a company-workspace conversation; the status pill reports what was
   archived.
3. Check the admin summary (needs a `compliance_admin` role):
   ```bash
   curl -s -H "Authorization: Bearer <token>" \
     https://archive.example.com/api/v1/admin/health-summary | jq
   ```
4. Confirm objects exist:
   ```bash
   aws s3 ls s3://<archive-bucket>/raw/ --recursive | head
   aws s3 ls s3://<archive-bucket>/normalized/ --recursive | head
   ```

## 10. Turn on the operational safety net

```bash
# Confirm the first backup lands
aws s3 ls s3://<archive-bucket>/backups/postgres/ --recursive | tail

# Prove it restores — do this now, not after an incident
aws ssm start-session --target <instance-id>
cd /opt/techsara-chat-archive
sudo docker compose -f compose.yaml -f compose.prod.yaml exec backup \
  sh /opt/scripts/verify_backup.sh --full-restore
```

Then schedule EBS snapshots (AWS Backup or DLM) as described in
[DISASTER_RECOVERY.md](DISASTER_RECOVERY.md), and confirm the SNS alarm
subscription in your email.

## Order summary

```
terraform apply → put_secrets.sh → DNS → OIDC client → bundle → deploy
  → extension build → extension id into SSM + Terraform → redeploy
  → enable capture gates → verify → backups and snapshots
```

## Cost estimate (us-east-1, indicative)

| Item | Monthly |
| --- | --- |
| t3a.large (on demand) | ~$55 |
| 130 GiB gp3 | ~$11 |
| Elastic IP (attached) | $0 |
| S3, first ~100 GB | ~$3 |
| CloudWatch logs and alarms | ~$5 |
| **Total** | **~$75/month** |

A one-year Compute Savings Plan takes the instance to roughly $35.
