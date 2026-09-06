// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

// Admin → Authority Policy
//
// Single-row policy doc that the feasibility resolver loads as context
// every time it evaluates an inbound partner query / counter. Admin can:
//   - Edit content inline in a markdown textarea
//   - Upload a .md file to replace content
//   - Reset to the bind-mounted seed file
//
// No live preview / split view yet — the textarea is intentionally large
// and uses a monospace font. Add a preview pane in a follow-up if needed.

import { useEffect, useRef, useState } from 'react'
import { t } from '../../strings'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertCircle, CheckCircle, FileText, Loader2, RotateCcw, Save, Upload,
} from 'lucide-react'

import { authorityPolicyApi } from '../../services/api'

export default function AuthorityPolicyPage() {
  const qc = useQueryClient()
  const [edits, setEdits] = useState('')
  const [hasLoaded, setHasLoaded] = useState(false)
  const [banner, setBanner] = useState(null) // { kind: 'ok'|'err', text }
  const fileInputRef = useRef(null)

  const { data, isLoading, isError } = useQuery({
    queryKey: ['authority-policy'],
    queryFn: () => authorityPolicyApi.get(),
  })

  useEffect(() => {
    if (data && !hasLoaded) {
      setEdits(data.content || '')
      setHasLoaded(true)
    }
  }, [data, hasLoaded])

  const saveMutation = useMutation({
    mutationFn: () => authorityPolicyApi.update(edits),
    onSuccess: (resp) => {
      qc.setQueryData(['authority-policy'], resp)
      flashBanner('ok', 'Saved')
    },
    onError: (e) => flashBanner('err', e?.response?.data?.detail || 'Save failed'),
  })

  const uploadMutation = useMutation({
    mutationFn: (file) => authorityPolicyApi.upload(file),
    onSuccess: (resp) => {
      qc.setQueryData(['authority-policy'], resp)
      setEdits(resp.content || '')
      flashBanner('ok', `Uploaded (${resp.size_bytes} bytes)`)
    },
    onError: (e) => flashBanner('err', e?.response?.data?.detail || 'Upload failed'),
  })

  const resetMutation = useMutation({
    mutationFn: () => authorityPolicyApi.resetToSeed(),
    onSuccess: (resp) => {
      qc.setQueryData(['authority-policy'], resp)
      setEdits(resp.content || '')
      flashBanner('ok', 'Reset to seed file')
    },
    onError: (e) => flashBanner('err', e?.response?.data?.detail || 'Reset failed'),
  })

  const flashBanner = (kind, text) => {
    setBanner({ kind, text })
    setTimeout(() => setBanner(null), 3000)
  }

  const onPickFile = (e) => {
    const file = e.target.files?.[0]
    if (file) uploadMutation.mutate(file)
    e.target.value = '' // allow re-uploading the same filename
  }

  const dirty = hasLoaded && edits !== (data?.content || '')
  const sizeBytes = new TextEncoder().encode(edits).length

  return (
    <div style={{ padding: '20px 24px', maxWidth: 1100 }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 6 }}>
        <FileText size={22} color="var(--accent)" />
        <h1 style={{ margin: 0, fontSize: 22, fontWeight: 700 }}>{t('page.policy.title')}</h1>
      </div>
      <p style={{ margin: '0 0 18px 0', color: 'var(--text-muted)', fontSize: 13, maxWidth: 800 }}>
        Authoritative policy document loaded by the feasibility resolver on every
        partner query / counter. Edits take effect on the next resolver call —
        no service restart required.
      </p>

      {/* Banner */}
      {banner && (
        <div style={{
          marginBottom: 14, padding: '8px 14px', borderRadius: 8,
          fontSize: 13, display: 'flex', alignItems: 'center', gap: 8,
          background: banner.kind === 'ok' ? 'rgba(16,185,129,0.10)' : 'rgba(239,68,68,0.10)',
          color: banner.kind === 'ok' ? '#10b981' : '#ef4444',
          border: `1px solid ${banner.kind === 'ok' ? 'rgba(16,185,129,0.30)' : 'rgba(239,68,68,0.30)'}`,
        }}>
          {banner.kind === 'ok'
            ? <CheckCircle size={14} />
            : <AlertCircle size={14} />}
          {banner.text}
        </div>
      )}

      {/* Action bar */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10, flexWrap: 'wrap',
      }}>
        <button
          onClick={() => saveMutation.mutate()}
          disabled={!dirty || saveMutation.isPending}
          style={primaryBtn(!dirty || saveMutation.isPending)}
        >
          {saveMutation.isPending
            ? <><Loader2 size={14} className="pp-spin" /> Saving…</>
            : <><Save size={14} /> Save</>}
        </button>
        <button
          onClick={() => fileInputRef.current?.click()}
          disabled={uploadMutation.isPending}
          style={secondaryBtn(uploadMutation.isPending)}
        >
          {uploadMutation.isPending
            ? <><Loader2 size={14} className="pp-spin" /> Uploading…</>
            : <><Upload size={14} /> Upload .md</>}
        </button>
        <input
          ref={fileInputRef}
          type="file"
          accept=".md,text/markdown,text/plain"
          onChange={onPickFile}
          style={{ display: 'none' }}
        />
        <button
          onClick={() => {
            if (window.confirm('Reset to the seed file? This overwrites any unsaved edits.')) {
              resetMutation.mutate()
            }
          }}
          disabled={resetMutation.isPending}
          style={secondaryBtn(resetMutation.isPending)}
        >
          {resetMutation.isPending
            ? <><Loader2 size={14} className="pp-spin" /> Resetting…</>
            : <><RotateCcw size={14} /> Reset to seed</>}
        </button>

        <div style={{ flex: 1 }} />

        {/* Meta */}
        <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>
          {sizeBytes.toLocaleString()} bytes
          {dirty && <span style={{ color: '#f59e0b', marginLeft: 8 }}>· unsaved changes</span>}
          {data?.updated_at && (
            <span style={{ marginLeft: 8 }}>
              · last saved {new Date(data.updated_at).toLocaleString()}
            </span>
          )}
        </span>
      </div>

      {/* Editor */}
      {isLoading && (
        <div style={loadingBox}>
          <Loader2 size={16} className="pp-spin" /> Loading…
        </div>
      )}
      {isError && (
        <div style={errorBox}>
          <AlertCircle size={14} /> Could not load policy doc.
        </div>
      )}
      {!isLoading && !isError && (
        <textarea
          value={edits}
          onChange={(e) => setEdits(e.target.value)}
          spellCheck={false}
          style={{
            width: '100%', boxSizing: 'border-box',
            minHeight: 600, padding: 14,
            fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace',
            fontSize: 13, lineHeight: 1.5,
            background: 'var(--bg-elevated)',
            color: 'var(--text-primary)',
            border: '1px solid var(--border)', borderRadius: 8,
            resize: 'vertical', outline: 'none',
          }}
          placeholder={t('ph.policy.editor')}
        />
      )}
    </div>
  )
}


const primaryBtn = (disabled) => ({
  display: 'inline-flex', alignItems: 'center', gap: 6,
  padding: '7px 14px', fontSize: 13, fontWeight: 600,
  background: disabled ? 'var(--bg-muted)' : 'var(--accent)',
  color: disabled ? 'var(--text-muted)' : '#fff',
  border: 'none', borderRadius: 6,
  cursor: disabled ? 'not-allowed' : 'pointer',
})

const secondaryBtn = (disabled) => ({
  display: 'inline-flex', alignItems: 'center', gap: 6,
  padding: '7px 12px', fontSize: 13, fontWeight: 500,
  background: 'var(--bg-elevated)', color: 'var(--text-primary)',
  border: '1px solid var(--border)', borderRadius: 6,
  cursor: disabled ? 'wait' : 'pointer',
})

const loadingBox = {
  padding: 14, fontSize: 13, color: 'var(--text-muted)',
  display: 'flex', alignItems: 'center', gap: 8,
  border: '1px solid var(--border)', borderRadius: 8,
}

const errorBox = {
  padding: 14, fontSize: 13, color: '#ef4444',
  background: 'rgba(239,68,68,0.05)',
  border: '1px solid rgba(239,68,68,0.25)', borderRadius: 8,
  display: 'flex', alignItems: 'center', gap: 8,
}
