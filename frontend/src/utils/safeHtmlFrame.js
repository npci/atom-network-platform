// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

// Hardening for the prototype-preview iframes (`srcDoc`).
//
// THE CONSTRAINT: this content is an interactive click-through prototype, and
// the generating prompt REQUIRES inline scripts — `onclick="go('screen-2')"`
// handlers and `setTimeout(() => go(...))` blocks (see the `prototype_screens`
// prompt in backend/app/agents/product_kit_agent.py). Stripping <script> with a
// sanitiser would therefore delete the feature, not secure it. So the goal is
// not "no script" but "script that can do no harm".
//
// WHY NOT NONCE-BASED CSP: a nonce-based CSP requires the server to know every
// script block's content at render time and inject matching nonce attributes.
// The prototype HTML is LLM-generated free text — we cannot predict or control
// the script content, so we cannot attach valid nonces. Hash-based CSP ('sha256-...')
// has the same limitation: the script content must be known at CSP-authoring time.
// `'unsafe-inline'` is the only viable option for dynamically-generated inline
// scripts, and the sandbox + egress-blocking CSP make it safe.
//
// THE RESIDUAL RISK, precisely. `sandbox="allow-scripts"` without
// `allow-same-origin` already puts the frame in an opaque origin, so it cannot
// read the parent DOM, its cookies, or its localStorage token. What it COULD
// still do is talk to the network: fetch()/XHR/WebSocket to an attacker host,
// an <img src> beacon, or a form POST — exfiltrating whatever is rendered
// (agent output, and for the partner console, authority-supplied documents).
//
// THE FIX: inject a restrictive CSP as the first thing in <head>. `default-src
// 'none'` denies every fetch directive that is not explicitly re-granted, so
// there is no egress at all, while `'unsafe-inline'` keeps the inline script and
// style the prototype needs. `data:` images are allowed because generated
// mockups routinely inline small SVG/PNG assets, and a data: URI cannot reach
// the network.
//
// Net effect: the prototype still runs exactly as designed; a malicious or
// prompt-injected payload inside it cannot phone home.

const FRAME_CSP = [
  "default-src 'none'",                    // deny everything not listed below
  "script-src 'unsafe-inline'",            // inline JS only — no remote scripts
  "style-src 'unsafe-inline'",             // inline CSS only
  "img-src data:",                         // inlined images only, never remote
  "font-src data:",
  "form-action 'none'",                    // no POSTing data out
  "connect-src 'none'",                    // no fetch / XHR / WebSocket / beacon
  "frame-src 'none'",                      // no nested frames
  "object-src 'none'",
  "base-uri 'none'",
].join('; ');

const CSP_META = `<meta http-equiv="Content-Security-Policy" content="${FRAME_CSP}">`;

// Matches ANY `<meta http-equiv="Content-Security-Policy" ...>` tag,
// regardless of the policy value it carries, attribute order, or quote
// style — used to strip pre-existing CSP declarations before this module
// injects its own. Global + case-insensitive so every occurrence is removed,
// not just the first.
const CSP_META_RE =
  /<meta\s+(?=[^>]*http-equiv=["']Content-Security-Policy["'])[^>]*>/gi;

/**
 * Wrap iframe `srcDoc` content with an egress-blocking CSP.
 *
 * SECURITY: `html` is LLM-generated from user-influenceable input (see
 * `product_kit_agent.py`'s `wrap_untrusted` calls), so it must be treated as
 * hostile. An earlier version of this function decided whether hardening was
 * "already applied" by checking whether the content merely CONTAINED the
 * marker substring `http-equiv="Content-Security-Policy"` — but that is a
 * property the untrusted content itself controls. Content crafted to include
 * that substring with a weaker policy value (or wrapped so it never reaches a
 * real `<meta>` tag) would satisfy the check and be rendered unmodified, with
 * no policy the browser would actually enforce, silently re-opening the
 * network egress (fetch/XHR/WebSocket/beacon) this module exists to close.
 *
 * The fix does not try to detect trustworthy pre-existing hardening at all —
 * that requires trusting the thing being defended against. Instead, EVERY
 * `<meta http-equiv="Content-Security-Policy">` tag already present is
 * stripped unconditionally, and exactly one, known-good `CSP_META` tag is
 * then inserted as the first element of `<head>`. This makes the function
 * idempotent by construction (running it twice yields the same output)
 * rather than by inspecting content it cannot trust, and it means an
 * attacker-supplied CSP tag — real or fake — can never survive into the
 * rendered frame.
 *
 * The meta tag must be the FIRST thing in <head> — a CSP delivered by
 * `http-equiv` only governs resources requested after it is parsed, so
 * anything placed above it would escape the policy.
 *
 * @param {string} html raw prototype markup (may be a full document or fragment)
 * @returns {string} the same markup with exactly one enforced CSP applied
 */
export function hardenFrameHtml(html) {
  if (typeof html !== 'string' || html.trim() === '') return '';

  // Strip any CSP meta tag(s) the untrusted content already carries — real
  // or spoofed — before injecting the one this module controls. Never trust
  // the content's self-description of its own hardening state.
  const stripped = html.replace(CSP_META_RE, '');

  // Case 1: a <head> exists — insert immediately after it opens.
  const headOpen = stripped.match(/<head[^>]*>/i);
  if (headOpen) {
    const at = headOpen.index + headOpen[0].length;
    return stripped.slice(0, at) + CSP_META + stripped.slice(at);
  }

  // Case 2: an <html> element but no <head> — create one.
  const htmlOpen = stripped.match(/<html[^>]*>/i);
  if (htmlOpen) {
    const at = htmlOpen.index + htmlOpen[0].length;
    return `${stripped.slice(0, at)}<head>${CSP_META}</head>${stripped.slice(at)}`;
  }

  // Case 3: a bare fragment — give it a document so the policy has somewhere to
  // live. Browsers would synthesise html/body anyway; doing it here means the
  // meta tag is guaranteed to be parsed first.
  return `<!DOCTYPE html><html><head>${CSP_META}</head><body>${stripped}</body></html>`;
}

export { FRAME_CSP };
