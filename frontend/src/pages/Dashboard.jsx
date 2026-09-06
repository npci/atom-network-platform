// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { changesApi } from '../services/api'
import { Plus, Search, ChevronRight, RefreshCw } from 'lucide-react'
import StatTile, { StatTileRow } from '../components/common/StatTile'
import { useAuth } from '../hooks/useAuth'

// Review-team roles get read-only visibility into all changes/kits but can't
// create change requests — the New Change action is disabled for them.
const READ_ONLY_ROLES = ['risk_reviewer', 'infosec_reviewer', 'tech_lead']

// Phase-state → {bg, color} palette. Matches the accent/success/muted tokens used
// elsewhere so theme changes carry through.
const PHASE_STATE_STYLES = {
  not_started: { bg: 'rgba(154,154,150,0.12)', color: '#9a9a96', border: 'rgba(154,154,150,0.25)' },
  in_progress: { bg: 'rgba(218,119,86,0.12)',  color: '#da7756', border: 'rgba(218,119,86,0.3)' },
  completed:   { bg: 'rgba(76,175,125,0.15)',  color: '#4caf7d', border: 'rgba(76,175,125,0.3)' },
  blocked:     { bg: 'rgba(224,108,108,0.12)', color: '#e06c6c', border: 'rgba(224,108,108,0.3)' },
}

function PhaseChip({ phase, letter, summary }) {
  const fallback = { state: 'not_started', label: 'Not Started' }
  const s = summary || fallback
  const style = PHASE_STATE_STYLES[s.state] || PHASE_STATE_STYLES.not_started
  const title = `Phase ${letter}${phase ? ' — ' + phase : ''}: ${s.label}${s.detail ? ' · ' + s.detail : ''}`
  return (
    <span
      title={title}
      style={{
        display: 'inline-flex', alignItems: 'center', gap: '6px',
        padding: '3px 9px',
        borderRadius: '12px',
        fontSize: '11px', fontWeight: 500,
        background: style.bg, color: style.color,
        border: `1px solid ${style.border}`,
        whiteSpace: 'nowrap', maxWidth: '200px',
        overflow: 'hidden', textOverflow: 'ellipsis',
      }}
    >
      <span style={{
        fontSize: '9px', fontWeight: 700, letterSpacing: '0.05em',
        opacity: 0.7,
      }}>{letter}</span>
      <span>{s.label}</span>
      {s.detail && (
        <span style={{ fontSize: '10px', opacity: 0.7 }}>· {s.detail}</span>
      )}
    </span>
  )
}

function PhaseChips({ change }) {
  return (
    <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap', justifyContent: 'flex-end' }}>
      <PhaseChip letter="A" phase="Idea to Design"        summary={change.phase_a} />
      <PhaseChip letter="B" phase="Design to Build"       summary={change.phase_b} />
      <PhaseChip letter="C" phase="Partner Collaboration" summary={change.phase_c} />
    </div>
  )
}

function formatDate(dateStr) {
  return new Date(dateStr).toLocaleDateString('en-IN', {
    day: '2-digit', month: 'short', year: 'numeric',
  })
}

export default function Dashboard() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { user } = useAuth()
  const canCreate = !READ_ONLY_ROLES.includes(user?.role)
  const [search, setSearch] = useState('')

  const { data, isLoading, isFetching } = useQuery({
    queryKey: ['changes'],
    queryFn: () => changesApi.list({ limit: 50 }).then((r) => r.data),
    // Lightweight poll so freshly-arrived rollouts surface without a
    // hard refresh. Endpoint is cheap (a single paginated SELECT).
    refetchInterval: 8000,
    refetchOnWindowFocus: true,
  })

  const items = data?.items?.filter((c) =>
    !search || c.title?.toLowerCase().includes(search.toLowerCase()) ||
    c.initial_prompt?.toLowerCase().includes(search.toLowerCase())
  ) ?? []

  // KPI tiles derived from the full inbox (not the search-filtered slice
  // — operators want at-a-glance totals, not "what's visible right now").
  // Each change carries phase_a / phase_b / phase_c summaries with a
  // `.state` field of {not_started | in_progress | completed | blocked}.
  const allChanges = data?.items ?? []
  const hasState = (c, state) =>
    c.phase_a?.state === state || c.phase_b?.state === state || c.phase_c?.state === state
  const stats = {
    total:           allChanges.length,
    active:          allChanges.filter((c) => hasState(c, 'in_progress')).length,
    awaitingPartner: allChanges.filter((c) =>
      c.phase_c?.state === 'not_started' || c.phase_c?.state === 'in_progress'
    ).length,
    blocked:         allChanges.filter((c) => hasState(c, 'blocked')).length,
  }

  return (
    <div style={{ padding: '24px clamp(16px, 3vw, 40px)', maxWidth: 1600, margin: '0 auto' }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: '28px' }}>
        <div>
          <h1 style={{ margin: '0 0 4px', fontSize: '20px', fontWeight: '600', color: 'var(--text-primary)' }}>
            Change Requests
          </h1>
          <p style={{ margin: 0, fontSize: '13px', color: 'var(--text-muted)' }}>
            {data?.total ?? 0} total change request{data?.total !== 1 ? 's' : ''}
          </p>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        {/* Force-pull the inbox now — kills the 8s wait when an
            operator knows the Authority just posted something. */}
        <button
          type="button"
          onClick={() => queryClient.invalidateQueries({ queryKey: ['changes'] })}
          disabled={isFetching}
          title="Refresh change list"
          style={{
            display: 'inline-flex', alignItems: 'center', gap: 6,
            padding: '9px 12px', fontSize: 12, fontWeight: 500,
            color: 'var(--text-secondary)', background: 'var(--bg-base)',
            border: '1px solid var(--border)', borderRadius: 6,
            cursor: isFetching ? 'wait' : 'pointer',
            opacity: isFetching ? 0.6 : 1,
          }}
        >
          <RefreshCw size={13} style={{ animation: isFetching ? 'spin 1s linear infinite' : 'none' }} />
          {isFetching ? 'Syncing…' : 'Sync'}
        </button>
        <button
          onClick={() => canCreate && navigate('/changes/new')}
          disabled={!canCreate}
          title={canCreate ? undefined : 'Review teams have read-only access — only PMs create changes'}
          style={{
            display: 'flex', alignItems: 'center', gap: '8px',
            padding: '9px 16px',
            background: 'var(--accent)',
            color: 'white',
            border: 'none', borderRadius: '6px',
            fontSize: '13px', fontWeight: '600',
            cursor: canCreate ? 'pointer' : 'not-allowed',
            opacity: canCreate ? 1 : 0.5,
            transition: 'background 0.15s',
          }}
          onMouseEnter={e => { if (canCreate) e.currentTarget.style.background = 'var(--accent-hover)' }}
          onMouseLeave={e => { if (canCreate) e.currentTarget.style.background = 'var(--accent)' }}
        >
          <Plus size={15} />
          New Change
        </button>
        </div>
      </div>

      {/* KPI tiles — operators want a single-glance overview before
          they start scrolling the list. */}
      <StatTileRow>
        <StatTile label="Total Changes"   value={stats.total}           accent="var(--text-secondary)" />
        <StatTile label="Active"          value={stats.active}          accent="#da7756"
                  hint={stats.active ? 'any phase in progress' : 'nothing in flight'} />
        <StatTile label="Awaiting Partner" value={stats.awaitingPartner} accent="#6ea8dc"
                  hint={stats.awaitingPartner ? 'Phase C pending' : 'all partners done'} />
        <StatTile label="Blocked"         value={stats.blocked}         accent="#e06c6c"
                  hint={stats.blocked ? 'needs attention' : null} />
      </StatTileRow>

      {/* Search */}
      <div style={{ position: 'relative', marginBottom: '20px' }}>
        <Search size={14} style={{
          position: 'absolute', left: '12px', top: '50%',
          transform: 'translateY(-50%)', color: 'var(--text-muted)',
        }} />
        <input
          type="text"
          placeholder="Search change requests…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          style={{
            width: '100%',
            padding: '9px 14px 9px 36px',
            background: 'var(--bg-elevated)',
            border: '1px solid var(--border)',
            borderRadius: '6px',
            color: 'var(--text-primary)',
            fontSize: '13px',
            outline: 'none',
          }}
          onFocus={e => e.target.style.borderColor = 'var(--accent)'}
          onBlur={e => e.target.style.borderColor = 'var(--border)'}
        />
      </div>

      {/* List */}
      {isLoading ? (
        <div style={{ textAlign: 'center', padding: '64px', color: 'var(--text-muted)', fontSize: '13px' }}>
          Loading…
        </div>
      ) : items.length === 0 ? (
        <div style={{ textAlign: 'center', padding: '64px' }}>
          <p style={{ color: 'var(--text-muted)', fontSize: '14px', marginBottom: '16px' }}>
            No change requests yet.
          </p>
          {canCreate && (
            <button
              onClick={() => navigate('/changes/new')}
              style={{
                padding: '9px 18px',
                background: 'var(--accent)',
                color: 'white', border: 'none', borderRadius: '6px',
                fontSize: '13px', fontWeight: '600', cursor: 'pointer',
              }}
            >
              Create your first change request
            </button>
          )}
        </div>
      ) : (
        <div style={{
          background: 'var(--bg-elevated)',
          border: '1px solid var(--border)',
          borderRadius: '8px',
          overflow: 'hidden',
        }}>
          {items.map((change, i) => (
            <button
              key={change.id}
              onClick={() => navigate(canCreate ? `/changes/${change.id}` : `/changes/${change.id}/product_kit`)}
              style={{
                width: '100%',
                display: 'flex', alignItems: 'center', gap: '16px',
                padding: '14px 20px',
                borderBottom: i < items.length - 1 ? '1px solid var(--border-subtle)' : 'none',
                background: 'transparent',
                border: 'none',
                cursor: 'pointer', textAlign: 'left',
                transition: 'background 0.15s',
              }}
              onMouseEnter={e => e.currentTarget.style.background = 'var(--bg-card)'}
              onMouseLeave={e => e.currentTarget.style.background = 'transparent'}
            >
              <div style={{ flex: 1, minWidth: 0 }}>
                <p style={{
                  margin: '0 0 3px',
                  fontSize: '14px', fontWeight: '500',
                  color: 'var(--text-primary)',
                  overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                }}>
                  {change.title || change.initial_prompt}
                </p>
                {change.title && (
                  <p style={{
                    margin: '0 0 4px', fontSize: '12px', color: 'var(--text-muted)',
                    overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                  }}>
                    {change.initial_prompt}
                  </p>
                )}
                <p style={{ margin: 0, fontSize: '11px', color: 'var(--text-muted)' }}>
                  {formatDate(change.created_at)}
                </p>
              </div>
              <PhaseChips change={change} />
              <ChevronRight size={14} style={{ color: 'var(--text-muted)', flexShrink: 0 }} />
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
