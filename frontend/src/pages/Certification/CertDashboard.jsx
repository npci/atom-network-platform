// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Award, ChevronRight, Search, RefreshCw, Calendar } from 'lucide-react'

import { certificationApi } from '../../services/api'
import PageHeader  from '../../components/cert/PageHeader'
import KpiStrip    from '../../components/cert/KpiStrip'
import DataTable   from '../../components/cert/DataTable'
import { relativeTime, DASHBOARD_ROW_STATUS, isStalledByActivity } from '../../lib/certStatus'

function StatusPill({ status }) {
  const meta = DASHBOARD_ROW_STATUS[status] || DASHBOARD_ROW_STATUS.kickoff
  const Icon = meta.icon
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: '4px',
      padding: '3px 10px', borderRadius: '999px',
      fontSize: '11px', fontWeight: 600,
      color: meta.color, background: `${meta.color}1A`,
      border: `1px solid ${meta.color}40`,
    }}>
      {Icon && <Icon size={11} />}
      {meta.label}
    </span>
  )
}

function PartnerBar({ live, certified, pending, failed, total }) {
  const liveSafe = live || 0
  // certified bucket on the bar should NOT double-count `live` partners
  // (they're counted as both certified+live by the backend; subtract here
  // to keep the bar segments adding up to total).
  const certifiedOnly = Math.max(0, (certified || 0) - liveSafe)
  const livePct = total ? (liveSafe / total) * 100 : 0
  const certPct = total ? (certifiedOnly / total) * 100 : 0
  const failPct = total ? ((failed || 0) / total) * 100 : 0
  const pendPct = total ? ((pending || 0) / total) * 100 : 0
  return (
    <div style={{ minWidth: 180 }}>
      <div style={{
        display: 'flex',
        gap: '2px',
        height: '6px',
        borderRadius: '4px',
        overflow: 'hidden',
        background: 'var(--bg-elevated)',
        marginBottom: '4px',
      }}>
        <div style={{ width: `${livePct}%`, background: '#2ea676' }} title="Live" />
        <div style={{ width: `${certPct}%`, background: '#7ed3e0' }} title="Certified" />
        <div style={{ width: `${failPct}%`, background: '#e06c6c' }} title="Failed" />
        <div style={{ width: `${pendPct}%`, background: 'var(--border)' }} title="Pending" />
      </div>
      <div style={{ display: 'flex', gap: 'var(--space-3)', fontSize: '11px', color: 'var(--text-muted)' }}>
        {liveSafe > 0 && <span style={{ color: '#2ea676' }}>🚀 {liveSafe} live</span>}
        {certifiedOnly > 0 && <span style={{ color: '#7ed3e0' }}>✓ {certifiedOnly} cert</span>}
        {failed > 0 && <span style={{ color: '#e06c6c' }}>✗ {failed} failed</span>}
        <span>{pending} pending</span>
      </div>
    </div>
  )
}

export default function CertDashboard() {
  const navigate = useNavigate()
  const [search, setSearch] = useState('')
  const [statusFilter, setStatusFilter] = useState('all')

  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ['cert-dashboard'],
    queryFn: async () => (await certificationApi.dashboard()).data,
    refetchInterval: 30000,
  })

  const changes = data?.changes || []
  const totals  = data?.totals  || { total_crs: 0, completed_crs: 0, in_progress_crs: 0, total_partners: 0, certified_total: 0 }

  // Derive enterprise-y secondary metrics from existing data
  const stalledCount = changes.filter(c => isStalledByActivity('in_progress', c.latest_run_at, 7)).length

  const filtered = changes.filter(cr => {
    const q = search.toLowerCase()
    const matchSearch = !q || (cr.title || '').toLowerCase().includes(q) || (cr.id || '').toLowerCase().includes(q)
    const matchStatus = statusFilter === 'all' || cr.status === statusFilter
    return matchSearch && matchStatus
  })

  return (
    <div style={{ padding: 'var(--space-7) var(--space-7)', maxWidth: '1200px' }}>

      <PageHeader
        icon={Award}
        crumbs={[{ label: 'Certification' }, { label: 'Overview' }]}
        title="Change Request Certification"
        subtitle="Cert progress for every released change request and its assigned partners."
        actions={
          <button
            onClick={() => refetch()}
            disabled={isFetching}
            style={{
              display: 'inline-flex', alignItems: 'center', gap: '6px',
              padding: '7px 12px',
              background: 'var(--bg-card)',
              border: '1px solid var(--border)',
              borderRadius: '6px',
              color: 'var(--text-secondary)',
              fontSize: '12px', fontWeight: 500,
              cursor: isFetching ? 'wait' : 'pointer',
            }}
          >
            <RefreshCw size={12} className={isFetching ? 'spin' : ''} /> Refresh
          </button>
        }
      />

      <KpiStrip
        loading={isLoading}
        tiles={[
          {
            label: 'Total CRs',
            value: totals.total_crs,
            sub:   `${changes.length} with assigned partners`,
            color: 'var(--accent)',
          },
          {
            label: 'In production',
            value: totals.live_crs ?? 0,
            sub:   `${totals.live_partners || 0} partner${(totals.live_partners || 0) === 1 ? '' : 's'} live`,
            color: '#2ea676',
          },
          {
            label: 'Awaiting go-live',
            value: totals.awaiting_go_live ?? 0,
            sub:   (totals.awaiting_go_live || 0) > 0 ? 'the Authority ops to mark live' : 'none pending',
            color: 'var(--success)',
          },
          {
            label: 'Active cert',
            value: totals.active_cert_crs ?? 0,
            sub:   stalledCount > 0 ? `${stalledCount} stalled >7d` : 'all on track',
            color: 'var(--warning)',
          },
          {
            label: 'Blocked / Withdrawn',
            value: (totals.blocked_crs || 0) + (totals.withdrawn_crs || 0),
            sub:   (totals.blocked_partners || 0) > 0
              ? `${totals.blocked_partners} partner${totals.blocked_partners === 1 ? '' : 's'} blocked`
              : ((totals.withdrawn_crs || 0) > 0 ? `${totals.withdrawn_crs} withdrawn` : 'none'),
            color: ((totals.blocked_crs || 0) + (totals.withdrawn_crs || 0)) > 0 ? '#ff7f9b' : 'var(--text-muted)',
          },
        ]}
      />

      {/* Filters bar — uses Section for consistent chrome */}
      <div style={{ marginBottom: 'var(--space-4)' }}>
        <div style={{
          display: 'flex',
          alignItems: 'center',
          gap: 'var(--space-3)',
          flexWrap: 'wrap',
        }}>
          <div style={{ position: 'relative', flex: '1 1 280px', maxWidth: '480px' }}>
            <Search size={14} style={{
              position: 'absolute', left: '10px', top: '50%',
              transform: 'translateY(-50%)', color: 'var(--text-muted)',
            }} />
            <input
              value={search}
              onChange={e => setSearch(e.target.value)}
              placeholder="Search by CR id or title…"
              style={{
                width: '100%',
                padding: '8px 12px 8px 32px',
                background: 'var(--bg-input)',
                border: '1px solid var(--border)',
                borderRadius: '6px',
                color: 'var(--text-primary)',
                fontSize: '13px',
                outline: 'none',
              }}
            />
          </div>
          <div style={{ display: 'flex', gap: 'var(--space-1)', flexWrap: 'wrap' }}>
            {[
              { v: 'all',              l: 'All' },
              { v: 'live',             l: 'Live' },
              { v: 'awaiting_go_live', l: 'Awaiting go-live' },
              { v: 'cert_done',        l: 'Certified' },
              { v: 'cert_in_flight',   l: 'Certifying' },
              { v: 'failed',           l: 'Failed' },
              { v: 'building',         l: 'Building' },
              { v: 'blocked',          l: 'Blocked' },
              { v: 'withdrawn',        l: 'Withdrawn' },
            ].map(s => {
              const active = statusFilter === s.v
              return (
                <button
                  key={s.v}
                  onClick={() => setStatusFilter(s.v)}
                  style={{
                    padding: '7px 12px',
                    borderRadius: '6px',
                    fontSize: '12px',
                    fontWeight: 500,
                    border: `1px solid ${active ? 'var(--accent)' : 'var(--border)'}`,
                    background: active ? 'var(--accent)' : 'var(--bg-card)',
                    color: active ? 'white' : 'var(--text-secondary)',
                    cursor: 'pointer',
                    transition: 'all 0.1s',
                  }}
                >
                  {s.l}
                </button>
              )
            })}
          </div>
        </div>
      </div>

      <DataTable
        loading={isLoading}
        error={isError ? error : null}
        onRetry={() => refetch()}
        rows={filtered}
        rowKey="id"
        onRowClick={cr => navigate(`/certification/changes/${cr.id}`)}
        emptyTitle={changes.length === 0 ? 'No change requests with assigned partners' : 'No change requests match your filter'}
        emptyDescription={changes.length === 0
          ? 'Assign partners on a change request in Phase C to see it here.'
          : 'Try clearing the search or status filter.'
        }
        columns={[
          {
            key: 'id',
            label: 'CR',
            width: 200,
            render: r => (
              <div>
                <span className="id-mono" style={{ color: 'var(--accent)', fontWeight: 700 }}>
                  {(r.id || '').slice(0, 8)}
                </span>
                <p style={{
                  margin: '2px 0 0', fontSize: '13px',
                  color: 'var(--text-primary)', fontWeight: 500,
                  overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                  maxWidth: '180px',
                }}>
                  {r.title}
                </p>
              </div>
            ),
          },
          {
            key: 'description',
            label: 'Description',
            render: r => (
              <span style={{
                fontSize: '12px',
                color: 'var(--text-muted)',
                display: '-webkit-box',
                WebkitLineClamp: 2,
                WebkitBoxOrient: 'vertical',
                overflow: 'hidden',
                lineHeight: 1.4,
              }}>
                {r.description || '—'}
              </span>
            ),
          },
          {
            key: 'released',
            label: 'Released',
            width: 130,
            render: r => (
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: '6px', fontSize: '12px', color: 'var(--text-secondary)' }}>
                <Calendar size={12} />
                {r.released_at
                  ? new Date(r.released_at).toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' })
                  : '—'}
              </span>
            ),
          },
          {
            key: 'status',
            label: 'Status',
            width: 120,
            render: r => <StatusPill status={r.status} />,
          },
          {
            key: 'partners',
            label: 'Partners',
            width: 220,
            render: r => <PartnerBar
              live={r.live}
              certified={r.certified}
              pending={r.pending}
              failed={r.failed}
              total={r.partners}
            />,
          },
          {
            key: 'activity',
            label: 'Activity',
            width: 110,
            render: r => (
              <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>
                {r.latest_run_at ? relativeTime(r.latest_run_at) : '—'}
              </span>
            ),
          },
          {
            key: 'chevron',
            label: '',
            width: 32,
            align: 'right',
            render: () => <ChevronRight size={14} style={{ color: 'var(--text-muted)' }} />,
          },
        ]}
        showDensityToggle
      />
    </div>
  )
}
