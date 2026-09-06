// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

/**
 * Cert Status — full lifecycle of a change request w.r.t. certification.
 *
 * Three tabs:
 *   1. Lifecycle  — vertical timeline of milestones for a selected change
 *                   (test cases generated → kit communicated → partner ack →
 *                    progress → ready → cert started → cert completed →
 *                    triage → approved → live)
 *   2. Activity   — flat user-friendly chronological log across all changes
 *                   (filterable by kind + partner + change)
 *   3. Engine     — A2A traffic feed between the Authority and the cert engine
 *                   (existing debug view, preserved)
 *
 * All data is read-only and powered by:
 *   GET /api/cert-status/timeline?change_id=&partner_id=&kinds=
 *   GET /api/cert-status/timeline/changes
 *   GET /api/certification/agent-messages
 */
import { useState, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import {
  Activity, RefreshCw, Filter, Search, ChevronRight, Cpu,
  ArrowDownRight, ArrowUpRight, FileText, Send, CheckCircle, Clock,
  Hammer, ShieldCheck, Award, Rocket, Ban, AlertTriangle, AlertCircle,
  MessagesSquare, Bot, Inbox,
} from 'lucide-react'

import { certificationApi } from '../../services/api'
import PageHeader  from '../../components/cert/PageHeader'
import KpiStrip    from '../../components/cert/KpiStrip'
import Section     from '../../components/cert/Section'
import StatusBadge from '../../components/cert/StatusBadge'
import { A2A_TASK_TYPE_LABEL, relativeTime } from '../../lib/certStatus'

// ── Visual taxonomy for timeline events ───────────────────────────────────
const KIND_META = {
  test_cases_generated: { label: 'Test cases generated', icon: FileText,    color: '#bc8cff' },
  test_suite_registered:{ label: 'Test suite synced',     icon: FileText,    color: '#3fb950' },
  kit_communicated:     { label: 'Kit delivered',        icon: Send,        color: '#58a6ff' },
  partner_acknowledged: { label: 'Acknowledged',         icon: CheckCircle, color: '#bc8cff' },
  partner_progress:     { label: 'Progress',             icon: Hammer,      color: '#d29922' },
  partner_ready:        { label: 'Ready for cert',       icon: ShieldCheck, color: '#58a6ff' },
  cert_started:         { label: 'Cert run started',     icon: Activity,    color: '#d29922' },
  cert_completed_pass:  { label: 'Cert run passed',      icon: Award,       color: '#3fb950' },
  cert_completed_fail:  { label: 'Cert run failed',      icon: AlertTriangle, color: '#e06c6c' },
  triage_generated:     { label: 'AI triage',            icon: Bot,         color: '#d29922' },
  approved_for_prod:    { label: 'Approved for prod',    icon: ShieldCheck, color: '#3fb950' },
  marked_live:          { label: 'Marked live',          icon: Rocket,      color: '#2ea676' },
  blocked:              { label: 'Blocked',              icon: Ban,         color: '#ff7f9b' },
  unblocked:            { label: 'Unblocked',            icon: CheckCircle, color: '#58a6ff' },
  withdrawn:            { label: 'Withdrawn',            icon: Ban,         color: '#8b949e' },
  query:                { label: 'Query',                icon: MessagesSquare, color: '#bc8cff' },
  clarification:        { label: 'Clarification',        icon: MessagesSquare, color: '#58a6ff' },
  defect_notice:        { label: 'Defect notice',        icon: AlertCircle, color: '#d29922' },
  defect_resolution:    { label: 'Defect resolved',      icon: CheckCircle, color: '#3fb950' },
}

const SEVERITY_COLOR = {
  info:    'var(--text-muted)',
  success: '#3fb950',
  warning: '#d29922',
  error:   '#e06c6c',
}

// Family groupings used by the Activity tab's filter chips
const KIND_FAMILIES = {
  all:           null,
  comms:         ['kit_communicated', 'partner_acknowledged', 'query', 'clarification'],
  status:        ['partner_progress', 'partner_ready', 'cert_started', 'cert_completed_pass', 'cert_completed_fail'],
  admin_actions: ['blocked', 'unblocked', 'withdrawn', 'approved_for_prod', 'marked_live'],
  defects:       ['triage_generated', 'defect_notice', 'defect_resolution'],
  artifacts:     ['test_cases_generated', 'test_suite_registered'],
}

const TABS = [
  { v: 'lifecycle', l: 'Lifecycle' },
  { v: 'activity',  l: 'Activity Log' },
  { v: 'engine',    l: 'Engine Traffic' },
]


// ════════════════════════════════════════════════════════════════════════════

export default function CertificationStatus() {
  const [tab, setTab] = useState('lifecycle')
  const [selectedChangeId, setSelectedChangeId] = useState(null)
  const [activityFamily, setActivityFamily] = useState('all')
  const [activitySearch, setActivitySearch] = useState('')
  const [engineFilter, setEngineFilter] = useState('all')

  // ── Available changes for the selector ─────────────────────────────────
  const { data: changesData } = useQuery({
    queryKey: ['cert-timeline-changes'],
    queryFn: async () => (await certificationApi.timelineChanges()).data,
  })
  const changes = changesData?.changes || []

  // Default selected change = first in list (only when on the lifecycle tab)
  if (tab === 'lifecycle' && !selectedChangeId && changes.length > 0) {
    setSelectedChangeId(changes[0].id)
  }

  // ── Tabs render ──────────────────────────────────────────────────────────
  return (
    <div style={{ padding: 'var(--space-7) var(--space-7)', maxWidth: '1280px' }}>

      <PageHeader
        icon={Activity}
        crumbs={[{ label: 'Certification' }, { label: 'Cert Status' }]}
        title="Certification Status"
        subtitle="The full lifecycle of every change with respect to certification — from Phase A test-case generation through partner readiness, cert runs, triage, and go-live."
      />

      {/* Tab strip */}
      <div role="tablist" style={{
        display: 'flex',
        gap: 'var(--space-1)',
        marginBottom: 'var(--space-5)',
        borderBottom: '1px solid var(--border)',
      }}>
        {TABS.map(t => {
          const active = tab === t.v
          return (
            <button
              key={t.v}
              role="tab"
              aria-selected={active}
              onClick={() => setTab(t.v)}
              style={{
                padding: '10px 16px',
                background: 'transparent',
                border: 'none',
                borderBottom: `2px solid ${active ? 'var(--accent)' : 'transparent'}`,
                color: active ? 'var(--accent)' : 'var(--text-secondary)',
                fontSize: '13px',
                fontWeight: active ? 600 : 500,
                cursor: 'pointer',
                marginBottom: '-1px',
              }}
            >
              {t.l}
            </button>
          )
        })}
      </div>

      {tab === 'lifecycle' && (
        <LifecycleTab
          changes={changes}
          selectedChangeId={selectedChangeId}
          setSelectedChangeId={setSelectedChangeId}
        />
      )}

      {tab === 'activity' && (
        <ActivityTab
          changes={changes}
          family={activityFamily}
          setFamily={setActivityFamily}
          search={activitySearch}
          setSearch={setActivitySearch}
        />
      )}

      {tab === 'engine' && (
        <EngineTab filter={engineFilter} setFilter={setEngineFilter} />
      )}
    </div>
  )
}


// ════════════════════════════════════════════════════════════════════════════
// Tab 1 — Lifecycle (vertical timeline for one change)
// ════════════════════════════════════════════════════════════════════════════

function LifecycleTab({ changes, selectedChangeId, setSelectedChangeId }) {
  const { data, isLoading, isFetching, refetch } = useQuery({
    queryKey: ['cert-timeline', selectedChangeId],
    queryFn: async () => (await certificationApi.timeline({ change_id: selectedChangeId, limit: 500 })).data,
    enabled: !!selectedChangeId,
    refetchInterval: 20000,
  })

  const events = data?.events || []
  const ch = changes.find(c => c.id === selectedChangeId)

  // Roll-up KPIs across this timeline
  const kpis = useMemo(() => {
    const counts = events.reduce((acc, e) => {
      acc[e.kind] = (acc[e.kind] || 0) + 1
      return acc
    }, {})
    const partners = new Set(events.map(e => e.partner_id).filter(Boolean))
    const certPasses = counts.cert_completed_pass || 0
    const certFails = counts.cert_completed_fail || 0
    const live = counts.marked_live || 0
    return {
      partners: partners.size,
      milestones: events.length,
      certPasses,
      certFails,
      live,
    }
  }, [events])

  if (changes.length === 0) {
    return (
      <Section>
        <p style={{ margin: 0, padding: 'var(--space-6)', textAlign: 'center', color: 'var(--text-muted)', fontSize: 13 }}>
          No change requests with cert activity yet. Assign a partner on a change in Phase C to start the lifecycle.
        </p>
      </Section>
    )
  }

  return (
    <>
      {/* Change selector */}
      <div style={{
        display: 'flex',
        alignItems: 'center',
        gap: 'var(--space-3)',
        marginBottom: 'var(--space-4)',
        flexWrap: 'wrap',
      }}>
        <label style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-secondary)' }}>
          Change request
        </label>
        <select
          value={selectedChangeId || ''}
          onChange={e => setSelectedChangeId(e.target.value)}
          style={{
            padding: '7px 12px',
            background: 'var(--bg-card)',
            border: '1px solid var(--border)',
            borderRadius: '6px',
            color: 'var(--text-primary)',
            fontSize: '13px',
            minWidth: '320px',
            cursor: 'pointer',
          }}
        >
          {changes.map(c => (
            <option key={c.id} value={c.id}>{c.title} · {c.id.slice(0, 8)}</option>
          ))}
        </select>
        {ch && (
          <Link
            to={`/certification/changes/${ch.id}`}
            style={{ fontSize: '12px', color: 'var(--accent)', textDecoration: 'none', fontWeight: 600 }}
          >
            View change detail →
          </Link>
        )}
        <button
          onClick={() => refetch()}
          disabled={isFetching}
          style={{
            marginLeft: 'auto',
            display: 'inline-flex', alignItems: 'center', gap: '6px',
            padding: '7px 12px',
            background: 'var(--bg-card)', border: '1px solid var(--border)',
            borderRadius: '6px', color: 'var(--text-secondary)',
            fontSize: '12px', fontWeight: 500,
            cursor: isFetching ? 'wait' : 'pointer',
          }}
        >
          <RefreshCw size={12} className={isFetching ? 'spin' : ''} /> Refresh
        </button>
      </div>

      {/* KPI strip — milestones for the selected change */}
      <KpiStrip
        loading={isLoading}
        tiles={[
          { label: 'Partners involved', value: kpis.partners, color: 'var(--accent)' },
          { label: 'Milestones',        value: kpis.milestones, sub: 'lifecycle events recorded', color: '#58a6ff' },
          { label: 'Cert runs passed',  value: kpis.certPasses, color: '#3fb950' },
          { label: 'Cert runs failed',  value: kpis.certFails,  color: kpis.certFails > 0 ? 'var(--danger)' : 'var(--text-muted)' },
          { label: 'Live in production', value: kpis.live,      color: '#2ea676' },
        ]}
      />

      {/* Vertical timeline */}
      <Section title={ch?.title || 'Timeline'} subtitle={ch ? `Status: ${ch.status} · ${ch.id}` : ''}>
        {isLoading && (
          <div style={{ padding: 'var(--space-5)', textAlign: 'center', color: 'var(--text-muted)' }}>Loading…</div>
        )}
        {!isLoading && events.length === 0 && (
          <div style={{ padding: 'var(--space-5)', textAlign: 'center', color: 'var(--text-muted)', fontSize: 13 }}>
            No lifecycle events for this change yet.
          </div>
        )}
        {events.length > 0 && <Timeline events={events} />}
      </Section>
    </>
  )
}


// ── Vertical timeline component ──────────────────────────────────────────
function Timeline({ events }) {
  // Group consecutive events by day for visual breathing room
  const grouped = useMemo(() => {
    const out = []
    let lastDay = null
    for (const e of events) {
      const day = new Date(e.timestamp).toDateString()
      if (day !== lastDay) {
        out.push({ kind: 'day', day, ts: e.timestamp })
        lastDay = day
      }
      out.push({ kind: 'event', e })
    }
    return out
  }, [events])

  return (
    <div style={{ position: 'relative', paddingLeft: 'var(--space-6)' }}>
      {/* Vertical rail */}
      <div style={{
        position: 'absolute',
        left: '14px', top: 0, bottom: 0,
        width: '2px',
        background: 'var(--border-subtle)',
      }} />

      {grouped.map((row, idx) => {
        if (row.kind === 'day') {
          return (
            <div key={`day-${row.day}-${idx}`} style={{
              margin: 'var(--space-4) 0 var(--space-3) -32px',
              fontSize: '11px', color: 'var(--text-muted)',
              textTransform: 'uppercase', letterSpacing: '0.06em', fontWeight: 600,
              paddingLeft: 'var(--space-6)',
            }}>
              {new Date(row.ts).toLocaleDateString('en-IN', { weekday: 'short', day: '2-digit', month: 'short', year: 'numeric' })}
            </div>
          )
        }
        const e = row.e
        const meta = KIND_META[e.kind] || { label: e.kind, icon: Activity, color: SEVERITY_COLOR[e.severity] }
        const Icon = meta.icon
        return (
          <div key={`evt-${idx}-${e.timestamp}`} style={{
            position: 'relative',
            display: 'flex', alignItems: 'flex-start', gap: 'var(--space-3)',
            paddingBottom: 'var(--space-4)',
          }}>
            {/* Dot */}
            <div style={{
              position: 'absolute', left: '-32px',
              width: '30px', height: '30px',
              borderRadius: '50%',
              background: 'var(--bg-card)',
              border: `2px solid ${meta.color}`,
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              flexShrink: 0,
              zIndex: 1,
            }}>
              <Icon size={13} style={{ color: meta.color }} />
            </div>
            {/* Content */}
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', flexWrap: 'wrap' }}>
                <span style={{ fontSize: '13px', fontWeight: 600, color: 'var(--text-primary)' }}>
                  {e.title}
                </span>
                {e.partner_name && (
                  <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>
                    · {e.partner_name}
                  </span>
                )}
                <span style={{ marginLeft: 'auto', fontSize: '11px', color: 'var(--text-muted)' }}>
                  {new Date(e.timestamp).toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' })}
                  {' · '}{relativeTime(e.timestamp)}
                </span>
              </div>
              {e.description && (
                <p style={{ margin: '4px 0 0', fontSize: '12px', color: 'var(--text-secondary)', lineHeight: 1.5 }}>
                  {e.description}
                </p>
              )}
              <div style={{ display: 'flex', gap: 'var(--space-2)', marginTop: '6px', flexWrap: 'wrap' }}>
                <ActorBadge actor={e.actor} />
                <KindBadge kind={e.kind} />
                {e.details?.run_number && (
                  <span style={kBadge('var(--text-muted)')}>
                    Run #{e.details.run_number}
                  </span>
                )}
              </div>
            </div>
          </div>
        )
      })}
    </div>
  )
}


// ════════════════════════════════════════════════════════════════════════════
// Tab 2 — Activity Log (flat, filterable)
// ════════════════════════════════════════════════════════════════════════════

function ActivityTab({ family, setFamily, search, setSearch }) {
  const wantedKinds = KIND_FAMILIES[family]
  const { data, isLoading, refetch, isFetching } = useQuery({
    queryKey: ['cert-timeline-flat', family],
    queryFn: async () => (await certificationApi.timeline({
      limit: 500,
      kinds: wantedKinds ? wantedKinds.join(',') : undefined,
    })).data,
    refetchInterval: 15000,
  })
  const events = data?.events || []

  const filtered = useMemo(() => {
    const q = search.toLowerCase()
    if (!q) return events
    return events.filter(e =>
      (e.title || '').toLowerCase().includes(q) ||
      (e.description || '').toLowerCase().includes(q) ||
      (e.partner_name || '').toLowerCase().includes(q) ||
      (e.change_title || '').toLowerCase().includes(q) ||
      (e.actor?.name || '').toLowerCase().includes(q)
    )
  }, [events, search])

  return (
    <>
      {/* Toolbar */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 'var(--space-3)',
        marginBottom: 'var(--space-3)', flexWrap: 'wrap',
      }}>
        <div style={{ position: 'relative', flex: '1 1 280px', maxWidth: '420px' }}>
          <Search size={14} style={{
            position: 'absolute', left: '10px', top: '50%',
            transform: 'translateY(-50%)', color: 'var(--text-muted)',
          }} />
          <input
            value={search}
            onChange={e => setSearch(e.target.value)}
            placeholder="Search by title, partner, change…"
            style={{
              width: '100%', padding: '8px 12px 8px 32px',
              background: 'var(--bg-input)', border: '1px solid var(--border)',
              borderRadius: '6px', color: 'var(--text-primary)', fontSize: '13px', outline: 'none',
            }}
          />
        </div>
        <button
          onClick={() => refetch()}
          disabled={isFetching}
          style={{
            display: 'inline-flex', alignItems: 'center', gap: '6px',
            padding: '7px 12px',
            background: 'var(--bg-card)', border: '1px solid var(--border)',
            borderRadius: '6px', color: 'var(--text-secondary)',
            fontSize: '12px', fontWeight: 500,
            cursor: isFetching ? 'wait' : 'pointer',
          }}
        >
          <RefreshCw size={12} className={isFetching ? 'spin' : ''} /> Refresh
        </button>
      </div>

      {/* Family filter chips */}
      <div style={{ display: 'flex', gap: 'var(--space-1)', marginBottom: 'var(--space-4)', flexWrap: 'wrap' }}>
        {[
          { v: 'all',           l: 'All activity' },
          { v: 'comms',         l: 'Communications' },
          { v: 'status',        l: 'Status changes' },
          { v: 'admin_actions', l: 'Admin actions' },
          { v: 'defects',       l: 'Defects & triage' },
          { v: 'artifacts',     l: 'Artifacts' },
        ].map(f => {
          const active = family === f.v
          return (
            <button
              key={f.v}
              onClick={() => setFamily(f.v)}
              style={{
                padding: '7px 12px',
                borderRadius: '999px',
                fontSize: '12px',
                fontWeight: 500,
                border: `1px solid ${active ? 'var(--accent)' : 'var(--border)'}`,
                background: active ? 'var(--accent)' : 'var(--bg-card)',
                color: active ? 'white' : 'var(--text-secondary)',
                cursor: 'pointer',
              }}
            >
              {f.l}
            </button>
          )
        })}
      </div>

      {isLoading && (
        <Section>
          <div style={{ padding: 'var(--space-5)', textAlign: 'center', color: 'var(--text-muted)' }}>Loading…</div>
        </Section>
      )}

      {!isLoading && filtered.length === 0 && (
        <Section>
          <p style={{ margin: 0, padding: 'var(--space-5)', textAlign: 'center', color: 'var(--text-muted)', fontSize: 13 }}>
            No activity matches this filter.
          </p>
        </Section>
      )}

      {filtered.length > 0 && (
        <Section padded={false}>
          {filtered.map((e, idx) => {
            const meta = KIND_META[e.kind] || { label: e.kind, icon: Activity, color: SEVERITY_COLOR[e.severity] }
            const Icon = meta.icon
            return (
              <div key={`evt-${e.timestamp}-${idx}`} style={{
                display: 'grid',
                gridTemplateColumns: '36px 1fr 200px 130px',
                gap: 'var(--space-3)',
                padding: 'var(--space-3) var(--space-5)',
                borderBottom: idx < filtered.length - 1 ? '1px solid var(--border-subtle)' : 'none',
                alignItems: 'center',
              }}>
                <div style={{
                  width: 28, height: 28, borderRadius: '50%',
                  background: `${meta.color}1A`, border: `1px solid ${meta.color}40`,
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  flexShrink: 0,
                }}>
                  <Icon size={12} style={{ color: meta.color }} />
                </div>
                <div style={{ minWidth: 0 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', flexWrap: 'wrap' }}>
                    <span style={{ fontSize: '13px', fontWeight: 600, color: 'var(--text-primary)' }}>{e.title}</span>
                    {e.partner_name && <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>· {e.partner_name}</span>}
                    <ActorBadge actor={e.actor} />
                  </div>
                  {e.description && (
                    <p style={{ margin: '3px 0 0', fontSize: '11px', color: 'var(--text-muted)', lineHeight: 1.4,
                      overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {e.description}
                    </p>
                  )}
                </div>
                <div style={{ minWidth: 0 }}>
                  {e.change_title && (
                    <Link
                      to={`/certification/changes/${e.change_id}`}
                      style={{ fontSize: '11px', color: 'var(--accent)', textDecoration: 'none' }}
                    >
                      <span className="id-mono" style={{ marginRight: 4 }}>{e.change_id?.slice(0, 8)}</span>
                      {e.change_title}
                    </Link>
                  )}
                </div>
                <div style={{ fontSize: '11px', color: 'var(--text-muted)', textAlign: 'right' }}>
                  {new Date(e.timestamp).toLocaleString('en-IN', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' })}
                  <div>{relativeTime(e.timestamp)}</div>
                </div>
              </div>
            )
          })}
        </Section>
      )}
    </>
  )
}


// ════════════════════════════════════════════════════════════════════════════
// Tab 3 — Engine Traffic (existing A2A feed, kept as-is)
// ════════════════════════════════════════════════════════════════════════════

function EngineTab({ filter, setFilter }) {
  const { data, isLoading, refetch, isFetching, dataUpdatedAt } = useQuery({
    queryKey: ['cert-agent-messages'],
    queryFn: async () => (await certificationApi.agentMessages(200)).data,
    refetchInterval: 8000,
  })
  const messages = data?.messages || []
  const engineCount = data?.engine_count || 0
  const filtered = useMemo(() => {
    return messages.filter(m => {
      if (filter === 'all') return true
      if (filter === 'outbound') return m.direction === 'outbound'
      if (filter === 'inbound')  return m.direction === 'inbound'
      if (filter === 'failed')   return m.status === 'failed' || m.status === 'delivery_failed'
      return true
    })
  }, [messages, filter])

  // Group by cert_run_id
  const groups = useMemo(() => {
    const out = []
    const byRunId = new Map()
    for (const m of filtered) {
      const rid = m.cert_run_id || `_unmatched_${m.id}`
      if (!byRunId.has(rid)) {
        byRunId.set(rid, [])
        out.push(rid)
      }
      byRunId.get(rid).push(m)
    }
    return out.map(rid => ({ run_id: rid, messages: byRunId.get(rid) }))
  }, [filtered])

  if (engineCount === 0 && !isLoading) {
    return (
      <Section>
        <div style={{ padding: 'var(--space-4)', display: 'flex', alignItems: 'center', gap: 'var(--space-3)' }}>
          <Cpu size={28} style={{ color: 'var(--text-muted)' }} />
          <div>
            <p style={{ margin: 0, fontSize: '14px', fontWeight: 600 }}>No certification engine registered</p>
            <p style={{ margin: '4px 0 0', fontSize: '12px', color: 'var(--text-muted)' }}>
              Go to <strong>Admin → Partners</strong> and register a partner with type <code>cert_engine</code>.
            </p>
          </div>
        </div>
      </Section>
    )
  }

  return (
    <>
      <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', marginBottom: 'var(--space-3)', flexWrap: 'wrap' }}>
        <span style={{ fontSize: '12px', color: 'var(--text-muted)' }}>
          {engineCount} engine{engineCount !== 1 ? 's' : ''} registered ·{' '}
          {dataUpdatedAt && <>last update {relativeTime(new Date(dataUpdatedAt).toISOString())}</>}
        </span>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 'var(--space-1)' }}>
          {[
            { v: 'all',      l: 'All' },
            { v: 'outbound', l: 'Outbound' },
            { v: 'inbound',  l: 'Inbound' },
            { v: 'failed',   l: 'Failed only' },
          ].map(f => {
            const active = filter === f.v
            return (
              <button
                key={f.v}
                onClick={() => setFilter(f.v)}
                style={{
                  padding: '6px 11px',
                  borderRadius: '999px',
                  fontSize: '11px',
                  fontWeight: 500,
                  border: `1px solid ${active ? 'var(--accent)' : 'var(--border)'}`,
                  background: active ? 'var(--accent)' : 'var(--bg-card)',
                  color: active ? 'white' : 'var(--text-secondary)',
                  cursor: 'pointer',
                }}
              >{f.l}</button>
            )
          })}
        </div>
        <button
          onClick={() => refetch()}
          disabled={isFetching}
          style={{
            display: 'inline-flex', alignItems: 'center', gap: '6px',
            padding: '6px 11px', background: 'var(--bg-card)', border: '1px solid var(--border)',
            borderRadius: '6px', color: 'var(--text-secondary)', fontSize: '12px',
            cursor: isFetching ? 'wait' : 'pointer',
          }}
        >
          <RefreshCw size={12} className={isFetching ? 'spin' : ''} />
        </button>
      </div>

      {!isLoading && groups.length === 0 && (
        <Section>
          <p style={{ margin: 0, padding: 'var(--space-5)', textAlign: 'center', color: 'var(--text-muted)', fontSize: 13 }}>
            {messages.length === 0 ? 'No A2A traffic yet.' : 'No messages match this filter.'}
          </p>
        </Section>
      )}

      <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-3)' }}>
        {groups.map(group => {
          const isUnmatched = group.run_id.startsWith('_unmatched_')
          const summary = group.messages.find(m => m.summary)?.summary
          const subtitle = summary
            ? `${summary.passed || 0}/${summary.total || 0} passed${(summary.failed || 0) > 0 ? ` · ${summary.failed} failed` : ''}`
            : `${group.messages.length} message${group.messages.length !== 1 ? 's' : ''}`
          return (
            <Section
              key={group.run_id}
              title={isUnmatched
                ? 'Unmatched message'
                : <><span className="id-mono" style={{ color: 'var(--accent)' }}>cert_run_id</span> <span className="id-mono">{group.run_id.slice(0, 12)}…</span></>}
              subtitle={subtitle}
              padded={false}
            >
              {group.messages.map((m, idx) => {
                const dirOut = m.direction === 'outbound'
                const DirIcon = dirOut ? ArrowUpRight : ArrowDownRight
                const dirColor = dirOut ? '#58a6ff' : '#3fb950'
                const TLabel = A2A_TASK_TYPE_LABEL[m.task_type] || { label: m.task_type, desc: '' }
                return (
                  <div key={m.id} style={{
                    display: 'grid',
                    gridTemplateColumns: '32px 180px 1fr 130px 120px',
                    padding: 'var(--space-3) var(--space-5)',
                    borderBottom: idx < group.messages.length - 1 ? '1px solid var(--border-subtle)' : 'none',
                    fontSize: 12, alignItems: 'center',
                  }}>
                    <DirIcon size={14} color={dirColor} />
                    <div>
                      <p style={{ margin: 0, color: 'var(--text-primary)', fontWeight: 600 }}>{TLabel.label}</p>
                      <p style={{ margin: 0, color: 'var(--text-muted)', fontSize: 11 }}>{m.direction}</p>
                    </div>
                    <p style={{ margin: 0, color: 'var(--text-secondary)' }}>{TLabel.desc}</p>
                    <StatusBadge kind="a2a_message" value={m.status} size="sm" />
                    <span style={{ color: 'var(--text-muted)', fontSize: 11 }}>{relativeTime(m.created_at)}</span>
                  </div>
                )
              })}
            </Section>
          )
        })}
      </div>
    </>
  )
}


// ── Small visual helpers ───────────────────────────────────────────────────

function ActorBadge({ actor }) {
  if (!actor) return null
  const color =
    actor.kind === 'user'    ? '#58a6ff' :
    actor.kind === 'partner' ? '#bc8cff' :
    'var(--text-muted)'
  return (
    <span style={kBadge(color)}>
      {actor.kind === 'user' && '@ '}
      {actor.kind === 'partner' && '◆ '}
      {actor.name}
    </span>
  )
}

function KindBadge({ kind }) {
  const meta = KIND_META[kind]
  if (!meta) return null
  return <span style={kBadge(meta.color)}>{meta.label}</span>
}

function kBadge(color) {
  return {
    display: 'inline-flex', alignItems: 'center',
    padding: '1px 7px',
    borderRadius: '999px',
    fontSize: 10,
    fontWeight: 600,
    color, background: `${color}1A`,
    border: `1px solid ${color}40`,
    whiteSpace: 'nowrap',
  }
}
