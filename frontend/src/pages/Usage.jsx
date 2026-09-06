// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { Fragment, useEffect, useState } from 'react'
import { agenticApi } from '../services/api.js'

const fmt = (n) => {
  if (n == null) return '—'
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 2) + 'M'
  if (n >= 1_000) return (n / 1_000).toFixed(n >= 100_000 ? 0 : 1) + 'k'
  return String(n)
}
const usd = (u) => u == null ? '—'
  : `${u.cost_complete === false ? '≥' : ''}$${u.cost_usd < 0.01 ? (u.cost_usd || 0).toFixed(4) : u.cost_usd.toFixed(2)}`

const card = { background: 'var(--bg-elevated, #11162a)', border: '1px solid var(--border)',
  borderRadius: 10, padding: '14px 16px', marginBottom: 14 }
const th = { textAlign: 'left', fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.05em',
  color: 'var(--text-muted)', padding: '6px 10px', borderBottom: '1px solid var(--border)' }
const td = { padding: '7px 10px', fontSize: 13, borderBottom: '1px solid var(--border-subtle)' }

function UsageCells({ u }) {
  const t = u.tokens || {}
  return (
    <>
      <td style={{ ...td, textAlign: 'right', fontWeight: 700 }}>{fmt(u.total_tokens)}</td>
      <td style={{ ...td, textAlign: 'right', color: '#16a34a', fontWeight: 600 }}>{usd(u)}</td>
      <td style={{ ...td, textAlign: 'right', color: 'var(--text-muted)' }}>{u.calls}</td>
      <td style={{ ...td, textAlign: 'right', color: 'var(--text-muted)' }}>
        {fmt(t.input)} / {fmt(t.output)} / {fmt(t.cache_read)}
      </td>
    </>
  )
}

const HEAD = (
  <tr>
    <th style={th}></th>
    <th style={{ ...th, textAlign: 'right' }}>Tokens</th>
    <th style={{ ...th, textAlign: 'right' }}>Cost</th>
    <th style={{ ...th, textAlign: 'right' }}>Calls</th>
    <th style={{ ...th, textAlign: 'right' }}>in / out / cache</th>
  </tr>
)

// One phase (sub-header) row + its per-section rows. Reused by the flow groups and the
// standalone Agentic Code Gen card so the two views render identically.
function PhaseRows({ ph, indent = 0 }) {
  return (
    <Fragment>
      <tr style={{ background: 'rgba(96,165,250,0.06)' }}>
        <td style={{ ...td, paddingLeft: 12 + indent, fontWeight: 700 }}>
          ▸ {ph.phase} <span style={{ fontSize: 11, color: 'var(--text-muted)', fontWeight: 400 }}>(phase)</span>
        </td>
        <UsageCells u={ph} />
      </tr>
      {(ph.sections || []).map(s => (
        <tr key={ph.phase_key + ':' + s.section}>
          <td style={{ ...td, paddingLeft: 30 + indent, color: 'var(--text-secondary)' }}>{s.section}</td>
          <UsageCells u={s} />
        </tr>
      ))}
    </Fragment>
  )
}

function ChangeDetail({ changeId, onClose }) {
  const [d, setD] = useState(null)
  const [err, setErr] = useState(null)
  useEffect(() => {
    agenticApi.usageChangeDetail(changeId).then(r => setD(r.data)).catch(e => setErr(e?.response?.data?.detail || e.message))
  }, [changeId])
  if (err) return <div style={{ ...card, color: '#ef4444' }}>{err}</div>
  if (!d) return <div style={{ ...card, color: 'var(--text-muted)' }}>Loading…</div>
  // Fall back to a flat phase list if the backend predates grouping.
  const groups = d.groups || (d.phases ? [{ group: 'all', label: '', total_tokens: d.total_tokens,
    cost_usd: d.cost_usd, cost_complete: d.cost_complete, calls: d.calls, tokens: d.tokens, phases: d.phases }] : [])
  return (
    <div style={card}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
        <strong style={{ fontSize: 15 }}>{d.title}</strong>
        <button onClick={onClose} style={{ padding: '4px 12px', fontSize: 12, background: 'transparent',
          color: 'var(--text-muted)', border: '1px solid var(--border)', borderRadius: 6, cursor: 'pointer' }}>← Back</button>
      </div>
      <div style={{ fontSize: 13, color: 'var(--text-secondary)', marginBottom: 10 }}>
        Total: <strong>{fmt(d.total_tokens)} tokens</strong> · <span style={{ color: '#16a34a' }}>{usd(d)}</span> · {d.calls} calls
      </div>
      <table style={{ width: '100%', borderCollapse: 'collapse' }}>
        <thead>{HEAD}</thead>
        <tbody>
          {groups.map(g => (
            <Fragment key={g.group}>
              {g.label && (
                <tr style={{ background: 'rgba(76,175,125,0.10)' }}>
                  <td style={{ ...td, fontWeight: 800 }}>{g.label}</td>
                  <UsageCells u={g} />
                </tr>
              )}
              {(g.phases || []).map(ph => (
                <PhaseRows key={g.group + ':' + ph.phase_key} ph={ph} indent={g.label ? 14 : 0} />
              ))}
            </Fragment>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function ChangeTable({ rows, onSelect }) {
  return (
    <table style={{ width: '100%', borderCollapse: 'collapse', marginTop: 8 }}>
      <thead>{HEAD}</thead>
      <tbody>
        {rows.map(c => (
          <tr key={c.change_request_id} onClick={() => onSelect(c.change_request_id)}
            style={{ cursor: 'pointer' }}
            onMouseEnter={e => e.currentTarget.style.background = 'rgba(96,165,250,0.06)'}
            onMouseLeave={e => e.currentTarget.style.background = 'transparent'}>
            <td style={{ ...td, fontWeight: 600 }}>{c.title} <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>›</span></td>
            <UsageCells u={c} />
          </tr>
        ))}
      </tbody>
    </table>
  )
}

export default function Usage() {
  const [changes, setChanges] = useState(null)
  const [codegenChanges, setCodegenChanges] = useState(null)
  const [grand, setGrand] = useState(null)
  const [other, setOther] = useState(null)
  const [sel, setSel] = useState(null)
  const [err, setErr] = useState(null)

  useEffect(() => {
    agenticApi.usageByChange().then(r => {
      setChanges(r.data.changes || [])
      setCodegenChanges(r.data.codegen_changes || [])
      setGrand(r.data.grand_total)
    }).catch(e => setErr(e?.response?.data?.detail || e.message))
    agenticApi.usageOther().then(r => setOther(r.data)).catch(() => {})
  }, [])

  return (
    <div style={{ maxWidth: 1000, margin: '0 auto', padding: '20px 16px' }}>
      <h1 style={{ fontSize: 22, marginBottom: 4 }}>🪙 LLM Usage</h1>
      <p style={{ color: 'var(--text-muted)', fontSize: 13, marginBottom: 16 }}>
        Token spend and estimated cost per change (click a change for the per-phase → per-section breakdown).
        Cost is best-effort — <code>≥</code> means one or more models are unpriced (tokens exact, $ partial).
      </p>

      {err && <div style={{ ...card, color: '#ef4444' }}>{err}</div>}

      {(grand || other) && (() => {
        // Header = EVERYTHING (change-attributed + non-flow), so the total always reflects all
        // recorded spend even before any change pipeline has been attributed.
        const g = grand || {}, o = other || {}
        const total = {
          total_tokens: (g.total_tokens || 0) + (o.total_tokens || 0),
          cost_usd: Math.round(((g.cost_usd || 0) + (o.cost_usd || 0)) * 10000) / 10000,
          cost_complete: (g.cost_complete !== false) && (o.cost_complete !== false),
          calls: (g.calls || 0) + (o.calls || 0),
        }
        return (
          <div style={{ ...card, display: 'flex', gap: 24, flexWrap: 'wrap', alignItems: 'baseline' }}>
            <div><div style={{ fontSize: 26, fontWeight: 800 }}>{fmt(total.total_tokens)}</div>
              <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>TOTAL TOKENS</div></div>
            <div><div style={{ fontSize: 26, fontWeight: 800, color: '#16a34a' }}>{usd(total)}</div>
              <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>EST. COST</div></div>
            <div><div style={{ fontSize: 26, fontWeight: 800 }}>{total.calls}</div>
              <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>LLM CALLS</div></div>
            {(o.total_tokens > 0) && (
              <div style={{ fontSize: 11, color: 'var(--text-muted)', alignSelf: 'center' }}>
                ({fmt(g.total_tokens || 0)} in changes · {fmt(o.total_tokens || 0)} non-flow)
              </div>
            )}
          </div>
        )
      })()}

      {sel ? <ChangeDetail changeId={sel} onClose={() => setSel(null)} /> : (
        <>
          {/* Flow changes — went through the pipeline (prompt enhancement → … → BRD → code). */}
          <div style={card}>
            <strong style={{ fontSize: 14 }}>Flow changes</strong>
            <div style={{ fontSize: 12, color: 'var(--text-muted)', margin: '2px 0 0' }}>
              Changes taken through the full pipeline. Click for the Phase A / B / C breakdown.
            </div>
            {changes && changes.length === 0 && <div style={{ color: 'var(--text-muted)', marginTop: 8 }}>No flow changes yet.</div>}
            {changes && changes.length > 0 && <ChangeTable rows={changes} onSelect={setSel} />}
            {!changes && <div style={{ color: 'var(--text-muted)', marginTop: 8 }}>Loading…</div>}
          </div>

          {/* Agentic Code Gen — direct quick-start code changes (no prompt enhancement / flow). */}
          {codegenChanges && codegenChanges.length > 0 && (
            <div style={card}>
              <strong style={{ fontSize: 14 }}>⚙ Agentic Code Gen <span style={{ fontSize: 11, color: 'var(--text-muted)', fontWeight: 400 }}>(direct code changes)</span></strong>
              <div style={{ fontSize: 12, color: 'var(--text-muted)', margin: '2px 0 0' }}>
                Started straight from the code-gen console — code change without going through prompt enhancement / BRD.
              </div>
              <ChangeTable rows={codegenChanges} onSelect={setSel} />
            </div>
          )}
        </>
      )}

      {/* Non-flow usage — LLM calls not attributable to a change (ad-hoc / background agents). */}
      {other && other.total_tokens > 0 && (
        <div style={card}>
          <strong style={{ fontSize: 14 }}>Other (non-flow) usage — by section</strong>
          <div style={{ fontSize: 12, color: 'var(--text-muted)', margin: '2px 0 8px' }}>
            LLM work not tied to a specific change (background jobs, ad-hoc agents). Total {fmt(other.total_tokens)} · {usd(other)}.
          </div>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>{HEAD}</thead>
            <tbody>
              {(other.sections || []).map(s => (
                <tr key={s.section}>
                  <td style={{ ...td, color: 'var(--text-secondary)' }}>{s.section}</td>
                  <UsageCells u={s} />
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
