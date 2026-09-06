// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useState, useEffect, useRef } from 'react'
import { t } from '../../strings'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ragApi } from '../../services/api'
import { RefreshCw, Trash2, Search, Database, FileText, AlertCircle, CheckCircle, Loader } from 'lucide-react'

const CATEGORY_LABELS = {
  rbi_guideline:   'RBI Guidelines',
  upi_product_doc: 'Network Product Docs',
  past_brd:        'Past BRDs',
  api_spec:        'API Specifications',
  xsd:             'Existing XSDs',
}

const CATEGORY_COLORS = {
  rbi_guideline:   { bg: 'rgba(100,160,220,0.12)', color: '#6ea8dc' },
  upi_product_doc: { bg: 'rgba(76,175,125,0.12)',  color: '#4caf7d' },
  past_brd:        { bg: 'rgba(232,164,74,0.12)',  color: '#e8a44a' },
  api_spec:        { bg: 'rgba(160,120,220,0.12)', color: '#b388e8' },
  xsd:             { bg: 'rgba(218,119,86,0.12)',  color: '#da7756' },
}

function Badge({ category }) {
  const cfg = CATEGORY_COLORS[category] || { bg: 'rgba(154,154,150,0.12)', color: '#9a9a96' }
  return (
    <span style={{
      padding: '2px 10px', borderRadius: '20px', fontSize: '11px', fontWeight: '500',
      background: cfg.bg, color: cfg.color, whiteSpace: 'nowrap',
    }}>
      {CATEGORY_LABELS[category] || category}
    </span>
  )
}

// ── Search panel ────────────────────────────────────────────────────────────
function SearchPanel() {
  const [query, setQuery] = useState('')
  const [results, setResults] = useState(null)
  const [searching, setSearching] = useState(false)
  const [error, setError] = useState('')

  const handleSearch = async (e) => {
    e.preventDefault()
    if (!query.trim()) return
    setSearching(true); setError(''); setResults(null)
    try {
      const res = await ragApi.search({ query, top_k: 5 })
      setResults(res.data)
    } catch (err) {
      setError(err.response?.data?.detail || 'Search failed')
    } finally {
      setSearching(false)
    }
  }

  return (
    <div style={{
      background: 'var(--bg-elevated)', border: '1px solid var(--border)',
      borderRadius: '8px', padding: '20px',
    }}>
      <h2 style={{ margin: '0 0 14px', fontSize: '13px', fontWeight: '600', color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
        Test Retrieval
      </h2>
      <form onSubmit={handleSearch} style={{ display: 'flex', gap: '10px', marginBottom: '16px' }}>
        <input
          value={query}
          onChange={e => setQuery(e.target.value)}
          placeholder={t('ph.kb.search')}
          style={{
            flex: 1, padding: '9px 14px',
            background: 'var(--bg-input)', border: '1px solid var(--border)',
            borderRadius: '6px', color: 'var(--text-primary)', fontSize: '13px', outline: 'none',
          }}
          onFocus={e => e.target.style.borderColor = 'var(--accent)'}
          onBlur={e => e.target.style.borderColor = 'var(--border)'}
        />
        <button
          type="submit"
          disabled={searching || !query.trim()}
          style={{
            display: 'flex', alignItems: 'center', gap: '6px',
            padding: '9px 16px', background: 'var(--accent)', color: 'white',
            border: 'none', borderRadius: '6px', fontSize: '13px', fontWeight: '600',
            cursor: searching ? 'not-allowed' : 'pointer', opacity: searching ? 0.7 : 1,
          }}
        >
          {searching ? <Loader size={14} className="spin" /> : <Search size={14} />}
          Search
        </button>
      </form>

      {error && (
        <div style={{ color: 'var(--danger)', fontSize: '13px', marginBottom: '12px' }}>{error}</div>
      )}

      {results && (
        <div>
          <p style={{ margin: '0 0 10px', fontSize: '12px', color: 'var(--text-muted)' }}>
            {results.count} chunk{results.count !== 1 ? 's' : ''} found
          </p>
          {results.results.length === 0 ? (
            <p style={{ color: 'var(--text-muted)', fontSize: '13px' }}>No relevant chunks found. Try ingesting documents first.</p>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
              {results.results.map((r) => (
                <div key={r.id} style={{
                  background: 'var(--bg-card)', border: '1px solid var(--border-subtle)',
                  borderRadius: '6px', padding: '12px 14px',
                }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '8px' }}>
                    <Badge category={r.doc_category} />
                    <span style={{ fontSize: '11px', color: 'var(--text-muted)', flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {r.source_file}
                    </span>
                    <span style={{
                      fontSize: '11px', fontWeight: '600',
                      color: r.score > 0.7 ? 'var(--success)' : r.score > 0.5 ? 'var(--warning)' : 'var(--text-muted)',
                    }}>
                      {(r.score * 100).toFixed(1)}%
                    </span>
                  </div>
                  <p style={{ margin: 0, fontSize: '12px', color: 'var(--text-secondary)', lineHeight: '1.6', whiteSpace: 'pre-wrap' }}>
                    {r.content.length > 400 ? r.content.slice(0, 400) + '…' : r.content}
                  </p>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// ── Main page ────────────────────────────────────────────────────────────────
export default function KnowledgeBase() {
  const queryClient = useQueryClient()
  const [taskId, setTaskId] = useState(null)
  const [taskState, setTaskState] = useState(null)
  const [taskInfo, setTaskInfo] = useState(null)
  const pollRef = useRef(null)

  const { data: status, isLoading, error: statusError } = useQuery({
    queryKey: ['rag-status'],
    queryFn: () => ragApi.status().then(r => r.data),
    refetchInterval: taskId ? 5000 : false,
  })

  // Poll task status while a task is running
  useEffect(() => {
    if (!taskId) return
    pollRef.current = setInterval(async () => {
      try {
        const res = await ragApi.taskStatus(taskId)
        const { state, info } = res.data
        setTaskState(state)
        setTaskInfo(info)
        if (state === 'SUCCESS' || state === 'FAILURE') {
          clearInterval(pollRef.current)
          setTaskId(null)
          queryClient.invalidateQueries(['rag-status'])
        }
      } catch { /* poll tick failed; the next interval retries */ }
    }, 2000)
    return () => clearInterval(pollRef.current)
  }, [taskId])

  const ingestMutation = useMutation({
    mutationFn: (force) => ragApi.ingest(force),
    onSuccess: (res) => {
      setTaskId(res.data.task_id)
      setTaskState('PENDING')
      setTaskInfo(null)
    },
  })

  const clearMutation = useMutation({
    mutationFn: () => ragApi.clearChunks(),
    onSuccess: () => queryClient.invalidateQueries(['rag-status']),
  })

  const isIngesting = !!taskId || ingestMutation.isPending

  const statCard = (label, value, color) => (
    <div style={{
      background: 'var(--bg-elevated)', border: '1px solid var(--border)',
      borderRadius: '8px', padding: '16px 20px', flex: 1,
    }}>
      <p style={{ margin: '0 0 6px', fontSize: '11px', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>{label}</p>
      <p style={{ margin: 0, fontSize: '28px', fontWeight: '700', color: color || 'var(--text-primary)' }}>{value}</p>
    </div>
  )

  return (
    <div style={{ padding: '32px', maxWidth: '960px' }}>
      {/* API error banner */}
      {statusError && (
        <div style={{
          display: 'flex', alignItems: 'flex-start', gap: '10px',
          padding: '12px 16px', borderRadius: '8px', marginBottom: '20px',
          background: 'rgba(224,108,108,0.12)', border: '1px solid rgba(224,108,108,0.3)',
        }}>
          <AlertCircle size={16} style={{ color: 'var(--danger)', flexShrink: 0, marginTop: '1px' }} />
          <div>
            <p style={{ margin: '0 0 2px', fontSize: '13px', fontWeight: '600', color: 'var(--danger)' }}>
              Failed to load knowledge base status
            </p>
            <p style={{ margin: 0, fontSize: '12px', color: 'var(--text-muted)' }}>
              {statusError.response?.data?.detail || statusError.message}
            </p>
          </div>
        </div>
      )}

      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: '28px' }}>
        <div>
          <h1 style={{ margin: '0 0 4px', fontSize: '20px', fontWeight: '600', color: 'var(--text-primary)' }}>
            Knowledge Base
          </h1>
          <p style={{ margin: 0, fontSize: '13px', color: 'var(--text-muted)' }}>
            Manage document ingestion for the RAG pipeline
          </p>
        </div>
        <div style={{ display: 'flex', gap: '10px' }}>
          <button
            onClick={() => clearMutation.mutate()}
            disabled={clearMutation.isPending || isIngesting || !status?.total_chunks}
            style={{
              display: 'flex', alignItems: 'center', gap: '6px',
              padding: '8px 14px', background: 'transparent',
              border: '1px solid var(--border)', borderRadius: '6px',
              color: 'var(--danger)', fontSize: '13px', cursor: 'pointer',
              opacity: (clearMutation.isPending || isIngesting || !status?.total_chunks) ? 0.5 : 1,
            }}
          >
            <Trash2 size={14} />
            Clear All
          </button>
          <button
            onClick={() => ingestMutation.mutate(false)}
            disabled={isIngesting}
            style={{
              display: 'flex', alignItems: 'center', gap: '6px',
              padding: '8px 14px', background: 'var(--accent)', color: 'white',
              border: 'none', borderRadius: '6px', fontSize: '13px', fontWeight: '600',
              cursor: isIngesting ? 'not-allowed' : 'pointer', opacity: isIngesting ? 0.7 : 1,
            }}
          >
            {isIngesting ? <Loader size={14} /> : <RefreshCw size={14} />}
            {isIngesting ? 'Ingesting…' : 'Ingest Documents'}
          </button>
        </div>
      </div>

      {/* Task progress banner */}
      {(isIngesting || taskState === 'SUCCESS' || taskState === 'FAILURE') && (
        <div style={{
          display: 'flex', alignItems: 'center', gap: '12px',
          padding: '12px 16px', borderRadius: '8px', marginBottom: '20px',
          background: taskState === 'SUCCESS' ? 'rgba(76,175,125,0.12)' :
                      taskState === 'FAILURE' ? 'rgba(224,108,108,0.12)' : 'var(--accent-subtle)',
          border: `1px solid ${taskState === 'SUCCESS' ? 'rgba(76,175,125,0.3)' :
                                taskState === 'FAILURE' ? 'rgba(224,108,108,0.3)' : 'rgba(218,119,86,0.3)'}`,
        }}>
          {taskState === 'SUCCESS' ? <CheckCircle size={16} style={{ color: 'var(--success)' }} /> :
           taskState === 'FAILURE' ? <AlertCircle size={16} style={{ color: 'var(--danger)' }} /> :
           <Loader size={16} style={{ color: 'var(--accent)' }} />}
          <div>
            {taskState === 'SUCCESS' && taskInfo ? (
              <span style={{ fontSize: '13px', color: 'var(--success)', fontWeight: '500' }}>
                Ingestion complete — {taskInfo.processed} files processed, {taskInfo.chunks_created} chunks created
                {taskInfo.skipped > 0 ? `, ${taskInfo.skipped} skipped` : ''}
              </span>
            ) : taskState === 'FAILURE' ? (
              <span style={{ fontSize: '13px', color: 'var(--danger)' }}>
                Ingestion failed: {taskInfo?.error || 'Unknown error'}
              </span>
            ) : (
              <span style={{ fontSize: '13px', color: 'var(--accent)' }}>
                {taskInfo?.status || 'Ingestion in progress…'}
              </span>
            )}
          </div>
        </div>
      )}

      {/* Stats row */}
      <div style={{ display: 'flex', gap: '16px', marginBottom: '24px' }}>
        {statCard('Total Chunks', isLoading ? '…' : (status?.total_chunks ?? 0), 'var(--accent)')}
        {statCard('Categories', isLoading ? '…' : Object.keys(status?.by_category ?? {}).length)}
        {statCard('Files Ingested', isLoading ? '…' : (status?.files?.length ?? 0))}
      </div>

      {/* Per-category breakdown */}
      {status?.by_category && Object.keys(status.by_category).length > 0 && (
        <div style={{
          background: 'var(--bg-elevated)', border: '1px solid var(--border)',
          borderRadius: '8px', padding: '20px', marginBottom: '24px',
        }}>
          <h2 style={{ margin: '0 0 14px', fontSize: '13px', fontWeight: '600', color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
            By Category
          </h2>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
            {Object.entries(status.by_category).map(([cat, count]) => (
              <div key={cat} style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                <Badge category={cat} />
                <div style={{ flex: 1, height: '6px', background: 'var(--border)', borderRadius: '3px', overflow: 'hidden' }}>
                  <div style={{
                    height: '100%', borderRadius: '3px',
                    width: `${Math.round((count / status.total_chunks) * 100)}%`,
                    background: CATEGORY_COLORS[cat]?.color || 'var(--accent)',
                    transition: 'width 0.4s ease',
                  }} />
                </div>
                <span style={{ fontSize: '12px', color: 'var(--text-muted)', minWidth: '60px', textAlign: 'right' }}>
                  {count} chunks
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* File list */}
      {status?.files?.length > 0 && (
        <div style={{
          background: 'var(--bg-elevated)', border: '1px solid var(--border)',
          borderRadius: '8px', marginBottom: '24px', overflow: 'hidden',
        }}>
          <div style={{ padding: '16px 20px', borderBottom: '1px solid var(--border-subtle)' }}>
            <h2 style={{ margin: 0, fontSize: '13px', fontWeight: '600', color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
              Ingested Files
            </h2>
          </div>
          {status.files.map((f, i) => (
            <div key={f.file} style={{
              display: 'flex', alignItems: 'center', gap: '12px',
              padding: '11px 20px',
              borderBottom: i < status.files.length - 1 ? '1px solid var(--border-subtle)' : 'none',
            }}>
              <FileText size={14} style={{ color: 'var(--text-muted)', flexShrink: 0 }} />
              <span style={{ flex: 1, fontSize: '13px', color: 'var(--text-primary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {f.file}
              </span>
              <Badge category={f.category} />
              <span style={{ fontSize: '12px', color: 'var(--text-muted)', minWidth: '70px', textAlign: 'right' }}>
                {f.chunks} chunks
              </span>
            </div>
          ))}
        </div>
      )}

      {/* Empty state */}
      {!isLoading && status?.total_chunks === 0 && (
        <div style={{
          background: 'var(--bg-elevated)', border: '1px solid var(--border)',
          borderRadius: '8px', padding: '48px', textAlign: 'center', marginBottom: '24px',
        }}>
          <Database size={32} style={{ color: 'var(--text-muted)', margin: '0 auto 12px' }} />
          <p style={{ margin: '0 0 6px', fontSize: '14px', color: 'var(--text-secondary)', fontWeight: '500' }}>
            No documents ingested yet
          </p>
          <p style={{ margin: '0 0 20px', fontSize: '13px', color: 'var(--text-muted)' }}>
            Place documents in the <code style={{ background: 'var(--bg-card)', padding: '1px 6px', borderRadius: '4px' }}>knowledge_base/</code> folder and click Ingest Documents.
          </p>
          <button
            onClick={() => ingestMutation.mutate(false)}
            disabled={isIngesting}
            style={{
              padding: '9px 20px', background: 'var(--accent)', color: 'white',
              border: 'none', borderRadius: '6px', fontSize: '13px', fontWeight: '600',
              cursor: 'pointer',
            }}
          >
            Ingest Documents
          </button>
        </div>
      )}

      {/* Search panel */}
      <SearchPanel />
    </div>
  )
}
