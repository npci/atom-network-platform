// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useState } from 'react'
import { t } from '../../strings'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { codeIndexingApi } from '../../services/api'
import {
  Database, Plus, Trash2, RefreshCw, Loader, CheckCircle, Code2,
  GitBranch, Clock, FileCode, AlertCircle, Layers, ChevronDown, ChevronRight,
} from 'lucide-react'
// R-6 — durable-job awareness for the multi-hour code indexing flow.
import { useJobs } from '../../context/useJobs'
import ProgressBanner from '../../components/jobs/ProgressBanner'
import { useAuth } from '../../hooks/useAuth'
import StatTile, { StatTileRow } from '../../components/common/StatTile'
import { useRepoRoles } from '../../hooks/useUiConfig'
import { roleOptions } from '../../utils/repoTopology'

export default function CodeIndexing() {
  const qc = useQueryClient()
  // Role vocabulary is the active domain pack's, not a hardcoded core/app/legacy
  // list. `builds_first` is a declared property of the role, so the highlight
  // follows the config rather than the literal string "core".
  const repoRoles = useRepoRoles()
  const repoRoleOptions = roleOptions(repoRoles)
  const buildsFirstRoles = new Set(repoRoles.filter(r => r.builds_first).map(r => r.key))
  const [label, setLabel] = useState('')
  const [repoPath, setRepoPath] = useState('')
  const [branch, setBranch] = useState('main')
  const [gitlabUrl, setGitlabUrl] = useState('')
  const [role, setRole] = useState('app')
  const [adding, setAdding] = useState(false)
  const [indexingId, setIndexingId] = useState(null)
  const [error, setError] = useState(null)
  const [showForm, setShowForm] = useState(false)
  const [openContextId, setOpenContextId] = useState(null)

  // R-6 — poll the repos list every 5 s when at least one repo has an
  // active indexing job, so the progress banner picks up `current_stage`
  // updates the backend writes during the multi-hour ingest.
  const { data: repos, isLoading } = useQuery({
    queryKey: ['code-repos'],
    queryFn: () => codeIndexingApi.listRepos().then(r => r.data),
    refetchInterval: (data) => {
      const arr = data?.state?.data ?? data
      const hasActive = Array.isArray(arr) && arr.some(r => r?.active_job)
      return hasActive ? 5_000 : false
    },
  })

  const { data: status } = useQuery({
    queryKey: ['code-indexing-status'],
    queryFn: () => codeIndexingApi.status().then(r => r.data),
  })

  // R-6 — JobsContext for the resume banner + sidebar tray integration.
  const { recordJob } = useJobs()
  const { user: me } = useAuth()

  const handleAdd = async () => {
    if (!label.trim() || !repoPath.trim()) return
    setAdding(true)
    setError(null)
    try {
      await codeIndexingApi.addRepo({
        label: label.trim(),
        gitlab_repo: repoPath.trim(),
        gitlab_branch: branch.trim() || 'main',
        gitlab_url: gitlabUrl.trim() || null,
        role,
      })
      setLabel('')
      setRepoPath('')
      setBranch('main')
      setGitlabUrl('')
      setRole('app')
      setShowForm(false)
      qc.invalidateQueries({ queryKey: ['code-repos'] })
    } catch (e) {
      setError(e.response?.data?.detail || 'Failed to add repo')
    } finally {
      setAdding(false)
    }
  }

  const handleRemove = async (repoId) => {
    setError(null)
    try {
      await codeIndexingApi.removeRepo(repoId)
      qc.invalidateQueries({ queryKey: ['code-repos'] })
      qc.invalidateQueries({ queryKey: ['code-indexing-status'] })
    } catch (e) {
      setError(e.response?.data?.detail || 'Failed to remove repo')
    }
  }

  const handleIndex = async (repoId) => {
    setIndexingId(repoId)
    setError(null)
    try {
      // R-6 — endpoint now returns 202 + {job_id} immediately (background-task).
      // Record the job in JobsContext so the sidebar tray and the per-repo
      // banner pick it up; refetch repos so the row gets `active_job` populated.
      const res = await codeIndexingApi.indexRepo(repoId)
      const data = res?.data || {}
      if (data.job_id) {
        recordJob({
          id:                 data.job_id,
          change_request_id:  null,                // admin-only job
          module:             'code_indexing',
          subtype:            repoId,
          status:             'running',
          started_at:         new Date().toISOString(),
          updated_at:         new Date().toISOString(),
          last_seen_seq:      0,
        })
      }
      qc.invalidateQueries({ queryKey: ['code-repos'] })
      qc.invalidateQueries({ queryKey: ['code-indexing-status'] })
    } catch (e) {
      setError(e.response?.data?.detail || 'Indexing failed')
    } finally {
      setIndexingId(null)
    }
  }

  const totalChunks = status?.total_chunks || 0
  const totalFiles = status?.total_files || 0
  const repoCount = repos?.length || 0
  const indexedRepos = (repos || []).filter(r => r.status === 'indexed' || r.status === 'completed').length

  return (
    <div style={{ padding: '32px 40px', maxWidth: 1600, margin: '0 auto' }}>

      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: '24px' }}>
        <div>
          <h1 style={{ margin: '0 0 4px', fontSize: '20px', fontWeight: '600', color: 'var(--text-primary)' }}>
            Code Indexing
          </h1>
          <p style={{ margin: 0, fontSize: '13px', color: 'var(--text-muted)' }}>
            Index Java source code from GitLab repositories into the vector database for Code RAG
          </p>
        </div>
        <button
          onClick={() => setShowForm(v => !v)}
          style={{
            display: 'flex', alignItems: 'center', gap: '6px',
            padding: '8px 16px', background: 'var(--accent)', color: 'white',
            border: 'none', borderRadius: '6px', fontSize: '13px', fontWeight: '600',
            cursor: 'pointer',
          }}
        >
          <Plus size={14} /> Add Repository
        </button>
      </div>

      <StatTileRow>
        <StatTile label="Repositories" value={repoCount}     accent="var(--text-secondary)"
                  hint={repoCount ? `${indexedRepos} indexed` : null} />
        <StatTile label="Files"        value={totalFiles}    accent="#6ea8dc" />
        <StatTile label="Vector Chunks" value={totalChunks}  accent="#b388e8" />
        <StatTile label="Status"       value={status?.indexing ? 'Running' : 'Idle'}
                  accent={status?.indexing ? '#da7756' : '#4caf7d'} />
      </StatTileRow>

      {/* Error */}
      {error && (
        <div style={{
          padding: '12px 16px', borderRadius: '8px', marginBottom: '20px',
          background: 'rgba(224,108,108,0.10)', border: '1px solid rgba(224,108,108,0.3)',
          display: 'flex', alignItems: 'center', gap: '8px',
        }}>
          <AlertCircle size={14} style={{ color: 'var(--danger)', flexShrink: 0 }} />
          <span style={{ fontSize: '13px', color: 'var(--danger)' }}>{error}</span>
        </div>
      )}

      {/* Add repo form */}
      {showForm && (
        <div style={{
          padding: '20px', marginBottom: '24px', borderRadius: '8px',
          background: 'var(--bg-elevated)', border: '1px solid var(--border)',
        }}>
          <h3 style={{ margin: '0 0 16px', fontSize: '14px', fontWeight: '600', color: 'var(--text-primary)' }}>
            Add GitLab Repository
          </h3>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px', marginBottom: '12px' }}>
            <div>
              <label style={{ display: 'block', fontSize: '11px', color: 'var(--text-muted)', marginBottom: '4px', textTransform: 'uppercase', letterSpacing: '0.04em' }}>Label *</label>
              <input
                value={label} onChange={e => setLabel(e.target.value)}
                placeholder={t('ph.codeIndex.repo')}
                style={{
                  width: '100%', padding: '8px 12px', borderRadius: '6px',
                  border: '1px solid var(--border)', background: 'var(--bg-input)',
                  color: 'var(--text-primary)', fontSize: '13px',
                }}
              />
            </div>
            <div>
              <label style={{ display: 'block', fontSize: '11px', color: 'var(--text-muted)', marginBottom: '4px', textTransform: 'uppercase', letterSpacing: '0.04em' }}>GitLab Repo Path *</label>
              <input
                value={repoPath} onChange={e => setRepoPath(e.target.value)}
                placeholder="e.g. root/network-platform"
                style={{
                  width: '100%', padding: '8px 12px', borderRadius: '6px',
                  border: '1px solid var(--border)', background: 'var(--bg-input)',
                  color: 'var(--text-primary)', fontSize: '13px',
                }}
              />
            </div>
            <div>
              <label style={{ display: 'block', fontSize: '11px', color: 'var(--text-muted)', marginBottom: '4px', textTransform: 'uppercase', letterSpacing: '0.04em' }}>Branch</label>
              <input
                value={branch} onChange={e => setBranch(e.target.value)}
                placeholder="main"
                style={{
                  width: '100%', padding: '8px 12px', borderRadius: '6px',
                  border: '1px solid var(--border)', background: 'var(--bg-input)',
                  color: 'var(--text-primary)', fontSize: '13px',
                }}
              />
            </div>
            <div>
              <label style={{ display: 'block', fontSize: '11px', color: 'var(--text-muted)', marginBottom: '4px', textTransform: 'uppercase', letterSpacing: '0.04em' }}>GitLab URL (optional override)</label>
              <input
                value={gitlabUrl} onChange={e => setGitlabUrl(e.target.value)}
                placeholder="Uses global config if blank"
                style={{
                  width: '100%', padding: '8px 12px', borderRadius: '6px',
                  border: '1px solid var(--border)', background: 'var(--bg-input)',
                  color: 'var(--text-primary)', fontSize: '13px',
                }}
              />
            </div>
            <div>
              <label style={{ display: 'block', fontSize: '11px', color: 'var(--text-muted)', marginBottom: '4px', textTransform: 'uppercase', letterSpacing: '0.04em' }}>Build role</label>
              <select
                value={role} onChange={e => setRole(e.target.value)}
                title="core = framework/XSD repo built FIRST (mvn install) so dependent app repos resolve its artifacts from the shared Maven cache"
                style={{
                  width: '100%', padding: '8px 12px', borderRadius: '6px',
                  border: '1px solid var(--border)', background: 'var(--bg-input)',
                  color: 'var(--text-primary)', fontSize: '13px',
                }}
              >
                <option value="app">app — business logic (built after core)</option>
                <option value="core">core — framework/XSDs (built first)</option>
                <option value="legacy">legacy — soft-fail eligible</option>
              </select>
            </div>
          </div>
          <div style={{ display: 'flex', gap: '10px', justifyContent: 'flex-end' }}>
            <button onClick={() => setShowForm(false)} style={{
              padding: '8px 14px', background: 'transparent', border: '1px solid var(--border)',
              borderRadius: '6px', color: 'var(--text-muted)', fontSize: '13px', cursor: 'pointer',
            }}>Cancel</button>
            <button
              onClick={handleAdd}
              disabled={adding || !label.trim() || !repoPath.trim()}
              style={{
                display: 'flex', alignItems: 'center', gap: '6px',
                padding: '8px 16px', background: 'var(--accent)', color: 'white',
                border: 'none', borderRadius: '6px', fontSize: '13px', fontWeight: '600',
                cursor: (adding || !label.trim() || !repoPath.trim()) ? 'not-allowed' : 'pointer',
                opacity: (adding || !label.trim() || !repoPath.trim()) ? 0.5 : 1,
              }}
            >
              {adding ? <Loader size={13} style={{ animation: 'spin 1s linear infinite' }} /> : <Plus size={13} />}
              Add Repository
            </button>
          </div>
        </div>
      )}

      {/* Stats row */}
      <div style={{ display: 'flex', gap: '16px', marginBottom: '24px' }}>
        {[
          { label: 'Repositories', value: repoCount, icon: Database },
          { label: 'Files Indexed', value: totalFiles, icon: FileCode },
          { label: 'Vector Chunks', value: totalChunks, icon: Code2 },
        ].map(stat => (
          <div key={stat.label} style={{
            flex: 1, padding: '16px 20px', borderRadius: '8px',
            background: 'var(--bg-elevated)', border: '1px solid var(--border)',
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '6px' }}>
              <stat.icon size={14} style={{ color: 'var(--accent)' }} />
              <span style={{ fontSize: '11px', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>
                {stat.label}
              </span>
            </div>
            <p style={{ margin: 0, fontSize: '24px', fontWeight: '700', color: 'var(--text-primary)' }}>
              {stat.value}
            </p>
          </div>
        ))}
      </div>

      {/* Loading */}
      {isLoading && (
        <div style={{ padding: '32px', textAlign: 'center', color: 'var(--text-muted)', fontSize: '13px' }}>
          Loading repositories...
        </div>
      )}

      {/* Empty state */}
      {!isLoading && repoCount === 0 && (
        <div style={{
          textAlign: 'center', padding: '48px 32px',
          background: 'var(--bg-elevated)', border: '1px solid var(--border)', borderRadius: '10px',
        }}>
          <Database size={36} style={{ color: 'var(--accent)', marginBottom: '12px' }} />
          <h2 style={{ margin: '0 0 8px', fontSize: '16px', fontWeight: '600', color: 'var(--text-primary)' }}>
            No repositories registered
          </h2>
          <p style={{ margin: '0 auto 20px', fontSize: '13px', color: 'var(--text-muted)', maxWidth: '400px' }}>
            Add your GitLab repositories to index Java source code into the vector database.
            This enables the Code RAG pipeline for AI-powered code generation.
          </p>
          <button onClick={() => setShowForm(true)} style={{
            padding: '9px 20px', background: 'var(--accent)', color: 'white',
            border: 'none', borderRadius: '8px', fontSize: '14px', fontWeight: '600', cursor: 'pointer',
          }}>
            <Plus size={14} style={{ marginRight: 6, verticalAlign: 'text-bottom' }} />
            Add First Repository
          </button>
        </div>
      )}

      {/* Repo cards */}
      {repos && repos.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
          {repos.map(repo => {
            // R-6 — `active_job` from the backend is the source-of-truth for
            // "indexing is currently running on this repo". Local
            // `indexingId` covers the brief in-flight state of the POST
            // before the next refetch returns. Either signals "indexing".
            const activeJob   = repo.active_job || null
            const isIndexing  = (indexingId === repo.id) || Boolean(activeJob)
            return (
              <div key={repo.id} style={{
                padding: '20px', borderRadius: '8px',
                background: 'var(--bg-elevated)', border: '1px solid var(--border)',
              }}>
                {/* Repo header */}
                <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '14px' }}>
                  <div style={{
                    width: '36px', height: '36px', borderRadius: '8px',
                    background: 'rgba(218,119,86,0.10)', border: '1px solid rgba(218,119,86,0.2)',
                    display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0,
                  }}>
                    <Database size={16} style={{ color: 'var(--accent)' }} />
                  </div>
                  <div style={{ flex: 1 }}>
                    <p style={{ margin: '0 0 2px', fontSize: '14px', fontWeight: '600', color: 'var(--text-primary)' }}>
                      {repo.label}
                    </p>
                    <p style={{ margin: 0, fontSize: '12px', color: 'var(--text-muted)', fontFamily: 'monospace' }}>
                      {repo.gitlab_repo}
                    </p>
                  </div>
                  <div style={{ display: 'flex', gap: '8px' }}>
                    <button
                      onClick={() => handleIndex(repo.id)}
                      disabled={isIndexing}
                      style={{
                        display: 'flex', alignItems: 'center', gap: '6px',
                        padding: '7px 14px', background: 'var(--accent)', color: 'white',
                        border: 'none', borderRadius: '6px', fontSize: '12px', fontWeight: '600',
                        cursor: isIndexing ? 'not-allowed' : 'pointer', opacity: isIndexing ? 0.7 : 1,
                      }}
                    >
                      {isIndexing
                        ? <Loader size={12} style={{ animation: 'spin 1s linear infinite' }} />
                        : <RefreshCw size={12} />}
                      {isIndexing ? 'Indexing...' : 'Index Now'}
                    </button>
                    <button
                      onClick={() => setOpenContextId(id => id === repo.id ? null : repo.id)}
                      style={{
                        display: 'flex', alignItems: 'center', gap: '5px',
                        padding: '7px 12px', background: 'transparent',
                        border: '1px solid var(--border)', borderRadius: '6px',
                        color: 'var(--text-secondary)', fontSize: '12px', cursor: 'pointer',
                      }}
                    >
                      {openContextId === repo.id ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                      <Layers size={12} /> View context
                    </button>
                    <button
                      onClick={() => handleRemove(repo.id)}
                      disabled={isIndexing}
                      style={{
                        display: 'flex', alignItems: 'center', gap: '5px',
                        padding: '7px 12px', background: 'transparent',
                        border: '1px solid var(--border)', borderRadius: '6px',
                        color: 'var(--danger)', fontSize: '12px', cursor: isIndexing ? 'not-allowed' : 'pointer',
                        opacity: isIndexing ? 0.5 : 1,
                      }}
                    >
                      <Trash2 size={12} /> Remove
                    </button>
                  </div>
                </div>

                {/* R-6 — per-repo resume banner. Renders only when an
                    indexing job is in flight. Picks up current_stage
                    from the backend job record (updated at boundaries:
                    'Cloning', 'Persisting stats', 'Indexed N/M'). */}
                {activeJob && (
                  <ProgressBanner
                    jobId={activeJob.id}
                    status={activeJob.status}
                    stage={activeJob.current_stage}
                    progress={activeJob.progress_pct}
                    startedAt={activeJob.started_at}
                    startedBy={activeJob.started_by_user_id}
                    resuming={false}
                    onCancel={null}    /* visible-only per the cancel=A default; mid-pipeline checks land in R-9 */
                    currentUserId={me?.id}
                  />
                )}

                {/* Repo stats */}
                <div style={{ display: 'flex', gap: '20px', flexWrap: 'wrap' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                    <GitBranch size={13} style={{ color: 'var(--text-muted)' }} />
                    <span style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>{repo.gitlab_branch}</span>
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}
                    title="Build order for multi-repo changes: core repos build first (mvn install) so app repos resolve their artifacts">
                    <span style={{ fontSize: '11px', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>role</span>
                    <select
                      value={repo.role || 'app'}
                      onChange={async (e) => {
                        try {
                          await codeIndexingApi.updateRepo(repo.id, { role: e.target.value })
                          qc.invalidateQueries({ queryKey: ['code-repos'] })
                        } catch (err) { setError(err.response?.data?.detail || 'Failed to update role') }
                      }}
                      style={{ fontSize: '12px', padding: '2px 6px', borderRadius: '5px',
                        border: '1px solid var(--border)',
                        background: buildsFirstRoles.has(repo.role) ? 'rgba(96,165,250,0.10)' : 'var(--bg-input)',
                        color: buildsFirstRoles.has(repo.role) ? '#60a5fa' : 'var(--text-secondary)' }}
                    >
                      {/* Options come from the active domain pack's declared
                          topology; falls back to the historical UPI vocabulary
                          when the domain declares none. */}
                      {repoRoleOptions.map(opt => (
                        <option key={opt.value} value={opt.value}>{opt.label}</option>
                      ))}
                    </select>
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                    <FileCode size={13} style={{ color: 'var(--text-muted)' }} />
                    <span style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>{repo.files_count} files</span>
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                    <Code2 size={13} style={{ color: 'var(--text-muted)' }} />
                    <span style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>{repo.chunks_count} chunks</span>
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                    <Clock size={13} style={{ color: 'var(--text-muted)' }} />
                    <span style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>
                      {repo.last_indexed_at
                        ? `Indexed ${new Date(repo.last_indexed_at).toLocaleString()}`
                        : 'Never indexed'}
                    </span>
                  </div>
                </div>

                {/* Indexing progress */}
                {isIndexing && (
                  <div style={{
                    marginTop: '14px', padding: '12px 16px', borderRadius: '6px',
                    background: 'rgba(218,119,86,0.06)', border: '1px solid rgba(218,119,86,0.15)',
                    display: 'flex', alignItems: 'center', gap: '10px',
                  }}>
                    <Loader size={14} style={{ color: 'var(--accent)', animation: 'spin 1s linear infinite' }} />
                    <span style={{ fontSize: '12px', color: 'var(--accent)' }}>
                      Fetching files from GitLab, chunking, and generating embeddings via Ollama...
                    </span>
                  </div>
                )}

                {/* Generated context viewer (module-wise context + indexed code) */}
                {openContextId === repo.id && <RepoContextPanel repoId={repo.id} />}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

// Shows everything the indexer generated for one repo: the module-wise context
// tree (module_context rows) + a summary of the indexed code chunks/symbols.
function RepoContextPanel({ repoId }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ['repo-context', repoId],
    queryFn: () => codeIndexingApi.getContext(repoId).then(r => r.data),
  })

  const wrap = {
    marginTop: '14px', padding: '16px', borderRadius: '6px',
    background: 'var(--bg-input)', border: '1px solid var(--border)', fontSize: '12px',
  }
  const muted = { color: 'var(--text-muted)' }

  if (isLoading) return <div style={wrap}><span style={muted}>Loading generated context…</span></div>
  if (error) return <div style={wrap}><span style={{ color: 'var(--danger)' }}>Failed to load context.</span></div>

  const modules = data?.modules || []
  const chunks = data?.chunks || { total: 0, by_language: {}, files: [] }
  const flow = data?.flow || null
  const epLabel = (e) => (typeof e === 'string' ? e : `${e.kind ? e.kind + ':' : ''}${e.name}`)
  // key_types rows are now {name, kind, file}; older indexes stored bare name strings.
  const ktLabel = (t) => (typeof t === 'string' ? t : (t.file ? `${t.name} → ${t.file}` : t.name))

  return (
    <div style={wrap}>
      {/* Reuse-first flow map: which API carries the transaction (debit/credit) leg. */}
      {flow && (
        <div style={{ marginBottom: 14, padding: '10px 12px', borderRadius: 6,
          background: 'rgba(245,158,11,0.06)', border: '1px solid rgba(245,158,11,0.3)' }}>
          <h4 style={{ margin: '0 0 6px', fontSize: '13px', color: 'var(--text-primary)' }}>🧭 API flow map</h4>
          {flow.summary && <div style={{ color: 'var(--text-secondary)', marginBottom: 6 }}>{flow.summary}</div>}
          {flow.transaction_apis?.length > 0 && (
            <div style={muted}><strong style={{ color: '#16a34a' }}>transaction (debit/credit):</strong>{' '}
              <span style={{ fontFamily: 'monospace' }}>{flow.transaction_apis.map(a => a.api).join(', ')}</span></div>
          )}
          {flow.meta_apis?.length > 0 && (
            <div style={muted}>meta/initiation/status: <span style={{ fontFamily: 'monospace' }}>{flow.meta_apis.map(a => a.api).join(', ')}</span></div>
          )}
          {flow.flows?.map((f, i) => (
            <div key={i} style={muted}>{f.name}: <span style={{ fontFamily: 'monospace' }}>{(f.steps || []).join(' → ')}</span></div>
          ))}
        </div>
      )}
      <h4 style={{ margin: '0 0 10px', fontSize: '13px', color: 'var(--text-primary)' }}>
        Module-wise context — {modules.length} module{modules.length === 1 ? '' : 's'}
      </h4>
      {modules.length === 0 && (
        <p style={{ ...muted, margin: '0 0 12px' }}>
          No module context yet. Re-index this repo (module context builds in parallel) — needs a Maven <code>pom.xml</code>.
        </p>
      )}
      {modules.map(m => (
        <div key={m.module_path} style={{
          marginLeft: `${m.depth * 18}px`, marginBottom: '10px', paddingLeft: '10px',
          borderLeft: '2px solid var(--border)',
        }}>
          <div style={{ fontWeight: 600, color: 'var(--text-primary)', fontFamily: 'monospace' }}>
            {m.module_path}
            {m.java_version && <span style={{ ...muted, fontWeight: 400 }}> · Java {m.java_version}</span>}
          </div>
          {m.summary && <div style={{ color: 'var(--text-secondary)', margin: '2px 0' }}>{m.summary}</div>}
          {m.functional_flow && (
            <div style={{ ...muted, fontStyle: 'italic', margin: '2px 0' }}>flow: {m.functional_flow}</div>
          )}
          {m.key_types?.length > 0 && (
            <div style={muted}>key types: <span style={{ fontFamily: 'monospace' }}>{m.key_types.map(ktLabel).join(', ')}</span></div>
          )}
          {m.entry_points?.length > 0 && (
            <div style={muted}>entry points: <span style={{ fontFamily: 'monospace' }}>{m.entry_points.map(epLabel).join(', ')}</span></div>
          )}
          {m.depends_on?.length > 0 && (
            <div style={muted}>depends on: <span style={{ fontFamily: 'monospace' }}>{m.depends_on.join(', ')}</span></div>
          )}
        </div>
      ))}

      <h4 style={{ margin: '14px 0 8px', fontSize: '13px', color: 'var(--text-primary)' }}>
        Indexed code — {chunks.total} chunk{chunks.total === 1 ? '' : 's'}
      </h4>
      {Object.keys(chunks.by_language || {}).length > 0 && (
        <div style={{ ...muted, marginBottom: '8px' }}>
          by language: {Object.entries(chunks.by_language).map(([k, v]) => `${k}: ${v}`).join('   ')}
        </div>
      )}
      {chunks.files?.length > 0 && (
        <details>
          <summary style={{ cursor: 'pointer', color: 'var(--text-secondary)' }}>
            {chunks.files.length} indexed file{chunks.files.length === 1 ? '' : 's'}
          </summary>
          <div style={{ marginTop: '8px', maxHeight: '320px', overflowY: 'auto' }}>
            {chunks.files.map(f => (
              <div key={f.path} style={{ marginBottom: '6px' }}>
                <span style={{ fontFamily: 'monospace', color: 'var(--text-secondary)' }}>{f.path}</span>
                <span style={muted}> — {f.chunks} chunk{f.chunks === 1 ? '' : 's'}{f.symbols?.length ? `, ${f.symbols.length} symbols` : ''}</span>
                {f.symbols?.length > 0 && (
                  <div style={{ ...muted, fontFamily: 'monospace', marginLeft: '12px' }}>
                    {f.symbols.map(s => `${s.kind || ''} ${s.name}`.trim()).join(', ')}
                  </div>
                )}
              </div>
            ))}
          </div>
        </details>
      )}
    </div>
  )
}
