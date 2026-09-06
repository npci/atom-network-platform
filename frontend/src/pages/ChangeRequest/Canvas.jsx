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
import { ArrowLeft, ArrowRight, Send, Loader, RefreshCw, Wifi, WifiOff, Layout, Download } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import ValidationPanel from '../../components/common/ValidationPanel'

export default function Canvas() {
  const { id } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [feedback, setFeedback]   = useState('')
  const [started, setStarted]     = useState(false)
  const [advancing, setAdvancing] = useState(false)
  const [downloading, setDownloading] = useState(false)
  const bottomRef = useRef(null)

  const { data: _change } = useQuery({
    queryKey: ['change', id],
    queryFn: () => changesApi.get(id).then(r => r.data),
  })

  // Phase 7 — eval verdict for the product canvas (research_to_canvas)
  const { data: gateEval } = useQuery({
    queryKey: ['eval-latest', id, 'research_to_canvas'],
    queryFn: () => evalApi.latest(id, 'research_to_canvas').then(r => r.data),
    enabled: !!id,
    refetchInterval: 8000,
    refetchOnWindowFocus: true,
  })

  // R-4 — useResumableJob is a drop-in superset of useAgentWS.
  const {
    messages, streaming, streamingText,
    connected, historyLoaded, error, validation, sendMessage,
    jobId, jobStatus, jobStage, jobProgress, jobStartedAt, jobStartedBy,
    isResuming, cancelJob,
  } = useResumableJob(id, 'canvas')

  const { user: me } = useAuth()
  const { openGateModal } = useGateModal()

  const assistantMsgs = messages.filter(m => m.role === 'assistant')
  const currentCanvas = assistantMsgs[assistantMsgs.length - 1]?.content || null
  const isFirst = assistantMsgs.length === 0

  useEffect(() => {
    if (streaming) bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [streaming, streamingText])

  // If canvas already exists on load, mark as started
  useEffect(() => {
    if (currentCanvas) setStarted(true)
  }, [currentCanvas])

  const handleStart = () => {
    setStarted(true)
    sendMessage('start')
  }

  // Auto-start canvas generation the first time this page is opened
  // after the user proceeds from Deep Research. Same pattern as Research.jsx.
  const autoStartedRef = useRef(false)
  useEffect(() => {
    if (
      !autoStartedRef.current &&
      historyLoaded &&
      isFirst &&
      !streaming &&
      !started &&
      connected
    ) {
      autoStartedRef.current = true
      handleStart()
    }
  }, [historyLoaded, isFirst, streaming, started, connected])

  const handleFeedback = () => {
    const text = feedback.trim()
    if (!text || streaming) return
    setFeedback('')
    sendMessage(text)
  }

  const handleDownload = async () => {
    setDownloading(true)
    try {
      // `credentials: 'include'` sends the httpOnly session cookie; fetch
      // omits cookies unless asked.
      const res = await fetch(`${api.defaults.baseURL}/changes/${id}/canvas/download`, {
        credentials: 'include',
      })
      if (!res.ok) throw new Error('Download failed')
      const blob = await res.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `product_canvas.docx`
      a.click()
      URL.revokeObjectURL(url)
    } catch (err) {
      console.error('download failed', err)
    } finally {
      setDownloading(false)
    }
  }

  const handleProceed = async () => {
    setAdvancing(true)
    try {
      await api.post(`/changes/${id}/advance`, {})
      queryClient.invalidateQueries({ queryKey: ['change', id] })
      navigate(`/changes/${id}/clarify`)
    } catch (err) {
      if (isEvalGateError(err)) {
        openGateModal({
          changeId: id,
          detail: err.response?.data?.detail,
          actionLabel: 'Finalize Canvas and generate BRD',
          retryAction: (payload) => api.post(`/changes/${id}/advance`, payload || {}),
          onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ['change', id] })
            navigate(`/changes/${id}/clarify`)
          },
        })
      } else {
        console.error('advance failed', err)
      }
    } finally {
      setAdvancing(false)
    }
  }

  const canvasContent = streaming ? (streamingText || '') : (currentCanvas || '')

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: 'calc(100vh - 0px)', overflow: 'hidden' }}>

      {/* Header */}
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
            Product Canvas
          </h1>
          <p style={{ margin: 0, fontSize: '11px', color: 'var(--text-muted)' }}>
            {t('canvas.framework')} · 10 sections · Download as .docx
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
          checkpointLabel="Research → Canvas"
          changeId={id}
          checkpointId="research_to_canvas"
          onAutoFix={(text) => { setStarted(true); sendMessage(text) }}
        />
        <TranscriptsDownloadButton changeId={id} section="canvas" label="Transcripts" />
        <SkipStepButton changeId={id} nextRoute={`/changes/${id}`} />
        {currentCanvas && !streaming && (
          <div style={{ display: 'flex', gap: '8px' }}>
            <button
              onClick={handleDownload}
              disabled={downloading}
              style={{
                display: 'flex', alignItems: 'center', gap: '6px',
                padding: '8px 14px', background: 'var(--bg-elevated)', color: 'var(--text-secondary)',
                border: '1px solid var(--border)', borderRadius: '6px', fontSize: '13px',
                fontWeight: '500', cursor: downloading ? 'not-allowed' : 'pointer',
                opacity: downloading ? 0.6 : 1,
              }}
            >
              {downloading
                ? <Loader size={13} style={{ animation: 'spin 1s linear infinite' }} />
                : <Download size={13} />}
              Download .docx
            </button>
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
              {advancing ? <Loader size={13} style={{ animation: 'spin 1s linear infinite' }} /> : null}
              Finalize Canvas → Generate BRD <ArrowRight size={14} />
            </button>
          </div>
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
                marginTop: '10px', padding: '6px 14px',
                background: 'var(--accent)', color: 'white',
                border: 'none', borderRadius: '6px', fontSize: '12px', fontWeight: '600', cursor: 'pointer',
              }}>Retry</button>
            )}
          </div>
        )}

        {/* Generating — survives re-mount: a durable job in flight shows the loader (the local
            `started` flag resets on navigation, so we also key off jobStatus/isResuming). */}
        {(jobStatus === 'running' || isResuming) && !streaming && !currentCanvas && historyLoaded && (
          <div style={{
            textAlign: 'center', padding: '64px 32px',
            background: 'var(--bg-elevated)', border: '1px solid var(--border)', borderRadius: '12px',
          }}>
            <Layout size={40} style={{ color: 'var(--accent)', marginBottom: '16px' }} />
            <h2 style={{ margin: '0 0 8px', fontSize: '18px', fontWeight: '600', color: 'var(--text-primary)' }}>
              Generating Product Canvas…
            </h2>
            <div style={{ display: 'inline-flex', alignItems: 'center', gap: '10px', color: 'var(--accent)', marginTop: 8 }}>
              <Loader size={16} style={{ animation: 'spin 1s linear infinite' }} />
              <span style={{ fontSize: '13px', fontWeight: '600' }}>{connected ? 'Generating…' : 'Reconnecting…'}</span>
            </div>
          </div>
        )}

        {/* Not started — suppressed when a durable job is already in flight (re-mount), so the
            page never falsely reverts to the "click Generate" prompt over a running job. */}
        {!started && isFirst && !streaming && historyLoaded && jobStatus !== 'running' && !isResuming && (
          <div style={{
            textAlign: 'center', padding: '64px 32px',
            background: 'var(--bg-elevated)', border: '1px solid var(--border)',
            borderRadius: '12px',
          }}>
            <Layout size={40} style={{ color: 'var(--accent)', marginBottom: '16px' }} />
            <h2 style={{ margin: '0 0 8px', fontSize: '18px', fontWeight: '600', color: 'var(--text-primary)' }}>
              Ready to generate Product Canvas
            </h2>
            <p style={{ margin: '0 auto 24px', fontSize: '13px', color: 'var(--text-muted)', maxWidth: '460px' }}>
              The AI will synthesise the research findings into the {t('canvas.framework')} canvas —
              10 sections covering Feature, Need, Market View, Scalability, Validation, Product
              Operating, Product Comms, Pricing, Risks, and Compliance. Exportable as .docx.
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
              Generate Canvas
            </button>
          </div>
        )}

        {/* Validation panel */}
        {!streaming && validation && <ValidationPanel validation={validation} />}

        {/* Canvas output */}
        {(streaming || canvasContent) && (
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
                <span style={{ fontSize: '12px', color: 'var(--accent)' }}>Generating product canvas…</span>
              </div>
            )}
            <div className="md-content" style={{ fontSize: '14px', lineHeight: '1.8', color: 'var(--text-primary)' }}>
              <ReactMarkdown>{canvasContent + (streaming ? '▌' : '')}</ReactMarkdown>
            </div>
            <div ref={bottomRef} />
          </div>
        )}

        {/* Revision pills */}
        {assistantMsgs.length > 1 && (
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
      {(currentCanvas || started) && !streaming && (
        <div style={{
          padding: '16px 24px', borderTop: '1px solid var(--border)',
          background: 'var(--bg-base)', flexShrink: 0,
        }}>
          <p style={{ margin: '0 0 10px', fontSize: '12px', color: 'var(--text-muted)', fontWeight: '500' }}>
            {currentCanvas
              ? 'Provide feedback to refine the canvas, or finalize and proceed to BRD above.'
              : 'Canvas is generating…'}
          </p>
          <div style={{ display: 'flex', gap: '10px' }}>
            <input
              value={feedback}
              onChange={e => setFeedback(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') handleFeedback() }}
              placeholder={t('ph.canvas.refine')}
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
