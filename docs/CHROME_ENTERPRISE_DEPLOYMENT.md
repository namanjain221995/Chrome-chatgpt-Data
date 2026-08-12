# Chrome Enterprise deployment

## What you are deploying

A Manifest V3 extension, force-installed on managed browsers, configured through
Chrome Enterprise policy. Policy tells the extension **where** the company
backend is; the backend decides **whether** anything may be captured.

## 1. Build and verify the package

```bash
make extension-zip
```

```
artifacts/techsara-chatgpt-archive-extension-1.0.0.zip
artifacts/techsara-chatgpt-archive-extension-1.0.0.zip.sha256
```

The ZIP is byte-reproducible: entries are sorted, timestamps pinned, no OS
metadata. Building the same commit twice gives the same SHA-256, so you can
verify that what you upload is what CI built.

```bash
sha256sum -c artifacts/*.zip.sha256
```

## 2. Publish

### Chrome Web Store, private listing (recommended)

1. Sign in to the [Developer Dashboard](https://chrome.google.com/webstore/devconsole)
   with a company account.
2. **New item** → upload the ZIP.
3. Visibility: **Private**, restricted to your Google Workspace domain.
4. Complete the privacy declarations honestly. The extension collects
   *personally identifiable information* (company email) and *user activity*
   (conversation content in the company workspace). Link to your privacy notice.
5. Publish, then record the **extension id**.

Google's review typically takes a few days. A private listing is still reviewed.

### Self-hosted (no store review)

Host `update.xml` and the `.crx` on an internal HTTPS server and set
`ExtensionInstallSources`. This avoids review but makes you responsible for
signing keys and update delivery. Most organisations should prefer the private
listing.

## 3. Register the extension id

```bash
aws ssm put-parameter --name /techsara-chat-archive/extension_ids \
  --value "<extension-id>" --type String --overwrite --region us-east-1
```

Add the origin to Terraform so S3 accepts direct uploads:

```hcl
extension_origins = ["chrome-extension://<extension-id>"]
```

```bash
cd infra/terraform && terraform apply
```

Then redeploy the backend so its CORS allowlist includes the extension.

Also add the OAuth redirect URI in Google Cloud Console:

```
https://<extension-id>.chromiumapp.org/oidc
```

## 4. Force-install and configure

Google Admin console → **Devices → Chrome → Apps & extensions → Users & browsers**:

1. Select the organizational unit.
2. Add the extension by id.
3. Installation policy: **Force install**.
4. Paste this into **Policy for extensions**:

```json
{
  "apiBaseUrl": { "Value": "https://archive.example.com" },
  "organizationSlug": { "Value": "techsara" },
  "oidcClientId": { "Value": "123456789-abc.apps.googleusercontent.com" },
  "allowedEmailDomains": { "Value": ["example.com"] },
  "managedWorkspaceLabel": { "Value": "TechSara's Workspace" },
  "managedWorkspaceIds": { "Value": [] },
  "privacyNoticeUrl": { "Value": "https://intranet.example.com/chatgpt-archive-privacy" },
  "supportContact": { "Value": "it-support@example.com" },
  "enabled": { "Value": true }
}
```

`enabled: true` only permits the extension to run. It does **not** enable
capture: the server gates decide that.

### Group Policy on Windows

`HKLM\Software\Policies\Google\Chrome\3rdparty\extensions\<extension-id>\policy`
with the same JSON.

### Linux

`/etc/opt/chrome/policies/managed/techsara-archive.json`:

```json
{
  "ExtensionInstallForcelist": ["<extension-id>;https://clients2.google.com/service/update2/crx"],
  "3rdparty": { "extensions": { "<extension-id>": { "apiBaseUrl": "https://archive.example.com" } } }
}
```

### macOS

A configuration profile with the `com.google.Chrome` payload and the same keys.

## 5. Verify on a managed device

1. `chrome://policy` → **Reload policies** → confirm the extension policy is
   present and shows no conflict.
2. `chrome://extensions` → the extension is installed and cannot be removed.
3. Click the icon: the popup should show "not signed in".
4. Sign in with a company Google account.
5. Open a company-workspace ChatGPT conversation. The popup should show the
   workspace as verified.
6. **Details** → confirm the policy panel reflects the server's gates.

## 6. Rolling out

| Stage | Audience | Watch for |
| --- | --- | --- |
| 1. Pilot | IT team, 5-10 people, one week | Sign-in failures, workspace verification failures, queue growth |
| 2. Early adopters | One department, 20-30 people, one week | Ingest error rate, p95 latency, backpressure |
| 3. General | Everyone | Queue depth, disk growth, CPU |

Keep the capture gates **off** during stage 1 if you want to validate delivery
and sign-in before any content is archived.

## Updating

Upload the new ZIP to the same store item. Managed browsers update within a few
hours. Because policy and configuration are server-side, most changes need no
extension update at all.

Set `minimum_extension_version` server-side to flag stale installs.

## Uninstalling

Remove the force-install entry, or set `"enabled": { "Value": false }`. Removing
the extension does not delete archived data; that is a retention decision.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| Extension not installed | OU mismatch or policy not applied | `chrome://policy`, check the OU |
| "Waiting for company configuration" | `apiBaseUrl` missing or unreachable | Check policy JSON and `/health/ready` |
| "Company sign-in is not configured" | `oidcClientId` missing | Add it to the policy |
| Sign-in fails with a redirect error | Redirect URI not registered | Add `https://<id>.chromiumapp.org/oidc` in Google Cloud Console |
| "not the managed company workspace" | Label mismatch | Compare `managedWorkspaceLabel` with what ChatGPT displays, exactly |
| Nothing archives, no error | Capture gates are off | Expected until both server gates are true |
| Attachment uploads fail | S3 CORS missing the extension origin | Add `extension_origins` in Terraform and apply |
