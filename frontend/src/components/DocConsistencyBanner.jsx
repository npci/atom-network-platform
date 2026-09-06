// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

/**
 * Surfaces the doc↔plan consistency check on a BRD/TSD. A blocker (the doc invented a network wire
 * message or schema change the ratified plan doesn't have) is red; lesser mismatches are amber.
 * Renders nothing when the doc is consistent or no plan existed to check against.
 */
export default function DocConsistencyBanner({ result, docLabel = 'document' }) {
  const findings = result?.findings || []
  if (!findings.length) return null
  const hasBlocker = result?.has_blocker || findings.some(f => f.severity === 'blocker')
  const attempts = result?.auto_repair_attempts || 0
  const tone = hasBlocker
    ? { fg: '#ef4444', bg: 'rgba(239,68,68,0.10)', bd: 'rgba(239,68,68,0.5)', icon: '⛔' }
    : { fg: '#d97706', bg: 'rgba(245,158,11,0.10)', bd: 'rgba(245,158,11,0.5)', icon: '⚠' }
  return (
    <div style={{ margin: '0 0 14px', padding: '10px 14px', borderRadius: 8,
      background: tone.bg, border: `1px solid ${tone.bd}` }}>
      <div style={{ fontWeight: 700, color: tone.fg, fontSize: 13.5 }}>
        {tone.icon} This {docLabel} diverges from the ratified plan
      </div>
      <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 4 }}>
        It describes a technical surface the implementation plan doesn’t define. A certifier reading
        this {docLabel} would expect APIs/messages that won’t be built.
        {attempts > 0
          ? ` Auto-correction ran ${attempts} time${attempts > 1 ? 's' : ''} but couldn’t fully reconcile it. Regenerate the ${docLabel}`
          : ` Regenerate the ${docLabel}`}
        {hasBlocker ? ' — or fix the plan if the doc is right.' : ', or accept these as intentional.'}
      </div>
      <ul style={{ margin: '8px 0 0', paddingLeft: 18 }}>
        {findings.slice(0, 8).map((f, i) => (
          <li key={i} style={{ fontSize: 12, color: 'var(--text-secondary)', marginBottom: 3 }}>
            <span style={{ fontSize: 9.5, fontWeight: 700, padding: '1px 6px', borderRadius: 10, marginRight: 6,
              background: f.severity === 'blocker' ? 'rgba(239,68,68,0.18)' : 'rgba(245,158,11,0.18)',
              color: f.severity === 'blocker' ? '#ef4444' : '#d97706' }}>
              {(f.severity || 'warning').toUpperCase()}
            </span>
            <code style={{ color: tone.fg }}>{f.item}</code>
            {f.detail ? <> — {f.detail}</> : null}
          </li>
        ))}
      </ul>
    </div>
  )
}
