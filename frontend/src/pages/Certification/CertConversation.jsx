// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useMemo, useState, useEffect } from 'react'
import { useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  MessagesSquare, RefreshCw, Code, Cpu, Building2, Lock, Check, X,
  Settings, Server, FlaskConical, Gavel, BadgeCheck, ClipboardList, Activity,
  Play, RotateCcw, AlertTriangle, ArrowRightLeft, Zap,
} from 'lucide-react'

import { a2aLogsApi, phaseCApi } from '../../services/api'
import PageHeader from '../../components/cert/PageHeader'
import KpiStrip from '../../components/cert/KpiStrip'
import Section from '../../components/cert/Section'
import { relativeTime } from '../../lib/certStatus'

// Cert A2A Conversation — the full certification lifecycle for one (feature, bank)
// as a structured, signed-A2A transcript. Sourced from the a2a_messages audit
// (/admin/a2a-logs). Messages are grouped by certification run (cflow_id), then
// by lifecycle phase; transport retries are de-duplicated and generic acks are
// folded into a "delivered" marker so the thread reads as a clean dialogue.

// ── Party (lane) identity ─────────────────────────────────────────────────
// Hues come straight from the platform cert palette (certStatus.js COLOR,
// mirrored in CertChangeDetail): blue = authority, amber = bank. The "soft" feel is
// carried by the *treatment* — faint lane tints + accent bars, never a fill.
const AUTHORITY = { key: 'npci', name: 'Authority Certification Agent', color: '#58a6ff', tint: 'rgba(88,166,255,0.07)', icon: Cpu }
const BANK = { key: 'bank', color: '#d29922', tint: 'rgba(210,153,34,0.07)', icon: Building2 }
// Cert status palette (shared with the rest of the Certification UI).
const OK = '#3fb950', BAD = '#e06c6c', WAIVE = '#bc8cff'

// Round 2 parties — the two network switches on the wire.
const CERT_SWITCH = { name: 'Certification Switch', color: '#58a6ff', icon: Cpu }
const BANK_SWITCH = { name: 'Bank Switch', color: '#d29922', icon: Server }

// ── Lifecycle phases (render order) + task-type → phase/label map ──────────
const PHASES = [
  { key: 'config',  label: 'Configuration',        icon: Settings },
  { key: 'setup',   label: 'Provisioning & setup',  icon: Server },
  { key: 'testing', label: 'Test execution',        icon: FlaskConical },
  { key: 'verdict', label: 'Verdict & waiver',      icon: Gavel },
  { key: 'signoff', label: 'Sign-off',              icon: BadgeCheck },
  { key: 'result',  label: 'Run summary',           icon: ClipboardList },
  { key: 'status',  label: 'Status updates',        icon: Activity },
]

const TASK_META = {
  cert_config_request:       { phase: 'config',  label: 'Configuration request' },
  cert_config_submission:    { phase: 'config',  label: 'Configuration submitted' },
  cert_setup_notification:   { phase: 'setup',   label: 'Setup & provisioning' },
  cert_test_preparation:     { phase: 'setup',   label: 'Test data prepared' },
  cert_test_request:         { phase: 'testing', label: 'Test request' },
  cert_test_instructions:    { phase: 'testing', label: 'Test instructions' },
  cert_case_result:          { phase: 'testing', label: 'Case result' },
  cert_verdict_notification: { phase: 'verdict', label: 'Verdict issued' },
  cert_waiver_request:       { phase: 'verdict', label: 'Waiver requested' },
  cert_waiver_decision:      { phase: 'verdict', label: 'Waiver decision' },
  cert_signoff_notification: { phase: 'signoff', label: 'Certification sign-off' },
  cert_test_response:        { phase: 'result',  label: 'Run summary' },
  cert_status_update:        { phase: 'status',  label: 'Status update' },
}

const prettify = t => (t || '').replace(/^cert_/, '').replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())
const metaFor = t => TASK_META[t] || { phase: 'status', label: prettify(t) }

// A short, coloured outcome pill derived from the message payload.
function outcomeOf(taskType, payload) {
  const st = (payload.status || '').toUpperCase()
  if (taskType === 'cert_case_result') {
    // Two vocabularies land here. The A2A spec's cert_case_result carries
    // passed/failed/error; the platform's older payloads carried PASS/FAIL/SKIP/
    // ERROR. Accept both so the transcript reads the same either way.
    if (st === 'PASS' || st === 'PASSED') return { label: 'Pass', color: OK }
    if (st === 'FAIL' || st === 'FAILED') return { label: 'Fail', color: BAD }
    // The spec has no "skipped", so a case that never produced a graded outcome
    // arrives as `error`. details.internal_status is the only thing that still
    // separates "nothing to grade against" from "something broke" — surface it,
    // because rendering an ungraded case as a failure is what made BE_22/BE_23
    // look like defects.
    const internal = (payload.details?.internal_status || '').toUpperCase()
    if (internal === 'SKIP') return { label: 'Skipped', color: 'var(--text-muted)' }
    if (st === 'ERROR' || internal === 'ERROR') return { label: 'Error', color: BAD }
    return st ? { label: st, color: 'var(--text-muted)' } : null
  }
  if (taskType === 'cert_verdict_notification' && payload.classification === 'waiver_eligible')
    return { label: 'Waiver-eligible', color: WAIVE }
  if (taskType === 'cert_waiver_decision')
    return /grant/i.test(payload.summary || '') ? { label: 'Waiver granted', color: WAIVE } : { label: 'Waiver', color: WAIVE }
  if (taskType === 'cert_signoff_notification') return { label: 'Certified', color: OK }
  return null
}

// Structured code delta (expected → actual) for result/verdict messages.
function codesOf(payload) {
  if (!payload.expected_code && !payload.actual_code) return null
  return { tc: payload.test_case_id, expected: payload.expected_code, actual: payload.actual_code }
}

// ── Turn model ─────────────────────────────────────────────────────────────
// Each audit row becomes 1 primary turn (the sent message) plus, when the
// receiver returned something meaningful, 1 reply turn. Generic acks
// (status=accepted, no task_type) become a `delivered` flag on the primary.
function buildTurns(items) {
  const turns = []
  for (const m of items) {
    const env = m.request_body || {}
    const payload = env.payload || {}
    const rep = m.response_body || null
    const repMeaningful = rep && (rep.task_type || rep.summary || rep.config || rep.test_data)
    const delivered = !!(rep && (rep.status === 'accepted' || rep.status === 'delivered' || repMeaningful))
    const primaryParty = m.direction === 'inbound' ? BANK : AUTHORITY

    turns.push({
      key: m.id + '-p',
      party: primaryParty.key,
      taskType: m.task_type,
      ...metaFor(m.task_type),
      summary: payload.summary || metaFor(m.task_type).label,
      outcome: outcomeOf(m.task_type, payload),
      codes: codesOf(payload),
      at: m.created_at,
      latency: m.latency_ms,
      delivered,
      raw: env,
    })

    if (repMeaningful) {
      const rp = rep.payload || rep
      const rt = rep.task_type || 'reply'
      turns.push({
        key: m.id + '-r',
        party: primaryParty.key === 'npci' ? 'bank' : 'npci',
        taskType: rt,
        phase: metaFor(m.task_type).phase,       // keep reply with its request's phase
        label: metaFor(rt).label,
        summary: rep.summary || rp.summary || metaFor(rt).label,
        outcome: outcomeOf(rt, rp),
        codes: codesOf(rp),
        at: m.created_at,
        latency: null,
        delivered: false,
        raw: rep,
      })
    }
  }
  return turns
}

// Collapse transport retries: identical (taskType, test_case, summary) messages
// on the same side keep only the richest instance (one that got a reply / latest).
function dedupe(items) {
  const byKey = new Map()
  for (const m of items) {
    const p = (m.request_body || {}).payload || {}
    const k = `${m.direction}|${m.task_type}|${p.test_case_id || ''}|${p.summary || ''}`
    const prev = byKey.get(k)
    if (!prev) { byKey.set(k, m); continue }
    const better = !!m.response_body && !prev.response_body
    const newer = new Date(m.created_at) > new Date(prev.created_at)
    if (better || (!!m.response_body === !!prev.response_body && newer)) byKey.set(k, m)
  }
  return [...byKey.values()].sort((a, b) => new Date(a.created_at) - new Date(b.created_at))
}

// ── Small presentational atoms ─────────────────────────────────────────────
function Avatar({ party, name }) {
  const P = party === 'bank' ? BANK : AUTHORITY
  const Icon = P.icon
  return (
    <div title={name || P.name} style={{
      width: 26, height: 26, borderRadius: 7, flexShrink: 0,
      background: `${P.color}1A`, border: `1px solid ${P.color}55`,
      display: 'flex', alignItems: 'center', justifyContent: 'center',
    }}>
      <Icon size={14} color={P.color} />
    </div>
  )
}

function Pill({ label, color }) {
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', padding: '2px 9px',
      borderRadius: 999, fontSize: 10, fontWeight: 700, letterSpacing: '0.03em',
      color, background: `${color}1A`, border: `1px solid ${color}40`, whiteSpace: 'nowrap',
    }}>{label}</span>
  )
}

function CodeDelta({ codes }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 8, flexWrap: 'wrap' }}>
      {codes.tc && <span className="id-mono" style={{ fontSize: 11, color: 'var(--text-secondary)' }}>{codes.tc}</span>}
      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 11, fontFamily: 'ui-monospace, monospace' }}>
        <span style={{ color: 'var(--text-muted)' }}>expected</span>
        <span style={{ padding: '1px 6px', borderRadius: 4, background: 'var(--bg-elevated)', border: '1px solid var(--border)', color: 'var(--text-secondary)' }}>{codes.expected ?? '—'}</span>
        <span style={{ color: 'var(--text-muted)' }}>got</span>
        <span style={{ padding: '1px 6px', borderRadius: 4, background: 'var(--bg-elevated)', border: '1px solid var(--border)', color: codes.expected === codes.actual ? 'var(--text-secondary)' : BAD }}>{codes.actual ?? '—'}</span>
      </span>
    </div>
  )
}

function MessageCard({ turn, bankName, txnIds, onViewTxn }) {
  const [raw, setRaw] = useState(false)
  const isNpci = turn.party === 'npci'
  const P = isNpci ? AUTHORITY : BANK
  const sender = isNpci ? AUTHORITY.name : `${bankName} Cert Agent`
  // A case-result message reports on one test case. If that case also executed on
  // the switch (Round 2), offer a jump to its underlying simulator transaction.
  const tcId = turn.taskType === 'cert_case_result' ? turn.codes?.tc : null
  const hasTxn = !!(tcId && onViewTxn && txnIds?.has(tcId))

  return (
    <div style={{ display: 'flex', justifyContent: isNpci ? 'flex-end' : 'flex-start', margin: '12px 0' }}>
      <div style={{
        position: 'relative', overflow: 'hidden',
        width: '82%', maxWidth: 620,
        background: `linear-gradient(${P.tint}, ${P.tint}), var(--bg-elevated)`,
        border: '1px solid var(--border-subtle)', borderRadius: 12,
      }}>
        <span aria-hidden style={{ position: 'absolute', left: 0, top: 0, bottom: 0, width: 3, background: P.color, opacity: 0.75 }} />
        <div style={{ padding: '13px 16px 13px 18px' }}>
          {/* Header: sender + task chip + outcome */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 9 }}>
            <Avatar party={turn.party} name={sender} />
            <div style={{ minWidth: 0, flex: 1 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 7, flexWrap: 'wrap' }}>
                <span style={{ fontSize: 12.5, fontWeight: 600, color: 'var(--text-primary)' }}>{sender}</span>
                <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>{turn.label}</span>
              </div>
              <span className="id-mono" style={{ fontSize: 9.5, color: 'var(--text-muted)', opacity: 0.85 }}>{turn.taskType}</span>
            </div>
            {turn.outcome && <Pill {...turn.outcome} />}
          </div>

          {/* Body */}
          {turn.summary && turn.summary !== turn.label && (
            <p style={{ margin: '9px 0 0', fontSize: 13, lineHeight: 1.5, color: 'var(--text-primary)' }}>{turn.summary}</p>
          )}
          {turn.codes && <CodeDelta codes={turn.codes} />}

          {/* Footer meta */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginTop: 10, flexWrap: 'wrap' }}>
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 10.5, color: 'var(--text-muted)' }} title={turn.at}>
              {relativeTime(turn.at)}
            </span>
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 10.5, color: 'var(--text-muted)' }} title="Signed + HMAC-sealed A2A">
              <Lock size={10} /> signed
            </span>
            {turn.delivered && (
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: 3, fontSize: 10.5, color: OK }} title="Receiver acknowledged">
                <Check size={11} /> delivered
              </span>
            )}
            {turn.latency != null && (
              <span style={{ fontSize: 10.5, color: 'var(--text-muted)', fontFamily: 'ui-monospace, monospace' }}>{turn.latency} ms</span>
            )}
            {hasTxn && (
              <button
                onClick={() => onViewTxn(tcId)}
                title={`View the cert-switch ⇄ bank-simulator transaction log for ${tcId}`}
                style={{ marginLeft: 'auto', display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 10.5, fontWeight: 600, color: CERT_SWITCH.color, background: 'none', border: 'none', cursor: 'pointer', padding: 0 }}
              >
                <ArrowRightLeft size={11} /> Transaction log →
              </button>
            )}
            <button
              onClick={() => setRaw(v => !v)}
              style={{ marginLeft: hasTxn ? 0 : 'auto', display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 10.5, color: 'var(--text-secondary)', background: 'none', border: 'none', cursor: 'pointer', padding: 0 }}
            >
              <Code size={11} /> {raw ? 'Hide envelope' : 'Raw envelope'}
            </button>
          </div>

          {raw && (
            <pre style={{
              margin: '10px 0 0', padding: '11px 13px',
              background: 'var(--bg-elevated)', border: '1px solid var(--border)', borderRadius: 8,
              fontSize: 11, lineHeight: 1.5, color: 'var(--text-secondary)',
              overflowX: 'auto', whiteSpace: 'pre-wrap', wordBreak: 'break-word', maxHeight: 320,
            }}>{JSON.stringify(turn.raw, null, 2)}</pre>
          )}
        </div>
      </div>
    </div>
  )
}

function PhaseDivider({ phase, count }) {
  const Icon = phase.icon
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 10, margin: '22px 0 6px' }}>
      <div style={{
        width: 24, height: 24, borderRadius: 6, flexShrink: 0,
        background: 'var(--bg-elevated)', border: '1px solid var(--border)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}>
        <Icon size={13} style={{ color: 'var(--text-secondary)' }} />
      </div>
      <span style={{ fontSize: 11, fontWeight: 700, letterSpacing: '0.07em', textTransform: 'uppercase', color: 'var(--text-secondary)' }}>
        {phase.label}
      </span>
      <span style={{ fontSize: 10.5, color: 'var(--text-muted)' }}>{count} message{count !== 1 ? 's' : ''}</span>
      <div style={{ flex: 1, height: 1, background: 'var(--border-subtle)' }} />
    </div>
  )
}

// ── Round 2 — online network switch↔switch exchange (from precertdb) ────────────
const prettyXml = x => (x || '').replace(/>\s*</g, '>\n<')

function XmlLeg({ dir, from, to, xml, rc, rcColor }) {
  const [open, setOpen] = useState(false)
  const FromIcon = from.icon
  return (
    <div style={{ borderLeft: `2px solid ${from.color}55`, paddingLeft: 10 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, flexWrap: 'wrap' }}>
        <FromIcon size={13} style={{ color: from.color }} />
        <span style={{ color: 'var(--text-primary)', fontWeight: 600 }}>{from.name}</span>
        <ArrowRightLeft size={12} style={{ color: 'var(--text-muted)' }} />
        <span style={{ color: 'var(--text-secondary)' }}>{to.name}</span>
        <span className="id-mono" style={{ fontSize: 10.5, color: 'var(--text-muted)' }}>{dir === 'req' ? 'request' : 'response'}</span>
        {rc != null && <span className="id-mono" style={{ fontSize: 11, color: rcColor, fontWeight: 600 }}>rc {rc}</span>}
        {xml && (
          <button onClick={() => setOpen(v => !v)} style={{ marginLeft: 'auto', display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 10.5, color: 'var(--text-secondary)', background: 'none', border: 'none', cursor: 'pointer', padding: 0 }}>
            <Code size={11} /> {open ? 'hide XML' : 'view XML'}
          </button>
        )}
      </div>
      {open && xml && (
        <pre style={{ margin: '6px 0 0', padding: '10px 12px', background: 'var(--bg-base)', border: '1px solid var(--border)', borderRadius: 8, fontSize: 10.5, lineHeight: 1.45, color: 'var(--text-secondary)', overflowX: 'auto', whiteSpace: 'pre-wrap', wordBreak: 'break-word', maxHeight: 300 }}>{prettyXml(xml)}</pre>
      )}
    </div>
  )
}

function NetworkExchange({ txn, highlight }) {
  const pass = (txn.review || '').toLowerCase() === 'success' || (txn.rc && txn.rc === txn.expected_rc)
  const rcColor = pass ? OK : BAD
  return (
    <div id={`upitxn-${txn.test_case_id}`} style={{
      background: 'var(--bg-elevated)',
      border: `1px solid ${highlight ? CERT_SWITCH.color : 'var(--border-subtle)'}`,
      boxShadow: highlight ? `0 0 0 3px ${CERT_SWITCH.color}3d` : 'none',
      borderRadius: 12, padding: '13px 16px', margin: '12px 0',
      transition: 'box-shadow .3s ease, border-color .3s ease',
      scrollMarginTop: 90,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 9, flexWrap: 'wrap' }}>
        <span className="id-mono" style={{ fontSize: 12.5, fontWeight: 700, color: 'var(--text-primary)' }}>{txn.test_case_id}</span>
        <span className="id-mono" style={{ fontSize: 10.5, color: 'var(--text-muted)' }}>{txn.api}</span>
        <span style={{ marginLeft: 'auto', display: 'inline-flex', alignItems: 'center', gap: 10 }}>
          <span style={{ fontSize: 11, fontFamily: 'ui-monospace,monospace', color: 'var(--text-muted)' }}>
            expected <span style={{ color: 'var(--text-secondary)' }}>{txn.expected_rc || '—'}</span> · got <span style={{ color: rcColor }}>{txn.rc || '—'}</span>
          </span>
          <Pill label={pass ? 'Pass' : 'Fail'} color={rcColor} />
        </span>
      </div>
      <div style={{ marginTop: 11, display: 'flex', flexDirection: 'column', gap: 9 }}>
        <XmlLeg dir="req" from={CERT_SWITCH} to={BANK_SWITCH} xml={txn.request_xml} />
        <XmlLeg dir="resp" from={BANK_SWITCH} to={CERT_SWITCH} xml={txn.response_xml} rc={txn.rc} rcColor={rcColor} />
      </div>
    </div>
  )
}

// ── Test case report modal — the per-case result set delivered to the bank ──
const RTh = ({ children }) => (
  <th style={{ textAlign: 'left', padding: '9px 18px', fontSize: 10, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em', color: 'var(--text-muted)', background: 'var(--bg-elevated)', whiteSpace: 'nowrap' }}>{children}</th>
)
const RTd = ({ children, mono, color, style }) => (
  <td className={mono ? 'id-mono' : undefined} style={{ padding: '9px 18px', fontSize: 12, color: color || 'var(--text-secondary)', whiteSpace: 'nowrap', ...style }}>{children}</td>
)

function ReportModal({ report, partnerName, cflow, onClose }) {
  const passed = report.filter(r => r.status === 'PASS').length
  const failed = report.filter(r => r.status === 'FAIL').length
  const hasInitiator = report.some(r => r.initiator)
  return (
    <div onClick={onClose} style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.55)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000, padding: 20 }}>
      <div onClick={e => e.stopPropagation()} style={{ background: 'var(--bg-card)', border: '1px solid var(--border)', borderRadius: 12, width: '92vw', maxWidth: 880, maxHeight: '86vh', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '15px 20px', borderBottom: '1px solid var(--border)' }}>
          <ClipboardList size={17} style={{ color: 'var(--accent)' }} />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontSize: 14, fontWeight: 700, color: 'var(--text-primary)' }}>Test case report</div>
            <div style={{ fontSize: 11.5, color: 'var(--text-muted)' }}>delivered to {partnerName}{cflow ? `  ·  ${cflow}` : ''}</div>
          </div>
          <button onClick={onClose} aria-label="Close" style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-secondary)', padding: 4, display: 'inline-flex' }}><X size={18} /></button>
        </div>
        <div style={{ display: 'flex', gap: 18, padding: '11px 20px', borderBottom: '1px solid var(--border-subtle)', fontSize: 12, color: 'var(--text-secondary)' }}>
          <span><b style={{ color: 'var(--text-primary)' }}>{report.length}</b> cases</span>
          <span style={{ color: OK }}><b>{passed}</b> passed</span>
          <span style={{ color: failed ? BAD : 'var(--text-muted)' }}><b>{failed}</b> failed</span>
        </div>
        <div style={{ overflow: 'auto' }}>
          {report.length === 0
            ? <div style={{ padding: 40, textAlign: 'center', color: 'var(--text-muted)', fontSize: 13 }}>No test case results in this run yet.</div>
            : (
              <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                <thead><tr style={{ position: 'sticky', top: 0 }}>
                  <RTh>Test case</RTh><RTh>API</RTh>{hasInitiator && <RTh>Initiator</RTh>}<RTh>Expected</RTh><RTh>Actual</RTh><RTh>Result</RTh>
                </tr></thead>
                <tbody>
                  {report.map(r => {
                    const pass = r.status === 'PASS'
                    return (
                      <tr key={r.tc} style={{ borderTop: '1px solid var(--border-subtle)' }}>
                        <RTd mono color="var(--text-primary)" style={{ fontWeight: 700 }}>{r.tc}</RTd>
                        <RTd mono color="var(--text-muted)">{r.api}</RTd>
                        {hasInitiator && <RTd style={{ textTransform: 'capitalize' }}>{r.initiator || '—'}</RTd>}
                        <RTd mono>{r.expected}</RTd>
                        <RTd mono color={pass ? OK : BAD}>{r.actual}</RTd>
                        <RTd><Pill label={pass ? 'Pass' : 'Fail'} color={pass ? OK : BAD} /></RTd>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            )}
        </div>
        <div style={{ padding: '10px 20px', borderTop: '1px solid var(--border-subtle)', fontSize: 11, color: 'var(--text-muted)' }}>
          This result set is delivered to the bank in the certification sign-off (cert_test_response).
        </div>
      </div>
    </div>
  )
}

// ── Page ────────────────────────────────────────────────────────────────────
export default function CertConversation() {
  const { crId, partnerId } = useParams()
  const [runId, setRunId] = useState(null)

  const { data, isLoading, refetch, isFetching } = useQuery({
    queryKey: ['cert-conversation', crId, partnerId],
    queryFn: () => a2aLogsApi.list({ change_request_id: crId, limit: 300 }).then(r => r.data),
    refetchInterval: 1500,   // snappy reveal — matches the ~2s demo pacing on the backend
  })

  const [busy, setBusy] = useState(null)   // 'run' | 'reset' | null
  const [notice, setNotice] = useState(null)
  const [round, setRound] = useState(1)    // 1 = Round 1 (offline, cert switch ⇄ bank simulator), 2 = Round 2 (online, real bank switch)
  const [r1tab, setR1tab] = useState('convo')  // Round 1 sub-view: 'convo' (agent↔agent) | 'txn' (switch↔simulator log)
  const [showReport, setShowReport] = useState(false)  // test case report modal

  const { data: r2data } = useQuery({
    queryKey: ['cert-network-txns', crId, partnerId],
    queryFn: () => phaseCApi.certTxns(crId, partnerId).then(r => r.data),
    refetchInterval: 1500,
  })
  const txns = r2data?.txns || []
  const txnIds = useMemo(() => new Set(txns.map(t => t.test_case_id)), [txns])
  const [jumpTc, setJumpTc] = useState(null)   // test case to scroll to in Round 1's transaction log
  const onViewTxn = tc => { setRound(1); setR1tab('txn'); setJumpTc(tc) }

  // On cross-link, open Round 1's transaction-log view, scroll the matching txn in + flash it.
  useEffect(() => {
    if (round !== 1 || r1tab !== 'txn' || !jumpTc) return
    const el = document.getElementById(`upitxn-${jumpTc}`)
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' })
    const t = setTimeout(() => setJumpTc(null), 2400)
    return () => clearTimeout(t)
  }, [round, r1tab, jumpTc, txns.length])

  async function runCert() {
    setBusy('run'); setNotice(null)
    try {
      await phaseCApi.demoRunCert(crId, partnerId)
      setNotice({ ok: true, text: 'Certification run started — messages stream in below as the two agents talk.' })
      setTimeout(() => refetch(), 1000)
    } catch (e) {
      setNotice({ ok: false, text: 'Run failed: ' + (e?.response?.data?.detail || e?.message || 'unknown error') })
    } finally { setBusy(null) }
  }

  async function resetCert() {
    if (!window.confirm(
      "Reset this bank's certification conversation and run history?\n\n"
      + 'This permanently clears the A2A messages and cert runs for this change + partner '
      + 'so you can start a fresh demo. Other partners are unaffected.'
    )) return
    setBusy('reset'); setNotice(null)
    try {
      await phaseCApi.demoResetCert(crId, partnerId)
      refetch()   // thread visibly empties — no success banner needed
    } catch (e) {
      setNotice({ ok: false, text: 'Reset failed: ' + (e?.response?.data?.detail || e?.message || 'unknown error') })
    } finally { setBusy(null) }
  }

  const model = useMemo(() => {
    const all = (data?.items || [])
      .filter(m => m.partner_id === partnerId && (m.task_type || '').startsWith('cert'))
      .sort((a, b) => new Date(a.created_at) - new Date(b.created_at))

    // Group into certification runs by cflow_id.
    const runMap = new Map()
    for (const m of all) {
      const cf = (m.request_body || {}).cflow_id || '—'
      if (!runMap.has(cf)) runMap.set(cf, [])
      runMap.get(cf).push(m)
    }
    const runs = [...runMap.entries()].map(([cflow, msgs]) => ({
      cflow,
      attempt: (msgs[0].request_body || {}).cert_attempt,
      count: msgs.length,
      first: msgs[0].created_at,
      last: msgs[msgs.length - 1].created_at,
      msgs,
    })).sort((a, b) => new Date(b.last) - new Date(a.last))

    const partnerName = all[0]?.partner_name || 'Partner bank'
    const changeTitle = all[0]?.change_title || (crId || '').slice(0, 8)
    return { runs, partnerName, changeTitle, empty: all.length === 0 }
  }, [data, partnerId, crId])

  const activeRun = model.runs.find(r => r.cflow === runId) || model.runs[0]

  const view = useMemo(() => {
    if (!activeRun) return null
    const clean = dedupe(activeRun.msgs)
    const turns = buildTurns(clean)
    const byPhase = PHASES.map(ph => ({ phase: ph, turns: turns.filter(t => t.phase === ph.key) }))
      .filter(g => g.turns.length > 0)

    const cases = clean.filter(m => m.task_type === 'cert_case_result')
      .map(m => (m.request_body?.payload?.status || '').toUpperCase())
    const passed = cases.filter(s => s === 'PASS').length
    const failed = cases.filter(s => s === 'FAIL').length
    const waivers = clean.filter(m => m.task_type === 'cert_waiver_decision').length
    return { byPhase, turnCount: turns.length, passed, failed, waivers }
  }, [activeRun])

  // Test case report — the per-case result set the Authority delivers to the bank at sign-off.
  // Sourced from the cert_case_result messages, enriched with the API from the txn log.
  const report = useMemo(() => {
    if (!activeRun) return []
    const txnByTc = new Map(txns.map(t => [t.test_case_id, t]))
    const seen = new Map()
    for (const m of activeRun.msgs) {
      if (m.task_type !== 'cert_case_result') continue
      const p = m.request_body?.payload || {}
      const tc = p.test_case_id
      if (!tc || seen.has(tc)) continue
      const t = txnByTc.get(tc)
      seen.set(tc, {
        tc,
        status:    (p.status || '').toUpperCase(),
        expected:  p.expected_code ?? t?.expected_rc ?? '—',
        actual:    p.actual_code ?? t?.rc ?? '—',
        api:       t?.api || p.api || '—',
        initiator: (p.initiator || p.reporter || '').toLowerCase(),
      })
    }
    return [...seen.values()]
  }, [activeRun, txns])

  // Round 1 (offline, cert switch ⇄ bank simulator) is what streams live — its two
  // facets are the A2A conversation (config/setup/case_result/verdict/sign-off) and
  // the switch↔simulator transaction log. Round 2 (online, cert switch ⇄ the bank's
  // real switch) is provisioned but not executed in this run.
  const lastMsg = activeRun ? activeRun.msgs[activeRun.msgs.length - 1] : null
  const lastType = lastMsg?.task_type
  const secsSince = lastMsg ? (Date.now() - new Date(lastMsg.created_at)) / 1000 : 1e9
  const terminal = lastType === 'cert_signoff_notification' || lastType === 'cert_test_response'
  const live = !!lastMsg && !terminal && secsSince < 12

  return (
    <div style={{ padding: 'var(--space-7)', maxWidth: 940 }}>
      <PageHeader
        icon={MessagesSquare}
        crumbs={[
          { label: 'Certification' },
          { label: 'Overview', to: '/certification/dashboard' },
          { label: (model.changeTitle || crId), to: `/certification/changes/${crId}` },
          { label: model.partnerName },
        ]}
        title={`A2A certification conversation — ${model.partnerName}`}
        subtitle={`${model.changeTitle} · full lifecycle over Google A2A, signed & HMAC-sealed, every message audited`}
        actions={
          <>
            <button
              onClick={runCert}
              disabled={busy === 'run'}
              title="Fire a fresh certification cycle for this bank over signed A2A"
              style={{ display: 'inline-flex', alignItems: 'center', gap: 6, padding: '7px 12px', background: 'var(--accent)', border: '1px solid var(--accent)', borderRadius: 6, color: 'white', fontSize: 12, fontWeight: 600, cursor: busy === 'run' ? 'wait' : 'pointer', opacity: busy === 'run' ? 0.7 : 1 }}
            >
              <Play size={12} /> {busy === 'run' ? 'Starting…' : 'Run certification'}
            </button>
            <button
              onClick={resetCert}
              disabled={busy === 'reset'}
              title="Clear this bank's conversation + run history so you can demo from scratch"
              style={{ display: 'inline-flex', alignItems: 'center', gap: 6, padding: '7px 12px', background: 'transparent', border: '1px solid var(--danger)', borderRadius: 6, color: 'var(--danger)', fontSize: 12, fontWeight: 600, cursor: busy === 'reset' ? 'wait' : 'pointer', opacity: busy === 'reset' ? 0.7 : 1 }}
            >
              <RotateCcw size={12} /> {busy === 'reset' ? 'Resetting…' : 'Reset'}
            </button>
            <button
              onClick={() => setShowReport(true)}
              title="View the per-test-case certification report delivered to the bank"
              style={{ display: 'inline-flex', alignItems: 'center', gap: 6, padding: '7px 12px', background: 'var(--bg-card)', border: '1px solid var(--border)', borderRadius: 6, color: 'var(--text-secondary)', fontSize: 12, fontWeight: 600, cursor: 'pointer' }}
            >
              <ClipboardList size={12} /> Test case report
            </button>
            <button
              onClick={() => refetch()}
              style={{ display: 'inline-flex', alignItems: 'center', gap: 6, padding: '7px 12px', background: 'var(--bg-card)', border: '1px solid var(--border)', borderRadius: 6, color: 'var(--text-secondary)', fontSize: 12, fontWeight: 600, cursor: 'pointer' }}
            >
              <RefreshCw size={12} style={isFetching ? { animation: 'spin 1s linear infinite' } : undefined} /> Refresh
            </button>
          </>
        }
      />

      {notice && (
        <div style={{
          margin: '0 0 var(--space-4)', padding: '10px 14px', borderRadius: 8, fontSize: 12.5,
          display: 'flex', alignItems: 'center', gap: 8,
          background: notice.ok ? 'rgba(63,185,80,0.10)' : 'rgba(224,108,108,0.10)',
          border: `1px solid ${notice.ok ? '#3fb95055' : '#e06c6c55'}`,
          color: notice.ok ? '#3fb950' : '#e06c6c',
        }}>
          {notice.ok ? <Check size={14} /> : <AlertTriangle size={14} />} {notice.text}
        </div>
      )}

      {isLoading && (
        <Section><div style={{ textAlign: 'center', padding: 32, color: 'var(--text-muted)', fontSize: 13 }}>Loading conversation…</div></Section>
      )}

      {!isLoading && model.empty && (
        <Section>
          <div style={{ textAlign: 'center', padding: 44, color: 'var(--text-muted)' }}>
            <MessagesSquare size={30} style={{ opacity: 0.25, marginBottom: 10 }} />
            <p style={{ margin: 0, fontSize: 13, color: 'var(--text-secondary)' }}>No certification A2A messages yet for this bank.</p>
            <p style={{ margin: '4px 0 0', fontSize: 11 }}>They appear once a certification run starts for this change and partner.</p>
          </div>
        </Section>
      )}

      {!isLoading && !model.empty && view && (
        <>
          {/* Run KPIs — Cases passed / failed / Waivers (Messages tile removed) */}
          <KpiStrip
            columns={3}
            tiles={[
              { label: 'Cases passed', value: view.passed, color: OK },
              { label: 'Cases failed', value: view.failed, color: view.failed ? BAD : 'var(--text-muted)' },
              { label: 'Waivers granted', value: view.waivers, color: view.waivers ? WAIVE : 'var(--text-muted)' },
            ]}
          />

          {/* Round selector — Round 1 (offline: cert switch ⇄ bank simulator) / Round 2 (online: cert switch ⇄ real bank switch) */}
          <div style={{ display: 'flex', gap: 10, margin: '0 0 var(--space-3)', flexWrap: 'wrap' }}>
            {[
              { n: 1, kind: 'offline', label: 'Round 1 · Offline', sub: 'Cert switch ⇄ bank simulator', icon: MessagesSquare, count: `${view.turnCount} messages · ${txns.length} transactions` },
              { n: 2, kind: 'online', label: 'Round 2 · Online', sub: 'Cert switch ⇄ bank switch (live)', icon: Zap, count: '—' },
            ].map(r => {
              const on = round === r.n
              const activeTab = live && r.kind === 'offline'
              const Icon = r.icon
              return (
                <button key={r.n} onClick={() => setRound(r.n)} style={{
                  flex: '1 1 260px', textAlign: 'left', cursor: 'pointer',
                  padding: '11px 14px', borderRadius: 10,
                  background: on ? 'var(--bg-elevated)' : 'var(--bg-card)',
                  border: `1.5px solid ${on ? 'var(--accent)' : 'var(--border)'}`,
                  opacity: r.kind === 'online' ? 0.9 : 1,
                }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
                    <Icon size={14} style={{ color: on ? 'var(--accent)' : 'var(--text-secondary)' }} />
                    <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-primary)' }}>{r.label}</span>
                    {r.kind === 'online'
                      ? <span style={{ marginLeft: 'auto', fontSize: 10, fontWeight: 600, color: 'var(--text-muted)', border: '1px solid var(--border)', borderRadius: 999, padding: '1px 7px' }}>not yet run</span>
                      : activeTab
                        ? <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5, marginLeft: 'auto', fontSize: 10.5, fontWeight: 600, color: OK }}><span className="live-dot" /> in progress</span>
                        : (terminal && <span style={{ marginLeft: 'auto', fontSize: 10.5, color: 'var(--text-muted)' }}>complete</span>)}
                  </div>
                  <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 3 }}>{r.sub} · {r.count}</div>
                </button>
              )
            })}
          </div>

          {/* Round 1 has two facets of the SAME offline round: the A2A conversation and the switch↔simulator transaction log */}
          {round === 1 && (
            <div style={{ display: 'flex', gap: 6, margin: '0 0 var(--space-4)', flexWrap: 'wrap' }}>
              {[
                { k: 'convo', label: 'Conversation', sub: 'Agent ↔ Agent', icon: MessagesSquare, count: view.turnCount },
                { k: 'txn', label: 'Transaction log', sub: 'Switch ↔ Simulator', icon: ArrowRightLeft, count: txns.length },
              ].map(s => {
                const on = r1tab === s.k
                const Icon = s.icon
                return (
                  <button key={s.k} onClick={() => setR1tab(s.k)} style={{
                    display: 'inline-flex', alignItems: 'center', gap: 6, cursor: 'pointer',
                    padding: '6px 12px', borderRadius: 999, fontSize: 11.5, fontWeight: 600,
                    color: on ? 'var(--accent)' : 'var(--text-secondary)',
                    background: on ? 'var(--bg-elevated)' : 'transparent',
                    border: `1px solid ${on ? 'var(--accent)' : 'var(--border)'}`,
                  }}>
                    <Icon size={12} /> {s.label}
                    <span style={{ fontSize: 10, color: 'var(--text-muted)', fontWeight: 500 }}>{s.sub} · {s.count}</span>
                  </button>
                )
              })}
            </div>
          )}

          {/* Run selector (only when >1 certification cycle exists) */}
          {model.runs.length > 1 && (
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, margin: '0 0 var(--space-4)', flexWrap: 'wrap' }}>
              <span style={{ fontSize: 11, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.06em', fontWeight: 600 }}>Run</span>
              {model.runs.map(r => {
                const on = r.cflow === activeRun.cflow
                return (
                  <button key={r.cflow} onClick={() => setRunId(r.cflow)} className="id-mono" style={{
                    padding: '4px 11px', borderRadius: 999, fontSize: 11, cursor: 'pointer',
                    color: on ? 'white' : 'var(--text-secondary)',
                    background: on ? 'var(--accent)' : 'var(--bg-card)',
                    border: `1px solid ${on ? 'var(--accent)' : 'var(--border)'}`,
                  }}>{r.cflow}{r.attempt ? ` · try ${r.attempt}` : ''}</button>
                )
              })}
            </div>
          )}

          {round === 1 && r1tab === 'convo' && (
            <Section
              title="Round 1 · Offline — Agent ↔ Agent"
              subtitle={`${activeRun.cflow}  ·  the signed A2A lifecycle conversation  ·  a case result links to its switch↔simulator transaction`}
            >
              {view.byPhase.map(g => (
                <div key={g.phase.key}>
                  <PhaseDivider phase={g.phase} count={g.turns.length} />
                  {g.turns.map(t => <MessageCard key={t.key} turn={t} bankName={model.partnerName} txnIds={txnIds} onViewTxn={onViewTxn} />)}
                </div>
              ))}
            </Section>
          )}

          {round === 1 && r1tab === 'txn' && (
            <Section
              title="Round 1 · Offline — Switch ↔ Simulator"
              subtitle="the network transactions the Round 1 results were graded from — precert (certification switch) ⇄ bank-sim (bank simulator)"
            >
              {txns.length === 0
                ? <div style={{ textAlign: 'center', padding: 36, color: 'var(--text-muted)', fontSize: 13 }}>No network transactions recorded yet for this run.</div>
                : txns.map(t => <NetworkExchange key={t.test_case_id} txn={t} highlight={jumpTc === t.test_case_id} />)}
            </Section>
          )}

          {round === 2 && (
            <Section
              title="Round 2 · Online — Switch ↔ Bank Switch"
              subtitle="live certification against the bank's real production switch"
            >
              <div style={{ textAlign: 'center', padding: 44, color: 'var(--text-muted)' }}>
                <Zap size={30} style={{ opacity: 0.25, marginBottom: 10 }} />
                <p style={{ margin: 0, fontSize: 13, color: 'var(--text-secondary)' }}>Round 2 (online) is not part of this run.</p>
                <p style={{ margin: '6px auto 0', fontSize: 11.5, lineHeight: 1.55, maxWidth: 480 }}>
                  Round 1 (offline) certifies the bank against the simulator — the agent↔agent conversation and its cert-switch ⇄ bank-simulator transaction log. Round 2 repeats the suite live, cert switch ⇄ the bank's real switch — provisioned here, pending enablement.
                </p>
              </div>
            </Section>
          )}
        </>
      )}

      <style>{`
        @keyframes spin { from { transform: rotate(0) } to { transform: rotate(360deg) } }
        @keyframes cpulse { 0%,100% { opacity: 1 } 50% { opacity: .25 } }
        .live-dot { width: 7px; height: 7px; border-radius: 50%; background: #3fb950; display: inline-block; animation: cpulse 1.2s ease-in-out infinite }
      `}</style>

      {showReport && (
        <ReportModal
          report={report}
          partnerName={model.partnerName}
          cflow={activeRun?.cflow}
          onClose={() => setShowReport(false)}
        />
      )}
    </div>
  )
}
