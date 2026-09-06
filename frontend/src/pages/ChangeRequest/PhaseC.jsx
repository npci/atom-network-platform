// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useState, useEffect, useRef } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { changesApi, evalApi, phaseCApi, partnersApi, resolverApi, escalationsApi, uiConfigApi } from '../../services/api'
import { kitDocLabel, formatKB } from '../../lib/kitLabels'
import EvalStatusPill from '../../components/eval/EvalStatusPill'
import PhaseALockedNotice from '../../components/common/PhaseALockedNotice'
import {
  ArrowLeft, Users, Send, Loader, CheckCircle, Circle, AlertCircle,
  Building2, Smartphone, Globe, Plus, Trash2, MessageSquare, X,
  Bot, UserCheck, ChevronDown, ChevronUp, ChevronRight, ChevronLeft, Edit3, Shield, Play, XCircle,
  Ban, ShieldCheck, Check, FileCheck, Calendar, Sparkles, RefreshCw,
  Lock, Unlock, Tag, BarChart2, Clock, Star, AlertTriangle, ToggleLeft, ToggleRight,
  Copy, FileText, Download, Paperclip, Package,
} from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import StatusStrip from '../../components/conversation/StatusStrip'
import TranscriptsDownloadButton from '../../components/TranscriptsDownloadButton'

const TYPE_META = {
  bank: { label: 'Bank', color: '#6ea8dc', icon: Building2 },
  psp:  { label: 'PSP',  color: '#4caf7d', icon: Smartphone },
  tpap: { label: 'TPAP', color: '#b388e8', icon: Globe },
}

const STATUS_COLORS = {
  // Pre-migration-0031 vocab (kept for back-compat)
  assigned:                { bg: 'var(--bg-elevated)',          color: 'var(--text-muted)', label: 'Assigned' },
  communicated:            { bg: 'rgba(218,119,86,0.1)',        color: 'var(--accent)',     label: 'Communicated' },
  acknowledged:            { bg: 'rgba(100,160,200,0.1)',       color: '#64a0c8',           label: 'Acknowledged' },
  in_progress:             { bg: 'rgba(218,119,86,0.1)',        color: 'var(--accent)',     label: 'In Progress' },
  ready:                   { bg: 'rgba(76,175,125,0.1)',        color: 'var(--success)',    label: 'Ready' },
  certified:               { bg: 'rgba(76,175,125,0.15)',       color: 'var(--success)',    label: 'Certified' },
  // Post-migration-0031 active vocabulary
  received:                { bg: 'rgba(218,119,86,0.1)',        color: 'var(--accent)',     label: 'Received' },
  accepted:                { bg: 'rgba(100,160,200,0.12)',      color: '#64a0c8',           label: 'Accepted' },
  applied:                 { bg: 'rgba(218,119,86,0.1)',        color: 'var(--accent)',     label: 'Applied' },
  tested:                  { bg: 'rgba(218,119,86,0.1)',        color: 'var(--accent)',     label: 'Tested' },
  ready_for_certification: { bg: 'rgba(76,175,125,0.10)',       color: 'var(--success)',    label: 'Ready for Cert' },
  certifying:              { bg: 'rgba(76,175,125,0.10)',       color: 'var(--success)',    label: 'Certifying' },
  ready_for_production:    { bg: 'rgba(76,175,125,0.15)',       color: 'var(--success)',    label: 'Ready for Prod' },
  in_production:           { bg: 'rgba(76,175,125,0.18)',       color: 'var(--success)',    label: 'In Production' },
  withdrawn:                { bg: 'var(--bg-elevated)',         color: 'var(--text-muted)', label: 'Withdrawn' },
}

// ── Negotiation Panel (per partner) ─────────────────────────────────────────



function btn(color, ghost = false) {
  return {
    display: 'inline-flex', alignItems: 'center', gap: '5px',
    padding: '5px 10px',
    background: ghost ? 'transparent' : `${color}1A`,
    border: `1px solid ${color}40`,
    borderRadius: '5px',
    color: color,
    fontSize: '11px', fontWeight: 600,
    cursor: 'pointer',
  }
}

function attentionChip(color, bg, border) {
  return {
    display: 'inline-flex', alignItems: 'center', gap: '5px',
    padding: '5px 12px',
    background: bg,
    border: `1px solid ${border}`,
    borderRadius: '20px',
    color: color,
    fontSize: '12px', fontWeight: 600,
    cursor: 'pointer',
  }
}

// ── Certification Panel (per partner) ───────────────────────────────────────

function CertPanel({ changeId, partnerId, partnerName, partnerStatus, blocked, blockedReason }) {
  const qc = useQueryClient()
  const [expanded, setExpanded] = useState(() => new Set(['ready_for_certification', 'ready', 'certifying']).has(partnerStatus))
  const [running, setRunning] = useState(false)
  const [triaging, setTriaging] = useState(false)
  const [selectedRun, setSelectedRun] = useState(null)
  const [dispatchError, setDispatchError] = useState(null)
  const [certRole, setCertRole] = useState('')

  const { data: runs, refetch } = useQuery({
    queryKey: ['cert-runs', changeId, partnerId],
    queryFn: () => phaseCApi.listCertRuns(changeId, partnerId).then(r => r.data),
    enabled: expanded,
    // A dispatched round completes asynchronously — partner-initiated cases
    // report over A2A and the join flips the verdict seconds later. Plain
    // interval, deliberately not conditional: a round can complete inside the
    // first tick and a condition latched on stale data stops polling exactly
    // when the verdict is about to land.
    refetchInterval: 5000,
  })

  const { data: runDetail } = useQuery({
    queryKey: ['cert-run-detail', changeId, partnerId, selectedRun],
    queryFn: () => phaseCApi.getCertRun(changeId, partnerId, selectedRun).then(r => r.data),
    enabled: !!selectedRun,
  })

  // The roles a partner can certify for come from the active domain pack's
  // cert vocabulary (config/ui.cert_roles) — never hardcoded here. Default to
  // the first declared role so one click dispatches a sensibly-scoped round.
  const { data: uiConfig } = useQuery({ queryKey: ['ui-config'], queryFn: uiConfigApi.get, staleTime: 300000 })
  const certRoles = uiConfig?.cert_roles || []
  useEffect(() => {
    if (!certRole && certRoles.length) setCertRole(certRoles[0].key)
  }, [certRoles, certRole])

  const handleStart = async () => {
    setRunning(true)
    setDispatchError(null)
    try {
      // The harness-agnostic dispatch seam — the same call the partner's
      // readiness declaration triggers. `advance` lets an operator-driven
      // round walk the assignment lifecycle forward through the real setter.
      const res = await phaseCApi.dispatchCert(changeId, partnerId, { role: certRole, advance: true })
      refetch()
      qc.invalidateQueries({ queryKey: ['phase-c-progress-grid', changeId] })
      qc.invalidateQueries({ queryKey: ['phase-c-partners', changeId] })
      if (res.data.run_id) setSelectedRun(res.data.run_id)
    } catch (e) {
      // A refusal (no baseline, empty scope, no harness) is the operator's to
      // read, not to guess at — surface the server's own reason verbatim.
      setDispatchError(e?.response?.data?.detail || e.message || 'dispatch failed')
    }
    finally { setRunning(false) }
  }

  // Cert can start from ready_for_certification (new), legacy 'ready', or
  // re-run while certifying. Once certified or beyond, the button is hidden.
  const READY_VALUES = new Set(['ready_for_certification', 'ready', 'certifying'])
  const POST_CERT    = new Set(['certified', 'ready_for_production', 'in_production'])
  const isReady     = READY_VALUES.has(partnerStatus) || POST_CERT.has(partnerStatus)
  const isCertified = POST_CERT.has(partnerStatus)
  const canStart    = (READY_VALUES.has(partnerStatus) || POST_CERT.has(partnerStatus)) && !blocked
  const isWithdrawn = partnerStatus === 'withdrawn'

  return (
    <div style={{
      marginBottom: '12px', borderRadius: '6px',
      background: 'var(--bg-elevated)', border: '1px solid var(--border-subtle)',
    }}>
      <div
        onClick={() => setExpanded(v => !v)}
        style={{ padding: '10px 16px', display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer' }}
      >
        <Shield size={13} style={{ color: isCertified ? 'var(--success)' : 'var(--accent)' }} />
        <span style={{ fontSize: '12px', fontWeight: '500', color: 'var(--text-primary)', flex: 1 }}>
          Certification — {partnerName}
        </span>
        {isCertified && (
          <span style={{ fontSize: '11px', padding: '1px 8px', borderRadius: '10px', background: 'rgba(76,175,125,0.15)', color: 'var(--success)', fontWeight: '600' }}>
            Certified
          </span>
        )}
        {runs?.length > 0 && !isCertified && (
          <span style={{ fontSize: '11px', color: 'var(--text-muted)', padding: '1px 6px', background: 'var(--bg-base)', borderRadius: '3px', border: '1px solid var(--border)' }}>
            {runs.length} run{runs.length !== 1 ? 's' : ''}
          </span>
        )}
        {expanded ? <ChevronUp size={13} style={{ color: 'var(--text-muted)' }} /> : <ChevronDown size={13} style={{ color: 'var(--text-muted)' }} />}
      </div>

      {expanded && (
        <div style={{ borderTop: '1px solid var(--border-subtle)', padding: '12px 16px' }}>
          {isWithdrawn && (
            <p style={{ margin: 0, fontSize: '12px', color: 'var(--danger)' }}>
              Partner has been withdrawn from this change. No further actions available.
            </p>
          )}
          {blocked && !isWithdrawn && (
            <div style={{
              display: 'flex', alignItems: 'flex-start', gap: '8px',
              padding: '8px 12px', marginBottom: '10px',
              background: 'rgba(255,127,155,0.08)', border: '1px solid rgba(255,127,155,0.3)',
              borderRadius: '5px', fontSize: '11px', color: 'var(--text-secondary)',
            }}>
              <Ban size={12} style={{ color: '#ff7f9b', marginTop: '2px', flexShrink: 0 }} />
              <span><strong>Blocked.</strong> {blockedReason || 'No reason given.'} Forward actions disabled until unblocked.</span>
            </div>
          )}
          {!isReady && !isWithdrawn && !blocked && (
            <p style={{ margin: 0, fontSize: '12px', color: 'var(--text-muted)' }}>
              Partner must declare readiness before certification can start.
            </p>
          )}

          {canStart && (
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap', marginBottom: runs?.length ? '12px' : 0 }}>
              <button onClick={handleStart} disabled={running} style={{
                display: 'flex', alignItems: 'center', gap: '5px',
                padding: '6px 14px', background: 'var(--accent)', color: 'white',
                border: 'none', borderRadius: '5px', fontSize: '11px', fontWeight: '600',
                cursor: running ? 'not-allowed' : 'pointer', opacity: running ? 0.6 : 1,
              }}>
                {running ? <Loader size={11} style={{ animation: 'spin 1s linear infinite' }} /> : <Play size={11} />}
                {runs?.length ? 'Re-run Certification' : 'Start Certification'}
              </button>
              {certRoles.length > 0 && (
                <label style={{ display: 'flex', alignItems: 'center', gap: '5px', fontSize: '11px', color: 'var(--text-muted)' }}>
                  as
                  <select value={certRole} onChange={e => setCertRole(e.target.value)} disabled={running}
                    style={{ fontSize: '11px', padding: '4px 6px', borderRadius: '4px',
                             background: 'var(--bg-base)', color: 'var(--text-primary)',
                             border: '1px solid var(--border)' }}>
                    {certRoles.map(r => <option key={r.key} value={r.key}>{r.label}</option>)}
                  </select>
                </label>
              )}
              {running && <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>Dispatching certification round…</span>}
            </div>
          )}
          {dispatchError && (
            <div style={{ display: 'flex', gap: '6px', alignItems: 'flex-start', margin: '8px 0',
                          padding: '8px 10px', borderRadius: '5px', fontSize: '11px',
                          background: 'rgba(255,127,155,0.08)', border: '1px solid rgba(255,127,155,0.25)',
                          color: 'var(--text-primary)' }}>
              <AlertCircle size={12} style={{ color: '#ff7f9b', marginTop: '1px', flexShrink: 0 }} />
              <span><strong>Dispatch refused.</strong> {String(dispatchError)}</span>
            </div>
          )}

          {/* Run history */}
          {runs?.map(run => (
            <div key={run.id}
              onClick={() => setSelectedRun(selectedRun === run.id ? null : run.id)}
              style={{
                padding: '10px 14px', marginBottom: '8px', borderRadius: '6px', cursor: 'pointer',
                background: selectedRun === run.id ? 'rgba(91,141,239,0.06)' : 'var(--bg-base)',
                border: `1px solid ${selectedRun === run.id ? 'rgba(91,141,239,0.2)' : 'var(--border-subtle)'}`,
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '12px' }}>
                <span style={{ fontWeight: '600', color: 'var(--text-primary)' }}>Run #{run.run_number}</span>
                <span style={{
                  fontSize: '11px', padding: '1px 6px', borderRadius: '3px', fontWeight: '600',
                  background: run.failed === 0 ? 'rgba(76,175,125,0.1)' : 'rgba(224,108,108,0.1)',
                  color: run.failed === 0 ? 'var(--success)' : 'var(--danger)',
                }}>{run.failed === 0 ? 'ALL PASS' : `${run.failed} FAILED`}</span>
                <span style={{ color: 'var(--success)', fontSize: '11px' }}>{run.passed} passed</span>
                {run.failed > 0 && <span style={{ color: 'var(--danger)', fontSize: '11px' }}>{run.failed} failed</span>}
                <span style={{ color: 'var(--text-muted)', fontSize: '11px', marginLeft: 'auto' }}>
                  {run.completed_at ? new Date(run.completed_at).toLocaleString() : ''}
                </span>
              </div>
            </div>
          ))}

          {/* Run detail — test results table with triage */}
          {runDetail && selectedRun && (
            <>
              {/* Triage button — shown when there are failures */}
              {runDetail.failed > 0 && (
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginTop: '8px', marginBottom: '8px' }}>
                  <button
                    onClick={async () => {
                      setTriaging(true)
                      try {
                        await phaseCApi.triggerTriage(changeId, partnerId)
                        qc.invalidateQueries({ queryKey: ['cert-run-detail', changeId, partnerId, selectedRun] })
                      } catch { /* ignore */ }
                      finally { setTriaging(false) }
                    }}
                    disabled={triaging}
                    style={{
                      display: 'flex', alignItems: 'center', gap: '5px',
                      padding: '6px 14px', background: 'rgba(218,119,86,0.1)', color: 'var(--accent)',
                      border: '1px solid rgba(218,119,86,0.3)', borderRadius: '5px',
                      fontSize: '11px', fontWeight: '600', cursor: triaging ? 'not-allowed' : 'pointer',
                    }}
                  >
                    {triaging ? <Loader size={11} style={{ animation: 'spin 1s linear infinite' }} /> : <Bot size={11} />}
                    {triaging ? 'Analyzing failures…' : 'AI Triage Failed Tests'}
                  </button>
                </div>
              )}

              <div style={{ border: '1px solid var(--border)', borderRadius: '6px', overflow: 'auto' }}>
                <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '11px' }}>
                  <thead>
                    <tr style={{ background: 'var(--bg-base)', borderBottom: '1px solid var(--border)' }}>
                      <th style={{ padding: '6px 10px', textAlign: 'left', fontWeight: '600', color: 'var(--text-muted)', fontSize: '11px', textTransform: 'uppercase' }}>ID</th>
                      <th style={{ padding: '6px 10px', textAlign: 'left', fontWeight: '600', color: 'var(--text-muted)', fontSize: '11px', textTransform: 'uppercase' }}>Direction</th>
                      <th style={{ padding: '6px 10px', textAlign: 'left', fontWeight: '600', color: 'var(--text-muted)', fontSize: '11px', textTransform: 'uppercase' }}>Status</th>
                      <th style={{ padding: '6px 10px', textAlign: 'left', fontWeight: '600', color: 'var(--text-muted)', fontSize: '11px', textTransform: 'uppercase' }}>Latency</th>
                      <th style={{ padding: '6px 10px', textAlign: 'left', fontWeight: '600', color: 'var(--text-muted)', fontSize: '11px', textTransform: 'uppercase' }}>Triage</th>
                    </tr>
                  </thead>
                  <tbody>
                    {runDetail.results.map((r, i) => {
                      const verdictColors = {
                        partner_code_bug: { bg: 'rgba(224,108,108,0.1)', color: 'var(--danger)', label: 'Code Bug' },
                        test_case_issue: { bg: 'rgba(218,119,86,0.1)', color: 'var(--accent)', label: 'Test Issue' },
                        env_issue: { bg: 'rgba(100,160,200,0.1)', color: '#64a0c8', label: 'Env Issue' },
                      }
                      const tv = r.triage
                      const vc = tv ? verdictColors[tv.final_verdict || tv.ai_verdict] : null

                      return (
                        <tr key={r.id} style={{ borderBottom: i < runDetail.results.length - 1 ? '1px solid var(--border-subtle)' : 'none' }}>
                          <td style={{ padding: '6px 10px', fontFamily: 'monospace', color: 'var(--text-primary)' }}>{r.test_case_id || '—'}</td>
                          <td style={{ padding: '6px 10px', color: 'var(--text-secondary)' }}>
                            {r.direction === 'npci_to_partner' ? 'the Authority → Partner' : 'Partner → the Authority'}
                          </td>
                          <td style={{ padding: '6px 10px' }}>
                            <span style={{
                              display: 'inline-flex', alignItems: 'center', gap: '4px',
                              fontSize: '10px', fontWeight: '600',
                              color: r.status === 'pass' ? 'var(--success)' : r.status === 'fail' ? 'var(--danger)' : 'var(--text-muted)',
                            }}>
                              {r.status === 'pass' ? <CheckCircle size={10} /> : r.status === 'fail' ? <XCircle size={10} /> : <Circle size={10} />}
                              {r.status.toUpperCase()}
                            </span>
                          </td>
                          <td style={{ padding: '6px 10px', color: 'var(--text-muted)' }}>{r.latency_ms ? `${r.latency_ms}ms` : '—'}</td>
                          <td style={{ padding: '6px 10px' }}>
                            {tv && vc ? (
                              <div>
                                <span style={{
                                  fontSize: '11px', padding: '2px 6px', borderRadius: '3px', fontWeight: '600',
                                  background: vc.bg, color: vc.color, display: 'inline-block', marginBottom: tv.ai_reasoning ? '3px' : 0,
                                }}>
                                  {vc.label}{tv.final_verdict ? ' ✓' : ''}
                                </span>
                                {tv.ai_reasoning && (
                                  <div style={{ fontSize: '10px', color: 'var(--text-muted)', lineHeight: '1.3' }}>
                                    {tv.ai_reasoning.slice(0, 80)}{tv.ai_reasoning.length > 80 ? '…' : ''}
                                  </div>
                                )}
                                {!tv.final_verdict && (
                                  <div style={{ display: 'flex', gap: '4px', marginTop: '4px' }}>
                                    {Object.entries(verdictColors).map(([v, meta]) => (
                                      <button key={v} onClick={async () => {
                                        await phaseCApi.approveTriage(changeId, partnerId, tv.id, v)
                                        qc.invalidateQueries({ queryKey: ['cert-run-detail', changeId, partnerId, selectedRun] })
                                      }} style={{
                                        padding: '2px 8px', fontSize: '11px', borderRadius: '3px', minHeight: '24px',
                                        background: v === tv.ai_verdict ? meta.bg : 'transparent',
                                        border: `1px solid ${v === tv.ai_verdict ? meta.color : 'var(--border)'}`,
                                        color: meta.color, cursor: 'pointer', fontWeight: '600',
                                      }}>
                                        {meta.label}
                                      </button>
                                    ))}
                                  </div>
                                )}
                              </div>
                            ) : r.status === 'fail' ? (
                              <span style={{ fontSize: '10px', color: 'var(--text-muted)' }}>Run triage ↑</span>
                            ) : '—'}
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            </>
          )}


        </div>
      )}
    </div>
  )
}

// ── Negotiation Panel (per partner) ─────────────────────────────────────────

// ── Partner chat list (left rail) — built for 350+ banks ───────────────────
//
// Keeps the master/detail container scalable: instead of stacking one
// chat panel per partner (which doesn't fit on screen past ~5 partners),
// we render a virtual-friendly list with search + per-partner pending
// badge, and only the selected partner's chat lives in the right pane.
function PartnerChatList({ partners, pendingByPartner, selectedPartnerId, onSelect, collapsed, onToggleCollapse }) {
  const [filter, setFilter] = useState('')

  const filtered = (partners || []).filter(p =>
    !filter || p.name.toLowerCase().includes(filter.toLowerCase())
  )

  // Two-letter avatar from the partner name — handles "SBI", "HDFC Bank", etc.
  const avatarOf = (name) => {
    const words = (name || '?').split(/\s+/).filter(Boolean)
    return (words[0]?.[0] || '?').toUpperCase() + (words[1]?.[0] || '').toUpperCase()
  }

  return (
    <div style={{
      display: 'flex', flexDirection: 'column', minHeight: 0,
      borderRight: '1px solid var(--border-subtle)', background: 'var(--bg-elevated)',
    }}>
      {/* Toggle header — chevron flips with collapsed state */}
      <div style={{
        padding: collapsed ? '8px 4px' : '8px 12px',
        borderBottom: '1px solid var(--border-subtle)',
        display: 'flex', alignItems: 'center',
        justifyContent: collapsed ? 'center' : 'space-between',
        gap: 6,
      }}>
        {!collapsed && (
          <input
            type="text"
            value={filter}
            onChange={e => setFilter(e.target.value)}
            placeholder="Search partners…"
            style={{
              flex: 1, padding: '7px 10px', borderRadius: '6px',
              border: '1px solid var(--border)', background: 'var(--bg-input)',
              color: 'var(--text-primary)', fontSize: '12px', fontFamily: 'inherit',
            }}
          />
        )}
        <button
          onClick={onToggleCollapse}
          title={collapsed ? 'Expand partner list' : 'Collapse partner list'}
          style={{
            display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
            width: 28, height: 28, padding: 0,
            background: 'transparent', border: 'none', cursor: 'pointer',
            color: 'var(--text-muted)', borderRadius: 4,
          }}
        >
          {collapsed ? <ChevronRight size={14} /> : <ChevronLeft size={14} />}
        </button>
      </div>

      {/* List — scrolls independently */}
      <div style={{ flex: 1, minHeight: 0, overflowY: 'auto' }}>
        {filtered.length === 0 && !collapsed && (
          <div style={{ padding: '24px 16px', textAlign: 'center', fontSize: '12px', color: 'var(--text-muted)' }}>
            No partners {filter ? 'match the search' : 'yet'}.
          </div>
        )}
        {filtered.map(p => {
          const isActive = p.partner_id === selectedPartnerId
          const pending = pendingByPartner[p.partner_id] || 0
          if (collapsed) {
            // Compact row: 40px circle avatar + pending badge overlay.
            return (
              <div
                key={p.partner_id}
                onClick={() => onSelect(p.partner_id)}
                title={`${p.name}${pending > 0 ? ` · ${pending} pending` : ''}`}
                style={{
                  padding: '8px 6px', cursor: 'pointer',
                  borderLeft: isActive ? '3px solid var(--accent)' : '3px solid transparent',
                  background: isActive ? 'rgba(218,119,86,0.06)' : 'transparent',
                  borderBottom: '1px solid var(--border-subtle)',
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  position: 'relative',
                }}
              >
                <div style={{
                  width: 34, height: 34, borderRadius: '50%',
                  background: isActive ? 'var(--accent)' : 'var(--bg-soft)',
                  color: isActive ? '#fff' : 'var(--text-secondary)',
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  fontSize: 11, fontWeight: 700, letterSpacing: 0.4,
                  border: isActive ? 'none' : '1px solid var(--border-subtle)',
                }}>{avatarOf(p.name)}</div>
                {pending > 0 && (
                  <span style={{
                    position: 'absolute', top: 4, right: 4,
                    fontSize: 9, padding: '1px 5px', borderRadius: 10, fontWeight: 700,
                    background: 'var(--accent)', color: 'white', minWidth: 14, textAlign: 'center',
                    border: '2px solid var(--bg-elevated)',
                  }}>{pending}</span>
                )}
              </div>
            )
          }
          return (
            <div
              key={p.partner_id}
              onClick={() => onSelect(p.partner_id)}
              style={{
                padding: '10px 14px', cursor: 'pointer',
                borderLeft: isActive ? '3px solid var(--accent)' : '3px solid transparent',
                background: isActive ? 'rgba(218,119,86,0.06)' : 'transparent',
                borderBottom: '1px solid var(--border-subtle)',
                display: 'flex', alignItems: 'center', gap: '8px',
              }}
            >
              <Users size={12} style={{ color: '#6ea8dc', flexShrink: 0 }} />
              <span style={{
                fontSize: '12px',
                fontWeight: isActive ? '600' : '500',
                color: 'var(--text-primary)', flex: 1,
                overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
              }}>{p.name}</span>
              {pending > 0 && (
                <span style={{
                  fontSize: '10px', padding: '2px 7px', borderRadius: '10px', fontWeight: '700',
                  background: 'var(--accent)', color: 'white', minWidth: '18px', textAlign: 'center',
                }}>{pending}</span>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}


// ── Action card for an open counter-proposal (Tier 1 + multi-round) ───────
function CounterActionCard({ changeId, partnerId, cp, onResolved }) {
  const [mode, setMode] = useState(null)  // null | 'reject' | 'counter'
  const [rationale, setRationale] = useState('')
  const [busy, setBusy] = useState(false)

  // PM only acts on PARTNER-originated counters. The Authority's own outbound
  // counters are display-only on this side (the partner is the one
  // who needs to respond next).
  const isFromPartner = (cp.originator || 'partner') === 'partner'
  const headerLabel = isFromPartner ? 'Partner counter' : 'the Authority counter (sent)'
  const headerColor = isFromPartner ? 'var(--accent)' : '#64a0c8'

  const accept = async () => {
    setBusy(true)
    try { await phaseCApi.acceptCounter(changeId, partnerId, cp.id); onResolved() }
    finally { setBusy(false) }
  }
  const reject = async () => {
    if (!rationale.trim()) return
    setBusy(true)
    try { await phaseCApi.rejectCounter(changeId, partnerId, cp.id, rationale); onResolved() }
    finally { setBusy(false) }
  }
  const counterBack = async () => {
    if (!rationale.trim()) return
    setBusy(true)
    try {
      await phaseCApi.counterBackCounter(changeId, partnerId, cp.id, {
        justification: rationale, valid_until_days: 7,
      })
      onResolved()
    } finally { setBusy(false) }
  }

  return (
    <div style={{
      padding: '10px 12px', marginBottom: 8,
      borderRadius: 8,
      background: isFromPartner ? 'rgba(218,119,86,0.06)' : 'rgba(100,160,200,0.06)',
      border: `1px solid ${isFromPartner ? 'rgba(218,119,86,0.30)' : 'rgba(100,160,200,0.30)'}`,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
        <span style={{ fontSize: 10, fontWeight: 700, padding: '2px 7px', borderRadius: 4, background: headerColor, color: 'white', textTransform: 'uppercase', letterSpacing: '0.5px' }}>
          {headerLabel}
        </span>
        {cp.valid_until && (
          <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>· valid until {new Date(cp.valid_until).toLocaleDateString()}</span>
        )}
      </div>
      <div style={{ fontSize: 12, lineHeight: 1.5, color: 'var(--text-primary)', marginBottom: 8, whiteSpace: 'pre-wrap' }}>
        {cp.justification}
      </div>

      {/* Display-only when waiting on partner */}
      {!isFromPartner && (
        <div style={{ fontSize: 11, color: 'var(--text-muted)', fontStyle: 'italic' }}>
          Awaiting partner response.
        </div>
      )}

      {/* PM action area — only for partner-originated open counters */}
      {isFromPartner && mode === null && (
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          <button onClick={accept} disabled={busy} style={{
            display: 'flex', alignItems: 'center', gap: 4, padding: '6px 12px',
            background: 'var(--success)', color: 'white', border: 'none', borderRadius: 5,
            fontSize: 11, fontWeight: 600, cursor: busy ? 'not-allowed' : 'pointer', opacity: busy ? 0.6 : 1,
          }}>
            <Check size={11} /> Accept counter
          </button>
          <button onClick={() => setMode('counter')} disabled={busy} style={{
            display: 'flex', alignItems: 'center', gap: 4, padding: '6px 12px',
            background: 'var(--accent)', color: 'white', border: 'none', borderRadius: 5,
            fontSize: 11, fontWeight: 600, cursor: 'pointer',
          }}>
            <Edit3 size={11} /> Counter back
          </button>
          <button onClick={() => setMode('reject')} disabled={busy} style={{
            display: 'flex', alignItems: 'center', gap: 4, padding: '6px 12px',
            background: 'transparent', color: 'var(--danger)', border: '1px solid var(--danger)',
            borderRadius: 5, fontSize: 11, fontWeight: 600, cursor: 'pointer',
          }}>
            <X size={11} /> Reject
          </button>
        </div>
      )}

      {isFromPartner && mode !== null && (
        <div>
          <textarea
            value={rationale}
            onChange={e => setRationale(e.target.value)}
            placeholder={mode === 'reject'
              ? 'Why are you rejecting this counter?'
              : 'Your counter terms — what you can/can\'t accept and what you propose instead.'}
            rows={mode === 'counter' ? 4 : 2}
            style={{
              width: '100%', padding: '6px 8px', borderRadius: 5,
              border: '1px solid var(--border)', background: 'var(--bg-input)',
              color: 'var(--text-primary)', fontSize: 11, fontFamily: 'inherit', marginBottom: 6,
            }}
          />
          <div style={{ display: 'flex', gap: 6 }}>
            <button
              onClick={mode === 'reject' ? reject : counterBack}
              disabled={busy || !rationale.trim()}
              style={{
                padding: '6px 12px',
                background: mode === 'reject' ? 'var(--danger)' : 'var(--accent)',
                color: 'white', border: 'none', borderRadius: 5,
                fontSize: 11, fontWeight: 600,
                cursor: (busy || !rationale.trim()) ? 'not-allowed' : 'pointer',
                opacity: (busy || !rationale.trim()) ? 0.6 : 1,
              }}
            >
              {mode === 'reject' ? 'Send rejection' : 'Send counter'}
            </button>
            <button onClick={() => { setMode(null); setRationale('') }} style={{
              padding: '6px 12px', background: 'transparent', color: 'var(--text-muted)',
              border: '1px solid var(--border)', borderRadius: 5, fontSize: 11, cursor: 'pointer',
            }}>Cancel</button>
          </div>
        </div>
      )}
    </div>
  )
}


// ── Action card for an open blocker (Tier 2) ──────────────────────────────
// The structured, TERMINAL resolve path: posts action_taken / resolution_text /
// artifact_ref to /resolve, which closes the Blocker row and sends BLOCKER_RESOLUTION.
// Rendered above the stream on the Blockers tab (NegotiationPanel). A free-text reply on
// that tab is a separate, NON-terminal interim status note — only this card closes a blocker.
function BlockerActionCard({ changeId, partnerId, blocker, onResolved }) {
  const [showResolveForm, setShowResolveForm] = useState(false)
  const [actionTaken, setActionTaken] = useState('')
  const [resolutionText, setResolutionText] = useState('')
  const [artifactRef, setArtifactRef] = useState('')
  const [busy, setBusy] = useState(false)

  const sevColor = blocker.severity === 'critical' ? 'var(--danger)' :
                   blocker.severity === 'high'     ? '#e67e22' :
                   blocker.severity === 'medium'   ? 'var(--accent)' : 'var(--text-muted)'

  const resolve = async () => {
    if (!actionTaken.trim() || !resolutionText.trim()) return
    setBusy(true)
    try {
      await phaseCApi.resolveBlocker(changeId, partnerId, blocker.id, {
        action_taken:    actionTaken,
        resolution_text: resolutionText,
        artifact_ref:    artifactRef.trim() || null,
      })
      onResolved()
    } finally { setBusy(false) }
  }

  return (
    <div style={{
      padding: '10px 12px', marginBottom: 8,
      borderRadius: 8, background: `${sevColor}10`,
      border: `1px solid ${sevColor}40`,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
        <span style={{ fontSize: 10, fontWeight: 700, padding: '2px 7px', borderRadius: 4, background: sevColor, color: 'white', textTransform: 'uppercase', letterSpacing: '0.5px' }}>
          Blocker · {blocker.severity}
        </span>
        <span style={{ fontSize: 10, color: 'var(--text-muted)', fontFamily: 'monospace' }}>{blocker.blocker_id}</span>
      </div>
      <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-primary)', marginBottom: 4 }}>
        {blocker.description}
      </div>
      {blocker.impact && (
        <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginBottom: 4 }}>
          <strong>Impact:</strong> {blocker.impact}
        </div>
      )}
      {blocker.options_considered?.length > 0 && (
        <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginBottom: 6 }}>
          <strong>Options:</strong>
          <ul style={{ margin: '2px 0 0 0', paddingLeft: 18 }}>
            {blocker.options_considered.map((o, i) => (
              <li key={i}>
                <span style={{ fontWeight: 500 }}>{o.option}</span>
                {o.eta && <span style={{ color: 'var(--text-muted)' }}> · ETA {o.eta}</span>}
                {o.impact && <span style={{ color: 'var(--text-muted)' }}> · {o.impact}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}

      {!showResolveForm ? (
        <button onClick={() => setShowResolveForm(true)} style={{
          display: 'flex', alignItems: 'center', gap: 4, padding: '6px 12px',
          background: sevColor, color: 'white', border: 'none', borderRadius: 5,
          fontSize: 11, fontWeight: 600, cursor: 'pointer',
        }}>
          <Shield size={11} /> Resolve
        </button>
      ) : (
        <div>
          <input
            type="text"
            value={actionTaken}
            onChange={e => setActionTaken(e.target.value)}
            placeholder="Action taken (e.g. ISSUE_PATCH_SDK, EXTEND_TIMELINE_4W)"
            style={{
              width: '100%', padding: '6px 8px', borderRadius: 5,
              border: '1px solid var(--border)', background: 'var(--bg-input)',
              color: 'var(--text-primary)', fontSize: 11, fontFamily: 'inherit', marginBottom: 4,
            }}
          />
          <textarea
            value={resolutionText}
            onChange={e => setResolutionText(e.target.value)}
            placeholder="Explanation for the partner (sent as BLOCKER_RESOLUTION)…"
            rows={2}
            style={{
              width: '100%', padding: '6px 8px', borderRadius: 5,
              border: '1px solid var(--border)', background: 'var(--bg-input)',
              color: 'var(--text-primary)', fontSize: 11, fontFamily: 'inherit', marginBottom: 4,
            }}
          />
          <input
            type="text"
            value={artifactRef}
            onChange={e => setArtifactRef(e.target.value)}
            placeholder="Optional artifact reference (e.g. patched SDK URL)"
            style={{
              width: '100%', padding: '6px 8px', borderRadius: 5,
              border: '1px solid var(--border)', background: 'var(--bg-input)',
              color: 'var(--text-primary)', fontSize: 11, fontFamily: 'inherit', marginBottom: 6,
            }}
          />
          <div style={{ display: 'flex', gap: 6 }}>
            <button onClick={resolve} disabled={busy || !actionTaken.trim() || !resolutionText.trim()} style={{
              padding: '6px 12px', background: 'var(--success)', color: 'white',
              border: 'none', borderRadius: 5, fontSize: 11, fontWeight: 600,
              cursor: (busy || !actionTaken.trim() || !resolutionText.trim()) ? 'not-allowed' : 'pointer',
              opacity: (busy || !actionTaken.trim() || !resolutionText.trim()) ? 0.6 : 1,
            }}>Send resolution</button>
            <button onClick={() => setShowResolveForm(false)} style={{
              padding: '6px 12px', background: 'transparent', color: 'var(--text-muted)',
              border: '1px solid var(--border)', borderRadius: 5, fontSize: 11, cursor: 'pointer',
            }}>Cancel</button>
          </div>
        </div>
      )}
    </div>
  )
}


// (Removed: `npciTabOf`, a three-way message classifier that was never called.)
//
// It routed on role — 'counter_proposal' → negotiation, 'blocker' → blocker, else general
// — which looks right but does not hold: inbound partner queries carry no
// `counter_proposal_id`, so the API cannot synthesize 'counter_proposal' for them. Only
// the Authority's decision gets the link, so classifying on it filed a partner's counter-proposal
// and its own rejection into different tabs.
//
// Channels are now split by SERVER-SIDE thread kind ('general' vs 'blocker'), which is
// authoritative. Reinstate a finer split only once inbound queries are back-linked to the
// CounterProposal that `ensure_cp_for_query` mints from them.

// Normalize message text for the pending-vs-committed dedup compare.
// Differences we have to absorb: extra whitespace, line-ending variants,
// case, surrounding markdown punctuation. Cheap O(n) — only called on
// pendingQuery and the partner-side message rows when both exist.
const normText = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase()


// ── Resolver recommendation card ──────────────────────────────────────────
// Renders above the composer when the feasibility resolver has produced a
// recommendation for the partner currently in view. Visual hierarchy:
//   - Header strip: action chip · confidence pill · model + timestamp meta
//   - Summary line under the header (one-sentence reason)
//   - Drafted response panel with Copy + Apply buttons inline at the top
//   - Footer rows: collapsible reasoning + citation chips
// Per-recommendation dismissal — keyed on the resolver_recommendation row id
// so a re-run (which generates a new row id) shows a fresh card.
const _RESOLVER_DISMISS_KEY = 'pp_resolver_dismissed_v1'
function _readDismissedSet() {
  try {
    const raw = localStorage.getItem(_RESOLVER_DISMISS_KEY)
    return new Set(raw ? JSON.parse(raw) : [])
  } catch { return new Set() }
}
function _markDismissed(id) {
  if (!id) return
  const s = _readDismissedSet()
  s.add(id)
  try { localStorage.setItem(_RESOLVER_DISMISS_KEY, JSON.stringify([...s])) }
  catch { /* quota / disabled — silent */ }
}


const TEAM_LABELS = { risk: 'Risk', infosec: 'InfoSec', tech: 'Tech' }

function ResolverCard({ rec, ac, cf, meta, escalation, onApply, onSend }) {
  const [reasoningOpen, setReasoningOpen] = useState(false)
  const [applied, setApplied] = useState(false)
  const [sending, setSending] = useState(false)
  // Escalations don't carry a usable partner-facing draft (the resolver only
  // writes a holding message). The PM responds after the team's review, so we
  // suppress the draft panel + send/apply actions on an escalation card.
  const isEscalation = rec.recommended_action === 'escalate'
  // Option B: after Apply or Copy the card collapses to a slim banner so
  // it doesn't duplicate the draft text that's now in the composer (or
  // clipboard). Click the banner to re-expand for reasoning / citations.
  // Values: null (expanded) | 'applied' | 'copied'.
  const [compactState, setCompactState] = useState(null)
  // Hard-dismissed by the PM (X button) — persisted in localStorage so a
  // refresh doesn't surface the same recommendation again. Re-runs produce
  // a new row id, so a fresh card appears for new partner messages.
  const [dismissed, setDismissed] = useState(() => _readDismissedSet().has(meta?.id))

  const handleDismiss = () => {
    _markDismissed(meta?.id)
    setDismissed(true)
  }

  if (dismissed) return null


  const handleSend = async () => {
    if (!onSend || sending) return
    setSending(true)
    try {
      await onSend()
      setCompactState('sent')
    } catch { /* parent handles errors */ }
    finally { setSending(false) }
  }

  const handleApply = () => {
    onApply()
    setApplied(true)
    setTimeout(() => setApplied(false), 1800)
    setCompactState('applied')
    // Scroll to the composer so the partner sees the textarea was filled.
    // Falls back gracefully if the composer isn't on the page (defensive).
    setTimeout(() => {
      const ta = document.querySelector('textarea[placeholder*="message" i], textarea[placeholder*="reply" i]')
      if (ta) {
        ta.scrollIntoView({ behavior: 'smooth', block: 'center' })
        ta.focus()
      }
    }, 50)
  }

  // Disappear entirely once the PM has sent a response via this card.
  if (compactState === 'sent') return null

  // Auto-rejected BRD violation — informational card only, no draft / actions.
  if (rec.recommended_action === 'auto_rejected_brd') {
    return (
      <div style={{
        margin: '8px 0 4px',
        background: '#fdecea',
        border: '1px solid #fca5a5',
        borderLeft: '3px solid #991b1b',
        borderRadius: 10,
        overflow: 'hidden',
      }}>
        <div style={{
          padding: '10px 14px',
          background: 'linear-gradient(180deg, rgba(254,226,226,0.6) 0%, #fdecea 100%)',
          borderBottom: '1px solid #fca5a5',
          display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap',
        }}>
          <AlertTriangle size={14} color="#991b1b" style={{ flexShrink: 0 }} />
          <span style={{
            fontSize: 10.5, fontWeight: 700, padding: '2px 9px', borderRadius: 999,
            background: '#fff', color: '#991b1b', border: '1px solid #fca5a5',
            textTransform: 'uppercase', letterSpacing: 0.5,
          }}>Auto-Rejected · BRD Violation</span>
          <button
            onClick={handleDismiss}
            title="Dismiss"
            style={{
              marginLeft: 'auto', display: 'inline-flex', alignItems: 'center',
              justifyContent: 'center', width: 24, height: 24, padding: 0,
              background: 'transparent', border: 'none', cursor: 'pointer',
              color: '#991b1b', borderRadius: 4,
            }}
          ><X size={14} /></button>
        </div>
        <div style={{ padding: '12px 14px' }}>
          <div style={{ fontSize: 12.5, color: '#7f1d1d', marginBottom: 10, lineHeight: 1.5 }}>
            {rec.action_summary || 'This query was auto-rejected because it contradicts a mandatory BRD requirement.'}
          </div>
          {rec.violated_requirement && (
            <div style={{
              background: '#fff', border: '1px solid #fca5a5', borderRadius: 7,
              padding: '8px 12px', marginBottom: 8,
            }}>
              <div style={{
                fontSize: 10, fontWeight: 700, color: '#991b1b',
                textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 4,
              }}>Requirement violated</div>
              <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-primary)' }}>
                {rec.violated_requirement}
              </div>
              {rec.violated_reason && (
                <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 4, lineHeight: 1.5 }}>
                  {rec.violated_reason}
                </div>
              )}
            </div>
          )}
          <div style={{ fontSize: 11.5, color: '#991b1b', fontStyle: 'italic' }}>
            No LLM analysis was run. The partner has been automatically notified.
          </div>
        </div>
      </div>
    )
  }

  // Compact banner shown after Apply / Copy. Single row, click to expand.
  if (compactState) {
    const policyCount = rec.cited_policy_sections?.length || 0
    const docCount    = rec.cited_change_docs?.length || 0
    const stateLabel = compactState === 'applied'
      ? 'Applied to composer'
      : compactState === 'sent'
      ? 'Sent to partner'
      : 'Copied to clipboard'
    const stateColor = '#10b981'
    return (
      <div style={{
        width: '100%', margin: '8px 0 4px',
        background: 'var(--bg-elevated)',
        border: '1px solid var(--border)',
        borderLeft: `3px solid ${ac.fg}`,
        borderRadius: 8,
        display: 'flex', alignItems: 'center',
      }}>
        <button
          onClick={() => setCompactState(null)}
          title="Click to re-expand the resolver card"
          style={{
            flex: 1, padding: '8px 14px', textAlign: 'left',
            background: 'transparent', border: 'none', cursor: 'pointer',
            display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap',
          }}
        >
          <Sparkles size={13} color={ac.fg} style={{ flexShrink: 0 }} />
          <span style={{
            fontSize: 10.5, fontWeight: 700, padding: '2px 8px',
            borderRadius: 999, background: '#fff', color: ac.fg,
            border: `1px solid ${ac.border}`,
            textTransform: 'uppercase', letterSpacing: 0.5,
          }}>{ac.label}</span>
          <span style={{
            fontSize: 11, fontWeight: 700, color: stateColor,
            display: 'inline-flex', alignItems: 'center', gap: 4,
          }}>
            <Check size={11} /> {stateLabel}
          </span>
          {(rec.reasoning?.length > 0 || policyCount + docCount > 0) && (
            <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
              ·
              {rec.reasoning?.length > 0 && ` ${rec.reasoning.length} reasoning ${rec.reasoning.length === 1 ? 'point' : 'points'}`}
              {(policyCount + docCount) > 0 && ` · ${policyCount + docCount} citations`}
            </span>
          )}
          <span style={{ marginLeft: 'auto', fontSize: 10.5, color: 'var(--text-muted)' }}>
            click to expand
          </span>
        </button>
        <button
          onClick={handleDismiss}
          title="Dismiss the resolver suggestion"
          style={{
            display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
            width: 32, height: 32, margin: '0 6px 0 4px', padding: 0,
            background: 'transparent', border: 'none', cursor: 'pointer',
            color: 'var(--text-muted)', borderRadius: 4,
          }}
        >
          <X size={14} />
        </button>
      </div>
    )
  }

  const policyChips = Array.isArray(rec.cited_policy_sections) ? rec.cited_policy_sections : []
  const docChips    = Array.isArray(rec.cited_change_docs)     ? rec.cited_change_docs     : []
  const hasCitations = policyChips.length > 0 || docChips.length > 0

  return (
    <div style={{
      margin: '8px 0 4px',
      background: 'var(--bg-elevated)',
      border: '1px solid var(--border)',
      borderLeft: `3px solid ${ac.fg}`,
      borderRadius: 10,
      overflow: 'hidden',
    }}>
      {/* Header strip */}
      <div style={{
        padding: '10px 14px',
        background: `linear-gradient(180deg, ${ac.bg} 0%, var(--bg-elevated) 100%)`,
        borderBottom: '1px solid var(--border-subtle)',
        display: 'flex', flexDirection: 'column', gap: 6,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          <Sparkles size={14} color={ac.fg} style={{ flexShrink: 0 }} />
          <span style={{
            fontSize: 10.5, fontWeight: 700, padding: '2px 9px',
            borderRadius: 999,
            background: '#fff', color: ac.fg,
            border: `1px solid ${ac.border}`,
            textTransform: 'uppercase', letterSpacing: 0.5,
          }}>{ac.label}</span>
          <span style={{
            fontSize: 10.5, fontWeight: 700, padding: '2px 9px',
            borderRadius: 999,
            background: cf.bg, color: cf.fg, border: `1px solid ${cf.border}`,
            textTransform: 'uppercase', letterSpacing: 0.5,
          }}>{rec.confidence} confidence</span>
          {(() => {
            // Escalation status chip. Once a ticket exists we drive the chip
            // off its state (awaiting review → review received); before the
            // ticket lands we fall back to the resolver's escalation_target.
            const team = escalation?.team || rec.escalation_target
            if (!team) return null
            const teamLabel = TEAM_LABELS[team] || team
            const reviewed = escalation && (escalation.status === 'responded' || escalation.status === 'closed')
            const style = reviewed
              ? { background: '#d1fae5', color: '#065f46', border: '1px solid #6ee7b7' }
              : { background: '#fee2e2', color: '#991b1b', border: '1px solid #fca5a5' }
            const text = reviewed
              ? `✓ ${teamLabel} review received`
              : escalation
                ? `Escalated to ${teamLabel} · awaiting review`
                : `Escalated to ${teamLabel}`
            return (
              <span style={{
                fontSize: 10.5, fontWeight: 700, padding: '2px 9px',
                borderRadius: 999, ...style,
                textTransform: 'uppercase', letterSpacing: 0.5,
              }}>{text}</span>
            )
          })()}
          <span style={{
            marginLeft: 'auto', fontSize: 10.5, color: 'var(--text-muted)',
            display: 'inline-flex', alignItems: 'center', gap: 4,
          }}>
            <Clock size={11} />
            {new Date(meta.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
            {meta.model_used && <span>· {meta.model_used.split(':')[1] || meta.model_used}</span>}
          </span>
          <button
            onClick={handleDismiss}
            title="Dismiss the resolver suggestion — won't reappear on refresh"
            style={{
              display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
              width: 24, height: 24, padding: 0,
              background: 'transparent', border: 'none', cursor: 'pointer',
              color: 'var(--text-muted)', borderRadius: 4,
            }}
          >
            <X size={14} />
          </button>
        </div>
        {rec.action_summary && (
          <div style={{
            fontSize: 12.5, color: 'var(--text-secondary)',
            lineHeight: 1.5, paddingLeft: 22,
          }}>
            {rec.action_summary}
          </div>
        )}
      </div>

      {/* Team review comments — shown once the escalated team responds, so the
          PM sees the team's position (and that the draft below now reflects it)
          instead of a bare "Escalated" with no resolution context. */}
      {escalation && (escalation.status === 'responded' || escalation.status === 'closed') && escalation.team_response_text && (
        <div style={{
          background: '#ecfdf5', borderBottom: '1px solid var(--border-subtle)',
          padding: '8px 14px 10px',
        }}>
          <div style={{
            fontSize: 10.5, fontWeight: 700, color: '#065f46',
            textTransform: 'uppercase', letterSpacing: 0.5,
            display: 'inline-flex', alignItems: 'center', gap: 5, marginBottom: 4,
          }}>
            <Check size={12} /> {TEAM_LABELS[escalation.team] || escalation.team} review received
          </div>
          <div style={{ fontSize: 12, color: 'var(--text-secondary)', lineHeight: 1.55, whiteSpace: 'pre-wrap' }}>
            {escalation.team_response_text}
          </div>
        </div>
      )}

      {/* Reasoning + citations — placed ABOVE the draft so PMs see WHY
          before reading HOW. Reasoning is the auditable thinking the PM
          needs to verify the recommendation; the draft is the action they
          take after agreeing with the reasoning. */}
      {(rec.reasoning?.length > 0 || hasCitations) && (
        <div style={{
          background: 'var(--bg-soft)',
          padding: '8px 14px 10px',
          borderBottom: '1px solid var(--border-subtle)',
          display: 'flex', flexDirection: 'column', gap: 8,
        }}>
          {rec.reasoning?.length > 0 && (
            <>
              <button
                onClick={() => setReasoningOpen(o => !o)}
                style={{
                  display: 'inline-flex', alignItems: 'center', gap: 5,
                  padding: '4px 0', margin: 0,
                  background: 'transparent', border: 'none', cursor: 'pointer',
                  fontSize: 11, fontWeight: 700, color: 'var(--text-secondary)',
                  textTransform: 'uppercase', letterSpacing: 0.5,
                  alignSelf: 'flex-start',
                }}
              >
                {reasoningOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                Reasoning · {rec.reasoning.length} point{rec.reasoning.length === 1 ? '' : 's'}
              </button>
              {reasoningOpen && (
                <ul style={{
                  margin: 0, paddingLeft: 22,
                  fontSize: 12, color: 'var(--text-secondary)', lineHeight: 1.55,
                }}>
                  {rec.reasoning.map((r, i) => (
                    <li key={i} style={{ marginBottom: 4 }}>{r}</li>
                  ))}
                </ul>
              )}
            </>
          )}
          {hasCitations && (
            <div style={{
              display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap',
            }}>
              <span style={{
                fontSize: 10.5, fontWeight: 700, color: 'var(--text-muted)',
                textTransform: 'uppercase', letterSpacing: 0.5,
                display: 'inline-flex', alignItems: 'center', gap: 4,
              }}>
                <Tag size={10} /> Cited
              </span>
              {policyChips.map((p, i) => (
                <span key={`pol-${i}`} style={{
                  fontSize: 10.5, fontWeight: 600,
                  padding: '2px 7px', borderRadius: 999,
                  background: 'rgba(37,99,235,0.08)', color: '#2563eb',
                  border: '1px solid rgba(37,99,235,0.20)',
                }}>policy {p}</span>
              ))}
              {docChips.map((d, i) => (
                <span key={`doc-${i}`} style={{
                  fontSize: 10.5, fontWeight: 600,
                  padding: '2px 7px', borderRadius: 999,
                  background: 'rgba(15,23,42,0.05)', color: 'var(--text-secondary)',
                  border: '1px solid var(--border-subtle)',
                  display: 'inline-flex', alignItems: 'center', gap: 4,
                }}>
                  <FileText size={9} /> {d}
                </span>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Drafted response. Hidden for escalations — the PM responds after the
          relevant team's review, not from the resolver's holding draft. */}
      {isEscalation ? (
        <div style={{ padding: '12px 14px', fontSize: 12, color: 'var(--text-muted)', lineHeight: 1.5 }}>
          {escalation && (escalation.status === 'responded' || escalation.status === 'closed')
            ? 'Review received — respond to the partner using the team\'s assessment above.'
            : 'Awaiting the review team\'s assessment. You\'ll respond to the partner once it comes back.'}
        </div>
      ) : (
        <div style={{ padding: '12px 14px' }}>
          <div style={{
            display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8,
          }}>
            <span style={{
              fontSize: 10, fontWeight: 700, color: 'var(--text-muted)',
              textTransform: 'uppercase', letterSpacing: 0.5,
            }}>Drafted response</span>
            <div style={{ flex: 1 }} />
            <button
              onClick={handleApply}
              title="Pre-fill the composer below with this draft (you can edit before sending)"
              style={{
                display: 'inline-flex', alignItems: 'center', gap: 5,
                padding: '5px 12px', fontSize: 11.5, fontWeight: 700,
                background: applied ? '#10b981' : 'var(--bg-elevated)',
                color: applied ? '#fff' : ac.fg,
                border: `1px solid ${applied ? '#10b981' : ac.border}`,
                borderRadius: 6, cursor: 'pointer', transition: 'background 0.2s',
              }}
            >
              <Check size={11} /> {applied ? 'Applied ✓' : 'Use this draft'}
            </button>
            {onSend && (
              <button
                onClick={handleSend}
                disabled={sending}
                title="Send this draft to the partner as-is, without editing"
                style={{
                  display: 'inline-flex', alignItems: 'center', gap: 5,
                  padding: '5px 12px', fontSize: 11.5, fontWeight: 700,
                  background: ac.fg, color: '#fff',
                  border: 'none', borderRadius: 6,
                  cursor: sending ? 'not-allowed' : 'pointer', opacity: sending ? 0.7 : 1,
                  transition: 'background 0.2s',
                }}
              >
                {sending ? <Loader size={11} style={{ animation: 'spin 1s linear infinite' }} /> : <Send size={11} />}
                {sending ? 'Sending…' : 'Send as-is'}
              </button>
            )}
          </div>
          <div style={{
            padding: '11px 13px', borderRadius: 7,
            background: 'var(--bg-soft)',
            border: '1px solid var(--border-subtle)',
            fontSize: 12.5, color: 'var(--text-primary)',
            lineHeight: 1.6, whiteSpace: 'pre-wrap', wordBreak: 'break-word',
          }}>
            {rec.draft_response}
          </div>
        </div>
      )}
    </div>
  )
}


function NegotiationPanel({ changeId, partnerId, partnerName, pendingMessages }) {
  const qc = useQueryClient()
  const [responding, setResponding] = useState(false)
  const [composer, setComposer] = useState('')
  const [sendError, setSendError] = useState(null)
  const [draftLoaded, setDraftLoaded] = useState(false)
  // activeTab: which of the 3 chat splits the operator is viewing.
  // The composer + AI-draft surfaces are tab-aware; the open counter /
  // blocker action cards stay always-visible above the stream because
  // they're action items requiring attention regardless of tab.
  const [activeTab, setActiveTab] = useState('negotiation')
  // Which partner message the PM is replying to. When set, the reply settles ONLY that
  // counter-proposal instead of sweeping every open one from the partner.
  const [replyTo, setReplyTo] = useState(null)   // { cpId, label } | null
  const scrollRef = useRef(null)

  // Feasibility resolver recommendation for this partner. The backend fires
  // `auto_resolve_background` on every inbound QUERY / COUNTER_PROPOSAL,
  // each producing a new resolver_recommendations row. The /latest endpoint
  // returns the most recent one for this (change, partner). We poll
  // continuously so subsequent partner messages surface their own fresh
  // recommendation card (was: stopping once data landed, which froze the
  // card on the first query's recommendation forever).
  //   3s while empty (waiting on the LLM call to complete, ~20-30s typical)
  //   8s while populated (just checking for newer recommendations)
  const { data: resolverRecs } = useQuery({
    queryKey: ['resolver-recs', changeId, partnerId],
    queryFn: () => resolverApi.list(changeId, partnerId),
    refetchInterval: (q) => (q.state.data?.length ? 8000 : 3000),
    enabled: Boolean(changeId && partnerId),
  })

  // Escalation tickets for this change (Risk/InfoSec/Tech). Lets the resolver
  // card show whether the query is awaiting a team's review or whether the
  // team's review comments have come back — so the PM isn't left staring at a
  // bare "Escalate" with no sense of where it stands.
  const { data: escalations } = useQuery({
    queryKey: ['phasec-escalations', changeId],
    queryFn: () => escalationsApi.list({ change_id: changeId }).catch(() => []),
    refetchInterval: 8000,
    enabled: Boolean(changeId),
  })
  // Counters + blockers for this partner — drives the structured action
  // cards above the message stream. Refetched after PM action.
  const { refetch: _refetchCounters } = useQuery({
    queryKey: ['counters', changeId, partnerId],
    queryFn: () => phaseCApi.listCounters(changeId, partnerId).then(r => r.data),
    refetchInterval: 6000,
    refetchOnWindowFocus: true,
  })
  const { data: blockersData, refetch: refetchBlockers } = useQuery({
    queryKey: ['blockers', changeId, partnerId],
    queryFn: () => phaseCApi.listBlockers(changeId, partnerId).then(r => r.data),
    refetchInterval: 6000,
    refetchOnWindowFocus: true,
  })
  const openBlockers = (blockersData || []).filter(b => b.status === 'open')

  // Find pending query for this partner from A2A messages. The audit
  // row's payload is the full A2A wrapper post-Slice-8 — the actual
  // message lives nested under `payload.payload`. Read both shapes
  // for back-compat with pre-Slice-8 rows.
  const pendingQuery = (pendingMessages || []).find(
    m => m.task_type === 'query' && m.status === 'submitted' && m.direction === 'inbound'
      && m.partner_name === partnerName,
  )
  const pendingPayload = pendingQuery
    ? (pendingQuery.payload?.payload || pendingQuery.payload || {})
    : null
  const pendingText = pendingPayload?.message || pendingPayload?.justification || ''
  const pendingKind = pendingPayload?.message_kind  // e.g. 'COUNTER_PROPOSAL'

  // True while the resolver is working on a NEWER query than the latest
  // recommendation we hold — i.e. a fresh partner message arrived but its
  // recommendation hasn't landed yet. Drives the "analysing…" banner AND
  // hides the previous (stale) resolver card so the PM isn't shown an old
  // suggestion while a new one is being generated.
  const _pendingMs = pendingQuery?.created_at ? new Date(pendingQuery.created_at).getTime() : 0

  const { data: thread, refetch } = useQuery({
    queryKey: ['negotiation', changeId, partnerId],
    queryFn: () => phaseCApi.getNegotiation(changeId, partnerId).then(r => r.data),
    // 2 s — was 5 s. The message stream is the primary latency complaint;
    // the heavier ['phase-c-progress-grid'] and ['phase-c-partners']
    // queries stay at their original intervals because they aren't on
    // the message-bubble path.
    refetchInterval: 2000,
    refetchOnWindowFocus: true,
  })

  // Round lifecycle for THIS partner. Rendered inline as centered "Round N
  // opened / closed (reason)" system notes so silent-close and PM force-close
  // are visible right in the conversation the PM is reading — otherwise the
  // round transitions only surface in the Hub's rounds section, easy to miss.
  const { data: partnerRounds } = useQuery({
    queryKey: ['negotiation-rounds', changeId],
    queryFn: () => phaseCApi.listRounds(changeId).then(r => r.data),
    refetchInterval: 8000,
    enabled: Boolean(changeId && partnerId),
  })

  const msgs = thread?.messages || []
  const visibleMsgs = msgs.filter(m => m.role !== 'ai_draft')

  // Pair each AI draft with the query it answers — the partner's query, the draft
  // written for it and the PO reply that settles it all carry that query's
  // correlation_id. This used to be "the last message, if it happens to be a
  // draft", so with several queries in flight every draft but the newest was
  // generated, stored and then never shown (and none at all once any other
  // message landed after them).
  const draftsByQuery = new Map()
  for (const m of msgs) {
    if (m.role === 'ai_draft' && m.correlation_id) draftsByQuery.set(m.correlation_id, m)
  }
  const answeredQueries = new Set(
    msgs.filter(m => (m.role === 'po_approved' || m.role === 'npci') && m.correlation_id)
        .map(m => m.correlation_id)
  )
  // Oldest first: msgs arrive in created_at order and the PM works the queue in order.
  const openQueries = msgs.filter(
    m => m.role === 'partner' && m.correlation_id && !answeredQueries.has(m.correlation_id)
  )
  // Threads written before drafts carried a correlation_id keep the old behaviour.
  const legacyTail = msgs.length > 0 ? msgs[msgs.length - 1] : null
  const legacyDraft = legacyTail?.role === 'ai_draft' && !legacyTail.correlation_id ? legacyTail : null

  // What the composer is working on: the query the PM explicitly replied to, else the
  // OLDEST outstanding one — newest-first is exactly what mis-stamped the outbound id.
  const selectedQueryId = replyTo?.correlationId || null
  const latestDraft =
    (selectedQueryId && draftsByQuery.get(selectedQueryId))
    || (openQueries.length ? draftsByQuery.get(openQueries[0].correlation_id) : null)
    || legacyDraft
    || null
  const hasPendingDraft = Boolean(latestDraft)

  // Which query's resolver suggestion to show. Recommendations come newest-first
  // and a query can carry several versions, so the first hit per query is current.
  const recsByQuery = new Map()
  for (const r of (resolverRecs || [])) {
    if (r.correlation_id && !recsByQuery.has(r.correlation_id)) recsByQuery.set(r.correlation_id, r)
  }
  // Follow the query being answered, else the oldest still open. The endpoint used
  // to return only the newest recommendation for the whole (change, partner), so
  // earlier queries' suggestions were generated and then unreachable.
  const resolverRec =
    (selectedQueryId && recsByQuery.get(selectedQueryId))
    || (openQueries.length ? recsByQuery.get(openQueries[0].correlation_id) : null)
    || (resolverRecs || [])[0]
    || null
  const escForRec = (escalations || []).find(
    e => e.a2a_message_id && e.a2a_message_id === resolverRec?.a2a_message_id,
  )
  const _recMs = resolverRec?.created_at ? new Date(resolverRec.created_at).getTime() : 0
  const resolverProcessing = Boolean(pendingQuery) && (!resolverRec || (Number.isFinite(_pendingMs) && _pendingMs > _recMs + 2000))

  // The query this reply will answer: the explicit pick, else the oldest still
  // open. Typing straight into the box used to send no target at all, and the
  // backend fell back to newest-wins — which is how an "ok" meant for the first
  // query was recorded against the third.
  const targetQuery =
    (selectedQueryId && openQueries.find(q => q.correlation_id === selectedQueryId))
    || openQueries[0]
    || null
  const targetCorrelationId = targetQuery?.correlation_id || latestDraft?.correlation_id || null

  // Blockers live in a SEPARATE thread (kind='blocker'), so they are NOT in `thread`
  // above. An operational escalation ("we're blocked in production") is a different
  // conversation from a spec negotiation — different urgency, different owner. Merging
  // them into one stream buried the blocker among counter-proposals.
  const { data: blockerThread } = useQuery({
    queryKey: ['blocker-thread', changeId, partnerId],
    queryFn: () => phaseCApi.getBlockerThread(changeId, partnerId).then(r => r.data),
    enabled: Boolean(changeId && partnerId),
    refetchInterval: 5000,
  })
  const blockerMsgs = (blockerThread?.messages || []).filter(m => m.role !== 'ai_draft')

  // Synthesise system-note timeline entries for THIS partner's round events
  // — round opened (started_at) and round closed (closed_at + status label).
  // Not persisted anywhere; derived on every render from the polled rounds
  // query so the PM sees "Round 1 opened … Round 1 closed (silent acceptance)
  // … Round 2 opened" inline with the message stream. Negotiation tab only —
  // rounds are a negotiation concept, not a blocker one.
  const roundSystemEntries = (partnerRounds || [])
    .filter(r => r.partner_id === partnerId)
    .flatMap(r => {
      const entries = []
      if (r.started_at) {
        entries.push({
          id: `round-open-${r.id}`,
          kind: 'system',
          created_at: r.started_at,
          content: `Round ${r.round_number} opened — deadline ${
            r.deadline_at ? new Date(r.deadline_at).toLocaleString() : '—'
          }`,
        })
      }
      if (r.closed_at && r.status !== 'open') {
        const reason = ({
          silently_accepted: 'silent acceptance · 24h window elapsed',
          closed_by_pm:      'PM closed the round',
          responded:         'partner responded',
        })[r.status] || r.status.replace(/_/g, ' ')
        entries.push({
          id: `round-close-${r.id}`,
          kind: 'system',
          created_at: r.closed_at,
          content: `Round ${r.round_number} closed — ${reason}`,
        })
      }
      return entries
    })

  // TWO channels, matching how the data is actually partitioned on the server:
  //   Negotiation — the whole `general` thread (queries, counter-proposals, decisions)
  //   Blockers    — the `blocker` thread (operational escalations)
  //
  // Deliberately NOT split further into Conversation vs Negotiation. Inbound queries are
  // written without a `counter_proposal_id` (the CP that `ensure_cp_for_query` mints from
  // them is never back-linked), so the API cannot mark a partner's counter-proposal as
  // such — only the Authority's decision carries the link. Splitting on that put a proposal in one
  // tab and its own rejection in another, which is worse than not splitting at all. Keep
  // the negotiation conversation whole until the back-link exists.
  const negotiationWithRounds = [...visibleMsgs, ...roundSystemEntries].sort(
    (a, b) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime(),
  )
  const TAB_MSGS = {
    negotiation: negotiationWithRounds,
    blocker:     blockerMsgs,
  }
  const tabMsgs = TAB_MSGS[activeTab] || TAB_MSGS.negotiation
  // Hide the resolver card once the PM has sent an actual text reply (role
  // 'npci') after the recommendation was generated. Using _recMs as reference
  // rather than _pendingMs avoids false-positives from auto-generated messages
  // (counter_decision, counter_resolution) that share the same timestamp window.
  const pmRepliedAfterQuery = _recMs > 0 && visibleMsgs.some(m =>
    (m.role === 'npci' || m.role === 'po_approved') && m.created_at && new Date(m.created_at).getTime() > _recMs
  )


  // Track which AI draft we've already responded to / dismissed. Without
  // this, the auto-load effect below repopulates the composer with the
  // same draft on the polling tick that runs right after Send — before
  // the new PO_APPROVED message has been refetched. Result: operator's
  // edited reply gets clobbered with the original AI draft text.
  const [addressedDraftId, setAddressedDraftId] = useState(null)

  // Send a reply to the partner. `rawText` lets the resolver card's "Send"
  // button dispatch the AI draft directly without routing through the composer;
  // handleRespond just feeds it the composer contents.
  const sendReply = async (rawText) => {
    const text = (rawText || '').trim()
    if (!text) return
    setResponding(true)
    setSendError(null)
    try {
      const res = await phaseCApi.respondToQuery(changeId, partnerId, text, {
        counterProposalId: replyTo?.cpId || null,
        blockerId: replyTo?.blockerId || null,
        // Answer THIS query, not whichever arrived last. The backend echoes this id
        // back as query_id and it decides which query the partner marks answered.
        queryCorrelationId: targetCorrelationId,
        kind: activeTab === 'blocker' ? 'blocker' : null,
      })
      if (res?.data?.thread) {
        qc.setQueryData(['negotiation', changeId, partnerId], res.data.thread)
      }
      setComposer('')
      setReplyTo(null)   // clear the per-message reply target after a successful send
      if (latestDraft) setAddressedDraftId(latestDraft.id)
      setDraftLoaded(false)
      refetch()
      qc.invalidateQueries({ queryKey: ['phase-c-messages', changeId] })
    } catch (e) {
      // Rethrow so the resolver card's "Send as-is" doesn't flip to "Sent to
      // partner" on a failed POST (review finding Q-1 — silent data loss).
      setSendError('Failed to send to the partner — the message was NOT delivered. Please try again.')
      throw e
    }
    finally { setResponding(false) }
  }

  const handleRespond = async () => {
    const text = composer.trim()
    if (!text) return
    setResponding(true)
    setSendError(null)
    try {
      const res = await phaseCApi.respondToQuery(changeId, partnerId, text, {
        counterProposalId: replyTo?.cpId || null,
        blockerId: replyTo?.blockerId || null,
        // Answer THIS query, not whichever arrived last. The backend echoes this id
        // back as query_id and it decides which query the partner marks answered.
        queryCorrelationId: targetCorrelationId,
        kind: activeTab === 'blocker' ? 'blocker' : null,
      })
      // Hydrate the cache directly from the response so the new bubble
      // appears in-place without waiting for the next poll tick. The
      // backend now returns the freshly-built thread on the same shape
      // as GET /negotiation.
      if (res?.data?.thread) {
        qc.setQueryData(['negotiation', changeId, partnerId], res.data.thread)
      }
      setComposer('')
      setReplyTo(null)
      // Pin the draft id we just answered so the auto-load below
      // doesn't recycle it back into the composer on the next render.
      if (latestDraft) setAddressedDraftId(latestDraft.id)
      setDraftLoaded(false)
      refetch()
      qc.invalidateQueries({ queryKey: ['phase-c-messages', changeId] })
    } catch {
      setSendError('Failed to send to the partner — the message was NOT delivered. Please try again.')
    }
    finally { setResponding(false) }
  }

  // Auto-load AI draft into composer when one becomes available — but
  // never re-load a draft we already responded to (tracked by id).
  // Gated on the General tab so a draft for a free-text query doesn't
  // clobber whatever the operator is typing on Negotiation / Blocker.
  useEffect(() => {
    if (
      activeTab === 'negotiation'
      && hasPendingDraft && !draftLoaded && latestDraft
      && latestDraft.id !== addressedDraftId
    ) {
      setComposer(latestDraft.content || '')
      setDraftLoaded(true)
    }
  }, [activeTab, hasPendingDraft, draftLoaded, latestDraft, addressedDraftId])

  // Reset addressed-draft pin whenever the active partner thread
  // changes — prevents stale state from suppressing a fresh draft
  // when the operator switches to a different partner.
  useEffect(() => {
    setAddressedDraftId(null)
    setComposer('')
  }, [partnerId, changeId])

  // Auto-scroll to bottom when messages or pending query change
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
    }
  }, [visibleMsgs.length, pendingQuery?.id])


  // ── Bubble component (used for both partner + the Authority sides) ────────────
  // The AI suggestion for ONE partner query, rendered directly beneath it so every
  // outstanding query shows its own draft and the PM can pick which to answer.
  const QueryDraftCard = ({ draft, active, onUse }) => (
    <div style={{
      marginLeft: 36, marginBottom: '12px', padding: '8px 12px', borderRadius: 8,
      background: 'rgba(218,119,86,0.05)',
      border: `1px solid ${active ? 'rgba(218,119,86,0.45)' : 'rgba(218,119,86,0.18)'}`,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '4px' }}>
        <Sparkles size={11} style={{ color: 'var(--accent)' }} />
        <span style={{ fontSize: '10px', fontWeight: '600', textTransform: 'uppercase', color: 'var(--accent)' }}>
          AI suggestion
        </span>
        <button onClick={onUse} style={{
          marginLeft: 'auto', padding: '2px 8px', borderRadius: 5,
          fontSize: '10px', fontWeight: '600', cursor: 'pointer',
          color: 'var(--accent)', background: 'transparent',
          border: '1px solid rgba(218,119,86,0.35)',
        }}>
          {active ? 'In composer' : 'Use this draft'}
        </button>
      </div>
      <div className="md-content" style={{ fontSize: '12px', lineHeight: '1.6', color: 'var(--text-primary)' }}>
        <ReactMarkdown>{draft.content || ''}</ReactMarkdown>
      </div>
    </div>
  )

  const Bubble = ({ side, name, color, icon, time, body, badge, onReply, inReplyTo }) => (
    <div style={{
      display: 'flex', flexDirection: side === 'right' ? 'row-reverse' : 'row',
      gap: '8px', marginBottom: '12px',
    }}>
      {/* Avatar circle */}
      <div style={{
        flexShrink: 0, width: '28px', height: '28px', borderRadius: '50%',
        background: `${color}20`, border: `1px solid ${color}40`,
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        color, marginTop: '14px',
      }}>{icon}</div>
      {/* Bubble + name strip */}
      <div style={{ maxWidth: '78%', display: 'flex', flexDirection: 'column', alignItems: side === 'right' ? 'flex-end' : 'flex-start' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '3px', flexDirection: side === 'right' ? 'row-reverse' : 'row' }}>
          <span style={{ fontSize: '11px', fontWeight: '600', color }}>{name}</span>
          <span style={{ fontSize: '10px', color: 'var(--text-muted)' }}>{time}</span>
          {badge && (
            <span style={{
              fontSize: '10px', padding: '1px 7px', borderRadius: '3px', fontWeight: '600',
              background: 'rgba(218,119,86,0.10)', color: 'var(--accent)',
              border: '1px solid rgba(218,119,86,0.25)',
            }}>{badge}</span>
          )}
        </div>
        <div style={{
          padding: '10px 14px', borderRadius: '12px',
          borderTopLeftRadius: side === 'left' ? '4px' : '12px',
          borderTopRightRadius: side === 'right' ? '4px' : '12px',
          background: side === 'right' ? 'rgba(76,175,125,0.10)' : 'rgba(100,160,200,0.08)',
          border: `1px solid ${side === 'right' ? 'rgba(76,175,125,0.25)' : 'rgba(100,160,200,0.20)'}`,
          color: 'var(--text-primary)', fontSize: '13px', lineHeight: '1.6',
        }}>
          {/* Quote of what this message answers, so a reply isn't an unattached
              bubble when several counters/blockers are in play. */}
          {inReplyTo && (
            <div style={{
              marginBottom: 6, paddingLeft: 8, borderLeft: '2px solid var(--border)',
              fontSize: 11, color: 'var(--text-muted)', fontStyle: 'italic',
              overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
            }} title={inReplyTo}>
              replying to: {inReplyTo}
            </div>
          )}
          <div className="md-content"><ReactMarkdown>{body || ''}</ReactMarkdown></div>
        </div>
        {/* Per-message Reply. Targets THIS message's counter-proposal so the reply
            settles only it — a bare reply still sweeps every open counter from the
            partner, which silently accepted asks the PM never addressed. */}
        {onReply && (
          <button
            onClick={onReply}
            style={{
              alignSelf: side === 'right' ? 'flex-end' : 'flex-start',
              marginTop: 4, padding: '2px 8px', borderRadius: 5,
              fontSize: 10, fontWeight: 600, cursor: 'pointer',
              color: 'var(--text-muted)', background: 'transparent',
              border: '1px solid var(--border-subtle)',
            }}
          >
            Reply
          </button>
        )}
      </div>
    </div>
  )

  return (
    <div style={{
      height: '100%', minHeight: 0, background: 'var(--bg-card)',
      display: 'flex', flexDirection: 'column',
    }}>
      {/* Header — minimal: partner name only. Pending/unread badges
          live on the partner list (left rail), not duplicated here. */}
      <div style={{
        padding: '10px 16px', borderBottom: '1px solid var(--border-subtle)',
        display: 'flex', alignItems: 'center', gap: '8px',
        background: 'var(--bg-elevated)',
      }}>
        <Users size={14} style={{ color: '#6ea8dc' }} />
        <span style={{ fontSize: '13px', fontWeight: '600', color: 'var(--text-primary)', flex: 1 }}>
          {partnerName}
        </span>
      </div>

      {/* Status strip — at-a-glance "what needs my attention". Driven
          by status_strip on the /negotiation response. Auto-hides when
          empty so the header isn't noisy on calm threads. */}
      <StatusStrip strip={thread?.status_strip} />

      {/* Channel tabs. Blockers are a genuinely separate conversation (own thread,
          own A2A task types, own urgency) — an operational escalation should not have
          to be spotted among counter-proposals. "All" stays the default so nothing is
          hidden by accident; the per-tab counts make it obvious where traffic is. */}
      <div style={{
        display: 'flex', gap: 4, padding: '6px 10px',
        borderBottom: '1px solid var(--border-subtle)', background: 'var(--bg-elevated)',
      }}>
        {[
          { key: 'negotiation', label: 'Negotiation', n: TAB_MSGS.negotiation.length },
          { key: 'blocker',     label: 'Blockers',    n: TAB_MSGS.blocker.length,
            alert: openBlockers.length },
        ].map(t => {
          const on = activeTab === t.key
          return (
            <button
              key={t.key}
              onClick={() => setActiveTab(t.key)}
              style={{
                display: 'inline-flex', alignItems: 'center', gap: 6,
                padding: '5px 11px', borderRadius: 6, cursor: 'pointer',
                fontSize: 12, fontWeight: on ? 700 : 500,
                color: on ? 'var(--text-primary)' : 'var(--text-muted)',
                background: on ? 'var(--bg-base)' : 'transparent',
                border: `1px solid ${on ? 'var(--border)' : 'transparent'}`,
              }}
            >
              {t.key === 'blocker' && (
                <AlertTriangle size={12} style={{ color: t.alert ? '#e06c6c' : 'var(--text-muted)' }} />
              )}
              {t.label}
              <span style={{ fontSize: 10, opacity: 0.7 }}>{t.n}</span>
              {/* Unresolved-blocker count is the one thing a PM must not miss. */}
              {t.alert > 0 && (
                <span style={{
                  minWidth: 15, height: 15, padding: '0 4px', borderRadius: 999,
                  background: '#e06c6c', color: '#fff',
                  fontSize: 9, fontWeight: 700, lineHeight: '15px', textAlign: 'center',
                }}>{t.alert}</span>
              )}
            </button>
          )
        })}
      </div>

      {/* Open-blocker Resolve cards — the structured, TERMINAL path that actually closes a
          blocker (sends BLOCKER_RESOLUTION, clears the assignment's blocked flag). Pinned
          above the stream on the Blockers tab because they're action items. The free-text
          composer below is a NON-terminal interim status note (BLOCKER_STATUS_UPDATE); only
          this card closes the Blocker row. */}
      {activeTab === 'blocker' && openBlockers.length > 0 && (
        <div style={{ padding: '10px 16px 0', flexShrink: 0 }}>
          {openBlockers.map(b => (
            <BlockerActionCard
              key={b.id}
              changeId={changeId}
              partnerId={partnerId}
              blocker={b}
              onResolved={() => {
                refetchBlockers()
                qc.invalidateQueries({ queryKey: ['blocker-thread', changeId, partnerId] })
              }}
            />
          ))}
        </div>
      )}
      {/* Messages — scrollable, fills available height */}
      <div ref={scrollRef} style={{
        flex: 1, minHeight: 0, padding: '14px 16px', overflowY: 'auto',
        background: 'var(--bg-base)',
      }}>
        {(() => {
          // Rounds follow kit versions (one 24h round per version), so the
          // stream no longer draws per-message "Round N" dividers.
          const items = []
          for (const m of tabMsgs) {
            // System events (state transitions like "Accepted as-is",
            // "Closed by partner's new counter") render as centered
            // grey notes — they're not authored messages and shouldn't
            // carry an avatar or chat bubble. Backend stamps
            // kind='system' on these synthetic counter_resolution rows.
            if (m.kind === 'system') {
              items.push(
                <div key={m.id} style={{
                  display: 'flex', justifyContent: 'center', margin: '12px 0',
                }}>
                  <span style={{
                    fontSize: '11px', color: 'var(--text-muted)',
                    padding: '4px 12px', borderRadius: '10px',
                    background: 'var(--bg-elevated)',
                    border: '1px solid var(--border-subtle)',
                    fontStyle: 'italic',
                  }}>
                    {m.content}
                  </span>
                </div>
              )
              continue
            }
            // Side is decided by ORIGINATOR, not role. For synthetic
            // counter_proposal / counter_resolution / blocker /
            // blocker_resolution entries the backend stamps `originator`
            // ('partner' | 'npci'); for plain NegotiationMessage rows
            // the originator is inferred from the role.
            const fromPartner =
              m.originator === 'partner'
              || (!m.originator && m.role === 'partner')
            let badge = null
            if (m.role === 'counter_proposal') {
              badge = 'Counter'
            } else if (m.role === 'counter_resolution') {
              const s = (m.status || '').toLowerCase()
              badge =
                s === 'accepted'        ? 'Accepted' :
                s === 'rejected'        ? 'Rejected' :
                s === 'countered_back'  ? 'Countered back' :
                                          'Resolved'
            } else if (m.role === 'blocker') {
              badge = `Blocker · ${(m.severity || 'high').toUpperCase()}`
            } else if (m.role === 'blocker_resolution') {
              badge = 'Blocker resolved'
            } else if (fromPartner && m.message_kind === 'COUNTER_PROPOSAL') {
              badge = 'Counter'
            }
            items.push(
              <Bubble
                key={m.id}
                side={fromPartner ? 'left' : 'right'}
                name={fromPartner ? partnerName : 'NPCI'}
                color={fromPartner ? '#6ea8dc' : 'var(--success)'}
                icon={fromPartner ? <Users size={12} /> : <UserCheck size={12} />}
                time={new Date(m.created_at).toLocaleString()}
                body={m.content}
                badge={badge}
                inReplyTo={m.in_reply_to}
                onReply={fromPartner ? () => {
                  setReplyTo({
                    cpId: m.counter_proposal_id || null,
                    blockerId: m.blocker_id || null,
                    correlationId: m.correlation_id || null,
                    label: (m.content || '').slice(0, 70),
                  })
                  scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight })
                } : undefined}
              />
            )
            // This query's own suggestion, rendered with it rather than hoisted into
            // a single banner — a banner can only ever show one.
            const qDraft = fromPartner && m.correlation_id && !answeredQueries.has(m.correlation_id)
              ? draftsByQuery.get(m.correlation_id)
              : null
            if (qDraft) {
              items.push(
                <QueryDraftCard
                  key={qDraft.id}
                  draft={qDraft}
                  active={selectedQueryId === m.correlation_id}
                  onUse={() => {
                    setComposer(qDraft.content || '')
                    setDraftLoaded(true)
                    setReplyTo({
                      cpId: m.counter_proposal_id || null,
                      blockerId: m.blocker_id || null,
                      correlationId: m.correlation_id,
                      label: (m.content || '').slice(0, 70),
                    })
                  }}
                />
              )
            }
          }
          return items
        })()}

        {/* Pending unprocessed query — shown as a partner bubble ONLY
            when the inbound audit row hasn't yet been folded into a
            NegotiationMessage. After the inbound handler creates the
            partner NM (same content), suppress this bubble so the
            same query doesn't appear twice in the timeline.
            Tightened dedup vs the prior trim-equality predicate:
              · normalize whitespace + case before compare (covers
                rendering differences between the A2A audit payload
                and the persisted NegotiationMessage.content)
              · match any partner-originated message (originator or
                role) — previously missed counter_proposal / blocker
                rows that carry the same content as the audit row
              · also suppress when ANY partner row exists within
                ±10 s of the pending row's timestamp — covers the
                brief race where the audit row arrives before the
                derived NegotiationMessage commits.
            Also tab-aware: pending bubble only shows on the matching
            tab (general by default; negotiation if message_kind is
            COUNTER_PROPOSAL). */}
        {(() => {
          if (!pendingQuery) return null
          const pendingNorm = normText(pendingText)
          const pendingMs = new Date(pendingQuery.created_at).getTime()
          const duplicateExists = visibleMsgs.some(m => {
            const fromPartner = m.originator === 'partner' || (!m.originator && (
              m.role === 'partner' || m.role === 'counter_proposal' || m.role === 'blocker'
            ))
            if (!fromPartner) return false
            if (pendingNorm && normText(m.content) === pendingNorm) return true
            const mMs = m.created_at ? new Date(m.created_at).getTime() : NaN
            return Number.isFinite(mMs) && Math.abs(mMs - pendingMs) <= 10_000
          })
          if (duplicateExists) return null
          return (
            <Bubble
              side="left"
              name={partnerName}
              color="#6ea8dc"
              icon={<Users size={12} />}
              time={new Date(pendingQuery.created_at).toLocaleString()}
              body={pendingText || JSON.stringify(pendingPayload)}
              badge={pendingKind === 'COUNTER_PROPOSAL' ? 'Counter' : 'New'}
            />
          )
        })()}

        {tabMsgs.length === 0 && !pendingQuery && (
          <div style={{ padding: '24px', textAlign: 'center', color: 'var(--text-muted)', fontSize: '12px' }}>
            No messages yet.
          </div>
        )}

        {/* Auto-rejection reply — shown inline in the chat when the resolver
            determined the partner's CP violates a mandatory BRD requirement.
            Renders on the Authority side (right) so it reads as the system's reply,
            mirroring what the partner already sees on their end. The ResolverCard
            (AI suggestion box) is suppressed for this action type. */}
        {resolverRec?.recommendation?.recommended_action === 'auto_rejected_brd' && !resolverProcessing && !pmRepliedAfterQuery && (() => {
          const rec = resolverRec.recommendation
          return (
            <div style={{ display: 'flex', justifyContent: 'flex-end', margin: '8px 0' }}>
              <div style={{
                maxWidth: '80%',
                background: '#fdecea',
                border: '1px solid #fca5a5',
                borderRight: '3px solid #991b1b',
                borderRadius: 10,
                padding: '10px 14px',
              }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 6 }}>
                  <AlertTriangle size={13} color="#991b1b" />
                  <span style={{ fontSize: 10.5, fontWeight: 700, color: '#991b1b', textTransform: 'uppercase', letterSpacing: 0.5 }}>
                    Auto-Rejected · BRD Violation
                  </span>
                </div>
                <div style={{ fontSize: 12.5, color: '#7f1d1d', lineHeight: 1.5 }}>
                  {rec.action_summary || 'This query was auto-rejected because it contradicts a mandatory BRD requirement.'}
                </div>
                {rec.violated_requirement && (
                  <div style={{ marginTop: 8, padding: '6px 10px', background: '#fff', borderRadius: 6, border: '1px solid #fca5a5' }}>
                    <div style={{ fontSize: 10, fontWeight: 700, color: '#991b1b', textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 2 }}>Requirement violated</div>
                    <div style={{ fontSize: 12.5, fontWeight: 600, color: 'var(--text-primary)' }}>{rec.violated_requirement}</div>
                    {rec.violated_reason && (
                      <div style={{ fontSize: 11.5, color: 'var(--text-muted)', marginTop: 3, lineHeight: 1.4 }}>{rec.violated_reason}</div>
                    )}
                  </div>
                )}
                <div style={{ fontSize: 11, color: '#991b1b', fontStyle: 'italic', marginTop: 6 }}>Partner has been automatically notified.</div>
              </div>
            </div>
          )
        })()}

      </div>

      {/* Sticky resolver zone — pinned between the message stream and the
          composer so the AI recommendation stays visible regardless of
          how long the thread grows. */}
      {(resolverProcessing || (resolverRec?.recommendation && !pmRepliedAfterQuery && resolverRec.recommendation.recommended_action !== 'auto_rejected_brd')) && (
        <div style={{
          borderTop: '1px solid var(--border-subtle)',
          background: 'var(--bg-base)',
          padding: '8px 16px',
          maxHeight: '260px',
          overflowY: 'auto',
          flexShrink: 0,
        }}>
          {resolverProcessing && (
            <div style={{
              margin: '4px 0',
              padding: '10px 14px',
              background: 'rgba(218,119,86,0.05)',
              border: '1px solid rgba(218,119,86,0.20)',
              borderLeft: '3px solid var(--accent)',
              borderRadius: 8,
              display: 'flex', alignItems: 'center', gap: 10,
            }}>
              <Loader size={13} style={{
                color: 'var(--accent)',
                animation: 'spin 1s linear infinite',
                flexShrink: 0,
              }} />
              <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 2 }}>
                <span style={{
                  fontSize: 12, fontWeight: 700, color: 'var(--accent)',
                  display: 'inline-flex', alignItems: 'center', gap: 5,
                }}>
                  <Sparkles size={11} />
                  Resolver agent analysing partner's query…
                </span>
                <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                  Reading AUTHORITY_POLICY.md, BRD/TSD and drafting a response · usually 20–30 seconds
                </span>
              </div>
            </div>
          )}
          {resolverRec?.recommendation && !resolverProcessing && !pmRepliedAfterQuery && resolverRec.recommendation.recommended_action !== 'auto_rejected_brd' && (() => {
            const rec = resolverRec.recommendation
            const ACTION_COLOURS = {
              refine_kit:           { bg: '#fef3c7', fg: '#92400e', border: '#fcd34d', label: 'Refine Kit' },
              clarify_inline:       { bg: '#d1fae5', fg: '#065f46', border: '#6ee7b7', label: 'Clarify Inline' },
              placeholder:          { bg: '#e0e7ff', fg: '#3730a3', border: '#a5b4fc', label: 'Placeholder' },
              wait_for_round_close: { bg: '#fae8ff', fg: '#86198f', border: '#e9d5ff', label: 'Wait for Round Close' },
              revise_workflow:      { bg: '#fee2e2', fg: '#991b1b', border: '#fca5a5', label: 'Revise Workflow' },
              escalate:             { bg: '#fee2e2', fg: '#991b1b', border: '#fca5a5', label: 'Escalated' },
              auto_rejected_brd:    { bg: '#fdecea', fg: '#7f1d1d', border: '#fca5a5', label: 'Auto-Rejected (BRD)' },
            }
            const CONF = {
              high:   { bg: 'rgba(16,185,129,0.10)', fg: '#10b981', border: 'rgba(16,185,129,0.30)' },
              medium: { bg: 'rgba(245,158,11,0.10)', fg: '#f59e0b', border: 'rgba(245,158,11,0.30)' },
              low:    { bg: 'rgba(239,68,68,0.10)',  fg: '#ef4444', border: 'rgba(239,68,68,0.30)' },
            }
            const ac = ACTION_COLOURS[rec.recommended_action] || ACTION_COLOURS.clarify_inline
            const cf = CONF[rec.confidence] || CONF.medium
            return (
              <ResolverCard
                key={resolverRec.id}
                rec={rec}
                ac={ac}
                cf={cf}
                meta={resolverRec}
                escalation={escForRec}
                onApply={() => { setComposer(rec.draft_response); setDraftLoaded(true) }}
                onSend={() => sendReply(rec.draft_response)}
              />
            )
          })()}
        </div>
      )}

      {/* Which query this reply will answer. A blind-typed reply had no visible
          target at all, so a mis-routed answer stayed invisible until the partner
          pointed it out. With more than one open, the picker switches target. */}
      {targetQuery && activeTab === 'negotiation' && (
        <div style={{
          padding: '6px 16px', borderTop: '1px solid var(--border-subtle)',
          background: 'var(--bg-elevated)',
          display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap',
        }}>
          <span style={{ fontSize: '10px', fontWeight: '600', textTransform: 'uppercase', color: 'var(--text-muted)' }}>
            Replying to
          </span>
          {openQueries.length > 1 ? (
            <select
              value={targetCorrelationId || ''}
              onChange={e => {
                const q = openQueries.find(x => x.correlation_id === e.target.value)
                if (!q) return
                setReplyTo({
                  cpId: q.counter_proposal_id || null,
                  blockerId: q.blocker_id || null,
                  correlationId: q.correlation_id,
                  label: (q.content || '').slice(0, 70),
                })
              }}
              style={{
                flex: 1, minWidth: 0, padding: '3px 6px', fontSize: '11px',
                background: 'var(--bg-input)', border: '1px solid var(--border)',
                borderRadius: 5, color: 'var(--text-primary)', outline: 'none',
              }}
            >
              {openQueries.map(q => (
                <option key={q.correlation_id} value={q.correlation_id}>
                  {(q.content || '').slice(0, 80)}
                </option>
              ))}
            </select>
          ) : (
            <span
              title={targetQuery.content}
              style={{
                flex: 1, minWidth: 0, fontSize: '11px', color: 'var(--text-primary)',
                overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
              }}
            >
              {targetQuery.content}
            </span>
          )}
          {openQueries.length > 1 && (
            <span style={{ fontSize: '10px', color: 'var(--text-muted)' }}>
              {openQueries.length} open
            </span>
          )}
        </div>
      )}

      {/* AI-draft hint banner (above composer when a draft is loaded).
          AI drafts are generated for general clarifying queries; we
          only surface the banner on the General tab to match. */}
      {hasPendingDraft && draftLoaded && activeTab === 'negotiation' && (
        <div style={{
          padding: '6px 16px', borderTop: '1px solid var(--border-subtle)',
          background: 'rgba(218,119,86,0.04)',
          display: 'flex', alignItems: 'center', gap: '6px',
        }}>
          <Sparkles size={11} style={{ color: 'var(--accent)' }} />
          <span style={{ fontSize: '10px', color: 'var(--accent)', fontWeight: '600', textTransform: 'uppercase' }}>
            AI draft loaded — edit and send
          </span>
        </div>
      )}

      {/* Composer */}
      <div style={{
        padding: '10px 12px', borderTop: '1px solid var(--border-subtle)',
        background: 'var(--bg-elevated)',
        display: 'flex', flexDirection: 'column', gap: '8px',
      }}>
        {/* Generate AI Response button — General tab only. The draft
            backend generator targets free-text clarifying queries; on
            Negotiation / Blocker tabs the action surfaces are the
            CounterActionCard / BlockerActionCard above.
            Its handleProcessQuery handler was removed as dead code — restore
            it from git history if this button is ever re-enabled. */}
        {/* {activeTab === 'general' && pendingQuery && !hasPendingDraft && (
          <button onClick={handleProcessQuery} disabled={processing} style={{
            alignSelf: 'flex-start',
            display: 'flex', alignItems: 'center', gap: '6px',
            padding: '6px 12px', background: 'var(--accent)', color: 'white',
            border: 'none', borderRadius: '6px', fontSize: '12px', fontWeight: '600',
            cursor: processing ? 'not-allowed' : 'pointer', opacity: processing ? 0.7 : 1,
          }}>
            {processing ? <Loader size={12} style={{ animation: 'spin 1s linear infinite' }} /> : <Bot size={12} />}
            {processing ? 'Generating…' : 'Generate AI draft'}
          </button>
        )} */}

        {/* The composer posts to /negotiation/respond, which ALWAYS writes to the
            `general` thread and sends CLARIFICATION_RESPONSE. It takes no channel
            argument — the endpoint coerces any kind outside ('general','cert') back to
            'general' — so a reply typed on the Blockers tab silently landed in
            Negotiation and went out with the wrong task type.
            A blocker is answered with BLOCKER_RESOLUTION via the Resolve action card,
            the only path that closes the Blocker row and notifies the partner properly.
            Hide the free-text box here rather than misroute it. */}
        {/* Reply target. A bare reply settles EVERY open counter from this partner;
            picking a message scopes it to that one. On the Blockers tab the reply is a
            free-text note that goes to the blocker thread as an interim
            BLOCKER_STATUS_UPDATE — it does NOT close the blocker (only Resolve does). */}
        {replyTo && (
          <div style={{
            display: 'flex', alignItems: 'center', gap: 8,
            padding: '6px 10px', borderRadius: 6, marginBottom: 2,
            background: 'var(--bg-base)', border: '1px solid var(--border-subtle)',
          }}>
            <span style={{ fontSize: 10, fontWeight: 700, color: 'var(--accent)' }}>
              REPLYING TO
            </span>
            <span style={{
              flex: 1, minWidth: 0, fontSize: 11, color: 'var(--text-muted)',
              overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
            }}>{replyTo.label}</span>
            <button onClick={() => setReplyTo(null)} title="Clear" style={{
              background: 'transparent', border: 'none', cursor: 'pointer',
              color: 'var(--text-muted)', fontSize: 14, lineHeight: 1,
            }}>x</button>
          </div>
        )}
        {activeTab === 'blocker' && (
          <p style={{ margin: '0 0 2px', fontSize: 10, color: 'var(--text-muted)' }}>
            Free-text note on this blocker — sent as an interim update. Use{' '}
            <strong style={{ color: 'var(--text-secondary)' }}>Resolve</strong> above to
            close it.
          </p>
        )}

        <div style={{ display: 'flex', gap: '8px', alignItems: 'flex-end' }}>
          <textarea
            value={composer}
            onChange={e => setComposer(e.target.value)}
            placeholder="Type a message to the partner…"
            rows={2}
            style={{
              flex: 1, padding: '10px 12px', borderRadius: '8px',
              border: '1px solid var(--border)', background: 'var(--bg-input)',
              color: 'var(--text-primary)', fontSize: '13px', resize: 'vertical',
              fontFamily: 'inherit', lineHeight: '1.6', minHeight: '40px',
            }}
            onKeyDown={(e) => {
              // Enter sends; Shift+Enter inserts a newline. isComposing guard
              // avoids accidental sends mid-IME composition (Hindi/CJK).
              if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault()
                if (composer.trim() && !responding) handleRespond()
              }
            }}
          />
          <button onClick={handleRespond} disabled={responding || !composer.trim()} style={{
            display: 'flex', alignItems: 'center', gap: '6px',
            padding: '10px 16px', background: 'var(--success)', color: 'white',
            border: 'none', borderRadius: '8px', fontSize: '13px', fontWeight: '600',
            cursor: (responding || !composer.trim()) ? 'not-allowed' : 'pointer',
            opacity: (responding || !composer.trim()) ? 0.6 : 1,
            flexShrink: 0, height: 'fit-content',
          }}>
            {responding ? <Loader size={13} style={{ animation: 'spin 1s linear infinite' }} /> : <Send size={13} />}
            Send
          </button>
        </div>
        {sendError && (
          <div style={{
            marginTop: '8px', padding: '8px 12px', fontSize: '12.5px',
            color: 'var(--danger)', background: 'rgba(239,68,68,0.08)',
            border: '1px solid rgba(239,68,68,0.25)', borderRadius: '8px',
          }}>
            {sendError}
          </div>
        )}
      </div>
    </div>
  )
}

// ── Negotiation Hub ───────────────────────────────────────────────────────────
// Cross-partner view: BRD requirements config, per-partner round status,
// cluster analysis, and governance lock. Collapsed by default.

const CATEGORY_LABELS = {
  timeline:     'Timeline',
  scope:        'Scope',
  limits:       'Limits & Thresholds',
  api_contract: 'API Contract',
  dependency:   'Dependencies',
  cert_role:    'Certification Role',
  general:      'General',
}

const DISPOSITION_STYLE = {
  auto_rejected:   { color: '#e05c5c', bg: 'rgba(220,53,69,0.10)', label: 'Auto-Rejected' },
  auto_accepted:   { color: 'var(--success)', bg: 'rgba(76,175,125,0.10)', label: 'Auto-Accepted' },
  escalated_to_pm: { color: 'var(--accent)', bg: 'rgba(218,119,86,0.10)', label: 'Escalated' },
  pending:         { color: 'var(--text-muted)', bg: 'var(--bg-elevated)', label: 'Pending' },
}

// Resolution status of a counter once it's settled. Takes precedence over the
// arrival-time auto_disposition so the Hub matches the resolution shown in
// Partner Messaging (a counter that was escalated then accepted reads
// "Accepted", not a stale "Escalated").
const CP_STATUS_STYLE = {
  accepted:       { color: 'var(--success)', bg: 'rgba(76,175,125,0.12)', label: 'Accepted' },
  rejected:       { color: '#e05c5c', bg: 'rgba(220,53,69,0.12)', label: 'Rejected' },
  countered_back: { color: 'var(--accent)', bg: 'rgba(218,119,86,0.12)', label: 'Countered back' },
  withdrawn:      { color: 'var(--text-muted)', bg: 'var(--bg-elevated)', label: 'Withdrawn' },
}

// Badge for a counter-proposal: the resolution once settled, else the
// arrival-time auto-disposition (open counters only).
function cpBadge(cp) {
  const st = (cp.status || '').toLowerCase()
  if (st && st !== 'open' && CP_STATUS_STYLE[st]) return CP_STATUS_STYLE[st]
  return DISPOSITION_STYLE[cp.auto_disposition] || DISPOSITION_STYLE.pending
}

const REC_STYLE = {
  accept:  { color: 'var(--success)', label: 'Accept' },
  modify:  { color: 'var(--accent)',  label: 'Modify' },
  reject:  { color: '#e05c5c',        label: 'Reject' },
}

// Kit doc types the revision plan can target (label map for the dropdown).
const KIT_DOC_OPTIONS = [
  ['product_doc', 'Product Document'], ['faq', 'FAQ'], ['circular', 'Circular'],
  ['manifest', 'Manifest'], ['product_note', 'Product Note'], ['product_deck', 'Product Deck'],
  ['promo_video', 'Promo Video'], ['explainer_video', 'Explainer Video'],
  ['cert_test_cases', 'Cert Test Cases'], ['prototype_screens', 'Prototype Screens'],
]

const _RP_STATUS = {
  draft:       { label: 'Draft',      bg: 'rgba(154,154,150,0.15)', fg: 'var(--text-muted)' },
  edited:      { label: 'Edited',     bg: 'rgba(100,160,200,0.12)', fg: '#64a0c8' },
  needs_retry: { label: 'Auto-drafted — review', bg: 'rgba(218,119,86,0.12)', fg: 'var(--accent)' },
  generating:  { label: 'Generating…', bg: 'rgba(218,119,86,0.12)', fg: 'var(--accent)' },
  generated:   { label: 'Generated',  bg: 'rgba(76,175,125,0.15)',  fg: 'var(--success)' },
  shipped:     { label: 'Shipped',    bg: 'rgba(76,175,125,0.15)',  fg: 'var(--success)' },
}

// Prepare-next-version panel: draft a per-doc revision plan from the closed
// round's outcomes, let the PM edit it, then regenerate v(N+1). Shipping is a
// separate action on Phase C.
function RevisionPlanPanel({ changeId, stage = 'round_closed', openRounds = 0 }) {
  const qc = useQueryClient()
  const [open, setOpen] = useState(false)
  const [items, setItems] = useState([])
  const [summary, setSummary] = useState('')
  const [loadedId, setLoadedId] = useState(null)
  const [busy, setBusy] = useState('')

  const { data } = useQuery({
    queryKey: ['revision-plan', changeId],
    queryFn: () => phaseCApi.getRevisionPlan(changeId),
    // Poll steadily so the panel reflects the current round promptly — when a
    // new round closes and a fresh plan is drafted, the UI updates within a few
    // seconds instead of showing the previous round's plan until a manual
    // action. (Faster while a kit is generating.) The items-sync effect only
    // re-syncs on a new plan.id, so polling never clobbers in-progress edits.
    refetchInterval: (q) => (q.state.data?.plan?.status === 'generating' ? 4000 : 6000),
  })
  const plan = data?.plan
  const target = data?.target_version
  const status = plan?.status
  const generating = status === 'generating'
  const generated = status === 'generated'

  // Sync server plan → local editable state when the plan row changes (don't
  // clobber mid-edit).
  useEffect(() => {
    if (plan && plan.id !== loadedId) {
      setItems(plan.items || [])
      setSummary(plan.summary || '')
      setLoadedId(plan.id)
    }
  }, [plan, loadedId])

  const run = async (key, fn) => {
    setBusy(key)
    try { await fn() } catch (e) { console.error('revision-plan action failed', e) }
    finally { setBusy(''); qc.invalidateQueries({ queryKey: ['revision-plan', changeId] }) }
  }
  const doDraft    = () => run('draft', () => phaseCApi.draftRevisionPlan(changeId).then(() => setLoadedId(null)))
  const doSave     = () => run('save', () => phaseCApi.saveRevisionPlan(changeId, { items, summary }).then(() => setLoadedId(null)))
  const doGenerate = () => run('generate', () => phaseCApi.generateRevisionKit(changeId))

  const setItem = (i, patch) => setItems(items.map((it, idx) => (idx === i ? { ...it, ...patch } : it)))
  const addItem = () => setItems([...items, { doc_type: 'faq', change_instruction: '', rationale: '', include: true }])
  const removeItem = (i) => setItems(items.filter((_, idx) => idx !== i))

  const head = {
    display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer',
    padding: '10px 12px', userSelect: 'none',
  }
  const sp = _RP_STATUS[status]

  return (
    <div style={{ marginBottom: '16px', borderRadius: '6px', border: '1px solid var(--border-subtle)', overflow: 'hidden' }}>
      <div style={head} onClick={() => setOpen(o => !o)} role="button">
        {open ? <ChevronDown size={13} style={{ color: 'var(--text-muted)' }} /> : <ChevronRight size={13} style={{ color: 'var(--text-muted)' }} />}
        <Sparkles size={13} style={{ color: 'var(--accent)' }} />
        <span style={{ fontSize: '13px', fontWeight: '600', color: 'var(--text-primary)' }}>
          What's changing in this update{target ? ` (v${target})` : ''}
        </span>
        {sp && (
          <span style={{ fontSize: '10px', fontWeight: 700, padding: '2px 8px', borderRadius: 10, background: sp.bg, color: sp.fg }}>
            {generating && <Loader size={9} style={{ animation: 'spin 1s linear infinite', marginRight: 4, verticalAlign: 'middle' }} />}
            {sp.label}
          </span>
        )}
      </div>

      {open && (
        <div style={{ padding: '4px 14px 14px' }}>
          {(stage === 'negotiating' || stage === 'frozen') ? (
            <div style={{ fontSize: 12, color: 'var(--text-muted)', padding: '6px 0', lineHeight: 1.5 }}>
              {stage === 'negotiating'
                ? `A negotiation round is still open${openRounds ? ` (${openRounds})` : ''}. Preparing the next version unlocks once the round closes.`
                : 'Negotiation is frozen — no further versions can be prepared.'}
            </div>
          ) : (<>
          {!plan && (
            <div style={{ fontSize: '12px', color: 'var(--text-muted)', display: 'flex', flexDirection: 'column', gap: 8 }}>
              <span>No plan yet. Draft one from this round's resolved outcomes — then edit which docs change and how.</span>
              <button onClick={doDraft} disabled={busy === 'draft'} style={btnPrimary}>
                {busy === 'draft' ? <Loader size={12} style={{ animation: 'spin 1s linear infinite' }} /> : <Sparkles size={12} />} Draft the changes (AI)
              </button>
            </div>
          )}

          {plan && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              {generated && (
                <div style={{ fontSize: 12, color: 'var(--success)', background: 'rgba(76,175,125,0.10)', border: '1px solid rgba(76,175,125,0.3)', borderRadius: 6, padding: '8px 10px' }}>
                  v{target} generated and ready. Select banks + the latest version and ship it from the Communicate / Ship action.
                </div>
              )}
              {generating && (
                <div style={{ fontSize: 12, color: 'var(--accent)' }}>
                  Regenerating the kit documents for v{target}… this can take a few minutes.
                </div>
              )}

              <div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
                  <label style={{ ...lbl, marginBottom: 0 }}>Summary of changes (ships alongside the kit)</label>
                  <div style={{ flex: 1 }} />
                  {summary.trim() && (
                    <button
                      onClick={async () => {
                        try {
                          const blob = await phaseCApi.downloadRevisionSummary(changeId)
                          const url = window.URL.createObjectURL(blob)
                          const a = document.createElement('a')
                          a.href = url; a.download = `change_summary_v${target}.docx`; a.click()
                          window.URL.revokeObjectURL(url)
                        } catch (e) { console.error('summary .docx download failed', e) }
                      }}
                      style={{ display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 11, fontWeight: 600, padding: '3px 8px', borderRadius: 6, border: '1px solid var(--border)', background: 'var(--bg-base)', color: 'var(--text-secondary)', cursor: 'pointer' }}
                    >
                      <Download size={11} /> .docx
                    </button>
                  )}
                </div>
                <textarea value={summary} onChange={e => setSummary(e.target.value)} disabled={generating || generated} rows={3} style={ta} />
              </div>

              <label style={lbl}>Documents to update</label>
              {items.length === 0 && (
                <p style={{ fontSize: 12, color: 'var(--text-muted)', margin: 0 }}>No documents flagged. Add the ones that should change in v{target}.</p>
              )}
              {items.map((it, i) => (
                <div key={i} style={{ border: '1px solid var(--border-subtle)', borderRadius: 6, padding: '8px 10px', display: 'flex', flexDirection: 'column', gap: 6 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <select value={it.doc_type} onChange={e => setItem(i, { doc_type: e.target.value })} disabled={generating || generated}
                      style={{ fontSize: 12, padding: '4px 8px', borderRadius: 4, border: '1px solid var(--border)', background: 'var(--bg-input)', color: 'var(--text-primary)' }}>
                      {KIT_DOC_OPTIONS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                    </select>
                    <label style={{ fontSize: 11, color: 'var(--text-muted)', display: 'flex', alignItems: 'center', gap: 4, cursor: 'pointer' }}>
                      <input type="checkbox" checked={it.include !== false} disabled={generating || generated}
                        onChange={e => setItem(i, { include: e.target.checked })} /> include
                    </label>
                    <div style={{ flex: 1 }} />
                    {!(generating || generated) && (
                      <button onClick={() => removeItem(i)} title="Remove" style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-muted)', padding: 2 }}><X size={13} /></button>
                    )}
                  </div>
                  <textarea value={it.change_instruction} onChange={e => setItem(i, { change_instruction: e.target.value })}
                    disabled={generating || generated} rows={2} placeholder="What should change in this document, and why…" style={ta} />
                </div>
              ))}

              {!(generating || generated) && (
                <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
                  <button onClick={addItem} style={btnGhost}><Plus size={12} /> Add document</button>
                  <div style={{ flex: 1 }} />
                  <button onClick={doDraft} disabled={busy === 'draft'} title="Re-draft from outcomes (discards edits)" style={btnGhost}>
                    {busy === 'draft' ? <Loader size={12} style={{ animation: 'spin 1s linear infinite' }} /> : null} Re-draft
                  </button>
                  <button onClick={doSave} disabled={busy === 'save'} style={btnGhost}>
                    {busy === 'save' ? <Loader size={12} style={{ animation: 'spin 1s linear infinite' }} /> : <Check size={12} />} Save
                  </button>
                  {(() => {
                    // Generate v(N+1) ships a REAL revision, so it requires at least
                    // one flagged document. A no-change round advances via the
                    // "Reviewed — no change, next round" action in the Hub bar
                    // instead (which doesn't bump the version).
                    const noFlaggedDocs = !items.some(it => it.include !== false);
                    const genDisabled = busy === 'generate' || noFlaggedDocs;
                    return (
                      <button onClick={doGenerate} disabled={genDisabled}
                        title={noFlaggedDocs ? `Flag a document to update before generating v${target} — if nothing changed this round, use “Reviewed — no change, next round” in the Hub bar` : undefined}
                        style={genDisabled ? { ...btnPrimary, opacity: 0.5, cursor: 'not-allowed' } : btnPrimary}>
                        {busy === 'generate' ? <Loader size={12} style={{ animation: 'spin 1s linear infinite' }} /> : <Sparkles size={12} />} Generate updated kit (v{target})
                      </button>
                    );
                  })()}
                </div>
              )}
            </div>
          )}
          </>)}
        </div>
      )}
    </div>
  )
}

const lbl = { display: 'block', fontSize: 11, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.04em', marginBottom: 4 }
const ta = { width: '100%', boxSizing: 'border-box', padding: '8px 10px', borderRadius: 6, border: '1px solid var(--border)', background: 'var(--bg-input)', color: 'var(--text-primary)', fontSize: 12, lineHeight: 1.5, resize: 'vertical', fontFamily: 'inherit' }
const btnPrimary = { display: 'inline-flex', alignItems: 'center', gap: 6, padding: '7px 14px', background: 'var(--accent)', color: 'white', border: 'none', borderRadius: 6, fontSize: 12, fontWeight: 600, cursor: 'pointer' }
const btnGhost = { display: 'inline-flex', alignItems: 'center', gap: 6, padding: '6px 12px', background: 'var(--bg-base)', color: 'var(--text-secondary)', border: '1px solid var(--border)', borderRadius: 6, fontSize: 12, fontWeight: 600, cursor: 'pointer' }

function NegotiationHub({ changeId }) {
  const qc = useQueryClient()
  const [showBRD, setShowBRD] = useState(false)
  const [showRounds, setShowRounds] = useState(true)
  const [showClusters, setShowClusters] = useState(true)
  const [newReq, setNewReq] = useState(null)
  const [generatingBRD, setGeneratingBRD] = useState(false)
  const [brdGenMsg, setBrdGenMsg] = useState('')
  const [clusterDeciding, setClusterDeciding] = useState(null)
  const [decisionText, setDecisionText] = useState('')
  const [modifiedValue, setModifiedValue] = useState('')
  const [finalizing, setFinalizing] = useState(false)
  const [confirmFinalize, setConfirmFinalize] = useState(false)
  const [actionErr, setActionErr] = useState('')
  const [advancingRound, setAdvancingRound] = useState(false)
  const [confirmAdvance, setConfirmAdvance] = useState(false)
  // True while the AI revision plan is being auto-drafted right after closing
  // a round, so the Hub shows "Analyzing the round…" instead of flashing the
  // wrong next-step button before the plan lands.
  const [autoDrafting, setAutoDrafting] = useState(false)
  // Whole-hub collapse. Default collapsed so the Hub sits first as a compact
  // control bar without pushing Partner Messaging down a screenful; the
  // governance stats + publish/finalize stay visible in the bar even collapsed.
  const [collapsed, setCollapsed] = useState(true)

  // Surface POST/PUT/DELETE failures instead of letting the promise reject
  // silently (the row/cluster would just not update with no feedback).
  const runAction = async (fn) => {
    setActionErr('')
    try {
      await fn()
    } catch (e) {
      setActionErr(e?.response?.data?.detail || 'Action failed — please retry.')
    }
  }

  const { data: govStatus } = useQuery({
    queryKey: ['negotiation-status', changeId],
    queryFn: () => phaseCApi.negotiationStatus(changeId).then(r => r.data),
    // Drives the header chip + action buttons (version / open_rounds / frozen).
    // Poll fast and treat data as always-stale so the header tracks reality
    // closely — e.g. after an external reset + re-ship, the brief "round
    // closed → open" gap isn't evident. (Overrides the global 30s staleTime.)
    refetchInterval: 3000,
    staleTime: 0,
    refetchOnMount: 'always',
  })

  const { data: brdReqs, refetch: refetchBRD } = useQuery({
    queryKey: ['brd-requirements', changeId],
    queryFn: () => phaseCApi.listBRDRequirements(changeId).then(r => r.data),
    enabled: showBRD,
    // Poll while open so the auto-segregation that runs on first kit ship (a
    // ~30-60s background LLM job) appears here without a manual refresh.
    refetchInterval: showBRD ? 6000 : false,
  })

  const { data: rounds, refetch: refetchRounds } = useQuery({
    queryKey: ['negotiation-rounds', changeId],
    queryFn: () => phaseCApi.listRounds(changeId).then(r => r.data),
    refetchInterval: 8000,
  })

  const { data: clusters, refetch: refetchClusters } = useQuery({
    queryKey: ['negotiation-clusters', changeId],
    queryFn: () => phaseCApi.listClusters(changeId).then(r => r.data),
    refetchInterval: 8000,
  })

  const version = govStatus?.negotiation_version || 1

  // ── Negotiation/version lifecycle stage ────────────────────────────────────
  // Single source of truth that gates the version actions so they run in order
  // (negotiate → round closes → prepare plan → generate → ship), instead of
  // every button being clickable at once.
  const { data: revPlanData } = useQuery({
    queryKey: ['revision-plan', changeId],
    queryFn: () => phaseCApi.getRevisionPlan(changeId),
    refetchInterval: (q) => (q.state.data?.plan?.status === 'generating' ? 4000 : 6000),
  })
  const openRounds = govStatus?.open_rounds ?? 0
  const planStatus = revPlanData?.plan?.status || null
  // `frozen` is the TERMINAL state (post-v3) only — it blocks all further
  // versions. Finalize (lock this round's specs) is NOT terminal: it closes the
  // round so the next version can be prepared, so it maps to round_closed.
  const frozen = Boolean(govStatus?.frozen)
  // A round being open means we're negotiating, full stop. (Don't treat a stale
  // "finalized" flag as permanently round-closed — once "No changes → continue"
  // opens the next round, the backend clears finalized and this flips back to
  // negotiating, instead of getting stuck showing the same button.)
  const roundsClosed = openRounds === 0
  const stage = frozen ? 'frozen'
    : planStatus === 'generating' ? 'generating'
    : planStatus === 'generated' ? 'ready_to_ship'
    : roundsClosed ? 'round_closed'
    : 'negotiating'
  const canPrepare = stage === 'round_closed'
  // Single version number used by BOTH the chip and the panel so they can't
  // disagree: the active plan's target_version (= current+1 while preparing,
  // = current once generated), falling back to current+1 when no plan yet.
  const nextVer = revPlanData?.target_version || (version + 1)
  const STAGE_CHIP = {
    negotiating:   { label: 'Round open — banks responding', bg: 'rgba(100,160,200,0.12)', fg: '#64a0c8' },
    round_closed:  { label: 'Round closed — choose next step', bg: 'rgba(218,119,86,0.12)', fg: 'var(--accent)' },
    generating:    { label: 'Preparing updated kit…', bg: 'rgba(218,119,86,0.12)', fg: 'var(--accent)' },
    ready_to_ship: { label: `Updated kit (v${nextVer}) ready to send`, bg: 'rgba(76,175,125,0.15)', fg: 'var(--success)' },
    frozen:        { label: 'Negotiation closed', bg: 'rgba(154,154,150,0.15)', fg: 'var(--text-muted)' },
  }
  const stageChip = STAGE_CHIP[stage]
  // Does the saved revision plan already flag document changes? If so, the
  // "No changes needed → continue" shortcut is contradictory and is hidden —
  // the forward action is "Generate updated kit".
  const planHasChanges = (revPlanData?.plan?.items || []).some(it => it.include !== false)
  // Plain-language "what to do now" shown at the top of the expanded Hub.
  const NEXT_STEP_HINT = {
    negotiating:   "Banks are responding. Review their requests below, then Close the current round when you're ready to decide the next step.",
    round_closed:  "Round closed. If the kit needs changes, prepare and generate an updated kit below — otherwise click “No changes needed → continue”.",
    generating:    'Preparing the updated kit — this takes a moment.',
    ready_to_ship: "The updated kit is ready. Send it to the banks with “Send kit to banks” at the top of the page.",
    frozen:        'Negotiation is closed — the terms are final.',
  }

  const handleAddReq = () => runAction(async () => {
    if (!newReq?.label?.trim()) return
    await phaseCApi.createBRDRequirement(changeId, newReq)
    setNewReq(null)
    refetchBRD()
  })

  const handleToggleMandatory = (req) => runAction(async () => {
    await phaseCApi.updateBRDRequirement(changeId, req.id, { is_mandatory: !req.is_mandatory })
    refetchBRD()
  })

  // LLM reads the change's BRD, extracts requirements, and classifies each
  // mandatory/optional. Non-destructive — skips labels already present, so
  // manual rows and prior edits survive a re-run.
  const handleGenerateBRD = async () => {
    setGeneratingBRD(true)
    setBrdGenMsg('')
    try {
      const res = await phaseCApi.generateBRDRequirements(changeId)
      const { created = [], skipped = 0, message } = res.data || {}
      setBrdGenMsg(
        message ||
        `Added ${created.length} requirement${created.length !== 1 ? 's' : ''}` +
          (skipped ? ` · skipped ${skipped} already present` : '') + '.'
      )
      refetchBRD()
    } catch (e) {
      setBrdGenMsg(e?.response?.data?.detail || 'Generation failed')
    } finally {
      setGeneratingBRD(false)
    }
  }

  const handleDeleteReq = (reqId) => runAction(async () => {
    await phaseCApi.deleteBRDRequirement(changeId, reqId)
    refetchBRD()
  })

  const handleCloseRound = (partnerId) => runAction(async () => {
    await phaseCApi.closeRound(changeId, partnerId)
    refetchRounds()
  })

  const handleSweep = () => runAction(async () => {
    await phaseCApi.sweepSilentAccept(changeId)
    refetchRounds()
    qc.invalidateQueries({ queryKey: ['phase-c-partners', changeId] })
  })

  const handleDecideCluster = (clusterId, decision) => runAction(async () => {
    const body = {
      decision,
      decision_text: decisionText || null,
      modified_value: decision === 'modify' && modifiedValue ? { value: modifiedValue } : null,
    }
    await phaseCApi.decideCluster(changeId, clusterId, body)
    setClusterDeciding(null)
    setDecisionText('')
    setModifiedValue('')
    refetchClusters()
    qc.invalidateQueries({ queryKey: ['phase-c-partners', changeId] })
  })

  const handleRefreshAI = (clusterId) => runAction(async () => {
    await phaseCApi.refreshClusterAI(changeId, clusterId)
    refetchClusters()
  })

  const handleFinalize = async () => {
    if (!confirmFinalize) { setConfirmFinalize(true); return }
    setConfirmFinalize(false)
    setFinalizing(true)
    setActionErr('')
    try {
      await phaseCApi.finalizeNegotiation(changeId)
      qc.invalidateQueries({ queryKey: ['negotiation-status', changeId] })
      qc.invalidateQueries({ queryKey: ['phase-c-partners', changeId] })
      setFinalizing(false)
      // Auto-draft the AI revision plan so "Round closed" lands directly on the
      // right next action (Generate updated kit if changes are suggested, else
      // No changes → continue) — no manual "Draft the changes" click needed.
      setAutoDrafting(true)
      try {
        await phaseCApi.draftRevisionPlan(changeId)
      } catch (e) {
        // Non-fatal: the panel's "Draft the changes (AI)" button stays available.
        console.error('auto-draft after round close failed', e)
      } finally {
        setAutoDrafting(false)
        qc.invalidateQueries({ queryKey: ['revision-plan', changeId] })
      }
    } catch (e) {
      setActionErr(e?.response?.data?.detail || 'Close round failed — please retry.')
      setFinalizing(false)
    }
  }


  // Publish + ship a new kit version in one step: bumps the version, clones the
  // current kit docs as the new version (placeholder until the regeneration
  // pipeline lands), snapshots the publication, and dispatches to all active
  // partners — who then see a "New version available" banner and must re-accept.

  // Reviewed — no kit change needed this round. Closes the current round and
  // starts the next one on the same version (no version bump), or freezes the
  // negotiation once the round cap is reached. The no-document-change path to
  // the round-based freeze, so a quiet round still advances the cap.
  const handleAdvanceRound = async () => {
    if (!confirmAdvance) { setConfirmAdvance(true); return }
    setConfirmAdvance(false)
    setAdvancingRound(true)
    setActionErr('')
    try {
      await phaseCApi.advanceRound(changeId)
      qc.invalidateQueries({ queryKey: ['negotiation-status', changeId] })
      qc.invalidateQueries({ queryKey: ['phase-c-partners', changeId] })
      qc.invalidateQueries({ queryKey: ['phase-c-status', changeId] })
    } catch (e) {
      setActionErr(e?.response?.data?.detail || 'Advance round failed — please retry.')
    } finally { setAdvancingRound(false) }
  }

  const s = { fontSize: '12px', color: 'var(--text-muted)' }
  const sectionHead = {
    display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer',
    padding: '10px 0', userSelect: 'none',
  }

  return (
    <div style={{ marginBottom: '24px', borderRadius: '8px', background: 'var(--bg-elevated)', border: '1px solid var(--border)', overflow: 'hidden' }}>
      {/* ── Governance Status Bar ─────────────────────────────────────────── */}
      <div style={{ padding: '12px 20px', borderBottom: collapsed ? 'none' : '1px solid var(--border-subtle)', display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap' }}>
        <div
          onClick={() => setCollapsed(c => !c)}
          style={{ display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer', userSelect: 'none', flex: 1, minWidth: 0 }}
        >
          {collapsed ? <ChevronRight size={14} style={{ color: 'var(--text-muted)' }} /> : <ChevronDown size={14} style={{ color: 'var(--text-muted)' }} />}
          <BarChart2 size={16} style={{ color: 'var(--accent)', flexShrink: 0 }} />
          <span style={{ fontSize: '14px', fontWeight: '600', color: 'var(--text-primary)' }}>
            Negotiation Hub
          </span>
        </div>
        {/* Version badge */}
        <span style={{ fontSize: '11px', padding: '2px 8px', borderRadius: '4px', background: 'var(--bg-base)', border: '1px solid var(--border)', color: 'var(--text-muted)', fontFamily: 'monospace' }}>
          v{version}
        </span>
        {/* Lifecycle stage — the single gate for version actions. A new version
            is only ever created via the gated Prepare → Generate → Ship flow
            below (no one-shot bump button). */}
        {stageChip && (
          <span style={{ fontSize: '11px', fontWeight: 700, padding: '2px 9px', borderRadius: 999, background: stageChip.bg, color: stageChip.fg }}>
            {stage === 'generating' && <Loader size={9} style={{ animation: 'spin 1s linear infinite', marginRight: 4, verticalAlign: 'middle' }} />}
            {stageChip.label}
          </span>
        )}
        {/* Stats */}
        {govStatus && (
          <>
            <span style={s}>{govStatus.total_clusters} cluster{govStatus.total_clusters !== 1 ? 's' : ''}</span>
            <span style={s}>·</span>
            <span style={s}>{govStatus.open_counter_proposals} open CP{govStatus.open_counter_proposals !== 1 ? 's' : ''}</span>
            <span style={s}>·</span>
            <span style={s}>{govStatus.open_rounds} open round{govStatus.open_rounds !== 1 ? 's' : ''}</span>
          </>
        )}
        {/* Stage-gated primary action — only the one relevant next step is
            shown, so the PM never faces a row of all-active buttons. The stage
            chip above already conveys finalized/closed state, so no extra badge. */}
        {stage === 'negotiating' && (
          <button
            onClick={handleFinalize}
            onBlur={() => setConfirmFinalize(false)}
            disabled={finalizing}
            title="Close the current round — lock the banks' decisions and end their response window so you can choose the next step."
            style={{ fontSize: '11px', padding: '4px 10px', borderRadius: '4px', border: confirmFinalize ? '1px solid rgba(220,80,80,0.6)' : '1px solid rgba(218,119,86,0.4)', background: confirmFinalize ? 'rgba(220,80,80,0.12)' : 'rgba(218,119,86,0.08)', color: confirmFinalize ? 'var(--danger)' : 'var(--accent)', cursor: finalizing ? 'wait' : 'pointer' }}
          >
            {finalizing ? 'Closing…' : confirmFinalize ? 'Click again to confirm' : 'Close current round'}
          </button>
        )}
        {stage === 'round_closed' && autoDrafting && (
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: '6px', fontSize: '11px', padding: '4px 10px', borderRadius: '4px', background: 'rgba(100,160,200,0.10)', color: '#64a0c8', border: '1px solid rgba(100,160,200,0.25)' }}>
            <Loader size={10} style={{ animation: 'spin 1s linear infinite' }} /> Analyzing the round…
          </span>
        )}
        {stage === 'round_closed' && !autoDrafting && !planHasChanges && (
          <button
            onClick={handleAdvanceRound}
            onBlur={() => setConfirmAdvance(false)}
            disabled={advancingRound}
            title="No changes to the kit this round — keep the current version and start the next round (or end the negotiation if this was the last allowed round)."
            style={{ fontSize: '11px', padding: '4px 10px', borderRadius: '4px', border: confirmAdvance ? '1px solid rgba(37,99,235,0.6)' : '1px solid rgba(37,99,235,0.35)', background: confirmAdvance ? 'rgba(37,99,235,0.12)' : 'rgba(37,99,235,0.06)', color: 'var(--accent)', cursor: advancingRound ? 'wait' : 'pointer' }}
          >
            {advancingRound ? 'Continuing…' : confirmAdvance ? 'Click again to confirm' : 'No changes needed → continue'}
          </button>
        )}
      </div>

      {!collapsed && (
      <div style={{ padding: '16px 20px' }}>

        {actionErr && (
          <div style={{
            display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8,
            marginBottom: '12px', padding: '8px 12px', borderRadius: '6px',
            background: 'rgba(220,80,80,0.10)', border: '1px solid rgba(220,80,80,0.30)',
            color: 'var(--danger)', fontSize: '12px',
          }}>
            <span>{actionErr}</span>
            <button onClick={() => setActionErr('')} style={{
              background: 'transparent', border: 'none', color: 'var(--danger)',
              cursor: 'pointer', fontSize: '12px', flexShrink: 0,
            }}><X size={13} /></button>
          </div>
        )}

        {/* Silent-close attention banner. When the 24h round window elapsed
            with no response from a partner, the sweep auto-accepts open CPs
            and closes the round. That transition is otherwise silent — the
            PM would have no reason to look. Highlight it here at the top of
            the Hub as soon as it happens (last 24h), and again call out
            when it also caused the negotiation to freeze. */}
        {(() => {
          const now = Date.now()
          const dayMs = 24 * 60 * 60 * 1000
          const silentRecent = (rounds || []).filter(r =>
            r.status === 'silently_accepted' && r.closed_at &&
            (now - new Date(r.closed_at).getTime()) < dayMs,
          )
          if (silentRecent.length === 0) return null
          const partners = Array.from(new Set(silentRecent.map(r => r.partner_name))).join(', ')
          return (
            <div style={{
              marginBottom: '14px', padding: '10px 12px', borderRadius: '6px',
              background: 'rgba(224,108,108,0.08)', border: '1px solid rgba(224,108,108,0.30)',
              fontSize: '12px', color: 'var(--text-secondary)', lineHeight: 1.5,
              display: 'flex', alignItems: 'flex-start', gap: '8px',
            }}>
              <AlertTriangle size={14} style={{ color: '#e05c5c', flexShrink: 0, marginTop: 2 }} />
              <div>
                <strong style={{ color: 'var(--text-primary)' }}>
                  Silent auto-close — no response from {partners}.
                </strong>{' '}
                {silentRecent.length} round{silentRecent.length !== 1 ? 's' : ''} auto-closed in the last 24h
                (24h round window elapsed with no response). Any open counter-proposals were
                treated as silently accepted. Review the outcomes below before the next kit
                revision ships.
              </div>
            </div>
          )
        })()}

        {/* Plain-language "what to do now" for the current stage. */}
        {NEXT_STEP_HINT[stage] && (
          <div style={{ marginBottom: '14px', padding: '10px 12px', borderRadius: '6px', background: 'rgba(100,160,200,0.08)', border: '1px solid rgba(100,160,200,0.20)', fontSize: '12px', color: 'var(--text-secondary)', lineHeight: 1.5 }}>
            <strong style={{ color: 'var(--text-primary)' }}>Next step: </strong>{
              stage === 'round_closed'
                ? (autoDrafting
                    ? 'Analyzing the round to see whether the kit needs changes…'
                    : planHasChanges
                      ? 'Round closed. The drafted update flags document changes below — review them, then click “Generate updated kit” to build and send the new version.'
                      : 'Round closed. Nothing is flagged for change — click “No changes needed → continue”, or add the documents that should change below.')
                : NEXT_STEP_HINT[stage]
            }
          </div>
        )}

        {/* ── Update the kit (revision plan → generate next version) ─────── */}
        <RevisionPlanPanel changeId={changeId} stage={stage} canPrepare={canPrepare} openRounds={openRounds} />

        {/* ── BRD Requirements ──────────────────────────────────────────── */}
        <div style={{ marginBottom: '16px', borderRadius: '6px', border: '1px solid var(--border-subtle)', overflow: 'hidden' }}>
          <div style={sectionHead} onClick={() => setShowBRD(v => !v)} role="button">
            {showBRD ? <ChevronDown size={13} style={{ color: 'var(--text-muted)' }} /> : <ChevronRight size={13} style={{ color: 'var(--text-muted)' }} />}
            <Tag size={13} style={{ color: 'var(--text-muted)' }} />
            <span style={{ fontSize: '13px', fontWeight: '600', color: 'var(--text-primary)' }}>BRD Requirements</span>
            <span style={{ fontSize: '11px', color: 'var(--text-muted)', marginLeft: '4px' }}>— configure mandatory flags &amp; tolerance</span>
          </div>
          {showBRD && (
            <div style={{ borderTop: '1px solid var(--border-subtle)', padding: '12px 0' }}>
              {(brdReqs || []).map(req => (
                <div key={req.id} style={{ display: 'flex', alignItems: 'center', gap: '10px', padding: '6px 8px', borderRadius: '4px' }}>
                  <button
                    onClick={() => handleToggleMandatory(req)}
                    title={req.is_mandatory ? 'Mandatory — click to make optional' : 'Optional — click to make mandatory'}
                    style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 0, display: 'flex', alignItems: 'center' }}
                  >
                    {req.is_mandatory
                      ? <ToggleRight size={18} style={{ color: '#e05c5c' }} />
                      : <ToggleLeft size={18} style={{ color: 'var(--text-muted)' }} />}
                  </button>
                  <span style={{ fontSize: '12px', color: 'var(--text-primary)', flex: 1 }}>{req.label}</span>
                  {req.source === 'ai' && (
                    <span
                      title={req.ai_rationale || 'AI-classified from the BRD — click the toggle to override'}
                      style={{
                        fontSize: '9px', padding: '1px 5px', borderRadius: '3px',
                        background: 'rgba(91,141,239,0.12)', color: '#5b8def',
                        border: '1px solid rgba(91,141,239,0.30)', fontWeight: 700, cursor: 'help',
                      }}
                    >AI</span>
                  )}
                  <span style={{
                    fontSize: '10px', padding: '1px 6px', borderRadius: '3px',
                    background: 'var(--bg-base)', color: 'var(--text-muted)', border: '1px solid var(--border)',
                  }}>{CATEGORY_LABELS[req.category] || req.category}</span>
                  {req.is_mandatory && (
                    <span style={{ fontSize: '10px', color: '#e05c5c', fontWeight: '600' }}>MANDATORY</span>
                  )}
                  {req.tolerance_config && (
                    <span style={{ fontSize: '10px', color: 'var(--text-muted)', fontFamily: 'monospace' }}>
                      tol: {JSON.stringify(req.tolerance_config)}
                    </span>
                  )}
                  <button onClick={() => handleDeleteReq(req.id)} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-muted)', padding: 0 }}>
                    <Trash2 size={12} />
                  </button>
                </div>
              ))}
              {(brdReqs || []).length === 0 && (
                <div style={{ padding: '8px 8px', fontSize: '12px', color: 'var(--text-muted)' }}>No requirements configured yet.</div>
              )}
              {/* Add new requirement inline */}
              {newReq ? (
                <div style={{ padding: '8px', borderTop: '1px solid var(--border-subtle)', display: 'flex', gap: '8px', flexWrap: 'wrap', marginTop: '8px' }}>
                  <input
                    placeholder="Label (e.g. Go-live date)"
                    value={newReq.label || ''}
                    onChange={e => setNewReq(r => ({ ...r, label: e.target.value }))}
                    style={{ flex: 1, minWidth: '160px', padding: '4px 8px', borderRadius: '4px', border: '1px solid var(--border)', background: 'var(--bg-base)', color: 'var(--text-primary)', fontSize: '12px' }}
                  />
                  <select
                    value={newReq.category || 'general'}
                    onChange={e => setNewReq(r => ({ ...r, category: e.target.value }))}
                    style={{ padding: '4px 8px', borderRadius: '4px', border: '1px solid var(--border)', background: 'var(--bg-base)', color: 'var(--text-primary)', fontSize: '12px' }}
                  >
                    {Object.entries(CATEGORY_LABELS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
                  </select>
                  <label style={{ display: 'flex', alignItems: 'center', gap: '4px', fontSize: '12px', color: 'var(--text-primary)', cursor: 'pointer' }}>
                    <input type="checkbox" checked={!!newReq.is_mandatory} onChange={e => setNewReq(r => ({ ...r, is_mandatory: e.target.checked }))} />
                    Mandatory
                  </label>
                  <button onClick={handleAddReq} style={{ padding: '4px 10px', borderRadius: '4px', background: 'var(--accent)', color: '#fff', border: 'none', cursor: 'pointer', fontSize: '12px' }}>Add</button>
                  <button onClick={() => setNewReq(null)} style={{ padding: '4px 8px', borderRadius: '4px', background: 'none', border: '1px solid var(--border)', color: 'var(--text-muted)', cursor: 'pointer', fontSize: '12px' }}>Cancel</button>
                </div>
              ) : (
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap', margin: '8px 8px 0' }}>
                  <button
                    onClick={() => setNewReq({ label: '', category: 'general', is_mandatory: false })}
                    style={{ display: 'flex', alignItems: 'center', gap: '6px', padding: '4px 10px', borderRadius: '4px', border: '1px dashed var(--border)', background: 'none', color: 'var(--text-muted)', cursor: 'pointer', fontSize: '12px' }}
                  >
                    <Plus size={12} /> Add Requirement
                  </button>
                  <button
                    onClick={handleGenerateBRD}
                    disabled={generatingBRD}
                    title="Read this change's BRD and classify its requirements as mandatory/optional"
                    style={{ display: 'flex', alignItems: 'center', gap: '6px', padding: '4px 10px', borderRadius: '4px', border: '1px solid rgba(91,141,239,0.4)', background: 'rgba(91,141,239,0.08)', color: '#5b8def', cursor: generatingBRD ? 'not-allowed' : 'pointer', fontSize: '12px', opacity: generatingBRD ? 0.7 : 1 }}
                  >
                    {generatingBRD
                      ? <Loader size={12} style={{ animation: 'spin 1s linear infinite' }} />
                      : <Sparkles size={12} />}
                    {generatingBRD ? 'Reading BRD…' : 'Generate from BRD (AI)'}
                  </button>
                  {brdGenMsg && (
                    <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>{brdGenMsg}</span>
                  )}
                </div>
              )}
            </div>
          )}
        </div>

        {/* ── Round Status Grid ──────────────────────────────────────────── */}
        <div style={{ marginBottom: '16px', borderRadius: '6px', border: '1px solid var(--border-subtle)', overflow: 'hidden' }}>
          <div style={sectionHead} onClick={() => setShowRounds(v => !v)} role="button">
            {showRounds ? <ChevronDown size={13} style={{ color: 'var(--text-muted)' }} /> : <ChevronRight size={13} style={{ color: 'var(--text-muted)' }} />}
            <Clock size={13} style={{ color: 'var(--text-muted)' }} />
            <span style={{ fontSize: '13px', fontWeight: '600', color: 'var(--text-primary)' }}>Negotiation Rounds</span>
            {rounds && rounds.length > 0 && (
              <span style={{ fontSize: '11px', padding: '1px 6px', borderRadius: '3px', background: 'var(--bg-base)', border: '1px solid var(--border)', color: 'var(--text-muted)' }}>
                {rounds.filter(r => r.status === 'open').length} open
              </span>
            )}
            {rounds && rounds.some(r => r.status === 'open' && r.seconds_remaining <= 0) && (
              <button
                onClick={e => { e.stopPropagation(); handleSweep() }}
                title="Apply silent acceptance to overdue rounds"
                style={{ marginLeft: 'auto', padding: '2px 8px', borderRadius: '4px', border: '1px solid var(--border)', background: 'none', color: 'var(--text-muted)', cursor: 'pointer', fontSize: '11px', display: 'flex', alignItems: 'center', gap: '4px' }}
              >
                <RefreshCw size={10} /> Close overdue rounds
              </button>
            )}
          </div>
          {showRounds && (
            <div style={{ borderTop: '1px solid var(--border-subtle)' }}>
              {(!rounds || rounds.length === 0) && (
                <div style={{ padding: '12px 8px', fontSize: '12px', color: 'var(--text-muted)' }}>No round states yet — rounds start when partners acknowledge the change kit.</div>
              )}
              {(rounds || []).map(r => {
                const hrs = Math.floor(r.seconds_remaining / 3600)
                const mins = Math.floor((r.seconds_remaining % 3600) / 60)
                const timeLeft = r.status === 'open' ? `${hrs}h ${mins}m left` : null
                const roundCounters = r.counter_proposals || []
                // Derive a more accurate badge when a silently-closed round had
                // counters that were actually rejected rather than accepted.
                const cpIsRejected = cp => {
                  const st = (cp.status || '').toLowerCase()
                  return st === 'rejected' || cp.auto_disposition === 'auto_rejected'
                }
                let badgeLabel = r.status.replace(/_/g, ' ')
                let badgeStyle = {
                  open:              { color: 'var(--accent)', bg: 'rgba(218,119,86,0.10)' },
                  silently_accepted: { color: 'var(--success)', bg: 'rgba(76,175,125,0.10)' },
                  closed_by_pm:      { color: 'var(--text-muted)', bg: 'var(--bg-base)' },
                  responded:         { color: '#64a0c8', bg: 'rgba(100,160,200,0.10)' },
                }[r.status] || { color: 'var(--text-muted)', bg: 'var(--bg-base)' }
                if (r.status === 'silently_accepted' && roundCounters.length > 0) {
                  const allRejected = roundCounters.every(cpIsRejected)
                  const anyRejected = roundCounters.some(cpIsRejected)
                  if (allRejected) {
                    badgeLabel = 'counters rejected'
                    badgeStyle = { color: '#e05c5c', bg: 'rgba(220,53,69,0.10)' }
                  } else if (anyRejected) {
                    badgeLabel = 'mixed outcome'
                    badgeStyle = { color: 'var(--accent)', bg: 'rgba(218,119,86,0.10)' }
                  }
                }
                return (
                  <div key={r.id} style={{ borderBottom: '1px solid var(--border-subtle)' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '10px', padding: '8px 8px' }}>
                      <span style={{ fontSize: '12px', color: 'var(--text-primary)', minWidth: '130px', fontWeight: '500' }}>{r.partner_name}</span>
                      <span style={{ fontSize: '10px', padding: '1px 6px', borderRadius: '3px', background: 'var(--bg-base)', border: '1px solid var(--border)', color: 'var(--text-muted)' }}>Rnd {r.round_number}</span>
                      <span style={{ fontSize: '10px', padding: '1px 6px', borderRadius: '3px', background: badgeStyle.bg, color: badgeStyle.color, fontWeight: '600', textTransform: 'uppercase' }}>{badgeLabel}</span>
                      {roundCounters.length > 0 && (
                        <span style={{ fontSize: '10px', padding: '1px 6px', borderRadius: '3px', background: 'rgba(218,119,86,0.10)', color: 'var(--accent)', border: '1px solid rgba(218,119,86,0.25)' }}>
                          {roundCounters.length} counter{roundCounters.length !== 1 ? 's' : ''}
                        </span>
                      )}
                      {timeLeft && (
                        <span style={{ fontSize: '11px', color: r.seconds_remaining < 3600 ? '#e05c5c' : 'var(--text-muted)', display: 'flex', alignItems: 'center', gap: '3px' }}>
                          <Clock size={10} /> {timeLeft}
                        </span>
                      )}
                      <span style={{ fontSize: '11px', color: 'var(--text-muted)', marginLeft: 'auto' }}>
                        Deadline {r.deadline_at ? new Date(r.deadline_at).toLocaleString() : '—'}
                      </span>
                      {r.status === 'open' && (
                        <button
                          onClick={() => handleCloseRound(r.partner_id)}
                          style={{ padding: '2px 8px', borderRadius: '4px', border: '1px solid var(--border)', background: 'none', color: 'var(--text-muted)', cursor: 'pointer', fontSize: '11px' }}
                        >Close this bank's round</button>
                      )}
                    </div>
                    {/* Counter-proposals raised in this round */}
                    {roundCounters.length > 0 && (
                      <div style={{ padding: '0 8px 8px 26px', display: 'flex', flexDirection: 'column', gap: '4px' }}>
                        {roundCounters.map(cp => {
                          const disp = cpBadge(cp)
                          return (
                            <div key={cp.id} style={{ fontSize: '11px', color: 'var(--text-muted)', padding: '4px 8px', borderRadius: '4px', background: 'var(--bg-base)', display: 'flex', alignItems: 'center', gap: '6px' }}>
                              <Edit3 size={10} style={{ color: 'var(--accent)', flexShrink: 0 }} />
                              {cp.request_category && (
                                <span style={{ fontSize: '9px', padding: '1px 5px', borderRadius: '3px', background: 'var(--bg-elevated)', border: '1px solid var(--border)', color: 'var(--text-muted)', flexShrink: 0 }}>
                                  {CATEGORY_LABELS[cp.request_category] || cp.request_category}
                                </span>
                              )}
                              <span style={{ flex: 1, color: 'var(--text-primary)' }}>
                                {cp.justification?.slice(0, 90)}{cp.justification?.length > 90 ? '…' : ''}
                              </span>
                              <span style={{ fontSize: '10px', padding: '1px 5px', borderRadius: '3px', background: disp.bg, color: disp.color, fontWeight: '600', flexShrink: 0 }}>{disp.label}</span>
                            </div>
                          )
                        })}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          )}
        </div>

        {/* ── Cross-Partner Clusters ─────────────────────────────────────── */}
        <div style={{ borderRadius: '6px', border: '1px solid var(--border-subtle)', overflow: 'hidden' }}>
          <div style={sectionHead} onClick={() => setShowClusters(v => !v)} role="button">
            {showClusters ? <ChevronDown size={13} style={{ color: 'var(--text-muted)' }} /> : <ChevronRight size={13} style={{ color: 'var(--text-muted)' }} />}
            <Users size={13} style={{ color: 'var(--text-muted)' }} />
            <span style={{ fontSize: '13px', fontWeight: '600', color: 'var(--text-primary)' }}>Cross-Partner Clusters</span>
            {clusters && (
              <span style={{ fontSize: '11px', padding: '1px 6px', borderRadius: '3px', background: 'var(--bg-base)', border: '1px solid var(--border)', color: 'var(--text-muted)' }}>
                {clusters.filter(c => c.pm_decision === 'pending').length} pending decision
              </span>
            )}
          </div>
          {showClusters && (
            <div style={{ borderTop: '1px solid var(--border-subtle)' }}>
              {(!clusters || clusters.length === 0) && (
                <div style={{ padding: '12px 8px', fontSize: '12px', color: 'var(--text-muted)' }}>No clusters yet — clusters form as partners submit counter-proposals.</div>
              )}
              {(clusters || []).map(c => {
                const recStyle = REC_STYLE[c.ai_recommendation] || {}
                const deciding = clusterDeciding === c.id
                return (
                  <div key={c.id} style={{ padding: '14px 12px', borderBottom: '1px solid var(--border-subtle)' }}>
                    {/* Cluster header row */}
                    <div style={{ display: 'flex', alignItems: 'flex-start', gap: '10px', flexWrap: 'wrap' }}>
                      <div style={{ flex: 1, minWidth: '220px' }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '4px' }}>
                          <span style={{ fontSize: '12px', fontWeight: '600', color: 'var(--text-primary)' }}>{c.topic_summary || c.cluster_key}</span>
                          <span style={{ fontSize: '10px', padding: '1px 6px', borderRadius: '3px', background: 'var(--bg-base)', color: 'var(--text-muted)', border: '1px solid var(--border)' }}>
                            {CATEGORY_LABELS[c.category] || c.category}
                          </span>
                          {c.conflict_with_cluster_id && (
                            <span title="Conflicts with another cluster" style={{ fontSize: '10px', padding: '1px 6px', borderRadius: '3px', background: 'rgba(220,53,69,0.10)', color: '#e05c5c', border: '1px solid rgba(220,53,69,0.25)', display: 'flex', alignItems: 'center', gap: '3px' }}>
                              <AlertTriangle size={9} /> Conflict
                            </span>
                          )}
                        </div>
                        <div style={{ fontSize: '11px', color: 'var(--text-muted)', marginBottom: '6px' }}>
                          {c.partner_count} partner{c.partner_count !== 1 ? 's' : ''}: {c.partner_names.join(', ')}
                        </div>
                        {c.ai_summary && (
                          <div style={{ fontSize: '12px', color: 'var(--text-primary)', lineHeight: '1.5', marginBottom: '6px', padding: '8px 10px', borderRadius: '4px', background: 'var(--bg-base)', border: '1px solid var(--border)' }}>
                            {c.ai_summary}
                          </div>
                        )}
                        {/* Individual CPs in cluster */}
                        {c.counter_proposals.map(cp => {
                          const disp = cpBadge(cp)
                          return (
                            <div key={cp.id} style={{ fontSize: '11px', color: 'var(--text-muted)', padding: '4px 6px', marginBottom: '2px', borderRadius: '3px', background: 'var(--bg-elevated)', display: 'flex', alignItems: 'center', gap: '6px' }}>
                              <span style={{ fontWeight: '500', minWidth: '80px', color: 'var(--text-primary)' }}>
                                {(c.partner_names.find((_, i) => c.counter_proposals[i]?.id === cp.id)) || 'Partner'}
                              </span>
                              <span style={{ flex: 1 }}>{cp.justification?.slice(0, 80)}{cp.justification?.length > 80 ? '…' : ''}</span>
                              <span style={{ fontSize: '10px', padding: '1px 5px', borderRadius: '3px', background: disp.bg, color: disp.color, fontWeight: '600' }}>{disp.label}</span>
                            </div>
                          )
                        })}
                      </div>

                      {/* Right column: AI recommendation + PM decision */}
                      <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: '6px', minWidth: '140px' }}>
                        {c.ai_recommendation && (
                          <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                            <Star size={11} style={{ color: recStyle.color }} />
                            <span style={{ fontSize: '11px', color: recStyle.color, fontWeight: '600' }}>AI: {recStyle.label}</span>
                            {c.confidence_score != null && (
                              <span style={{ fontSize: '10px', color: 'var(--text-muted)' }}>{Math.round(c.confidence_score * 100)}%</span>
                            )}
                            <button
                              onClick={() => handleRefreshAI(c.id)}
                              title="Refresh AI analysis"
                              style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-muted)', padding: 0, display: 'flex', alignItems: 'center' }}
                            ><RefreshCw size={10} /></button>
                          </div>
                        )}
                        {c.pm_decision !== 'pending' ? (
                          <span style={{
                            fontSize: '11px', padding: '2px 8px', borderRadius: '4px', fontWeight: '600',
                            ...({ accept: { background: 'rgba(76,175,125,0.15)', color: 'var(--success)' }, modify: { background: 'rgba(218,119,86,0.12)', color: 'var(--accent)' }, reject: { background: 'rgba(220,53,69,0.12)', color: '#e05c5c' } }[c.pm_decision] || {}),
                          }}>
                            PM: {c.pm_decision.toUpperCase()}
                          </span>
                        ) : !deciding ? (
                          <button
                            onClick={() => setClusterDeciding(c.id)}
                            style={{ fontSize: '11px', padding: '4px 10px', borderRadius: '4px', border: '1px solid var(--accent)', background: 'rgba(218,119,86,0.08)', color: 'var(--accent)', cursor: 'pointer' }}
                          >Review &amp; Decide</button>
                        ) : null}
                      </div>
                    </div>

                    {/* Decision form */}
                    {deciding && (
                      <div style={{ marginTop: '10px', padding: '10px', borderRadius: '4px', background: 'var(--bg-base)', border: '1px solid var(--border)' }}>
                        <div style={{ fontSize: '12px', color: 'var(--text-primary)', fontWeight: '600', marginBottom: '8px' }}>PM Decision</div>
                        <textarea
                          placeholder="Decision rationale (optional)"
                          value={decisionText}
                          onChange={e => setDecisionText(e.target.value)}
                          rows={2}
                          style={{ width: '100%', padding: '6px 8px', borderRadius: '4px', border: '1px solid var(--border)', background: 'var(--bg-elevated)', color: 'var(--text-primary)', fontSize: '12px', resize: 'vertical', boxSizing: 'border-box', marginBottom: '6px' }}
                        />
                        <input
                          placeholder="Modified value (for 'Modify' decision)"
                          value={modifiedValue}
                          onChange={e => setModifiedValue(e.target.value)}
                          style={{ width: '100%', padding: '5px 8px', borderRadius: '4px', border: '1px solid var(--border)', background: 'var(--bg-elevated)', color: 'var(--text-primary)', fontSize: '12px', boxSizing: 'border-box', marginBottom: '8px' }}
                        />
                        <div style={{ display: 'flex', gap: '6px' }}>
                          <button onClick={() => handleDecideCluster(c.id, 'accept')} style={{ padding: '4px 10px', borderRadius: '4px', border: 'none', background: 'rgba(76,175,125,0.15)', color: 'var(--success)', cursor: 'pointer', fontSize: '12px', fontWeight: '600' }}>Accept</button>
                          <button onClick={() => handleDecideCluster(c.id, 'modify')} style={{ padding: '4px 10px', borderRadius: '4px', border: 'none', background: 'rgba(218,119,86,0.12)', color: 'var(--accent)', cursor: 'pointer', fontSize: '12px', fontWeight: '600' }}>Accept with change</button>
                          <button onClick={() => handleDecideCluster(c.id, 'reject')} style={{ padding: '4px 10px', borderRadius: '4px', border: 'none', background: 'rgba(220,53,69,0.12)', color: '#e05c5c', cursor: 'pointer', fontSize: '12px', fontWeight: '600' }}>Reject</button>
                          <button onClick={() => { setClusterDeciding(null); setDecisionText(''); setModifiedValue('') }} style={{ padding: '4px 8px', borderRadius: '4px', border: '1px solid var(--border)', background: 'none', color: 'var(--text-muted)', cursor: 'pointer', fontSize: '12px' }}>Cancel</button>
                        </div>
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          )}
        </div>
      </div>
      )}
    </div>
  )
}


export default function PhaseC() {
  const { id } = useParams()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [showAssign, setShowAssign] = useState(false)
  const [selectedPartners, setSelectedPartners] = useState([])
  const [error, setError] = useState(null)
  // Unified "Communicate Change" panel: pick banks + an existing version, ship at once.
  const [showShipPanel, setShowShipPanel] = useState(false)
  const [shipBankIds, setShipBankIds] = useState([])
  const [shipping, setShipping] = useState(false)
  const [shipResult, setShipResult] = useState(null)
  // Version to communicate. We always ship the current latest docs, so this is
  // pinned to the latest version; older versions are shown for context only.
  // `null` = not yet initialised (defaults to latest once the version loads).
  const [shipVersion, setShipVersion] = useState(null)
  // Per-item shipment picker (migration 0115). `shipItems` is keyed by doc_type
  // → { checked, generated, override }. Populated when the modal opens.
  const [shipItems, setShipItems] = useState({})
  const [manifestLoading, setManifestLoading] = useState(false)
  // Collapse state — secondary sections start collapsed; user can expand as needed.
  // Assigned Partners and Implementation Progress stay always-visible (not collapsible).
  const [showNegotiations, setShowNegotiations] = useState(false)
  const [selectedPartnerId, setSelectedPartnerId] = useState(null)
  // Collapse partner list to a thin column so the chat / resolver card get
  // the full width. Defaults to collapsed for the typical single-partner
  // demo flow — PM expands when they need to switch between many partners.
  const [chatListCollapsed, setChatListCollapsed] = useState(true)
  const [showCertification, setShowCertification] = useState(false)
  const [showMessageLog, setShowMessageLog] = useState(false)
  const [expandedGridRows, setExpandedGridRows] = useState(new Set())
  const [startingAllCert, setStartingAllCert] = useState(false)
  const messagingRef = useRef(null)
  const certRef = useRef(null)
  const negotiationHubRef = useRef(null)
  const hasAutoOpenedRef = useRef(false)
  const hasAutoOpenedCertRef = useRef(false)
  const { data: gateEval } = useQuery({
    queryKey: ['eval-latest', id, 'product_kit_to_phase_c_communication'],
    queryFn: () => evalApi.latest(id, 'product_kit_to_phase_c_communication').then(r => r.data),
    enabled: !!id,
    refetchInterval: 8000,
    refetchOnWindowFocus: true,
  })

  // Auto-poll the active Phase C queries so partner status changes
  // (received → accepted → ready → certified) show up without a
  // manual refresh. 6s is fast enough to feel live without hammering.
  const { data: change, isFetching: changeFetching } = useQuery({
    queryKey: ['change', id],
    queryFn: () => changesApi.get(id).then(r => r.data),
    refetchInterval: 6000,
    refetchOnWindowFocus: true,
  })

  // Current published version of the kit. Drives the "Ship version" selector
  // beside Communicate — first-time changes are v1; once a new version has been
  // published the selector reflects it. We always ship the current latest docs.
  const { data: negStatus } = useQuery({
    queryKey: ['phase-c-neg-status', id],
    queryFn: () => phaseCApi.negotiationStatus(id).then(r => r.data),
    refetchInterval: 6000,
    refetchOnWindowFocus: true,
  })
  const currentVersion = negStatus?.negotiation_version || 1
  const availableVersions = Array.from({ length: currentVersion }, (_, i) => i + 1)
  // Keep the selection pinned to the latest version (the only shippable one).
  useEffect(() => {
    if (shipVersion == null || shipVersion > currentVersion) setShipVersion(currentVersion)
  }, [currentVersion])  // eslint-disable-line react-hooks/exhaustive-deps

  // Force-pull every Phase C query so the operator can demand fresh
  // partner state instead of waiting for the next poll tick. Same UX
  // as a browser hard-reload, but keeps the React tree mounted.
  const handleSync = () => {
    qc.invalidateQueries({ queryKey: ['change', id] })
    qc.invalidateQueries({ queryKey: ['phase-c-status', id] })
    qc.invalidateQueries({ queryKey: ['phase-c-partners', id] })
    qc.invalidateQueries({ queryKey: ['phase-c-messages', id] })
    qc.invalidateQueries({ queryKey: ['phase-c-progress-grid', id] })
    qc.invalidateQueries({ queryKey: ['cert-summary', id] })
  }

  const { data: _phaseCStatus } = useQuery({
    queryKey: ['phase-c-status', id],
    queryFn: () => phaseCApi.status(id).then(r => r.data),
    retry: false,
    refetchInterval: 6000,
    refetchOnWindowFocus: true,
  })

  const { data: assignedPartners, refetch: refetchAssigned } = useQuery({
    queryKey: ['phase-c-partners', id],
    queryFn: () => phaseCApi.listAssignedPartners(id).then(r => r.data),
    refetchInterval: 6000,
    refetchOnWindowFocus: true,
  })

  const { data: allPartners } = useQuery({
    queryKey: ['all-partners'],
    queryFn: () => partnersApi.list().then(r => r.data),
    enabled: showAssign || showShipPanel,
  })

  const { data: messages } = useQuery({
    queryKey: ['phase-c-messages', id],
    queryFn: () => phaseCApi.messages(id).then(r => r.data),
    refetchInterval: 5000,
    refetchOnWindowFocus: true,
  })

  const { data: progressGrid } = useQuery({
    queryKey: ['phase-c-progress-grid', id],
    queryFn: () => phaseCApi.getProgressGrid(id).then(r => r.data),
    enabled: (assignedPartners || []).length > 0,
    refetchInterval: 5000,
    refetchOnWindowFocus: true,
  })

  const assignedIds = new Set((assignedPartners || []).map(p => p.partner_id))
  const availablePartners = (allPartners || []).filter(p => p.status === 'active' && !assignedIds.has(p.id))

  const pendingQueriesCount = (messages || []).filter(m => m.task_type === 'query' && m.status === 'submitted' && m.direction === 'inbound').length
  const openCountersTotal   = (assignedPartners || []).reduce((s, p) => s + (p.open_counters_count || 0), 0)
  const openBlockersTotal   = (assignedPartners || []).reduce((s, p) => s + (p.open_blockers_count || 0), 0)
  const readyForCertCount   = (assignedPartners || []).filter(p => ['ready_for_certification', 'ready', 'certifying'].includes(p.status)).length
  const hasActivePartners   = (assignedPartners || []).some(p => p.status !== 'assigned')
  // Simplified negotiation stage derived from negStatus (no plan_status available here;
  // `round_closed` covers the round_closed/generating/ready_to_ship sub-states).
  const negStage = !negStatus || !hasActivePartners ? null
    : negStatus.frozen ? 'frozen'
    : (negStatus.open_rounds ?? 0) > 0 ? 'negotiating'
    : 'round_closed'
  const hasRoundClosed      = negStage === 'round_closed'
  const totalAttentionItems = pendingQueriesCount + openCountersTotal + openBlockersTotal + readyForCertCount + (hasRoundClosed ? 1 : 0)

  useEffect(() => {
    if (hasAutoOpenedRef.current) return
    if ((pendingQueriesCount + openCountersTotal + openBlockersTotal) > 0) {
      setShowNegotiations(true)
      hasAutoOpenedRef.current = true
    }
  }, [pendingQueriesCount, openCountersTotal, openBlockersTotal])  // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (hasAutoOpenedCertRef.current) return
    if (readyForCertCount > 0) {
      setShowCertification(true)
      hasAutoOpenedCertRef.current = true
    }
  }, [readyForCertCount])  // eslint-disable-line react-hooks/exhaustive-deps

  const handleAssign = async () => {
    if (!selectedPartners.length) return
    setError(null)
    try {
      await phaseCApi.assignPartners(id, selectedPartners)
      setSelectedPartners([])
      setShowAssign(false)
      refetchAssigned()
      qc.invalidateQueries({ queryKey: ['phase-c-status', id] })
    } catch (e) { setError(e.response?.data?.detail || 'Assignment failed') }
  }

  const handleRemove = async (partnerId) => {
    try {
      await phaseCApi.removePartner(id, partnerId)
      refetchAssigned()
      qc.invalidateQueries({ queryKey: ['phase-c-status', id] })
    } catch (e) { setError(e.response?.data?.detail || 'Remove failed') }
  }

  // Open the unified panel pre-selecting the already-assigned banks. Pre-select
  // from current state immediately, then refetch and re-sync so banks assigned
  // moments ago (not yet in the 6s poll) still show as selected.
  const loadShipManifest = async () => {
    setManifestLoading(true)
    try {
      const res = await phaseCApi.shipManifest(id)
      const items = res.data?.items || []
      const next = {}
      for (const it of items) {
        next[it.doc_type] = {
          checked: false,
          generated: it.generated,
          override: it.override,
        }
      }
      setShipItems(next)
    } catch (e) {
      setError(e.response?.data?.detail || 'Failed to load ship manifest')
    } finally {
      setManifestLoading(false)
    }
  }

  const openShipPanel = async () => {
    setShipResult(null)
    setShipBankIds((assignedPartners || []).map(p => p.partner_id))
    setShipItems({})
    setShowShipPanel(true)
    // Kick off both in parallel — bank list refresh + manifest load.
    loadShipManifest()
    try {
      const fresh = await refetchAssigned()
      if (fresh?.data) setShipBankIds(fresh.data.map(p => p.partner_id))
    } catch { /* keep the optimistic selection */ }
  }

  const toggleShipItem = (docType) => {
    setShipItems(prev => {
      const cur = prev[docType]
      if (!cur) return prev
      return { ...prev, [docType]: { ...cur, checked: !cur.checked } }
    })
  }

  const setAllShipItems = (checked) => {
    setShipItems(prev => {
      const next = {}
      for (const [dt, item] of Object.entries(prev)) {
        // Only tickable items (generated OR override present) can be flipped on.
        const hasContent = !!(item.generated || item.override)
        next[dt] = { ...item, checked: checked && hasContent }
      }
      return next
    })
  }

  const handleStartAllCert = async () => {
    const eligible = (assignedPartners || []).filter(
      p => ['ready_for_certification', 'ready', 'certifying'].includes(p.status) && !p.blocked
    )
    if (!eligible.length) return
    setStartingAllCert(true)
    await Promise.all(eligible.map(p => phaseCApi.startCert(id, p.partner_id).catch(() => {})))
    qc.invalidateQueries({ queryKey: ['phase-c-progress-grid', id] })
    qc.invalidateQueries({ queryKey: ['phase-c-partners', id] })
    setStartingAllCert(false)
  }

  const handleShipKit = async () => {
    if (!shipBankIds.length) return
    const checkedItems = Object.entries(shipItems)
      .filter(([, v]) => v.checked)
      .map(([dt]) => dt)
    if (!checkedItems.length) {
      setError('Select at least one item to ship.')
      return
    }
    setShipping(true); setShipResult(null); setError(null)
    try {
      const res = await phaseCApi.shipKit(id, shipBankIds, shipVersion ?? currentVersion, checkedItems)
      setShipResult(res.data)
      setShowShipPanel(false)
      refetchAssigned()
      qc.invalidateQueries({ queryKey: ['phase-c-status', id] })
      qc.invalidateQueries({ queryKey: ['phase-c-partners', id] })
      qc.invalidateQueries({ queryKey: ['phase-c-messages', id] })
    } catch (e) { setError(e.response?.data?.detail || 'Ship failed') }
    finally { setShipping(false) }
  }

  // Phase-A gate for the direct URL. ChangeDetail already hides the entry
  // to this page (see ChangeDetail.jsx:3462), so this only fires when
  // someone deep-links /changes/:id/phase-c before Phase A is complete.
  if (change && change.status !== 'completed') {
    return (
      <PhaseALockedNotice
        changeId={id}
        changeTitle={change.title}
        phaseLabel="Phase C — Partner Collaboration"
        navigate={navigate}
      />
    )
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: 'calc(100vh - 0px)', overflow: 'hidden' }}>
      {/* Header */}
      <div style={{
        padding: '16px 24px', borderBottom: '1px solid var(--border)',
        display: 'flex', alignItems: 'center', gap: '16px', flexShrink: 0, background: 'var(--bg-base)',
      }}>
        <button onClick={() => navigate(`/changes/${id}`)} style={{
          display: 'flex', alignItems: 'center', gap: '5px', background: 'none',
          border: 'none', cursor: 'pointer', fontSize: '13px', color: 'var(--text-muted)',
        }}>
          <ArrowLeft size={14} /> Back
        </button>
        <div style={{ flex: 1 }}>
          <h1 style={{ margin: 0, fontSize: '16px', fontWeight: '600', color: 'var(--text-primary)' }}>
            Phase C — Partner Collaboration
          </h1>
          <p style={{ margin: 0, fontSize: '11px', color: 'var(--text-muted)' }}>
            {change?.title || 'Loading…'}
          </p>
        </div>
        <TranscriptsDownloadButton changeId={id} />
        <button
          type="button"
          onClick={handleSync}
          disabled={changeFetching}
          title="Pull the latest state from every partner"
          style={{
            display: 'inline-flex', alignItems: 'center', gap: 6,
            padding: '6px 10px', fontSize: 12, fontWeight: 500,
            color: 'var(--text-secondary)', background: 'var(--bg-base)',
            border: '1px solid var(--border)', borderRadius: 6,
            cursor: changeFetching ? 'wait' : 'pointer',
            opacity: changeFetching ? 0.6 : 1,
          }}
        >
          <RefreshCw size={13} style={{ animation: changeFetching ? 'spin 1s linear infinite' : 'none' }} />
          {changeFetching ? 'Syncing…' : 'Sync'}
        </button>
        {/* Eval verdict pill for the Product-Kit→Phase-C gate. The gate itself
            runs on the /communicate endpoint; main's ship-kit panel is the
            current send flow (ungated — see phase_c.ship_kit TODO). */}
        <EvalStatusPill
          verdict={gateEval?.verdict}
          checkpointLabel="Product Kit to Phase C communication gate"
          changeId={id}
          checkpointId="product_kit_to_phase_c_communication"
        />
        {(assignedPartners || []).length === 0 ? (
          <button
            onClick={() => setShowAssign(true)}
            title="Assign partners first before communicating the change"
            style={{
              display: 'flex', alignItems: 'center', gap: '6px',
              padding: '8px 18px', background: 'var(--bg-elevated)', color: 'var(--text-muted)',
              border: '1px solid var(--border)', borderRadius: '6px', fontSize: '13px', fontWeight: '600',
              cursor: 'pointer',
            }}>
            <Send size={13} /> Communicate Change
          </button>
        ) : (
          <button
            onClick={openShipPanel}
            title="Select banks and a kit version, then communicate to all selected at once"
            style={{
              display: 'flex', alignItems: 'center', gap: '6px',
              padding: '8px 18px', background: 'var(--accent)', color: 'white',
              border: 'none', borderRadius: '6px', fontSize: '13px', fontWeight: '600',
              cursor: 'pointer',
            }}>
            <Send size={13} /> Communicate Change
          </button>
        )}
      </div>

      {/* ── Attention Required strip ──────────────────────────────────────────
          Chips appear only when there is something needing PM action.
          Hidden when all counts are zero. Clicking a chip opens and scrolls
          to the relevant section. */}
      {totalAttentionItems > 0 && (
        <div style={{
          display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap',
          padding: '8px 24px', borderBottom: '1px solid var(--border-subtle)',
          background: 'var(--bg-elevated)', flexShrink: 0,
        }}>
          <span style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.05em', marginRight: '4px' }}>
            Needs attention
          </span>
          {pendingQueriesCount > 0 && (
            <button
              onClick={() => { setShowNegotiations(true); messagingRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }) }}
              style={attentionChip('var(--accent)', 'rgba(218,119,86,0.12)', 'rgba(218,119,86,0.3)')}
            >
              <AlertCircle size={11} />
              {pendingQueriesCount} pending quer{pendingQueriesCount === 1 ? 'y' : 'ies'}
            </button>
          )}
          {openCountersTotal > 0 && (
            <button
              onClick={() => { setShowNegotiations(true); messagingRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }) }}
              style={attentionChip('#64a0c8', 'rgba(100,160,200,0.12)', 'rgba(100,160,200,0.3)')}
            >
              <Edit3 size={11} />
              {openCountersTotal} open counter{openCountersTotal === 1 ? '' : 's'}
            </button>
          )}
          {openBlockersTotal > 0 && (
            <button
              onClick={() => { setShowNegotiations(true); messagingRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }) }}
              style={attentionChip('var(--danger)', 'rgba(239,68,68,0.10)', 'rgba(239,68,68,0.3)')}
            >
              <Ban size={11} />
              {openBlockersTotal} blocker{openBlockersTotal === 1 ? '' : 's'}
            </button>
          )}
          {readyForCertCount > 0 && (
            <button
              onClick={() => { setShowCertification(true); certRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }) }}
              style={attentionChip('var(--success)', 'rgba(76,175,125,0.12)', 'rgba(76,175,125,0.3)')}
            >
              <ShieldCheck size={11} />
              {readyForCertCount} ready for cert
            </button>
          )}
          {negStage === 'round_closed' && (
            <button
              onClick={() => negotiationHubRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })}
              style={attentionChip('var(--accent)', 'rgba(218,119,86,0.10)', 'rgba(218,119,86,0.3)')}
            >
              <AlertTriangle size={11} />
              Round closed — choose next step
            </button>
          )}
          {negStage === 'negotiating' && (
            <button
              onClick={() => negotiationHubRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })}
              style={attentionChip('#64a0c8', 'rgba(100,160,200,0.08)', 'rgba(100,160,200,0.25)')}
            >
              <BarChart2 size={11} />
              Round open — banks responding
            </button>
          )}
        </div>
      )}

      {/* ── Unified "Communicate Change" panel ─────────────────────────────────
          Pick banks (any not yet assigned are assigned on ship) + an existing
          kit version, then communicate to all selected at once. New-version
          generation is intentionally NOT here — parked for later. */}
      {showShipPanel && (
        <div
          onClick={() => setShowShipPanel(false)}
          style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.45)', zIndex: 1000, display: 'flex', alignItems: 'center', justifyContent: 'center' }}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            style={{ width: 720, maxWidth: '92vw', maxHeight: '88vh', overflow: 'auto', background: 'var(--bg-elevated)', border: '1px solid var(--border)', borderRadius: '12px', padding: '20px 22px' }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '16px' }}>
              <Send size={16} style={{ color: 'var(--accent)' }} />
              <span style={{ fontSize: '15px', fontWeight: '700', color: 'var(--text-primary)', flex: 1 }}>Communicate Change</span>
              <button onClick={() => setShowShipPanel(false)} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-muted)' }}><X size={16} /></button>
            </div>

            {/* Prerequisite checklist */}
            <div style={{
              marginBottom: '16px', padding: '10px 14px', borderRadius: '8px',
              background: 'var(--bg-base)', border: '1px solid var(--border-subtle)',
              display: 'flex', flexDirection: 'column', gap: '6px',
            }}>
              {[
                {
                  ok: (assignedPartners || []).length > 0,
                  label: `Partners assigned (${(assignedPartners || []).length})`,
                  warn: 'No partners assigned yet.',
                },
                {
                  ok: availableVersions.length > 0 && Object.values(shipItems).some(it => !!(it.generated || it.override)),
                  label: `Kit documents exist (v${currentVersion})`,
                  warn: 'No kit documents found.',
                },
                {
                  ok: gateEval?.verdict === 'pass',
                  warn: gateEval?.verdict === 'fail' ? 'Eval gate failed — review before shipping.' : 'Eval gate not yet run.',
                  label: gateEval?.verdict === 'pass' ? 'Eval gate passed' : gateEval?.verdict === 'fail' ? 'Eval gate failed' : 'Eval gate: not yet run',
                  isWarn: !gateEval || gateEval.verdict !== 'pass',
                },
              ].map((item, i) => (
                <div key={i} style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '12px' }}>
                  {item.ok
                    ? <CheckCircle size={13} style={{ color: 'var(--success)', flexShrink: 0 }} />
                    : <AlertCircle size={13} style={{ color: item.isWarn ? 'var(--text-muted)' : 'var(--accent)', flexShrink: 0 }} />
                  }
                  <span style={{ color: item.ok ? 'var(--text-primary)' : 'var(--text-secondary)' }}>
                    {item.label}
                  </span>
                </div>
              ))}
            </div>

            {/* Version */}
            <div style={{ marginBottom: '16px' }}>
              <label style={{ fontSize: '12px', fontWeight: '600', color: 'var(--text-muted)', display: 'block', marginBottom: '6px' }}>Kit version to ship</label>
              <select
                value={shipVersion ?? currentVersion}
                onChange={(e) => setShipVersion(Number(e.target.value))}
                style={{ padding: '8px 10px', borderRadius: '6px', border: '1px solid var(--border)', background: 'var(--bg-base)', color: 'var(--text-primary)', fontSize: '13px', cursor: 'pointer', minWidth: '180px' }}
              >
                {availableVersions.map(v => (
                  <option key={v} value={v}>{v === currentVersion ? `v${v} (latest)` : `v${v}`}</option>
                ))}
              </select>
              {availableVersions.length === 1 && (
                <span style={{ fontSize: '11px', color: 'var(--text-muted)', marginLeft: '10px' }}>Only v1 exists so far.</span>
              )}
            </div>

            {/* Items to ship — per-doc-type checkbox list with optional file
                overrides. All rows start UNCHECKED; user picks explicitly or
                clicks Select all. If an item has an override upload, that file
                ships in place of the generated one. */}
            <div style={{ marginBottom: '18px' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '8px' }}>
                <label style={{ fontSize: '12px', fontWeight: '600', color: 'var(--text-muted)', flex: 1 }}>
                  Items to ship — {Object.values(shipItems).filter(it => it.checked).length} selected
                </label>
                <button
                  type="button"
                  onClick={() => setAllShipItems(true)}
                  disabled={manifestLoading}
                  style={{ padding: '4px 10px', fontSize: 11, borderRadius: 5, border: '1px solid var(--border)', background: 'var(--bg-base)', color: 'var(--text-secondary)', cursor: 'pointer' }}
                >Select all</button>
                <button
                  type="button"
                  onClick={() => setAllShipItems(false)}
                  disabled={manifestLoading}
                  style={{ padding: '4px 10px', fontSize: 11, borderRadius: 5, border: '1px solid var(--border)', background: 'var(--bg-base)', color: 'var(--text-secondary)', cursor: 'pointer' }}
                >Clear all</button>
              </div>
              <div style={{ maxHeight: 280, overflowY: 'auto', border: '1px solid var(--border-subtle)', borderRadius: 6, background: 'var(--bg-base)' }}>
                {manifestLoading && (
                  <div style={{ padding: 12, fontSize: 12, color: 'var(--text-muted)', display: 'flex', alignItems: 'center', gap: 6 }}>
                    <Loader size={12} style={{ animation: 'spin 1s linear infinite' }} /> Loading items…
                  </div>
                )}
                {!manifestLoading && Object.keys(shipItems).length === 0 && (
                  <div style={{ padding: 12, fontSize: 12, color: 'var(--text-muted)' }}>No ship-eligible items.</div>
                )}
                {!manifestLoading && Object.entries(shipItems).map(([dt, item]) => {
                  const label = kitDocLabel(dt)
                  const hasContent = !!(item.generated || item.override)
                  const statusLabel = item.override
                    ? `override: ${item.override.filename}${item.override.size_bytes ? ` · ${formatKB(item.override.size_bytes)}` : ''}`
                    : item.generated
                      ? `v${item.generated.version} · will ship generated`
                      : 'not generated'
                  return (
                    <div key={dt} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '8px 12px', borderBottom: '1px solid var(--border-subtle)' }}>
                      <input
                        type="checkbox"
                        checked={!!item.checked}
                        disabled={!hasContent}
                        onChange={() => toggleShipItem(dt)}
                        title={hasContent ? '' : 'Nothing to ship for this item'}
                        style={{ cursor: hasContent ? 'pointer' : 'not-allowed' }}
                      />
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ fontSize: 13, fontWeight: 500, color: 'var(--text-primary)' }}>{label}</div>
                        <div style={{ fontSize: 11, color: item.override ? 'var(--accent)' : 'var(--text-muted)', display: 'flex', alignItems: 'center', gap: 4 }}>
                          {item.override && <Paperclip size={10} />}
                          {statusLabel}
                        </div>
                      </div>
                    </div>
                  )
                })}
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 6 }}>
                <span style={{ fontSize: 11, color: 'var(--text-muted)', flex: 1 }}>
                  Uploaded overrides ship in place of the generated file.
                </span>
                <button
                  type="button"
                  onClick={() => { setShowShipPanel(false); navigate(`/product-kit/${id}`) }}
                  style={{
                    display: 'inline-flex', alignItems: 'center', gap: 4,
                    padding: '4px 8px', fontSize: 11,
                    background: 'transparent', border: 'none',
                    color: 'var(--accent)', cursor: 'pointer',
                  }}
                >
                  <Package size={11} /> Manage overrides in Product Kit →
                </button>
              </div>
            </div>

            {/* Banks */}
            <div style={{ marginBottom: '18px' }}>
              <label style={{ fontSize: '12px', fontWeight: '600', color: 'var(--text-muted)', display: 'block', marginBottom: '6px' }}>
                Banks — {shipBankIds.length} selected · already-assigned marked ✓
              </label>
              {(allPartners || []).filter(p => p.status === 'active').length === 0 ? (
                <p style={{ fontSize: '12px', color: 'var(--text-muted)', margin: 0 }}>No active partners available.</p>
              ) : (
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px' }}>
                  {(allPartners || []).filter(p => p.status === 'active').map(p => {
                    const meta = TYPE_META[p.partner_type] || TYPE_META.bank
                    const selected = shipBankIds.includes(p.id)
                    const isAssigned = assignedIds.has(p.id)
                    return (
                      <button
                        key={p.id}
                        onClick={() => setShipBankIds(prev => selected ? prev.filter(x => x !== p.id) : [...prev, p.id])}
                        style={{
                          padding: '6px 12px', borderRadius: '6px', fontSize: '12px', cursor: 'pointer',
                          background: selected ? `${meta.color}20` : 'var(--bg-base)',
                          border: `1px solid ${selected ? meta.color : 'var(--border)'}`,
                          color: selected ? meta.color : 'var(--text-secondary)',
                          fontWeight: selected ? '600' : '400',
                        }}
                      >
                        {p.name} ({meta.label}){isAssigned ? ' ✓' : ''}
                      </button>
                    )
                  })}
                </div>
              )}
            </div>

            {shipResult && (
              <div style={{ fontSize: '12px', color: 'var(--success)', marginBottom: '12px', display: 'inline-flex', alignItems: 'center', gap: '5px' }}>
                <Check size={12} /> Shipped v{shipResult.negotiation_version} to {shipResult.partners_notified} bank{shipResult.partners_notified !== 1 ? 's' : ''}{shipResult.partners_skipped ? ` (${shipResult.partners_skipped} skipped)` : ''}.
              </div>
            )}

            <div style={{ display: 'flex', gap: '8px', justifyContent: 'flex-end' }}>
              <button onClick={() => setShowShipPanel(false)} style={{ padding: '8px 14px', borderRadius: '6px', border: '1px solid var(--border)', background: 'transparent', color: 'var(--text-secondary)', fontSize: '13px', cursor: 'pointer' }}>Close</button>
              <button
                onClick={handleShipKit}
                disabled={shipping || !shipBankIds.length || !Object.values(shipItems).some(it => it.checked)}
                style={{
                  padding: '8px 18px', borderRadius: '6px', border: 'none', background: 'var(--accent)', color: 'white', fontSize: '13px', fontWeight: '600',
                  cursor: (shipping || !shipBankIds.length || !Object.values(shipItems).some(it => it.checked)) ? 'not-allowed' : 'pointer',
                  opacity: (shipping || !shipBankIds.length || !Object.values(shipItems).some(it => it.checked)) ? 0.5 : 1,
                  display: 'inline-flex', alignItems: 'center', gap: '6px',
                }}
              >
                {shipping ? <Loader size={13} style={{ animation: 'spin 1s linear infinite' }} /> : <Send size={13} />}
                Ship {Object.values(shipItems).filter(it => it.checked).length} item{Object.values(shipItems).filter(it => it.checked).length === 1 ? '' : 's'} to {shipBankIds.length} bank{shipBankIds.length !== 1 ? 's' : ''}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Main content */}
      <div style={{ flex: 1, overflowY: 'auto', padding: '24px' }}>
        {error && (
          <div style={{ padding: '10px 16px', borderRadius: '8px', marginBottom: '16px', background: 'rgba(224,108,108,0.08)', border: '1px solid rgba(224,108,108,0.25)', display: 'flex', alignItems: 'center', gap: '8px' }}>
            <AlertCircle size={14} style={{ color: 'var(--danger)' }} />
            <span style={{ fontSize: '13px', color: 'var(--danger)', flex: 1 }}>{error}</span>
            <button onClick={() => setError(null)} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-muted)' }}><X size={14} /></button>
          </div>
        )}

        {shipResult && (
          <div style={{ padding: '12px 16px', borderRadius: '8px', marginBottom: '16px', background: 'rgba(76,175,125,0.08)', border: '1px solid rgba(76,175,125,0.25)', display: 'flex', alignItems: 'center', gap: '8px' }}>
            <CheckCircle size={14} style={{ color: 'var(--success)' }} />
            <span style={{ fontSize: '13px', color: 'var(--success)' }}>
              Kit v{shipResult.negotiation_version} shipped to {shipResult.partners_notified} bank{shipResult.partners_notified !== 1 ? 's' : ''}
              {shipResult.partners_skipped ? ` · ${shipResult.partners_skipped} skipped` : ''}
            </span>
          </div>
        )}

        {/* Partner Assignment */}
        <div style={{ marginBottom: '24px', borderRadius: '8px', background: 'var(--bg-elevated)', border: '1px solid var(--border)', overflow: 'hidden' }}>
          <div style={{
            padding: '14px 20px', borderBottom: '1px solid var(--border-subtle)',
            display: 'flex', alignItems: 'center', gap: '10px',
          }}>
            <Users size={16} style={{ color: 'var(--accent)' }} />
            <span style={{ fontSize: '14px', fontWeight: '600', color: 'var(--text-primary)', flex: 1 }}>
              Assigned Partners ({(assignedPartners || []).length})
            </span>
            <button onClick={() => setShowAssign(v => !v)} style={{
              display: 'flex', alignItems: 'center', gap: '5px',
              padding: '5px 12px', background: 'var(--accent)', color: 'white',
              border: 'none', borderRadius: '5px', fontSize: '11px', fontWeight: '600', cursor: 'pointer',
            }}>
              <Plus size={11} /> Assign Partner
            </button>
          </div>

          {/* Assign form */}
          {showAssign && (
            <div style={{ padding: '14px 20px', borderBottom: '1px solid var(--border-subtle)', background: 'var(--bg-base)' }}>
              {availablePartners.length === 0 ? (
                <p style={{ margin: 0, fontSize: '12px', color: 'var(--text-muted)' }}>All active partners are already assigned.</p>
              ) : (
                <>
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px', marginBottom: '10px' }}>
                    {availablePartners.map(p => {
                      const meta = TYPE_META[p.partner_type] || TYPE_META.bank
                      const selected = selectedPartners.includes(p.id)
                      return (
                        <button key={p.id} onClick={() => {
                          setSelectedPartners(prev => selected ? prev.filter(x => x !== p.id) : [...prev, p.id])
                        }} style={{
                          padding: '6px 12px', borderRadius: '6px', fontSize: '12px', cursor: 'pointer',
                          background: selected ? `${meta.color}20` : 'var(--bg-elevated)',
                          border: `1px solid ${selected ? meta.color : 'var(--border)'}`,
                          color: selected ? meta.color : 'var(--text-secondary)',
                          fontWeight: selected ? '600' : '400',
                        }}>
                          {p.name} ({meta.label})
                        </button>
                      )
                    })}
                  </div>
                  <button onClick={handleAssign} disabled={!selectedPartners.length} style={{
                    padding: '6px 14px', background: 'var(--accent)', color: 'white',
                    border: 'none', borderRadius: '5px', fontSize: '12px', fontWeight: '600',
                    cursor: selectedPartners.length ? 'pointer' : 'not-allowed', opacity: selectedPartners.length ? 1 : 0.5,
                  }}>
                    Assign Selected ({selectedPartners.length})
                  </button>
                </>
              )}
            </div>
          )}

          {/* Partner list */}
          {(!assignedPartners || assignedPartners.length === 0) && (
            <div style={{ padding: '32px', textAlign: 'center' }}>
              <p style={{ margin: 0, fontSize: '13px', color: 'var(--text-muted)' }}>
                No partners assigned yet. Click "Assign Partner" to add banks, PSPs, or TPAPs.
              </p>
            </div>
          )}
          {(assignedPartners || []).map((p, i) => {
            const meta = TYPE_META[p.partner_type] || TYPE_META.bank
            const sc = STATUS_COLORS[p.status] || STATUS_COLORS.assigned
            const Icon = meta.icon
            const ack = p.acceptance_meta?.acknowledged
            const acc = p.acceptance_meta?.accepted
            const verifiedCount = (ack?.kit_files_received || []).filter(f => f.checksum_verified).length
            const totalCount = (ack?.kit_files_received || []).length
            const allVerified = totalCount > 0 && verifiedCount === totalCount
            return (
              <div key={p.partner_id} style={{
                padding: '12px 20px',
                borderBottom: i < assignedPartners.length - 1 ? '1px solid var(--border-subtle)' : 'none',
              }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                  <Icon size={14} style={{ color: meta.color, flexShrink: 0 }} />
                  <span style={{ fontSize: '13px', color: 'var(--text-primary)', fontWeight: '500', flex: 1 }}>{p.name}</span>
                  <span style={{
                    fontSize: '11px', padding: '2px 8px', borderRadius: '4px', fontWeight: '600',
                    background: `${meta.color}15`, color: meta.color, border: `1px solid ${meta.color}30`,
                    textTransform: 'uppercase',
                  }}>{meta.label}</span>
                  <span style={{
                    fontSize: '11px', padding: '2px 10px', borderRadius: '20px', fontWeight: '600',
                    background: sc.bg, color: sc.color, border: `1px solid ${sc.color}30`,
                  }}>{sc.label}</span>
                  {p.status === 'assigned' && (
                    <button onClick={() => handleRemove(p.partner_id)} style={{
                      padding: '4px 8px', background: 'transparent', border: '1px solid var(--border)',
                      borderRadius: '4px', color: 'var(--danger)', cursor: 'pointer',
                    }}><Trash2 size={11} /></button>
                  )}

                </div>

                {/* Rollout-contract metadata badges. Auto-ack receipt =
                    file checksums verified by partner. Accept = explicit
                    PROPOSAL_ACCEPTANCE with named approver / CAB ref.
                    Counter / blocker badges flag PM-action-needed. */}
                {(ack || acc || (p.open_counters_count || 0) > 0 || (p.open_blockers_count || 0) > 0) && (
                  <div style={{ display: 'flex', gap: '8px', marginTop: '8px', flexWrap: 'wrap', paddingLeft: '26px' }}>
                    {(p.open_counters_count || 0) > 0 && (
                      <span style={{
                        display: 'inline-flex', alignItems: 'center', gap: '4px',
                        fontSize: '11px', padding: '3px 8px', borderRadius: '4px',
                        background: 'rgba(218,119,86,0.15)', color: 'var(--accent)',
                        border: '1px solid rgba(218,119,86,0.35)', fontWeight: 700,
                      }}>
                        <Edit3 size={10} /> {p.open_counters_count} open counter{p.open_counters_count > 1 ? 's' : ''}
                      </span>
                    )}
                    {(p.open_blockers_count || 0) > 0 && (
                      <span style={{
                        display: 'inline-flex', alignItems: 'center', gap: '4px',
                        fontSize: '11px', padding: '3px 8px', borderRadius: '4px',
                        background: 'rgba(220,53,69,0.12)', color: 'var(--danger)',
                        border: '1px solid rgba(220,53,69,0.35)', fontWeight: 700,
                      }}>
                        <Shield size={10} /> {p.open_blockers_count} blocker{p.open_blockers_count > 1 ? 's' : ''}
                      </span>
                    )}
                    {ack && (
                      <span
                        title={
                          totalCount
                            ? `${verifiedCount}/${totalCount} files verified — received ${ack.received_at || ''}`
                            : 'Auto-acknowledged on receipt'
                        }
                        style={{
                          display: 'inline-flex', alignItems: 'center', gap: '4px',
                          fontSize: '11px', padding: '3px 8px', borderRadius: '4px',
                          background: allVerified ? 'rgba(76,175,125,0.08)' : 'rgba(218,119,86,0.08)',
                          color: allVerified ? 'var(--success)' : 'var(--accent)',
                          border: `1px solid ${allVerified ? 'rgba(76,175,125,0.25)' : 'rgba(218,119,86,0.25)'}`,
                        }}
                      >
                        <FileCheck size={10} />
                        {totalCount
                          ? `Auto-ack: ${verifiedCount}/${totalCount} verified`
                          : 'Auto-acknowledged'}
                      </span>
                    )}
                    {acc && (
                      <span
                        title={
                          [
                            acc.accepted_by?.name && `By: ${acc.accepted_by.name}`,
                            acc.accepted_at && `At: ${new Date(acc.accepted_at).toLocaleString()}`,
                            acc.internal_change_advisory_ref && `CAB: ${acc.internal_change_advisory_ref}`,
                            acc.implementation_kickoff_date && `Kickoff: ${acc.implementation_kickoff_date}`,
                          ].filter(Boolean).join(' • ')
                        }
                        style={{
                          display: 'inline-flex', alignItems: 'center', gap: '4px',
                          fontSize: '11px', padding: '3px 8px', borderRadius: '4px',
                          background: 'rgba(76,175,125,0.10)', color: 'var(--success)',
                          border: '1px solid rgba(76,175,125,0.25)',
                        }}
                      >
                        <Check size={10} />
                        Accepted{acc.accepted_by?.name ? ` by ${acc.accepted_by.name}` : ''}
                      </span>
                    )}
                    {acc?.implementation_kickoff_date && (
                      <span style={{
                        display: 'inline-flex', alignItems: 'center', gap: '4px',
                        fontSize: '11px', padding: '3px 8px', borderRadius: '4px',
                        background: 'var(--bg-elevated)', color: 'var(--text-muted)',
                        border: '1px solid var(--border)',
                      }}>
                        <Calendar size={10} />
                        Kickoff {acc.implementation_kickoff_date}
                      </span>
                    )}
                  </div>
                )}

              </div>
            )
          })}
        </div>

        {/* Negotiation Hub — BRD requirements, round status, clusters, governance.
            Surfaced first: its mandatory-requirement config powers the auto-reject
            pipeline that runs on every partner message below. Collapsed by default. */}
        {(assignedPartners || []).some(p => p.status !== 'assigned') && (
          <div ref={negotiationHubRef}>
            <NegotiationHub changeId={id} />
          </div>
        )}

        {/* Partner messaging — master/detail (search + list on left,
            chat on right). Sized by viewport so the chat fills space
            instead of stacking N panels. */}
        {(() => {
          const chatPartners = (assignedPartners || []).filter(p => p.status !== 'assigned')
          if (chatPartners.length === 0) return null

          // Per-partner pending counts (drives the unread badges in the
          // left rail). Sum: pending QUERY messages + open counter
          // proposals + open blockers — each is something a PM needs
          // to action.
          const pendingByPartner = {}
          ;(messages || []).forEach(m => {
            if (m.task_type === 'query' && m.status === 'submitted' && m.direction === 'inbound') {
              pendingByPartner[m.partner_id] = (pendingByPartner[m.partner_id] || 0) + 1
            }
          })
          chatPartners.forEach(p => {
            const cps = p.open_counters_count || 0
            const blks = p.open_blockers_count || 0
            if (cps + blks > 0) {
              pendingByPartner[p.partner_id] = (pendingByPartner[p.partner_id] || 0) + cps + blks
            }
          })
          const totalPending = Object.values(pendingByPartner).reduce((a, b) => a + b, 0)

          // Auto-select: if nothing selected, prefer the first partner
          // with a pending message; otherwise the first partner.
          const selected = selectedPartnerId && chatPartners.find(p => p.partner_id === selectedPartnerId)
            ? selectedPartnerId
            : (chatPartners.find(p => pendingByPartner[p.partner_id] > 0)?.partner_id
               || chatPartners[0]?.partner_id)
          const selectedPartner = chatPartners.find(p => p.partner_id === selected)

          return (
            <div ref={messagingRef} style={{
              marginBottom: '24px', borderRadius: '8px',
              background: 'var(--bg-elevated)', border: '1px solid var(--border)',
              overflow: 'hidden',
            }}>
              <div
                onClick={() => setShowNegotiations(v => !v)}
                style={{
                  padding: '14px 20px', borderBottom: showNegotiations ? '1px solid var(--border-subtle)' : 'none',
                  display: 'flex', alignItems: 'center', gap: '10px', cursor: 'pointer', userSelect: 'none',
                }}
              >
                {showNegotiations ? <ChevronDown size={14} style={{ color: 'var(--text-muted)' }} /> : <ChevronRight size={14} style={{ color: 'var(--text-muted)' }} />}
                <Bot size={16} style={{ color: 'var(--accent)' }} />
                <span style={{ fontSize: '14px', fontWeight: '600', color: 'var(--text-primary)', flex: 1 }}>
                  Partner Messaging
                </span>
                <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>
                  {chatPartners.length} partner{chatPartners.length !== 1 ? 's' : ''}
                </span>
                {totalPending > 0 && (
                  <span style={{
                    fontSize: '11px', padding: '3px 10px', borderRadius: '20px', fontWeight: '600',
                    background: 'rgba(218,119,86,0.12)', color: 'var(--accent)',
                    border: '1px solid rgba(218,119,86,0.3)',
                  }}>
                    {totalPending} pending
                  </span>
                )}
              </div>

              {showNegotiations && (
                <div style={{
                  display: 'grid',
                  gridTemplateColumns: `${chatListCollapsed ? '52px' : '260px'} 1fr`,
                  height: 'min(640px, calc(100vh - 180px))',
                  minHeight: '420px',
                  transition: 'grid-template-columns 0.2s ease',
                }}>
                  <PartnerChatList
                    partners={chatPartners}
                    pendingByPartner={pendingByPartner}
                    selectedPartnerId={selected}
                    onSelect={setSelectedPartnerId}
                    collapsed={chatListCollapsed}
                    onToggleCollapse={() => setChatListCollapsed(v => !v)}
                  />
                  <div style={{ display: 'flex', flexDirection: 'column', minHeight: 0 }}>
                    {selectedPartner ? (
                      <NegotiationPanel
                        key={selectedPartner.partner_id}
                        changeId={id}
                        partnerId={selectedPartner.partner_id}
                        partnerName={selectedPartner.name}
                        pendingMessages={(messages || []).filter(m => m.partner_id === selectedPartner.partner_id)}
                      />
                    ) : (
                      <div style={{
                        flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center',
                        color: 'var(--text-muted)', fontSize: '12px',
                      }}>
                        Select a partner from the list to view the conversation.
                      </div>
                    )}
                  </div>
                </div>
              )}
            </div>
          )
        })()}

        {/* Progress Grid */}
        {progressGrid?.partners?.length > 0 && (
          <div style={{ marginBottom: '24px', borderRadius: '8px', background: 'var(--bg-elevated)', border: '1px solid var(--border)', overflow: 'hidden' }}>
            <div style={{ padding: '14px 20px', borderBottom: '1px solid var(--border-subtle)', display: 'flex', alignItems: 'center', gap: '10px' }}>
              <CheckCircle size={16} style={{ color: 'var(--accent)' }} />
              <span style={{ fontSize: '14px', fontWeight: '600', color: 'var(--text-primary)', flex: 1 }}>
                Implementation Progress
              </span>
              <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>
                Click a partner name to open their conversation · ▶ to see milestone detail
              </span>
            </div>
            <div style={{ overflowX: 'auto' }}>
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '12px' }}>
                <thead>
                  <tr style={{ background: 'var(--bg-base)', borderBottom: '1px solid var(--border)' }}>
                    <th style={{ width: '28px', padding: '8px 0 8px 12px' }} />
                    <th style={{ padding: '8px 16px', textAlign: 'left', fontWeight: '600', color: 'var(--text-muted)', fontSize: '11px', textTransform: 'uppercase' }}>Partner</th>
                    <th style={{ padding: '8px 12px', textAlign: 'left', fontWeight: '600', color: 'var(--text-muted)', fontSize: '11px', textTransform: 'uppercase' }}>Status</th>
                    <th style={{ padding: '8px 12px', textAlign: 'left', fontWeight: '600', color: 'var(--text-muted)', fontSize: '11px', textTransform: 'uppercase' }}>Last Updated</th>
                    <th style={{ padding: '8px 12px', textAlign: 'left', fontWeight: '600', color: 'var(--text-muted)', fontSize: '11px', textTransform: 'uppercase' }}>Action</th>
                  </tr>
                </thead>
                <tbody>
                  {progressGrid.partners.flatMap((p, i) => {
                    const meta = TYPE_META[p.partner_type] || TYPE_META.bank
                    const ap = (assignedPartners || []).find(a => a.partner_id === p.partner_id)
                    const statusKey = ap?.status || (p.status !== 'assigned' ? 'communicated' : 'assigned')
                    const sc = STATUS_COLORS[statusKey] || STATUS_COLORS.assigned
                    const isLast = i === progressGrid.partners.length - 1
                    const expanded = expandedGridRows.has(p.partner_id)
                    const toggleExpanded = () => setExpandedGridRows(prev => {
                      const next = new Set(prev)
                      next.has(p.partner_id) ? next.delete(p.partner_id) : next.add(p.partner_id)
                      return next
                    })
                    const updatedAt = ap?.updated_at
                    const updatedStr = updatedAt
                      ? new Date(updatedAt).toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: '2-digit' })
                      : '—'
                    const counters = ap?.open_counters_count || 0
                    const blockers = ap?.open_blockers_count || 0
                    const isReadyForCert = ['ready_for_certification', 'ready', 'certifying'].includes(statusKey)
                    const rowBorder = (!expanded && !isLast) ? '1px solid var(--border-subtle)' : 'none'

                    const mainRow = (
                      <tr key={p.partner_id} style={{ borderBottom: rowBorder }}>
                        <td style={{ padding: '10px 0 10px 12px' }}>
                          <button
                            onClick={toggleExpanded}
                            title={expanded ? 'Hide milestones' : 'Show milestones'}
                            style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-muted)', padding: '2px', display: 'flex', alignItems: 'center' }}
                          >
                            {expanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                          </button>
                        </td>
                        <td style={{ padding: '10px 16px' }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                            <button
                              onClick={() => {
                                setShowNegotiations(true)
                                setSelectedPartnerId(p.partner_id)
                                messagingRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
                              }}
                              style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 0, fontWeight: '600', color: 'var(--accent)', fontSize: '13px', textDecoration: 'underline', textUnderlineOffset: '2px' }}
                            >
                              {p.name}
                            </button>
                            <span style={{
                              fontSize: '10px', padding: '1px 6px', borderRadius: '3px',
                              background: `${meta.color}15`, color: meta.color, fontWeight: '600',
                              textTransform: 'uppercase',
                            }}>{meta.label}</span>
                          </div>
                        </td>
                        <td style={{ padding: '10px 12px' }}>
                          <span style={{
                            display: 'inline-flex', alignItems: 'center',
                            padding: '3px 10px', borderRadius: '20px',
                            fontSize: '11px', fontWeight: '600',
                            background: sc.bg, color: sc.color,
                          }}>
                            {sc.label}
                          </span>
                        </td>
                        <td style={{ padding: '10px 12px', color: 'var(--text-muted)', fontSize: '12px', whiteSpace: 'nowrap' }}>
                          {updatedStr}
                        </td>
                        <td style={{ padding: '10px 12px' }}>
                          <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap' }}>
                            {isReadyForCert && (
                              <button
                                onClick={() => { setShowCertification(true); certRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }) }}
                                style={{ ...btn('var(--success)'), fontSize: '11px' }}
                              >
                                <ShieldCheck size={10} /> Start cert
                              </button>
                            )}
                            {counters > 0 && (
                              <button
                                onClick={() => { setShowNegotiations(true); setSelectedPartnerId(p.partner_id); messagingRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }) }}
                                style={{ ...btn('#64a0c8'), fontSize: '11px' }}
                              >
                                <Edit3 size={10} /> {counters} counter{counters !== 1 ? 's' : ''}
                              </button>
                            )}
                            {blockers > 0 && (
                              <button
                                onClick={() => { setShowNegotiations(true); setSelectedPartnerId(p.partner_id); messagingRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }) }}
                                style={{ ...btn('var(--danger)'), fontSize: '11px' }}
                              >
                                <Ban size={10} /> {blockers} blocker{blockers !== 1 ? 's' : ''}
                              </button>
                            )}
                          </div>
                        </td>
                      </tr>
                    )

                    if (!expanded) return [mainRow]

                    const detailRow = (
                      <tr key={`${p.partner_id}-detail`} style={{ borderBottom: !isLast ? '1px solid var(--border-subtle)' : 'none', background: 'var(--bg-base)' }}>
                        <td />
                        <td colSpan={4} style={{ padding: '8px 16px 12px' }}>
                          <div style={{ display: 'flex', gap: '20px', alignItems: 'center', flexWrap: 'wrap' }}>
                            {[
                              { label: 'Communicated', done: p.status !== 'assigned' },
                              { label: 'Accepted',      done: p.accepted },
                              { label: 'Design',        done: p.design_completed },
                              { label: 'Coding',        done: p.coding_completed },
                              { label: 'Testing',       done: p.testing_completed },
                              { label: 'Ready for Cert', done: p.ready_for_cert },
                              { label: 'Certified',     done: p.certified },
                            ].map(({ label, done }) => (
                              <div key={label} style={{ display: 'flex', alignItems: 'center', gap: '5px' }}>
                                {done
                                  ? <CheckCircle size={13} style={{ color: 'var(--success)', flexShrink: 0 }} />
                                  : <Circle size={13} style={{ color: 'var(--border)', flexShrink: 0 }} />}
                                <span style={{ fontSize: '11px', color: done ? 'var(--text-primary)' : 'var(--text-muted)' }}>{label}</span>
                              </div>
                            ))}
                          </div>
                        </td>
                      </tr>
                    )

                    return [mainRow, detailRow]
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* Certification panels — one per ready/certified partner (collapsed by default) */}
        {(assignedPartners || []).filter(p => ['ready', 'certified', 'in_progress', 'communicated', 'acknowledged'].includes(p.status)).length > 0 && (
          <div ref={certRef} style={{ marginBottom: '24px', borderRadius: '8px', background: 'var(--bg-elevated)', border: '1px solid var(--border)', overflow: 'hidden' }}>
            <div style={{
              padding: '14px 20px', borderBottom: showCertification ? '1px solid var(--border-subtle)' : 'none',
              display: 'flex', alignItems: 'center', gap: '10px',
            }}>
              <div
                onClick={() => setShowCertification(v => !v)}
                style={{ display: 'flex', alignItems: 'center', gap: '10px', flex: 1, cursor: 'pointer', userSelect: 'none' }}
              >
                {showCertification ? <ChevronDown size={14} style={{ color: 'var(--text-muted)' }} /> : <ChevronRight size={14} style={{ color: 'var(--text-muted)' }} />}
                <Shield size={16} style={{ color: 'var(--accent)' }} />
                <span style={{ fontSize: '14px', fontWeight: '600', color: 'var(--text-primary)' }}>
                  Certification Testing
                </span>
              </div>
              {readyForCertCount > 0 && (
                <button
                  onClick={() => { setShowCertification(true); handleStartAllCert() }}
                  disabled={startingAllCert}
                  style={{ ...btn('var(--success)'), fontSize: '11px', opacity: startingAllCert ? 0.6 : 1 }}
                >
                  <Play size={10} />
                  {startingAllCert ? 'Starting…' : `Start All (${readyForCertCount})`}
                </button>
              )}
            </div>
            {showCertification && (
              <div style={{ padding: '12px 16px' }}>
                {(assignedPartners || []).filter(p => p.status !== 'assigned').map(p => (
                  <CertPanel
                    key={p.partner_id}
                    changeId={id}
                    partnerId={p.partner_id}
                    partnerName={p.name}
                    partnerStatus={p.status}
                    blocked={p.blocked || false}
                    blockedReason={p.blocked_reason}
                  />
                ))}
              </div>
            )}
          </div>
        )}

        {/* A2A Message Log (collapsed by default) */}
        {messages && messages.length > 0 && (
          <div style={{ borderRadius: '8px', background: 'var(--bg-elevated)', border: '1px solid var(--border)', overflow: 'hidden' }}>
            <div
              onClick={() => setShowMessageLog(v => !v)}
              style={{
                padding: '14px 20px', borderBottom: showMessageLog ? '1px solid var(--border-subtle)' : 'none',
                display: 'flex', alignItems: 'center', gap: '10px', cursor: 'pointer', userSelect: 'none',
              }}
            >
              {showMessageLog ? <ChevronDown size={14} style={{ color: 'var(--text-muted)' }} /> : <ChevronRight size={14} style={{ color: 'var(--text-muted)' }} />}
              <MessageSquare size={16} style={{ color: 'var(--accent)' }} />
              <span style={{ fontSize: '14px', fontWeight: '600', color: 'var(--text-primary)' }}>
                A2A Message Log ({messages.length})
              </span>
            </div>
            {showMessageLog && messages.map((m, i) => (
              <div key={m.task_id} style={{
                padding: '10px 20px', display: 'flex', alignItems: 'center', gap: '10px',
                borderBottom: i < messages.length - 1 ? '1px solid var(--border-subtle)' : 'none',
                fontSize: '12px',
              }}>
                <span style={{
                  fontSize: '10px', padding: '2px 8px', borderRadius: '4px',
                  background: m.direction === 'outbound' ? 'rgba(218,119,86,0.1)' : 'rgba(100,160,200,0.1)',
                  color: m.direction === 'outbound' ? 'var(--accent)' : '#64a0c8',
                  fontWeight: '600', textTransform: 'uppercase', minWidth: '70px', textAlign: 'center',
                }}>{m.direction === 'outbound' ? 'the Authority →' : '→ the Authority'}</span>
                <span style={{ color: 'var(--text-primary)', fontWeight: '500' }}>{m.partner_name}</span>
                <span style={{
                  fontSize: '10px', padding: '1px 6px', borderRadius: '3px',
                  background: 'var(--bg-base)', border: '1px solid var(--border)',
                  color: 'var(--text-muted)', fontFamily: 'monospace',
                }}>{m.task_type}</span>
                <span style={{ color: 'var(--text-muted)', marginLeft: 'auto', fontSize: '11px' }}>
                  {new Date(m.created_at).toLocaleString()}
                </span>
                <span style={{
                  fontSize: '10px', padding: '1px 6px', borderRadius: '3px',
                  background: m.status === 'delivered' || m.status === 'completed' ? 'rgba(76,175,125,0.1)' : 'var(--bg-base)',
                  color: m.status === 'delivered' || m.status === 'completed' ? 'var(--success)' : 'var(--text-muted)',
                  border: '1px solid var(--border)',
                }}>{m.status}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
