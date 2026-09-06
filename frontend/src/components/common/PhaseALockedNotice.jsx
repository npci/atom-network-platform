// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { Lock, ArrowLeft } from 'lucide-react'

// Shown on /changes/:id/phase-b and /changes/:id/phase-c when someone deep-links
// to a change whose Phase A hasn't reached `completed` yet. ChangeDetail already
// hides the corresponding entry blocks (ChangeDetail.jsx:3343 for Phase B,
// :3462 for Phase C) — this catches the direct-URL path so those pages never
// half-render against a change that isn't ready.
export default function PhaseALockedNotice({ changeId, changeTitle, phaseLabel, navigate }) {
  return (
    <div style={{
      minHeight: 'calc(100vh - 0px)', display: 'flex', flexDirection: 'column',
      background: 'var(--bg-base)',
    }}>
      <div style={{
        padding: '16px 24px', borderBottom: '1px solid var(--border)',
        display: 'flex', alignItems: 'center', gap: 16, background: 'var(--bg-base)',
      }}>
        <button
          onClick={() => navigate(`/changes/${changeId}`)}
          style={{
            display: 'flex', alignItems: 'center', gap: 5, background: 'none',
            border: 'none', cursor: 'pointer', fontSize: 13, color: 'var(--text-muted)',
          }}
        >
          <ArrowLeft size={14} /> Back to change
        </button>
        <div style={{ flex: 1 }}>
          <h1 style={{ margin: 0, fontSize: 16, fontWeight: 600, color: 'var(--text-primary)' }}>
            {phaseLabel}
          </h1>
          {changeTitle && (
            <p style={{ margin: 0, fontSize: 11, color: 'var(--text-muted)' }}>
              {changeTitle}
            </p>
          )}
        </div>
      </div>
      <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 32 }}>
        <div style={{
          maxWidth: 520, textAlign: 'center',
          padding: '32px 28px',
          background: 'var(--bg-elevated)',
          border: '1px solid var(--border)', borderRadius: 12,
        }}>
          <div style={{
            width: 44, height: 44, borderRadius: '50%', margin: '0 auto 16px',
            background: 'rgba(218,119,86,0.12)', display: 'flex',
            alignItems: 'center', justifyContent: 'center',
          }}>
            <Lock size={20} style={{ color: 'var(--accent)' }} />
          </div>
          <h2 style={{ margin: '0 0 8px', fontSize: 16, fontWeight: 600, color: 'var(--text-primary)' }}>
            {phaseLabel} is locked
          </h2>
          <p style={{ margin: '0 0 20px', fontSize: 13, lineHeight: 1.55, color: 'var(--text-secondary)' }}>
            Complete Phase A (BRD, Tech Spec, XSD, Product Kit) for this change before
            starting {phaseLabel}.
          </p>
          <button
            onClick={() => navigate(`/changes/${changeId}`)}
            style={{
              display: 'inline-flex', alignItems: 'center', gap: 6,
              padding: '9px 18px', background: 'var(--accent)', color: 'white',
              border: 'none', borderRadius: 7, fontSize: 13, fontWeight: 600,
              cursor: 'pointer',
            }}
          >
            <ArrowLeft size={13} /> Go to Phase A
          </button>
        </div>
      </div>
    </div>
  )
}
