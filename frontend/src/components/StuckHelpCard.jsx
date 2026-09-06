// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useState } from 'react'
import { agenticApi } from '../services/api.js'

/**
 * "Ask AI what to do" — shown next to Resume on a failed run. Asks the backend's stuck-helper
 * for 2-3 recovery options drawn from a closed action catalog. The user picks one OR types a
 * free-text direction; free text is LLM-validated and unsafe/unclear input bounces back to the
 * same card with the textbox hidden, so the user MUST pick an option.
 */
export default function StuckHelpCard({ runId, onApplied }) {
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [proposal, setProposal] = useState(null)        // {summary, options, recommended}
  const [selOpt, setSelOpt] = useState(null)
  const [custom, setCustom] = useState('')
  const [hideText, setHideText] = useState(false)       // true after an UNCLEAR/UNSAFE validator verdict
  const [validatorMsg, setValidatorMsg] = useState('')
  const [error, setError] = useState(null)
  const [applied, setApplied] = useState(null)          // {action} once dispatched

  const askAI = async () => {
    setOpen(true); setLoading(true); setError(null); setValidatorMsg(''); setHideText(false)
    try {
      const { data } = await agenticApi.stuckHelp(runId)
      setProposal(data); setSelOpt(data?.recommended || data?.options?.[0]?.id || null)
    } catch (e) { setError(e?.response?.data?.detail || e.message) }
    finally { setLoading(false) }
  }

  const decide = async () => {
    if (!proposal) return
    setBusy(true); setError(null); setValidatorMsg('')
    try {
      const useCustom = !hideText && custom.trim().length > 0
      const payload = useCustom ? { custom_direction: custom.trim() }
                                : { action_code: (proposal.options || []).find(o => o.id === selOpt)?.action_code }
      if (!useCustom && !payload.action_code) { setBusy(false); return }
      const { data } = await agenticApi.stuckDecide(runId, payload)
      if (data.applied) { setApplied({ action: data.action }); onApplied?.(data.action); return }
      // Validator rejected: bounce back to the (fresh) options card, hide the textbox.
      if (data.options_only) {
        setProposal({ summary: data.summary, options: data.options, recommended: data.recommended })
        setSelOpt(data.recommended || data.options?.[0]?.id || null)
        setHideText(true); setCustom('')
        setValidatorMsg((data.verdict === 'UNSAFE' ? "That direction looks unsafe — please pick one of the options below."
                                                   : "I couldn't map that to a safe action. Please pick an option below.")
                        + (data.why ? ` (${data.why})` : ''))
      }
    } catch (e) { setError(e?.response?.data?.detail || e.message) }
    finally { setBusy(false) }
  }

  if (!open) return (
    <button onClick={askAI} title="Ask the LLM for recovery options for this stuck run"
      style={{ marginTop: 10, marginLeft: 8, padding: '7px 16px', fontWeight: 600, background: 'transparent',
        color: '#60a5fa', border: '1px solid #60a5fa', borderRadius: 6, cursor: 'pointer' }}>
      🤖 Ask AI what to do
    </button>
  )

  if (applied) return (
    <div style={{ marginTop: 10, padding: '10px 12px', borderRadius: 6,
      background: 'rgba(52,211,153,0.10)', border: '1px solid rgba(52,211,153,0.4)', color: '#34d399', fontSize: 13 }}>
      ✓ Applied recovery action: <code>{applied.action}</code>. Watch the activity log for next steps.
    </div>
  )

  return (
    <section style={{ marginTop: 12, padding: '12px 14px', borderRadius: 8,
      background: 'rgba(96,165,250,0.06)', border: '1px solid rgba(96,165,250,0.35)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <strong style={{ fontSize: 14, color: '#60a5fa' }}>🤖 Recovery options</strong>
        {loading && <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>asking the agent…</span>}
      </div>
      {proposal?.summary && (
        <p style={{ fontSize: 13, color: 'var(--text-secondary)', marginTop: 6 }}>{proposal.summary}</p>
      )}
      {validatorMsg && (
        <div style={{ marginTop: 6, padding: '7px 10px', borderRadius: 6, fontSize: 12.5,
          background: 'rgba(245,158,11,0.10)', border: '1px solid rgba(245,158,11,0.4)', color: '#d97706' }}>
          {validatorMsg}
        </div>
      )}
      {!loading && proposal && (proposal.options || []).map(o => {
        const rec = o.id === proposal.recommended
        return (
          <label key={o.id} style={{ display: 'block', margin: '8px 0', padding: '10px 12px', borderRadius: 6, cursor: 'pointer',
            background: selOpt === o.id ? 'rgba(96,165,250,0.10)' : 'transparent',
            border: '1px solid ' + (selOpt === o.id ? 'rgba(96,165,250,0.5)' : 'var(--border)') }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
              <input type="radio" name="stuck-option" checked={selOpt === o.id}
                onChange={() => { setSelOpt(o.id); setCustom('') }} />
              <span style={{ fontWeight: 700 }}>{o.title}</span>
              <span style={{ fontSize: 10, fontWeight: 700, padding: '1px 6px', borderRadius: 10,
                background: 'rgba(148,163,184,0.15)', color: '#94a3b8' }}>{o.action_code}</span>
              {rec && <span style={{ fontSize: 10, fontWeight: 700, padding: '1px 6px', borderRadius: 10,
                background: 'rgba(52,211,153,0.15)', color: '#16a34a', border: '1px solid rgba(52,211,153,0.4)' }}>RECOMMENDED</span>}
            </div>
            {o.why && <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 4, marginLeft: 26 }}>{o.why}</div>}
            {o.tradeoffs && <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 2, marginLeft: 26 }}>Tradeoffs: {o.tradeoffs}</div>}
          </label>
        )
      })}
      {!hideText && !loading && proposal && (
        <textarea value={custom} rows={2}
          onChange={e => { setCustom(e.target.value); if (e.target.value.length > 0) setSelOpt(null) }}
          onFocus={() => { if (custom.length > 0) setSelOpt(null) }}
          placeholder="…or describe what you'd like to do (will be validated before applying)"
          style={{ width: '100%', marginTop: 6, padding: 8, borderRadius: 6, border: '1px solid var(--border)',
            background: 'var(--bg-input, #0b1021)', color: 'var(--text-primary)', fontSize: 13 }} />
      )}
      {error && <div style={{ color: '#ef4444', fontSize: 12, marginTop: 6 }}>{error}</div>}
      <div style={{ display: 'flex', gap: 8, marginTop: 8, flexWrap: 'wrap' }}>
        <button onClick={decide} disabled={busy || loading || !proposal || (!selOpt && (hideText || !custom.trim()))}
          style={{ padding: '7px 14px', fontWeight: 600, background: '#2563eb', color: '#fff',
            border: 'none', borderRadius: 6, cursor: 'pointer',
            opacity: (busy || !proposal || (!selOpt && (hideText || !custom.trim()))) ? 0.55 : 1 }}>
          {busy ? 'Applying…' : 'Apply this'}
        </button>
        <button onClick={() => { setOpen(false); setProposal(null); setCustom(''); setHideText(false) }}
          style={{ padding: '7px 14px', fontWeight: 600, background: 'transparent', color: 'var(--text-muted)',
            border: '1px solid var(--border)', borderRadius: 6, cursor: 'pointer' }}>
          Close
        </button>
      </div>
    </section>
  )
}
