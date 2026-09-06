// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

// Headline-number tile used on dashboard-y pages. Shared across the
// main Dashboard + the admin list pages (Approvals, Partners, Users,
// Product/Code knowledge, Code indexing) so the visual treatment
// stays consistent and we don't fork the styling.
//
// Props:
//   label   — uppercase eyebrow text
//   value   — the big number (or short string)
//   accent  — CSS color for the value text (default: --text-primary)
//   hint    — optional sub-text below the number; pass null/false to omit
//   onClick — optional click handler; tile becomes button-like when set
export default function StatTile({ label, value, accent, hint, onClick }) {
  const clickable = typeof onClick === 'function'
  return (
    <div
      role={clickable ? 'button' : undefined}
      tabIndex={clickable ? 0 : undefined}
      onClick={clickable ? onClick : undefined}
      onKeyDown={clickable ? (e) => { if (e.key === 'Enter' || e.key === ' ') onClick() } : undefined}
      style={{
        padding: '16px 18px',
        background: 'var(--bg-elevated)',
        border: '1px solid var(--border)',
        borderRadius: '8px',
        display: 'flex', flexDirection: 'column', gap: '6px',
        minHeight: '88px',
        cursor: clickable ? 'pointer' : 'default',
        transition: clickable ? 'border-color 0.15s, background 0.15s' : 'none',
      }}
      onMouseEnter={clickable ? (e) => { e.currentTarget.style.borderColor = 'var(--accent)' } : undefined}
      onMouseLeave={clickable ? (e) => { e.currentTarget.style.borderColor = 'var(--border)' } : undefined}
    >
      <span style={{
        fontSize: '11px', fontWeight: 600, letterSpacing: '0.04em',
        textTransform: 'uppercase', color: 'var(--text-muted)',
      }}>{label}</span>
      <span style={{
        fontSize: '28px', fontWeight: 600,
        color: accent || 'var(--text-primary)',
        lineHeight: 1.1,
      }}>{value}</span>
      {hint && (
        <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>{hint}</span>
      )}
    </div>
  )
}


// Convenience wrapper for the most common pattern — a responsive grid
// that auto-wraps. Drop in directly above a list/table.
//
// `minTileWidth` defaults to 180 (was 220) so the standard 4-tile row
// fits on 1024px-wide viewports without pushing a horizontal scrollbar.
// With sidebar=220 + Dashboard padding=80, the available inner width on
// a 1024px viewport is ~724px — four 180px tiles + three 14px gaps =
// 762px still overflows there, but auto-fit wraps to a 2×2 grid as soon
// as the row can't fit, which is the desired responsive behaviour.
// `minWidth: 0` lets the grid itself shrink below the intrinsic width
// of its content when nested inside flex containers (which Dashboard,
// AppLayout, and the admin pages all are).
export function StatTileRow({ children, minTileWidth = 180 }) {
  return (
    <div style={{
      display: 'grid',
      gridTemplateColumns: `repeat(auto-fit, minmax(${minTileWidth}px, 1fr))`,
      gap: '14px',
      marginBottom: '24px',
      minWidth: 0,
    }}>
      {children}
    </div>
  )
}
