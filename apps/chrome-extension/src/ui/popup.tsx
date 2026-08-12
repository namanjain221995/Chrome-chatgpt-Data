/**
 * Popup: login status, verified workspace, current conversation, archive
 * status, last sync, offline queue size, backend health, privacy notice and the
 * "Archive current conversation now" action.
 *
 * The wording here is deliberately conservative: it never claims that the whole
 * workspace history has been archived.
 */

import { useCallback, useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import type { ArchiveStatus } from '../shared/types';
import {
  Button,
  Pill,
  Row,
  captureTone,
  formatBytes,
  formatTime,
  palette,
  sendMessage,
} from './shared';

function StatusPanel({ status }: { status: ArchiveStatus }): JSX.Element {
  const tone = captureTone(status);
  return (
    <div>
      <Row
        label="Signed in"
        value={
          status.signedIn ? (
            <Pill tone="ok">{status.email ?? 'yes'}</Pill>
          ) : (
            <Pill tone="warn">not signed in</Pill>
          )
        }
      />
      <Row
        label="Company workspace"
        value={
          status.workspaceVerified ? (
            <Pill tone="ok">{status.workspaceLabel ?? 'verified'}</Pill>
          ) : (
            <Pill tone="muted">not verified here</Pill>
          )
        }
      />
      <Row
        label="Archiving"
        value={
          <Pill tone={tone}>
            {status.killSwitch
              ? 'paused by admin'
              : status.captureActive
                ? 'active'
                : 'not enabled'}
          </Pill>
        }
      />
      <Row
        label="Current conversation"
        value={
          status.currentConversationId ? (
            <code style={{ fontSize: 11 }}>{status.currentConversationId.slice(0, 18)}…</code>
          ) : (
            <span style={{ color: palette.muted }}>none open</span>
          )
        }
      />
      <Row label="Last successful sync" value={formatTime(status.lastSyncAt)} />
      <Row
        label="Waiting to upload"
        value={
          status.queueSize === 0
            ? 'nothing queued'
            : `${status.queueSize} item${status.queueSize === 1 ? '' : 's'} (${formatBytes(status.queueBytes)})`
        }
      />
      <Row
        label="Archive service"
        value={
          status.backendHealthy ? (
            <Pill tone="ok">reachable</Pill>
          ) : (
            <Pill tone="bad">unreachable</Pill>
          )
        }
      />
      <Row
        label="Archived from this browser"
        value={`${status.archivedConversationCount} conversation${
          status.archivedConversationCount === 1 ? '' : 's'
        }`}
        title="Conversations this browser profile has archived. Other devices and unopened conversations are not included."
      />
    </div>
  );
}

function Popup(): JSX.Element {
  const [status, setStatus] = useState<ArchiveStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const response = await sendMessage<ArchiveStatus>({ type: 'GET_STATUS' });
    if (response.ok && response.data) setStatus(response.data);
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 4000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const run = useCallback(
    async (message: unknown, successText: string) => {
      setBusy(true);
      setNotice(null);
      const response = await sendMessage(message);
      setNotice(response.ok ? successText : (response.error ?? 'Something went wrong.'));
      await refresh();
      setBusy(false);
    },
    [refresh],
  );

  if (!status) {
    return (
      <div style={{ padding: 16, color: palette.muted, font: '13px system-ui' }}>Loading…</div>
    );
  }

  return (
    <div
      style={{
        width: 340,
        padding: 16,
        background: palette.bg,
        color: palette.text,
        font: '13px/1.45 system-ui, -apple-system, sans-serif',
      }}
    >
      <header style={{ marginBottom: 10 }}>
        <h1 style={{ fontSize: 14, margin: 0 }}>TechSara ChatGPT Archive</h1>
        <p style={{ margin: '4px 0 0', fontSize: 11, color: palette.muted }}>
          Company-managed archive of approved workspace conversations.
        </p>
      </header>

      {status.policyBlockReason && (
        <div
          style={{
            marginBottom: 10,
            padding: '8px 10px',
            borderRadius: 8,
            border: `1px solid ${palette.warn}44`,
            background: `${palette.warn}14`,
            color: palette.warn,
            fontSize: 11,
          }}
        >
          {status.policyBlockReason}
        </div>
      )}

      <StatusPanel status={status} />

      <p
        style={{
          margin: '10px 0',
          fontSize: 11,
          color: palette.muted,
          borderLeft: `2px solid ${palette.border}`,
          paddingLeft: 8,
        }}
      >
        {status.coverageStatement}
      </p>

      <div style={{ display: 'flex', gap: 8, marginBottom: 8 }}>
        {status.signedIn ? (
          <Button
            onClick={() =>
              void run({ type: 'ARCHIVE_CURRENT_CONVERSATION' }, 'Archiving this conversation…')
            }
            disabled={busy || !status.captureActive}
          >
            Archive current conversation now
          </Button>
        ) : (
          <Button onClick={() => void run({ type: 'SIGN_IN' }, 'Signed in.')} disabled={busy}>
            Sign in with company account
          </Button>
        )}
      </div>

      <div style={{ display: 'flex', gap: 8 }}>
        <Button
          variant="ghost"
          onClick={() => void run({ type: 'FLUSH_QUEUE' }, 'Upload attempted.')}
          disabled={busy}
        >
          Upload now
        </Button>
        <Button variant="ghost" onClick={() => chrome.runtime.openOptionsPage()}>
          Details
        </Button>
      </div>

      {notice && (
        <p style={{ marginTop: 10, fontSize: 11, color: palette.accent }} role="status">
          {notice}
        </p>
      )}

      <footer style={{ marginTop: 12, fontSize: 10, color: palette.muted }}>
        {status.privacyNoticeUrl ? (
          <a
            href={status.privacyNoticeUrl}
            target="_blank"
            rel="noopener noreferrer"
            style={{ color: palette.accent }}
          >
            Employee privacy notice
          </a>
        ) : (
          'Employee privacy notice unavailable'
        )}
        {' · Never captures drafts, cookies or personal workspaces.'}
      </footer>
    </div>
  );
}

const container = document.getElementById('root');
if (container) createRoot(container).render(<Popup />);
