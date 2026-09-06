// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useState, useEffect, useCallback } from 'react'
import { t } from '../strings'
import { useAuth } from '../hooks/useAuth'
import { authApi } from '../services/api'
import { BRAND_NAME, brandLogo } from '../brand'

// Login always renders in light theme regardless of user preference.
const light = {
  bgBase:      '#f9f9f8',
  bgCard:      '#ffffff',
  border:      '#e5e5e3',
  textPrimary: '#1a1a18',
  textSecondary:'#6b6b68',
  textMuted:   '#9b9b97',
  accent:      '#da7756',
  accentHover: '#c96a47',
  danger:      '#c43a3a',
  inputBg:     '#ffffff',
}

const inputStyle = {
  width: '100%', padding: '10px 14px', background: light.inputBg,
  border: `1px solid ${light.border}`, borderRadius: '6px',
  color: light.textPrimary, fontSize: '14px', outline: 'none', transition: 'border-color 0.15s',
}
const labelStyle = {
  display: 'block', fontSize: '13px', color: light.textSecondary, marginBottom: '6px', fontWeight: '500',
}
function primaryBtn(loading) {
  return {
    width: '100%', padding: '10px 16px', background: loading ? light.textMuted : light.accent,
    color: 'white', border: 'none', borderRadius: '6px', fontSize: '14px', fontWeight: '600',
    cursor: loading ? 'not-allowed' : 'pointer', transition: 'background 0.15s',
  }
}

// Guided-Steps progress rail (design direction C). Two real steps — CAPTCHA
// lives on the credentials screen — so the user sees the verification step
// coming before they reach it, instead of a surprise second screen.
function StepRail({ s1, s2, secondLabel }) {
  const node = (label, state, num) => {
    const filled = state === 'done' || state === 'active'
    return (
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flex: 'none' }}>
        <span style={{
          width: '22px', height: '22px', borderRadius: '50%', display: 'grid', placeItems: 'center',
          fontFamily: 'ui-monospace, Menlo, monospace', fontSize: '11px', fontWeight: 600,
          background: filled ? light.accent : 'transparent',
          border: `1.5px solid ${filled ? light.accent : light.border}`,
          color: filled ? '#fff' : light.textMuted,
        }}>{state === 'done' ? '✓' : num}</span>
        <span style={{ fontSize: '11px', fontWeight: 600, letterSpacing: '0.01em',
          color: state === 'upcoming' ? light.textMuted : light.textPrimary }}>{label}</span>
      </div>
    )
  }
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '22px' }}>
      {node('Credentials', s1, '1')}
      <span style={{ height: '1.5px', flex: 1, minWidth: '12px',
        background: s1 === 'done' ? light.accent : light.border }} />
      {node(secondLabel, s2, '2')}
    </div>
  )
}

// Split-Ledger layout CSS. Inline styles can't express media queries, so the
// responsive rules (collapse the brand panel on narrow screens) live here.
const SPLIT_CSS = `
.lgn-root{min-height:100vh;display:flex;background:${light.bgBase};font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,Roboto,sans-serif}
.lgn-brand{
  flex:1 1 45%;max-width:540px;position:relative;overflow:hidden;
  display:flex;flex-direction:column;justify-content:space-between;padding:52px 56px;
  background:
    radial-gradient(circle at 1px 1px, rgba(26,26,24,.05) 1px, transparent 0) 0 0/18px 18px,
    linear-gradient(158deg,#f6efe9 0%,#fbf9f7 62%);
  border-right:1px solid ${light.border};
}
.lgn-form{flex:1 1 55%;display:flex;align-items:center;justify-content:center;padding:40px 24px;background:${light.bgCard}}
.lgn-card{width:100%;max-width:380px}
.lgn-mobile{display:none}
@media(max-width:820px){
  .lgn-brand{display:none}
  .lgn-form{background:${light.bgBase}}
  .lgn-mobile{display:block}
}
`

export default function Login() {
  const { login, completeAuth } = useAuth()
  const [form, setForm] = useState({ username: '', password: '' })
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  // Multi-step: 'password' → 'otp' (enrolled) | 'enroll' (first-login MFA registration)
  const [step, setStep] = useState('password')
  const [mfaToken, setMfaToken] = useState('')
  const [otp, setOtp] = useState('')
  const [enroll, setEnroll] = useState(null)       // { qr_png_b64, secret }
  const [backup, setBackup] = useState(null)       // { codes, user, token }

  // CAPTCHA
  const [captcha, setCaptcha] = useState(null)     // { challenge_id, image_b64 } | null
  const [captchaAnswer, setCaptchaAnswer] = useState('')

  const loadCaptcha = useCallback(async () => {
    try { setCaptcha((await authApi.captcha()).data) }
    catch { setCaptcha(null) }
    setCaptchaAnswer('')
  }, [])
  useEffect(() => { loadCaptcha() }, [loadCaptcha])

  const onFocusBorder = e => e.target.style.borderColor = light.accent
  const onBlurBorder = e => e.target.style.borderColor = light.border

  const handlePassword = async (e) => {
    e.preventDefault(); setError(''); setLoading(true)
    try {
      const extra = captcha ? { captcha_id: captcha.challenge_id, captcha_answer: captchaAnswer } : {}
      const res = await login(form.username, form.password, extra)
      if (res.done) return
      setMfaToken(res.mfaToken)
      if (res.mfaRequired) setStep('otp')
      else if (res.mfaEnrollmentRequired) {
        const s = await authApi.mfaSetup(res.mfaToken)
        setEnroll(s.data); setStep('enroll')
      }
    } catch (err) {
      setError(err.response?.data?.detail || 'Login failed. Please try again.')
      if (captcha) loadCaptcha()
    } finally { setLoading(false) }
  }

  const handleOtp = async (e) => {
    e.preventDefault(); setError(''); setLoading(true)
    try {
      const r = await authApi.mfaVerify(mfaToken, otp.trim())
      // Session arrives as an httpOnly cookie on this response; only the user
      // is passed on.
      completeAuth(r.data.user)
    } catch (err) { setError(err.response?.data?.detail || 'Verification failed.') }
    finally { setLoading(false) }
  }

  const handleActivate = async (e) => {
    e.preventDefault(); setError(''); setLoading(true)
    try {
      const r = await authApi.mfaActivate(otp.trim(), mfaToken)
      // No token retained — activation set the session cookie server-side.
      setBackup({ codes: r.data.backup_codes, user: r.data.user })
    } catch (err) { setError(err.response?.data?.detail || 'Could not activate MFA.') }
    finally { setLoading(false) }
  }

  // ── Split-Ledger shell: brand panel (left) + step content (right) ─────────
  const shell = (heading, children) => {
    // Progress rail state: credentials first, then verify/enrol.
    const secondLabel = (step === 'enroll' || backup) ? 'Set up 2FA' : 'Verify'
    const pastCreds = backup || step === 'otp' || step === 'enroll'
    const s1 = pastCreds ? 'done' : 'active'
    const s2 = backup ? 'done' : (pastCreds ? 'active' : 'upcoming')
    return (
    <div className="lgn-root">
      <style>{SPLIT_CSS}</style>

      <aside className="lgn-brand">
        {/* 'light' is hard-coded, not read from the theme context: this panel
            renders on a light surface whatever the user's preference is, and
            the dark-variant wordmark is white — it would vanish here. */}
        <img src={brandLogo('light')} alt={BRAND_NAME}
             style={{ height: '72px', width: 'auto', maxWidth: '100%', objectFit: 'contain' }} />
        <div>
          <div style={{ width: '34px', height: '3px', background: light.accent, borderRadius: '2px', margin: '18px 0' }} />
          <p style={{ margin: 0, fontSize: '15px', lineHeight: 1.55, color: light.textSecondary, maxWidth: '30ch' }}>
            {t('login.tagline')}
          </p>
        </div>
        <div style={{ fontSize: '11.5px', letterSpacing: '0.03em', color: light.textMuted,
          fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
          display: 'flex', alignItems: 'center', gap: '8px' }}>
          <span style={{ width: '6px', height: '6px', borderRadius: '50%', background: '#2e9c6a', display: 'inline-block' }} />
          TLS 1.3 · session secured &nbsp;·&nbsp; Internal use only
        </div>
      </aside>

      <main className="lgn-form">
        <div className="lgn-card">
          {/* Compact brand — only shown when the left panel collapses */}
          <div className="lgn-mobile" style={{ textAlign: 'center', marginBottom: '28px' }}>
            <img src={brandLogo('light')} alt={BRAND_NAME}
                 style={{ height: '56px', width: 'auto', maxWidth: '100%',
                          margin: '0 auto', display: 'block', objectFit: 'contain' }} />
          </div>

          <StepRail s1={s1} s2={s2} secondLabel={secondLabel} />

          {heading && (
            <h2 style={{ margin: '0 0 18px', fontSize: '19px', fontWeight: 640,
              letterSpacing: '-0.01em', color: light.textPrimary }}>{heading}</h2>
          )}
          {children}

          <p style={{ fontSize: '11px', color: light.textMuted, marginTop: '24px', marginBottom: 0,
            fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', letterSpacing: '0.02em' }}>
            {t('login.footer')}
          </p>
        </div>
      </main>
    </div>
    )
  }

  const errorBox = error && (
    <div style={{ background: 'rgba(196,58,58,0.08)', border: '1px solid rgba(196,58,58,0.25)',
      color: light.danger, fontSize: '13px', borderRadius: '6px', padding: '10px 14px', marginBottom: '16px' }}>
      {error}
    </div>
  )

  // Backup codes acknowledgement (after first-login enrolment).
  if (backup) {
    return shell('Two-factor enabled',
      <div>
        <p style={{ fontSize: '13px', color: light.textSecondary, marginTop: 0 }}>
          Save these one-time backup codes somewhere safe — each works once if you lose your authenticator.
        </p>
        <div style={{ fontFamily: 'ui-monospace, Menlo, monospace', fontSize: '13px', background: light.bgBase,
          border: `1px solid ${light.border}`, borderRadius: '6px', padding: '12px',
          display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '4px 16px', marginBottom: '16px' }}>
          {backup.codes.map(c => <span key={c}>{c}</span>)}
        </div>
        <button onClick={() => completeAuth(backup.user)} style={primaryBtn(false)}>
          I've saved them — continue
        </button>
      </div>
    )
  }

  // OTP step (enrolled user).
  if (step === 'otp') {
    return shell('Verify it’s you',
      <form onSubmit={handleOtp}>
        <p style={{ fontSize: '13px', color: light.textSecondary, marginTop: 0, marginBottom: '16px' }}>
          Enter the 6-digit code from your authenticator app (or a backup code).
        </p>
        <input autoFocus required value={otp} onChange={e => setOtp(e.target.value)}
          style={{ ...inputStyle, marginBottom: '16px', letterSpacing: '0.3em', textAlign: 'center' }}
          placeholder="123456" inputMode="numeric" autoComplete="one-time-code"
          onFocus={onFocusBorder} onBlur={onBlurBorder} />
        {errorBox}
        <button type="submit" disabled={loading} style={primaryBtn(loading)}>{loading ? 'Verifying…' : 'Verify'}</button>
      </form>
    )
  }

  // First-login MFA registration (enforced).
  if (step === 'enroll' && enroll) {
    return shell('Set up two-factor',
      <form onSubmit={handleActivate}>
        <p style={{ fontSize: '13px', color: light.textSecondary, marginTop: 0, marginBottom: '12px' }}>
          Scan this QR in your authenticator app, then enter the code to finish.
        </p>
        <div style={{ textAlign: 'center', marginBottom: '12px' }}>
          <img src={`data:image/png;base64,${enroll.qr_png_b64}`} alt="MFA QR"
            style={{ width: '168px', height: '168px', border: `1px solid ${light.border}`, borderRadius: '8px' }} />
          <div style={{ fontSize: '11px', color: light.textMuted, marginTop: '6px', wordBreak: 'break-all' }}>
            or enter key: <code>{enroll.secret}</code>
          </div>
        </div>
        <input autoFocus required value={otp} onChange={e => setOtp(e.target.value)}
          style={{ ...inputStyle, marginBottom: '16px', letterSpacing: '0.3em', textAlign: 'center' }}
          placeholder="123456" inputMode="numeric" autoComplete="one-time-code"
          onFocus={onFocusBorder} onBlur={onBlurBorder} />
        {errorBox}
        <button type="submit" disabled={loading} style={primaryBtn(loading)}>{loading ? 'Activating…' : 'Activate'}</button>
      </form>
    )
  }

  // Password step (default).
  return shell('Sign in',
    <form onSubmit={handlePassword}>
      <div style={{ marginBottom: '16px' }}>
        <label style={labelStyle}>Username</label>
        <input type="text" required value={form.username}
          onChange={(e) => setForm({ ...form, username: e.target.value })}
          style={inputStyle} placeholder="Enter your username" autoFocus
          onFocus={onFocusBorder} onBlur={onBlurBorder} />
      </div>

      <div style={{ marginBottom: '20px' }}>
        <label style={labelStyle}>Password</label>
        <input type="password" required value={form.password}
          onChange={(e) => setForm({ ...form, password: e.target.value })}
          style={inputStyle} placeholder="Enter your password"
          onFocus={onFocusBorder} onBlur={onBlurBorder} />
      </div>

      {captcha && (
        <div style={{ marginBottom: '20px' }}>
          <label style={labelStyle}>Security check</label>
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '8px' }}>
            <img src={`data:image/png;base64,${captcha.image_b64}`} alt="CAPTCHA"
              style={{ height: '52px', borderRadius: '6px', border: `1px solid ${light.border}`, background: '#fff' }} />
            <button type="button" onClick={loadCaptcha} title="New image"
              style={{ padding: '6px 10px', fontSize: '12px', cursor: 'pointer', background: 'transparent',
                color: light.textSecondary, border: `1px solid ${light.border}`, borderRadius: '6px' }}>↻ Refresh</button>
          </div>
          <input type="text" required value={captchaAnswer}
            onChange={(e) => setCaptchaAnswer(e.target.value)} style={inputStyle}
            placeholder="Type the characters above" autoComplete="off" autoCapitalize="characters"
            onFocus={onFocusBorder} onBlur={onBlurBorder} />
        </div>
      )}

      {errorBox}

      <button type="submit" disabled={loading} style={primaryBtn(loading)}
        onMouseEnter={e => { if (!loading) e.currentTarget.style.background = light.accentHover }}
        onMouseLeave={e => { if (!loading) e.currentTarget.style.background = light.accent }}>
        {loading ? 'Signing in…' : 'Sign in'}
      </button>
    </form>
  )
}
