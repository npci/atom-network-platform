// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

/**
 * Clarification stage — the agentic Change-Analysis flow.
 *
 * The legacy gap-question UI (clarifyApi-driven Q&A) has been removed. The code-grounded
 * `AnalysisPanel` now owns this stage end to end: it asks the questions (with options +
 * recommendations), records the answers, proposes the plan, and gates ratification +
 * "Proceed to BRD". Open to the product/tech planning roles (PM included).
 */
import { useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { changesApi } from '../../services/api'
import SkipStepButton from '../../components/common/SkipStepButton'
import TranscriptsDownloadButton from '../../components/TranscriptsDownloadButton'
import AnalysisPanel from '../../components/AnalysisPanel'
import { ArrowLeft } from 'lucide-react'

export default function Clarification() {
  const { id } = useParams()
  const navigate = useNavigate()
  // 'pending' (resolving) | 'active' (analysis owns the stage) | 'disabled' (no indexed repos)
  const [analysisStatus, setAnalysisStatus] = useState('pending')

  const { data: change } = useQuery({
    queryKey: ['change', id],
    queryFn:  () => changesApi.get(id).then(r => r.data),
  })

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      {/* Header */}
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '14px 24px', borderBottom: '1px solid var(--border)', background: 'var(--bg-elevated)',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '14px' }}>
          <button onClick={() => navigate(`/changes/${id}/canvas`)} style={{
            display: 'flex', alignItems: 'center', gap: '6px', padding: '6px 10px', fontSize: '12px',
            background: 'var(--bg-base)', color: 'var(--text-secondary)',
            border: '1px solid var(--border)', borderRadius: '6px', cursor: 'pointer',
          }}>
            <ArrowLeft size={14} /> Back to Canvas
          </button>
          <div style={{ width: 1, height: 18, background: 'var(--border)' }} />
          <div>
            <p style={{ margin: 0, fontSize: '14px', fontWeight: '600', color: 'var(--text-primary)' }}>
              Clarification
            </p>
            <p style={{ margin: 0, fontSize: '11px', color: 'var(--text-muted)' }}>
              {change?.title || change?.initial_prompt?.slice(0, 80)}
            </p>
          </div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
          <TranscriptsDownloadButton changeId={id} section="clarification" label="Transcripts" />
          <SkipStepButton changeId={id} nextRoute={`/changes/${id}`} />
        </div>
      </div>

      {/* Body — the agentic Change-Analysis owns this stage */}
      <div style={{ flex: 1, overflow: 'auto', padding: '32px', background: 'var(--bg-base)' }}>
        <div style={{ maxWidth: '820px', margin: '0 auto' }}>
          <AnalysisPanel changeId={id} onStatus={setAnalysisStatus} />
          {analysisStatus === 'disabled' && (
            <div style={{ textAlign: 'center', color: 'var(--text-muted)', marginTop: 60, fontSize: 14, lineHeight: 1.7 }}>
              No code-grounded analysis is available for this change
              (no indexed repositories selected for it).<br />
              Use “Skip step” above to proceed to BRD.
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
