// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

/**
 * ProgressBanner — the resume banner shown at the top of any screen
 * whose backend agent is mid-stream and the user just navigated back.
 *
 * Renders nothing when there's no in-flight job. Otherwise shows:
 *   - a spinner + the current stage label
 *   - an optional progress bar (when jobProgress is a number 0..100)
 *   - "started X min Y s ago" relative timestamp
 *   - an attribution line when a different user started the job
 *     (visibility-Y rule: shown for change-scoped jobs)
 *   - a Cancel link when the cancel handler is provided
 *
 * Used by R-3+ screen wiring like:
 *
 *   const { jobId, jobStatus, jobStage, jobProgress, jobStartedAt,
 *           jobStartedBy, isResuming, cancelJob } = useResumableJob(id, 'brd')
 *
 *   return (
 *     <>
 *       <ProgressBanner
 *         jobId={jobId}
 *         status={jobStatus}
 *         stage={jobStage}
 *         progress={jobProgress}
 *         startedAt={jobStartedAt}
 *         startedBy={jobStartedBy}
 *         resuming={isResuming}
 *         onCancel={cancelJob}
 *         currentUserId={me?.id}
 *       />
 *       ...rest of the screen
 *     </>
 *   )
 */
import { useEffect, useState } from 'react'
import { Loader, X } from 'lucide-react'

function formatRelative(iso) {
  if (!iso) return ''
  const t = new Date(iso).getTime()
  if (!t) return ''
  const seconds = Math.max(0, Math.floor((Date.now() - t) / 1000))
  if (seconds < 60) return `${seconds}s ago`
  const m = Math.floor(seconds / 60)
  const s = seconds % 60
  if (m < 60) return s ? `${m}m ${s}s ago` : `${m}m ago`
  const h = Math.floor(m / 60)
  const remM = m % 60
  return remM ? `${h}h ${remM}m ago` : `${h}h ago`
}

export default function ProgressBanner({
  jobId,
  status,
  stage,
  progress,
  startedAt,
  startedBy,
  resuming,
  onCancel,
  currentUserId,
}) {
  // Tick once a second so the relative-time string stays fresh while
  // the user is looking at the banner.
  const [, setTick] = useState(0)
  useEffect(() => {
    if (!jobId) return
    const t = setInterval(() => setTick(x => x + 1), 1000)
    return () => clearInterval(t)
  }, [jobId])

  // Show ONLY for a genuinely in-flight job. A null/absent status means the
  // job was completed and dropped from JobsContext (the originating client
  // clears it the moment its WS stream ends) — stay hidden in that case rather
  // than spinning "Working…" next to an already-rendered result + Proceed button.
  if (!jobId || (status !== 'running' && status !== 'pending')) {
    return null
  }

  const isOwnJob = !startedBy || startedBy === currentUserId
  const relStarted = formatRelative(startedAt)
  const showProgress = typeof progress === 'number' && progress >= 0 && progress <= 100

  const bannerText = resuming
    ? 'Catching up — replaying recent progress…'
    : (stage || 'Working…')

  return (
    <div
      role="status"
      aria-live="polite"
      style={{
        display: 'flex', alignItems: 'center', gap: '12px',
        padding: '10px 14px',
        margin: '0 0 12px',
        background: 'rgba(218,119,86,0.08)',
        border: '1px solid rgba(218,119,86,0.2)',
        borderRadius: '6px',
        fontSize: '13px',
      }}
    >
      <Loader
        size={14}
        style={{ color: 'var(--accent)', animation: 'spin 1s linear infinite', flexShrink: 0 }}
      />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ color: 'var(--accent)', fontWeight: 500 }}>{bannerText}</div>
        <div style={{ color: 'var(--text-muted)', fontSize: '11px', marginTop: '2px' }}>
          {relStarted && <span>started {relStarted}</span>}
          {!isOwnJob && (
            <span> · started by another user</span>
          )}
          {jobId && (
            <span> · job <code style={{ fontSize: '10px', opacity: 0.7 }}>{jobId.slice(0, 8)}</code></span>
          )}
        </div>
        {showProgress && (
          <div
            style={{
              marginTop: '6px', height: '4px', background: 'rgba(0,0,0,0.06)',
              borderRadius: '2px', overflow: 'hidden',
            }}
          >
            <div
              style={{
                width: `${progress}%`,
                height: '100%',
                background: 'var(--accent)',
                transition: 'width 400ms ease',
              }}
            />
          </div>
        )}
      </div>
      {onCancel && (
        <button
          onClick={onCancel}
          title="Cancel this job"
          style={{
            background: 'transparent', border: 'none', cursor: 'pointer',
            color: 'var(--text-muted)', display: 'flex', alignItems: 'center',
            gap: '4px', fontSize: '12px',
          }}
        >
          <X size={12} /> Cancel
        </button>
      )}
    </div>
  )
}
