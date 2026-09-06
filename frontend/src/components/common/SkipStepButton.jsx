// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { SkipForward } from 'lucide-react'
import api from '../../services/api'
import { useIsDevMode } from '../../hooks/useUiConfig'
import { useGateModal } from '../../context/useGateModal'
import { isEvalGateError } from '../../lib/evalGate'

// SkipStepButton — dev-only "skip this step" control.
//
// Visibility: rendered only when `useIsDevMode()` is true. The hook reads
// /api/config/ui which the backend computes from `app_env != "production"`.
// In production the backend also rejects force_skip=true (defence in
// depth), so even if the button were rendered it wouldn't actually skip.
//
// Behaviour: POST /api/changes/{id}/advance with `force_skip: true`. The
// backend bypasses the CLARIFICATION→BRD gate when this flag is set.
// On success, invalidates the change query and (optionally) navigates to
// the next route.
//
// Props:
//   changeId    — required. The change-request UUID.
//   nextRoute   — optional. If provided, navigates here on success.
//                 Pass the full path including /changes/{id}/...
//   label       — optional override of the button text.
//   onSkipped   — optional callback fired AFTER the skip succeeds.
export default function SkipStepButton({
  changeId,
  nextRoute,
  label = 'Skip step',
  onSkipped,
}) {
  const isDev = useIsDevMode()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [busy, setBusy] = useState(false)
  const { openGateModal } = useGateModal()

  if (!isDev) return null

  const handleClick = async () => {
    if (busy) return
    const ok = window.confirm(
      `Skip this step?\n\nThe change request will advance to the next stage WITHOUT generating this step's artifact. Downstream agents will receive empty input from this stage.\n\nThis is a dev-mode control and is disabled in production.`
    )
    if (!ok) return
    setBusy(true)
    try {
      await api.post(`/changes/${changeId}/advance`, { force_skip: true })
      queryClient.invalidateQueries({ queryKey: ['change', changeId] })
      if (typeof onSkipped === 'function') onSkipped()
      if (nextRoute) navigate(nextRoute)
    } catch (err) {
      if (isEvalGateError(err)) {
        openGateModal({
          changeId,
          detail: err.response?.data?.detail,
          actionLabel: `Skip step (${label})`,
          retryAction: (payload) => api.post(`/changes/${changeId}/advance`, {
            force_skip: true,
            ...(payload || {}),
          }),
          onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ['change', changeId] })
            if (typeof onSkipped === 'function') onSkipped()
            if (nextRoute) navigate(nextRoute)
          },
        })
      } else {
        console.error('skip step failed', err)
        alert(`Skip failed: ${err.response?.data?.detail || err.message}`)
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <button
      onClick={handleClick}
      disabled={busy}
      title="Dev-only: skip this step without running its agent"
      style={{
        display: 'inline-flex', alignItems: 'center', gap: '6px',
        padding: '6px 12px',
        background: 'transparent',
        color: '#d97706',
        border: '1px dashed #d97706',
        borderRadius: '6px',
        fontSize: '12px', fontWeight: '500',
        cursor: busy ? 'not-allowed' : 'pointer',
        opacity: busy ? 0.6 : 1,
      }}
    >
      <SkipForward size={13} />
      {busy ? 'Skipping…' : label}
    </button>
  )
}
