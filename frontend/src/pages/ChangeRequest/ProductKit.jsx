// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useState, useRef, useEffect, useCallback } from 'react'
import { hardenFrameHtml } from '../../utils/safeHtmlFrame'
import { t } from '../../strings'
import { useParams, useNavigate, useSearchParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { agentsApi, certSyncApi } from '../../services/api'
import { wsUrl } from '../../utils/basePath'
import {
  FileText, Film, Video, HelpCircle, CheckSquare, Bell, Globe, Code, Monitor,
  ChevronRight, Play, RefreshCw, Download, CheckCircle2, Circle, Loader, ArrowLeft, Zap,
  Send, Upload, Maximize2, X,
} from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import ProductKitGenerateAll from './ProductKitGenerateAll'
import { DocxDownloadButton, PptxDownloadButton } from './BRD'
import DocumentSourceToggle from '../../components/DocumentSourceToggle'
import ShowArtifactsButton from '../../components/ShowArtifactsButton'
import TranscriptsDownloadButton from '../../components/TranscriptsDownloadButton'
import StalenessBadge from '../../components/StalenessBadge'
// R-5 — JobsContext for durable-job awareness; ProgressBanner for the resume UI.
import { useJobs } from '../../context/useJobs'
import ProgressBanner from '../../components/jobs/ProgressBanner'
import { useAuth } from '../../hooks/useAuth'
import SyncDiffModal from '../../components/cert/SyncDiffModal'
import SkipStepButton from '../../components/common/SkipStepButton'

// ── Doc type definitions ──────────────────────────────────────────────────────

const DOC_TYPES = [
  {
    key:   'product_deck',
    label: 'Product Deck',
    desc:  '12-slide executive presentation outline',
    icon:  Globe,
  },
  {
    key:   'promo_video',
    label: 'Promo Video Script',
    desc:  '60–90 sec marketing video script for end customers',
    icon:  Film,
  },
  {
    key:   'explainer_video',
    label: 'Explainer Video Script',
    desc:  '2–3 min step-by-step tutorial for PSP developer teams',
    icon:  Video,
  },
  {
    key:   'faq',
    label: 'FAQ Document',
    desc:  'Q&A for customers, PSPs, and banks',
    icon:  HelpCircle,
  },
  {
    key:   'cert_test_cases',
    label: 'Certification Test Cases',
    desc:  '25–35 partner certification test cases',
    icon:  CheckSquare,
  },
  {
    key:   'circular',
    label: t('artifact.circular'),
    desc:  'Official technical circular to ecosystem participants',
    icon:  Bell,
  },
  {
    key:   'manifest',
    label: 'Manifest File',
    desc:  'Machine-readable YAML manifest for A2A partner agents',
    icon:  Code,
  },
  {
    key:   'prototype_screens',
    label: 'Prototype Screens',
    desc:  'HTML wireframe mockups for PSP / TPAP UI teams',
    icon:  Monitor,
  },
  {
    // WHY expose Product Note here:
    // the backend already routes this doc type through the restored docgen
    // pipeline, but after the merge the UI no longer gave users a way to
    // generate or review it. Adding it back here restores the feature while
    // keeping main's newer resumable-job UX untouched.
    key:   'product_note',
    label: 'Product Document',
    desc:  'Comprehensive product document for stakeholders & partners',
    icon:  FileText,
  },
]

// Backend-emitted stage names → user-friendly labels. Covers both the legacy
// markdown stub stages and the Excel Testcase Engine LangGraph nodes.
// Anything not mapped renders the raw stage name (so future stages aren't
// silently swallowed). Comparison is lowercase.
const STAGE_LABELS = {
  // Engine — LangGraph node names
  'enhance':       'Analyzing brief',
  'enhancing':     'Analyzing brief',
  'await_input':   'Waiting for input',
  'awaiting_input':'Waiting for input',
  'needs_input':   'Waiting for input',
  'plan':          'Planning workbook',
  'planner':       'Planning workbook',
  'planning':      'Planning workbook',
  'write':         'Writing test cases',
  'writing':       'Writing test cases',
  'render':        'Rendering Excel',
  'rendering':     'Rendering Excel',
  'validate':      'Validating workbook',
  'validating':    'Validating workbook',
  'repair':        'Repairing defects',
  'repairing':     'Repairing defects',
  'revalidate':    'Re-validating',
  'attach_report': 'Attaching report',
  'deliver':       'Finalizing',
  'pending':       'Queued',
  'queued':        'Queued',
  'completed':     'Completed',
  'completed_with_warnings': 'Completed with warnings',
  'failed':        'Failed',
}

function friendlyStage(raw) {
  if (!raw) return ''
  const key = String(raw).toLowerCase().trim()
  return STAGE_LABELS[key] || raw
}

// ── WebSocket hook (product kit variant) ─────────────────────────────────────

function useProductKitWS(changeId, docType) {
  const [chunks, setChunks]       = useState('')
  const [streaming, setStreaming] = useState(false)
  const [done, setDone]           = useState(false)
  const [history, setHistory]     = useState([])
  const [error, setError]         = useState(null)
  // R-5 — durable-job awareness, mirroring useResumableJob's surface so the
  // tray + banner work on Product Kit screens too.
  const [jobId, setJobId]         = useState(null)
  const [jobStage, setJobStage]   = useState(null)
  // Live progress from WS `progress` events. JobsContext polls every ~5s, so
  // a local mirror lets the loader's progress bar move smoothly between polls.
  const [livePct, setLivePct]     = useState(null)
  const [isResuming, setIsResuming] = useState(false)
  const wsRef  = useRef(null)
  const active = useRef(false)
  const jobIdRef = useRef(null)
  const runActiveRef = useRef(false)
  const closingRef = useRef(false)

  const { recordJob, updateJob, completeJob, activeJobsForChange } = useJobs()

  const clearLocalJob = useCallback((id = null) => {
    const finalJobId = id || jobIdRef.current
    if (finalJobId) completeJob(finalJobId)
    jobIdRef.current = null
    setJobId(null)
    setJobStage(null)
  }, [completeJob])

  const markTerminalError = useCallback((detail, id = null) => {
    setError(detail || 'Generation failed')
    setStreaming(false)
    setLivePct(null)
    setDone(false)
    setIsResuming(false)
    runActiveRef.current = false
    clearLocalJob(id)
  }, [clearLocalJob])

  const reset = useCallback(() => {
    setChunks('')
    setStreaming(false)
    setDone(false)
    setError(null)
    setLivePct(null)
    runActiveRef.current = false
  }, [])

  const send = useCallback((message) => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
      markTerminalError('WebSocket is not connected. Please try again.')
      return false
    }
    runActiveRef.current = true
    wsRef.current.send(JSON.stringify({ message }))
    return true
  }, [markTerminalError])

  const connect = useCallback(() => {
    if (active.current) return
    active.current = true
    closingRef.current = false

    const ws = new WebSocket(wsUrl(`api/ws/changes/${changeId}/product-kit/${docType}`))
    wsRef.current = ws

    ws.onopen = () => {
      // Auth rides the handshake: the session cookie is httpOnly and the
      // browser attaches it to the WebSocket upgrade automatically, so no
      // token is sent (or readable) here. The hello frame is still sent —
      // the server reads one frame before streaming.
      ws.send(JSON.stringify({}))
    }

    ws.onmessage = (evt) => {
      const data = JSON.parse(evt.data)
      if (data.type === 'history') {
        setHistory(data.messages || [])
      } else if (data.type === 'active_jobs') {
        // R-5 — a job for this (changeId, product_kit, doc_type) was already
        // running when we connected. Pick the first match and ask for replay.
        const jobs = (data.jobs || []).filter(j => j.subtype === docType)
        if (jobs.length > 0) {
          const j = jobs[0]
          jobIdRef.current = j.id
          runActiveRef.current = true
          setJobId(j.id)
          setJobStage(j.current_stage || null)
          setIsResuming(true)
          // WHY also flip streaming=true: the DocPanel's loader is gated by
          // (streaming || generating). Without this flip, refreshing or
          // tab-switching back into a running job shows the stale saved
          // content with no indication that work is in progress — the only
          // hint was the small ProgressBanner. Setting streaming here keeps
          // the prominent loader visible until the engine streams its final
          // chunk (which clears stale content via displayContent logic).
          setStreaming(true)
          recordJob({ ...j, last_seen_seq: 0 })
          if (ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'replay_request', job_id: j.id, since_seq: 0 }))
          }
        }
      } else if (data.type === 'job_id') {
        // Backend just created a fresh job for the message we sent.
        jobIdRef.current = data.job_id
        runActiveRef.current = true
        setJobId(data.job_id)
        recordJob({
          id: data.job_id, change_request_id: changeId,
          module: 'product_kit', subtype: docType,
          status: 'running',
          started_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
          last_seen_seq: 0,
        })
      } else if (data.type === 'progress') {
        setJobStage(data.current_stage || null)
        // Engine reports per-batch / per-stage progress_pct. Mirror it
        // locally so the loader's bar moves between JobsContext polls.
        if (typeof data.progress_pct === 'number') setLivePct(data.progress_pct)
        if (data.job_id) {
          const patch = { current_stage: data.current_stage }
          if (typeof data.progress_pct === 'number') patch.progress_pct = data.progress_pct
          updateJob(data.job_id, patch)
        }
        // WHY treat terminal stages as end-of-stream: the engine emits a
        // progress event with stage=completed / completed_with_warnings /
        // failed BEFORE the separate `done` event lands (the latter is sent
        // by the WS handler only after complete_job + final markdown chunk
        // flush). Without this, the loader keeps spinning even though the
        // workbook is done — visible to the user as "Workbook completed
        // with validation report" + still-rotating spinner.
        const terminal = data.stage === 'completed' ||
                         data.stage === 'completed_with_warnings' ||
                         data.stage === 'failed'
        if (terminal) {
          setStreaming(false)
          setLivePct(100)
          setDone(data.stage !== 'failed')
          runActiveRef.current = false
          clearLocalJob(data.job_id)
          // Clear local jobId so the ProgressBanner gates off — without
          // this it shows status='running' (via the (jobId ? 'running' :
          // null) fallback), keeping the cancel banner alive after the
          // engine finished.
        }
      } else if (data.type === 'replay') {
        // Server is replaying buffered chunks for a reconnect. Append in order.
        for (const c of (data.chunks || [])) {
          setChunks(prev => prev + c.text)
        }
        setIsResuming(false)
      } else if (data.type === 'chunk') {
        setChunks(prev => prev + data.text)
        runActiveRef.current = true
        setStreaming(true)
      } else if (data.type === 'done') {
        setStreaming(false)
        setDone(true)
        setLivePct(100)
        runActiveRef.current = false
        clearLocalJob(data.job_id)
      } else if (data.type === 'error') {
        markTerminalError(data.detail, data.job_id)
      }
    }

    ws.onerror = () => {
      if (runActiveRef.current || jobIdRef.current) {
        markTerminalError('WebSocket connection error')
      } else {
        setError('WebSocket connection error')
      }
    }
    ws.onclose = () => {
      active.current = false
      if (!closingRef.current && (runActiveRef.current || jobIdRef.current)) {
        markTerminalError('Generation connection closed before completion')
      }
    }
  // recordJob/updateJob/completeJob are stable from JobsContext.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [changeId, docType, clearLocalJob, markTerminalError])

  // Auto-connect when hook mounts
  useEffect(() => {
    connect()
    return () => {
      closingRef.current = true
      active.current = false
      wsRef.current?.close()
    }
  }, [connect])

  // Look up the canonical job record (for jobStartedAt etc) from JobsContext.
  const matchingJob = activeJobsForChange(changeId, 'product_kit')
                        .find(j => j.subtype === docType) || null

  // Prefer the live WS-reported pct over the polled JobsContext value.
  // The poll lags ~5s; the WS event arrives instantly. Fall back to the
  // poll value when no WS event has arrived yet (e.g. mid-resume).
  const effectivePct = (
    typeof livePct === 'number' ? livePct
    : typeof matchingJob?.progress_pct === 'number' ? matchingJob.progress_pct
    : null
  )

  return {
    chunks, streaming, done, history, error, send, reset,
    // R-5 additions:
    jobId, jobStage, isResuming,
    jobStatus:    matchingJob?.status || (jobId ? 'running' : null),
    jobProgress:  effectivePct,
    jobStartedAt: matchingJob?.started_at || null,
    jobStartedBy: matchingJob?.started_by_user_id || null,
  }
}

// ── Video upload (promo_video / explainer_video) ──────────────────────────────
// The script is generated by the agent; the PM produces the actual MP4
// off-platform and uploads it here. It ships to the partner with the kit on
// "Communicate to partner". MP4 only, 25 MB.
function VideoUploadPanel({ changeId, docType, readOnly, hasScript }) {
  const [videoUrl, setVideoUrl] = useState(null)
  const [filename, setFilename] = useState(null)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState(null)
  const fileRef = useRef(null)
  const urlRef = useRef(null)

  // ── AI video generation state ──
  const [opts, setOpts] = useState(null)         // {providers, default_*, ...}
  const [provider, setProvider] = useState('')
  const [model, setModel] = useState('')
  const [gen, setGen] = useState(null)           // {status, stage, pct} while running
  const [genError, setGenError] = useState(null)
  const pollRef = useRef(null)

  const setUrl = (u) => {
    if (urlRef.current) URL.revokeObjectURL(urlRef.current)
    urlRef.current = u
    setVideoUrl(u)
  }

  const loadVideo = useCallback(async () => {
    try {
      const resp = await agentsApi.productKitVideoBlob(changeId, docType)
      setUrl(URL.createObjectURL(resp.data))
    } catch (err) {
      if (err?.response?.status === 404) setUrl(null)
    }
  }, [changeId, docType])

  // Load provider/model/duration options from the backend (settings-driven).
  useEffect(() => {
    let alive = true
    agentsApi.getVideoGenOptions(changeId, docType)
      .then(r => {
        if (!alive) return
        setOpts(r.data)
        setProvider(r.data.default_provider)
        setModel(r.data.default_model)
      })
      .catch(() => {})
    return () => { alive = false }
  }, [changeId, docType])

  const modelsForProvider = (opts?.providers?.[provider]) || []
  useEffect(() => {
    // Keep model valid when provider changes.
    if (modelsForProvider.length && !modelsForProvider.includes(model)) {
      setModel(modelsForProvider[0])
    }
  }, [provider]) // eslint-disable-line react-hooks/exhaustive-deps

  const stopPoll = () => { if (pollRef.current) { clearTimeout(pollRef.current); pollRef.current = null } }

  const pollJob = useCallback((jobId) => {
    const tick = async () => {
      try {
        const { data: job } = await agentsApi.getJob(jobId)
        const status = job.status
        setGen({ status, stage: job.current_stage || '', pct: job.progress_pct || 0 })
        if (status === 'succeeded') { stopPoll(); setGen(null); await loadVideo(); return }
        if (status === 'failed' || status === 'cancelled') {
          stopPoll(); setGen(null)
          setGenError(job.error_message || `Generation ${status}.`); return
        }
        pollRef.current = setTimeout(tick, 4000)
      } catch {
        pollRef.current = setTimeout(tick, 6000)
      }
    }
    tick()
  }, [loadVideo])

  const onGenerate = async () => {
    setGenError(null)
    setGen({ status: 'running', stage: 'Starting…', pct: 0 })
    try {
      const { data } = await agentsApi.generateProductKitVideo(changeId, docType, provider, model)
      pollJob(data.job_id)
    } catch (err) {
      setGen(null)
      setGenError(err?.response?.data?.detail || 'Could not start generation.')
    }
  }

  useEffect(() => { loadVideo() }, [loadVideo])
  useEffect(() => () => { if (urlRef.current) URL.revokeObjectURL(urlRef.current); stopPoll() }, [])

  const onFile = async (e) => {
    const file = e.target.files?.[0]
    if (e.target) e.target.value = ''
    if (!file) return
    if (!file.name.toLowerCase().endsWith('.mp4')) { setError('Only .mp4 files are accepted.'); return }
    if (file.size > 25 * 1024 * 1024) { setError('Video too large (max 25 MB).'); return }
    setError(null); setUploading(true)
    try {
      await agentsApi.uploadProductKitVideo(changeId, docType, file)
      setFilename(file.name)
      await loadVideo()
    } catch (err) {
      setError(err?.response?.data?.detail || 'Upload failed.')
    } finally { setUploading(false) }
  }

  return (
    <div style={{
      margin: '0 20px 16px', padding: '14px 16px',
      border: '1px solid var(--border-subtle)', borderRadius: 8,
      background: 'var(--bg-elevated)',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: videoUrl ? 12 : 8 }}>
        <Video size={15} style={{ color: 'var(--accent)' }} />
        <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-primary)' }}>Video file</span>
        <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
          {videoUrl ? (filename || 'uploaded — ships to partner with the kit') : 'MP4, max 25 MB — produce it from the script above'}
        </span>
      </div>
      {videoUrl && (
        <video
          src={videoUrl}
          controls
          style={{ width: '100%', maxHeight: 320, borderRadius: 6, background: '#000', display: 'block', marginBottom: 10 }}
        />
      )}

      {/* ── AI generation (provider/model + Generate) ── */}
      {!readOnly && opts?.enabled && (
        <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 8, marginBottom: 10 }}>
          <select
            value={provider}
            onChange={e => setProvider(e.target.value)}
            disabled={!!gen}
            style={{ padding: '6px 8px', borderRadius: 6, fontSize: 12, background: 'var(--bg-card)', color: 'var(--text-primary)', border: '1px solid var(--border)' }}
          >
            {Object.keys(opts.providers).map(p => <option key={p} value={p}>{p}</option>)}
          </select>
          <select
            value={model}
            onChange={e => setModel(e.target.value)}
            disabled={!!gen}
            style={{ padding: '6px 8px', borderRadius: 6, fontSize: 12, background: 'var(--bg-card)', color: 'var(--text-primary)', border: '1px solid var(--border)' }}
          >
            {modelsForProvider.map(m => <option key={m} value={m}>{m}</option>)}
          </select>
          <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
            {opts.default_duration_sec}s · {Math.ceil(opts.default_duration_sec / opts.segment_max_sec)} clips · {opts.aspect_ratio}
          </span>
          <button
            onClick={onGenerate}
            disabled={!!gen || !hasScript}
            title={hasScript ? 'Generate the video from the segmented script' : 'Generate the script first'}
            style={{
              display: 'inline-flex', alignItems: 'center', gap: 6,
              padding: '7px 14px', borderRadius: 7, fontSize: 12, fontWeight: 600,
              background: gen || !hasScript ? 'var(--bg-card)' : 'var(--accent)',
              color: gen || !hasScript ? 'var(--text-muted)' : '#fff',
              border: 'none', cursor: gen ? 'wait' : (!hasScript ? 'not-allowed' : 'pointer'),
            }}
          >
            {gen ? <Loader size={13} className="spin" /> : <Play size={13} />}
            {gen ? 'Generating…' : videoUrl ? 'Regenerate video' : 'Generate video'}
          </button>
        </div>
      )}
      {gen && (
        <div style={{ marginBottom: 10, fontSize: 12, color: 'var(--text-secondary)' }}>
          {gen.stage || 'Working…'}{gen.pct ? ` — ${gen.pct}%` : ''}
          <div style={{ marginTop: 4, height: 4, borderRadius: 2, background: 'var(--bg-card)' }}>
            <div style={{ width: `${gen.pct || 4}%`, height: '100%', borderRadius: 2, background: 'var(--accent)', transition: 'width .4s' }} />
          </div>
        </div>
      )}
      {genError && (
        <div style={{ marginBottom: 10, fontSize: 12, color: 'var(--danger)' }}>{genError}</div>
      )}

      {/* ── Manual upload (fallback) ── */}
      {!readOnly && (
        <>
          <input ref={fileRef} type="file" accept="video/mp4,.mp4" onChange={onFile} style={{ display: 'none' }} />
          <button
            onClick={() => fileRef.current?.click()}
            disabled={uploading || !!gen}
            style={{
              display: 'inline-flex', alignItems: 'center', gap: 6,
              padding: '7px 14px', borderRadius: 7, fontSize: 12, fontWeight: 600,
              background: 'transparent',
              color: 'var(--text-secondary)',
              border: '1px solid var(--border)', cursor: uploading ? 'wait' : 'pointer',
            }}
          >
            <Upload size={13} /> {uploading ? 'Uploading…' : videoUrl ? 'Replace with upload' : 'Upload MP4 instead'}
          </button>
        </>
      )}
      {error && (
        <div style={{ marginTop: 8, fontSize: 12, color: 'var(--danger)' }}>{error}</div>
      )}
    </div>
  )
}

// ── Doc panel (right side) ────────────────────────────────────────────────────

function DocPanel({ changeId, docDef, onGenerated, autogen = false }) {
  const {
    chunks, streaming, done, _history, error, send, reset,
    // R-5 — durable-job fields surfaced by the upgraded hook.
    jobId, jobStatus, jobStage, jobProgress, jobStartedAt, jobStartedBy, isResuming,
  } = useProductKitWS(changeId, docDef.key)
  const { user: me } = useAuth()
  const readOnly = ['risk_reviewer', 'infosec_reviewer', 'tech_lead'].includes(me?.role)
  const { cancelJob: cancelJobCtx } = useJobs()
  const handleCancel = useCallback(() => {
    if (jobId) cancelJobCtx(jobId)
  }, [jobId, cancelJobCtx])
  const [feedback, setFeedback] = useState('')
  const [generating, setGenerating] = useState(false)
  // Inline error specifically for the .xlsx download path. Cleared when the
  // user starts a new download or regenerates. Replaces the previous alert().
  const [xlsxError, setXlsxError] = useState(null)
  const [xlsxDownloading, setXlsxDownloading] = useState(false)
  // Upload-override state: user edits the generated xlsx locally, then
  // uploads it here to become the new source of truth for both download AND
  // Product Kit shipping to the partner. Latest-wins via a superseding
  // AgentJob row; see excel_testcase_engine/api.py:upload_cert_testcase_xlsx.
  const uploadFileRef = useRef(null)
  const [xlsxUploadFile, setXlsxUploadFile] = useState(null)
  const [xlsxUploading, setXlsxUploading] = useState(false)
  const [xlsxReverting, setXlsxReverting] = useState(false)
  const [xlsxStatus, setXlsxStatus] = useState(null)
  // Guaranteed-changing key part for the certPlan query. Bumped on every
  // Apply / Revert so the queryKey becomes a fresh cache entry and forces
  // React Query to fetch — invalidateQueries alone occasionally fails to
  // refetch when the version-driven queryKey change happens in the same
  // render tick as the invalidation.
  const [certPlanReloadKey, setCertPlanReloadKey] = useState(0)
  // Cert-simulator sync modal state (only relevant for cert_test_cases).
  // Click "Push to Cert Sim" → modal opens → /diff runs → operator confirms →
  // /apply registers any pre-authored or operator-typed flow_definitions, then
  // applies TC decisions.
  const [showSyncModal, setShowSyncModal] = useState(false)
  const [showPrototypeModal, setShowPrototypeModal] = useState(false)
  const [modalScale, setModalScale] = useState(1)
  const bottomRef = useRef(null)

  useEffect(() => {
    if (!showPrototypeModal) return
    const onKey = (e) => { if (e.key === 'Escape') setShowPrototypeModal(false) }
    const recomputeScale = () => {
      const fitH = (window.innerHeight - 64) / 780
      const fitW = (window.innerWidth - 64) / 390
      setModalScale(Math.max(0.4, Math.min(1, fitH, fitW)))
    }
    recomputeScale()
    window.addEventListener('keydown', onKey)
    window.addEventListener('resize', recomputeScale)
    return () => {
      window.removeEventListener('keydown', onKey)
      window.removeEventListener('resize', recomputeScale)
    }
  }, [showPrototypeModal])

  // Last-synced status line for cert_test_cases. Polls the sync_log endpoint.
  const isCertCases = docDef.key === 'cert_test_cases'
  const isAdmin = me?.role === 'admin'
  const { data: syncLog } = useQuery({
    queryKey: ['cert-sync-log', changeId],
    queryFn:  () => certSyncApi.log(changeId).then(r => r.data),
    enabled:  isCertCases && isAdmin,
    refetchInterval: 30000,
  })
  const latestApply = syncLog?.latest_apply

  const { data: savedDoc } = useQuery({
    queryKey: ['product-kit-doc', changeId, docDef.key],
    queryFn:  () => agentsApi.productKitDoc(changeId, docDef.key).then(r => r.data),
  })

  const existingContent = savedDoc?.content || null
  const existingVersion = savedDoc?.version || 0
  const isDocUploaded   = savedDoc?.source === 'uploaded'

  const isStreaming    = streaming || generating

  // ── Video-script version history ──
  const isVideoDoc = docDef.key === 'promo_video' || docDef.key === 'explainer_video'
  // null = viewing the latest/live version; a number = viewing that past version (read-only).
  const [viewVersion, setViewVersion] = useState(null)
  const { data: scriptVersions } = useQuery({
    queryKey: ['video-script-versions', changeId, docDef.key, existingVersion],
    queryFn:  () => agentsApi.listVideoScriptVersions(changeId, docDef.key).then(r => r.data),
    enabled:  isVideoDoc,
  })
  const { data: pastVersionDoc } = useQuery({
    queryKey: ['video-script-version', changeId, docDef.key, viewVersion],
    queryFn:  () => agentsApi.getVideoScriptVersion(changeId, docDef.key, viewVersion).then(r => r.data),
    enabled:  isVideoDoc && viewVersion != null && viewVersion !== existingVersion,
  })
  // Viewing an older version is read-only (no refine on history).
  const isViewingPast = isVideoDoc && viewVersion != null && viewVersion !== existingVersion
  // Reset to latest whenever a new version lands.
  useEffect(() => { setViewVersion(null) }, [existingVersion])

  // Structured JSON companion for cert_test_cases — drives the table view.
  //
  // Bypasses React Query entirely: earlier iterations tried invalidate/refetch
  // patterns and even `useQuery` with a version-derived key, but the JSON fetch
  // silently no-op'd after upload+apply (0 GET /json in backend logs across
  // dozens of tests). Root cause was React Query's `enabled` gate + queryKey
  // reactivity interacting with the WebSocket-driven `isStreaming` flag in ways
  // we couldn't tame without deep instrumentation. Plain useState + useEffect
  // gives us direct, deterministic control — the useEffect fires whenever
  // isCertCases becomes true or certPlanReloadKey bumps.
  const [certPlan, setCertPlan] = useState(null)
  useEffect(() => {
    if (!isCertCases) return
    let cancelled = false
    ;(async () => {
      try {
        const { certTestCasesJsonApi: _api } = await import('../../services/api')
        const plan = await _api.get(changeId)
        if (!cancelled) setCertPlan(plan)
      } catch (err) {
        // 404 (no JSON yet) is normal on freshly-generated packs.
        if (!cancelled && err?.response?.status !== 404) {
          console.warn('certPlan fetch failed', err)
        }
        if (!cancelled) setCertPlan(null)
      }
    })()
    return () => { cancelled = true }
  }, [isCertCases, changeId, certPlanReloadKey])
  // Determine display content: while streaming, show only freshly streamed
  // chunks. When idle, the saved version wins — and an uploaded doc always
  // wins over any stale generated chunks still held from this session.
  const displayContent = isViewingPast
    ? (pastVersionDoc?.content || '')
    : isStreaming
      ? chunks
      : (isDocUploaded ? (existingContent || '') : (chunks || existingContent || ''))
  const hasContent     = Boolean(displayContent)

  useEffect(() => {
    if (streaming) bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [chunks, streaming])

  // Fetch upload/override status when the cert_test_cases panel opens so the
  // pill can show "v3 (uploaded by X)" vs "engine-generated". Deliberately
  // simple polling on hasContent (re-runs after a generation completes) —
  // no websocket needed, this endpoint is cheap.
  useEffect(() => {
    if (docDef.key !== 'cert_test_cases' || !hasContent) return
    let cancelled = false
    ;(async () => {
      try {
        const { xlsxApi } = await import('../../services/api')
        const s = await xlsxApi.status(changeId)
        if (!cancelled) setXlsxStatus(s)
      } catch { /* silent — pill just won't render */ }
    })()
    return () => { cancelled = true }
  }, [docDef.key, hasContent, changeId, xlsxUploading, xlsxReverting])

  const queryClient = useQueryClient()
  useEffect(() => {
    if (done && chunks) {
      onGenerated()
      setGenerating(false)
      queryClient.invalidateQueries(['product-kit-doc', changeId, docDef.key])
      queryClient.invalidateQueries({ queryKey: ['artifact-staleness', changeId] })
      if (isCertCases) {
        queryClient.invalidateQueries({ queryKey: ['cert-test-cases-json', changeId] })
      }
    }
  }, [done, chunks]) // eslint-disable-line react-hooks/exhaustive-deps

  // Always release the `generating` latch when the run reaches a terminal
  // state. The earlier effect only released on (done && chunks) — which
  // never fires when the engine errors before producing chunks. Symptom:
  // big inline loader keeps spinning under the error banner. Catching
  // both terminal signals (done / error) here closes that hole.
  useEffect(() => {
    if (generating && (done || error)) {
      setGenerating(false)
    }
  }, [done, error, generating])

  const handleGenerate = () => {
    reset()
    setGenerating(true)
    if (!send('start')) setGenerating(false)
  }

  const autogenFiredRef = useRef(false)
  useEffect(() => {
    if (!autogen || autogenFiredRef.current) return
    autogenFiredRef.current = true
    const t = setTimeout(() => handleGenerate(), 800)
    return () => clearTimeout(t)
  }, [autogen]) // eslint-disable-line react-hooks/exhaustive-deps

  const handleFeedback = () => {
    if (!feedback.trim()) return
    reset()
    setGenerating(true)
    if (!send(feedback.trim())) setGenerating(false)
    setFeedback('')
  }

  const handleDownload = () => {
    const ext = docDef.key === 'prototype_screens' ? 'html'
              : docDef.key === 'manifest' ? 'yaml'
              : 'md'
    const blob = new Blob([displayContent], { type: 'text/plain' })
    const url  = URL.createObjectURL(blob)
    const a    = document.createElement('a')
    a.href = url
    a.download = `${docDef.key}_v${existingVersion || 1}.${ext}`
    a.click()
    URL.revokeObjectURL(url)
  }

  const Icon = docDef.icon

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      {/* Panel header */}
      <div style={{
        padding: '16px 20px', borderBottom: '1px solid var(--border-subtle)',
        display: 'flex', alignItems: 'center', gap: '12px', flexShrink: 0,
      }}>
        <div style={{
          width: '32px', height: '32px', borderRadius: '8px',
          background: 'rgba(218,119,86,0.10)', border: '1px solid rgba(218,119,86,0.2)',
          display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0,
        }}>
          <Icon size={16} style={{ color: 'var(--accent)' }} />
        </div>
        <div style={{ flex: 1 }}>
          <p style={{ margin: 0, fontSize: '14px', fontWeight: '600', color: 'var(--text-primary)' }}>
            {docDef.label}
          </p>
          <p style={{ margin: 0, fontSize: '11px', color: 'var(--text-muted)' }}>{docDef.desc}</p>
        </div>
        <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
          {existingVersion > 0 && (
            <span style={{
              fontSize: '11px', padding: '2px 8px', borderRadius: '4px',
              background: 'var(--bg-elevated)', border: '1px solid var(--border)',
              color: 'var(--text-muted)',
            }}>v{existingVersion}</span>
          )}
          {(docDef.key === 'product_note' || docDef.key === 'circular') && (
            <StalenessBadge changeId={changeId} stageKey="product_kit" subtype={docDef.key} />
          )}
          {(docDef.key === 'product_note' || docDef.key === 'circular') && !readOnly && (
            <DocumentSourceToggle
              changeId={changeId}
              docType="product_kit"
              subtype={docDef.key}
              docLabel={docDef.label}
              source={savedDoc?.source}
              originalFilename={savedDoc?.original_filename}
              uploadedAt={savedDoc?.uploaded_at}
              confirmReplace={Boolean(existingContent) || isStreaming}
              canRevert={!isStreaming && savedDoc?.has_generated_version}
              onGenerateInstead={isStreaming ? undefined : handleGenerate}
              onBeforeUpload={async () => { if (isStreaming) { try { handleCancel() } catch { /* already finished or gone; upload proceeds either way */ } } }}
              onUploaded={() => {
                onGenerated()
                queryClient.invalidateQueries({ queryKey: ['product-kit-doc', changeId, docDef.key] })
                queryClient.invalidateQueries({ queryKey: ['artifact-staleness', changeId] })
              }}
            />
          )}
          {hasContent && (
            <>
              {/* WHY this conditional: the Excel Testcase Engine produces a
                 dedicated .xlsx for cert_test_cases (the Authority-format workbook
                 with multi-step protocol flows). Other doc-types still
                 stream as markdown — they don't have an xlsx companion.
                 The button hits /changes/{id}/product-kit/cert_test_cases/xlsx
                 which streams the file the engine attached to the host
                 job_registry record. */}
              {docDef.key === 'cert_test_cases' && (
                <button
                  disabled={xlsxDownloading || isStreaming}
                  onClick={async () => {
                    setXlsxError(null)
                    setXlsxDownloading(true)
                    try {
                      const { xlsxApi } = await import('../../services/api')
                      const blob = await xlsxApi.download(changeId)
                      const url = window.URL.createObjectURL(blob)
                      const a = document.createElement('a')
                      a.href = url
                      a.download = `cert_testcases_v${existingVersion || 1}.xlsx`
                      a.click()
                      window.URL.revokeObjectURL(url)
                    } catch (err) {
                      // Inline banner shown in the content area; ProgressBanner
                      // and ReactMarkdown content remain undisturbed.
                      setXlsxError(err?.response?.data?.detail || err.message || 'Download failed')
                    } finally {
                      setXlsxDownloading(false)
                    }
                  }}
                  style={{
                    display: 'flex', alignItems: 'center', gap: '5px',
                    padding: '5px 10px', borderRadius: '6px',
                    background: 'var(--bg-elevated)', border: '1px solid var(--border)',
                    color: 'var(--text-muted)', fontSize: '12px',
                    cursor: (xlsxDownloading || isStreaming) ? 'not-allowed' : 'pointer',
                    opacity: (xlsxDownloading || isStreaming) ? 0.5 : 1,
                    fontWeight: 600,
                  }}
                  title="Download the Authority-format Excel workbook"
                >
                  {xlsxDownloading
                    ? <Loader size={12} style={{ animation: 'spin 1s linear infinite' }} />
                    : <Download size={12} />} .xlsx
                </button>
              )}
              {/* Upload override — user downloads the .xlsx, edits it locally,
                  then uploads back here. Landing this creates a superseding
                  AgentJob so downstream (download + Product Kit shipping)
                  picks it up automatically. Hidden native file input driven
                  by a labelled trigger so we can style it. Read-only viewers
                  (risk/infosec/tech-lead) never see it. */}
              {docDef.key === 'cert_test_cases' && !readOnly && (
                <>
                  <input
                    ref={uploadFileRef}
                    type="file"
                    accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    style={{ display: 'none' }}
                    onChange={(e) => {
                      setXlsxError(null)
                      setXlsxUploadFile(e.target.files?.[0] || null)
                    }}
                  />
                  {!xlsxUploadFile ? (
                    <button
                      disabled={xlsxUploading || xlsxReverting || isStreaming}
                      onClick={() => uploadFileRef.current?.click()}
                      style={{
                        display: 'flex', alignItems: 'center', gap: '5px',
                        padding: '5px 10px', borderRadius: '6px',
                        background: 'var(--bg-elevated)',
                        border: '1px solid var(--border)',
                        color: 'var(--text-muted)', fontSize: '12px',
                        cursor: (xlsxUploading || isStreaming) ? 'not-allowed' : 'pointer',
                        opacity: (xlsxUploading || isStreaming) ? 0.5 : 1,
                        fontWeight: 600,
                      }}
                      title="Upload an edited .xlsx to replace the current cert test cases"
                    >
                      <Upload size={12} /> Upload .xlsx
                    </button>
                  ) : (
                    <>
                      <span style={{
                        fontSize: '11px', color: 'var(--text-muted)',
                        maxWidth: '200px', overflow: 'hidden',
                        textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                      }} title={xlsxUploadFile.name}>
                        {xlsxUploadFile.name}
                      </span>
                      <button
                        disabled={xlsxUploading || isStreaming}
                        onClick={async () => {
                          setXlsxError(null)
                          setXlsxUploading(true)
                          try {
                            const { xlsxApi } = await import('../../services/api')
                            const result = await xlsxApi.uploadOverride(changeId, xlsxUploadFile)
                            setXlsxUploadFile(null)
                            if (uploadFileRef.current) uploadFileRef.current.value = ''
                            setXlsxStatus({
                              version: result.version,
                              source: 'user_upload',
                              uploaded_at: result.uploaded_at,
                              warnings: result.warnings || [],
                            })
                            // Bump the reload counter — triggers the
                            // certPlan useEffect which fetches fresh JSON
                            // and updates the table. No page reload, no
                            // WebSocket disconnect, no tab-loss on reload.
                            setCertPlanReloadKey(k => k + 1)
                            // Also invalidate the doc query so the version
                            // pill + markdown fallback reflect the new
                            // uploaded content.
                            queryClient.invalidateQueries({
                              queryKey: ['product-kit-doc', changeId, docDef.key],
                            })
                            if (result.warnings?.length) {
                              setXlsxError(
                                `Uploaded v${result.version} with warnings: ` +
                                result.warnings.join('; '),
                              )
                            }
                          } catch (err) {
                            setXlsxError(
                              err?.response?.data?.detail ||
                              err.message || 'Upload failed',
                            )
                          } finally {
                            setXlsxUploading(false)
                          }
                        }}
                        style={{
                          display: 'flex', alignItems: 'center', gap: '5px',
                          padding: '5px 10px', borderRadius: '6px',
                          background: 'rgba(56,161,105,0.15)',
                          border: '1px solid rgba(56,161,105,0.4)',
                          color: 'var(--success, #38a169)', fontSize: '12px',
                          cursor: (xlsxUploading || isStreaming) ? 'not-allowed' : 'pointer',
                          opacity: (xlsxUploading || isStreaming) ? 0.5 : 1,
                          fontWeight: 600,
                        }}
                        title="Apply the uploaded workbook as the new source of truth"
                      >
                        {xlsxUploading
                          ? <Loader size={12} style={{ animation: 'spin 1s linear infinite' }} />
                          : <CheckCircle2 size={12} />} Apply
                      </button>
                      <button
                        disabled={xlsxUploading}
                        onClick={() => {
                          setXlsxUploadFile(null)
                          setXlsxError(null)
                          if (uploadFileRef.current) uploadFileRef.current.value = ''
                        }}
                        style={{
                          display: 'flex', alignItems: 'center', gap: '3px',
                          padding: '5px 8px', borderRadius: '6px',
                          background: 'transparent', border: '1px solid var(--border)',
                          color: 'var(--text-muted)', fontSize: '12px',
                          cursor: 'pointer',
                        }}
                        title="Discard the picked file"
                      >
                        <X size={12} />
                      </button>
                    </>
                  )}
                  {/* Revert only when the current source IS a user upload
                      (nothing to revert otherwise). Endpoint returns 409 when
                      no engine-generated predecessor exists — we surface
                      that as an inline error. */}
                  {xlsxStatus?.source === 'user_upload' && (
                    <button
                      disabled={xlsxUploading || xlsxReverting || isStreaming}
                      onClick={async () => {
                        setXlsxError(null)
                        setXlsxReverting(true)
                        try {
                          const { xlsxApi } = await import('../../services/api')
                          const result = await xlsxApi.revertOverride(changeId)
                          setXlsxStatus({
                            version: result.version,
                            source: 'user_revert',
                            reverted_at: result.reverted_at,
                          })
                          // Same reload-key + doc-invalidate as Apply.
                          setCertPlanReloadKey(k => k + 1)
                          queryClient.invalidateQueries({
                            queryKey: ['product-kit-doc', changeId, docDef.key],
                          })
                        } catch (err) {
                          setXlsxError(
                            err?.response?.data?.detail ||
                            err.message || 'Revert failed',
                          )
                        } finally {
                          setXlsxReverting(false)
                        }
                      }}
                      style={{
                        display: 'flex', alignItems: 'center', gap: '5px',
                        padding: '5px 10px', borderRadius: '6px',
                        background: 'transparent',
                        border: '1px solid var(--border)',
                        color: 'var(--text-muted)', fontSize: '12px',
                        cursor: (xlsxUploading || xlsxReverting || isStreaming)
                          ? 'not-allowed' : 'pointer',
                        opacity: (xlsxUploading || xlsxReverting || isStreaming) ? 0.5 : 1,
                        fontWeight: 600,
                      }}
                      title="Revert to the last engine-generated cert test cases pack"
                    >
                      {xlsxReverting
                        ? <Loader size={12} style={{ animation: 'spin 1s linear infinite' }} />
                        : <RefreshCw size={12} />} Revert
                    </button>
                  )}
                  {/* Version + source pill. Only rendered when we know the
                      status; keeps the toolbar clean before the first
                      generation. */}
                  {xlsxStatus && (
                    <span style={{
                      fontSize: '10px', color: 'var(--text-muted)',
                      padding: '3px 8px', borderRadius: '10px',
                      background: xlsxStatus.source === 'user_upload'
                        ? 'rgba(218,119,86,0.12)'
                        : 'var(--bg-elevated)',
                      border: '1px solid var(--border)',
                    }}>
                      v{xlsxStatus.version || 1} · {
                        xlsxStatus.source === 'user_upload' ? 'uploaded' :
                        xlsxStatus.source === 'user_revert' ? 'reverted' :
                        'engine'
                      }
                    </span>
                  )}
                </>
              )}
              {isCertCases && isAdmin && (
                <button
                  disabled={isStreaming}
                  onClick={() => setShowSyncModal(true)}
                  style={{
                    display: 'flex', alignItems: 'center', gap: '5px',
                    padding: '5px 10px', borderRadius: '6px',
                    background: 'rgba(218,119,86,0.10)',
                    border: '1px solid rgba(218,119,86,0.3)',
                    color: 'var(--accent)', fontSize: '12px',
                    cursor: isStreaming ? 'not-allowed' : 'pointer',
                    opacity: isStreaming ? 0.5 : 1,
                    fontWeight: 600,
                  }}
                  title="Diff and push these test cases to cert-agent's tc_store (incl. any new flow definitions)"
                >
                  <Upload size={12} /> Push to Cert Sim
                </button>
              )}

              {/* JSON dump of the WorkbookPlan — verbatim Pydantic schema
                  consumed by downstream test-management imports / diffs.
                  Hits /changes/{id}/product-kit/cert_test_cases/json. */}
              {docDef.key === 'cert_test_cases' && (
                <button
                  disabled={xlsxDownloading || isStreaming}
                  onClick={async () => {
                    setXlsxError(null)
                    setXlsxDownloading(true)
                    try {
                      const { certTestCasesJsonApi } = await import('../../services/api')
                      const blob = await certTestCasesJsonApi.download(changeId)
                      const url = window.URL.createObjectURL(blob)
                      const a = document.createElement('a')
                      a.href = url
                      a.download = `cert_testcases_v${existingVersion || 1}.json`
                      a.click()
                      window.URL.revokeObjectURL(url)
                    } catch (err) {
                      setXlsxError(err?.response?.data?.detail || err.message || 'Download failed')
                    } finally {
                      setXlsxDownloading(false)
                    }
                  }}
                  style={{
                    display: 'flex', alignItems: 'center', gap: '5px',
                    padding: '5px 10px', borderRadius: '6px',
                    background: 'var(--bg-elevated)', border: '1px solid var(--border)',
                    color: 'var(--text-muted)', fontSize: '12px',
                    cursor: (xlsxDownloading || isStreaming) ? 'not-allowed' : 'pointer',
                    opacity: (xlsxDownloading || isStreaming) ? 0.5 : 1,
                    fontWeight: 600,
                  }}
                  title="Download structured WorkbookPlan as JSON"
                >
                  <Download size={12} /> .json
                </button>
              )}
              <DocxDownloadButton changeId={changeId} docType="product_kit" subtype={docDef.key} label=".docx" />
              {docDef.key === 'product_deck' && (
                <PptxDownloadButton changeId={changeId} docType="product_kit" subtype="product_deck" label=".pptx" />
              )}
              <button
                onClick={handleDownload}
                style={{
                  display: 'flex', alignItems: 'center', gap: '5px',
                  padding: '5px 10px', borderRadius: '6px',
                  background: 'var(--bg-elevated)', border: '1px solid var(--border)',
                  color: 'var(--text-muted)', fontSize: '12px', cursor: 'pointer',
                }}
                title="Download raw markdown source"
              >
                <Download size={12} /> .md
              </button>
            </>
          )}
        </div>
      </div>

      {/* Content area */}
      <div style={{ flex: 1, overflow: 'auto', padding: '20px' }}>
        {/* R-5 — resume banner. Visible when this doc_type's job is in flight.
            Stage label is mapped through friendlyStage() so users see
            "Writing test cases" instead of "write" / "writer". */}
        <ProgressBanner
          jobId={jobId} status={jobStatus} stage={friendlyStage(jobStage)} progress={jobProgress}
          startedAt={jobStartedAt} startedBy={jobStartedBy}
          resuming={isResuming} onCancel={handleCancel}
          currentUserId={me?.id}
        />

        {error && (
          <div style={{
            display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between',
            gap: '12px',
            padding: '12px 16px', borderRadius: '8px', marginBottom: '16px',
            background: 'rgba(224,108,108,0.08)', border: '1px solid rgba(224,108,108,0.25)',
            color: 'var(--danger)', fontSize: '13px',
          }}>
            <div style={{ flex: 1, minWidth: 0 }}>
              <p style={{ margin: '0 0 4px', fontWeight: 600 }}>Generation failed</p>
              <p style={{ margin: 0, fontSize: '12px', wordBreak: 'break-word' }}>{error}</p>
            </div>
            <div style={{ display: 'flex', gap: '8px', flexShrink: 0 }}>
              <button
                onClick={() => { reset(); handleGenerate() }}
                disabled={isStreaming}
                style={{
                  padding: '5px 12px', borderRadius: '6px',
                  background: 'var(--danger)', color: 'white',
                  border: 'none', fontSize: '12px', fontWeight: 600,
                  cursor: isStreaming ? 'not-allowed' : 'pointer',
                  opacity: isStreaming ? 0.6 : 1,
                }}
                title="Reset error and retry generation"
              >
                Try again
              </button>
              <button
                onClick={() => reset()}
                style={{
                  background: 'none', border: 'none', cursor: 'pointer',
                  color: 'var(--danger)', fontSize: '12px', fontWeight: 600,
                  padding: '5px 8px',
                }}
                title="Dismiss"
              >
                Dismiss
              </button>
            </div>
          </div>
        )}

        {/* Last-synced status line for cert_test_cases (admins only) */}
        {isCertCases && isAdmin && (
          <div style={{
            padding: '8px 14px', borderRadius: '6px', marginBottom: '12px',
            background: latestApply ? 'rgba(63,185,80,0.06)' : 'var(--bg-elevated)',
            border: `1px solid ${latestApply ? 'rgba(63,185,80,0.25)' : 'var(--border)'}`,
            fontSize: '12px', color: 'var(--text-secondary)',
            display: 'flex', alignItems: 'center', gap: '8px',
          }}>
            <Upload size={12} style={{ color: latestApply ? '#3fb950' : 'var(--text-muted)' }} />
            {latestApply ? (
              <span>
                Last synced to cert simulator{' '}
                <strong>{relTime(latestApply.created_at)}</strong> by{' '}
                <strong>{latestApply.actor || 'system'}</strong>
                {' · '}
                <strong>{latestApply.summary?.applied || 0}</strong> applied
                {(latestApply.summary?.failed?.length || 0) > 0 && (
                  <span style={{ color: 'var(--danger)' }}>
                    {' · '}<strong>{latestApply.summary.failed.length}</strong> failed
                  </span>
                )}
                {' · subset '}<code className="id-mono">{latestApply.summary?.subset || `cr-${changeId.slice(0, 8)}`}</code>
              </span>
            ) : (
              <span style={{ color: 'var(--text-muted)' }}>
                Not yet synced to cert simulator. Click <strong>Push to Cert Sim</strong> above to register these test cases.
              </span>
            )}
          </div>
        )}

        {xlsxError && (
          <div style={{
            display: 'flex', alignItems: 'center', justifyContent: 'space-between',
            gap: '12px',
            padding: '10px 16px', borderRadius: '8px', marginBottom: '16px',
            background: 'rgba(224,108,108,0.08)', border: '1px solid rgba(224,108,108,0.25)',
            color: 'var(--danger)', fontSize: '13px',
          }}>
            <span>Excel download failed: {xlsxError}</span>
            <button
              onClick={() => setXlsxError(null)}
              style={{
                background: 'none', border: 'none', cursor: 'pointer',
                color: 'var(--danger)', fontSize: '12px', fontWeight: 600,
              }}
              title="Dismiss"
            >
              Dismiss
            </button>
          </div>
        )}

        {!hasContent && !isStreaming && (
          <div style={{
            display: 'flex', flexDirection: 'column', alignItems: 'center',
            justifyContent: 'center', height: '280px', gap: '16px',
          }}>
            <div style={{
              width: '56px', height: '56px', borderRadius: '16px',
              background: 'rgba(218,119,86,0.08)', border: '1px solid rgba(218,119,86,0.2)',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
            }}>
              <Icon size={24} style={{ color: 'var(--accent)' }} />
            </div>
            <div style={{ textAlign: 'center' }}>
              <p style={{ margin: '0 0 4px', fontSize: '15px', fontWeight: '600', color: 'var(--text-primary)' }}>
                {readOnly ? `${docDef.label} not available yet` : `Generate ${docDef.label}`}
              </p>
              <p style={{ margin: 0, fontSize: '13px', color: 'var(--text-muted)' }}>
                {readOnly
                  ? 'the Authority hasn’t published this document yet.'
                  : 'AI will create this document from the approved BRD and Tech Spec'}
              </p>
            </div>
            {!readOnly && (
              <button
                onClick={handleGenerate}
                style={{
                  display: 'flex', alignItems: 'center', gap: '8px',
                  padding: '10px 24px', background: 'var(--accent)', color: 'white',
                  border: 'none', borderRadius: '8px', fontSize: '14px', fontWeight: '600',
                  cursor: 'pointer',
                }}
                onMouseEnter={e => e.currentTarget.style.background = 'var(--accent-hover)'}
                onMouseLeave={e => e.currentTarget.style.background = 'var(--accent)'}
              >
                <Play size={14} /> Generate
              </button>
            )}
          </div>
        )}

        {isStreaming && !chunks && (
          <div style={{
            display: 'flex', flexDirection: 'column', alignItems: 'center',
            justifyContent: 'center', height: '320px', gap: '14px',
            background: 'var(--bg-card)', border: '1px solid var(--border-subtle)',
            borderRadius: '8px',
          }}>
            <Loader size={28} style={{ animation: 'spin 1s linear infinite', color: 'var(--accent)' }} />
            <div style={{ textAlign: 'center' }}>
              <p style={{ margin: '0 0 4px', fontSize: '15px', fontWeight: '600', color: 'var(--text-primary)' }}>
                Generating {docDef.label}…
              </p>
              <p style={{ margin: 0, fontSize: '13px', color: 'var(--text-muted)' }}>
                {jobStage ? friendlyStage(jobStage) : 'Starting up…'}
                {typeof jobProgress === 'number' && jobProgress >= 0 ? ` · ${jobProgress}%` : ''}
              </p>
              {isResuming && (
                <p style={{ margin: '4px 0 0', fontSize: '11px', color: 'var(--text-muted)', fontStyle: 'italic' }}>
                  Picked up an in-progress run — replaying recent progress.
                </p>
              )}
            </div>
            {typeof jobProgress === 'number' && jobProgress >= 0 && jobProgress <= 100 && (
              <div style={{
                width: '60%', maxWidth: '320px', height: '6px',
                background: 'var(--bg-base)', borderRadius: '3px', overflow: 'hidden',
                border: '1px solid var(--border-subtle)',
              }}>
                <div style={{
                  width: `${jobProgress}%`, height: '100%',
                  background: 'var(--accent)', transition: 'width 0.3s ease',
                }} />
              </div>
            )}
          </div>
        )}

        {isVideoDoc && scriptVersions?.length > 0 && (
          <div style={{
            display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap',
            marginBottom: 10, padding: '8px 12px', borderRadius: 8,
            background: isViewingPast ? 'rgba(218,119,86,0.08)' : 'var(--bg-elevated)',
            border: `1px solid ${isViewingPast ? 'rgba(218,119,86,0.3)' : 'var(--border-subtle)'}`,
          }}>
            <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)' }}>
              Script version
            </span>
            <select
              value={viewVersion ?? existingVersion}
              onChange={e => {
                const v = Number(e.target.value)
                setViewVersion(v === existingVersion ? null : v)
              }}
              style={{ padding: '5px 8px', borderRadius: 6, fontSize: 12, background: 'var(--bg-card)', color: 'var(--text-primary)', border: '1px solid var(--border)' }}
            >
              {scriptVersions.map(v => (
                <option key={v.version} value={v.version}>
                  v{v.version}{v.version === existingVersion ? ' (latest)' : ''}
                  {v.created_at ? ` — ${new Date(v.created_at).toLocaleString('en-IN', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' })}` : ''}
                  {v.has_video ? ' · 🎬' : ''}
                </option>
              ))}
            </select>
            {isViewingPast && (
              <button
                onClick={() => setViewVersion(null)}
                style={{ padding: '5px 10px', borderRadius: 6, fontSize: 12, fontWeight: 600, background: 'var(--accent)', color: '#fff', border: 'none', cursor: 'pointer' }}
              >
                Back to latest (v{existingVersion})
              </button>
            )}
            {isViewingPast && (
              <span style={{ fontSize: 11, color: 'var(--accent)' }}>Read-only — older version</span>
            )}
          </div>
        )}

        {displayContent && (
          <div style={{
            background: 'var(--bg-card)', border: '1px solid var(--border-subtle)',
            borderRadius: '8px', padding: '20px 24px',
          }}>
            {docDef.key === 'prototype_screens' ? (
              <div style={{ display: 'flex', justifyContent: 'center', padding: '8px 0' }}>
                {/* Scaled inline preview — reserves the visual size (234×468), while
                    the iframe renders at native 390×780 internally so prototype HTML
                    designed for that viewport reads correctly. */}
                <div style={{ position: 'relative', width: 234, height: 468 }}>
                  <div style={{
                    width: 390, height: 780,
                    borderRadius: 36, padding: 12,
                    background: '#111', boxShadow: '0 12px 40px rgba(0,0,0,0.35)',
                    position: 'absolute', top: 0, left: 0,
                    transform: 'scale(0.6)', transformOrigin: 'top left',
                  }}>
                    <div style={{
                      position: 'absolute', top: 12, left: '50%', transform: 'translateX(-50%)',
                      width: 110, height: 22, background: '#111', borderRadius: 14, zIndex: 2,
                    }} />
                    <iframe
                      title="Prototype preview"
                      srcDoc={hardenFrameHtml(displayContent)}
                      sandbox="allow-scripts"
                      style={{
                        width: '100%', height: '100%',
                        border: 'none', borderRadius: 28,
                        background: '#fff', display: 'block',
                      }}
                    />
                  </div>
                  <button
                    onClick={() => setShowPrototypeModal(true)}
                    title="Open full-size preview"
                    style={{
                      position: 'absolute', top: 4, right: 4, zIndex: 3,
                      padding: '6px 10px', background: 'rgba(255,255,255,0.94)',
                      border: '1px solid var(--border)', borderRadius: 6,
                      cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 5,
                      fontSize: 11, fontWeight: 600, color: 'var(--text-primary)',
                      boxShadow: '0 2px 6px rgba(0,0,0,0.15)',
                    }}
                  >
                    <Maximize2 size={12} /> Expand
                  </button>
                </div>
              </div>
            ) : isCertCases && !isStreaming && certPlan?.test_cases?.length ? (
              <CertTestCasesTable plan={certPlan} />
            ) : (
              <div className="md-content" style={{ fontSize: '13px', lineHeight: '1.7' }}>
                <ReactMarkdown>{displayContent}</ReactMarkdown>
              </div>
            )}
            <div ref={bottomRef} />
          </div>
        )}
      </div>

      {isVideoDoc && !isViewingPast && (
        <VideoUploadPanel changeId={changeId} docType={docDef.key} readOnly={readOnly} hasScript={hasContent} />
      )}

      {/* Feedback bar (Revise / Regenerate) — shown once content exists, hidden
          for read-only review teams and when viewing a past script version. */}
      {hasContent && !readOnly && !isViewingPast && (
        <div style={{
          padding: '12px 20px', borderTop: '1px solid var(--border-subtle)',
          background: 'var(--bg-elevated)', flexShrink: 0,
        }}>
          <div style={{ display: 'flex', gap: '8px', alignItems: 'flex-end' }}>
            <textarea
              value={feedback}
              onChange={e => setFeedback(e.target.value)}
              placeholder="Request a revision or ask to adjust specific sections…"
              disabled={isStreaming}
              rows={2}
              style={{
                flex: 1, padding: '8px 12px', borderRadius: '8px',
                border: '1px solid var(--border)', background: 'var(--bg-base)',
                color: 'var(--text-primary)', fontSize: '13px', resize: 'none',
                fontFamily: 'inherit',
              }}
            />
            <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
              <button
                onClick={handleFeedback}
                disabled={isStreaming || !feedback.trim()}
                style={{
                  padding: '8px 16px', background: 'var(--accent)', color: 'white',
                  border: 'none', borderRadius: '8px', fontSize: '13px', fontWeight: '600',
                  cursor: isStreaming || !feedback.trim() ? 'not-allowed' : 'pointer',
                  opacity: isStreaming || !feedback.trim() ? 0.5 : 1,
                  display: 'flex', alignItems: 'center', gap: '6px', whiteSpace: 'nowrap',
                }}
              >
                {isStreaming ? <Loader size={12} style={{ animation: 'spin 1s linear infinite' }} /> : <RefreshCw size={12} />}
                Revise
              </button>
              <button
                onClick={handleGenerate}
                disabled={isStreaming}
                style={{
                  padding: '8px 16px', background: 'var(--bg-base)', color: 'var(--text-secondary)',
                  border: '1px solid var(--border)', borderRadius: '8px', fontSize: '13px', fontWeight: '500',
                  cursor: isStreaming ? 'not-allowed' : 'pointer',
                  opacity: isStreaming ? 0.5 : 1, whiteSpace: 'nowrap',
                }}
              >
                Regenerate
              </button>
            </div>
          </div>
        </div>
      )}
      {/* Cert-simulator sync modal — only mounted when triggered by the Push button. */}
      {showSyncModal && (
        <SyncDiffModal
          changeId={changeId}
          changeTitle={savedDoc?.change_title || `Change ${changeId.slice(0, 8)}`}
          onClose={() => setShowSyncModal(false)}
          onApplied={() => {
            // Refresh the sync log + cert timeline + dashboard caches
            queryClient.invalidateQueries({ queryKey: ['cert-sync-log', changeId] })
            queryClient.invalidateQueries({ queryKey: ['cert-timeline'] })
            queryClient.invalidateQueries({ queryKey: ['cert-dashboard'] })
          }}
        />
      )}
      {showPrototypeModal && (
        <div
          onClick={() => setShowPrototypeModal(false)}
          style={{
            position: 'fixed', inset: 0, zIndex: 1000,
            background: 'rgba(0,0,0,0.72)',
            overflow: 'auto',
            display: 'flex',
            padding: 24, backdropFilter: 'blur(2px)',
          }}
        >
          <div onClick={e => e.stopPropagation()} style={{
            position: 'relative', margin: 'auto',
            width: 390 * modalScale, height: 780 * modalScale,
          }}>
            <button
              onClick={() => setShowPrototypeModal(false)}
              title="Close (Esc)"
              aria-label="Close prototype preview"
              style={{
                position: 'absolute', top: -14, right: -14, zIndex: 2,
                width: 36, height: 36, borderRadius: '50%',
                background: '#fff', border: 'none', cursor: 'pointer',
                boxShadow: '0 4px 14px rgba(0,0,0,0.4)',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
              }}
            >
              <X size={16} />
            </button>
            <div style={{
              width: 390, height: 780,
              borderRadius: 36, padding: 12,
              background: '#111', boxShadow: '0 24px 60px rgba(0,0,0,0.6)',
              position: 'absolute', top: 0, left: 0,
              transform: `scale(${modalScale})`, transformOrigin: 'top left',
            }}>
              <div style={{
                position: 'absolute', top: 12, left: '50%', transform: 'translateX(-50%)',
                width: 110, height: 22, background: '#111', borderRadius: 14, zIndex: 2,
              }} />
              <iframe
                title="Prototype preview (expanded)"
                srcDoc={hardenFrameHtml(displayContent)}
                sandbox="allow-scripts"
                style={{
                  width: '100%', height: '100%',
                  border: 'none', borderRadius: 28,
                  background: '#fff', display: 'block',
                }}
              />
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

// Tiny relative-time helper for the last-synced status line.
function relTime(iso) {
  if (!iso) return ''
  const ms = Date.now() - new Date(iso).getTime()
  const s = Math.floor(ms / 1000)
  if (s < 60)    return `${s}s ago`
  if (s < 3600)  return `${Math.floor(s / 60)} min ago`
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`
  return new Date(iso).toLocaleDateString('en-IN', { day: '2-digit', month: 'short' })
}

// ── Cert test cases — structured table view ───────────────────────────────────
//
// Replaces the per-case markdown sections (each TC = H3 + DETAILS/DESCRIPTION/
// TEST STEPS code blocks) with a scannable table. Source is the simulator-
// contract JSON companion written by excel_writer/exporters.write_companions.
//
// Row expansion preserves the prose blocks (details / description / steps) so
// nothing the markdown view showed is lost.

const STATUS_COLORS = {
  Success: '#3fb950',
  Failure: '#e06c6c',
  Deemed:  '#d29922',
  Partial: '#58a6ff',
}

function certTh() {
  return {
    padding: '8px 12px',
    textAlign: 'left',
    fontSize: '10px',
    fontWeight: 600,
    color: 'var(--text-muted)',
    textTransform: 'uppercase',
    letterSpacing: '0.06em',
    borderBottom: '1px solid var(--border)',
    whiteSpace: 'nowrap',
    background: 'var(--bg-elevated)',
    position: 'sticky',
    top: 0,
    zIndex: 1,
  }
}

function certTd() {
  return {
    padding: '8px 12px',
    fontSize: '12px',
    color: 'var(--text-primary)',
    verticalAlign: 'top',
    borderBottom: '1px solid var(--border-subtle)',
  }
}

function certPill(color) {
  return {
    display: 'inline-flex', alignItems: 'center',
    padding: '2px 8px',
    borderRadius: '999px',
    fontSize: '10px',
    fontWeight: 700,
    color,
    background: `${color}1A`,
    border: `1px solid ${color}40`,
    letterSpacing: '0.04em',
    whiteSpace: 'nowrap',
  }
}

// Column layout matches the Authority workbook exactly:
//   S.NO | TEST ID | DETAILS | DESCRIPTION | Scope | TXN INITIATED BY | STATUS | TEST STEPS
// Multi-line blocks (DETAILS / DESCRIPTION / TEST STEPS) preserve their
// newlines via `white-space: pre-wrap` — no click-to-expand, everything
// visible inline like the .xlsx a reviewer would open.

function certBlockCell(width = 320) {
  return {
    ...certTd(),
    whiteSpace: 'pre-wrap',
    fontFamily: 'var(--font-mono, monospace)',
    fontSize: '11px',
    lineHeight: 1.5,
    color: 'var(--text-secondary)',
    minWidth: width,
    maxWidth: width * 1.6,
    verticalAlign: 'top',
  }
}

function CertSheetTable({ sheetName, sheetCases }) {
  return (
    <div style={{ marginBottom: 24 }}>
      <div style={{
        display: 'flex', alignItems: 'baseline', gap: 10,
        margin: '4px 0 8px',
      }}>
        <span style={{
          fontSize: 13, fontWeight: 700, color: 'var(--text-primary)',
          letterSpacing: '0.02em',
        }}>
          {sheetName}
        </span>
        <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
          {sheetCases.length} {sheetCases.length === 1 ? 'case' : 'cases'}
        </span>
      </div>
      <div style={{
        border: '1px solid var(--border)',
        borderRadius: 8,
        overflow: 'auto',
        background: 'var(--bg-card)',
      }}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              <th style={{ ...certTh(), width: 48 }}>S.NO</th>
              <th style={{ ...certTh(), width: 90 }}>TEST ID</th>
              <th style={certTh()}>DETAILS</th>
              <th style={certTh()}>DESCRIPTION</th>
              <th style={{ ...certTh(), width: 96 }}>Scope</th>
              <th style={{ ...certTh(), width: 140 }}>TXN INITIATED BY</th>
              <th style={{ ...certTh(), width: 96 }}>STATUS</th>
              <th style={certTh()}>TEST STEPS</th>
            </tr>
          </thead>
          <tbody>
            {sheetCases.map((tc, i) => {
              const statusColor = STATUS_COLORS[tc.expected_status] || 'var(--text-muted)'
              const r = tc.rendered || {}
              return (
                <tr
                  key={tc.test_id || i}
                  style={{
                    background: i % 2 === 0 ? 'transparent' : 'var(--bg-elevated)',
                  }}
                >
                  <td style={{ ...certTd(), textAlign: 'center', color: 'var(--text-muted)', verticalAlign: 'top' }}>
                    {i + 1}
                  </td>
                  <td style={{
                    ...certTd(),
                    fontFamily: 'var(--font-mono, monospace)',
                    fontWeight: 600, whiteSpace: 'nowrap',
                    verticalAlign: 'top',
                  }}>
                    {tc.test_id}
                  </td>
                  <td style={certBlockCell(280)}>
                    {(r.details_block || '').trim()}
                  </td>
                  <td style={{
                    ...certBlockCell(280),
                    fontFamily: 'inherit',
                    fontSize: 12,
                  }}>
                    {(r.description_block || tc.scenario_summary || '').trim()}
                  </td>
                  <td style={{ ...certTd(), whiteSpace: 'nowrap', verticalAlign: 'top' }}>
                    {tc.scope || '—'}
                  </td>
                  <td style={{ ...certTd(), whiteSpace: 'nowrap', verticalAlign: 'top' }}>
                    {tc.txn_initiated_by || '—'}
                  </td>
                  <td style={{ ...certTd(), verticalAlign: 'top' }}>
                    <span style={certPill(statusColor)}>{tc.expected_status}</span>
                  </td>
                  <td style={certBlockCell(360)}>
                    {(r.steps_block || '').trim()}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// Map test-ID prefix → role sheet name for engine-generated packs whose
// JSON is a flat test_cases list without sheet grouping. Real the Authority packs
// follow this prefix convention (PR/PE/RE/BE/MT), so we can rebuild the
// grouping without any backend change. Anything else lands in "Other".
const TEST_ID_SHEET_HINTS = [
  { prefix: 'PR_',  sheet: 'Payer PSP' },
  { prefix: 'PE_',  sheet: 'Payee PSP' },
  { prefix: 'RE_',  sheet: 'Remitter' },
  { prefix: 'BE_',  sheet: 'Beneficiary' },
  { prefix: 'MT_',  sheet: 'META-API' },
  { prefix: 'META', sheet: 'META-API' },
  { prefix: 'UAT_', sheet: 'UAT MOBILE APP' },
  { prefix: 'DL_',  sheet: 'Delegate' },
  { prefix: 'VC_',  sheet: 'Voucher' },
  // Skip placeholders / metadata rows entirely — surfaced separately
  // as "issues" if we ever need to; for now, out of the table.
]

function inferSheetFromTestId(testId) {
  if (!testId) return 'Test cases'
  const t = String(testId).toUpperCase()
  // Metadata-ish placeholders emitted by the engine when planning failed.
  if (t.startsWith('IND_') || t.startsWith('VER_') || t.startsWith('SUM_') ||
      t.startsWith('MODES_') || t.includes('PLACEHOLDER')) {
    return null   // signal: drop from the table
  }
  for (const { prefix, sheet } of TEST_ID_SHEET_HINTS) {
    if (t.startsWith(prefix)) return sheet
  }
  return 'Test cases'
}

function groupCasesBySheet(cases) {
  const groups = new Map()
  const order = []
  for (const tc of cases) {
    // Prefer explicit `sheet` field (future engine JSON) or fall back to
    // prefix inference for legacy engine JSONs that predate the `sheet`
    // field. Skips metadata-ish rows.
    const key = tc.sheet || inferSheetFromTestId(tc.test_id)
    if (!key) continue
    if (!groups.has(key)) { groups.set(key, []); order.push(key) }
    groups.get(key).push(tc)
  }
  return order.map(name => ({ name, test_cases: groups.get(name) }))
}

function CertTestCasesTable({ plan }) {
  // Grouping precedence:
  //   1. plan.sheets (uploaded packs — our own parser populates this)
  //   2. per-case `sheet` field (post-fix engine JSONs)
  //   3. test_id prefix inference (pre-fix engine JSONs on disk today)
  // The fallback path ensures the exact same grouped table view for both
  // upload and legacy engine-generated JSONs, without a backend backfill.
  let sheets
  if (Array.isArray(plan?.sheets) && plan.sheets.length > 0) {
    sheets = plan.sheets.filter(s => (s.test_cases || []).length > 0)
  } else if (Array.isArray(plan?.test_cases) && plan.test_cases.length > 0) {
    sheets = groupCasesBySheet(plan.test_cases)
  } else {
    sheets = []
  }
  if (sheets.length === 0) {
    sheets = [{ name: 'Test cases', test_cases: plan?.test_cases || [] }]
  }

  const totalCases = sheets.reduce((n, s) => n + (s.test_cases?.length || 0), 0)
  const sheetNames = sheets.map(s => s.name).join(' · ')

  return (
    <div>
      <div style={{
        display: 'flex', alignItems: 'baseline', gap: 12, flexWrap: 'wrap',
        marginBottom: 16, fontSize: 12, color: 'var(--text-secondary)',
      }}>
        {plan?.feature_name && (
          <span style={{ fontSize: 14, fontWeight: 600, color: 'var(--text-primary)' }}>
            {plan.feature_name}
          </span>
        )}
        <span><strong>{totalCases}</strong> test cases across {sheets.length} sheet{sheets.length === 1 ? '' : 's'}</span>
        <span style={{ color: 'var(--text-muted)', fontSize: 11 }}>{sheetNames}</span>
      </div>
      {sheets.map(sheet => (
        <CertSheetTable
          key={sheet.name}
          sheetName={sheet.name}
          sheetCases={sheet.test_cases || []}
        />
      ))}
    </div>
  )
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function ProductKit() {
  const { id } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [searchParams] = useSearchParams()
  const requestedDoc = searchParams.get('doc')
  const autogenRequested = searchParams.get('autogen') === '1'
  const initialDoc = requestedDoc && DOC_TYPES.some(d => d.key === requestedDoc)
    ? requestedDoc
    : DOC_TYPES[0].key
  const [selectedDoc, setSelectedDoc] = useState(initialDoc)
  const [completing, setCompleting]   = useState(false)
  const [genAllOpen, setGenAllOpen]   = useState(false)
  // Review teams (Risk/InfoSec/Tech) get a read-only product-kit view: they can
  // read/download the docs but not generate, complete, or touch other phases.
  const { user: me } = useAuth()
  const readOnly = ['risk_reviewer', 'infosec_reviewer', 'tech_lead'].includes(me?.role)

  const { data: kitData, isLoading } = useQuery({
    queryKey: ['product-kit', id],
    queryFn:  () => agentsApi.productKitAll(id).then(r => r.data),
    refetchInterval: 5000,
  })

  const documents = kitData?.documents || []
  const docMap    = Object.fromEntries(documents.map(d => [d.doc_type, d]))
  // Count only the doc types the UI actually shows. The backend still returns
  // every ProductKitDocType (incl. the retired `product_doc`), which would
  // otherwise skew the count and break `allDone`.
  const generated = DOC_TYPES.filter(dt => docMap[dt.key]?.has_content).length
  const allDone   = generated === DOC_TYPES.length

  // Live job status per doc subtype — used by the sidebar to render
  // "Generating…" with a spinner on whichever doc is currently being
  // generated. Reading from JobsContext keeps the sidebar in sync with the
  // ProgressBanner / loader without an additional WS connection.
  const { activeJobsForChange } = useJobs()
  const runningJobsByType = Object.fromEntries(
    activeJobsForChange(id, 'product_kit').map(j => [j.subtype, j])
  )

  const selectedDocDef = DOC_TYPES.find(d => d.key === selectedDoc)

  const handleDocGenerated = () => {
    queryClient.invalidateQueries(['product-kit', id])
  }

  const handleComplete = async () => {
    setCompleting(true)
    try {
      await agentsApi.productKitComplete(id)
      navigate(`/changes/${id}`)
    } catch (err) {
      console.error('Complete failed', err)
    } finally {
      setCompleting(false)
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100vh', overflow: 'hidden' }}>
      {/* Top bar */}
      <div style={{
        padding: '14px 24px', borderBottom: '1px solid var(--border-subtle)',
        background: 'var(--bg-elevated)', display: 'flex', alignItems: 'center',
        justifyContent: 'space-between', flexShrink: 0,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '16px' }}>
          <button
            onClick={() => navigate(readOnly ? '/dashboard' : `/changes/${id}`)}
            style={{
              display: 'flex', alignItems: 'center', gap: '6px',
              fontSize: '13px', color: 'var(--text-muted)',
              background: 'none', border: 'none', cursor: 'pointer', padding: 0,
            }}
            onMouseEnter={e => e.currentTarget.style.color = 'var(--text-primary)'}
            onMouseLeave={e => e.currentTarget.style.color = 'var(--text-muted)'}
          >
            <ArrowLeft size={14} /> Back
          </button>
          <div style={{ width: '1px', height: '18px', background: 'var(--border)' }} />
          <div>
            <p style={{ margin: 0, fontSize: '14px', fontWeight: '600', color: 'var(--text-primary)' }}>
              Product Kit
            </p>
            <p style={{ margin: 0, fontSize: '11px', color: 'var(--text-muted)' }}>
              {generated} / {DOC_TYPES.length} documents generated
            </p>
          </div>
        </div>

        {/* Progress bar + Generate-All + Complete button */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '16px' }}>
          {!readOnly && <ShowArtifactsButton changeId={id} />}
          <TranscriptsDownloadButton changeId={id} section="product_kit" label="Transcripts" />
          {!readOnly && <SkipStepButton changeId={id} nextRoute={`/changes/${id}`} />}
          <div style={{ width: '160px' }}>
            <div style={{
              height: '4px', borderRadius: '4px', background: 'var(--bg-base)',
              overflow: 'hidden',
            }}>
              <div style={{
                height: '100%', borderRadius: '4px', background: 'var(--success)',
                width: `${(generated / DOC_TYPES.length) * 100}%`,
                transition: 'width 0.4s',
              }} />
            </div>
          </div>
          {!allDone && !readOnly && (
            <button
              onClick={() => setGenAllOpen(true)}
              title="Generate all remaining docs in parallel"
              style={{
                display: 'flex', alignItems: 'center', gap: '6px',
                padding: '8px 14px', background: 'var(--accent)', color: 'white',
                border: 'none', borderRadius: '8px', fontSize: '12px', fontWeight: '600',
                cursor: 'pointer',
              }}
            >
              <Zap size={13} /> Generate All
            </button>
          )}
          {allDone && !readOnly && (
            <button
              onClick={handleComplete}
              disabled={completing}
              style={{
                display: 'flex', alignItems: 'center', gap: '8px',
                padding: '8px 18px', background: 'var(--success)', color: 'white',
                border: 'none', borderRadius: '8px', fontSize: '13px', fontWeight: '600',
                cursor: completing ? 'not-allowed' : 'pointer',
                opacity: completing ? 0.7 : 1,
              }}
            >
              {completing ? <Loader size={13} style={{ animation: 'spin 1s linear infinite' }} /> : <CheckCircle2 size={13} />}
              Complete Product Kit
            </button>
          )}
        </div>
      </div>

      {/* Body: sidebar + main panel */}
      <div style={{ display: 'flex', flex: 1, overflow: 'hidden' }}>
        {/* Left sidebar — doc list */}
        <div style={{
          width: '260px', flexShrink: 0, borderRight: '1px solid var(--border-subtle)',
          background: 'var(--bg-elevated)', overflow: 'auto',
        }}>
          {isLoading ? (
            <div style={{ padding: '20px', color: 'var(--text-muted)', fontSize: '13px' }}>
              Loading…
            </div>
          ) : (
            DOC_TYPES.map(dt => {
              const docData  = docMap[dt.key] || {}
              const isActive = selectedDoc === dt.key
              const isDone   = docData.has_content
              const runningJob = runningJobsByType[dt.key]
              const isRunning  = Boolean(runningJob)
              const Icon     = dt.icon

              return (
                <div
                  key={dt.key}
                  onClick={() => setSelectedDoc(dt.key)}
                  style={{
                    display: 'flex', alignItems: 'center', gap: '10px',
                    padding: '10px 14px', cursor: 'pointer',
                    borderBottom: '1px solid var(--border-subtle)',
                    background: isActive ? 'rgba(218,119,86,0.08)' : 'transparent',
                    borderLeft: isActive ? '3px solid var(--accent)' : '3px solid transparent',
                    transition: 'background 0.15s',
                  }}
                  onMouseEnter={e => { if (!isActive) e.currentTarget.style.background = 'var(--bg-card)' }}
                  onMouseLeave={e => { if (!isActive) e.currentTarget.style.background = 'transparent' }}
                >
                  <div style={{
                    width: '28px', height: '28px', borderRadius: '6px', flexShrink: 0,
                    background: isRunning
                      ? 'rgba(218,119,86,0.10)'
                      : isDone ? 'rgba(76,175,125,0.10)' : 'var(--bg-base)',
                    border: `1px solid ${isRunning
                      ? 'rgba(218,119,86,0.25)'
                      : isDone ? 'rgba(76,175,125,0.25)' : 'var(--border)'}`,
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                  }}>
                    {isRunning
                      ? <Loader size={13} style={{ animation: 'spin 1s linear infinite', color: 'var(--accent)' }} />
                      : <Icon size={13} style={{ color: isDone ? 'var(--success)' : 'var(--text-muted)' }} />}
                  </div>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <p style={{
                      margin: '0 0 1px', fontSize: '12px', fontWeight: '500',
                      color: isActive ? 'var(--accent)' : isDone ? 'var(--text-primary)' : 'var(--text-secondary)',
                      overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                    }}>
                      {dt.label}
                    </p>
                    {isRunning ? (
                      <p style={{
                        margin: 0, fontSize: '10px', color: 'var(--accent)',
                        overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                      }}>
                        {friendlyStage(runningJob.current_stage) || 'Generating…'}
                      </p>
                    ) : isDone ? (
                      <p style={{ margin: 0, fontSize: '10px', color: 'var(--success)' }}>Generated</p>
                    ) : null}
                  </div>
                  <div style={{ flexShrink: 0 }}>
                    {isRunning
                      ? <Loader size={13} style={{ animation: 'spin 1s linear infinite', color: 'var(--accent)' }} />
                      : isDone
                        ? <CheckCircle2 size={13} style={{ color: 'var(--success)' }} />
                        : isActive
                          ? <ChevronRight size={13} style={{ color: 'var(--accent)' }} />
                          : <Circle size={13} style={{ color: 'var(--border)' }} />
                    }
                  </div>
                </div>
              )
            })
          )}
        </div>

        {/* Right — doc panel */}
        <div style={{ flex: 1, overflow: 'hidden', background: 'var(--bg-base)' }}>
          {selectedDocDef && (
            <DocPanel
              key={selectedDoc}   /* remount when doc type changes to get fresh WS */
              changeId={id}
              docDef={selectedDocDef}
              onGenerated={handleDocGenerated}
              autogen={autogenRequested && selectedDoc === requestedDoc}
            />
          )}
        </div>
      </div>

      {genAllOpen && (
        <ProductKitGenerateAll
          changeId={id}
          /* Only regenerate docs that aren't already done, unless none — then all */
          docTypes={(() => {
            const missing = DOC_TYPES.filter(dt => !docMap[dt.key]?.has_content).map(dt => dt.key)
            return missing.length ? missing : DOC_TYPES.map(dt => dt.key)
          })()}
          docLabels={Object.fromEntries(DOC_TYPES.map(dt => [dt.key, dt.label]))}
          totalKitDocs={DOC_TYPES.length}
          /* Done docs OUTSIDE this run only. When every doc is done the run
             regenerates ALL of them — counting them as prior completions too
             double-counts each doc ("16 of 9 complete"). */
          alreadyDoneCount={(() => {
            const done = DOC_TYPES.filter(dt => docMap[dt.key]?.has_content).length
            return done === DOC_TYPES.length ? 0 : done
          })()}
          onClose={() => setGenAllOpen(false)}
          onAllDone={() => {
            // Refresh the list, every per-doc body, and the staleness signal —
            // not just the sidebar list — so the open panel and badges update.
            queryClient.invalidateQueries(['product-kit', id])
            DOC_TYPES.forEach(dt =>
              queryClient.invalidateQueries({ queryKey: ['product-kit-doc', id, dt.key] }))
            queryClient.invalidateQueries({ queryKey: ['artifact-staleness', id] })
          }}
        />
      )}
    </div>
  )
}
