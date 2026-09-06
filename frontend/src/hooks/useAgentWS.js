// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

/**
 * useAgentWS — React hook for a streaming agent WebSocket connection.
 *
 * Opens a WebSocket to ws(s)://<host>/api/ws/changes/<changeId>/<module>,
 * authenticates with the stored JWT, and exposes:
 *   - messages: [{role, content}]   full conversation history
 *   - streaming: bool               true while a token stream is in progress
 *   - streamingText: str            accumulation of the current stream
 *   - ready: bool                   true when enhancer declares <<PROMPT_READY>>
 *   - enhancedPrompt: str|null      final enriched prompt
 *   - sendMessage(text)             send a user turn
 *   - close()                       close the socket
 *
 * Resilience:
 *   - Auto-reconnect on unexpected close (TCP RST, proxy timeout, network blip)
 *     with exponential backoff. Manual close() suppresses reconnect.
 *   - Stale-connection watchdog: if a stream is in-flight but no chunk has
 *     arrived in STALE_STREAM_MS, force-close to trigger a reconnect AND
 *     clear streaming state so `useResumableJob`'s REST-polled fallback
 *     becomes the visible source of truth. Suppressed while `serverBusyRef`
 *     says the durable job is still running — see the watchdog effect.
 */
import { useState, useRef, useCallback, useEffect } from 'react'
import { wsUrl } from '../utils/basePath'

// Auto-reconnect backoff bounds. Each consecutive close doubles the delay
// up to RECONNECT_MAX_MS so we don't hammer a backend that's actually down.
const RECONNECT_BASE_MS = 1000
const RECONNECT_MAX_MS  = 30000
// Stale-stream watchdog. If streaming==true but no chunk has arrived in
// this many ms, the WS is presumed dead at the network layer (browsers
// don't surface TCP-level keepalive timeouts to JS reliably). We force a
// close to flush the stale `streaming=true` state and trigger reconnect.
const STALE_CHECK_INTERVAL_MS = 15000
const STALE_STREAM_MS         = 45000

export function useAgentWS(changeId, module, serverBusyRef) {
  const [messages, setMessages]         = useState([])    // [{role, content}]
  const [streaming, setStreaming]       = useState(false)
  const [streamingText, setStreamingText] = useState('')
  const [ready, setReady]               = useState(false)
  const [enhancedPrompt, setEnhancedPrompt] = useState(null)
  const [connected, setConnected]       = useState(false)
  const [historyLoaded, setHistoryLoaded] = useState(false)
  const [error, setError]               = useState(null)
  const [validation, setValidation]     = useState(null)  // {error_count, warning_count, has_errors, issues: [...]}
  // Docgen merge (Session 20+) — backend WS handlers now optionally include
  // `docgen_job_id` on the `done` payload when the LangGraph pipeline ran.
  // BRD.jsx / TechSpec.jsx / ProductKit.jsx read this to populate the
  // section-wise edit dropdown via docgenApi.sections().
  const [docgenJobId, setDocgenJobId]   = useState(null)
  // The durable agent-job id the backend announces at generation START (before any chunk),
  // so JobsContext can track the running job immediately — without this, a navigate-away
  // mid-generation left no trackable job and the page reverted to the idle "Generate" state.
  const [liveJobId, setLiveJobId]       = useState(null)
  // Doc↔plan consistency result (BRD/TSD): findings where the doc invented an API/message/schema
  // the ratified plan doesn't have. Arrives on the `done` payload; drives a divergence banner.
  const [docConsistency, setDocConsistency] = useState(null)

  const wsRef     = useRef(null)
  const bufferRef = useRef('')   // accumulate the current stream turn
  // StrictMode-safe close: in dev, React mounts → unmounts → re-mounts every
  // component to test cleanup robustness. A naive `close()` on unmount kills
  // the WS mid-handshake before the second mount can re-use it. We defer the
  // close by 80 ms; if a new mount fires within that window, it cancels the
  // pending close and the original socket stays alive.
  const closeTimerRef     = useRef(null)
  const manualCloseRef    = useRef(false)
  const reconnectAttemptsRef = useRef(0)
  // Seeded on mount rather than in the useRef initialiser: Date.now() during
  // render is impure, and leaving it 0 would make the first comparison read as
  // an enormous elapsed time — idle detection is measured from the last activity, seeded at mount.
  useEffect(() => { lastActivityRef.current = Date.now() }, [])
  const reconnectTimerRef = useRef(null)
  const lastActivityRef   = useRef(0)
  const lastChunkRef      = useRef(0)      // time of the last CONTENT chunk (not any msg)
  const finalizedRef      = useRef(false)  // this turn already finalized (done or synthetic)
  const streamingRef      = useRef(false)
  const staleCheckTimerRef = useRef(null)

  const clearStreamState = useCallback(() => {
    bufferRef.current = ''
    streamingRef.current = false
    setStreamingText('')
    setStreaming(false)
  }, [])

  // Graceful completion fallback (used by the stale-stream watchdog): if a
  // stream goes quiet without a `done` event, DON'T discard what we have —
  // commit the buffered text as the assistant message and clear `streaming`
  // so the page shows the result + its "Proceed" action instead of an endless
  // "Generating…". Idempotent: a later real `done` won't double-append
  // (guarded by finalizedRef).
  const finalizeStream = useCallback((reason) => {
    if (finalizedRef.current) { streamingRef.current = false; setStreaming(false); return }
    finalizedRef.current = true
    const fullText = bufferRef.current
    bufferRef.current = ''
    streamingRef.current = false
    setStreamingText('')
    setStreaming(false)
    if (fullText) setMessages(prev => [...prev, { role: 'assistant', content: fullText }])
    window.dispatchEvent(new CustomEvent('agent-ws-done', {
      detail: { changeId, module, ready: false, hasValidation: false, reason: reason || 'stale' },
    }))
  }, [changeId, module])

  const connect = useCallback(() => {
    // Cancel any pending deferred close from the previous (StrictMode) cleanup.
    if (closeTimerRef.current) {
      clearTimeout(closeTimerRef.current)
      closeTimerRef.current = null
    }
    if (wsRef.current) return   // already open
    manualCloseRef.current = false

    const url = wsUrl(`api/ws/changes/${changeId}/${module}`)
    const ws  = new WebSocket(url)
    wsRef.current = ws
    lastActivityRef.current = Date.now()

    ws.onopen = () => {
      setConnected(true)
      setError(null)
      reconnectAttemptsRef.current = 0
      lastActivityRef.current = Date.now()
      // Hello frame. No token: the session cookie is httpOnly and the
      // browser attaches it to the WebSocket upgrade automatically, so it
      // is neither sent nor readable here. The frame is still required —
      // the server reads one before it starts streaming.
      ws.send(JSON.stringify({}))
    }

    ws.onclose = () => {
      setConnected(false)
      wsRef.current = null
      // A close mid-stream means we'll never see `done` — flush the stale
      // streaming state so useResumableJob's REST-polled replay becomes
      // visible. If we're still actually streaming server-side, the
      // resume polling continues to deliver chunks.
      if (streamingRef.current) clearStreamState()

      // Unexpected close → reconnect with backoff. Manual close (component
      // unmount, explicit close() call) sets manualCloseRef and suppresses.
      if (!manualCloseRef.current) {
        const attempt = reconnectAttemptsRef.current
        reconnectAttemptsRef.current = attempt + 1
        const delay = Math.min(RECONNECT_MAX_MS, RECONNECT_BASE_MS * (2 ** attempt))
        if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current)
        reconnectTimerRef.current = setTimeout(() => {
          reconnectTimerRef.current = null
          // Guard: if the component unmounted in the meantime, skip.
          if (!manualCloseRef.current) connect()
        }, delay)
      }
    }

    ws.onerror = () => {
      setError('WebSocket connection failed')
      setConnected(false)
    }

    ws.onmessage = (evt) => {
      lastActivityRef.current = Date.now()
      const data = JSON.parse(evt.data)

      if (data.type === 'history') {
        // Restore prior conversation; signal that history has been loaded
        setMessages(data.messages || [])
        setHistoryLoaded(true)
        // Restore ready/enhancedPrompt state if prompt enhancement was already completed
        if (data.ready) {
          setReady(true)
          setEnhancedPrompt(data.enhanced_prompt)
        }
        return
      }

      if (data.type === 'job_id') {
        // R-3 — backend registered the durable job and is about to stream. Capture it NOW so
        // useResumableJob records it in JobsContext → a re-mount mid-generation finds the
        // running job and shows the loader / replays chunks instead of reverting to idle.
        if (data.job_id) setLiveJobId(data.job_id)
        return
      }

      if (data.type === 'chunk') {
        bufferRef.current += data.text
        setStreamingText(bufferRef.current)
        streamingRef.current = true
        finalizedRef.current = false          // fresh content → a new turn to finalize
        lastChunkRef.current = Date.now()      // watchdog measures silence since this
        setStreaming(true)
        return
      }

      if (data.type === 'done') {
        const wasFinalized = finalizedRef.current   // watchdog may have already committed the buffer
        finalizedRef.current = true
        const fullText = data.full || bufferRef.current
        bufferRef.current = ''
        streamingRef.current = false
        setStreamingText('')
        setStreaming(false)
        if (!wasFinalized) setMessages(prev => [...prev, { role: 'assistant', content: fullText }])
        window.dispatchEvent(new CustomEvent('agent-ws-done', {
          detail: { changeId, module, ready: Boolean(data.ready), hasValidation: Boolean(data.validation) },
        }))
        if (data.ready) {
          setReady(true)
          setEnhancedPrompt(data.enhanced_prompt)
        }
        if (data.validation) {
          setValidation(data.validation)
        }
        if (data.docgen_job_id) {
          setDocgenJobId(data.docgen_job_id)
        }
        if (data.doc_consistency) {
          setDocConsistency(data.doc_consistency)
        }
        return
      }

      if (data.type === 'error') {
        setError(data.detail || 'Unknown error')
        streamingRef.current = false
        setStreaming(false)
        bufferRef.current = ''
        setStreamingText('')
      }
    }
  }, [changeId, module, clearStreamState])

  const sendMessage = useCallback((text) => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return
    setMessages(prev => [...prev, { role: 'user', content: text }])
    bufferRef.current = ''
    finalizedRef.current = false
    setStreamingText('')
    setValidation(null)
    wsRef.current.send(JSON.stringify({ message: text }))
  }, [])

  const close = useCallback(() => {
    // Immediate close (used by manual `close()` callers — not by the StrictMode
    // unmount path, which uses the deferred close in the effect cleanup below).
    if (closeTimerRef.current) {
      clearTimeout(closeTimerRef.current)
      closeTimerRef.current = null
    }
    manualCloseRef.current = true
    if (reconnectTimerRef.current) {
      clearTimeout(reconnectTimerRef.current)
      reconnectTimerRef.current = null
    }
    wsRef.current?.close()
    wsRef.current = null
  }, [])

  // Connect on mount, disconnect on unmount.
  // Cleanup defers the close by 80 ms so that a StrictMode immediate remount
  // can cancel it (see `connect()` above) — the same socket survives the
  // mount/unmount/mount cycle and we don't see "closed before connection
  // established" in the console.
  useEffect(() => {
    connect()
    return () => {
      manualCloseRef.current = true
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current)
        reconnectTimerRef.current = null
      }
      const ws = wsRef.current
      if (!ws) return
      closeTimerRef.current = setTimeout(() => {
        if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) {
          ws.close()
        }
        if (wsRef.current === ws) wsRef.current = null
        closeTimerRef.current = null
      }, 80)
    }
  }, [connect, close])

  // Stale-stream watchdog. Runs every STALE_CHECK_INTERVAL_MS while mounted.
  // Measures silence since the last CONTENT chunk (not any message), so a
  // trailing non-chunk frame can't keep "Generating…" alive forever. Token
  // streams emit chunks sub-second; STALE_STREAM_MS of no chunk means the
  // stream finished (and we likely missed `done`) or died. Either way we
  // FINALIZE GRACEFULLY — commit the buffered text as the result and clear
  // `streaming` — instead of force-closing and discarding it, so the page
  // recovers without a manual refresh. If nothing was buffered yet, fall back
  // to the old force-close (lets reconnect + REST-replay take over).
  useEffect(() => {
    staleCheckTimerRef.current = setInterval(() => {
      if (!streamingRef.current) return
      const idleMs = Date.now() - (lastChunkRef.current || lastActivityRef.current)
      if (idleMs < STALE_STREAM_MS) return
      // Silence is not proof of death. The plan-fidelity checker and every
      // auto-correction round are long LLM calls that emit no chunk, so a
      // healthy generation sits quiet for minutes. While the durable job still
      // reads `running` — polled over REST, a channel independent of this
      // socket — finalizing here would commit the partial turn as a completed
      // one, which the page renders as an extra "v2 / v3 …" revision chip per
      // correction and which unlocks Proceed mid-generation. Suppression lifts
      // the moment the job reaches a terminal state, re-arming the recovery
      // paths below for a stream that really is dead.
      if (serverBusyRef?.current) return
      if (bufferRef.current) {
        finalizeStream('stale')   // keep the streamed content, show Proceed
        return
      }
      const ws = wsRef.current
      if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
        try { ws.close() } catch { /* ignore */ }
      }
    }, STALE_CHECK_INTERVAL_MS)
    return () => {
      if (staleCheckTimerRef.current) {
        clearInterval(staleCheckTimerRef.current)
        staleCheckTimerRef.current = null
      }
    }
  }, [finalizeStream, serverBusyRef])

  return {
    messages,
    streaming,
    streamingText,
    ready,
    enhancedPrompt,
    connected,
    historyLoaded,
    error,
    validation,
    sendMessage,
    docgenJobId,
    liveJobId,
    docConsistency,
  }
}
