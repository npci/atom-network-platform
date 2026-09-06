// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

/**
 * KpiStrip — horizontal row of stat tiles.
 *
 *   <KpiStrip tiles={[
 *     { label: 'Total CRs', value: 12 },
 *     { label: 'Completed', value: 8, color: 'var(--success)' },
 *     { label: 'In Progress', value: 3, sub: '2 stalled >7d', color: 'var(--warning)' },
 *     { label: 'Partners', value: '24/30', sub: 'certified', color: 'var(--accent)' },
 *   ]} />
 *
 * Each tile: label, value, optional secondary line, optional accent colour.
 * Renders as a CSS grid that auto-fits on narrow viewports.
 */
import { Loader } from 'lucide-react'

export default function KpiStrip({ tiles, loading = false }) {
  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: `repeat(auto-fit, minmax(180px, 1fr))`,
        gap: 'var(--space-3)',
        marginBottom: 'var(--space-6)',
      }}
    >
      {(tiles || []).map((t, i) => (
        <div
          key={t.label || i}
          style={{
            background: 'var(--bg-card)',
            border: '1px solid var(--border)',
            borderRadius: '10px',
            padding: 'var(--space-4)',
            position: 'relative',
            overflow: 'hidden',
          }}
        >
          {/* coloured accent bar on the left */}
          {t.color && (
            <span
              aria-hidden
              style={{
                position: 'absolute',
                left: 0, top: 0, bottom: 0,
                width: '3px',
                background: t.color,
              }}
            />
          )}
          <p style={{
            margin: 0,
            fontSize: '11px',
            color: 'var(--text-muted)',
            textTransform: 'uppercase',
            letterSpacing: '0.06em',
            fontWeight: 600,
          }}>
            {t.label}
          </p>
          <p style={{
            margin: '6px 0 2px',
            fontSize: '24px',
            fontWeight: 700,
            color: t.color || 'var(--text-primary)',
            lineHeight: 1.1,
            display: 'flex',
            alignItems: 'center',
            gap: 'var(--space-2)',
          }}>
            {loading ? <span className="skeleton" style={{ width: 60, height: 24 }} /> : t.value}
            {t.trend && (
              <span style={{
                fontSize: '11px',
                fontWeight: 600,
                color: t.trend.positive ? 'var(--success)' : 'var(--danger)',
              }}>
                {t.trend.positive ? '↑' : '↓'} {t.trend.value}
              </span>
            )}
          </p>
          {t.sub && (
            <p style={{ margin: 0, fontSize: '11px', color: 'var(--text-muted)' }}>{t.sub}</p>
          )}
        </div>
      ))}
    </div>
  )
}
