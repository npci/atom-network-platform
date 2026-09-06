// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useState, useRef, useEffect } from 'react'
import { t } from '../../strings'
import { useParams, useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import api, { changesApi, evalApi } from '../../services/api'
// R-4 — durable-job-aware wrapper + resume banner.
import { useResumableJob } from '../../hooks/useResumableJob'
import ProgressBanner from '../../components/jobs/ProgressBanner'
import SkipStepButton from '../../components/common/SkipStepButton'
import TranscriptsDownloadButton from '../../components/TranscriptsDownloadButton'
import EvalStatusPill from '../../components/eval/EvalStatusPill'
import { useAuth } from '../../hooks/useAuth'
import { useGateModal } from '../../context/useGateModal'
import { isEvalGateError } from '../../lib/evalGate'
import { ArrowLeft, ArrowRight, Send, Loader, RefreshCw, Wifi, WifiOff, BookOpen } from 'lucide-react'
import ReactMarkdown from 'react-markdown'

export default function Research() {
  const { id } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [feedback, setFeedback]   = useState('')
  const [started, setStarted]     = useState(false)
  const [advancing, setAdvancing] = useState(false)
  const reportRef  = useRef(null)

  const { data: _change } = useQuery({
    queryKey: ['change', id],
    queryFn: () => changesApi.get(id).then(r => r.data),
  })

  // Phase 7 — eval verdict for the research summary
  const { data: gateEval } = useQuery({
    queryKey: ['eval-latest', id, 'prompt_to_research'],
    queryFn: () => evalApi.latest(id, 'prompt_to_research').then(r => r.data),
    enabled: !!id,
    refetchInterval: 8000,
    refetchOnWindowFocus: true,
  })

  // R-4 — useResumableJob is a drop-in superset of useAgentWS.
  const {
    messages, streaming, streamingText,
    connected, historyLoaded, error, sendMessage,
    jobId, jobStatus, jobStage, jobProgress, jobStartedAt, jobStartedBy,
    isResuming, cancelJob,
  } = useResumableJob(id, 'research')

  const { user: me } = useAuth()
  const { openGateModal } = useGateModal()

  // The latest assistant message is the current research report
  const assistantMsgs = messages.filter(m => m.role === 'assistant')
  const currentReport = assistantMsgs[assistantMsgs.length - 1]?.content || null
  const isFirstResearch = assistantMsgs.length === 0

  // Auto-scroll inside the report area
  useEffect(() => {
    if (streaming) reportRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [streaming, streamingText])

  const handleStart = () => {
    setStarted(true)
    sendMessage('start')
  }

  // Auto-start research the first time this page is opened after the
  // enhanced prompt was approved — saves a redundant click on a button
  // whose only job was to send 'start'. Guards mirror the conditions for
  // showing the "Ready to start" empty state, plus a once-per-mount ref.
  const autoStartedRef = useRef(false)
  useEffect(() => {
    if (
      !autoStartedRef.current &&
      historyLoaded &&
      isFirstResearch &&
      !streaming &&
      !started &&
      connected
    ) {
      autoStartedRef.current = true
      handleStart()
    }
  }, [historyLoaded, isFirstResearch, streaming, started, connected])

  const handleFeedback = () => {
    const text = feedback.trim()
    if (!text || streaming) return
    setFeedback('')
    sendMessage(text)
  }

  const handleProceed = async () => {
    if (advancing) return
    setAdvancing(true)
    try {
      await api.post(`/changes/${id}/advance`, {})
      queryClient.invalidateQueries({ queryKey: ['change', id] })
      navigate(`/changes/${id}/canvas`)
    } catch (err) {
      if (isEvalGateError(err)) {
        openGateModal({
          changeId: id,
          detail: err.response?.data?.detail,
          actionLabel: 'Proceed to Product Canvas',
          retryAction: (payload) => api.post(`/changes/${id}/advance`, payload || {}),
          onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ['change', id] })
            navigate(`/changes/${id}/canvas`)
          },
        })
      } else {
        console.error('advance failed', err)
      }
    } finally {
      setAdvancing(false)
    }
  }

  const reportContent = streaming
    ? (streamingText || '')
    : (currentReport || '')

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100vh' }}>

      {/* ── Header ── */}
      <div style={{
        padding: '16px 24px', borderBottom: '1px solid var(--border)',
        display: 'flex', alignItems: 'center', gap: '16px', flexShrink: 0,
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
        <div style={{ flex: 1 }}>
          <h1 style={{ margin: 0, fontSize: '16px', fontWeight: '600', color: 'var(--text-primary)' }}>
            Deep Research
          </h1>
          <p style={{ margin: 0, fontSize: '11px', color: 'var(--text-muted)' }}>
            Market analysis · Product context · RBI compliance
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
          checkpointLabel="Prompt → Research"
          changeId={id}
          checkpointId="prompt_to_research"
          onAutoFix={(text) => { setStarted(true); sendMessage(text) }}
        />
        <TranscriptsDownloadButton changeId={id} section="deep_research" label="Transcripts" />
        <SkipStepButton changeId={id} nextRoute={`/changes/${id}`} />
        {currentReport && !streaming && (
          <button
            onClick={handleProceed}
            disabled={advancing}
            style={{
              display: 'flex', alignItems: 'center', gap: '8px',
              padding: '8px 18px', background: 'var(--accent)', color: 'white',
              border: 'none', borderRadius: '6px', fontSize: '13px',
              fontWeight: '600', cursor: advancing ? 'not-allowed' : 'pointer',
              opacity: advancing ? 0.7 : 1,
            }}
          >
            {advancing && <Loader size={13} style={{ animation: 'spin 1s linear infinite' }} />}
            Proceed to Product Canvas <ArrowRight size={14} />
          </button>
        )}
      </div>

      {/* ── Main content ── */}
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
            <p style={{ margin: '0 0 4px', fontSize: '13px', fontWeight: '600', color: 'var(--danger)' }}>
              Error
            </p>
            <p style={{ margin: 0, fontSize: '12px', color: 'var(--text-secondary)' }}>{error}</p>
            {!connected && (
              <button
                onClick={() => window.location.reload()}
                style={{
                  marginTop: '10px', padding: '6px 14px',
                  background: 'var(--accent)', color: 'white',
                  border: 'none', borderRadius: '6px',
                  fontSize: '12px', fontWeight: '600', cursor: 'pointer',
                }}
              >
                Retry
              </button>
            )}
          </div>
        )}

        {/* Generating — survives re-mount: a durable job in flight shows the loader (the local
            `started` flag resets on navigation, so we also key off jobStatus/isResuming). */}
        {(jobStatus === 'running' || isResuming) && !streaming && !currentReport && historyLoaded && (
          <div style={{
            textAlign: 'center', padding: '64px 32px',
            background: 'var(--bg-elevated)', border: '1px solid var(--border)', borderRadius: '12px',
          }}>
            <BookOpen size={40} style={{ color: 'var(--accent)', marginBottom: '16px' }} />
            <h2 style={{ margin: '0 0 8px', fontSize: '18px', fontWeight: '600', color: 'var(--text-primary)' }}>
              Deep Research in progress…
            </h2>
            <div style={{ display: 'inline-flex', alignItems: 'center', gap: '10px', color: 'var(--accent)', marginTop: 8 }}>
              <Loader size={16} style={{ animation: 'spin 1s linear infinite' }} />
              <span style={{ fontSize: '13px', fontWeight: '600' }}>{connected ? 'Researching…' : 'Reconnecting…'}</span>
            </div>
          </div>
        )}

        {/* Not started yet — suppressed when a durable job is already in flight (re-mount). */}
        {!started && isFirstResearch && !streaming && historyLoaded && jobStatus !== 'running' && !isResuming && (
          <div style={{
            textAlign: 'center', padding: '64px 32px',
            background: 'var(--bg-elevated)', border: '1px solid var(--border)',
            borderRadius: '12px',
          }}>
            <BookOpen size={40} style={{ color: 'var(--accent)', marginBottom: '16px' }} />
            <h2 style={{ margin: '0 0 8px', fontSize: '18px', fontWeight: '600', color: 'var(--text-primary)' }}>
              Ready to start Deep Research
            </h2>
            <p style={{ fontSize: '13px', color: 'var(--text-muted)', maxWidth: '400px', margin: '0 auto 24px' }}>
              The AI will analyse market trends, query the internal knowledge base, and
              review RBI guidelines — producing a structured research report in three sections.
            </p>
            <button
              onClick={handleStart}
              disabled={!connected}
              style={{
                padding: '10px 28px', background: 'var(--accent)', color: 'white',
                border: 'none', borderRadius: '8px', fontSize: '14px',
                fontWeight: '600', cursor: connected ? 'pointer' : 'not-allowed',
                opacity: connected ? 1 : 0.5,
              }}
            >
              Start Research
            </button>
          </div>
        )}

        {/* Streaming / report display */}
        {(streaming || reportContent) && (
          <div style={{
            background: 'var(--bg-elevated)', border: '1px solid var(--border)',
            borderRadius: '8px', padding: '28px 32px',
          }}>
            {streaming && (
              <div style={{
                display: 'flex', alignItems: 'center', gap: '8px',
                marginBottom: '16px', padding: '8px 14px',
                background: 'rgba(218,119,86,0.08)', border: '1px solid rgba(218,119,86,0.2)',
                borderRadius: '6px',
              }}>
                <Loader size={14} style={{ color: 'var(--accent)', animation: 'spin 1s linear infinite' }} />
                <span style={{ fontSize: '12px', color: 'var(--accent)' }}>Generating research report…</span>
              </div>
            )}
            <div className="md-content research-report" style={{ fontSize: '14px', lineHeight: '1.8', color: 'var(--text-primary)' }}>
              <ReactMarkdown>{reportContent + (streaming ? '▌' : '')}</ReactMarkdown>
            </div>
            <div ref={reportRef} />
          </div>
        )}

        {/* Version history pills */}
        {assistantMsgs.length > 1 && (
          <div style={{ display: 'flex', gap: '8px', marginTop: '16px', flexWrap: 'wrap' }}>
            <span style={{ fontSize: '11px', color: 'var(--text-muted)', alignSelf: 'center' }}>Revisions:</span>
            {assistantMsgs.map((_, i) => (
              <span key={i} style={{
                padding: '2px 10px', borderRadius: '20px', fontSize: '11px',
                background: i === assistantMsgs.length - 1 ? 'var(--accent)' : 'var(--bg-elevated)',
                color: i === assistantMsgs.length - 1 ? 'white' : 'var(--text-muted)',
                border: '1px solid var(--border)',
              }}>
                v{i + 1}
              </span>
            ))}
          </div>
        )}
      </div>

      {/* ── Feedback bar ── */}
      {(currentReport || started) && !streaming && (
        <div style={{
          padding: '16px 24px', borderTop: '1px solid var(--border)',
          background: 'var(--bg-base)', flexShrink: 0,
        }}>
          <p style={{ margin: '0 0 10px', fontSize: '12px', color: 'var(--text-muted)', fontWeight: '500' }}>
            {currentReport
              ? 'Provide feedback to refine the research, or proceed to Product Canvas above.'
              : 'Research is starting…'}
          </p>
          <div style={{ display: 'flex', gap: '10px' }}>
            <input
              value={feedback}
              onChange={e => setFeedback(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') handleFeedback() }}
              placeholder={t('ph.research.refine')}
              style={{
                flex: 1, padding: '9px 14px',
                background: 'var(--bg-input)', border: '1px solid var(--border)',
                borderRadius: '6px', color: 'var(--text-primary)', fontSize: '13px',
                outline: 'none',
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
