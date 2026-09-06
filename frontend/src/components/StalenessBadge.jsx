// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useQuery } from '@tanstack/react-query'
import { AlertTriangle } from 'lucide-react'
import { agentsApi } from '../services/api'

// "Source changed — regenerate recommended" badge. Reads the artifact-staleness
// signal and renders only when THIS stage is stale (an upstream document — e.g.
// the BRD — was changed/uploaded after this one was produced).
//
// stageKey: 'tech_spec' | 'xsd' | 'product_kit'
// subtype:  required when stageKey === 'product_kit' (e.g. 'product_note')
export default function StalenessBadge({ changeId, stageKey, subtype }) {
  const { data } = useQuery({
    queryKey: ['artifact-staleness', changeId],
    queryFn: () => agentsApi.artifactStaleness(changeId).then(r => r.data),
    enabled: !!changeId,
    staleTime: 10_000,
  })

  const stale = stageKey === 'product_kit'
    ? Boolean(data?.product_kit?.[subtype])
    : Boolean(data?.[stageKey])

  if (!stale) return null

  return (
    <span
      title="An upstream document changed after this one was produced. Regenerating is recommended so it reflects the latest source."
      style={{
        display: 'inline-flex', alignItems: 'center', gap: '5px',
        padding: '3px 9px', fontSize: '11px', fontWeight: 500,
        color: '#d97706', background: 'rgba(217,119,6,0.10)',
        border: '1px solid rgba(217,119,6,0.30)', borderRadius: '6px',
      }}
    >
      <AlertTriangle size={11} />
      Source changed — regenerate recommended
    </span>
  )
}
