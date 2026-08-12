/** Small presentational pieces shared by the popup and options pages. */

import React from 'react';
import type { ArchiveStatus } from '../shared/types';

export const palette = {
  bg: '#0f172a',
  panel: '#111c33',
  text: '#e5edff',
  muted: '#94a3b8',
  ok: '#34d399',
  warn: '#fbbf24',
  bad: '#f87171',
  accent: '#60a5fa',
  border: '#1e2b47',
} as const;

export function Pill({
  tone,
  children,
}: {
  tone: 'ok' | 'warn' | 'bad' | 'muted';
  children: React.ReactNode;
}): JSX.Element {
  const color = tone === 'muted' ? palette.muted : palette[tone];
  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 6,
        padding: '2px 8px',
        borderRadius: 999,
        fontSize: 11,
        fontWeight: 600,
        color,
        border: `1px solid ${color}44`,
        background: `${color}14`,
      }}
    >
      <span
        aria-hidden
        style={{ width: 6, height: 6, borderRadius: 999, background: color }}
      />
      {children}
    </span>
  );
}

export function Row({
  label,
  value,
  title,
}: {
  label: string;
  value: React.ReactNode;
  title?: string;
}): JSX.Element {
  return (
    <div
      title={title}
      style={{
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
        gap: 12,
        padding: '7px 0',
        borderBottom: `1px solid ${palette.border}`,
      }}
    >
      <span style={{ color: palette.muted, fontSize: 12 }}>{label}</span>
      <span style={{ fontSize: 12, textAlign: 'right', wordBreak: 'break-word' }}>{value}</span>
    </div>
  );
}

export function Button({
  onClick,
  children,
  variant = 'primary',
  disabled,
}: {
  onClick: () => void;
  children: React.ReactNode;
  variant?: 'primary' | 'ghost';
  disabled?: boolean;
}): JSX.Element {
  const primary = variant === 'primary';
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      style={{
        flex: 1,
        padding: '9px 12px',
        borderRadius: 8,
        border: `1px solid ${primary ? palette.accent : palette.border}`,
        background: primary ? `${palette.accent}22` : 'transparent',
        color: disabled ? palette.muted : palette.text,
        fontSize: 12,
        fontWeight: 600,
        cursor: disabled ? 'not-allowed' : 'pointer',
        opacity: disabled ? 0.6 : 1,
      }}
    >
      {children}
    </button>
  );
}

export function formatTime(value: number | null): string {
  if (!value) return 'never';
  const delta = Date.now() - value;
  if (delta < 60_000) return 'just now';
  if (delta < 3_600_000) return `${Math.floor(delta / 60_000)} min ago`;
  if (delta < 86_400_000) return `${Math.floor(delta / 3_600_000)} h ago`;
  return new Date(value).toLocaleString();
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function captureTone(status: ArchiveStatus): 'ok' | 'warn' | 'bad' {
  if (status.killSwitch) return 'bad';
  if (!status.captureActive) return 'warn';
  return status.workspaceVerified ? 'ok' : 'warn';
}

export async function sendMessage<T = unknown>(message: unknown): Promise<{
  ok: boolean;
  data?: T;
  error?: string;
}> {
  try {
    return (await chrome.runtime.sendMessage(message)) as { ok: boolean; data?: T; error?: string };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : 'Extension error' };
  }
}
