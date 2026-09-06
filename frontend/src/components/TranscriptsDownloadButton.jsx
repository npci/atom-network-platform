// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useState } from 'react'
import { Download } from 'lucide-react'
import { agenticApi } from '../services/api'

// One-click ZIP of a change's transcript logs. Without `section` it's the whole pipeline
// (every stage that recorded anything); with one it's just that stage — the same button
// component sits in each stage page's header passing its own section key, which must match
// a key in the backend's _TRANSCRIPT_SECTIONS (app/api/agentic.py).
// A 404 (nothing recorded) arrives as a Blob → unwrap the JSON detail.
export default function TranscriptsDownloadButton({ changeId, section, label }) {
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)
  const download = async () => {
    if (busy) return
    setErr(null); setBusy(true)
    try {
      const res = await agenticApi.transcriptsZip(changeId, section)
      const url = URL.createObjectURL(res.data)
      const a = document.createElement('a')
      a.href = url
      a.download = `transcripts-${String(changeId).slice(0, 8)}${section ? `-${section}` : ''}.zip`
      a.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      let detail = e?.response?.data?.detail
      if (!detail && e?.response?.data instanceof Blob) {
        try { detail = JSON.parse(await e.response.data.text())?.detail } catch { /* noop */ }
      }
      setErr(detail || e.message)
    } finally { setBusy(false) }
  }
  const title = section
    ? `Download the verbatim LLM transcript logs for this stage as a ZIP`
    : `Download the transcript logs for every stage of this change as a ZIP`
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
      {err && <span style={{ fontSize: 11, color: 'var(--danger, #ef4444)' }}>{err}</span>}
      <button onClick={download} disabled={busy} title={title}
        style={{ display: 'inline-flex', alignItems: 'center', gap: 6, padding: '5px 12px', fontSize: 12,
          fontWeight: 600, background: 'transparent', color: 'var(--accent, #2563eb)',
          border: '1px solid var(--accent, #2563eb)', borderRadius: 6, cursor: busy ? 'wait' : 'pointer' }}>
        <Download size={13} /> {busy ? 'Preparing…' : (label || 'Transcript logs (ZIP)')}
      </button>
    </span>
  )
}
