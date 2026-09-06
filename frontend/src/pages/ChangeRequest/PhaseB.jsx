// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useState, useRef, useEffect, useCallback } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { changesApi, phaseBApi, agenticApi, governanceApi } from '../../services/api'
import { wsUrl } from '../../utils/basePath'
import { safeHref } from '../../utils/safeUrl'
import {
  isDemoBuildMode, streamDemoBuildLogs, buildDemoResult,
} from '../../lib/demoBuildLogs'
import {
  ArrowLeft, Code2, CheckCircle, Circle, Loader,
  Wifi, WifiOff, ChevronDown, ChevronUp, FileCode, RefreshCw,
  Database, Folder, ThumbsUp, Shield, Search, AlertTriangle, ArrowRight, RotateCcw,
} from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import AgenticPhasePanel from '../../components/AgenticPhasePanel'
import GovernanceStageCard from '../../components/GovernanceStageCard'
import TranscriptsDownloadButton from '../../components/TranscriptsDownloadButton'
import PhaseALockedNotice from '../../components/common/PhaseALockedNotice'
import { useAuth } from '../../hooks/useAuth'

// ── Pipeline steps ────────────────────────────────────────────────────────────

// Session 23 — Build + Deploy unified into one step. The host's
// build_and_deploy.sh clones, builds, deploys and starts services in
// a single SSH stream, so the standalone "Deploy" panel is gone. The
// underlying enum still has DEPLOY for legacy in-flight runs.
// UAT test-gen + test-exec are likewise ONE script-based step now (the
// operator's test script generates and executes the suite in one run) —
// keyed 'test_gen' because that is the backend step; a legacy run parked
// at 'test_exec' is normalised onto the same node (see stepAlias).
const STEPS = [
  { key: 'code_change',  label: 'Code Change',     shortLabel: 'Code' },
  { key: 'code_review',  label: 'Code Review',     shortLabel: 'Review' },
  { key: 'git',          label: 'Git / MR',        shortLabel: 'Git' },
  { key: 'build',        label: 'Build + Deploy',  shortLabel: 'Build' },
  { key: 'test_gen',     label: 'UAT Tests',       shortLabel: 'UAT' },
  { key: 'triage',       label: 'AI Triage',       shortLabel: 'Triage' },
]

// Backend steps that fold into one stepper node.
const stepAlias = (s) => (s === 'test_exec' ? 'test_gen' : s)

// Choosing WHICH script runs is gated server-side to these roles (the
// configured default script needs no elevation) — mirror it in the UI so
// other roles never see an input that can only 403.
const canPickScript = (user) => ['admin', 'tech_lead'].includes(user?.role)

// Render only the tail of a streaming log: the pane is a live view, not an
// archive (the full text stays on the row / in the diagnostics file), and
// rendering tens of thousands of lines per poll tick makes the very panel
// the operator is watching janky.
const LOG_TAIL_LINES = 1000
function logTail(text) {
  const all = (text || '').split('\n')
  if (all.length <= LOG_TAIL_LINES) return { lines: all, omitted: 0 }
  return { lines: all.slice(-LOG_TAIL_LINES), omitted: all.length - LOG_TAIL_LINES }
}

// Stick-to-bottom autoscroll: follow the stream only while the operator is
// already at the bottom — never yank the pane back down while they are
// scrolled up reading earlier output.
function stickToBottom(el) {
  if (!el) return
  const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80
  if (nearBottom) el.scrollTop = el.scrollHeight
}

// Governance-enabled variant: EA + InfoSec review stages sit between Git and Build.
const STEPS_GOV = [
  ...STEPS.slice(0, 3),
  { key: 'ea_review',      label: 'EA Review',      shortLabel: 'EA' },
  { key: 'infosec_review', label: 'InfoSec Review', shortLabel: 'InfoSec' },
  ...STEPS.slice(3),
]

const STEP_ORDER = STEPS.map(s => s.key)

function getStepState(stepKey, currentStep, order = STEP_ORDER) {
  if (currentStep === 'completed') return 'done'
  const si = order.indexOf(stepKey)
  const ci = order.indexOf(currentStep)
  if (si < ci)  return 'done'
  if (si === ci) return 'active'
  return 'pending'
}

// Map a live agentic run's phase → the Phase-B stepper step, so the bar advances
// Code → Review → Git as the agent works (before the legacy handover at Build).
// The agent owns workspace_ready…verification (Code), review (Review), then
// awaiting_human_approval…pushing (Git — the MR is the next/in-flight action).
function agenticBarStep(run) {
  switch (run?.phase) {
    case 'review':
      return 'code_review'
    case 'awaiting_human_approval':
    case 'pushing':
    case 'rebase_reverify':
      return 'git'
    default:   // created / workspace_ready / context_ready / xsd_discovery / code_change / verification
      return 'code_change'
  }
}

// ── Pipeline bar ──────────────────────────────────────────────────────────────

function PipelineBar({ currentStep, steps = STEPS }) {
  const order = steps.map(s => s.key)
  const n = steps.length
  const activeIndex = currentStep === 'completed' ? -1 : order.indexOf(currentStep)
  const doneCount = currentStep === 'completed' ? n
    : steps.filter(s => getStepState(s.key, currentStep, order) === 'done').length
  const pct = Math.round((doneCount / n) * 100)
  const fillPct = currentStep === 'completed' ? 100
    : activeIndex >= 0 ? (activeIndex / (n - 1)) * 100
    : 0

  return (
    <div style={{ padding: '20px 24px 8px' }}>
      {/* Nodes + track */}
      <div style={{ position: 'relative', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        {/* Grey track */}
        <div style={{
          position: 'absolute', top: '15px', left: `${(0.5 / n) * 100}%`,
          right: `${(0.5 / n) * 100}%`, height: '3px',
          background: 'var(--border)', borderRadius: '2px',
        }} />
        {/* Colour fill */}
        <div style={{
          position: 'absolute', top: '15px', left: `${(0.5 / n) * 100}%`,
          width: `${fillPct * (1 - 1 / n)}%`, height: '3px',
          background: 'linear-gradient(90deg, var(--success), var(--accent))',
          borderRadius: '2px', transition: 'width 0.4s ease',
        }} />
        {/* Step nodes */}
        {steps.map(step => {
          const state = getStepState(step.key, currentStep, order)
          return (
            <div key={step.key} style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '6px', zIndex: 1, flex: 1 }}>
              <div style={{
                width: 30, height: 30, borderRadius: '50%', display: 'flex',
                alignItems: 'center', justifyContent: 'center', fontSize: '11px', fontWeight: '700',
                background: state === 'done' ? 'var(--success)' : state === 'active' ? 'var(--accent)' : 'var(--bg-elevated)',
                border: `2px solid ${state === 'done' ? 'var(--success)' : state === 'active' ? 'var(--accent)' : 'var(--border)'}`,
                color: state === 'pending' ? 'var(--text-muted)' : 'white',
                boxShadow: state === 'active' ? '0 0 0 4px rgba(218,119,86,0.15)' : 'none',
              }}>
                {state === 'done'
                  ? <CheckCircle size={14} />
                  : state === 'active'
                    ? <Loader size={13} style={{ animation: 'spin 1.5s linear infinite' }} />
                    : <Circle size={12} />}
              </div>
              <span style={{ fontSize: '10px', color: state === 'pending' ? 'var(--text-muted)' : 'var(--text-secondary)', fontWeight: state === 'active' ? '600' : '400', textAlign: 'center', lineHeight: 1.2 }}>
                {step.shortLabel}
              </span>
            </div>
          )
        })}
      </div>
      {/* Progress bar */}
      <div style={{ marginTop: '16px', display: 'flex', alignItems: 'center', gap: '10px' }}>
        <span style={{ fontSize: '11px', color: 'var(--text-muted)', whiteSpace: 'nowrap' }}>
          {doneCount} of {n} steps
        </span>
        <div style={{ flex: 1, height: '4px', background: 'var(--border)', borderRadius: '2px', overflow: 'hidden' }}>
          <div style={{
            height: '100%', width: `${pct}%`,
            background: 'linear-gradient(90deg, var(--success), var(--accent))',
            borderRadius: '2px', transition: 'width 0.4s ease',
          }} />
        </div>
        <span style={{ fontSize: '11px', color: 'var(--accent)', fontWeight: '600', minWidth: '32px', textAlign: 'right' }}>{pct}%</span>
      </div>
    </div>
  )
}

// ── File tab viewer ───────────────────────────────────────────────────────────

function FileViewer({ files, runRepos }) {
  const [selected, setSelected] = useState(0)
  if (!files || files.length === 0) return null

  // M-5 — map repo_id → label (from the run summary). Lets us badge each
  // file with its target repo when multi-repo is in effect.
  const repoLabelById = {}
  ;(runRepos || []).forEach(r => { repoLabelById[r.repo_id] = r.label })

  const file = files[selected]
  const fileRepoLabel = file?.repo_id ? repoLabelById[file.repo_id] : null
  return (
    <div style={{ border: '1px solid var(--border)', borderRadius: '8px', overflow: 'hidden', marginTop: '16px' }}>
      {/* Tabs */}
      <div style={{ display: 'flex', overflowX: 'auto', background: 'var(--bg-base)', borderBottom: '1px solid var(--border)' }}>
        {files.map((f, i) => {
          const repoLabel = f.repo_id ? repoLabelById[f.repo_id] : null
          return (
            <button key={i} onClick={() => setSelected(i)} style={{
              padding: '8px 14px', fontSize: '11px', fontWeight: i === selected ? '600' : '400',
              color: i === selected ? 'var(--accent)' : 'var(--text-muted)',
              background: i === selected ? 'var(--bg-elevated)' : 'transparent',
              border: 'none', borderBottom: i === selected ? '2px solid var(--accent)' : '2px solid transparent',
              cursor: 'pointer', whiteSpace: 'nowrap', display: 'flex', alignItems: 'center', gap: '5px',
            }}>
              <FileCode size={11} />
              {f.path.split('/').pop()}
              {repoLabel && (
                <span style={{
                  fontSize: '9px', padding: '1px 6px', borderRadius: '8px',
                  background: 'rgba(218,119,86,0.12)', color: 'var(--accent)',
                  fontWeight: 600, marginLeft: 4,
                }}>
                  {repoLabel}
                </span>
              )}
            </button>
          )
        })}
      </div>
      {/* File path + repo */}
      <div style={{
        padding: '6px 14px', background: 'var(--bg-base)',
        borderBottom: '1px solid var(--border-subtle)',
        display: 'flex', alignItems: 'center', gap: '8px',
      }}>
        <span style={{ fontSize: '10px', color: 'var(--text-muted)', fontFamily: 'monospace' }}>
          {file.path}
        </span>
        {fileRepoLabel && (
          <span style={{
            fontSize: '10px', padding: '2px 8px', borderRadius: '10px',
            background: 'rgba(218,119,86,0.12)', color: 'var(--accent)',
            fontWeight: 600, marginLeft: 'auto',
          }}>
            → {fileRepoLabel}
          </span>
        )}
      </div>
      {/* Content */}
      <pre style={{
        margin: 0, padding: '16px', overflowX: 'auto', maxHeight: '500px', overflowY: 'auto',
        fontSize: '12px', lineHeight: '1.6', background: 'var(--bg-elevated)',
        color: 'var(--text-primary)', fontFamily: "'JetBrains Mono', 'Fira Code', 'Consolas', monospace",
      }}>
        <code>{file.content}</code>
      </pre>
    </div>
  )
}

// ── Code generation output ────────────────────────────────────────────────────

function parseMarkdownAndFiles(output) {
  if (!output) return { markdown: '', files: [] }
  const filePattern = /<<FILE:\s*(.+?)>>\n([\s\S]*?)<<END_FILE>>/g
  let markdown = output
  const files = []
  let match
  while ((match = filePattern.exec(output)) !== null) {
    files.push({ path: match[1].trim(), content: match[2].rstrip ? match[2].rstrip() : match[2] })
    markdown = markdown.replace(match[0], `> 📄 \`${match[1].trim()}\``)
  }
  return { markdown, files }
}

// ── Ingest codebase panel ─────────────────────────────────────────────────────

function IngestPanel({ changeId, onIngested }) {
  const [repo, setRepo] = useState('')
  const [branch, setBranch] = useState('main')
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)

  const handleIngest = async () => {
    if (loading) return
    setLoading(true)
    setError(null)
    try {
      const res = await phaseBApi.ingestCodebase(changeId, { gitlab_repo: repo || undefined, gitlab_branch: branch || undefined })
      setResult(res.data)
      onIngested?.()
    } catch (e) {
      setError(e.response?.data?.detail || 'Ingestion failed')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={{
      padding: '16px', background: 'var(--bg-elevated)', border: '1px solid var(--border)',
      borderRadius: '8px', marginBottom: '16px',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '12px' }}>
        <Database size={14} style={{ color: 'var(--accent)' }} />
        <span style={{ fontSize: '13px', fontWeight: '600', color: 'var(--text-primary)' }}>Ingest Codebase (Code RAG)</span>
        <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>— Optional: fetch Java source files to give the AI full codebase context</span>
      </div>
      <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
        <input
          value={repo} onChange={e => setRepo(e.target.value)}
          placeholder="GitLab repo (e.g. group/project) — uses env default if blank"
          style={{ flex: '2 1 260px', padding: '7px 12px', background: 'var(--bg-input)', border: '1px solid var(--border)', borderRadius: '6px', color: 'var(--text-primary)', fontSize: '12px' }}
        />
        <input
          value={branch} onChange={e => setBranch(e.target.value)}
          placeholder="Branch"
          style={{ flex: '1 1 100px', padding: '7px 12px', background: 'var(--bg-input)', border: '1px solid var(--border)', borderRadius: '6px', color: 'var(--text-primary)', fontSize: '12px' }}
        />
        <button onClick={handleIngest} disabled={loading} style={{
          padding: '7px 16px', background: loading ? 'var(--bg-elevated)' : 'var(--accent)',
          color: loading ? 'var(--text-muted)' : 'white', border: '1px solid var(--border)',
          borderRadius: '6px', fontSize: '12px', fontWeight: '500', cursor: loading ? 'not-allowed' : 'pointer',
          display: 'flex', alignItems: 'center', gap: '6px',
        }}>
          {loading ? <Loader size={12} style={{ animation: 'spin 1s linear infinite' }} /> : <Folder size={12} />}
          {loading ? 'Ingesting…' : 'Ingest'}
        </button>
      </div>
      {result && (
        <p style={{ margin: '8px 0 0', fontSize: '11px', color: 'var(--success)' }}>
          ✓ Ingested {result.files_fetched} files → {result.chunks_stored} chunks stored in Code RAG
        </p>
      )}
      {error && <p style={{ margin: '8px 0 0', fontSize: '11px', color: 'var(--danger)' }}>✗ {error}</p>}
    </div>
  )
}

// ── Severity badge ────────────────────────────────────────────────────────────

const SEVERITY_COLORS = {
  blocker:  { bg: '#4a0e0e', color: '#ff6b6b', border: '#ff6b6b' },
  critical: { bg: 'rgba(224,108,108,0.12)', color: 'var(--danger)', border: 'rgba(224,108,108,0.3)' },
  high:     { bg: 'rgba(224,108,108,0.12)', color: 'var(--danger)', border: 'rgba(224,108,108,0.3)' },
  major:    { bg: 'rgba(218,119,86,0.10)', color: 'var(--accent)', border: 'rgba(218,119,86,0.25)' },
  medium:   { bg: 'rgba(218,119,86,0.10)', color: 'var(--accent)', border: 'rgba(218,119,86,0.25)' },
  minor:    { bg: 'rgba(180,180,100,0.10)', color: '#b4b464', border: 'rgba(180,180,100,0.25)' },
  low:      { bg: 'rgba(180,180,100,0.10)', color: '#b4b464', border: 'rgba(180,180,100,0.25)' },
  info:     { bg: 'rgba(100,160,200,0.10)', color: '#64a0c8', border: 'rgba(100,160,200,0.25)' },
}

function SeverityBadge({ severity }) {
  const s = (severity || 'info').toLowerCase()
  const c = SEVERITY_COLORS[s] || SEVERITY_COLORS.info
  return (
    <span style={{
      padding: '2px 8px', borderRadius: '4px', fontSize: '10px', fontWeight: '600',
      textTransform: 'uppercase', letterSpacing: '0.04em',
      background: c.bg, color: c.color, border: `1px solid ${c.border}`,
    }}>{severity}</span>
  )
}

// ── Review panel (shared by Code Review + IS Review) ──────────────────────────

function ReviewPanel({ type, changeId, currentStep, onRefetchRun, onLoopBackComplete }) {
  const isCode   = type === 'code_review'
  const label    = isCode ? 'Code Review' : 'IS (Security) Review'
  const icon     = isCode ? <Search size={16} /> : <Shield size={16} />
  const stepKey  = isCode ? 'code_review' : 'is_review'
  const issuesKey = isCode ? 'issues' : 'findings'

  const [running, setRunning]   = useState(false)
  const [result, setResult]     = useState(null)
  const [looping, setLooping]   = useState(false)
  const [skipping, setSkipping] = useState(false)
  const [err, setErr]           = useState(null)

  // Load existing review result if step is past this one
  const { data: existingReview } = useQuery({
    queryKey: [`phase-b-${type}`, changeId],
    queryFn:  () => (isCode ? phaseBApi.getCodeReview(changeId) : phaseBApi.getISReview(changeId)).then(r => r.data),
    retry:    false,
    enabled:  getStepState(stepKey, currentStep) !== 'pending',
  })

  useEffect(() => {
    if (existingReview && !result) setResult(existingReview)
  }, [existingReview])

  const handleRun = async () => {
    setRunning(true); setErr(null)
    try {
      const res = isCode
        ? await phaseBApi.triggerCodeReview(changeId)
        : await phaseBApi.triggerISReview(changeId)
      setResult(res.data)
      onRefetchRun()
    } catch (e) {
      setErr(e.response?.data?.detail || `${label} failed`)
    } finally {
      setRunning(false)
    }
  }

  const handleLoopBack = async () => {
    setLooping(true)
    try {
      isCode
        ? await phaseBApi.codeReviewLoopBack(changeId)
        : await phaseBApi.isReviewLoopBack(changeId)
      await onRefetchRun()
      // Notify parent to reconnect WS and pre-fill feedback with review context
      const summary = items.map(
        (item, i) => `${i + 1}. [${item.severity}] ${item.file}${item.line ? ':' + item.line : ''} — ${item.message || item.description}`
      ).join('\n')
      onLoopBackComplete?.(label, summary)
    } catch (e) {
      setErr(e.response?.data?.detail || 'Loop back failed')
    } finally {
      setLooping(false)
    }
  }

  const state = getStepState(stepKey, currentStep)
  if (state === 'pending') return null

  const items = result?.[issuesKey] || result?.issues || result?.findings || []
  const isClean = result?.status === 'clean'
  const hasIssues = result?.status === 'issues_found'

  return (
    <div style={{
      marginBottom: '24px', background: 'var(--bg-elevated)',
      border: '1px solid var(--border)', borderRadius: '8px', overflow: 'hidden',
    }}>
      {/* Header */}
      <div style={{
        padding: '14px 20px', borderBottom: '1px solid var(--border-subtle)',
        display: 'flex', alignItems: 'center', gap: '10px',
      }}>
        <div style={{ color: isClean ? 'var(--success)' : hasIssues ? 'var(--danger)' : 'var(--accent)' }}>
          {icon}
        </div>
        <span style={{ fontSize: '14px', fontWeight: '600', color: 'var(--text-primary)', flex: 1 }}>
          {label}
        </span>
        {isClean && (
          <span style={{ fontSize: '11px', padding: '3px 10px', borderRadius: '20px', fontWeight: '600', background: 'rgba(76,175,125,0.1)', color: 'var(--success)', border: '1px solid rgba(76,175,125,0.3)' }}>
            <CheckCircle size={11} style={{ marginRight: 4, verticalAlign: 'text-bottom' }} />
            Clean — No issues
          </span>
        )}
        {hasIssues && (
          <span style={{ fontSize: '11px', padding: '3px 10px', borderRadius: '20px', fontWeight: '600', background: 'rgba(224,108,108,0.1)', color: 'var(--danger)', border: '1px solid rgba(224,108,108,0.3)' }}>
            <AlertTriangle size={11} style={{ marginRight: 4, verticalAlign: 'text-bottom' }} />
            {items.length} issue{items.length !== 1 ? 's' : ''} found
          </span>
        )}
        {state === 'active' && !result && (
          <button onClick={handleRun} disabled={running} style={{
            display: 'flex', alignItems: 'center', gap: '6px',
            padding: '7px 16px', background: 'var(--accent)', color: 'white',
            border: 'none', borderRadius: '6px', fontSize: '12px', fontWeight: '600',
            cursor: running ? 'not-allowed' : 'pointer', opacity: running ? 0.7 : 1,
          }}>
            {running ? <Loader size={12} style={{ animation: 'spin 1s linear infinite' }} /> : icon}
            {running ? 'Running…' : `Run ${label}`}
          </button>
        )}
      </div>

      {/* Body */}
      <div style={{ padding: '16px 20px' }}>
        {err && <p style={{ margin: '0 0 12px', fontSize: '12px', color: 'var(--danger)' }}>{err}</p>}

        {running && (
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '20px', justifyContent: 'center' }}>
            <Loader size={16} style={{ color: 'var(--accent)', animation: 'spin 1s linear infinite' }} />
            <span style={{ fontSize: '13px', color: 'var(--text-muted)' }}>Analysing code against {isCode ? 'SonarQube + PMD rules' : 'OWASP Top 10 + the Authority security standards'}…</span>
          </div>
        )}

        {!running && !result && state === 'active' && (
          <p style={{ margin: 0, fontSize: '13px', color: 'var(--text-muted)', textAlign: 'center', padding: '16px' }}>
            Click "Run {label}" above to start the automated review.
          </p>
        )}

        {/* Rules checked & stats summary — shown for code review */}
        {isCode && (result?.rules_checked || result?.stats) && (
          <div style={{ marginBottom: '16px' }}>
            {/* Stats cards */}
            {result?.stats && (
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: '10px', marginBottom: '12px' }}>
                <div style={{ padding: '10px 14px', borderRadius: '6px', background: 'var(--bg-elevated)', border: '1px solid var(--border-subtle)', textAlign: 'center' }}>
                  <p style={{ margin: '0 0 2px', fontSize: '18px', fontWeight: '700', color: 'var(--text-primary)' }}>{result.stats.files_reviewed || 0}</p>
                  <p style={{ margin: 0, fontSize: '10px', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>Files Reviewed</p>
                </div>
                <div style={{ padding: '10px 14px', borderRadius: '6px', background: 'var(--bg-elevated)', border: '1px solid var(--border-subtle)', textAlign: 'center' }}>
                  <p style={{ margin: '0 0 2px', fontSize: '18px', fontWeight: '700', color: result.stats.sonarqube_issues > 0 ? 'var(--danger)' : 'var(--success)' }}>{result.stats.sonarqube_issues || 0}</p>
                  <p style={{ margin: 0, fontSize: '10px', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>SonarQube Issues</p>
                </div>
                <div style={{ padding: '10px 14px', borderRadius: '6px', background: 'var(--bg-elevated)', border: '1px solid var(--border-subtle)', textAlign: 'center' }}>
                  <p style={{ margin: '0 0 2px', fontSize: '18px', fontWeight: '700', color: result.stats.pmd_issues > 0 ? 'var(--danger)' : 'var(--success)' }}>{result.stats.pmd_issues || 0}</p>
                  <p style={{ margin: 0, fontSize: '10px', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>PMD Issues</p>
                </div>
                <div style={{ padding: '10px 14px', borderRadius: '6px', background: 'var(--bg-elevated)', border: '1px solid var(--border-subtle)', textAlign: 'center' }}>
                  <p style={{ margin: '0 0 2px', fontSize: '18px', fontWeight: '700', color: 'var(--text-primary)' }}>
                    {(result.rules_checked?.sonarqube?.total || 0) + (result.rules_checked?.pmd?.total || 0)}
                  </p>
                  <p style={{ margin: 0, fontSize: '10px', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>Rules Checked</p>
                </div>
              </div>
            )}
            {/* Rules breakdown */}
            {result?.rules_checked && (
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '10px' }}>
                {result.rules_checked.sonarqube && (
                  <div style={{ padding: '10px 14px', borderRadius: '6px', background: 'var(--bg-elevated)', border: '1px solid var(--border-subtle)' }}>
                    <p style={{ margin: '0 0 6px', fontSize: '11px', fontWeight: '600', color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>
                      SonarQube Rules ({result.rules_checked.sonarqube.total})
                    </p>
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px' }}>
                      {(result.rules_checked.sonarqube.rules || []).map(r => (
                        <span key={r} style={{
                          padding: '2px 8px', borderRadius: '4px', fontSize: '10px',
                          background: 'var(--bg-base)', border: '1px solid var(--border)',
                          color: 'var(--text-muted)',
                        }}>{r}</span>
                      ))}
                    </div>
                  </div>
                )}
                {result.rules_checked.pmd && (
                  <div style={{ padding: '10px 14px', borderRadius: '6px', background: 'var(--bg-elevated)', border: '1px solid var(--border-subtle)' }}>
                    <p style={{ margin: '0 0 6px', fontSize: '11px', fontWeight: '600', color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>
                      PMD Rules ({result.rules_checked.pmd.total})
                    </p>
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px' }}>
                      {(result.rules_checked.pmd.rules || []).map(r => (
                        <span key={r} style={{
                          padding: '2px 8px', borderRadius: '4px', fontSize: '10px',
                          background: 'var(--bg-base)', border: '1px solid var(--border)',
                          color: 'var(--text-muted)',
                        }}>{r}</span>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            )}
            {/* Severity breakdown */}
            {result?.stats?.by_severity && Object.keys(result.stats.by_severity).length > 0 && (
              <div style={{ display: 'flex', gap: '8px', marginTop: '10px', flexWrap: 'wrap' }}>
                <span style={{ fontSize: '11px', color: 'var(--text-muted)', alignSelf: 'center' }}>By severity:</span>
                {Object.entries(result.stats.by_severity).map(([sev, count]) => (
                  <span key={sev} style={{
                    padding: '2px 10px', borderRadius: '20px', fontSize: '10px', fontWeight: '600',
                    background: 'var(--bg-elevated)', border: '1px solid var(--border)',
                    color: 'var(--text-secondary)', textTransform: 'uppercase',
                  }}>{sev}: {count}</span>
                ))}
              </div>
            )}
          </div>
        )}

        {isClean && (
          <div style={{
            padding: '16px', borderRadius: '6px', textAlign: 'center',
            background: 'rgba(76,175,125,0.06)', border: '1px solid rgba(76,175,125,0.2)',
          }}>
            <CheckCircle size={24} style={{ color: 'var(--success)', marginBottom: '8px' }} />
            <p style={{ margin: 0, fontSize: '13px', color: 'var(--text-secondary)' }}>
              {result?.summary || `All files passed the ${label.toLowerCase()}.`}
            </p>
          </div>
        )}

        {hasIssues && items.length > 0 && (
          <>
            <p style={{ margin: '0 0 12px', fontSize: '12px', color: 'var(--text-muted)' }}>
              {result?.summary}
            </p>
            <div style={{ border: '1px solid var(--border)', borderRadius: '6px', overflow: 'auto' }}>
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '12px' }}>
                <thead>
                  <tr style={{ background: 'var(--bg-base)', borderBottom: '1px solid var(--border)' }}>
                    <th style={{ padding: '8px 12px', textAlign: 'left', fontWeight: '600', color: 'var(--text-muted)', fontSize: '10px', textTransform: 'uppercase' }}>Severity</th>
                    <th style={{ padding: '8px 12px', textAlign: 'left', fontWeight: '600', color: 'var(--text-muted)', fontSize: '10px', textTransform: 'uppercase' }}>{isCode ? 'Rule' : 'CWE / OWASP'}</th>
                    <th style={{ padding: '8px 12px', textAlign: 'left', fontWeight: '600', color: 'var(--text-muted)', fontSize: '10px', textTransform: 'uppercase' }}>File : Line</th>
                    <th style={{ padding: '8px 12px', textAlign: 'left', fontWeight: '600', color: 'var(--text-muted)', fontSize: '10px', textTransform: 'uppercase' }}>{isCode ? 'Issue' : 'Finding'}</th>
                    <th style={{ padding: '8px 12px', textAlign: 'left', fontWeight: '600', color: 'var(--text-muted)', fontSize: '10px', textTransform: 'uppercase' }}>{isCode ? 'Fix' : 'Remediation'}</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((item, i) => (
                    <tr key={i} style={{ borderBottom: i < items.length - 1 ? '1px solid var(--border-subtle)' : 'none' }}>
                      <td style={{ padding: '8px 12px', verticalAlign: 'top' }}>
                        <SeverityBadge severity={item.severity} />
                      </td>
                      <td style={{ padding: '8px 12px', verticalAlign: 'top', color: 'var(--text-secondary)', fontFamily: 'monospace', fontSize: '11px' }}>
                        {isCode ? item.rule : [item.cwe, item.owasp].filter(Boolean).join(' / ')}
                      </td>
                      <td style={{ padding: '8px 12px', verticalAlign: 'top', color: 'var(--text-secondary)', fontFamily: 'monospace', fontSize: '11px', whiteSpace: 'nowrap' }}>
                        {item.file}{item.line ? `:${item.line}` : ''}
                      </td>
                      <td style={{ padding: '8px 12px', verticalAlign: 'top', color: 'var(--text-primary)', lineHeight: '1.5' }}>
                        {item.message || item.description}
                      </td>
                      <td style={{ padding: '8px 12px', verticalAlign: 'top', color: 'var(--text-muted)', lineHeight: '1.5' }}>
                        {item.fix || item.remediation}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div style={{ marginTop: '16px', display: 'flex', gap: '10px', justifyContent: 'flex-end' }}>
              <button onClick={handleLoopBack} disabled={looping || skipping} style={{
                display: 'flex', alignItems: 'center', gap: '6px',
                padding: '8px 16px', background: 'rgba(218,119,86,0.1)',
                color: 'var(--accent)', border: '1px solid rgba(218,119,86,0.3)',
                borderRadius: '6px', fontSize: '12px', fontWeight: '600',
                cursor: (looping || skipping) ? 'not-allowed' : 'pointer', opacity: (looping || skipping) ? 0.7 : 1,
              }}>
                {looping ? <Loader size={12} style={{ animation: 'spin 1s linear infinite' }} /> : <RotateCcw size={12} />}
                Fix Issues — Send back to Code Change Agent
              </button>
              <button
                onClick={async () => {
                  setSkipping(true)
                  setErr(null)
                  try {
                    await phaseBApi.advanceStep(changeId)
                    await onRefetchRun()
                  } catch (e) {
                    setErr(e.response?.data?.detail || 'Skip failed')
                  } finally {
                    setSkipping(false)
                  }
                }}
                disabled={skipping || looping}
                style={{
                  display: 'flex', alignItems: 'center', gap: '6px',
                  padding: '8px 16px', background: 'var(--bg-elevated)',
                  color: 'var(--text-secondary)', border: '1px solid var(--border)',
                  borderRadius: '6px', fontSize: '12px', fontWeight: '600',
                  cursor: (skipping || looping) ? 'not-allowed' : 'pointer', opacity: (skipping || looping) ? 0.7 : 1,
                }}
              >
                {skipping ? <Loader size={12} style={{ animation: 'spin 1s linear infinite' }} /> : <ArrowRight size={12} />}
                Skip & Proceed to Git
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  )
}

// ── Git / MR panel ───────────────────────────────────────────────────────────

const GIT_STEPS = [
  { key: 'branch_created', label: 'Creating feature branch' },
  { key: 'committed',      label: 'Committing files' },
  { key: 'mr_raised',      label: 'Raising Merge Request' },
]

// M-5 — small helper for per-repo MR badge colour.
// `mr_state` mirrors GitLab's value: "opened" | "merged" | "closed" | null.
function _mrBadge(mrState) {
  if (mrState === 'merged') return { bg: 'rgba(76,175,125,0.15)', fg: 'var(--success)', label: 'merged' }
  if (mrState === 'closed') return { bg: 'rgba(180,180,180,0.15)', fg: 'var(--text-muted)', label: 'closed' }
  if (mrState === 'opened') return { bg: 'rgba(218,119,86,0.15)', fg: 'var(--accent)', label: 'open' }
  return null
}

function GitPanel({ changeId, currentStep, onRefetchRun }) {
  const state = getStepState('git', currentStep)

  const [running, setRunning]       = useState(false)
  const [progress, setProgress]     = useState(0)  // 0-2 index into GIT_STEPS
  const [result, setResult]         = useState(null)
  const [err, setErr]               = useState(null)
  // M-5 — multi-select. Keys: repo_id; values: bool. Empty selection means
  // "use whatever phase_b_run_repos rows already exist for this run".
  const [selectedRepos, setSelectedRepos] = useState({})
  // Optional feature-branch override. Required when the backend 422s because the
  // run has no feature branch (it refuses to commit to a base branch).
  const [branchInput, setBranchInput] = useState('')

  const { data: existingEvent } = useQuery({
    queryKey: ['phase-b-git', changeId],
    queryFn:  () => phaseBApi.getGitEvent(changeId).then(r => r.data),
    retry:    false,
    enabled:  state !== 'pending',
  })

  const { data: repos } = useQuery({
    queryKey: ['phase-b-git-repos', changeId],
    queryFn:  () => phaseBApi.listGitRepos(changeId).then(r => r.data),
    enabled:  state !== 'pending',   // load repos at GIT or beyond (supports "push later")
  })

  useEffect(() => {
    if (existingEvent && !result) setResult(existingEvent)
  }, [existingEvent])

  useEffect(() => {
    // Default: pre-select all available repos so a multi-repo CR pushes to
    // every registered repo. Operators can de-select to restrict the push.
    if (repos?.length && Object.keys(selectedRepos).length === 0) {
      const initial = {}
      repos.forEach(r => { initial[r.id] = true })
      setSelectedRepos(initial)
    }
  }, [repos])

  // Rendered nothing until GIT is reachable. This return sits AFTER every hook
  // on purpose: hooks must run in the same order on every render, so an early
  // return above them changes the hook count between renders and React starts
  // reading another hook's state. The queries above are already guarded with
  // `enabled: state !== 'pending'`, so nothing is fetched while pending.
  if (state === 'pending') return null

  // M-5 — done state now considers either the legacy single-MR status OR
  // the presence of at least one MR url in the per-repo `repos` array.
  const repoResults = result?.repos || []
  const anyMrCreated =
    repoResults.some(r => r.mr_url) ||
    result?.status === 'mr_raised' ||
    result?.status === 'merged'
  // "done" means an MR was actually raised — NOT merely that the stepper advanced past
  // GIT. A run that moved on to build/test WITHOUT pushing must still offer "push later"
  // here, not show a false "MR Created" badge.
  const isDone = anyMrCreated
  // Offer the push once the run is at GIT or beyond and nothing has been pushed yet.
  const canPush = !isDone && state !== 'pending'

  const handlePush = async () => {
    setRunning(true)
    setErr(null)
    setProgress(0)

    const timer = setInterval(() => {
      setProgress(prev => Math.min(prev + 1, 2))
    }, 900)

    try {
      // Multi-select selected repos go via the M-2 start-time route already.
      // For push-time itself we send no body to use the multi-repo fan-out
      // path; if exactly ONE repo is selected we send `repo_id` for the
      // legacy single-repo override (handy for hotfixing one repo).
      const selected = Object.entries(selectedRepos)
        .filter(([, v]) => v)
        .map(([k]) => k)
      const body = (selected.length === 1) ? { repo_id: selected[0] } : {}
      if (branchInput.trim()) body.branch = branchInput.trim()
      const res = await phaseBApi.triggerGitPush(changeId, body)
      clearInterval(timer)
      setProgress(2)
      setResult(res.data)
      await onRefetchRun()
    } catch (e) {
      clearInterval(timer)
      setErr(e.response?.data?.detail || 'Git push failed')
    } finally {
      setRunning(false)
    }
  }

  return (
    <div style={{
      marginBottom: '24px', background: 'var(--bg-elevated)',
      border: '1px solid var(--border)', borderRadius: '8px', overflow: 'hidden',
    }}>
      {/* Header */}
      <div style={{
        padding: '14px 20px', borderBottom: '1px solid var(--border-subtle)',
        display: 'flex', alignItems: 'center', gap: '10px',
      }}>
        <div style={{ color: isDone ? 'var(--success)' : 'var(--accent)' }}>
          {isDone ? <CheckCircle size={16} /> : <FileCode size={16} />}
        </div>
        <span style={{ fontSize: '14px', fontWeight: '600', color: 'var(--text-primary)', flex: 1 }}>
          Git / MR
        </span>
        {isDone && (
          <span style={{
            fontSize: '11px', padding: '3px 10px', borderRadius: '20px', fontWeight: '600',
            background: 'rgba(76,175,125,0.1)', color: 'var(--success)',
            border: '1px solid rgba(76,175,125,0.3)',
          }}>
            <CheckCircle size={11} style={{ marginRight: 4, verticalAlign: 'text-bottom' }} /> MR Created
          </span>
        )}
        {canPush && !running && (
          <button onClick={handlePush} style={{
            display: 'flex', alignItems: 'center', gap: '6px',
            padding: '7px 16px', background: 'var(--accent)', color: 'white',
            border: 'none', borderRadius: '6px', fontSize: '12px', fontWeight: '600',
            cursor: 'pointer',
          }}>
            <ArrowRight size={12} /> Push to GitLab & Create MR
          </button>
        )}
      </div>

      {/* Body */}
      <div style={{ padding: '16px 20px' }}>
        {err && <p style={{ margin: '0 0 12px', fontSize: '12px', color: 'var(--danger)' }}>{err}</p>}

        {/* Progress animation */}
        {running && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '12px', padding: '8px 0' }}>
            {GIT_STEPS.map((step, i) => {
              const stepDone = i < progress
              const stepActive = i === progress
              return (
                <div key={step.key} style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                  <div style={{
                    width: '22px', height: '22px', borderRadius: '50%',
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                    background: stepDone ? 'var(--success)' : stepActive ? 'var(--accent)' : 'var(--bg-base)',
                    border: `2px solid ${stepDone ? 'var(--success)' : stepActive ? 'var(--accent)' : 'var(--border)'}`,
                    color: (stepDone || stepActive) ? 'white' : 'var(--text-muted)',
                    flexShrink: 0,
                  }}>
                    {stepDone
                      ? <CheckCircle size={11} />
                      : stepActive
                        ? <Loader size={10} style={{ animation: 'spin 1s linear infinite' }} />
                        : <Circle size={9} />}
                  </div>
                  <span style={{
                    fontSize: '13px',
                    color: stepDone ? 'var(--success)' : stepActive ? 'var(--accent)' : 'var(--text-muted)',
                    fontWeight: stepActive ? '600' : '400',
                  }}>
                    {step.label}{stepDone ? ' ✓' : stepActive ? '…' : ''}
                  </span>
                </div>
              )
            })}
          </div>
        )}

        {/* Not started — repo multi-select (shown at GIT or later, until pushed) */}
        {!running && canPush && (
          <div style={{ padding: '8px 0' }}>
            {repos && repos.length > 0 && (
              <div style={{ marginBottom: '14px' }}>
                <label style={{
                  display: 'block', fontSize: '11px', color: 'var(--text-muted)',
                  marginBottom: '8px', textTransform: 'uppercase', letterSpacing: '0.04em',
                }}>
                  Target Repositories
                  {repos.length > 1 && (
                    <span style={{ marginLeft: 8, color: 'var(--accent)', textTransform: 'none', fontSize: '10px' }}>
                      (one MR per selected repo)
                    </span>
                  )}
                </label>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                  {repos.map(r => (
                    <label key={r.id} style={{
                      display: 'flex', alignItems: 'center', gap: '10px',
                      padding: '8px 12px', borderRadius: '6px',
                      border: '1px solid var(--border)', background: 'var(--bg-input)',
                      cursor: 'pointer', fontSize: '12px',
                    }}>
                      <input
                        type="checkbox"
                        checked={!!selectedRepos[r.id]}
                        onChange={e => setSelectedRepos(prev => ({ ...prev, [r.id]: e.target.checked }))}
                        style={{ cursor: 'pointer' }}
                      />
                      <span style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{r.label}</span>
                      <span style={{ color: 'var(--text-muted)', fontFamily: 'monospace' }}>
                        {r.gitlab_repo}
                      </span>
                      <span style={{ marginLeft: 'auto', color: 'var(--text-muted)', fontSize: '11px' }}>
                        default branch: {r.gitlab_branch}
                      </span>
                    </label>
                  ))}
                </div>
              </div>
            )}
            {repos && repos.length === 0 && (
              <p style={{
                margin: '0 0 12px', fontSize: '12px', color: 'var(--text-muted)',
                padding: '8px 12px', background: 'rgba(218,119,86,0.06)',
                borderRadius: '6px', border: '1px solid rgba(218,119,86,0.15)',
              }}>
                No repositories registered. The default GitLab settings from Configuration will be used.
                Add repos in Admin &gt; Code Indexing for more control.
              </p>
            )}
            <div style={{ marginBottom: '14px' }}>
              <label style={{
                display: 'block', fontSize: '11px', color: 'var(--text-muted)',
                marginBottom: '8px', textTransform: 'uppercase', letterSpacing: '0.04em',
              }}>
                Feature branch <span style={{ textTransform: 'none' }}>(optional override)</span>
              </label>
              <input
                type="text"
                value={branchInput}
                onChange={e => setBranchInput(e.target.value)}
                placeholder="leave empty to use the run's feature branch"
                style={{
                  width: '100%', padding: '8px 12px', borderRadius: '6px',
                  border: '1px solid var(--border)', background: 'var(--bg-input)',
                  color: 'var(--text-primary)', fontSize: '12px', fontFamily: 'monospace',
                }}
              />
            </div>
            <p style={{ margin: 0, fontSize: '12px', color: 'var(--text-muted)' }}>
              {Object.values(selectedRepos).filter(Boolean).length > 1
                ? 'Files generated by the AI are routed per-repo by their [repo-label] markers. One MR is opened per selected repo, all on the same shared branch.'
                : 'Push approved code to GitLab, create a feature branch, and raise a Merge Request.'}
            </p>
          </div>
        )}

        {/* Completed — multi-repo per-MR card list */}
        {isDone && result && (
          <div style={{
            padding: '14px', borderRadius: '8px',
            background: 'rgba(76,175,125,0.06)', border: '1px solid rgba(76,175,125,0.2)',
          }}>
            {/* Shared branch line at the top */}
            {result.branch_name && (
              <div style={{
                display: 'flex', alignItems: 'baseline', gap: '8px',
                paddingBottom: '10px', marginBottom: '12px',
                borderBottom: '1px solid rgba(76,175,125,0.15)',
              }}>
                <span style={{ fontSize: '11px', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>
                  Branch
                </span>
                <span style={{ color: 'var(--text-primary)', fontFamily: 'monospace', fontSize: '12px' }}>
                  {result.branch_name}
                </span>
                {result.summary && (
                  <span style={{ marginLeft: 'auto', fontSize: '11px', color: 'var(--text-muted)' }}>
                    {result.summary}
                  </span>
                )}
              </div>
            )}

            {/* Per-repo MR rows. M-4: result.repos is the canonical list.
                Falls back to a synthetic single-row when only legacy fields
                are populated (legacy single-repo run). */}
            {(repoResults.length > 0
              ? repoResults
              : [{
                  repo_id:     'legacy',
                  label:       'Repository',
                  gitlab_repo: result.gitlab_repo || '',
                  branch:      result.branch_name || '',
                  mr_url:      result.mr_url,
                  mr_iid:      result.mr_iid,
                  mr_state:    result.mr_url ? 'opened' : null,
                }]
            ).map((r, idx) => {
              const badge = _mrBadge(r.mr_state)
              return (
                <div key={r.repo_id || idx} style={{
                  display: 'flex', alignItems: 'center', gap: '10px',
                  padding: '10px 12px', marginBottom: '8px',
                  background: 'var(--bg-base)', borderRadius: '6px',
                  border: '1px solid var(--border-subtle)',
                }}>
                  <FileCode size={14} style={{ color: 'var(--accent)', flexShrink: 0 }} />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ display: 'flex', alignItems: 'baseline', gap: '8px', flexWrap: 'wrap' }}>
                      <span style={{ fontSize: '13px', fontWeight: 600, color: 'var(--text-primary)' }}>
                        {r.label}
                      </span>
                      <span style={{ fontSize: '11px', color: 'var(--text-muted)', fontFamily: 'monospace' }}>
                        {r.gitlab_repo}
                      </span>
                    </div>
                    {(r.mr_url || r.mr_iid) && (
                      <div style={{ marginTop: '4px', fontSize: '11px' }}>
                        {r.mr_url ? (
                          <a href={safeHref(r.mr_url)} target="_blank" rel="noopener noreferrer"
                             style={{ color: 'var(--accent)', fontFamily: 'monospace', wordBreak: 'break-all' }}>
                            !{r.mr_iid} — {r.mr_url}
                          </a>
                        ) : (
                          <span style={{ color: 'var(--accent)', fontFamily: 'monospace' }}>!{r.mr_iid}</span>
                        )}
                      </div>
                    )}
                    {!r.mr_url && r.error && (
                      <div style={{ marginTop: '4px', fontSize: '11px', color: 'var(--danger)' }}>
                        {r.error}
                      </div>
                    )}
                    {!r.mr_url && !r.error && r.status === 'skipped_empty' && (
                      <div style={{ marginTop: '4px', fontSize: '11px', color: 'var(--text-muted)' }}>
                        No files routed to this repo — skipped
                      </div>
                    )}
                  </div>
                  {badge && (
                    <span style={{
                      fontSize: '10px', padding: '3px 9px', borderRadius: '20px', fontWeight: 600,
                      background: badge.bg, color: badge.fg, flexShrink: 0,
                    }}>
                      {badge.label}
                    </span>
                  )}
                </div>
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
}

// ── Pipeline step panel (Build+Deploy, UAT Test Gen, UAT Execute, UAT Triage) ─

// ── Build + Deploy panel (unified — Session 23) ─────────────────────────────
//
// The host's `build_and_deploy.sh` does clone → build → deploy → service
// startup in one shell run. We render three collapsible log sections plus
// an artifacts table and a services-up table after the run completes.

function CollapsibleLog({ title, body, defaultOpen = false }) {
  const [open, setOpen] = useState(defaultOpen)
  if (!body) return null
  return (
    <div style={{ marginTop: '10px' }}>
      <button onClick={() => setOpen(v => !v)} style={{
        display: 'flex', alignItems: 'center', gap: '6px', width: '100%',
        padding: '6px 12px', background: 'var(--bg-base)', border: '1px solid var(--border)',
        borderRadius: '5px', fontSize: '11px', color: 'var(--text-muted)', cursor: 'pointer',
        textAlign: 'left', fontWeight: '600',
      }}>
        {open ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
        {title}
      </button>
      {open && (
        <pre style={{
          marginTop: '6px', padding: '14px', borderRadius: '6px',
          background: '#1a1a2e', color: '#a8b2d1', fontSize: '11px',
          lineHeight: '1.5', maxHeight: '400px', overflowY: 'auto',
          whiteSpace: 'pre-wrap', wordBreak: 'break-word',
          border: '1px solid var(--border)',
        }}>
          {body}
        </pre>
      )}
    </div>
  )
}

function BuildPanel({ changeId, currentStep, onRefetchRun }) {
  const state = getStepState('build', currentStep)
  const { user: authUser } = useAuth()

  const [triggering, setTriggering] = useState(false)  // POST in flight
  const [result, setResult]         = useState(null)
  const [err, setErr]               = useState(null)
  const [coreBranch, setCoreBranch] = useState('master')
  const [appBranch, setAppBranch] = useState('master')
  const [scriptPath, setScriptPath] = useState('')
  const livePreRef                  = useRef(null)
  const prevStatusRef               = useRef(null)

  // Demo mode (?demo=1 in URL, localStorage flag, or window.__DEMO_MODE__):
  // intercept the Run click with a canned ~30s Maven build + deploy stream
  // instead of calling the real backend. See lib/demoBuildLogs.js.
  const demoMode = isDemoBuildMode()
  const [demoLines, setDemoLines]     = useState([])
  const demoLinesRef                  = useRef([])
  const demoAbortRef                  = useRef(null)
  const demoLogEndRef                 = useRef(null)

  // Auto-scroll the live demo log to the bottom whenever a new line arrives.
  useEffect(() => {
    if (demoLines.length > 0) {
      demoLogEndRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
    }
  }, [demoLines.length])

  // Stop the demo cleanly if the user navigates away mid-stream.
  useEffect(() => () => { demoAbortRef.current?.abort() }, [])

  // Follow the stream only while the operator is already at the bottom.
  useEffect(() => {
    stickToBottom(livePreRef.current)
  }, [result?.build_log?.length])

  // The trigger returns the QUEUED row immediately and the backend streams the
  // script's output onto it — poll while queued/running so the log is live.
  const { data: existingBuild, refetch: refetchBuild } = useQuery({
    queryKey: ['phase-b-build', changeId],
    queryFn:  () => phaseBApi.getBuild(changeId).then(r => r.data),
    retry:    false,
    enabled:  state !== 'pending',
    refetchInterval: (query) => {
      const s = query.state.data?.status
      return (s === 'queued' || s === 'running') ? 2500 : false
    },
  })

  useEffect(() => {
    if (!existingBuild) return
    setResult(existingBuild)
    if (existingBuild.core_branch && prevStatusRef.current === null) setCoreBranch(existingBuild.core_branch)
    if (existingBuild.app_branch && prevStatusRef.current === null) setAppBranch(existingBuild.app_branch)
    if (existingBuild.script_path && prevStatusRef.current === null) setScriptPath(existingBuild.script_path)
    // The step advances server-side when the background build succeeds —
    // refetch the run exactly once on the running→terminal transition.
    const prev = prevStatusRef.current
    prevStatusRef.current = existingBuild.status
    if ((prev === 'running' || prev === 'queued')
        && (existingBuild.status === 'success' || existingBuild.status === 'failure')) {
      onRefetchRun()
    }
  }, [existingBuild])

  // Same reason as GitPanel: this return must sit AFTER every hook, or the hook
  // count changes between renders and React reads the wrong slot.
  if (state === 'pending') return null

  const running  = triggering || result?.status === 'queued' || result?.status === 'running'
  const isDone   = result?.status === 'success' || (state === 'done' && !running)
  const isFailed = result?.status === 'failure'

  const handleBuild = async () => {
    if (demoMode) return handleDemoBuild()
    setTriggering(true)
    setErr(null)
    try {
      const res = await phaseBApi.triggerBuild(changeId, {
        core_branch: coreBranch || 'master',
        app_branch: appBranch || 'master',
        ...(scriptPath.trim() ? { script_path: scriptPath.trim() } : {}),
      })
      prevStatusRef.current = res.data?.status || 'queued'
      setResult(res.data)
      await refetchBuild()
    } catch (e) {
      setErr(e.response?.data?.detail || 'Build + deploy failed to start')
    } finally {
      setTriggering(false)
    }
  }

  const handleDemoBuild = async () => {
    setTriggering(true)
    setErr(null)
    setResult(null)
    demoLinesRef.current = []
    setDemoLines([])
    const abort = new AbortController()
    demoAbortRef.current = abort
    try {
      // Run visual stream + backend trigger in parallel. When the backend
      // is configured with PHASE_B_RUNNER_MODE=mock, /trigger sleeps ~30s
      // and writes a real BuildRun — the timings line up so both finish
      // together and the step actually advances on the backend. When the
      // backend is in ssh/local mode, the call may still succeed (real
      // build) or fail; we fall back to the fake-success payload so the
      // demo UI always lands somewhere sensible.
      const [, triggerOutcome] = await Promise.all([
        streamDemoBuildLogs({
          signal: abort.signal,
          onLine: (text) => {
            demoLinesRef.current = [...demoLinesRef.current, text]
            setDemoLines(demoLinesRef.current)
          },
        }),
        phaseBApi.triggerBuild(changeId, {
          core_branch: coreBranch || 'master',
          app_branch: appBranch || 'master',
        }).then(
          r => ({ ok: true,  data: r.data }),
          e => ({ ok: false, data: null, error: e }),
        ),
      ])
      if (abort.signal.aborted) return

      if (triggerOutcome.ok && ['success', 'queued', 'running'].includes(triggerOutcome.data?.status)) {
        // Backend accepted it (the trigger now returns the queued row and the
        // demo runner completes it ~in step with the visual stream). Use the
        // persisted row; the poll picks up the completion + step advance.
        prevStatusRef.current = triggerOutcome.data.status
        setResult(triggerOutcome.data)
        await refetchBuild()
      } else {
        // Backend rejected the call (ssh key invalid, host unreachable,
        // etc.). Keep the demo authentic on screen by falling through to
        // the synthetic success payload — operator can re-run with real
        // backend later.
        setResult(buildDemoResult({
          coreBranch: coreBranch || 'master',
          appBranch: appBranch || 'master',
          allLines:   demoLinesRef.current,
        }))
      }
    } catch (e) {
      setErr('Demo stream failed: ' + (e?.message || 'unknown'))
    } finally {
      setTriggering(false)
    }
  }

  const duration = result?.triggered_at && result?.completed_at
    ? Math.round((new Date(result.completed_at) - new Date(result.triggered_at)) / 1000)
    : null

  const inputStyle = {
    flex: 1, minWidth: 0,
    padding: '6px 10px', fontSize: '12px',
    background: 'var(--bg-base)', color: 'var(--text-primary)',
    border: '1px solid var(--border)', borderRadius: '5px',
    fontFamily: 'monospace',
  }

  return (
    <div style={{
      marginBottom: '24px', background: 'var(--bg-elevated)',
      border: '1px solid var(--border)', borderRadius: '8px', overflow: 'hidden',
    }}>
      {/* Header */}
      <div style={{
        padding: '14px 20px', borderBottom: '1px solid var(--border-subtle)',
        display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap',
      }}>
        <div style={{ color: isDone ? 'var(--success)' : isFailed ? 'var(--danger)' : 'var(--accent)' }}>
          {isDone ? <CheckCircle size={16} /> : isFailed ? <AlertTriangle size={16} /> : <Code2 size={16} />}
        </div>
        <span style={{ fontSize: '14px', fontWeight: '600', color: 'var(--text-primary)', flex: 1 }}>
          Build + Deploy — the network Stack
          {result?.id && (
            <span style={{
              marginLeft: '10px', fontSize: '10px', fontWeight: '500',
              padding: '2px 8px', borderRadius: '10px', background: 'var(--bg-base)',
              color: 'var(--text-muted)', fontFamily: 'monospace',
            }}>
              run {result.id.slice(0, 8)}
            </span>
          )}
          {demoMode && (
            <span title="Demo mode — Run/Retry will play canned logs and call the backend mock instead of SSH"
              style={{
                marginLeft: '8px', fontSize: '10px', fontWeight: '700',
                padding: '2px 8px', borderRadius: '10px',
                background: 'rgba(218,119,86,0.15)', color: '#c97a3a',
                border: '1px solid rgba(218,119,86,0.35)',
                letterSpacing: '0.5px', textTransform: 'uppercase',
              }}>
              Demo Mode
            </span>
          )}
        </span>
        {isDone && (
          <span style={{
            fontSize: '11px', padding: '3px 10px', borderRadius: '20px', fontWeight: '600',
            background: 'rgba(76,175,125,0.1)', color: 'var(--success)',
            border: '1px solid rgba(76,175,125,0.3)',
          }}>
            <CheckCircle size={11} style={{ marginRight: 4, verticalAlign: 'text-bottom' }} />
            Build + Deploy Successful {duration ? `(${duration}s)` : ''}
          </span>
        )}
        {isFailed && (
          <span style={{
            fontSize: '11px', padding: '3px 10px', borderRadius: '20px', fontWeight: '600',
            background: 'rgba(224,108,108,0.1)', color: 'var(--danger)',
            border: '1px solid rgba(224,108,108,0.3)',
          }}>
            <AlertTriangle size={11} style={{ marginRight: 4, verticalAlign: 'text-bottom' }} />
            Failed
          </span>
        )}
        {state === 'active' && !isDone && !isFailed && !running && (
          <div style={{ display: 'flex', gap: '8px' }}>
            <button onClick={handleBuild} style={{
              display: 'flex', alignItems: 'center', gap: '6px',
              padding: '7px 16px', background: 'var(--accent)', color: 'white',
              border: 'none', borderRadius: '6px', fontSize: '12px', fontWeight: '600', cursor: 'pointer',
            }}>
              <ArrowRight size={12} /> Run Build + Deploy
            </button>
            <button onClick={async () => { await phaseBApi.advanceStep(changeId); await onRefetchRun() }} style={{
              display: 'flex', alignItems: 'center', gap: '6px',
              padding: '7px 16px', background: 'var(--bg-elevated)', color: 'var(--text-secondary)',
              border: '1px solid var(--border)', borderRadius: '6px', fontSize: '12px', fontWeight: '600', cursor: 'pointer',
            }}>
              <ArrowRight size={12} /> Skip
            </button>
          </div>
        )}
        {isFailed && !running && (
          <div style={{ display: 'flex', gap: '8px' }}>
            <button onClick={handleBuild} style={{
              display: 'flex', alignItems: 'center', gap: '6px',
              padding: '7px 16px', background: 'var(--accent)', color: 'white',
              border: 'none', borderRadius: '6px', fontSize: '12px', fontWeight: '600', cursor: 'pointer',
            }}>
              <RefreshCw size={12} /> Retry
            </button>
            <button onClick={async () => { await phaseBApi.advanceStep(changeId); await onRefetchRun() }} style={{
              display: 'flex', alignItems: 'center', gap: '6px',
              padding: '7px 16px', background: 'var(--bg-elevated)', color: 'var(--text-secondary)',
              border: '1px solid var(--border)', borderRadius: '6px', fontSize: '12px', fontWeight: '600', cursor: 'pointer',
            }}>
              <ArrowRight size={12} /> Skip
            </button>
          </div>
        )}
      </div>

      {/* Body */}
      <div style={{ padding: '16px 20px' }}>
        {err && <p style={{ margin: '0 0 12px', fontSize: '12px', color: 'var(--danger)' }}>{err}</p>}

        {/* Branch + script inputs — visible while step is active and not yet running. */}
        {state === 'active' && !running && !isDone && (
          <div style={{
            display: 'flex', gap: '12px', alignItems: 'center', flexWrap: 'wrap',
            marginBottom: '12px',
          }}>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '4px', flex: '1 1 180px' }}>
              <label style={{ fontSize: '10px', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.5px' }}>
                core branch
              </label>
              <input
                value={coreBranch}
                onChange={e => setCoreBranch(e.target.value)}
                placeholder="master"
                style={inputStyle}
              />
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '4px', flex: '1 1 180px' }}>
              <label style={{ fontSize: '10px', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.5px' }}>
                app branch
              </label>
              <input
                value={appBranch}
                onChange={e => setAppBranch(e.target.value)}
                placeholder="master"
                style={inputStyle}
              />
            </div>
            {canPickScript(authUser) && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '4px', flex: '2 1 320px' }}>
                <label style={{ fontSize: '10px', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.5px' }}>
                  build + deploy script (under PHASE_B_SCRIPT_ROOT)
                </label>
                <input
                  value={scriptPath}
                  onChange={e => setScriptPath(e.target.value)}
                  placeholder="e.g. nlln/build_and_deploy.sh — empty = configured default"
                  style={inputStyle}
                />
              </div>
            )}
          </div>
        )}

        {/* Running progress — the backend streams the script's output onto the
            BuildRun row every ~2s and the poll renders it here live. Demo mode
            keeps its own canned terminal pane below. */}
        {running && !demoMode && (
          <div style={{ marginBottom: '12px' }}>
            <div style={{
              display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '6px',
              fontSize: '11px', fontWeight: '600', color: 'var(--text-muted)',
              textTransform: 'uppercase', letterSpacing: '0.5px',
            }}>
              <Loader size={11} style={{ color: 'var(--accent)', animation: 'spin 1s linear infinite' }} />
              {result?.script_path
                ? `Running ${result.script_path.split('/').pop()} — streaming log`
                : 'Build + Deploy running — streaming log'}
            </div>
            <pre ref={livePreRef} style={{
              background: '#0d1117', color: '#c9d1d9',
              padding: '12px 14px', borderRadius: '6px',
              fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
              fontSize: '11.5px', lineHeight: '1.55',
              maxHeight: '420px', overflowY: 'auto', overflowX: 'auto',
              whiteSpace: 'pre-wrap', wordBreak: 'break-word', margin: 0,
              border: '1px solid #30363d',
            }}>
              {(() => {
                const tail = logTail(result?.build_log)
                const text = tail.lines.join('\n')
                return (
                  <>
                    {tail.omitted > 0 && (
                      <span style={{ color: '#8b949e' }}>
                        {`[… ${tail.omitted} earlier lines omitted — the full log opens below once the run completes]\n`}
                      </span>
                    )}
                    {text || 'Waiting for the first output lines…'}
                  </>
                )
              })()}
            </pre>
          </div>
        )}

        {/* Demo mode — live terminal pane. Renders during the stream and
            stays visible after completion so the audience can scroll back. */}
        {demoMode && demoLines.length > 0 && (
          <div style={{ marginBottom: '12px' }}>
            <div style={{
              display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '6px',
              fontSize: '11px', fontWeight: '600', color: 'var(--text-muted)',
              textTransform: 'uppercase', letterSpacing: '0.5px',
            }}>
              {running
                ? <Loader size={11} style={{ color: 'var(--accent)', animation: 'spin 1s linear infinite' }} />
                : <CheckCircle size={11} style={{ color: 'var(--success)' }} />}
              {running ? 'Build + Deploy — streaming…' : 'Build + Deploy — completed'}
              <span style={{ marginLeft: 'auto', fontWeight: '400', color: 'var(--text-muted)' }}>
                {demoLines.length} lines
              </span>
            </div>
            <pre style={{
              background: '#0d1117', color: '#c9d1d9',
              padding: '12px 14px', borderRadius: '6px',
              fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
              fontSize: '11.5px', lineHeight: '1.55',
              maxHeight: '420px', overflowY: 'auto', overflowX: 'auto',
              whiteSpace: 'pre', margin: 0,
              border: '1px solid #30363d',
            }}>
              {demoLines.map((line, i) => {
                let color = '#c9d1d9'
                if (line.startsWith('[INFO] BUILD SUCCESS')) color = '#7ee787'
                else if (line.startsWith('[INFO] BUILD FAILURE') || line.includes('Failures: ') && !line.includes('Failures: 0')) color = '#ff7b72'
                else if (line.startsWith('[INFO] Downloaded')) color = '#79c0ff'
                else if (line.startsWith('[INFO] Tests run')) color = '#d2a8ff'
                else if (line.startsWith('● ') || line.startsWith('── Deploy complete')) color = '#7ee787'
                else if (line.startsWith('$ ')) color = '#ffa657'
                return (
                  <div key={i} style={{ color }}>{line || ' '}</div>
                )
              })}
              <div ref={demoLogEndRef} />
            </pre>
          </div>
        )}

        {/* Not started */}
        {!running && !result && state === 'active' && (
          <p style={{ margin: 0, fontSize: '12px', color: 'var(--text-muted)', textAlign: 'center', padding: '8px 16px' }}>
            Runs the operator's <code style={{ background: 'var(--bg-base)', padding: '1px 6px', borderRadius: '3px' }}>build_and_deploy.sh</code> over SSH.
            Clones <code>network-core</code> + <code>network-2.0</code>, builds with Maven, deploys 4 web artifacts and starts services.
          </p>
        )}

        {/* Result summary */}
        {(isDone || isFailed) && result && (
          <div style={{
            padding: '14px', borderRadius: '8px',
            background: isDone ? 'rgba(76,175,125,0.06)' : 'rgba(224,108,108,0.06)',
            border: isDone ? '1px solid rgba(76,175,125,0.2)' : '1px solid rgba(224,108,108,0.2)',
            marginBottom: '6px',
          }}>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))', gap: '10px', fontSize: '12px' }}>
              <div>
                <span style={{ color: 'var(--text-muted)', display: 'block', marginBottom: '2px', fontSize: '10px' }}>Status</span>
                <span style={{ color: isDone ? 'var(--success)' : 'var(--danger)', fontWeight: '600' }}>
                  {isDone ? 'SUCCESS' : 'FAILURE'}
                </span>
              </div>
              <div>
                <span style={{ color: 'var(--text-muted)', display: 'block', marginBottom: '2px', fontSize: '10px' }}>Duration</span>
                <span style={{ color: 'var(--text-primary)' }}>{duration ? `${duration} seconds` : '—'}</span>
              </div>
              <div>
                <span style={{ color: 'var(--text-muted)', display: 'block', marginBottom: '2px', fontSize: '10px' }}>Host</span>
                <span style={{ color: 'var(--text-primary)', fontFamily: 'monospace', fontSize: '11px' }}>{result.host || '—'}</span>
              </div>
              <div>
                <span style={{ color: 'var(--text-muted)', display: 'block', marginBottom: '2px', fontSize: '10px' }}>Branches</span>
                <span style={{ color: 'var(--text-primary)', fontFamily: 'monospace', fontSize: '11px' }}>
                  {(result.core_branch || '—')} / {(result.app_branch || '—')}
                </span>
              </div>
              {result.script_path && (
                <div>
                  <span style={{ color: 'var(--text-muted)', display: 'block', marginBottom: '2px', fontSize: '10px' }}>Script</span>
                  <span style={{ color: 'var(--text-primary)', fontFamily: 'monospace', fontSize: '11px' }}>
                    {result.script_path}
                  </span>
                </div>
              )}
            </div>
          </div>
        )}

        {/* Deployed artifacts table */}
        {result?.deployed_artifacts?.length > 0 && (
          <div style={{ marginTop: '10px' }}>
            <div style={{ fontSize: '11px', fontWeight: '600', color: 'var(--text-muted)', marginBottom: '4px' }}>
              Artifacts
            </div>
            <div style={{ border: '1px solid var(--border)', borderRadius: '6px', overflow: 'hidden' }}>
              {result.deployed_artifacts.map((a, i) => (
                <div key={i} style={{
                  display: 'grid', gridTemplateColumns: '1fr 1fr',
                  fontSize: '11px', padding: '6px 10px', fontFamily: 'monospace',
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

        {/* Services started table */}
        {result?.services_started?.length > 0 && (
          <div style={{ marginTop: '10px' }}>
            <div style={{ fontSize: '11px', fontWeight: '600', color: 'var(--text-muted)', marginBottom: '4px' }}>
              Services Up
            </div>
            <div style={{ border: '1px solid var(--border)', borderRadius: '6px', overflow: 'hidden' }}>
              {result.services_started.map((s, i) => (
                <div key={i} style={{
                  display: 'grid', gridTemplateColumns: '1fr 100px',
                  fontSize: '11px', padding: '6px 10px', fontFamily: 'monospace',
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

        {/* Log sections — build is real, deploy is mocked for the demo. */}
        <CollapsibleLog title="Build log" body={result?.build_log} defaultOpen={isFailed} />
        <CollapsibleLog title="Deploy log" body={result?.deploy_log} />
      </div>
    </div>
  )
}

// ── UAT Tests panel (combined gen + exec, script-based) ───────────────────────
// One operator script generates AND executes the suite (path validated
// server-side against PHASE_B_SCRIPT_ROOT); the backend streams its output
// onto the UATTestRun row and this panel polls it live. Once the script has
// run — pass or fail — the pipeline moves to Triage: failures are exactly
// what the AI triage step is for.

function uatLineColor(line) {
  if (/^PASS\b/.test(line)) return '#7ee787'
  if (/^FAIL\b/.test(line)) return '#ff7b72'
  if (/^SKIP\b/.test(line)) return '#ffa657'
  if (/^TESTS:/.test(line)) return '#79c0ff'
  if (line.startsWith('[backend]') || line.startsWith('[exit=')) return '#8b949e'
  return '#c9d1d9'
}

function UatTestPanel({ changeId, currentStep, onRefetchRun }) {
  const state = getStepState('test_gen', currentStep)
  const { user: authUser } = useAuth()
  const allowPick = canPickScript(authUser)

  const [triggering, setTriggering] = useState(false)
  const [run, setRun]               = useState(null)
  const [scriptPath, setScriptPath] = useState('')
  const [baseUrl, setBaseUrl]       = useState('')
  const [err, setErr]               = useState(null)
  const logPreRef                   = useRef(null)
  const prevStatusRef               = useRef(null)

  // Poll the latest run while its script streams; stop once it lands.
  const { data: latest, refetch } = useQuery({
    queryKey: ['phase-b-uat-run', changeId],
    queryFn:  () => phaseBApi.getLatestTestRun(changeId).then(r => r.data),
    retry:    false,
    enabled:  state !== 'pending',
    refetchInterval: (query) =>
      (query.state.data?.test_run?.status === 'running' ? 2500 : false),
  })

  useEffect(() => {
    if (!latest?.test_run) return
    setRun(latest.test_run)
    if (latest.test_run.script_path && prevStatusRef.current === null) {
      setScriptPath(latest.test_run.script_path)
    }
    // The backend advances to TRIAGE itself when the script finishes —
    // refetch the run exactly once on the running→completed transition.
    const prev = prevStatusRef.current
    prevStatusRef.current = latest.test_run.status
    if (prev === 'running' && latest.test_run.status === 'completed') onRefetchRun()
  }, [latest])

  // Follow the stream only while the operator is already at the bottom.
  useEffect(() => {
    stickToBottom(logPreRef.current)
  }, [run?.log?.length])

  // After the hooks, as in the other panels on this page.
  if (state === 'pending') return null

  const running     = triggering || run?.status === 'running'
  const isDone      = run?.status === 'completed'
  const hasFailures = (run?.failed || 0) > 0
  const tail        = logTail(run?.log)

  const handleRun = async () => {
    setTriggering(true); setErr(null)
    try {
      // Empty script path = the operator-configured PHASE_B_TEST_SCRIPT
      // default (works in any runner mode, no elevated role needed).
      const res = await phaseBApi.triggerUatTests(changeId, {
        ...(scriptPath.trim() ? { script_path: scriptPath.trim() } : {}),
        ...(baseUrl.trim() ? { base_url: baseUrl.trim() } : {}),
      })
      prevStatusRef.current = res.data?.test_run?.status || 'running'
      setRun(res.data?.test_run || null)
      await refetch()
    } catch (e) {
      setErr(e.response?.data?.detail || 'UAT tests failed to start')
    } finally {
      setTriggering(false)
    }
  }

  const inputStyle = {
    flex: 1, minWidth: 0,
    padding: '6px 10px', fontSize: '12px',
    background: 'var(--bg-base)', color: 'var(--text-primary)',
    border: '1px solid var(--border)', borderRadius: '5px',
    fontFamily: 'monospace',
  }

  return (
    <div style={{
      marginBottom: '24px', background: 'var(--bg-elevated)',
      border: '1px solid var(--border)', borderRadius: '8px', overflow: 'hidden',
    }}>
      <div style={{
        padding: '14px 20px', borderBottom: '1px solid var(--border-subtle)',
        display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap',
      }}>
        <div style={{ color: isDone ? (hasFailures ? 'var(--danger)' : 'var(--success)') : 'var(--accent)' }}>
          {isDone ? (hasFailures ? <AlertTriangle size={16} /> : <CheckCircle size={16} />) : <Code2 size={16} />}
        </div>
        <span style={{ fontSize: '14px', fontWeight: '600', color: 'var(--text-primary)', flex: 1 }}>
          UAT Tests — script-based (gen + exec)
          {isDone && (
            <span style={{
              marginLeft: '10px', fontSize: '11px', fontWeight: '500',
              padding: '2px 10px', borderRadius: '20px',
              background: hasFailures ? 'rgba(224,108,108,0.1)' : 'rgba(76,175,125,0.1)',
              color: hasFailures ? 'var(--danger)' : 'var(--success)',
              border: hasFailures ? '1px solid rgba(224,108,108,0.3)' : '1px solid rgba(76,175,125,0.3)',
            }}>
              {run.passed}/{run.total} passed{run.failed ? `, ${run.failed} failed` : ''}
            </span>
          )}
        </span>
        {state === 'active' && !running && (
          <button onClick={handleRun} style={{
            display: 'flex', alignItems: 'center', gap: '6px',
            padding: '7px 16px', background: 'var(--accent)', color: 'white',
            border: 'none', borderRadius: '6px', fontSize: '12px', fontWeight: '600', cursor: 'pointer',
          }}>
            {isDone ? <RefreshCw size={12} /> : <ArrowRight size={12} />}
            {isDone ? 'Re-run Tests' : 'Run UAT Tests'}
          </button>
        )}
      </div>

      <div style={{ padding: '16px 20px' }}>
        {err && <p style={{ margin: '0 0 12px', fontSize: '12px', color: 'var(--danger)' }}>{err}</p>}

        {/* Script inputs — while the step is active and nothing is streaming.
            Only admin/tech_lead may choose WHICH script runs (server-enforced);
            everyone else runs the operator-configured default. */}
        {state === 'active' && !running && (
          <div style={{ display: 'flex', gap: '12px', alignItems: 'center', flexWrap: 'wrap', marginBottom: '12px' }}>
            {allowPick ? (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '4px', flex: '2 1 300px' }}>
                <label style={{ fontSize: '10px', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.5px' }}>
                  test script (under PHASE_B_SCRIPT_ROOT)
                </label>
                <input
                  value={scriptPath}
                  onChange={e => setScriptPath(e.target.value)}
                  placeholder="e.g. nlln/run_uat_tests.sh — empty = configured default"
                  style={inputStyle}
                />
              </div>
            ) : (
              <p style={{ margin: 0, fontSize: '11.5px', color: 'var(--text-muted)', flex: '2 1 300px' }}>
                Runs the operator-configured UAT test script. Choosing a different
                script requires a tech lead or admin.
              </p>
            )}
            <div style={{ display: 'flex', flexDirection: 'column', gap: '4px', flex: '1 1 220px' }}>
              <label style={{ fontSize: '10px', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.5px' }}>
                base URL of the deployment (optional, passed as $1)
              </label>
              <input
                value={baseUrl}
                onChange={e => setBaseUrl(e.target.value)}
                placeholder="https://uat.example.internal"
                style={inputStyle}
              />
            </div>
          </div>
        )}

        {!running && !run && state === 'active' && (
          <p style={{ margin: 0, fontSize: '12px', color: 'var(--text-muted)', textAlign: 'center', padding: '8px 16px' }}>
            One script generates and executes the UAT suite; its output streams here live
            (<code style={{ background: 'var(--bg-base)', padding: '1px 6px', borderRadius: '3px' }}>PASS/FAIL</code> per
            case + a <code style={{ background: 'var(--bg-base)', padding: '1px 6px', borderRadius: '3px' }}>TESTS:</code> summary).
            Failures continue to Triage — that is where they get looked at.
          </p>
        )}

        {/* Summary chips */}
        {isDone && (
          <div style={{
            display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(100px, 1fr))', gap: '10px',
            marginBottom: '12px', padding: '12px 14px',
            background: hasFailures ? 'rgba(224,108,108,0.06)' : 'rgba(76,175,125,0.06)',
            border: hasFailures ? '1px solid rgba(224,108,108,0.2)' : '1px solid rgba(76,175,125,0.2)',
            borderRadius: '6px', fontSize: '12px',
          }}>
            <div><span style={{ color: 'var(--text-muted)', display: 'block', fontSize: '10px' }}>Total</span><span style={{ fontWeight: '600' }}>{run.total ?? '—'}</span></div>
            <div><span style={{ color: 'var(--text-muted)', display: 'block', fontSize: '10px' }}>Passed</span><span style={{ color: 'var(--success)', fontWeight: '600' }}>{run.passed ?? '—'}</span></div>
            <div><span style={{ color: 'var(--text-muted)', display: 'block', fontSize: '10px' }}>Failed</span><span style={{ color: run.failed ? 'var(--danger)' : 'var(--text-secondary)', fontWeight: '600' }}>{run.failed ?? '—'}</span></div>
            <div><span style={{ color: 'var(--text-muted)', display: 'block', fontSize: '10px' }}>Skipped</span><span style={{ fontWeight: '600' }}>{run.skipped ?? '—'}</span></div>
            {run.script_path && (
              <div style={{ gridColumn: 'span 2' }}>
                <span style={{ color: 'var(--text-muted)', display: 'block', fontSize: '10px' }}>Script</span>
                <span style={{ fontFamily: 'monospace', fontSize: '11px' }}>{run.script_path}</span>
              </div>
            )}
          </div>
        )}

        {/* Live / final log pane */}
        {(running || run?.log) && (
          <div>
            <div style={{
              display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '6px',
              fontSize: '11px', fontWeight: '600', color: 'var(--text-muted)',
              textTransform: 'uppercase', letterSpacing: '0.5px',
            }}>
              {running
                ? <Loader size={11} style={{ color: 'var(--accent)', animation: 'spin 1s linear infinite' }} />
                : hasFailures
                  ? <AlertTriangle size={11} style={{ color: 'var(--danger)' }} />
                  : <CheckCircle size={11} style={{ color: 'var(--success)' }} />}
              {running ? 'Test script — streaming…' : 'Test script — output'}
            </div>
            <pre ref={logPreRef} style={{
              background: '#0d1117',
              padding: '12px 14px', borderRadius: '6px',
              fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
              fontSize: '11.5px', lineHeight: '1.55',
              maxHeight: '440px', overflowY: 'auto', overflowX: 'auto',
              whiteSpace: 'pre-wrap', wordBreak: 'break-word', margin: 0,
              border: '1px solid #30363d',
            }}>
              {run?.log ? (
                <>
                  {tail.omitted > 0 && (
                    <div style={{ color: '#8b949e' }}>
                      [… {tail.omitted} earlier lines omitted — the full stream is kept server-side]
                    </div>
                  )}
                  {tail.lines.map((line, i) => (
                    <div key={i} style={{ color: uatLineColor(line) }}>{line || ' '}</div>
                  ))}
                </>
              ) : <span style={{ color: '#8b949e' }}>Waiting for the first output lines…</span>}
            </pre>
          </div>
        )}
      </div>
    </div>
  )
}


// ── Triage panel (AI over the build + UAT logs, plus the walkthrough) ─────────
// "Run AI Triage" reads the latest build+deploy log and UAT script log,
// classifies every visible failure (code bug / test-case issue / environment
// issue) with quoted evidence, and shows the plain-language developer +
// tester walkthrough beside it. Completing the step stays a human decision.

const TRIAGE_CLASS_STYLE = {
  code_bug:        { label: 'Code bug',        bg: 'rgba(224,108,108,0.12)', fg: 'var(--danger)' },
  test_case_issue: { label: 'Test case issue', bg: 'rgba(217,119,6,0.12)',   fg: '#d97706' },
  env_issue:       { label: 'Env issue',       bg: 'rgba(91,141,239,0.12)',  fg: '#5b8def' },
}

const TRIAGE_NEXT_LABEL = {
  proceed:   'Proceed — nothing blocking',
  fix_code:  'Fix the code',
  fix_tests: 'Fix the test cases',
  fix_env:   'Fix the environment',
}

function TriagePanel({ changeId, currentStep, onRefetchRun }) {
  const state = getStepState('triage', currentStep)

  const [runningAi, setRunningAi]   = useState(false)
  const [completing, setCompleting] = useState(false)
  const [err, setErr]               = useState(null)

  const { data: triage, refetch } = useQuery({
    queryKey: ['phase-b-triage', changeId],
    queryFn:  () => phaseBApi.getTriage(changeId).then(r => r.data),
    retry:    false,
    enabled:  state !== 'pending',
  })

  if (state === 'pending') return null

  const report = triage?.report
  const wt     = triage?.walkthrough
  const isPass = report?.overall === 'pass'

  const handleRunAi = async () => {
    setRunningAi(true); setErr(null)
    try {
      await phaseBApi.runTriage(changeId)
      await refetch()
    } catch (e) {
      setErr(e.response?.data?.detail || 'AI triage failed')
    } finally {
      setRunningAi(false)
    }
  }

  const handleComplete = async () => {
    setCompleting(true); setErr(null)
    try {
      await phaseBApi.advanceStep(changeId)
      await onRefetchRun()
    } catch (e) {
      setErr(e.response?.data?.detail || 'Could not complete triage')
    } finally {
      setCompleting(false)
    }
  }

  const sectionTitle = (text) => (
    <div style={{
      fontSize: '11px', fontWeight: '700', color: 'var(--text-muted)',
      textTransform: 'uppercase', letterSpacing: '0.5px', margin: '14px 0 6px',
    }}>{text}</div>
  )

  return (
    <div style={{
      marginBottom: '24px', background: 'var(--bg-elevated)',
      border: '1px solid var(--border)', borderRadius: '8px', overflow: 'hidden',
    }}>
      <div style={{
        padding: '14px 20px', borderBottom: '1px solid var(--border-subtle)',
        display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap',
      }}>
        <div style={{ color: state === 'done' ? 'var(--success)' : 'var(--accent)' }}>
          {state === 'done' ? <CheckCircle size={16} /> : <Search size={16} />}
        </div>
        <span style={{ fontSize: '14px', fontWeight: '600', color: 'var(--text-primary)', flex: 1 }}>
          AI Triage — logs, verdicts &amp; walkthrough
          {report && (
            <span style={{
              marginLeft: '10px', fontSize: '11px', fontWeight: '600',
              padding: '2px 10px', borderRadius: '20px',
              background: isPass ? 'rgba(76,175,125,0.1)' : 'rgba(217,119,6,0.12)',
              color: isPass ? 'var(--success)' : '#d97706',
              border: isPass ? '1px solid rgba(76,175,125,0.3)' : '1px solid rgba(217,119,6,0.35)',
            }}>
              {isPass ? 'All clear' : `${(report.findings || []).length || 'Issues'} finding${(report.findings || []).length === 1 ? '' : 's'}`}
            </span>
          )}
        </span>
        {!runningAi && state === 'active' && (
          <div style={{ display: 'flex', gap: '8px' }}>
            <button onClick={handleRunAi} style={{
              display: 'flex', alignItems: 'center', gap: '6px',
              padding: '7px 16px', background: 'var(--accent)', color: 'white',
              border: 'none', borderRadius: '6px', fontSize: '12px', fontWeight: '600', cursor: 'pointer',
            }}>
              {report ? <RefreshCw size={12} /> : <Search size={12} />}
              {report ? 'Re-run AI Triage' : 'Run AI Triage'}
            </button>
            {report && (
              <button onClick={handleComplete} disabled={completing} style={{
                display: 'flex', alignItems: 'center', gap: '6px',
                padding: '7px 16px', background: 'var(--success)', color: 'white',
                border: 'none', borderRadius: '6px', fontSize: '12px', fontWeight: '600',
                cursor: completing ? 'wait' : 'pointer', opacity: completing ? 0.7 : 1,
              }}>
                <CheckCircle size={12} /> Complete Triage
              </button>
            )}
          </div>
        )}
      </div>

      <div style={{ padding: '16px 20px' }}>
        {err && <p style={{ margin: '0 0 12px', fontSize: '12px', color: 'var(--danger)' }}>{err}</p>}

        {runningAi && (
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px', padding: '20px', justifyContent: 'center' }}>
            <Loader size={16} style={{ color: 'var(--accent)', animation: 'spin 1s linear infinite' }} />
            <span style={{ fontSize: '13px', color: 'var(--text-muted)' }}>
              AI is reading the build + test logs and preparing the walkthrough…
            </span>
          </div>
        )}

        {!runningAi && !report && state === 'active' && (
          <p style={{ margin: 0, fontSize: '13px', color: 'var(--text-muted)', textAlign: 'center', padding: '16px' }}>
            AI triages the Build + Deploy log and the UAT test log — each failure is classified
            (code bug / test case issue / environment issue) with quoted evidence — and the
            developer &amp; tester walkthrough of the change is shown alongside.
          </p>
        )}

        {!runningAi && report && (
          <>
            {/* Overall verdict */}
            <div style={{
              padding: '12px 14px', borderRadius: '6px', fontSize: '12.5px', lineHeight: 1.6,
              background: isPass ? 'rgba(76,175,125,0.06)' : 'rgba(217,119,6,0.07)',
              border: isPass ? '1px solid rgba(76,175,125,0.2)' : '1px solid rgba(217,119,6,0.3)',
              color: 'var(--text-secondary)',
            }}>
              <strong style={{ color: isPass ? 'var(--success)' : '#d97706' }}>
                {isPass ? '✓ No blocking issues found. ' : '⚠ Issues found. '}
              </strong>
              {report.summary}
              {report.next_action && (
                <div style={{ marginTop: '6px', fontSize: '11.5px', color: 'var(--text-muted)' }}>
                  Recommended next action: <strong>{TRIAGE_NEXT_LABEL[report.next_action] || report.next_action}</strong>
                  {report.ai === false && ' · (deterministic summary — AI was unavailable)'}
                </div>
              )}
            </div>

            {/* Findings */}
            {(report.findings || []).length > 0 && (
              <>
                {sectionTitle(`Findings (${report.findings.length})`)}
                <div style={{ border: '1px solid var(--border)', borderRadius: '6px', overflow: 'hidden' }}>
                  {report.findings.map((f, i) => {
                    const cls = TRIAGE_CLASS_STYLE[f.classification] || TRIAGE_CLASS_STYLE.env_issue
                    return (
                      <div key={i} style={{
                        padding: '10px 14px', fontSize: '12px', lineHeight: 1.6,
                        borderTop: i ? '1px solid var(--border-subtle)' : 'none',
                        background: i % 2 ? 'var(--bg-base)' : 'transparent',
                      }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap', marginBottom: '4px' }}>
                          <span style={{
                            fontSize: '10px', fontWeight: '700', padding: '2px 8px', borderRadius: '10px',
                            background: cls.bg, color: cls.fg, textTransform: 'uppercase', letterSpacing: '0.5px',
                          }}>{cls.label}</span>
                          <span style={{ fontSize: '10px', color: 'var(--text-muted)', textTransform: 'uppercase' }}>
                            source: {f.source}{f.test_id ? ` · ${f.test_id}` : ''}
                          </span>
                        </div>
                        <div style={{ color: 'var(--text-secondary)' }}>{f.reasoning}</div>
                        {f.evidence && (
                          <pre style={{
                            margin: '6px 0', padding: '8px 10px', borderRadius: '4px',
                            background: '#0d1117', color: '#c9d1d9', fontSize: '10.5px',
                            overflowX: 'auto', whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                          }}>{f.evidence}</pre>
                        )}
                        {f.remediation && (
                          <div style={{ fontSize: '11.5px', color: 'var(--text-muted)' }}>
                            <strong>Fix:</strong> {f.remediation}
                          </div>
                        )}
                      </div>
                    )
                  })}
                </div>
              </>
            )}

            {/* Dev + tester walkthrough */}
            {sectionTitle('Developer & tester walkthrough')}
            {!wt && (
              <p style={{ margin: 0, fontSize: '12px', color: 'var(--text-muted)' }}>
                No walkthrough could be generated — the change's diff is no longer available
                (workspace cleaned up) and none was stored on the agentic run.
              </p>
            )}
            {wt && (
              <div style={{ border: '1px solid var(--border)', borderRadius: '6px', padding: '12px 16px', fontSize: '12.5px', lineHeight: 1.65 }}>
                {wt.summary && <p style={{ margin: '0 0 8px', color: 'var(--text-secondary)' }}>{wt.summary}</p>}
                {wt.api_surface && (
                  <p style={{ margin: '0 0 8px', color: 'var(--text-secondary)' }}>
                    <strong>API surface:</strong> {wt.api_surface}
                  </p>
                )}
                {(wt.flow || []).length > 0 && (
                  <>
                    <strong style={{ fontSize: '11.5px', color: 'var(--text-primary)' }}>Runtime flow</strong>
                    <ol style={{ margin: '4px 0 10px', paddingLeft: '20px', color: 'var(--text-secondary)' }}>
                      {wt.flow.map((s, i) => <li key={i} style={{ marginBottom: '2px' }}>{s}</li>)}
                    </ol>
                  </>
                )}
                {(wt.decision_points || []).length > 0 && (
                  <>
                    <strong style={{ fontSize: '11.5px', color: 'var(--text-primary)' }}>Decision points</strong>
                    <div style={{ border: '1px solid var(--border-subtle)', borderRadius: '5px', overflow: 'hidden', margin: '4px 0 10px' }}>
                      {wt.decision_points.map((d, i) => (
                        <div key={i} style={{
                          display: 'grid', gridTemplateColumns: '110px 1fr 1fr', gap: '8px',
                          padding: '6px 10px', fontSize: '11.5px',
                          borderTop: i ? '1px solid var(--border-subtle)' : 'none',
                          background: i % 2 ? 'var(--bg-base)' : 'transparent',
                        }}>
                          <span style={{ fontFamily: 'monospace', color: 'var(--accent)' }}>{d.code}</span>
                          <span style={{ color: 'var(--text-secondary)' }}>{d.when}</span>
                          <span style={{ color: 'var(--text-muted)' }}>{d.result}</span>
                        </div>
                      ))}
                    </div>
                  </>
                )}
                {(wt.tester_scenarios || []).length > 0 && (
                  <>
                    <strong style={{ fontSize: '11.5px', color: 'var(--text-primary)' }}>Tester scenarios</strong>
                    <div style={{ border: '1px solid var(--border-subtle)', borderRadius: '5px', overflow: 'hidden', margin: '4px 0 10px' }}>
                      <div style={{
                        display: 'grid', gridTemplateColumns: '30px 1fr 1.2fr 1.2fr',
                        padding: '6px 10px', fontSize: '10px', fontWeight: '700',
                        color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.5px',
                        background: 'var(--bg-base)', borderBottom: '1px solid var(--border-subtle)', gap: '8px',
                      }}>
                        <span>#</span><span>Scenario</span><span>Input</span><span>Expected</span>
                      </div>
                      {wt.tester_scenarios.map((s, i) => (
                        <div key={i} style={{
                          display: 'grid', gridTemplateColumns: '30px 1fr 1.2fr 1.2fr', gap: '8px',
                          padding: '6px 10px', fontSize: '11.5px',
                          borderTop: i ? '1px solid var(--border-subtle)' : 'none',
                          background: i % 2 ? 'var(--bg-base)' : 'transparent',
                        }}>
                          <span style={{ color: 'var(--text-muted)' }}>{s.id ?? i + 1}</span>
                          <span style={{ color: 'var(--text-primary)' }}>{s.scenario}</span>
                          <span style={{ color: 'var(--text-secondary)' }}>{s.input}</span>
                          <span style={{ color: 'var(--text-secondary)' }}>{s.expected}</span>
                        </div>
                      ))}
                    </div>
                  </>
                )}
                {(wt.caveats || []).length > 0 && (
                  <>
                    <strong style={{ fontSize: '11.5px', color: 'var(--text-primary)' }}>Caveats</strong>
                    <ul style={{ margin: '4px 0 0', paddingLeft: '20px', color: 'var(--text-muted)' }}>
                      {wt.caveats.map((c, i) => <li key={i} style={{ marginBottom: '2px' }}>{c}</li>)}
                    </ul>
                  </>
                )}
              </div>
            )}
          </>
        )}

        {!runningAi && !report && state === 'done' && (
          <div style={{
            padding: '16px', borderRadius: '6px', textAlign: 'center',
            background: 'rgba(76,175,125,0.06)', border: '1px solid rgba(76,175,125,0.2)',
          }}>
            <CheckCircle size={24} style={{ color: 'var(--success)', marginBottom: '8px' }} />
            <p style={{ margin: 0, fontSize: '13px', color: 'var(--text-secondary)' }}>
              Triage completed.
            </p>
          </div>
        )}
      </div>
    </div>
  )
}


// ── Main page ─────────────────────────────────────────────────────────────────

export default function PhaseB() {
  const { id } = useParams()
  const navigate = useNavigate()
  const qc = useQueryClient()

  const [feedback, setFeedback] = useState('')
  const [started, setStarted] = useState(false)
  const [streaming, setStreaming] = useState(false)
  const [streamingText, setStreamingText] = useState('')
  const [connected, setConnected] = useState(false)
  const [error, setError] = useState(null)
  const [iterations, setIterations] = useState([])   // {role, content, iteration?, files?}
  const [approving, setApproving] = useState(false)
  const [showIngest, setShowIngest] = useState(false)
  const [fixInProgress, setFixInProgress] = useState(false)  // true after loop-back until new code is generated

  const wsRef    = useRef(null)
  const bufRef   = useRef('')
  const bottomRef = useRef(null)

  // ── Queries ─────────────────────────────────────────────────────────────────
  const { data: change } = useQuery({
    queryKey: ['change', id],
    queryFn:  () => changesApi.get(id).then(r => r.data),
  })

  // Agentic Phase B (THE BOOK v3.4): when the change opts into the agentic engine,
  // the code-change step is driven by a `code` agentic run that adopts Phase A's
  // approved-XSD workspace and produces ONE combined MR (XSD + Java).
  const { user: me } = useAuth()
  // Agentic codegen is admin + tech-lead only — others get the legacy Phase-B pipeline.
  // Agentic is the ONLY codegen path for admin/tech-lead — no per-change opt-in.
  const agenticEnabled = (me?.role === 'admin' || me?.role === 'tech_lead')
  const { data: agRepos } = useQuery({
    queryKey: ['phase-b-repos', id],
    queryFn:  () => phaseBApi.listGitRepos(id).then(r => r.data),
    enabled:  !!id && agenticEnabled,
  })
  // Latest code run state (survives reloads) — drives the "Continue to next stage"
  // button once the change is approved, whether the push happened or was deferred.
  const { data: agCodeRuns } = useQuery({
    queryKey: ['agentic-code-runs', id],
    queryFn:  () => agenticApi.listChangeRuns(id, 'code').then(r => r.data),
    enabled:  !!id && agenticEnabled,
    refetchInterval: 15_000,
  })
  const [agAdvancing, setAgAdvancing] = useState(false)
  const [govStarting, setGovStarting] = useState(false)
  const agLatestRun = agCodeRuns?.runs?.[0]
  const agApproved = !!agLatestRun && agLatestRun.status === 'completed'
    && (agLatestRun.pushed || agLatestRun.push_deferred || agLatestRun.phase === 'completed')
  // Governance stages (EA → InfoSec) sit between approval and Build. Derived
  // server-side from the stage runs; poll faster while a stage is live.
  const { data: govStatus, refetch: refetchGov } = useQuery({
    queryKey: ['governance-status', id],
    queryFn:  () => governanceApi.status(id),
    enabled:  !!id && agenticEnabled && agApproved,
    refetchInterval: (q) => {
      const d = q.state.data
      const live = d?.enabled && d?.started && !d?.all_passed
      return live ? 5_000 : 30_000
    },
  })
  const govEnabled = !!govStatus?.enabled
  const handleGovStart = async () => {
    setGovStarting(true)
    try {
      await governanceApi.start(id)
      await refetchGov()
    } catch (err) {
      setError(err.response?.data?.detail || 'Could not start governance reviews')
    } finally {
      setGovStarting(false)
    }
  }
  const handleAgProceed = async () => {
    setAgAdvancing(true)
    try {
      // The agentic run replaced code_change/code_review/git — hand the pipeline
      // over at BUILD. The page then falls through to the legacy stepper.
      await phaseBApi.agenticComplete(id)
      await refetchRun()
      qc.invalidateQueries({ queryKey: ['phase-b', id] })
    } catch (err) {
      setError(err.response?.data?.detail || 'Could not advance the stage')
    } finally {
      setAgAdvancing(false)
    }
  }

  const { data: runData, refetch: refetchRun } = useQuery({
    queryKey: ['phase-b', id],
    queryFn:  () => phaseBApi.get(id).then(r => r.data),
    retry:    false,
    enabled:  !!id,
  })

  // ── WS connect ──────────────────────────────────────────────────────────────
  const connectWS = useCallback(() => {
    if (wsRef.current) return
    const ws = new WebSocket(wsUrl(`api/ws/changes/${id}/phase-b/code`))
    wsRef.current = ws

    ws.onopen = () => {
      setConnected(true)
      setError(null)
      // Session rides the handshake cookie (httpOnly); hello frame only.
      ws.send(JSON.stringify({}))
    }
    ws.onclose = () => { setConnected(false); wsRef.current = null }
    ws.onerror = () => { setError('WebSocket connection failed'); setConnected(false) }

    ws.onmessage = evt => {
      const data = JSON.parse(evt.data)

      if (data.type === 'history') {
        // Restore prior code iterations as conversation entries
        const msgs = (data.messages || []).filter(m => m.role === 'assistant')
        setIterations(msgs.map(m => ({
          role: 'assistant',
          content: m.content,
          iteration: m.iteration,
          files: [], // files will be re-parsed from content
        })))
        return
      }
      if (data.type === 'chunk') {
        bufRef.current += data.text
        setStreamingText(bufRef.current)
        setStreaming(true)
        return
      }
      if (data.type === 'done') {
        const full = data.full || bufRef.current
        bufRef.current = ''
        setStreamingText('')
        setStreaming(false)
        setFixInProgress(false)
        setIterations(prev => [...prev, {
          role:      'assistant',
          content:   full,
          iteration: data.iteration,
          files:     data.files || [],
        }])
        refetchRun()
        return
      }
      if (data.type === 'error') {
        setError(data.detail || 'Error from server')
        setStreaming(false)
        bufRef.current = ''
        setStreamingText('')
      }
    }
  }, [id, refetchRun])

  // Connect WS when run exists and step is code_change (includes loop-back reconnect)
  const needsWS = !!runData && (runData.current_step === 'code_change')
  useEffect(() => {
    if (runData) {
      if (needsWS && !wsRef.current) {
        connectWS()
      }
    }
    return () => { wsRef.current?.close(); wsRef.current = null }
  }, [!!runData, needsWS, connectWS])

  useEffect(() => {
    if (streaming) bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [streamingText, streaming])

  // ── Actions ──────────────────────────────────────────────────────────────────
  const handleStart = async () => {
    setStarted(true)
    if (runData) {
      // Run already exists — if WS is connected send immediately, else queue
      if (connected && wsRef.current) {
        sendMsg('start')
      } else {
        pendingStartRef.current = true
      }
      return
    }
    try {
      // M-2/M-5 — pass ALL registered repos as repo_ids by default so the run
      // is multi-repo-aware from the start. Operators can de-select repos at
      // git-push time via the GitPanel checkboxes if they want a narrower push.
      // The branch is auto-generated as `change-<short>/iter-1` and shared
      // across every selected repo.
      let repoIds = []
      try {
        const reposRes = await phaseBApi.listGitRepos(id)
        repoIds = (reposRes.data || []).map(r => r.id)
      } catch (e) {
        // Non-fatal — fall through to legacy single-repo path on backend
        console.warn('Could not list registered repos; starting Phase B without multi-repo binding', e)
      }
      const branchName = `change-${id.slice(0, 8)}/iter-1`
      await phaseBApi.start(id, {
        repo_ids:        repoIds.length ? repoIds : undefined,
        branch_override: repoIds.length ? branchName : undefined,
      })
      await refetchRun()
      // WS will connect via useEffect after runData is set, then auto-send 'start'
      pendingStartRef.current = true
    } catch (e) {
      setError(e.response?.data?.detail || 'Failed to start Phase B')
      setStarted(false)
    }
  }

  // After run exists and WS connects we need to send start if user clicked it
  const pendingStartRef = useRef(false)
  const pendingFixRef = useRef(null)  // holds fix message to auto-send after loop-back reconnect
  useEffect(() => {
    if (connected && wsRef.current) {
      if (pendingStartRef.current) {
        pendingStartRef.current = false
        sendMsg('start')
      }
      if (pendingFixRef.current) {
        const fixMsg = pendingFixRef.current
        pendingFixRef.current = null
        setIterations(prev => [...prev, { role: 'user', content: fixMsg }])
        sendMsg(fixMsg)
        setFeedback('')
      }
    }
  }, [connected])

  const sendMsg = (text) => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return
    wsRef.current.send(JSON.stringify({ message: text }))
  }

  const handleFeedback = () => {
    const text = feedback.trim()
    if (!text || streaming) return
    setFeedback('')
    setIterations(prev => [...prev, { role: 'user', content: text }])
    sendMsg(text)
  }

  const handleApprove = async () => {
    if (!runData || approving) return
    const latestIteration = runData.iteration_count
    if (!latestIteration) return
    setApproving(true)
    try {
      await phaseBApi.approveIteration(id, latestIteration)
      await refetchRun()
      qc.invalidateQueries(['phase-b', id])
    } catch (e) {
      setError(e.response?.data?.detail || 'Approval failed')
    } finally {
      setApproving(false)
    }
  }

  const handleLoopBackComplete = useCallback((reviewType, summary) => {
    // Close old WS so the reconnect effect fires a fresh connection
    if (wsRef.current) {
      wsRef.current.close()
      wsRef.current = null
    }
    setConnected(false)
    setFixInProgress(true)
    // Queue the fix message to auto-send once WS reconnects
    const fixMsg = `Fix the following ${reviewType} issues and regenerate all affected files:\n${summary}`
    pendingFixRef.current = fixMsg
    setFeedback(fixMsg)
  }, [])

  // Phase-A gate for the direct URL. ChangeDetail already hides the entry
  // to this page (see ChangeDetail.jsx:3343), so this only fires when
  // someone deep-links /changes/:id/phase-b before Phase A is complete.
  if (change && change.status !== 'completed') {
    return (
      <PhaseALockedNotice
        changeId={id}
        changeTitle={change.title}
        phaseLabel="Phase B — Design to Build"
        navigate={navigate}
      />
    )
  }

  // ── Derived ──────────────────────────────────────────────────────────────────
  const lastAssistantEntry = [...iterations].reverse().find(m => m.role === 'assistant')
  const hasOutput = !!lastAssistantEntry
  // test_exec (legacy in-flight rows) folds into the combined UAT node.
  const currentStep = stepAlias(runData?.current_step || 'code_change')
  const iterationCount = runData?.iteration_count || 0
  const approved = currentStep !== 'code_change'

  // ── Agentic Phase B (THE BOOK v3.4) — focused view, bypasses the legacy pipeline
  // only while the pipeline is still at code_change: the agentic run replaces
  // code_change/code_review/git, and "Continue to next stage" hands over to the
  // legacy stepper at BUILD (build+deploy, UAT gen/exec/triage stay legacy).
  if (agenticEnabled) {
    const repoIds = (Array.isArray(agRepos) ? agRepos : []).map(r => r.id)
    const agIntent = change?.enhanced_prompt || change?.title || change?.initial_prompt || ''
    // ONE stepper for the whole of Phase B. The agentic run owns Code/Review/Git
    // (shown by the panel); Build→UAT→Triage are the existing legacy panels, which
    // light up only after the handover (`agentic-complete` creates the legacy run at
    // BUILD). `currentStep` is the legacy run's step — null/`code_change` before the
    // handover. Before handover the bar tracks the AGENTIC phase (Code→Review→Git);
    // once approved+pushed it sits at Build, then follows the legacy run.
    const handedOff = !!runData && currentStep !== 'code_change'
    // With governance enabled, the bar walks EA → InfoSec between Git and Build.
    const govPendingStep = govEnabled && agApproved && !govStatus?.all_passed
      ? (govStatus?.ea?.passed ? 'infosec_review' : 'ea_review') : null
    const barStep = handedOff ? currentStep
      : (agApproved ? (govPendingStep || 'build') : agenticBarStep(agLatestRun))
    return (
      <div style={{ display: 'flex', flexDirection: 'column', height: 'calc(100vh - 0px)', overflow: 'hidden' }}>
        {/* Header */}
        <div style={{
          padding: '16px 24px', borderBottom: '1px solid var(--border)',
          display: 'flex', alignItems: 'center', gap: '16px', flexShrink: 0, background: 'var(--bg-base)',
        }}>
          <button onClick={() => navigate(`/changes/${id}`)} style={{
            display: 'flex', alignItems: 'center', gap: '5px', background: 'none',
            border: 'none', cursor: 'pointer', fontSize: '13px', color: 'var(--text-muted)',
          }}>
            <ArrowLeft size={14} /> Back
          </button>
          <div style={{ flex: 1 }}>
            <h1 style={{ margin: 0, fontSize: '16px', fontWeight: '600', color: 'var(--text-primary)' }}>
              Phase B — Agentic code generation
            </h1>
            <p style={{ margin: 0, fontSize: '11px', color: 'var(--text-muted)' }}>
              {change?.title || 'Loading…'} · the agent writes, verifies, reviews + raises the MR; then build → UAT
            </p>
          </div>
          <TranscriptsDownloadButton changeId={id} />
        </div>

        {/* One pipeline bar for the whole phase */}
        <div style={{ borderBottom: '1px solid var(--border)', background: 'var(--bg-base)', flexShrink: 0 }}>
          <PipelineBar currentStep={barStep} steps={govEnabled ? STEPS_GOV : STEPS} />
        </div>

        <div style={{ flex: 1, overflowY: 'auto', padding: '24px' }}>
          {/* Code + Review + Git — driven by the agentic run (no legacy 'Generate Code' CTA). */}
          <div style={{
            background: 'var(--bg-elevated)', border: '1px solid var(--border)',
            borderRadius: '12px', padding: '20px 24px',
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
              <Code2 size={15} style={{ color: 'var(--accent)' }} />
              <strong style={{ fontSize: 14 }}>Code · Review · Git</strong>
              <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>— handled by the agent: XSD + code together (one branch/MR per repo)</span>
            </div>
            <AgenticPhasePanel changeId={id} kind="code" repoIds={repoIds} intent={agIntent}
              onApproved={() => qc.invalidateQueries({ queryKey: ['agentic-code-runs', id] })} />
          </div>
          {error && (
            <div style={{ marginTop: '12px', padding: '10px 14px', borderRadius: '8px', fontSize: '13px',
              background: 'rgba(239,68,68,0.08)', border: '1px solid rgba(239,68,68,0.4)', color: '#ef4444' }}>
              {String(error)}
            </div>
          )}

          {/* Approved but not yet moved on. With governance enabled the path is:
              Start governance reviews → EA card → InfoSec card → Build unlocks.
              With it disabled (flag off) this is the original one-click handover. */}
          {agApproved && !handedOff && (
            <div style={{
              marginTop: '16px', padding: '14px 18px', borderRadius: '12px', display: 'flex',
              alignItems: 'center', gap: '14px', flexWrap: 'wrap',
              background: 'rgba(52,211,153,0.08)', border: '1px solid rgba(52,211,153,0.35)',
            }}>
              <div style={{ flex: 1, minWidth: 240 }}>
                <strong style={{ fontSize: '14px', color: '#16a34a' }}>✓ Code change approved</strong>
                <div style={{ fontSize: '12.5px', color: 'var(--text-muted)', marginTop: 2 }}>
                  {govEnabled && !govStatus?.all_passed
                    ? (govStatus?.started
                        ? 'Governance reviews in progress below — Build unlocks when both stages pass.'
                        : 'Next: the EA and InfoSec governance reviews run on the approved code, fixing what they find. Build unlocks after both pass.')
                    : agLatestRun?.push_deferred
                      ? 'Git push is deferred — push anytime from the panel above. You can move to Build now.'
                      : 'The branch and merge request are on git. Move to Build + Deploy when ready.'}
                </div>
                {govEnabled && !govStatus?.skills_ready && !govStatus?.started && (
                  <div style={{ fontSize: '11.5px', color: '#d97706', marginTop: 4 }}>
                    ⚠ An admin must upload both governance skills (Admin → Governance Skills) before the reviews can start.
                  </div>
                )}
              </div>
              {govEnabled && !govStatus?.all_passed ? (
                !govStatus?.started && (
                  <button onClick={handleGovStart} disabled={govStarting || !govStatus?.skills_ready} style={{
                    display: 'flex', alignItems: 'center', gap: '6px', flexShrink: 0,
                    padding: '9px 18px', background: 'var(--accent)', color: 'white',
                    border: 'none', borderRadius: '8px', fontSize: '13px', fontWeight: 600,
                    cursor: govStarting ? 'wait' : !govStatus?.skills_ready ? 'not-allowed' : 'pointer',
                    opacity: (govStarting || !govStatus?.skills_ready) ? 0.6 : 1,
                  }}>
                    {govStarting ? <Loader size={14} style={{ animation: 'spin 1s linear infinite' }} /> : <Shield size={14} />}
                    Start governance reviews
                  </button>
                )
              ) : (
                <button onClick={handleAgProceed} disabled={agAdvancing} style={{
                  display: 'flex', alignItems: 'center', gap: '6px', flexShrink: 0,
                  padding: '9px 18px', background: 'var(--accent)', color: 'white',
                  border: 'none', borderRadius: '8px', fontSize: '13px', fontWeight: 600,
                  cursor: agAdvancing ? 'wait' : 'pointer', opacity: agAdvancing ? 0.6 : 1,
                }}>
                  {agAdvancing ? <Loader size={14} style={{ animation: 'spin 1s linear infinite' }} /> : <ArrowRight size={14} />}
                  Continue to Build + Deploy
                </button>
              )}
            </div>
          )}

          {/* Governance stage cards — subtle, with the same observability as codegen. */}
          {govEnabled && agApproved && !handedOff && govStatus?.started && (
            <>
              <GovernanceStageCard stage="ea" view={govStatus.ea} onChanged={refetchGov} changeId={id} />
              <GovernanceStageCard stage="infosec" view={govStatus.infosec} onChanged={refetchGov} changeId={id} />
              {/* A stage stopped (failed/cancelled) → offer the retry that /start provides. */}
              {['failed', 'gave_up', 'cancelled'].includes(govStatus?.ea?.status) ||
               ['failed', 'gave_up', 'cancelled'].includes(govStatus?.infosec?.status) ? (
                <button onClick={handleGovStart} disabled={govStarting} style={{
                  marginTop: 10, padding: '7px 16px', background: 'transparent', color: 'var(--accent)',
                  border: '1px solid var(--accent)', borderRadius: 8, fontSize: 12.5, fontWeight: 600,
                  cursor: govStarting ? 'wait' : 'pointer' }}>
                  {govStarting ? 'Restarting…' : 'Retry the stopped stage'}
                </button>
              ) : null}
            </>
          )}

          {/* Build → UAT → Triage — the existing legacy panels, live only after handover. */}
          {handedOff && (
            <div style={{ marginTop: '20px' }}>
              <BuildPanel changeId={id} currentStep={currentStep} onRefetchRun={refetchRun} />
              <UatTestPanel changeId={id} currentStep={currentStep} onRefetchRun={refetchRun} />
              <TriagePanel changeId={id} currentStep={currentStep} onRefetchRun={refetchRun} />
              {runData?.status === 'completed' && (
                <div style={{ marginTop: '16px', padding: '14px 18px', borderRadius: '12px',
                  background: 'rgba(52,211,153,0.08)', border: '1px solid rgba(52,211,153,0.35)' }}>
                  <strong style={{ fontSize: '14px', color: '#16a34a' }}>🎉 Phase B complete</strong>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    )
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: 'calc(100vh - 0px)', overflow: 'hidden' }}>

      {/* ── Header ── */}
      <div style={{
        padding: '16px 24px', borderBottom: '1px solid var(--border)',
        display: 'flex', alignItems: 'center', gap: '16px', flexShrink: 0,
        background: 'var(--bg-base)',
      }}>
        <button onClick={() => navigate(`/changes/${id}`)} style={{
          display: 'flex', alignItems: 'center', gap: '5px', background: 'none',
          border: 'none', cursor: 'pointer', fontSize: '13px', color: 'var(--text-muted)',
        }}>
          <ArrowLeft size={14} /> Back
        </button>
        <div style={{ flex: 1 }}>
          <h1 style={{ margin: 0, fontSize: '16px', fontWeight: '600', color: 'var(--text-primary)' }}>
            Phase B — Design to Build
          </h1>
          <p style={{ margin: 0, fontSize: '11px', color: 'var(--text-muted)' }}>
            {change?.title || 'Loading…'} · {STEPS.find(s => s.key === currentStep)?.label || currentStep}
          </p>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
          {connected
            ? <Wifi size={14} style={{ color: 'var(--success)' }} />
            : <WifiOff size={14} style={{ color: 'var(--danger)' }} />}
          <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>
            {connected ? 'Connected' : 'Connecting…'}
          </span>
        </div>
        {hasOutput && !streaming && !approved && !fixInProgress && (
          <button onClick={handleApprove} disabled={approving} style={{
            display: 'flex', alignItems: 'center', gap: '7px',
            padding: '8px 18px', background: 'var(--success)', color: 'white',
            border: 'none', borderRadius: '6px', fontSize: '13px', fontWeight: '600',
            cursor: approving ? 'not-allowed' : 'pointer', opacity: approving ? 0.7 : 1,
          }}>
            {approving ? <Loader size={13} style={{ animation: 'spin 1s linear infinite' }} /> : <ThumbsUp size={13} />}
            Approve & Proceed to Code Review
          </button>
        )}
        {approved && currentStep !== 'code_change' && (
          <div style={{
            display: 'flex', alignItems: 'center', gap: '6px', padding: '7px 14px',
            background: 'rgba(76,175,125,0.1)', border: '1px solid rgba(76,175,125,0.3)',
            borderRadius: '6px', fontSize: '12px', fontWeight: '600', color: 'var(--success)',
          }}>
            <CheckCircle size={13} /> Code Approved — {STEPS.find(s => s.key === currentStep)?.label || currentStep}
          </div>
        )}
      </div>

      {/* ── Pipeline bar ── */}
      {runData && (
        <div style={{ borderBottom: '1px solid var(--border)', background: 'var(--bg-base)', flexShrink: 0 }}>
          <PipelineBar currentStep={currentStep} />
        </div>
      )}

      {/* ── Main content ── */}
      <div style={{ flex: 1, overflowY: 'auto', padding: '24px' }}>

        {error && (
          <div style={{ padding: '12px 16px', borderRadius: '8px', marginBottom: '16px', background: 'rgba(224,108,108,0.10)', border: '1px solid rgba(224,108,108,0.3)' }}>
            <p style={{ margin: 0, fontSize: '13px', color: 'var(--danger)' }}>{error}</p>
          </div>
        )}

        {/* Not started / waiting for generation */}
        {iterations.length === 0 && !streaming && (
          <div style={{ textAlign: 'center', padding: '48px 32px', background: 'var(--bg-elevated)', border: '1px solid var(--border)', borderRadius: '12px' }}>
            {/* Waiting for connection after clicking start */}
            {started && !streaming && (
              <div style={{ marginBottom: '24px' }}>
                <Loader size={32} style={{ color: 'var(--accent)', animation: 'spin 1.5s linear infinite', marginBottom: '12px' }} />
                <p style={{ margin: 0, fontSize: '14px', color: 'var(--accent)', fontWeight: '600' }}>
                  {connected ? 'Generating code from Tech Spec and BRD…' : 'Connecting to AI agent…'}
                </p>
                <p style={{ margin: '8px 0 0', fontSize: '12px', color: 'var(--text-muted)' }}>
                  This may take 30–60 seconds. Output will stream below once ready.
                </p>
              </div>
            )}
            {/* Initial state — not started yet */}
            {!started && (
              <>
                <Code2 size={40} style={{ color: 'var(--accent)', marginBottom: '16px' }} />
                <h2 style={{ margin: '0 0 8px', fontSize: '18px', fontWeight: '600', color: 'var(--text-primary)' }}>
                  {runData ? 'Phase B — Generate Code' : 'Ready to start Phase B — Design to Build'}
                </h2>
                <p style={{ margin: '0 auto 24px', fontSize: '13px', color: 'var(--text-muted)', maxWidth: '480px' }}>
                  The AI will read your Tech Spec and BRD then generate Java/Spring Boot code changes.
                  {!runData && ' Optionally ingest the existing codebase first so the AI has full context.'}
                </p>
                <div style={{ display: 'flex', gap: '12px', justifyContent: 'center', flexWrap: 'wrap' }}>
                  <button onClick={() => setShowIngest(v => !v)} style={{
                    padding: '9px 20px', background: 'var(--bg-elevated)',
                    color: 'var(--text-secondary)', border: '1px solid var(--border)',
                    borderRadius: '8px', fontSize: '13px', fontWeight: '500', cursor: 'pointer',
                    display: 'flex', alignItems: 'center', gap: '7px',
                  }}>
                    <Database size={14} /> {showIngest ? 'Hide' : 'Ingest Codebase (Optional)'}
                  </button>
                  <button onClick={handleStart} style={{
                    padding: '9px 24px', background: 'var(--accent)', color: 'white',
                    border: 'none', borderRadius: '8px', fontSize: '14px', fontWeight: '600', cursor: 'pointer',
                  }}>
                    {runData ? 'Generate Code' : 'Start Code Generation'}
                  </button>
                </div>
                {showIngest && (
                  <div style={{ marginTop: '24px', textAlign: 'left' }}>
                    <IngestPanel changeId={id} onIngested={() => setShowIngest(false)} />
                  </div>
                )}
              </>
            )}
          </div>
        )}

        {/* Iteration list */}
        {iterations.map((entry, idx) => {
          if (entry.role === 'user') {
            return (
              <div key={idx} style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: '16px' }}>
                <div style={{
                  maxWidth: '60%', padding: '10px 14px', borderRadius: '10px 10px 3px 10px',
                  background: 'var(--accent)', color: 'white', fontSize: '13px', lineHeight: '1.6',
                }}>
                  {entry.content}
                </div>
              </div>
            )
          }

          // assistant — show full markdown + file viewer
          const { markdown, files: parsedFiles } = parseMarkdownAndFiles(entry.content)
          const files = entry.files?.length ? entry.files : parsedFiles

          return (
            <div key={idx} style={{ marginBottom: '24px' }}>
              <div style={{
                display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '10px',
              }}>
                <div style={{
                  padding: '3px 10px', borderRadius: '20px', fontSize: '11px', fontWeight: '600',
                  background: 'rgba(218,119,86,0.12)', color: 'var(--accent)',
                  border: '1px solid rgba(218,119,86,0.25)',
                }}>
                  Iteration {entry.iteration}
                </div>
                {files.length > 0 && (
                  <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>
                    {files.length} file{files.length !== 1 ? 's' : ''} generated
                  </span>
                )}
              </div>
              <div style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)', borderRadius: '8px', padding: '20px 24px' }}>
                <div className="md-content" style={{ fontSize: '13px', lineHeight: '1.8' }}>
                  <ReactMarkdown>{markdown}</ReactMarkdown>
                </div>
                {files.length > 0 && <FileViewer files={files} runRepos={runData?.repos} />}
              </div>
            </div>
          )
        })}

        {/* Streaming in progress */}
        {streaming && (
          <div style={{ marginBottom: '24px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '10px' }}>
              <div style={{ padding: '3px 10px', borderRadius: '20px', fontSize: '11px', fontWeight: '600', background: 'rgba(218,119,86,0.12)', color: 'var(--accent)', border: '1px solid rgba(218,119,86,0.25)' }}>
                Iteration {iterationCount + 1}
              </div>
              <Loader size={12} style={{ color: 'var(--accent)', animation: 'spin 1s linear infinite' }} />
              <span style={{ fontSize: '11px', color: 'var(--accent)' }}>Generating…</span>
            </div>
            <div style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)', borderRadius: '8px', padding: '20px 24px' }}>
              <div className="md-content" style={{ fontSize: '13px', lineHeight: '1.8' }}>
                <ReactMarkdown>{streamingText + '▌'}</ReactMarkdown>
              </div>
            </div>
          </div>
        )}

        {/* ── Code Review panel ── */}
        {runData && (
          <ReviewPanel type="code_review" changeId={id} currentStep={currentStep} onRefetchRun={refetchRun} onLoopBackComplete={handleLoopBackComplete} />
        )}

        {/* ── IS Review panel (disabled) ── */}

        {/* ── Git / MR panel ── */}
        {runData && (
          <GitPanel changeId={id} currentStep={currentStep} onRefetchRun={refetchRun} />
        )}

        {/* ── Build panel ── */}
        {runData && (
          <BuildPanel changeId={id} currentStep={currentStep} onRefetchRun={refetchRun} />
        )}

        {/* ── Pipeline step panels (UAT is ONE script-based step; Deploy is folded into Build) ── */}
        {runData && (
          <UatTestPanel
            changeId={id}
            currentStep={currentStep}
            onRefetchRun={refetchRun}
          />
        )}
        {runData && (
          <TriagePanel
            changeId={id}
            currentStep={currentStep}
            onRefetchRun={refetchRun}
          />
        )}

        {/* ── Phase B completed ── */}
        {runData?.status === 'completed' && (
          <div style={{
            textAlign: 'center', padding: '48px 32px', background: 'rgba(76,175,125,0.06)',
            border: '1px solid rgba(76,175,125,0.25)', borderRadius: '12px', marginBottom: '24px',
          }}>
            <CheckCircle size={40} style={{ color: 'var(--success)', marginBottom: '16px' }} />
            <h2 style={{ margin: '0 0 8px', fontSize: '18px', fontWeight: '600', color: 'var(--text-primary)' }}>
              Phase B Complete
            </h2>
            <p style={{ margin: '0 auto 24px', fontSize: '13px', color: 'var(--text-muted)', maxWidth: '460px' }}>
              All pipeline steps have been completed. Code has been generated, reviewed, built, deployed,
              and UAT tested successfully.
            </p>
            <button
              onClick={() => navigate(`/changes/${id}`)}
              style={{
                padding: '10px 24px', background: 'var(--success)', color: 'white',
                border: 'none', borderRadius: '8px', fontSize: '14px', fontWeight: '600',
                cursor: 'pointer',
              }}
            >
              Back to Change Request
            </button>
          </div>
        )}

        <div ref={bottomRef} />
      </div>

      {/* ── Feedback bar ── */}
      {(hasOutput || started) && !approved && (
        <div style={{ padding: '16px 24px', borderTop: '1px solid var(--border)', background: 'var(--bg-base)', flexShrink: 0 }}>
          {iterationCount > 0 && (
            <p style={{ margin: '0 0 8px', fontSize: '11px', color: 'var(--text-muted)' }}>
              Iteration {iterationCount} · {runData?.iteration_count || 0} total ·{' '}
              Approve above to proceed to Code Review, or refine below.
            </p>
          )}
          <div style={{ display: 'flex', gap: '10px' }}>
            <input
              value={feedback}
              onChange={e => setFeedback(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') handleFeedback() }}
              placeholder="e.g. Add input validation on the transaction limit field…"
              disabled={streaming || !connected}
              style={{
                flex: 1, padding: '9px 14px', background: 'var(--bg-input)',
                border: '1px solid var(--border)', borderRadius: '6px',
                color: 'var(--text-primary)', fontSize: '13px',
              }}
              onFocus={e => e.target.style.borderColor = 'var(--accent)'}
              onBlur={e => e.target.style.borderColor = 'var(--border)'}
            />
            <button
              onClick={handleFeedback}
              disabled={!feedback.trim() || streaming || !connected}
              style={{
                display: 'flex', alignItems: 'center', gap: '6px',
                padding: '9px 16px', background: 'var(--bg-elevated)',
                border: '1px solid var(--border)', borderRadius: '6px',
                color: 'var(--text-secondary)', fontSize: '13px', fontWeight: '500',
                cursor: (!feedback.trim() || streaming || !connected) ? 'not-allowed' : 'pointer',
                opacity: (!feedback.trim() || streaming || !connected) ? 0.5 : 1,
              }}
            >
              <RefreshCw size={13} /> Regenerate
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
