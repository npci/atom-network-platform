// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

function Section({ title, children }) {
  return (
    <div style={{ marginTop: '10px' }}>
      <p style={{
        margin: '0 0 6px',
        fontSize: '10px',
        color: 'var(--text-muted)',
        textTransform: 'uppercase',
        letterSpacing: '0.05em',
        fontWeight: 700,
      }}>
        {title}
      </p>
      {children}
    </div>
  )
}

function Chip({ label, value, tone = 'default' }) {
  const palette = {
    default: { bg: 'var(--bg-base)', color: 'var(--text-secondary)', border: 'var(--border)' },
    warn: { bg: 'rgba(218,119,86,0.10)', color: 'var(--accent)', border: 'rgba(218,119,86,0.30)' },
    danger: { bg: 'rgba(224,108,108,0.10)', color: 'var(--danger)', border: 'rgba(224,108,108,0.30)' },
    success: { bg: 'rgba(76,175,125,0.10)', color: 'var(--success)', border: 'rgba(76,175,125,0.30)' },
  }[tone] || { bg: 'var(--bg-base)', color: 'var(--text-secondary)', border: 'var(--border)' }

  return (
    <span style={{
      display: 'inline-flex',
      alignItems: 'center',
      gap: '4px',
      fontSize: '10px',
      padding: '2px 8px',
      borderRadius: '999px',
      background: palette.bg,
      color: palette.color,
      border: `1px solid ${palette.border}`,
      fontWeight: 600,
    }}>
      {label && <span style={{ opacity: 0.8 }}>{label}</span>}
      <span>{value}</span>
    </span>
  )
}

export default function WhyBlockedPanel({ detail }) {
  if (!detail) return null

  const verdictTone = detail.verdict === 'PASS'
    ? 'success'
    : detail.verdict === 'WARN'
      ? 'warn'
      : 'danger'

  return (
    <div style={{
      marginTop: '12px',
      padding: '12px',
      background: 'var(--bg-base)',
      border: '1px solid var(--border)',
      borderRadius: '8px',
    }}>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px' }}>
        <Chip label="Checkpoint" value={detail.checkpoint_id || 'unknown'} />
        <Chip label="Mode" value={detail.policy_mode || 'unknown'} tone={detail.policy_mode === 'hard_gate' ? 'danger' : detail.policy_mode === 'soft_gate' ? 'warn' : 'default'} />
        {detail.verdict && <Chip label="Verdict" value={detail.verdict} tone={verdictTone} />}
      </div>

      <Section title="Gate reason">
        <p style={{ margin: 0, fontSize: '12px', color: 'var(--text-primary)', lineHeight: 1.5 }}>
          {detail.reason}
        </p>
      </Section>

      {detail.reasons?.length > 0 && (
        <Section title="Top reasons">
          <ul style={{ margin: 0, paddingLeft: '18px', fontSize: '12px', color: 'var(--text-secondary)' }}>
            {detail.reasons.slice(0, 5).map((r, idx) => (
              <li key={`${idx}-${r}`} style={{ marginBottom: '4px' }}>{r}</li>
            ))}
          </ul>
        </Section>
      )}

      {detail.hard_fail_codes?.length > 0 && (
        <Section title="Hard-fail codes">
          <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap' }}>
            {detail.hard_fail_codes.map((c) => (
              <Chip key={c} value={c} tone="danger" />
            ))}
          </div>
        </Section>
      )}

      {detail.hard_fail_details?.length > 0 && (
        <Section title="Hard-fail details">
          <div style={{ display: 'grid', gap: '8px' }}>
            {detail.hard_fail_details.map((item) => (
              <div key={item.code} style={{ padding: '8px', borderRadius: '6px', border: '1px solid var(--border-subtle)', background: 'var(--bg-elevated)' }}>
                <p style={{ margin: '0 0 4px', fontSize: '11px', color: 'var(--text-primary)', fontWeight: 600 }}>
                  {item.code} — {item.title}
                </p>
                {item.meaning && (
                  <p style={{ margin: '0 0 4px', fontSize: '11px', color: 'var(--text-secondary)' }}>
                    {item.meaning}
                  </p>
                )}
                {item.remediation && (
                  <p style={{ margin: 0, fontSize: '11px', color: 'var(--text-muted)' }}>
                    Remediation: {item.remediation}
                  </p>
                )}
              </div>
            ))}
          </div>
        </Section>
      )}

      {(detail.source_artifact_ids?.length > 0 || detail.target_artifact_ids?.length > 0) && (
        <Section title="Artifacts">
          <div style={{ display: 'grid', gap: '6px' }}>
            <p style={{ margin: 0, fontSize: '11px', color: 'var(--text-secondary)' }}>
              <strong>Source:</strong> {detail.source_artifact_ids?.length ? detail.source_artifact_ids.join(', ') : 'None'}
            </p>
            <p style={{ margin: 0, fontSize: '11px', color: 'var(--text-secondary)' }}>
              <strong>Target:</strong> {detail.target_artifact_ids?.length ? detail.target_artifact_ids.join(', ') : 'None'}
            </p>
          </div>
        </Section>
      )}
    </div>
  )
}
