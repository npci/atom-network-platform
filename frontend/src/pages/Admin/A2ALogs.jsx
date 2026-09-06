// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useState } from 'react'
import { t } from '../../strings'
import { useQuery } from '@tanstack/react-query'
import {
  Network, Filter, ChevronDown, ChevronRight, AlertCircle,
  ArrowDownLeft, ArrowUpRight, X, Loader, RefreshCw,
} from 'lucide-react'
import { a2aLogsApi } from '../../services/api'

// Slice 25 — Admin A2A communications log UI.
//
// Reads `/api/admin/a2a-logs` with the operator-supplied filter set.
// One row per a2a_messages audit row; expandable to show the full
// request_body and response_body JSON side by side.
//
// Filters AND together. Empty filters = no constraint. Pagination
// via limit + offset (server-side).

const PAGE_SIZE = 50

const TASK_TYPES = [
  '', // all
  // ── Protocol v1 — Phase C ──
  'change_communication',
  'proposal_acknowledged',
  'change_acknowledgement',
  'query',
  'clarification_response',
  'counter_proposal',
  'counter_decision',
  'milestone_update',
  'milestone_status_request',
  'milestone_status_report',
  'cert_readiness_declaration',
  'blocker',
  'blocker_status_update',
  'blocker_resolution',
  // ── Protocol v1 — Certification ──
  'cert_config_request',
  'cert_config_submission',
  'cert_setup_notification',
  'cert_test_preparation',
  'cert_case_result',
  'cert_verdict_notification',
  'cert_verdict_dispute',
  'cert_waiver_request',
  'cert_waiver_decision',
  'cert_fix_notification',
  'cert_signoff_notification',
  'cert_status_request',
  'cert_status_report',
  'cert_run_abort',
  'echo',
  // ── v1.0+ext ──
  'cert_witness_request',
  'cert_witness_scheduled',
  // ── Legacy (pre-v1, still present on historical rows) ──
  'status_update',
  'cert_query',
  'cert_status_update',
  'cert_test_request',
  'cert_test_response',
  'cert_acknowledgement',
  'cert_completion_signoff',
  'defect_notice',
  'defect_resolution',
]

export default function A2ALogs() {
  const [filters, setFilters] = useState({
    change_request_id: '',
    change_title:      '',
    partner_name:      '',
    direction:         '',
    task_type:         '',
    success_only:      false,
  })
  const [page, setPage] = useState(0)
  const [expandedId, setExpandedId] = useState(null)

  const { data: stats } = useQuery({
    queryKey: ['a2a-logs-stats'],
    queryFn: () => a2aLogsApi.stats().then(r => r.data),
    refetchInterval: 30000,
  })

  const { data, isLoading, isFetching, refetch } = useQuery({
    queryKey: ['a2a-logs', filters, page],
    queryFn: () => a2aLogsApi.list({
      ...trimEmpty(filters),
      limit:  PAGE_SIZE,
      offset: page * PAGE_SIZE,
    }).then(r => r.data),
    keepPreviousData: true,
  })

  const setFilter = (k, v) => {
    setFilters(prev => ({ ...prev, [k]: v }))
    setPage(0) // reset page on any filter change
  }
  const resetFilters = () => {
    setFilters({
      change_request_id: '', change_title: '', partner_name: '',
      direction: '', task_type: '', success_only: false,
    })
    setPage(0)
  }

  const totalPages = data ? Math.ceil(data.total / PAGE_SIZE) : 0

  return (
    <div style={{ padding: 32, maxWidth: 1400 }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
        <Network size={20} />
        <h1 style={{ margin: 0, fontSize: 20, fontWeight: 600, color: 'var(--text-primary)' }}>
          A2A Communications Log
        </h1>
      </div>
      <p style={{ margin: '0 0 24px', fontSize: 13, color: 'var(--text-muted)' }}>
        Every inbound and outbound A2A message with full request and response bodies.
        Filterable by change, title, or partner; click any row to expand.
      </p>

      {/* Stats */}
      {stats && (
        <div style={{ display: 'flex', gap: 16, marginBottom: 24 }}>
          {[
            { label: 'Total',    value: stats.total },
            { label: 'Inbound',  value: stats.inbound,  icon: ArrowDownLeft, color: '#6ea8dc' },
            { label: 'Outbound', value: stats.outbound, icon: ArrowUpRight,  color: '#4caf7d' },
            { label: 'Failed',   value: stats.failed,   icon: AlertCircle,   color: '#e06c6c' },
          ].map(s => {
            const Icon = s.icon
            return (
              <div key={s.label} style={{
                flex: 1, padding: '14px 16px', borderRadius: 8,
                background: 'var(--bg-elevated)', border: '1px solid var(--border)',
                textAlign: 'center',
              }}>
                <p style={{ margin: '0 0 2px', fontSize: 22, fontWeight: 700, color: s.color || 'var(--text-primary)' }}>
                  {s.value}
                </p>
                <p style={{ margin: 0, fontSize: 10, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.04em', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 4 }}>
                  {Icon && <Icon size={11} />} {s.label}
                </p>
              </div>
            )
          })}
        </div>
      )}

      {/* Filters */}
      <div style={{
        padding: 14, marginBottom: 16, borderRadius: 8,
        background: 'var(--bg-elevated)', border: '1px solid var(--border)',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12 }}>
          <Filter size={14} />
          <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-primary)' }}>Filters</span>
          {hasAnyFilter(filters) && (
            <button onClick={resetFilters} style={btnGhost}>
              <X size={11} /> Clear
            </button>
          )}
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr 140px 200px', gap: 10 }}>
          <FilterField
            label="Change Request ID (exact)"
            value={filters.change_request_id}
            onChange={v => setFilter('change_request_id', v)}
            placeholder="UUID"
          />
          <FilterField
            label="Change Title (substring)"
            value={filters.change_title}
            onChange={v => setFilter('change_title', v)}
            placeholder={t('ph.a2a.agentFilter')}
          />
          <FilterField
            label="Partner Name (substring)"
            value={filters.partner_name}
            onChange={v => setFilter('partner_name', v)}
            placeholder="e.g. SBI, ICICI"
          />
          <FilterSelect
            label="Direction"
            value={filters.direction}
            onChange={v => setFilter('direction', v)}
            options={[['', 'All'], ['inbound', 'Inbound'], ['outbound', 'Outbound']]}
          />
          <FilterSelect
            label="Task Type"
            value={filters.task_type}
            onChange={v => setFilter('task_type', v)}
            options={TASK_TYPES.map(t => [t, t || 'All'])}
          />
        </div>
        <label style={{ display: 'inline-flex', alignItems: 'center', gap: 6, marginTop: 10, fontSize: 12, color: 'var(--text-secondary)' }}>
          <input
            type="checkbox"
            checked={filters.success_only}
            onChange={e => setFilter('success_only', e.target.checked)}
          />
          Success only (hide rows with error_code)
        </label>
      </div>

      {/* Result count + pagination */}
      <div style={{ display: 'flex', alignItems: 'center', marginBottom: 8, fontSize: 12, color: 'var(--text-muted)' }}>
        <span>
          {data ? `${data.total.toLocaleString()} message${data.total === 1 ? '' : 's'} match` : 'Loading…'}
          {isFetching && !isLoading && <Loader size={11} style={{ marginLeft: 6, animation: 'spin 1s linear infinite', verticalAlign: 'middle' }} />}
        </span>
        <span style={{ flex: 1 }} />
        {totalPages > 1 && (
          <span style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <button disabled={page === 0} onClick={() => setPage(p => Math.max(0, p - 1))} style={btnGhost}>
              ← Prev
            </button>
            Page {page + 1} of {totalPages}
            <button disabled={page + 1 >= totalPages} onClick={() => setPage(p => p + 1)} style={btnGhost}>
              Next →
            </button>
          </span>
        )}
      </div>

      {/* Loading / empty */}
      {isLoading && <p style={{ color: 'var(--text-muted)', fontSize: 13 }}>Loading messages…</p>}
      {!isLoading && data && data.total === 0 && (
        <div style={{
          padding: '32px 24px', textAlign: 'center', borderRadius: 8,
          background: 'var(--bg-elevated)', border: '1px solid var(--border)',
        }}>
          <p style={{ margin: 0, fontSize: 13, color: 'var(--text-muted)' }}>
            No messages match the current filters.
          </p>
        </div>
      )}

      {/* Table */}
      {data && data.items.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          {data.items.map(m => (
            <MessageRow
              key={m.id}
              msg={m}
              expanded={expandedId === m.id}
              onToggle={() => setExpandedId(expandedId === m.id ? null : m.id)}
              onResent={refetch}
            />
          ))}
        </div>
      )}
    </div>
  )
}


function MessageRow({ msg, expanded, onToggle, onResent }) {
  const dirInbound = msg.direction === 'inbound'
  const Arrow = dirInbound ? ArrowDownLeft : ArrowUpRight
  const arrowColor = dirInbound ? '#6ea8dc' : '#4caf7d'
  const isFailed = !!msg.error_code

  return (
    <div style={{
      borderRadius: 6,
      background: 'var(--bg-elevated)',
      border: `1px solid ${isFailed ? 'rgba(224,108,108,0.3)' : 'var(--border)'}`,
      overflow: 'hidden',
    }}>
      <div onClick={onToggle} style={{
        display: 'grid',
        gridTemplateColumns: '20px 28px 1fr 220px 140px 80px 80px 70px',
        alignItems: 'center', gap: 10,
        padding: '10px 14px', cursor: 'pointer',
      }}>
        {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <Arrow size={14} style={{ color: arrowColor }} />
        <span style={{ fontSize: 12, color: 'var(--text-primary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          <strong>{msg.task_type}</strong>
          {msg.change_title && <span style={{ color: 'var(--text-muted)' }}> · {msg.change_title}</span>}
        </span>
        <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>{msg.partner_name || msg.partner_id?.slice(0, 8)}</span>
        <span style={{ fontSize: 11, color: 'var(--text-muted)', fontFamily: 'monospace' }}>
          {msg.created_at && new Date(msg.created_at).toLocaleString()}
        </span>
        <span style={statusBadge(msg.status, isFailed)} title={isFailed ? msg.error_code : undefined}>
          {msg.status}
        </span>
        <span style={{ fontSize: 10, color: 'var(--text-muted)', textAlign: 'right' }}>
          {msg.latency_ms != null ? `${msg.latency_ms}ms` : '—'}
        </span>
        <span style={{ fontSize: 10, color: 'var(--text-muted)', textAlign: 'right', fontFamily: 'monospace' }}>
          {msg.protocol_ver || ''}
        </span>
      </div>

      {expanded && (
        <div style={{ padding: '14px 18px', borderTop: '1px solid var(--border)', background: 'var(--bg-base)' }}>
          {/* Audit metadata */}
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 10, marginBottom: 14, fontSize: 11 }}>
            <Meta label="Audit Row ID"  value={msg.id} mono />
            <Meta label="Change Req"    value={msg.change_request_id} mono />
            <Meta label="Caller IP"     value={msg.caller_ip || '—'} />
            <Meta label="JWT Sub"       value={msg.jwt_sub || '—'} mono />
            <Meta label="Task State"    value={msg.task_state || '—'} />
            <Meta label="SDK Task ID"   value={msg.task_id_a2a || '—'} mono />
            <Meta label="mTLS FP"       value={msg.client_cert_fingerprint ? `${msg.client_cert_fingerprint.slice(0,4)}…${msg.client_cert_fingerprint.slice(-4)}` : '—'} mono />
            <Meta label="Latency"       value={msg.latency_ms != null ? `${msg.latency_ms} ms` : '—'} />
          </div>

          {/* Retry state + manual resend — outbound failures only. The sweeper retries
              transient errors automatically; this covers what it deliberately won't:
              attempts exhausted, or a non-retryable 4xx the operator has since fixed. */}
          {isFailed && !dirInbound && <ResendPanel msg={msg} onDone={onResent} />}

          {/* Bodies side by side */}
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
            <BodyPane title="Request body"  json={msg.request_body}  />
            <BodyPane title="Response body" json={msg.response_body} />
          </div>
        </div>
      )}
    </div>
  )
}


/**
 * ResendPanel — retry state for a failed outbound message + a manual resend action.
 *
 * Shows what the automatic sweeper is doing (attempts so far, when the next retry is due)
 * so an operator can tell "still being retried" from "given up". A message with no
 * next_retry_at is terminal — either the attempt cap was hit or the error is a
 * non-retryable 4xx — and the button is the only way to move it.
 */
function ResendPanel({ msg, onDone }) {
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState(null)

  const nextRetry = msg.next_retry_at ? new Date(msg.next_retry_at) : null
  const terminal = !nextRetry

  const doResend = async () => {
    setBusy(true); setResult(null)
    try {
      const { data } = await a2aLogsApi.resend(msg.id)
      setResult(data?.delivered
        ? { ok: true, text: `Delivered on attempt ${data.attempts}` }
        : { ok: false, text: `Still failing (${data?.error_code || 'unknown'}) — attempt ${data?.attempts}` })
      if (data?.delivered) onDone?.()
    } catch (e) {
      setResult({ ok: false, text: e?.response?.data?.detail || 'Resend failed' })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 14, flexWrap: 'wrap',
      padding: '10px 12px', marginBottom: 14, borderRadius: 6,
      background: 'rgba(224,108,108,0.06)', border: '1px solid rgba(224,108,108,0.25)',
    }}>
      <div style={{ fontSize: 11, color: 'var(--text-secondary)' }}>
        <strong style={{ color: '#e06c6c' }}>Not delivered</strong>
        {msg.error_code && <span style={{ fontFamily: 'monospace' }}> · {msg.error_code}</span>}
        <span> · attempts: {msg.attempts ?? 0}</span>
        <span> · {terminal
          ? 'no further automatic retry — resend manually'
          : `next auto-retry ${nextRetry.toLocaleTimeString()}`}</span>
      </div>
      <button
        type="button" onClick={doResend} disabled={busy}
        style={{
          display: 'inline-flex', alignItems: 'center', gap: 6,
          padding: '5px 12px', borderRadius: 6, fontSize: 11, fontWeight: 600,
          cursor: busy ? 'default' : 'pointer', opacity: busy ? 0.6 : 1,
          color: '#fff', background: '#c0554f', border: '1px solid #a8433e',
        }}
      >
        <RefreshCw size={12} style={busy ? { animation: 'spin 1s linear infinite' } : undefined} />
        {busy ? 'Resending…' : 'Resend now'}
      </button>
      {result && (
        <span style={{ fontSize: 11, fontWeight: 600, color: result.ok ? '#4caf7d' : '#e06c6c' }}>
          {result.text}
        </span>
      )}
    </div>
  )
}


function Meta({ label, value, mono }) {
  return (
    <div>
      <p style={{ margin: '0 0 2px', fontSize: 9, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>{label}</p>
      <p style={{
        margin: 0, fontSize: 11, color: 'var(--text-secondary)',
        fontFamily: mono ? 'monospace' : 'inherit',
        overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
      }} title={value}>{value}</p>
    </div>
  )
}


function BodyPane({ title, json }) {
  const empty = json == null || (typeof json === 'object' && Object.keys(json).length === 0)
  return (
    <div>
      <p style={{ margin: '0 0 6px', fontSize: 11, fontWeight: 600, color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>
        {title}
      </p>
      {empty ? (
        <pre style={preEmptyStyle}>(none)</pre>
      ) : (
        <pre style={preStyle}>{JSON.stringify(json, null, 2)}</pre>
      )}
    </div>
  )
}


function FilterField({ label, value, onChange, placeholder }) {
  return (
    <div>
      <label style={labelStyle}>{label}</label>
      <input
        value={value}
        onChange={e => onChange(e.target.value)}
        placeholder={placeholder}
        style={inputStyle}
      />
    </div>
  )
}


function FilterSelect({ label, value, onChange, options }) {
  return (
    <div>
      <label style={labelStyle}>{label}</label>
      <select value={value} onChange={e => onChange(e.target.value)} style={inputStyle}>
        {options.map(([v, l]) => (
          <option key={v} value={v}>{l}</option>
        ))}
      </select>
    </div>
  )
}


function statusBadge(status, isFailed) {
  return {
    fontSize: 10, padding: '2px 8px', borderRadius: 4, fontWeight: 600,
    background: isFailed ? 'rgba(224,108,108,0.10)' : 'rgba(76,175,125,0.10)',
    color:      isFailed ? '#e06c6c' : '#4caf7d',
    border: `1px solid ${isFailed ? 'rgba(224,108,108,0.30)' : 'rgba(76,175,125,0.30)'}`,
    textAlign: 'center', fontFamily: 'monospace',
  }
}


function trimEmpty(o) {
  const out = {}
  for (const k of Object.keys(o)) {
    if (o[k] === '' || o[k] === false || o[k] == null) continue
    out[k] = o[k]
  }
  return out
}


function hasAnyFilter(o) {
  return !!(
    o.change_request_id || o.change_title || o.partner_name ||
    o.direction || o.task_type || o.success_only
  )
}


const labelStyle = {
  display: 'block', fontSize: 10, color: 'var(--text-muted)',
  marginBottom: 4, textTransform: 'uppercase', letterSpacing: '0.04em',
}
const inputStyle = {
  width: '100%', padding: '7px 10px', borderRadius: 5,
  border: '1px solid var(--border)', background: 'var(--bg-input)',
  color: 'var(--text-primary)', fontSize: 12,
}
const btnGhost = {
  padding: '4px 10px', background: 'transparent', border: '1px solid var(--border)',
  borderRadius: 4, color: 'var(--text-muted)', fontSize: 11, cursor: 'pointer',
  display: 'inline-flex', alignItems: 'center', gap: 4,
}
const preStyle = {
  margin: 0, padding: 10, borderRadius: 5,
  background: 'var(--bg-input)', border: '1px solid var(--border)',
  fontSize: 11, color: 'var(--text-primary)', fontFamily: 'monospace',
  maxHeight: 400, overflow: 'auto',
}
const preEmptyStyle = { ...preStyle, color: 'var(--text-muted)', fontStyle: 'italic' }
