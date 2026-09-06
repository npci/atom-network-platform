// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { AlertTriangle, AlertCircle, CheckCircle2 } from 'lucide-react'

/**
 * ValidationPanel — displays post-generation validation issues (errors + warnings).
 * Backend sends validation in the "done" WS event, forwarded by useAgentWS as `validation`.
 *
 * Props:
 *   validation: { error_count, warning_count, has_errors, issues: [{severity, rule, message, evidence}] }
 *   hideErrors: when true, filters out error-severity issues and shows only warnings.
 *               Used on the read-only ChangeDetail screen; step pages still show errors.
 */
export default function ValidationPanel({ validation, hideErrors = false }) {
  if (!validation) return null

  const rawIssues = validation.issues || []
  const visibleIssues = hideErrors
    ? rawIssues.filter((i) => i.severity !== 'error')
    : rawIssues
  const errorCount = hideErrors ? 0 : (validation.error_count || 0)
  const warningCount = hideErrors
    ? visibleIssues.filter((i) => i.severity === 'warning').length
    : (validation.warning_count || 0)

  if (visibleIssues.length === 0) {
    // When we're suppressing errors, don't show a misleading green "all OK" if
    // errors actually exist — render nothing instead.
    if (hideErrors && rawIssues.length > 0) return null
    return (
      <div style={{
        display: 'flex', alignItems: 'center', gap: '8px',
        padding: '10px 14px', marginBottom: '12px',
        background: 'rgba(76,175,125,0.10)',
        border: '1px solid rgba(76,175,125,0.35)',
        borderRadius: '6px', fontSize: '12px', color: 'var(--text-secondary)',
      }}>
        <CheckCircle2 size={15} style={{ color: '#4caf7d' }} />
        <span>No network/authority validation issues detected.</span>
      </div>
    )
  }

  const headerBg = errorCount > 0 ? 'rgba(224,108,108,0.12)' : 'rgba(232,164,74,0.12)'
  const headerBorder = errorCount > 0 ? 'rgba(224,108,108,0.4)' : 'rgba(232,164,74,0.4)'
  const headerColor = errorCount > 0 ? '#e06c6c' : '#e8a44a'
  const Icon = errorCount > 0 ? AlertCircle : AlertTriangle

  return (
    <div style={{
      padding: '10px 14px', marginBottom: '12px',
      background: headerBg, border: `1px solid ${headerBorder}`,
      borderRadius: '6px',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '8px' }}>
        <Icon size={15} style={{ color: headerColor }} />
        <span style={{ fontSize: '12px', fontWeight: '600', color: headerColor }}>
          {errorCount > 0 && `${errorCount} error${errorCount === 1 ? '' : 's'}`}
          {errorCount > 0 && warningCount > 0 && ', '}
          {warningCount > 0 && `${warningCount} warning${warningCount === 1 ? '' : 's'}`}
        </span>
        <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>
          from network/authority validation
        </span>
      </div>

      <ul style={{ margin: 0, paddingLeft: '20px', fontSize: '12px', lineHeight: '1.6', color: 'var(--text-secondary)' }}>
        {visibleIssues.map((issue, i) => (
          <li key={i} style={{ marginBottom: '4px' }}>
            <span style={{
              display: 'inline-block', minWidth: '60px', fontSize: '10px',
              fontWeight: '700', letterSpacing: '0.05em',
              color: issue.severity === 'error' ? '#e06c6c' : '#e8a44a',
              marginRight: '6px',
            }}>
              {issue.severity.toUpperCase()}
            </span>
            <span>{issue.message}</span>
            {issue.evidence && (
              <div style={{
                marginTop: '2px', marginLeft: '66px',
                fontFamily: 'var(--font-mono, monospace)',
                fontSize: '11px', color: 'var(--text-muted)',
                background: 'var(--bg-base)', padding: '2px 6px',
                borderRadius: '3px', display: 'inline-block',
              }}>
                {issue.evidence}
              </div>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}
