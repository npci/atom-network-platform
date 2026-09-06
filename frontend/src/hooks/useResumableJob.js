// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

/**
 * useResumableJob — wraps useAgentWS with resume-on-mount semantics.
 *
 * Drop-in replacement for useAgentWS for screens that should show a
 * progress banner when the user navigates back mid-stream. Returns
 * everything useAgentWS does PLUS:
 *
 *   jobId          — the active job_id (or null)
 *   jobStatus      — 'running' | 'succeeded' | 'failed' | 'cancelled' | null
 *   jobStage       — current_stage banner string ("Writing section content…")
 *   jobProgress    — 0..100 or null
 *   jobStartedAt   — ISO timestamp (for "started 2 min 14 s ago" copy)
 *   jobStartedBy   — user_id (visibility-Y attribution; resolved to a label
 *                    in the banner component, not here)
 *   isResuming     — true while the page is replaying chunks AFTER a remount
 *                    (false during a fresh first-mount stream)
 *   replayedChunks — count of chunks delivered via replay (debug / display)
 *   cancelJob      — async () => void   // wraps JobsContext.cancelJob
 *
 * R-3 will wire the WS handlers to:
 *   1. send `{type: "active_jobs", jobs: [...]}` right after auth,
 *   2. accept `{type: "replay_request", job_id, since_seq}`,
 *   3. respond with `{type: "replay", job_id, chunks: [{seq, text}, ...]}`,
 *   4. emit `{type: "progress", job_id, progress_pct, current_stage}`.
 *
 * Until R-3 lands, this hook degrades gracefully: it just forwards
 * useAgentWS unchanged. The fields above will be null / 0 / false.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useAgentWS } from './useAgentWS'
import { useJobs } from '../context/useJobs'
import api from '../services/api'

// How often to poll /jobs/{id}/chunks while a job is running. The backend
// streams to the *originating* WebSocket only; a second client (this page
// after a remount/refresh) needs to pull from the durable chunk buffer to
// see the in-flight output. 2s is responsive without being chatty.
const CHUNK_POLL_INTERVAL_MS = 2000

export function useResumableJob(changeId, module) {
  // Liveness for useAgentWS's stale-stream watchdog, kept in sync from the job
  // record below. A ref, not state: the watchdog reads it inside an interval
  // callback, and re-running that effect on every status poll would reset the
  // timer.
  const serverBusyRef = useRef(false)
  const ws = useAgentWS(changeId, module, serverBusyRef)
  const { activeJobs, activeJobsForChange, recordJob, updateJob, completeJob, cancelJob, jobsLoaded } = useJobs()

  // Frontend convention: WS URL paths are kebab-case (`tech-spec`, `product-kit`),
  // but the backend `agent_jobs.module` column stores snake_case (`tech_spec`,
  // `product_kit`). Normalise here so callers can pass the same module string
  // they pass to useAgentWS without keeping a separate map.
  const jobModule = (module || '').replace(/-/g, '_')

  // Look up any active job for this (changeId, jobModule). At most one per pair
  // for the simple screens; Product Kit "all" mode handles its own per-tab
  // job lookup at the screen level.
  const matchingJob = activeJobsForChange(changeId, jobModule)[0] || null
  // Prefer the job id announced at generation START (ws.liveJobId) so the running job is known
  // immediately; fall back to the docgen-pipeline id (set on done) and JobsContext.
  const jobId = ws.liveJobId || ws.docgenJobId || matchingJob?.id || null

  // Internal: track the seq we've delivered to the renderer. Used to keep
  // append-vs-replace decisions correct on reconnect.
  const lastSeenSeqRef = useRef(0)
  const [isResuming, setIsResuming] = useState(false)
  const [replayedChunks, setReplayedChunks] = useState(0)

  // ── REST-replay rendering state ────────────────────────────────────────
  // The originating WS streams chunks to itself; a remounted client sees
  // none of those over its own WS. We pull from the durable chunk buffer
  // (job_registry) instead. `replayedText` accumulates the chunks as the
  // user-visible streaming text; `replayActive` mirrors WS-side `streaming`
  // for callers that just want a single "in-progress" boolean.
  const [replayedText, setReplayedText] = useState('')
  const [replayActive, setReplayActive] = useState(false)
  const replayedTextRef = useRef('')   // ref mirror for the polling closure
  const replayStartedJobIdRef = useRef(null)   // last job we started replaying

  // ── On mount + while a job is running: fetch the chunk buffer, render it
  //    as streaming text, and poll for new chunks until the job hits a
  //    terminal state. Replaces the older "fetch once on mount" behaviour
  //    so partial content updates live (every CHUNK_POLL_INTERVAL_MS).
  useEffect(() => {
    // No active job → ensure the replay-streaming flag is down. This matters
    // when the job was just completed (the originating client clears it on
    // WS-stream-end): the running tick's interval is torn down by cleanup, but
    // setReplayActive(false) only fires *inside* tick() on a terminal status —
    // which never runs once the job is gone. Without this reset, replayActive
    // stays true and `visibleStreaming` keeps the "Generating…" pill spinning
    // with no Proceed button.
    if (!matchingJob?.id) { setReplayActive(false); return }
    let cancelled = false
    let pollHandle = null

    // First time we see this job_id this mount: clear any stale buffer.
    if (replayStartedJobIdRef.current !== matchingJob.id) {
      replayStartedJobIdRef.current = matchingJob.id
      replayedTextRef.current = ''
      setReplayedText('')
      lastSeenSeqRef.current = 0
    }

    const fetchNewChunks = async () => {
      // Always cursor off our OWN ref, never the JobsContext record. That record's
      // last_seen_seq survives the unmount, but `replayedText` does not — falling
      // back to it on a remount asked the server for chunks we'd never rendered,
      // leaving the resumed page with a transcript missing everything up to that seq.
      const since = lastSeenSeqRef.current
      const r = await api.get(`/jobs/${matchingJob.id}/chunks`, {
        params: { since_seq: since },
      })
      if (cancelled) return
      const chunks = r.data?.chunks || []
      if (chunks.length === 0) return
      const newText = chunks.map(c => c.text || '').join('')
      replayedTextRef.current = replayedTextRef.current + newText
      setReplayedText(replayedTextRef.current)
      const maxSeq = chunks[chunks.length - 1].seq
      lastSeenSeqRef.current = Math.max(lastSeenSeqRef.current, maxSeq)
      setReplayedChunks(c => c + chunks.length)
      updateJob(matchingJob.id, { last_seen_seq: maxSeq })
    }

    const refreshStatus = async () => {
      const jobR = await api.get(`/jobs/${matchingJob.id}`)
      if (cancelled) return null
      const j = jobR.data
      if (j?.status === 'succeeded' || j?.status === 'failed' || j?.status === 'cancelled') {
        return j.status
      }
      updateJob(matchingJob.id, {
        status:        j?.status,
        progress_pct:  j?.progress_pct,
        current_stage: j?.current_stage,
      })
      return null
    }

    const tick = async () => {
      if (cancelled) return
      try {
        await fetchNewChunks()
        const terminal = await refreshStatus()
        if (terminal) {
          // One final chunk-drain so we don't miss anything written between
          // the last fetch and the terminal flip.
          try { await fetchNewChunks() } catch { /* ignore */ }
          if (pollHandle) { clearInterval(pollHandle); pollHandle = null }
          setReplayActive(false)
          // For 'succeeded' we keep replayedText so the page renders the
          // final document; the JobsContext drop happens here so the banner
          // disappears.
          completeJob(matchingJob.id)
        }
      } catch (err) {
        if (err?.response?.status === 404) {
          if (pollHandle) { clearInterval(pollHandle); pollHandle = null }
          setReplayActive(false)
          completeJob(matchingJob.id)
        }
        // Other errors → keep polling; transient network blips shouldn't
        // tear down the resume view.
        // eslint-disable-next-line no-console
        console.debug('useResumableJob: poll tick failed', err?.response?.status || err?.message)
      }
    }

    // Initial pull (drives the resume banner) + start polling for live updates.
    setIsResuming(true)
    setReplayActive(true)
    tick().finally(() => {
      if (!cancelled) setIsResuming(false)
    })
    pollHandle = setInterval(tick, CHUNK_POLL_INTERVAL_MS)

    return () => {
      cancelled = true
      if (pollHandle) clearInterval(pollHandle)
    }
    // Only run when matchingJob.id changes (i.e., a job appeared / vanished
    // for this changeId+module). Other deps come from refs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [matchingJob?.id])

  // ── When WS announces a job_id (R-3 onwards), record it so other tabs
  //    and the sidebar tray see it.
  useEffect(() => {
    const announced = ws.liveJobId || ws.docgenJobId
    if (!announced) return
    recordJob({
      id:                 announced,
      change_request_id:  changeId,
      module:             jobModule,
      status:             'running',
      started_at:         new Date().toISOString(),
      updated_at:         new Date().toISOString(),
      last_seen_seq:      0,
    })
    // Don't include recordJob/changeId/module in deps — recordJob is stable from JobsContext,
    // changeId/module are stable for the lifetime of a screen mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ws.liveJobId, ws.docgenJobId])

  // ── When the WS stream THIS client owns finishes, optimistically drop the
  //    job from JobsContext so the resume banner clears in lockstep with the
  //    inline "Generating…" indicator. Without this the banner kept spinning
  //    "Working…" after the stream ended and the Proceed button appeared —
  //    until the next chunk-poll tick (up to CHUNK_POLL_INTERVAL_MS), or
  //    indefinitely once the poller removed the record and jobStatus went
  //    null. That simultaneous "done + still working" state reads as "is the
  //    generation finished or not?".
  //
  //    Gated on ws.liveJobId: only the originating client (whose own WS
  //    announced the job start) completes optimistically. A remounted observer
  //    has no liveJobId and must keep polling for the server's authoritative
  //    terminal status. The periodic refreshFromServer reconciles either way.
  //    Fires only on the true→false streaming edge so we never complete on the
  //    initial render (where streaming is also false).
  const wasStreamingRef = useRef(false)
  useEffect(() => {
    if (ws.streaming) {
      wasStreamingRef.current = true
      return
    }
    if (wasStreamingRef.current) {
      wasStreamingRef.current = false
      if (ws.liveJobId) completeJob(ws.liveJobId)
    }
  }, [ws.streaming, ws.liveJobId, completeJob])

  // ── Cancel handler — wraps the context's cancelJob
  const handleCancel = useCallback(async () => {
    if (!jobId) return
    await cancelJob(jobId)
  }, [jobId, cancelJob])

  // Job record (latest known) used to drive the banner UI.
  const job = jobId ? (activeJobs[jobId] || matchingJob) : null

  // Tell the WS watchdog the backend is still working. Goes false as soon as the
  // job reaches a terminal state (completeJob drops the record), so a genuinely
  // dead stream is still recovered. No job record — legacy handlers that never
  // announce a job_id — leaves this false and the watchdog behaves as before.
  useEffect(() => {
    serverBusyRef.current = (job?.status === 'running')
  }, [job?.status])

  // ── Compose visible streaming state ─────────────────────────────────────
  // Prefer the WS's own stream when it's actively delivering chunks (live
  // and lowest-latency). Otherwise, surface the REST-polled replay so a
  // remounted client still sees the in-flight output. When the polled job
  // finishes, replayedText carries the final document until the next WS
  // history reload brings it in as a proper message.
  const visibleStreaming    = ws.streaming || replayActive
  const visibleStreamingText = ws.streaming ? ws.streamingText : (replayedText || '')

  // If the polled replay finished but the WS history hasn't picked up the
  // saved assistant message yet (separate-tab case), inject it so consumer
  // pages render the completed content without forcing a page reload.
  const composedMessages = useMemo(() => {
    if (!replayedText) return ws.messages
    if (replayActive)  return ws.messages
    const hasAssistant = (ws.messages || []).some(m => m.role === 'assistant')
    if (hasAssistant)  return ws.messages
    return [...(ws.messages || []), { role: 'assistant', content: replayedText }]
  }, [ws.messages, replayedText, replayActive])

  return {
    ...ws,
    messages:      composedMessages,
    streaming:     visibleStreaming,
    streamingText: visibleStreamingText,
    jobId,
    jobStatus:    job?.status     || null,
    jobStage:     job?.current_stage || null,
    jobProgress:  (typeof job?.progress_pct === 'number') ? job.progress_pct : null,
    jobStartedAt: job?.started_at || null,
    jobStartedBy: job?.started_by_user_id || null,
    isResuming,
    replayedChunks,
    jobsLoaded,
    cancelJob: handleCancel,
  }
}
