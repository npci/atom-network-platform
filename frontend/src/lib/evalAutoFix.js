// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

// Build a structured "fix this and regenerate" feedback prompt from an
// eval verdict's reasons + hard-fail codes + warn codes. Used by the
// EvalStatusPill's Auto-fix button to feed the existing artifact-
// generation WebSocket without round-tripping a new backend endpoint.

const FIXED_LEAD = (
  'The previous version failed evaluation. Address every numbered finding ' +
  'below in your next regeneration and return a full revised artifact. ' +
  'Do not skip findings. Do not invent fixes that contradict the source ' +
  'artifacts.'
)

const FALLBACK_BODY = (
  'No specific findings were recorded. Improve clarity, completeness, ' +
  'and grounding in the source artifacts.'
)

const MAX_REASONS = 12  // keep the prompt readable
const MAX_REASON_CHARS = 600 // each line

function clean(line) {
  if (typeof line !== 'string') return ''
  const trimmed = line.trim()
  if (!trimmed) return ''
  if (trimmed.length <= MAX_REASON_CHARS) return trimmed
  return trimmed.slice(0, MAX_REASON_CHARS) + '…'
}

// Strip the "[critic:dimension score=...]" tag the critic adds so the
// generator sees plain English findings, not internal tags.
function stripCriticTag(line) {
  return line.replace(/^\[critic[^\]]*\]\s*/i, '').trim()
}

export function buildAutoFixFeedback(verdict, options = {}) {
  if (!verdict || typeof verdict !== 'object') return ''

  const reasons = Array.isArray(verdict.reasons) ? verdict.reasons : []
  const hardCodes = Array.isArray(verdict.hard_fail_codes) ? verdict.hard_fail_codes : []
  const warnCodes = Array.isArray(verdict.warn_codes) ? verdict.warn_codes : []

  const lines = []
  lines.push(FIXED_LEAD)

  if (verdict.verdict) {
    lines.push('')
    lines.push(`Last verdict: ${verdict.verdict}` + (
      typeof verdict.confidence === 'number'
        ? ` (confidence ${Math.round(verdict.confidence * 100)}%)`
        : ''
    ))
  }

  if (hardCodes.length > 0) {
    lines.push('Hard-fail codes: ' + hardCodes.join(', '))
  }
  if (warnCodes.length > 0) {
    lines.push('Warn codes: ' + warnCodes.join(', '))
  }

  const numbered = []
  for (const raw of reasons) {
    const cleaned = clean(stripCriticTag(raw))
    if (cleaned) numbered.push(cleaned)
    if (numbered.length >= MAX_REASONS) break
  }

  lines.push('')
  if (numbered.length === 0) {
    lines.push(FALLBACK_BODY)
  } else {
    lines.push('Findings to fix:')
    numbered.forEach((reason, i) => {
      lines.push(`${i + 1}. ${reason}`)
    })
    if (reasons.length > numbered.length) {
      lines.push(`(${reasons.length - numbered.length} additional findings truncated)`)
    }
  }

  const manualFeedback = clean(options.manualFeedback || '')
  if (manualFeedback) {
    lines.push('')
    lines.push('Additional user guidance to apply after the evaluator findings above:')
    lines.push(manualFeedback)
  }

  lines.push('')
  lines.push(
    'After applying every fix, output the complete revised artifact in the ' +
    'same format as before. Do not add commentary.'
  )

  if (options.tag) {
    lines.push('')
    lines.push(`# auto-fix:${options.tag}`)
  }

  return lines.join('\n')
}
