// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useEffect, useMemo, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertCircle,
  CheckCircle,
  History,
  Loader,
  Save,
  ShieldAlert,
} from 'lucide-react'
import { evalApi } from '../../services/api'
import { useUiConfig } from '../../hooks/useUiConfig'

const POLICY_OPTIONS = ['disabled', 'advisory', 'soft_gate', 'hard_gate']
const PROD_CONFIRM_TEXT = 'I understand this changes production eval gates'

// Display order follows the change-request workflow so operators see
// checkpoints in the same sequence the PM walks through. Unknown
// checkpoints fall to the end while preserving their relative order.
const CHECKPOINT_DISPLAY_ORDER = [
  // Phase A — full Phase 7 coverage, in the order a change moves through
  'initial_to_prompt_enhanced',
  'prompt_to_research',
  'research_to_canvas',
  'canvas_to_clarification',
  'clarification_to_brd',
  'brd_to_tech_spec',
  'tech_spec_to_xsd',
  // Phase C handoff and partner-response gates
  'product_kit_to_phase_c_communication',
  'phase_c_query_to_po_response',
]

function sortCheckpoints(rows) {
  const indexOf = (id) => {
    const i = CHECKPOINT_DISPLAY_ORDER.indexOf(id)
    return i === -1 ? CHECKPOINT_DISPLAY_ORDER.length : i
  }
  return [...rows].sort((a, b) => {
    const ai = indexOf(a.checkpoint_id)
    const bi = indexOf(b.checkpoint_id)
    if (ai !== bi) return ai - bi
    return a.checkpoint_id.localeCompare(b.checkpoint_id)
  })
}

function prettyDate(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleString('en-IN', {
    day: '2-digit',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
    year: 'numeric',
  })
}

export default function EvalPolicy() {
  const queryClient = useQueryClient()
  const { data: uiConfig } = useUiConfig()
  const appEnv = (uiConfig?.app_env || 'development').toLowerCase()
  const isProduction = appEnv === 'production'

  const [edits, setEdits] = useState({})
  const [reason, setReason] = useState('')
  const [confirmProduction, setConfirmProduction] = useState(false)
  const [confirmText, setConfirmText] = useState('')
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState(null)

  const { data: policiesData, isLoading } = useQuery({
    queryKey: ['eval-policy-list'],
    queryFn: () => evalApi.listPolicies().then(r => r.data),
  })

  const { data: auditData, isFetching: auditFetching } = useQuery({
    queryKey: ['eval-policy-audit'],
    queryFn: () => evalApi.listPolicyAudit({ limit: 25 }).then(r => r.data),
  })

  const orderedPolicies = useMemo(
    () => sortCheckpoints(policiesData?.policies || []),
    [policiesData],
  )

  useEffect(() => {
    if (!policiesData?.policies) return
    const next = {}
    for (const row of policiesData.policies) next[row.checkpoint_id] = row.policy_mode
    setEdits(next)
  }, [policiesData])

  const originalMap = useMemo(() => {
    const map = {}
    for (const row of policiesData?.policies || []) map[row.checkpoint_id] = row.policy_mode
    return map
  }, [policiesData])

  const changedPolicies = useMemo(() => {
    const out = {}
    for (const [checkpoint, mode] of Object.entries(edits)) {
      if (originalMap[checkpoint] !== mode) out[checkpoint] = mode
    }
    return out
  }, [edits, originalMap])

  const hasChanges = Object.keys(changedPolicies).length > 0
  const reasonOk = reason.trim().length >= 8
  const prodConfirmOk = !isProduction || (confirmProduction && confirmText.trim() === PROD_CONFIRM_TEXT)
  const canSave = hasChanges && reasonOk && prodConfirmOk && !saving

  const handleSave = async () => {
    if (!canSave) return
    setSaving(true)
    setSaved(false)
    setError(null)
    try {
      await evalApi.updatePolicies({
        policies: changedPolicies,
        reason: reason.trim(),
        confirm_production: isProduction ? confirmProduction : false,
        confirm_text: isProduction ? confirmText.trim() : null,
      })
      setSaved(true)
      setReason('')
      setConfirmProduction(false)
      setConfirmText('')
      queryClient.invalidateQueries({ queryKey: ['eval-policy-list'] })
      queryClient.invalidateQueries({ queryKey: ['eval-policy-audit'] })
      setTimeout(() => setSaved(false), 3500)
    } catch (e) {
      setError(e?.response?.data?.detail || 'Failed to update policy modes')
    } finally {
      setSaving(false)
    }
  }

  if (isLoading) {
    return <div style={{ padding: '32px', fontSize: '13px', color: 'var(--text-muted)' }}>Loading eval policy…</div>
  }

  return (
    <div style={{ padding: '32px 40px', maxWidth: 1100, margin: '0 auto' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: '22px', gap: '14px' }}>
        <div>
          <h1 style={{ margin: '0 0 4px', fontSize: '20px', fontWeight: 600, color: 'var(--text-primary)' }}>
            Eval Policy
          </h1>
          <p style={{ margin: 0, fontSize: '13px', color: 'var(--text-muted)' }}>
            Checkpoint-level gate modes with required reason and full audit trail.
          </p>
        </div>
        <span style={{
          fontSize: '11px',
          padding: '4px 10px',
          borderRadius: '20px',
          background: isProduction ? 'rgba(224,108,108,0.12)' : 'rgba(76,175,125,0.10)',
          border: `1px solid ${isProduction ? 'rgba(224,108,108,0.35)' : 'rgba(76,175,125,0.30)'}`,
          color: isProduction ? 'var(--danger)' : 'var(--success)',
          fontWeight: 600,
        }}>
          Env: {appEnv}
        </span>
      </div>

      {saved && (
        <div style={{
          padding: '10px 14px',
          marginBottom: '14px',
          borderRadius: '8px',
          background: 'rgba(76,175,125,0.08)',
          border: '1px solid rgba(76,175,125,0.30)',
          color: 'var(--success)',
          display: 'flex',
          alignItems: 'center',
          gap: '8px',
          fontSize: '13px',
        }}>
          <CheckCircle size={14} /> Policy changes saved.
        </div>
      )}
      {error && (
        <div style={{
          padding: '10px 14px',
          marginBottom: '14px',
          borderRadius: '8px',
          background: 'rgba(224,108,108,0.08)',
          border: '1px solid rgba(224,108,108,0.28)',
          color: 'var(--danger)',
          display: 'flex',
          alignItems: 'center',
          gap: '8px',
          fontSize: '13px',
        }}>
          <AlertCircle size={14} /> {error}
        </div>
      )}

      <div style={{
        borderRadius: '8px',
        border: '1px solid var(--border)',
        background: 'var(--bg-elevated)',
        overflow: 'hidden',
        marginBottom: '18px',
      }}>
        <div style={{ padding: '14px 20px', borderBottom: '1px solid var(--border-subtle)', fontSize: '14px', fontWeight: 600, color: 'var(--text-primary)' }}>
          Checkpoint policy modes
        </div>
        <div style={{ padding: '14px 20px', display: 'grid', gap: '12px' }}>
          {orderedPolicies.map((row) => {
            const changed = originalMap[row.checkpoint_id] !== edits[row.checkpoint_id]
            return (
              <div key={row.checkpoint_id} style={{
                display: 'grid',
                gridTemplateColumns: '1fr 180px auto',
                gap: '10px',
                alignItems: 'center',
                padding: '10px 12px',
                borderRadius: '6px',
                border: changed ? '1px solid rgba(218,119,86,0.35)' : '1px solid var(--border-subtle)',
                background: changed ? 'rgba(218,119,86,0.05)' : 'var(--bg-base)',
              }}>
                <div>
                  <p style={{ margin: 0, fontSize: '13px', color: 'var(--text-primary)', fontWeight: 600 }}>
                    {row.checkpoint_id}
                  </p>
                  <p style={{ margin: '2px 0 0', fontSize: '11px', color: 'var(--text-muted)' }}>
                    Source: {row.source}
                  </p>
                </div>
                <select
                  value={edits[row.checkpoint_id] || row.policy_mode}
                  onChange={(e) => setEdits((prev) => ({ ...prev, [row.checkpoint_id]: e.target.value }))}
                  style={{
                    width: '100%',
                    padding: '7px 10px',
                    borderRadius: '6px',
                    border: '1px solid var(--border)',
                    background: 'var(--bg-input)',
                    color: 'var(--text-primary)',
                    fontSize: '12px',
                    fontFamily: 'inherit',
                  }}
                >
                  {POLICY_OPTIONS.map((mode) => (
                    <option key={mode} value={mode}>{mode}</option>
                  ))}
                </select>
                {changed && (
                  <span style={{
                    fontSize: '10px',
                    color: 'var(--accent)',
                    fontWeight: 700,
                    textTransform: 'uppercase',
                    letterSpacing: '0.05em',
                  }}>
                    changed
                  </span>
                )}
              </div>
            )
          })}
        </div>
      </div>

      <div style={{
        borderRadius: '8px',
        border: '1px solid var(--border)',
        background: 'var(--bg-elevated)',
        overflow: 'hidden',
        marginBottom: '18px',
      }}>
        <div style={{ padding: '14px 20px', borderBottom: '1px solid var(--border-subtle)', fontSize: '14px', fontWeight: 600, color: 'var(--text-primary)' }}>
          Change reason and confirmation
        </div>
        <div style={{ padding: '14px 20px', display: 'grid', gap: '10px' }}>
          <label style={{ fontSize: '11px', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.05em', fontWeight: 700 }}>
            Reason (required, min 8 chars)
          </label>
          <textarea
            rows={3}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="Explain why this policy change is needed."
            style={{
              width: '100%',
              padding: '9px 10px',
              borderRadius: '6px',
              border: '1px solid var(--border)',
              background: 'var(--bg-input)',
              color: 'var(--text-primary)',
              fontSize: '13px',
              fontFamily: 'inherit',
              resize: 'vertical',
            }}
          />

          {isProduction && (
            <div style={{
              padding: '10px 12px',
              borderRadius: '6px',
              background: 'rgba(224,108,108,0.08)',
              border: '1px solid rgba(224,108,108,0.28)',
              display: 'grid',
              gap: '8px',
            }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '7px', color: 'var(--danger)', fontSize: '12px', fontWeight: 600 }}>
                <ShieldAlert size={13} />
                Production strict confirmation
              </div>
              <label style={{ display: 'flex', gap: '7px', alignItems: 'center', fontSize: '12px', color: 'var(--text-secondary)' }}>
                <input
                  type="checkbox"
                  checked={confirmProduction}
                  onChange={(e) => setConfirmProduction(e.target.checked)}
                />
                I confirm this change affects production eval gating behavior.
              </label>
              <input
                value={confirmText}
                onChange={(e) => setConfirmText(e.target.value)}
                placeholder={PROD_CONFIRM_TEXT}
                style={{
                  width: '100%',
                  padding: '8px 10px',
                  borderRadius: '6px',
                  border: '1px solid var(--border)',
                  background: 'var(--bg-input)',
                  color: 'var(--text-primary)',
                  fontSize: '12px',
                  fontFamily: 'inherit',
                }}
              />
            </div>
          )}

          <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
            <button
              onClick={handleSave}
              disabled={!canSave}
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: '6px',
                padding: '8px 16px',
                borderRadius: '6px',
                border: canSave ? 'none' : '1px solid var(--border)',
                background: canSave ? 'var(--accent)' : 'var(--bg-base)',
                color: canSave ? 'white' : 'var(--text-muted)',
                fontSize: '13px',
                fontWeight: 700,
                cursor: canSave ? 'pointer' : 'not-allowed',
                opacity: canSave ? 1 : 0.7,
              }}
            >
              {saving ? <Loader size={13} style={{ animation: 'spin 1s linear infinite' }} /> : <Save size={13} />}
              Save policy updates
            </button>
          </div>
        </div>
      </div>

      <div style={{
        borderRadius: '8px',
        border: '1px solid var(--border)',
        background: 'var(--bg-elevated)',
        overflow: 'hidden',
      }}>
        <div style={{ padding: '14px 20px', borderBottom: '1px solid var(--border-subtle)', display: 'flex', alignItems: 'center', gap: '8px' }}>
          <History size={14} style={{ color: 'var(--accent)' }} />
          <span style={{ fontSize: '14px', fontWeight: 600, color: 'var(--text-primary)' }}>
            Recent policy changes
          </span>
          {auditFetching && <Loader size={12} style={{ marginLeft: 'auto', color: 'var(--text-muted)', animation: 'spin 1s linear infinite' }} />}
        </div>
        <div style={{ padding: '12px 20px' }}>
          {(auditData?.items || []).length === 0 ? (
            <p style={{ margin: 0, fontSize: '12px', color: 'var(--text-muted)' }}>
              No policy changes recorded yet.
            </p>
          ) : (
            <div style={{ display: 'grid', gap: '8px' }}>
              {(auditData.items || []).map((row) => (
                <div key={row.id} style={{
                  borderRadius: '6px',
                  border: '1px solid var(--border-subtle)',
                  background: 'var(--bg-base)',
                  padding: '9px 10px',
                }}>
                  <p style={{ margin: '0 0 3px', fontSize: '12px', color: 'var(--text-primary)', fontWeight: 600 }}>
                    {row.checkpoint_id}: {row.old_policy_mode} → {row.new_policy_mode}
                  </p>
                  <p style={{ margin: '0 0 3px', fontSize: '11px', color: 'var(--text-secondary)' }}>
                    {row.reason}
                  </p>
                  <p style={{ margin: 0, fontSize: '10px', color: 'var(--text-muted)' }}>
                    {row.actor_username} · {row.app_env} · {prettyDate(row.created_at)}
                  </p>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
