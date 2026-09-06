// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useState, useRef } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ragApi } from '../../services/api'
import {
  Upload, FileText, Trash2, Loader, CheckCircle, AlertCircle,
  Search, Database, FolderOpen, Plus, X,
} from 'lucide-react'
import StatTile, { StatTileRow } from '../../components/common/StatTile'

export default function ProductKnowledge() {
  const qc = useQueryClient()
  const fileRef = useRef(null)
  const [uploading, setUploading] = useState(false)
  const [selectedFile, setSelectedFile] = useState(null)
  const [category, setCategory] = useState('')
  const [newCategory, setNewCategory] = useState('')
  const [showNewCat, setShowNewCat] = useState(false)
  const [error, setError] = useState(null)
  const [success, setSuccess] = useState(null)
  const [searchQuery, setSearchQuery] = useState('')
  const [searchResults, setSearchResults] = useState(null)
  const [searching, setSearching] = useState(false)

  const { data: status } = useQuery({
    queryKey: ['rag-status'],
    queryFn: () => ragApi.status().then(r => r.data),
  })

  const { data: categories } = useQuery({
    queryKey: ['rag-categories'],
    queryFn: () => ragApi.categories().then(r => r.data),
  })

  // Filter out java_source from status
  const productFiles = (status?.files || []).filter(f => f.category !== 'java_source')
  const productCategories = (status?.by_category || {})
  const totalProductChunks = Object.entries(productCategories)
    .filter(([k]) => k !== 'java_source')
    .reduce((sum, [, v]) => sum + v, 0)
  const categoryCount = Object.keys(productCategories).filter(k => k !== 'java_source').length

  const handleUpload = async () => {
    if (!selectedFile) return
    const cat = showNewCat ? newCategory.trim().toLowerCase().replace(/\s+/g, '_') : category
    if (!cat) { setError('Please select or enter a category'); return }

    setUploading(true)
    setError(null)
    setSuccess(null)
    try {
      const res = await ragApi.upload(selectedFile, cat)
      setSuccess(`Uploaded "${res.data.file_name}" — ${res.data.chunks_created} chunks created in "${res.data.category}"`)
      setSelectedFile(null)
      setCategory('')
      setNewCategory('')
      setShowNewCat(false)
      if (fileRef.current) fileRef.current.value = ''
      qc.invalidateQueries({ queryKey: ['rag-status'] })
      qc.invalidateQueries({ queryKey: ['rag-categories'] })
    } catch (e) {
      setError(e.response?.data?.detail || 'Upload failed')
    } finally {
      setUploading(false)
    }
  }

  const handleDelete = async (fileName) => {
    setError(null)
    try {
      await ragApi.deleteFile(fileName)
      qc.invalidateQueries({ queryKey: ['rag-status'] })
      qc.invalidateQueries({ queryKey: ['rag-categories'] })
    } catch (e) {
      setError(e.response?.data?.detail || 'Delete failed')
    }
  }

  const handleSearch = async () => {
    if (!searchQuery.trim()) return
    setSearching(true)
    try {
      const res = await ragApi.search({ query: searchQuery, top_k: 5 })
      setSearchResults(res.data)
    } catch  {
      setError('Search failed')
    } finally {
      setSearching(false)
    }
  }

  const catLabel = (val) => {
    const found = categories?.find(c => c.value === val)
    return found?.label || val.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase())
  }

  const totalChunks = (status?.files || [])
    .filter(f => f.category !== 'java_source')
    .reduce((acc, f) => acc + (f.chunks || 0), 0)

  return (
    <div style={{ padding: '32px 40px', maxWidth: 1600, margin: '0 auto' }}>
      {/* Header */}
      <div style={{ marginBottom: '24px' }}>
        <h1 style={{ margin: '0 0 4px', fontSize: '20px', fontWeight: '600', color: 'var(--text-primary)' }}>
          Product Knowledge
        </h1>
        <p style={{ margin: 0, fontSize: '13px', color: 'var(--text-muted)' }}>
          Upload and manage product documents (RBI guidelines, BRDs, API specs, XSDs) for the RAG pipeline
        </p>
      </div>

      <StatTileRow>
        <StatTile label="Documents"  value={productFiles.length} accent="var(--text-secondary)" />
        <StatTile label="Categories" value={categoryCount}       accent="#6ea8dc" />
        <StatTile label="Chunks"     value={totalChunks}         accent="#b388e8"
                  hint={totalChunks ? 'embedded + searchable' : null} />
        <StatTile label="Status"     value={status?.ready ? 'Ready' : 'Indexing'}
                  accent={status?.ready ? '#4caf7d' : '#da7756'} />
      </StatTileRow>

      {/* Messages */}
      {error && (
        <div style={{ padding: '10px 16px', borderRadius: '8px', marginBottom: '16px', background: 'rgba(224,108,108,0.08)', border: '1px solid rgba(224,108,108,0.25)', display: 'flex', alignItems: 'center', gap: '8px' }}>
          <AlertCircle size={14} style={{ color: 'var(--danger)', flexShrink: 0 }} />
          <span style={{ fontSize: '13px', color: 'var(--danger)', flex: 1 }}>{error}</span>
          <button onClick={() => setError(null)} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-muted)' }}><X size={14} /></button>
        </div>
      )}
      {success && (
        <div style={{ padding: '10px 16px', borderRadius: '8px', marginBottom: '16px', background: 'rgba(76,175,125,0.08)', border: '1px solid rgba(76,175,125,0.25)', display: 'flex', alignItems: 'center', gap: '8px' }}>
          <CheckCircle size={14} style={{ color: 'var(--success)', flexShrink: 0 }} />
          <span style={{ fontSize: '13px', color: 'var(--success)', flex: 1 }}>{success}</span>
          <button onClick={() => setSuccess(null)} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-muted)' }}><X size={14} /></button>
        </div>
      )}

      {/* Upload section */}
      <div style={{ padding: '20px', marginBottom: '24px', borderRadius: '8px', background: 'var(--bg-elevated)', border: '1px solid var(--border)' }}>
        <h3 style={{ margin: '0 0 16px', fontSize: '14px', fontWeight: '600', color: 'var(--text-primary)', display: 'flex', alignItems: 'center', gap: '8px' }}>
          <Upload size={16} style={{ color: 'var(--accent)' }} /> Upload Document
        </h3>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px', marginBottom: '14px' }}>
          {/* File picker */}
          <div>
            <label style={{ display: 'block', fontSize: '11px', color: 'var(--text-muted)', marginBottom: '4px', textTransform: 'uppercase', letterSpacing: '0.04em' }}>File (PDF, DOCX, TXT, MD, XML, XSD)</label>
            <input
              ref={fileRef}
              type="file"
              accept=".pdf,.docx,.txt,.md,.xml,.xsd"
              onChange={e => setSelectedFile(e.target.files?.[0] || null)}
              style={{ width: '100%', padding: '7px 12px', borderRadius: '6px', border: '1px solid var(--border)', background: 'var(--bg-input)', color: 'var(--text-primary)', fontSize: '12px' }}
            />
          </div>
          {/* Category */}
          <div>
            <label style={{ display: 'block', fontSize: '11px', color: 'var(--text-muted)', marginBottom: '4px', textTransform: 'uppercase', letterSpacing: '0.04em' }}>Category</label>
            {!showNewCat ? (
              <div style={{ display: 'flex', gap: '8px' }}>
                <select
                  value={category}
                  onChange={e => setCategory(e.target.value)}
                  style={{ flex: 1, padding: '8px 12px', borderRadius: '6px', border: '1px solid var(--border)', background: 'var(--bg-input)', color: 'var(--text-primary)', fontSize: '13px' }}
                >
                  <option value="">Select category...</option>
                  {categories?.map(c => (
                    <option key={c.value} value={c.value}>{c.label} {c.chunks > 0 ? `(${c.chunks} chunks)` : ''}</option>
                  ))}
                </select>
                <button onClick={() => setShowNewCat(true)} title="Add new category" style={{
                  padding: '8px 12px', background: 'transparent', border: '1px solid var(--border)',
                  borderRadius: '6px', color: 'var(--accent)', cursor: 'pointer', display: 'flex', alignItems: 'center',
                }}>
                  <Plus size={14} />
                </button>
              </div>
            ) : (
              <div style={{ display: 'flex', gap: '8px' }}>
                <input
                  value={newCategory}
                  onChange={e => setNewCategory(e.target.value)}
                  placeholder="e.g. Internal Policies"
                  style={{ flex: 1, padding: '8px 12px', borderRadius: '6px', border: '1px solid var(--border)', background: 'var(--bg-input)', color: 'var(--text-primary)', fontSize: '13px' }}
                />
                <button onClick={() => { setShowNewCat(false); setNewCategory('') }} style={{
                  padding: '8px 12px', background: 'transparent', border: '1px solid var(--border)',
                  borderRadius: '6px', color: 'var(--text-muted)', cursor: 'pointer', display: 'flex', alignItems: 'center',
                }}>
                  <X size={14} />
                </button>
              </div>
            )}
          </div>
        </div>
        <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
          <button
            onClick={handleUpload}
            disabled={uploading || !selectedFile || (!category && !newCategory.trim())}
            style={{
              display: 'flex', alignItems: 'center', gap: '6px',
              padding: '8px 18px', background: 'var(--accent)', color: 'white',
              border: 'none', borderRadius: '6px', fontSize: '13px', fontWeight: '600',
              cursor: (uploading || !selectedFile) ? 'not-allowed' : 'pointer',
              opacity: (uploading || !selectedFile) ? 0.5 : 1,
            }}
          >
            {uploading ? <Loader size={13} style={{ animation: 'spin 1s linear infinite' }} /> : <Upload size={13} />}
            {uploading ? 'Uploading & Indexing...' : 'Upload & Index'}
          </button>
        </div>
      </div>

      {/* Stats */}
      <div style={{ display: 'flex', gap: '16px', marginBottom: '24px' }}>
        {[
          { label: 'Total Chunks', value: totalProductChunks, icon: Database },
          { label: 'Categories', value: categoryCount, icon: FolderOpen },
          { label: 'Files', value: productFiles.length, icon: FileText },
        ].map(stat => (
          <div key={stat.label} style={{ flex: 1, padding: '16px 20px', borderRadius: '8px', background: 'var(--bg-elevated)', border: '1px solid var(--border)' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '6px' }}>
              <stat.icon size={14} style={{ color: 'var(--accent)' }} />
              <span style={{ fontSize: '11px', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>{stat.label}</span>
            </div>
            <p style={{ margin: 0, fontSize: '24px', fontWeight: '700', color: 'var(--text-primary)' }}>{stat.value}</p>
          </div>
        ))}
      </div>

      {/* Category breakdown */}
      {categoryCount > 0 && (
        <div style={{ marginBottom: '24px', borderRadius: '8px', background: 'var(--bg-elevated)', border: '1px solid var(--border)', overflow: 'hidden' }}>
          <div style={{ padding: '14px 20px', borderBottom: '1px solid var(--border-subtle)' }}>
            <h3 style={{ margin: 0, fontSize: '13px', fontWeight: '600', color: 'var(--text-primary)' }}>By Category</h3>
          </div>
          {Object.entries(productCategories)
            .filter(([k]) => k !== 'java_source')
            .map(([cat, count], i, arr) => (
              <div key={cat} style={{
                padding: '12px 20px', display: 'flex', alignItems: 'center', gap: '12px',
                borderBottom: i < arr.length - 1 ? '1px solid var(--border-subtle)' : 'none',
              }}>
                <span style={{ fontSize: '13px', color: 'var(--text-primary)', flex: 1, fontWeight: '500' }}>{catLabel(cat)}</span>
                <span style={{ fontSize: '12px', color: 'var(--text-muted)' }}>{count} chunks</span>
                <div style={{ width: '100px', height: '4px', borderRadius: '2px', background: 'var(--border)', overflow: 'hidden' }}>
                  <div style={{ height: '100%', width: `${Math.min((count / totalProductChunks) * 100, 100)}%`, background: 'var(--accent)', borderRadius: '2px' }} />
                </div>
              </div>
            ))}
        </div>
      )}

      {/* Files list */}
      {productFiles.length > 0 && (
        <div style={{ marginBottom: '24px', borderRadius: '8px', background: 'var(--bg-elevated)', border: '1px solid var(--border)', overflow: 'hidden' }}>
          <div style={{ padding: '14px 20px', borderBottom: '1px solid var(--border-subtle)' }}>
            <h3 style={{ margin: 0, fontSize: '13px', fontWeight: '600', color: 'var(--text-primary)' }}>Indexed Files</h3>
          </div>
          {productFiles.map((f, i) => (
            <div key={f.file} style={{
              padding: '10px 20px', display: 'flex', alignItems: 'center', gap: '12px',
              borderBottom: i < productFiles.length - 1 ? '1px solid var(--border-subtle)' : 'none',
            }}>
              <FileText size={14} style={{ color: 'var(--text-muted)', flexShrink: 0 }} />
              <span style={{ color: 'var(--text-primary)', flex: 1, fontFamily: 'monospace', fontSize: '12px' }}>{f.file}</span>
              <span style={{
                fontSize: '10px', padding: '2px 8px', borderRadius: '4px',
                background: 'rgba(218,119,86,0.1)', color: 'var(--accent)',
                border: '1px solid rgba(218,119,86,0.2)', fontWeight: '600',
              }}>{catLabel(f.category)}</span>
              <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>{f.chunks} chunks</span>
              <button onClick={() => handleDelete(f.file)} style={{
                padding: '4px 8px', background: 'transparent', border: '1px solid var(--border)',
                borderRadius: '4px', color: 'var(--danger)', cursor: 'pointer', display: 'flex', alignItems: 'center',
              }} title="Delete">
                <Trash2 size={12} />
              </button>
            </div>
          ))}
        </div>
      )}

      {/* Search */}
      <div style={{ padding: '20px', borderRadius: '8px', background: 'var(--bg-elevated)', border: '1px solid var(--border)' }}>
        <h3 style={{ margin: '0 0 12px', fontSize: '13px', fontWeight: '600', color: 'var(--text-primary)', display: 'flex', alignItems: 'center', gap: '8px' }}>
          <Search size={14} style={{ color: 'var(--accent)' }} /> Test Retrieval
        </h3>
        <div style={{ display: 'flex', gap: '8px', marginBottom: searchResults ? '16px' : 0 }}>
          <input
            value={searchQuery}
            onChange={e => setSearchQuery(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') handleSearch() }}
            placeholder="Search product knowledge..."
            style={{ flex: 1, padding: '8px 12px', borderRadius: '6px', border: '1px solid var(--border)', background: 'var(--bg-input)', color: 'var(--text-primary)', fontSize: '13px' }}
          />
          <button onClick={handleSearch} disabled={searching || !searchQuery.trim()} style={{
            padding: '8px 14px', background: 'var(--accent)', color: 'white',
            border: 'none', borderRadius: '6px', fontSize: '13px', fontWeight: '600', cursor: 'pointer',
            opacity: (searching || !searchQuery.trim()) ? 0.5 : 1,
          }}>
            {searching ? <Loader size={13} style={{ animation: 'spin 1s linear infinite' }} /> : <Search size={13} />}
          </button>
        </div>
        {searchResults && (
          <div>
            <p style={{ margin: '0 0 8px', fontSize: '11px', color: 'var(--text-muted)' }}>{searchResults.count} results</p>
            {searchResults.results.map((r, i) => (
              <div key={i} style={{
                padding: '10px 14px', marginBottom: '8px', borderRadius: '6px',
                background: 'var(--bg-base)', border: '1px solid var(--border-subtle)',
              }}>
                <div style={{ display: 'flex', gap: '8px', marginBottom: '4px' }}>
                  <span style={{ fontSize: '10px', padding: '1px 6px', borderRadius: '3px', background: 'rgba(218,119,86,0.1)', color: 'var(--accent)', fontWeight: '600' }}>
                    {catLabel(r.doc_category)}
                  </span>
                  <span style={{ fontSize: '10px', color: 'var(--text-muted)', fontFamily: 'monospace' }}>{r.source_file}</span>
                  <span style={{ fontSize: '10px', color: 'var(--success)', marginLeft: 'auto' }}>score: {r.score}</span>
                </div>
                <p style={{ margin: 0, fontSize: '12px', color: 'var(--text-secondary)', lineHeight: '1.5', maxHeight: '60px', overflow: 'hidden' }}>
                  {r.content.slice(0, 200)}...
                </p>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
