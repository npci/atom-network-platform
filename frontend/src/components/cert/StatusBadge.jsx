// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

/**
 * StatusBadge — single-source-of-truth pill for any status enum.
 *
 *   <StatusBadge kind="assignment"  value="certified" />
 *   <StatusBadge kind="cert_run"    value="running"   size="sm" />
 *   <StatusBadge kind="tc_result"   value="fail"      withIcon={false} />
 *
 * Colours/icons/labels live in lib/certStatus.js.
 */
import { resolveStatus } from '../../lib/certStatus'

export default function StatusBadge({
  kind,
  value,
  size = 'md',
  withIcon = true,
  className,
  style,
}) {
  if (!value) return null
  const resolved = resolveStatus(kind, value)
  if (!resolved) return null
  const { label, color, icon: Icon } = resolved

  const dims = size === 'sm'
    ? { padding: '2px 8px',  fontSize: '10px', iconSize: 9  }
    : { padding: '3px 10px', fontSize: '11px', iconSize: 11 }

  return (
    <span
      className={className}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 'var(--space-1)',
        padding: dims.padding,
        borderRadius: '999px',
        fontSize: dims.fontSize,
        fontWeight: 600,
        color,
        background: `${color}1A`,
        border: `1px solid ${color}40`,
        whiteSpace: 'nowrap',
        ...style,
      }}
    >
      {withIcon && Icon && <Icon size={dims.iconSize} />}
      {label}
    </span>
  )
}
