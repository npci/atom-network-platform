// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

/**
 * Partner Cert Matrix — enterprise-grade redesign.
 *
 * Layout (top → bottom):
 *   1. PageHeader with breadcrumb + refresh action
 *   2. KPI strip — 5 partner-level tiles
 *   3. "Needs attention" alert banner if any partner is blocked or has failing
 *      cert runs (clickable, pre-filters the list)
 *   4. Toolbar — search + type chips + status chips + sort select + hide-withdrawn
 *   5. Sortable matrix table (DataTable with expand-row): one row per partner,
 *      columns for live / certified / failing / blocked / activity. Expand
 *      reveals a clean per-CR sub-table with status pill, latest run, and a
 *      jump-to-detail link.
 *
 * Drives off the same three endpoints as before — no new backend.
 */
import { useState, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import {
  Building2, Globe, Smartphone, Cpu, Search, ChevronRight, ChevronDown,
  Users, RefreshCw, AlertTriangle, ExternalLink, Rocket, Award, Hammer, Ban,
  Clock, MessagesSquare,
} from 'lucide-react'

import { partnersApi, certificationApi } from '../../services/api'
import PageHeader  from '../../components/cert/PageHeader'
import KpiStrip    from '../../components/cert/KpiStrip'
import DataTable   from '../../components/cert/DataTable'
import StatusBadge from '../../components/cert/StatusBadge'
import { relativeTime, ASSIGNMENT_FLAGS, assignmentChips } from '../../lib/certStatus'

const TYPE_META = {
  bank:        { label: 'Bank',        icon: Building2,  color: '#58a6ff' },
  psp:         { label: 'PSP',         icon: Smartphone, color: '#3fb950' },
  tpap:        { label: 'TPAP',        icon: Globe,      color: '#d29922' },
  cert_engine: { label: 'Cert Engine', icon: Cpu,        color: '#e8b347' },
}

const STATUS_FILTERS = [
  { v: 'all',        l: 'All' },
  { v: 'live',       l: 'In production' },
  { v: 'certified',  l: 'Certified' },
  { v: 'cert',       l: 'In cert' },
  { v: 'building',   l: 'Building' },
  { v: 'attention',  l: 'Needs attention' },
]

const SORT_OPTIONS = [
  { v: 'name',     l: 'Name (A→Z)' },
  { v: 'crs',      l: 'Most CRs' },
  { v: 'live',     l: 'Most live' },
  { v: 'failing',  l: 'Most failing' },
  { v: 'blocked',  l: 'Most blocked' },
  { v: 'activity', l: 'Latest activity' },
]

// Domain extraction for endpoint URL display
function domainOf(url) {
  if (!url) return ''
  try { return new URL(url).host } catch { return url.replace(/^https?:\/\//, '').split('/')[0] }
}

export default function CertPartnerEntries() {
  const navigate = useNavigate()
  const [search, setSearch] = useState('')
  const [typeFilter, setTypeFilter] = useState('all')
  const [statusFilter, setStatusFilter] = useState('all')
  const [sortBy, setSortBy] = useState('crs')
  const [hideWithdrawn, setHideWithdrawn] = useState(true)
  const [expandedPid, setExpandedPid] = useState(null)

  // ── Data ─────────────────────────────────────────────────────────────────
  const { data: partners, isLoading: partnersLoading, refetch: refetchPartners, isFetching: partnersFetching } = useQuery({
    queryKey: ['admin-partners'],
    queryFn: async () => (await partnersApi.list()).data,
  })

  const { data: dashboard, isLoading: dashLoading, refetch: refetchDash } = useQuery({
    queryKey: ['cert-dashboard'],
    queryFn: async () => (await certificationApi.dashboard()).data,
    refetchInterval: 30000,
  })

  const crIds = useMemo(
    () => (dashboard?.changes || []).slice(0, 30).map(c => c.id),
    [dashboard]
  )

  const summaries = useQuery({
    queryKey: ['cert-summaries', crIds.join(',')],
    enabled: crIds.length > 0,
    queryFn: async () => {
      const out = []
      for (const id of crIds) {
        try { out.push((await certificationApi.changeSummary(id)).data) } catch { /* ignore */ }
      }
      return out
    },
  })

  // ── Build per-partner index from cert summaries ─────────────────────────
  const partnerRows = useMemo(() => {
    if (!partners || !summaries.data) return []
    const idx = new Map()
    for (const p of partners) {
      const types = p.partner_types || [p.partner_type || 'bank']
      if (types.includes('cert_engine')) continue
      idx.set(p.id, { partner: p, types, entries: [] })
    }
    for (const s of summaries.data) {
      for (const ap of (s.partners || [])) {
        const row = idx.get(ap.partner_id)
        if (!row) continue
        row.entries.push({
          change_id:    s.change_id,
          change_title: s.change_title,
          assignment_status: ap.assignment_status,
          run:          ap.latest_run,
          failed:       (ap.latest_run?.failed || 0) > 0,
          blocked:      ap.blocked || false,
          blocked_reason: ap.blocked_reason,
          open_threads: ap.open_threads || 0,
          current_state_since: ap.current_state_since,
        })
      }
    }
    // Compute per-partner aggregates so we can sort/filter on them.
    return Array.from(idx.values()).map(r => {
      const e = r.entries
      const live    = e.filter(x => x.assignment_status === 'in_production').length
      const certified = e.filter(x => ['certified', 'ready_for_production'].includes(x.assignment_status)).length
      const incert  = e.filter(x => ['certifying', 'ready_for_certification', 'ready'].includes(x.assignment_status)).length
      const building = e.filter(x => ['applied', 'tested', 'in_progress', 'received', 'accepted', 'communicated', 'acknowledged'].includes(x.assignment_status)).length
      const failing = e.filter(x => x.failed).length
      const blocked = e.filter(x => x.blocked).length
      const withdrawn = e.filter(x => x.assignment_status === 'withdrawn').length
      const negotiating = e.filter(x => (x.open_threads || 0) > 0).length
      const latestActivity = e
        .map(x => x.run?.completed_at)
        .filter(Boolean)
        .sort((a, b) => new Date(b) - new Date(a))[0]
      const needsAttention = blocked > 0 || failing > 0
      return { ...r, agg: { live, certified, incert, building, failing, blocked, withdrawn, negotiating, latestActivity, needsAttention, total: e.length } }
    })
  }, [partners, summaries.data])

  // ── Filter + sort ───────────────────────────────────────────────────────
  const filtered = useMemo(() => {
    const q = search.toLowerCase()
    return partnerRows
      .filter(r => {
        if (q && !r.partner.name.toLowerCase().includes(q) && !(r.partner.endpoint_url || '').toLowerCase().includes(q)) return false
        if (typeFilter !== 'all' && !r.types.includes(typeFilter)) return false
        if (hideWithdrawn && r.agg.total > 0 && r.agg.withdrawn === r.agg.total) return false
        if (statusFilter === 'live'      && r.agg.live === 0) return false
        if (statusFilter === 'certified' && r.agg.certified === 0) return false
        if (statusFilter === 'cert'      && r.agg.incert === 0) return false
        if (statusFilter === 'building'  && r.agg.building === 0) return false
        if (statusFilter === 'attention' && !r.agg.needsAttention) return false
        return true
      })
      .sort((a, b) => {
        switch (sortBy) {
          case 'name':     return a.partner.name.localeCompare(b.partner.name)
          case 'crs':      return b.agg.total - a.agg.total
          case 'live':     return b.agg.live - a.agg.live
          case 'failing':  return b.agg.failing - a.agg.failing
          case 'blocked':  return b.agg.blocked - a.agg.blocked
          case 'activity': return new Date(b.agg.latestActivity || 0) - new Date(a.agg.latestActivity || 0)
          default:         return 0
        }
      })
  }, [partnerRows, search, typeFilter, statusFilter, hideWithdrawn, sortBy])

  // ── Aggregate KPIs across all partners ──────────────────────────────────
  const kpis = useMemo(() => {
    const total       = partnerRows.length
    const withCRs     = partnerRows.filter(r => r.agg.total > 0).length
    const totalLive   = partnerRows.reduce((s, r) => s + r.agg.live, 0)
    const totalBlocked = partnerRows.reduce((s, r) => s + r.agg.blocked, 0)
    const totalFailing = partnerRows.reduce((s, r) => s + r.agg.failing, 0)
    const totalAssignments = partnerRows.reduce((s, r) => s + r.agg.total, 0)
    const partnersNeedingAttention = partnerRows.filter(r => r.agg.needsAttention).length
    return { total, withCRs, totalLive, totalBlocked, totalFailing, totalAssignments, partnersNeedingAttention }
  }, [partnerRows])

  const isLoading = partnersLoading || dashLoading || summaries.isLoading
  const isFetching = partnersFetching || summaries.isFetching

  return (
    <div style={{ padding: 'var(--space-7) var(--space-7)', maxWidth: '1280px' }}>

      <PageHeader
        icon={Users}
        crumbs={[{ label: 'Certification' }, { label: 'By Partner' }]}
        title="Partner Cert Matrix"
        subtitle="Each partner's certification status across every change request they're assigned to. Click a partner to drill into their per-change progress."
        actions={
          <button
            onClick={() => { refetchPartners(); refetchDash(); summaries.refetch() }}
            disabled={isFetching}
            style={btnSecondary(isFetching)}
          >
            <RefreshCw size={12} className={isFetching ? 'spin' : ''} /> Refresh
          </button>
        }
      />

      {/* KPI strip */}
      <KpiStrip
        loading={isLoading}
        tiles={[
          {
            label: 'Total partners',
            value: kpis.total,
            sub:   `${kpis.withCRs} with active CRs`,
            color: 'var(--accent)',
          },
          {
            label: 'Active assignments',
            value: kpis.totalAssignments,
            sub:   kpis.totalAssignments && kpis.total ? `${(kpis.totalAssignments / kpis.total).toFixed(1)} avg per partner` : '—',
            color: '#58a6ff',
          },
          {
            label: 'In production',
            value: kpis.totalLive,
            sub:   kpis.totalLive > 0 ? 'partner × change combos live' : 'none live yet',
            color: '#2ea676',
          },
          {
            label: 'Blocked',
            value: kpis.totalBlocked,
            sub:   kpis.totalBlocked > 0 ? 'admin attention required' : 'all clear',
            color: kpis.totalBlocked > 0 ? '#ff7f9b' : 'var(--text-muted)',
          },
          {
            label: 'Failing',
            value: kpis.totalFailing,
            sub:   kpis.totalFailing > 0 ? 'last cert run had failures' : 'all green',
            color: kpis.totalFailing > 0 ? 'var(--danger)' : 'var(--text-muted)',
          },
        ]}
      />

      {/* Needs attention banner */}
      {kpis.partnersNeedingAttention > 0 && statusFilter !== 'attention' && (
        <button
          onClick={() => setStatusFilter('attention')}
          style={{
            display: 'flex', alignItems: 'center', gap: 'var(--space-3)', width: '100%',
            padding: 'var(--space-3) var(--space-5)', marginBottom: 'var(--space-4)',
            background: 'rgba(224,108,108,0.06)', border: '1px solid rgba(224,108,108,0.30)',
            borderRadius: '10px', color: 'var(--text-primary)', fontSize: '13px',
            cursor: 'pointer', textAlign: 'left',
          }}
        >
          <AlertTriangle size={16} style={{ color: 'var(--danger)', flexShrink: 0 }} />
          <div style={{ flex: 1 }}>
            <strong>{kpis.partnersNeedingAttention}</strong> partner{kpis.partnersNeedingAttention === 1 ? '' : 's'} need attention
            <span style={{ color: 'var(--text-muted)', marginLeft: 'var(--space-2)' }}>
              · {kpis.totalBlocked} blocked · {kpis.totalFailing} with failing runs
            </span>
          </div>
          <span style={{ color: 'var(--accent)', fontSize: '12px', fontWeight: 600 }}>View →</span>
        </button>
      )}

      {/* Toolbar */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-3)', marginBottom: 'var(--space-3)', flexWrap: 'wrap' }}>
        <div style={{ position: 'relative', flex: '1 1 240px', maxWidth: '420px' }}>
          <Search size={14} style={{
            position: 'absolute', left: '10px', top: '50%',
            transform: 'translateY(-50%)', color: 'var(--text-muted)',
          }} />
          <input
            value={search}
            onChange={e => setSearch(e.target.value)}
            placeholder="Search partner name or endpoint…"
            style={{
              width: '100%', padding: '8px 12px 8px 32px',
              background: 'var(--bg-input)', border: '1px solid var(--border)',
              borderRadius: '6px', color: 'var(--text-primary)', fontSize: '13px', outline: 'none',
            }}
          />
        </div>
        <div style={{ display: 'flex', gap: 'var(--space-1)' }}>
          {[{ v: 'all', l: 'All' }, { v: 'bank', l: 'Banks' }, { v: 'psp', l: 'PSPs' }, { v: 'tpap', l: 'TPAPs' }].map(t => (
            <button
              key={t.v}
              onClick={() => setTypeFilter(t.v)}
              style={chip(typeFilter === t.v)}
            >{t.l}</button>
          ))}
        </div>
        <select
          value={sortBy}
          onChange={e => setSortBy(e.target.value)}
          style={{
            padding: '7px 10px', background: 'var(--bg-card)', border: '1px solid var(--border)',
            borderRadius: '6px', color: 'var(--text-secondary)', fontSize: '12px', cursor: 'pointer',
          }}
        >
          {SORT_OPTIONS.map(s => <option key={s.v} value={s.v}>Sort: {s.l}</option>)}
        </select>
        <label style={{
          display: 'inline-flex', alignItems: 'center', gap: '6px',
          fontSize: '12px', color: 'var(--text-muted)', cursor: 'pointer',
        }}>
          <input
            type="checkbox"
            checked={hideWithdrawn}
            onChange={e => setHideWithdrawn(e.target.checked)}
            style={{ accentColor: 'var(--accent)' }}
          />
          Hide fully withdrawn
        </label>
      </div>

      {/* Status filter chips */}
      <div style={{ display: 'flex', gap: 'var(--space-1)', marginBottom: 'var(--space-4)', flexWrap: 'wrap' }}>
        {STATUS_FILTERS.map(s => (
          <button
            key={s.v}
            onClick={() => setStatusFilter(s.v)}
            style={chip(statusFilter === s.v, s.v === 'attention' && kpis.partnersNeedingAttention > 0)}
          >
            {s.v === 'attention' && <AlertTriangle size={11} style={{ marginRight: 4 }} />}
            {s.l}
          </button>
        ))}
      </div>

      {/* Matrix table */}
      <DataTable
        loading={isLoading}
        rows={filtered}
        rowKey={r => r.partner.id}
        emptyTitle={partnerRows.length === 0 ? 'No partners with assignments yet' : 'No partners match this filter'}
        emptyDescription={partnerRows.length === 0
          ? 'Register partners in Admin → Partners and assign them on a change in Phase C.'
          : 'Adjust the filters above or clear them to see all partners.'}
        columns={[
          {
            key: 'partner',
            label: 'Partner',
            width: 280,
            render: r => <PartnerCell row={r} expanded={expandedPid === r.partner.id} />,
          },
          { key: 'crs',  label: 'CRs',       width: 60,  align: 'right', render: r => <Metric value={r.agg.total} dim={r.agg.total === 0} /> },
          { key: 'live', label: 'Live',      width: 70,  align: 'right', render: r => <Metric value={r.agg.live} icon={Rocket} color="#2ea676" dim={r.agg.live === 0} /> },
          { key: 'cert', label: 'Certified', width: 90,  align: 'right', render: r => <Metric value={r.agg.certified} icon={Award} color="#3fb950" dim={r.agg.certified === 0} /> },
          { key: 'bld',  label: 'Building',  width: 80,  align: 'right', render: r => <Metric value={r.agg.building + r.agg.incert} icon={Hammer} color="#d29922" dim={(r.agg.building + r.agg.incert) === 0} /> },
          { key: 'fail', label: 'Failing',   width: 80,  align: 'right', render: r => <Metric value={r.agg.failing} icon={AlertTriangle} color="var(--danger)" dim={r.agg.failing === 0} /> },
          { key: 'blk',  label: 'Blocked',   width: 80,  align: 'right', render: r => <Metric value={r.agg.blocked} icon={Ban} color="#ff7f9b" dim={r.agg.blocked === 0} /> },
          {
            key: 'act',
            label: 'Activity',
            width: 110,
            render: r => (
              <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                {r.agg.latestActivity ? relativeTime(r.agg.latestActivity) : '—'}
              </span>
            ),
          },
          {
            key: 'chev',
            label: '',
            width: 28,
            align: 'right',
            render: r => expandedPid === r.partner.id
              ? <ChevronDown size={14} style={{ color: 'var(--text-muted)' }} />
              : <ChevronRight size={14} style={{ color: 'var(--text-muted)' }} />,
          },
        ]}
        expandable={{
          isExpanded:    r => expandedPid === r.partner.id,
          onToggle:      r => setExpandedPid(expandedPid === r.partner.id ? null : r.partner.id),
          renderExpanded: r => <ExpandedPartner row={r} navigate={navigate} />,
        }}
        showDensityToggle
      />
    </div>
  )
}

// ── Sub-components ─────────────────────────────────────────────────────────

function PartnerCell({ row }) {
  const primary = row.types[0] || 'bank'
  const meta = TYPE_META[primary] || TYPE_META.bank
  const Icon = meta.icon
  const dom = domainOf(row.partner.endpoint_url)
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-3)', minWidth: 0 }}>
      <div style={{
        width: 32, height: 32, borderRadius: 6, flexShrink: 0,
        background: `${meta.color}1A`, border: `1px solid ${meta.color}40`,
        display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}>
        <Icon size={14} color={meta.color} />
      </div>
      <div style={{ minWidth: 0, flex: 1 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', flexWrap: 'wrap' }}>
          <span style={{ fontSize: '13px', fontWeight: 600, color: 'var(--text-primary)' }}>
            {row.partner.name}
          </span>
          {row.types.map(t => (
            <span key={t} style={{
              fontSize: '9px', padding: '1px 6px', borderRadius: 999,
              background: `${(TYPE_META[t]?.color || meta.color)}1A`,
              color: TYPE_META[t]?.color || meta.color,
              border: `1px solid ${(TYPE_META[t]?.color || meta.color)}40`,
              textTransform: 'uppercase', fontWeight: 700, letterSpacing: '0.04em',
            }}>{t}</span>
          ))}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', marginTop: 2 }}>
          <span className="id-mono" style={{ color: 'var(--text-muted)' }}>
            {row.partner.id?.slice(0, 8)}
          </span>
          {dom && (
            <span style={{ fontSize: '11px', color: 'var(--text-muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 160 }}>
              · {dom}
            </span>
          )}
        </div>
      </div>
    </div>
  )
}

function Metric({ value, icon: Icon, color, dim }) {
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: '4px',
      fontSize: '13px', fontWeight: 600,
      color: dim ? 'var(--text-muted)' : (color || 'var(--text-primary)'),
      opacity: dim ? 0.5 : 1,
    }}>
      {Icon && !dim && <Icon size={11} />}
      {value}
    </span>
  )
}

function ExpandedPartner({ row, navigate }) {
  const e = row.entries
  if (e.length === 0) {
    return (
      <div style={{ padding: 'var(--space-5)', fontSize: '12px', color: 'var(--text-muted)' }}>
        This partner has no change-request assignments yet.
      </div>
    )
  }
  // Sort entries: needs-attention first, then by status order
  const sorted = [...e].sort((a, b) => {
    const aAtt = (a.blocked ? 2 : 0) + (a.failed ? 1 : 0)
    const bAtt = (b.blocked ? 2 : 0) + (b.failed ? 1 : 0)
    if (aAtt !== bAtt) return bAtt - aAtt
    return (a.change_title || '').localeCompare(b.change_title || '')
  })
  return (
    <div style={{ padding: 'var(--space-3) var(--space-5) var(--space-5)' }}>
      <div style={{
        display: 'grid',
        gridTemplateColumns: '110px 1fr 130px 200px 140px 28px',
        padding: 'var(--space-2) var(--space-3)',
        background: 'var(--bg-elevated)',
        borderRadius: '6px 6px 0 0',
        border: '1px solid var(--border-subtle)',
        fontSize: '10px',
        textTransform: 'uppercase',
        letterSpacing: '0.06em',
        fontWeight: 600,
        color: 'var(--text-muted)',
      }}>
        <span>CR</span>
        <span>Title</span>
        <span>Status</span>
        <span>Latest run</span>
        <span>Activity</span>
        <span />
      </div>
      <div style={{ border: '1px solid var(--border-subtle)', borderTop: 'none', borderRadius: '0 0 6px 6px', overflow: 'hidden' }}>
        {sorted.map((entry, idx) => {
          const flags = assignmentChips(entry)
          return (
            <div
              key={entry.change_id}
              onClick={(ev) => {
                ev.stopPropagation()
                navigate(`/certification/changes/${entry.change_id}`)
              }}
              style={{
                display: 'grid',
                gridTemplateColumns: '110px 1fr 130px 200px 140px 28px',
                padding: 'var(--space-3)',
                borderBottom: idx < sorted.length - 1 ? '1px solid var(--border-subtle)' : 'none',
                cursor: 'pointer', fontSize: '12px',
                alignItems: 'center',
                background: entry.blocked ? 'rgba(255,127,155,0.05)' : (entry.failed ? 'rgba(224,108,108,0.04)' : 'transparent'),
                transition: 'background 0.1s',
              }}
              onMouseEnter={ev => ev.currentTarget.style.background = 'var(--sidebar-hover)'}
              onMouseLeave={ev => ev.currentTarget.style.background =
                entry.blocked ? 'rgba(255,127,155,0.05)' : (entry.failed ? 'rgba(224,108,108,0.04)' : 'transparent')}
            >
              <span className="id-mono" style={{ color: 'var(--accent)' }}>
                {(entry.change_id || '').slice(0, 8)}
              </span>
              <span style={{
                color: 'var(--text-primary)',
                overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                paddingRight: 'var(--space-3)',
              }}>
                {entry.change_title}
              </span>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '3px', alignItems: 'flex-start' }}>
                <StatusBadge kind="assignment" value={entry.assignment_status} size="sm" />
                {flags.length > 0 && (
                  <div style={{ display: 'flex', gap: '2px', flexWrap: 'wrap' }}>
                    {flags.map(k => {
                      const m = ASSIGNMENT_FLAGS[k]
                      if (!m) return null
                      const I = m.icon
                      return (
                        <span key={k} style={{
                          display: 'inline-flex', alignItems: 'center', gap: '2px',
                          padding: '0 5px', borderRadius: 999,
                          fontSize: 9, fontWeight: 600,
                          color: m.color, background: `${m.color}1A`,
                          border: `1px solid ${m.color}40`,
                        }}>
                          <I size={8} />{m.label}
                        </span>
                      )
                    })}
                  </div>
                )}
              </div>
              <div style={{ fontSize: 11 }}>
                {entry.run ? (
                  <>
                    <span style={{ color: 'var(--text-secondary)' }}>Run #{entry.run.run_number}: </span>
                    <span style={{ color: '#3fb950', fontWeight: 600 }}>{entry.run.passed || 0}</span>
                    <span style={{ color: 'var(--text-muted)' }}>/{entry.run.total || 0}</span>
                    {(entry.run.failed || 0) > 0 && (
                      <span style={{ color: '#e06c6c', marginLeft: 4 }}>· {entry.run.failed} failed</span>
                    )}
                  </>
                ) : (
                  <span style={{ color: 'var(--text-muted)', fontStyle: 'italic' }}>No runs yet</span>
                )}
              </div>
              <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                {entry.run?.completed_at ? relativeTime(entry.run.completed_at) : '—'}
              </span>
              <ExternalLink size={12} style={{ color: 'var(--text-muted)' }} />
            </div>
          )
        })}
      </div>
      {/* Blocked banner with reason if any */}
      {row.entries.some(x => x.blocked) && (
        <div style={{
          marginTop: 'var(--space-3)', padding: 'var(--space-2) var(--space-3)',
          background: 'rgba(255,127,155,0.06)', border: '1px solid rgba(255,127,155,0.25)',
          borderRadius: 6, fontSize: 11, color: 'var(--text-secondary)',
        }}>
          <strong style={{ color: '#ff7f9b' }}>Blocked on:</strong>{' '}
          {row.entries.filter(x => x.blocked).map(x => x.change_title || x.change_id?.slice(0, 8)).join(', ')}
        </div>
      )}
    </div>
  )
}

// ── Style helpers ──────────────────────────────────────────────────────────

function chip(active, hot = false) {
  return {
    display: 'inline-flex', alignItems: 'center',
    padding: '6px 11px',
    borderRadius: '999px',
    fontSize: '11px',
    fontWeight: 600,
    border: `1px solid ${active ? 'var(--accent)' : (hot ? 'rgba(224,108,108,0.4)' : 'var(--border)')}`,
    background: active ? 'var(--accent)' : (hot ? 'rgba(224,108,108,0.08)' : 'var(--bg-card)'),
    color: active ? 'white' : (hot ? 'var(--danger)' : 'var(--text-secondary)'),
    cursor: 'pointer',
    transition: 'all 0.1s',
  }
}

function btnSecondary(disabled) {
  return {
    display: 'inline-flex', alignItems: 'center', gap: '6px',
    padding: '7px 12px',
    background: 'var(--bg-card)', border: '1px solid var(--border)',
    borderRadius: '6px', color: 'var(--text-secondary)',
    fontSize: '12px', fontWeight: 500,
    cursor: disabled ? 'wait' : 'pointer',
  }
}
