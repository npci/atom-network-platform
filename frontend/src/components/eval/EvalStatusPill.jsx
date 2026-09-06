// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useCallback, useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Loader, RefreshCw, ShieldAlert, Wand2, X } from 'lucide-react'
import { useAuth } from '../../hooks/useAuth'
import { evalApi } from '../../services/api'
import { buildAutoFixFeedback } from '../../lib/evalAutoFix'
import WhyBlockedPanel from './WhyBlockedPanel'

// EvalStatusPill — header badge for a checkpoint verdict. Click to inspect
// the reasons that produced the verdict, and (when the user is admin and the
// verdict is gating) override it inline without having to attempt /advance.
// When the parent page provides `onAutoFix`, an "Auto-fix and retry" button
// also appears: it builds a feedback prompt from the verdict's findings and
// hands it to the page, which forwards it into the existing artifact-
// generation WebSocket.
export default function EvalStatusPill({
  verdict,
  checkpointId,
  checkpointLabel,
  changeId,
  onAutoFix,
}) {
  const { user } = useAuth()
  const queryClient = useQueryClient()
  const verdictValue = verdict?.verdict || 'NO_VERDICT'
  const policyMode = verdict?.policy_mode || 'unknown'
  const confidence = (typeof verdict?.confidence === 'number')
    ? ` · ${Math.round(verdict.confidence * 100)}%`
    : ''

  const [open, setOpen] = useState(false)
  const [reason, setReason] = useState('')
  const [manualFeedback, setManualFeedback] = useState('')
  const [awaitingAutoFix, setAwaitingAutoFix] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  // `evaluating` drives the spinning "Evaluating…" pill so the user can see an
  // eval is running server-side and doesn't skip the step before it lands.
  const [evaluating, setEvaluating] = useState(false)
  const wrapperRef = useRef(null)
  const currentVerdictIdRef = useRef(verdict?.id || null)
  const evalStartIdRef = useRef(null)

  useEffect(() => {
    if (!open) return undefined
    const onDocClick = (e) => {
      if (wrapperRef.current && !wrapperRef.current.contains(e.target)) {
        setOpen(false)
        setError(null)
      }
    }
    const onEsc = (e) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDocClick)
    document.addEventListener('keydown', onEsc)
    return () => {
      document.removeEventListener('mousedown', onDocClick)
      document.removeEventListener('keydown', onEsc)
    }
  }, [open])

  const scheduleEvalRefresh = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ['eval-latest', changeId, checkpointId] })
    for (const delay of [1500, 4000, 9000, 15000]) {
      window.setTimeout(() => {
        queryClient.invalidateQueries({ queryKey: ['eval-latest', changeId, checkpointId] })
      }, delay)
    }
  }, [changeId, checkpointId, queryClient])

  useEffect(() => {
    if (!awaitingAutoFix) return undefined
    const onAgentDone = (event) => {
      if (event?.detail?.changeId !== changeId) return
      setAwaitingAutoFix(false)
      scheduleEvalRefresh()
    }
    window.addEventListener('agent-ws-done', onAgentDone)
    return () => window.removeEventListener('agent-ws-done', onAgentDone)
  }, [awaitingAutoFix, changeId, scheduleEvalRefresh])

  // Keep a live ref of the current verdict id so the agent-done handler can
  // remember which verdict pre-dated the eval it's about to wait for.
  useEffect(() => { currentVerdictIdRef.current = verdict?.id || null }, [verdict?.id])

  // When any artifact finishes generating for this change, the advisory eval
  // fires server-side right after. Show the spinning "Evaluating…" pill and
  // poll for the fresh verdict.
  useEffect(() => {
    const onAgentDone = (event) => {
      if (event?.detail?.changeId !== changeId) return
      evalStartIdRef.current = currentVerdictIdRef.current
      setEvaluating(true)
      scheduleEvalRefresh()
    }
    window.addEventListener('agent-ws-done', onAgentDone)
    return () => window.removeEventListener('agent-ws-done', onAgentDone)
  }, [changeId, scheduleEvalRefresh])

  // Stop spinning once a fresh verdict lands (its id differs from the one that
  // existed when eval started).
  useEffect(() => {
    if (evaluating && (verdict?.id || null) !== evalStartIdRef.current) {
      setEvaluating(false)
    }
  }, [verdict?.id, evaluating])

  // Safety net — never spin forever if the verdict never arrives.
  useEffect(() => {
    if (!evaluating) return undefined
    const t = window.setTimeout(() => setEvaluating(false), 60000)
    return () => window.clearTimeout(t)
  }, [evaluating])

  const palette = verdictValue === 'PASS'
    ? { bg: 'rgba(76,175,125,0.10)', color: 'var(--success)', border: 'rgba(76,175,125,0.30)' }
    : verdictValue === 'WARN'
      ? { bg: 'rgba(218,119,86,0.10)', color: 'var(--accent)', border: 'rgba(218,119,86,0.30)' }
      : verdictValue === 'FAIL'
        ? { bg: 'rgba(224,108,108,0.10)', color: 'var(--danger)', border: 'rgba(224,108,108,0.30)' }
        : { bg: 'var(--bg-elevated)', color: 'var(--text-muted)', border: 'var(--border)' }

  // Distinct in-progress look so a running eval reads clearly as "working".
  const evaluatingPalette = {
    bg: 'rgba(100,160,200,0.12)', color: '#64a0c8', border: 'rgba(100,160,200,0.35)',
  }
  const activePalette = evaluating ? evaluatingPalette : palette
  const label = verdictValue === 'NO_VERDICT' ? 'No verdict yet' : `${verdictValue}${confidence}`
  const hasVerdict = Boolean(verdict && verdict.id)
  const isAdmin = user?.role === 'admin'
  const isOverridable = hasVerdict && (verdictValue === 'FAIL' || verdictValue === 'WARN')
  const canOverride = Boolean(isAdmin && isOverridable && changeId && checkpointId)
  const canAutoFix = Boolean(isOverridable && typeof onAutoFix === 'function')

  // Shape expected by WhyBlockedPanel — built from the stored verdict row.
  const detail = hasVerdict
    ? {
      checkpoint_id: checkpointId || verdict.checkpoint_id,
      policy_mode: verdict.policy_mode,
      verdict: verdict.verdict,
      reason: verdict.reasons?.[0] || '',
      reasons: verdict.reasons || [],
      hard_fail_codes: verdict.hard_fail_codes || [],
      warn_codes: verdict.warn_codes || [],
      source_artifact_ids: verdict.source_artifact_ids || [],
      target_artifact_ids: verdict.target_artifact_ids || [],
      verdict_id: verdict.id,
    }
    : null

  const handleAutoFix = () => {
    setError(null)
    if (!canAutoFix) return
    const feedback = buildAutoFixFeedback(verdict, { tag: checkpointId, manualFeedback })
    try {
      onAutoFix(feedback, { verdict, checkpointId })
      setAwaitingAutoFix(true)
      setManualFeedback('')
      setOpen(false)
    } catch (err) {
      const msg = err?.message || 'Auto-fix failed'
      setError(msg)
    }
  }

  const handleOverride = async () => {
    setError(null)
    const trimmed = (reason || '').trim()
    if (trimmed.length < 8) {
      setError('Override reason must be at least 8 characters.')
      return
    }
    setBusy(true)
    try {
      await evalApi.override(changeId, {
        checkpoint_id: checkpointId || verdict.checkpoint_id,
        reason: trimmed,
        previous_verdict_id: verdict.id,
      })
      // Refresh the parent's eval-latest query so the pill updates.
      queryClient.invalidateQueries({ queryKey: ['eval-latest', changeId, checkpointId] })
      setOpen(false)
      setReason('')
    } catch (err) {
      const msg = err?.response?.data?.detail || err?.message || 'Override failed'
      setError(typeof msg === 'string' ? msg : 'Override failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div ref={wrapperRef} style={{ position: 'relative', display: 'inline-flex' }}>
      <button
        type="button"
        onClick={() => hasVerdict && setOpen((v) => !v)}
        disabled={!hasVerdict}
        title={evaluating
          ? `${checkpointLabel || 'Eval checkpoint'} · evaluation in progress…`
          : hasVerdict
            ? `${checkpointLabel || 'Eval checkpoint'} · ${policyMode} · click for details`
            : `${checkpointLabel || 'Eval checkpoint'} · waiting for first verdict`}
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          gap: '6px',
          padding: '3px 10px',
          borderRadius: '999px',
          border: `1px solid ${activePalette.border}`,
          background: activePalette.bg,
          color: activePalette.color,
          fontSize: '11px',
          fontWeight: 600,
          whiteSpace: 'nowrap',
          cursor: hasVerdict ? 'pointer' : 'default',
          fontFamily: 'inherit',
        }}
      >
        <span style={{ opacity: 0.8, fontSize: '10px', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
          Eval
        </span>
        {evaluating ? (
          <>
            <Loader size={11} style={{ animation: 'spin 1s linear infinite' }} />
            <span>Evaluating…</span>
          </>
        ) : (
          <>
            <span>{label}</span>
            <span style={{ opacity: 0.75 }}>({policyMode})</span>
          </>
        )}
      </button>

      {open && hasVerdict && (
        <div
          role="dialog"
          style={{
            position: 'absolute',
            top: 'calc(100% + 6px)',
            right: 0,
            width: 'min(460px, 92vw)',
            zIndex: 1200,
            background: 'var(--bg-card)',
            border: '1px solid var(--border)',
            borderRadius: '10px',
            boxShadow: '0 10px 30px rgba(0,0,0,0.30)',
            padding: '14px',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'flex-start', gap: '10px', marginBottom: '4px' }}>
            <div style={{ flex: 1 }}>
              <p style={{ margin: 0, fontSize: '13px', fontWeight: 700, color: 'var(--text-primary)' }}>
                {checkpointLabel || 'Eval verdict'}
              </p>
              <p style={{ margin: '2px 0 0', fontSize: '11px', color: 'var(--text-muted)' }}>
                Latest verdict · policy mode <strong>{policyMode}</strong>
              </p>
            </div>
            <button
              type="button"
              onClick={() => setOpen(false)}
              style={{
                border: 'none',
                background: 'transparent',
                color: 'var(--text-muted)',
                cursor: 'pointer',
                padding: '2px',
              }}
              title="Close"
            >
              <X size={14} />
            </button>
          </div>

          <WhyBlockedPanel detail={detail} />

          {/* Auto-fix and retry — pushes a feedback prompt into the existing
              artifact-generation flow so the model can address the findings
              and produce a new verdict. */}
          {canAutoFix && (
            <div style={{ marginTop: '12px' }}>
              <div style={{
                padding: '10px 12px',
                borderRadius: '6px',
                border: '1px solid rgba(218,119,86,0.30)',
                background: 'rgba(218,119,86,0.06)',
              }}>
                <div style={{ display: 'flex', alignItems: 'flex-start', gap: '10px' }}>
                  <Wand2 size={14} style={{ color: 'var(--accent)', marginTop: '2px', flexShrink: 0 }} />
                  <div style={{ flex: 1 }}>
                    <p style={{
                      margin: '0 0 4px',
                      fontSize: '12px',
                      fontWeight: 700,
                      color: 'var(--text-primary)',
                    }}>
                      Auto-fix and retry
                    </p>
                    <p style={{ margin: 0, fontSize: '11px', color: 'var(--text-muted)', lineHeight: 1.5 }}>
                      The harness will hand the findings above back to the
                      generator with explicit fix instructions. A new verdict
                      is written automatically when the artifact is regenerated.
                    </p>
                    <textarea
                      value={manualFeedback}
                      onChange={(e) => setManualFeedback(e.target.value)}
                      rows={2}
                      placeholder="Optional: add your manual guidance. It will be appended after the critic findings."
                      style={{
                        width: '100%',
                        marginTop: '8px',
                        padding: '7px 9px',
                        borderRadius: '6px',
                        border: '1px solid var(--border)',
                        background: 'var(--bg-input)',
                        color: 'var(--text-primary)',
                        fontSize: '11px',
                        resize: 'vertical',
                        fontFamily: 'inherit',
                      }}
                    />
                  </div>
                  <button
                    type="button"
                    onClick={handleAutoFix}
                    title="Send the findings back into the generator to regenerate the artifact"
                    style={{
                      display: 'inline-flex', alignItems: 'center', gap: '6px',
                      padding: '7px 11px', borderRadius: '6px',
                      border: '1px solid rgba(218,119,86,0.35)',
                      background: 'rgba(218,119,86,0.10)',
                      color: 'var(--accent)',
                      fontSize: '12px', fontWeight: 700, cursor: 'pointer',
                      flexShrink: 0,
                    }}
                  >
                    <RefreshCw size={12} /> Auto-fix
                  </button>
                </div>
              </div>
            </div>
          )}

          {/* Read-only context when there's nothing to override */}
          {!canOverride && isOverridable && !isAdmin && (
            <div style={{
              marginTop: '10px',
              padding: '9px 10px',
              borderRadius: '6px',
              background: 'rgba(100,160,200,0.10)',
              border: '1px solid rgba(100,160,200,0.25)',
              color: '#64a0c8',
              fontSize: '11px',
              display: 'flex', alignItems: 'center', gap: '6px',
            }}>
              <ShieldAlert size={12} /> Contact an admin to override this verdict.
            </div>
          )}

          {/* Override section — admin only, FAIL/WARN only */}
          {canOverride && (
            <div style={{ marginTop: '12px' }}>
              <label style={{
                display: 'block', marginBottom: '5px',
                fontSize: '10px', fontWeight: 700, letterSpacing: '0.05em',
                textTransform: 'uppercase', color: 'var(--text-muted)',
              }}>
                Override reason (min 8 chars)
              </label>
              <textarea
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                rows={3}
                placeholder="Explain why this verdict should be manually unblocked."
                style={{
                  width: '100%',
                  padding: '8px 10px',
                  borderRadius: '6px',
                  border: '1px solid var(--border)',
                  background: 'var(--bg-input)',
                  color: 'var(--text-primary)',
                  fontSize: '12px',
                  resize: 'vertical',
                  fontFamily: 'inherit',
                }}
              />
              {error && (
                <div style={{
                  marginTop: '8px',
                  padding: '7px 9px',
                  borderRadius: '6px',
                  background: 'rgba(224,108,108,0.10)',
                  border: '1px solid rgba(224,108,108,0.25)',
                  color: 'var(--danger)',
                  fontSize: '11px',
                }}>{error}</div>
              )}
              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '8px', marginTop: '10px' }}>
                <button
                  type="button"
                  onClick={() => setOpen(false)}
                  disabled={busy}
                  style={{
                    padding: '7px 11px', borderRadius: '6px',
                    border: '1px solid var(--border)', background: 'transparent',
                    color: 'var(--text-secondary)', fontSize: '12px',
                    cursor: busy ? 'not-allowed' : 'pointer',
                  }}
                >Close</button>
                <button
                  type="button"
                  onClick={handleOverride}
                  disabled={busy || reason.trim().length < 8}
                  style={{
                    display: 'inline-flex', alignItems: 'center', gap: '6px',
                    padding: '7px 12px', borderRadius: '6px',
                    border: 'none', background: '#c35f47', color: 'white',
                    fontSize: '12px', fontWeight: 700,
                    cursor: (busy || reason.trim().length < 8) ? 'not-allowed' : 'pointer',
                    opacity: (busy || reason.trim().length < 8) ? 0.7 : 1,
                  }}
                >
                  {busy && <Loader size={12} style={{ animation: 'spin 1s linear infinite' }} />}
                  Override and continue
                </button>
              </div>
              <p style={{ margin: '6px 0 0', fontSize: '10px', color: 'var(--text-muted)' }}>
                The override is recorded in the audit trail with your username
                and reason. After overriding, this checkpoint will read PASS
                until a new verdict is written.
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
