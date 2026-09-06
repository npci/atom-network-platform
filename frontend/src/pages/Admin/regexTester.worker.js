// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

// Runs a regex match OFF the main thread. A catastrophic-backtracking pattern
// (e.g. /(a+)+$/ on a long string) can hang for seconds; the main thread arms a
// timeout and terminate()s this worker instead of freezing the tab. Same-origin
// Vite-bundled worker, so it satisfies the prod CSP (script-src 'self').
//
// SCR findings #4/#14 (ReDoS / Regex Injection, "Client Regex Injection") — the
// worker+timeout above already contains a slow pattern to this one thread, and
// a hard length cap on both inputs is cheap defense-in-depth: it keeps the
// worker-thread hang itself short (less backtracking surface) even before the
// 400ms caller timeout fires, and this is an admin-only diagnostic tool, so a
// tighter bound costs nothing in practice.
//
// InfoSec review (Second review): the timeout/isolation above contains the
// BLAST RADIUS of a slow pattern, but does nothing to stop the dangerous
// pattern from being compiled and run in the first place — it just bounds
// how long that run is allowed to take. `looksCatastrophic()` below is a
// zero-dependency static pre-check that rejects the well-known
// exponential-backtracking shapes (nested/overlapping quantified groups,
// e.g. `(a+)+`, `(a*)+`, `([a-z]+)*`, `(a|a)+`) BEFORE `RegExp` ever
// compiles them, so the worker no longer has to rely on timing alone to
// survive the common vulnerable cases.
const MAX_INPUT_LENGTH = 2000

// Heuristic catastrophic-backtracking detector (no external dependency —
// mirrors the core idea used by tools like `safe-regex`/`recheck`, scoped to
// the patterns that matter for this admin-only tester). Not a formal proof
// of linear-time matching; it is a pragmatic filter for the two textbook
// vulnerable shapes:
//   1. A quantified group whose body itself contains a quantifier, e.g.
//      (a+)+, (a*)+, (a+)*, (a*)*, ([a-z]+)*, (\d{2,})+
//   2. A quantified group whose alternatives overlap, e.g. (a|a)+, (a|ab)+
function looksCatastrophic(pattern) {
  // Case 1: nested quantifiers — a `(...)` group containing a `+`/`*`/`{n,}`
  // repetition token, immediately followed by another repetition token.
  const nestedQuantifier = /\([^()]*[+*][^()]*\)\s*[+*]|\([^()]*\{\d*,\}[^()]*\)\s*[+*]/
  if (nestedQuantifier.test(pattern)) return true

  // Case 2: alternation inside a quantified group where two branches share a
  // common prefix character class (the classic (a|a)+ / (a|ab)+ shape).
  const quantifiedAlternation = /\(([^()]*\|[^()]*)\)\s*[+*]/g
  let m
  while ((m = quantifiedAlternation.exec(pattern)) !== null) {
    const branches = m[1].split('|').map((b) => b.trim())
    const firstChars = branches.map((b) => b[0]).filter(Boolean)
    if (new Set(firstChars).size < firstChars.length) return true
  }
  return false
}

self.onmessage = (e) => {
  const { pattern, flags, sample } = e.data || {}
  try {
    if ((pattern || '').length > MAX_INPUT_LENGTH || (sample || '').length > MAX_INPUT_LENGTH) {
      self.postMessage({ ok: false, error: `pattern/sample exceeds ${MAX_INPUT_LENGTH} characters` })
      return
    }
    if (looksCatastrophic(pattern || '')) {
      self.postMessage({
        ok: false,
        error: 'pattern rejected: nested/overlapping quantifiers can cause catastrophic backtracking (ReDoS)',
      })
      return
    }
    const m = new RegExp(pattern, flags || '').exec(sample)
    self.postMessage({ ok: true, matched: !!m, matchText: m ? m[0] : null, index: m ? m.index : -1 })
  } catch (err) {
    self.postMessage({ ok: false, error: String((err && err.message) || err) })
  }
}
