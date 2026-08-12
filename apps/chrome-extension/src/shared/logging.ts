/**
 * Diagnostics that are safe to share with IT support.
 *
 * Message text, HTML, file bytes and tokens never reach the log ring. Only
 * counts, identifiers, timings and error codes are recorded. The ring buffer is
 * bounded so a long-lived service worker cannot grow without limit.
 */

export type LogLevel = 'debug' | 'info' | 'warn' | 'error';

export interface LogEntry {
  at: number;
  level: LogLevel;
  event: string;
  detail: Record<string, unknown>;
}

const MAX_ENTRIES = 200;
const MAX_DETAIL_KEYS = 12;
const MAX_STRING = 200;

const FORBIDDEN_KEYS = new Set([
  'text',
  'html',
  'content',
  'body',
  'message',
  'messages',
  'prompt',
  'answer',
  'title',
  'token',
  'access_token',
  'refresh_token',
  'id_token',
  'authorization',
  'cookie',
  'password',
  'secret',
  'bytes',
  'file',
  'blob',
  'upload_url',
  'signature',
]);

const ring: LogEntry[] = [];
let currentLevel: LogLevel = 'info';
const LEVEL_ORDER: Record<LogLevel, number> = { debug: 10, info: 20, warn: 30, error: 40 };

function sanitizeValue(value: unknown, depth = 0): unknown {
  if (depth > 3) return '[deep]';
  if (value === null || value === undefined) return value;
  if (typeof value === 'string') {
    return value.length > MAX_STRING ? `${value.slice(0, MAX_STRING)}…` : value;
  }
  if (typeof value === 'number' || typeof value === 'boolean') return value;
  if (Array.isArray(value)) return { arrayLength: value.length };
  if (typeof value === 'object') {
    const output: Record<string, unknown> = {};
    let count = 0;
    for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
      if (count >= MAX_DETAIL_KEYS) break;
      if (FORBIDDEN_KEYS.has(key.toLowerCase())) {
        output[key] = '[redacted]';
      } else {
        output[key] = sanitizeValue(item, depth + 1);
      }
      count += 1;
    }
    return output;
  }
  return '[unsupported]';
}

export function sanitizeDetail(detail?: Record<string, unknown>): Record<string, unknown> {
  if (!detail) return {};
  return sanitizeValue(detail) as Record<string, unknown>;
}

export function setLogLevel(level: LogLevel): void {
  currentLevel = level;
}

function record(level: LogLevel, event: string, detail?: Record<string, unknown>): void {
  const entry: LogEntry = { at: Date.now(), level, event, detail: sanitizeDetail(detail) };
  ring.push(entry);
  while (ring.length > MAX_ENTRIES) ring.shift();

  if (LEVEL_ORDER[level] < LEVEL_ORDER[currentLevel]) return;
  const line = `[techsara-archive] ${event}`;
  if (level === 'error') console.error(line, entry.detail);
  else if (level === 'warn') console.warn(line, entry.detail);
  else console.info(line, entry.detail);
}

export const log = {
  debug: (event: string, detail?: Record<string, unknown>): void => record('debug', event, detail),
  info: (event: string, detail?: Record<string, unknown>): void => record('info', event, detail),
  warn: (event: string, detail?: Record<string, unknown>): void => record('warn', event, detail),
  error: (event: string, detail?: Record<string, unknown>): void => record('error', event, detail),
};

export function recentLogs(limit = 100): LogEntry[] {
  return ring.slice(-limit);
}

export function clearLogs(): void {
  ring.length = 0;
}

/** Error message with any token-shaped substring removed. */
export function safeErrorMessage(error: unknown): string {
  const raw = error instanceof Error ? error.message : String(error);
  return raw
    .replace(/eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}/g, '[token]')
    .replace(/(?:Bearer|bearer)\s+\S+/g, 'Bearer [token]')
    .replace(/X-Amz-Signature=[0-9a-f]+/gi, 'X-Amz-Signature=[redacted]')
    .slice(0, 300);
}
