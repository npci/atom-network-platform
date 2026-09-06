// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { AlertCircle, BarChart3, Loader, RefreshCw, ShieldCheck, Timer, TrendingUp } from 'lucide-react'
import { evalApi } from '../../services/api'

const WINDOWS = [
  { id: '24h',  label: 'Last 24h',  hours: 24 },
  { id: '7d',   label: 'Last 7 days', hours: 24 * 7 },
  { id: '30d',  label: 'Last 30 days', hours: 24 * 30 },
  { id: 'all',  label: 'All time',  hours: null },
]

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

function pct(num, denom) {
  if (!denom) return 0
  return Math.round((num / denom) * 100)
}

function fmtMs(ms) {
  if (!ms) return '0 ms'
  if (ms < 1000) return `${Math.round(ms)} ms`
  return `${(ms / 1000).toFixed(1)} s`
}

function Card({ title, value, sub, color = 'var(--text-primary)', icon: Icon }) {
  return (
    <div style={{
      flex: 1,
      minWidth: 0,
      padding: '14px 16px',
      borderRadius: '8px',
      border: '1px solid var(--border)',
      background: 'var(--bg-elevated)',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '7px', marginBottom: '6px' }}>
        {Icon && <Icon size={13} style={{ color: 'var(--text-muted)' }} />}
        <span style={{ fontSize: '11px', fontWeight: 700, letterSpacing: '0.05em', textTransform: 'uppercase', color: 'var(--text-muted)' }}>
          {title}
        </span>
      </div>
      <p style={{ margin: 0, fontSize: '22px', fontWeight: 700, color }}>{value}</p>
      {sub && <p style={{ margin: '3px 0 0', fontSize: '11px', color: 'var(--text-muted)' }}>{sub}</p>}
    </div>
  )
}

function VerdictBar({ pass: passCnt = 0, warn = 0, fail = 0 }) {
  const total = passCnt + warn + fail
  if (!total) {
    return <div style={{ height: '6px', borderRadius: '3px', background: 'var(--border-subtle)', width: '100%' }} />
  }
  const pp = pct(passCnt, total)
  const wp = pct(warn, total)
  const fp = 100 - pp - wp
  return (
    <div title={`${passCnt} PASS · ${warn} WARN · ${fail} FAIL`} style={{
      display: 'flex',
      height: '6px',
      borderRadius: '3px',
      overflow: 'hidden',
      width: '100%',
      background: 'var(--border-subtle)',
    }}>
      {pp > 0 && <div style={{ width: `${pp}%`, background: 'var(--success)' }} />}
      {wp > 0 && <div style={{ width: `${wp}%`, background: 'var(--accent)' }} />}
      {fp > 0 && <div style={{ width: `${fp}%`, background: 'var(--danger)' }} />}
    </div>
  )
}

function orderCheckpoints(checkpoints) {
  const indexOf = (id) => {
    const i = CHECKPOINT_DISPLAY_ORDER.indexOf(id)
    return i === -1 ? CHECKPOINT_DISPLAY_ORDER.length : i
  }
  return [...checkpoints].sort((a, b) => {
    const ai = indexOf(a.checkpoint_id)
    const bi = indexOf(b.checkpoint_id)
    if (ai !== bi) return ai - bi
    return a.checkpoint_id.localeCompare(b.checkpoint_id)
  })
}

export default function EvalMetrics() {
  const [windowId, setWindowId] = useState('7d')

  // `since` is computed inside queryFn, not during render. Calling Date.now()
  // in render made `params` a new object on EVERY render, and it was part of
  // the query key — so the key changed continuously and the query refetched
  // far more often than the 15s interval intended. Keying on windowId alone
  // is both pure and what the caller actually varies.
  const buildParams = () => {
    const sel = WINDOWS.find((w) => w.id === windowId) || WINDOWS[0]
    if (!sel.hours) return {}
    return { since: new Date(Date.now() - sel.hours * 3600 * 1000).toISOString() }
  }

  const { data, isFetching, refetch } = useQuery({
    queryKey: ['eval-metrics', windowId],
    queryFn: () => evalApi.metrics(buildParams()).then((r) => r.data),
    refetchInterval: 15000,
    keepPreviousData: true,
  })

  const g = data?.global || {}
  const totalGlobal = g.total || 0
  const orderedCheckpoints = orderCheckpoints(data?.checkpoints || [])

  return (
    <div style={{ padding: '32px 40px', maxWidth: 1300, margin: '0 auto' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: '14px', marginBottom: '18px' }}>
        <div>
          <h1 style={{ margin: '0 0 4px', fontSize: '20px', fontWeight: 600, color: 'var(--text-primary)' }}>
            Eval Metrics
          </h1>
          <p style={{ margin: 0, fontSize: '13px', color: 'var(--text-muted)' }}>
            Pass / warn / fail rates, override usage, and critic share across all checkpoints.
          </p>
        </div>
        <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
          <select
            value={windowId}
            onChange={(e) => setWindowId(e.target.value)}
            style={{
              padding: '7px 10px', borderRadius: '6px', fontSize: '12px',
              border: '1px solid var(--border)', background: 'var(--bg-elevated)',
              color: 'var(--text-primary)',
            }}
          >
            {WINDOWS.map((w) => <option key={w.id} value={w.id}>{w.label}</option>)}
          </select>
          <button
            onClick={() => refetch()}
            disabled={isFetching}
            style={{
              display: 'inline-flex', alignItems: 'center', gap: '6px',
              padding: '7px 11px', borderRadius: '6px',
              border: '1px solid var(--border)', background: 'var(--bg-elevated)',
              color: 'var(--text-secondary)', fontSize: '12px', fontWeight: 600,
              cursor: isFetching ? 'wait' : 'pointer',
            }}
          >
            {isFetching ? <Loader size={12} style={{ animation: 'spin 1s linear infinite' }} /> : <RefreshCw size={12} />}
            Refresh
          </button>
        </div>
      </div>

      {/* Global summary cards */}
      <div style={{ display: 'flex', gap: '10px', marginBottom: '16px', flexWrap: 'wrap' }}>
        <Card title="Total verdicts" value={totalGlobal} icon={BarChart3} />
        <Card
          title="Pass rate"
          value={`${pct(g.PASS, totalGlobal)}%`}
          sub={`${g.PASS || 0} of ${totalGlobal}`}
          color="var(--success)"
          icon={TrendingUp}
        />
        <Card
          title="Warn rate"
          value={`${pct(g.WARN, totalGlobal)}%`}
          sub={`${g.WARN || 0} of ${totalGlobal}`}
          color="var(--accent)"
        />
        <Card
          title="Fail rate"
          value={`${pct(g.FAIL, totalGlobal)}%`}
          sub={`${g.FAIL || 0} of ${totalGlobal}`}
          color="var(--danger)"
          icon={AlertCircle}
        />
        <Card
          title="Overrides"
          value={g.overrides || 0}
          sub={`${pct(g.overrides, totalGlobal)}% of verdicts`}
          color="#6ea8dc"
          icon={ShieldCheck}
        />
        <Card
          title="Critic share"
          value={`${Math.round((g.critic_share || 0) * 100)}%`}
          sub={`avg latency ${fmtMs(g.avg_latency_ms)}`}
          icon={Timer}
        />
      </div>

      {/* Per-checkpoint table */}
      <div style={{
        borderRadius: '8px', border: '1px solid var(--border)', background: 'var(--bg-elevated)',
        overflow: 'hidden', marginBottom: '16px',
      }}>
        <div style={{
          padding: '14px 16px', borderBottom: '1px solid var(--border-subtle)',
          fontSize: '13px', fontWeight: 700, color: 'var(--text-primary)',
        }}>Per-checkpoint breakdown</div>

        <div style={{
          display: 'grid',
          gridTemplateColumns: '1.2fr 90px 90px 90px 90px 90px 90px 120px',
          padding: '8px 16px',
          fontSize: '10px', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em',
          color: 'var(--text-muted)', borderBottom: '1px solid var(--border-subtle)',
          background: 'var(--bg-base)',
        }}>
          <span>Checkpoint</span>
          <span style={{ textAlign: 'right' }}>Total</span>
          <span style={{ textAlign: 'right' }}>Pass %</span>
          <span style={{ textAlign: 'right' }}>Warn %</span>
          <span style={{ textAlign: 'right' }}>Fail %</span>
          <span style={{ textAlign: 'right' }}>Overrides</span>
          <span style={{ textAlign: 'right' }}>Avg lat.</span>
          <span>Verdict mix</span>
        </div>

        {orderedCheckpoints.length === 0 && !isFetching && (
          <div style={{ padding: '40px 20px', textAlign: 'center', color: 'var(--text-muted)', fontSize: '13px' }}>
            No verdicts in this window.
          </div>
        )}

        {orderedCheckpoints.map((c) => (
          <div key={c.checkpoint_id} style={{
            display: 'grid',
            gridTemplateColumns: '1.2fr 90px 90px 90px 90px 90px 90px 120px',
            padding: '11px 16px',
            fontSize: '12px',
            borderBottom: '1px solid var(--border-subtle)',
            alignItems: 'center',
          }}>
            <span style={{ fontFamily: 'ui-monospace,monospace', color: 'var(--text-primary)' }}>
              {c.checkpoint_id}
            </span>
            <span style={{ textAlign: 'right', color: 'var(--text-secondary)' }}>{c.total}</span>
            <span style={{ textAlign: 'right', color: 'var(--success)', fontWeight: 600 }}>
              {pct(c.PASS, c.total)}%
            </span>
            <span style={{ textAlign: 'right', color: 'var(--accent)', fontWeight: 600 }}>
              {pct(c.WARN, c.total)}%
            </span>
            <span style={{ textAlign: 'right', color: 'var(--danger)', fontWeight: 600 }}>
              {pct(c.FAIL, c.total)}%
            </span>
            <span style={{ textAlign: 'right', color: '#6ea8dc' }}>{c.overrides}</span>
            <span style={{ textAlign: 'right', color: 'var(--text-secondary)' }}>
              {fmtMs(c.avg_latency_ms)}
            </span>
            <VerdictBar pass={c.PASS} warn={c.WARN} fail={c.FAIL} />
          </div>
        ))}
      </div>

      {/* Top hard-fail codes */}
      <div style={{
        borderRadius: '8px', border: '1px solid var(--border)', background: 'var(--bg-elevated)',
        overflow: 'hidden',
      }}>
        <div style={{
          padding: '14px 16px', borderBottom: '1px solid var(--border-subtle)',
          fontSize: '13px', fontWeight: 700, color: 'var(--text-primary)',
        }}>Top hard-fail codes</div>
        {(g.top_hard_fail_codes || []).length === 0 ? (
          <div style={{ padding: '20px', textAlign: 'center', color: 'var(--text-muted)', fontSize: '13px' }}>
            No hard-fail codes in this window. 🎉
          </div>
        ) : (
          <div style={{ padding: '12px 16px', display: 'grid', gap: '6px' }}>
            {(g.top_hard_fail_codes || []).map((c) => (
              <div key={c.code} style={{
                display: 'grid',
                gridTemplateColumns: '1fr 90px 120px',
                gap: '10px',
                alignItems: 'center',
                padding: '7px 10px',
                borderRadius: '6px',
                background: 'var(--bg-base)',
              }}>
                <span style={{ fontFamily: 'ui-monospace,monospace', fontSize: '12px', color: 'var(--text-primary)' }}>
                  {c.code}
                </span>
                <span style={{ textAlign: 'right', color: 'var(--danger)', fontWeight: 700, fontSize: '12px' }}>
                  {c.count}
                </span>
                <div style={{ height: '6px', borderRadius: '3px', background: 'var(--border-subtle)', overflow: 'hidden' }}>
                  <div style={{
                    width: `${Math.min(100, Math.round((c.count / (g.top_hard_fail_codes[0]?.count || 1)) * 100))}%`,
                    height: '100%',
                    background: 'var(--danger)',
                  }} />
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
