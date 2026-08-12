/**
 * Options / status page.
 *
 * Two jobs:
 *   1. Historical Archive Progress — an honest account of what this browser has
 *      archived, and an explicit statement of what it has *not*.
 *   2. Support diagnostics — versions, policy gates, queue state and recent
 *      event names. No message content ever appears here.
 */

import { useCallback, useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import type { ArchiveStatus, RuntimeConfig } from '../shared/types';
import { Button, Pill, Row, formatBytes, formatTime, palette, sendMessage } from './shared';

function Section({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
}): JSX.Element {
  return (
    <section
      style={{
        background: palette.panel,
        border: `1px solid ${palette.border}`,
        borderRadius: 12,
        padding: 16,
        marginBottom: 16,
      }}
    >
      <h2 style={{ fontSize: 14, margin: 0 }}>{title}</h2>
      {subtitle && (
        <p style={{ margin: '4px 0 12px', fontSize: 12, color: palette.muted }}>{subtitle}</p>
      )}
      {children}
    </section>
  );
}

function Options(): JSX.Element {
  const [status, setStatus] = useState<ArchiveStatus | null>(null);
  const [config, setConfig] = useState<RuntimeConfig | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const [statusResponse, configResponse] = await Promise.all([
      sendMessage<ArchiveStatus>({ type: 'GET_STATUS' }),
      sendMessage<RuntimeConfig>({ type: 'GET_CONFIG' }),
    ]);
    if (statusResponse.ok && statusResponse.data) setStatus(statusResponse.data);
    if (configResponse.ok && configResponse.data) setConfig(configResponse.data);
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  if (!status) {
    return <div style={{ padding: 24, color: palette.muted }}>Loading…</div>;
  }

  const policy = config?.policy;

  return (
    <main
      style={{
        maxWidth: 720,
        margin: '0 auto',
        padding: 24,
        background: palette.bg,
        color: palette.text,
        font: '13px/1.5 system-ui, -apple-system, sans-serif',
        minHeight: '100vh',
      }}
    >
      <h1 style={{ fontSize: 18, marginTop: 0 }}>TechSara ChatGPT Archive</h1>

      <Section
        title="Historical Archive Progress"
        subtitle="What this browser profile has archived so far."
      >
        <Row
          label="Conversations archived from this browser"
          value={String(status.archivedConversationIds.length)}
        />
        <Row label="Conversations recorded by the service" value={String(status.archivedConversationCount)} />
        <Row label="Messages recorded by the service" value={String(status.archivedMessageCount)} />
        <Row label="Last successful sync" value={formatTime(status.lastSyncAt)} />
        <div
          style={{
            marginTop: 12,
            padding: 12,
            borderRadius: 8,
            border: `1px solid ${palette.warn}44`,
            background: `${palette.warn}10`,
            fontSize: 12,
          }}
        >
          <strong style={{ color: palette.warn }}>What this does not cover.</strong>
          <ul style={{ margin: '8px 0 0', paddingLeft: 18, color: palette.muted }}>
            <li>Conversations you have never opened in this browser are not archived here.</li>
            <li>Conversations from other devices or browsers are not included.</li>
            <li>Text you typed but never sent is never captured.</li>
            <li>Hidden model reasoning is never captured.</li>
            <li>
              Files shown in older conversations may only exist as metadata: the page does not
              always expose the original bytes.
            </li>
          </ul>
          <p style={{ margin: '8px 0 0', color: palette.muted }}>
            To extend coverage, open older company conversations — each one is archived when you
            open it. Company-wide coverage requires the authorized enterprise compliance feed,
            which your administrator configures on the server.
          </p>
        </div>
      </Section>

      <Section title="Policy" subtitle="Every switch below is decided by the server.">
        <Row
          label="Browser content capture enabled"
          value={
            policy?.browser_content_capture_enabled ? (
              <Pill tone="ok">yes</Pill>
            ) : (
              <Pill tone="warn">no</Pill>
            )
          }
        />
        <Row
          label="Written authorization confirmed"
          value={
            policy?.openai_written_authorization_confirmed ? (
              <Pill tone="ok">yes</Pill>
            ) : (
              <Pill tone="warn">no</Pill>
            )
          }
        />
        <Row
          label="Archiving active"
          value={policy?.capture_active ? <Pill tone="ok">yes</Pill> : <Pill tone="warn">no</Pill>}
        />
        <Row
          label="Administrator kill switch"
          value={policy?.kill_switch ? <Pill tone="bad">engaged</Pill> : <Pill tone="ok">off</Pill>}
        />
        <Row label="Personal workspace capture" value={<Pill tone="ok">never</Pill>} />
        <Row label="Unsent draft capture" value={<Pill tone="ok">never</Pill>} />
        <Row label="Configuration version" value={String(status.configVersion ?? 'unknown')} />
      </Section>

      <Section title="Support diagnostics" subtitle="Safe to share with IT. Contains no message content.">
        <Row label="Extension version" value={chrome.runtime.getManifest().version} />
        <Row label="Extension id" value={<code style={{ fontSize: 11 }}>{chrome.runtime.id}</code>} />
        <Row label="Archive service" value={config?.api_base_url ?? 'not configured'} />
        <Row
          label="Backend reachable"
          value={status.backendHealthy ? <Pill tone="ok">yes</Pill> : <Pill tone="bad">no</Pill>}
        />
        <Row
          label="Offline queue"
          value={`${status.queueSize} items (${formatBytes(status.queueBytes)})`}
        />
        <Row label="Last sync error" value={status.lastSyncError ?? 'none'} />
        <div style={{ display: 'flex', gap: 8, marginTop: 12 }}>
          <Button
            onClick={() =>
              void sendMessage({ type: 'REFRESH_CONFIG' }).then(() => {
                setNotice('Configuration refreshed.');
                return refresh();
              })
            }
          >
            Refresh configuration
          </Button>
          <Button
            variant="ghost"
            onClick={() =>
              void sendMessage({ type: 'FLUSH_QUEUE' }).then(() => {
                setNotice('Upload attempted.');
                return refresh();
              })
            }
          >
            Upload queued items
          </Button>
          <Button
            variant="ghost"
            onClick={() =>
              void sendMessage({ type: 'SIGN_OUT' }).then(() => {
                setNotice('Signed out of this browser.');
                return refresh();
              })
            }
          >
            Sign out
          </Button>
        </div>
        {notice && (
          <p style={{ marginTop: 10, fontSize: 12, color: palette.accent }} role="status">
            {notice}
          </p>
        )}
      </Section>

      <Section title="Privacy" subtitle="What this extension will never do.">
        <ul style={{ margin: 0, paddingLeft: 18, color: palette.muted, fontSize: 12 }}>
          <li>It never reads what you type before you send it.</li>
          <li>It never reads cookies or ChatGPT session tokens.</li>
          <li>It never archives personal-workspace conversations.</li>
          <li>It never sends or edits messages on your behalf.</li>
          <li>It only runs on approved company ChatGPT addresses.</li>
        </ul>
        {status.privacyNoticeUrl && (
          <p style={{ marginTop: 12 }}>
            <a
              href={status.privacyNoticeUrl}
              target="_blank"
              rel="noopener noreferrer"
              style={{ color: palette.accent }}
            >
              Read the full employee privacy notice
            </a>
          </p>
        )}
      </Section>
    </main>
  );
}

const container = document.getElementById('root');
if (container) createRoot(container).render(<Options />);
