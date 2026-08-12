#!/usr/bin/env node
/**
 * Manifest V3 validation.
 *
 * Fails the build on anything that would either be rejected by the Chrome Web
 * Store / Enterprise policy, or that would silently widen what the extension
 * can reach. Runs against `public/manifest.json` and, when present, the built
 * `dist/` output so a broken bundle never ships.
 */

import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, '..');

const errors = [];
const warnings = [];

function fail(message) {
  errors.push(message);
}

function warn(message) {
  warnings.push(message);
}

const manifestPath = join(root, 'public', 'manifest.json');
if (!existsSync(manifestPath)) {
  console.error('manifest.json not found at', manifestPath);
  process.exit(1);
}

let manifest;
try {
  manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
} catch (error) {
  console.error('manifest.json is not valid JSON:', error.message);
  process.exit(1);
}

// ---------------------------------------------------------------------------
// Core Manifest V3 requirements
// ---------------------------------------------------------------------------

if (manifest.manifest_version !== 3) fail('manifest_version must be 3');
if (!manifest.name) fail('name is required');
if (!/^\d+(\.\d+){0,3}$/.test(manifest.version ?? '')) {
  fail(`version "${manifest.version}" is not a valid Chrome version string`);
}
if (!manifest.description) fail('description is required');
if (!manifest.background?.service_worker) fail('background.service_worker is required');
if (manifest.background?.type !== 'module') {
  warn('background.type should be "module" for an ES-module service worker');
}
if (!Array.isArray(manifest.content_scripts) || manifest.content_scripts.length === 0) {
  fail('at least one content script is required');
}

// ---------------------------------------------------------------------------
// Least privilege
// ---------------------------------------------------------------------------

const APPROVED_HOSTS = ['https://chatgpt.com/*', 'https://chat.openai.com/*'];
const ALLOWED_PERMISSIONS = new Set(['storage', 'alarms', 'identity']);
const FORBIDDEN_PERMISSIONS = new Set([
  'cookies',
  'webRequest',
  'webRequestBlocking',
  'debugger',
  'history',
  'downloads',
  'clipboardRead',
  'management',
  'proxy',
  'privacy',
  'tabCapture',
  'desktopCapture',
  'nativeMessaging',
  'declarativeNetRequest',
  '<all_urls>',
]);

for (const permission of manifest.permissions ?? []) {
  if (FORBIDDEN_PERMISSIONS.has(permission)) {
    fail(`permission "${permission}" is forbidden by policy (privacy risk)`);
  } else if (!ALLOWED_PERMISSIONS.has(permission)) {
    warn(`permission "${permission}" is not on the reviewed allowlist`);
  }
}

for (const host of manifest.host_permissions ?? []) {
  if (!APPROVED_HOSTS.includes(host)) {
    fail(`host_permission "${host}" is outside the approved ChatGPT origins`);
  }
}

for (const script of manifest.content_scripts ?? []) {
  for (const match of script.matches ?? []) {
    if (!APPROVED_HOSTS.includes(match)) {
      fail(`content script match "${match}" is outside the approved ChatGPT origins`);
    }
  }
  if (script.all_frames === true) {
    fail('content scripts must not run in all frames');
  }
  if (script.world && script.world !== 'ISOLATED') {
    fail('content scripts must run in the ISOLATED world, never MAIN');
  }
}

// ---------------------------------------------------------------------------
// Content Security Policy
// ---------------------------------------------------------------------------

const csp = manifest.content_security_policy?.extension_pages ?? '';
if (!csp.includes("script-src 'self'")) {
  fail("content_security_policy.extension_pages must pin script-src to 'self'");
}
if (/unsafe-eval|unsafe-inline|http:/.test(csp)) {
  fail('content_security_policy must not allow unsafe-eval, unsafe-inline or http:');
}

// ---------------------------------------------------------------------------
// Enterprise policy schema
// ---------------------------------------------------------------------------

if (!manifest.storage?.managed_schema) {
  fail('storage.managed_schema is required for Chrome Enterprise policy delivery');
} else {
  const schemaPath = join(root, 'public', manifest.storage.managed_schema);
  if (!existsSync(schemaPath)) {
    fail(`managed schema ${manifest.storage.managed_schema} is missing`);
  } else {
    const schema = JSON.parse(readFileSync(schemaPath, 'utf8'));
    if (schema.type !== 'object') fail('managed schema must describe an object');
    if (!schema.properties?.apiBaseUrl) {
      fail('managed schema must expose apiBaseUrl so IT can point at the backend');
    }
  }
}

// ---------------------------------------------------------------------------
// Icons
// ---------------------------------------------------------------------------

for (const [size, path] of Object.entries(manifest.icons ?? {})) {
  if (!existsSync(join(root, 'public', path))) {
    fail(`icon ${size} is missing at public/${path}`);
  }
}

// ---------------------------------------------------------------------------
// Built output (when present)
// ---------------------------------------------------------------------------

const dist = join(root, 'dist');
if (existsSync(dist)) {
  const required = [
    manifest.background.service_worker,
    ...(manifest.content_scripts ?? []).flatMap((s) => s.js ?? []),
    manifest.action?.default_popup,
    manifest.options_page,
    'manifest.json',
  ].filter(Boolean);

  for (const file of required) {
    if (!existsSync(join(dist, file))) fail(`built output is missing ${file}`);
  }

  // A content script is a classic script: a static import would throw on
  // injection and capture would silently never start.
  for (const script of (manifest.content_scripts ?? []).flatMap((s) => s.js ?? [])) {
    const file = join(dist, script);
    if (!existsSync(file)) continue;
    const source = readFileSync(file, 'utf8');
    if (/(^|[\s;])import\s*[{*"']/.test(source) || /\bfrom\s*["']\.\//.test(source)) {
      fail(`${script} contains an ES import; content scripts must be self-contained`);
    }
    if (/\beval\s*\(/.test(source) || /new Function\s*\(/.test(source)) {
      fail(`${script} contains eval/new Function, which the CSP forbids`);
    }
  }

  const stray = readdirSync(dist).filter((name) => name.endsWith('.map'));
  if (stray.length > 0) warn(`source maps present in dist: ${stray.join(', ')}`);
}

// ---------------------------------------------------------------------------
// Report
// ---------------------------------------------------------------------------

for (const message of warnings) console.warn(`  warning: ${message}`);
for (const message of errors) console.error(`  error:   ${message}`);

if (errors.length > 0) {
  console.error(`\nmanifest validation failed with ${errors.length} error(s)`);
  process.exit(1);
}
console.log(
  `manifest v3 validation passed (${warnings.length} warning(s)) - ` +
    `${manifest.name} ${manifest.version}`,
);
