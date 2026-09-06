// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

/**
 * ProductKitGenerateAll — modal for parallel generation of all Product Kit docs.
 *
 * Opens a single WebSocket to /ws/changes/{id}/product-kit-all which
 * multiplexes chunks across doc types. UI shows per-doc progress lanes
 * that fill as tokens stream in.
 */
import { useRef, useState } from 'react'
import { CheckCircle2, AlertCircle, Loader, X, Play, Download } from 'lucide-react'
import { docxApi, xlsxApi } from '../../services/api'
import { wsUrl } from '../../utils/basePath'

const DOC_STATUS = {
  pending:   { color: 'var(--text-muted)',    label: 'Pending',     Icon: Loader },
  streaming: { color: 'var(--accent)',        label: 'Streaming…',  Icon: Loader },
  done:      { color: '#4caf7d',              label: 'Done',        Icon: CheckCircle2 },
  error:     { color: '#e06c6c',              label: 'Failed',      Icon: AlertCircle },
}

export default function ProductKitGenerateAll({
  changeId, docTypes, docLabels, totalKitDocs, alreadyDoneCount, onClose, onAllDone,
}) {
  const [open, setOpen]         = useState(true)   // opens on mount; no effect needed
  const [running, setRunning]   = useState(false)
  const [perDoc, setPerDoc]     = useState({}) // { doc_type: {status, chars, error} }
  const [fatalErr, setFatalErr] = useState(null)
  const wsRef = useRef(null)

  // `total` (this-run) drives the per-lane list; `kitTotal` (whole kit) drives
  // the headline counter so users see "X of 10 complete" rather than only the
  // subset being regenerated.
  const total          = docTypes.length
  const kitTotal       = totalKitDocs ?? total
  const priorDone      = alreadyDoneCount ?? 0
  const runDone        = Object.values(perDoc).filter(d => d.status === 'done' || d.status === 'error').length
  const completedTotal = priorDone + runDone
  const percent        = kitTotal ? Math.round((completedTotal / kitTotal) * 100) : 0

  const start = () => {
    setRunning(true)
    setFatalErr(null)
    setPerDoc(Object.fromEntries(docTypes.map(dt => [dt, { status: 'pending', chars: 0 }])))

    const url = wsUrl(`api/ws/changes/${changeId}/product-kit-all`)
    const ws = new WebSocket(url)
    wsRef.current = ws

    ws.onopen = () => {
      // Auth rides the handshake: the session cookie is httpOnly and the
      // browser attaches it to the WebSocket upgrade automatically, so no
      // token is sent (or readable) here. The hello frame is still sent —
      // the server reads one frame before streaming.
      ws.send(JSON.stringify({}))
      ws.send(JSON.stringify({ doc_types: docTypes }))
    }

    ws.onmessage = (evt) => {
      const data = JSON.parse(evt.data)

      if (data.type === 'started') {
        setPerDoc(prev => ({ ...prev, [data.doc_type]: { ...prev[data.doc_type], status: 'streaming' } }))
      } else if (data.type === 'chunk') {
        setPerDoc(prev => ({
          ...prev,
          [data.doc_type]: {
            ...prev[data.doc_type],
            status: 'streaming',
            chars: (prev[data.doc_type]?.chars || 0) + (data.text?.length || 0),
          },
        }))
      } else if (data.type === 'doc_done') {
        setPerDoc(prev => ({
          ...prev,
          [data.doc_type]: {
            ...prev[data.doc_type],
            status: 'done',
            validation: data.validation,
            docxAvailable: !!data.docx_available,
            xlsxAvailable: !!data.xlsx_available,
          },
        }))
      } else if (data.type === 'doc_error') {
        setPerDoc(prev => ({
          ...prev,
          [data.doc_type]: { ...prev[data.doc_type], status: 'error', error: data.detail },
        }))
      } else if (data.type === 'all_done') {
        setRunning(false)
        if (onAllDone) onAllDone()
      } else if (data.type === 'error') {
        setFatalErr(data.detail || 'Unknown error')
        setRunning(false)
      }
    }

    ws.onerror = () => setFatalErr('WebSocket connection failed')
    ws.onclose = () => { /* no-op */ }
  }

  const close = () => {
    setOpen(false)
    try { wsRef.current?.close() } catch { /* ignore */ }
    onClose?.()
  }

  if (!open) return null

  return (
    <div style={{
      position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.5)',
      display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 999,
    }}>
      <div style={{
        width: '620px', maxWidth: '95vw', maxHeight: '80vh',
        background: 'var(--bg-base)', border: '1px solid var(--border)',
        borderRadius: '10px', display: 'flex', flexDirection: 'column',
        overflow: 'hidden',
      }}>
        {/* Header */}
        <div style={{
          padding: '14px 18px', borderBottom: '1px solid var(--border-subtle)',
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        }}>
          <div>
            <p style={{ margin: 0, fontSize: '14px', fontWeight: '600', color: 'var(--text-primary)' }}>
              Generate all Product Kit docs in parallel
            </p>
            <p style={{ margin: '2px 0 0', fontSize: '11px', color: 'var(--text-muted)' }}>
              {running
                ? `${completedTotal} of ${kitTotal} complete (${percent}%)`
                : `${total} documents selected — backend runs up to 3 concurrently`}
            </p>
          </div>
          <button onClick={close} disabled={running} title="Close"
            style={{
              background: 'none', border: 'none', cursor: running ? 'not-allowed' : 'pointer',
              color: 'var(--text-muted)', padding: '4px',
            }}>
            <X size={16} />
          </button>
        </div>

        {/* Overall progress bar */}
        <div style={{ padding: '12px 18px 0' }}>
          <div style={{ height: '4px', background: 'var(--bg-elevated)', borderRadius: '4px', overflow: 'hidden' }}>
            <div style={{
              height: '100%', background: '#4caf7d', borderRadius: '4px',
              width: `${percent}%`, transition: 'width 0.4s',
            }} />
          </div>
        </div>

        {/* Per-doc lanes */}
        <div style={{ padding: '14px 18px', overflowY: 'auto', flex: 1 }}>
          {docTypes.map(dt => {
            const entry  = perDoc[dt] || { status: 'pending', chars: 0 }
            const cfg    = DOC_STATUS[entry.status] || DOC_STATUS.pending
            const Icon   = cfg.Icon
            const label  = docLabels?.[dt] || dt
            return (
              <div key={dt} style={{
                display: 'flex', alignItems: 'center', gap: '12px',
                padding: '8px 10px', marginBottom: '6px',
                background: 'var(--bg-elevated)', borderRadius: '6px',
                border: '1px solid var(--border-subtle)',
              }}>
                <Icon
                  size={14}
                  style={{
                    color: cfg.color, flexShrink: 0,
                    animation: entry.status === 'streaming' ? 'spin 1s linear infinite' : undefined,
                  }}
                />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: 'flex', alignItems: 'baseline', gap: '8px' }}>
                    <span style={{ fontSize: '12px', fontWeight: '500', color: 'var(--text-primary)' }}>{label}</span>
                    <span style={{ fontSize: '10px', color: cfg.color, fontWeight: '600' }}>
                      {cfg.label}
                    </span>
                    {entry.chars > 0 && (
                      <span style={{ fontSize: '10px', color: 'var(--text-muted)' }}>
                        {entry.chars.toLocaleString()} chars
                      </span>
                    )}
                  </div>
                  {entry.error && (
                    <p style={{ margin: '2px 0 0', fontSize: '11px', color: '#e06c6c' }}>
                      {entry.error}
                    </p>
                  )}
                  {entry.validation && (entry.validation.error_count > 0 || entry.validation.warning_count > 0) && (
                    <p style={{ margin: '2px 0 0', fontSize: '10px', color: '#e8a44a' }}>
                      {entry.validation.error_count} error(s), {entry.validation.warning_count} warning(s) from validator
                    </p>
                  )}
                </div>
                {entry.status === 'done' && (
                  <div style={{ display: 'flex', gap: '6px', marginLeft: 'auto' }}>
                    {dt === 'cert_test_cases' && entry.xlsxAvailable && (
                      <button
                        onClick={async () => {
                          try {
                            const blob = await xlsxApi.download(changeId)
                            const url = URL.createObjectURL(blob)
                            const a = document.createElement('a')
                            a.href = url; a.download = 'cert_testcases.xlsx'
                            document.body.appendChild(a); a.click()
                            document.body.removeChild(a); URL.revokeObjectURL(url)
                          } catch { /* silent */ }
                        }}
                        style={{
                          display: 'flex', alignItems: 'center', gap: '4px',
                          padding: '4px 8px', fontSize: '11px',
                          background: 'var(--bg-base)', color: 'var(--text-secondary)',
                          border: '1px solid var(--border)', borderRadius: '4px',
                          cursor: 'pointer',
                        }}
                        title="Download .xlsx">
                        <Download size={10} /> .xlsx
                      </button>
                    )}
                    {entry.docxAvailable && (
                      <button
                        onClick={async () => {
                          try {
                            // Shape changed to {blob, filename}; product_kit
                            // entries still serve .docx so the fallback name
                            // matches the historical behaviour.
                            const { blob, filename } = await docxApi.download(changeId, 'product_kit', dt)
                            const url = URL.createObjectURL(blob)
                            const a = document.createElement('a')
                            a.href = url
                            a.download = filename || `product_kit_${dt}.docx`
                            document.body.appendChild(a); a.click()
                            document.body.removeChild(a); URL.revokeObjectURL(url)
                          } catch { /* silent */ }
                        }}
                        style={{
                          display: 'flex', alignItems: 'center', gap: '4px',
                          padding: '4px 8px', fontSize: '11px',
                          background: 'var(--bg-base)', color: 'var(--text-secondary)',
                          border: '1px solid var(--border)', borderRadius: '4px',
                          cursor: 'pointer',
                        }}
                        title="Download .docx">
                        <Download size={10} /> .docx
                      </button>
                    )}
                  </div>
                )}
              </div>
            )
          })}
        </div>

        {fatalErr && (
          <div style={{
            margin: '0 18px 12px', padding: '8px 12px',
            background: 'rgba(224,108,108,0.10)', border: '1px solid rgba(224,108,108,0.3)',
            borderRadius: '6px', fontSize: '12px', color: '#e06c6c',
          }}>
            {fatalErr}
          </div>
        )}

        {/* Footer — start button */}
        <div style={{
          padding: '12px 18px', borderTop: '1px solid var(--border-subtle)',
          display: 'flex', justifyContent: 'flex-end', gap: '10px',
        }}>
          <button onClick={close} disabled={running}
            style={{
              padding: '8px 14px', fontSize: '12px',
              background: 'var(--bg-elevated)', color: 'var(--text-secondary)',
              border: '1px solid var(--border)', borderRadius: '6px',
              cursor: running ? 'not-allowed' : 'pointer',
              opacity: running ? 0.5 : 1,
            }}>
            {running ? 'Generating…' : 'Close'}
          </button>
          {!running && runDone < total && (
            <button onClick={start} style={{
              padding: '8px 14px', fontSize: '12px', fontWeight: '600',
              background: 'var(--accent)', color: 'white',
              border: 'none', borderRadius: '6px', cursor: 'pointer',
              display: 'flex', alignItems: 'center', gap: '6px',
            }}>
              <Play size={12} /> Start
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
