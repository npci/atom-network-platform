// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useState, useRef, useEffect } from 'react'
import { t } from '../../strings'
import { useParams, useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import api, { agentsApi, evalApi, changesApi, phaseBApi, agenticApi } from '../../services/api'
import AgenticPhasePanel from '../../components/AgenticPhasePanel'
// R-4 — durable-job-aware wrapper + resume banner.
import { useResumableJob } from '../../hooks/useResumableJob'
import ProgressBanner from '../../components/jobs/ProgressBanner'
import SkipStepButton from '../../components/common/SkipStepButton'
import { useAuth } from '../../hooks/useAuth'
import { useRepoRoles } from '../../hooks/useUiConfig'
import { validateSelection, defaultSelection, UNCONFIGURED_TOPOLOGY_NOTICE } from '../../utils/repoTopology'
import { useGateModal } from '../../context/useGateModal'
import { isEvalGateError } from '../../lib/evalGate'
import EvalStatusPill from '../../components/eval/EvalStatusPill'
import {
  ArrowLeft, ArrowRight, Loader, RefreshCw, Wifi, WifiOff,
  FileCode, CheckCircle, XCircle, AlertTriangle, Download,
} from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import ValidationPanel from '../../components/common/ValidationPanel'
import { DocxDownloadButton } from './BRD'
import ShowArtifactsButton from '../../components/ShowArtifactsButton'
import TranscriptsDownloadButton from '../../components/TranscriptsDownloadButton'
import StalenessBadge from '../../components/StalenessBadge'

export default function XSD() {
  const { id } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [feedback, setFeedback]       = useState('')
  const [assessing, setAssessing]     = useState(false)
  const [assessment, setAssessment]   = useState(null)   // { text, is_required }
  const [started, setStarted]         = useState(false)
  const [advancing, setAdvancing]     = useState(false)
  const [_xsdApproved, setXsdApproved] = useState(false)   // agentic Phase-A approved
  const bottomRef = useRef(null)
  const proceededRef = useRef(false)   // hard idempotency guard for stage-advance
  const { user: me } = useAuth()
  // Agentic codegen is admin + tech-lead only — others get the legacy XSD path.
  const agenticAllowed = me?.role === 'admin' || me?.role === 'tech_lead'

  // Agentic Phase A (THE BOOK v3.4): when the change opts into the agentic engine,
  // the XSD stage drives a repo-editing XSD run (reviewed as a git diff) instead of
  // the legacy streamed-document generation.
  const { data: change } = useQuery({
    queryKey: ['change', id],
    queryFn: () => changesApi.get(id).then(r => r.data),
    enabled: !!id,
  })
  // Agentic is the ONLY codegen path for admin/tech-lead — no per-change opt-in.
  const agenticEnabled = agenticAllowed
  const { data: gitRepos } = useQuery({
    queryKey: ['phase-b-repos', id],
    queryFn: () => phaseBApi.listGitRepos(id).then(r => r.data),
    enabled: !!id && agenticEnabled,
  })
  const repoList = Array.isArray(gitRepos) ? gitRepos : []
  // Carry the repo selection made at PLANNING (the Change-Analysis run) through to the
  // XSD stage, so the schema work runs against the exact repos the plan was grounded in
  // — instead of re-defaulting here (which let a wrong/no-role repo be picked).
  const { data: analysisRunsData, isSuccess: analysisLoaded } = useQuery({
    queryKey: ['analysis-runs', id],
    queryFn: () => agenticApi.listChangeRuns(id, 'analysis').then(r => r.data),
    enabled: !!id && agenticEnabled,
  })
  const planningRepoIds = analysisRunsData?.runs?.[0]?.selected_repo_ids || []
  // The topology the run must span is declared by the active domain pack, not
  // assumed to be UPI's core+app pair. See utils/repoTopology.js.
  const repoRoles = useRepoRoles()
  const [selectedRepoIds, setSelectedRepoIds] = useState(null)   // null = not yet initialised
  const [_reposFromPlanning, setReposFromPlanning] = useState(false)
  useEffect(() => {
    // Wait for the analysis-run lookup to settle so we can prefer its repos before
    // falling back to the pack-declared default selection.
    if (selectedRepoIds !== null || !repoList.length || !analysisLoaded) return
    const inherited = planningRepoIds.filter(rid => repoList.some(r => r.id === rid))
    if (inherited.length) {
      setSelectedRepoIds(inherited)
      setReposFromPlanning(true)
      return
    }
    setSelectedRepoIds(defaultSelection(repoList, repoRoles))
  }, [repoList, selectedRepoIds, analysisLoaded, planningRepoIds, repoRoles])
  const repoIds = selectedRepoIds || []
  const { reason: repoBlockReason, needsWarning: repoTopologyUnconfigured } =
    validateSelection(repoList, repoIds, repoRoles)
  const agenticIntent = change?.enhanced_prompt || change?.title || change?.initial_prompt || ''

  // Load persisted XSD from DB
  const { data: xsdData, refetch: refetchXsd } = useQuery({
    queryKey: ['xsd', id],
    queryFn: () => agentsApi.xsd(id).then(r => r.data),
    enabled: !!id,
  })
  const isUploaded = xsdData?.source === 'uploaded'

  // R-4 — useResumableJob is a drop-in superset of useAgentWS.
  const {
    messages, streaming, streamingText,
    connected, historyLoaded, error, validation, sendMessage,
    jobId, jobStatus, jobStage, jobProgress, jobStartedAt, jobStartedBy,
    isResuming, cancelJob,
  } = useResumableJob(id, 'xsd')

  const { openGateModal } = useGateModal()
  const { data: gateEval } = useQuery({
    queryKey: ['eval-latest', id, 'tech_spec_to_xsd'],
    queryFn: () => evalApi.latest(id, 'tech_spec_to_xsd').then(r => r.data),
    enabled: !!id,
    refetchInterval: 8000,
    refetchOnWindowFocus: true,
  })

  const assistantMsgs  = messages.filter(m => m.role === 'assistant')
  const currentContent = assistantMsgs[assistantMsgs.length - 1]?.content || null
  // Generate-or-Upload — uploaded XSD substitutes the generated one and skips
  // the assessment/generation flow.
  const displayContent = streaming
    ? (streamingText || '')
    : (currentContent || (isUploaded ? (xsdData?.content || '') : ''))

  useEffect(() => {
    if (streaming) bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [streaming, streamingText])

  useEffect(() => {
    if (currentContent) setStarted(true)
  }, [currentContent])

  // On generation completion, refresh the persisted row + staleness signal.
  const prevStreamingRef = useRef(streaming)
  useEffect(() => {
    if (prevStreamingRef.current && !streaming) {
      refetchXsd()
      queryClient.invalidateQueries({ queryKey: ['artifact-staleness', id] })
    }
    prevStreamingRef.current = streaming
  }, [streaming, refetchXsd, queryClient, id])

  // Restore assessment from persisted DB data
  useEffect(() => {
    if (isUploaded) return
    if (xsdData?.is_required !== null && xsdData?.is_required !== undefined && !assessment) {
      // If we have a DB record but no WS history, show the persisted assessment
      if (!currentContent && xsdData.content && xsdData.is_required !== null) {
        setAssessment({ text: xsdData.content, is_required: xsdData.is_required })
      }
    }
  }, [xsdData, currentContent, assessment])

  const handleAssess = async () => {
    setAssessing(true)
    try {
      const res = await agentsApi.assessXsd(id)
      setAssessment({ text: res.data.assessment, is_required: res.data.is_required })
    } catch (err) {
      console.error('assessment failed', err)
    } finally {
      setAssessing(false)
    }
  }

  const handleGenerate = () => {
    setStarted(true)
    sendMessage('start')
  }

  const xsdRequired    = assessment?.is_required === true
  const xsdNotRequired = assessment?.is_required === false

  // Auto-run assessment once the XSD record has loaded — saves a manual
  // "Run Assessment" click. xsdData being defined (even if empty) tells us
  // the persisted-restore effect above had its chance to populate
  // assessment; if it didn't, we kick the REST assessment ourselves.
  const autoAssessedRef = useRef(false)
  useEffect(() => {
    if (
      !autoAssessedRef.current &&
      !agenticEnabled &&
      xsdData !== undefined &&
      !isUploaded &&
      !assessment &&
      !assessing
    ) {
      autoAssessedRef.current = true
      handleAssess()
    }
  }, [xsdData, isUploaded, assessment, assessing, agenticEnabled])

  // Auto-generate the XSD once the assessment confirms it's required.
  // Same guards as the "Generate XSD" empty state below.
  const autoGeneratedRef = useRef(false)
  const isFirstXsd = assistantMsgs.length === 0
  useEffect(() => {
    if (
      !autoGeneratedRef.current &&
      !agenticEnabled &&
      !isUploaded &&
      assessment &&
      xsdRequired &&
      isFirstXsd &&
      !streaming &&
      !started &&
      !currentContent &&
      historyLoaded &&
      connected
    ) {
      autoGeneratedRef.current = true
      handleGenerate()
    }
  }, [isUploaded, assessment, xsdRequired, isFirstXsd, streaming, started, currentContent, historyLoaded, connected, agenticEnabled])

  const handleFeedback = () => {
    const text = feedback.trim()
    if (!text || streaming) return
    setFeedback('')
    sendMessage(text)
  }

  const handleProceed = async () => {
    // Idempotent: advance AT MOST ONCE, and only FROM the xsd stage. The panel can call
    // onApproved() more than once (the approve click + the "run is completed" effect +
    // any revisit of an already-approved run) — without this guard each call advanced a
    // stage, cascading xsd → tech_spec → product_kit → completed with nothing generated.
    if (proceededRef.current || advancing) return
    if (change?.status && change.status !== 'xsd') return   // already advanced — no-op
    proceededRef.current = true
    setAdvancing(true)
    try {
      await api.post(`/changes/${id}/advance`, {})
      queryClient.invalidateQueries({ queryKey: ['change', id] })
      navigate(`/changes/${id}`)
    } catch (err) {
      if (isEvalGateError(err)) {
        openGateModal({
          changeId: id,
          detail: err.response?.data?.detail,
          actionLabel: 'Complete stage',
          retryAction: (payload) => api.post(`/changes/${id}/advance`, payload || {}),
          onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ['change', id] })
            navigate(`/changes/${id}`)
          },
        })
      } else {
        console.error('advance failed', err)
      }
      proceededRef.current = false   // genuine failure — allow a retry
    } finally {
      setAdvancing(false)
    }
  }

  const handleDownload = () => {
    if (!currentContent) return
    const blob = new Blob([currentContent], { type: 'text/plain' })
    const url  = URL.createObjectURL(blob)
    const a    = document.createElement('a')
    a.href     = url
    a.download = `xsd_changes_v${assistantMsgs.length}.md`
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: 'calc(100vh - 0px)', overflow: 'hidden' }}>

      {/* Header */}
      <div style={{
        padding: '16px 24px', borderBottom: '1px solid var(--border)',
        display: 'flex', alignItems: 'center', gap: '16px', flexShrink: 0,
        background: 'var(--bg-base)',
      }}>
        <button onClick={() => navigate(`/changes/${id}`)} style={{
          display: 'flex', alignItems: 'center', gap: '5px',
          background: 'none', border: 'none', cursor: 'pointer', fontSize: '13px', color: 'var(--text-muted)',
        }}>
          <ArrowLeft size={14} /> Back
        </button>
        <div style={{ flex: 1 }}>
          <h1 style={{ margin: 0, fontSize: '16px', fontWeight: '600', color: 'var(--text-primary)' }}>
            XSD Update
          </h1>
          <p style={{ margin: 0, fontSize: '11px', color: 'var(--text-muted)' }}>
            Schema change assessment · XSD generation · Download for GitLab commit
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
        <EvalStatusPill
          verdict={gateEval?.verdict}
          checkpointLabel="Tech Spec to XSD gate"
          changeId={id}
          checkpointId="tech_spec_to_xsd"
        />

        <StalenessBadge changeId={id} stageKey="xsd" />

        <ShowArtifactsButton changeId={id} />

        <TranscriptsDownloadButton changeId={id} section="xsd" label="Transcripts" />

        <SkipStepButton changeId={id} nextRoute={`/changes/${id}`} />

        {/* Download XSD */}
        {currentContent && !streaming && (
          <button onClick={handleDownload} style={{
            display: 'flex', alignItems: 'center', gap: '6px',
            padding: '8px 14px', background: 'var(--bg-elevated)',
            border: '1px solid var(--border)', borderRadius: '6px',
            fontSize: '13px', color: 'var(--text-secondary)', cursor: 'pointer',
          }}>
            <Download size={13} /> Download
          </button>
        )}

        {/* Proceed to TSD — only when there is nothing to approve in the panel:
            schema not required, or the legacy (non-agentic) doc path. The agentic
            path advances to the Tech Spec automatically on schema approval (onApproved),
            so there is no separate "complete stage" click there. */}
        {((xsdNotRequired || (!agenticEnabled && (currentContent || isUploaded))) && !streaming) && (
          <button
            onClick={handleProceed}
            disabled={advancing}
            style={{
              display: 'flex', alignItems: 'center', gap: '8px',
              padding: '8px 18px', background: 'var(--accent)', color: 'white',
              border: 'none', borderRadius: '6px', fontSize: '13px', fontWeight: '600',
              cursor: advancing ? 'not-allowed' : 'pointer', opacity: advancing ? 0.7 : 1,
            }}
          >
            {advancing && <Loader size={13} style={{ animation: 'spin 1s linear infinite' }} />}
            Continue to Tech Spec <ArrowRight size={14} />
          </button>
        )}
      </div>

      {/* Main content */}
      <div style={{ flex: 1, overflowY: 'auto', padding: '24px' }}>
        {/* R-4 — resume banner. Renders only when a durable job is active. */}
        <ProgressBanner
          jobId={jobId} status={jobStatus} stage={jobStage} progress={jobProgress}
          startedAt={jobStartedAt} startedBy={jobStartedBy}
          resuming={isResuming} onCancel={cancelJob}
          currentUserId={me?.id}
        />

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

        {/* Agentic Phase A (THE BOOK v3.4): generate the XSD edits in the repo,
            review the schema diff, and approve to hand off to Phase B (code). */}
        {agenticEnabled && (
          <div style={{
            background: 'var(--bg-elevated)', border: '1px solid var(--border)',
            borderRadius: '12px', padding: '20px 24px', marginBottom: '20px',
          }}>
            <h2 style={{ margin: '0 0 4px', fontSize: '16px', fontWeight: 600, color: 'var(--text-primary)' }}>
              Phase A — Agentic XSD generation
            </h2>
            <p style={{ margin: '0 0 14px', fontSize: '13px', color: 'var(--text-muted)' }}>
              The agent discovers the schema scope, edits the XSDs in the repo, and presents the
              diff for your review. The schema edits stay in the workspace (no git push) and the
              flow continues to the TSD — the same workspace is reused for code generation later.
            </p>

            {/* Repos are CARRIED from the clarification/planning stage — not re-selected here.
                Read-only confirmation of the scope chosen at the start. */}
            <div style={{ marginBottom: '16px', padding: '10px 14px', borderRadius: '8px',
              background: 'var(--bg-input, rgba(127,127,127,0.05))', border: '1px solid var(--border)' }}>
              <div style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>
                <strong>Repositories</strong> — carried from the planning stage, not re-selected:{' '}
                <span style={{ fontWeight: 600, color: 'var(--text-primary)' }}>
                  {repoList.filter(r => repoIds.includes(r.id)).map(r => r.label).join('  +  ') || '—'}
                </span>
              </div>
              {repoTopologyUnconfigured && (
                <div style={{ fontSize: '11px', color: '#b45309', marginTop: 6 }}>⚠ {UNCONFIGURED_TOPOLOGY_NOTICE}</div>
              )}
              {repoBlockReason && (
                <div style={{ fontSize: '11px', color: '#b45309', marginTop: 6 }}>{repoBlockReason}</div>
              )}
            </div>

            <AgenticPhasePanel
              changeId={id}
              kind="xsd"
              repoIds={repoIds}
              intent={agenticIntent}
              variant="clean"
              startBlockedReason={repoBlockReason}
              onApproved={() => { setXsdApproved(true); handleProceed() }}
            />
          </div>
        )}

        {/* Step 1: Assessment — auto-runs on mount via autoAssessedRef effect.
            Skipped entirely when the user uploaded an XSD or agentic Phase A is driving. */}
        {!agenticEnabled && !assessment && !isUploaded && !displayContent && (
          <div style={{
            textAlign: 'center', padding: '64px 32px',
            background: 'var(--bg-elevated)', border: '1px solid var(--border)', borderRadius: '12px',
            marginBottom: '20px',
          }}>
            <FileCode size={40} style={{ color: 'var(--accent)', marginBottom: '16px' }} />
            <h2 style={{ margin: '0 0 8px', fontSize: '18px', fontWeight: '600', color: 'var(--text-primary)' }}>
              XSD Change Assessment
            </h2>
            <p style={{ margin: '0 auto 24px', fontSize: '13px', color: 'var(--text-muted)', maxWidth: '460px' }}>
              Analysing the Technical Specification and BRD to determine whether
              XML Schema changes are required for this feature.
            </p>
            <div style={{ display: 'inline-flex', alignItems: 'center', gap: '10px', color: 'var(--accent)' }}>
              <Loader size={16} style={{ animation: 'spin 1s linear infinite' }} />
              <span style={{ fontSize: '13px', fontWeight: '600' }}>Analysing…</span>
            </div>
          </div>
        )}

        {/* Assessment result banner (legacy path only — when agentic drives, the prior
            assessment moves to a reference appendix at the END until approval is done) */}
        {assessment && !agenticEnabled && (
          <div style={{
            padding: '16px 20px', borderRadius: '10px', marginBottom: '20px',
            background: xsdRequired
              ? 'rgba(218,119,86,0.08)'
              : 'rgba(76,175,125,0.08)',
            border: `1px solid ${xsdRequired ? 'rgba(218,119,86,0.3)' : 'rgba(76,175,125,0.3)'}`,
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '12px' }}>
              {xsdRequired
                ? <AlertTriangle size={18} style={{ color: 'var(--accent)', flexShrink: 0 }} />
                : <CheckCircle size={18} style={{ color: 'var(--success)', flexShrink: 0 }} />}
              <p style={{
                margin: 0, fontSize: '14px', fontWeight: '600',
                color: xsdRequired ? 'var(--accent)' : 'var(--success)',
              }}>
                {xsdRequired ? 'XSD Changes Required' : 'No XSD Changes Required'}
              </p>
              {/* Re-assess button */}
              <button
                onClick={handleAssess}
                disabled={assessing}
                style={{
                  marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: '5px',
                  padding: '4px 12px', background: 'transparent',
                  border: '1px solid var(--border)', borderRadius: '5px',
                  fontSize: '11px', color: 'var(--text-muted)', cursor: 'pointer',
                }}
              >
                {assessing ? <Loader size={11} style={{ animation: 'spin 1s linear infinite' }} /> : <RefreshCw size={11} />}
                Re-assess
              </button>
            </div>
            <div className="md-content" style={{ fontSize: '13px', lineHeight: '1.7', color: 'var(--text-primary)' }}>
              <ReactMarkdown>{assessment.text}</ReactMarkdown>
            </div>
          </div>
        )}

        {/* Step 2: Generate XSD — auto-runs once assessment confirms it's
            required (via autoGeneratedRef effect). This block is a brief
            loading view; the streaming XSD output below takes over as soon
            as the WS responds. */}
        {!agenticEnabled && assessment && xsdRequired && !started && !streaming && !currentContent && historyLoaded && (
          <div style={{
            textAlign: 'center', padding: '48px 32px',
            background: 'var(--bg-elevated)', border: '1px solid var(--border)', borderRadius: '12px',
          }}>
            <h2 style={{ margin: '0 0 8px', fontSize: '18px', fontWeight: '600', color: 'var(--text-primary)' }}>
              Preparing XSD Generation…
            </h2>
            <p style={{ margin: '0 auto 24px', fontSize: '13px', color: 'var(--text-muted)', maxWidth: '440px' }}>
              Connecting and starting generation of the updated XSD files.
            </p>
            <Loader size={20} style={{ color: 'var(--accent)', animation: 'spin 1s linear infinite' }} />
          </div>
        )}

        {/* Validation panel */}
        {!streaming && validation && <ValidationPanel validation={validation} />}

        {/* Download DOCX (legacy document path) */}
        {!agenticEnabled && !streaming && displayContent && (
          <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: '12px' }}>
            <DocxDownloadButton changeId={id} docType="xsd" label="Download" />
          </div>
        )}

        {/* XSD output (legacy document path — agentic shows repo diffs instead) */}
        {!agenticEnabled && (streaming || displayContent) && (
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
                <span style={{ fontSize: '12px', color: 'var(--accent)' }}>Generating XSD…</span>
              </div>
            )}
            <div className="md-content" style={{ fontSize: '13px', lineHeight: '1.8', color: 'var(--text-primary)' }}>
              <ReactMarkdown>{displayContent + (streaming ? '▌' : '')}</ReactMarkdown>
            </div>
            <div ref={bottomRef} />
          </div>
        )}

        {/* Not-required completion card (legacy path only — when agentic drives, the
            run's own conclusion + approval gate decide this, not the document assessment) */}
        {!agenticEnabled && xsdNotRequired && !currentContent && (
          <div style={{
            padding: '24px', borderRadius: '10px', textAlign: 'center',
            background: 'rgba(76,175,125,0.06)', border: '1px solid rgba(76,175,125,0.25)',
          }}>
            <CheckCircle size={32} style={{ color: 'var(--success)', marginBottom: '12px' }} />
            <p style={{ margin: '0 0 6px', fontSize: '15px', fontWeight: '600', color: 'var(--text-primary)' }}>
              No XSD changes needed for this feature
            </p>
            <p style={{ margin: 0, fontSize: '13px', color: 'var(--text-muted)' }}>
              You can proceed to complete this stage and continue to the Product Kit.
            </p>
          </div>
        )}

        {/* Revision pills — hidden while streaming (or during the docgen
            replay window that keeps `streaming` truthy after WS done)
            so the newly-bumped v{N+1} chip doesn't appear alongside
            the "Generating…" spinner. */}
        {!streaming && assistantMsgs.length > 1 && (
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

        {/* Prior document-only assessment — reference appendix while the agentic flow
            is the primary path. Collapsed; the agent already consults it internally. */}
        {assessment && agenticEnabled && (
          <details style={{
            marginTop: '20px', padding: '14px 18px',
            background: 'var(--bg-elevated)', border: '1px solid var(--border)', borderRadius: '12px',
          }}>
            <summary style={{ cursor: 'pointer', fontSize: '13px', fontWeight: 600, color: 'var(--text-secondary)' }}>
              Prior XSD change assessment (document-based, reference only)
            </summary>
            <div className="md-content" style={{ fontSize: '13px', lineHeight: '1.7', color: 'var(--text-primary)', marginTop: '10px' }}>
              <ReactMarkdown>{assessment.text}</ReactMarkdown>
            </div>
          </details>
        )}
      </div>

      {/* Feedback bar — legacy document path only (agentic refines via its own gate) */}
      {!agenticEnabled && currentContent && !streaming && (
        <div style={{
          padding: '16px 24px', borderTop: '1px solid var(--border)',
          background: 'var(--bg-base)', flexShrink: 0,
        }}>
          <p style={{ margin: '0 0 10px', fontSize: '12px', color: 'var(--text-muted)', fontWeight: '500' }}>
            Refine the XSD, or download and proceed to Product Kit above.
          </p>
          <div style={{ display: 'flex', gap: '10px' }}>
            <input
              value={feedback}
              onChange={e => setFeedback(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') handleFeedback() }}
              placeholder={t('ph.xsd.refine')}
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
              disabled={!feedback.trim() || streaming || !connected}
              style={{
                display: 'flex', alignItems: 'center', gap: '6px',
                padding: '9px 16px', background: 'var(--bg-elevated)',
                border: '1px solid var(--border)', borderRadius: '6px',
                color: 'var(--text-secondary)', fontSize: '13px', fontWeight: '500',
                cursor: (!feedback.trim() || streaming || !connected) ? 'not-allowed' : 'pointer',
                opacity: (!feedback.trim() || streaming || !connected) ? 0.5 : 1,
              }}
            >
              <RefreshCw size={13} /> Refine
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
