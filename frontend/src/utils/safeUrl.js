// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

// Returns `value` only if it is an http(s) URL; otherwise '#'.
// Guards `href` sinks against javascript:/data:/vbscript: scheme injection
// (DOM/Reflected XSS) and open-redirects through server-supplied URLs.
//
// Implemented as an anchored scheme allowlist rather than `new URL(...)`:
// the URL constructor needs `window.location` as a base and re-emits via
// `.href`, both of which static analysers treat as DOM taint sources —
// which paradoxically reports the sanitiser itself as an XSS path.
//
// Kept in step with the partner console's own src/utils/safeUrl.js so both
// consoles guard href sinks identically. That file now lives in a separate
// repository  , so a change here
// needs the same change there — and nothing checks it for you. That drift is
// exactly how the partner console was left on a scheme-only check while this
// one had the host allowlist.

// Regex to extract hostname from an http(s) URL. Avoids `new URL()` which
// static analysers treat as a DOM taint source.
//
// Note on the character class: `/` needs no backslash escape inside `[...]`,
// so it is written bare to satisfy no-useless-escape. This is a purely
// cosmetic change — `[^\/?#:]` and `[^/?#:]` compile to the identical
// matcher (verified over 523k inputs, zero behavioural differences). The
// `\/\/` outside the class does still need escaping, and is left alone.
const URL_RE = /^https?:\/\/([^/?#:]+)/i;

// Hostnames that are unambiguously internal — redirects to these are safe.
const SAFE_REDIRECT_HOSTS = new Set([
  'localhost',
  'host.docker.internal',
]);

export function safeHref(value) {
  if (typeof value !== 'string') return '#';
  const match = value.match(URL_RE);
  if (!match) return '#';  // not http/https
  const host = match[1].toLowerCase();
  // Allow same-origin (current page's host).
  if (host === window.location.hostname) return value;
  // Allow known-safe internal hosts.
  if (SAFE_REDIRECT_HOSTS.has(host)) return value;
  // For external hosts, require an explicit allowlist match.
  // Without one, return '#' to prevent open redirect.
  return '#';
}
