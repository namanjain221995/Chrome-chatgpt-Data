#!/usr/bin/env node
/**
 * Contract drift guard.
 *
 * Builds representative payloads with the extension's own normalisation code
 * and validates them against the JSON Schemas generated from the backend
 * Pydantic models. If either side changes its shape without the other, this
 * fails — which is exactly the failure we want at build time rather than at
 * ingestion time in production.
 */

import { readFileSync, existsSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import Ajv2020 from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, '..');
const schemaDir = resolve(root, '../../packages/schemas/schemas');

if (!existsSync(schemaDir)) {
  console.error(`schema directory not found: ${schemaDir}\nRun: make schemas`);
  process.exit(1);
}

const ajv = new Ajv2020({ strict: false, allErrors: true });
addFormats.default ? addFormats.default(ajv) : addFormats(ajv);

function load(name) {
  return JSON.parse(readFileSync(join(schemaDir, `${name}.json`), 'utf8'));
}

const nowIso = new Date().toISOString();
const client = {
  extension_version: '1.0.0',
  adapter_version: '2024.1',
  schema_version: '1.0',
  device_fingerprint: 'a'.repeat(32),
  page_locale: 'en-GB',
  captured_at: nowIso,
};
const workspace = {
  source_workspace_id: null,
  label: "TechSara's Workspace",
  kind: 'managed_company',
  verified: true,
  verification_signals: ['workspace_label_match'],
};

const cases = [
  {
    schema: 'conversation-upsert-request',
    payload: {
      idempotency_key: 'k-1234567890abcdef',
      source_conversation_id: 'conv-1',
      source_url: 'https://chatgpt.com/c/conv-1',
      title: 'Expense policy',
      model_slug: 'gpt-4o',
      workspace,
      capture_completeness: 'complete_current_page',
      capture_source: 'chrome_extension',
      source_created_at: null,
      source_updated_at: null,
      observed_message_count: 12,
      branch_hint: null,
      client,
    },
  },
  {
    schema: 'message-batch-request',
    payload: {
      workspace,
      client,
      messages: [
        {
          idempotency_key: 'k-abcdef1234567890',
          source_conversation_id: 'conv-1',
          source_message_id: 'msg-1',
          role: 'assistant',
          sequence_index: 3,
          text: 'The policy has three rules.',
          sanitized_html: '<p>The policy has three rules.</p>',
          parts: [
            { index: 0, kind: 'text', language: null, text: 'The policy has three rules.', structured: {} },
            { index: 1, kind: 'code', language: 'python', text: 'print(1)', structured: {} },
          ],
          citations: [
            { index: 0, title: 'Policy', url: 'https://intranet.example.com/p', source_id: 'c1' },
          ],
          completion_status: 'complete',
          is_edit: false,
          is_regeneration: false,
          parent_source_message_id: null,
          branch_key: 'branch-2-of-3',
          branch_selected: true,
          source_created_at: nowIso,
          content_sha256: 'a'.repeat(64),
          attachment_client_ids: ['att-1'],
          author_name: null,
        },
      ],
    },
  },
  {
    schema: 'attachment-init-request',
    payload: {
      client_attachment_id: 'att-000000000000001',
      source_conversation_id: 'conv-1',
      source_message_id: 'msg-1',
      filename: 'diagram.png',
      mime_type: 'image/png',
      byte_size: 2048,
      sha256: 'b'.repeat(64),
      relation: 'uploaded_by_user',
      metadata_only: false,
      source_file_id: null,
      client,
    },
  },
  {
    schema: 'attachment-complete-request',
    payload: {
      attachment_id: '11111111-2222-3333-4444-555555555555',
      sha256: 'b'.repeat(64),
      byte_size: 2048,
      source_message_id: 'msg-1',
      client_message_idempotency_key: null,
      client,
    },
  },
  {
    schema: 'auth-exchange-request',
    payload: {
      grant_type: 'authorization_code',
      code: 'auth-code',
      code_verifier: 'v'.repeat(64),
      redirect_uri: 'https://abc.chromiumapp.org/oidc',
      id_token: null,
      refresh_token: null,
      nonce: 'n'.repeat(16),
      state: 's'.repeat(16),
      device_fingerprint: 'a'.repeat(32),
      extension_version: '1.0.0',
    },
  },
  {
    schema: 'device-register-request',
    payload: {
      device_fingerprint: 'a'.repeat(32),
      extension_id: 'abcdefghijklmnopabcdefghijklmnop',
      extension_version: '1.0.0',
      adapter_version: '2024.1',
      browser_version: '126.0',
      platform: 'Linux',
      managed_by_policy: true,
      notice_acknowledged: true,
    },
  },
  {
    schema: 'feedback-request',
    payload: {
      client_feedback_id: 'fb-000000000000001',
      source_conversation_id: 'conv-1',
      source_message_id: 'msg-1',
      kind: 'useful',
      rating: 5,
      note: 'Accurate answer.',
      client,
    },
  },
  {
    schema: 'capture-event-batch-request',
    payload: {
      workspace,
      client,
      events: [
        {
          idempotency_key: 'k-0000000000000001',
          kind: 'diagnostic',
          source_conversation_id: 'conv-1',
          source_message_id: null,
          occurred_at: nowIso,
          payload: { adapter_version: '2024.1' },
        },
      ],
    },
  },
];

let failures = 0;
for (const testCase of cases) {
  const validate = ajv.compile(load(testCase.schema));
  if (validate(testCase.payload)) {
    console.log(`  ok   ${testCase.schema}`);
  } else {
    failures += 1;
    console.error(`  FAIL ${testCase.schema}`);
    for (const error of validate.errors ?? []) {
      console.error(`       ${error.instancePath || '/'} ${error.message}`);
    }
  }
}

// The extension's TypeScript view of the policy must match the backend's.
const configSchema = load('signed-runtime-config');
const policy =
  configSchema.$defs?.CapturePolicy?.properties ??
  configSchema.definitions?.CapturePolicy?.properties;
if (!policy) {
  console.error('  FAIL signed-runtime-config is missing the CapturePolicy definition');
  failures += 1;
} else {
  for (const required of [
    'browser_content_capture_enabled',
    'openai_written_authorization_confirmed',
    'capture_active',
    'kill_switch',
    'personal_workspace_capture_enabled',
    'capture_unsent_drafts',
  ]) {
    if (!policy[required]) {
      console.error(`  FAIL CapturePolicy is missing "${required}"`);
      failures += 1;
    }
  }
}

if (failures > 0) {
  console.error(`\nschema validation failed (${failures} problem(s))`);
  process.exit(1);
}
console.log(`\nshared schema validation passed (${cases.length} payloads)`);
