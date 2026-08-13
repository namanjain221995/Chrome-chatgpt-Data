# Google Workspace sign-in setup

Employees sign in to the archive with their company Google account. This
document is the exact console sequence, split by who has to do each part.

Only two scopes are requested: `openid email profile`. The archive never asks
for Gmail, Drive, Calendar or any other Google data.

## Who does what

| Part | Who | Why |
| --- | --- | --- |
| A. Create the project inside the company organisation and grant access | **Google Workspace administrator** | Only a project owned by the `techsarasolutions.com` organisation can set the consent screen to *Internal*. A personal project cannot. |
| B. Consent screen and OAuth client | Either | Can be done by the administrator, or by an engineer once part A grants them access. |
| C. Store the credentials in AWS SSM and deploy | Engineer | The values live in Parameter Store, never in Git. |

The administrator only strictly needs to do **part A**. Doing A and then
granting access is the smallest ask, and lets the engineer iterate without
going back to them.

## Why the organisation matters

The OAuth consent screen has two user types:

* **Internal** — only accounts in `techsarasolutions.com` can even reach the
  sign-in screen. No Google verification review. **This is what we want.**
* **External** — any Google account in the world reaches the consent screen,
  and the app needs Google's verification review before it can be published.

*Internal* is offered **only** when the Cloud project belongs to the Workspace
organisation. That is the entire reason an administrator is involved.

The backend independently rejects any identity whose `hd` claim is not the
configured domain, so External would not actually let an outsider in. But it
would put a public sign-in page in front of an internal system, and it drags in
a verification process. Use Internal.

---

## Part A — Workspace administrator

1. Sign in to <https://console.cloud.google.com> with the
   `@techsarasolutions.com` administrator account.
2. Click the **project selector** at the top of the page.
3. Confirm the organisation shown is **techsarasolutions.com**, not
   *No organisation*. If there is no organisation, the Workspace has never been
   linked to Google Cloud — do that first, or the rest cannot be Internal.
4. **New project**
   * Project name: `techsara-chatgpt-archive`
   * Organisation: `techsarasolutions.com`
   * Location: the organisation (not *No organisation*)
   * **Create**
5. Grant the engineer access so they can finish parts B and C:
   * **IAM & Admin → IAM → Grant access**
   * Principal: the engineer's `@techsarasolutions.com` address
   * Role: **Editor** (or **Owner** if they should manage it long term)
   * **Save**

That is all the administrator has to do. Tell the engineer the project name.

---

## Part B — consent screen and OAuth client

Make sure `techsara-chatgpt-archive` is the selected project before starting.

### B1. Consent screen

1. **APIs & Services → OAuth consent screen**
2. User type: **Internal** → **Create**
   * If *Internal* is greyed out, the project is not in the organisation. Go
     back to part A; nothing else here will be correct.
3. App information
   * App name: `TechSara ChatGPT Archive`
   * User support email: a monitored `@techsarasolutions.com` address
   * Application home page: `https://archive.techsarasolutions.com`
   * Developer contact: the same address
4. **Save and continue**
5. Scopes: **do not add any.** The two scopes used (`openid email profile`) are
   granted by default and need no configuration. **Save and continue.**
6. Review the summary and finish.

### B2. OAuth client

The extension signs in with `chrome.identity.launchWebAuthFlow`, and the
**backend** exchanges the resulting code for tokens using a client secret. That
is a confidential client, so the type is *Web application* — not *Desktop* and
not *Chrome App*.

1. **APIs & Services → Credentials → Create credentials → OAuth client ID**
2. Application type: **Web application**
3. Name: `TechSara ChatGPT Archive — extension`
4. **Authorised JavaScript origins:** leave empty.
5. **Authorised redirect URIs → Add URI:**

   ```
   https://<EXTENSION_ID>.chromiumapp.org/oidc
   ```

   Substitute the real extension id. See *Getting the extension id* below —
   this value must be exact, including the `/oidc` suffix, or sign-in fails
   with `redirect_uri_mismatch`.
6. **Create**. Copy the **Client ID** and **Client secret**.

Do not paste the client secret into chat, a ticket or an email. Put it straight
into Parameter Store as described in part C, or hand it over through the
company password manager.

---

## Getting the extension id

The redirect URI contains the extension id, and where that id comes from
depends on how the extension is distributed.

| Distribution | Where the id comes from | Available now? |
| --- | --- | --- |
| **Chrome Web Store**, private listing (recommended in [CHROME_ENTERPRISE_DEPLOYMENT.md](CHROME_ENTERPRISE_DEPLOYMENT.md)) | Assigned by the Store on first upload | No — upload the package first, then read the id from the item's URL or dashboard |
| **Self-hosted CRX**, signed with the project signing key | Derived from that key, so it is fixed and knowable in advance | Yes |

For the self-hosted route, derive it from the signing key without exposing the
key itself:

```bash
openssl rsa -in <signing-key>.pem -pubout -outform DER \
  | sha256sum | head -c 32 | tr '0123456789abcdef' 'abcdefghijklmnop'
```

The output is a 32-character `a`–`p` string.

An OAuth client can hold several redirect URIs, so a practical approach is to
add the self-hosted id now to unblock testing, and add the Web Store id later
once it exists. Remove whichever is not used before rollout.

---

## Part C — store the credentials and deploy

From AWS CloudShell or an administrator workstation:

```bash
aws ssm put-parameter --name /techsara-chat-archive/oidc_client_id \
  --value '<client-id>' --type String --overwrite --region us-east-1

aws ssm put-parameter --name /techsara-chat-archive/oidc_client_secret \
  --value '<client-secret>' --type SecureString --overwrite --region us-east-1

aws ssm put-parameter --name /techsara-chat-archive/oidc_required_hd \
  --value 'techsarasolutions.com' --type String --overwrite --region us-east-1

aws ssm put-parameter --name /techsara-chat-archive/extension_ids \
  --value '<EXTENSION_ID>' --type String --overwrite --region us-east-1
```

`extension_ids` is what opens the backend's CORS allowlist to
`chrome-extension://<id>`. Without it the extension is refused even with a
valid credential.

Then deploy — GitHub **Actions → Deploy to production → Run workflow** — and
confirm the placeholder is gone:

```bash
ssh ec2-user@<host> 'sudo /opt/techsara-chat-archive/scripts/verify_production.sh' \
  | grep -i oidc
```

The warning `OIDC_CLIENT_ID is still the bootstrap placeholder` should no
longer appear.

---

## What the backend enforces regardless

Console configuration is not the security boundary. On every sign-in the
backend independently:

* verifies the ID token signature against Google's JWKS;
* requires the `hd` claim to equal `OIDC_REQUIRED_HD`;
* requires the email domain to be in `ALLOWED_EMAIL_DOMAINS`;
* requires `email_verified`;
* checks the PKCE `code_verifier` and the `nonce` it issued.

A misconfigured consent screen cannot let an outside account in. It can only
make the sign-in page more public than it should be.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `redirect_uri_mismatch` | The registered URI does not match `https://<id>.chromiumapp.org/oidc` exactly | Compare character by character, including the `/oidc` suffix and the id |
| *Internal* not selectable | Project is not in the Workspace organisation | Recreate the project under the organisation (part A) |
| Sign-in succeeds, backend returns 403 | `hd` claim does not match `oidc_required_hd`, or the domain is not in `allowed_email_domains` | Check both SSM parameters, then redeploy |
| Browser blocks the call with a CORS error | The extension id is not in `extension_ids` | Set it in SSM and redeploy |
| `invalid_client` on exchange | Client secret missing or wrong in SSM | Re-store `oidc_client_secret`, redeploy |
