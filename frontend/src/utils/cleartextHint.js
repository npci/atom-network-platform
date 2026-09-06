// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

// Advisory check for "this http:// URL looks like it leaves the host".
//
// PURPOSE AND LIMITS. This is a UI HINT, not a security control. The real
// decision is made server-side, where the hostname is actually resolved and
// classified (backend/app/core/ssrf_guard.py `check_cleartext_url`, which every
// other service on this wire mirrors with its own equivalent). A browser cannot
// resolve a hostname to an IP, so this can only pattern-match — and anything it concludes could be
// bypassed by posting to the API directly. Its job is to tell an operator
// *before* they hit Send that the value they typed will be refused, instead of
// letting them discover it from an error afterwards.
//
// Because it cannot resolve, it errs toward SILENCE: a bare service name like
// `cert-agent` is assumed local (that is the normal docker case), and only
// forms that clearly denote an off-host destination are flagged. A false
// warning on a routine workflow would train operators to ignore the banner,
// which is worse than showing nothing.

// Hostnames that are unambiguously local without needing resolution.
const LOCAL_NAMES = new Set(['localhost', 'host.docker.internal', '::1']);

// Literal IPv4 in the ranges where cleartext is acceptable. Mirrors the server's
// explicit list (loopback + RFC-1918) — deliberately NOT "any private-looking
// address": 169.254/16 (cloud metadata) and 100.64/10 (CGNAT) are excluded
// there and must be excluded here too, or the hint would contradict the guard.
function isLocalIpv4(host) {
  const m = host.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
  if (!m) return false;
  const [a, b] = [Number(m[1]), Number(m[2])];
  if (m.slice(1).some(o => Number(o) > 255)) return false;
  if (a === 127) return true;                        // loopback
  if (a === 10) return true;                         // 10/8
  if (a === 172 && b >= 16 && b <= 31) return true;  // 172.16/12
  if (a === 192 && b === 168) return true;           // 192.168/16
  return false;
}

/**
 * @param {string} url operator-supplied endpoint
 * @returns {string|null} a warning to display, or null when nothing to say
 */
export function cleartextWarning(url) {
  const raw = (url || '').trim();
  if (!raw) return null;

  let parsed;
  try {
    parsed = new URL(raw);
  } catch {
    return null;   // incomplete typing — say nothing until it parses
  }

  if (parsed.protocol !== 'http:') return null;   // https (or anything else) is fine here

  const host = parsed.hostname.toLowerCase().replace(/\.$/, '');
  if (LOCAL_NAMES.has(host)) return null;
  if (isLocalIpv4(host)) return null;
  if (host.startsWith('[')) return null;          // IPv6 literal — leave it to the server

  // A bare label with no dots (`cert-agent`, `bank-agent`) is a docker service
  // name in every supported deployment, so treat it as local.
  if (!host.includes('.')) return null;

  return `This is a plaintext http:// URL to "${host}", which looks like it is not on this `
       + 'host. Certification envelopes carry the network test transaction data, so the server will '
       + 'refuse to send them in the clear off-host. Use https://, or allowlist the host if '
       + 'the link is trusted.';
}
