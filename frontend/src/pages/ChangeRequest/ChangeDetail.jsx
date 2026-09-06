// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useState, useRef, useEffect, useCallback, useMemo } from 'react'
import { hardenFrameHtml } from '../../utils/safeHtmlFrame'
import { t } from '../../strings'
import { useParams, useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { changesApi, agentsApi, phaseBApi, phaseCApi, clarifyApi, validationApi, agenticApi, uiConfigApi } from '../../services/api'
import { wsUrl } from '../../utils/basePath'
import { safeHref } from '../../utils/safeUrl'
import {
  ArrowLeft, ArrowRight, CheckCircle, Circle, Clock,
  ChevronDown, ChevronUp, ChevronRight, MessageSquare, BookOpen, Layout, FileText, Code2, FileCode,
  Package, Loader, Download, Users, MessageCircleQuestion, Shield, AlertTriangle,
  Trash2, X, Upload, Maximize2, Eye,
} from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import ValidationPanel from '../../components/common/ValidationPanel'
import AnalysisPanel from '../../components/AnalysisPanel'
import ChangeIdChip from '../../components/ChangeIdChip'
import TranscriptsDownloadButton from '../../components/TranscriptsDownloadButton'
import { DocxDownloadButton, PptxDownloadButton, XlsxDownloadButton } from './BRD'
// Admin one-click cascade delete — shows current user's role to gate the button.
import { useAuth } from '../../hooks/useAuth'


// ── Admin: Delete change request modal ───────────────────────────────────────
//
// Two-step confirm: user must type the change-request title verbatim before
// the Delete button activates. Backend re-validates the title server-side
// so this is a UX guard not a security gate.
function DeleteChangeModal({ change, onClose, onDeleted }) {
  const [confirmText, setConfirmText] = useState('')
  const [deleting, setDeleting]       = useState(false)
  const [error, setError]             = useState(null)
  const expectedTitle = (change?.title || '').trim()
  const canDelete     = confirmText.trim() === expectedTitle && !deleting

  const handleDelete = async () => {
    if (!canDelete) return
    setDeleting(true); setError(null)
    try {
      const res = await changesApi.adminDelete(change.id, expectedTitle)
      onDeleted(res.data)
    } catch (err) {
      setError(err.response?.data?.detail || 'Delete failed')
      setDeleting(false)
    }
  }

  return (
    <div
      role="dialog" aria-modal="true"
      style={{
        position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.55)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        zIndex: 1000, padding: '24px',
      }}
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: 'var(--bg-base)', borderRadius: '10px',
          maxWidth: '560px', width: '100%', padding: '24px',
          border: '2px solid var(--danger)',
          boxShadow: '0 12px 32px rgba(0,0,0,0.35)',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '12px' }}>
          <AlertTriangle size={22} style={{ color: 'var(--danger)' }} />
          <h2 style={{ margin: 0, fontSize: '18px', fontWeight: 600, color: 'var(--danger)' }}>
            Delete change request — irreversible
          </h2>
          <button
            onClick={onClose} disabled={deleting}
            style={{ marginLeft: 'auto', background: 'transparent', border: 'none',
                     cursor: deleting ? 'not-allowed' : 'pointer', color: 'var(--text-muted)' }}
            aria-label="Close"
          >
            <X size={18} />
          </button>
        </div>

        <p style={{ margin: '0 0 12px', fontSize: '13px', color: 'var(--text-secondary)' }}>
          This permanently removes <strong>everything</strong> tied to this change request:
        </p>
        <ul style={{ margin: '0 0 16px', paddingLeft: '20px', fontSize: '12px',
                     color: 'var(--text-secondary)', lineHeight: '1.7' }}>
          <li>All conversations, BRDs, Tech Specs, XSDs, Canvas, Research, Product Kit docs.</li>
          <li>All clarifications, approvals, notifications, feedback.</li>
          <li>All Phase B code iterations, reviews, builds, deployments, test runs.</li>
          <li>All Phase C partner assignments, negotiation messages, certification runs.</li>
          <li>Document chunks indexed for RAG, Apache AGE graph nodes, Redis chunk buffers.</li>
          <li>On-disk artefacts (DOCX outputs, docgen pipeline state, diagram PNGs).</li>
        </ul>

        <div style={{ padding: '10px 14px', background: 'rgba(224,108,108,0.08)',
                      border: '1px solid rgba(224,108,108,0.25)', borderRadius: '6px',
                      marginBottom: '14px', fontSize: '12px', color: 'var(--text-secondary)' }}>
          Outbound A2A partner messages already sent are <strong>not recallable</strong> — only
          the local record is removed. There is no undo.
        </div>

        <p style={{ margin: '0 0 6px', fontSize: '12px', color: 'var(--text-muted)' }}>
          To confirm, type the change request title exactly:
          <br />
          <code style={{ display: 'inline-block', marginTop: '4px', padding: '2px 8px',
                         background: 'var(--bg-elevated)', borderRadius: '4px',
                         fontSize: '12px', color: 'var(--text-primary)' }}>
            {expectedTitle}
          </code>
        </p>
        <input
          autoFocus
          value={confirmText}
          onChange={(e) => setConfirmText(e.target.value)}
          disabled={deleting}
          placeholder="Type the title to confirm"
          style={{
            width: '100%', padding: '9px 12px', marginTop: '6px',
            background: 'var(--bg-input)', border: '1px solid var(--border)',
            borderRadius: '6px', color: 'var(--text-primary)', fontSize: '13px',
            outline: 'none',
          }}
        />

        {error && (
          <div style={{ marginTop: '12px', padding: '10px 12px',
                        background: 'rgba(224,108,108,0.10)',
                        border: '1px solid rgba(224,108,108,0.3)',
                        borderRadius: '6px', fontSize: '12px',
                        color: 'var(--danger)' }}>
            {error}
          </div>
        )}

        <div style={{ display: 'flex', gap: '10px', justifyContent: 'flex-end', marginTop: '18px' }}>
          <button
            onClick={onClose} disabled={deleting}
            style={{
              padding: '8px 16px', background: 'var(--bg-elevated)',
              border: '1px solid var(--border)', borderRadius: '6px',
              color: 'var(--text-secondary)', fontSize: '13px',
              cursor: deleting ? 'not-allowed' : 'pointer', opacity: deleting ? 0.5 : 1,
            }}
          >
            Cancel
          </button>
          <button
            onClick={handleDelete} disabled={!canDelete}
            style={{
              display: 'flex', alignItems: 'center', gap: '6px',
              padding: '8px 18px', background: canDelete ? 'var(--danger)' : '#888',
              border: 'none', borderRadius: '6px', color: 'white',
              fontSize: '13px', fontWeight: 600,
              cursor: canDelete ? 'pointer' : 'not-allowed', opacity: canDelete ? 1 : 0.6,
            }}
          >
            {deleting
              ? <Loader size={13} style={{ animation: 'spin 1s linear infinite' }} />
              : <Trash2 size={13} />}
            {deleting ? 'Deleting…' : 'Delete permanently'}
          </button>
        </div>
      </div>
    </div>
  )
}

// ── Stage definitions ─────────────────────────────────────────────────────────

import { STAGES, EXPANDABLE_STAGES } from './stages'

const STATUS_ORDER = STAGES.map(s => s.key)

// Accuracy S5 reorder: v2 changes run XSD before the Tech Spec. v1 (default) is
// unchanged. Returns the stage list in the order for this change's workflow_version.
function stagesFor(workflowVersion) {
  if ((workflowVersion || 2) < 2) return STAGES   // v2 (XSD→TSD) is the default; only explicit v1 keeps the legacy order
  const byKey = Object.fromEntries(STAGES.map(s => [s.key, s]))
  const order = ['prompt_enhancement', 'research', 'canvas', 'clarification',
                 'brd', 'xsd', 'tech_spec', 'product_kit']
  return order.map(k => byKey[k]).filter(Boolean)
}

function getStageState(stageKey, currentStatus, order = STATUS_ORDER) {
  if (currentStatus === 'completed') return 'done'
  const si = order.indexOf(stageKey)
  const ci = order.indexOf(currentStatus)
  if (si < ci)  return 'done'
  if (si === ci) return 'active'
  return 'pending'
}

// ── Phase B stage definitions ─────────────────────────────────────────────────

const PHASE_B_STAGES = [
  { key: 'code_change',  label: 'Code Generation', shortLabel: 'Code',    icon: Code2 },
  { key: 'code_review',  label: 'Code Review',     shortLabel: 'Review',  icon: FileText },
  { key: 'is_review',    label: 'IS Review',       shortLabel: 'IS',      icon: Shield },
  { key: 'git',          label: 'Git / MR',        shortLabel: 'Git',     icon: FileCode },
  { key: 'build',        label: 'Build',           shortLabel: 'Build',   icon: Package },
  { key: 'deploy',       label: 'Deploy',          shortLabel: 'Deploy',  icon: ArrowRight },
  { key: 'test_gen',     label: 'UAT Test Gen',    shortLabel: 'Test Gen', icon: FileText },
  { key: 'test_exec',    label: 'UAT Execute',     shortLabel: 'Execute', icon: CheckCircle },
  { key: 'triage',       label: 'UAT Triage',      shortLabel: 'Triage',  icon: Circle },
]

const PHASE_B_ORDER = PHASE_B_STAGES.map(s => s.key)

// Stages that actually render in the timeline. IS Review still runs on
// the backend (kept in PHASE_B_STAGES / PHASE_B_ORDER so the
// getPhaseBStageState math stays aligned with the PhaseBStep enum), but
// hide it from the Change Detail view to keep the audience focused on
// code → build → deploy → test.
const HIDDEN_PHASE_B_STAGES = new Set(['is_review'])
const VISIBLE_PHASE_B_STAGES = PHASE_B_STAGES.filter(s => !HIDDEN_PHASE_B_STAGES.has(s.key))

function getPhaseBStageState(stageKey, currentStep, runStatus) {
  if (runStatus === 'completed') return 'done'
  const si = PHASE_B_ORDER.indexOf(stageKey)
  const ci = PHASE_B_ORDER.indexOf(currentStep)
  if (si < ci)  return 'done'
  if (si === ci) return 'active'
  return 'pending'
}

// ── Phase B detail components ─────────────────────────────────────────────────

const PHASE_B_STEP_DESCRIPTIONS = {
  code_change:  'AI-generated Java/Spring Boot code from Tech Spec and BRD',
  code_review:  'Automated code quality review against SonarQube + PMD rules',
  is_review:    'Information-security review against OWASP Top 10 + the Authority standards',
  git:          'Feature branch creation, file commit, and Merge Request on GitLab',
  build:        'CI build pipeline — compile, package, and quality checks',
  deploy:       'Deployment to UAT environment with health check verification',
  test_gen:     'AI-generated UAT test cases from BRD and Tech Spec',
  test_exec:    'Automated execution of UAT test suite against deployment',
  triage:       'AI triage of test failures — code bug, test issue, or env issue',
}

function CodeGenDetail({ changeId }) {
  const { data, isLoading } = useQuery({
    queryKey: ['phase-b-iterations', changeId],
    queryFn: () => phaseBApi.listIterations(changeId).then(r => r.data),
  })
  const [expandedIter, setExpandedIter] = useState(null)

  if (isLoading) return (
    <div style={{ padding: '16px 20px', fontSize: '12px', color: 'var(--text-muted)' }}>Loading…</div>
  )

  const iterations = data || []
  return (
    <div style={{ padding: '16px 20px' }}>
      <p style={{ margin: '0 0 12px', fontSize: '12px', color: 'var(--text-muted)' }}>
        {iterations.length} iteration{iterations.length !== 1 ? 's' : ''} generated
      </p>
      <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
        {iterations.map(it => {
          const isOpen = expandedIter === it.iteration_number
          return (
            <div key={it.iteration_number} style={{
              borderRadius: '6px',
              background: 'var(--bg-elevated)', border: '1px solid var(--border-subtle)',
              overflow: 'hidden',
            }}>
              <div
                onClick={() => setExpandedIter(isOpen ? null : it.iteration_number)}
                style={{
                  padding: '10px 14px', display: 'flex', alignItems: 'center', gap: '10px',
                  cursor: 'pointer', transition: 'background 0.15s',
                }}
                onMouseEnter={e => e.currentTarget.style.background = 'var(--bg-card)'}
                onMouseLeave={e => e.currentTarget.style.background = 'transparent'}
              >
                <span style={{
                  padding: '2px 8px', borderRadius: '4px', fontSize: '10px', fontWeight: '600',
                  background: 'rgba(218,119,86,0.12)', color: 'var(--accent)',
                  border: '1px solid rgba(218,119,86,0.25)',
                }}>
                  v{it.iteration_number}
                </span>
                <span style={{ fontSize: '12px', color: 'var(--text-secondary)', flex: 1 }}>
                  {it.trigger === 'initial' ? 'Initial generation' : it.trigger === 'code_review_feedback' ? 'Code review fix' : it.trigger === 'is_review_feedback' ? 'Security fix' : 'Feedback revision'}
                </span>
                <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>
                  {it.files_count} file{it.files_count !== 1 ? 's' : ''}
                </span>
                {it.approved && <CheckCircle size={13} style={{ color: 'var(--success)' }} />}
                {isOpen ? <ChevronUp size={13} style={{ color: 'var(--text-muted)' }} /> : <ChevronDown size={13} style={{ color: 'var(--text-muted)' }} />}
              </div>
              {isOpen && <IterationFileList changeId={changeId} iteration={it.iteration_number} />}
            </div>
          )
        })}
      </div>
    </div>
  )
}

// Per-iteration file drill-down
function IterationFileList({ changeId, iteration }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ['phase-b-iteration', changeId, iteration],
    queryFn: () => phaseBApi.getIteration(changeId, iteration).then(r => r.data),
  })
  const [openFile, setOpenFile] = useState(null)

  if (isLoading) return (
    <div style={{ padding: '10px 14px', fontSize: '11px', color: 'var(--text-muted)' }}>Loading files…</div>
  )
  if (error) return (
    <div style={{ padding: '10px 14px', fontSize: '11px', color: 'var(--danger)' }}>
      Failed to load iteration: {error.message}
    </div>
  )

  // Backend returns `files_changed` (list of {path, content})
  const files = data?.files_changed || []
  const feedback = data?.user_feedback
  const fullOutput = data?.generated_output

  if (!files.length && !fullOutput) return (
    <div style={{ padding: '10px 14px', fontSize: '11px', color: 'var(--text-muted)' }}>
      No file data stored for this iteration.
    </div>
  )

  return (
    <div style={{ padding: '10px 14px', borderTop: '1px solid var(--border-subtle)', background: 'var(--bg-base)' }}>
      {feedback && (
        <div style={{
          marginBottom: '10px', padding: '8px 10px',
          background: 'var(--bg-card)', border: '1px solid var(--border-subtle)',
          borderRadius: '4px', fontSize: '11px', color: 'var(--text-secondary)',
        }}>
          <span style={{ fontWeight: '600', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.05em', fontSize: '10px' }}>
            Trigger feedback:
          </span>
          <p style={{ margin: '4px 0 0', lineHeight: '1.5' }}>{feedback}</p>
        </div>
      )}

      {files.length > 0 ? files.map((f, i) => {
        const isOpen = openFile === i
        return (
          <div key={i} style={{ borderBottom: i < files.length - 1 ? '1px solid var(--border-subtle)' : 'none', padding: '6px 0' }}>
            <div
              onClick={() => setOpenFile(isOpen ? null : i)}
              style={{
                display: 'flex', alignItems: 'center', gap: '8px',
                cursor: 'pointer', fontSize: '11px',
              }}>
              <FileCode size={12} style={{ color: 'var(--text-muted)', flexShrink: 0 }} />
              <span style={{ color: 'var(--text-secondary)', fontFamily: 'var(--font-mono, monospace)', flex: 1, wordBreak: 'break-all' }}>
                {f.path || '(unnamed file)'}
              </span>
              {f.content && (
                <span style={{ fontSize: '10px', color: 'var(--text-muted)' }}>
                  {f.content.split('\n').length} lines
                </span>
              )}
              {isOpen ? <ChevronUp size={11} style={{ color: 'var(--text-muted)' }} /> : <ChevronDown size={11} style={{ color: 'var(--text-muted)' }} />}
            </div>
            {isOpen && f.content && (
              <pre style={{
                marginTop: '6px', marginBottom: 0,
                padding: '8px 10px', fontSize: '10px', lineHeight: '1.5',
                background: 'var(--bg-card)', border: '1px solid var(--border-subtle)',
                borderRadius: '4px', overflowX: 'auto',
                whiteSpace: 'pre', fontFamily: 'var(--font-mono, monospace)',
                color: 'var(--text-secondary)', maxHeight: '500px',
              }}>{f.content}</pre>
            )}
          </div>
        )
      }) : fullOutput ? (
        // Fall back: show the raw generated output if parsing didn't yield files
        <details style={{ fontSize: '11px', color: 'var(--text-muted)' }}>
          <summary style={{ cursor: 'pointer', padding: '4px 0' }}>Raw generated output ({fullOutput.length.toLocaleString()} chars)</summary>
          <pre style={{
            marginTop: '6px', padding: '8px 10px', fontSize: '10px', lineHeight: '1.5',
            background: 'var(--bg-card)', border: '1px solid var(--border-subtle)',
            borderRadius: '4px', overflowX: 'auto',
            whiteSpace: 'pre-wrap', wordBreak: 'break-all',
            color: 'var(--text-secondary)', maxHeight: '500px',
          }}>{fullOutput}</pre>
        </details>
      ) : null}
    </div>
  )
}

// IS Review expanded detail — mirrors CodeReviewDetail structure
function ISReviewDetail({ changeId }) {
  const { data, isLoading } = useQuery({
    queryKey: ['phase-b-is-review-detail', changeId],
    queryFn: () => phaseBApi.getISReview(changeId).then(r => r.data),
    retry: false,
  })

  if (isLoading) return (
    <div style={{ padding: '16px 20px', fontSize: '12px', color: 'var(--text-muted)' }}>Loading…</div>
  )
  if (!data) return (
    <div style={{ padding: '16px 20px', fontSize: '12px', color: 'var(--text-muted)' }}>No IS review data.</div>
  )

  const isClean = data.status === 'clean'
  const findings = data.findings || data.issues || []
  const stats = data.stats || {}

  return (
    <div style={{ padding: '16px 20px' }}>
      {(stats.files_reviewed !== undefined || stats.findings_count !== undefined) && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: '8px', marginBottom: '12px' }}>
          <div style={{ padding: '8px 12px', borderRadius: '6px', background: 'var(--bg-elevated)', border: '1px solid var(--border-subtle)', textAlign: 'center' }}>
            <p style={{ margin: '0 0 1px', fontSize: '16px', fontWeight: '700', color: 'var(--text-primary)' }}>{stats.files_reviewed || 0}</p>
            <p style={{ margin: 0, fontSize: '9px', color: 'var(--text-muted)', textTransform: 'uppercase' }}>Files</p>
          </div>
          {['high', 'medium', 'low'].map(sev => {
            const c = findings.filter(f => (f.severity || '').toLowerCase() === sev).length
            return (
              <div key={sev} style={{ padding: '8px 12px', borderRadius: '6px', background: 'var(--bg-elevated)', border: '1px solid var(--border-subtle)', textAlign: 'center' }}>
                <p style={{ margin: '0 0 1px', fontSize: '16px', fontWeight: '700',
                  color: sev === 'high' ? 'var(--danger)' : sev === 'medium' ? 'var(--accent)' : 'var(--success)' }}>{c}</p>
                <p style={{ margin: 0, fontSize: '9px', color: 'var(--text-muted)', textTransform: 'uppercase' }}>{sev}</p>
              </div>
            )
          })}
        </div>
      )}

      <div style={{
        padding: '10px 14px', borderRadius: '6px', marginBottom: findings.length > 0 ? '12px' : 0,
        background: isClean ? 'rgba(76,175,125,0.06)' : 'rgba(224,108,108,0.06)',
        border: `1px solid ${isClean ? 'rgba(76,175,125,0.2)' : 'rgba(224,108,108,0.2)'}`,
        display: 'flex', alignItems: 'center', gap: '8px',
      }}>
        <Shield size={14} style={{ color: isClean ? 'var(--success)' : 'var(--danger)' }} />
        <span style={{ fontSize: '12px', color: isClean ? 'var(--success)' : 'var(--danger)', fontWeight: '600' }}>
          {isClean ? 'No security issues found' : `${findings.length} finding${findings.length !== 1 ? 's' : ''}`}
        </span>
      </div>

      {findings.length > 0 && (
        <div style={{ border: '1px solid var(--border)', borderRadius: '6px', overflow: 'auto', fontSize: '11px' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ background: 'var(--bg-elevated)', borderBottom: '1px solid var(--border)' }}>
                <th style={{ padding: '6px 10px', textAlign: 'left', fontWeight: '600', color: 'var(--text-muted)', textTransform: 'uppercase', fontSize: '9px' }}>Category</th>
                <th style={{ padding: '6px 10px', textAlign: 'left', fontWeight: '600', color: 'var(--text-muted)', textTransform: 'uppercase', fontSize: '9px' }}>Severity</th>
                <th style={{ padding: '6px 10px', textAlign: 'left', fontWeight: '600', color: 'var(--text-muted)', textTransform: 'uppercase', fontSize: '9px' }}>File</th>
                <th style={{ padding: '6px 10px', textAlign: 'left', fontWeight: '600', color: 'var(--text-muted)', textTransform: 'uppercase', fontSize: '9px' }}>Finding</th>
              </tr>
            </thead>
            <tbody>
              {findings.map((f, i) => (
                <tr key={i} style={{ borderBottom: i < findings.length - 1 ? '1px solid var(--border-subtle)' : 'none' }}>
                  <td style={{ padding: '6px 10px', color: 'var(--text-secondary)', fontSize: '10px', fontWeight: '600' }}>{f.category || f.owasp_category || '—'}</td>
                  <td style={{ padding: '6px 10px', color: (f.severity || '').toLowerCase() === 'high' ? 'var(--danger)' : 'var(--accent)', fontWeight: '600', textTransform: 'uppercase' }}>{f.severity}</td>
                  <td style={{ padding: '6px 10px', color: 'var(--text-secondary)', fontFamily: 'monospace' }}>{f.file}{f.line ? `:${f.line}` : ''}</td>
                  <td style={{ padding: '6px 10px', color: 'var(--text-primary)' }}>{f.message || f.description}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

// One collapsible terminal-styled log block. Used by BuildDetail to show
// the build / deploy / startup sections side-by-side.
function LogBlock({ title, body, defaultOpen = false }) {
  const [open, setOpen] = useState(defaultOpen)
  if (!body) return null
  const lineCount = body.split('\n').length
  return (
    <div style={{ marginTop: '10px' }}>
      <button
        onClick={() => setOpen(s => !s)}
        style={{
          padding: '5px 10px', fontSize: '11px',
          background: 'var(--bg-elevated)', color: 'var(--text-secondary)',
          border: '1px solid var(--border)', borderRadius: '4px',
          cursor: 'pointer', display: 'flex', alignItems: 'center', gap: '5px',
        }}>
        {open ? <ChevronUp size={11} /> : <ChevronDown size={11} />}
        {open ? 'Hide' : 'Show'} {title} ({lineCount} lines, {body.length.toLocaleString()} chars)
      </button>
      {open && (
        <pre style={{
          margin: '8px 0 0', padding: '12px 14px',
          background: '#0d1117', color: '#c9d1d9',
          border: '1px solid #30363d', borderRadius: '4px',
          overflowX: 'auto', maxHeight: '500px', overflowY: 'auto',
          fontSize: '10.5px', lineHeight: '1.55',
          fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
          whiteSpace: 'pre',
        }}>{body}</pre>
      )}
    </div>
  )
}

// Build detail — surfaces the BuildRun row: status, branches, duration,
// deployed artifacts, services brought up, and the three log bodies
// (build / deploy / startup) as separate collapsibles.
function BuildDetail({ changeId }) {
  const { data, isLoading } = useQuery({
    queryKey: ['phase-b-build-detail', changeId],
    queryFn: () => phaseBApi.getBuild(changeId).then(r => r.data),
    retry: false,
  })

  if (isLoading) return (
    <div style={{ padding: '16px 20px', fontSize: '12px', color: 'var(--text-muted)' }}>Loading…</div>
  )
  if (!data) return (
    <div style={{ padding: '16px 20px', fontSize: '12px', color: 'var(--text-muted)' }}>No build data yet.</div>
  )

  const isDone = data.status === 'success'
  const isFailed = data.status === 'failure'
  const duration = data.triggered_at && data.completed_at
    ? Math.round((new Date(data.completed_at) - new Date(data.triggered_at)) / 1000)
    : null

  return (
    <div style={{ padding: '16px 20px' }}>
      <div style={{
        padding: '10px 14px', borderRadius: '6px', marginBottom: '12px',
        background: isDone ? 'rgba(76,175,125,0.06)' : isFailed ? 'rgba(224,108,108,0.06)' : 'var(--bg-elevated)',
        border: `1px solid ${isDone ? 'rgba(76,175,125,0.2)' : isFailed ? 'rgba(224,108,108,0.2)' : 'var(--border-subtle)'}`,
        display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap',
      }}>
        {isDone ? <CheckCircle size={14} style={{ color: 'var(--success)' }} />
          : isFailed ? <AlertTriangle size={14} style={{ color: 'var(--danger)' }} />
          : <Clock size={14} style={{ color: 'var(--text-muted)' }} />}
        <span style={{ fontSize: '12px', fontWeight: '600',
          color: isDone ? 'var(--success)' : isFailed ? 'var(--danger)' : 'var(--text-secondary)' }}>
          {isDone ? 'Build + Deploy Successful' : isFailed ? 'Build + Deploy Failed' : (data.status || 'Pending')}
        </span>
        {duration !== null && (
          <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>{duration}s</span>
        )}
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))',
                    gap: '10px', fontSize: '12px', marginBottom: '12px' }}>
        {data.host && (
          <div>
            <span style={{ color: 'var(--text-muted)', display: 'block', marginBottom: '2px', fontSize: '10px' }}>Host</span>
            <span style={{ color: 'var(--text-primary)', fontFamily: 'monospace', fontSize: '11px' }}>{data.host}</span>
          </div>
        )}
        {data.core_branch && (
          <div>
            <span style={{ color: 'var(--text-muted)', display: 'block', marginBottom: '2px', fontSize: '10px' }}>network-core branch</span>
            <span style={{ color: 'var(--text-primary)', fontFamily: 'monospace', fontSize: '11px' }}>{data.core_branch}</span>
          </div>
        )}
        {data.app_branch && (
          <div>
            <span style={{ color: 'var(--text-muted)', display: 'block', marginBottom: '2px', fontSize: '10px' }}>network-2.0 branch</span>
            <span style={{ color: 'var(--text-primary)', fontFamily: 'monospace', fontSize: '11px' }}>{data.app_branch}</span>
          </div>
        )}
        {data.triggered_at && (
          <div>
            <span style={{ color: 'var(--text-muted)', display: 'block', marginBottom: '2px', fontSize: '10px' }}>Triggered</span>
            <span style={{ color: 'var(--text-primary)', fontSize: '11px' }}>{new Date(data.triggered_at).toLocaleString()}</span>
          </div>
        )}
        {data.completed_at && (
          <div>
            <span style={{ color: 'var(--text-muted)', display: 'block', marginBottom: '2px', fontSize: '10px' }}>Completed</span>
            <span style={{ color: 'var(--text-primary)', fontSize: '11px' }}>{new Date(data.completed_at).toLocaleString()}</span>
          </div>
        )}
      </div>

      <LogBlock title="Build log" body={data.build_log} defaultOpen={isFailed} />
    </div>
  )
}


// Deploy detail — owns the deploy + startup logs, deployed artifact and
// service tables. Pulls from the same BuildRun row as BuildDetail since
// the host script writes all three log sections together; the UI splits
// them onto the corresponding timeline steps.
function DeployDetail({ changeId }) {
  const { data, isLoading } = useQuery({
    queryKey: ['phase-b-build-detail', changeId],
    queryFn: () => phaseBApi.getBuild(changeId).then(r => r.data),
    retry: false,
  })

  if (isLoading) return (
    <div style={{ padding: '16px 20px', fontSize: '12px', color: 'var(--text-muted)' }}>Loading…</div>
  )
  if (!data) return (
    <div style={{ padding: '16px 20px', fontSize: '12px', color: 'var(--text-muted)' }}>No deploy data yet.</div>
  )

  const isFailed = data.status === 'failure'
  const hasArtifacts = data.deployed_artifacts?.length > 0
  const hasServices  = data.services_started?.length > 0
  const hasLogs      = data.deploy_log || data.startup_log

  if (!hasArtifacts && !hasServices && !hasLogs) return (
    <div style={{ padding: '16px 20px', fontSize: '12px', color: 'var(--text-muted)' }}>
      No deploy artefacts captured for this build.
    </div>
  )

  return (
    <div style={{ padding: '16px 20px' }}>
      {hasArtifacts && (
        <div style={{ marginBottom: '10px' }}>
          <div style={{ fontSize: '11px', fontWeight: '600', color: 'var(--text-muted)', marginBottom: '4px' }}>
            Artifacts
          </div>
          <div style={{ border: '1px solid var(--border)', borderRadius: '4px', overflow: 'hidden' }}>
            {data.deployed_artifacts.map((a, i) => (
              <div key={i} style={{
                display: 'grid', gridTemplateColumns: '1fr 1fr',
                fontSize: '11px', padding: '5px 10px', fontFamily: 'monospace',
                borderTop: i ? '1px solid var(--border-subtle)' : 'none',
                background: i % 2 ? 'var(--bg-base)' : 'transparent',
              }}>
                <span style={{ color: 'var(--text-primary)' }}>{a.path}</span>
                <span style={{ color: 'var(--text-muted)' }}>→ {a.dest}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {hasServices && (
        <div style={{ marginBottom: '10px' }}>
          <div style={{ fontSize: '11px', fontWeight: '600', color: 'var(--text-muted)', marginBottom: '4px' }}>
            Services Up
          </div>
          <div style={{ border: '1px solid var(--border)', borderRadius: '4px', overflow: 'hidden' }}>
            {data.services_started.map((s, i) => (
              <div key={i} style={{
                display: 'grid', gridTemplateColumns: '1fr 100px',
                fontSize: '11px', padding: '5px 10px', fontFamily: 'monospace',
                borderTop: i ? '1px solid var(--border-subtle)' : 'none',
                background: i % 2 ? 'var(--bg-base)' : 'transparent',
              }}>
                <span style={{ color: 'var(--text-primary)' }}>{s.name}</span>
                <span style={{ color: 'var(--text-muted)' }}>pid {s.pid}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      <LogBlock title="Deploy log"  body={data.deploy_log}  defaultOpen={isFailed} />
      <LogBlock title="Startup log" body={data.startup_log} />
    </div>
  )
}


// UAT Test Gen detail — table of generated UAT cases, expandable rows.
// Script-based runs (the combined gen+exec step) store no per-case rows —
// their cases live inside the test script and the evidence is the run log
// on the UAT Execute stage, so say that instead of a misleading empty state.
function TestGenDetail({ changeId }) {
  const { data, isLoading } = useQuery({
    queryKey: ['phase-b-test-cases', changeId],
    queryFn: () => phaseBApi.listTestCases(changeId).then(r => r.data),
    retry: false,
  })
  const { data: runData } = useQuery({
    queryKey: ['phase-b-test-run-latest', changeId],
    queryFn: () => phaseBApi.getLatestTestRun(changeId).then(r => r.data),
    retry: false,
  })
  const [expandedId, setExpandedId] = useState(null)

  if (isLoading) return (
    <div style={{ padding: '16px 20px', fontSize: '12px', color: 'var(--text-muted)' }}>Loading…</div>
  )
  const cases = data?.test_cases || []
  if (cases.length === 0) return (
    <div style={{ padding: '16px 20px', fontSize: '12px', color: 'var(--text-muted)' }}>
      {runData?.test_run?.script_path
        ? 'Script-based UAT — the test script defines and executes its own cases; see the UAT Execute stage for the run log.'
        : 'No UAT test cases generated yet.'}
    </div>
  )

  return (
    <div style={{ padding: '16px 20px' }}>
      <div style={{
        padding: '8px 12px', marginBottom: '10px',
        background: 'rgba(76,175,125,0.06)', border: '1px solid rgba(76,175,125,0.2)',
        borderRadius: '6px', fontSize: '12px', color: 'var(--text-secondary)',
      }}>
        <strong style={{ color: 'var(--success)' }}>{cases.length} test cases</strong>
        <span style={{ marginLeft: '8px', color: 'var(--text-muted)' }}>
          suite version {data.suite_version}
        </span>
      </div>

      <div style={{ border: '1px solid var(--border)', borderRadius: '4px', overflow: 'hidden' }}>
        <div style={{
          display: 'grid', gridTemplateColumns: '70px 65px 90px 1.5fr 70px 30px',
          background: 'var(--bg-elevated)', padding: '7px 12px',
          fontSize: '10px', fontWeight: '700', color: 'var(--text-muted)',
          textTransform: 'uppercase', letterSpacing: '0.5px',
          borderBottom: '1px solid var(--border)',
        }}>
          <span>ID</span><span>Method</span><span>Category</span><span>Endpoint</span><span>Expected</span><span></span>
        </div>
        {cases.map(c => {
          const isOpen = expandedId === c.id
          return (
            <div key={c.id} style={{ borderTop: '1px solid var(--border-subtle)' }}>
              <div
                onClick={() => setExpandedId(isOpen ? null : c.id)}
                style={{
                  display: 'grid', gridTemplateColumns: '70px 65px 90px 1.5fr 70px 30px',
                  padding: '7px 12px', fontSize: '11.5px', cursor: 'pointer',
                  background: isOpen ? 'var(--bg-base)' : 'transparent',
                  alignItems: 'center', gap: '4px',
                }}>
                <span style={{ fontFamily: 'monospace', fontWeight: '600' }}>{c.test_id}</span>
                <span style={{
                  display: 'inline-block', padding: '1px 5px', borderRadius: '3px',
                  fontFamily: 'monospace', fontSize: '10px', fontWeight: '700',
                  background: c.http_method === 'GET' ? 'rgba(91,141,239,0.15)' : 'rgba(218,119,86,0.15)',
                  color:      c.http_method === 'GET' ? 'var(--accent)' : '#c97a3a',
                  textAlign: 'center', justifySelf: 'start',
                }}>{c.http_method}</span>
                <span style={{ fontSize: '10px', color: 'var(--text-muted)' }}>{c.category}</span>
                <span style={{ fontFamily: 'monospace', fontSize: '10.5px', color: 'var(--text-secondary)',
                               overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {c.endpoint}
                </span>
                <span style={{ fontFamily: 'monospace', color: 'var(--text-muted)' }}>{c.expected_status}</span>
                <span style={{ color: 'var(--text-muted)', textAlign: 'right' }}>
                  {isOpen ? <ChevronUp size={11} /> : <ChevronDown size={11} />}
                </span>
              </div>
              {isOpen && (
                <div style={{ padding: '10px 14px 12px', background: 'var(--bg-base)',
                              borderTop: '1px solid var(--border-subtle)',
                              fontSize: '11px', color: 'var(--text-secondary)', lineHeight: '1.55' }}>
                  <div style={{ fontWeight: '600', color: 'var(--text-primary)', marginBottom: '4px' }}>{c.title}</div>
                  <div style={{ marginBottom: '6px' }}>{c.description}</div>
                  {c.preconditions && (
                    <div style={{ marginBottom: '6px' }}><strong>Preconditions:</strong> {c.preconditions}</div>
                  )}
                  {c.request_payload && (
                    <div style={{ marginBottom: '6px' }}>
                      <strong>Request body:</strong>
                      <pre style={{ background: '#0d1117', color: '#c9d1d9', padding: '6px 10px',
                                    borderRadius: '4px', fontSize: '10px', margin: '4px 0',
                                    overflowX: 'auto', whiteSpace: 'pre' }}>
                        {JSON.stringify(c.request_payload, null, 2)}
                      </pre>
                    </div>
                  )}
                  {c.expected_response && (
                    <div style={{ marginBottom: '6px' }}>
                      <strong>Expected response:</strong>
                      <pre style={{ background: '#0d1117', color: '#7ee787', padding: '6px 10px',
                                    borderRadius: '4px', fontSize: '10px', margin: '4px 0',
                                    overflowX: 'auto', whiteSpace: 'pre' }}>
                        {JSON.stringify(c.expected_response, null, 2)}
                      </pre>
                    </div>
                  )}
                  <div style={{ fontSize: '10.5px', color: 'var(--text-muted)' }}>
                    <strong>Pass criteria:</strong> {c.pass_criteria}
                  </div>
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}


// UAT Execute detail — summary + per-test PASS/FAIL log.
function TestExecDetail({ changeId }) {
  const { data: runData, isLoading } = useQuery({
    queryKey: ['phase-b-test-run-latest', changeId],
    queryFn: () => phaseBApi.getLatestTestRun(changeId).then(r => r.data),
    retry: false,
  })
  const { data: casesData } = useQuery({
    queryKey: ['phase-b-test-cases', changeId],
    queryFn: () => phaseBApi.listTestCases(changeId).then(r => r.data),
    retry: false,
  })

  if (isLoading) return (
    <div style={{ padding: '16px 20px', fontSize: '12px', color: 'var(--text-muted)' }}>Loading…</div>
  )
  if (!runData?.test_run) return (
    <div style={{ padding: '16px 20px', fontSize: '12px', color: 'var(--text-muted)' }}>
      No UAT test run yet.
    </div>
  )

  const run = runData.test_run
  const results = runData.results || []
  const casesById = Object.fromEntries((casesData?.test_cases || []).map(c => [c.id, c]))
  // Script-based runs carry their evidence as the script's log; legacy mock
  // runs carry per-case result rows. Render whichever this run has.
  const logLines = (run.log || '').split('\n')
  const logColor = (line) => {
    if (/^\s*PASS\b/.test(line)) return '#7ee787'
    if (/^\s*FAIL\b/.test(line)) return '#ff7b72'
    if (/^\s*SKIP\b/.test(line)) return '#ffa657'
    if (/^\s*TESTS:/.test(line)) return '#79c0ff'
    return '#c9d1d9'
  }

  return (
    <div style={{ padding: '16px 20px' }}>
      <div style={{
        display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(100px, 1fr))', gap: '10px',
        marginBottom: '12px', padding: '12px 14px',
        background: 'rgba(76,175,125,0.06)', border: '1px solid rgba(76,175,125,0.2)',
        borderRadius: '6px', fontSize: '12px',
      }}>
        <div><span style={{ color: 'var(--text-muted)', display: 'block', fontSize: '10px' }}>Total</span>
             <span style={{ fontWeight: '600' }}>{run.total}</span></div>
        <div><span style={{ color: 'var(--text-muted)', display: 'block', fontSize: '10px' }}>Passed</span>
             <span style={{ color: 'var(--success)', fontWeight: '600' }}>{run.passed}</span></div>
        <div><span style={{ color: 'var(--text-muted)', display: 'block', fontSize: '10px' }}>Failed</span>
             <span style={{ color: run.failed ? 'var(--danger)' : 'var(--text-secondary)', fontWeight: '600' }}>{run.failed}</span></div>
        <div><span style={{ color: 'var(--text-muted)', display: 'block', fontSize: '10px' }}>Skipped</span>
             <span style={{ fontWeight: '600' }}>{run.skipped}</span></div>
        {run.base_url && (
          <div><span style={{ color: 'var(--text-muted)', display: 'block', fontSize: '10px' }}>Base URL</span>
               <span style={{ fontFamily: 'monospace', fontSize: '10.5px' }}>{run.base_url}</span></div>
        )}
        {run.script_path && (
          <div style={{ gridColumn: 'span 2' }}>
            <span style={{ color: 'var(--text-muted)', display: 'block', fontSize: '10px' }}>Script</span>
            <span style={{ fontFamily: 'monospace', fontSize: '10.5px' }}>{run.script_path}</span>
          </div>
        )}
      </div>

      <pre style={{
        background: '#0d1117', color: '#c9d1d9',
        padding: '12px 14px', borderRadius: '4px',
        fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
        fontSize: '10.5px', lineHeight: '1.6',
        maxHeight: '440px', overflowY: 'auto',
        whiteSpace: 'pre-wrap', wordBreak: 'break-word', margin: 0,
        border: '1px solid #30363d',
      }}>
        {run.log
          ? logLines.map((line, i) => (
              <div key={i} style={{ color: logColor(line) }}>{line || ' '}</div>
            ))
          : results.map(r => {
              const c = casesById[r.test_case_id]
              const pass = r.status === 'pass'
              const id = c?.test_id || r.test_case_id.slice(0, 8)
              const ep = c?.endpoint || ''
              const m  = (c?.http_method || '').padEnd(4)
              return (
                <div key={r.id} style={{ color: pass ? '#7ee787' : '#ff7b72' }}>
                  {pass ? '✓ PASS' : '✗ FAIL'}  {id}  {m} {ep}  →  {r.actual_status} ({r.latency_ms}ms)
                </div>
              )
            })}
        {!run.log && (
          <>
            <div style={{ color: '#79c0ff', marginTop: '8px' }}>──────────────────────────────────────────────────</div>
            <div style={{ color: '#7ee787', fontWeight: 'bold' }}>
              {run.passed}/{run.total} tests passed
              {run.failed ? ` · ${run.failed} failed` : ''}
            </div>
          </>
        )}
      </pre>
    </div>
  )
}

function CodeReviewDetail({ changeId }) {
  const { data, isLoading } = useQuery({
    queryKey: ['phase-b-code-review-detail', changeId],
    queryFn: () => phaseBApi.getCodeReview(changeId).then(r => r.data),
    retry: false,
  })

  if (isLoading) return (
    <div style={{ padding: '16px 20px', fontSize: '12px', color: 'var(--text-muted)' }}>Loading…</div>
  )
  if (!data) return (
    <div style={{ padding: '16px 20px', fontSize: '12px', color: 'var(--text-muted)' }}>No review data.</div>
  )

  const isClean = data.status === 'clean'
  const issues = data.issues || []
  const stats = data.stats || {}
  const rulesChecked = data.rules_checked || {}

  return (
    <div style={{ padding: '16px 20px' }}>
      {/* Stats cards */}
      {(stats.files_reviewed !== undefined || rulesChecked.sonarqube || rulesChecked.pmd) && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: '8px', marginBottom: '12px' }}>
          <div style={{ padding: '8px 12px', borderRadius: '6px', background: 'var(--bg-elevated)', border: '1px solid var(--border-subtle)', textAlign: 'center' }}>
            <p style={{ margin: '0 0 1px', fontSize: '16px', fontWeight: '700', color: 'var(--text-primary)' }}>{stats.files_reviewed || 0}</p>
            <p style={{ margin: 0, fontSize: '9px', color: 'var(--text-muted)', textTransform: 'uppercase' }}>Files</p>
          </div>
          <div style={{ padding: '8px 12px', borderRadius: '6px', background: 'var(--bg-elevated)', border: '1px solid var(--border-subtle)', textAlign: 'center' }}>
            <p style={{ margin: '0 0 1px', fontSize: '16px', fontWeight: '700', color: (stats.sonarqube_issues || 0) > 0 ? 'var(--danger)' : 'var(--success)' }}>{stats.sonarqube_issues || 0}</p>
            <p style={{ margin: 0, fontSize: '9px', color: 'var(--text-muted)', textTransform: 'uppercase' }}>SonarQube</p>
          </div>
          <div style={{ padding: '8px 12px', borderRadius: '6px', background: 'var(--bg-elevated)', border: '1px solid var(--border-subtle)', textAlign: 'center' }}>
            <p style={{ margin: '0 0 1px', fontSize: '16px', fontWeight: '700', color: (stats.pmd_issues || 0) > 0 ? 'var(--danger)' : 'var(--success)' }}>{stats.pmd_issues || 0}</p>
            <p style={{ margin: 0, fontSize: '9px', color: 'var(--text-muted)', textTransform: 'uppercase' }}>PMD</p>
          </div>
          <div style={{ padding: '8px 12px', borderRadius: '6px', background: 'var(--bg-elevated)', border: '1px solid var(--border-subtle)', textAlign: 'center' }}>
            <p style={{ margin: '0 0 1px', fontSize: '16px', fontWeight: '700', color: 'var(--text-primary)' }}>{(rulesChecked.sonarqube?.total || 0) + (rulesChecked.pmd?.total || 0)}</p>
            <p style={{ margin: 0, fontSize: '9px', color: 'var(--text-muted)', textTransform: 'uppercase' }}>Rules</p>
          </div>
        </div>
      )}

      {/* Status badge */}
      <div style={{
        padding: '10px 14px', borderRadius: '6px', marginBottom: issues.length > 0 ? '12px' : 0,
        background: isClean ? 'rgba(76,175,125,0.06)' : 'rgba(224,108,108,0.06)',
        border: `1px solid ${isClean ? 'rgba(76,175,125,0.2)' : 'rgba(224,108,108,0.2)'}`,
        display: 'flex', alignItems: 'center', gap: '8px',
      }}>
        <CheckCircle size={14} style={{ color: isClean ? 'var(--success)' : 'var(--danger)' }} />
        <span style={{ fontSize: '12px', color: isClean ? 'var(--success)' : 'var(--danger)', fontWeight: '600' }}>
          {isClean ? 'All files passed code review' : `${issues.length} issue${issues.length !== 1 ? 's' : ''} found`}
        </span>
      </div>

      {/* Issues table */}
      {issues.length > 0 && (
        <div style={{ border: '1px solid var(--border)', borderRadius: '6px', overflow: 'auto', fontSize: '11px' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ background: 'var(--bg-elevated)', borderBottom: '1px solid var(--border)' }}>
                <th style={{ padding: '6px 10px', textAlign: 'left', fontWeight: '600', color: 'var(--text-muted)', textTransform: 'uppercase', fontSize: '9px' }}>Ruleset</th>
                <th style={{ padding: '6px 10px', textAlign: 'left', fontWeight: '600', color: 'var(--text-muted)', textTransform: 'uppercase', fontSize: '9px' }}>Severity</th>
                <th style={{ padding: '6px 10px', textAlign: 'left', fontWeight: '600', color: 'var(--text-muted)', textTransform: 'uppercase', fontSize: '9px' }}>File</th>
                <th style={{ padding: '6px 10px', textAlign: 'left', fontWeight: '600', color: 'var(--text-muted)', textTransform: 'uppercase', fontSize: '9px' }}>Issue</th>
              </tr>
            </thead>
            <tbody>
              {issues.map((iss, i) => (
                <tr key={i} style={{ borderBottom: i < issues.length - 1 ? '1px solid var(--border-subtle)' : 'none' }}>
                  <td style={{ padding: '6px 10px', color: 'var(--text-secondary)', fontSize: '10px', fontWeight: '600', textTransform: 'uppercase' }}>{iss.ruleset || '—'}</td>
                  <td style={{ padding: '6px 10px', color: 'var(--accent)', fontWeight: '600', textTransform: 'uppercase' }}>{iss.severity}</td>
                  <td style={{ padding: '6px 10px', color: 'var(--text-secondary)', fontFamily: 'monospace' }}>{iss.file}{iss.line ? `:${iss.line}` : ''}</td>
                  <td style={{ padding: '6px 10px', color: 'var(--text-primary)' }}>{iss.message}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function GitDetail({ changeId }) {
  const { data, isLoading } = useQuery({
    queryKey: ['phase-b-git-detail', changeId],
    queryFn: () => phaseBApi.getGitEvent(changeId).then(r => r.data),
    retry: false,
  })

  if (isLoading) return (
    <div style={{ padding: '16px 20px', fontSize: '12px', color: 'var(--text-muted)' }}>Loading…</div>
  )
  if (!data) return (
    <div style={{ padding: '16px 20px', fontSize: '12px', color: 'var(--text-muted)' }}>No git data.</div>
  )

  return (
    <div style={{ padding: '16px 20px' }}>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '10px', fontSize: '12px' }}>
        <div>
          <span style={{ color: 'var(--text-muted)', display: 'block', marginBottom: '2px', fontSize: '10px' }}>Branch</span>
          <span style={{ color: 'var(--text-primary)', fontFamily: 'monospace', fontSize: '11px' }}>{data.branch_name}</span>
        </div>
        <div>
          <span style={{ color: 'var(--text-muted)', display: 'block', marginBottom: '2px', fontSize: '10px' }}>Commit</span>
          <span style={{ color: 'var(--text-primary)', fontFamily: 'monospace', fontSize: '11px' }}>{data.commit_sha?.slice(0, 12)}</span>
        </div>
        <div>
          <span style={{ color: 'var(--text-muted)', display: 'block', marginBottom: '2px', fontSize: '10px' }}>Merge Request</span>
          <span style={{ color: 'var(--accent)', fontFamily: 'monospace', fontSize: '11px' }}>!{data.mr_iid}</span>
        </div>
        <div>
          <span style={{ color: 'var(--text-muted)', display: 'block', marginBottom: '2px', fontSize: '10px' }}>Status</span>
          <span style={{ color: 'var(--success)', fontSize: '11px', fontWeight: '600', textTransform: 'capitalize' }}>{data.status?.replace('_', ' ')}</span>
        </div>
      </div>
      {data.mr_url && (
        <div style={{ marginTop: '10px', paddingTop: '10px', borderTop: '1px solid var(--border-subtle)' }}>
          <span style={{ fontSize: '10px', color: 'var(--text-muted)' }}>MR URL: </span>
          <a href={safeHref(data.mr_url)} target="_blank" rel="noopener noreferrer"
             style={{ fontSize: '11px', color: 'var(--accent)', fontFamily: 'monospace', wordBreak: 'break-all' }}>
            {data.mr_url}
          </a>
        </div>
      )}
    </div>
  )
}

function SimpleStepDetail({ stepKey }) {
  return (
    <div style={{ padding: '16px 20px' }}>
      <p style={{ margin: 0, fontSize: '12px', color: 'var(--text-muted)' }}>
        {PHASE_B_STEP_DESCRIPTIONS[stepKey] || 'Step completed.'}
      </p>
    </div>
  )
}

const EXPANDABLE_PHASE_B = ['code_change', 'code_review', 'is_review', 'git', 'build', 'deploy', 'test_gen', 'test_exec', 'triage']

// For agentic changes, Code Gen / Review / Git are done by the agentic run, not the
// legacy pipeline — so the legacy detail panels are empty. Show an agentic-aware
// note pointing at the Phase B page (where the agentic panel + MR live) instead.
const AGENTIC_OWNED_STAGES = new Set(['code_change', 'code_review', 'git'])

function AgenticStageDetail({ stage, changeId }) {
  const navigate = useNavigate()
  const blurb = {
    code_change: 'The code was written by the AI agentic code-gen run (XSD-driven, verified by a real build).',
    code_review: 'The change was reviewed by the agentic reviewer before the approval gate.',
    git: 'The agentic run committed the change and opened the merge request on approval.',
  }[stage.key] || 'Completed by the agentic code-gen run.'
  return (
    <div style={{ padding: '16px 20px', fontSize: '12px', color: 'var(--text-secondary)' }}>
      <p style={{ margin: '0 0 10px' }}>✓ {blurb}</p>
      <button onClick={() => navigate(`/changes/${changeId}/phase-b`)} style={{
        padding: '6px 14px', fontSize: '12px', fontWeight: 600, cursor: 'pointer',
        background: 'transparent', color: 'var(--accent)', border: '1px solid var(--accent)', borderRadius: 6,
      }}>Open Phase B — diff &amp; merge request →</button>
    </div>
  )
}

function PhaseBStageRow({ stage, state, changeId, isLast, agentic = false }) {
  const [expanded, setExpanded] = useState(false)
  const canExpand = state === 'done' && EXPANDABLE_PHASE_B.includes(stage.key)
  const Icon = stage.icon || Circle

  return (
    <div style={{ borderBottom: isLast ? 'none' : '1px solid var(--border-subtle)' }}>
      <div
        onClick={() => canExpand && setExpanded(e => !e)}
        style={{
          display: 'flex', alignItems: 'center', gap: '14px',
          padding: '12px 20px',
          cursor: canExpand ? 'pointer' : 'default',
          background: state === 'active' ? 'rgba(218,119,86,0.06)' : 'transparent',
          transition: 'background 0.15s',
          opacity: state === 'pending' ? 0.5 : 1,
        }}
        onMouseEnter={e => { if (canExpand) e.currentTarget.style.background = 'var(--bg-card)' }}
        onMouseLeave={e => { if (state !== 'active') e.currentTarget.style.background = 'transparent' }}
      >
        <div style={{ flexShrink: 0 }}>
          {state === 'done' ? (
            <CheckCircle size={18} style={{ color: 'var(--success)' }} />
          ) : state === 'active' ? (
            <Clock size={18} style={{ color: 'var(--accent)' }} />
          ) : (
            <Circle size={18} style={{ color: 'var(--border)' }} />
          )}
        </div>
        <div style={{ flex: 1 }}>
          <p style={{
            margin: '0 0 2px', fontSize: '13px', fontWeight: '500',
            color: state === 'pending' ? 'var(--text-muted)' : 'var(--text-primary)',
          }}>
            {stage.label}
          </p>
          <p style={{ margin: 0, fontSize: '11px', color: 'var(--text-muted)' }}>
            {PHASE_B_STEP_DESCRIPTIONS[stage.key] || ''}
          </p>
        </div>
        {state === 'active' && (
          <span style={{
            fontSize: '11px', padding: '2px 10px', borderRadius: '20px',
            background: 'rgba(218,119,86,0.12)', color: 'var(--accent)',
            border: '1px solid rgba(218,119,86,0.3)', fontWeight: '500', whiteSpace: 'nowrap',
          }}>
            In Progress
          </span>
        )}
        {canExpand && (
          <div style={{ color: 'var(--text-muted)', flexShrink: 0 }}>
            {expanded ? <ChevronUp size={15} /> : <ChevronDown size={15} />}
          </div>
        )}
      </div>

      {canExpand && expanded && (
        <div style={{ borderTop: '1px solid var(--border-subtle)', background: 'var(--bg-base)' }}>
          {agentic && AGENTIC_OWNED_STAGES.has(stage.key)
            ? <AgenticStageDetail stage={stage} changeId={changeId} />
            : <>
          {stage.key === 'code_change' && <CodeGenDetail changeId={changeId} />}
          {stage.key === 'code_review' && <CodeReviewDetail changeId={changeId} />}
          {stage.key === 'is_review' && <ISReviewDetail changeId={changeId} />}
          {stage.key === 'git' && <GitDetail changeId={changeId} />}
          {stage.key === 'build'     && <BuildDetail     changeId={changeId} />}
          {stage.key === 'deploy'    && <DeployDetail    changeId={changeId} />}
          {stage.key === 'test_gen'  && <TestGenDetail   changeId={changeId} />}
          {stage.key === 'test_exec' && <TestExecDetail  changeId={changeId} />}
          {stage.key === 'triage' && (
            <SimpleStepDetail stepKey={stage.key} />
          )}
            </>}
        </div>
      )}
    </div>
  )
}

// ── Sub-components ────────────────────────────────────────────────────────────

function ChatBubble({ role, content }) {
  const isUser = role === 'user'
  return (
    <div style={{ display: 'flex', justifyContent: isUser ? 'flex-end' : 'flex-start', marginBottom: '10px' }}>
      {!isUser && (
        <div style={{
          width: '24px', height: '24px', borderRadius: '50%', background: 'var(--accent)',
          color: 'white', display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontSize: '9px', fontWeight: '700', flexShrink: 0, marginRight: '8px', marginTop: '2px',
        }}>AI</div>
      )}
      <div style={{
        maxWidth: '80%', padding: '10px 14px',
        borderRadius: isUser ? '10px 10px 3px 10px' : '10px 10px 10px 3px',
        background: isUser ? 'var(--accent)' : 'var(--bg-card)',
        color: isUser ? 'white' : 'var(--text-primary)',
        border: isUser ? 'none' : '1px solid var(--border-subtle)',
        fontSize: '13px', lineHeight: '1.6',
      }}>
        {isUser
          ? <p style={{ margin: 0 }}>{content}</p>
          : <div className="md-content"><ReactMarkdown>{content}</ReactMarkdown></div>
        }
      </div>
      {isUser && (
        <div style={{
          width: '24px', height: '24px', borderRadius: '50%', background: 'var(--bg-elevated)',
          border: '1px solid var(--border)', color: 'var(--text-muted)',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontSize: '9px', fontWeight: '700', flexShrink: 0, marginLeft: '8px', marginTop: '2px',
        }}>YOU</div>
      )}
    </div>
  )
}

// Prompt Enhancement expanded detail
export function PromptEnhancementDetail({ changeId, enhancedPrompt }) {
  const { data, isLoading } = useQuery({
    queryKey: ['conversation', changeId, 'prompt_enhancer'],
    queryFn:  () => agentsApi.conversation(changeId, 'prompt_enhancer').then(r => r.data),
  })

  if (isLoading) return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '16px', color: 'var(--text-muted)', fontSize: '13px' }}>
      <Loader size={14} style={{ animation: 'spin 1s linear infinite' }} /> Loading conversation…
    </div>
  )

  const messages = data || []

  return (
    <div style={{ padding: '16px 20px 20px' }}>
      {messages.length === 0 ? (
        <p style={{ margin: 0, fontSize: '13px', color: 'var(--text-muted)' }}>No conversation recorded.</p>
      ) : (
        <div style={{ marginBottom: enhancedPrompt ? '16px' : 0 }}>
          {messages.map((m, i) => <ChatBubble key={i} role={m.role} content={m.content} />)}
        </div>
      )}

      {enhancedPrompt && (
        <div style={{
          padding: '12px 16px', borderRadius: '8px',
          background: 'rgba(76,175,125,0.08)', border: '1px solid rgba(76,175,125,0.25)',
        }}>
          <p style={{ margin: '0 0 6px', fontSize: '11px', fontWeight: '600', color: 'var(--success)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
            Final Enriched Prompt
          </p>
          <p style={{ margin: 0, fontSize: '13px', color: 'var(--text-primary)', lineHeight: '1.65' }}>
            {enhancedPrompt}
          </p>
        </div>
      )}
    </div>
  )
}

// Deep Research expanded detail
export function ResearchDetail({ changeId }) {
  const { data: researchData, isLoading: loadingReport } = useQuery({
    queryKey: ['research', changeId],
    queryFn:  () => agentsApi.research(changeId).then(r => r.data),
  })
  const { data: convData, isLoading: loadingConv } = useQuery({
    queryKey: ['conversation', changeId, 'researcher'],
    queryFn:  () => agentsApi.conversation(changeId, 'researcher').then(r => r.data),
  })

  if (loadingReport || loadingConv) return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '16px', color: 'var(--text-muted)', fontSize: '13px' }}>
      <Loader size={14} style={{ animation: 'spin 1s linear infinite' }} /> Loading research…
    </div>
  )

  const report   = researchData?.report
  const version  = researchData?.version || 1
  const messages = convData || []

  // Pair each user feedback with the assistant response that follows it.
  // Skip the first user message (initial trigger) since that's implicit.
  const pairs = []
  for (let i = 0; i < messages.length; i++) {
    if (messages[i].role !== 'user') continue
    const assistantAfter = messages.slice(i + 1).find(m => m.role === 'assistant')
    pairs.push({ user: messages[i], assistant: assistantAfter || null })
  }
  const feedbackPairs = pairs.slice(1)  // drop initial trigger pair

  return (
    <div style={{ padding: '16px 20px 20px' }}>
      {/* Research report */}
      {report ? (
        <div style={{
          background: 'var(--bg-card)', border: '1px solid var(--border-subtle)',
          borderRadius: '8px', padding: '20px 24px', marginBottom: feedbackPairs.length ? '20px' : 0,
        }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '14px' }}>
            <p style={{ margin: 0, fontSize: '11px', fontWeight: '600', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
              Research Report
            </p>
            <span style={{
              fontSize: '11px', padding: '2px 8px', borderRadius: '4px',
              background: 'var(--bg-elevated)', border: '1px solid var(--border)',
              color: 'var(--text-muted)',
            }}>v{version}</span>
          </div>
          <div className="md-content research-report">
            <ReactMarkdown>{report}</ReactMarkdown>
          </div>
        </div>
      ) : (
        <p style={{ margin: 0, fontSize: '13px', color: 'var(--text-muted)' }}>No research report recorded.</p>
      )}

      {/* Feedback rounds — each with PM input + assistant response */}
      {feedbackPairs.length > 0 && (
        <div>
          <p style={{ margin: '0 0 10px', fontSize: '11px', fontWeight: '600', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
            Feedback Rounds ({feedbackPairs.length})
          </p>
          {feedbackPairs.map((pair, i) => (
            <div key={i} style={{
              padding: '12px 16px', borderRadius: '6px', marginBottom: '10px',
              background: 'var(--bg-card)', border: '1px solid var(--border-subtle)',
            }}>
              <p style={{ margin: '0 0 6px', fontSize: '11px', color: 'var(--text-muted)', fontWeight: '600', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
                Feedback #{i + 1}
              </p>
              <p style={{ margin: 0, fontSize: '13px', color: 'var(--text-primary)', lineHeight: '1.6', fontWeight: '500' }}>
                {pair.user.content}
              </p>
              {pair.assistant && (
                <div style={{
                  marginTop: '10px', paddingTop: '10px',
                  borderTop: '1px dashed var(--border-subtle)',
                }}>
                  <p style={{ margin: '0 0 6px', fontSize: '10px', color: 'var(--text-muted)', fontWeight: '600', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
                    Revised report
                  </p>
                  <div className="md-content" style={{ fontSize: '12px', color: 'var(--text-secondary)', lineHeight: '1.65' }}>
                    <ReactMarkdown>{pair.assistant.content.slice(0, 1500) + (pair.assistant.content.length > 1500 ? '…' : '')}</ReactMarkdown>
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// Canvas expanded detail
export function CanvasDetail({ changeId }) {
  const { data, isLoading } = useQuery({
    queryKey: ['canvas', changeId],
    queryFn: () => agentsApi.canvas(changeId).then(r => r.data),
  })
  // Hoisted above the loading return: it is a hook, so it must run on every
  // render, not only once the query has resolved.
  const { id } = useParams()

  if (isLoading) return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '16px', color: 'var(--text-muted)', fontSize: '13px' }}>
      <Loader size={14} style={{ animation: 'spin 1s linear infinite' }} /> Loading canvas…
    </div>
  )

  const content = data?.content
  const version = data?.version || 1

  return (
    <div style={{ padding: '16px 20px 20px' }}>
      {content ? (
        <div style={{
          background: 'var(--bg-card)', border: '1px solid var(--border-subtle)',
          borderRadius: '8px', padding: '20px 24px',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '14px', gap: '10px' }}>
            <p style={{ margin: 0, fontSize: '11px', fontWeight: '600', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
              Product Canvas
            </p>
            <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
              <DocxDownloadButton changeId={id} docType="canvas" label=".docx" />
              <span style={{
                fontSize: '11px', padding: '2px 8px', borderRadius: '4px',
                background: 'var(--bg-elevated)', border: '1px solid var(--border)', color: 'var(--text-muted)',
              }}>v{version}</span>
            </div>
          </div>
          <div className="md-content" style={{ fontSize: '13px', lineHeight: '1.7' }}>
            <ReactMarkdown>{content}</ReactMarkdown>
          </div>
        </div>
      ) : (
        <p style={{ margin: 0, fontSize: '13px', color: 'var(--text-muted)' }}>No canvas recorded.</p>
      )}
    </div>
  )
}

// Clarification expanded detail — Sprint 5
function ClarificationDetail({ changeId }) {
  const { data, isLoading } = useQuery({
    queryKey: ['clarification', changeId],
    queryFn:  () => clarifyApi.get(changeId).then(r => r.data),
  })

  if (isLoading) return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '16px', color: 'var(--text-muted)', fontSize: '13px' }}>
      <Loader size={14} style={{ animation: 'spin 1s linear infinite' }} /> Loading clarifications…
    </div>
  )

  if (!data || data.exists === false) {
    return (
      <p style={{ margin: 0, padding: '16px 20px', fontSize: '13px', color: 'var(--text-muted)' }}>
        No clarification session has been run for this change request.
      </p>
    )
  }

  const statusColors = {
    pending:  { color: '#e8a44a', label: 'Awaiting answers' },
    answered: { color: '#4caf7d', label: 'Answered' },
    skipped:  { color: '#6ea8dc', label: 'Skipped (no blocking gaps)' },
    stale:    { color: '#da7756', label: 'Stale — context changed' },
  }
  const statusCfg = statusColors[data.status] || statusColors.pending
  const questions = data.questions || []
  const answers = data.answers || {}
  const assumedGaps = data.assumed_gaps || []
  const blockingCount = (data.blocking_gap_keys || []).length

  return (
    <div style={{ padding: '16px 20px 20px' }}>
      {/* Header summary */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '14px', flexWrap: 'wrap' }}>
        <span style={{
          fontSize: '11px', padding: '3px 10px', borderRadius: '20px',
          background: `${statusCfg.color}22`, border: `1px solid ${statusCfg.color}55`,
          color: statusCfg.color, fontWeight: '500',
        }}>{statusCfg.label}</span>
        <span style={{
          fontSize: '11px', padding: '2px 8px', borderRadius: '4px',
          background: 'var(--bg-elevated)', border: '1px solid var(--border)',
          color: 'var(--text-muted)',
        }}>v{data.version}</span>
        <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>
          {blockingCount} blocking gap{blockingCount !== 1 ? 's' : ''} · {assumedGaps.length} assumed
        </span>
      </div>

      {/* Questions with answers */}
      {questions.length > 0 ? (
        <div style={{ marginBottom: assumedGaps.length ? '16px' : 0 }}>
          <p style={{ margin: '0 0 10px', fontSize: '11px', fontWeight: '600', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
            Clarification Q&amp;A
          </p>
          {questions.map((q, i) => {
            const ans = (answers[q.id] || '').trim()
            return (
              <div key={q.id} style={{
                padding: '12px 16px', marginBottom: '10px', borderRadius: '6px',
                background: 'var(--bg-card)', border: '1px solid var(--border-subtle)',
              }}>
                <div style={{ display: 'flex', alignItems: 'flex-start', gap: '10px', marginBottom: '8px' }}>
                  <div style={{
                    flexShrink: 0, width: '24px', height: '24px', borderRadius: '50%',
                    background: 'rgba(218,119,86,0.10)',
                    border: '1px solid rgba(218,119,86,0.25)',
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                    color: 'var(--accent)', fontSize: '11px', fontWeight: '600',
                  }}>{i + 1}</div>
                  <div style={{ flex: 1 }}>
                    <p style={{ margin: 0, fontSize: '13px', color: 'var(--text-primary)', lineHeight: '1.55' }}>
                      {q.text}
                    </p>
                    {q.gap_key && (
                      <p style={{ margin: '2px 0 0', fontSize: '10px', color: 'var(--text-muted)', fontFamily: 'var(--font-mono, monospace)' }}>
                        gap: {q.gap_key}
                      </p>
                    )}
                  </div>
                </div>
                <div style={{
                  marginLeft: '34px', paddingLeft: '10px',
                  borderLeft: `3px solid ${ans ? '#4caf7d' : 'var(--border-subtle)'}`,
                }}>
                  {ans ? (
                    <p style={{ margin: 0, fontSize: '12px', color: 'var(--text-secondary)', lineHeight: '1.6', whiteSpace: 'pre-wrap' }}>
                      {ans}
                    </p>
                  ) : (
                    <p style={{ margin: 0, fontSize: '11px', color: 'var(--text-muted)', fontStyle: 'italic' }}>
                      (no answer submitted)
                    </p>
                  )}
                </div>
              </div>
            )
          })}
        </div>
      ) : (
        <p style={{ margin: '0 0 14px', fontSize: '13px', color: 'var(--text-muted)' }}>
          No clarification questions were generated — the spec was complete.
        </p>
      )}

      {/* Assumed gaps */}
      {assumedGaps.length > 0 && (
        <div style={{
          padding: '12px 16px', borderRadius: '6px',
          background: 'var(--bg-elevated)', border: '1px dashed var(--border)',
        }}>
          <p style={{ margin: '0 0 8px', fontSize: '11px', fontWeight: '600', color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
            <MessageCircleQuestion size={11} style={{ verticalAlign: '-2px', marginRight: '4px' }} />
            Non-blocking assumptions (platform defaults used)
          </p>
          <ul style={{ margin: 0, paddingLeft: '18px', fontSize: '11px', color: 'var(--text-muted)', lineHeight: '1.7' }}>
            {assumedGaps.map((g, i) => (
              <li key={i}><strong>{g.key}:</strong> {g.default}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

// Clarification stage: the code-grounded AnalysisPanel auto-starts when the flow is
// enabled and, once active, REPLACES the legacy clarification questions (not both).
// When disabled (flag off / no role-flagged repos), only the legacy UI shows.
export function ClarificationStageDetail({ changeId }) {
  // The agentic Change-Analysis IS the clarification stage now — the legacy
  // clarification Q&A has been removed. The analysis panel owns this stage.
  return <AnalysisPanel changeId={changeId} hideActions />
}

// Hook: fetch validation for an artifact
function useArtifactValidation(changeId, docType, enabled = true, subtype) {
  return useQuery({
    queryKey: ['validation', changeId, docType, subtype || ''],
    queryFn:  () => validationApi.run(changeId, docType, subtype).then(r => r.data),
    enabled: Boolean(enabled && changeId && docType),
    staleTime: 60_000,
  })
}

// BRD expanded detail
// Generate-or-Upload provenance pill, shown when an artifact was user-uploaded.
function UploadedBadge({ source, filename }) {
  if (source !== 'uploaded') return null
  return (
    <span title={filename ? `Uploaded: ${filename}` : 'User-uploaded document'} style={{
      fontSize: '11px', padding: '2px 8px', borderRadius: '4px',
      background: 'rgba(106,169,255,0.10)', border: '1px solid rgba(106,169,255,0.30)',
      color: 'var(--accent, #6aa9ff)',
    }}>Uploaded</span>
  )
}

export function BRDDetail({ changeId }) {
  // ALL hooks must run every render — never put them after an early return.
  const { data: brdData, isLoading: loadingBrd } = useQuery({
    queryKey: ['brd', changeId],
    queryFn: () => agentsApi.brd(changeId).then(r => r.data),
  })
  const { data: approvalsData } = useQuery({
    queryKey: ['brd-approvals', changeId],
    queryFn: () => agentsApi.brdApprovals(changeId).then(r => r.data),
    enabled: !!brdData,
  })
  const { data: validation } = useArtifactValidation(changeId, 'brd', Boolean(brdData?.content))

  if (loadingBrd) return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '16px', color: 'var(--text-muted)', fontSize: '13px' }}>
      <Loader size={14} style={{ animation: 'spin 1s linear infinite' }} /> Loading BRD…
    </div>
  )

  const content   = brdData?.content
  const version   = brdData?.version || 1
  const status    = brdData?.status
  const approvals = approvalsData?.approvals || []

  const statusColor = {
    draft:     'var(--text-muted)',
    submitted: 'var(--accent)',
    approved:  'var(--success)',
    rejected:  'var(--danger)',
  }

  return (
    <div style={{ padding: '16px 20px 20px' }}>
      {/* Approval summary with reviewer comments */}
      {approvals.length > 0 && (
        <div style={{ marginBottom: '16px' }}>
          <p style={{ margin: '0 0 8px', fontSize: '11px', fontWeight: '600', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
            Approvals
          </p>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            {approvals.map(a => (
              <div key={a.id} style={{
                padding: '10px 14px', borderRadius: '6px',
                background: a.status === 'approved' ? 'rgba(76,175,125,0.06)' : a.status === 'rejected' ? 'rgba(224,108,108,0.06)' : 'var(--bg-card)',
                border: `1px solid ${a.status === 'approved' ? 'rgba(76,175,125,0.25)' : a.status === 'rejected' ? 'rgba(224,108,108,0.25)' : 'var(--border-subtle)'}`,
              }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: a.comments ? '6px' : 0 }}>
                  <span style={{
                    fontSize: '11px', fontWeight: '600',
                    color: a.status === 'approved' ? 'var(--success)' : a.status === 'rejected' ? 'var(--danger)' : 'var(--text-muted)',
                    textTransform: 'uppercase', letterSpacing: '0.05em',
                  }}>
                    {a.status}
                  </span>
                  <span style={{ fontSize: '12px', color: 'var(--text-primary)', fontWeight: '500' }}>
                    {a.reviewer_name || a.reviewer_id}
                    {a.reviewer_role && (
                      <span style={{ fontSize: '11px', color: 'var(--text-muted)', fontWeight: 400, marginLeft: '6px' }}>
                        ({a.reviewer_role.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())})
                      </span>
                    )}
                  </span>
                </div>
                {a.comments && (
                  <p style={{
                    margin: 0, fontSize: '12px', color: 'var(--text-secondary)',
                    lineHeight: '1.55', whiteSpace: 'pre-wrap',
                    paddingLeft: '8px', borderLeft: '3px solid var(--border-subtle)',
                  }}>
                    {a.comments}
                  </p>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Validation panel */}
      {validation && validation.issues && validation.issues.length > 0 && (
        <ValidationPanel validation={validation} hideErrors />
      )}

      {content ? (
        <div style={{
          background: 'var(--bg-card)', border: '1px solid var(--border-subtle)',
          borderRadius: '8px', padding: '20px 24px',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '14px', gap: '10px' }}>
            <p style={{ margin: 0, fontSize: '11px', fontWeight: '600', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
              BRD
            </p>
            <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
              <UploadedBadge source={brdData?.source} filename={brdData?.original_filename} />
              <DocxDownloadButton changeId={changeId} docType="brd" label=".docx" />
              {status && (
                <span style={{
                  fontSize: '11px', padding: '2px 8px', borderRadius: '4px',
                  background: 'var(--bg-elevated)', border: '1px solid var(--border)',
                  color: statusColor[status] || 'var(--text-muted)',
                  textTransform: 'capitalize',
                }}>{status}</span>
              )}
              <span style={{
                fontSize: '11px', padding: '2px 8px', borderRadius: '4px',
                background: 'var(--bg-elevated)', border: '1px solid var(--border)', color: 'var(--text-muted)',
              }}>v{version}</span>
            </div>
          </div>
          <div className="md-content" style={{ fontSize: '13px', lineHeight: '1.7' }}>
            <ReactMarkdown>{content}</ReactMarkdown>
          </div>
        </div>
      ) : (
        <p style={{ margin: 0, fontSize: '13px', color: 'var(--text-muted)' }}>No BRD recorded.</p>
      )}
    </div>
  )
}

// TechSpec expanded detail
export function TechSpecDetail({ changeId }) {
  const { data, isLoading } = useQuery({
    queryKey: ['tech-spec', changeId],
    queryFn: () => agentsApi.techSpec(changeId).then(r => r.data),
  })
  const { data: convData } = useQuery({
    queryKey: ['conversation', changeId, 'tech_spec'],
    queryFn:  () => agentsApi.conversation(changeId, 'tech_spec').then(r => r.data),
  })
  const { data: validation } = useArtifactValidation(changeId, 'tech_spec', Boolean(data?.content))

  if (isLoading) return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '16px', color: 'var(--text-muted)', fontSize: '13px' }}>
      <Loader size={14} style={{ animation: 'spin 1s linear infinite' }} /> Loading Tech Spec…
    </div>
  )

  const content = data?.content
  const version = data?.version || 1
  const messages = convData || []

  // Pair user feedback with assistant responses (mirrors ResearchDetail)
  const pairs = []
  for (let i = 0; i < messages.length; i++) {
    if (messages[i].role !== 'user') continue
    const assistantAfter = messages.slice(i + 1).find(m => m.role === 'assistant')
    pairs.push({ user: messages[i], assistant: assistantAfter || null })
  }
  const feedbackPairs = pairs.slice(1)

  return (
    <div style={{ padding: '16px 20px 20px' }}>
      {validation && validation.issues && validation.issues.length > 0 && (
        <ValidationPanel validation={validation} hideErrors />
      )}

      {content ? (
        <div style={{
          background: 'var(--bg-card)', border: '1px solid var(--border-subtle)',
          borderRadius: '8px', padding: '20px 24px', marginBottom: feedbackPairs.length ? '20px' : 0,
        }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '14px', gap: '10px' }}>
            <p style={{ margin: 0, fontSize: '11px', fontWeight: '600', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
              Technical Specification
            </p>
            <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
              <UploadedBadge source={data?.source} filename={data?.original_filename} />
              <DocxDownloadButton changeId={changeId} docType="tech_spec" label=".docx" />
              <span style={{
                fontSize: '11px', padding: '2px 8px', borderRadius: '4px',
                background: 'var(--bg-elevated)', border: '1px solid var(--border)', color: 'var(--text-muted)',
              }}>v{version}</span>
            </div>
          </div>
          <div className="md-content" style={{ fontSize: '13px', lineHeight: '1.7' }}>
            <ReactMarkdown>{content}</ReactMarkdown>
          </div>
        </div>
      ) : (
        <p style={{ margin: 0, fontSize: '13px', color: 'var(--text-muted)' }}>No Tech Spec recorded.</p>
      )}

      {feedbackPairs.length > 0 && (
        <div>
          <p style={{ margin: '0 0 10px', fontSize: '11px', fontWeight: '600', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
            Feedback Rounds ({feedbackPairs.length})
          </p>
          {feedbackPairs.map((pair, i) => (
            <div key={i} style={{
              padding: '12px 16px', marginBottom: '10px', borderRadius: '6px',
              background: 'var(--bg-card)', border: '1px solid var(--border-subtle)',
            }}>
              <p style={{ margin: '0 0 6px', fontSize: '11px', color: 'var(--text-muted)', fontWeight: '600', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
                Feedback #{i + 1}
              </p>
              <p style={{ margin: 0, fontSize: '13px', color: 'var(--text-primary)', lineHeight: '1.6', fontWeight: '500' }}>
                {pair.user.content}
              </p>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// Turn an XSD git diff into a plain-English, PM-readable summary — no diff syntax.
// We read only the ADDED lines and translate schema constructs into business terms.
function xsdChangeSummary(diff) {
  const added = String(diff || '').split('\n')
    .filter(l => l.startsWith('+') && !l.startsWith('+++'))
    .map(l => l.slice(1).trim())

  // Multi-line XML comments are the agent's own plain-English rationale — collect them.
  const notes = []
  let buf = null
  for (const line of added) {
    if (buf !== null) {
      buf += ' ' + line.replace('-->', '').trim()
      if (line.includes('-->')) { notes.push(buf.replace(/\s+/g, ' ').trim()); buf = null }
      continue
    }
    if (line.includes('<!--')) {
      const text = line.replace('<!--', '').replace('-->', '').trim()
      if (line.includes('-->')) { if (text) notes.push(text) }
      else buf = text
    }
  }

  // Field names on REMOVED lines — so a genuinely NEW field (added only) is told apart
  // from a MODIFIED one (a type/use/minOccurs edit shows as remove+add in a git diff).
  const removedNames = new Set()
  for (const l of String(diff || '').split('\n')
        .filter(l => l.startsWith('-') && !l.startsWith('---')).map(l => l.slice(1).trim())) {
    const m = l.match(/<xs:(?:attribute|element)\s+name="([^"]+)"/)
    if (m) removedNames.add(m[1])
  }

  // Fields (attributes/elements) → name + required/optional, tagged new vs modified.
  const fields = []
  for (const l of added) {
    const m = l.match(/<xs:(?:attribute|element)\s+name="([^"]+)"/)
    if (m) {
      const optional = /use="optional"|minOccurs="0"/.test(l)
      const typeM = l.match(/type="(?:[^:"]+:)?([^"]+)"/)
      fields.push({ name: m[1], optional, type: typeM ? typeM[1] : null, modified: removedNames.has(m[1]) })
    }
  }

  // New data types + their allowed values (enumerations).
  const types = []
  let curType = null
  for (const l of added) {
    const tm = l.match(/<xs:(?:simpleType|complexType)\s+name="([^"]+)"/)
    if (tm) { curType = { name: tm[1], values: [] }; types.push(curType); continue }
    const em = l.match(/<xs:enumeration\s+value="([^"]+)"/)
    if (em && curType) curType.values.push(em[1])
  }

  return { notes, fields, types }
}

// Parse a git diff into clean rows for a PM-friendly added/removed view:
// no +/- prefixes, no "diff --git"/"index"/"@@" plumbing — just the content,
// tagged added | removed | context so we can colour it green/red/neutral.
function cleanDiffRows(diff) {
  const rows = []
  for (const raw of String(diff || '').split('\n')) {
    if (/^(diff --git|index |--- |\+\+\+ |@@ )/.test(raw)) continue
    if (raw.startsWith('+')) rows.push({ type: 'added', text: raw.slice(1) })
    else if (raw.startsWith('-')) rows.push({ type: 'removed', text: raw.slice(1) })
    else rows.push({ type: 'context', text: raw.replace(/^ /, '') })
  }
  // Trim leading/trailing blank context for a tighter view.
  while (rows.length && rows[0].type === 'context' && !rows[0].text.trim()) rows.shift()
  while (rows.length && rows[rows.length - 1].type === 'context' && !rows[rows.length - 1].text.trim()) rows.pop()
  return rows
}

// XSD expanded detail
export function XSDDetail({ changeId }) {
  const { data, isLoading } = useQuery({
    queryKey: ['xsd', changeId],
    queryFn: () => agentsApi.xsd(changeId).then(r => r.data),
  })
  const { data: validation } = useArtifactValidation(changeId, 'xsd', Boolean(data?.content))

  // Agentic XSD: the schema edits live as a repo git diff on the kind='xsd' run, NOT
  // in the xsds doc table — fetch + render them so the stage isn't wrongly "empty".
  const { data: xsdRuns } = useQuery({
    queryKey: ['xsd-runs', changeId],
    queryFn: () => agenticApi.listChangeRuns(changeId, 'xsd').then(r => r.data).catch(() => ({ runs: [] })),
  })
  const xsdRun = (xsdRuns?.runs || [])[0]
  const { data: xsdDiffData } = useQuery({
    queryKey: ['xsd-run-diff', xsdRun?.run_id],
    queryFn: () => agenticApi.getDiff(xsdRun.run_id).then(r => r.data).catch(() => ({ diffs: {} })),
    enabled: !!xsdRun?.run_id,
  })
  // Full (post-change) file contents — the downloadable schema, frozen at handoff.
  const { data: xsdFilesData } = useQuery({
    queryKey: ['xsd-run-files', xsdRun?.run_id],
    queryFn: () => agenticApi.getXsdFiles(xsdRun.run_id).then(r => r.data).catch(() => ({ files: [] })),
    enabled: !!xsdRun?.run_id,
  })
  const fileContentByPath = Object.fromEntries((xsdFilesData?.files || []).map(f => [f.path, f.content]))
  const downloadFile = (path) => {
    const content = fileContentByPath[path]
    if (!content) return
    const name = path.split('/').pop()
    const a = document.createElement('a')
    a.href = `data:application/xml;charset=utf-8,${encodeURIComponent(content)}`
    a.download = name
    a.click()
  }
  // Schema stage shows ONLY the .xsd files the agent changed/created — never the
  // generated/touched .java that may share the run's diff (those aren't schema and
  // shouldn't be offered for partner download).
  const agenticDiffs = Object.entries(xsdDiffData?.diffs || {})
    .map(([rid, d]) => ({
      rid, diff: String(d || ''),
      files: [...String(d || '').matchAll(/^diff --git a\/(.+?) b\//gm)]
        .map(m => m[1]).filter(f => f.toLowerCase().endsWith('.xsd')),
    }))
    .filter(x => x.files.length > 0)

  if (isLoading) return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '16px', color: 'var(--text-muted)', fontSize: '13px' }}>
      <Loader size={14} style={{ animation: 'spin 1s linear infinite' }} /> Loading XSD…
    </div>
  )

  const content     = data?.content
  const version     = data?.version || 1
  const isRequired  = data?.is_required

  return (
    <div style={{ padding: '16px 20px 20px' }}>
      {isRequired === false && (
        <div style={{
          padding: '10px 16px', borderRadius: '6px', marginBottom: '12px',
          background: 'rgba(76,175,125,0.08)', border: '1px solid rgba(76,175,125,0.25)',
          fontSize: '13px', color: 'var(--success)',
        }}>
          No XSD changes were required for this feature.
        </div>
      )}
      {validation && validation.issues && validation.issues.length > 0 && (
        <ValidationPanel validation={validation} hideErrors />
      )}
      {content ? (
        <div style={{
          background: 'var(--bg-card)', border: '1px solid var(--border-subtle)',
          borderRadius: '8px', padding: '20px 24px',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '14px', gap: '10px' }}>
            <p style={{ margin: 0, fontSize: '11px', fontWeight: '600', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
              {isRequired ? 'XSD Changes' : 'XSD Assessment'}
            </p>
            <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
              <DocxDownloadButton changeId={changeId} docType="xsd" label="Download" />
              <span style={{
                fontSize: '11px', padding: '2px 8px', borderRadius: '4px',
                background: 'var(--bg-elevated)', border: '1px solid var(--border)', color: 'var(--text-muted)',
              }}>v{version}</span>
            </div>
          </div>
          <div className="md-content" style={{ fontSize: '13px', lineHeight: '1.7' }}>
            <ReactMarkdown>{content}</ReactMarkdown>
          </div>
        </div>
      ) : agenticDiffs.length > 0 ? (
        <div style={{ background: 'var(--bg-card)', border: '1px solid var(--border-subtle)', borderRadius: '8px', padding: '16px 18px' }}>
          <p style={{ margin: '0 0 12px', fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
            Schema changes — {agenticDiffs.length} file{agenticDiffs.length > 1 ? 's' : ''} updated
          </p>
          {agenticDiffs.map(({ rid, diff, files }) => {
            const { notes, fields, types } = xsdChangeSummary(diff)
            return (
              <div key={rid} style={{ marginBottom: '16px', padding: '14px 16px', borderRadius: '8px', border: '1px solid var(--border-subtle)', background: 'var(--bg-base)' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 12, marginBottom: 4 }}>
                  <div>
                    <div style={{ fontSize: '13px', fontWeight: 600, color: 'var(--text-primary)' }}>
                      {files.map(f => f.split('/').pop()).join(', ')}
                    </div>
                    <div style={{ fontSize: '11px', color: 'var(--text-muted)', marginTop: 2 }}>{files[0]}</div>
                  </div>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 4, flexShrink: 0 }}>
                    {files.filter(f => fileContentByPath[f]).map(f => (
                      <button key={f} onClick={() => downloadFile(f)}
                        style={{ fontSize: 11, padding: '5px 10px', borderRadius: 5, border: '1px solid var(--border)',
                          background: 'var(--bg-elevated)', color: 'var(--text-secondary)', fontWeight: 600, cursor: 'pointer', whiteSpace: 'nowrap' }}>
                        Download {f.split('/').pop()}
                      </button>
                    ))}
                  </div>
                </div>
                <div style={{ marginBottom: 12 }} />

                {notes.length > 0 && (
                  <div style={{ marginBottom: 12 }}>
                    <div style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 6 }}>Why this change</div>
                    {notes.map((n, i) => (
                      <p key={i} style={{ margin: '0 0 6px', fontSize: '12.5px', color: 'var(--text-secondary)', lineHeight: 1.6 }}>{n}</p>
                    ))}
                  </div>
                )}

                {['new', 'modified'].map(group => {
                  const items = fields.filter(f => (group === 'modified') === !!f.modified)
                  if (items.length === 0) return null
                  return (
                    <div key={group} style={{ marginBottom: 12 }}>
                      <div style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 6 }}>
                        {group === 'modified' ? 'Fields changed' : 'New fields added'}
                      </div>
                      <ul style={{ margin: 0, padding: '0 0 0 18px' }}>
                        {items.map((f, i) => (
                          <li key={i} style={{ fontSize: '12.5px', color: 'var(--text-secondary)', marginBottom: 4, lineHeight: 1.5 }}>
                            <strong style={{ color: 'var(--text-primary)' }}>{f.name}</strong>
                            {' '}<span style={{ color: f.optional ? '#6ea8dc' : '#e8a44a', fontSize: 11 }}>({f.optional ? 'optional' : 'required'})</span>
                            {f.type && <span style={{ color: 'var(--text-muted)' }}> — {f.type}</span>}
                          </li>
                        ))}
                      </ul>
                    </div>
                  )
                })}

                {types.length > 0 && (
                  <div style={{ marginBottom: 4 }}>
                    <div style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 6 }}>New value sets</div>
                    <ul style={{ margin: 0, padding: '0 0 0 18px' }}>
                      {types.map((t, i) => (
                        <li key={i} style={{ fontSize: '12.5px', color: 'var(--text-secondary)', marginBottom: 4, lineHeight: 1.5 }}>
                          <strong style={{ color: 'var(--text-primary)' }}>{t.name}</strong>
                          {t.values.length > 0 && <span style={{ color: 'var(--text-muted)' }}> — allowed values: {t.values.join(', ')}</span>}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}

                {notes.length === 0 && fields.length === 0 && types.length === 0 && (
                  <p style={{ margin: 0, fontSize: '12.5px', color: 'var(--text-muted)' }}>Schema structure was adjusted (no new fields or value sets).</p>
                )}

                {/* Detailed line-by-line changes — green = added, red = removed.
                    Plain content, no git +/- syntax. Collapsed by default. */}
                <details style={{ marginTop: 12 }}>
                  <summary style={{ cursor: 'pointer', fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
                    View detailed changes
                  </summary>
                  <div style={{ marginTop: 8, border: '1px solid var(--border-subtle)', borderRadius: 6, overflow: 'hidden' }}>
                    {cleanDiffRows(diff).map((row, i) => (
                      <div key={i} style={{
                        display: 'flex', alignItems: 'flex-start', gap: 8,
                        padding: '2px 10px', fontFamily: 'var(--font-mono, monospace)', fontSize: '11.5px', lineHeight: 1.6,
                        background: row.type === 'added' ? 'rgba(76,175,80,0.10)' : row.type === 'removed' ? 'rgba(244,67,54,0.10)' : 'transparent',
                        color: row.type === 'added' ? '#2e7d32' : row.type === 'removed' ? '#c62828' : 'var(--text-muted)',
                      }}>
                        <span style={{ flexShrink: 0, width: 52, fontSize: 9.5, fontWeight: 700, textTransform: 'uppercase', opacity: 0.7, paddingTop: 1 }}>
                          {row.type === 'added' ? 'Added' : row.type === 'removed' ? 'Removed' : ''}
                        </span>
                        <span style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{row.text || ' '}</span>
                      </div>
                    ))}
                  </div>
                </details>
              </div>
            )
          })}
        </div>
      ) : (
        <p style={{ margin: 0, fontSize: '13px', color: 'var(--text-muted)' }}>No XSD data recorded.</p>
      )}
    </div>
  )
}

// ── Product Kit: single document row (expandable) ────────────────────────────

const DOC_LABELS = {
  product_deck:      'Product Deck',
  promo_video:       'Promo Video Script',
  explainer_video:   'Explainer Video Script',
  faq:               'FAQ Document',
  cert_test_cases:   'Certification Test Cases',
  circular:          t('artifact.circular'),
  manifest:          'Manifest File',
  prototype_screens: 'Prototype Screens',
  product_note:      'Product Document',
}

// ── Cert test cases — markdown → structured table ─────────────────────────────
//
// The /product-kit endpoint returns markdown for cert_test_cases (the JSON
// companion is fetched separately on the standalone ProductKit page). Here
// we parse the deterministic per-case markdown template — enforced by
// backend/app/excel_testcase_engine/prompts/writer.md — into table rows so
// the inline expansion on the Change Detail page matches the standalone view.
//
// Parsing failure (e.g. uploaded-docx variant where the template doesn't
// hold) falls back to plain markdown render.

const CERT_STATUS_COLORS = {
  Success: '#10b981',
  Failure: '#ef4444',
  Deemed:  '#f59e0b',
  Partial: '#2563eb',
}

/**
 * Read one `Label: value` field out of a DETAILS block.
 *
 * Line scan rather than a regex. The previous version built
 *   `^<label>\s*:\s*([\s\S]*?)(?=\n[A-Z][a-zA-Z ]*\s*:|$)`   (with /m)
 * per call, and its lookahead was ambiguous: `[a-zA-Z ]*` and `\s*` can both
 * match a space, so a long run of spaces containing no colon had very many ways
 * to split between them and the engine retried at every start position.
 * Measured on the real helper that was quadratic — 8k spaces 22 ms, 64k 1975 ms,
 * 128k 17.6 s — and since the parser calls this SEVEN times per test case, a
 * document of 20 such cases froze the tab for ~10 s.
 *
 * Behaviour matches the regex, which the `/m` flag made effectively
 * SINGLE-LINE: `$` matched at each line end, so the lazy group always stopped at
 * the first newline. Verified against the old implementation over 152
 * label/block combinations.
 *
 * ONE deliberate difference, fixing a bug rather than preserving it: for a label
 * with an empty value (`"API Involved:"` followed by `"Type: x"`), the old `\s*`
 * after the colon consumed the NEWLINE, so the capture started on the next line
 * and returned `"Type: x"` — another field's text reported as this one's value.
 * This returns `''`.
 *
 * NOTE: this is a byte-for-byte twin of the helper in the partner console's
 * src/pages/ChangeDetail.jsx, which lives in a separate repository
 *   Keep the two in step.
 */
function _detailField(detailsBlock, label) {
  if (!detailsBlock || !label) return ''

  const lines = String(detailsBlock).split('\n')
  const prefix = `${label}:`

  for (let i = 0; i < lines.length; i += 1) {
    if (!lines[i].startsWith(prefix)) continue
    return lines[i].slice(prefix.length).replace(/\s+/g, ' ').trim()
  }
  return ''
}

function parseCertTestCasesMd(md) {
  if (!md || typeof md !== 'string') return null
  const featureMatch = md.match(/^# (.+)$/m)
  const feature = featureMatch ? featureMatch[1].trim() : ''

  const chunks = md.split(/\n(?=### )/g)
  const caseChunks = chunks.filter(c => c.startsWith('### '))
  if (caseChunks.length === 0) return null

  const cases = []
  for (const chunk of caseChunks) {
    const headingMatch = chunk.match(/^###\s+(\S+)\s+—\s+([^(\n]+?)(?:\s+\(highlighted\))?\s*$/m)
    if (!headingMatch) continue
    const test_id = headingMatch[1].trim()
    const expected_status = headingMatch[2].trim()

    const detailsMatch = chunk.match(/\*\*DETAILS\*\*\s*\n```[^\n]*\n([\s\S]*?)\n```/)
    const details_block = detailsMatch ? detailsMatch[1] : ''

    const descMatch = chunk.match(/\*\*DESCRIPTION\*\*\s*\n+([\s\S]*?)\n+\*\*TEST STEPS\*\*/)
    const description_block = descMatch ? descMatch[1].trim() : ''

    const stepsMatch = chunk.match(/\*\*TEST STEPS\*\*\s*\n+```[^\n]*\n([\s\S]*?)\n```/)
    const steps_block = stepsMatch ? stepsMatch[1] : ''

    const respMatch = chunk.match(/_Response code:\s*`([^`]+)`_/)
    const response_code = respMatch ? respMatch[1] : ''

    cases.push({
      test_id,
      expected_status,
      apis:           _detailField(details_block, 'API Involved'),
      api_type:       _detailField(details_block, 'Type'),
      entities:       _detailField(details_block, 'Entity Involved'),
      approval_type:  _detailField(details_block, 'Approval Type'),
      payer_handle:   _detailField(details_block, 'Payer Handle'),
      payee_handle:   _detailField(details_block, 'Payee Handle'),
      details_block,
      description_block,
      steps_block,
      response_code,
    })
  }

  if (cases.length === 0) return null
  return { feature, test_cases: cases }
}

const _certTh = {
  padding: '8px 12px',
  textAlign: 'left',
  fontSize: 10,
  fontWeight: 700,
  color: 'var(--text-muted)',
  textTransform: 'uppercase',
  letterSpacing: '0.06em',
  borderBottom: '1px solid var(--border-subtle)',
  background: 'var(--bg-elevated)',
  whiteSpace: 'nowrap',
  position: 'sticky',
  top: 0,
  zIndex: 1,
}

const _certTd = {
  padding: '8px 12px',
  fontSize: 12,
  color: 'var(--text-primary)',
  verticalAlign: 'top',
  borderBottom: '1px solid var(--border-subtle)',
}

function _certPill(color) {
  return {
    display: 'inline-flex', alignItems: 'center',
    padding: '2px 8px',
    borderRadius: 999,
    fontSize: 10,
    fontWeight: 700,
    color,
    background: `${color}1A`,
    border: `1px solid ${color}40`,
    letterSpacing: '0.04em',
    whiteSpace: 'nowrap',
  }
}

function CertTestCaseRow({ tc, index }) {
  const [open, setOpen] = useState(false)
  const statusColor = CERT_STATUS_COLORS[tc.expected_status] || 'var(--text-muted)'
  return (
    <>
      <tr
        onClick={() => setOpen(v => !v)}
        style={{ cursor: 'pointer', background: index % 2 === 0 ? 'transparent' : 'var(--bg-elevated)' }}
      >
        <td style={{ ..._certTd, width: 24, textAlign: 'center', color: 'var(--text-muted)' }}>
          <ChevronRight size={12} style={{ transform: open ? 'rotate(90deg)' : 'none', transition: 'transform 0.15s' }} />
        </td>
        <td style={{ ..._certTd, fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', fontWeight: 600, whiteSpace: 'nowrap' }}>
          {tc.test_id}
        </td>
        <td style={_certTd}>
          <span style={_certPill(statusColor)}>{tc.expected_status}</span>
        </td>
        <td style={{ ..._certTd, fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', fontSize: 11 }}>
          {tc.apis || '—'}
        </td>
        <td style={{ ..._certTd, color: 'var(--text-secondary)' }}>{tc.api_type || '—'}</td>
        <td style={{ ..._certTd, color: 'var(--text-secondary)', maxWidth: 220 }}>{tc.entities || '—'}</td>
        <td style={{ ..._certTd, whiteSpace: 'nowrap' }}>{tc.approval_type || '—'}</td>
        <td style={{ ..._certTd, whiteSpace: 'nowrap' }}>{tc.payer_handle || '—'}</td>
        <td style={{ ..._certTd, whiteSpace: 'nowrap' }}>{tc.payee_handle || '—'}</td>
        <td style={{ ..._certTd, fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' }}>
          {tc.response_code || '—'}
        </td>
      </tr>
      {open && (
        <tr style={{ background: 'var(--bg-base)' }}>
          <td colSpan={10} style={{ padding: '12px 20px 16px 44px', borderBottom: '1px solid var(--border-subtle)' }}>
            <CertTestCaseExpanded tc={tc} />
          </td>
        </tr>
      )}
    </>
  )
}

function CertTestCaseExpanded({ tc }) {
  const blockStyle = {
    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
    fontSize: 11,
    lineHeight: 1.55,
    background: 'var(--bg-card)',
    border: '1px solid var(--border-subtle)',
    borderRadius: 6,
    padding: '10px 12px',
    whiteSpace: 'pre-wrap',
    margin: 0,
    color: 'var(--text-primary)',
    overflow: 'auto',
  }
  const labelStyle = {
    fontSize: 10, fontWeight: 700, color: 'var(--text-muted)',
    textTransform: 'uppercase', letterSpacing: '0.06em',
    margin: '0 0 6px',
  }
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      {tc.description_block && (
        <div>
          <p style={labelStyle}>Description</p>
          <pre style={{ ...blockStyle, fontFamily: 'inherit', fontSize: 12 }}>{tc.description_block}</pre>
        </div>
      )}
      {tc.steps_block && (
        <div>
          <p style={labelStyle}>Test Steps</p>
          <pre style={blockStyle}>{tc.steps_block}</pre>
        </div>
      )}
      {tc.details_block && (
        <div>
          <p style={labelStyle}>Details (raw)</p>
          <pre style={blockStyle}>{tc.details_block}</pre>
        </div>
      )}
    </div>
  )
}

function CertTestCasesView({ markdown }) {
  const plan = useMemo(() => parseCertTestCasesMd(markdown), [markdown])
  if (!plan || !plan.test_cases?.length) {
    return (
      <div className="md-content" style={{ fontSize: '13px', lineHeight: '1.7' }}>
        <ReactMarkdown>{markdown || ''}</ReactMarkdown>
      </div>
    )
  }
  return (
    <div>
      <div style={{
        display: 'flex', alignItems: 'baseline', gap: 12, flexWrap: 'wrap',
        marginBottom: 12, fontSize: 12, color: 'var(--text-secondary)',
      }}>
        {plan.feature && (
          <span style={{ fontSize: 14, fontWeight: 700, color: 'var(--text-primary)' }}>
            {plan.feature}
          </span>
        )}
        <span><strong>{plan.test_cases.length}</strong> test cases</span>
        <span style={{ color: 'var(--text-muted)' }}>Click a row to expand description, steps, and details.</span>
      </div>
      <div style={{
        border: '1px solid var(--border-subtle)',
        borderRadius: 8,
        overflow: 'auto',
        maxHeight: '70vh',
        background: 'var(--bg-card)',
      }}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              <th style={{ ..._certTh, width: 24 }} />
              <th style={_certTh}>TC ID</th>
              <th style={_certTh}>Status</th>
              <th style={_certTh}>APIs</th>
              <th style={_certTh}>Type</th>
              <th style={_certTh}>Entities</th>
              <th style={_certTh}>Approval</th>
              <th style={_certTh}>Payer</th>
              <th style={_certTh}>Payee</th>
              <th style={_certTh}>Resp</th>
            </tr>
          </thead>
          <tbody>
            {plan.test_cases.map((tc, i) => (
              <CertTestCaseRow key={tc.test_id || i} tc={tc} index={i} />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function ProductKitDocRow({ changeId, docType, hasContent, version, kitVersion, negVersion, source, isLast }) {
  const [expanded, setExpanded] = useState(false)
  const [showPrototypeModal, setShowPrototypeModal] = useState(false)
  const [modalScale, setModalScale] = useState(1)
  const isVideo = docType === 'promo_video' || docType === 'explainer_video'
  const [videoUrl, setVideoUrl] = useState(null)

  // For promo/explainer rows, load the uploaded MP4 (auth'd blob -> object URL)
  // when expanded; 404 = no video uploaded -> leave the player hidden. The URL
  // is revoked on collapse / unmount via the effect cleanup.
  useEffect(() => {
    if (!expanded || !isVideo) { setVideoUrl(null); return }
    let url = null, cancelled = false
    agentsApi.productKitVideoBlob(changeId, docType)
      .then(r => { if (!cancelled) { url = URL.createObjectURL(r.data); setVideoUrl(url) } })
      .catch(() => {})
    return () => { cancelled = true; if (url) URL.revokeObjectURL(url); setVideoUrl(null) }
  }, [expanded, isVideo, changeId, docType])

  useEffect(() => {
    if (!showPrototypeModal) return
    const onKey = (e) => { if (e.key === 'Escape') setShowPrototypeModal(false) }
    const recomputeScale = () => {
      const fitH = (window.innerHeight - 64) / 780
      const fitW = (window.innerWidth - 64) / 390
      setModalScale(Math.max(0.4, Math.min(1, fitH, fitW)))
    }
    recomputeScale()
    window.addEventListener('keydown', onKey)
    window.addEventListener('resize', recomputeScale)
    return () => {
      window.removeEventListener('keydown', onKey)
      window.removeEventListener('resize', recomputeScale)
    }
  }, [showPrototypeModal])

  const { data, isLoading } = useQuery({
    queryKey: ['product-kit-doc', changeId, docType, negVersion],
    queryFn:  () => agentsApi.productKitDoc(changeId, docType, negVersion).then(r => r.data),
    enabled:  expanded && hasContent,
  })

  // Lazily fetch validation only when expanded (avoids N+1 on collapsed list)
  const { data: validation } = useArtifactValidation(
    changeId, 'product_kit', expanded && hasContent && Boolean(data?.content), docType,
  )

  const content = data?.content

  const handleDownload = (e) => {
    e.stopPropagation()
    if (!content) return
    const ext = docType === 'prototype_screens' ? 'html'
              : docType === 'manifest' ? 'yaml'
              : 'md'
    const blob = new Blob([content], { type: 'text/plain' })
    const url  = URL.createObjectURL(blob)
    const a    = document.createElement('a')
    a.href = url
    a.download = `${docType}_v${version || 1}.${ext}`
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div style={{ borderBottom: isLast ? 'none' : '1px solid var(--border-subtle)' }}>
      {/* Row header */}
      <div
        onClick={() => hasContent && setExpanded(e => !e)}
        style={{
          display: 'flex', alignItems: 'center', gap: '10px',
          padding: '10px 20px',
          cursor: hasContent ? 'pointer' : 'default',
          transition: 'background 0.15s',
        }}
        onMouseEnter={e => { if (hasContent) e.currentTarget.style.background = 'var(--bg-card)' }}
        onMouseLeave={e => { e.currentTarget.style.background = 'transparent' }}
      >
        <div style={{ flexShrink: 0 }}>
          {hasContent
            ? <CheckCircle size={15} style={{ color: 'var(--success)' }} />
            : <Circle size={15} style={{ color: 'var(--border)' }} />
          }
        </div>
        <div style={{ flex: 1 }}>
          <p style={{
            margin: 0, fontSize: '13px', fontWeight: '500',
            color: hasContent ? 'var(--text-primary)' : 'var(--text-muted)',
          }}>
            {DOC_LABELS[docType] || docType}
          </p>
          {hasContent && (
            <p style={{ margin: 0, fontSize: '11px', color: 'var(--text-muted)' }}>Kit v{kitVersion || version} · Click to view</p>
          )}
        </div>
        {source === 'uploaded' && <UploadedBadge source={source} />}
        {hasContent && (
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            {expanded && content && (
              <>
                {/* Rendered .docx/.pptx/.xlsx exports always reflect the latest
                    kit; hide them when viewing an earlier (read-only) snapshot so
                    they can't silently hand back the wrong version. */}
                {negVersion == null && (
                  <>
                    <DocxDownloadButton changeId={changeId} docType="product_kit" subtype={docType} label=".docx" />
                    {docType === 'product_deck' && (
                      <PptxDownloadButton changeId={changeId} docType="product_kit" subtype="product_deck" label=".pptx" />
                    )}
                    {docType === 'cert_test_cases' && (
                      <XlsxDownloadButton changeId={changeId} label=".xlsx" />
                    )}
                  </>
                )}
                <button
                  onClick={handleDownload}
                  title="Download raw markdown/html/yaml"
                  style={{
                    display: 'flex', alignItems: 'center', gap: '4px',
                    padding: '4px 10px', borderRadius: '6px',
                    background: 'var(--bg-elevated)', border: '1px solid var(--border)',
                    color: 'var(--text-muted)', fontSize: '11px', cursor: 'pointer',
                  }}
                >
                  <Download size={11} /> Raw
                </button>
              </>
            )}
            <div style={{ color: 'var(--text-muted)' }}>
              {expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
            </div>
          </div>
        )}
      </div>

      {/* Expanded content */}
      {expanded && (
        <div style={{ borderTop: '1px solid var(--border-subtle)', background: 'var(--bg-base)', padding: '16px 20px 20px' }}>
          {isLoading ? (
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: 'var(--text-muted)', fontSize: '13px' }}>
              <Loader size={13} style={{ animation: 'spin 1s linear infinite' }} /> Loading…
            </div>
          ) : content ? (
            <div style={{
              background: 'var(--bg-card)', border: '1px solid var(--border-subtle)',
              borderRadius: '8px', padding: '20px 24px',
            }}>
              {validation && validation.issues && validation.issues.length > 0 && (
                <ValidationPanel validation={validation} hideErrors />
              )}
              {isVideo && videoUrl && (
                <video
                  src={videoUrl}
                  controls
                  style={{ width: '100%', maxHeight: 360, borderRadius: 8, background: '#000', display: 'block', marginBottom: 16 }}
                />
              )}
              {docType === 'prototype_screens' ? (
                <div style={{ display: 'flex', justifyContent: 'center', padding: '8px 0' }}>
                  <div style={{ position: 'relative', width: 234, height: 468 }}>
                    <div style={{
                      width: 390, height: 780,
                      borderRadius: 36, padding: 12,
                      background: '#111', boxShadow: '0 12px 40px rgba(0,0,0,0.35)',
                      position: 'absolute', top: 0, left: 0,
                      transform: 'scale(0.6)', transformOrigin: 'top left',
                    }}>
                      <div style={{
                        position: 'absolute', top: 12, left: '50%', transform: 'translateX(-50%)',
                        width: 110, height: 22, background: '#111', borderRadius: 14, zIndex: 2,
                      }} />
                      <iframe
                        title="Prototype preview"
                        srcDoc={hardenFrameHtml(content)}
                        sandbox="allow-scripts"
                        style={{
                          width: '100%', height: '100%',
                          border: 'none', borderRadius: 28,
                          background: '#fff', display: 'block',
                        }}
                      />
                    </div>
                    <button
                      onClick={() => setShowPrototypeModal(true)}
                      title="Open full-size preview"
                      style={{
                        position: 'absolute', top: 4, right: 4, zIndex: 3,
                        padding: '6px 10px', background: 'rgba(255,255,255,0.94)',
                        border: '1px solid var(--border)', borderRadius: 6,
                        cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 5,
                        fontSize: 11, fontWeight: 600, color: 'var(--text-primary)',
                        boxShadow: '0 2px 6px rgba(0,0,0,0.15)',
                      }}
                    >
                      <Maximize2 size={12} /> Expand
                    </button>
                  </div>
                </div>
              ) : docType === 'cert_test_cases' ? (
                <CertTestCasesView markdown={content} />
              ) : (
                <div className="md-content" style={{ fontSize: '13px', lineHeight: '1.7' }}>
                  <ReactMarkdown>{content}</ReactMarkdown>
                </div>
              )}
            </div>
          ) : (
            <p style={{ margin: 0, fontSize: '13px', color: 'var(--text-muted)' }}>No content available.</p>
          )}
        </div>
      )}
      {showPrototypeModal && content && (
        <div
          onClick={() => setShowPrototypeModal(false)}
          style={{
            position: 'fixed', inset: 0, zIndex: 1000,
            background: 'rgba(0,0,0,0.72)',
            overflow: 'auto',
            display: 'flex',
            padding: 24, backdropFilter: 'blur(2px)',
          }}
        >
          <div onClick={e => e.stopPropagation()} style={{
            position: 'relative', margin: 'auto',
            width: 390 * modalScale, height: 780 * modalScale,
          }}>
            <button
              onClick={() => setShowPrototypeModal(false)}
              title="Close (Esc)"
              aria-label="Close prototype preview"
              style={{
                position: 'absolute', top: -14, right: -14, zIndex: 2,
                width: 36, height: 36, borderRadius: '50%',
                background: '#fff', border: 'none', cursor: 'pointer',
                boxShadow: '0 4px 14px rgba(0,0,0,0.4)',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
              }}
            >
              <X size={16} />
            </button>
            <div style={{
              width: 390, height: 780,
              borderRadius: 36, padding: 12,
              background: '#111', boxShadow: '0 24px 60px rgba(0,0,0,0.6)',
              position: 'absolute', top: 0, left: 0,
              transform: `scale(${modalScale})`, transformOrigin: 'top left',
            }}>
              <div style={{
                position: 'absolute', top: 12, left: '50%', transform: 'translateX(-50%)',
                width: 110, height: 22, background: '#111', borderRadius: 14, zIndex: 2,
              }} />
              <iframe
                title="Prototype preview (expanded)"
                srcDoc={hardenFrameHtml(content)}
                sandbox="allow-scripts"
                style={{
                  width: '100%', height: '100%',
                  border: 'none', borderRadius: 28,
                  background: '#fff', display: 'block',
                }}
              />
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

// ProductKit expanded detail
export function ProductKitDetail({ changeId }) {
  // null = follow the latest snapshot; a number pins an earlier kit version.
  const [selectedVersion, setSelectedVersion] = useState(null)
  const { data, isLoading } = useQuery({
    queryKey: ['product-kit', changeId, selectedVersion],
    queryFn:  () => agentsApi.productKitAll(changeId, selectedVersion).then(r => r.data),
  })

  if (isLoading) return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '16px', color: 'var(--text-muted)', fontSize: '13px' }}>
      <Loader size={14} style={{ animation: 'spin 1s linear infinite' }} /> Loading Product Kit…
    </div>
  )

  const documents  = data?.documents || []
  const generated  = documents.filter(d => d.has_content).length
  const docMap     = Object.fromEntries(documents.map(d => [d.doc_type, d]))
  const docTypeOrder = Object.keys(DOC_LABELS)
  const kitVersion  = data?.negotiation_version || 1          // version being shown
  const currentVersion = data?.current_version || kitVersion  // newest available
  const versions = data?.available_versions || [kitVersion]
  const isLatest = kitVersion === currentVersion

  return (
    <div>
      <div style={{
        padding: '10px 20px', borderBottom: '1px solid var(--border-subtle)',
        display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '10px',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' }}>
          <p style={{ margin: 0, fontSize: '11px', fontWeight: '600', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
            {generated} / {docTypeOrder.length} documents generated
          </p>
          {versions.length > 1 ? (
            <span style={{ display: 'inline-flex', gap: '2px', border: '1px solid var(--border)', borderRadius: '5px', padding: '2px', background: 'var(--bg-elevated)' }}>
              {versions.map(v => {
                const active = v === kitVersion
                return (
                  <button
                    key={v}
                    onClick={() => setSelectedVersion(v === currentVersion ? null : v)}
                    title={v === currentVersion ? `Kit v${v} (latest)` : `Kit v${v}`}
                    style={{
                      fontSize: '10px', fontWeight: '600', cursor: 'pointer',
                      border: 'none', borderRadius: '3px', padding: '2px 8px',
                      background: active ? 'var(--accent)' : 'transparent',
                      color: active ? 'white' : 'var(--text-muted)',
                    }}
                  >
                    v{v}{v === currentVersion ? ' ★' : ''}
                  </button>
                )
              })}
            </span>
          ) : (
            <span style={{
              fontSize: '10px', fontWeight: '600', color: 'var(--accent)',
              background: 'var(--bg-elevated)', border: '1px solid var(--border)',
              borderRadius: '4px', padding: '1px 6px', whiteSpace: 'nowrap',
            }}>
              Kit v{kitVersion} (latest)
            </span>
          )}
        </div>
        <p style={{ margin: 0, fontSize: '11px', color: 'var(--text-muted)' }}>
          {isLatest ? 'Click a document to view its content' : `Viewing kit v${kitVersion} (read-only)`}
        </p>
      </div>
      {docTypeOrder.map((docType, i) => {
        const doc = docMap[docType] || {}
        return (
          <ProductKitDocRow
            key={`${docType}-${kitVersion}`}
            changeId={changeId}
            docType={docType}
            hasContent={!!doc.has_content}
            version={doc.version || 0}
            kitVersion={doc.negotiation_version || kitVersion}
            negVersion={selectedVersion}
            source={doc.source}
            isLast={i === docTypeOrder.length - 1}
          />
        )
      })}
    </div>
  )
}

// ── Pipeline progress bar ─────────────────────────────────────────────────────

function PipelineBar({ stages, currentStatus }) {
  const order = stages.map(s => s.key)
  const doneCount = currentStatus === 'completed'
    ? stages.length
    : stages.filter(s => getStageState(s.key, currentStatus, order) === 'done').length
  const activeIndex = currentStatus === 'completed'
    ? -1
    : stages.findIndex(s => getStageState(s.key, currentStatus, order) === 'active')
  const pct = Math.round((doneCount / stages.length) * 100)

  // The connector fills up to and including the active node center
  // Each node sits at position i / (n-1) along the bar
  const n = stages.length
  const fillPct = currentStatus === 'completed'
    ? 100
    : activeIndex >= 0
      ? (activeIndex / (n - 1)) * 100
      : (doneCount > 0 ? ((doneCount - 1) / (n - 1)) * 100 : 0)

  return (
    <div style={{ padding: '20px 24px 16px' }}>
      {/* Nodes + connector track */}
      <div style={{ position: 'relative', display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between' }}>

        {/* Grey track */}
        <div style={{
          position: 'absolute',
          top: '15px',
          left: `${(0.5 / n) * 100}%`,
          right: `${(0.5 / n) * 100}%`,
          height: '2px',
          background: 'var(--border)',
          borderRadius: '2px',
        }} />

        {/* Filled track */}
        <div style={{
          position: 'absolute',
          top: '15px',
          left: `${(0.5 / n) * 100}%`,
          width: `calc(${fillPct}% * (1 - ${1 / n}))`,
          height: '2px',
          background: 'linear-gradient(90deg, var(--success), var(--accent))',
          borderRadius: '2px',
          transition: 'width 0.4s ease',
        }} />

        {stages.map((stage) => {
          const state = getStageState(stage.key, currentStatus, order)
          const Icon = stage.icon || Circle
          const isActive = state === 'active'

          const nodeBg =
            state === 'done'   ? 'var(--success)' :
            state === 'active' ? 'var(--accent)'  : 'var(--bg-elevated)'
          const nodeBorder =
            state === 'done'   ? 'var(--success)' :
            state === 'active' ? 'var(--accent)'  : 'var(--border)'
          const nodeColor =
            state === 'pending' ? 'var(--border)' : 'white'
          const labelColor =
            state === 'done'   ? 'var(--success)' :
            state === 'active' ? 'var(--accent)'  : 'var(--text-muted)'

          return (
            <div
              key={stage.key}
              style={{
                flex: 1,
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                gap: '6px',
                zIndex: 1,
              }}
            >
              {/* Node circle */}
              <div style={{
                width: '30px',
                height: '30px',
                borderRadius: '50%',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                background: nodeBg,
                border: `2px solid ${nodeBorder}`,
                boxShadow: isActive ? `0 0 0 4px rgba(218,119,86,0.15)` : 'none',
                transition: 'all 0.25s ease',
                flexShrink: 0,
              }}>
                {state === 'done' ? (
                  <CheckCircle size={14} color="white" />
                ) : state === 'active' ? (
                  <Clock size={14} color="white" style={{ animation: 'spin 2s linear infinite' }} />
                ) : (
                  <Icon size={13} color={nodeColor} />
                )}
              </div>

              {/* Short label */}
              <span style={{
                fontSize: '10px',
                fontWeight: isActive ? '600' : '400',
                color: labelColor,
                textAlign: 'center',
                whiteSpace: 'nowrap',
                lineHeight: '1.3',
              }}>
                {stage.shortLabel}
              </span>
            </div>
          )
        })}
      </div>

      {/* Progress summary bar */}
      <div style={{ marginTop: '14px' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '5px' }}>
          <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>
            {currentStatus === 'completed'
              ? 'All stages completed'
              : `${doneCount} of ${stages.length} stages completed`}
          </span>
          <span style={{
            fontSize: '11px',
            fontWeight: '600',
            color: pct === 100 ? 'var(--success)' : 'var(--accent)',
          }}>
            {pct}%
          </span>
        </div>
        <div style={{
          height: '4px',
          background: 'var(--border)',
          borderRadius: '4px',
          overflow: 'hidden',
        }}>
          <div style={{
            height: '100%',
            width: `${pct}%`,
            background: pct === 100
              ? 'var(--success)'
              : 'linear-gradient(90deg, var(--success), var(--accent))',
            borderRadius: '4px',
            transition: 'width 0.4s ease',
          }} />
        </div>
      </div>
    </div>
  )
}

// ── Stage row ─────────────────────────────────────────────────────────────────


function StageRow({ stage, state, changeId, enhancedPrompt, isLast }) {
  const navigate = useNavigate()
  const [expanded, setExpanded] = useState(false)
  const canExpand = state === 'done' && EXPANDABLE_STAGES.includes(stage.key)
  const canNavigate = state === 'active'
  const isClickable = canExpand || canNavigate
  const Icon = stage.icon || Circle

  const handleRowClick = () => {
    if (canNavigate) {
      const path = stage.key === 'clarification' ? 'clarify' : stage.key
      navigate(`/changes/${changeId}/${path}`)
    } else if (canExpand) {
      setExpanded(e => !e)
    }
  }

  return (
    <div style={{
      borderBottom: isLast ? 'none' : '1px solid var(--border-subtle)',
    }}>
      {/* Row header */}
      <div
        onClick={handleRowClick}
        style={{
          display: 'flex', alignItems: 'center', gap: '14px',
          padding: '12px 20px',
          cursor: isClickable ? 'pointer' : 'default',
          background: state === 'active' ? 'rgba(218,119,86,0.06)' : 'transparent',
          transition: 'background 0.15s',
        }}
        onMouseEnter={e => { if (isClickable) e.currentTarget.style.background = 'var(--bg-card)' }}
        onMouseLeave={e => { if (state !== 'active') e.currentTarget.style.background = 'transparent' }}
      >
        {/* Status icon */}
        <div style={{ flexShrink: 0 }}>
          {state === 'done' ? (
            <CheckCircle size={18} style={{ color: 'var(--success)' }} />
          ) : state === 'active' ? (
            <Clock size={18} style={{ color: 'var(--accent)' }} />
          ) : (
            <Circle size={18} style={{ color: 'var(--border)' }} />
          )}
        </div>

        {/* Labels */}
        <div style={{ flex: 1 }}>
          <p style={{
            margin: '0 0 2px', fontSize: '13px', fontWeight: '500',
            color: state === 'pending' ? 'var(--text-muted)' : 'var(--text-primary)',
          }}>
            {stage.label}
          </p>
          <p style={{ margin: 0, fontSize: '11px', color: 'var(--text-muted)' }}>
            {stage.desc}
          </p>
        </div>

        {/* Right badges */}
        {(state === 'done' || state === 'active') && EXPANDABLE_STAGES.includes(stage.key) && (
          <button
            onClick={(e) => { e.stopPropagation(); navigate(`/changes/${changeId}/view/${stage.key}`) }}
            title="View details (read-only) — does not run the step"
            style={{
              display: 'inline-flex', alignItems: 'center', gap: '5px', flexShrink: 0,
              fontSize: '11px', fontWeight: '500', cursor: 'pointer',
              padding: '3px 10px', borderRadius: '6px', whiteSpace: 'nowrap',
              border: '1px solid var(--border)', background: 'var(--bg-elevated)', color: 'var(--text-muted)',
            }}
            onMouseEnter={e => { e.currentTarget.style.borderColor = 'var(--accent)'; e.currentTarget.style.color = 'var(--accent)' }}
            onMouseLeave={e => { e.currentTarget.style.borderColor = 'var(--border)'; e.currentTarget.style.color = 'var(--text-muted)' }}
          >
            <Eye size={13} /> View
          </button>
        )}
        {state === 'active' && (
          <span style={{
            fontSize: '11px', padding: '2px 10px', borderRadius: '20px',
            background: 'rgba(218,119,86,0.12)', color: 'var(--accent)',
            border: '1px solid rgba(218,119,86,0.3)', fontWeight: '500', whiteSpace: 'nowrap',
          }}>
            In Progress
          </span>
        )}
        {canExpand && (
          <div style={{ color: 'var(--text-muted)', flexShrink: 0 }}>
            {expanded ? <ChevronUp size={15} /> : <ChevronDown size={15} />}
          </div>
        )}
      </div>

      {/* Expanded detail panel */}
      {canExpand && expanded && (
        <div style={{ borderTop: '1px solid var(--border-subtle)', background: 'var(--bg-base)' }}>
          {stage.key === 'prompt_enhancement' && (
            <PromptEnhancementDetail changeId={changeId} enhancedPrompt={enhancedPrompt} />
          )}
          {stage.key === 'research' && (
            <ResearchDetail changeId={changeId} />
          )}
          {stage.key === 'canvas' && (
            <CanvasDetail changeId={changeId} />
          )}
          {stage.key === 'clarification' && (
            <ClarificationStageDetail changeId={changeId} />
          )}
          {stage.key === 'brd' && (
            <BRDDetail changeId={changeId} />
          )}
          {stage.key === 'tech_spec' && (
            <TechSpecDetail changeId={changeId} />
          )}
          {stage.key === 'xsd' && (
            <XSDDetail changeId={changeId} />
          )}
          {stage.key === 'product_kit' && (
            <ProductKitDetail changeId={changeId} />
          )}
        </div>
      )}
    </div>
  )
}

// ── Certification — a top-level stage beside Phases A/B/C ────────────────────
// One-click certification per assigned partner. Replaces the old "Push to
// Cert Environment" card (the cert-agent tc_store LLM importer, a legacy
// external-engine sync) with the REAL dispatch: the harness-agnostic seam
// (`cert/dispatch` → certification_dispatch.run_certification), so the domain
// pack's declared harness runs with production semantics — round pack built
// and published, partner's case class announced over A2A, verdict finalized
// by the join when the partner's reports land.
//
// The "as <role>" selector scopes the round to the sheet of the change's cert
// workbook that THIS actor executes; roles come from the active domain pack's
// cert vocabulary via /config/ui (never hardcoded here).
function CertificationPanel({ changeId }) {
  const qc = useQueryClient()
  const [busyPartner, setBusyPartner] = useState(null)
  const [dispatchError, setDispatchError] = useState(null)
  const [role, setRole] = useState('')
  // Evidence drill-down: which (partner, run) is open, and which case row.
  const [openRun, setOpenRun] = useState(null)          // {partnerId, runId} | null
  const [openCase, setOpenCase] = useState(null)

  const { data: phaseC } = useQuery({
    queryKey: ['phase-c-status-detail', changeId],
    queryFn: () => phaseCApi.status(changeId).then(r => r.data),
    // Polled for the same reason the runs list below is: the partner's status
    // advances on ITS report arriving over A2A, not on anything this page did,
    // so without this the pill sits on 'certifying' until a manual refresh.
    refetchInterval: 5000,
  })
  const partners = phaseC?.partners || []

  const { data: uiConfig } = useQuery({
    queryKey: ['ui-config'],
    queryFn: () => uiConfigApi.get(),
    staleTime: 300000,
  })
  const certRoles = uiConfig?.cert_roles || []
  useEffect(() => {
    if (!role && certRoles.length) setRole(certRoles[0].key)
  }, [certRoles, role])

  // Latest runs per partner — polled while any round is still RUNNING, since
  // partner-initiated cases report asynchronously over A2A and the verdict
  // lands a few seconds after dispatch.
  const partnerIds = partners.map(p => p.partner_id).join(',')
  const { data: runsByPartner } = useQuery({
    queryKey: ['cert-stage-runs', changeId, partnerIds],
    enabled: partners.length > 0,
    queryFn: async () => {
      const out = {}
      await Promise.all(partners.map(async p => {
        try {
          const r = await phaseCApi.listCertRuns(changeId, p.partner_id)
          out[p.partner_id] = r.data || []
        } catch { out[p.partner_id] = [] }
      }))
      return out
    },
    // Plain interval, deliberately not conditional: a round can complete
    // inside the first tick and a condition latched on stale data stops
    // polling exactly when the verdict is about to land. The list call is
    // cheap and the panel unmounts with the page.
    refetchInterval: 5000,
  })

  const { data: openRunDetail } = useQuery({
    queryKey: ['cert-run-evidence', changeId, openRun?.partnerId, openRun?.runId],
    queryFn: () => phaseCApi.getCertRun(changeId, openRun.partnerId, openRun.runId).then(r => r.data),
    enabled: !!openRun,
    // The per-case rows are the LAST thing to settle: each verdict arrives on
    // its own A2A report, seconds after the round is dispatched. The runs list
    // above already polls, so without this the header counter ticked to 6/6
    // while the case list under it still read ERROR until a page refresh.
    refetchInterval: 5000,
  })

  // Follow the newest round. Re-running (in particular re-running AS A DIFFERENT
  // ROLE) creates a new run id with a different case set, but `openRun` still
  // held the previous one — so the evidence list kept showing the old role's
  // cases and only a refresh revealed the new ones. Only the latest run is
  // openable from the button above, so following it never yanks the operator
  // away from a round they deliberately opened.
  useEffect(() => {
    if (!openRun) return
    const latestId = ((runsByPartner || {})[openRun.partnerId] || [])[0]?.id
    if (latestId && latestId !== openRun.runId) {
      setOpenRun({ partnerId: openRun.partnerId, runId: latestId })
      setOpenCase(null)
    }
  }, [runsByPartner, openRun])

  const onDispatch = async (partnerId) => {
    setBusyPartner(partnerId)
    setDispatchError(null)
    try {
      await phaseCApi.dispatchCert(changeId, partnerId, { role, advance: true })
      qc.invalidateQueries({ queryKey: ['cert-stage-runs', changeId] })
      qc.invalidateQueries({ queryKey: ['phase-c-status-detail', changeId] })
    } catch (e) {
      setDispatchError(e?.response?.data?.detail || e.message || 'dispatch failed')
    } finally {
      setBusyPartner(null)
    }
  }

  const pill = (label, color) => (
    <span style={{
      fontSize: 11, fontWeight: 600, padding: '2px 10px', borderRadius: 20,
      color, border: '1px solid currentColor',
    }}>{label}</span>
  )

  return (
    <div style={{
      background: 'var(--bg-elevated)', border: '1px solid var(--border)',
      borderRadius: '10px', overflow: 'hidden', marginTop: '24px',
    }}>
      <div style={{
        padding: '14px 20px', borderBottom: '1px solid var(--border-subtle)',
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
      }}>
        <div>
          <h2 style={{ margin: '0 0 2px', fontSize: '13px', fontWeight: 600, color: 'var(--text-primary)' }}>
            Certification
          </h2>
          <p style={{ margin: 0, fontSize: '11px', color: 'var(--text-muted)' }}>
            Dispatch a certification round per partner · graded against this change's contract pack · verdict lands when the partner's cases report
          </p>
        </div>
        {partners.length > 0 && partners.every(p => p.status === 'certified') &&
          pill('All Certified', 'var(--success)')}
      </div>

      <div style={{ padding: '16px 20px', display: 'flex', flexDirection: 'column', gap: 12 }}>
        {partners.length === 0 && (
          <p style={{ margin: 0, fontSize: 12, color: 'var(--text-muted)' }}>
            No partners assigned yet — assign and communicate the change in Phase C first.
          </p>
        )}

        {partners.map(p => {
          const runs = (runsByPartner || {})[p.partner_id] || []
          const latest = runs[0]
          const running = latest?.status === 'running'
          const busy = busyPartner === p.partner_id
          return (
            <div key={p.partner_id} style={{
              display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap',
              padding: '10px 14px', borderRadius: 8,
              background: 'var(--bg-base)', border: '1px solid var(--border-subtle)',
            }}>
              <Shield size={14} style={{ color: p.status === 'certified' ? 'var(--success)' : 'var(--accent)', flexShrink: 0 }} />
              <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-primary)' }}>{p.name}</span>
              {p.status === 'certified'
                ? pill('Certified', 'var(--success)')
                : pill(p.status?.replaceAll('_', ' ') || '—', 'var(--text-muted)')}

              <span style={{ flex: 1 }} />

              {latest && (
                <button
                  onClick={() => setOpenRun(openRun?.runId === latest.id ? null
                    : { partnerId: p.partner_id, runId: latest.id })}
                  title="Show per-case evidence: payloads, grading, pack"
                  style={{ fontSize: 11, color: 'var(--accent)', background: 'none',
                           border: '1px solid var(--border)', borderRadius: 4,
                           padding: '3px 8px', cursor: 'pointer' }}
                >
                  Run #{latest.run_number} · {latest.passed ?? 0}/{latest.total ?? 0}
                  {running ? ' · running…' : (latest.failed > 0 ? ` · ${latest.failed} failed` : ' · all pass')}
                  {' '}▾
                </button>
              )}

              {certRoles.length > 0 && (
                <label style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 11, color: 'var(--text-muted)' }}>
                  as
                  <select value={role} onChange={e => setRole(e.target.value)} disabled={busy}
                    style={{ fontSize: 11, padding: '4px 6px', borderRadius: 4,
                             background: 'var(--bg-elevated)', color: 'var(--text-primary)',
                             border: '1px solid var(--border)' }}>
                    {certRoles.map(r => <option key={r.key} value={r.key}>{r.label}</option>)}
                  </select>
                </label>
              )}

              <button
                onClick={() => onDispatch(p.partner_id)}
                disabled={busy || running}
                style={{
                  display: 'inline-flex', alignItems: 'center', gap: 8,
                  padding: '8px 18px',
                  background: (busy || running) ? 'var(--border)' : 'var(--accent)',
                  color: (busy || running) ? 'var(--text-muted)' : 'white',
                  border: 'none', borderRadius: 8, fontSize: 12, fontWeight: 600,
                  cursor: (busy || running) ? 'not-allowed' : 'pointer',
                }}
              >
                {(busy || running) ? <Loader size={13} className="spin" /> : <Upload size={13} />}
                {busy ? 'Dispatching…' : running ? 'Round running…'
                  : (runs.length ? 'Re-run Certification' : 'Run Certification')}
                {!busy && !running && <ArrowRight size={13} />}
              </button>
            </div>
          )
        })}

        {openRun && openRunDetail && (
          <div style={{ border: '1px solid var(--border)', borderRadius: 8, padding: '10px 14px',
                        background: 'var(--bg-base)' }}>
            <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-primary)', marginBottom: 6 }}>
              Run #{openRunDetail.run_number} evidence — {openRunDetail.passed}/{openRunDetail.total} passed
            </div>
            {(openRunDetail.results || []).map(r => {
              const d = (r.actual_response || {}).details || r.actual_response || {}
              const pay = d.payloads || {}
              const isOpen = openCase === r.id
              const mono = { fontFamily: 'monospace', fontSize: 10.5, whiteSpace: 'pre-wrap',
                             wordBreak: 'break-all', background: 'var(--bg-elevated)',
                             padding: '8px 10px', borderRadius: 6, margin: '4px 0 8px',
                             border: '1px solid var(--border-subtle)' }
              return (
                <div key={r.id} style={{ borderTop: '1px solid var(--border-subtle)', padding: '5px 0' }}>
                  <div onClick={() => setOpenCase(isOpen ? null : r.id)}
                       style={{ display: 'flex', alignItems: 'center', gap: 10, cursor: 'pointer' }}>
                    <span style={{ fontFamily: 'monospace', fontSize: 11.5, fontWeight: 600,
                                   color: 'var(--text-primary)' }}>{r.test_case_id}</span>
                    <span style={{ fontSize: 10, fontWeight: 700, textTransform: 'uppercase',
                                   color: r.status === 'pass' ? 'var(--success)'
                                        : r.status === 'fail' ? '#dc2626' : '#d97706' }}>
                      {r.status}
                    </span>
                    <span style={{ fontSize: 10.5, color: 'var(--text-muted)' }}>
                      {(r.actual_response || {}).reporter === 'bank' ? 'executed by partner' : r.direction}
                      {d.sim_pack ? ` · pack ${String(d.sim_pack).split(' ')[0]}` : ''}
                    </span>
                    <span style={{ flex: 1 }} />
                    <ChevronRight size={12} style={{ color: 'var(--text-muted)',
                                                     transform: isOpen ? 'rotate(90deg)' : 'none' }} />
                  </div>
                  {isOpen && (
                    <div style={{ fontSize: 11, color: 'var(--text-primary)', paddingLeft: 4 }}>
                      {(d.assertion_failures || []).length > 0 && (<>
                        <div style={{ fontWeight: 600, color: '#dc2626', margin: '6px 0 2px' }}>Grading failures</div>
                        {d.assertion_failures.map((f, i) =>
                          <div key={i} style={{ fontFamily: 'monospace', fontSize: 10.5 }}>• {typeof f === 'string' ? f : JSON.stringify(f)}</div>)}
                      </>)}
                      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, margin: '6px 0' }}>
                        <div>
                          <div style={{ fontWeight: 600, color: 'var(--text-muted)' }}>Expected</div>
                          <div style={mono}>{JSON.stringify(d.expected || r.expected_response || {}, null, 1)}</div>
                        </div>
                        <div>
                          <div style={{ fontWeight: 600, color: 'var(--text-muted)' }}>Observed</div>
                          <div style={mono}>{JSON.stringify(d.observed || {}, null, 1)}</div>
                        </div>
                      </div>
                      {pay.sut_request && (<>
                        <div style={{ fontWeight: 600, color: 'var(--text-muted)' }}>Request → partner application</div>
                        <div style={mono}>{pay.sut_request}</div>
                      </>)}
                      {pay.sut_response && (<>
                        <div style={{ fontWeight: 600, color: 'var(--text-muted)' }}>Partner application response (graded artifact)</div>
                        <div style={mono}>{pay.sut_response}</div>
                      </>)}
                      {pay.sim_response && (<>
                        <div style={{ fontWeight: 600, color: 'var(--text-muted)' }}>Simulator reply (HTTP {String(d.sim_status ?? '')})</div>
                        <div style={mono}>{pay.sim_response}</div>
                      </>)}
                      {!pay.sut_response && (
                        <div style={{ color: 'var(--text-muted)' }}>
                          No raw payloads on this row — rounds dispatched before payload
                          capture landed carry parsed evidence only.
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        )}

        {dispatchError && (
          <div style={{ display: 'flex', gap: 6, alignItems: 'flex-start',
                        padding: '8px 10px', borderRadius: 6, fontSize: 11,
                        background: 'rgba(220,38,38,0.08)', border: '1px solid rgba(220,38,38,0.25)',
                        color: 'var(--text-primary)' }}>
            <AlertTriangle size={12} style={{ color: '#dc2626', marginTop: 1, flexShrink: 0 }} />
            <span><strong>Dispatch refused.</strong> {String(dispatchError)}</span>
          </div>
        )}
      </div>
    </div>
  )
}


// ── Main page ─────────────────────────────────────────────────────────────────

export default function ChangeDetail() {
  const { id } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [resetting, setResetting] = useState(false)
  const [showDeleteModal, setShowDeleteModal] = useState(false)
  const { user: me } = useAuth()
  const isAdmin = me?.role === 'admin'
  // Whether THIS change was actually done by the agentic engine is a property of the
  // change (does a Phase-B 'code' run exist?), NOT of the viewer's role and NOT the
  // legacy `agentic_enabled` flag. Drives whether the overview claims the stages were
  // AI-written + build-verified. (403 for non-agentic roles → empty → legacy view.)
  const { data: codeRunsData } = useQuery({
    queryKey: ['code-runs', id],
    queryFn: () => agenticApi.listChangeRuns(id, 'code').then(r => r.data).catch(() => ({ runs: [] })),
    enabled: !!id,
  })
  const hasAgenticCodeRun = (codeRunsData?.runs || []).length > 0

  const { data: change, isLoading } = useQuery({
    queryKey: ['change', id],
    queryFn:  () => changesApi.get(id).then(r => r.data),
    staleTime: 0,
  })

  const { data: phaseBData } = useQuery({
    queryKey: ['phase-b', id],
    queryFn:  () => phaseBApi.get(id).then(r => r.data),
    retry: false,
    enabled: change?.status === 'completed',
  })

  const { data: phaseCStatus } = useQuery({
    queryKey: ['phase-c-status-detail', id],
    queryFn:  () => phaseCApi.status(id).then(r => r.data),
    retry: false,
    enabled: change?.status === 'completed',
    refetchInterval: 15000,
  })

  const { data: phaseCMessages } = useQuery({
    queryKey: ['phase-c-messages-detail', id],
    queryFn:  () => phaseCApi.messages(id).then(r => r.data),
    retry: false,
    enabled: change?.status === 'completed',
  })

  if (isLoading) return (
    <div style={{ padding: '32px', fontSize: '13px', color: 'var(--text-muted)' }}>Loading…</div>
  )
  if (!change) return (
    <div style={{ padding: '32px', fontSize: '13px', color: 'var(--danger)' }}>Change request not found.</div>
  )

  return (
    <div style={{ padding: '32px 40px', maxWidth: 1600, margin: '0 auto' }}>

      {/* Back */}
      <button
        onClick={() => navigate('/dashboard')}
        style={{
          display: 'flex', alignItems: 'center', gap: '6px',
          fontSize: '13px', color: 'var(--text-muted)',
          background: 'none', border: 'none', cursor: 'pointer',
          padding: 0, marginBottom: '24px',
        }}
        onMouseEnter={e => e.currentTarget.style.color = 'var(--text-primary)'}
        onMouseLeave={e => e.currentTarget.style.color = 'var(--text-muted)'}
      >
        <ArrowLeft size={14} /> Back to Dashboard
      </button>

      {/* Header */}
      <div style={{ marginBottom: '28px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap', marginBottom: '6px' }}>
          <h1 style={{ margin: 0, fontSize: '20px', fontWeight: '600', color: 'var(--text-primary)' }}>
            {change.title || 'Change Request'}
          </h1>
          {/* Change-id chip — click to copy; this id is also the log/transcript folder name */}
          <ChangeIdChip id={id} />
          <TranscriptsDownloadButton changeId={id} />
        </div>
        <p style={{ margin: '0 0 10px', fontSize: '13px', color: 'var(--text-muted)', lineHeight: '1.6' }}>
          {change.initial_prompt}
        </p>
      </div>

      {/* Phase A — Idea to Design */}
      <div style={{
        background: 'var(--bg-elevated)', border: '1px solid var(--border)',
        borderRadius: '10px', overflow: 'hidden', marginBottom: '24px',
      }}>
        <div style={{
          padding: '14px 20px', borderBottom: '1px solid var(--border-subtle)',
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        }}>
          <div>
            <h2 style={{ margin: '0 0 2px', fontSize: '13px', fontWeight: '600', color: 'var(--text-primary)' }}>
              Phase A — Idea to Design
            </h2>
            <p style={{ margin: 0, fontSize: '11px', color: 'var(--text-muted)' }}>
              Prompt enhancement · Research · Canvas · BRD · Tech Spec · XSD · Product Kit
            </p>
          </div>
          {change.status === 'completed' && (
            <span style={{
              fontSize: '11px', padding: '3px 10px', borderRadius: '20px', fontWeight: '500',
              background: 'rgba(76,175,125,0.1)', color: 'var(--success)',
              border: '1px solid rgba(76,175,125,0.3)',
            }}>
              Complete
            </span>
          )}
          {change.status !== 'completed' && (
            <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>
              Click a completed step to view details
            </span>
          )}
        </div>

        {/* Visual pipeline bar — stage order follows the change's workflow_version */}
        {(() => {
          const stages = stagesFor(change.workflow_version)
          const order = stages.map(s => s.key)
          return (
            <>
              <div style={{ borderBottom: '1px solid var(--border-subtle)', background: 'var(--bg-base)' }}>
                <PipelineBar stages={stages} currentStatus={change.status} />
              </div>
              {stages.map((stage, i) => (
                <StageRow
                  key={stage.key}
                  stage={stage}
                  state={getStageState(stage.key, change.status, order)}
                  changeId={id}
                  enhancedPrompt={change.enhanced_prompt}
                  isLast={i === stages.length - 1}
                />
              ))}
            </>
          )
        })()}
      </div>

      {/* Phase B — Design to Build */}
      {change.status === 'completed' && (
        <div style={{
          background: 'var(--bg-elevated)', border: '1px solid var(--border)',
          borderRadius: '10px', overflow: 'hidden',
        }}>
          <div style={{
            padding: '14px 20px', borderBottom: '1px solid var(--border-subtle)',
            display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          }}>
            <div>
              <h2 style={{ margin: '0 0 2px', fontSize: '13px', fontWeight: '600', color: 'var(--text-primary)' }}>
                Phase B — Design to Build
              </h2>
              <p style={{ margin: 0, fontSize: '11px', color: 'var(--text-muted)' }}>
                Code generation · Code review · Git / MR · Build · Deploy · UAT testing
              </p>
            </div>
            {phaseBData?.status === 'completed' && (
              <span style={{
                fontSize: '11px', padding: '3px 10px', borderRadius: '20px', fontWeight: '500',
                background: 'rgba(76,175,125,0.1)', color: 'var(--success)',
                border: '1px solid rgba(76,175,125,0.3)',
              }}>
                Complete
              </span>
            )}
            {phaseBData && phaseBData.status !== 'completed' && (
              <span style={{
                fontSize: '11px', padding: '3px 10px', borderRadius: '20px', fontWeight: '500',
                background: 'rgba(218,119,86,0.1)', color: 'var(--accent)',
                border: '1px solid rgba(218,119,86,0.3)',
              }}>
                In Progress
              </span>
            )}
            {phaseBData && (
              <button
                onClick={() => {
                  if (!window.confirm('Reset Phase B? This will delete all code iterations, reviews, and git events. Phase A artifacts are preserved.')) return
                  setResetting(true)
                  phaseBApi.reset(id).then(() => {
                    queryClient.invalidateQueries({ queryKey: ['phase-b', id] })
                    setResetting(false)
                  }).catch(() => setResetting(false))
                }}
                disabled={resetting}
                style={{
                  display: 'flex', alignItems: 'center', gap: '5px',
                  padding: '4px 12px', fontSize: '11px', fontWeight: '500',
                  background: 'transparent',
                  color: 'var(--text-muted)',
                  border: '1px solid var(--border)',
                  borderRadius: '6px', cursor: resetting ? 'not-allowed' : 'pointer',
                }}
              >
                {resetting ? 'Resetting…' : 'Reset Phase B'}
              </button>
            )}
          </div>

          {/* Phase B pipeline bar — shown when run exists */}
          {phaseBData && (
            <div style={{ borderBottom: '1px solid var(--border-subtle)', background: 'var(--bg-base)' }}>
              <PipelineBar
                stages={VISIBLE_PHASE_B_STAGES}
                currentStatus={phaseBData.status === 'completed' ? 'completed' : phaseBData.current_step}
              />
            </div>
          )}

          {/* Phase B step rows — expandable for completed steps */}
          {phaseBData && VISIBLE_PHASE_B_STAGES.map((stage, i) => (
            <PhaseBStageRow
              key={stage.key}
              stage={stage}
              state={getPhaseBStageState(stage.key, phaseBData.current_step, phaseBData.status)}
              changeId={id}
              agentic={hasAgenticCodeRun}
              isLast={i === VISIBLE_PHASE_B_STAGES.length - 1}
            />
          ))}

          {/* Action area */}
          <div style={{ padding: '16px 20px' }}>
            {!phaseBData && (
              <p style={{ margin: '0 0 16px', fontSize: '13px', color: 'var(--text-secondary)', lineHeight: '1.65' }}>
                Phase A artifacts (BRD, Tech Spec, XSD, Product Kit) are ready.
                Phase B will generate Java/Spring Boot code, run reviews,
                push to GitLab, build, deploy, and execute UAT tests.
              </p>
            )}
            {phaseBData?.status !== 'completed' && (
              <button
                onClick={() => navigate(`/changes/${id}/phase-b`)}
                style={{
                  display: 'flex', alignItems: 'center', gap: '8px',
                  padding: '10px 22px', background: 'var(--accent)', color: 'white',
                  border: 'none', borderRadius: '8px', fontSize: '13px', fontWeight: '600',
                  cursor: 'pointer',
                }}
              >
                <Code2 size={14} /> {phaseBData ? 'Continue Phase B' : 'Start Phase B'}
                <ArrowRight size={14} />
              </button>
            )}
          </div>
        </div>
      )}

      {/* Certification — a stage in its own right beside Phases A/B/C.
          Dispatches real certification rounds through the harness-agnostic
          seam (the domain pack's declared harness); gated on Phase A
          completion because the round's case set comes from the published
          cert workbook. */}
      {change.status === 'completed' && (
        <CertificationPanel changeId={id} />
      )}

      {/* Phase C — Partner Collaboration */}
      {change.status === 'completed' && (() => {
        const partners = phaseCStatus?.partners || []
        const totalPartners = partners.length
        const communicated = partners.filter(p => p.status !== 'assigned').length
        const ready = partners.filter(p => p.status === 'ready' || p.status === 'certified').length
        const certified = partners.filter(p => p.status === 'certified').length
        const pendingQueries = (phaseCMessages || []).filter(m => m.task_type === 'query' && m.status === 'submitted' && m.direction === 'inbound').length

        const STATUS_FLOW = ['assigned', 'communicated', 'acknowledged', 'in_progress', 'ready', 'certified']
        const STATUS_LABELS = { assigned: 'Assigned', communicated: 'Communicated', acknowledged: 'Acknowledged', in_progress: 'In Progress', ready: 'Ready', certified: 'Certified' }

        return (
          <div style={{
            background: 'var(--bg-elevated)', border: '1px solid var(--border)',
            borderRadius: '10px', overflow: 'hidden', marginTop: '24px',
          }}>
            {/* Header */}
            <div style={{
              padding: '14px 20px', borderBottom: '1px solid var(--border-subtle)',
              display: 'flex', alignItems: 'center', justifyContent: 'space-between',
            }}>
              <div>
                <h2 style={{ margin: '0 0 2px', fontSize: '13px', fontWeight: '600', color: 'var(--text-primary)' }}>
                  Phase C — Partner Collaboration
                </h2>
                <p style={{ margin: 0, fontSize: '11px', color: 'var(--text-muted)' }}>
                  A2A communication · Negotiation · Certification testing
                </p>
              </div>
              {pendingQueries > 0 && (
                <span style={{
                  fontSize: '11px', padding: '3px 10px', borderRadius: '20px', fontWeight: '600',
                  background: 'rgba(218,119,86,0.12)', color: 'var(--accent)',
                  border: '1px solid rgba(218,119,86,0.3)',
                }}>
                  {pendingQueries} pending quer{pendingQueries === 1 ? 'y' : 'ies'}
                </span>
              )}
              {certified === totalPartners && totalPartners > 0 && (
                <span style={{
                  fontSize: '11px', padding: '3px 10px', borderRadius: '20px', fontWeight: '500',
                  background: 'rgba(76,175,125,0.1)', color: 'var(--success)',
                  border: '1px solid rgba(76,175,125,0.3)',
                }}>
                  All Certified
                </span>
              )}
            </div>

            {/* Stats row */}
            {totalPartners > 0 && (
              <div style={{ display: 'flex', borderBottom: '1px solid var(--border-subtle)' }}>
                {[
                  { label: 'Partners', value: totalPartners },
                  { label: 'Communicated', value: communicated },
                  { label: 'Ready', value: ready },
                  { label: 'Certified', value: certified },
                ].map(s => (
                  <div key={s.label} style={{ flex: 1, padding: '12px 16px', textAlign: 'center', borderRight: '1px solid var(--border-subtle)' }}>
                    <p style={{ margin: '0 0 1px', fontSize: '18px', fontWeight: '700', color: 'var(--text-primary)' }}>{s.value}</p>
                    <p style={{ margin: 0, fontSize: '9px', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>{s.label}</p>
                  </div>
                ))}
              </div>
            )}

            {/* Partner rows with status */}
            {partners.map((p, i) => {
              const statusIdx = STATUS_FLOW.indexOf(p.status)
              return (
                <div key={p.partner_id} style={{
                  padding: '10px 20px', display: 'flex', alignItems: 'center', gap: '12px',
                  borderBottom: i < partners.length - 1 ? '1px solid var(--border-subtle)' : 'none',
                }}>
                  <span style={{ fontSize: '13px', color: 'var(--text-primary)', fontWeight: '500', flex: 1 }}>{p.name}</span>
                  <span style={{
                    fontSize: '10px', padding: '2px 8px', borderRadius: '4px', fontWeight: '600',
                    background: p.partner_type === 'bank' ? 'rgba(110,168,220,0.1)' : p.partner_type === 'psp' ? 'rgba(76,175,125,0.1)' : 'rgba(179,136,232,0.1)',
                    color: p.partner_type === 'bank' ? '#6ea8dc' : p.partner_type === 'psp' ? '#4caf7d' : '#b388e8',
                    textTransform: 'uppercase',
                  }}>{p.partner_type}</span>
                  {/* Mini status pipeline */}
                  <div style={{ display: 'flex', gap: '3px', alignItems: 'center' }}>
                    {STATUS_FLOW.map((s, si) => (
                      <div key={s} style={{
                        width: si <= statusIdx ? '18px' : '14px',
                        height: '4px',
                        borderRadius: '2px',
                        background: si <= statusIdx
                          ? (p.status === 'certified' ? 'var(--success)' : 'var(--accent)')
                          : 'var(--border)',
                        transition: 'all 0.3s',
                      }} title={STATUS_LABELS[s]} />
                    ))}
                  </div>
                  <span style={{
                    fontSize: '10px', padding: '2px 10px', borderRadius: '20px', fontWeight: '600', minWidth: '80px', textAlign: 'center',
                    background: p.status === 'certified' ? 'rgba(76,175,125,0.1)' : p.status === 'ready' ? 'rgba(76,175,125,0.08)' : 'var(--bg-base)',
                    color: p.status === 'certified' ? 'var(--success)' : p.status === 'ready' ? 'var(--success)' : 'var(--text-muted)',
                    border: `1px solid ${p.status === 'certified' || p.status === 'ready' ? 'rgba(76,175,125,0.3)' : 'var(--border)'}`,
                  }}>
                    {STATUS_LABELS[p.status] || p.status}
                  </span>
                </div>
              )
            })}

            {/* Action area */}
            <div style={{ padding: '14px 20px', borderTop: totalPartners > 0 ? '1px solid var(--border-subtle)' : 'none' }}>
              <button
                onClick={() => navigate(`/changes/${id}/phase-c`)}
                style={{
                  display: 'flex', alignItems: 'center', gap: '8px',
                  padding: '9px 20px', background: 'var(--accent)', color: 'white',
                  border: 'none', borderRadius: '7px', fontSize: '13px', fontWeight: '600',
                  cursor: 'pointer',
                }}
              >
                <Users size={14} /> {totalPartners > 0 ? 'Manage Phase C' : 'Start Phase C'}
                <ArrowRight size={14} />
              </button>
            </div>
          </div>
        )
      })()}

      {isAdmin && (
        <div style={{ marginTop: '40px', display: 'flex', justifyContent: 'flex-end' }}>
          <button
            onClick={() => setShowDeleteModal(true)}
            style={{
              display: 'flex', alignItems: 'center', gap: '6px',
              padding: '8px 16px', background: 'transparent',
              border: '1px solid var(--danger)', borderRadius: '6px',
              color: 'var(--danger)', fontSize: '13px', fontWeight: 500,
              cursor: 'pointer', whiteSpace: 'nowrap',
            }}
          >
            <Trash2 size={13} /> Delete change request
          </button>
        </div>
      )}

      {showDeleteModal && (
        <DeleteChangeModal
          change={change}
          onClose={() => setShowDeleteModal(false)}
          onDeleted={(result) => {
            setShowDeleteModal(false)
            // Best-effort toast via window.alert — replace with a real toast
            // component if/when the design system has one. The summary
            // counts are useful for an admin to verify scope.
            try {
              const s = result?.summary || {}
              const errCount = (s.errors || []).length
              const msg = `Change request deleted.\n` +
                          `agent_jobs cancelled: ${s.agent_jobs_cancelled ?? 0}, ` +
                          `deleted: ${s.agent_jobs_deleted ?? 0}\n` +
                          `document_chunks: ${s.document_chunks_deleted ?? 0}\n` +
                          `artifact dirs: ${s.artifact_dirs_removed ?? 0}\n` +
                          `redis buffers: ${s.redis_chunk_buffers_cleared ?? 0}\n` +
                          (errCount > 0 ? `non-fatal errors: ${errCount} (see backend log)\n` : '') +
                          `duration: ${s.duration_ms ?? 0}ms`
              alert(msg)
            } catch {
              alert('Change request deleted.')
            }
            // Invalidate the dashboard list so the deleted CR disappears.
            queryClient.invalidateQueries({ queryKey: ['changes'] })
            navigate('/dashboard')
          }}
        />
      )}
    </div>
  )
}
