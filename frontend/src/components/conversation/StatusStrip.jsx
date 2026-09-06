// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { AlertTriangle, MessageCircle, GitPullRequest } from 'lucide-react'

// Color + icon per awaiting-action kind. Counter is partner-blue (matches
// the counter bubble accent on chat surfaces), blocker is danger-red,
// query is the AI/draft purple used elsewhere.
const KIND_META = {
  counter: { color: '#6ea8dc', Icon: GitPullRequest  },
  blocker: { color: '#e06c6c', Icon: AlertTriangle   },
  query:   { color: '#bc8cff', Icon: MessageCircle   },
}

/**
 * Sticky one-line "what needs your attention" header rendered above the
 * conversation timeline. Driven by the backend `status_strip` object
 * returned alongside `messages` on /negotiation. Hidden when nothing is
 * open and nothing is awaiting action — no empty-state noise.
 *
 * Shape of `strip` (from backend):
 *   { open_counters: int,
 *     open_blockers: int,
 *     awaiting_action: [{ kind, ref, label }] }
 */
export default function StatusStrip({ strip }) {
  if (!strip) return null
  const openCounters   = strip.open_counters   ?? 0
  const openBlockers   = strip.open_blockers   ?? 0
  const awaitingAction = strip.awaiting_action ?? []

  if (openCounters === 0 && openBlockers === 0 && awaitingAction.length === 0) {
    return null
  }

  return (
    <div style={{
      padding: '8px 14px',
      borderBottom: '1px solid var(--border-subtle)',
      background: 'var(--bg-elevated)',
      display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 10,
      fontSize: 11, flexShrink: 0,
    }}>
      {(openCounters > 0 || openBlockers > 0) && (
        <div style={{ display: 'flex', gap: 10, color: 'var(--text-secondary)' }}>
          {openCounters > 0 && (
            <span>
              <strong style={{ color: '#6ea8dc' }}>{openCounters}</strong>{' '}
              open counter{openCounters !== 1 ? 's' : ''}
            </span>
          )}
          {openBlockers > 0 && (
            <span>
              <strong style={{ color: '#e06c6c' }}>{openBlockers}</strong>{' '}
              active blocker{openBlockers !== 1 ? 's' : ''}
            </span>
          )}
        </div>
      )}

      {awaitingAction.length > 0 && (
        <>
          <span style={{
            fontSize: 9, color: 'var(--text-muted)',
            textTransform: 'uppercase', letterSpacing: 0.5, fontWeight: 700,
          }}>Awaiting you</span>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
            {awaitingAction.map((a, i) => {
              const meta = KIND_META[a.kind] || KIND_META.query
              return (
                <span
                  key={`${a.kind}-${a.ref}-${i}`}
                  title={a.ref}
                  style={{
                    display: 'inline-flex', alignItems: 'center', gap: 4,
                    padding: '2px 8px', borderRadius: 999,
                    background: `${meta.color}15`, border: `1px solid ${meta.color}40`,
                    color: meta.color, fontWeight: 600, fontSize: 10,
                  }}
                >
                  <meta.Icon size={10} />
                  {a.label}
                </span>
              )
            })}
          </div>
        </>
      )}
    </div>
  )
}
