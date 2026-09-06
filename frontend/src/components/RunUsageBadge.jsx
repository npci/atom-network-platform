// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useEffect, useState } from 'react'
import { agenticApi } from '../services/api.js'

const fmtTokens = (n) => {
  if (n == null) return '—'
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 2) + 'M'
  if (n >= 1_000) return (n / 1_000).toFixed(n >= 100_000 ? 0 : 1) + 'k'
  return String(n)
}

/**
 * Live token + $ spend for a run. Polls the /usage endpoint (which sums the append-only
 * llm_usage events) every few seconds while the run is active, then settles to a final figure.
 * Cost is best-effort: when an unpriced model is involved the $ is marked "≥" (partial).
 */
export default function RunUsageBadge({ runId, active }) {
  const [u, setU] = useState(null)

  useEffect(() => {
    if (!runId) return
    let alive = true
    const load = async () => {
      try { const { data } = await agenticApi.runUsage(runId); if (alive) setU(data) } catch { /* noop */ }
    }
    load()
    // Poll while the run is live; once it's terminal a single load is enough.
    const id = active ? setInterval(load, 4000) : null
    return () => { alive = false; if (id) clearInterval(id) }
  }, [runId, active])

  if (!u || !u.total_tokens) return null
  const usd = u.cost_usd != null
    ? `${u.cost_complete ? '' : '≥'}$${u.cost_usd < 0.01 ? u.cost_usd.toFixed(4) : u.cost_usd.toFixed(2)}`
    : null
  const t = u.tokens || {}
  const title = `LLM spend for this run\n`
    + `calls: ${u.calls}\n`
    + `input: ${(t.input || 0).toLocaleString()}\n`
    + `output: ${(t.output || 0).toLocaleString()}\n`
    + `cache read: ${(t.cache_read || 0).toLocaleString()}\n`
    + `cache write: ${(t.cache_write || 0).toLocaleString()}`
    + (u.cost_complete ? '' : '\n(≥ : one or more models are unpriced — tokens counted, $ partial)')

  return (
    <span title={title} style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 11.5,
      padding: '2px 9px', borderRadius: 12, background: 'rgba(148,163,184,0.12)',
      border: '1px solid var(--border)', color: 'var(--text-secondary)', whiteSpace: 'nowrap' }}>
      🪙 {fmtTokens(u.total_tokens)} tokens
      {usd && <span style={{ color: '#16a34a', fontWeight: 600 }}>· {usd}</span>}
      <span style={{ color: 'var(--text-muted)' }}>· {u.calls} calls</span>
    </span>
  )
}
