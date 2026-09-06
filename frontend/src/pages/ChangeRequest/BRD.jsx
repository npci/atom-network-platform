// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useState, useRef, useEffect } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { changesApi, agentsApi, docxApi, pptxApi, xlsxApi, docgenApi, evalApi } from '../../services/api'
// R-3 — durable-job-aware wrapper around useAgentWS + the resume banner.
import { useResumableJob } from '../../hooks/useResumableJob'
import ProgressBanner from '../../components/jobs/ProgressBanner'
import DocConsistencyBanner from '../../components/DocConsistencyBanner'
import SkipStepButton from '../../components/common/SkipStepButton'
import EvalStatusPill from '../../components/eval/EvalStatusPill'
import { useAuth } from '../../hooks/useAuth'
import {
  ArrowLeft, ArrowRight, Send, Loader, RefreshCw, Wifi, WifiOff,
  FileText, CheckCircle, XCircle, Clock, RotateCcw, Download, SkipForward,
} from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import ValidationPanel from '../../components/common/ValidationPanel'
import DocumentSourceToggle from '../../components/DocumentSourceToggle'
import ShowArtifactsButton from '../../components/ShowArtifactsButton'
import TranscriptsDownloadButton from '../../components/TranscriptsDownloadButton'
import ReconciliationPanel from '../../components/ReconciliationPanel'
import PlanVersionHistory from '../../components/PlanVersionHistory'
import { useIsDevMode } from '../../hooks/useUiConfig'

// Small reusable DOCX download button
export function DocxDownloadButton({ changeId, docType, subtype, label = 'Download .docx' }) {
  const [downloading, setDownloading] = useState(false)
  const [error, setError] = useState(null)
  const handleClick = async () => {
    setDownloading(true); setError(null)
    try {
      // docxApi.download now returns {blob, filename}. The server's
      // Content-Disposition decides the extension — .docx for BRD/TSD/
      // Canvas/Product Kit, .xsd or .zip for XSD downloads. Falls back
      // to the legacy hardcoded name only when the header is absent
      // (e.g. older backend before the multi-format change).
      const { blob, filename } = await docxApi.download(changeId, docType, subtype)
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = filename || `${docType}${subtype ? '_' + subtype : ''}.docx`
      document.body.appendChild(a); a.click()
      document.body.removeChild(a); URL.revokeObjectURL(url)
    } catch (e) {
      setError(e.response?.data?.detail || 'No file yet')
      setTimeout(() => setError(null), 3000)
    } finally {
      setDownloading(false)
    }
  }
  return (
    <button onClick={handleClick} disabled={downloading} title={error || label}
      style={{
        display: 'flex', alignItems: 'center', gap: '5px',
        padding: '6px 10px', fontSize: '12px', fontWeight: '500',
        background: error ? 'rgba(224,108,108,0.10)' : 'var(--bg-elevated)',
        color: error ? '#e06c6c' : 'var(--text-secondary)',
        border: `1px solid ${error ? 'rgba(224,108,108,0.3)' : 'var(--border)'}`,
        borderRadius: '6px', cursor: downloading ? 'wait' : 'pointer',
        opacity: downloading ? 0.6 : 1,
      }}>
      {downloading ? <Loader size={11} style={{ animation: 'spin 1s linear infinite' }} /> : <Download size={11} />}
      {error || label}
    </button>
  )
}


// D8 — Product Deck companion. Same UX as DocxDownloadButton but hits
// the .pptx route. 404 → "No file yet" inline tooltip; regeneration
// is the recovery path (no on-demand build).
export function PptxDownloadButton({ changeId, docType, subtype, label = '.pptx' }) {
  const [downloading, setDownloading] = useState(false)
  const [error, setError] = useState(null)
  const handleClick = async () => {
    setDownloading(true); setError(null)
    try {
      const { blob, filename } = await pptxApi.download(changeId, docType, subtype)
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = filename || `${docType}${subtype ? '_' + subtype : ''}.pptx`
      document.body.appendChild(a); a.click()
      document.body.removeChild(a); URL.revokeObjectURL(url)
    } catch (e) {
      setError(e.response?.data?.detail || 'No file yet')
      setTimeout(() => setError(null), 3000)
    } finally {
      setDownloading(false)
    }
  }
  return (
    <button onClick={handleClick} disabled={downloading} title={error || label}
      style={{
        display: 'flex', alignItems: 'center', gap: '5px',
        padding: '6px 10px', fontSize: '12px', fontWeight: '500',
        background: error ? 'rgba(224,108,108,0.10)' : 'var(--bg-elevated)',
        color: error ? '#e06c6c' : 'var(--text-secondary)',
        border: `1px solid ${error ? 'rgba(224,108,108,0.3)' : 'var(--border)'}`,
        borderRadius: '6px', cursor: downloading ? 'wait' : 'pointer',
        opacity: downloading ? 0.6 : 1,
      }}>
      {downloading ? <Loader size={11} style={{ animation: 'spin 1s linear infinite' }} /> : <Download size={11} />}
      {error || label}
    </button>
  )
}


// Certification Test Cases Excel workbook download. The Excel Testcase
// Engine writes the file as a job artefact; the API streams the bytes.
// Distinct from docx/pptx because the data path is different (engine
// job_registry attached files vs ProductKitDocument blobs). 404 falls
// back to inline error tooltip — recovery is regenerating cert_test_cases.
export function XlsxDownloadButton({ changeId, label = '.xlsx' }) {
  const [downloading, setDownloading] = useState(false)
  const [error, setError] = useState(null)
  const handleClick = async () => {
    setDownloading(true); setError(null)
    try {
      const blob = await xlsxApi.download(changeId)
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `cert_test_cases.xlsx`
      document.body.appendChild(a); a.click()
      document.body.removeChild(a); URL.revokeObjectURL(url)
    } catch (e) {
      setError(e.response?.data?.detail || 'No file yet')
      setTimeout(() => setError(null), 3000)
    } finally {
      setDownloading(false)
    }
  }
  return (
    <button onClick={handleClick} disabled={downloading} title={error || label}
      style={{
        display: 'flex', alignItems: 'center', gap: '5px',
        padding: '6px 10px', fontSize: '12px', fontWeight: '500',
        background: error ? 'rgba(224,108,108,0.10)' : 'var(--bg-elevated)',
        color: error ? '#e06c6c' : 'var(--text-secondary)',
        border: `1px solid ${error ? 'rgba(224,108,108,0.3)' : 'var(--border)'}`,
        borderRadius: '6px', cursor: downloading ? 'wait' : 'pointer',
        opacity: downloading ? 0.6 : 1,
      }}>
      {downloading ? <Loader size={11} style={{ animation: 'spin 1s linear infinite' }} /> : <Download size={11} />}
      {error || label}
    </button>
  )
}

const ROLE_LABELS = {
  product_manager: 'Product Manager',
  tech_lead:       'Tech Lead',
  infosec_reviewer: 'InfoSec Reviewer',
  risk_reviewer:   'Risk Reviewer',
}

function ApprovalBadge({ status }) {
  const map = {
    pending:  { color: 'var(--text-muted)',  bg: 'var(--bg-elevated)',            border: 'var(--border)',               label: 'Pending',   Icon: Clock },
    approved: { color: 'var(--success)',     bg: 'rgba(76,175,125,0.08)',          border: 'rgba(76,175,125,0.3)',        label: 'Approved',  Icon: CheckCircle },
    rejected: { color: 'var(--danger)',      bg: 'rgba(224,108,108,0.08)',         border: 'rgba(224,108,108,0.3)',       label: 'Rejected',  Icon: XCircle },
  }
  const s = map[status] || map.pending
  const { Icon } = s
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: '4px',
      padding: '2px 10px', borderRadius: '20px', fontSize: '11px', fontWeight: '500',
      color: s.color, background: s.bg, border: `1px solid ${s.border}`,
    }}>
      <Icon size={11} /> {s.label}
    </span>
  )
}

// Persisted BRD version history — every regeneration is saved as its own row
// (GET /brd/versions), so a reviewer can open and read any earlier version. Prior
// versions used to be overwritten in place, so only the latest ever existed; now the
// full history is available. Renders nothing until there is more than one version.
function BRDVersionHistory({ changeId }) {
  const [openVersion, setOpenVersion] = useState(null)
  const { data } = useQuery({
    queryKey: ['brd-versions', changeId],
    queryFn: () => agentsApi.listBRDVersions(changeId).then(r => r.data),
    enabled: !!changeId,
    refetchOnWindowFocus: true,
  })
  const { data: verData } = useQuery({
    queryKey: ['brd-version', changeId, openVersion],
    queryFn: () => agentsApi.getBRDVersion(changeId, openVersion).then(r => r.data),
    enabled: !!changeId && openVersion != null,
  })
  const versions = data?.versions || []
  if (versions.length <= 1) return null
  const latest = versions[0]?.version
  return (
    <div style={{ display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap', marginBottom: '12px' }}>
      <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>Version history:</span>
      {versions.map(v => (
        <button key={v.version} onClick={() => setOpenVersion(v.version)}
          title={[v.status, v.source === 'uploaded' ? 'uploaded' : null, v.created_at ? new Date(v.created_at).toLocaleString() : null].filter(Boolean).join(' \u00b7 ')}
          style={{
            padding: '2px 10px', borderRadius: '20px', fontSize: '11px', cursor: 'pointer',
            background: v.version === latest ? 'var(--accent)' : 'var(--bg-elevated)',
            color: v.version === latest ? 'white' : 'var(--text-muted)',
            border: '1px solid var(--border)',
          }}>
          v{v.version}{v.source === 'uploaded' ? ' \u2924' : ''}
        </button>
      ))}
      {openVersion != null && (
        <div onClick={() => setOpenVersion(null)}
          style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.55)', zIndex: 1000,
            display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '24px' }}>
          <div onClick={e => e.stopPropagation()}
            style={{ background: 'var(--bg-base)', border: '1px solid var(--border)', borderRadius: '8px',
              maxWidth: '860px', width: '100%', maxHeight: '86vh', overflow: 'auto', padding: '24px 28px' }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '12px' }}>
              <h3 style={{ margin: 0, fontSize: '15px', fontWeight: 600, color: 'var(--text-primary)' }}>
                BRD \u2014 version {openVersion}{openVersion === latest ? ' (latest)' : ''}
              </h3>
              <button onClick={() => setOpenVersion(null)}
                style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-muted)' }}>
                <XCircle size={18} />
              </button>
            </div>
            <div className="md-content" style={{ fontSize: '14px', lineHeight: '1.8', color: 'var(--text-primary)' }}>
              <ReactMarkdown>{verData?.content || '_Loading\u2026_'}</ReactMarkdown>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

export default function BRD() {
  const { id } = useParams()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [feedback, setFeedback]       = useState('')
  const [started, setStarted]         = useState(false)
  const [submitting, setSubmitting]   = useState(false)
  const [submitDone, setSubmitDone]   = useState(false)
  const [submitError, setSubmitError] = useState(null)
  const [revising, setRevising]       = useState(false)
  // Create-mode gate: before doing anything, the user chooses to generate or
  // upload the BRD. null = undecided (show the gate), 'generate' = auto-start
  // AI generation, 'upload' = the user is uploading their own.
  const [createMode, setCreateMode]   = useState(null)
  // Optimistic gate: the instant an upload finishes we KNOW a reconciliation is about to
  // run, but the backend 'checking' row (+ its poll) lands ~1–2s later. This bridges that
  // window so the uploaded doc + approval never flash as "ready" first.
  const [awaitingReconcile, setAwaitingReconcile] = useState(false)
  const approvalPanelRef              = useRef(null)
  // Docgen merge (Session 20+) — section-wise edit (Phase G UX). Empty
  // selection ('All sections') falls through to the existing WS revise.
  const [docgenSections, setDocgenSections] = useState([])
  const [editSection, setEditSection]       = useState('')
  const [editingSection, setEditingSection] = useState(false)
  const bottomRef = useRef(null)
  // Poll-budget anchor for the no-reconciliation case (below). This page component
  // is NOT remounted on client-side navigation between change-requests, so reset the
  // anchor when `id` changes — otherwise a warm-navigated change inherits the elapsed
  // budget and its 'checking' panel may never start polling.
  const reconMountedAt = useRef(Date.now())
  useEffect(() => { reconMountedAt.current = Date.now() }, [id])

  const { data: change } = useQuery({
    queryKey: ['change', id],
    queryFn: () => changesApi.get(id).then(r => r.data),
  })

  const { data: brdData, refetch: refetchBrd } = useQuery({
    queryKey: ['brd', id],
    queryFn: () => agentsApi.brd(id).then(r => r.data),
    enabled: !!id,
  })

  // Phase 7 — eval verdict for the BRD (clarification_to_brd checkpoint)
  const { data: gateEval } = useQuery({
    queryKey: ['eval-latest', id, 'clarification_to_brd'],
    queryFn: () => evalApi.latest(id, 'clarification_to_brd').then(r => r.data),
    enabled: !!id,
    refetchInterval: 8000,
    refetchOnWindowFocus: true,
  })

  const isSubmitted = submitDone ||
    ['submitted', 'approved', 'rejected'].includes(brdData?.status)

  const { data: approvalsData, refetch: refetchApprovals } = useQuery({
    queryKey: ['brd-approvals', id],
    queryFn: () => agentsApi.brdApprovals(id).then(r => r.data),
    enabled: isSubmitted,
    refetchInterval: isSubmitted ? 10_000 : false,
  })

  // R-3 — useResumableJob is a drop-in upgrade for useAgentWS that
  // additionally surfaces { jobId, jobStage, jobProgress, jobStartedAt,
  // jobStartedBy, isResuming, cancelJob }. Existing fields keep the
  // same names so the rest of the page is unchanged.
  const {
    messages, streaming, streamingText,
    connected, historyLoaded, error, validation, sendMessage,
    docgenJobId,
    // R-3 additions:
    jobId, jobStatus, jobStage, jobProgress, jobStartedAt, jobStartedBy,
    isResuming, cancelJob, docConsistency,
  } = useResumableJob(id, 'brd')

  // For the resume banner's "started by another user" attribution check.
  const { user: me } = useAuth()

  const assistantMsgs   = messages.filter(m => m.role === 'assistant')
  const currentBrd      = assistantMsgs[assistantMsgs.length - 1]?.content || null
  // Generate-or-Upload — when the persisted BRD was uploaded, show its content
  // and suppress generation. The uploaded row has no WS assistant message, so
  // fall back to the persisted content.
  const isUploaded      = brdData?.source === 'uploaded'
  // When the latest persisted BRD is an upload, it is the source of truth — show
  // its content and ignore any stale generated text still held in the WS history.
  const brdContent      = streaming
    ? (streamingText || '')
    : (isUploaded ? (brdData?.content || '') : (currentBrd || ''))

  // Uploaded-doc ↔ plan reconciliation. The BRD is NOT final while this is open:
  //   'pending'  → conflicts to resolve (panel shown, approval gated)
  //   'applying' → resolved, the corrected BRD is regenerating (progress shown, gated)
  // Approval/download stay blocked (server 409s too) until it clears. Shares the query
  // key with ReconciliationPanel, so there's no extra fetch. Keeps polling while
  // 'applying' so the page advances to the regenerated BRD the moment it's ready.
  const { data: reconData } = useQuery({
    queryKey: ['reconciliation', id, 'brd'],
    queryFn: () => agentsApi.getReconciliation(id, 'brd').then(r => r.data),
    enabled: !!id && isUploaded && !submitDone,
    refetchInterval: (q) => {
      const d = q?.state?.data
      if (d?.exists && d.status !== 'applying') return false   // pending → the user's turn
      if (d?.exists) return 3000                                // applying → moderate
      if (d?.grounding_summary) return false                    // resolved with code-check findings → terminal
      // No reconciliation ever surfaced and none is imminent — stop after a couple
      // minutes instead of polling every 2s for the lifetime of the tab.
      if (Date.now() - reconMountedAt.current > 120000) return false
      return 2000                                              // waiting for 'checking' to surface → poll fast
    },
  })
  const reconOpen      = !!reconData?.exists                   // checking / pending / applying → doc NOT final
  const reconApplying  = reconData?.regenerating === true      // resolved, doc regenerating (for the tooltip)
  // Doc is not final while a reconciliation is open OR one is imminent (just uploaded).
  const docNotFinal    = reconOpen || awaitingReconcile
  // §8.1 soft gate: an accepted change the code check flagged as overturning a ratified
  // decision blocks approval until it's acknowledged in the code-check panel.
  const overturnsBlocks = !!(reconData?.grounding_summary?.overturns && !reconData?.grounding_summary?.acknowledged)
  const approvalBlocked = docNotFinal || overturnsBlocks

  // Release the optimistic gate once the real reconciliation state lands (reconData.exists),
  // or — for a clean upload that surfaces no reconciliation — after a short safety timeout.
  useEffect(() => {
    if (!awaitingReconcile) return
    if (reconData?.exists) { setAwaitingReconcile(false); return }
    const t = setTimeout(() => setAwaitingReconcile(false), 12000)
    return () => clearTimeout(t)
  }, [awaitingReconcile, reconData?.exists])

  // When regeneration finishes (applying → cleared), pull the corrected BRD so the
  // page swaps the stale content for the new version.
  const prevApplyingRef = useRef(false)
  useEffect(() => {
    if (prevApplyingRef.current && !reconApplying) {
      refetchBrd()
      qc.invalidateQueries({ queryKey: ['brd', id] })
      qc.invalidateQueries({ queryKey: ['artifact-staleness', id] })
    }
    prevApplyingRef.current = reconApplying
  }, [reconApplying, refetchBrd, qc, id])

  useEffect(() => {
    if (streaming) bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [streaming, streamingText])

  useEffect(() => {
    if (currentBrd) setStarted(true)
  }, [currentBrd])

  // Sync submitDone from persisted brd status
  useEffect(() => {
    if (['submitted', 'approved', 'rejected'].includes(brdData?.status)) {
      setSubmitDone(true)
    }
  }, [brdData])

  // Refetch BRD when streaming completes — the BRD row is persisted by the
  // backend during generation, but the brdData query is only fetched once on
  // mount. Without this refetch brdData.id stays undefined, and the Submit
  // handler's guard silently returns when the user clicks the button.
  const prevStreamingRef = useRef(streaming)
  useEffect(() => {
    if (prevStreamingRef.current && !streaming) {
      refetchBrd()
      // A new BRD version changes downstream staleness — refresh the signal.
      qc.invalidateQueries({ queryKey: ['artifact-staleness', id] })
      // The just-generated version is a new persisted row — refresh the history list.
      qc.invalidateQueries({ queryKey: ['brd-versions', id] })
    }
    prevStreamingRef.current = streaming
  }, [streaming, refetchBrd, qc, id])

  const handleStart = () => {
    setStarted(true)
    sendMessage('start')
  }

  // Auto-start BRD generation once the user arrives here from Clarification
  // ("Proceed to BRD"). This generates the document; submission to approvers
  // is still a manual user action via the existing Submit button below.
  const autoStartedRef = useRef(false)
  const isFirstBrd = assistantMsgs.length === 0
  // Generation only starts once the user explicitly chooses "Generate" in the
  // create-mode gate (createMode === 'generate'). No more auto-start on load.
  useEffect(() => {
    if (
      !autoStartedRef.current &&
      createMode === 'generate' &&
      historyLoaded &&
      isFirstBrd &&
      !streaming &&
      !started &&
      connected &&
      brdData !== undefined && !isUploaded
    ) {
      autoStartedRef.current = true
      handleStart()
    }
  }, [createMode, historyLoaded, isFirstBrd, streaming, started, connected, brdData, isUploaded])

  // Whether a BRD already exists (generated this session or persisted/uploaded).
  const hasExistingBrd = Boolean(brdData?.content) || assistantMsgs.length > 0
  // Show the choose-how gate only when there's nothing yet and no run in flight.
  const showChoiceGate =
    brdData !== undefined && !hasExistingBrd && !isUploaded &&
    !streaming && !started && !isResuming && !jobId && createMode === null

  const handleFeedback = async () => {
    const text = feedback.trim()
    if (!text || streaming || editingSection) return

    // Docgen merge (Session 20+) — Phase G section-wise edit. When the user
    // has picked a specific section AND a docgen job exists for this BRD,
    // route to docgenApi.editSection (fast — regenerates one section only).
    // Otherwise fall through to the existing WS revise (full-doc).
    if (editSection && docgenJobId) {
      setEditingSection(true)
      setFeedback('')
      try {
        await docgenApi.editSection(id, 'BRD', editSection, text)
        await refetchBrd()
      } catch (err) {
        console.error('section edit failed', err)
        alert(err.response?.data?.detail || 'Section edit failed')
      } finally {
        setEditingSection(false)
      }
      return
    }

    setFeedback('')
    sendMessage(text)
  }

  // Docgen merge (Session 20+) — fetch the section list whenever a docgen
  // job completes for this BRD. Empty list → dropdown stays hidden →
  // legacy WS revise path remains the only option.
  useEffect(() => {
    if (!streaming && docgenJobId && id) {
      docgenApi.sections(id, 'BRD')
        .then(r => setDocgenSections(r.data?.sections || []))
        .catch(() => setDocgenSections([]))
    }
  }, [streaming, docgenJobId, id])

  const handleSubmit = async () => {
    setSubmitError(null)
    setSubmitting(true)
    try {
      // brdData may be stale if the user clicks immediately after streaming
      // completes (race with refetchBrd from the streaming-end effect). Try
      // one inline refetch before giving up.
      let brdId = brdData?.id
      if (!brdId) {
        const r = await refetchBrd()
        brdId = r.data?.id
      }
      if (!brdId) {
        throw new Error('BRD is still being generated — wait for it to finish, then retry.')
      }
      await agentsApi.submitBRD(id, brdId)
      setSubmitDone(true)
      // With backend dev_skip_approvals the submit auto-approves the BRD row
      // without creating approval rows — refetch the BRD so brdApproved (and
      // the Continue button) reflect that immediately.
      await Promise.all([refetchApprovals(), refetchBrd()])
      requestAnimationFrame(() => {
        approvalPanelRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
      })
    } catch (err) {
      setSubmitError(err.response?.data?.detail || err.message || 'Submit failed')
      console.error('submit failed', err)
    } finally {
      setSubmitting(false)
    }
  }

  const handleAutoRevise = () => {
    if (!approvalsData?.approvals) return
    const rejections = approvalsData.approvals.filter(a => a.status === 'rejected')
    if (rejections.length === 0) return
    setRevising(true)
    setSubmitDone(false)
    sendMessage('__auto_revise__')
    setTimeout(() => setRevising(false), 2000)
  }

  const approvals     = approvalsData?.approvals || []
  // Dev skip-approval leaves no approval rows — the BRD row itself is APPROVED.
  const brdApproved   = brdData?.status === 'approved'
  const allApproved   = brdApproved ||
    (approvals.length > 0 && approvals.every(a => a.status === 'approved'))
  const anyRejected   = approvals.some(a => a.status === 'rejected')

  // Dev-mode fast-path: skip the 4-stakeholder BRD approval entirely.
  const isDev = useIsDevMode()
  const [devSkipping, setDevSkipping] = useState(false)
  const handleDevSkipApproval = async () => {
    if (devSkipping) return
    setDevSkipping(true)
    setSubmitError(null)
    try {
      // Ensure the BRD row exists (race with streaming-end refetch).
      let brdId = brdData?.id
      if (!brdId) brdId = (await refetchBrd()).data?.id
      if (!brdId) throw new Error('BRD is still being generated — wait for it to finish, then retry.')
      await agentsApi.devAutoApproveBRD(id)
      // workflow_version 2 reorders BRD → XSD → TSD, so after the BRD the next
      // stage is XSD, not Tech Spec. v1 keeps the old BRD → Tech Spec order.
      const nextStage = (change?.workflow_version ?? 2) >= 2 ? 'xsd' : 'tech_spec'
      navigate(`/changes/${id}/${nextStage}`)
    } catch (err) {
      setSubmitError(err.response?.data?.detail || err.message || 'Skip approval failed')
      setDevSkipping(false)
    }
  }
  const pendingCount  = approvals.filter(a => a.status === 'pending').length

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: 'calc(100vh - 0px)', overflow: 'hidden' }}>

      {/* Header — wraps on narrow viewports so the title isn't crushed and
          the action buttons flow onto a second row instead of overflowing. */}
      <div style={{
        padding: '16px 24px', borderBottom: '1px solid var(--border)',
        display: 'flex', alignItems: 'center', gap: '10px 16px', flexShrink: 0,
        flexWrap: 'wrap',
        background: 'var(--bg-base)',
      }}>
        <button
          onClick={() => navigate(`/changes/${id}`)}
          style={{
            display: 'flex', alignItems: 'center', gap: '5px',
            background: 'none', border: 'none', cursor: 'pointer',
            fontSize: '13px', color: 'var(--text-muted)',
          }}
        >
          <ArrowLeft size={14} /> Back
        </button>
        <div style={{ flex: '1 1 auto', minWidth: '220px' }}>
          <h1 style={{ margin: 0, fontSize: '16px', fontWeight: '600', color: 'var(--text-primary)' }}>
            Business Requirement Document
          </h1>
          <p style={{ margin: 0, fontSize: '11px', color: 'var(--text-muted)' }}>
            Full BRD · Multi-stakeholder approval workflow
          </p>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
          {connected
            ? <Wifi size={14} style={{ color: 'var(--success)' }} />
            : <WifiOff size={14} style={{ color: 'var(--danger)' }} />}
          <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>
            {connected ? 'Connected' : 'Connecting…'}
          </span>
        </div>

        <ShowArtifactsButton changeId={id} />
        <TranscriptsDownloadButton changeId={id} section="brd" label="Transcripts" />
        <EvalStatusPill
          verdict={gateEval?.verdict}
          checkpointLabel="Clarification → BRD"
          changeId={id}
          checkpointId="clarification_to_brd"
          onAutoFix={(text) => sendMessage(text)}
        />

        <SkipStepButton changeId={id} nextRoute={`/changes/${id}`} />

        {/* Generate-or-Upload — stays visible during generation so the user can
            interrupt by uploading. Uploading mid-stream cancels the run first. */}
        {!submitDone && (
          <DocumentSourceToggle
            changeId={id}
            docType="brd"
            docLabel="BRD"
            source={streaming ? 'generated' : brdData?.source}
            originalFilename={brdData?.original_filename}
            uploadedAt={brdData?.uploaded_at}
            confirmReplace={hasExistingBrd || streaming}
            canRevert={!streaming && brdData?.has_generated_version}
            onGenerateInstead={streaming ? undefined : (() => { setCreateMode('generate'); handleStart() })}
            onBeforeUpload={async () => { if (streaming) { try { await cancelJob?.() } catch { /* already finished or gone; upload proceeds either way */ } } }}
            onUploaded={async () => { setAwaitingReconcile(true); setStarted(true); await refetchBrd(); await qc.invalidateQueries({ queryKey: ['brd', id] }); qc.invalidateQueries({ queryKey: ['artifact-staleness', id] }); qc.invalidateQueries({ queryKey: ['reconciliation', id, 'brd'] }) }}
          />
        )}

        {/* Plan version history — self-hides until a reconciliation re-versioned the plan. */}
        <PlanVersionHistory changeId={id} />

        {/* Header action buttons. In dev mode "Skip BRD Approval" renders as
            an ADDITIONAL fast-path next to the normal Submit-for-Approval
            flow — it must not replace it. */}
        {(currentBrd || isUploaded) && !streaming && !submitDone && isDev && (
          <button
            onClick={handleDevSkipApproval}
            disabled={devSkipping || !brdData?.id || approvalBlocked}
            title={overturnsBlocks
              ? 'A change overturns a ratified decision — acknowledge it in the code-check panel below first'
              : docNotFinal
              ? (reconApplying ? 'Regenerating the BRD from your resolutions — hold on…'
                 : awaitingReconcile ? 'Checking your BRD against the ratified plan…'
                 : 'Resolve the reconciliation conflicts below before approving')
              : 'Dev-only: approve the BRD and advance to Tech Spec without reviewer sign-off'}
            style={{
              display: 'inline-flex', alignItems: 'center', gap: '8px',
              padding: '8px 18px', background: 'transparent', color: '#d97706',
              border: '1px dashed #d97706', borderRadius: '6px', fontSize: '13px',
              fontWeight: '600',
              cursor: (devSkipping || !brdData?.id || approvalBlocked) ? 'not-allowed' : 'pointer',
              opacity: (devSkipping || !brdData?.id || approvalBlocked) ? 0.6 : 1,
            }}
          >
            {devSkipping
              ? <Loader size={13} style={{ animation: 'spin 1s linear infinite' }} />
              : <SkipForward size={14} />}
            Skip BRD Approval
          </button>
        )}
        {(currentBrd || isUploaded) && !streaming && !submitDone && (
          <button
            onClick={handleSubmit}
            disabled={submitting || !brdData?.id || approvalBlocked}
            title={overturnsBlocks
              ? 'A change overturns a ratified decision — acknowledge it in the code-check panel below first'
              : docNotFinal
              ? (reconApplying ? 'Regenerating the BRD from your resolutions — hold on…'
                 : awaitingReconcile ? 'Checking your BRD against the ratified plan…'
                 : 'Resolve the reconciliation conflicts below before submitting for approval')
              : (!brdData?.id ? 'BRD must finish generating before it can be submitted' : '')}
            style={{
              display: 'flex', alignItems: 'center', gap: '8px',
              padding: '8px 18px', background: 'var(--accent)', color: 'white',
              border: 'none', borderRadius: '6px', fontSize: '13px',
              fontWeight: '600',
              cursor: (submitting || !brdData?.id || approvalBlocked) ? 'not-allowed' : 'pointer',
              opacity: (submitting || !brdData?.id || approvalBlocked) ? 0.6 : 1,
            }}
          >
            {submitting
              ? <Loader size={13} style={{ animation: 'spin 1s linear infinite' }} />
              : <FileText size={14} />}
            Submit for Approval
          </button>
        )}

        {allApproved && (
          <button
            onClick={() => navigate(`/changes/${id}`)}
            style={{
              display: 'flex', alignItems: 'center', gap: '8px',
              padding: '8px 18px', background: 'var(--success)', color: 'white',
              border: 'none', borderRadius: '6px', fontSize: '13px', fontWeight: '600', cursor: 'pointer',
            }}
          >
            Continue to {(change?.workflow_version ?? 2) >= 2 ? 'XSD' : 'Tech Spec'} <ArrowRight size={14} />
          </button>
        )}

        {anyRejected && !streaming && (
          <button
            onClick={handleAutoRevise}
            disabled={revising}
            style={{
              display: 'flex', alignItems: 'center', gap: '8px',
              padding: '8px 18px', background: 'rgba(218,119,86,0.12)',
              color: 'var(--accent)', border: '1px solid rgba(218,119,86,0.3)',
              borderRadius: '6px', fontSize: '13px', fontWeight: '600',
              cursor: revising ? 'not-allowed' : 'pointer',
            }}
          >
            <RotateCcw size={13} /> Auto-Revise from Feedback
          </button>
        )}
      </div>

      {/* Inline submit error — surfaces what the previous silent console.error
          hid (e.g. BRD not ready, backend 4xx/5xx, network failure). Clears
          on the next click via setSubmitError(null) at the top of handleSubmit. */}
      {submitError && (
        <div style={{
          padding: '10px 24px',
          background: 'rgba(224,108,108,0.08)',
          borderBottom: '1px solid rgba(224,108,108,0.25)',
          color: 'var(--danger)',
          fontSize: '12px',
          fontWeight: 500,
          flexShrink: 0,
        }}>
          {submitError}
        </div>
      )}

      {/* Main scrollable area */}
      <div style={{ flex: 1, overflowY: 'auto', padding: '24px' }}>
        {/* Uploaded-BRD ↔ plan reconciliation conflicts — lives here (not the fixed
            header) so a long conflict list scrolls with the page. Self-hides until
            the background check surfaces conflicts. */}
        {isUploaded && <ReconciliationPanel key={id} changeId={id} />}

        {error && (
          <div style={{
            padding: '12px 16px', borderRadius: '8px', marginBottom: '16px',
            background: 'rgba(224,108,108,0.10)', border: '1px solid rgba(224,108,108,0.3)',
          }}>
            <p style={{ margin: '0 0 4px', fontSize: '13px', fontWeight: '600', color: 'var(--danger)' }}>Error</p>
            <p style={{ margin: 0, fontSize: '12px', color: 'var(--text-secondary)' }}>{error}</p>
            {!connected && (
              <button onClick={() => window.location.reload()} style={{
                marginTop: '10px', padding: '6px 14px', background: 'var(--accent)', color: 'white',
                border: 'none', borderRadius: '6px', fontSize: '12px', fontWeight: '600', cursor: 'pointer',
              }}>Retry</button>
            )}
          </div>
        )}

        {/* Approval status panel — shown after submission. Renders even when
            approvals[] is still loading so the page isn't blank between
            setSubmitDone(true) and the first refetchApprovals() response. */}
        {submitDone && (
          <div ref={approvalPanelRef} style={{
            background: 'var(--bg-elevated)', border: '1px solid var(--border)',
            borderRadius: '10px', padding: '20px 24px', marginBottom: '24px',
          }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '16px' }}>
              <h3 style={{ margin: 0, fontSize: '13px', fontWeight: '600', color: 'var(--text-primary)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
                Approval Status
              </h3>
              <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
                {pendingCount > 0 && (
                  <span style={{ fontSize: '12px', color: 'var(--text-muted)' }}>
                    {pendingCount} awaiting review
                  </span>
                )}
                {allApproved && (
                  <span style={{
                    fontSize: '12px', padding: '3px 12px', borderRadius: '20px',
                    background: 'rgba(76,175,125,0.12)', color: 'var(--success)',
                    border: '1px solid rgba(76,175,125,0.3)', fontWeight: '600',
                  }}>
                    All Approved
                  </span>
                )}
                {anyRejected && !allApproved && (
                  <span style={{
                    fontSize: '12px', padding: '3px 12px', borderRadius: '20px',
                    background: 'rgba(224,108,108,0.08)', color: 'var(--danger)',
                    border: '1px solid rgba(224,108,108,0.3)', fontWeight: '600',
                  }}>
                    Changes Requested
                  </span>
                )}
              </div>
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
              {approvals.length === 0 && !brdApproved && (
                <div style={{
                  display: 'flex', alignItems: 'center', gap: '8px',
                  padding: '12px 16px', borderRadius: '8px',
                  background: 'var(--bg-card)', border: '1px solid var(--border-subtle)',
                  color: 'var(--text-muted)', fontSize: '12px',
                }}>
                  <Loader size={12} style={{ animation: 'spin 1s linear infinite' }} />
                  Loading approvers…
                </div>
              )}
              {approvals.length === 0 && brdApproved && (
                <div style={{
                  display: 'flex', alignItems: 'center', gap: '8px',
                  padding: '12px 16px', borderRadius: '8px',
                  background: 'var(--bg-card)', border: '1px solid var(--border-subtle)',
                  color: 'var(--success)', fontSize: '12px',
                }}>
                  <CheckCircle size={12} />
                  Approved without reviewer sign-off (dev skip-approvals)
                </div>
              )}
              {approvals.map(a => (
                <div key={a.id} style={{
                  padding: '12px 16px', borderRadius: '8px',
                  background: 'var(--bg-card)', border: '1px solid var(--border-subtle)',
                }}>
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: a.comments ? '8px' : 0 }}>
                    <div>
                      <span style={{ fontSize: '13px', fontWeight: '500', color: 'var(--text-primary)' }}>
                        {a.reviewer_name || a.reviewer_id}
                        {(ROLE_LABELS[a.reviewer_role] || a.reviewer_role) && (
                          <span style={{ fontSize: '12px', fontWeight: 400, color: 'var(--text-muted)', marginLeft: '6px' }}>
                            ({ROLE_LABELS[a.reviewer_role] || a.reviewer_role})
                          </span>
                        )}
                      </span>
                    </div>
                    <ApprovalBadge status={a.status} />
                  </div>
                  {a.comments && (
                    <p style={{
                      margin: 0, fontSize: '12px', color: 'var(--text-secondary)',
                      lineHeight: '1.5', padding: '8px 12px',
                      background: 'var(--bg-base)', borderRadius: '6px',
                      borderLeft: '3px solid var(--border)',
                    }}>
                      {a.comments}
                    </p>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Create-mode gate — choose to generate or upload before anything runs */}
        {showChoiceGate && (
          <div style={{
            textAlign: 'center', padding: '56px 32px',
            background: 'var(--bg-elevated)', border: '1px solid var(--border)',
            borderRadius: '12px',
          }}>
            <FileText size={40} style={{ color: 'var(--accent)', marginBottom: '16px' }} />
            <h2 style={{ margin: '0 0 8px', fontSize: '18px', fontWeight: '600', color: 'var(--text-primary)' }}>
              How would you like to create the BRD?
            </h2>
            <p style={{ margin: '0 auto 24px', fontSize: '13px', color: 'var(--text-muted)', maxWidth: '460px' }}>
              Generate a full Business Requirement Document with AI from the research, product canvas
              and clarifications — or upload your own existing BRD to use in its place.
            </p>
            <div style={{ display: 'flex', gap: '12px', justifyContent: 'center', alignItems: 'center', flexWrap: 'wrap' }}>
              <button
                onClick={() => setCreateMode('generate')}
                disabled={!connected}
                style={{
                  display: 'inline-flex', alignItems: 'center', gap: '8px',
                  padding: '10px 22px', background: 'var(--accent)', color: 'white',
                  border: 'none', borderRadius: '8px', fontSize: '14px', fontWeight: '600',
                  cursor: connected ? 'pointer' : 'not-allowed', opacity: connected ? 1 : 0.5,
                }}
              >
                <Send size={14} /> Generate with AI
              </button>
              <DocumentSourceToggle
                changeId={id}
                docType="brd"
                source={brdData?.source}
                label="Upload a document"
                onUploaded={async () => { setAwaitingReconcile(true); setStarted(true); await refetchBrd(); qc.invalidateQueries({ queryKey: ['artifact-staleness', id] }); qc.invalidateQueries({ queryKey: ['reconciliation', id, 'brd'] }) }}
              />
            </div>
            {!connected && (
              <p style={{ marginTop: '14px', fontSize: '11px', color: 'var(--text-muted)' }}>Connecting…</p>
            )}
          </div>
        )}

        {/* Preparing — shown once the user chooses Generate, until the stream starts. Also
            shown on RE-MOUNT mid-generation (user switched tabs and came back): createMode is
            local and resets, but jobStatus/isResuming come from the durable job, so the loader
            survives navigation instead of falsely reverting to the idle "click Generate" state. */}
        {(createMode === 'generate' || jobStatus === 'running' || isResuming) && !streaming && !brdContent && historyLoaded && (
          <div style={{
            textAlign: 'center', padding: '64px 32px',
            background: 'var(--bg-elevated)', border: '1px solid var(--border)',
            borderRadius: '12px',
          }}>
            <FileText size={40} style={{ color: 'var(--accent)', marginBottom: '16px' }} />
            <h2 style={{ margin: '0 0 8px', fontSize: '18px', fontWeight: '600', color: 'var(--text-primary)' }}>
              Preparing BRD…
            </h2>
            <p style={{ margin: '0 auto 24px', fontSize: '13px', color: 'var(--text-muted)', maxWidth: '440px' }}>
              The AI will produce a full 14-section Business Requirement Document from the enriched
              prompt, research report, and approved product canvas — ready for multi-stakeholder review.
            </p>
            <div style={{ display: 'inline-flex', alignItems: 'center', gap: '10px', color: 'var(--accent)' }}>
              <Loader size={16} style={{ animation: 'spin 1s linear infinite' }} />
              <span style={{ fontSize: '13px', fontWeight: '600' }}>
                {connected ? 'Generating…' : 'Connecting…'}
              </span>
            </div>
          </div>
        )}

        {/* R-3 — resume banner. Renders only when a durable job is active.
            On a fresh first-mount it appears alongside the streaming text;
            on a re-mount mid-job (user navigated away, came back) it appears
            BEFORE the chunks have replayed and disappears once the job
            completes. Cancel link is wired to JobsContext.cancelJob via
            useResumableJob. */}
        <ProgressBanner
          jobId={jobId}
          status={jobStatus}
          stage={jobStage}
          progress={jobProgress}
          startedAt={jobStartedAt}
          startedBy={jobStartedBy}
          resuming={isResuming}
          onCancel={cancelJob}
          currentUserId={me?.id}
        />

        {/* Validation panel — shown after generation completes */}
        {!submitDone && !streaming && validation && <ValidationPanel validation={validation} />}

        {/* Download DOCX — only once the doc is FINAL: generation complete, and (for an
            upload) no open reconciliation. Hidden once submitted. */}
        {!submitDone && !streaming && brdContent && !docNotFinal && (
          <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: '12px' }}>
            <DocxDownloadButton changeId={id} docType="brd" label="Download BRD (.docx)" />
          </div>
        )}

        {/* Plan-fidelity gate — flags if the BRD invented wire APIs/schemas the plan doesn't have. */}
        {!submitDone && !streaming && brdContent && !docNotFinal && <DocConsistencyBanner result={docConsistency} docLabel="BRD" />}

        {/* Persisted version history — open any prior BRD version (read-only). Also gated on
            !docNotFinal: while a reconciliation is open the BRD is not yet final, so its
            version list must stay hidden along with the document itself. */}
        {!submitDone && !streaming && !docNotFinal && <BRDVersionHistory changeId={id} />}

        {/* BRD document — hidden once submitted, and for the WHOLE time a reconciliation
            is open (checking → conflicts → regenerating): the uploaded/not-yet-reconciled
            markdown must not be shown until the final BRD is generated. */}
        {!submitDone && !docNotFinal && (streaming || brdContent) && (
          <div style={{
            background: 'var(--bg-elevated)', border: '1px solid var(--border)',
            borderRadius: '8px', padding: '28px 32px',
          }}>
            {streaming && (
              <div style={{
                display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '16px',
                padding: '8px 14px', background: 'rgba(218,119,86,0.08)',
                border: '1px solid rgba(218,119,86,0.2)', borderRadius: '6px',
              }}>
                <Loader size={14} style={{ color: 'var(--accent)', animation: 'spin 1s linear infinite' }} />
                <span style={{ fontSize: '12px', color: 'var(--accent)' }}>Generating BRD…</span>
              </div>
            )}
            <div className="md-content" style={{ fontSize: '14px', lineHeight: '1.8', color: 'var(--text-primary)' }}>
              <ReactMarkdown>{brdContent + (streaming ? '▌' : '')}</ReactMarkdown>
            </div>
            <div ref={bottomRef} />
          </div>
        )}

        {/* Revision pills — hidden while streaming (or during the docgen
            replay window that keeps `streaming` truthy after WS done)
            so the newly-bumped v{N+1} chip doesn't appear alongside
            the "Generating…" spinner. */}
        {!submitDone && !streaming && assistantMsgs.length > 1 && (
          <div style={{ display: 'flex', gap: '8px', marginTop: '16px', flexWrap: 'wrap' }}>
            <span style={{ fontSize: '11px', color: 'var(--text-muted)', alignSelf: 'center' }}>Revisions:</span>
            {assistantMsgs.map((_, i) => (
              <span key={i} style={{
                padding: '2px 10px', borderRadius: '20px', fontSize: '11px',
                background: i === assistantMsgs.length - 1 ? 'var(--accent)' : 'var(--bg-elevated)',
                color: i === assistantMsgs.length - 1 ? 'white' : 'var(--text-muted)',
                border: '1px solid var(--border)',
              }}>v{i + 1}</span>
            ))}
          </div>
        )}
      </div>

      {/* Feedback bar — only before submission */}
      {(currentBrd || started) && !streaming && !submitDone && (
        <div style={{
          padding: '16px 24px', borderTop: '1px solid var(--border)',
          background: 'var(--bg-base)', flexShrink: 0,
        }}>
          <p style={{ margin: '0 0 10px', fontSize: '12px', color: 'var(--text-muted)', fontWeight: '500' }}>
            {currentBrd
              ? 'Provide feedback to refine the BRD, then submit for stakeholder approval above.'
              : 'BRD is generating…'}
          </p>
          <div style={{ display: 'flex', gap: '10px' }}>
            {/* Docgen merge (Session 20+) — section-wise edit dropdown.
                Empty 'All sections' falls through to the WS full-doc revise.
                Only renders when a docgen job exists AND has section list. */}
            {docgenSections.length > 0 && (
              <select
                value={editSection}
                onChange={e => setEditSection(e.target.value)}
                disabled={streaming || editingSection}
                title="Edit one section (fast) — or 'All sections' for full revise"
                style={{
                  padding: '9px 10px',
                  background: 'var(--bg-input)', border: '1px solid var(--border)',
                  borderRadius: '6px', color: 'var(--text-primary)',
                  fontSize: '13px', outline: 'none', maxWidth: '180px',
                }}
              >
                <option value="">All sections</option>
                {docgenSections.map(h => (
                  <option key={h} value={h}>{h}</option>
                ))}
              </select>
            )}
            <input
              value={feedback}
              onChange={e => setFeedback(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') handleFeedback() }}
              placeholder={editSection
                ? `Edit only "${editSection}" — describe the change…`
                : 'e.g. Strengthen the security section with the network fraud controls…'}
              disabled={editingSection}
              style={{
                flex: 1, padding: '9px 14px',
                background: 'var(--bg-input)', border: '1px solid var(--border)',
                borderRadius: '6px', color: 'var(--text-primary)', fontSize: '13px', outline: 'none',
              }}
              onFocus={e => e.target.style.borderColor = 'var(--accent)'}
              onBlur={e => e.target.style.borderColor = 'var(--border)'}
            />
            <button
              onClick={handleFeedback}
              disabled={!feedback.trim() || streaming || editingSection || !connected}
              style={{
                display: 'flex', alignItems: 'center', gap: '6px',
                padding: '9px 16px', background: 'var(--bg-elevated)',
                border: '1px solid var(--border)', borderRadius: '6px',
                color: 'var(--text-secondary)', fontSize: '13px', fontWeight: '500',
                cursor: (!feedback.trim() || streaming || editingSection || !connected) ? 'not-allowed' : 'pointer',
                opacity: (!feedback.trim() || streaming || editingSection || !connected) ? 0.5 : 1,
              }}
            >
              <RefreshCw size={13} /> {editingSection ? 'Editing…' : 'Refine'}
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
