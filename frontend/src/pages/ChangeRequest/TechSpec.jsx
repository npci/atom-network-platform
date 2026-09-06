// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useState, useRef, useEffect } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import api, { docgenApi, agentsApi, evalApi, changesApi } from '../../services/api'
// R-4 — durable-job-aware wrapper + resume banner.
import { useResumableJob } from '../../hooks/useResumableJob'
import ProgressBanner from '../../components/jobs/ProgressBanner'
import DocConsistencyBanner from '../../components/DocConsistencyBanner'
import SkipStepButton from '../../components/common/SkipStepButton'
import { useAuth } from '../../hooks/useAuth'
import { useGateModal } from '../../context/useGateModal'
import { isEvalGateError } from '../../lib/evalGate'
import EvalStatusPill from '../../components/eval/EvalStatusPill'
import { ArrowLeft, ArrowRight, Loader, RefreshCw, Wifi, WifiOff, Code2 } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import ValidationPanel from '../../components/common/ValidationPanel'
import { DocxDownloadButton } from './BRD'
import DocumentSourceToggle from '../../components/DocumentSourceToggle'
import ReconciliationPanel from '../../components/ReconciliationPanel'
import ShowArtifactsButton from '../../components/ShowArtifactsButton'
import TranscriptsDownloadButton from '../../components/TranscriptsDownloadButton'
import StalenessBadge from '../../components/StalenessBadge'

export default function TechSpec() {
  const { id } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [feedback, setFeedback] = useState('')
  const [started, setStarted]   = useState(false)
  const [advancing, setAdvancing] = useState(false)
  // Create-mode gate: null = ask generate-or-upload first; 'generate' = auto-start.
  const [createMode, setCreateMode] = useState(null)
  // Docgen merge (Session 20+) — section-wise edit (Phase G UX).
  const [docgenSections, setDocgenSections] = useState([])
  const [editSection, setEditSection]       = useState('')
  const [editingSection, setEditingSection] = useState(false)
  const bottomRef = useRef(null)
  // Poll-budget anchor for the no-reconciliation case (below). This page component is
  // NOT remounted on client-side navigation between change-requests, so reset the anchor
  // when `id` changes — otherwise a warm-navigated change inherits the elapsed budget.
  const reconMountedAt = useRef(Date.now())
  useEffect(() => { reconMountedAt.current = Date.now() }, [id])

  // R-4 — useResumableJob is a drop-in superset of useAgentWS. Module string
  // 'tech-spec' (kebab) is converted to 'tech_spec' (snake) inside the hook
  // for the registry lookup.
  const {
    messages, streaming, streamingText,
    connected, historyLoaded, error, validation, sendMessage,
    docgenJobId,
    jobId, jobStatus, jobStage, jobProgress, jobStartedAt, jobStartedBy,
    isResuming, jobsLoaded, cancelJob, docConsistency,
  } = useResumableJob(id, 'tech-spec')

  const { user: me } = useAuth()
  const { openGateModal } = useGateModal()
  // workflow_version 2 runs XSD BEFORE the TSD, so after the TSD the next stage is the
  // Product Kit (not XSD). v1 keeps the old TSD → XSD order.
  const { data: change } = useQuery({
    queryKey: ['change', id],
    queryFn: () => changesApi.get(id).then(r => r.data),
    enabled: !!id,
  })
  const nextStage = (change?.workflow_version ?? 2) >= 2 ? 'product_kit' : 'xsd'
  const nextLabel = nextStage === 'product_kit' ? 'Proceed to Product Kit' : 'Proceed to XSD'
  const { data: gateEval } = useQuery({
    queryKey: ['eval-latest', id, 'brd_to_tech_spec'],
    queryFn: () => evalApi.latest(id, 'brd_to_tech_spec').then(r => r.data),
    enabled: !!id,
    refetchInterval: 8000,
    refetchOnWindowFocus: true,
  })

  // Generate-or-Upload — persisted TSD row (for provenance + uploaded content).
  const { data: tsData, refetch: refetchTs } = useQuery({
    queryKey: ['tech-spec', id],
    queryFn: () => agentsApi.techSpec(id).then(r => r.data),
    enabled: !!id,
  })
  const isUploaded = tsData?.source === 'uploaded'

  const assistantMsgs  = messages.filter(m => m.role === 'assistant')
  const currentContent = assistantMsgs[assistantMsgs.length - 1]?.content || null
  // An uploaded Tech Spec is the source of truth — it wins over stale WS text.
  const displayContent = streaming
    ? (streamingText || '')
    : (isUploaded ? (tsData?.content || '') : (currentContent || ''))
  const hasExistingTs  = Boolean(tsData?.content) || assistantMsgs.length > 0

  // Uploaded-TSD ↔ plan reconciliation — the TSD isn't final while this is open
  // ('pending' conflicts, or 'applying' while the corrected TSD regenerates). Advancing
  // is blocked server-side in both; mirror that + keep polling through 'applying'.
  const { data: reconData } = useQuery({
    queryKey: ['reconciliation', id, 'tech_spec'],
    queryFn: () => agentsApi.getReconciliation(id, 'tech_spec').then(r => r.data),
    enabled: !!id && isUploaded,
    refetchInterval: (q) => {
      const d = q?.state?.data
      if (d?.exists && d.status !== 'applying') return false
      if (d?.grounding_summary) return false                    // resolved with code-check findings → terminal
      if (Date.now() - reconMountedAt.current > 120000) return false
      return 5000
    },
  })
  const reconOpen = !!reconData?.exists            // checking / pending / applying → doc NOT final
  const reconApplying = reconData?.regenerating === true   // for the Proceed tooltip

  useEffect(() => {
    if (streaming) bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [streaming, streamingText])

  useEffect(() => {
    if (currentContent) setStarted(true)
  }, [currentContent])

  // On generation completion, refresh the persisted row (source/version/
  // has_generated_version) and the downstream staleness signal.
  const prevStreamingRef = useRef(streaming)
  useEffect(() => {
    if (prevStreamingRef.current && !streaming) {
      refetchTs()
      queryClient.invalidateQueries({ queryKey: ['artifact-staleness', id] })
    }
    prevStreamingRef.current = streaming
  }, [streaming, refetchTs, queryClient, id])

  const handleStart = () => {
    setStarted(true)
    sendMessage('start')
  }

  // Auto-start Tech Spec generation once the user arrives here from BRD
  // (either after approval or via "Skip for approvals"). Guards mirror the
  // empty-state condition below plus a once-per-mount ref. Same pattern as
  // Research/Canvas.
  //
  // `jobsLoaded` + `!jobId` + `!isResuming` are required to avoid firing a
  // duplicate generation when the page is remounted while a previous run
  // is still in progress (e.g. user navigates Back → Tech Spec, or refreshes).
  // Without them, the WS handshake completes (historyLoaded=true) before
  // the JobsContext /jobs/active fetch lands, the message list is still
  // empty, and the guard wrongly concludes "no run in flight — start one".
  const autoStartedRef = useRef(false)
  const isFirstTechSpec = assistantMsgs.length === 0
  useEffect(() => {
    if (
      !autoStartedRef.current &&
      createMode === 'generate' &&
      historyLoaded &&
      jobsLoaded &&
      !jobId &&
      !isResuming &&
      isFirstTechSpec &&
      !streaming &&
      !started &&
      connected &&
      tsData !== undefined && !isUploaded
    ) {
      autoStartedRef.current = true
      handleStart()
    }
  }, [createMode, historyLoaded, jobsLoaded, jobId, isResuming, isFirstTechSpec, streaming, started, connected, tsData, isUploaded])

  const handleFeedback = async () => {
    const text = feedback.trim()
    if (!text || streaming || editingSection) return

    // Docgen merge (Session 20+) — Phase G section-wise edit.
    if (editSection && docgenJobId) {
      setEditingSection(true)
      setFeedback('')
      try {
        await docgenApi.editSection(id, 'TSD', editSection, text)
        queryClient.invalidateQueries({ queryKey: ['tech-spec', id] })
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

  // Docgen merge — fetch section list when a docgen TSD job completes.
  useEffect(() => {
    if (!streaming && docgenJobId && id) {
      docgenApi.sections(id, 'TSD')
        .then(r => setDocgenSections(r.data?.sections || []))
        .catch(() => setDocgenSections([]))
    }
  }, [streaming, docgenJobId, id])

  const handleProceed = async () => {
    setAdvancing(true)
    try {
      await api.post(`/changes/${id}/advance`, {})
      queryClient.invalidateQueries({ queryKey: ['change', id] })
      navigate(`/changes/${id}/${nextStage}`)
    } catch (err) {
      if (isEvalGateError(err)) {
        openGateModal({
          changeId: id,
          detail: err.response?.data?.detail,
          actionLabel: nextLabel,
          retryAction: (payload) => api.post(`/changes/${id}/advance`, payload || {}),
          onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ['change', id] })
            navigate(`/changes/${id}/${nextStage}`)
          },
        })
      } else {
        console.error('advance failed', err)
      }
    } finally {
      setAdvancing(false)
    }
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
            Technical Specification
          </h1>
          <p style={{ margin: 0, fontSize: '11px', color: 'var(--text-muted)' }}>
            API design · Data model · Security · Deployment · Testing strategy
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
        <StalenessBadge changeId={id} stageKey="tech_spec" />
        <ShowArtifactsButton changeId={id} />
        <TranscriptsDownloadButton changeId={id} section="tech_spec" label="Transcripts" />
        <EvalStatusPill
          verdict={gateEval?.verdict}
          checkpointLabel="BRD to Tech Spec gate"
          changeId={id}
          checkpointId="brd_to_tech_spec"
          onAutoFix={(text) => { setStarted(true); sendMessage(text) }}
        />
        <SkipStepButton changeId={id} nextRoute={`/changes/${id}`} />
        {(
          <DocumentSourceToggle
            changeId={id}
            docType="tech_spec"
            docLabel="Tech Spec"
            source={tsData?.source}
            originalFilename={tsData?.original_filename}
            uploadedAt={tsData?.uploaded_at}
            confirmReplace={hasExistingTs || streaming}
            canRevert={!streaming && tsData?.has_generated_version}
            onGenerateInstead={streaming ? undefined : (() => { setCreateMode('generate'); handleStart() })}
            onBeforeUpload={async () => { if (streaming) { try { await cancelJob?.() } catch { /* already finished or gone; upload proceeds either way */ } } }}
            onUploaded={async () => { setStarted(true); await refetchTs(); await queryClient.invalidateQueries({ queryKey: ['tech-spec', id] }); queryClient.invalidateQueries({ queryKey: ['artifact-staleness', id] }); queryClient.invalidateQueries({ queryKey: ['reconciliation', id, 'tech_spec'] }) }}
          />
        )}

        {(currentContent || isUploaded) && !streaming && (
          <button
            onClick={handleProceed}
            disabled={advancing || reconOpen}
            title={reconOpen
              ? (reconApplying ? 'Regenerating the Tech Spec from your resolutions — hold on…'
                               : 'Resolve the reconciliation conflicts below before proceeding')
              : ''}
            style={{
              display: 'flex', alignItems: 'center', gap: '8px',
              padding: '8px 18px', background: 'var(--accent)', color: 'white',
              border: 'none', borderRadius: '6px', fontSize: '13px', fontWeight: '600',
              cursor: (advancing || reconOpen) ? 'not-allowed' : 'pointer',
              opacity: (advancing || reconOpen) ? 0.7 : 1,
            }}
          >
            {advancing && <Loader size={13} style={{ animation: 'spin 1s linear infinite' }} />}
            {nextLabel} <ArrowRight size={14} />
          </button>
        )}
      </div>

      {/* Main content */}
      <div style={{ flex: 1, overflowY: 'auto', padding: '24px' }}>
        {/* Uploaded-TSD ↔ plan reconciliation conflicts — in the scrollable area (not
            the fixed header) so a long list scrolls. Self-hides until the check runs. */}
        {isUploaded && <ReconciliationPanel key={id} changeId={id} docKind="tech_spec" />}

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

        {/* Not started — hidden when a durable job is already in flight,
            so the user sees the ProgressBanner + polled stream instead of
            a "Generate" button that would queue a duplicate run. */}
        {createMode === null && !started && !streaming && historyLoaded && !jobId && !isUploaded && !displayContent && (
          <div style={{
            textAlign: 'center', padding: '64px 32px',
            background: 'var(--bg-elevated)', border: '1px solid var(--border)', borderRadius: '12px',
          }}>
            <Code2 size={40} style={{ color: 'var(--accent)', marginBottom: '16px' }} />
            <h2 style={{ margin: '0 0 8px', fontSize: '18px', fontWeight: '600', color: 'var(--text-primary)' }}>
              How would you like to create the Tech Spec?
            </h2>
            <p style={{ margin: '0 auto 24px', fontSize: '13px', color: 'var(--text-muted)', maxWidth: '460px' }}>
              Generate a comprehensive 13-section Technical Specification with AI — grounded in the
              approved BRD and research report — or upload your own existing Tech Spec to use instead.
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
                Generate with AI
              </button>
              <DocumentSourceToggle
                changeId={id}
                docType="tech_spec"
                source={tsData?.source}
                label="Upload a document"
                onUploaded={async () => { setStarted(true); await refetchTs(); queryClient.invalidateQueries({ queryKey: ['artifact-staleness', id] }) }}
              />
            </div>
          </div>
        )}

        {/* Validation panel */}
        {!streaming && validation && <ValidationPanel validation={validation} />}

        {/* Download DOCX — only once final: not while an upload reconciliation is open. */}
        {!streaming && displayContent && !reconOpen && (
          <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: '12px' }}>
            <DocxDownloadButton changeId={id} docType="tech_spec" label="Download Tech Spec (.docx)" />
          </div>
        )}

        {/* Plan-fidelity gate — flags if the TSD invented wire APIs/schemas the plan doesn't have. */}
        {!streaming && displayContent && !reconOpen && <DocConsistencyBanner result={docConsistency} docLabel="TSD" />}

        {/* Document — hidden for the WHOLE time a reconciliation is open (checking →
            conflicts → regenerating); shown only once the final TSD is generated. */}
        {!reconOpen && (streaming || displayContent) && (
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
                <span style={{ fontSize: '12px', color: 'var(--accent)' }}>Generating Technical Specification…</span>
              </div>
            )}
            <div className="md-content" style={{ fontSize: '14px', lineHeight: '1.8', color: 'var(--text-primary)' }}>
              <ReactMarkdown>{displayContent + (streaming ? '▌' : '')}</ReactMarkdown>
            </div>
            <div ref={bottomRef} />
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
      </div>

      {/* Feedback bar */}
      {(currentContent || started) && !streaming && (
        <div style={{
          padding: '16px 24px', borderTop: '1px solid var(--border)',
          background: 'var(--bg-base)', flexShrink: 0,
        }}>
          <p style={{ margin: '0 0 10px', fontSize: '12px', color: 'var(--text-muted)', fontWeight: '500' }}>
            {currentContent
              ? 'Provide feedback to refine the spec, or proceed to XSD assessment above.'
              : 'Tech Spec is generating…'}
          </p>
          <div style={{ display: 'flex', gap: '10px' }}>
            {/* Docgen merge — section-wise edit dropdown. */}
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
                : 'e.g. Add more detail on the retry and circuit-breaker strategy…'}
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
