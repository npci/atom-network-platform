// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useMemo, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import {
  AlertCircle, ChevronDown, ChevronRight, Filter, Loader,
  RefreshCw, ShieldCheck, FileText, RotateCw,
} from 'lucide-react'
import { useAuth } from '../../hooks/useAuth'
import { evalApi } from '../../services/api'

const VERDICTS = ['PASS', 'WARN', 'FAIL']
const POLICY_MODES = ['disabled', 'advisory', 'soft_gate', 'hard_gate']
const PAGE_SIZE = 100

// Display order should match the Eval Policy admin page so operators
// see checkpoints in the same Phase A flow sequence.
const CHECKPOINT_DISPLAY_ORDER = [
  'initial_to_prompt_enhanced',
  'prompt_to_research',
  'research_to_canvas',
  'canvas_to_clarification',
  'clarification_to_brd',
  'brd_to_tech_spec',
  'tech_spec_to_xsd',
  'product_kit_to_phase_c_communication',
  'phase_c_query_to_po_response',
]

function fmtTime(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleString('en-IN', {
    day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
}

function relTime(iso) {
  if (!iso) return ''
  const ms = Date.now() - new Date(iso).getTime()
  const s = Math.floor(ms / 1000)
  if (s < 60) return `${s}s ago`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  const d = Math.floor(h / 24)
  return `${d}d ago`
}

function verdictPalette(v) {
  if (v === 'PASS') return { bg: 'rgba(76,175,125,0.10)', border: 'rgba(76,175,125,0.30)', color: 'var(--success)' }
  if (v === 'WARN') return { bg: 'rgba(218,119,86,0.10)', border: 'rgba(218,119,86,0.30)', color: 'var(--accent)' }
  if (v === 'FAIL') return { bg: 'rgba(224,108,108,0.10)', border: 'rgba(224,108,108,0.30)', color: 'var(--danger)' }
  return { bg: 'var(--bg-elevated)', border: 'var(--border)', color: 'var(--text-muted)' }
}

function VerdictChip({ value, count }) {
  const p = verdictPalette(value)
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: '4px',
      padding: '2px 8px', fontSize: '11px', fontWeight: 700,
      borderRadius: '4px', border: `1px solid ${p.border}`, background: p.bg, color: p.color,
    }}>
      {typeof count === 'number' && <span style={{ opacity: 0.85 }}>{count}</span>}
      {value}
    </span>
  )
}

function ModeChip({ value }) {
  const isHard = value === 'hard_gate'
  const isSoft = value === 'soft_gate'
  return (
    <span style={{
      display: 'inline-block', padding: '2px 8px', fontSize: '11px', fontWeight: 600,
      borderRadius: '4px',
      border: `1px solid ${isHard ? 'rgba(224,108,108,0.30)' : isSoft ? 'rgba(218,119,86,0.30)' : 'var(--border)'}`,
      background: isHard ? 'rgba(224,108,108,0.08)' : isSoft ? 'rgba(218,119,86,0.08)' : 'var(--bg-base)',
      color: isHard ? 'var(--danger)' : isSoft ? 'var(--accent)' : 'var(--text-muted)',
    }}>{value}</span>
  )
}

function Code({ children }) {
  return (
    <span style={{
      fontFamily: 'ui-monospace,SFMono-Regular,Menlo,Consolas,monospace',
      fontSize: '11px', padding: '1px 5px',
      borderRadius: '3px', background: 'var(--bg-base)', border: '1px solid var(--border-subtle)',
      color: 'var(--text-secondary)',
    }}>{children}</span>
  )
}

export default function EvalLogs() {
  const { user } = useAuth()
  const isAdmin = user?.role === 'admin'
  const queryClient = useQueryClient()
  const [rerunBusy, setRerunBusy] = useState(() => new Set())
  const [rerunError, setRerunError] = useState(null)
  const [expandedGroups, setExpandedGroups] = useState(() => new Set())
  const [expandedVerdicts, setExpandedVerdicts] = useState(() => new Set())
  const [filters, setFilters] = useState({
    change_id: '',
    checkpoint: '',
    verdict: '',
    policy_mode: '',
    is_override: '',
    since: '',
  })
  const [offset, setOffset] = useState(0)

  const queryParams = useMemo(() => {
    const p = { limit: PAGE_SIZE, offset }
    for (const [k, v] of Object.entries(filters)) {
      if (v !== '' && v !== null && v !== undefined) p[k] = v
    }
    return p
  }, [filters, offset])

  const { data, isFetching, refetch } = useQuery({
    queryKey: ['eval-logs', queryParams],
    queryFn: () => evalApi.listAllVerdicts(queryParams).then(r => r.data),
    keepPreviousData: true,
  })

  const items = data?.items || []
  const total = data?.total || 0
  const showingFrom = total === 0 ? 0 : offset + 1
  const showingTo = Math.min(offset + items.length, total)

  // Group verdicts by change_request_id (preserve newest-first order)
  const groups = useMemo(() => {
    const map = new Map()
    for (const item of items) {
      if (!map.has(item.change_request_id)) {
        map.set(item.change_request_id, {
          id: item.change_request_id,
          title: item.change_request_title || item.change_request_id,
          verdicts: [],
        })
      }
      map.get(item.change_request_id).verdicts.push(item)
    }
    for (const g of map.values()) {
      g.counts = { PASS: 0, WARN: 0, FAIL: 0 }
      g.overrides = 0
      g.has_hard_gate = false
      g.has_soft_gate = false
      g.latest_at = g.verdicts[0]?.created_at
      g.distinct_checkpoints = new Set()
      for (const v of g.verdicts) {
        g.counts[v.verdict] = (g.counts[v.verdict] || 0) + 1
        if (v.is_override) g.overrides += 1
        if (v.policy_mode === 'hard_gate') g.has_hard_gate = true
        if (v.policy_mode === 'soft_gate') g.has_soft_gate = true
        g.distinct_checkpoints.add(v.checkpoint_id)
      }
      g.distinct_checkpoint_count = g.distinct_checkpoints.size
    }
    return Array.from(map.values())
  }, [items])

  // If a single group survives the filters, auto-expand it for the user.
  const effectiveExpandedGroups = useMemo(() => {
    if (groups.length === 1) {
      const next = new Set(expandedGroups)
      next.add(groups[0].id)
      return next
    }
    return expandedGroups
  }, [groups, expandedGroups])

  const updateFilter = (key, value) => {
    setFilters((prev) => ({ ...prev, [key]: value }))
    setOffset(0)
  }

  const toggleGroup = (id) => {
    setExpandedGroups((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id); else next.add(id)
      return next
    })
  }

  const toggleVerdict = (id) => {
    setExpandedVerdicts((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id); else next.add(id)
      return next
    })
  }

  const expandAll = () => setExpandedGroups(new Set(groups.map((g) => g.id)))
  const collapseAll = () => setExpandedGroups(new Set())

  const handleRerun = async (changeId, checkpointId, key) => {
    setRerunError(null)
    setRerunBusy((prev) => new Set(prev).add(key))
    try {
      await evalApi.rerunCheckpoint(changeId, checkpointId)
      queryClient.invalidateQueries({ queryKey: ['eval-logs'] })
    } catch (err) {
      setRerunError(err?.response?.data?.detail || 'Re-run failed')
    } finally {
      setRerunBusy((prev) => {
        const next = new Set(prev)
        next.delete(key)
        return next
      })
    }
  }

  return (
    <div style={{ padding: '32px 40px', maxWidth: 1300, margin: '0 auto' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: '14px', marginBottom: '20px' }}>
        <div>
          <h1 style={{ margin: '0 0 4px', fontSize: '20px', fontWeight: 600, color: 'var(--text-primary)' }}>
            Eval Logs
          </h1>
          <p style={{ margin: 0, fontSize: '13px', color: 'var(--text-muted)' }}>
            Every checkpoint verdict, grouped by change. Click a row to drill in.
          </p>
        </div>
        <div style={{ display: 'flex', gap: '8px' }}>
          <button
            onClick={expandAll}
            disabled={groups.length === 0}
            title="Expand all groups"
            style={{
              padding: '7px 11px', borderRadius: '6px',
              border: '1px solid var(--border)', background: 'var(--bg-elevated)',
              color: 'var(--text-secondary)', fontSize: '12px', fontWeight: 600,
              cursor: groups.length === 0 ? 'not-allowed' : 'pointer',
              opacity: groups.length === 0 ? 0.6 : 1,
            }}
          >Expand all</button>
          <button
            onClick={collapseAll}
            disabled={expandedGroups.size === 0}
            title="Collapse all groups"
            style={{
              padding: '7px 11px', borderRadius: '6px',
              border: '1px solid var(--border)', background: 'var(--bg-elevated)',
              color: 'var(--text-secondary)', fontSize: '12px', fontWeight: 600,
              cursor: expandedGroups.size === 0 ? 'not-allowed' : 'pointer',
              opacity: expandedGroups.size === 0 ? 0.6 : 1,
            }}
          >Collapse all</button>
          <button
            onClick={() => refetch()}
            disabled={isFetching}
            style={{
              display: 'inline-flex', alignItems: 'center', gap: '6px',
              padding: '7px 12px', borderRadius: '6px',
              border: '1px solid var(--border)', background: 'var(--bg-elevated)',
              color: 'var(--text-secondary)', cursor: isFetching ? 'wait' : 'pointer',
              fontSize: '12px', fontWeight: 600,
            }}
          >
            {isFetching ? <Loader size={12} style={{ animation: 'spin 1s linear infinite' }} /> : <RefreshCw size={12} />}
            Refresh
          </button>
        </div>
      </div>

      {/* Filters */}
      <div style={{
        borderRadius: '8px', border: '1px solid var(--border)', background: 'var(--bg-elevated)',
        padding: '14px 16px', marginBottom: '14px',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '7px', marginBottom: '10px', color: 'var(--text-secondary)' }}>
          <Filter size={13} />
          <span style={{ fontSize: '12px', fontWeight: 700, letterSpacing: '0.05em', textTransform: 'uppercase' }}>Filters</span>
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(6, minmax(0, 1fr))', gap: '8px' }}>
          <input
            placeholder="Change ID"
            value={filters.change_id}
            onChange={(e) => updateFilter('change_id', e.target.value.trim())}
            style={{
              padding: '7px 9px', fontSize: '12px', borderRadius: '6px',
              border: '1px solid var(--border)', background: 'var(--bg-input)', color: 'var(--text-primary)',
              fontFamily: 'ui-monospace,SFMono-Regular,Menlo,Consolas,monospace',
            }}
          />
          <select
            value={filters.checkpoint}
            onChange={(e) => updateFilter('checkpoint', e.target.value)}
            style={{
              padding: '7px 9px', fontSize: '12px', borderRadius: '6px',
              border: '1px solid var(--border)', background: 'var(--bg-input)', color: 'var(--text-primary)',
            }}
          >
            <option value="">Any checkpoint</option>
            {CHECKPOINT_DISPLAY_ORDER.map((cp) => (
              <option key={cp} value={cp}>{cp}</option>
            ))}
          </select>
          <select
            value={filters.verdict}
            onChange={(e) => updateFilter('verdict', e.target.value)}
            style={{
              padding: '7px 9px', fontSize: '12px', borderRadius: '6px',
              border: '1px solid var(--border)', background: 'var(--bg-input)', color: 'var(--text-primary)',
            }}
          >
            <option value="">Any verdict</option>
            {VERDICTS.map((v) => <option key={v} value={v}>{v}</option>)}
          </select>
          <select
            value={filters.policy_mode}
            onChange={(e) => updateFilter('policy_mode', e.target.value)}
            style={{
              padding: '7px 9px', fontSize: '12px', borderRadius: '6px',
              border: '1px solid var(--border)', background: 'var(--bg-input)', color: 'var(--text-primary)',
            }}
          >
            <option value="">Any policy mode</option>
            {POLICY_MODES.map((m) => <option key={m} value={m}>{m}</option>)}
          </select>
          <select
            value={filters.is_override}
            onChange={(e) => updateFilter('is_override', e.target.value)}
            style={{
              padding: '7px 9px', fontSize: '12px', borderRadius: '6px',
              border: '1px solid var(--border)', background: 'var(--bg-input)', color: 'var(--text-primary)',
            }}
          >
            <option value="">Overrides: any</option>
            <option value="true">Only overrides</option>
            <option value="false">Exclude overrides</option>
          </select>
          <input
            type="datetime-local"
            value={filters.since}
            onChange={(e) => updateFilter('since', e.target.value)}
            title="Show verdicts from this time forward"
            style={{
              padding: '7px 9px', fontSize: '12px', borderRadius: '6px',
              border: '1px solid var(--border)', background: 'var(--bg-input)', color: 'var(--text-primary)',
            }}
          />
        </div>
      </div>

      {/* Groups */}
      <div style={{
        borderRadius: '8px', border: '1px solid var(--border)', background: 'var(--bg-elevated)',
        overflow: 'hidden',
      }}>
        {groups.length === 0 && !isFetching && (
          <div style={{ padding: '40px 20px', textAlign: 'center', color: 'var(--text-muted)', fontSize: '13px' }}>
            No verdicts match the current filters.
          </div>
        )}

        {groups.map((g, gi) => {
          const isExp = effectiveExpandedGroups.has(g.id)
          return (
            <div key={g.id} style={{
              borderTop: gi === 0 ? 'none' : '1px solid var(--border)',
              background: isExp ? 'var(--bg-base)' : 'transparent',
            }}>
              {/* Group header */}
              <div
                onClick={() => toggleGroup(g.id)}
                style={{
                  display: 'grid',
                  gridTemplateColumns: '24px 1fr auto auto auto',
                  gap: '14px',
                  padding: '14px 16px', alignItems: 'center', cursor: 'pointer',
                }}
              >
                <span style={{ color: 'var(--text-muted)' }}>
                  {isExp ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
                </span>
                <div style={{ minWidth: 0 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                    <FileText size={13} style={{ color: 'var(--text-muted)', flexShrink: 0 }} />
                    <Link
                      to={`/changes/${g.id}`}
                      onClick={(e) => e.stopPropagation()}
                      style={{
                        color: 'var(--text-primary)', fontWeight: 600, fontSize: '13px',
                        textDecoration: 'none', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                      }}
                    >
                      {g.title}
                    </Link>
                    <Code>{g.id.slice(0, 8)}</Code>
                  </div>
                  <p style={{ margin: '3px 0 0', fontSize: '11px', color: 'var(--text-muted)' }}>
                    {g.verdicts.length} verdict{g.verdicts.length === 1 ? '' : 's'} ·{' '}
                    {g.distinct_checkpoint_count} checkpoint{g.distinct_checkpoint_count === 1 ? '' : 's'} ·{' '}
                    latest {relTime(g.latest_at)}
                  </p>
                </div>

                {/* Verdict count chips */}
                <div style={{ display: 'flex', gap: '5px', flexShrink: 0 }}>
                  {g.counts.FAIL > 0 && <VerdictChip value="FAIL" count={g.counts.FAIL} />}
                  {g.counts.WARN > 0 && <VerdictChip value="WARN" count={g.counts.WARN} />}
                  {g.counts.PASS > 0 && <VerdictChip value="PASS" count={g.counts.PASS} />}
                </div>

                {/* Policy mode indicator */}
                <div style={{ display: 'flex', gap: '5px', flexShrink: 0 }}>
                  {g.has_hard_gate && <ModeChip value="hard_gate" />}
                  {g.has_soft_gate && !g.has_hard_gate && <ModeChip value="soft_gate" />}
                </div>

                {/* Override badge */}
                <div style={{ flexShrink: 0, minWidth: 90, textAlign: 'right' }}>
                  {g.overrides > 0 ? (
                    <span style={{
                      display: 'inline-flex', alignItems: 'center', gap: '4px',
                      padding: '2px 8px', fontSize: '11px', fontWeight: 600, borderRadius: '4px',
                      border: '1px solid rgba(110,168,220,0.30)', background: 'rgba(110,168,220,0.10)', color: '#6ea8dc',
                    }}>
                      <ShieldCheck size={11} /> {g.overrides} override{g.overrides === 1 ? '' : 's'}
                    </span>
                  ) : (
                    <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>—</span>
                  )}
                </div>
              </div>

              {/* Verdicts inside group */}
              {isExp && (
                <div style={{ borderTop: '1px solid var(--border-subtle)' }}>
                  {/* Column header for the verdicts list */}
                  <div style={{
                    display: 'grid',
                    gridTemplateColumns: '40px 170px 1fr 80px 110px 90px 100px',
                    padding: '8px 16px 8px 56px',
                    fontSize: '10px', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em',
                    color: 'var(--text-muted)', borderBottom: '1px solid var(--border-subtle)',
                  }}>
                    <span></span>
                    <span>Time</span>
                    <span>Checkpoint</span>
                    <span>Verdict</span>
                    <span>Policy mode</span>
                    <span>Confidence</span>
                    <span>Override</span>
                  </div>

                  {g.verdicts.map((row) => {
                    const isVExp = expandedVerdicts.has(row.id)
                    return (
                      <div key={row.id} style={{ borderBottom: '1px solid var(--border-subtle)' }}>
                        <div
                          onClick={() => toggleVerdict(row.id)}
                          style={{
                            display: 'grid',
                            gridTemplateColumns: '40px 170px 1fr 80px 110px 90px 100px',
                            padding: '9px 16px 9px 56px', alignItems: 'center', cursor: 'pointer',
                            fontSize: '12px', color: 'var(--text-primary)',
                          }}
                        >
                          <span style={{ color: 'var(--text-muted)' }}>
                            {isVExp ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                          </span>
                          <span style={{ color: 'var(--text-secondary)' }}>{fmtTime(row.created_at)}</span>
                          <span style={{ fontFamily: 'ui-monospace,monospace', fontSize: '11px', color: 'var(--text-secondary)' }}>
                            {row.checkpoint_id}
                          </span>
                          <span><VerdictChip value={row.verdict} /></span>
                          <span><ModeChip value={row.policy_mode} /></span>
                          <span style={{ color: 'var(--text-secondary)' }}>
                            {typeof row.confidence === 'number' ? `${Math.round(row.confidence * 100)}%` : '—'}
                          </span>
                          <span>
                            {row.is_override ? (
                              <span style={{
                                display: 'inline-flex', alignItems: 'center', gap: '4px',
                                padding: '2px 8px', fontSize: '11px', fontWeight: 600, borderRadius: '4px',
                                border: '1px solid rgba(110,168,220,0.30)', background: 'rgba(110,168,220,0.10)', color: '#6ea8dc',
                              }}>
                                <ShieldCheck size={11} /> override
                              </span>
                            ) : (
                              <span style={{ color: 'var(--text-muted)', fontSize: '11px' }}>—</span>
                            )}
                          </span>
                        </div>

                        {isVExp && (
                          <div style={{ padding: '6px 18px 16px 80px' }}>
                            <div style={{ display: 'grid', gap: '12px' }}>
                              <div>
                                <p style={{ margin: '0 0 4px', fontSize: '10px', fontWeight: 700, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>Reasons</p>
                                {(row.reasons && row.reasons.length > 0) ? (
                                  <ul style={{ margin: 0, paddingLeft: '18px', color: 'var(--text-secondary)', fontSize: '12px', lineHeight: 1.55 }}>
                                    {row.reasons.map((r, i) => <li key={i}>{r}</li>)}
                                  </ul>
                                ) : (
                                  <p style={{ margin: 0, color: 'var(--text-muted)', fontSize: '12px' }}>No reasons recorded.</p>
                                )}
                              </div>

                              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>
                                <div>
                                  <p style={{ margin: '0 0 4px', fontSize: '10px', fontWeight: 700, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>Hard-fail codes</p>
                                  {row.hard_fail_codes?.length ? (
                                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: '5px' }}>
                                      {row.hard_fail_codes.map((c) => (
                                        <span key={c} style={{
                                          fontSize: '11px', fontWeight: 600, padding: '2px 8px',
                                          borderRadius: '4px', border: '1px solid rgba(224,108,108,0.30)',
                                          background: 'rgba(224,108,108,0.10)', color: 'var(--danger)',
                                        }}>
                                          <AlertCircle size={10} style={{ display: 'inline', marginRight: 4 }} />
                                          {c}
                                        </span>
                                      ))}
                                    </div>
                                  ) : (
                                    <p style={{ margin: 0, color: 'var(--text-muted)', fontSize: '12px' }}>—</p>
                                  )}
                                </div>
                                <div>
                                  <p style={{ margin: '0 0 4px', fontSize: '10px', fontWeight: 700, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>Warn codes</p>
                                  {row.warn_codes?.length ? (
                                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: '5px' }}>
                                      {row.warn_codes.map((c) => (
                                        <span key={c} style={{
                                          fontSize: '11px', fontWeight: 600, padding: '2px 8px',
                                          borderRadius: '4px', border: '1px solid rgba(218,119,86,0.30)',
                                          background: 'rgba(218,119,86,0.10)', color: 'var(--accent)',
                                        }}>{c}</span>
                                      ))}
                                    </div>
                                  ) : (
                                    <p style={{ margin: 0, color: 'var(--text-muted)', fontSize: '12px' }}>—</p>
                                  )}
                                </div>
                              </div>

                              {row.is_override && (
                                <div style={{
                                  padding: '9px 12px', borderRadius: '6px',
                                  border: '1px solid rgba(110,168,220,0.30)', background: 'rgba(110,168,220,0.06)',
                                }}>
                                  <p style={{ margin: '0 0 4px', fontSize: '11px', fontWeight: 700, color: '#6ea8dc', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
                                    Override · by {row.override_actor || 'unknown'}
                                  </p>
                                  <p style={{ margin: 0, fontSize: '12px', color: 'var(--text-secondary)' }}>
                                    {row.override_reason || '(no reason recorded)'}
                                  </p>
                                  {row.previous_verdict_id && (
                                    <p style={{ margin: '4px 0 0', fontSize: '11px', color: 'var(--text-muted)' }}>
                                      Previous verdict: <Code>{row.previous_verdict_id}</Code>
                                    </p>
                                  )}
                                </div>
                              )}

                              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: '10px', fontSize: '11px', color: 'var(--text-muted)' }}>
                                <div>
                                  <span style={{ display: 'block', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Transition</span>
                                  <span style={{ color: 'var(--text-secondary)' }}>{row.from_stage} → {row.to_stage}</span>
                                </div>
                                <div>
                                  <span style={{ display: 'block', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Rubric</span>
                                  <span style={{ color: 'var(--text-secondary)' }}>{row.rubric_version}</span>
                                </div>
                                <div>
                                  <span style={{ display: 'block', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Latency</span>
                                  <span style={{ color: 'var(--text-secondary)' }}>{row.latency_ms} ms</span>
                                </div>
                                <div>
                                  <span style={{ display: 'block', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Verdict ID</span>
                                  <Code>{row.id}</Code>
                                </div>
                              </div>

                              {isAdmin && (
                                <div style={{
                                  marginTop: '4px',
                                  display: 'flex',
                                  alignItems: 'center',
                                  justifyContent: 'flex-end',
                                  gap: '10px',
                                }}>
                                  <button
                                    type="button"
                                    disabled={rerunBusy.has(row.id)}
                                    onClick={() => handleRerun(row.change_request_id, row.checkpoint_id, row.id)}
                                    style={{
                                      display: 'inline-flex', alignItems: 'center', gap: '6px',
                                      padding: '6px 11px', borderRadius: '6px',
                                      border: '1px solid var(--border)', background: 'var(--bg-elevated)',
                                      color: 'var(--text-secondary)', fontSize: '11px', fontWeight: 600,
                                      cursor: rerunBusy.has(row.id) ? 'wait' : 'pointer',
                                    }}
                                    title="Re-evaluate this checkpoint with the latest persisted artifacts"
                                  >
                                    {rerunBusy.has(row.id)
                                      ? <Loader size={11} style={{ animation: 'spin 1s linear infinite' }} />
                                      : <RotateCw size={11} />}
                                    Re-run eval
                                  </button>
                                </div>
                              )}
                            </div>
                          </div>
                        )}
                      </div>
                    )
                  })}
                </div>
              )}
            </div>
          )
        })}
      </div>

      {rerunError && (
        <div style={{
          marginTop: '10px',
          padding: '9px 12px',
          borderRadius: '6px',
          background: 'rgba(224,108,108,0.10)',
          border: '1px solid rgba(224,108,108,0.30)',
          color: 'var(--danger)',
          fontSize: '12px',
        }}>{rerunError}</div>
      )}

      {/* Pagination */}
      <div style={{
        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        marginTop: '12px', fontSize: '12px', color: 'var(--text-muted)',
      }}>
        <span>
          Showing {showingFrom}–{showingTo} of {total} verdicts across {groups.length} change{groups.length === 1 ? '' : 's'}
        </span>
        <div style={{ display: 'flex', gap: '8px' }}>
          <button
            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            disabled={offset === 0}
            style={{
              padding: '6px 12px', fontSize: '12px', borderRadius: '6px',
              border: '1px solid var(--border)', background: 'var(--bg-elevated)',
              color: offset === 0 ? 'var(--text-muted)' : 'var(--text-secondary)',
              cursor: offset === 0 ? 'not-allowed' : 'pointer',
            }}
          >Previous</button>
          <button
            onClick={() => setOffset(offset + PAGE_SIZE)}
            disabled={offset + items.length >= total}
            style={{
              padding: '6px 12px', fontSize: '12px', borderRadius: '6px',
              border: '1px solid var(--border)', background: 'var(--bg-elevated)',
              color: (offset + items.length >= total) ? 'var(--text-muted)' : 'var(--text-secondary)',
              cursor: (offset + items.length >= total) ? 'not-allowed' : 'pointer',
            }}
          >Next</button>
        </div>
      </div>
    </div>
  )
}
