# Privacy and employee notice

This document has two parts: the notice to give employees, and the internal
detail behind it.

---

# Part 1 — Notice to employees

## What your company archives

Your company keeps a record of conversations in the **managed company ChatGPT
workspace**. A managed Chrome extension archives:

- conversations you open in the company workspace, including their earlier
  messages once they load;
- every message you send and every answer you receive in that workspace;
- files and images you attach to a company conversation;
- your company identity (the Google account you sign in with) and which browser
  profile captured the record.

## What is never archived

- **Anything you type but do not send.** Draft text is never read.
- **Your keystrokes.** There is no keylogger of any kind.
- **Your passwords, cookies or ChatGPT session.** These are never accessed.
- **Personal ChatGPT workspaces.** If you are not in the company workspace,
  nothing is archived.
- **Any other website.** The extension runs only on ChatGPT addresses.
- **The model's hidden reasoning.** It is not shown in the page, so it is not
  archived.

## What is honestly not covered

- Conversations you have never opened in this browser are not archived by the
  extension.
- Conversations from another device or browser are not included.
- Files shown in older conversations may be recorded as a name and a size only,
  because the page does not always make the original file available.

## Seeing your own status

Click the extension icon. The popup shows whether you are signed in, whether the
company workspace was verified, whether archiving is active, when the last
successful sync happened and how much is waiting to upload. **Details** opens a
page with what has been archived from this browser and an explicit list of what
is not covered.

## Who can see archived conversations

Access is limited by role and every access is logged:

- **Compliance administrators** — for compliance and legal obligations
- **Security reviewers** — for security investigations
- **Data curators** — for approved, curated exports
- **IT support** — system health only; support staff cannot read message content

## Your rights

Depending on where you live, you may have the right to know what is held about
you, to ask for a copy, to ask for correction, and in some cases to ask for
deletion. Some records must be kept for legal or compliance reasons and cannot
be deleted on request. Contact your privacy team or IT support.

## Retention

Conversations are retained for the period in your company's retention policy
(the default configuration is 365 days), then soft-deleted, then permanently
removed after a grace period — unless a legal hold applies.

## Questions

Contact IT support or your privacy team. If you believe something was archived
that should not have been, report it: there is a documented process to
investigate and remove it.

---

# Part 2 — Internal detail

## Lawful basis and prerequisites

Before enabling capture:

1. Record the lawful basis (legitimate interests, legal obligation, or consent
   where required).
2. Complete a data protection impact assessment where your jurisdiction requires
   one.
3. Consult works councils or employee representatives where required — in
   several jurisdictions this is mandatory before monitoring.
4. Give employees this notice **before** capture starts, not after.
5. Record the written authorization decision, then set
   `OPENAI_WRITTEN_AUTHORIZATION_CONFIRMED=true`.

The system stays fail-closed until step 5, by design.

## Data categories

| Category | Examples | Sensitivity |
| --- | --- | --- |
| Identity | Company email, user id, device id | Personal data |
| Content | Prompts, answers, code, attachments | Potentially sensitive; may contain personal data of third parties |
| Metadata | Timestamps, conversation ids, adapter versions | Low |
| Derived | Checksums, pseudonymised employee hash, token estimates | Pseudonymous |

Conversation content is the risk concentration: employees paste customer data,
credentials and personal information into chat assistants. Treat the archive as
your most sensitive datastore, not as logs.

## Minimisation in practice

- Drafts, keystrokes, cookies and tokens are never collected.
- S3 prefixes use a pseudonymised workspace hash, never the raw label.
- `employee_id_hash` is a SHA-256 pseudonym, not an email address.
- Logs never contain message content unless `LOG_MESSAGE_CONTENT=true`, which
  the production settings guard refuses.
- Support diagnostics contain counts and versions, never content.

## Subject access requests

```sql
-- What is held about one employee
SELECT c.id, c.source_conversation_id, c.title, c.created_at, c.capture_completeness
  FROM conversations c JOIN users u ON u.id = c.user_id
 WHERE u.email = 'employee@example.com' AND c.deleted_at IS NULL
 ORDER BY c.created_at DESC;
```

Export through the audited admin API rather than by hand, so the disclosure is
itself recorded.

## Deletion requests

1. Check for a legal hold. If one applies, deletion is refused and the requester
   is told why.
2. Apply a soft delete with a reason recorded.
3. Physical deletion happens after the grace period, and writes an audit row.
4. S3 objects are covered by lifecycle rules; the instance role cannot delete
   objects, so any immediate S3 removal is a deliberate administrator action.

## Third parties

No conversation content is sent to any third-party model or service for
classification, enrichment or training. The only external calls the backend makes
are to your identity provider (JWKS and token exchange) and, if configured, to
the authorized OpenAI Enterprise Compliance interface.
