// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useState } from 'react'
import { t } from '../../strings'
import { useQuery } from '@tanstack/react-query'
import { ragApi, codeIndexingApi } from '../../services/api'
import {
  Code2, FileCode, Database, Search, Loader, GitBranch,
} from 'lucide-react'
import StatTile, { StatTileRow } from '../../components/common/StatTile'

export default function CodeKnowledge() {
  const [searchQuery, setSearchQuery] = useState('')
  const [searchResults, setSearchResults] = useState(null)
  const [searching, setSearching] = useState(false)

  const { data: status } = useQuery({
    queryKey: ['rag-status'],
    queryFn: () => ragApi.status().then(r => r.data),
  })

  const { data: indexingStatus } = useQuery({
    queryKey: ['code-indexing-status'],
    queryFn: () => codeIndexingApi.status().then(r => r.data),
  })

  // Java source chunks only
  const javaChunks = status?.by_category?.java_source || 0
  const javaFiles = (status?.files || []).filter(f => f.category === 'java_source')
  const repos = indexingStatus?.repos || []

  const handleSearch = async () => {
    if (!searchQuery.trim()) return
    setSearching(true)
    try {
      const res = await ragApi.search({ query: searchQuery, top_k: 5, categories: ['java_source'] })
      setSearchResults(res.data)
    } catch { /* ignore */ } finally {
      setSearching(false)
    }
  }

  return (
    <div style={{ padding: '32px 40px', maxWidth: 1600, margin: '0 auto' }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: '24px' }}>
        <div>
          <h1 style={{ margin: '0 0 4px', fontSize: '20px', fontWeight: '600', color: 'var(--text-primary)' }}>
            Code Knowledge
          </h1>
          <p style={{ margin: 0, fontSize: '13px', color: 'var(--text-muted)' }}>
            Java source code indexed from GitLab repositories — used by the Code RAG pipeline
          </p>
        </div>
      </div>

      <StatTileRow>
        <StatTile label="Vector Chunks" value={javaChunks}        accent="#b388e8"
                  hint={javaChunks ? 'embedded + searchable' : null} />
        <StatTile label="Java Files"    value={javaFiles.length}  accent="var(--text-secondary)" />
        <StatTile label="Repositories"  value={repos.length}      accent="#6ea8dc" />
        <StatTile label="Status"        value={repos.length ? 'Indexed' : 'Empty'}
                  accent={repos.length ? '#4caf7d' : 'var(--text-muted)'} />
      </StatTileRow>

      {/* Repos */}
      {repos.length > 0 && (
        <div style={{ marginBottom: '24px', borderRadius: '8px', background: 'var(--bg-elevated)', border: '1px solid var(--border)', overflow: 'hidden' }}>
          <div style={{ padding: '14px 20px', borderBottom: '1px solid var(--border-subtle)' }}>
            <h3 style={{ margin: 0, fontSize: '13px', fontWeight: '600', color: 'var(--text-primary)' }}>Indexed Repositories</h3>
          </div>
          {repos.map((r, i) => (
            <div key={r.id} style={{
              padding: '12px 20px', display: 'flex', alignItems: 'center', gap: '12px',
              borderBottom: i < repos.length - 1 ? '1px solid var(--border-subtle)' : 'none',
            }}>
              <GitBranch size={14} style={{ color: 'var(--accent)', flexShrink: 0 }} />
              <span style={{ fontSize: '13px', color: 'var(--text-primary)', fontWeight: '500', flex: 1 }}>{r.label}</span>
              <span style={{ fontSize: '12px', color: 'var(--text-muted)', fontFamily: 'monospace' }}>{r.gitlab_repo}</span>
              <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>{r.files_count} files</span>
              <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>{r.chunks_count} chunks</span>
              {r.last_indexed_at && (
                <span style={{ fontSize: '10px', color: 'var(--text-muted)' }}>
                  {new Date(r.last_indexed_at).toLocaleDateString()}
                </span>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Files list */}
      {javaFiles.length > 0 && (
        <div style={{ marginBottom: '24px', borderRadius: '8px', background: 'var(--bg-elevated)', border: '1px solid var(--border)', overflow: 'hidden' }}>
          <div style={{ padding: '14px 20px', borderBottom: '1px solid var(--border-subtle)' }}>
            <h3 style={{ margin: 0, fontSize: '13px', fontWeight: '600', color: 'var(--text-primary)' }}>
              Indexed Java Files ({javaFiles.length})
            </h3>
          </div>
          <div style={{ maxHeight: '300px', overflowY: 'auto' }}>
            {javaFiles.map((f, i) => (
              <div key={f.file} style={{
                padding: '8px 20px', display: 'flex', alignItems: 'center', gap: '10px',
                borderBottom: i < javaFiles.length - 1 ? '1px solid var(--border-subtle)' : 'none',
              }}>
                <FileCode size={12} style={{ color: 'var(--text-muted)', flexShrink: 0 }} />
                <span style={{ fontSize: '12px', color: 'var(--text-primary)', fontFamily: 'monospace', flex: 1 }}>{f.file}</span>
                <span style={{ fontSize: '10px', color: 'var(--text-muted)' }}>{f.chunks} chunks</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Search */}
      <div style={{ padding: '20px', borderRadius: '8px', background: 'var(--bg-elevated)', border: '1px solid var(--border)' }}>
        <h3 style={{ margin: '0 0 12px', fontSize: '13px', fontWeight: '600', color: 'var(--text-primary)', display: 'flex', alignItems: 'center', gap: '8px' }}>
          <Search size={14} style={{ color: 'var(--accent)' }} /> Test Code Retrieval
        </h3>
        <div style={{ display: 'flex', gap: '8px', marginBottom: searchResults ? '16px' : 0 }}>
          <input
            value={searchQuery}
            onChange={e => setSearchQuery(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') handleSearch() }}
            placeholder={t('ph.codeKnowledge.search')}
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
                  <span style={{ fontSize: '10px', color: 'var(--text-muted)', fontFamily: 'monospace' }}>{r.source_file}</span>
                  <span style={{ fontSize: '10px', color: 'var(--success)', marginLeft: 'auto' }}>score: {r.score}</span>
                </div>
                <pre style={{ margin: 0, fontSize: '11px', color: 'var(--text-secondary)', lineHeight: '1.4', maxHeight: '80px', overflow: 'hidden', whiteSpace: 'pre-wrap' }}>
                  {r.content.slice(0, 300)}...
                </pre>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
