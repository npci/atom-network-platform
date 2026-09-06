// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

/**
 * Section — bordered card with optional title, subtitle, and right-side
 * action slot. Replaces the ad-hoc bordered <div>s scattered across the
 * Certification pages.
 *
 *   <Section title="Filters" actions={<Button/>}>...</Section>
 *   <Section title="Run #4" subtitle="completed · 38/39 passed">...</Section>
 *
 * The card uses theme tokens so it adapts to light/dark.
 */

export default function Section({
  title,
  subtitle,
  actions,
  children,
  padded = true,
  className,
  style,
}) {
  return (
    <section
      className={className}
      style={{
        background: 'var(--bg-card)',
        border: '1px solid var(--border)',
        borderRadius: '10px',
        overflow: 'hidden',
        ...style,
      }}
    >
      {(title || actions) && (
        <header
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: 'var(--space-3)',
            padding: 'var(--space-3) var(--space-5)',
            borderBottom: '1px solid var(--border-subtle)',
          }}
        >
          <div style={{ minWidth: 0 }}>
            {title && (
              <p
                style={{
                  margin: 0,
                  fontSize: '13px',
                  fontWeight: 600,
                  color: 'var(--text-primary)',
                  whiteSpace: 'nowrap',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                }}
              >
                {title}
              </p>
            )}
            {subtitle && (
              <p
                style={{
                  margin: '2px 0 0',
                  fontSize: '11px',
                  color: 'var(--text-muted)',
                }}
              >
                {subtitle}
              </p>
            )}
          </div>
          {actions && <div style={{ display: 'flex', gap: 'var(--space-2)', alignItems: 'center' }}>{actions}</div>}
        </header>
      )}
      <div style={padded ? { padding: 'var(--space-5)' } : undefined}>
        {children}
      </div>
    </section>
  )
}
