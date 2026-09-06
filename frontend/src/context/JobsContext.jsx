// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

/**
 * JobsContext — global registry of in-flight long-running async jobs.
 *
 * Foundation for the resume-progress feature (R-2 in the plan).
 *
 * Lifecycle of a job from the frontend's perspective:
 *
 *   user starts a generation (BRD/TSD/Canvas/Research/etc.)
 *      │
 *      ▼
 *   server creates a row in `agent_jobs` and emits `job_id` on the WS.
 *      │
 *      ▼
 *   useResumableJob picks the `job_id` off the WS and calls
 *   JobsContext.recordJob(...) so the rest of the app (sidebar tray,
 *   other tabs after a localStorage rehydrate) knows the job exists.
 *      │
 *      ▼
 *   user navigates away; the WS closes; job_id stays in JobsContext.
 *      │
 *      ▼
 *   user comes back to the same screen — useResumableJob mounts again,
 *   reads JobsContext for any active job for this (changeId, module),
 *   and asks the WS to replay since seq=lastSeenSeq.
 *      │
 *      ▼
 *   server keeps appending chunks to Redis as the handler progresses.
 *   Reconnected client receives the buffered tail + live updates.
 *      │
 *      ▼
 *   on completion / failure / cancellation, the WS sends `done` /
 *   `error`, useResumableJob calls JobsContext.completeJob(...) and
 *   the entry leaves the active set.
 *
 * Persistence: the active-jobs map is mirrored to localStorage so that
 * a full page reload (or browser restart within ~30 min) recovers the
 * known set of jobs and reconciles against the server via
 * GET /api/jobs/active.
 *
 * Public surface used by R-3+ screen wiring:
 *   useJobs() →
 *     activeJobs:        Record<job_id, JobRecord>     // current view
 *     activeJobsForChange(changeId, module?):JobRecord[]
 *     recordJob(job):    void   // called by useResumableJob on first chunk
 *     updateJob(id, fields): void
 *     completeJob(id):   void
 *     cancelJob(id):     Promise<void>   // POSTs /api/jobs/:id/cancel
 *     refreshFromServer():Promise<void>  // reconcile on demand
 *
 * Where each piece lives in the system:
 *   - active-jobs map: in-memory React state + localStorage mirror (this file)
 *   - WS replay protocol: useResumableJob + the WS handlers (R-3)
 *   - durable lifecycle: Postgres `agent_jobs` (R-1)
 *   - chunk buffer: Redis `job:chunks:<job_id>` (R-1)
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import api from '../services/api'
import { useAuthStore } from '../store/useStore'

const STORAGE_KEY = 'a2a:active_jobs'
const REFRESH_INTERVAL_MS = 30_000   // re-hit /jobs/active periodically so the tray catches jobs started by other tabs / users
const STALE_AFTER_MS = 30 * 60_000   // a localStorage entry older than 30 min that the server doesn't confirm → drop

import { JobsContext } from './useJobs'

/**
 * @typedef {Object} JobRecord
 * @property {string}  id                    job_id
 * @property {string|null} change_request_id null for admin-only jobs (code indexing)
 * @property {string}  module                'brd' | 'tech_spec' | 'research' | 'canvas' | 'product_kit' | 'code_indexing' | etc.
 * @property {string|null} subtype           e.g. 'circular' for Product Kit, repo_id for code_indexing
 * @property {'pending'|'running'|'succeeded'|'failed'|'cancelled'} status
 * @property {number|null}  progress_pct
 * @property {string|null}  current_stage
 * @property {string}  started_at            ISO timestamp
 * @property {string}  updated_at            ISO timestamp
 * @property {string|null}  started_by_user_id
 * @property {Object}  metadata
 * @property {number}  last_seen_seq         highest chunk seq this client has received (used for replay catchup)
 */

function loadFromStorage() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object') return {}
    // Drop entries older than STALE_AFTER_MS — they're almost certainly orphans
    // from a job that completed / failed while we weren't around to see it.
    const now = Date.now()
    const cleaned = {}
    for (const [id, rec] of Object.entries(parsed)) {
      const t = rec?.started_at ? new Date(rec.started_at).getTime() : 0
      if (t && (now - t) < STALE_AFTER_MS) {
        cleaned[id] = rec
      }
    }
    return cleaned
  } catch {
    return {}
  }
}

function saveToStorage(activeJobs) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(activeJobs))
  } catch {
    // localStorage can be full or disabled in incognito — swallow.
  }
}

export function JobsProvider({ children }) {
  // Map job_id → JobRecord. Keyed by id rather than (changeId, module) so a
  // single change request can have parallel jobs (Product Kit "all" mode
  // produces 10 sibling jobs at once).
  const [activeJobs, setActiveJobs] = useState(() => loadFromStorage())
  const [jobsLoaded, setJobsLoaded] = useState(false)
  const refreshTimerRef = useRef(null)
  const lastRefreshAtRef = useRef(0)

  // Mirror to localStorage on every change.
  useEffect(() => {
    saveToStorage(activeJobs)
  }, [activeJobs])

  // ── Reconcile against the server ──────────────────────────────────────────
  // Fetches every active job the current user can see and merges with our
  // local map. Local entries that the server doesn't confirm get dropped
  // (the job completed / failed while this tab was offline).
  const refreshFromServer = useCallback(async () => {
    // Logged-in check. The session is an httpOnly cookie that JavaScript
    // cannot read, so probe the persisted user marker instead — a
    // localStorage token lookup here would be permanently false and would
    // silently stop this reconciler from ever running.
    if (!useAuthStore.getState().user) return     // not logged in
    try {
      const r = await api.get('/jobs/active')
      const serverJobs = r.data?.jobs || []
      lastRefreshAtRef.current = Date.now()

      setActiveJobs(prev => {
        const next = {}
        // Take everything the server still considers active.
        for (const j of serverJobs) {
          // Preserve last_seen_seq from local if we have it (server doesn't track that).
          const seq = prev[j.id]?.last_seen_seq ?? 0
          next[j.id] = { ...j, last_seen_seq: seq }
        }
        // Don't bring forward stale local-only entries — if the server doesn't
        // know about them, they're either completed (and we missed the 'done')
        // or were never properly created. Either way: drop them.
        return next
      })
    } catch (err) {
      // 401 → token expired. The api.js interceptor will redirect to /login,
      // we don't need to do anything here.
      // Other errors → silent; the tray shows what we last knew.
      // eslint-disable-next-line no-console
      console.debug('JobsContext.refreshFromServer failed:', err?.response?.status || err?.message)
    } finally {
      // Flip jobsLoaded true on first completion (success OR failure). After
      // this point, consumers can trust activeJobsForChange() to reflect the
      // server's view — instead of an empty map that just hasn't been
      // hydrated yet. Required to prevent auto-start races on remount.
      setJobsLoaded(true)
    }
  }, [])

  // Periodic reconcile so the tray catches jobs started by another tab or by
  // another user (under the visibility-Y rule). Interval is generous because
  // every screen that mounts also triggers a targeted refresh via R-3 hooks.
  useEffect(() => {
    refreshFromServer()   // initial
    refreshTimerRef.current = setInterval(refreshFromServer, REFRESH_INTERVAL_MS)
    return () => clearInterval(refreshTimerRef.current)
  }, [refreshFromServer])

  // ── Imperative API used by useResumableJob ────────────────────────────────

  const recordJob = useCallback((job) => {
    if (!job?.id) return
    setActiveJobs(prev => ({
      ...prev,
      [job.id]: {
        ...prev[job.id],
        ...job,
        last_seen_seq: prev[job.id]?.last_seen_seq ?? job.last_seen_seq ?? 0,
      },
    }))
  }, [])

  const updateJob = useCallback((id, fields) => {
    if (!id) return
    setActiveJobs(prev => {
      if (!prev[id]) return prev
      return { ...prev, [id]: { ...prev[id], ...fields } }
    })
  }, [])

  const completeJob = useCallback((id) => {
    if (!id) return
    setActiveJobs(prev => {
      if (!prev[id]) return prev
      const { [id]: _gone, ...rest } = prev
      return rest
    })
  }, [])

  const cancelJob = useCallback(async (id) => {
    if (!id) return
    try {
      await api.post(`/jobs/${id}/cancel`)
      // Optimistic — mark cancelled locally; the next refreshFromServer
      // will drop it because it's no longer active.
      setActiveJobs(prev => prev[id]
        ? { ...prev, [id]: { ...prev[id], status: 'cancelled' } }
        : prev)
    } catch (err) {
      // eslint-disable-next-line no-console
      console.error('JobsContext.cancelJob failed:', err?.response?.data || err)
      throw err
    }
  }, [])

  // ── Selector helper: jobs scoped to a specific change request + module ────
  const activeJobsForChange = useCallback((changeId, module = null) => {
    if (!changeId) return []
    return Object.values(activeJobs).filter(j =>
      j.change_request_id === changeId &&
      j.status !== 'succeeded' && j.status !== 'failed' && j.status !== 'cancelled' &&
      (module ? j.module === module : true)
    )
  }, [activeJobs])

  return (
    <JobsContext.Provider value={{
      activeJobs,
      activeJobsForChange,
      recordJob,
      updateJob,
      completeJob,
      cancelJob,
      refreshFromServer,
      jobsLoaded,
    }}>
      {children}
    </JobsContext.Provider>
  )
}

