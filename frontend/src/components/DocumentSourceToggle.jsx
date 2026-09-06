// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useRef, useState } from 'react'
import { Upload, Loader, FileUp, RotateCcw, AlertTriangle, Sparkles } from 'lucide-react'
import { agentsApi } from '../services/api'

// Generate-or-Upload affordance. Renders an "Uploaded" provenance pill when
// the current document was user-supplied, an "Upload instead / Replace upload"
// button, and — when the current doc is an upload and a generated version
// exists — a "Revert to generated" button. Replace actions are confirmed via
// an in-app modal (not the browser's window.confirm). On any change it calls
// onUploaded() so the host page can refetch + re-render.
//
// Accepted file types mirror the backend (ALLOWED_UPLOAD_EXTENSIONS).
const ACCEPT = '.docx,.pdf,.md,.txt'

export default function DocumentSourceToggle({
  changeId,
  docType,                 // 'brd' | 'tech_spec' | 'product_kit'
  subtype = null,          // Product Kit doc_type, e.g. 'product_note'
  source = 'generated',
  originalFilename = null,
  uploadedAt = null,
  onUploaded,
  disabled = false,
  label,
  confirmReplace = false,   // when true, confirm (in-app) before replacing
  docLabel = 'document',
  canRevert = false,        // a generated version exists to revert to
  onGenerateInstead,        // when set + no generated version, offer "Generate instead"
  onBeforeUpload,           // awaited before uploading (e.g. cancel an in-flight generation)
}) {
  const fileRef = useRef(null)
  const [uploading, setUploading] = useState(false)
  const [reverting, setReverting] = useState(false)
  const [error, setError] = useState(null)
  // Pending file awaiting in-app replace confirmation.
  const [pendingFile, setPendingFile] = useState(null)
  // Confirm modal for revert-to-generated.
  const [confirmRevert, setConfirmRevert] = useState(false)
  // Confirm modal for generate-instead (when there's no generated version yet).
  const [confirmGenerate, setConfirmGenerate] = useState(false)

  const isUploaded = source === 'uploaded'

  const onFilePicked = (e) => {
    const file = e.target.files?.[0]
    if (fileRef.current) fileRef.current.value = ''
    if (!file) return
    if (confirmReplace) setPendingFile(file)   // open in-app modal
    else doUpload(file)
  }

  const doUpload = async (file) => {
    setPendingFile(null)
    setUploading(true); setError(null)
    try {
      // Stop any in-flight generation first so it can't save a version on top
      // of the upload.
      await onBeforeUpload?.()
      const res = await agentsApi.uploadArtifact(changeId, docType, file, subtype)
      onUploaded?.(res.data)
    } catch (err) {
      setError(err.response?.data?.detail || 'Upload failed')
      setTimeout(() => setError(null), 4000)
    } finally {
      setUploading(false)
    }
  }

  const doRevert = async () => {
    setConfirmRevert(false)
    setReverting(true); setError(null)
    try {
      const res = await agentsApi.revertArtifactToGenerated(changeId, docType, subtype)
      onUploaded?.(res.data)   // host refetches + re-renders
    } catch (err) {
      setError(err.response?.data?.detail || 'Revert failed')
      setTimeout(() => setError(null), 4000)
    } finally {
      setReverting(false)
    }
  }

  const pill = {
    display: 'inline-flex', alignItems: 'center', gap: '5px',
    padding: '4px 8px', fontSize: '11px', fontWeight: 500,
    color: 'var(--accent, #6aa9ff)', background: 'rgba(106,169,255,0.10)',
    border: '1px solid rgba(106,169,255,0.30)', borderRadius: '6px',
  }
  const btn = {
    display: 'flex', alignItems: 'center', gap: '5px',
    padding: '6px 10px', fontSize: '12px', fontWeight: 500,
    background: error ? 'rgba(224,108,108,0.10)' : 'var(--bg-elevated)',
    color: error ? '#e06c6c' : 'var(--text-secondary)',
    border: `1px solid ${error ? 'rgba(224,108,108,0.3)' : 'var(--border)'}`,
    borderRadius: '6px',
    cursor: (uploading || disabled) ? 'wait' : 'pointer',
    opacity: (uploading || disabled) ? 0.6 : 1,
  }

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
      {isUploaded && (
        <span style={pill}
          title={`${originalFilename || 'Uploaded document'}${uploadedAt ? ` — uploaded ${new Date(uploadedAt).toLocaleString()}` : ''}`}>
          <FileUp size={11} style={{ flexShrink: 0 }} />
          <span style={{
            maxWidth: '220px', overflow: 'hidden',
            textOverflow: 'ellipsis', whiteSpace: 'nowrap',
          }}>
            {originalFilename ? originalFilename : 'Uploaded'}
          </span>
        </span>
      )}

      {isUploaded && canRevert && (
        <button onClick={() => setConfirmRevert(true)} disabled={reverting || uploading}
          title="Discard the uploaded document and go back to the previously generated version"
          style={{ ...btn, cursor: reverting ? 'wait' : 'pointer' }}>
          {reverting
            ? <Loader size={11} style={{ animation: 'spin 1s linear infinite' }} />
            : <RotateCcw size={11} />}
          Revert to generated
        </button>
      )}

      {isUploaded && !canRevert && onGenerateInstead && (
        <button onClick={() => setConfirmGenerate(true)} disabled={uploading || reverting}
          title="Generate this document with AI instead of using the uploaded one"
          style={btn}>
          <Sparkles size={11} />
          Generate instead
        </button>
      )}

      <button onClick={() => fileRef.current?.click()} disabled={uploading || disabled}
        title={error || (isUploaded ? 'Replace the uploaded document' : 'Upload your own document instead of generating')}
        style={btn}>
        {uploading
          ? <Loader size={11} style={{ animation: 'spin 1s linear infinite' }} />
          : <Upload size={11} />}
        {error || label || (isUploaded ? 'Replace upload' : 'Upload instead')}
      </button>
      <input ref={fileRef} type="file" accept={ACCEPT} onChange={onFilePicked}
        style={{ display: 'none' }} />

      {/* In-app replace confirmation */}
      {pendingFile && (
        <ConfirmModal
          icon={<AlertTriangle size={18} style={{ color: '#d97706' }} />}
          title={`Replace the current ${docLabel}?`}
          body={<>Replace with <strong>{pendingFile.name}</strong>. The uploaded version becomes the
            document used everywhere downstream; the previous version will no longer be considered.</>}
          confirmLabel="Replace"
          onCancel={() => setPendingFile(null)}
          onConfirm={() => doUpload(pendingFile)}
        />
      )}

      {/* In-app revert confirmation */}
      {confirmRevert && (
        <ConfirmModal
          icon={<RotateCcw size={18} style={{ color: 'var(--accent)' }} />}
          title={`Revert to the generated ${docLabel}?`}
          body={<>This discards the uploaded version as the active document and restores the
            previously generated one for all downstream use.</>}
          confirmLabel="Revert"
          onCancel={() => setConfirmRevert(false)}
          onConfirm={doRevert}
        />
      )}

      {/* In-app generate-instead confirmation */}
      {confirmGenerate && (
        <ConfirmModal
          icon={<Sparkles size={18} style={{ color: 'var(--accent)' }} />}
          title={`Generate the ${docLabel} with AI?`}
          body={<>This runs AI generation and uses the generated version in place of the uploaded
            one for all downstream use.</>}
          confirmLabel="Generate"
          onCancel={() => setConfirmGenerate(false)}
          onConfirm={() => { setConfirmGenerate(false); onGenerateInstead?.() }}
        />
      )}
    </div>
  )
}

// Small in-app confirmation modal (replaces window.confirm).
function ConfirmModal({ icon, title, body, confirmLabel, onCancel, onConfirm }) {
  return (
    <div
      onClick={onCancel}
      style={{
        position: 'fixed', inset: 0, zIndex: 1000,
        background: 'rgba(0,0,0,0.45)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}
    >
      <div onClick={e => e.stopPropagation()} style={{
        width: 'min(440px, 92vw)', background: 'var(--bg-elevated)',
        border: '1px solid var(--border)', borderRadius: '10px',
        padding: '20px 22px', boxShadow: '0 18px 50px rgba(0,0,0,0.35)',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '10px' }}>
          {icon}
          <h3 style={{ margin: 0, fontSize: '15px', fontWeight: 600, color: 'var(--text-primary)' }}>{title}</h3>
        </div>
        <p style={{ margin: '0 0 18px', fontSize: '13px', lineHeight: 1.6, color: 'var(--text-secondary)' }}>{body}</p>
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '10px' }}>
          <button onClick={onCancel} style={{
            padding: '8px 16px', fontSize: '13px', fontWeight: 500, cursor: 'pointer',
            background: 'transparent', color: 'var(--text-secondary)',
            border: '1px solid var(--border)', borderRadius: '6px',
          }}>Cancel</button>
          <button onClick={onConfirm} style={{
            padding: '8px 16px', fontSize: '13px', fontWeight: 600, cursor: 'pointer',
            background: 'var(--accent)', color: 'white', border: 'none', borderRadius: '6px',
          }}>{confirmLabel}</button>
        </div>
      </div>
    </div>
  )
}
