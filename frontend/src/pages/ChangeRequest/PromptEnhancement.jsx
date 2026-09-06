// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useState, useRef, useEffect } from 'react'
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
import { ArrowLeft, ArrowRight, Send, Loader, Wifi, WifiOff, Wand2 } from 'lucide-react'
import ReactMarkdown from 'react-markdown'

// Renders a single chat message bubble
function MessageBubble({ role, content }) {
  const isUser = role === 'user'
  return (
    <div style={{
      display: 'flex',
      justifyContent: isUser ? 'flex-end' : 'flex-start',
      marginBottom: '16px',
    }}>
      {!isUser && (
        <div style={{
          width: '28px', height: '28px', borderRadius: '50%',
          background: 'var(--accent)', color: 'white',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontSize: '11px', fontWeight: '700', flexShrink: 0, marginRight: '10px',
          marginTop: '2px',
        }}>AI</div>
      )}
      <div style={{
        maxWidth: '78%',
        padding: '12px 16px',
        borderRadius: isUser ? '12px 12px 4px 12px' : '12px 12px 12px 4px',
        background: isUser ? 'var(--accent)' : 'var(--bg-elevated)',
        color: isUser ? 'white' : 'var(--text-primary)',
        border: isUser ? 'none' : '1px solid var(--border)',
        fontSize: '13px',
        lineHeight: '1.65',
      }}>
        {isUser ? (
          <p style={{ margin: 0 }}>{content}</p>
        ) : (
          <div className="md-content" style={{ margin: 0 }}>
            <ReactMarkdown>{content}</ReactMarkdown>
          </div>
        )}
      </div>
      {isUser && (
        <div style={{
          width: '28px', height: '28px', borderRadius: '50%',
          background: 'var(--bg-elevated)', border: '1px solid var(--border)',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontSize: '11px', fontWeight: '700', flexShrink: 0, marginLeft: '10px',
          marginTop: '2px', color: 'var(--text-muted)',
        }}>ME</div>
      )}
    </div>
  )
}

export default function PromptEnhancement() {
  const { id } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [input, setInput] = useState('')
  // Once the prompt is ready the composer collapses to a single "Refine prompt"
  // action so Proceed stays the primary path; opening it re-exposes the chat
  // input, and the backend treats that turn as a rewrite of the existing prompt.
  const [refineOpen, setRefineOpen] = useState(false)
  const bottomRef = useRef(null)

  const { data: change } = useQuery({
    queryKey: ['change', id],
    queryFn: () => changesApi.get(id).then(r => r.data),
  })

  // Phase 7 — eval verdict for this step's checkpoint (initial_to_prompt_enhanced)
  const { data: gateEval } = useQuery({
    queryKey: ['eval-latest', id, 'initial_to_prompt_enhanced'],
    queryFn: () => evalApi.latest(id, 'initial_to_prompt_enhanced').then(r => r.data),
    enabled: !!id,
    refetchInterval: (query) => query.state.data?.verdict?.id ? 8000 : 2000,
    refetchOnWindowFocus: true,
  })

  // R-4 — useResumableJob is a drop-in superset of useAgentWS that surfaces
  // jobId/jobStage/jobProgress/jobStartedAt/isResuming/cancelJob.
  const {
    messages, streaming, streamingText, ready, enhancedPrompt,
    connected, historyLoaded, error, sendMessage,
    jobId, jobStatus, jobStage, jobProgress, jobStartedAt, jobStartedBy,
    isResuming, cancelJob,
  } = useResumableJob(id, 'enhance')

  const { user: me } = useAuth()
  const { openGateModal } = useGateModal()

  // Auto-scroll
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, streamingText])

  useEffect(() => {
    if (!ready) return
    queryClient.invalidateQueries({ queryKey: ['eval-latest', id, 'initial_to_prompt_enhanced'] })
  }, [ready, id, queryClient])

  // Auto-send the initial prompt only when:
  // 1. History has been confirmed loaded from server (historyLoaded=true)
  // 2. The conversation is genuinely empty (no prior user messages)
  // 3. We haven't already sent in this session (sentInitialRef guard)
  const sentInitialRef = useRef(false)
  useEffect(() => {
    const hasExistingUserMsg = messages.some(m => m.role === 'user')
    if (
      historyLoaded &&
      !hasExistingUserMsg &&
      !sentInitialRef.current &&
      change?.initial_prompt
    ) {
      sentInitialRef.current = true
      sendMessage(change.initial_prompt)
    }
  }, [historyLoaded, messages, change, sendMessage])

  const handleSend = () => {
    const text = input.trim()
    if (!text || streaming) return
    setInput('')
    sendMessage(text)
  }

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend() }
  }

  const handleProceed = async () => {
    if (!enhancedPrompt) return
    try {
      await api.post(`/changes/${id}/advance`, { enhanced_prompt: enhancedPrompt })
      queryClient.invalidateQueries({ queryKey: ['change', id] })
      navigate(`/changes/${id}/research`)
    } catch (err) {
      if (isEvalGateError(err)) {
        openGateModal({
          changeId: id,
          detail: err.response?.data?.detail,
          actionLabel: 'Proceed to Deep Research',
          retryAction: (payload) => api.post(`/changes/${id}/advance`, {
            enhanced_prompt: enhancedPrompt,
            ...(payload || {}),
          }),
          onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ['change', id] })
            navigate(`/changes/${id}/research`)
          },
        })
      } else {
        console.error('advance failed', err)
      }
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: 'calc(100vh - 0px)', overflow: 'hidden' }}>

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
            Prompt Enhancement
          </h1>
          <p style={{ margin: 0, fontSize: '11px', color: 'var(--text-muted)' }}>
            {change?.title || 'Clarifying your idea with AI'}
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
          checkpointLabel="Initial → Enhanced Prompt"
          changeId={id}
          checkpointId="initial_to_prompt_enhanced"
          onAutoFix={(text) => sendMessage(text)}
        />
        <TranscriptsDownloadButton changeId={id} section="prompt_enhancement" label="Transcripts" />
        <SkipStepButton changeId={id} nextRoute={`/changes/${id}`} />
        {ready && (
          <button
            onClick={handleProceed}
            // A refinement in flight will replace enhancedPrompt — advancing now
            // would carry the pre-refinement text forward.
            disabled={streaming}
            style={{
              display: 'flex', alignItems: 'center', gap: '8px',
              padding: '8px 18px', background: 'var(--accent)', color: 'white',
              border: 'none', borderRadius: '6px', fontSize: '13px',
              fontWeight: '600', cursor: streaming ? 'not-allowed' : 'pointer',
              opacity: streaming ? 0.5 : 1,
            }}
          >
            Proceed to Deep Research <ArrowRight size={14} />
          </button>
        )}
      </div>

      {/* ── Chat area ── */}
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
            <p style={{ margin: 0, fontSize: '12px', color: 'var(--text-secondary)' }}>
              {error}
            </p>
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

        {messages.map((m, i) => (
          <MessageBubble key={i} role={m.role} content={m.content} />
        ))}

        {/* Streaming token accumulator */}
        {streaming && streamingText && (
          <MessageBubble role="assistant" content={streamingText + '▌'} />
        )}

        {/* Waiting indicator */}
        {streaming && !streamingText && (
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '16px' }}>
            <div style={{
              width: '28px', height: '28px', borderRadius: '50%',
              background: 'var(--accent)', color: 'white',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              fontSize: '11px', fontWeight: '700',
            }}>AI</div>
            <div style={{
              padding: '10px 16px',
              background: 'var(--bg-elevated)', border: '1px solid var(--border)',
              borderRadius: '12px 12px 12px 4px',
              display: 'flex', alignItems: 'center', gap: '6px',
            }}>
              <Loader size={14} style={{ color: 'var(--accent)', animation: 'spin 1s linear infinite' }} />
              <span style={{ fontSize: '13px', color: 'var(--text-muted)' }}>Thinking…</span>
            </div>
          </div>
        )}

        <div ref={bottomRef} />
      </div>

      {/* ── Refine affordance — the composer's collapsed state once ready ── */}
      {ready && !refineOpen && (
        <div style={{
          padding: '14px 24px', borderTop: '1px solid var(--border)',
          background: 'var(--bg-base)', flexShrink: 0,
          display: 'flex', alignItems: 'center', gap: '12px',
        }}>
          <p style={{ margin: 0, flex: 1, fontSize: '12px', color: 'var(--text-muted)' }}>
            The enhanced prompt is ready. Proceed when you're happy with it — or ask for changes first.
          </p>
          <button
            onClick={() => setRefineOpen(true)}
            style={{
              display: 'flex', alignItems: 'center', gap: '7px',
              padding: '8px 16px', background: 'var(--bg-elevated)',
              color: 'var(--text-primary)', border: '1px solid var(--border)',
              borderRadius: '6px', fontSize: '13px', fontWeight: '600',
              cursor: 'pointer', flexShrink: 0,
            }}
          >
            <Wand2 size={14} /> Refine prompt
          </button>
        </div>
      )}

      {/* ── Input bar ── */}
      {(!ready || refineOpen) && (
        <div style={{
          padding: '16px 24px', borderTop: '1px solid var(--border)',
          background: 'var(--bg-base)', flexShrink: 0,
        }}>
          {ready && (
            <div style={{
              display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '10px',
            }}>
              <Wand2 size={13} style={{ color: 'var(--accent)' }} />
              <span style={{ fontSize: '12px', fontWeight: '600', color: 'var(--text-primary)' }}>
                Refine the enhanced prompt
              </span>
              <button
                onClick={() => { setRefineOpen(false); setInput('') }}
                disabled={streaming}
                style={{
                  marginLeft: 'auto', background: 'none', border: 'none',
                  fontSize: '12px', color: 'var(--text-muted)',
                  cursor: streaming ? 'not-allowed' : 'pointer',
                }}
              >
                Cancel
              </button>
            </div>
          )}
          <div style={{ display: 'flex', gap: '10px', alignItems: 'flex-end' }}>
            <textarea
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={ready
                ? 'Describe the change — e.g. "cap the transaction note at 50 characters and call out the SGF impact"'
                : 'Type your answer… (Enter to send, Shift+Enter for new line)'}
              rows={3}
              style={{
                flex: 1, padding: '10px 14px',
                background: 'var(--bg-input)', border: '1px solid var(--border)',
                borderRadius: '8px', color: 'var(--text-primary)', fontSize: '13px',
                resize: 'none', outline: 'none', lineHeight: '1.6',
                fontFamily: 'inherit',
              }}
              onFocus={e => e.target.style.borderColor = 'var(--accent)'}
              onBlur={e => e.target.style.borderColor = 'var(--border)'}
            />
            <button
              onClick={handleSend}
              disabled={!input.trim() || streaming || !connected}
              style={{
                width: '44px', height: '44px',
                background: 'var(--accent)', color: 'white',
                border: 'none', borderRadius: '8px',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                cursor: (!input.trim() || streaming || !connected) ? 'not-allowed' : 'pointer',
                opacity: (!input.trim() || streaming || !connected) ? 0.5 : 1,
                transition: 'opacity 0.15s',
                flexShrink: 0,
              }}
            >
              {streaming ? <Loader size={16} /> : <Send size={16} />}
            </button>
          </div>
          <p style={{ margin: '6px 0 0', fontSize: '11px', color: 'var(--text-muted)' }}>
            {ready
              ? 'The AI rewrites the full prompt with your change applied. Refine as many times as you need, then Proceed.'
              : "Answer the AI's questions — it will declare the prompt ready when it has enough context."}
          </p>
        </div>
      )}
    </div>
  )
}
