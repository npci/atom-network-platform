// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useQuery } from '@tanstack/react-query'
import { agentsApi } from '../services/api'

// Plan version history + the changelog explaining why each version exists (e.g.
// "Reconciled uploaded BRD v2: …"). Renders nothing until there's >1 version, so
// it only appears once a reconciliation (or approach change) re-versioned the plan.

const CARD = {
  marginTop: 12, padding: '12px 14px', borderRadius: 8,
  background: 'rgba(96,165,250,0.06)', border: '1px solid rgba(96,165,250,0.3)',
}

function summarize(rev) {
  if (!rev) return null
  if (rev.kind === 'upload_reconciliation') {
    const ds = (rev.deltas || []).map(d => d.directive).filter(Boolean)
    const docLabel = rev.doc_kind === 'tech_spec' ? 'Tech Spec' : 'BRD'
    return `Reconciled uploaded ${docLabel}${rev.doc_version ? ` v${rev.doc_version}` : ''}`
      + (ds.length ? `: ${ds.join('; ')}` : '')
  }
  if (rev.kind === 'approach_decision') {
    return `Approach change: ${rev.chosen_title || rev.approach || ''}${rev.why ? ` — ${rev.why}` : ''}`
  }
  return rev.kind || null
}

export default function PlanVersionHistory({ changeId }) {
  const { data } = useQuery({
    queryKey: ['analysis-versions', changeId],
    queryFn: () => agentsApi.listAnalysisVersions(changeId).then(r => r.data),
    enabled: !!changeId,
  })
  const versions = data?.versions || []
  if (versions.length < 2) return null

  return (
    <section style={CARD}>
      <div style={{ fontWeight: 700, fontSize: 13 }}>📋 Plan version history</div>
      <div style={{ fontSize: 11.5, color: 'var(--text-muted)', marginBottom: 4 }}>
        Each version records what changed and why — the latest is authoritative.
      </div>
      {versions.map((v, i) => {
        const why = summarize(v.revision)
        const isLatest = i === 0
        return (
          <div key={v.version} style={{ padding: '6px 0', borderTop: '1px solid var(--border-subtle)' }}>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
              <span style={{ fontWeight: 700, fontSize: 12.5 }}>v{v.version}</span>
              {isLatest
                ? <span style={{ fontSize: 10, fontWeight: 700, padding: '1px 6px', borderRadius: 10, background: 'rgba(52,211,153,0.15)', color: '#16a34a' }}>CURRENT</span>
                : <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>{v.status}</span>}
              {v.created_at && <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>{new Date(v.created_at).toLocaleString()}</span>}
            </div>
            {why && <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 2 }}>{why}</div>}
          </div>
        )
      })}
    </section>
  )
}
