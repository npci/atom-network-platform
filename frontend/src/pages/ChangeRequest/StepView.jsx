// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useParams, useNavigate, Navigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ArrowLeft, Eye } from 'lucide-react'
import { changesApi } from '../../services/api'
import { STAGES, EXPANDABLE_STAGES } from './stages'
import {
  PromptEnhancementDetail,
  ResearchDetail,
  CanvasDetail,
  ClarificationStageDetail,
  BRDDetail,
  TechSpecDetail,
  XSDDetail,
  ProductKitDetail,
} from './ChangeDetail'

// Read-only viewers, keyed by Phase-A stage. These components only issue GET
// requests — no WebSocket, no generate/submit mutation — so this page shows a
// step's data without ever initiating the step.
const DETAIL_BY_KEY = {
  prompt_enhancement: PromptEnhancementDetail,
  research:           ResearchDetail,
  canvas:             CanvasDetail,
  clarification:      ClarificationStageDetail,
  brd:                BRDDetail,
  tech_spec:          TechSpecDetail,
  xsd:                XSDDetail,
  product_kit:        ProductKitDetail,
}

export default function StepView() {
  const { id, stepKey } = useParams()
  const navigate = useNavigate()

  const Detail = DETAIL_BY_KEY[stepKey]
  const stage = STAGES.find(s => s.key === stepKey)

  const { data: change } = useQuery({
    queryKey: ['change', id],
    queryFn:  () => changesApi.get(id).then(r => r.data),
    staleTime: 0,
  })

  // Unknown / non-viewable step → back to the change details screen.
  if (!Detail || !EXPANDABLE_STAGES.includes(stepKey)) {
    return <Navigate to={`/changes/${id}`} replace />
  }

  const label = stage?.label || stepKey

  return (
    <div style={{ padding: '32px 40px', maxWidth: 1200, margin: '0 auto' }}>
      {/* Back */}
      <button
        onClick={() => navigate(`/changes/${id}`)}
        style={{
          display: 'flex', alignItems: 'center', gap: '6px',
          fontSize: '13px', color: 'var(--text-muted)',
          background: 'none', border: 'none', cursor: 'pointer',
          padding: 0, marginBottom: '24px',
        }}
        onMouseEnter={e => e.currentTarget.style.color = 'var(--text-primary)'}
        onMouseLeave={e => e.currentTarget.style.color = 'var(--text-muted)'}
      >
        <ArrowLeft size={14} /> Back to change
      </button>

      {/* Header */}
      <div style={{ marginBottom: '20px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap', marginBottom: '6px' }}>
          <h1 style={{ margin: 0, fontSize: '20px', fontWeight: '600', color: 'var(--text-primary)' }}>
            {label}
          </h1>
          <span style={{
            display: 'inline-flex', alignItems: 'center', gap: '5px',
            fontSize: '11px', fontWeight: '600', color: 'var(--text-muted)',
            background: 'var(--bg-elevated)', border: '1px solid var(--border)',
            borderRadius: '20px', padding: '2px 10px', textTransform: 'uppercase', letterSpacing: '0.06em',
          }}>
            <Eye size={12} /> Read-only
          </span>
        </div>
        {change?.title && (
          <p style={{ margin: 0, fontSize: '13px', color: 'var(--text-muted)', lineHeight: '1.6' }}>
            {change.title}
          </p>
        )}
      </div>

      {/* Read-only detail — same viewer used for the inline expansion, no run controls */}
      <div style={{
        background: 'var(--bg-elevated)', border: '1px solid var(--border)',
        borderRadius: '10px', overflow: 'hidden',
      }}>
        <Detail changeId={id} enhancedPrompt={change?.enhanced_prompt} />
      </div>
    </div>
  )
}
