// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

/**
 * ActiveJobsTray — sidebar / header indicator showing every in-flight job
 * the current user can see. Click → expand → list with one row per job
 * → click a row → navigate to the relevant screen which auto-resumes.
 *
 * Visibility (matches the JobsContext / API rules):
 *   - Change-request-scoped jobs (BRD, TSD, Canvas, Research, etc.) →
 *     visible to all authed users with attribution
 *   - Admin-only jobs (Code Indexing, RAG re-ingest) → visible only
 *     to original user + admins (filtered server-side; we just render
 *     what the API gives us)
 *
 * Renders nothing when no jobs are active — silent unless something's
 * happening.
 */
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { Loader, ChevronDown, ChevronUp, Activity } from 'lucide-react'
import { useJobs } from '../../context/useJobs'

const MODULE_LABELS = {
  brd:           'BRD',
  tech_spec:     'Tech Spec',
  research:      'Deep Research',
  canvas:        'Product Canvas',
  xsd:           'XSD',
  product_kit:   'Product Kit',
  enhance:       'Prompt Enhancer',
  clarification: 'Clarification',
  code_indexing: 'Code Indexing',
  code_change:   'Code Change',
  code_review:   'Code Review',
  is_review:     'IS Review',
  build:         'Build',
}

function moduleLabel(m) {
  return MODULE_LABELS[m] || m
}

function deepLinkFor(job) {
  // Map (module, change_request_id, subtype) → app route. Falls back to
  // null when there's no obvious destination (admin tasks). The tray
  // simply doesn't make those rows clickable.
  if (!job.change_request_id) {
    if (job.module === 'code_indexing') return '/admin/code-indexing'
    if (job.module === 'rag_ingest')    return '/admin/code-knowledge'
    return null
  }
  const base = `/changes/${job.change_request_id}`
  switch (job.module) {
    case 'brd':           return `${base}/brd`
    case 'tech_spec':     return `${base}/tech-spec`
    case 'research':      return `${base}/research`
    case 'canvas':        return `${base}/canvas`
    case 'xsd':           return `${base}/xsd`
    case 'product_kit':   return `${base}/product-kit`
    case 'enhance':       return `${base}/enhance`
    case 'clarification': return `${base}`
    default:              return base
  }
}

function shortAge(iso) {
  if (!iso) return ''
  const sec = Math.floor((Date.now() - new Date(iso).getTime()) / 1000)
  if (sec < 60) return `${sec}s`
  const m = Math.floor(sec / 60)
  if (m < 60) return `${m}m`
  return `${Math.floor(m / 60)}h${m % 60}m`
}

export default function ActiveJobsTray() {
  const { activeJobs } = useJobs()
  const jobs = Object.values(activeJobs).filter(j =>
    j.status === 'running' || j.status === 'pending'
  )
  const [expanded, setExpanded] = useState(false)

  if (jobs.length === 0) return null

  return (
    <div
      style={{
        margin: '8px 12px',
        padding: '8px 10px',
        background: 'var(--bg-elevated)',
        border: '1px solid var(--border)',
        borderRadius: '6px',
        fontSize: '12px',
      }}
    >
      <button
        onClick={() => setExpanded(e => !e)}
        style={{
          display: 'flex', alignItems: 'center', gap: '8px', width: '100%',
          background: 'transparent', border: 'none', cursor: 'pointer',
          color: 'var(--text-secondary)', padding: 0,
        }}
        title={expanded ? 'Collapse jobs list' : 'Expand jobs list'}
      >
        <Activity size={13} style={{ color: 'var(--accent)' }} />
        <span style={{ flex: 1, textAlign: 'left' }}>
          {jobs.length} active {jobs.length === 1 ? 'job' : 'jobs'}
        </span>
        {expanded
          ? <ChevronUp size={12} />
          : <ChevronDown size={12} />}
      </button>

      {expanded && (
        <ul style={{ listStyle: 'none', margin: '8px 0 0', padding: 0 }}>
          {jobs.map(job => {
            const link = deepLinkFor(job)
            const row = (
              <div
                style={{
                  display: 'flex', alignItems: 'center', gap: '8px',
                  padding: '6px 4px',
                  borderTop: '1px solid var(--border-subtle, var(--border))',
                  fontSize: '11px',
                  color: 'var(--text-primary)',
                }}
              >
                <Loader
                  size={11}
                  style={{ color: 'var(--accent)', animation: 'spin 1s linear infinite', flexShrink: 0 }}
                />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontWeight: 500 }}>
                    {moduleLabel(job.module)}
                    {job.subtype && (
                      <span style={{ color: 'var(--text-muted)', fontWeight: 400 }}> · {job.subtype}</span>
                    )}
                  </div>
                  <div
                    style={{
                      color: 'var(--text-muted)', fontSize: '10px',
                      whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                    }}
                    title={job.current_stage || ''}
                  >
                    {job.current_stage || 'in progress'} · {shortAge(job.started_at)}
                  </div>
                </div>
                {typeof job.progress_pct === 'number' && (
                  <div style={{ color: 'var(--text-muted)', fontSize: '10px' }}>
                    {job.progress_pct}%
                  </div>
                )}
              </div>
            )
            return (
              <li key={job.id}>
                {link
                  ? <Link
                      to={link}
                      style={{ textDecoration: 'none', color: 'inherit', display: 'block' }}
                      onClick={() => setExpanded(false)}
                    >{row}</Link>
                  : row}
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
