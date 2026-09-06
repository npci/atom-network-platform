// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { partnersApi } from '../../services/api'
import {
  Users, Plus, Loader, CheckCircle, AlertCircle, Shield, Wifi,
  RotateCcw, Eye, EyeOff, X, Building2, Smartphone, Cpu,
  Copy, Power, PowerOff, Pencil, Save, Lock, KeyRound, Network, Gauge,
  ShieldAlert, ChevronDown, ChevronUp,
} from 'lucide-react'
import StatTile, { StatTileRow } from '../../components/common/StatTile'

// Four-party network transaction roles are the operator-selectable types. cert_engine is the
// authority-internal cert-agent partner — kept here for label/display of the existing row, but
// flagged `internal` so it's filtered out of the create/edit type pickers.
const TYPE_META = {
  payer_psp:   { label: 'Payer PSP',   color: '#4caf7d', icon: Smartphone },
  payee_psp:   { label: 'Payee PSP',   color: '#3f9fd0', icon: Smartphone },
  remitter:    { label: 'Remitter',    color: '#6ea8dc', icon: Building2 },
  beneficiary: { label: 'Beneficiary', color: '#b388e8', icon: Building2 },
  cert_engine: { label: 'Cert Engine', color: '#e8b347', icon: Cpu, internal: true },
}

export default function Partners() {
  const qc = useQueryClient()
  const [showForm, setShowForm] = useState(false)
  const [name, setName] = useState('')
  const [types, setTypes] = useState(['payer_psp'])
  const [endpoint, setEndpoint] = useState('')
  const [adding, setAdding] = useState(false)
  const [error, setError] = useState(null)
  // newSecrets replaces newKey to hold ALL plaintext secrets returned
  // ONCE by the backend (api_key + jwt_signing_secret + signing_secret).
  // After Slice 3 + 5, the create + each rotate endpoint may return any
  // subset of these three fields; render whichever arrived.
  const [newSecrets, setNewSecrets] = useState(null)
  const [revealedKeys] = useState({})
  const [testResults, setTestResults] = useState({})
  const [testing, setTesting] = useState({})
  const [editingId, setEditingId] = useState(null)
  const [editName, setEditName] = useState('')
  const [editTypes, setEditTypes] = useState(['payer_psp'])
  const [editEndpoint, setEditEndpoint] = useState('')
  // Cert-Agent bank short-code (e.g. 'SBI'). Required for the readiness
  // orchestrator to trigger cert runs against this partner — empty means
  // declaring Ready for Certification silently skips with a logged warning.
  const [editBankId, setEditBankId] = useState('')
  const [saving, setSaving] = useState(false)
  const [search, setSearch] = useState('')
  const [filterType, setFilterType] = useState('')
  const [filterStatus, setFilterStatus] = useState('')
  // Slice 2/3/5/6/7 — security panel state.
  // expandedSecurity: id of the partner whose Security panel is open.
  // securityDraft: per-partner staged edits before save.
  const [expandedSecurity, setExpandedSecurity] = useState(null)
  const [securityDraft, setSecurityDraft] = useState({})
  const [securitySaving, setSecuritySaving] = useState({})

  const { data: partners, isLoading } = useQuery({
    queryKey: ['partners'],
    queryFn: () => partnersApi.list().then(r => r.data),
  })

  const { data: stats } = useQuery({
    queryKey: ['partner-stats'],
    queryFn: () => partnersApi.stats().then(r => r.data),
  })

  const handleAdd = async () => {
    if (!name.trim()) return
    setAdding(true); setError(null); setNewSecrets(null)
    try {
      const res = await partnersApi.create({ name: name.trim(), partner_types: types, endpoint_url: endpoint.trim() || null })
      // Backend returns all three secrets ONCE — surface them all.
      setNewSecrets({
        title: 'Partner registered',
        partnerName: res.data.name,
        api_key:             res.data.api_key,
        jwt_signing_secret:  res.data.jwt_signing_secret,
        signing_secret:      res.data.signing_secret,
      })
      setName(''); setTypes(['payer_psp']); setEndpoint(''); setShowForm(false)
      qc.invalidateQueries({ queryKey: ['partners'] })
      qc.invalidateQueries({ queryKey: ['partner-stats'] })
    } catch (e) {
      setError(e.response?.data?.detail || 'Failed to add partner')
    } finally { setAdding(false) }
  }

  const handleRotateKey = async (id) => {
    setNewSecrets(null)
    try {
      const res = await partnersApi.rotateKey(id)
      setNewSecrets({
        title: 'API key rotated',
        partnerName: res.data.name,
        api_key: res.data.api_key,
      })
      qc.invalidateQueries({ queryKey: ['partners'] })
    } catch (e) { setError(e.response?.data?.detail || 'Key rotation failed') }
  }

  // Slice 3 — rotate the per-partner JWT signing secret. Returned ONCE.
  const handleRotateJwtSecret = async (id) => {
    if (!window.confirm('Rotate the JWT signing secret for this partner? The partner must install the new value before its next outbound call lands.')) return
    setNewSecrets(null); setError(null)
    try {
      const res = await partnersApi.rotateJwtSecret(id)
      setNewSecrets({
        title: 'JWT signing secret rotated',
        partnerName: res.data.name,
        jwt_signing_secret: res.data.jwt_signing_secret,
      })
      qc.invalidateQueries({ queryKey: ['partners'] })
    } catch (e) { setError(e.response?.data?.detail || 'JWT secret rotation failed') }
  }

  // Slice 5 — rotate the per-partner HMAC envelope secret. Returned ONCE.
  const handleRotateHmacSecret = async (id) => {
    if (!window.confirm('Rotate the HMAC envelope secret for this partner? The partner must install the new value before its next outbound call lands.')) return
    setNewSecrets(null); setError(null)
    try {
      const res = await partnersApi.rotateHmacSecret(id)
      setNewSecrets({
        title: 'HMAC signing secret rotated',
        partnerName: res.data.name,
        signing_secret: res.data.signing_secret,
      })
      qc.invalidateQueries({ queryKey: ['partners'] })
    } catch (e) { setError(e.response?.data?.detail || 'HMAC secret rotation failed') }
  }

  // Slice 2 — emergency lever: revoke every active a2a_sessions row.
  const handleRevokeSessions = async (id) => {
    if (!window.confirm('Revoke ALL active sessions for this partner? They will be forced to re-authenticate on their next call.')) return
    setError(null)
    try {
      await partnersApi.revokeSessions(id)
      qc.invalidateQueries({ queryKey: ['partners'] })
    } catch (e) { setError(e.response?.data?.detail || 'Session revocation failed') }
  }

  // ── Security panel helpers ──────────────────────────────────────
  const openSecurity = (p) => {
    setExpandedSecurity(p.id)
    // Stage current values so dirty-tracking works.
    setSecurityDraft(prev => ({
      ...prev,
      [p.id]: {
        tls_tier:                p.tls_tier || 'jwt',
        client_cert_fingerprint: p.client_cert_fingerprint || '',
        allowed_cidrs_text:      Array.isArray(p.allowed_cidrs) ? p.allowed_cidrs.join('\n') : '',
        rate_limit_rps:          p.rate_limit_rps ?? 100,
        // Outbound TLS: '' = inherit global · 'true' = verify · 'false' = skip.
        ssl_verify:              p.ssl_verify === true ? 'true' : p.ssl_verify === false ? 'false' : '',
        ca_cert_pem:             '',
        ca_cert_baseline:        '',
        // Inline-attachment cap edited in MB. '' = inherit global · '0' = no limit.
        max_inline_mb:           p.max_inline_attachment_bytes == null ? ''
                                   : (p.max_inline_attachment_bytes / 1048576 % 1 === 0
                                       ? String(p.max_inline_attachment_bytes / 1048576)
                                       : (p.max_inline_attachment_bytes / 1048576).toFixed(2)),
      },
    }))
    // Lazy-load the stored PEM so the textarea shows/edits the current cert.
    if (p.has_ca_cert) {
      partnersApi.getCaCert(p.id)
        .then(r => setDraft(p.id, { ca_cert_pem: r.data.ca_cert_pem || '', ca_cert_baseline: r.data.ca_cert_pem || '' }))
        .catch(() => {})
    }
  }

  const setDraft = (id, patch) =>
    setSecurityDraft(prev => ({ ...prev, [id]: { ...(prev[id] || {}), ...patch } }))

  const handleSaveSecurity = async (p) => {
    const d = securityDraft[p.id]
    if (!d) return
    setSecuritySaving(s => ({ ...s, [p.id]: true })); setError(null)
    try {
      // Diff each field against the partner row and PATCH only what
      // changed — the four endpoints are independent so we don't
      // need an atomic batch.
      const calls = []
      if (d.tls_tier !== (p.tls_tier || 'jwt')) {
        calls.push(partnersApi.setTlsTier(p.id, d.tls_tier))
      }
      const fpNew = (d.client_cert_fingerprint || '').trim() || null
      if (fpNew !== (p.client_cert_fingerprint || null)) {
        calls.push(partnersApi.setCertFingerprint(p.id, fpNew))
      }
      const cidrs = (d.allowed_cidrs_text || '')
        .split(/[\n,]/).map(s => s.trim()).filter(Boolean)
      const cidrsCurrent = Array.isArray(p.allowed_cidrs) ? p.allowed_cidrs : []
      const cidrsChanged =
        cidrs.length !== cidrsCurrent.length
        || cidrs.some((c, i) => c !== cidrsCurrent[i])
      if (cidrsChanged) {
        calls.push(partnersApi.setAllowedCidrs(p.id, cidrs.length ? cidrs : null))
      }
      const rps = parseInt(d.rate_limit_rps, 10)
      if (Number.isFinite(rps) && rps !== (p.rate_limit_rps ?? 100)) {
        calls.push(partnersApi.setRateLimit(p.id, rps))
      }
      // Outbound TLS verify: '' -> null (inherit global), else true/false.
      const svNew = d.ssl_verify === 'true' ? true : d.ssl_verify === 'false' ? false : null
      if (svNew !== (p.ssl_verify ?? null)) {
        calls.push(partnersApi.setSslVerify(p.id, svNew))
      }
      // CA cert: push only when the PEM text changed vs what we loaded on open.
      if ((d.ca_cert_pem || '').trim() !== (d.ca_cert_baseline || '').trim()) {
        calls.push(partnersApi.uploadCaCert(p.id, (d.ca_cert_pem || '').trim()))
      }
      // Inline-attachment cap: '' -> null (inherit global); else MB -> bytes.
      // Invalid text leaves it unchanged (miNew stays at the current value).
      const miTxt = (d.max_inline_mb ?? '').trim()
      let miNew = p.max_inline_attachment_bytes ?? null
      if (miTxt === '') miNew = null
      else if (Number.isFinite(parseFloat(miTxt))) miNew = Math.max(0, Math.round(parseFloat(miTxt) * 1048576))
      if (miNew !== (p.max_inline_attachment_bytes ?? null)) {
        calls.push(partnersApi.setMaxInlineAttachment(p.id, miNew))
      }
      if (calls.length === 0) {
        return
      }
      await Promise.all(calls)
      qc.invalidateQueries({ queryKey: ['partners'] })
    } catch (e) {
      setError(e.response?.data?.detail || 'Security update failed')
    } finally {
      setSecuritySaving(s => ({ ...s, [p.id]: false }))
    }
  }

  const handleDeactivate = async (id) => {
    if (!window.confirm('Deactivate this partner?')) return
    try {
      await partnersApi.deactivate(id)
      qc.invalidateQueries({ queryKey: ['partners'] })
      qc.invalidateQueries({ queryKey: ['partner-stats'] })
    } catch (e) { setError(e.response?.data?.detail || 'Deactivation failed') }
  }

  const handleActivate = async (id) => {
    try {
      await partnersApi.activate(id)
      qc.invalidateQueries({ queryKey: ['partners'] })
      qc.invalidateQueries({ queryKey: ['partner-stats'] })
    } catch (e) { setError(e.response?.data?.detail || 'Activation failed') }
  }

  const startEdit = (p) => {
    setEditingId(p.id)
    setEditName(p.name)
    setEditTypes(p.partner_types || [p.partner_type])
    setEditEndpoint(p.endpoint_url || '')
    setEditBankId(p.cert_agent_bank_id || '')
  }

  const handleSaveEdit = async () => {
    if (!editName.trim()) return
    setSaving(true); setError(null)
    try {
      await partnersApi.update(editingId, {
        name: editName.trim(),
        partner_types: editTypes,
        endpoint_url: editEndpoint.trim() || null,
        // Backend treats empty string as "clear the mapping"; the .trim()
        // gives us that for free when the operator wipes the input.
        cert_agent_bank_id: editBankId.trim(),
      })
      setEditingId(null)
      qc.invalidateQueries({ queryKey: ['partners'] })
      qc.invalidateQueries({ queryKey: ['partner-stats'] })
    } catch (e) { setError(e.response?.data?.detail || 'Update failed') }
    finally { setSaving(false) }
  }

  const handleTest = async (id) => {
    setTesting(p => ({ ...p, [id]: true }))
    try {
      const res = await partnersApi.test(id)
      setTestResults(p => ({ ...p, [id]: res.data }))
    } catch (e) { setTestResults(p => ({ ...p, [id]: { status: 'error', message: e.message } })) }
    finally { setTesting(p => ({ ...p, [id]: false })) }
  }

  // Slice 6 — flip A2A wire (legacy ↔ a2a_sdk). Backend audits the change
  // via logger.info; UI just optimistically refreshes the cache.
  const handleProtocolChange = async (id, protocol_version) => {
    try {
      await partnersApi.setProtocol(id, protocol_version)
      qc.invalidateQueries({ queryKey: ['partners'] })
    } catch (e) {
      setError(e.response?.data?.detail || 'Protocol switch failed')
    }
  }

  const partnerList = partners || []
  const _hasType = (p, ...ts) => {
    const pt = Array.isArray(p.partner_type) ? p.partner_type : [p.partner_type]
    return ts.some(t => pt.includes(t))
  }
  const summary = {
    total:   partnerList.length,
    active:  partnerList.filter(p => p.status === 'active').length,
    // PSP-tier = payer + payee PSPs; bank-tier = remitter + beneficiary banks.
    psps:    partnerList.filter(p => _hasType(p, 'payer_psp', 'payee_psp')).length,
    banks:   partnerList.filter(p => _hasType(p, 'remitter', 'beneficiary')).length,
  }

  return (
    <div style={{ padding: '32px 40px', maxWidth: 1600, margin: '0 auto' }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: '24px' }}>
        <div>
          <h1 style={{ margin: '0 0 4px', fontSize: '20px', fontWeight: '600', color: 'var(--text-primary)' }}>
            Partner Registry
          </h1>
          <p style={{ margin: 0, fontSize: '13px', color: 'var(--text-muted)' }}>
            Manage ecosystem partners (Banks, PSPs, TPAPs) for A2A communication
          </p>
        </div>
        <button onClick={() => setShowForm(v => !v)} style={{
          display: 'flex', alignItems: 'center', gap: '6px',
          padding: '8px 16px', background: 'var(--accent)', color: 'white',
          border: 'none', borderRadius: '6px', fontSize: '13px', fontWeight: '600', cursor: 'pointer',
        }}>
          <Plus size={14} /> Add Partner
        </button>
      </div>

      <StatTileRow>
        <StatTile label="Total Partners" value={summary.total}  accent="var(--text-secondary)" />
        <StatTile label="Active"         value={summary.active} accent="#4caf7d"
                  hint={summary.total ? `${Math.round(summary.active / summary.total * 100)}% of registry` : null} />
        <StatTile label="Banks"          value={summary.banks}  accent="#6ea8dc" />
        <StatTile label="PSPs"           value={summary.psps}   accent="#b388e8" />
      </StatTileRow>

      {/* Messages */}
      {error && (
        <div style={{ padding: '10px 16px', borderRadius: '8px', marginBottom: '16px', background: 'rgba(224,108,108,0.08)', border: '1px solid rgba(224,108,108,0.25)', display: 'flex', alignItems: 'center', gap: '8px' }}>
          <AlertCircle size={14} style={{ color: 'var(--danger)' }} />
          <span style={{ fontSize: '13px', color: 'var(--danger)', flex: 1 }}>{error}</span>
          <button onClick={() => setError(null)} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-muted)' }}><X size={14} /></button>
        </div>
      )}
      {newSecrets && (
        <div style={{ padding: '14px 16px', borderRadius: '8px', marginBottom: '16px', background: 'rgba(76,175,125,0.08)', border: '1px solid rgba(76,175,125,0.25)' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '8px' }}>
            <Shield size={14} style={{ color: 'var(--success)' }} />
            <span style={{ fontSize: '13px', fontWeight: '600', color: 'var(--success)' }}>
              {newSecrets.title}{newSecrets.partnerName ? ` — ${newSecrets.partnerName}` : ''} · Copy now (shown only once)
            </span>
            <span style={{ flex: 1 }} />
            <button onClick={() => setNewSecrets(null)} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-muted)' }}><X size={14} /></button>
          </div>
          {/* One row per secret the backend returned. Slice 3 + 5 mean
              create returns api_key + jwt_signing_secret + signing_secret
              and each rotate returns just one. Render whichever showed up. */}
          {[
            { key: 'api_key',            label: 'API Key',                   icon: KeyRound },
            { key: 'jwt_signing_secret', label: 'JWT Signing Secret',        icon: Lock },
            { key: 'signing_secret',     label: 'HMAC Envelope Secret',      icon: Lock },
          ].filter(row => newSecrets[row.key]).map(row => {
            const Icon = row.icon
            return (
              <div key={row.key} style={{ display: 'flex', gap: '8px', alignItems: 'center', marginBottom: '6px' }}>
                <span style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: '11px', color: 'var(--text-muted)', minWidth: 165 }}>
                  <Icon size={11} /> {row.label}
                </span>
                <code style={{ flex: 1, padding: '6px 10px', background: 'var(--bg-base)', border: '1px solid var(--border)', borderRadius: '6px', fontSize: '12px', fontFamily: 'monospace', color: 'var(--text-primary)', wordBreak: 'break-all' }}>
                  {newSecrets[row.key]}
                </code>
                <button onClick={() => navigator.clipboard.writeText(newSecrets[row.key])} style={{
                  padding: '6px 10px', background: 'var(--bg-elevated)', border: '1px solid var(--border)',
                  borderRadius: '6px', cursor: 'pointer', display: 'flex', alignItems: 'center', gap: '4px',
                  fontSize: '11px', color: 'var(--text-muted)',
                }}>
                  <Copy size={12} /> Copy
                </button>
              </div>
            )
          })}
          <div style={{ fontSize: '11px', color: 'var(--text-muted)', marginTop: 4 }}>
            Ship the *_secret values to the partner via a secure channel — the partner installs
            them in its Settings page (npci_jwt_secret + npci_hmac_secret).
          </div>
        </div>
      )}

      {/* Add form */}
      {showForm && (
        <div style={{ padding: '20px', marginBottom: '24px', borderRadius: '8px', background: 'var(--bg-elevated)', border: '1px solid var(--border)' }}>
          <h3 style={{ margin: '0 0 16px', fontSize: '14px', fontWeight: '600', color: 'var(--text-primary)' }}>Register New Partner</h3>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '12px', marginBottom: '14px' }}>
            <div>
              <label style={{ display: 'block', fontSize: '11px', color: 'var(--text-muted)', marginBottom: '4px', textTransform: 'uppercase', letterSpacing: '0.04em' }}>Partner Name *</label>
              <input value={name} onChange={e => setName(e.target.value)} placeholder="e.g. State Bank of India"
                style={{ width: '100%', padding: '8px 12px', borderRadius: '6px', border: '1px solid var(--border)', background: 'var(--bg-input)', color: 'var(--text-primary)', fontSize: '13px' }} />
            </div>
            <div>
              <label style={{ display: 'block', fontSize: '11px', color: 'var(--text-muted)', marginBottom: '4px', textTransform: 'uppercase', letterSpacing: '0.04em' }}>Type(s) * — select one or more</label>
              <div style={{ display: 'flex', gap: '8px' }}>
                {Object.entries(TYPE_META).filter(([, meta]) => !meta.internal).map(([key, meta]) => {
                  const selected = types.includes(key)
                  return (
                    <button key={key} type="button" onClick={() => {
                      setTypes(prev => selected ? (prev.length > 1 ? prev.filter(t => t !== key) : prev) : [...prev, key])
                    }} style={{
                      flex: 1, padding: '8px 12px', borderRadius: '6px', fontSize: '12px', fontWeight: '600', cursor: 'pointer',
                      background: selected ? `${meta.color}20` : 'var(--bg-input)',
                      border: `2px solid ${selected ? meta.color : 'var(--border)'}`,
                      color: selected ? meta.color : 'var(--text-muted)',
                    }}>
                      {meta.label}
                    </button>
                  )
                })}
              </div>
            </div>
            <div>
              <label style={{ display: 'block', fontSize: '11px', color: 'var(--text-muted)', marginBottom: '4px', textTransform: 'uppercase', letterSpacing: '0.04em' }}>Endpoint URL</label>
              <input value={endpoint} onChange={e => setEndpoint(e.target.value)} placeholder="https://partner-uat.example.com"
                style={{ width: '100%', padding: '8px 12px', borderRadius: '6px', border: '1px solid var(--border)', background: 'var(--bg-input)', color: 'var(--text-primary)', fontSize: '13px' }} />
            </div>
          </div>
          <div style={{ display: 'flex', gap: '10px', justifyContent: 'flex-end' }}>
            <button onClick={() => setShowForm(false)} style={{ padding: '8px 14px', background: 'transparent', border: '1px solid var(--border)', borderRadius: '6px', color: 'var(--text-muted)', fontSize: '13px', cursor: 'pointer' }}>Cancel</button>
            <button onClick={handleAdd} disabled={adding || !name.trim()} style={{
              display: 'flex', alignItems: 'center', gap: '6px', padding: '8px 16px', background: 'var(--accent)', color: 'white',
              border: 'none', borderRadius: '6px', fontSize: '13px', fontWeight: '600',
              cursor: (adding || !name.trim()) ? 'not-allowed' : 'pointer', opacity: (adding || !name.trim()) ? 0.5 : 1,
            }}>
              {adding ? <Loader size={13} style={{ animation: 'spin 1s linear infinite' }} /> : <Plus size={13} />}
              Register Partner
            </button>
          </div>
        </div>
      )}

      {/* Stats */}
      <div style={{ display: 'flex', gap: '16px', marginBottom: '24px' }}>
        {[
          { label: 'Total', value: stats?.total || 0 },
          { label: 'Active', value: stats?.active || 0 },
          { label: 'Payer PSPs', value: stats?.by_type?.payer_psp || 0 },
          { label: 'Payee PSPs', value: stats?.by_type?.payee_psp || 0 },
          { label: 'Remitters', value: stats?.by_type?.remitter || 0 },
          { label: 'Beneficiaries', value: stats?.by_type?.beneficiary || 0 },
        ].map(s => (
          <div key={s.label} style={{ flex: 1, padding: '14px 16px', borderRadius: '8px', background: 'var(--bg-elevated)', border: '1px solid var(--border)', textAlign: 'center' }}>
            <p style={{ margin: '0 0 2px', fontSize: '20px', fontWeight: '700', color: 'var(--text-primary)' }}>{s.value}</p>
            <p style={{ margin: 0, fontSize: '10px', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>{s.label}</p>
          </div>
        ))}
      </div>

      {/* Loading */}
      {isLoading && <p style={{ color: 'var(--text-muted)', fontSize: '13px' }}>Loading partners...</p>}

      {/* Empty */}
      {!isLoading && (!partners || partners.length === 0) && (
        <div style={{ textAlign: 'center', padding: '48px 32px', background: 'var(--bg-elevated)', border: '1px solid var(--border)', borderRadius: '10px' }}>
          <Users size={36} style={{ color: 'var(--accent)', marginBottom: '12px' }} />
          <h2 style={{ margin: '0 0 8px', fontSize: '16px', fontWeight: '600', color: 'var(--text-primary)' }}>No partners registered</h2>
          <p style={{ margin: '0 auto 20px', fontSize: '13px', color: 'var(--text-muted)', maxWidth: '400px' }}>
            Register partners by their four-party role (Payer/Payee PSP, Remitter, Beneficiary) to enable A2A communication for change management.
          </p>
          <button onClick={() => setShowForm(true)} style={{ padding: '9px 20px', background: 'var(--accent)', color: 'white', border: 'none', borderRadius: '8px', fontSize: '14px', fontWeight: '600', cursor: 'pointer' }}>
            <Plus size={14} style={{ marginRight: 6, verticalAlign: 'text-bottom' }} /> Register First Partner
          </button>
        </div>
      )}

      {/* Search & Filter */}
      {partners && partners.length > 0 && (
        <div style={{ display: 'flex', gap: '10px', marginBottom: '16px', alignItems: 'center' }}>
          <div style={{ flex: 1, position: 'relative' }}>
            <input
              value={search}
              onChange={e => setSearch(e.target.value)}
              placeholder="Search partners by name or endpoint..."
              style={{
                width: '100%', padding: '9px 14px 9px 34px', borderRadius: '6px',
                border: '1px solid var(--border)', background: 'var(--bg-input)',
                color: 'var(--text-primary)', fontSize: '13px',
              }}
            />
            <span style={{ position: 'absolute', left: '10px', top: '50%', transform: 'translateY(-50%)', color: 'var(--text-muted)' }}>🔍</span>
          </div>
          <select value={filterType} onChange={e => setFilterType(e.target.value)} style={{
            padding: '9px 12px', borderRadius: '6px', border: '1px solid var(--border)',
            background: 'var(--bg-input)', color: 'var(--text-primary)', fontSize: '12px',
          }}>
            <option value="">All Types</option>
            <option value="payer_psp">Payer PSP</option>
            <option value="payee_psp">Payee PSP</option>
            <option value="remitter">Remitter</option>
            <option value="beneficiary">Beneficiary</option>
            <option value="cert_engine">Cert Engine</option>
          </select>
          <select value={filterStatus} onChange={e => setFilterStatus(e.target.value)} style={{
            padding: '9px 12px', borderRadius: '6px', border: '1px solid var(--border)',
            background: 'var(--bg-input)', color: 'var(--text-primary)', fontSize: '12px',
          }}>
            <option value="">All Status</option>
            <option value="active">Active</option>
            <option value="inactive">Inactive</option>
          </select>
          {(search || filterType || filterStatus) && (
            <button onClick={() => { setSearch(''); setFilterType(''); setFilterStatus('') }} style={{
              padding: '9px 12px', background: 'transparent', border: '1px solid var(--border)',
              borderRadius: '6px', color: 'var(--text-muted)', fontSize: '12px', cursor: 'pointer',
            }}>Clear</button>
          )}
        </div>
      )}

      {/* Partner cards */}
      {partners && partners.length > 0 && (() => {
        const filtered = partners.filter(p => {
          const q = search.toLowerCase()
          if (q && !p.name.toLowerCase().includes(q) && !(p.endpoint_url || '').toLowerCase().includes(q)) return false
          if (filterType) {
            const ptypes = p.partner_types || [p.partner_type]
            if (!ptypes.includes(filterType)) return false
          }
          if (filterStatus && p.status !== filterStatus) return false
          return true
        })
        return (
        <div>
          {filtered.length === 0 ? (
            <p style={{ padding: '24px', textAlign: 'center', color: 'var(--text-muted)', fontSize: '13px' }}>
              No partners match your search. {partners.length} total partner{partners.length !== 1 ? 's' : ''} registered.
            </p>
          ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
            {(search || filterType || filterStatus) && (
              <p style={{ margin: 0, fontSize: '12px', color: 'var(--text-muted)' }}>
                Showing {filtered.length} of {partners.length} partners
              </p>
            )}
          {filtered.map(p => {
            const ptypes = p.partner_types || [p.partner_type]
            const meta = TYPE_META[ptypes[0]] || TYPE_META.payer_psp
            const Icon = meta.icon
            const isActive = p.status === 'active'
            const testResult = testResults[p.id]

            return (
              <div key={p.id} style={{
                padding: '20px', borderRadius: '8px',
                background: 'var(--bg-elevated)', border: '1px solid var(--border)',
                opacity: isActive ? 1 : 0.6,
              }}>
                {/* Inline edit form */}
                {editingId === p.id ? (
                  <div style={{ marginBottom: '14px' }}>
                    <div style={{ display: 'grid', gridTemplateColumns: '1.2fr 1.2fr 1.2fr 0.8fr', gap: '10px', marginBottom: '12px' }}>
                      <div>
                        <label style={{ display: 'block', fontSize: '10px', color: 'var(--text-muted)', marginBottom: '3px', textTransform: 'uppercase', letterSpacing: '0.04em' }}>Name</label>
                        <input value={editName} onChange={e => setEditName(e.target.value)} style={{
                          width: '100%', padding: '7px 10px', borderRadius: '5px', border: '1px solid var(--border)',
                          background: 'var(--bg-input)', color: 'var(--text-primary)', fontSize: '13px',
                        }} />
                      </div>
                      <div>
                        <label style={{ display: 'block', fontSize: '10px', color: 'var(--text-muted)', marginBottom: '3px', textTransform: 'uppercase', letterSpacing: '0.04em' }}>Type(s)</label>
                        <div style={{ display: 'flex', gap: '6px' }}>
                          {Object.entries(TYPE_META).filter(([, meta]) => !meta.internal).map(([key, meta]) => {
                            const selected = editTypes.includes(key)
                            return (
                              <button key={key} type="button" onClick={() => {
                                setEditTypes(prev => selected ? (prev.length > 1 ? prev.filter(t => t !== key) : prev) : [...prev, key])
                              }} style={{
                                flex: 1, padding: '7px 8px', borderRadius: '5px', fontSize: '11px', fontWeight: '600', cursor: 'pointer',
                                background: selected ? `${meta.color}20` : 'var(--bg-input)',
                                border: `2px solid ${selected ? meta.color : 'var(--border)'}`,
                                color: selected ? meta.color : 'var(--text-muted)',
                              }}>
                                {meta.label}
                              </button>
                            )
                          })}
                        </div>
                      </div>
                      <div>
                        <label style={{ display: 'block', fontSize: '10px', color: 'var(--text-muted)', marginBottom: '3px', textTransform: 'uppercase', letterSpacing: '0.04em' }}>Endpoint URL</label>
                        <input value={editEndpoint} onChange={e => setEditEndpoint(e.target.value)} placeholder="https://partner.example.com" style={{
                          width: '100%', padding: '7px 10px', borderRadius: '5px', border: '1px solid var(--border)',
                          background: 'var(--bg-input)', color: 'var(--text-primary)', fontSize: '13px',
                        }} />
                      </div>
                      <div>
                        <label
                          title="Cert-Agent bank short-code (e.g. SBI). Must match a bank configured in cert-agent's Bank Config. Required before this partner can run cert tests."
                          style={{ display: 'block', fontSize: '10px', color: 'var(--text-muted)', marginBottom: '3px', textTransform: 'uppercase', letterSpacing: '0.04em' }}
                        >
                          Cert-Agent Bank ID
                        </label>
                        <input
                          value={editBankId}
                          onChange={e => setEditBankId(e.target.value.toUpperCase())}
                          placeholder="e.g. SBI"
                          style={{
                            width: '100%', padding: '7px 10px', borderRadius: '5px', border: '1px solid var(--border)',
                            background: 'var(--bg-input)', color: 'var(--text-primary)', fontSize: '13px',
                            fontFamily: 'monospace',
                          }}
                        />
                      </div>
                    </div>
                    <div style={{ display: 'flex', gap: '8px', justifyContent: 'flex-end' }}>
                      <button onClick={() => setEditingId(null)} style={{
                        padding: '6px 12px', background: 'transparent', border: '1px solid var(--border)',
                        borderRadius: '5px', fontSize: '11px', color: 'var(--text-muted)', cursor: 'pointer',
                      }}>Cancel</button>
                      <button onClick={handleSaveEdit} disabled={saving || !editName.trim()} style={{
                        display: 'flex', alignItems: 'center', gap: '5px',
                        padding: '6px 14px', background: 'var(--accent)', color: 'white',
                        border: 'none', borderRadius: '5px', fontSize: '11px', fontWeight: '600',
                        cursor: (saving || !editName.trim()) ? 'not-allowed' : 'pointer',
                        opacity: (saving || !editName.trim()) ? 0.5 : 1,
                      }}>
                        {saving ? <Loader size={11} style={{ animation: 'spin 1s linear infinite' }} /> : <Save size={11} />}
                        Save
                      </button>
                    </div>
                  </div>
                ) : (
                  /* Normal header view */
                  <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '14px' }}>
                    <div style={{
                      width: '36px', height: '36px', borderRadius: '8px',
                      background: `${meta.color}15`, border: `1px solid ${meta.color}30`,
                      display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0,
                    }}>
                      <Icon size={16} style={{ color: meta.color }} />
                    </div>
                    <div style={{ flex: 1 }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '6px', flexWrap: 'wrap' }}>
                        <p style={{ margin: 0, fontSize: '14px', fontWeight: '600', color: 'var(--text-primary)' }}>{p.name}</p>
                        {ptypes.map(t => {
                          const tm = TYPE_META[t] || TYPE_META.payer_psp
                          return (
                            <span key={t} style={{
                              fontSize: '10px', padding: '1px 8px', borderRadius: '4px', fontWeight: '600',
                              background: `${tm.color}15`, color: tm.color, border: `1px solid ${tm.color}30`,
                              textTransform: 'uppercase',
                            }}>{tm.label}</span>
                          )
                        })}
                        <span style={{
                          fontSize: '10px', padding: '1px 8px', borderRadius: '4px', fontWeight: '600',
                          background: isActive ? 'rgba(76,175,125,0.1)' : 'rgba(224,108,108,0.1)',
                          color: isActive ? 'var(--success)' : 'var(--danger)',
                          border: `1px solid ${isActive ? 'rgba(76,175,125,0.3)' : 'rgba(224,108,108,0.3)'}`,
                        }}>{isActive ? 'Active' : 'Inactive'}</span>
                      </div>
                      {p.endpoint_url && (
                        <p style={{ margin: '2px 0 0', fontSize: '11px', color: 'var(--text-muted)', fontFamily: 'monospace' }}>{p.endpoint_url}</p>
                      )}
                    </div>
                    <div style={{ display: 'flex', gap: '6px' }}>
                      <button onClick={() => startEdit(p)} title="Edit" style={{
                        padding: '6px 10px', background: 'transparent', border: '1px solid var(--border)',
                        borderRadius: '5px', cursor: 'pointer', color: 'var(--accent)', display: 'flex', alignItems: 'center', gap: '4px', fontSize: '11px',
                      }}>
                        <Pencil size={11} /> Edit
                      </button>
                      <button onClick={() => handleTest(p.id)} disabled={testing[p.id]} title="Test Connection" style={{
                        padding: '6px 10px', background: 'transparent', border: '1px solid var(--border)',
                        borderRadius: '5px', cursor: 'pointer', color: 'var(--text-muted)', display: 'flex', alignItems: 'center', gap: '4px', fontSize: '11px',
                      }}>
                        {testing[p.id] ? <Loader size={11} style={{ animation: 'spin 1s linear infinite' }} /> : <Wifi size={11} />} Test
                      </button>
                      <button onClick={() => handleRotateKey(p.id)} title="Rotate API Key" style={{
                        padding: '6px 10px', background: 'transparent', border: '1px solid var(--border)',
                        borderRadius: '5px', cursor: 'pointer', color: 'var(--text-muted)', display: 'flex', alignItems: 'center', gap: '4px', fontSize: '11px',
                      }}>
                        <RotateCcw size={11} /> Rotate Key
                      </button>
                      {isActive ? (
                        <button onClick={() => handleDeactivate(p.id)} title="Deactivate" style={{
                          padding: '6px 10px', background: 'transparent', border: '1px solid var(--border)',
                          borderRadius: '5px', cursor: 'pointer', color: 'var(--danger)', display: 'flex', alignItems: 'center', gap: '4px', fontSize: '11px',
                        }}>
                          <PowerOff size={11} /> Deactivate
                        </button>
                      ) : (
                        <button onClick={() => handleActivate(p.id)} title="Activate" style={{
                          padding: '6px 10px', background: 'transparent', border: '1px solid var(--border)',
                          borderRadius: '5px', cursor: 'pointer', color: 'var(--success)', display: 'flex', alignItems: 'center', gap: '4px', fontSize: '11px',
                        }}>
                          <Power size={11} /> Activate
                        </button>
                      )}
                    </div>
                  </div>
                )}

                {/* API Key + A2A Protocol */}
                <div style={{ display: 'flex', alignItems: 'center', gap: '16px', marginBottom: testResult ? '10px' : 0, flexWrap: 'wrap' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                    <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>API Key:</span>
                    <code style={{ fontSize: '11px', fontFamily: 'monospace', color: 'var(--text-secondary)' }}>
                      {revealedKeys[p.id] ? p.api_key_masked : p.api_key_masked}
                    </code>
                  </div>
                  {/* Slice 6 — wire selector. Default 'legacy' = hand-rolled
                      POST. 'a2a_sdk' = SDK JSON-RPC. Flipping takes effect
                      on the partner's NEXT outbound call (no app restart). */}
                  <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                    <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>A2A Protocol:</span>
                    <select
                      value={p.protocol_version || 'legacy'}
                      onChange={(e) => handleProtocolChange(p.id, e.target.value)}
                      disabled={!isActive}
                      title={
                        isActive
                          ? "Switch wire between hand-rolled POST (legacy) and SDK JSON-RPC (a2a_sdk)"
                          : "Activate the partner first"
                      }
                      style={{
                        padding: '3px 8px', fontSize: '11px',
                        background: 'var(--bg-card)',
                        border: '1px solid var(--border)',
                        borderRadius: '4px',
                        color: 'var(--text-secondary)',
                        cursor: isActive ? 'pointer' : 'not-allowed',
                        opacity: isActive ? 1 : 0.5,
                      }}
                    >
                      <option value="legacy">legacy</option>
                      <option value="a2a_sdk">a2a_sdk</option>
                    </select>
                  </div>
                </div>

                {/* Test result */}
                {testResult && (
                  <div style={{
                    marginTop: '10px', padding: '8px 14px', borderRadius: '6px',
                    background: testResult.status === 'ok' ? 'rgba(76,175,125,0.06)' : 'rgba(224,108,108,0.06)',
                    border: `1px solid ${testResult.status === 'ok' ? 'rgba(76,175,125,0.2)' : 'rgba(224,108,108,0.2)'}`,
                    display: 'flex', alignItems: 'center', gap: '8px',
                  }}>
                    {testResult.status === 'ok'
                      ? <CheckCircle size={13} style={{ color: 'var(--success)' }} />
                      : <AlertCircle size={13} style={{ color: 'var(--danger)' }} />}
                    <span style={{ fontSize: '12px', color: testResult.status === 'ok' ? 'var(--success)' : 'var(--danger)' }}>
                      {testResult.message}
                    </span>
                  </div>
                )}

                {/* ── Security panel (Slice 2/3/5/6/7 of A2A hardening) ──
                    Collapsed by default — expanded view shows the full
                    state row plus inline editors and the rotate / revoke
                    actions. Read-only summary chips render either way
                    so operators can see configured-vs-not at a glance. */}
                {(() => {
                  const isExpanded = expandedSecurity === p.id
                  const draft = securityDraft[p.id] || {}
                  const tier = p.tls_tier || 'jwt'
                  const cidrs = Array.isArray(p.allowed_cidrs) ? p.allowed_cidrs : []
                  const fpMask = p.client_cert_fingerprint
                    ? `${p.client_cert_fingerprint.slice(0, 4)}…${p.client_cert_fingerprint.slice(-4)}`
                    : null
                  return (
                    <div style={{ marginTop: 12, paddingTop: 12, borderTop: '1px solid var(--border)' }}>
                      {/* Summary row + expand toggle */}
                      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
                        <button onClick={() => isExpanded ? setExpandedSecurity(null) : openSecurity(p)} style={{
                          padding: '4px 10px', background: 'transparent', border: '1px solid var(--border)',
                          borderRadius: '5px', cursor: 'pointer', color: 'var(--text-secondary)',
                          fontSize: '11px', display: 'flex', alignItems: 'center', gap: 4,
                        }}>
                          <Shield size={11} /> Security
                          {isExpanded ? <ChevronUp size={11} /> : <ChevronDown size={11} />}
                        </button>
                        {/* Compact state chips */}
                        <span style={{
                          fontSize: 10, padding: '1px 8px', borderRadius: 4, fontWeight: 600,
                          background: tier === 'mtls' ? 'rgba(110,168,220,0.10)' : 'rgba(255,255,255,0.04)',
                          color: tier === 'mtls' ? '#6ea8dc' : 'var(--text-muted)',
                          border: `1px solid ${tier === 'mtls' ? 'rgba(110,168,220,0.3)' : 'var(--border)'}`,
                          display: 'flex', alignItems: 'center', gap: 3,
                        }}><Lock size={9} /> tier: {tier}</span>
                        {fpMask && (
                          <span style={{ fontSize: 10, color: 'var(--text-muted)', fontFamily: 'monospace' }}>
                            fp {fpMask}
                          </span>
                        )}
                        {cidrs.length > 0 && (
                          <span style={{
                            fontSize: 10, padding: '1px 8px', borderRadius: 4, fontWeight: 600,
                            background: 'rgba(76,175,125,0.08)', color: 'var(--success, #4caf7d)',
                            border: '1px solid rgba(76,175,125,0.25)',
                            display: 'flex', alignItems: 'center', gap: 3,
                          }}><Network size={9} /> {cidrs.length} CIDR{cidrs.length > 1 ? 's' : ''}</span>
                        )}
                        <span style={{ fontSize: 10, color: 'var(--text-muted)', display: 'flex', alignItems: 'center', gap: 3 }}>
                          <Gauge size={10} /> {p.rate_limit_rps ?? 100} rps
                        </span>
                      </div>

                      {/* Expanded editor */}
                      {isExpanded && (
                        <div style={{
                          marginTop: 12, padding: 14, borderRadius: 6,
                          background: 'rgba(255,255,255,0.02)', border: '1px solid var(--border)',
                        }}>
                          <div style={{ display: 'grid', gridTemplateColumns: '180px 1fr 180px', gap: 12, marginBottom: 12 }}>
                            {/* tls_tier select */}
                            <div>
                              <label style={{ display: 'block', fontSize: 10, color: 'var(--text-muted)', marginBottom: 4, textTransform: 'uppercase', letterSpacing: '0.04em' }}>TLS Tier</label>
                              <select
                                value={draft.tls_tier ?? tier}
                                onChange={e => setDraft(p.id, { tls_tier: e.target.value })}
                                style={{
                                  width: '100%', padding: '6px 10px', borderRadius: '5px',
                                  border: '1px solid var(--border)', background: 'var(--bg-input)',
                                  color: 'var(--text-primary)', fontSize: 12,
                                }}>
                                <option value="jwt">jwt — :443</option>
                                <option value="mtls">mtls — :8443 (banks)</option>
                              </select>
                            </div>
                            {/* fingerprint */}
                            <div>
                              <label style={{ display: 'block', fontSize: 10, color: 'var(--text-muted)', marginBottom: 4, textTransform: 'uppercase', letterSpacing: '0.04em' }}>
                                Client Cert Fingerprint (SHA-256, 64-hex)
                              </label>
                              <input
                                value={draft.client_cert_fingerprint ?? ''}
                                onChange={e => setDraft(p.id, { client_cert_fingerprint: e.target.value })}
                                placeholder={(draft.tls_tier ?? tier) === 'mtls' ? 'required for mtls tier' : 'leave blank for jwt tier'}
                                style={{
                                  width: '100%', padding: '6px 10px', borderRadius: '5px',
                                  border: '1px solid var(--border)', background: 'var(--bg-input)',
                                  color: 'var(--text-primary)', fontSize: 12, fontFamily: 'monospace',
                                }} />
                            </div>
                            {/* rate limit */}
                            <div>
                              <label style={{ display: 'block', fontSize: 10, color: 'var(--text-muted)', marginBottom: 4, textTransform: 'uppercase', letterSpacing: '0.04em' }}>Rate Limit (rps)</label>
                              <input type="number" min={1} max={10000}
                                value={draft.rate_limit_rps ?? 100}
                                onChange={e => setDraft(p.id, { rate_limit_rps: e.target.value })}
                                style={{
                                  width: '100%', padding: '6px 10px', borderRadius: '5px',
                                  border: '1px solid var(--border)', background: 'var(--bg-input)',
                                  color: 'var(--text-primary)', fontSize: 12,
                                }} />
                            </div>
                          </div>

                          {/* CIDR allowlist */}
                          <div style={{ marginBottom: 12 }}>
                            <label style={{ display: 'block', fontSize: 10, color: 'var(--text-muted)', marginBottom: 4, textTransform: 'uppercase', letterSpacing: '0.04em' }}>
                              Allowed CIDRs (one per line, blank = no IP enforcement)
                            </label>
                            <textarea
                              rows={3}
                              value={draft.allowed_cidrs_text ?? ''}
                              onChange={e => setDraft(p.id, { allowed_cidrs_text: e.target.value })}
                              placeholder="10.0.0.0/8&#10;192.168.1.0/24"
                              style={{
                                width: '100%', padding: '6px 10px', borderRadius: '5px',
                                border: '1px solid var(--border)', background: 'var(--bg-input)',
                                color: 'var(--text-primary)', fontSize: 12, fontFamily: 'monospace',
                                resize: 'vertical',
                              }} />
                          </div>

                          {/* Outbound TLS — verify THIS partner's HTTPS server cert
                              (used by the Test button + real A2A calls) */}
                          <div style={{ display: 'grid', gridTemplateColumns: '1fr', gap: 12, marginBottom: 12 }}>
                            <div>
                              <label style={{ display: 'block', fontSize: 10, color: 'var(--text-muted)', marginBottom: 4, textTransform: 'uppercase', letterSpacing: '0.04em' }}>
                                SSL Verification (outbound to this partner)
                              </label>
                              <select
                                value={draft.ssl_verify ?? ''}
                                onChange={e => setDraft(p.id, { ssl_verify: e.target.value })}
                                style={{
                                  width: '100%', padding: '6px 10px', borderRadius: '5px',
                                  border: '1px solid var(--border)', background: 'var(--bg-input)',
                                  color: 'var(--text-primary)', fontSize: 12,
                                }}>
                                <option value="">Inherit global default</option>
                                <option value="true">Verify (secure)</option>
                                <option value="false">Skip verification (self-signed / internal)</option>
                              </select>
                            </div>
                            <div>
                              <label style={{ display: 'block', fontSize: 10, color: 'var(--text-muted)', marginBottom: 4, textTransform: 'uppercase', letterSpacing: '0.04em' }}>
                                Max inline attachment (MB)
                              </label>
                              <input
                                type="number" min="0" step="1" placeholder="Inherit global default"
                                value={draft.max_inline_mb ?? ''}
                                onChange={e => setDraft(p.id, { max_inline_mb: e.target.value })}
                                style={{
                                  width: '100%', padding: '6px 10px', borderRadius: '5px',
                                  border: '1px solid var(--border)', background: 'var(--bg-input)',
                                  color: 'var(--text-primary)', fontSize: 12,
                                }} />
                              <div style={{ fontSize: 9, color: 'var(--text-muted)', marginTop: 3 }}>
                                Blank = inherit global · 0 = no limit. Attachments larger than this
                                (e.g. big promo videos) are dropped from the kit so the send fits the
                                partner's ingress body limit.
                              </div>
                            </div>
                            <div>
                              <label style={{ display: 'block', fontSize: 10, color: 'var(--text-muted)', marginBottom: 4, textTransform: 'uppercase', letterSpacing: '0.04em' }}>
                                Trusted CA / Certificate (PEM){(draft.ca_cert_baseline || '').trim() ? ' — configured' : ''}
                              </label>
                              <input type="file" accept=".pem,.crt,.cer,.cert,application/x-pem-file"
                                onChange={e => {
                                  const f = e.target.files?.[0]
                                  if (!f) return
                                  const rd = new FileReader()
                                  rd.onload = () => setDraft(p.id, { ca_cert_pem: String(rd.result || '') })
                                  rd.readAsText(f)
                                }}
                                style={{ display: 'block', marginBottom: 6, fontSize: 11, color: 'var(--text-secondary)' }} />
                              <textarea rows={4}
                                value={draft.ca_cert_pem ?? ''}
                                onChange={e => setDraft(p.id, { ca_cert_pem: e.target.value })}
                                placeholder={'-----BEGIN CERTIFICATE-----\n…\n-----END CERTIFICATE-----\n(upload a file above or paste; blank clears it)'}
                                style={{
                                  width: '100%', padding: '6px 10px', borderRadius: '5px',
                                  border: '1px solid var(--border)', background: 'var(--bg-input)',
                                  color: 'var(--text-primary)', fontSize: 11, fontFamily: 'monospace',
                                  resize: 'vertical',
                                }} />
                            </div>
                          </div>

                          {/* Action row: save + the three rotate buttons + revoke */}
                          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', flexWrap: 'wrap' }}>
                            <button onClick={() => handleRotateJwtSecret(p.id)} title="Rotate JWT signing secret (Slice 3)" style={{
                              padding: '6px 10px', background: 'transparent', border: '1px solid var(--border)',
                              borderRadius: '5px', fontSize: 11, color: 'var(--text-secondary)', cursor: 'pointer',
                              display: 'flex', alignItems: 'center', gap: 4,
                            }}><RotateCcw size={11} /> Rotate JWT Secret</button>
                            <button onClick={() => handleRotateHmacSecret(p.id)} title="Rotate HMAC envelope secret (Slice 5)" style={{
                              padding: '6px 10px', background: 'transparent', border: '1px solid var(--border)',
                              borderRadius: '5px', fontSize: 11, color: 'var(--text-secondary)', cursor: 'pointer',
                              display: 'flex', alignItems: 'center', gap: 4,
                            }}><RotateCcw size={11} /> Rotate HMAC Secret</button>
                            <button onClick={() => handleRevokeSessions(p.id)} title="Revoke all active a2a_sessions (Slice 2)" style={{
                              padding: '6px 10px', background: 'transparent',
                              border: '1px solid rgba(224,108,108,0.4)',
                              borderRadius: '5px', fontSize: 11, color: 'var(--danger, #e06c6c)', cursor: 'pointer',
                              display: 'flex', alignItems: 'center', gap: 4,
                            }}><ShieldAlert size={11} /> Revoke Sessions</button>
                            <span style={{ flex: 1 }} />
                            <button onClick={() => handleSaveSecurity(p)} disabled={securitySaving[p.id]} style={{
                              padding: '6px 14px', background: 'var(--accent)', color: 'white',
                              border: 'none', borderRadius: '5px', fontSize: 11, fontWeight: 600,
                              cursor: securitySaving[p.id] ? 'not-allowed' : 'pointer',
                              opacity: securitySaving[p.id] ? 0.5 : 1,
                              display: 'flex', alignItems: 'center', gap: 4,
                            }}>
                              {securitySaving[p.id]
                                ? <Loader size={11} style={{ animation: 'spin 1s linear infinite' }} />
                                : <Save size={11} />}
                              Save Security
                            </button>
                          </div>
                        </div>
                      )}
                    </div>
                  )
                })()}
              </div>
            )
          })}
        </div>
          )}
        </div>
        )
      })()}
    </div>
  )
}
