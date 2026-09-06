// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useMemo, useState } from 'react'
import { AlertTriangle, Loader, ShieldAlert, X } from 'lucide-react'
import WhyBlockedPanel from './WhyBlockedPanel'

export default function GateBlockedModal({
  detail,
  actionLabel = 'Proceed',
  isAdmin = false,
  busy = false,
  error = null,
  onClose,
  onRetry,
  onAcknowledge,
  onOverride,
}) {
  const [overrideReason, setOverrideReason] = useState('')
  const isSoftAck = Boolean(detail?.requires_ack && detail?.required_ack_verdict_id)
  const isHardGate = detail?.policy_mode === 'hard_gate'
  const canOverride = Boolean(isAdmin && detail?.override_allowed && isHardGate)
  const showRetry = Boolean(onRetry && detail?.retry_available)

  const title = useMemo(() => {
    if (isSoftAck) return 'Proceed requires acknowledgement'
    return 'Transition blocked by eval gate'
  }, [isSoftAck])

  return (
    <div
      onClick={() => !busy && onClose?.()}
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 1300,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        background: 'rgba(0,0,0,0.55)',
        padding: '20px',
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: 'min(820px, 96vw)',
          maxHeight: '88vh',
          overflowY: 'auto',
          borderRadius: '10px',
          border: '1px solid var(--border)',
          background: 'var(--bg-card)',
          boxShadow: '0 12px 40px rgba(0,0,0,0.35)',
        }}
      >
        <div style={{
          display: 'flex',
          alignItems: 'flex-start',
          gap: '10px',
          padding: '14px 16px',
          borderBottom: '1px solid var(--border-subtle)',
        }}>
          <AlertTriangle size={18} style={{ color: 'var(--accent)', flexShrink: 0, marginTop: '1px' }} />
          <div style={{ flex: 1 }}>
            <p style={{ margin: 0, fontSize: '15px', fontWeight: 700, color: 'var(--text-primary)' }}>
              {title}
            </p>
            <p style={{ margin: '4px 0 0', fontSize: '12px', color: 'var(--text-muted)' }}>
              Action: {actionLabel}
            </p>
          </div>
          <button
            type="button"
            onClick={() => !busy && onClose?.()}
            disabled={busy}
            style={{
              border: 'none',
              background: 'none',
              color: 'var(--text-muted)',
              cursor: busy ? 'not-allowed' : 'pointer',
              padding: '4px',
            }}
            title="Close"
          >
            <X size={16} />
          </button>
        </div>

        <div style={{ padding: '14px 16px' }}>
          <WhyBlockedPanel detail={detail} />

          {error && (
            <div style={{
              marginTop: '10px',
              padding: '9px 10px',
              borderRadius: '6px',
              background: 'rgba(224,108,108,0.10)',
              border: '1px solid rgba(224,108,108,0.25)',
              color: 'var(--danger)',
              fontSize: '12px',
            }}>
              {error}
            </div>
          )}

          {isHardGate && !isAdmin && (
            <div style={{
              marginTop: '10px',
              padding: '9px 10px',
              borderRadius: '6px',
              background: 'rgba(100,160,200,0.10)',
              border: '1px solid rgba(100,160,200,0.25)',
              color: '#64a0c8',
              fontSize: '12px',
              display: 'flex',
              alignItems: 'center',
              gap: '6px',
            }}>
              <ShieldAlert size={13} />
              Contact an admin to request an override for this checkpoint.
            </div>
          )}

          {canOverride && (
            <div style={{ marginTop: '12px' }}>
              <label style={{
                display: 'block',
                fontSize: '11px',
                color: 'var(--text-muted)',
                textTransform: 'uppercase',
                letterSpacing: '0.05em',
                fontWeight: 700,
                marginBottom: '6px',
              }}>
                Override reason
              </label>
              <textarea
                value={overrideReason}
                onChange={(e) => setOverrideReason(e.target.value)}
                rows={3}
                placeholder="Required audit reason (minimum 8 characters)."
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
            </div>
          )}

          <div style={{
            marginTop: '14px',
            display: 'flex',
            gap: '8px',
            justifyContent: 'flex-end',
            flexWrap: 'wrap',
          }}>
            <button
              type="button"
              onClick={() => onClose?.()}
              disabled={busy}
              style={{
                padding: '8px 12px',
                borderRadius: '6px',
                border: '1px solid var(--border)',
                background: 'transparent',
                color: 'var(--text-secondary)',
                fontSize: '12px',
                cursor: busy ? 'not-allowed' : 'pointer',
              }}
            >
              Close
            </button>

            {showRetry && (
              <button
                type="button"
                onClick={() => onRetry?.()}
                disabled={busy}
                style={{
                  padding: '8px 12px',
                  borderRadius: '6px',
                  border: '1px solid rgba(218,119,86,0.35)',
                  background: 'rgba(218,119,86,0.10)',
                  color: 'var(--accent)',
                  fontSize: '12px',
                  fontWeight: 600,
                  cursor: busy ? 'not-allowed' : 'pointer',
                }}
              >
                Retry check ({detail.retries_used}/{detail.max_retries})
              </button>
            )}

            {isSoftAck && (
              <button
                type="button"
                onClick={() => onAcknowledge?.()}
                disabled={busy}
                style={{
                  padding: '8px 12px',
                  borderRadius: '6px',
                  border: 'none',
                  background: 'var(--accent)',
                  color: 'white',
                  fontSize: '12px',
                  fontWeight: 700,
                  cursor: busy ? 'not-allowed' : 'pointer',
                  opacity: busy ? 0.75 : 1,
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: '6px',
                }}
              >
                {busy && <Loader size={12} style={{ animation: 'spin 1s linear infinite' }} />}
                Acknowledge and continue
              </button>
            )}

            {canOverride && (
              <button
                type="button"
                onClick={() => onOverride?.(overrideReason)}
                disabled={busy || overrideReason.trim().length < 8}
                style={{
                  padding: '8px 12px',
                  borderRadius: '6px',
                  border: 'none',
                  background: '#c35f47',
                  color: 'white',
                  fontSize: '12px',
                  fontWeight: 700,
                  cursor: (busy || overrideReason.trim().length < 8) ? 'not-allowed' : 'pointer',
                  opacity: (busy || overrideReason.trim().length < 8) ? 0.7 : 1,
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: '6px',
                }}
              >
                {busy && <Loader size={12} style={{ animation: 'spin 1s linear infinite' }} />}
                Override and continue
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
