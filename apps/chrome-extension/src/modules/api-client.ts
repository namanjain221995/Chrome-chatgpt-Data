/**
 * Backend HTTP client.
 *
 * Every call is authenticated with the short-lived backend access token,
 * carries a correlation id, and classifies failures into retryable vs terminal
 * so the offline queue can make a sensible decision instead of hammering.
 */

import type {
  BatchResponse,
  SignedRuntimeConfig,
} from '../shared/types';
import { log, safeErrorMessage } from '../shared/logging';
import { randomId } from '../shared/util';

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code: string,
    readonly retryable: boolean,
    readonly retryAfterSeconds: number | null = null,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

export interface ApiClientOptions {
  baseUrl: string;
  getAccessToken: () => Promise<string | null>;
  onUnauthorized?: () => Promise<void>;
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
}

const RETRYABLE_STATUSES = new Set([408, 425, 429, 500, 502, 503, 504]);
const DEFAULT_TIMEOUT_MS = 20_000;

export class ApiClient {
  constructor(private readonly options: ApiClientOptions) {}

  private get fetchImpl(): typeof fetch {
    return this.options.fetchImpl ?? globalThis.fetch.bind(globalThis);
  }

  private url(path: string): string {
    const base = this.options.baseUrl.replace(/\/+$/, '');
    return `${base}${path.startsWith('/') ? path : `/${path}`}`;
  }

  private async request<T>(
    path: string,
    init: RequestInit & { authenticated?: boolean | 'optional' } = {},
  ): Promise<T> {
    const { authenticated = true, ...rest } = init;
    const headers = new Headers(rest.headers ?? {});
    headers.set('Content-Type', 'application/json');
    headers.set('Accept', 'application/json');
    headers.set('X-Correlation-Id', randomId().slice(0, 32));

    if (authenticated) {
      const token = await this.options.getAccessToken();
      // `'optional'` is for endpoints that answer both anonymous and
      // signed-in callers, with a different document for each: send the token
      // when there is one, but never fail for want of it.
      if (!token && authenticated !== 'optional') {
        throw new ApiError('Not signed in', 401, 'unauthenticated', false);
      }
      if (token) headers.set('Authorization', `Bearer ${token}`);
    }

    const controller = new AbortController();
    const timer = setTimeout(
      () => controller.abort(),
      this.options.timeoutMs ?? DEFAULT_TIMEOUT_MS,
    );

    let response: Response;
    try {
      response = await this.fetchImpl(this.url(path), {
        ...rest,
        headers,
        signal: controller.signal,
        credentials: 'omit',
        cache: 'no-store',
        // The archive backend is a different origin from ChatGPT; no cookies.
        mode: 'cors',
      });
    } catch (error) {
      throw new ApiError(
        `Network error: ${safeErrorMessage(error)}`,
        0,
        'network_error',
        true,
      );
    } finally {
      clearTimeout(timer);
    }

    if (response.status === 204) return undefined as T;

    let body: unknown = null;
    const text = await response.text();
    if (text) {
      try {
        body = JSON.parse(text);
      } catch {
        body = null;
      }
    }

    if (!response.ok) {
      const error = (body as { error?: { code?: string; message?: string } } | null)?.error;
      const code = error?.code ?? `http_${response.status}`;
      const message = error?.message ?? `Request failed with ${response.status}`;
      const retryAfter = Number.parseInt(response.headers.get('Retry-After') ?? '', 10);

      if (response.status === 401 && this.options.onUnauthorized) {
        await this.options.onUnauthorized();
      }
      throw new ApiError(
        message,
        response.status,
        code,
        RETRYABLE_STATUSES.has(response.status),
        Number.isFinite(retryAfter) ? retryAfter : null,
      );
    }
    return body as T;
  }

  // -- endpoints ----------------------------------------------------------

  async getConfig(): Promise<SignedRuntimeConfig> {
    // The server releases the managed workspace label and id allowlist only to
    // an authenticated caller, and the workspace verifier refuses to verify
    // anything without them. Asking anonymously while signed in therefore
    // leaves capture permanently disabled -- so send the token when we have
    // one. The endpoint still answers anonymous callers with the public
    // document, which is what an unauthenticated start-up needs.
    return this.request<SignedRuntimeConfig>('/api/v1/config', {
      method: 'GET',
      authenticated: 'optional',
    });
  }

  async health(): Promise<boolean> {
    try {
      await this.request('/health/ready', { method: 'GET', authenticated: false });
      return true;
    } catch (error) {
      log.debug('health_check_failed', { reason: safeErrorMessage(error) });
      return false;
    }
  }

  async exchange(payload: Record<string, unknown>): Promise<Record<string, unknown>> {
    return this.request('/api/v1/auth/exchange', {
      method: 'POST',
      body: JSON.stringify(payload),
      authenticated: false,
    });
  }

  async registerDevice(payload: Record<string, unknown>): Promise<Record<string, unknown>> {
    return this.request('/api/v1/devices/register', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  }

  async upsertConversation(payload: unknown): Promise<Record<string, unknown>> {
    return this.request('/api/v1/conversations/upsert', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  }

  async sendMessages(payload: unknown): Promise<BatchResponse> {
    return this.request<BatchResponse>('/api/v1/messages/batch', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  }

  async sendCaptureEvents(payload: unknown): Promise<BatchResponse> {
    return this.request<BatchResponse>('/api/v1/capture-events/batch', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  }

  async initAttachment(payload: unknown): Promise<{
    attachment_id: string;
    state: string;
    upload_url: string | null;
    upload_headers: Record<string, string>;
    s3_key: string | null;
    expires_at: string | null;
    duplicate: boolean;
  }> {
    return this.request('/api/v1/attachments/init', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  }

  async completeAttachment(payload: unknown): Promise<Record<string, unknown>> {
    return this.request('/api/v1/attachments/complete', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  }

  async sendFeedback(payload: unknown): Promise<Record<string, unknown>> {
    return this.request('/api/v1/feedback', { method: 'POST', body: JSON.stringify(payload) });
  }

  async syncStatus(): Promise<{
    archived_conversation_count: number;
    archived_message_count: number;
    known_conversation_ids: string[];
    last_sync_at: string | null;
    queue_depth: number;
    backpressure: boolean;
    capture_enabled: boolean;
    kill_switch: boolean;
    coverage_statement: string;
  }> {
    return this.request('/api/v1/sync/status', { method: 'GET' });
  }

  /**
   * Upload bytes straight to S3 with the presigned PUT.
   *
   * Deliberately *not* authenticated with the backend token: the presigned URL
   * carries its own short-lived authorization, and sending our bearer token to
   * a storage host would leak it.
   */
  async uploadToStorage(
    url: string,
    headers: Record<string, string>,
    bytes: ArrayBuffer,
  ): Promise<void> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 120_000);
    try {
      const response = await this.fetchImpl(url, {
        method: 'PUT',
        headers,
        body: bytes,
        signal: controller.signal,
        credentials: 'omit',
        mode: 'cors',
      });
      if (!response.ok) {
        throw new ApiError(
          `Upload failed with ${response.status}`,
          response.status,
          'upload_failed',
          RETRYABLE_STATUSES.has(response.status),
        );
      }
    } catch (error) {
      if (error instanceof ApiError) throw error;
      throw new ApiError(`Upload error: ${safeErrorMessage(error)}`, 0, 'upload_error', true);
    } finally {
      clearTimeout(timer);
    }
  }
}
