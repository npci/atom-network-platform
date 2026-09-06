// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useState, useEffect, useMemo } from 'react'
import { useQuery, useMutation } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import {
  Send, Loader, CheckCircle, XCircle, AlertCircle, Server, Globe, ArrowDownLeft,
} from 'lucide-react'
import { certA2AApi, partnersApi } from '../../services/api'
import { cleartextWarning } from '../../utils/cleartextHint'

// Hand-fire Part B (certification) A2A messages — internal testing surface.
//
// Three target modes:
//   partner  — registered partner_agents row; uses its endpoint + secrets, so
//              JWT minting and HMAC signing are exercised (writes an audit row).
//   endpoint — arbitrary base URL, e.g. a locally-run cert stack. Unsigned.
//   inbound  — we play the bank and post at our OWN ingress, so the receive
//              path (HMAC → JWT → session → executor) runs for real.
//
// The 5 Partner→Authority messages can only go through `inbound`; the 5 Authority→Partner ones
// only through partner/endpoint. The 4 Either messages work in all three.

const MODES = [
  { id: 'partner',  label: 'Registered partner', icon: Server,        hint: 'Signed — JWT + HMAC, writes an audit row' },
  { id: 'endpoint', label: 'Raw endpoint',       icon: Globe,         hint: 'Unsigned — point at a local cert stack' },
  { id: 'inbound',  label: 'Simulate inbound',   icon: ArrowDownLeft, hint: 'We act as the bank, posting at our own ingress' },
]

export default function CertA2ATrigger() {
  const [mode, setMode]                 = useState('endpoint')
  const [taskType, setTaskType]         = useState('')
  const [partnerId, setPartnerId]       = useState('')
  const [endpointUrl, setEndpointUrl]   = useState('http://cert-agent:8000')
  const [rpcUrl, setRpcUrl]             = useState('')
  const [cflowId, setCflowId]           = useState('CFLOW-TEST-001')
  const [certAttempt, setCertAttempt]   = useState(1)
  const [changeId, setChangeId]         = useState('')
  const [payloadText, setPayloadText]   = useState('')
  const [payloadErr, setPayloadErr]     = useState('')
  const [result, setResult]             = useState(null)

  const { data: tplData, isLoading: tplLoading } = useQuery({
    queryKey: ['cert-a2a-templates'],
    queryFn: () => certA2AApi.templates().then(r => r.data),
  })
  const { data: partnerData } = useQuery({
    queryKey: ['partners'],
    queryFn: () => partnersApi.list().then(r => r.data),
  })

  const templates = tplData?.templates || []
  const partners  = partnerData?.partners || partnerData || []

  // Which task types are legal for the chosen mode.
  const available = useMemo(() => templates.filter(t => (
    mode === 'inbound' ? t.direction !== 'npci_to_bank' : t.sendable
  )), [templates, mode])

  const current = templates.find(t => t.task_type === taskType)

  // Advisory only — the server makes the real decision (it can resolve the
  // host; the browser cannot). Surfaced here so an operator sees the problem
  // while typing rather than as a refusal after pressing Send.
  const endpointWarning = useMemo(
    () => (mode === 'endpoint' ? cleartextWarning(endpointUrl) : null),
    [mode, endpointUrl],
  )
  const rpcWarning = useMemo(
    () => (mode === 'endpoint' ? cleartextWarning(rpcUrl) : null),
    [mode, rpcUrl],
  )

  // Reset the selection when the mode makes it illegal, and reload the template
  // payload whenever the task type changes — the operator edits from a
  // spec-shaped starting point rather than a blank box.
  useEffect(() => {
    if (!available.length) return
    if (!available.some(t => t.task_type === taskType)) setTaskType(available[0].task_type)
  }, [available, taskType])

  useEffect(() => {
    const t = templates.find(x => x.task_type === taskType)
    if (t) { setPayloadText(JSON.stringify(t.payload, null, 2)); setPayloadErr(''); setResult(null) }
  }, [taskType, templates])

  const sendMut = useMutation({
    mutationFn: (body) => (
      mode === 'inbound' ? certA2AApi.simulateInbound(body) : certA2AApi.send(body)
    ).then(r => r.data),
    onSuccess: (d) => setResult(d),
    onError:   (e) => setResult({ status: 'error', detail: e?.response?.data?.detail || String(e) }),
  })

  const submit = () => {
    let payload
    try { payload = JSON.parse(payloadText) }
    catch (e) { setPayloadErr(`Invalid JSON: ${e.message}`); return }
    setPayloadErr(''); setResult(null)

    const base = { task_type: taskType, cflow_id: cflowId, cert_attempt: Number(certAttempt) || 1, payload }
    if (changeId.trim()) base.change_id = changeId.trim()
    if (mode === 'partner')  base.partner_id   = partnerId
    if (mode === 'endpoint') {
      base.endpoint_url = endpointUrl
      if (rpcUrl.trim()) base.rpc_url = rpcUrl.trim()
    }
    if (mode === 'inbound')  base.partner_id   = partnerId
    sendMut.mutate(base)
  }

  const needsPartner = mode === 'partner' || mode === 'inbound'
  const canSubmit = taskType && cflowId.trim() &&
    (mode === 'endpoint' ? endpointUrl.trim() : partnerId) && !sendMut.isPending

  const ok = result && ['delivered', 'accepted'].includes(result.status)

  return (
    <div style={{ padding: '20px 24px', maxWidth: 1100 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 4 }}>
        <Send size={18} style={{ color: 'var(--accent)' }} />
        <h1 style={{ fontSize: 18, fontWeight: 700, color: 'var(--text-primary)', margin: 0 }}>
          Cert A2A Trigger
        </h1>
      </div>
      <p style={{ fontSize: 12, color: 'var(--text-secondary)', marginBottom: 18 }}>
        Fire the 14 Part B certification messages by hand. Payloads are prefilled from the
        spec shapes — edit before sending.
      </p>

      {/* ── mode ── */}
      <div style={{ display: 'flex', gap: 8, marginBottom: 16, flexWrap: 'wrap' }}>
        {MODES.map(m => {
          const Icon = m.icon, active = mode === m.id
          return (
            <button key={m.id} onClick={() => { setMode(m.id); setResult(null) }} title={m.hint}
              style={{
                display: 'flex', alignItems: 'center', gap: 7, padding: '8px 13px',
                borderRadius: 8, fontSize: 12, fontWeight: 600, cursor: 'pointer',
                background: active ? 'var(--accent)' : 'var(--bg-elevated)',
                color: active ? 'white' : 'var(--text-secondary)',
                border: `1px solid ${active ? 'var(--accent)' : 'var(--border-subtle)'}`,
              }}>
              <Icon size={13} /> {m.label}
            </button>
          )
        })}
      </div>
      <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 16 }}>
        {MODES.find(m => m.id === mode)?.hint}
      </div>

      {/* ── target + envelope ── */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 14 }}>
        {needsPartner ? (
          <Field label={mode === 'inbound' ? 'Bank to impersonate' : 'Partner'}>
            <select value={partnerId} onChange={e => setPartnerId(e.target.value)} style={inputStyle}>
              <option value="">— select —</option>
              {partners.map(p => (
                <option key={p.id} value={p.id}>
                  {p.name}{p.endpoint_url ? '' : ' (no endpoint)'}
                </option>
              ))}
            </select>
          </Field>
        ) : (
          <Field label="Base URL" hint="bare host — discovery reads /.well-known/agent-card.json">
            <input value={endpointUrl} onChange={e => setEndpointUrl(e.target.value)} style={inputStyle} />
            <input value={rpcUrl} onChange={e => setRpcUrl(e.target.value)} style={{ ...inputStyle, marginTop: 6 }}
                   placeholder="RPC URL override (optional) — skips card discovery" />
            {(endpointWarning || rpcWarning) && (
              <div style={{
                marginTop: 8, padding: '8px 10px', borderRadius: 6, fontSize: 12, lineHeight: 1.5,
                background: 'rgba(245,165,36,0.10)', border: '1px solid rgba(245,165,36,0.45)',
                color: 'var(--text-primary)', display: 'flex', gap: 8, alignItems: 'flex-start',
              }}>
                <AlertCircle size={14} style={{ flexShrink: 0, marginTop: 2, color: '#f5a524' }} />
                <span>{endpointWarning || rpcWarning}</span>
              </div>
            )}
          </Field>
        )}
        <Field label="Task type">
          <select value={taskType} onChange={e => setTaskType(e.target.value)} style={inputStyle}
                  disabled={tplLoading}>
            {available.map(t => (
              <option key={t.task_type} value={t.task_type}>
                {t.task_type} · {t.direction === 'either' ? 'either' : t.direction.replace('_to_', '→')}
              </option>
            ))}
          </select>
        </Field>
        <Field label="cflow_id"><input value={cflowId} onChange={e => setCflowId(e.target.value)} style={inputStyle} /></Field>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 2fr', gap: 12 }}>
          <Field label="cert_attempt">
            <input type="number" min="1" value={certAttempt}
                   onChange={e => setCertAttempt(e.target.value)} style={inputStyle} />
          </Field>
          <Field label="change_id" hint="optional">
            <input value={changeId} onChange={e => setChangeId(e.target.value)} style={inputStyle} />
          </Field>
        </div>
      </div>

      {/* ── payload ── */}
      <Field label="Payload" hint={current ? `spec shape for ${current.task_type}` : ''}>
        <textarea value={payloadText} onChange={e => setPayloadText(e.target.value)} rows={16}
          spellCheck={false}
          style={{ ...inputStyle, fontFamily: 'monospace', fontSize: 11.5, lineHeight: 1.5, resize: 'vertical' }} />
      </Field>
      {payloadErr && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11.5, color: 'var(--danger)', marginTop: 6 }}>
          <AlertCircle size={12} /> {payloadErr}
        </div>
      )}

      <button onClick={submit} disabled={!canSubmit}
        style={{
          marginTop: 14, display: 'flex', alignItems: 'center', gap: 7, padding: '9px 18px',
          borderRadius: 8, border: 'none', fontSize: 13, fontWeight: 600,
          background: canSubmit ? 'var(--accent)' : 'var(--bg-elevated)',
          color: canSubmit ? 'white' : 'var(--text-muted)',
          cursor: canSubmit ? 'pointer' : 'not-allowed',
        }}>
        {sendMut.isPending ? <Loader size={13} style={{ animation: 'spin 1s linear infinite' }} /> : <Send size={13} />}
        {sendMut.isPending ? 'Sending…' : mode === 'inbound' ? 'Simulate inbound' : 'Send'}
      </button>

      {/* ── result ── */}
      {result && (
        <div style={{
          marginTop: 18, padding: '12px 14px', borderRadius: 8,
          background: ok ? 'var(--accent)10' : 'var(--danger)10',
          border: `1px solid ${ok ? 'var(--accent)40' : 'var(--danger)40'}`,
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
            {ok ? <CheckCircle size={14} style={{ color: 'var(--accent)' }} />
                : <XCircle size={14} style={{ color: 'var(--danger)' }} />}
            <span style={{ fontSize: 13, fontWeight: 700, color: 'var(--text-primary)' }}>
              {result.status}
            </span>
            {result.error_code && (
              <code style={{ fontSize: 11, color: 'var(--danger)' }}>{result.error_code}</code>
            )}
            {result.message_id && result.mode === 'partner' && (
              <Link to="/admin/a2a-logs" style={{ fontSize: 11, color: 'var(--accent)', marginLeft: 'auto' }}>
                view in A2A Logs →
              </Link>
            )}
          </div>
          {result.detail && (
            <div style={{ fontSize: 11.5, color: 'var(--text-secondary)', marginBottom: 8 }}>{result.detail}</div>
          )}
          {result.envelope && (
            <pre style={{
              margin: 0, padding: 10, borderRadius: 6, background: 'var(--bg-base)',
              fontSize: 11, lineHeight: 1.45, maxHeight: 320, overflow: 'auto',
              color: 'var(--text-secondary)',
            }}>{JSON.stringify(result.envelope, null, 2)}</pre>
          )}
        </div>
      )}
    </div>
  )
}

const inputStyle = {
  width: '100%', padding: '7px 10px', borderRadius: 6, fontSize: 12.5,
  background: 'var(--bg-base)', color: 'var(--text-primary)',
  border: '1px solid var(--border-subtle)', outline: 'none',
}

function Field({ label, hint, children }) {
  return (
    <div>
      <label style={{
        display: 'block', fontSize: 11, fontWeight: 600, marginBottom: 5,
        color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.4px',
      }}>
        {label}{hint && <span style={{ textTransform: 'none', fontWeight: 400, color: 'var(--text-muted)' }}> — {hint}</span>}
      </label>
      {children}
    </div>
  )
}
