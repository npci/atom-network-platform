// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useState } from 'react'
import { X, Loader, Eye, EyeOff, CheckCircle, AlertCircle } from 'lucide-react'
import { authApi } from '../../services/api'

// Self-contained password-change dialog. Three fields, client-side
// validation (matches the backend's Standard policy at api/auth.py:
// ≥8 chars + ≥1 letter + ≥1 digit + differs from current), single
// POST, success closes the modal. The user keeps their existing JWT
// — no forced re-login.
//
// Usage:
//   <ChangePasswordModal open={open} onClose={() => setOpen(false)} />
export default function ChangePasswordModal({ open, onClose }) {
  const [current, setCurrent] = useState('')
  const [next, setNext]       = useState('')
  const [confirm, setConfirm] = useState('')
  const [show, setShow]       = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError]   = useState(null)
  const [success, setSuccess] = useState(false)

  if (!open) return null

  // Client-side policy check — mirrors backend so the user gets
  // immediate feedback before the network round-trip.
  function clientValidate() {
    if (!current) return 'Enter your current password'
    if (next.length < 8) return 'New password must be at least 8 characters'
    if (!/[A-Za-z]/.test(next)) return 'New password must contain at least one letter'
    if (!/\d/.test(next)) return 'New password must contain at least one digit'
    if (next === current) return 'New password must differ from the current one'
    if (next !== confirm) return 'New password and confirmation do not match'
    return null
  }

  async function onSubmit(e) {
    e.preventDefault()
    const ce = clientValidate()
    if (ce) { setError(ce); return }

    setError(null); setSubmitting(true)
    try {
      await authApi.changePassword(current, next)
      setSuccess(true)
      // Brief celebration then close — user keeps session.
      setTimeout(() => {
        setSuccess(false); setCurrent(''); setNext(''); setConfirm('')
        onClose?.()
      }, 1100)
    } catch (err) {
      setError(err.response?.data?.detail || err.message || 'Password change failed')
    } finally {
      setSubmitting(false)
    }
  }

  function handleClose() {
    if (submitting) return
    setError(null); setSuccess(false)
    setCurrent(''); setNext(''); setConfirm('')
    onClose?.()
  }

  const inputStyle = {
    width: '100%',
    padding: '9px 12px',
    background: 'var(--bg-base)',
    border: '1px solid var(--border)',
    borderRadius: '6px',
    color: 'var(--text-primary)',
    fontSize: '13px',
    outline: 'none',
    fontFamily: 'inherit',
  }
  const labelStyle = {
    display: 'block',
    fontSize: '11px', fontWeight: 600, letterSpacing: '0.04em',
    textTransform: 'uppercase', color: 'var(--text-muted)',
    marginBottom: '6px',
  }

  return (
    <div
      onClick={handleClose}
      style={{
        position: 'fixed', inset: 0, zIndex: 1000,
        background: 'rgba(0,0,0,0.55)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        padding: '20px',
      }}
    >
      <form
        onClick={(e) => e.stopPropagation()}
        onSubmit={onSubmit}
        style={{
          width: 440, maxWidth: '92vw',
          padding: 24,
          background: 'var(--bg-elevated)',
          border: '1px solid var(--border)',
          borderRadius: 10,
          boxShadow: '0 12px 40px rgba(0,0,0,0.35)',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 18 }}>
          <h2 style={{ margin: 0, fontSize: 16, fontWeight: 600, color: 'var(--text-primary)' }}>
            Change Password
          </h2>
          <button
            type="button" onClick={handleClose} disabled={submitting}
            style={{ background: 'none', border: 'none', color: 'var(--text-muted)', cursor: submitting ? 'wait' : 'pointer', padding: 4 }}
            title="Close"
          >
            <X size={16} />
          </button>
        </div>

        <p style={{ fontSize: 12, color: 'var(--text-muted)', margin: '0 0 16px', lineHeight: 1.5 }}>
          Choose a password with at least 8 characters including a letter and a digit.
        </p>

        {/* Current */}
        <div style={{ marginBottom: 14 }}>
          <label style={labelStyle}>Current password</label>
          <input
            type={show ? 'text' : 'password'}
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
            disabled={submitting || success}
            autoComplete="current-password"
            style={inputStyle}
          />
        </div>

        {/* New */}
        <div style={{ marginBottom: 14 }}>
          <label style={labelStyle}>New password</label>
          <input
            type={show ? 'text' : 'password'}
            value={next}
            onChange={(e) => setNext(e.target.value)}
            disabled={submitting || success}
            autoComplete="new-password"
            style={inputStyle}
          />
        </div>

        {/* Confirm + show-toggle */}
        <div style={{ marginBottom: 16 }}>
          <label style={labelStyle}>Confirm new password</label>
          <input
            type={show ? 'text' : 'password'}
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            disabled={submitting || success}
            autoComplete="new-password"
            style={inputStyle}
          />
          <button
            type="button" onClick={() => setShow(v => !v)}
            style={{
              display: 'inline-flex', alignItems: 'center', gap: 4,
              marginTop: 8, padding: 0,
              fontSize: 11, color: 'var(--text-muted)',
              background: 'none', border: 'none', cursor: 'pointer',
            }}
          >
            {show ? <EyeOff size={11} /> : <Eye size={11} />}
            {show ? 'Hide passwords' : 'Show passwords'}
          </button>
        </div>

        {/* Inline status */}
        {error && (
          <div style={{
            padding: '8px 12px', borderRadius: 6, marginBottom: 12,
            background: 'rgba(224,108,108,0.10)',
            border: '1px solid rgba(224,108,108,0.30)',
            color: '#e06c6c', fontSize: 12,
            display: 'flex', alignItems: 'center', gap: 8,
          }}>
            <AlertCircle size={13} /> {error}
          </div>
        )}
        {success && (
          <div style={{
            padding: '8px 12px', borderRadius: 6, marginBottom: 12,
            background: 'rgba(76,175,125,0.10)',
            border: '1px solid rgba(76,175,125,0.30)',
            color: '#4caf7d', fontSize: 12,
            display: 'flex', alignItems: 'center', gap: 8,
          }}>
            <CheckCircle size={13} /> Password changed
          </div>
        )}

        {/* Actions */}
        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          <button
            type="button" onClick={handleClose} disabled={submitting}
            style={{
              padding: '8px 14px',
              background: 'transparent',
              border: '1px solid var(--border)',
              borderRadius: 6,
              fontSize: 13, color: 'var(--text-secondary)',
              cursor: submitting ? 'wait' : 'pointer',
            }}
          >
            Cancel
          </button>
          <button
            type="submit" disabled={submitting || success}
            style={{
              display: 'inline-flex', alignItems: 'center', gap: 6,
              padding: '8px 16px',
              background: 'var(--accent)',
              color: 'white',
              border: 'none', borderRadius: 6,
              fontSize: 13, fontWeight: 600,
              cursor: submitting ? 'wait' : 'pointer',
              opacity: submitting || success ? 0.7 : 1,
            }}
          >
            {submitting && <Loader size={12} style={{ animation: 'spin 1s linear infinite' }} />}
            {submitting ? 'Saving…' : success ? 'Done' : 'Change password'}
          </button>
        </div>
      </form>
    </div>
  )
}
