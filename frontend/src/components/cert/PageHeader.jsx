// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

/**
 * PageHeader — top-of-page chrome: breadcrumb, title, subtitle, action slot.
 *
 *   <PageHeader
 *     icon={Award}
 *     crumbs={[{label: 'Certification', to: '/certification/dashboard'}, {label: 'Overview'}]}
 *     title="Change Request Certification"
 *     subtitle="Cert progress for every released change"
 *     actions={<button onClick={refetch}>Refresh</button>}
 *   />
 *
 * Crumbs render as `Certification › Overview` with optional `to` for a Link.
 */
import { Link } from 'react-router-dom'
import { ChevronRight } from 'lucide-react'

export default function PageHeader({
  icon: Icon,
  crumbs = [],
  title,
  subtitle,
  actions,
}) {
  return (
    <header
      style={{
        display: 'flex',
        alignItems: 'flex-start',
        justifyContent: 'space-between',
        gap: 'var(--space-4)',
        marginBottom: 'var(--space-6)',
        paddingBottom: 'var(--space-4)',
        borderBottom: '1px solid var(--border-subtle)',
      }}
    >
      <div style={{ minWidth: 0 }}>
        {crumbs.length > 0 && (
          <nav
            aria-label="Breadcrumb"
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 'var(--space-1)',
              marginBottom: 'var(--space-2)',
              fontSize: '11px',
              color: 'var(--text-muted)',
              flexWrap: 'wrap',
            }}
          >
            {crumbs.map((c, idx) => (
              <span key={idx} style={{ display: 'inline-flex', alignItems: 'center', gap: 'var(--space-1)' }}>
                {idx > 0 && <ChevronRight size={11} aria-hidden style={{ opacity: 0.5 }} />}
                {c.to
                  ? (
                    <Link
                      to={c.to}
                      style={{
                        color: 'var(--text-muted)',
                        textDecoration: 'none',
                      }}
                      onMouseEnter={e => (e.currentTarget.style.color = 'var(--text-primary)')}
                      onMouseLeave={e => (e.currentTarget.style.color = 'var(--text-muted)')}
                    >
                      {c.label}
                    </Link>
                  )
                  : (
                    <span style={{ color: idx === crumbs.length - 1 ? 'var(--text-secondary)' : 'var(--text-muted)' }}>
                      {c.label}
                    </span>
                  )}
              </span>
            ))}
          </nav>
        )}
        <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)' }}>
          {Icon && <Icon size={20} style={{ color: 'var(--accent)', flexShrink: 0 }} />}
          <h1 style={{
            margin: 0,
            fontSize: '20px',
            fontWeight: 700,
            color: 'var(--text-primary)',
            lineHeight: 1.2,
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}>
            {title}
          </h1>
        </div>
        {subtitle && (
          <p style={{
            margin: '6px 0 0',
            fontSize: '13px',
            color: 'var(--text-muted)',
            maxWidth: '720px',
          }}>
            {subtitle}
          </p>
        )}
      </div>
      {actions && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', flexShrink: 0 }}>
          {actions}
        </div>
      )}
    </header>
  )
}
