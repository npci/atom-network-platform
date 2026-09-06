// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

/**
 * SyncDiffModal — confirms the Phase A → cert-agent test-case push.
 *
 * Workflow:
 *   1. On open, calls certSyncApi.diff(changeId) → renders sections:
 *        - "New APIs detected" — pre-populated from `proposed_flow_defs` when
 *          Phase A's flow_generator authored them; otherwise blank rows the
 *          operator fills in. Each row is a checkbox + editable form.
 *        - Added / Changed / Removed test-case rows (3 collapsible sections
 *          with per-row checkboxes).
 *   2. Operator can bulk-toggle, edit inline, expand "fields_changed".
 *   3. On "Confirm Push", calls certSyncApi.apply(changeId, decisions, flow_registrations);
 *      backend registers any new flows on cert-agent first, then applies TC
 *      decisions. Result counts shown; closes on success.
 *
 * Props:
 *   changeId, changeTitle, onClose, onApplied
 */
import { useState, useMemo, useEffect } from 'react'
import {
  X, ChevronDown, ChevronRight, Loader, AlertTriangle, Plus, Edit3, Trash2,
  CheckCircle, FileText, Zap, Sparkles,
} from 'lucide-react'
import { certSyncApi } from '../../services/api'

const FLOW_COLORS = {
  PAY:      '#58a6ff',
  COLLECT:  '#3fb950',
  MANDATE:  '#bc8cff',
  REFUND:   '#d29922',
  BALANCE:  '#7ed3e0',
  VALCUST:  '#bc8cff',
  CHKTXN:   '#8b949e',
  REVERSAL: '#e06c6c',
  MINISTMT: '#7ed3e0',
}

const CONFIDENCE_COLORS = {
  high:      '#3fb950',
  medium:    '#d29922',
  low:       '#e06c6c',
  'llm-draft': '#bc8cff',
}

function suggestFlowCode(apiName = '') {
  return String(apiName).replace(/^Req/i, '').replace(/[^A-Za-z0-9]/g, '').toUpperCase() || 'CUSTOM'
}
function suggestRespName(apiName = '') {
  return String(apiName).replace(/^Req/i, 'Resp')
}

function FlowChip({ flow }) {
  const c = FLOW_COLORS[flow] || '#8b949e'
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center',
      padding: '1px 7px', borderRadius: 999,
      fontSize: 9, fontWeight: 700,
      color: c, background: `${c}1A`, border: `1px solid ${c}40`,
      letterSpacing: '0.04em',
    }}>{flow}</span>
  )
}

function CodeChip({ code }) {
  if (!code) return null
  const isSuccess = code === '00'
  const c = isSuccess ? '#3fb950' : '#e06c6c'
  return (
    <span className="id-mono" style={{
      padding: '1px 6px', borderRadius: 4,
      fontSize: 10, fontWeight: 700,
      color: c, background: `${c}1A`, border: `1px solid ${c}40`,
    }}>{code}</span>
  )
}

function ConfidenceChip({ confidence }) {
  if (!confidence) return null
  const c = CONFIDENCE_COLORS[confidence] || '#8b949e'
  return (
    <span style={{
      padding: '1px 6px', borderRadius: 4,
      fontSize: 9, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.04em',
      color: c, background: `${c}1A`, border: `1px solid ${c}40`,
    }}>{confidence}</span>
  )
}

// Keyed on the stored `initiated_by` value, which is wire/DB data — the labels
// beside it are what the operator reads.
const INITIATOR_COLORS = {
  NPCI: '#58a6ff',
  BANK: '#d29922',
}
const INITIATOR_LABELS = {
  NPCI: 'Authority',
  BANK: 'Bank',
}
function InitiatorChip({ initiatedBy, pspAs }) {
  const norm = String(initiatedBy || '').toUpperCase()
  const isKnown = norm === 'NPCI' || norm === 'BANK'
  const c = INITIATOR_COLORS[norm] || '#8b949e'
  const label = isKnown ? `${INITIATOR_LABELS[norm]}-initiated` : 'initiator unknown'
  return (
    <span title={pspAs ? `PSP as ${pspAs}` : 'PSP role unspecified'} style={{
      padding: '1px 6px', borderRadius: 4,
      fontSize: 9, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.04em',
      color: c, background: `${c}1A`, border: `1px solid ${c}40`,
    }}>
      {label}{pspAs ? ` · ${pspAs}` : ''}
    </span>
  )
}

function Section({ title, count, color, icon: Icon, defaultOpen, allChecked, onToggleAll, children }) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div style={{
      marginBottom: 'var(--space-3)',
      border: `1px solid ${color}40`, borderRadius: 8,
      background: `${color}08`, overflow: 'hidden',
    }}>
      <div style={{
        display: 'flex', alignItems: 'center', gap: 'var(--space-2)',
        padding: 'var(--space-2) var(--space-4)',
        cursor: 'pointer',
        borderBottom: open ? `1px solid ${color}30` : 'none',
      }} onClick={() => setOpen(v => !v)}>
        <Icon size={14} color={color} />
        <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-primary)' }}>
          {title}
        </span>
        <span style={{ fontSize: 11, color, fontWeight: 700 }}>({count})</span>
        {count > 0 && (
          <label
            onClick={e => e.stopPropagation()}
            style={{
              marginLeft: 'auto', display: 'inline-flex', alignItems: 'center', gap: 4,
              fontSize: 10, color: 'var(--text-muted)', cursor: 'pointer',
            }}
          >
            <input type="checkbox" checked={allChecked} onChange={onToggleAll} style={{ accentColor: color }} />
            apply all
          </label>
        )}
        {open ? <ChevronDown size={14} color="var(--text-muted)" /> : <ChevronRight size={14} color="var(--text-muted)" />}
      </div>
      {open && count > 0 && <div>{children}</div>}
      {open && count === 0 && (
        <p style={{ margin: 0, padding: 'var(--space-3)', fontSize: 11, color: 'var(--text-muted)', textAlign: 'center' }}>
          no items
        </p>
      )}
    </div>
  )
}

function RowFieldDiff({ before, after, fieldsChanged }) {
  const [open, setOpen] = useState(false)
  return (
    <div style={{ marginTop: 4 }}>
      <button
        onClick={(e) => { e.stopPropagation(); setOpen(v => !v) }}
        style={{
          display: 'inline-flex', alignItems: 'center', gap: 3,
          fontSize: 10, color: 'var(--text-muted)',
          background: 'none', border: 'none', cursor: 'pointer', padding: 0,
        }}
      >
        {open ? <ChevronDown size={10} /> : <ChevronRight size={10} />}
        {fieldsChanged.length} field{fieldsChanged.length !== 1 ? 's' : ''} changed: {fieldsChanged.join(', ')}
      </button>
      {open && (
        <div style={{
          marginTop: 6, padding: 'var(--space-2)',
          background: 'var(--bg-base)', border: '1px solid var(--border-subtle)',
          borderRadius: 4, fontSize: 11, fontFamily: 'ui-monospace, monospace',
        }}>
          {fieldsChanged.map(f => (
            <div key={f} style={{ display: 'grid', gridTemplateColumns: '110px 1fr 1fr', gap: 8, padding: '3px 0' }}>
              <span style={{ color: 'var(--text-muted)' }}>{f}</span>
              <span style={{ color: '#e06c6c', textDecoration: 'line-through', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {JSON.stringify(before?.[f])?.slice(0, 80)}
              </span>
              <span style={{ color: '#3fb950', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {JSON.stringify(after?.[f])?.slice(0, 80)}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export default function SyncDiffModal({ changeId, changeTitle, onClose, onApplied }) {
  const [loading, setLoading]   = useState(true)
  const [error, setError]       = useState(null)
  const [diff, setDiff]         = useState(null)
  const [decisions, setDecisions] = useState({})
  const [applying, setApplying] = useState(false)
  const [applyResult, setApplyResult] = useState(null)
  // Editable inline flow registrations, keyed by api_request name.
  // Hydrated from `proposed_flow_defs` (engine-authored) OR from `unknown_apis`
  // (engine couldn't author). Operator can edit any field before submit.
  const [flowRegs, setFlowRegs] = useState({})

  useEffect(() => {
    let alive = true
    setLoading(true)
    certSyncApi.diff(changeId)
      .then(r => {
        if (!alive) return
        setDiff(r.data)
        const init = {}
        for (const t of r.data.added || [])    init[t.tc_id] = 'add'
        for (const t of r.data.changed || [])  init[t.tc_id] = 'update'
        for (const t of r.data.removed || [])  init[t.tc_id] = 'delete'
        setDecisions(init)

        // Hydrate flow registration drafts. Source priority:
        //   1. proposed_flow_defs (engine-authored) — pre-fill all fields, mark `prepopulated`
        //   2. unknown_apis (engine couldn't author) — blank form with name suggestions
        const regs = {}
        const proposedByApi = new Map()
        for (const fd of r.data.proposed_flow_defs || []) {
          proposedByApi.set(fd.api_request, fd)
        }
        for (const fd of r.data.proposed_flow_defs || []) {
          regs[fd.api_request] = {
            checked:           true,
            prepopulated:      true,
            confidence:        fd.confidence || 'medium',
            source:            fd.source || '',
            flow_code:         fd.flow_code || suggestFlowCode(fd.api_request),
            api_request:       fd.api_request,
            api_response:      fd.api_response || suggestRespName(fd.api_request),
            request_xml_template: fd.request_xml_template || '',
            simulator_endpoint: fd.simulator_endpoint || '/execute',
            expected_resp_codes: (fd.expected_resp_codes || ['00']).join(','),
            role:              fd.role || '',
            description:       fd.description || '',
          }
        }
        for (const u of r.data.unknown_apis || []) {
          if (proposedByApi.has(u.api)) continue  // already covered by engine
          regs[u.api] = {
            checked:           true,
            prepopulated:      false,
            confidence:        'low',
            source:            '',
            flow_code:         suggestFlowCode(u.api),
            api_request:       u.api,
            api_response:      suggestRespName(u.api),
            request_xml_template: '',
            simulator_endpoint: '/execute',
            expected_resp_codes: '00',
            role:              '',
            description:       `Auto-detected from CR (${u.tc_ids?.length || 0} TC${u.tc_ids?.length === 1 ? '' : 's'}). Engine could not author — please fill in XML.`,
          }
        }
        setFlowRegs(regs)
        setLoading(false)
      })
      .catch(err => {
        if (!alive) return
        setError(err?.response?.data?.detail || err.message || 'Failed to compute diff')
        setLoading(false)
      })
    return () => { alive = false }
  }, [changeId])

  const counts = useMemo(() => {
    const planned = { add: 0, update: 0, delete: 0, skip: 0 }
    for (const v of Object.values(decisions)) planned[v] = (planned[v] || 0) + 1
    return planned
  }, [decisions])

  const flowRegCount = useMemo(
    () => Object.values(flowRegs).filter(r => r.checked && r.flow_code && r.api_request).length,
    [flowRegs]
  )

  const initiatorBreakdown = useMemo(() => {
    if (!diff) return { npci: 0, bank: 0, unknown: 0 }
    const rows = [
      ...(diff.added || []),
      ...((diff.changed || []).map(c => c.after || {})),
      ...(diff.removed || []),
    ]
    let npci = 0, bank = 0, unknown = 0
    for (const r of rows) {
      const v = String(r?.initiated_by || '').toUpperCase()
      if (v === 'NPCI') npci += 1
      else if (v === 'BANK') bank += 1
      else unknown += 1
    }
    return { npci, bank, unknown }
  }, [diff])

  function toggleSection(items, kind, on) {
    setDecisions(d => {
      const next = { ...d }
      for (const t of items) next[t.tc_id] = on ? kind : 'skip'
      return next
    })
  }

  function toggleOne(tc_id, kind) {
    setDecisions(d => {
      const cur = d[tc_id]
      const isSet = cur === kind
      return { ...d, [tc_id]: isSet ? 'skip' : kind }
    })
  }

  function patchFlowReg(api, patch) {
    setFlowRegs(prev => ({ ...prev, [api]: { ...prev[api], ...patch } }))
  }

  async function handleApply() {
    setApplying(true)
    try {
      const decisionsPayload = Object.entries(decisions).map(([tc_id, action]) => ({ tc_id, action }))
      const flow_registrations = Object.values(flowRegs)
        .filter(r => r.checked && r.flow_code && r.api_request && r.api_response)
        .map(r => ({
          flow_code:           r.flow_code.trim().toUpperCase(),
          api_request:         r.api_request.trim(),
          api_response:        r.api_response.trim(),
          simulator_endpoint:  r.simulator_endpoint?.trim() || '/execute',
          role:                r.role?.trim() || '',
          description:         r.description?.trim() || '',
          request_xml_template: r.request_xml_template || '',
          expected_resp_codes: (r.expected_resp_codes || '00').split(',').map(s => s.trim()).filter(Boolean),
        }))
      const res = await certSyncApi.apply(changeId, decisionsPayload, flow_registrations)
      setApplyResult(res.data)
      onApplied?.(res.data)
    } catch (err) {
      setError(err?.response?.data?.detail || err.message || 'Apply failed')
    } finally {
      setApplying(false)
    }
  }

  // Block submit when any low-confidence pre-populated row still has empty XML.
  const blockedReason = useMemo(() => {
    for (const r of Object.values(flowRegs)) {
      if (!r.checked) continue
      const needsXml = r.confidence === 'low' || r.confidence === 'llm-draft'
      if (needsXml && !(r.request_xml_template || '').trim()) {
        return `Flow ${r.flow_code} is ${r.confidence} with empty XML — fill it in or uncheck.`
      }
    }
    return null
  }, [flowRegs])

  return (
    <div style={{
      position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.55)',
      display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000,
    }}>
      <div style={{
        background: 'var(--bg-card)', border: '1px solid var(--border)',
        borderRadius: 12, width: '92vw', maxWidth: 980, maxHeight: '88vh',
        display: 'flex', flexDirection: 'column', overflow: 'hidden',
      }}>
        {/* Header */}
        <div style={{
          padding: 'var(--space-4) var(--space-5)',
          borderBottom: '1px solid var(--border)',
          display: 'flex', alignItems: 'center', gap: 'var(--space-3)',
        }}>
          <FileText size={18} style={{ color: 'var(--accent)' }} />
          <div style={{ flex: 1, minWidth: 0 }}>
            <h3 style={{ margin: 0, fontSize: 15, fontWeight: 700, color: 'var(--text-primary)' }}>
              Push test suite to cert simulator
            </h3>
            <p style={{ margin: '2px 0 0', fontSize: 11, color: 'var(--text-muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {changeTitle}
              <span className="id-mono" style={{ marginLeft: 8 }}>· subset cr-{changeId?.slice(0, 8)}</span>
            </p>
          </div>
          <button onClick={onClose} style={{
            background: 'transparent', border: 'none', cursor: 'pointer',
            color: 'var(--text-muted)', padding: 4,
          }}><X size={16} /></button>
        </div>

        {/* Body */}
        <div style={{ flex: 1, overflowY: 'auto', padding: 'var(--space-4) var(--space-5)' }}>
          {loading && (
            <div style={{ padding: 'var(--space-7)', textAlign: 'center', color: 'var(--text-muted)' }}>
              <Loader size={20} className="spin" style={{ display: 'block', margin: '0 auto var(--space-2)' }} />
              Computing diff with cert-agent…
            </div>
          )}

          {error && !applyResult && (
            <div style={{
              padding: 'var(--space-4)',
              background: 'rgba(224,108,108,0.08)',
              border: '1px solid rgba(224,108,108,0.3)',
              borderRadius: 8,
              display: 'flex', alignItems: 'flex-start', gap: 'var(--space-2)',
            }}>
              <AlertTriangle size={16} style={{ color: 'var(--danger)', flexShrink: 0, marginTop: 2 }} />
              <div>
                <p style={{ margin: 0, fontSize: 13, fontWeight: 600, color: 'var(--text-primary)' }}>Cannot proceed</p>
                <p style={{ margin: '4px 0 0', fontSize: 12, color: 'var(--text-secondary)' }}>{error}</p>
              </div>
            </div>
          )}

          {applyResult && (
            <div style={{
              padding: 'var(--space-4)',
              background: applyResult.failed?.length ? 'rgba(218,153,34,0.08)' : 'rgba(63,185,80,0.08)',
              border: `1px solid ${applyResult.failed?.length ? 'rgba(218,153,34,0.3)' : 'rgba(63,185,80,0.3)'}`,
              borderRadius: 8,
            }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', marginBottom: 'var(--space-2)' }}>
                <CheckCircle size={16} style={{ color: applyResult.failed?.length ? '#d29922' : '#3fb950' }} />
                <span style={{ fontSize: 14, fontWeight: 600, color: 'var(--text-primary)' }}>Push complete</span>
              </div>
              <p style={{ margin: 0, fontSize: 12, color: 'var(--text-secondary)' }}>
                <strong>{applyResult.applied}</strong> applied, <strong>{applyResult.skipped}</strong> skipped
                {(applyResult.failed?.length || 0) > 0 && (
                  <>, <strong style={{ color: 'var(--danger)' }}>{applyResult.failed.length} failed</strong></>
                )}
                · subset <code className="id-mono">{applyResult.subset}</code>
              </p>
              {(applyResult.flow_registrations?.length || 0) > 0 && (
                <p style={{ margin: '6px 0 0', fontSize: 11, color: 'var(--text-muted)' }}>
                  Flows registered:{' '}
                  {applyResult.flow_registrations.map((f, i) => (
                    <span key={i} style={{ marginRight: 8 }}>
                      <code className="id-mono">{f.flow_code}</code>
                      {' '}
                      {f.status === 'ok'
                        ? <span style={{ color: '#3fb950' }}>✓</span>
                        : <span style={{ color: 'var(--danger)' }} title={f.error}>✗</span>}
                    </span>
                  ))}
                </p>
              )}
            </div>
          )}

          {!loading && !error && diff && !applyResult && (
            <>
              {/* Banner */}
              <div style={{
                padding: 'var(--space-3) var(--space-4)',
                marginBottom: 'var(--space-3)',
                background: 'var(--bg-elevated)',
                border: '1px solid var(--border)',
                borderRadius: 8,
                fontSize: 12, color: 'var(--text-secondary)',
              }}>
                Cert-agent currently has <strong>{diff.existing_count}</strong> TC{diff.existing_count !== 1 ? 's' : ''} in this subset.
                Phase A produced <strong>{diff.plan_count}</strong>.
                {(diff.skipped_parse?.length || 0) > 0 && (
                  <> {diff.skipped_parse.length} skipped due to parse errors. </>
                )}
                {(initiatorBreakdown.npci + initiatorBreakdown.bank + initiatorBreakdown.unknown) > 0 && (
                  <div style={{ marginTop: 6, display: 'flex', gap: 'var(--space-3)', alignItems: 'center', flexWrap: 'wrap' }}>
                    <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>Initiator split:</span>
                    <span style={{ fontSize: 12 }}>
                      <strong style={{ color: INITIATOR_COLORS.NPCI }}>{initiatorBreakdown.npci}</strong>
                      <span style={{ color: 'var(--text-muted)', marginLeft: 4 }}>authority-initiated</span>
                    </span>
                    <span style={{ fontSize: 12 }}>
                      <strong style={{ color: INITIATOR_COLORS.BANK }}>{initiatorBreakdown.bank}</strong>
                      <span style={{ color: 'var(--text-muted)', marginLeft: 4 }}>Bank-initiated</span>
                    </span>
                    {initiatorBreakdown.unknown > 0 && (
                      <span style={{ fontSize: 12 }}>
                        <strong style={{ color: 'var(--warning, #d29922)' }}>{initiatorBreakdown.unknown}</strong>
                        <span style={{ color: 'var(--text-muted)', marginLeft: 4 }}>
                          unknown — set <code className="id-mono">txn_initiated_by</code> on the stub
                        </span>
                      </span>
                    )}
                  </div>
                )}
              </div>

              {/* Skipped-parse list */}
              {(diff.skipped_parse?.length || 0) > 0 && (
                <details style={{ marginBottom: 'var(--space-3)' }}>
                  <summary style={{ fontSize: 11, color: 'var(--warning)', cursor: 'pointer' }}>
                    {diff.skipped_parse.length} TC{diff.skipped_parse.length !== 1 ? 's' : ''} skipped at parse time — click to inspect
                  </summary>
                  <div style={{
                    marginTop: 6, padding: 'var(--space-2)',
                    background: 'var(--bg-base)', border: '1px solid var(--border-subtle)',
                    borderRadius: 4, fontSize: 11, maxHeight: 180, overflow: 'auto',
                  }}>
                    {diff.skipped_parse.map((s, i) => (
                      <div key={i} style={{ padding: '2px 0' }}>
                        <span className="id-mono" style={{ color: 'var(--text-muted)' }}>{s.tc_id}</span>
                        {' — '}
                        <span style={{ color: 'var(--text-secondary)' }}>{s.reason}</span>
                      </div>
                    ))}
                  </div>
                </details>
              )}

              {/* New APIs detected — flow_registration cards (engine-authored OR blank) */}
              {Object.keys(flowRegs).length > 0 && (
                <div style={{
                  marginBottom: 'var(--space-3)',
                  border: '1px solid rgba(188,140,255,0.4)',
                  borderRadius: 8,
                  background: 'rgba(188,140,255,0.06)',
                  overflow: 'hidden',
                }}>
                  <div style={{
                    padding: 'var(--space-2) var(--space-4)',
                    borderBottom: '1px solid rgba(188,140,255,0.2)',
                    display: 'flex', alignItems: 'center', gap: 'var(--space-2)',
                  }}>
                    <Zap size={14} color="#bc8cff" />
                    <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-primary)' }}>
                      New APIs detected
                    </span>
                    <span style={{ fontSize: 11, color: '#bc8cff', fontWeight: 700 }}>
                      ({Object.keys(flowRegs).length})
                    </span>
                    <span style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--text-muted)' }}>
                      {(diff.proposed_flow_defs?.length || 0) > 0
                        ? 'pre-filled by Phase A — review before push'
                        : 'fill in to register; review before push'}
                    </span>
                  </div>
                  <div style={{ padding: 'var(--space-3) var(--space-4)' }}>
                    {Object.values(flowRegs).map(r => <FlowRegCard key={r.api_request} r={r} patch={patchFlowReg} />)}
                  </div>
                </div>
              )}

              {/* Added */}
              <Section
                title="Added" count={diff.added?.length || 0} color="#3fb950" icon={Plus}
                defaultOpen={true}
                allChecked={(diff.added || []).every(t => decisions[t.tc_id] === 'add')}
                onToggleAll={(e) => toggleSection(diff.added || [], 'add', e.target.checked)}
              >
                {(diff.added || []).map(t => (
                  <RowItem key={t.tc_id} tc={t}
                    checked={decisions[t.tc_id] === 'add'}
                    onToggle={() => toggleOne(t.tc_id, 'add')} accent="#3fb950" />
                ))}
              </Section>

              {/* Changed */}
              <Section
                title="Changed" count={diff.changed?.length || 0} color="#d29922" icon={Edit3}
                defaultOpen={true}
                allChecked={(diff.changed || []).every(t => decisions[t.tc_id] === 'update')}
                onToggleAll={(e) => toggleSection(diff.changed || [], 'update', e.target.checked)}
              >
                {(diff.changed || []).map(t => (
                  <RowItem key={t.tc_id} tc={t.after}
                    checked={decisions[t.tc_id] === 'update'}
                    onToggle={() => toggleOne(t.tc_id, 'update')} accent="#d29922">
                    <RowFieldDiff before={t.before} after={t.after} fieldsChanged={t.fields_changed || []} />
                  </RowItem>
                ))}
              </Section>

              {/* Removed */}
              <Section
                title="Removed (will delete from cert-agent)" count={diff.removed?.length || 0}
                color="#e06c6c" icon={Trash2}
                defaultOpen={(diff.removed?.length || 0) > 0}
                allChecked={(diff.removed || []).every(t => decisions[t.tc_id] === 'delete')}
                onToggleAll={(e) => toggleSection(diff.removed || [], 'delete', e.target.checked)}
              >
                {(diff.removed || []).map(t => (
                  <RowItem key={t.tc_id} tc={t}
                    checked={decisions[t.tc_id] === 'delete'}
                    onToggle={() => toggleOne(t.tc_id, 'delete')} accent="#e06c6c" />
                ))}
              </Section>
            </>
          )}
        </div>

        {/* Footer */}
        {!applyResult && (
          <div style={{
            padding: 'var(--space-3) var(--space-5)',
            borderTop: '1px solid var(--border)',
            display: 'flex', alignItems: 'center', gap: 'var(--space-3)',
          }}>
            <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>
              Will apply: <strong style={{ color: '#3fb950' }}>{counts.add} add</strong>
              {' · '}
              <strong style={{ color: '#d29922' }}>{counts.update} update</strong>
              {' · '}
              <strong style={{ color: '#e06c6c' }}>{counts.delete} delete</strong>
              {flowRegCount > 0 && <> · <strong style={{ color: '#bc8cff' }}>{flowRegCount} new flow{flowRegCount !== 1 ? 's' : ''}</strong></>}
              {counts.skip > 0 && <> · <span>{counts.skip} skip</span></>}
            </span>
            <div style={{ marginLeft: 'auto', display: 'flex', gap: 'var(--space-2)' }}>
              <button onClick={onClose} disabled={applying} style={{
                padding: '7px 14px', background: 'transparent',
                border: '1px solid var(--border)', borderRadius: 6,
                color: 'var(--text-secondary)', fontSize: 12, cursor: 'pointer',
              }}>Cancel</button>
              <button
                onClick={handleApply}
                disabled={applying || loading || !!error || !!blockedReason ||
                          (counts.add + counts.update + counts.delete + flowRegCount) === 0}
                title={blockedReason || ''}
                style={{
                  padding: '7px 18px',
                  background: 'var(--accent)', border: 'none', borderRadius: 6,
                  color: 'white', fontSize: 12, fontWeight: 700,
                  cursor: applying || loading || !!error || !!blockedReason ? 'not-allowed' : 'pointer',
                  opacity: applying || loading || !!error || !!blockedReason ||
                           (counts.add + counts.update + counts.delete + flowRegCount) === 0 ? 0.5 : 1,
                }}
              >
                {applying ? <Loader size={12} className="spin" /> : 'Confirm push'}
              </button>
            </div>
          </div>
        )}

        {applyResult && (
          <div style={{
            padding: 'var(--space-3) var(--space-5)',
            borderTop: '1px solid var(--border)',
            display: 'flex', justifyContent: 'flex-end',
          }}>
            <button onClick={onClose} style={{
              padding: '7px 18px',
              background: 'var(--accent)', border: 'none', borderRadius: 6,
              color: 'white', fontSize: 12, fontWeight: 700, cursor: 'pointer',
            }}>Close</button>
          </div>
        )}
      </div>
    </div>
  )
}

function FlowRegCard({ r, patch }) {
  const [showXml, setShowXml] = useState(false)
  const isLowEmpty = r.confidence === 'low' && !(r.request_xml_template || '').trim()
  return (
    <div style={{
      padding: 'var(--space-3)',
      marginBottom: 'var(--space-2)',
      background: 'var(--bg-base)',
      border: `1px solid ${r.checked ? (isLowEmpty ? 'rgba(224,108,108,0.5)' : 'rgba(188,140,255,0.5)') : 'var(--border-subtle)'}`,
      borderRadius: 6,
      opacity: r.checked ? 1 : 0.6,
    }}>
      <label style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', cursor: 'pointer', marginBottom: r.checked ? 'var(--space-2)' : 0 }}>
        <input type="checkbox" checked={!!r.checked} onChange={e => patch(r.api_request, { checked: e.target.checked })} style={{ accentColor: '#bc8cff' }} />
        <span className="id-mono" style={{ fontWeight: 700, color: 'var(--text-primary)' }}>{r.api_request}</span>
        {r.prepopulated && (
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 3, fontSize: 10, color: '#bc8cff', background: 'rgba(188,140,255,0.12)', border: '1px solid rgba(188,140,255,0.4)', padding: '1px 6px', borderRadius: 4 }}>
            <Sparkles size={10} /> Pre-populated from Phase A
          </span>
        )}
        <ConfidenceChip confidence={r.confidence} />
      </label>
      {r.checked && (
        <>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 'var(--space-2)', marginBottom: r.checked ? 6 : 0 }}>
            <FieldInput label="Flow code" value={r.flow_code || ''} onChange={v => patch(r.api_request, { flow_code: v })} placeholder="DISPUTE" />
            <FieldInput label="Resp api"  value={r.api_response || ''} onChange={v => patch(r.api_request, { api_response: v })} placeholder="RespDispute" />
            <FieldInput label="Endpoint"  value={r.simulator_endpoint || ''} onChange={v => patch(r.api_request, { simulator_endpoint: v })} placeholder="/execute" />
            <FieldInput label="Role"      value={r.role || ''} onChange={v => patch(r.api_request, { role: v })} placeholder="REMITTER_BANK" />
            <FieldInput label="Resp codes (csv)" value={r.expected_resp_codes || ''} onChange={v => patch(r.api_request, { expected_resp_codes: v })} placeholder="00,DSP01" />
            <FieldInput label="Description" value={r.description || ''} onChange={v => patch(r.api_request, { description: v })} />
          </div>
          {r.source && (
            <details style={{ marginBottom: 6 }}>
              <summary style={{ fontSize: 10, color: 'var(--text-muted)', cursor: 'pointer' }}>show source</summary>
              <code style={{ display: 'block', marginTop: 4, padding: 6, background: 'var(--bg-elevated)', borderRadius: 4, fontSize: 10, color: 'var(--text-secondary)' }}>
                {r.source}
              </code>
            </details>
          )}
          <button
            onClick={() => setShowXml(v => !v)}
            style={{
              fontSize: 10, color: isLowEmpty ? 'var(--danger)' : 'var(--text-muted)',
              background: 'none', border: 'none', cursor: 'pointer', padding: 0,
              display: 'inline-flex', alignItems: 'center', gap: 3,
            }}
          >
            {showXml ? <ChevronDown size={10} /> : <ChevronRight size={10} />}
            request_xml_template {(r.request_xml_template || '').length} chars
            {isLowEmpty && ' — empty (operator must fill before push)'}
          </button>
          {showXml && (
            <textarea
              value={r.request_xml_template || ''}
              onChange={e => patch(r.api_request, { request_xml_template: e.target.value })}
              spellCheck={false}
              style={{
                width: '100%', minHeight: 160, marginTop: 4,
                padding: 'var(--space-2)', fontSize: 11,
                fontFamily: 'ui-monospace, monospace',
                background: 'var(--bg-elevated)',
                border: '1px solid var(--border-subtle)',
                borderRadius: 4, color: 'var(--text-primary)',
                resize: 'vertical',
              }}
              placeholder='<?xml version="1.0"?><...> with {{txn_id}} {{payer_vpa}} {{amount}} placeholders'
            />
          )}
        </>
      )}
    </div>
  )
}

function FieldInput({ label, value, onChange, placeholder, colSpan }) {
  return (
    <label style={{
      display: 'flex', flexDirection: 'column', gap: 2,
      gridColumn: colSpan ? `span ${colSpan}` : undefined,
    }}>
      <span style={{ fontSize: 9, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>
        {label}
      </span>
      <input type="text" value={value} onChange={e => onChange(e.target.value)} placeholder={placeholder}
        style={{
          padding: '5px 7px', fontSize: 11,
          fontFamily: 'ui-monospace, monospace',
          background: 'var(--bg-elevated)',
          border: '1px solid var(--border-subtle)',
          borderRadius: 4, color: 'var(--text-primary)',
        }}
      />
    </label>
  )
}

function RowItem({ tc, checked, onToggle, accent, children }) {
  return (
    <div onClick={onToggle} style={{
      display: 'flex', alignItems: 'flex-start', gap: 'var(--space-2)',
      padding: 'var(--space-2) var(--space-4)',
      cursor: 'pointer',
      borderTop: '1px solid var(--border-subtle)',
      background: checked ? `${accent}08` : 'transparent',
      opacity: checked ? 1 : 0.55,
    }}>
      <input type="checkbox" checked={checked} onChange={onToggle} onClick={e => e.stopPropagation()}
        style={{ marginTop: 4, accentColor: accent, flexShrink: 0 }} />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', flexWrap: 'wrap' }}>
          <span className="id-mono" style={{ color: 'var(--text-primary)', fontWeight: 700 }}>
            {tc?.tc_id || '?'}
          </span>
          {tc?.flow && <FlowChip flow={tc.flow} />}
          {tc?.expected_resp_code && <CodeChip code={tc.expected_resp_code} />}
          <InitiatorChip initiatedBy={tc?.initiated_by} pspAs={tc?.psp_as} />
          {tc?.role && <span style={{ fontSize: 9, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>{tc.role}</span>}
        </div>
        {tc?.name && (
          <p style={{
            margin: '2px 0 0', fontSize: 12, color: 'var(--text-secondary)',
            overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: '100%',
          }}>{tc.name}</p>
        )}
        {children}
      </div>
    </div>
  )
}
