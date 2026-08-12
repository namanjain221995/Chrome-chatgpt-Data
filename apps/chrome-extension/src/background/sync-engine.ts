/**
 * Sync engine: the only component that talks to the backend.
 *
 * Everything captured in the page is enqueued first and uploaded second, so a
 * closed laptop lid, a dropped VPN or an evicted service worker cannot lose a
 * message. Failures are retried with jittered backoff; terminal rejections are
 * dropped rather than retried forever.
 */

import type {
  BatchResponse,
  CaptureCompleteness,
  ClientContext,
  MessagePayload,
  QueueItem,
  RuntimeConfig,
  WorkspaceRef,
} from '../shared/types';
import { log, safeErrorMessage } from '../shared/logging';
import { ApiClient, ApiError } from '../modules/api-client';
import { OfflineQueue } from '../modules/offline-queue';
import { chunk, clientContext, idempotencyKey } from '../shared/util';

export interface SyncDeps {
  api: ApiClient;
  queue: OfflineQueue;
  config: () => RuntimeConfig | null;
  extensionVersion: string;
  deviceFingerprint: () => Promise<string | null>;
}

export interface FlushResult {
  attempted: number;
  succeeded: number;
  retried: number;
  dropped: number;
  backpressure: boolean;
}

const DEFAULT_FLUSH_LIMIT = 25;

export class SyncEngine {
  constructor(private readonly deps: SyncDeps) {}

  private async context(): Promise<ClientContext> {
    return clientContext(this.deps.extensionVersion, await this.deps.deviceFingerprint());
  }

  async enqueueConversation(
    workspace: WorkspaceRef,
    conversation: {
      sourceConversationId: string;
      title: string | null;
      url: string;
      modelSlug: string | null;
      messageCount: number;
    },
    completeness: CaptureCompleteness,
  ): Promise<void> {
    const key = await idempotencyKey([
      'conv',
      conversation.sourceConversationId,
      completeness,
      conversation.messageCount,
      conversation.title ?? '',
    ]);
    const payload = {
      idempotency_key: key,
      source_conversation_id: conversation.sourceConversationId,
      source_url: conversation.url.slice(0, 2048),
      title: conversation.title,
      model_slug: conversation.modelSlug,
      workspace,
      capture_completeness: completeness,
      capture_source: 'chrome_extension' as const,
      observed_message_count: conversation.messageCount,
      client: await this.context(),
    };
    await this.deps.queue.enqueue('conversation_upsert', payload, key);
  }

  async enqueueMessages(workspace: WorkspaceRef, messages: MessagePayload[]): Promise<number> {
    if (messages.length === 0) return 0;
    const limits = this.deps.config()?.limits;
    const batchSize = Math.max(1, limits?.max_batch_items ?? 100);
    const context = await this.context();

    let queued = 0;
    for (const batch of chunk(messages, batchSize)) {
      const key = await idempotencyKey([
        'batch',
        batch[0]?.source_conversation_id ?? '',
        ...batch.map((m) => m.idempotency_key),
      ]);
      const created = await this.deps.queue.enqueue(
        'message_batch',
        { workspace, messages: batch, client: context },
        key,
      );
      if (created) queued += batch.length;
    }
    return queued;
  }

  async enqueueAttachmentInit(payload: Record<string, unknown>, key: string): Promise<void> {
    await this.deps.queue.enqueue('attachment_init', payload, key);
  }

  /** Send everything that is due. Never throws; failures are recorded. */
  async flush(limit = DEFAULT_FLUSH_LIMIT): Promise<FlushResult> {
    const result: FlushResult = {
      attempted: 0,
      succeeded: 0,
      retried: 0,
      dropped: 0,
      backpressure: false,
    };
    const due = await this.deps.queue.takeDue(limit);
    for (const item of due) {
      result.attempted += 1;
      try {
        const response = await this.dispatch(item);
        await this.deps.queue.remove(item.id);
        result.succeeded += 1;
        if (response?.backpressure) result.backpressure = true;
      } catch (error) {
        const apiError = error instanceof ApiError ? error : null;
        const retryable = apiError ? apiError.retryable : true;
        if (!retryable) {
          await this.deps.queue.remove(item.id);
          result.dropped += 1;
          log.warn('sync_item_rejected', {
            kind: item.kind,
            code: apiError?.code,
            status: apiError?.status,
          });
          continue;
        }
        const outcome = await this.deps.queue.reschedule(item, safeErrorMessage(error));
        if (outcome === 'dropped') result.dropped += 1;
        else result.retried += 1;
        if (apiError?.code === 'backpressure') result.backpressure = true;
      }
    }
    return result;
  }

  private async dispatch(item: QueueItem): Promise<BatchResponse | null> {
    switch (item.kind) {
      case 'conversation_upsert':
        await this.deps.api.upsertConversation(item.payload);
        return null;
      case 'message_batch':
        return this.deps.api.sendMessages(item.payload);
      case 'capture_event_batch':
        return this.deps.api.sendCaptureEvents(item.payload);
      case 'attachment_init':
        await this.deps.api.initAttachment(item.payload);
        return null;
      case 'attachment_complete':
        await this.deps.api.completeAttachment(item.payload);
        return null;
      case 'feedback':
        await this.deps.api.sendFeedback(item.payload);
        return null;
      default:
        throw new ApiError(`Unknown queue item kind`, 0, 'unknown_kind', false);
    }
  }

  /**
   * Attachment upload: metadata -> presigned PUT -> direct upload -> complete.
   *
   * The bytes go straight to S3; they never pass through the backend, and the
   * backend only acknowledges after it has verified size and checksum.
   */
  async uploadAttachment(params: {
    workspaceReady: boolean;
    sourceConversationId: string;
    sourceMessageId: string | null;
    clientAttachmentId: string;
    filename: string;
    mimeType: string;
    byteSize: number;
    sha256: string;
    bytes: ArrayBuffer;
  }): Promise<'uploaded' | 'duplicate' | 'metadata_only' | 'failed'> {
    if (!params.workspaceReady) return 'failed';
    const context = await this.context();
    try {
      const init = await this.deps.api.initAttachment({
        client_attachment_id: params.clientAttachmentId,
        source_conversation_id: params.sourceConversationId,
        source_message_id: params.sourceMessageId,
        filename: params.filename,
        mime_type: params.mimeType,
        byte_size: params.byteSize,
        sha256: params.sha256,
        relation: 'uploaded_by_user',
        metadata_only: false,
        client: context,
      });

      if (init.duplicate) return 'duplicate';
      if (!init.upload_url) return 'metadata_only';

      await this.deps.api.uploadToStorage(init.upload_url, init.upload_headers, params.bytes);
      await this.deps.api.completeAttachment({
        attachment_id: init.attachment_id,
        sha256: params.sha256,
        byte_size: params.byteSize,
        source_message_id: params.sourceMessageId,
        client: context,
      });
      return 'uploaded';
    } catch (error) {
      log.warn('attachment_upload_failed', { reason: safeErrorMessage(error) });
      return 'failed';
    }
  }

  /** Record metadata for a file whose bytes the page never exposed. */
  async recordMetadataOnlyAttachment(params: {
    sourceConversationId: string;
    sourceMessageId: string | null;
    clientAttachmentId: string;
    filename: string;
    mimeType: string | null;
    byteSize: number | null;
    relation: 'generated_by_assistant' | 'referenced_historical';
    sourceFileId: string | null;
  }): Promise<void> {
    const key = await idempotencyKey(['att-meta', params.clientAttachmentId]);
    const digest = await idempotencyKey(['placeholder', params.clientAttachmentId]);
    await this.enqueueAttachmentInit(
      {
        client_attachment_id: params.clientAttachmentId,
        source_conversation_id: params.sourceConversationId,
        source_message_id: params.sourceMessageId,
        filename: params.filename,
        mime_type: params.mimeType ?? 'application/octet-stream',
        byte_size: Math.max(1, params.byteSize ?? 1),
        // A metadata-only record still needs a syntactically valid digest; it
        // is never used to verify bytes because no bytes are uploaded.
        sha256: digest.replace(/[^0-9a-f]/g, '0').padEnd(64, '0').slice(0, 64),
        relation: params.relation,
        metadata_only: true,
        source_file_id: params.sourceFileId,
        client: await this.context(),
      },
      key,
    );
  }
}
