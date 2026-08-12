/**
 * k6 load test for the 250-employee envelope.
 *
 * Target from the specification:
 *   - 250 registered employees, 100 online clients, 50 concurrent syncers
 *   - sustained 10 ingestion requests/second
 *   - short bursts of 25 requests/second
 *   - batches up to 100 messages or 2 MiB
 *   - mixed message batches and attachment-init calls
 *
 * Run against a dedicated load environment, never production and never a live
 * ChatGPT account:
 *
 *   k6 run -e BASE_URL=https://archive-load.example.com \
 *          -e TOKEN_FILE=./tokens.json tests/load/k6-ingest.js
 *
 * Thresholds are treated as pass/fail gates, so a regression fails CI rather
 * than producing a graph nobody reads.
 */

import http from 'k6/http';
import { check, sleep, fail } from 'k6';
import { Counter, Rate, Trend } from 'k6/metrics';
import { randomIntBetween } from 'https://jslib.k6.io/k6-utils/1.4.0/index.js';
import crypto from 'k6/crypto';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8080';
const MESSAGES_PER_BATCH = Number(__ENV.MESSAGES_PER_BATCH || 25);
const WORKSPACE_LABEL = __ENV.WORKSPACE_LABEL || "TechSara's Workspace";

// --- custom metrics --------------------------------------------------------
const acceptedMessages = new Counter('archive_messages_accepted');
const duplicateMessages = new Counter('archive_messages_duplicate');
const rejectedMessages = new Counter('archive_messages_rejected');
const backpressureRate = new Rate('archive_backpressure');
const queueDepth = new Trend('archive_queue_depth');
const batchDuration = new Trend('archive_batch_duration_ms');

export const options = {
  scenarios: {
    // 50 concurrent sync clients at a sustained 10 req/s.
    sustained_sync: {
      executor: 'constant-arrival-rate',
      rate: Number(__ENV.SUSTAINED_RPS || 10),
      timeUnit: '1s',
      duration: __ENV.SUSTAINED_DURATION || '5m',
      preAllocatedVUs: 50,
      maxVUs: 100,
      exec: 'ingestMessages',
      tags: { scenario: 'sustained' },
    },
    // Short 25 req/s bursts, the "everyone returns from a meeting" shape.
    burst_sync: {
      executor: 'ramping-arrival-rate',
      startRate: 5,
      timeUnit: '1s',
      preAllocatedVUs: 60,
      maxVUs: 150,
      startTime: '1m',
      stages: [
        { target: 25, duration: '30s' },
        { target: 25, duration: '1m' },
        { target: 5, duration: '30s' },
      ],
      exec: 'ingestMessages',
      tags: { scenario: 'burst' },
    },
    // Attachment metadata calls interleaved with message ingestion.
    attachments: {
      executor: 'constant-arrival-rate',
      rate: 2,
      timeUnit: '1s',
      duration: __ENV.SUSTAINED_DURATION || '5m',
      preAllocatedVUs: 10,
      maxVUs: 25,
      exec: 'initAttachment',
      tags: { scenario: 'attachments' },
    },
    // Popup polling: cheap, frequent, must stay fast under load.
    status_polling: {
      executor: 'constant-vus',
      vus: 10,
      duration: __ENV.SUSTAINED_DURATION || '5m',
      exec: 'pollStatus',
      tags: { scenario: 'status' },
    },
  },

  thresholds: {
    'http_req_duration{scenario:sustained}': ['p(50)<300', 'p(95)<1200', 'p(99)<2500'],
    'http_req_duration{scenario:burst}': ['p(95)<2500'],
    'http_req_duration{scenario:status}': ['p(95)<500'],
    http_req_failed: ['rate<0.01'],
    archive_backpressure: ['rate<0.05'],
    checks: ['rate>0.99'],
  },
};

// --- helpers ---------------------------------------------------------------

function token() {
  // Tokens are minted out of band by scripts/load_test_tokens.py so this file
  // never contains a credential.
  const raw = __ENV.ACCESS_TOKEN;
  if (!raw) {
    fail('ACCESS_TOKEN is required. Generate one with scripts/load_test_tokens.py');
  }
  return raw;
}

function headers() {
  return {
    Authorization: `Bearer ${token()}`,
    'Content-Type': 'application/json',
    'X-Correlation-Id': `k6-${__VU}-${__ITER}`,
  };
}

function clientContext() {
  return {
    extension_version: '1.0.0',
    adapter_version: '2024.1',
    schema_version: '1.0',
    device_fingerprint: `load${String(__VU).padStart(28, '0')}`,
    page_locale: 'en-GB',
    captured_at: new Date().toISOString(),
  };
}

function workspace() {
  return {
    source_workspace_id: null,
    label: WORKSPACE_LABEL,
    kind: 'managed_company',
    verified: true,
    verification_signals: ['workspace_label_match'],
  };
}

function sha256(value) {
  return crypto.sha256(value, 'hex');
}

function conversationId() {
  // 250 employees x a handful of live conversations each.
  return `load-conv-${__VU}-${Math.floor(__ITER / 20)}`;
}

// --- scenarios -------------------------------------------------------------

export function ingestMessages() {
  const conversation = conversationId();
  const messages = [];

  for (let index = 0; index < MESSAGES_PER_BATCH; index += 1) {
    const role = index % 2 === 0 ? 'user' : 'assistant';
    const text =
      role === 'user'
        ? `Load question ${__VU}-${__ITER}-${index}: what is the policy on expenses?`
        : `Load answer ${__VU}-${__ITER}-${index}: ${'the documented policy applies. '.repeat(
            randomIntBetween(1, 12),
          )}`;

    messages.push({
      idempotency_key: `k-load-${__VU}-${__ITER}-${index}`,
      source_conversation_id: conversation,
      source_message_id: `load-msg-${__VU}-${__ITER}-${index}`,
      role,
      sequence_index: __ITER * MESSAGES_PER_BATCH + index,
      text,
      parts: [{ index: 0, kind: 'text', text, structured: {} }],
      citations: [],
      completion_status: 'complete',
      is_edit: false,
      is_regeneration: false,
      branch_selected: true,
      content_sha256: sha256(text),
      attachment_client_ids: [],
    });
  }

  const started = Date.now();
  const response = http.post(
    `${BASE_URL}/api/v1/messages/batch`,
    JSON.stringify({ workspace: workspace(), client: clientContext(), messages }),
    { headers: headers(), tags: { endpoint: 'messages_batch' } },
  );
  batchDuration.add(Date.now() - started);

  const ok = check(response, {
    'batch accepted': (r) => r.status === 200,
    'response is json': (r) => (r.headers['Content-Type'] || '').includes('application/json'),
  });

  if (ok && response.status === 200) {
    const body = response.json();
    acceptedMessages.add(body.accepted || 0);
    duplicateMessages.add(body.duplicate || 0);
    rejectedMessages.add(body.rejected || 0);
    backpressureRate.add(body.backpressure === true);
    if (body.queue_depth !== null && body.queue_depth !== undefined) {
      queueDepth.add(body.queue_depth);
    }
  } else if (response.status === 503) {
    // Backpressure is a correct, designed response — not a failure.
    backpressureRate.add(true);
    sleep(randomIntBetween(1, 5));
  }

  sleep(randomIntBetween(1, 3));
}

export function initAttachment() {
  const content = `load-attachment-${__VU}-${__ITER}`;
  const payload = {
    client_attachment_id: `att-load-${__VU}-${__ITER}`,
    source_conversation_id: conversationId(),
    filename: `load-${__VU}-${__ITER}.png`,
    mime_type: 'image/png',
    byte_size: randomIntBetween(1024, 1024 * 512),
    sha256: sha256(content),
    relation: 'uploaded_by_user',
    metadata_only: false,
    client: clientContext(),
  };

  const response = http.post(`${BASE_URL}/api/v1/attachments/init`, JSON.stringify(payload), {
    headers: headers(),
    tags: { endpoint: 'attachments_init' },
  });

  check(response, {
    'attachment init accepted': (r) => r.status === 200 || r.status === 403,
    'presign returned or policy blocked': (r) =>
      r.status !== 200 || r.json('upload_url') !== undefined,
  });

  sleep(randomIntBetween(2, 6));
}

export function pollStatus() {
  const response = http.get(`${BASE_URL}/api/v1/sync/status`, {
    headers: headers(),
    tags: { endpoint: 'sync_status' },
  });

  check(response, {
    'status ok': (r) => r.status === 200,
    'coverage statement present': (r) =>
      r.status !== 200 || String(r.json('coverage_statement') || '').length > 0,
  });

  sleep(randomIntBetween(5, 15));
}

export function setup() {
  const health = http.get(`${BASE_URL}/health/ready`);
  if (health.status !== 200) {
    fail(`backend is not ready at ${BASE_URL} (status ${health.status})`);
  }
  console.log(`load test against ${BASE_URL}`);
  return { startedAt: new Date().toISOString() };
}

export function handleSummary(data) {
  const metric = (name, stat) => {
    const value = data.metrics[name]?.values?.[stat];
    return value === undefined ? 'n/a' : Math.round(value * 100) / 100;
  };

  const report = [
    '# Load test report',
    '',
    `Target: ${BASE_URL}`,
    `Duration: ${Math.round((data.state?.testRunDurationMs ?? 0) / 1000)}s`,
    '',
    '## Latency (all endpoints)',
    `- p50: ${metric('http_req_duration', 'p(50)')} ms`,
    `- p95: ${metric('http_req_duration', 'p(95)')} ms`,
    `- p99: ${metric('http_req_duration', 'p(99)')} ms`,
    '',
    '## Throughput',
    `- requests: ${metric('http_reqs', 'count')}`,
    `- request rate: ${metric('http_reqs', 'rate')}/s`,
    `- messages accepted: ${metric('archive_messages_accepted', 'count')}`,
    `- messages duplicate: ${metric('archive_messages_duplicate', 'count')}`,
    `- messages rejected: ${metric('archive_messages_rejected', 'count')}`,
    '',
    '## Health',
    `- failed requests: ${metric('http_req_failed', 'rate')}`,
    `- backpressure rate: ${metric('archive_backpressure', 'rate')}`,
    `- max queue depth: ${metric('archive_queue_depth', 'max')}`,
    '',
    'Record alongside this report (see docs/SCALING_250_USERS.md):',
    'database connections, CPU, memory, worker queue lag and EBS write latency.',
    '',
  ].join('\n');

  return {
    stdout: report,
    'artifacts/load-test-report.md': report,
    'artifacts/load-test-summary.json': JSON.stringify(data, null, 2),
  };
}
