// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  Building2, Globe, Smartphone, Cpu, ExternalLink, Loader,
  AlertTriangle, CheckCircle, Activity, ChevronDown, ChevronRight,
  Bot, FlaskConical, MessagesSquare,
} from 'lucide-react'

import { certificationApi, changesApi, phaseCApi } from '../../services/api'
import PageHeader  from '../../components/cert/PageHeader'
import KpiStrip    from '../../components/cert/KpiStrip'
import Section     from '../../components/cert/Section'
import StatusBadge from '../../components/cert/StatusBadge'
import {
  ASSIGNMENT_FLOW, ASSIGNMENT_STATUS, ASSIGNMENT_FLAGS,
  assignmentChips, relativeTime,
} from '../../lib/certStatus'

// Chip rail component — renders concurrent flags (blocked / negotiating /
// triage_pending / stalled) next to the main status badge.
function ChipRail({ flags }) {
  if (!flags || flags.length === 0) return null
  return (
    <div style={{ display: 'inline-flex', gap: '4px', marginLeft: 'var(--space-2)', flexWrap: 'wrap' }}>
      {flags.map(key => {
        const meta = ASSIGNMENT_FLAGS[key]
        if (!meta) return null
        const Icon = meta.icon
        return (
          <span key={key} style={{
            display: 'inline-flex', alignItems: 'center', gap: '3px',
            padding: '1px 7px', borderRadius: '999px',
            fontSize: '10px', fontWeight: 600,
            color: meta.color, background: `${meta.color}1A`,
            border: `1px solid ${meta.color}40`,
          }}>
            <Icon size={9} /> {meta.label}
          </span>
        )
      })}
    </div>
  )
}

// Status history strip — collapsed by default; renders last 5 transitions.
function StatusHistory({ changeId, partnerId }) {
  const { data, isLoading } = useQuery({
    queryKey: ['status-history', changeId, partnerId],
    queryFn: () => phaseCApi.statusHistory(changeId, partnerId).then(r => r.data),
    enabled: !!(changeId && partnerId),
  })
  if (isLoading || !data) return null
  const transitions = (data.transitions || []).slice(0, 5)
  if (transitions.length === 0) return null
  return (
    <details style={{ marginTop: 'var(--space-2)' }}>
      <summary style={{
        cursor: 'pointer', fontSize: '11px', color: 'var(--text-muted)',
        userSelect: 'none', display: 'inline-block',
      }}>
        Status history ({data.transitions.length})
      </summary>
      <div style={{
        marginTop: 'var(--space-2)', padding: 'var(--space-2) var(--space-3)',
        background: 'var(--bg-base)', border: '1px solid var(--border-subtle)',
        borderRadius: '6px', fontSize: '11px',
      }}>
        {transitions.map(t => (
          <div key={t.id} style={{ display: 'flex', gap: 'var(--space-2)', padding: '3px 0', alignItems: 'baseline' }}>
            <span className="id-mono" style={{ color: 'var(--text-muted)', minWidth: 100 }}>
              {t.created_at ? relativeTime(t.created_at) : ''}
            </span>
            <span style={{ color: 'var(--text-secondary)' }}>{t.from_status || '—'}</span>
            <span style={{ color: 'var(--text-muted)' }}>→</span>
            <span style={{ color: 'var(--text-primary)', fontWeight: 600 }}>{t.to_status}</span>
            <span style={{ color: 'var(--text-muted)' }}>· {t.actor}</span>
            {t.reason && <span style={{ color: 'var(--text-muted)', fontStyle: 'italic', marginLeft: 'auto' }}>{t.reason.slice(0, 60)}</span>}
          </div>
        ))}
      </div>
    </details>
  )
}

const TYPE_META = {
  bank:        { color: '#58a6ff', icon: Building2  },
  psp:         { color: '#3fb950', icon: Smartphone },
  tpap:        { color: '#d29922', icon: Globe      },
  cert_engine: { color: '#e8b347', icon: Cpu        },
}

// ── Step state for the partner flow diagram ──────────────────────────────
function stateForStep(stepKey, partnerStatus, latestRunFailed) {
  const stepMeta = ASSIGNMENT_STATUS[stepKey]
  const curMeta  = ASSIGNMENT_STATUS[partnerStatus]
  if (stepKey === 'certified' && latestRunFailed) return 'failed'
  if (!stepMeta || !curMeta) return 'pending'
  if (stepMeta.order < curMeta.order)  return 'done'
  if (stepMeta.order === curMeta.order) return 'active'
  return 'pending'
}

function FlowDiagram({ status, latestRunFailed }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 0, flexWrap: 'nowrap' }}>
      {ASSIGNMENT_FLOW.map((step, idx) => {
        const state = stateForStep(step.key, status, latestRunFailed)
        const dotColor =
          state === 'done'   ? '#3fb950' :
          state === 'active' ? 'var(--accent)' :
          state === 'failed' ? '#e06c6c' :
          'var(--border)'
        const labelColor =
          state === 'done'   ? '#3fb950' :
          state === 'active' ? 'var(--accent)' :
          state === 'failed' ? '#e06c6c' :
          'var(--text-muted)'

        return (
          <div key={step.key} style={{ display: 'flex', alignItems: 'center', flex: idx < ASSIGNMENT_FLOW.length - 1 ? 1 : 0 }}>
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', minWidth: '74px' }}>
              <div style={{
                width: '22px', height: '22px', borderRadius: '50%',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                background: state === 'done' ? '#3fb950' : state === 'active' ? 'var(--accent)' : 'var(--bg-elevated)',
                border: `2px solid ${dotColor}`,
                transition: 'all 0.2s',
              }}>
                {state === 'done'    && <CheckCircle size={12} color="white" />}
                {state === 'active'  && <div style={{ width: '8px', height: '8px', borderRadius: '50%', background: 'white' }} />}
                {state === 'pending' && <div style={{ width: '6px', height: '6px', borderRadius: '50%', background: 'var(--border)' }} />}
                {state === 'failed'  && <AlertTriangle size={12} color="white" />}
              </div>
              <span style={{
                fontSize: '10px',
                color: labelColor,
                marginTop: '5px',
                textAlign: 'center',
                lineHeight: 1.3,
                fontWeight: state === 'active' ? 600 : 400,
              }}>
                {step.label}
              </span>
            </div>
            {idx < ASSIGNMENT_FLOW.length - 1 && (
              <div style={{
                flex: 1,
                height: '2px',
                marginBottom: '18px',
                background: state === 'done' ? '#3fb950' : 'var(--border)',
                transition: 'background 0.2s',
              }} />
            )}
          </div>
        )
      })}
    </div>
  )
}

// ── Recent runs strip — shows last 3 runs as small pills ─────────────────
function RecentRunsStrip({ changeId, partnerId }) {
  const { data, isLoading } = useQuery({
    queryKey: ['cert-runs', changeId, partnerId],
    queryFn: async () => (await phaseCApi.listCertRuns(changeId, partnerId)).data,
    enabled: !!(changeId && partnerId),
    refetchInterval: 15000,
  })
  if (isLoading || !data || data.length === 0) return null
  const recent = data.slice(0, 3)
  return (
    <div style={{ display: 'flex', gap: 'var(--space-2)', alignItems: 'center', marginTop: 'var(--space-2)' }}>
      <span style={{ fontSize: '10px', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.06em', fontWeight: 600 }}>
        Recent runs
      </span>
      {recent.map(run => {
        const total = run.total ?? 0
        const passed = run.passed ?? 0
        const failed = run.failed ?? 0
        const allPass = total > 0 && failed === 0 && passed === total
        const color = allPass ? '#3fb950' : failed > 0 ? '#e06c6c' : 'var(--text-muted)'
        return (
          <span
            key={run.id}
            title={`Started ${run.started_at ? new Date(run.started_at).toLocaleString() : '—'}`}
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: '5px',
              padding: '2px 8px',
              borderRadius: '999px',
              fontSize: '10px',
              fontWeight: 600,
              color,
              background: `${color}1A`,
              border: `1px solid ${color}40`,
              whiteSpace: 'nowrap',
            }}
          >
            #{run.run_number} · {passed}/{total}
            {failed > 0 && <span> · {failed} failed</span>}
          </span>
        )
      })}
    </div>
  )
}

// ── Detailed test-results panel — drill into a partner's per-TC outcomes ──
//
// Rendered inline below each partner card when expanded. Pulls the run
// list (existing endpoint) + the selected run detail (existing endpoint)
// + lets PO trigger AI triage on failures. Same data the PhaseC.jsx
// CertPanel uses, surfaced here so the Certification section is
// self-contained.

const TC_STATUS_PILL = {
  pass:  { label: 'PASS',  color: '#3fb950' },
  fail:  { label: 'FAIL',  color: '#e06c6c' },
  skip:  { label: 'SKIP',  color: '#8b949e' },
  error: { label: 'ERROR', color: '#e06c6c' },
}

const VERDICT_PILL = {
  partner_code_bug: { label: 'Code bug',    color: '#e06c6c' },
  test_case_issue:  { label: 'Test issue',  color: '#d29922' },
  env_issue:        { label: 'Env issue',   color: '#bc8cff' },
}

function TestResultsPanel({ changeId, partnerId, partnerName }) {
  const [selectedRunId, setSelectedRunId] = useState(null)
  const [tcFilter, setTcFilter] = useState('all') // all | pass | fail | skip
  const [triaging, setTriaging] = useState(false)

  const { data: runs, isLoading: runsLoading, refetch: refetchRuns } = useQuery({
    queryKey: ['cert-runs', changeId, partnerId],
    queryFn: () => phaseCApi.listCertRuns(changeId, partnerId).then(r => r.data),
  })

  // Default selection: latest run
  const effectiveRunId = selectedRunId ?? runs?.[0]?.id

  const { data: runDetail, isLoading: detailLoading, refetch: refetchDetail } = useQuery({
    queryKey: ['cert-run-detail', changeId, partnerId, effectiveRunId],
    queryFn: () => phaseCApi.getCertRun(changeId, partnerId, effectiveRunId).then(r => r.data),
    enabled: !!effectiveRunId,
  })

  async function handleTriage() {
    setTriaging(true)
    try {
      await phaseCApi.triggerTriage(changeId, partnerId)
      refetchDetail()
    } catch (e) {
      alert('Triage failed: ' + (e?.response?.data?.detail || e?.message))
    } finally {
      setTriaging(false)
    }
  }

  // Filter the per-TC results
  const results = (runDetail?.results || []).filter(r => tcFilter === 'all' || r.status === tcFilter)
  const counts = (runDetail?.results || []).reduce((acc, r) => {
    acc[r.status] = (acc[r.status] || 0) + 1
    return acc
  }, {})
  const dirBreakdown = (runDetail?.results || []).reduce((acc, r) => {
    const key = r.direction === 'partner_to_npci' ? 'bank' : 'npci'
    acc[key] = acc[key] || { total: 0, pass: 0, fail: 0 }
    acc[key].total += 1
    if (r.status === 'pass') acc[key].pass += 1
    else if (r.status === 'fail') acc[key].fail += 1
    return acc
  }, {})

  if (runsLoading) {
    return (
      <div style={{ marginTop: 'var(--space-3)', padding: 'var(--space-5)', textAlign: 'center', color: 'var(--text-muted)', fontSize: '12px' }}>
        <Loader size={14} className="spin" style={{ verticalAlign: 'middle' }} /> Loading runs…
      </div>
    )
  }

  if (!runs || runs.length === 0) {
    return (
      <div style={{
        marginTop: 'var(--space-3)', padding: 'var(--space-4)',
        background: 'var(--bg-base)', border: '1px solid var(--border-subtle)',
        borderRadius: '6px',
        textAlign: 'center', color: 'var(--text-muted)', fontSize: '12px',
      }}>
        No certification runs for {partnerName} yet. Trigger one from Phase C to populate this panel.
      </div>
    )
  }

  return (
    <div style={{
      marginTop: 'var(--space-3)',
      background: 'var(--bg-base)',
      border: '1px solid var(--border-subtle)',
      borderRadius: '6px',
      overflow: 'hidden',
    }}>
      {/* Run history strip — clickable pills, latest first */}
      <div style={{
        display: 'flex', alignItems: 'center', flexWrap: 'wrap',
        gap: 'var(--space-2)',
        padding: 'var(--space-3) var(--space-4)',
        borderBottom: '1px solid var(--border-subtle)',
        background: 'var(--bg-elevated)',
      }}>
        <span style={{ fontSize: '10px', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.06em', fontWeight: 600, marginRight: 'var(--space-2)' }}>
          Run history
        </span>
        {runs.map(run => {
          const isActive = effectiveRunId === run.id
          const allPass = run.failed === 0 && run.passed > 0
          const color = allPass ? '#3fb950' : run.failed > 0 ? '#e06c6c' : 'var(--text-muted)'
          return (
            <button
              key={run.id}
              onClick={() => setSelectedRunId(run.id)}
              style={{
                display: 'inline-flex', alignItems: 'center', gap: '5px',
                padding: '4px 10px',
                borderRadius: '999px',
                fontSize: '11px',
                fontWeight: 600,
                color: isActive ? 'white' : color,
                background: isActive ? color : `${color}1A`,
                border: `1px solid ${isActive ? color : `${color}40`}`,
                cursor: 'pointer',
              }}
            >
              #{run.run_number} · {run.passed || 0}/{run.total || 0}
              {run.failed > 0 && <span> · {run.failed} fail</span>}
            </button>
          )
        })}
        <button
          onClick={() => refetchRuns()}
          style={{
            marginLeft: 'auto',
            fontSize: '11px',
            color: 'var(--text-muted)',
            background: 'transparent',
            border: 'none',
            cursor: 'pointer',
          }}
        >
          Refresh
        </button>
      </div>

      {/* Run summary + TC filter chips */}
      {detailLoading && (
        <div style={{ padding: 'var(--space-5)', textAlign: 'center', color: 'var(--text-muted)', fontSize: '12px' }}>
          <Loader size={14} className="spin" style={{ verticalAlign: 'middle' }} /> Loading test results…
        </div>
      )}
      {runDetail && (
        <>
          <div style={{
            display: 'flex', alignItems: 'center', gap: 'var(--space-3)', flexWrap: 'wrap',
            padding: 'var(--space-3) var(--space-4)',
            borderBottom: '1px solid var(--border-subtle)',
          }}>
            <span style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-primary)' }}>
              Run #{runDetail.run_number} ·{' '}
              <span style={{ color: '#3fb950' }}>{runDetail.passed || 0} pass</span>
              {(runDetail.failed || 0) > 0 && <span style={{ color: '#e06c6c' }}> · {runDetail.failed} fail</span>}
              {(runDetail.skipped || 0) > 0 && <span style={{ color: 'var(--text-muted)' }}> · {runDetail.skipped} skip</span>}
              <span style={{ color: 'var(--text-muted)' }}> / {runDetail.total} total</span>
            </span>
            <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>
              · started {relativeTime(runDetail.started_at)}
              {runDetail.completed_at && <> · completed {relativeTime(runDetail.completed_at)}</>}
            </span>
            {(dirBreakdown.npci || dirBreakdown.bank) && (
              <span style={{ display: 'inline-flex', gap: 'var(--space-3)', fontSize: '11px' }}>
                {dirBreakdown.npci && (
                  <span title="TCs the Authority side initiated" style={{ color: 'var(--text-secondary)' }}>
                    <strong style={{ color: '#58a6ff' }}>the Authority</strong>
                    {' '}
                    <span style={{ color: '#3fb950' }}>{dirBreakdown.npci.pass}</span>
                    <span style={{ color: 'var(--text-muted)' }}>/{dirBreakdown.npci.total}</span>
                    {dirBreakdown.npci.fail > 0 && <span style={{ color: '#e06c6c' }}> · {dirBreakdown.npci.fail} fail</span>}
                  </span>
                )}
                {dirBreakdown.bank && (
                  <span title="TCs the bank side initiated" style={{ color: 'var(--text-secondary)' }}>
                    <strong style={{ color: '#d29922' }}>Bank</strong>
                    {' '}
                    <span style={{ color: '#3fb950' }}>{dirBreakdown.bank.pass}</span>
                    <span style={{ color: 'var(--text-muted)' }}>/{dirBreakdown.bank.total}</span>
                    {dirBreakdown.bank.fail > 0 && <span style={{ color: '#e06c6c' }}> · {dirBreakdown.bank.fail} fail</span>}
                  </span>
                )}
              </span>
            )}
            <div style={{ marginLeft: 'auto', display: 'flex', gap: 'var(--space-1)' }}>
              {[
                { v: 'all',  l: `All ${runDetail.total || 0}` },
                { v: 'fail', l: `Fail ${counts.fail || 0}` },
                { v: 'pass', l: `Pass ${counts.pass || 0}` },
                ...(counts.skip ? [{ v: 'skip', l: `Skip ${counts.skip}` }] : []),
                ...(counts.error ? [{ v: 'error', l: `Error ${counts.error}` }] : []),
              ].map(f => {
                const active = tcFilter === f.v
                return (
                  <button
                    key={f.v}
                    onClick={() => setTcFilter(f.v)}
                    style={{
                      padding: '4px 9px',
                      borderRadius: '999px',
                      fontSize: '10px',
                      fontWeight: 600,
                      border: `1px solid ${active ? 'var(--accent)' : 'var(--border)'}`,
                      background: active ? 'var(--accent)' : 'transparent',
                      color: active ? 'white' : 'var(--text-secondary)',
                      cursor: 'pointer',
                    }}
                  >
                    {f.l}
                  </button>
                )
              })}
              {(runDetail.failed || 0) > 0 && (
                <button
                  onClick={handleTriage}
                  disabled={triaging}
                  style={{
                    display: 'inline-flex', alignItems: 'center', gap: '5px',
                    padding: '4px 10px',
                    borderRadius: '6px',
                    fontSize: '11px',
                    fontWeight: 600,
                    background: 'rgba(218,119,86,0.10)',
                    color: 'var(--accent)',
                    border: '1px solid rgba(218,119,86,0.30)',
                    cursor: triaging ? 'wait' : 'pointer',
                  }}
                >
                  {triaging ? <Loader size={11} className="spin" /> : <Bot size={11} />}
                  AI Triage
                </button>
              )}
            </div>
          </div>

          {/* Test-result table */}
          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '12px' }}>
              <thead>
                <tr style={{ background: 'var(--bg-elevated)' }}>
                  <th style={th()}>TC</th>
                  <th style={th()}>Direction</th>
                  <th style={th()}>Status</th>
                  <th style={th()}>Latency</th>
                  <th style={th()}>Expected</th>
                  <th style={th()}>Actual</th>
                  <th style={th()}>Triage</th>
                </tr>
              </thead>
              <tbody>
                {results.length === 0 && (
                  <tr>
                    <td colSpan={7} style={{ padding: 'var(--space-5)', textAlign: 'center', color: 'var(--text-muted)', fontSize: '12px' }}>
                      No test results match this filter.
                    </td>
                  </tr>
                )}
                {results.map((r, idx) => {
                  const sp = TC_STATUS_PILL[r.status] || TC_STATUS_PILL.error
                  const tv = r.triage
                  const v  = tv ? VERDICT_PILL[tv.final_verdict || tv.ai_verdict] : null
                  return (
                    <tr key={r.id} style={{
                      borderTop: idx === 0 ? 'none' : '1px solid var(--border-subtle)',
                      background: r.status === 'fail' ? 'rgba(224,108,108,0.04)' : 'transparent',
                    }}>
                      <td style={td()}>
                        <span className="id-mono" style={{ color: 'var(--text-primary)', fontWeight: 600 }}>{r.test_case_id}</span>
                      </td>
                      <td style={td()}>
                        <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>
                          {r.direction === 'partner_to_npci' ? 'Partner → the Authority' : 'the Authority → Partner'}
                        </span>
                      </td>
                      <td style={td()}>
                        <span style={pill(sp.color)}>{sp.label}</span>
                      </td>
                      <td style={td()}>
                        <span style={{ fontSize: '11px', color: 'var(--text-muted)', fontFamily: 'ui-monospace, monospace' }}>
                          {r.latency_ms ? `${r.latency_ms} ms` : '—'}
                        </span>
                      </td>
                      <td style={td()}>
                        <ResponseCell data={r.expected_response} />
                      </td>
                      <td style={td()}>
                        <ResponseCell data={r.actual_response} />
                      </td>
                      <td style={td()}>
                        {v ? (
                          <div>
                            <span style={pill(v.color)}>{v.label}</span>
                            {tv?.ai_reasoning && (
                              <div style={{ marginTop: 4, fontSize: '10px', color: 'var(--text-muted)', maxWidth: 240, lineHeight: 1.3 }} title={tv.ai_reasoning}>
                                {tv.ai_reasoning.slice(0, 80)}{tv.ai_reasoning.length > 80 && '…'}
                              </div>
                            )}
                          </div>
                        ) : r.status === 'fail' ? (
                          <span style={{ fontSize: '10px', color: 'var(--text-muted)', fontStyle: 'italic' }}>Run AI Triage ↑</span>
                        ) : (
                          <span style={{ color: 'var(--text-muted)' }}>—</span>
                        )}
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
  )
}

function ResponseCell({ data }) {
  if (!data) return <span style={{ color: 'var(--text-muted)' }}>—</span>
  const code = data.response_code || data.responseCode
  const status = data.result_status || data.status
  return (
    <div style={{ fontSize: '11px', lineHeight: 1.4 }}>
      {code && <span className="id-mono" style={{ color: 'var(--text-primary)', fontWeight: 600 }}>{code}</span>}
      {status && <span style={{ color: 'var(--text-muted)', marginLeft: 4 }}>{status}</span>}
      {data.error_code && <span style={{ color: '#e06c6c', marginLeft: 4 }}>· err {data.error_code}</span>}
    </div>
  )
}

function th() {
  return {
    padding: '8px 12px',
    textAlign: 'left',
    fontSize: '10px',
    fontWeight: 600,
    color: 'var(--text-muted)',
    textTransform: 'uppercase',
    letterSpacing: '0.06em',
    borderBottom: '1px solid var(--border)',
    whiteSpace: 'nowrap',
  }
}

function td() {
  return {
    padding: '8px 12px',
    verticalAlign: 'top',
  }
}

function pill(color) {
  return {
    display: 'inline-flex', alignItems: 'center',
    padding: '2px 8px',
    borderRadius: '999px',
    fontSize: '10px',
    fontWeight: 700,
    color, background: `${color}1A`,
    border: `1px solid ${color}40`,
    letterSpacing: '0.04em',
  }
}

export default function CertChangeDetail() {
  const { crId } = useParams()
  const [expandedPartnerId, setExpandedPartnerId] = useState(null)

  const { data: summary, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['cert-summary', crId],
    queryFn: async () => (await certificationApi.changeSummary(crId)).data,
    refetchInterval: 15000,
    enabled: !!crId,
  })

  const { data: cr } = useQuery({
    queryKey: ['change', crId],
    queryFn: async () => (await changesApi.get(crId)).data,
    enabled: !!crId,
  })

  if (isError) {
    return (
      <div style={{ padding: 'var(--space-7)' }}>
        <PageHeader
          icon={Activity}
          crumbs={[
            { label: 'Certification' },
            { label: 'Overview', to: '/certification/dashboard' },
            { label: crId?.slice(0, 8) || 'Detail' },
          ]}
          title="Change detail"
          subtitle="Failed to load change request"
        />
        <Section title="Error">
          <p style={{ margin: 0, fontSize: '13px', color: 'var(--danger)' }}>
            {error?.response?.data?.detail || error?.message}
          </p>
          <button
            onClick={() => refetch()}
            style={{
              marginTop: 'var(--space-3)',
              padding: '6px 14px',
              border: '1px solid var(--border)',
              borderRadius: '6px',
              background: 'var(--bg-elevated)',
              color: 'var(--text-secondary)',
              cursor: 'pointer',
              fontSize: '12px',
            }}>
            Retry
          </button>
        </Section>
      </div>
    )
  }

  if (isLoading || !summary) {
    return (
      <div style={{ padding: 'var(--space-7)' }}>
        <PageHeader
          crumbs={[{ label: 'Certification' }, { label: 'Overview', to: '/certification/dashboard' }, { label: 'Loading…' }]}
          title="Loading…"
        />
        <Section><Loader size={16} className="spin" style={{ verticalAlign: 'middle' }} /> Loading change detail</Section>
      </div>
    )
  }

  const partners = summary.partners || []
  const counts = {
    certified:   partners.filter(p => p.assignment_status === 'certified').length,
    ready:       partners.filter(p => p.assignment_status === 'ready').length,
    in_progress: partners.filter(p => p.assignment_status === 'in_progress').length,
    failed:      partners.filter(p => (p.latest_run?.failed || 0) > 0).length,
  }

  const latestRun = partners
    .map(p => p.latest_run)
    .filter(r => r && r.completed_at)
    .sort((a, b) => new Date(b.completed_at) - new Date(a.completed_at))[0]

  return (
    <div style={{ padding: 'var(--space-7) var(--space-7)', maxWidth: '1200px' }}>

      <PageHeader
        icon={Activity}
        crumbs={[
          { label: 'Certification' },
          { label: 'Overview', to: '/certification/dashboard' },
          { label: summary.change_id?.slice(0, 8) || crId },
        ]}
        title={summary.change_title || 'Change detail'}
        subtitle={
          <span>
            <span className="id-mono" style={{ marginRight: 'var(--space-2)' }}>{summary.change_id}</span>
            {cr?.status && (
              <span style={{
                padding: '1px 8px', borderRadius: '999px',
                background: 'var(--bg-elevated)', border: '1px solid var(--border)',
                fontSize: '11px', color: 'var(--text-muted)',
              }}>
                {cr.status}
              </span>
            )}
          </span>
        }
        actions={
          <Link
            to={`/changes/${crId}/phase-c`}
            style={{
              display: 'inline-flex', alignItems: 'center', gap: '6px',
              padding: '7px 12px',
              background: 'var(--accent)',
              border: 'none',
              borderRadius: '6px',
              color: 'white',
              fontSize: '12px', fontWeight: 600,
              textDecoration: 'none',
            }}
          >
            View in Phase C <ExternalLink size={11} />
          </Link>
        }
      />

      {/* Two-column hero: KPIs + Latest activity */}
      <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr', gap: 'var(--space-4)', marginBottom: 'var(--space-6)' }}>
        <KpiStrip
          columns={4}
          tiles={[
            { label: 'Certified',   value: counts.certified,   color: 'var(--success)' },
            { label: 'Ready',       value: counts.ready,       color: '#58a6ff' },
            { label: 'In Progress', value: counts.in_progress, color: 'var(--warning)' },
            { label: 'Failed',      value: counts.failed,      color: 'var(--danger)' },
          ]}
        />
        <Section title="Latest activity">
          {latestRun ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-2)' }}>
              <div>
                <p style={{ margin: 0, fontSize: '11px', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
                  Most recent cert run
                </p>
                <p style={{ margin: '2px 0 0', fontSize: '13px', fontWeight: 600, color: 'var(--text-primary)' }}>
                  Run #{latestRun.run_number} · {latestRun.passed}/{latestRun.total} passed
                  {latestRun.failed > 0 && <span style={{ color: 'var(--danger)' }}> · {latestRun.failed} failed</span>}
                </p>
              </div>
              <div>
                <p style={{ margin: 0, fontSize: '11px', color: 'var(--text-muted)' }}>
                  {relativeTime(latestRun.completed_at)}
                </p>
              </div>
            </div>
          ) : (
            <p style={{ margin: 0, fontSize: '12px', color: 'var(--text-muted)' }}>
              No cert runs yet. Click <strong>View in Phase C</strong> → <strong>Start Certification</strong> on a ready partner.
            </p>
          )}
        </Section>
      </div>

      {/* Partners list */}
      <div style={{ marginBottom: 'var(--space-3)', display: 'flex', alignItems: 'baseline', justifyContent: 'space-between' }}>
        <h3 style={{ margin: 0, fontSize: '14px', fontWeight: 600, color: 'var(--text-primary)' }}>
          Partner certification status
        </h3>
        <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>
          {partners.length} partner{partners.length !== 1 ? 's' : ''} assigned
        </span>
      </div>

      {partners.length === 0 ? (
        <Section>
          <p style={{ margin: 0, fontSize: '13px', color: 'var(--text-muted)', textAlign: 'center', padding: 'var(--space-5)' }}>
            No partners assigned to this change yet.
          </p>
        </Section>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-3)' }}>
          {partners.map(p => {
            const types = p.partner_types || ['bank']
            const primary = types[0]
            const meta = TYPE_META[primary] || TYPE_META.bank
            const Icon = meta.icon
            const failed = (p.latest_run?.failed || 0) > 0
            return (
              <Section key={p.partner_id} padded={false}>
                <div style={{ padding: 'var(--space-4) var(--space-5)' }}>
                  {/* Partner header */}
                  <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-3)', marginBottom: 'var(--space-3)' }}>
                    <div style={{
                      width: 36, height: 36, borderRadius: 8,
                      background: `${meta.color}1A`, border: `1px solid ${meta.color}40`,
                      display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0,
                    }}>
                      <Icon size={16} color={meta.color} />
                    </div>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', flexWrap: 'wrap' }}>
                        <span style={{ fontSize: '14px', fontWeight: 600, color: 'var(--text-primary)' }}>
                          {p.partner_name}
                        </span>
                        <StatusBadge kind="assignment" value={p.assignment_status} size="sm" />
                        <ChipRail flags={assignmentChips(p)} />
                        {types.map(t => (
                          <span key={t} style={{
                            fontSize: '10px',
                            padding: '1px 7px',
                            borderRadius: 999,
                            background: `${(TYPE_META[t]?.color || meta.color)}1A`,
                            color: TYPE_META[t]?.color || meta.color,
                            border: `1px solid ${(TYPE_META[t]?.color || meta.color)}40`,
                            textTransform: 'uppercase',
                            fontWeight: 600,
                            letterSpacing: '0.04em',
                          }}>
                            {t}
                          </span>
                        ))}
                      </div>
                      <span className="id-mono" style={{ color: 'var(--text-muted)' }}>
                        {p.partner_id?.slice(0, 8)}
                      </span>
                      <RecentRunsStrip changeId={crId} partnerId={p.partner_id} />
                    </div>
                    {p.latest_run && (
                      <div style={{ textAlign: 'right', flexShrink: 0 }}>
                        <p style={{ margin: 0, fontSize: '11px', color: 'var(--text-muted)' }}>
                          Run #{p.latest_run.run_number} · {p.latest_run.status}
                        </p>
                        {p.latest_run.total != null && (
                          <p style={{ margin: '2px 0 0', fontSize: '13px', color: 'var(--text-primary)', fontWeight: 600 }}>
                            <span style={{ color: '#3fb950' }}>{p.latest_run.passed || 0}</span>
                            <span style={{ color: 'var(--text-muted)' }}> / {p.latest_run.total}</span>
                            {(p.latest_run.failed || 0) > 0 && (
                              <span style={{ color: '#e06c6c', marginLeft: 6 }}>· {p.latest_run.failed} failed</span>
                            )}
                          </p>
                        )}
                      </div>
                    )}
                    {/* Full cert lifecycle rendered as an A2A chat (signed + audited) */}
                    <Link
                      to={`/certification/changes/${crId}/conversation/${p.partner_id}`}
                      style={{
                        display: 'inline-flex', alignItems: 'center', gap: '5px',
                        padding: '6px 12px',
                        background: 'var(--bg-elevated)',
                        border: '1px solid var(--border)',
                        borderRadius: '6px',
                        color: 'var(--text-secondary)',
                        fontSize: '11px', fontWeight: 600,
                        textDecoration: 'none', flexShrink: 0,
                      }}
                    >
                      <MessagesSquare size={11} /> A2A Conversation
                    </Link>
                    {/* Toggle: show/hide detailed test results inline */}
                    <button
                      onClick={() => setExpandedPartnerId(
                        expandedPartnerId === p.partner_id ? null : p.partner_id
                      )}
                      style={{
                        display: 'inline-flex', alignItems: 'center', gap: '5px',
                        padding: '6px 12px',
                        background: expandedPartnerId === p.partner_id ? 'var(--accent)' : 'var(--bg-elevated)',
                        border: `1px solid ${expandedPartnerId === p.partner_id ? 'var(--accent)' : 'var(--border)'}`,
                        borderRadius: '6px',
                        color: expandedPartnerId === p.partner_id ? 'white' : 'var(--text-secondary)',
                        fontSize: '11px',
                        fontWeight: 600,
                        cursor: 'pointer',
                        flexShrink: 0,
                      }}
                    >
                      <FlaskConical size={11} />
                      {expandedPartnerId === p.partner_id ? 'Hide test results' : 'View test results'}
                      {expandedPartnerId === p.partner_id ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                    </button>
                  </div>
                  {/* Flow */}
                  <FlowDiagram status={p.assignment_status} latestRunFailed={failed} />
                  {/* Blocked banner */}
                  {p.blocked && (
                    <div style={{
                      marginTop: 'var(--space-3)', padding: '8px 12px',
                      background: 'rgba(255,127,155,0.08)', border: '1px solid rgba(255,127,155,0.3)',
                      borderRadius: '5px', fontSize: '11px', color: 'var(--text-secondary)',
                    }}>
                      <strong style={{ color: '#ff7f9b' }}>Blocked:</strong> {p.blocked_reason || 'No reason given'} · since {relativeTime(p.blocked_at)}
                    </div>
                  )}
                  <StatusHistory changeId={crId} partnerId={p.partner_id} />
                  {/* Detailed test results — drilled in only when expanded */}
                  {expandedPartnerId === p.partner_id && (
                    <TestResultsPanel
                      changeId={crId}
                      partnerId={p.partner_id}
                      partnerName={p.partner_name}
                    />
                  )}
                </div>
              </Section>
            )
          })}
        </div>
      )}
    </div>
  )
}
