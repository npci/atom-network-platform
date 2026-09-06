// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useRef, useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { agentsApi } from '../services/api'

// Bound how long we poll while there's no signal either way (no open reconciliation,
// no terminal grounding summary) — otherwise a change that never had / will have a
// reconciliation polls every 5s forever (~app-lifetime of the tab). 2 minutes covers
// the real case (detection surfacing its 'checking' row shortly after upload) without
// polling indefinitely once that window has clearly passed.
const NO_SIGNAL_POLL_BUDGET_MS = 120000

// Conflicts between a user-uploaded BRD and the ratified plan. Mirrors the
// AnalysisPanel clarification card: preset options as radios + a free-text box
// (mutually exclusive, per the backend contract), all-answered before submit.
// Renders nothing until the background reconciliation surfaces conflicts.

const CARD = {
  marginTop: 12, padding: '14px 16px', borderRadius: 8,
  background: 'rgba(217,119,6,0.07)', border: '1px solid rgba(217,119,6,0.45)',
}

const JURIS = {
  contradicts_plan:  { label: 'Contradicts the plan',       color: '#dc2626' },
  drops_requirement: { label: 'Drops a plan requirement',   color: '#d97706' },
  extends_plan:      { label: 'Adds beyond the plan',       color: '#2563eb' },
  review:            { label: 'Needs review',               color: '#64748b' },
}

const answered = (a) => !!(a && (a.chosen_option_id || (a.custom_answer || '').trim()))

// Hoisted out of the component: a component defined during render is a NEW
// component type on every render, so React unmounts and remounts its subtree
// instead of updating it. This one closes over nothing, so it belongs here.
const Spinner = () => (
  <span style={{ width: 16, height: 16, border: '2px solid rgba(217,119,6,0.35)', borderTopColor: '#d97706', borderRadius: '50%', display: 'inline-block', animation: 'spin 1s linear infinite', flexShrink: 0 }} />
)

export default function ReconciliationPanel({ changeId, docKind = 'brd' }) {
  const qc = useQueryClient()
  const [answers, setAnswers] = useState({})
  const [err, setErr] = useState(null)
  const docLabel = docKind === 'tech_spec' ? 'Tech Spec' : 'BRD'
  const mountedAt = useRef(0)
  // Seeded on mount rather than in the useRef initialiser: Date.now() during
  // render is impure, and leaving it 0 would make the first comparison read as
  // an enormous elapsed time — the no-signal poll budget is measured from mount.
  useEffect(() => { mountedAt.current = Date.now() }, [])

  const { data } = useQuery({
    queryKey: ['reconciliation', changeId, docKind],
    queryFn: () => agentsApi.getReconciliation(changeId, docKind).then(r => r.data),
    // Poll while the check runs (pre-conflicts) and while 'applying' (the corrected
    // doc is regenerating) so this advances on its own; stop on 'pending' (user's turn).
    refetchInterval: (query) => {
      const d = query?.state?.data
      if (d?.exists && d.status !== 'applying') return false   // pending → user's turn
      if (!d?.exists && d?.grounding_summary) return false      // resolved with code-check findings → terminal
      if (!d?.exists && Date.now() - mountedAt.current > NO_SIGNAL_POLL_BUDGET_MS) return false
      return 5000
    },
  })

  const submit = useMutation({
    mutationFn: (resolutions) => agentsApi.decideReconciliation(changeId, resolutions, docKind),
    onSuccess: () => {
      setErr(null); setAnswers({})
      qc.invalidateQueries({ queryKey: ['reconciliation', changeId, docKind] })
      qc.invalidateQueries({ queryKey: ['brd', changeId] })   // plan-wins may have corrected the BRD
    },
    onError: (e) => setErr(e?.response?.data?.detail || 'Could not submit resolutions.'),
  })

  const dismiss = useMutation({
    mutationFn: () => agentsApi.dismissReconciliation(changeId, docKind),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['reconciliation', changeId, docKind] }),
    onError: (e) => setErr(e?.response?.data?.detail || 'Could not dismiss.'),
  })

  const answerGrounding = useMutation({
    mutationFn: ({ index, question, answer }) => agentsApi.answerGrounding(changeId, index, question, answer, docKind),
  })

  const ackOverturns = useMutation({
    mutationFn: () => agentsApi.acknowledgeOverturns(changeId, docKind),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['reconciliation', changeId, docKind] }),
  })

  if (!data?.exists) {
    // Resolved, but the code check on the accepted changes found something worth seeing
    // before approval (a risk, an open question, or a change that overturns a ratified
    // decision). Advisory — the doc is already final; this doesn't re-hide it. The one
    // exception: an unacknowledged overturns-ratified soft-gates approval (§8.1).
    if (data?.grounding_summary) return <GroundingCard summary={data.grounding_summary} docLabel={docLabel} mutation={answerGrounding} ack={ackOverturns} />
    return null
  }

  // Checking — the detection axes (LLM) are still running right after upload. Show a
  // loader so the wait is explicit; the query keeps polling until conflicts surface
  // (→ the resolve UI below) or the doc comes back clean (→ this self-hides).
  if (data.status === 'checking' || data.checking) {
    return (
      <section style={CARD}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <Spinner />
          <div>
            <div style={{ fontSize: 14, fontWeight: 700 }}>Checking your {docLabel} against the ratified plan…</div>
            <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 2 }}>
              Comparing every requirement, added rule and implementation step — this takes a
              moment. Any conflicts to resolve will appear here.
            </div>
          </div>
        </div>
      </section>
    )
  }

  // Resolved — the corrected doc is regenerating from the plan. Show progress in place
  // of the conflict list; the query keeps polling until this clears and the parent
  // swaps in the new document.
  if (data.status === 'applying' || data.regenerating) {
    return (
      <section style={CARD}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <span style={{ width: 16, height: 16, border: '2px solid rgba(217,119,6,0.35)', borderTopColor: '#d97706', borderRadius: '50%', display: 'inline-block', animation: 'spin 1s linear infinite', flexShrink: 0 }} />
          <div>
            <div style={{ fontSize: 14, fontWeight: 700 }}>Applying your resolutions — regenerating the {docLabel}…</div>
            <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 2 }}>
              Reconciling your {docLabel} with the ratified plan. It isn’t final yet — approval and download
              unlock automatically once the updated {docLabel} is ready.
            </div>
          </div>
        </div>
      </section>
    )
  }

  const conflicts = data.conflicts || []
  const allAnswered = conflicts.every(c => answered(answers[c.id]))

  return (
    <section style={CARD}>
      <div style={{ fontSize: 14, fontWeight: 700 }}>
        ⚠ {conflicts.length} conflict{conflicts.length > 1 ? 's' : ''} between your uploaded {docLabel} and the ratified plan
      </div>
      <div style={{ fontSize: 12, color: 'var(--text-muted)', margin: '4px 0 8px' }}>
        Resolve each before the Tech Spec / code can be generated — choose whether your {docLabel} or the plan
        is right, or type your own resolution.
      </div>

      {/* Feasibility signal — make it evident the code-grounded check RAN, even when
          everything is buildable (otherwise the check is invisible on a clean result). */}
      {(() => {
        if (!conflicts.some(c => c.feasibility_checked)) return null
        const reds = conflicts.filter(c => (c.red_options || []).length).length
        const warns = conflicts.filter(c => (c.warn_options || []).length).length
        const clean = reds === 0 && warns === 0
        const parts = []
        if (reds) parts.push(`${reds} not buildable`)
        if (warns) parts.push(`${warns} need new wire/schema build`)
        return (
          <div style={{ fontSize: 12, margin: '0 0 10px', display: 'flex', gap: 6, alignItems: 'center',
                        color: clean ? '#16a34a' : '#d97706' }}>
            <span>{clean ? '✓' : '⚠'}</span>
            <span>Feasibility validated against the code{clean ? ' — every resolution is buildable' : ` — ${parts.join(', ')} (see the marked options)`}</span>
          </div>
        )
      })()}

      {(() => {
        // B2: dropped-requirement conflicts almost always resolve to "plan is right"
        // (add the requirement back). Offer an explicit one-click accept for all of
        // them, so a thin doc's many omissions don't each need a separate decision.
        const omissions = conflicts.filter(c => c.jurisdiction === 'drops_requirement')
        if (omissions.length < 2) return null
        return (
          <button
            onClick={() => setAnswers(a => ({ ...a, ...Object.fromEntries(omissions.map(c => [c.id, { chosen_option_id: 'plan_wins' }])) }))}
            style={{ marginBottom: 8, padding: '5px 12px', fontSize: 12, fontWeight: 600, background: 'transparent', color: '#2563eb', border: '1px solid rgba(37,99,235,0.5)', borderRadius: 6, cursor: 'pointer' }}>
            ✓ Accept all {omissions.length} dropped-requirement items as “the plan is right”
          </button>
        )
      })()}

      {conflicts.map((c) => {
        const sel = answers[c.id] || {}
        const j = JURIS[c.jurisdiction] || JURIS.review
        return (
          <div key={c.id} style={{ margin: '10px 0', paddingBottom: 8, borderBottom: '1px solid var(--border-subtle)' }}>
            <span style={{ fontSize: 10, fontWeight: 700, padding: '1px 7px', borderRadius: 10, background: `${j.color}22`, color: j.color }}>
              {j.label}
            </span>
            <div style={{ fontWeight: 600, fontSize: 13, marginTop: 5 }}>{c.text}</div>

            {(c.options || []).filter(o => !o.free_text).map(o => {
              const isRed = (c.red_options || []).includes(o.id)   // definitively not buildable
              const isWarn = !isRed && (c.warn_options || []).includes(o.id)  // buildable, but NEW unscoped wire work
              return (
                <label key={o.id} title={(isRed || isWarn) ? (c.feasibility_reason || '') : ''}
                  style={{ display: 'flex', gap: 8, alignItems: 'flex-start', margin: '6px 0', cursor: isRed ? 'not-allowed' : 'pointer', opacity: isRed ? 0.65 : 1 }}>
                  <input type="radio" name={`recon-${c.id}`} checked={sel.chosen_option_id === o.id} disabled={isRed}
                    onChange={() => setAnswers(a => ({ ...a, [c.id]: { chosen_option_id: o.id } }))} />
                  <span style={{ fontSize: 12.5, color: isRed ? '#dc2626' : (isWarn ? '#d97706' : undefined), textDecoration: isRed ? 'line-through' : undefined }}>
                    {o.label}{isRed ? ' — not feasible' : ''}{isWarn ? ' — ⚠ new build (not in code or plan)' : ''}
                  </span>
                </label>
              )
            })}

            <input type="text" placeholder="…or type your own resolution"
              value={sel.custom_answer || ''}
              onChange={e => setAnswers(a => ({ ...a, [c.id]: { custom_answer: e.target.value } }))}
              style={{ width: '100%', marginTop: 4, padding: 6, borderRadius: 6, border: '1px solid var(--border)', background: 'var(--bg-input, #0b1021)', color: 'var(--text-primary)', fontSize: 12 }} />

            {answered(sel) && (
              <div style={{ marginTop: 6, fontSize: 12, color: '#16a34a' }}>
                ✓ {sel.chosen_option_id
                  ? (c.options || []).find(o => o.id === sel.chosen_option_id)?.label
                  : sel.custom_answer}
              </div>
            )}
          </div>
        )
      })}

      {err && <div style={{ color: '#dc2626', fontSize: 12, marginTop: 6 }}>{err}</div>}

      <button
        onClick={() => submit.mutate(Object.fromEntries(conflicts.map(c => [c.id, answers[c.id] || {}])))}
        disabled={submit.isPending || !allAnswered}
        style={{ marginTop: 8, padding: '8px 16px', fontWeight: 600, background: '#d97706', color: '#fff', border: 'none', borderRadius: 6, cursor: (submit.isPending || !allAnswered) ? 'not-allowed' : 'pointer', opacity: (submit.isPending || !allAnswered) ? 0.6 : 1 }}>
        {submit.isPending ? 'Submitting…' : allAnswered ? 'Resolve conflicts' : `Answer all ${conflicts.length} first`}
      </button>
      <button
        onClick={() => dismiss.mutate()}
        disabled={dismiss.isPending}
        title="Withdraw these conflicts without resolving — unblocks downstream (the uploaded doc stays as-is)."
        style={{ marginTop: 8, marginLeft: 8, padding: '8px 14px', fontSize: 12, background: 'transparent', color: 'var(--text-muted)', border: '1px solid var(--border)', borderRadius: 6, cursor: dismiss.isPending ? 'not-allowed' : 'pointer' }}>
        {dismiss.isPending ? 'Dismissing…' : 'Dismiss (not applicable)'}
      </button>
    </section>
  )
}


// S3 — the code check on the changes the user KEPT (brd-wins/custom). Advisory: the doc
// is already final, so this surfaces impact / risk / overturns and lets the user answer
// any open question the grounding raised (→ Decision Ledger), before they approve.
const RISK_COLOR = { high: '#dc2626', low: '#d97706', none: '#64748b' }
const RISK_LABEL = { high: 'HIGH RISK', low: 'LOW RISK', none: 'FYI' }

function GroundingCard({ summary, docLabel, mutation, ack }) {
  const [answers, setAnswers] = useState({})
  const [sent, setSent] = useState({})
  const deltas = summary?.deltas || []
  const needsAck = summary.overturns && !summary.acknowledged
  return (
    <section style={{ ...CARD,
      background: summary.overturns ? 'rgba(220,38,38,0.06)' : CARD.background,
      border: summary.overturns ? '1px solid rgba(220,38,38,0.45)' : CARD.border }}>
      <div style={{ fontSize: 14, fontWeight: 700 }}>🔍 Code check on your accepted changes</div>
      <div style={{ fontSize: 12, color: 'var(--text-muted)', margin: '4px 0 10px' }}>
        Your {docLabel} is final. The code check on the changes you kept flagged{' '}
        {deltas.length} item{deltas.length > 1 ? 's' : ''} worth reviewing.
      </div>
      {summary.overturns && (
        <div style={{ fontSize: 12.5, fontWeight: 600, color: '#dc2626', background: 'rgba(220,38,38,0.09)',
          border: '1px solid rgba(220,38,38,0.3)', borderRadius: 6, padding: '7px 10px', marginBottom: 10 }}>
          ⚠ One or more of these overturns a decision the plan already ratified — make sure that’s intended.
        </div>
      )}
      {needsAck && (
        <label style={{ display: 'flex', gap: 8, alignItems: 'flex-start', fontSize: 12.5, fontWeight: 600,
          margin: '0 0 12px', cursor: ack?.isPending ? 'wait' : 'pointer', color: 'var(--text-primary)' }}>
          <input type="checkbox" checked={false} disabled={ack?.isPending}
            onChange={() => ack?.mutate()} style={{ marginTop: 2 }} />
          <span>I understand this overturns a ratified decision — <b>this unblocks approval</b>.</span>
        </label>
      )}
      {summary.overturns && summary.acknowledged && (
        <div style={{ fontSize: 12, color: '#16a34a', marginBottom: 10 }}>✓ Acknowledged — approval unblocked.</div>
      )}
      {deltas.map((d, i) => (
        <div key={i} style={{ margin: '10px 0', paddingBottom: 10, borderBottom: '1px solid var(--border-subtle)' }}>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
            <span style={{ fontSize: 10, fontWeight: 700, padding: '1px 7px', borderRadius: 10,
              background: `${RISK_COLOR[d.risk] || RISK_COLOR.none}22`, color: RISK_COLOR[d.risk] || RISK_COLOR.none }}>
              {RISK_LABEL[d.risk] || RISK_LABEL.none}
            </span>
            {d.overturns_ratified && (
              <span style={{ fontSize: 10, fontWeight: 700, padding: '1px 7px', borderRadius: 10,
                background: 'rgba(220,38,38,0.13)', color: '#dc2626' }}>OVERTURNS PLAN</span>
            )}
            <span style={{ fontSize: 12.5, fontWeight: 600 }}>{d.directive}</span>
          </div>
          {d.impact && <div style={{ fontSize: 12.5, color: 'var(--text-secondary)', marginTop: 5 }}>{d.impact}</div>}
          {d.risk_note && <div style={{ fontSize: 12, color: RISK_COLOR[d.risk], marginTop: 3 }}>{d.risk_note}</div>}
          {d.question && (sent[i] ? (
            <div style={{ fontSize: 12, color: '#16a34a', marginTop: 8 }}>✓ Answer recorded — it’ll guide the downstream build.</div>
          ) : (
            <div style={{ marginTop: 8 }}>
              <div style={{ fontSize: 12.5, fontStyle: 'italic', color: 'var(--text-primary)', marginBottom: 5 }}>❔ {d.question}</div>
              <div style={{ display: 'flex', gap: 8 }}>
                <input type="text" placeholder="Answer to guide the code…" value={answers[i] || ''}
                  onChange={e => setAnswers(a => ({ ...a, [i]: e.target.value }))}
                  style={{ flex: 1, padding: 6, borderRadius: 6, border: '1px solid var(--border)',
                    background: 'var(--bg-input, #0b1021)', color: 'var(--text-primary)', fontSize: 12 }} />
                <button
                  onClick={() => { const answer = (answers[i] || '').trim(); if (!answer) return
                    mutation.mutate({ index: i, question: d.question, answer },
                      { onSuccess: () => setSent(s => ({ ...s, [i]: true })) }) }}
                  disabled={!(answers[i] || '').trim() || mutation.isPending}
                  style={{ padding: '6px 12px', fontSize: 12, fontWeight: 600, background: '#d97706', color: '#fff',
                    border: 'none', borderRadius: 6, cursor: 'pointer', opacity: !(answers[i] || '').trim() ? 0.5 : 1 }}>
                  Record
                </button>
              </div>
            </div>
          ))}
        </div>
      ))}
    </section>
  )
}
