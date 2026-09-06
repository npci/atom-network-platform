// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useRef, useState } from 'react'
import { t } from '../../strings'
import { useNavigate } from 'react-router-dom'
import { useMutation } from '@tanstack/react-query'
import { changesApi } from '../../services/api'
import { ArrowLeft, FileText, Sparkles, X } from 'lucide-react'

const inputStyle = {
  width: '100%',
  background: 'var(--bg-input)',
  border: '1px solid var(--border)',
  borderRadius: '6px',
  color: 'var(--text-primary)',
  fontSize: '14px',
  outline: 'none',
  transition: 'border-color 0.15s',
}

export default function NewChange() {
  const navigate = useNavigate()
  const [form, setForm] = useState({ initial_prompt: '' })
  const [sourceFile, setSourceFile] = useState(null)
  const [error, setError] = useState('')
  const fileInputRef = useRef(null)

  const { mutate, isPending } = useMutation({
    // Create the change, then attach the optional source BRD before entering the flow —
    // the enhancer's FIRST turn must already see it (that's the whole point of the seed).
    mutationFn: async (data) => {
      const res = await changesApi.create(data)
      if (sourceFile) {
        try {
          await changesApi.uploadSourceDocument(res.data.id, sourceFile)
        } catch (upErr) {
          // The change exists; the flow continues without the seed. Surface, don't block.
          const detail = upErr.response?.data?.detail || 'source document upload failed'
          throw Object.assign(new Error(detail), { changeId: res.data.id, detail })
        }
      }
      return res
    },
    onSuccess: (res) => navigate(`/changes/${res.data.id}/prompt_enhancement`),
    onError: (err) => {
      if (err.changeId) {
        // Change was created but the attachment failed — let the user proceed anyway.
        setError(`Change created, but the document was not attached (${err.detail}). ` +
                 'You can continue without it — redirecting…')
        setTimeout(() => navigate(`/changes/${err.changeId}/prompt_enhancement`), 2500)
        return
      }
      setError(err.response?.data?.detail || 'Failed to create change request')
    },
  })

  const handleSubmit = (e) => {
    e.preventDefault()
    setError('')
    if (!form.initial_prompt.trim() && !sourceFile) {
      setError('Describe the change idea, or attach a requirements document — one of the two is needed.')
      return
    }
    // With a document attached, the document IS the description — derive a stub
    // prompt so the (non-nullable) initial_prompt still reads sensibly in lists.
    const payload = form.initial_prompt.trim()
      ? form
      : { ...form, initial_prompt: `Implement the change described in the attached document: ${sourceFile.name}` }
    mutate(payload)
  }

  return (
    <div style={{ padding: '32px 40px', maxWidth: 900, margin: '0 auto' }}>
      <button
        onClick={() => navigate('/dashboard')}
        style={{
          display: 'flex', alignItems: 'center', gap: '6px',
          fontSize: '13px', color: 'var(--text-muted)',
          background: 'none', border: 'none', cursor: 'pointer',
          padding: '0', marginBottom: '24px',
          transition: 'color 0.15s',
        }}
        onMouseEnter={e => e.currentTarget.style.color = 'var(--text-primary)'}
        onMouseLeave={e => e.currentTarget.style.color = 'var(--text-muted)'}
      >
        <ArrowLeft size={14} />
        Back to Dashboard
      </button>

      <div style={{ marginBottom: '28px' }}>
        <h1 style={{ margin: '0 0 6px', fontSize: '20px', fontWeight: '600', color: 'var(--text-primary)' }}>
          New Change Request
        </h1>
        <p style={{ margin: 0, fontSize: '13px', color: 'var(--text-muted)' }}>
          Describe the network feature idea. The AI will guide you through research, BRD, tech spec, and product kit.
        </p>
      </div>

      <form onSubmit={handleSubmit}>
        {/* No Title field by design — the title is AI-generated from the idea (or the
            attached document) at creation, and re-generated when the enhanced prompt is
            accepted. A typed title outlives every later decision and is never revised
            when clarification supersedes a value it embeds. */}

        <div style={{ marginBottom: '20px' }}>
          <label style={{
            display: 'block', fontSize: '13px',
            color: 'var(--text-secondary)', marginBottom: '6px', fontWeight: '500',
          }}>
            Describe your idea{' '}
            {sourceFile
              ? <span style={{ color: 'var(--text-muted)', fontWeight: '400' }}>(optional — the attached document will be used)</span>
              : <span style={{ color: 'var(--accent)' }}>*</span>}
          </label>
          <textarea
            required={!sourceFile}
            rows={7}
            value={form.initial_prompt}
            onChange={(e) => setForm({ ...form, initial_prompt: e.target.value })}
            style={{
              ...inputStyle,
              padding: '12px 14px',
              resize: 'vertical',
              lineHeight: '1.6',
              fontFamily: 'inherit',
            }}
            placeholder={t('ph.newChange.description')}
            onFocus={e => e.target.style.borderColor = 'var(--accent)'}
            onBlur={e => e.target.style.borderColor = 'var(--border)'}
          />
          <p style={{ margin: '4px 0 0', fontSize: '11px', color: 'var(--text-muted)' }}>
            {form.initial_prompt.length} characters
          </p>
        </div>

        <div style={{ marginBottom: '20px' }}>
          <label style={{
            display: 'block', fontSize: '13px',
            color: 'var(--text-secondary)', marginBottom: '6px', fontWeight: '500',
          }}>
            Attach a detailed BRD / requirements document{' '}
            <span style={{ color: 'var(--text-muted)', fontWeight: '400' }}>(optional)</span>
          </label>
          <input
            ref={fileInputRef}
            type="file"
            accept=".pdf,.docx,.txt,.md,.png,.jpg,.jpeg"
            style={{ display: 'none' }}
            onChange={(e) => setSourceFile(e.target.files?.[0] || null)}
          />
          {sourceFile ? (
            <div style={{
              display: 'flex', alignItems: 'center', gap: '8px',
              padding: '10px 14px', ...inputStyle, width: 'auto',
            }}>
              <FileText size={15} style={{ color: 'var(--accent)', flexShrink: 0 }} />
              <span style={{ fontSize: '13px', color: 'var(--text-primary)' }}>{sourceFile.name}</span>
              <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>
                {(sourceFile.size / 1024).toFixed(0)} KB
              </span>
              <button
                type="button"
                onClick={() => { setSourceFile(null); if (fileInputRef.current) fileInputRef.current.value = '' }}
                style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 2,
                         color: 'var(--text-muted)', display: 'flex' }}
                title="Remove"
              >
                <X size={14} />
              </button>
            </div>
          ) : (
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              style={{
                display: 'flex', alignItems: 'center', gap: '8px',
                padding: '10px 14px', cursor: 'pointer', ...inputStyle, width: 'auto',
              }}
              onMouseEnter={e => e.currentTarget.style.borderColor = 'var(--accent)'}
              onMouseLeave={e => e.currentTarget.style.borderColor = 'var(--border)'}
            >
              <FileText size={15} style={{ color: 'var(--text-muted)' }} />
              <span style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
                Upload document (PDF, DOCX, TXT, MD, PNG/JPG — max 15 MB)
              </span>
            </button>
          )}
          <p style={{ margin: '4px 0 0', fontSize: '11px', color: 'var(--text-muted)' }}>
            The AI still runs every stage (clarification, research, canvas, BRD) — your document
            becomes its source material, so it works from your facts instead of assuming.
            Scanned PDFs are OCR&apos;d; embedded diagrams/screenshots are read by the vision model.
          </p>
        </div>

        {error && (
          <div style={{
            background: 'rgba(224,108,108,0.1)',
            border: '1px solid rgba(224,108,108,0.3)',
            color: 'var(--danger)',
            fontSize: '13px', borderRadius: '6px',
            padding: '10px 14px', marginBottom: '20px',
          }}>
            {error}
          </div>
        )}

        <button
          type="submit"
          disabled={isPending}
          style={{
            display: 'flex', alignItems: 'center', gap: '8px',
            padding: '10px 20px',
            background: isPending ? 'var(--text-muted)' : 'var(--accent)',
            color: 'white', border: 'none', borderRadius: '6px',
            fontSize: '14px', fontWeight: '600',
            cursor: isPending ? 'not-allowed' : 'pointer',
            transition: 'background 0.15s',
          }}
          onMouseEnter={e => { if (!isPending) e.currentTarget.style.background = 'var(--accent-hover)' }}
          onMouseLeave={e => { if (!isPending) e.currentTarget.style.background = 'var(--accent)' }}
        >
          <Sparkles size={15} />
          {isPending ? 'Starting…' : 'Start AI-Assisted Process'}
        </button>
      </form>
    </div>
  )
}
