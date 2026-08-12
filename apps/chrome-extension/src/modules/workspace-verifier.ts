/**
 * Workspace verification. Fails closed, always.
 *
 * The rules come from the signed server configuration; the extension has no
 * local override. When anything is missing, ambiguous or contradictory the
 * verdict is "not verified" and no content is captured.
 */

import type {
  RuntimeConfig,
  WorkspaceObservation,
  WorkspaceRef,
  WorkspaceRules,
} from '../shared/types';
import { isApprovedUrl } from './dom-adapter';

export type VerificationReason =
  | 'verified_by_id'
  | 'verified_by_label'
  | 'no_config'
  | 'kill_switch'
  | 'capture_gates_closed'
  | 'url_not_approved'
  | 'personal_workspace'
  | 'label_mismatch'
  | 'id_not_allowlisted'
  | 'no_signals'
  | 'rules_unconfigured';

export interface VerificationResult {
  verified: boolean;
  reason: VerificationReason;
  ref: WorkspaceRef;
}

const STRONG_SIGNALS = new Set(['workspace_id_match', 'workspace_label_match']);

function unverifiedRef(observation: WorkspaceObservation, kind: 'personal' | 'unverified'): WorkspaceRef {
  return {
    source_workspace_id: observation.sourceWorkspaceId,
    label: observation.label,
    kind,
    verified: false,
    verification_signals: observation.signals,
  };
}

/**
 * Decide whether the observed page is the managed company workspace.
 *
 * @param observation what the DOM adapter saw
 * @param config the signed server configuration (null = fail closed)
 * @param url the page URL, checked against the approved patterns
 */
export function verifyWorkspace(
  observation: WorkspaceObservation,
  config: RuntimeConfig | null,
  url: string,
): VerificationResult {
  if (!config) {
    return { verified: false, reason: 'no_config', ref: unverifiedRef(observation, 'unverified') };
  }
  if (config.policy.kill_switch) {
    return { verified: false, reason: 'kill_switch', ref: unverifiedRef(observation, 'unverified') };
  }
  if (!config.policy.capture_active) {
    return {
      verified: false,
      reason: 'capture_gates_closed',
      ref: unverifiedRef(observation, 'unverified'),
    };
  }
  if (!isApprovedUrl(url, config.workspace_rules.allowed_url_patterns)) {
    return {
      verified: false,
      reason: 'url_not_approved',
      ref: unverifiedRef(observation, 'unverified'),
    };
  }
  // An explicit personal marker ends the decision immediately.
  if (observation.looksPersonal) {
    return {
      verified: false,
      reason: 'personal_workspace',
      ref: unverifiedRef(observation, 'personal'),
    };
  }

  const rules: WorkspaceRules = config.workspace_rules;
  const allowedIds = rules.managed_workspace_ids.filter(Boolean);
  const configuredLabel = (rules.managed_workspace_label ?? '').trim().toLowerCase();

  if (allowedIds.length === 0 && !configuredLabel) {
    // Nothing to verify against: refuse rather than guess.
    return {
      verified: false,
      reason: 'rules_unconfigured',
      ref: unverifiedRef(observation, 'unverified'),
    };
  }

  const signals = observation.signals.filter((signal) => signal.length > 0);
  const strongSignals = signals.filter((signal) => STRONG_SIGNALS.has(signal));

  if (allowedIds.length > 0) {
    const observedId = observation.sourceWorkspaceId?.trim();
    if (!observedId || !allowedIds.includes(observedId)) {
      return {
        verified: false,
        reason: 'id_not_allowlisted',
        ref: unverifiedRef(observation, 'unverified'),
      };
    }
    return {
      verified: true,
      reason: 'verified_by_id',
      ref: {
        source_workspace_id: observedId,
        label: observation.label,
        kind: 'managed_company',
        verified: true,
        verification_signals: signals,
      },
    };
  }

  const observedLabel = (observation.label ?? '').trim().toLowerCase();
  if (!observedLabel || observedLabel !== configuredLabel) {
    return {
      verified: false,
      reason: 'label_mismatch',
      ref: unverifiedRef(observation, 'unverified'),
    };
  }
  if (strongSignals.length < Math.max(1, rules.min_signals)) {
    return { verified: false, reason: 'no_signals', ref: unverifiedRef(observation, 'unverified') };
  }

  return {
    verified: true,
    reason: 'verified_by_label',
    ref: {
      source_workspace_id: observation.sourceWorkspaceId,
      label: observation.label,
      kind: 'managed_company',
      verified: true,
      verification_signals: signals,
    },
  };
}

export function describeReason(reason: VerificationReason): string {
  switch (reason) {
    case 'verified_by_id':
    case 'verified_by_label':
      return 'Company workspace verified.';
    case 'no_config':
      return 'Waiting for company configuration from the archive service.';
    case 'kill_switch':
      return 'Archiving is paused by your administrator.';
    case 'capture_gates_closed':
      return 'Archiving is not enabled by your administrator yet.';
    case 'url_not_approved':
      return 'This page is not an approved ChatGPT address.';
    case 'personal_workspace':
      return 'Personal workspace detected - nothing is archived here.';
    case 'label_mismatch':
      return 'This is not the managed company workspace - nothing is archived.';
    case 'id_not_allowlisted':
      return 'This workspace is not on the company allowlist - nothing is archived.';
    case 'no_signals':
      return 'The company workspace could not be confirmed - nothing is archived.';
    case 'rules_unconfigured':
      return 'Workspace rules are not configured on the server - nothing is archived.';
    default:
      return 'Workspace not verified - nothing is archived.';
  }
}
