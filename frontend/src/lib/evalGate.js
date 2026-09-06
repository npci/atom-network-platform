// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

export function isEvalGateDetail(detail) {
  return Boolean(
    detail &&
    typeof detail === 'object' &&
    typeof detail.blocked === 'boolean' &&
    typeof detail.checkpoint_id === 'string'
  )
}

export function isEvalGateError(err) {
  return err?.response?.status === 409 && isEvalGateDetail(err?.response?.data?.detail)
}

export function normalizeGateDetail(detail) {
  const d = (detail && typeof detail === 'object') ? detail : {}
  return {
    checkpoint_id: String(d.checkpoint_id || ''),
    policy_mode: String(d.policy_mode || ''),
    verdict: d.verdict || null,
    blocked: Boolean(d.blocked),
    reason: String(d.reason || 'Transition blocked by evaluation gate.'),
    hard_fail_codes: Array.isArray(d.hard_fail_codes) ? d.hard_fail_codes : [],
    warn_codes: Array.isArray(d.warn_codes) ? d.warn_codes : [],
    reasons: Array.isArray(d.reasons) ? d.reasons : [],
    hard_fail_details: Array.isArray(d.hard_fail_details) ? d.hard_fail_details : [],
    source_artifact_ids: Array.isArray(d.source_artifact_ids) ? d.source_artifact_ids : [],
    target_artifact_ids: Array.isArray(d.target_artifact_ids) ? d.target_artifact_ids : [],
    verdict_id: d.verdict_id || null,
    requires_ack: Boolean(d.requires_ack),
    required_ack_verdict_id: d.required_ack_verdict_id || null,
    retry_available: Boolean(d.retry_available),
    retries_used: Number.isFinite(d.retries_used) ? d.retries_used : 0,
    max_retries: Number.isFinite(d.max_retries) ? d.max_retries : 1,
    override_allowed: Boolean(d.override_allowed),
  }
}

export function getErrorMessage(err, fallback = 'Action failed') {
  const detail = err?.response?.data?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  if (detail && typeof detail === 'object' && typeof detail.reason === 'string' && detail.reason.trim()) {
    return detail.reason
  }
  return err?.message || fallback
}
