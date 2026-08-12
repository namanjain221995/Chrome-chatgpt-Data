/**
 * Company Google Workspace / OIDC sign-in.
 *
 * Authorization Code with PKCE via `chrome.identity.launchWebAuthFlow`:
 *   * `code_verifier` is random per attempt and never leaves the extension;
 *   * `state` and `nonce` are generated here and re-checked on return;
 *   * the authorization code is exchanged by the *backend*, which validates the
 *     ID token signature, issuer, audience, expiry, nonce and hosted domain;
 *   * tokens live in `chrome.storage.session` (memory-backed, cleared on
 *     browser exit) and never in page `localStorage`.
 */

import { log, safeErrorMessage } from '../shared/logging';

const SESSION_KEY = 'authSession';
const PKCE_KEY = 'pkceState';
const SCOPES = 'openid email profile';

export interface AuthSession {
  accessToken: string;
  refreshToken: string;
  expiresAt: number;
  refreshExpiresAt: number;
  email: string;
  userId: string;
  organizationId: string;
  deviceId: string | null;
  roles: string[];
  noticeAcknowledged: boolean;
}

export interface PkceState {
  verifier: string;
  state: string;
  nonce: string;
  createdAt: number;
}

function base64UrlEncode(bytes: ArrayBuffer): string {
  const binary = String.fromCharCode(...new Uint8Array(bytes));
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function randomVerifier(byteLength = 48): string {
  const bytes = crypto.getRandomValues(new Uint8Array(byteLength));
  return base64UrlEncode(bytes.buffer as ArrayBuffer);
}

export async function pkceChallenge(verifier: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier));
  return base64UrlEncode(digest);
}

export async function readSession(): Promise<AuthSession | null> {
  try {
    const stored = await chrome.storage.session.get(SESSION_KEY);
    return (stored?.[SESSION_KEY] as AuthSession | undefined) ?? null;
  } catch {
    return null;
  }
}

export async function writeSession(session: AuthSession): Promise<void> {
  await chrome.storage.session.set({ [SESSION_KEY]: session });
}

export async function clearSession(): Promise<void> {
  await chrome.storage.session.remove([SESSION_KEY, PKCE_KEY]);
}

export function sessionValid(session: AuthSession | null, now = Date.now()): boolean {
  // 60 s of slack so a request in flight does not expire mid-call.
  return Boolean(session && session.expiresAt - 60_000 > now);
}

export function refreshUsable(session: AuthSession | null, now = Date.now()): boolean {
  return Boolean(session?.refreshToken && session.refreshExpiresAt > now);
}

export interface AuthorizeOptions {
  clientId: string;
  redirectUri: string;
  hostedDomain?: string | null;
  authorizationEndpoint?: string;
  loginHint?: string;
}

const GOOGLE_AUTH_ENDPOINT = 'https://accounts.google.com/o/oauth2/v2/auth';

export async function buildAuthorizationUrl(
  options: AuthorizeOptions,
): Promise<{ url: string; pkce: PkceState }> {
  const verifier = randomVerifier();
  const challenge = await pkceChallenge(verifier);
  const pkce: PkceState = {
    verifier,
    state: randomVerifier(24),
    nonce: randomVerifier(24),
    createdAt: Date.now(),
  };

  const url = new URL(options.authorizationEndpoint ?? GOOGLE_AUTH_ENDPOINT);
  url.searchParams.set('client_id', options.clientId);
  url.searchParams.set('redirect_uri', options.redirectUri);
  url.searchParams.set('response_type', 'code');
  url.searchParams.set('scope', SCOPES);
  url.searchParams.set('code_challenge', challenge);
  url.searchParams.set('code_challenge_method', 'S256');
  url.searchParams.set('state', pkce.state);
  url.searchParams.set('nonce', pkce.nonce);
  url.searchParams.set('prompt', 'select_account');
  if (options.hostedDomain) url.searchParams.set('hd', options.hostedDomain);
  if (options.loginHint) url.searchParams.set('login_hint', options.loginHint);

  await chrome.storage.session.set({ [PKCE_KEY]: pkce });
  return { url: url.toString(), pkce };
}

export interface AuthorizationResult {
  code: string;
  state: string;
}

/** Parse the provider redirect, rejecting anything that does not match state. */
export async function parseRedirect(redirectUrl: string): Promise<AuthorizationResult> {
  const url = new URL(redirectUrl);
  const params = url.searchParams.size > 0 ? url.searchParams : new URLSearchParams(url.hash.slice(1));

  const error = params.get('error');
  if (error) throw new Error(`Sign-in was refused (${error})`);

  const code = params.get('code');
  const state = params.get('state');
  if (!code || !state) throw new Error('Sign-in response was incomplete');

  const stored = await chrome.storage.session.get(PKCE_KEY);
  const pkce = stored?.[PKCE_KEY] as PkceState | undefined;
  if (!pkce) throw new Error('Sign-in state is missing; please try again');
  if (pkce.state !== state) throw new Error('Sign-in state mismatch; the attempt was discarded');
  if (Date.now() - pkce.createdAt > 10 * 60 * 1000) {
    throw new Error('Sign-in took too long; please try again');
  }
  return { code, state };
}

export async function readPkce(): Promise<PkceState | null> {
  const stored = await chrome.storage.session.get(PKCE_KEY);
  return (stored?.[PKCE_KEY] as PkceState | undefined) ?? null;
}

export async function clearPkce(): Promise<void> {
  await chrome.storage.session.remove(PKCE_KEY);
}

export function redirectUri(): string {
  return chrome.identity.getRedirectURL('oidc');
}

/** Launch the interactive flow and return the provider redirect URL. */
export async function launchWebAuthFlow(url: string): Promise<string> {
  const result = await chrome.identity.launchWebAuthFlow({ url, interactive: true });
  if (!result) throw new Error('Sign-in was cancelled');
  return result;
}

export function describeAuthError(error: unknown): string {
  const message = safeErrorMessage(error);
  log.warn('auth_failed', { reason: message });
  if (/cancel/i.test(message)) return 'Sign-in was cancelled.';
  if (/domain|allowlist|hosted/i.test(message)) {
    return 'Use your company Google account to sign in.';
  }
  return 'Sign-in failed. Please try again or contact IT support.';
}
