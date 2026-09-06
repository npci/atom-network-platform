// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Layers, CheckCircle, Circle, Loader, FileUp, Download } from 'lucide-react'
import { agentsApi, docxApi } from '../services/api'

// Map an artifact summary key to the download endpoint's (doc_type, subtype).
// Returns null for items with no downloadable .docx route (e.g. Research).
function downloadTarget(key) {
  if (key === 'research') return null
  if (key.startsWith('product_kit:')) return { docType: 'product_kit', subtype: key.split(':')[1] }
  return { docType: key, subtype: undefined }
}

// Show Artifacts — a small popover listing which documents exist for this
// change (and are therefore available as downstream context) vs which are
// not yet generated. Nominal UI: a button that toggles a dropdown.
export default function ShowArtifactsButton({ changeId }) {
  const [open, setOpen] = useState(false)

  const { data, isLoading, refetch } = useQuery({
    queryKey: ['artifact-summary', changeId],
    queryFn: () => agentsApi.artifactSummary(changeId).then(r => r.data),
    enabled: open && !!changeId,
  })
  const items = data?.artifacts || []
  const inContext = items.filter(i => i.present).length
  const [downloadingKey, setDownloadingKey] = useState(null)

  const handleDownload = async (it) => {
    const target = downloadTarget(it.key)
    if (!target || downloadingKey) return
    setDownloadingKey(it.key)
    try {
      const { blob, filename } = await docxApi.download(changeId, target.docType, target.subtype)
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = filename || `${it.key.replace(':', '_')}.docx`
      document.body.appendChild(a); a.click()
      document.body.removeChild(a); URL.revokeObjectURL(url)
    } catch (e) {
      console.error('artifact download failed', e)
    } finally {
      setDownloadingKey(null)
    }
  }

  return (
    <div style={{ position: 'relative' }}>
      <button
        onClick={() => { setOpen(o => !o); if (!open) refetch() }}
        title="Show which documents are available as context"
        style={{
          display: 'inline-flex', alignItems: 'center', gap: '6px',
          padding: '6px 12px', background: 'transparent',
          color: 'var(--text-secondary)', border: '1px solid var(--border)',
          borderRadius: '6px', fontSize: '12px', fontWeight: 500,
          cursor: 'pointer',
        }}
      >
        <Layers size={13} /> Show Artifacts
      </button>

      {open && (
        <div style={{
          position: 'absolute', top: 'calc(100% + 6px)', right: 0, zIndex: 50,
          minWidth: '260px', padding: '10px 12px',
          background: 'var(--bg-elevated)', border: '1px solid var(--border)',
          borderRadius: '8px', boxShadow: '0 8px 24px rgba(0,0,0,0.18)',
        }}>
          <div style={{
            display: 'flex', justifyContent: 'space-between', alignItems: 'center',
            marginBottom: '8px',
          }}>
            <span style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
              Context Documents
            </span>
            {!isLoading && (
              <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>{inContext}/{items.length} in context</span>
            )}
          </div>

          {isLoading ? (
            <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '12px', color: 'var(--text-muted)', padding: '6px 0' }}>
              <Loader size={12} style={{ animation: 'spin 1s linear infinite' }} /> Loading…
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
              {items.map(it => (
                <div key={it.key} style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '12px' }}>
                  {it.present
                    ? <CheckCircle size={13} style={{ color: 'var(--success)', flexShrink: 0 }} />
                    : <Circle size={13} style={{ color: 'var(--border)', flexShrink: 0 }} />}
                  <span style={{ color: it.present ? 'var(--text-primary)' : 'var(--text-muted)' }}>{it.label}</span>
                  {it.source === 'uploaded' && (
                    <span title="User-uploaded" style={{ display: 'inline-flex', alignItems: 'center', gap: '3px', color: 'var(--accent, #6aa9ff)', fontSize: '10px' }}>
                      <FileUp size={10} /> uploaded
                    </span>
                  )}
                  {it.present && downloadTarget(it.key) ? (
                    <button
                      onClick={() => handleDownload(it)}
                      disabled={downloadingKey === it.key}
                      title="Download .docx"
                      style={{
                        marginLeft: 'auto', display: 'inline-flex', alignItems: 'center', gap: '4px',
                        padding: '2px 6px', background: 'transparent', color: 'var(--text-secondary)',
                        border: '1px solid var(--border)', borderRadius: '5px',
                        fontSize: '10px', cursor: downloadingKey === it.key ? 'wait' : 'pointer',
                      }}
                    >
                      {downloadingKey === it.key
                        ? <Loader size={10} style={{ animation: 'spin 1s linear infinite' }} />
                        : <Download size={10} />}
                      .docx
                    </button>
                  ) : (
                    <span style={{ marginLeft: 'auto', fontSize: '10px', color: 'var(--text-muted)' }}>
                      {it.present ? 'in context' : '—'}
                    </span>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
