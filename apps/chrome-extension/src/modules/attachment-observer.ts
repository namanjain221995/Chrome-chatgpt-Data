/**
 * Attachment observer.
 *
 * Captures ONLY the File/Blob objects the employee explicitly hands to ChatGPT
 * through a file input, a paste or a drop. It never reads the filesystem, never
 * fetches a URL to reconstruct a file, and never touches historical attachments
 * whose bytes the page does not expose.
 *
 * The bytes are hashed here (Web Crypto) and handed to the service worker,
 * which asks the backend for a short-lived presigned S3 PUT. No AWS credential
 * ever exists in the page or the extension.
 */

import type { AttachmentRef } from '../shared/types';
import { log, safeErrorMessage } from '../shared/logging';
import { randomId, sha256Hex } from '../shared/util';

export interface CapturedAttachment {
  ref: AttachmentRef;
  bytes: ArrayBuffer;
  sha256: string;
  source: 'input' | 'paste' | 'drop';
}

export type AttachmentListener = (attachment: CapturedAttachment) => void | Promise<void>;

export interface AttachmentObserverOptions {
  doc?: Document;
  maxBytes?: number;
  allowedMimeTypes?: string[];
  conversationId?: () => string | null;
}

const DEFAULT_MAX_BYTES = 20 * 1024 * 1024;

export class AttachmentObserver {
  private readonly doc: Document;
  private readonly listeners = new Set<AttachmentListener>();
  private readonly seen = new Set<string>();
  private readonly cancelled = new Set<string>();
  private running = false;
  private maxBytes: number;
  private allowedMimeTypes: Set<string> | null;

  private readonly onChange = (event: Event): void => {
    const target = event.target as HTMLInputElement | null;
    if (!target || target.type !== 'file' || !target.files) return;
    void this.handleFiles(Array.from(target.files), 'input');
  };

  private readonly onPaste = (event: ClipboardEvent): void => {
    const items = event.clipboardData?.items;
    if (!items) return;
    const files: File[] = [];
    for (const item of Array.from(items)) {
      // Only real file entries; plain-text clipboard content is ignored so a
      // pasted password or draft can never become an "attachment".
      if (item.kind !== 'file') continue;
      const file = item.getAsFile();
      if (file) files.push(file);
    }
    if (files.length > 0) void this.handleFiles(files, 'paste');
  };

  private readonly onDrop = (event: DragEvent): void => {
    const files = event.dataTransfer?.files;
    if (!files || files.length === 0) return;
    void this.handleFiles(Array.from(files), 'drop');
  };

  constructor(private readonly options: AttachmentObserverOptions = {}) {
    this.doc = options.doc ?? document;
    this.maxBytes = options.maxBytes ?? DEFAULT_MAX_BYTES;
    this.allowedMimeTypes = options.allowedMimeTypes
      ? new Set(options.allowedMimeTypes.map((t) => t.toLowerCase()))
      : null;
  }

  configure(limits: { maxBytes?: number; allowedMimeTypes?: string[] }): void {
    if (limits.maxBytes) this.maxBytes = limits.maxBytes;
    if (limits.allowedMimeTypes) {
      this.allowedMimeTypes = new Set(limits.allowedMimeTypes.map((t) => t.toLowerCase()));
    }
  }

  onAttachment(listener: AttachmentListener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  start(): void {
    if (this.running) return;
    this.running = true;
    // Capture phase so the events are seen even if the app stops propagation.
    this.doc.addEventListener('change', this.onChange, true);
    this.doc.addEventListener('paste', this.onPaste as EventListener, true);
    this.doc.addEventListener('drop', this.onDrop as EventListener, true);
  }

  stop(): void {
    if (!this.running) return;
    this.running = false;
    this.doc.removeEventListener('change', this.onChange, true);
    this.doc.removeEventListener('paste', this.onPaste as EventListener, true);
    this.doc.removeEventListener('drop', this.onDrop as EventListener, true);
    this.seen.clear();
  }

  /** Cancel an in-flight attachment (employee removed it before sending). */
  cancel(clientAttachmentId: string): void {
    this.cancelled.add(clientAttachmentId);
  }

  isCancelled(clientAttachmentId: string): boolean {
    return this.cancelled.has(clientAttachmentId);
  }

  private async handleFiles(files: File[], source: CapturedAttachment['source']): Promise<void> {
    for (const file of files) {
      try {
        await this.handleFile(file, source);
      } catch (error) {
        log.warn('attachment_capture_failed', {
          source,
          reason: safeErrorMessage(error),
          size: file.size,
        });
      }
    }
  }

  private async handleFile(file: File, source: CapturedAttachment['source']): Promise<void> {
    if (file.size <= 0) return;
    if (file.size > this.maxBytes) {
      log.warn('attachment_too_large', { size: file.size, limit: this.maxBytes });
      return;
    }
    const mime = (file.type || 'application/octet-stream').split(';')[0]?.toLowerCase() ?? '';
    if (this.allowedMimeTypes && !this.allowedMimeTypes.has(mime)) {
      log.info('attachment_type_not_allowed', { mime });
      return;
    }

    const bytes = await file.arrayBuffer();
    const digest = await sha256Hex(bytes);
    const conversationId = this.options.conversationId?.() ?? 'unknown';

    // Same bytes + same name in the same conversation = the same attachment,
    // so a re-render or duplicate event does not upload twice.
    const dedupeKey = `${conversationId}:${digest}:${file.name}`;
    if (this.seen.has(dedupeKey)) return;
    this.seen.add(dedupeKey);

    const ref: AttachmentRef = {
      clientAttachmentId: `att-${digest.slice(0, 24)}-${randomId().slice(0, 8)}`,
      filename: file.name || 'attachment.bin',
      mimeType: mime,
      byteSize: file.size,
      metadataOnly: false,
      sourceFileId: null,
      relation: 'uploaded_by_user',
    };

    const captured: CapturedAttachment = { ref, bytes, sha256: digest, source };
    for (const listener of this.listeners) {
      if (this.cancelled.has(ref.clientAttachmentId)) return;
      await listener(captured);
    }
  }
}
