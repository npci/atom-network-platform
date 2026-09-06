// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useEffect, useState } from 'react'
import { agenticApi } from '../services/api'

// On-demand, plain-language developer + tester walkthrough of a code change:
// what the API does now, the runtime flow, the decision/decline logic, and concrete
// tester scenarios — plus a downloadable QA-sheet CSV. Self-contained; owns its state.
export default function ChangeWalkthrough({ runId }) {
  const [wt, setWt] = useState(null)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState(null)
  const [open, setOpen] = useState(false)

  useEffect(() => {
    let alive = true
    agenticApi.getWalkthrough(runId)
      .then(r => { if (alive && r.data?.walkthrough) { setWt(r.data.walkthrough); setOpen(true) } })
      .catch(() => {})
    return () => { alive = false }
  }, [runId])

  const generate = async () => {
    setLoading(true); setErr(null)
    try {
      const r = await agenticApi.generateWalkthrough(runId)
      setWt(r.data.walkthrough); setOpen(true)
    } catch (e) {
      setErr(e?.response?.data?.detail || 'Could not generate the walkthrough.')
    } finally { setLoading(false) }
  }

  const downloadCsv = async () => {
    try {
      const r = await agenticApi.walkthroughCsv(runId)
      const url = URL.createObjectURL(r.data)
      const a = document.createElement('a')
      a.href = url; a.download = `walkthrough-${String(runId).slice(0, 8)}.csv`
      document.body.appendChild(a); a.click(); a.remove()
      URL.revokeObjectURL(url)
    } catch { setErr('Could not download the QA sheet.') }
  }

  const card = { marginTop: 12, borderRadius: 10, border: '1px solid var(--border)', overflow: 'hidden' }
  const head = { display: 'flex', alignItems: 'center', gap: 10, padding: '10px 14px', background: 'rgba(99,102,241,0.06)' }
  const btn = (bg, fg) => ({ padding: '6px 14px', fontWeight: 600, fontSize: 13, background: bg, color: fg,
    border: 'none', borderRadius: 6, cursor: loading ? 'wait' : 'pointer', opacity: loading ? 0.7 : 1 })
  const h = { fontSize: 12, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em',
    color: 'var(--text-muted)', margin: '14px 0 6px' }
  const cell = { padding: '6px 10px', borderBottom: '1px solid var(--border)', fontSize: 13,
    color: 'var(--text-secondary)', verticalAlign: 'top', textAlign: 'left' }

  return (
    <div style={card}>
      <div style={head}>
        <span style={{ fontWeight: 700, fontSize: 14, color: 'var(--text-primary)', flex: 1 }}>
          📖 Developer &amp; tester walkthrough
        </span>
        {wt && <button onClick={downloadCsv} style={btn('transparent', 'var(--text-secondary)')}>⬇ QA sheet (CSV)</button>}
        <button onClick={generate} disabled={loading} style={btn('#6366f1', '#fff')}>
          {loading ? 'Generating…' : wt ? 'Regenerate' : 'Generate walkthrough'}
        </button>
        {wt && <button onClick={() => setOpen(o => !o)} style={btn('transparent', 'var(--text-muted)')}>{open ? 'Hide' : 'Show'}</button>}
      </div>

      {err && <div style={{ padding: '10px 14px', color: '#fca5a5', fontSize: 13 }}>{err}</div>}
      {!wt && !loading && !err && (
        <div style={{ padding: '12px 14px', fontSize: 13, color: 'var(--text-muted)' }}>
          Generate a plain-language summary of this change — the flow, the decision logic, and ready-to-run tester scenarios.
        </div>
      )}

      {wt && open && (
        <div style={{ padding: '4px 16px 16px' }}>
          {wt.summary && <p style={{ fontSize: 14, color: 'var(--text-primary)', lineHeight: 1.6 }}>{wt.summary}</p>}
          {wt.api_surface && (<><div style={h}>What the API does now</div>
            <p style={{ fontSize: 13, color: 'var(--text-secondary)', lineHeight: 1.6, whiteSpace: 'pre-wrap' }}>{wt.api_surface}</p></>)}

          {wt.flow?.length > 0 && (<><div style={h}>The flow, step by step</div>
            <ol style={{ margin: 0, paddingLeft: 20, fontSize: 13, color: 'var(--text-secondary)', lineHeight: 1.7 }}>
              {wt.flow.map((s, i) => <li key={i}>{s}</li>)}
            </ol></>)}

          {wt.decision_points?.length > 0 && (<><div style={h}>Decision / decline logic</div>
            <table style={{ width: '100%', borderCollapse: 'collapse' }}><tbody>
              {wt.decision_points.map((d, i) => (
                <tr key={i}>
                  <td style={{ ...cell, fontWeight: 700, color: 'var(--text-primary)', whiteSpace: 'nowrap', width: 70 }}>{d.code}</td>
                  <td style={cell}>{d.when}</td>
                  <td style={cell}>{d.result}</td>
                </tr>
              ))}
            </tbody></table></>)}

          {wt.tester_scenarios?.length > 0 && (<><div style={h}>Tester scenarios</div>
            <table style={{ width: '100%', borderCollapse: 'collapse' }}>
              <thead><tr>
                <th style={{ ...cell, fontWeight: 700, color: 'var(--text-muted)', width: 28 }}>#</th>
                <th style={{ ...cell, fontWeight: 700, color: 'var(--text-muted)' }}>Scenario</th>
                <th style={{ ...cell, fontWeight: 700, color: 'var(--text-muted)' }}>Send</th>
                <th style={{ ...cell, fontWeight: 700, color: 'var(--text-muted)' }}>Expected</th>
              </tr></thead>
              <tbody>{wt.tester_scenarios.map((s, i) => (
                <tr key={i}>
                  <td style={cell}>{s.id ?? i + 1}</td>
                  <td style={{ ...cell, color: 'var(--text-primary)' }}>{s.scenario}</td>
                  <td style={cell}>{s.input}</td>
                  <td style={cell}>{s.expected}</td>
                </tr>
              ))}</tbody>
            </table></>)}

          {wt.caveats?.length > 0 && (<><div style={h}>Heads-ups for testers</div>
            <ul style={{ margin: 0, paddingLeft: 20, fontSize: 13, color: 'var(--text-secondary)', lineHeight: 1.7 }}>
              {wt.caveats.map((c, i) => <li key={i}>{c}</li>)}
            </ul></>)}
        </div>
      )}
    </div>
  )
}
