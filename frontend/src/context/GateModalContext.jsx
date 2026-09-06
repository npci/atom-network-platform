// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useCallback, useMemo, useRef, useState } from 'react'
import GateBlockedModal from '../components/eval/GateBlockedModal'
import { useAuth } from '../hooks/useAuth'
import { evalApi } from '../services/api'
import { getErrorMessage, isEvalGateError, normalizeGateDetail } from '../lib/evalGate'

import { GateModalContext } from './useGateModal'

export function GateModalProvider({ children }) {
  const { user } = useAuth()
  const [modal, setModal] = useState(null)
  const modalRef = useRef(null)
  modalRef.current = modal

  const closeModal = useCallback(() => setModal(null), [])

  const openGateModal = useCallback(({ changeId, detail, retryAction, onSuccess, actionLabel }) => {
    if (!changeId || typeof retryAction !== 'function') return
    setModal({
      changeId,
      detail: normalizeGateDetail(detail),
      retryAction,
      onSuccess,
      actionLabel: actionLabel || 'Proceed',
      busy: false,
      error: null,
    })
  }, [])

  const setBusy = useCallback((busy) => {
    setModal((prev) => prev ? { ...prev, busy } : prev)
  }, [])

  const setError = useCallback((error) => {
    setModal((prev) => prev ? { ...prev, error } : prev)
  }, [])

  const updateDetail = useCallback((detail) => {
    setModal((prev) => prev ? { ...prev, detail: normalizeGateDetail(detail), error: null } : prev)
  }, [])

  const completeSuccess = useCallback((response) => {
    const current = modalRef.current
    try {
      current?.onSuccess?.(response)
    } finally {
      setModal(null)
    }
  }, [])

  const runRetry = useCallback(async (payload = {}) => {
    const current = modalRef.current
    if (!current) return
    setBusy(true)
    setError(null)
    try {
      const response = await current.retryAction(payload)
      completeSuccess(response)
    } catch (err) {
      if (isEvalGateError(err)) {
        updateDetail(err.response?.data?.detail)
      } else {
        setError(getErrorMessage(err, 'Retry failed'))
      }
      setBusy(false)
    }
  }, [completeSuccess, setBusy, setError, updateDetail])

  const acknowledgeAndRetry = useCallback(async () => {
    const current = modalRef.current
    if (!current?.detail?.required_ack_verdict_id) {
      setError('Missing required acknowledgement verdict id.')
      return
    }
    await runRetry({ eval_acknowledged_verdict_id: current.detail.required_ack_verdict_id })
  }, [runRetry, setError])

  const overrideAndRetry = useCallback(async (reason) => {
    const current = modalRef.current
    if (!current) return
    const trimmed = (reason || '').trim()
    if (trimmed.length < 8) {
      setError('Override reason must be at least 8 characters.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      await evalApi.override(current.changeId, {
        checkpoint_id: current.detail.checkpoint_id,
        reason: trimmed,
        previous_verdict_id: current.detail.verdict_id || undefined,
      })
      const retryResponse = await current.retryAction({})
      completeSuccess(retryResponse)
    } catch (err) {
      if (isEvalGateError(err)) {
        updateDetail(err.response?.data?.detail)
      } else {
        setError(getErrorMessage(err, 'Override failed'))
      }
      setBusy(false)
    }
  }, [completeSuccess, setBusy, setError, updateDetail])

  const value = useMemo(() => ({ openGateModal, closeModal }), [openGateModal, closeModal])

  return (
    <GateModalContext.Provider value={value}>
      {children}
      {modal && (
        <GateBlockedModal
          detail={modal.detail}
          actionLabel={modal.actionLabel}
          busy={modal.busy}
          error={modal.error}
          isAdmin={user?.role === 'admin'}
          onClose={closeModal}
          onRetry={modal.detail?.retry_available ? () => runRetry({}) : undefined}
          onAcknowledge={modal.detail?.requires_ack ? acknowledgeAndRetry : undefined}
          onOverride={overrideAndRetry}
        />
      )}
    </GateModalContext.Provider>
  )
}

