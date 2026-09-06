// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useState, useEffect, useRef, useCallback } from 'react'
import api, { logsApi } from '../../services/api'
import { RefreshCw, Pause, Play, Trash2, Download } from 'lucide-react'

const LEVEL_COLORS = {
  DEBUG:    { color: '#9a9a96',   bg: 'transparent' },
  INFO:     { color: '#6ea8dc',   bg: 'transparent' },
  WARNING:  { color: '#e8a44a',   bg: 'rgba(232,164,74,0.08)' },
  ERROR:    { color: '#e06c6c',   bg: 'rgba(224,108,108,0.10)' },
  CRITICAL: { color: '#ff5f5f',   bg: 'rgba(255,95,95,0.14)' },
}

const ALL_LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']

function LevelBadge({ level }) {
  const cfg = LEVEL_COLORS[level] || LEVEL_COLORS.INFO
  return (
    <span style={{
      display: 'inline-block', minWidth: '62px', textAlign: 'center',
      padding: '1px 7px', borderRadius: '4px', fontSize: '10px',
      fontWeight: '700', letterSpacing: '0.06em',
      color: cfg.color, background: cfg.bg,
      border: `1px solid ${cfg.color}44`,
      fontFamily: 'var(--font-mono, monospace)',
    }}>
      {level}
    </span>
  )
}

function formatTs(iso) {
  const d = new Date(iso)
  return d.toLocaleTimeString('en-GB', { hour12: false }) +
    '.' + String(d.getMilliseconds()).padStart(3, '0')
}

function LogRow({ entry, style }) {
  return (
    <div style={{
      display: 'grid',
      gridTemplateColumns: '72px 74px 180px 1fr',
      gap: '10px',
      padding: '3px 14px',
      alignItems: 'baseline',
      background: LEVEL_COLORS[entry.level]?.bg || 'transparent',
      borderBottom: '1px solid var(--border-subtle)',
      fontFamily: 'var(--font-mono, monospace)',
      fontSize: '12px',
      lineHeight: '1.55',
      ...style,
    }}>
      <span style={{ color: 'var(--text-muted)', whiteSpace: 'nowrap' }}>
        {formatTs(entry.ts)}
      </span>
      <LevelBadge level={entry.level} />
      <span style={{
        color: 'var(--text-muted)', overflow: 'hidden',
        textOverflow: 'ellipsis', whiteSpace: 'nowrap',
      }} title={entry.logger}>
        {entry.logger}
      </span>
      <span style={{ color: 'var(--text-primary)', wordBreak: 'break-word' }}>
        {entry.message}
        {entry.exc && (
          <pre style={{
            margin: '4px 0 0', padding: '6px 8px',
            background: 'rgba(224,108,108,0.08)', borderRadius: '4px',
            fontSize: '11px', color: 'var(--danger)', overflowX: 'auto',
            whiteSpace: 'pre-wrap', wordBreak: 'break-all',
          }}>{entry.exc}</pre>
        )}
      </span>
    </div>
  )
}

export default function Logs() {
  const [entries, setEntries] = useState([])
  const [filter, setFilter]   = useState('')           // text search
  const [levels, setLevels]   = useState(new Set(ALL_LEVELS))
  const [paused, setPaused]   = useState(false)
  const [connected, setConnected] = useState(false)
  const [fetchError, setFetchError] = useState(null)

  const bottomRef  = useRef(null)
  const pausedRef  = useRef(false)
  const esRef      = useRef(null)
  const bufferRef  = useRef([])   // holds entries received while paused

  // Keep ref in sync with state (used inside event handler closure)
  useEffect(() => { pausedRef.current = paused }, [paused])

  // Derive SSE URL from the same axios baseURL so it always matches
  const sseBase = api.defaults.baseURL || '/api'

  // ── Load recent history once ──────────────────────────────────────────────
  useEffect(() => {
    setFetchError(null)
    logsApi.recent(500)
      .then(res => setEntries(res.data?.entries || []))
      .catch((err) => setFetchError(`Failed to load logs: ${err.response?.status || err.message}`))
  }, [])

  // ── SSE stream ────────────────────────────────────────────────────────────
  useEffect(() => {
    const ctrl = new AbortController()

    // `credentials: 'include'` sends the httpOnly session cookie. fetch
    // does NOT send cookies by default on all paths, so this is required —
    // unlike axios, where it is configured once on the instance.
    fetch(`${sseBase}/logs/stream`, {
      credentials: 'include',
      signal: ctrl.signal,
    }).then(async (res) => {
      if (!res.ok || !res.body) {
        setFetchError(`SSE stream failed: HTTP ${res.status}`)
        return
      }
      setConnected(true)
      setFetchError(null)
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let partial = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        partial += decoder.decode(value, { stream: true })
        const lines = partial.split('\n')
        partial = lines.pop()  // keep incomplete line
        for (const line of lines) {
          if (!line.startsWith('data:')) continue
          try {
            const entry = JSON.parse(line.slice(5).trim())
            if (pausedRef.current) {
              bufferRef.current.push(entry)
            } else {
              setEntries(prev => [...prev.slice(-1999), entry])
            }
          } catch { /* malformed log frame; drop it rather than break the stream */ }
        }
      }
    }).catch((err) => {
      if (err.name !== 'AbortError') setFetchError(`SSE connection error: ${err.message}`)
    }).finally(() => setConnected(false))

    esRef.current = ctrl
    return () => ctrl.abort()
  }, [sseBase])

  // ── Auto-scroll to bottom when not paused ────────────────────────────────
  useEffect(() => {
    if (!paused) bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [entries, paused])

  // ── Resume: flush buffer ──────────────────────────────────────────────────
  const resume = useCallback(() => {
    setPaused(false)
    if (bufferRef.current.length) {
      setEntries(prev => [...prev.slice(-1999 + bufferRef.current.length), ...bufferRef.current])
      bufferRef.current = []
    }
  }, [])

  // ── Download ──────────────────────────────────────────────────────────────
  const download = () => {
    const text = entries.map(e =>
      `${e.ts}  ${e.level.padEnd(8)}  ${e.logger}\n  ${e.message}${e.exc ? '\n' + e.exc : ''}`
    ).join('\n')
    const blob = new Blob([text], { type: 'text/plain' })
    const url  = URL.createObjectURL(blob)
    const a    = document.createElement('a')
    a.href = url; a.download = 'atom-logs.txt'; a.click()
    URL.revokeObjectURL(url)
  }

  // ── Filtered view ─────────────────────────────────────────────────────────
  const lowerFilter = filter.toLowerCase()
  const visible = entries.filter(e =>
    levels.has(e.level) &&
    (lowerFilter === '' ||
      e.message.toLowerCase().includes(lowerFilter) ||
      e.logger.toLowerCase().includes(lowerFilter))
  )

  const toggleLevel = (lvl) => {
    setLevels(prev => {
      const next = new Set(prev)
      next.has(lvl) ? next.delete(lvl) : next.add(lvl)
      return next
    })
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: 'calc(100vh - 0px)', overflow: 'hidden' }}>

      {/* ── Toolbar ── */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap',
        padding: '16px 24px', borderBottom: '1px solid var(--border)',
        background: 'var(--bg-base)', flexShrink: 0,
      }}>
        <div>
          <h1 style={{ margin: 0, fontSize: '18px', fontWeight: '600', color: 'var(--text-primary)' }}>
            Live Logs
          </h1>
          <p style={{ margin: 0, fontSize: '12px', color: 'var(--text-muted)' }}>
            Real-time application log stream
          </p>
        </div>

        {/* Connection indicator */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginLeft: '4px' }}>
          <span style={{
            width: '8px', height: '8px', borderRadius: '50%',
            background: connected ? '#4caf7d' : '#e06c6c',
            display: 'inline-block',
            boxShadow: connected ? '0 0 0 2px rgba(76,175,125,0.25)' : 'none',
          }} />
          <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>
            {connected ? 'Live' : 'Disconnected'}
          </span>
        </div>

        {/* Level filter buttons */}
        <div style={{ display: 'flex', gap: '6px', marginLeft: '8px' }}>
          {ALL_LEVELS.map(lvl => (
            <button
              key={lvl}
              onClick={() => toggleLevel(lvl)}
              style={{
                padding: '3px 10px', borderRadius: '4px', fontSize: '11px',
                fontWeight: '600', letterSpacing: '0.05em', cursor: 'pointer',
                border: `1px solid ${LEVEL_COLORS[lvl]?.color || '#ccc'}55`,
                background: levels.has(lvl) ? `${LEVEL_COLORS[lvl]?.color}22` : 'transparent',
                color: levels.has(lvl) ? LEVEL_COLORS[lvl]?.color : 'var(--text-muted)',
                transition: 'all 0.15s',
              }}
            >
              {lvl}
            </button>
          ))}
        </div>

        {/* Text search */}
        <input
          value={filter}
          onChange={e => setFilter(e.target.value)}
          placeholder="Filter messages…"
          style={{
            flex: 1, minWidth: '160px', maxWidth: '320px',
            padding: '6px 12px', background: 'var(--bg-input)',
            border: '1px solid var(--border)', borderRadius: '6px',
            color: 'var(--text-primary)', fontSize: '12px', outline: 'none',
          }}
          onFocus={e => e.target.style.borderColor = 'var(--accent)'}
          onBlur={e => e.target.style.borderColor = 'var(--border)'}
        />

        <span style={{ fontSize: '12px', color: 'var(--text-muted)', whiteSpace: 'nowrap' }}>
          {visible.length} / {entries.length}
        </span>

        <div style={{ display: 'flex', gap: '8px', marginLeft: 'auto' }}>
          <button
            onClick={() => setEntries([])}
            title="Clear"
            style={{
              display: 'flex', alignItems: 'center', gap: '5px',
              padding: '6px 12px', background: 'transparent',
              border: '1px solid var(--border)', borderRadius: '6px',
              color: 'var(--text-secondary)', fontSize: '12px', cursor: 'pointer',
            }}
          >
            <Trash2 size={13} /> Clear
          </button>
          <button
            onClick={download}
            title="Download"
            style={{
              display: 'flex', alignItems: 'center', gap: '5px',
              padding: '6px 12px', background: 'transparent',
              border: '1px solid var(--border)', borderRadius: '6px',
              color: 'var(--text-secondary)', fontSize: '12px', cursor: 'pointer',
            }}
          >
            <Download size={13} /> Download
          </button>
          <button
            onClick={paused ? resume : () => setPaused(true)}
            style={{
              display: 'flex', alignItems: 'center', gap: '5px',
              padding: '6px 12px',
              background: paused ? 'var(--accent)' : 'transparent',
              border: `1px solid ${paused ? 'var(--accent)' : 'var(--border)'}`,
              borderRadius: '6px',
              color: paused ? 'white' : 'var(--text-secondary)',
              fontSize: '12px', cursor: 'pointer',
            }}
          >
            {paused ? <><Play size={13} /> Resume {bufferRef.current.length ? `(+${bufferRef.current.length})` : ''}</> : <><Pause size={13} /> Pause</>}
          </button>
        </div>
      </div>

      {/* ── Column headers ── */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: '72px 74px 180px 1fr',
        gap: '10px',
        padding: '5px 14px',
        background: 'var(--bg-elevated)',
        borderBottom: '1px solid var(--border)',
        fontSize: '10px', fontWeight: '600',
        color: 'var(--text-muted)', letterSpacing: '0.07em', textTransform: 'uppercase',
        flexShrink: 0,
      }}>
        <span>Time</span>
        <span>Level</span>
        <span>Logger</span>
        <span>Message</span>
      </div>

      {/* ── Error banner ── */}
      {fetchError && (
        <div style={{
          padding: '8px 14px', background: 'rgba(224,108,108,0.10)',
          borderBottom: '1px solid rgba(224,108,108,0.3)',
          fontSize: '12px', color: '#e06c6c',
        }}>
          {fetchError}
        </div>
      )}

      {/* ── Log rows ── */}
      <div style={{ flex: 1, overflowY: 'auto', background: 'var(--bg-base)' }}>
        {visible.length === 0 ? (
          <div style={{ padding: '48px', textAlign: 'center', color: 'var(--text-muted)', fontSize: '13px' }}>
            {entries.length === 0 ? 'No logs yet — waiting for application activity…' : 'No entries match the current filter.'}
          </div>
        ) : (
          visible.map((e, i) => <LogRow key={i} entry={e} />)
        )}
        <div ref={bottomRef} />
      </div>
    </div>
  )
}
