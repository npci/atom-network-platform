// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useState, useEffect, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import api, { agenticApi } from '../services/api'
import { useRepoRoles } from '../hooks/useUiConfig'
import {
  validateSelection, topologyHint, UNCONFIGURED_TOPOLOGY_NOTICE,
} from '../utils/repoTopology'

// Change-Analysis stage UI (accuracy S2/S3). Renders the code-grounded analysis
// run's two gates — the PM clarification BATCH and the plan ratification — inside
// the Clarification stage. Polling-based (the flow is interactive, not high-freq).
// Renders nothing when no analysis run exists, so the legacy clarification UI is
// unaffected until the new flow is activated.

const PHASE_FRIENDLY = {
  pending: 'Getting ready…',
  workspace_ready: 'Preparing the repository…',
  context_ready: 'Reading your requirement…',
  analyzing: 'Reading the code and analysing the change…',
  awaiting_clarifications: 'A few questions need your answer',
  awaiting_plan_approval: 'Implementation plan ready for your review',
  completed: 'Analysis complete — plan ratified',
  failed: 'Paused — you can retry',
  gave_up: 'Paused — you can retry',
  cancelled: 'Cancelled',
}

const card = {
  marginTop: 12, padding: '14px 16px', borderRadius: 8,
  background: 'rgba(96,165,250,0.06)', border: '1px solid rgba(96,165,250,0.3)',
}

// functional_plan fields are LLM-emitted — a field the schema documents as text can
// come back as an object/array (e.g. compatibility → {wire_compatible, …}). Never
// hand a raw object to React (error #31); stringify the unexpected shape instead.
const asText = (v) => (v == null || typeof v === 'string') ? v : JSON.stringify(v)

// Render the ratified technical_analysis (flows, modules, per-file changes, schema,
// decisions, risks…) as readable sections instead of a raw JSON blob. Generic + resilient:
// strings → prose, string lists → bullets, object lists → one compact "key: value" line each,
// and any unmapped key still renders under its own label. Never hands React a raw object.
const _TA_LABELS = {
  flows: 'Flows', modules: 'Modules', impacted_repos: 'Impacted repos',
  per_file_changes: 'File changes', file_change_list: 'File changes',
  schema_inventory: 'Schema inventory', data_model_changes: 'Data model / schema changes',
  reuse_findings: 'Reuse findings', constraints: 'Constraints',
  critical_decisions: 'Critical decisions', risks: 'Risks',
  consumers_to_update: 'Consumers to update', enforcement_audit: 'Enforcement audit',
}
const _taItem = (it) => {
  if (it == null) return ''
  if (typeof it === 'string') return it
  if (typeof it === 'object')
    return Object.entries(it)
      .filter(([k, v]) => v != null && v !== '' && !/^(repo|id)$/i.test(k))
      .map(([k, v]) => `${k}: ${typeof v === 'object' ? JSON.stringify(v) : v}`)
      .join(' · ')
  return String(it)
}
function TechnicalAnalysisView({ ta }) {
  if (!ta || typeof ta !== 'object') return null
  const keys = [...Object.keys(_TA_LABELS), ...Object.keys(ta).filter(k => !(k in _TA_LABELS))]
    .filter(k => k !== 'critical_decisions' && ta[k] != null && (!Array.isArray(ta[k]) || ta[k].length))
  if (!keys.length) return null
  return (
    <div style={{ marginTop: 4 }}>
      {keys.map(k => (
        <div key={k} style={{ marginTop: 8 }}>
          <div style={{ fontSize: 11.5, fontWeight: 700, color: 'var(--text-secondary)' }}>{_TA_LABELS[k] || k}</div>
          {Array.isArray(ta[k]) ? (
            <ul style={{ fontSize: 12, color: 'var(--text-secondary)', margin: '3px 0 0', paddingLeft: 16 }}>
              {ta[k].map((it, i) => <li key={i} style={{ marginBottom: 2 }}>{_taItem(it)}</li>)}
            </ul>
          ) : (
            <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 3, whiteSpace: 'pre-wrap' }}>{asText(ta[k])}</div>
          )}
        </div>
      ))}
    </div>
  )
}

// Compact token count for the live spend line: 1234 → 1.2K, 1_200_000 → 1.2M.
const fmtTokens = (n) => {
  if (!n) return '0'
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}K`
  return String(n)
}
// Cost is an ESTIMATE: it's priced from provider-reported tokens, and through the AiNxt
// gateway the input + prompt-cache tokens are under-reported (cache counters hardcoded 0),
// so the figure is a lower bound. `cost_complete=false` means at least one call hit an
// unpriced model and its $ is missing entirely → show a "~…+" band, never a hard number.
const fmtCost = (usd, complete) => {
  const n = usd < 1 ? usd.toFixed(3) : usd.toFixed(2)
  return complete ? `$${n} est.` : `~$${n}+ est.`
}

const _CD_SOURCE_LABEL = {
  requirement: 'from requirement', code_verified: 'code-verified', human_decision: 'your decision',
}
// The ratified critical decisions ARE the core of what the generated BRD/TSD will contain
// (the plan gate even rejects ratification when they're missing). They used to live buried
// inside the collapsed "technical analysis" — a PM was unlikely to ever open it — so we surface
// them here as an always-visible block, right where the plan is ratified.
function CriticalDecisionsView({ decisions }) {
  const list = Array.isArray(decisions) ? decisions.filter(Boolean) : []
  if (!list.length) return null
  return (
    <div style={{ marginTop: 10, padding: '10px 12px', borderRadius: 8,
      background: 'rgba(37,99,235,0.07)', border: '1px solid rgba(37,99,235,0.4)' }}>
      <div style={{ fontSize: 12.5, fontWeight: 800, color: '#2563eb' }}>🎯 Critical decisions — these form the core of the BRD</div>
      <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 2 }}>
        Every point below is written into the generated document. Read them before ratifying — ratifying locks them in.
      </div>
      <ol style={{ margin: '8px 0 0', paddingLeft: 18 }}>
        {list.map((d, i) => {
          if (typeof d === 'string') return <li key={i} style={{ fontSize: 12.5, color: 'var(--text-secondary)', marginBottom: 6 }}>{d}</li>
          const dim = d.dimension ? String(d.dimension).replace(/_/g, ' ') : null
          return (
            <li key={i} style={{ fontSize: 12.5, color: 'var(--text-primary)', marginBottom: 8 }}>
              {dim && <span style={{ fontWeight: 700, textTransform: 'capitalize' }}>{dim}: </span>}
              <span>{d.decision || d.directive}</span>
              {d.source && (
                <span style={{ fontSize: 10, fontWeight: 700, marginLeft: 6, padding: '1px 6px', borderRadius: 10,
                  background: 'rgba(148,163,184,0.15)', color: '#94a3b8' }}>{_CD_SOURCE_LABEL[d.source] || d.source}</span>
              )}
              {d.directive && d.directive !== d.decision && (
                <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 2 }}>→ {d.directive}</div>
              )}
              {d.evidence && (
                <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 2, fontStyle: 'italic' }}>{asText(d.evidence)}</div>
              )}
            </li>
          )
        })}
      </ol>
    </div>
  )
}

// Turn one raw analysis event into a friendly "what the agent is doing now" line.
const _TOOL_ICON = { read_file: '📄', grep: '🔍', glob: '🗂️', run_command: '⚙️',
  module_context: '🧭', flow_context: '🔀', ast_query: '🌳', symbol_graph: '🕸️', read_doc: '📑',
  record_fact: '📌' }
// Plural label for a COLLAPSED run of adjacent same-tool calls. The agent batches —
// one analysis turn fired 12 record_fact calls back to back — and an uncollapsed
// feed let that single burst evict every read and search before it (see _collapse).
const _TOOL_PLURAL = {
  read_file:      n => `Read ${n} files`,
  grep:           n => `Ran ${n} searches`,
  glob:           n => `Ran ${n} searches`,
  record_fact:    n => `Recorded ${n} findings`,
  module_context: n => `Mapped ${n} modules`,
  flow_context:   n => `Traced ${n} flows`,
  read_doc:       n => `Read ${n} doc sections`,
}
const _SEARCH_TOOLS = new Set(['grep', 'glob', 'code_search_semantic'])

/** Collapse ADJACENT tool_calls of the same tool into one row carrying a count.
 *  Preserves order and never merges across a different tool or an llm_turn, so
 *  the feed still reads as a narrative — it just can't be flooded by one batch. */
function _collapse(events) {
  const out = []
  for (const e of events) {
    const name = e?.kind === 'tool_call' ? (e.payload?.name || 'tool') : null
    const prev = out[out.length - 1]
    if (name && prev && prev.name === name) { prev.count += 1; prev.last = e; continue }
    out.push({ name, count: 1, last: e, key: e.seq ?? out.length })
  }
  return out
}
function analysisActivity(e) {
  const p = e?.payload || {}
  if (e?.kind === 'llm_turn') {
    const t = (p.text || '').trim()
    return { icon: '💭', text: t ? `Thinking — ${t.slice(0, 100)}${t.length > 100 ? '…' : ''}` : 'Thinking…' }
  }
  if (e?.kind === 'tool_call') {
    const name = p.name || 'tool'
    const input = String(p.input || '')
    const path = (input.match(/'path':\s*'([^']+)'/) || [])[1]
    const pat = (input.match(/'pattern':\s*'([^']*)'/) || [])[1]
    const icon = _TOOL_ICON[name] || '🔧'
    if (path) return { icon, text: `Reading ${path.split('/').slice(-2).join('/')}` }
    if (pat !== undefined) return { icon, text: `Searching “${pat || '…'}”` }
    return { icon, text: (p.action || name).replace(/^[^\w]+\s*/, '') }
  }
  return { icon: '•', text: (p.action || e?.kind || '').replace(/^[^\w]+\s*/, '') }
}

export default function AnalysisPanel({ changeId, onStatus, hideActions = false }) {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [answers, setAnswers] = useState({})        // {qid: {chosen_option_id, custom_answer}}
  const [rectify, setRectify] = useState('')        // optional plan rectification (binding functional choice)
  const [feedback, setFeedback] = useState('')
  const [showTech, setShowTech] = useState(false)
  const [err, setErr] = useState(null)
  const [selectedRepos, setSelectedRepos] = useState(null)   // null = not yet initialised from defaults

  // Don't swallow failures into "no runs" — a 403/500/network error would otherwise read
  // as "no analysis", hiding the real problem. Let it surface as a query error + banner.
  const { data: runsData, error: runsError } = useQuery({
    queryKey: ['analysis-runs', changeId],
    queryFn: () => agenticApi.listChangeRuns(changeId, 'analysis').then(r => r.data),
    refetchInterval: 4000,
    enabled: !!changeId,
    retry: 1,
  })
  const run = (runsData?.runs || [])[0]
  const phase = run?.phase

  // Selectable indexed repos + the default (core+app) pre-selection. The codebase
  // scope is CHOSEN here at planning, then carried to XSD + downstream — never
  // re-asked. enabled=false → the flow is off; the panel steps aside for legacy.
  const { data: repoOpts } = useQuery({
    queryKey: ['analysis-repos', changeId],
    queryFn: () => agenticApi.getAnalysisRepos(changeId).then(r => r.data).catch(() => null),
    enabled: !!changeId && !run,
  })
  const disabled = repoOpts?.enabled === false
  // Declared repo topology (see utils/repoTopology.js). Must be read here, above
  // the `if (!run)` early return, so the hook order stays stable across renders.
  const repoRoles = useRepoRoles()
  useEffect(() => {
    if (selectedRepos === null && repoOpts?.default_repo_ids) setSelectedRepos(repoOpts.default_repo_ids)
  }, [repoOpts, selectedRepos])
  const toggleRepo = (rid) => setSelectedRepos(prev => {
    const s = prev || []
    return s.includes(rid) ? s.filter(x => x !== rid) : [...s, rid]
  })

  const startAnalysis = useMutation({
    mutationFn: () => agenticApi.ensureAnalysis(changeId, selectedRepos || []),
    onSuccess: () => { setErr(null); qc.invalidateQueries({ queryKey: ['analysis-runs', changeId] }) },
    onError: (e) => setErr(e?.response?.data?.detail || e.message),
  })

  // Tell the parent the flow state so it replaces (not duplicates) the legacy
  // clarification UI: 'disabled' → show legacy; 'active' → this panel owns the
  // stage (the repo picker OR a running analysis); 'pending' → still resolving.
  // Hold the latest onStatus in a ref so the effect depends only on the STATE that
  // drives the status — an unstable onStatus prop from a caller that didn't useCallback
  // can't make this fire every render.
  const onStatusRef = useRef(onStatus)
  useEffect(() => { onStatusRef.current = onStatus })
  useEffect(() => {
    const cb = onStatusRef.current
    if (!cb) return
    if (disabled) cb('disabled')
    else if (run || repoOpts) cb('active')
    else cb('pending')
  }, [run, disabled, repoOpts])

  // Poll events through the whole live span (workspace → context → analysing →
  // clarifications) so the UI can show what the agent is doing RIGHT NOW, not a
  // static "analysing…". Slower poll while it's just thinking, faster at the gate.
  const liveAnalyzing = ['pending', 'workspace_ready', 'context_ready', 'analyzing'].includes(phase)
  const { data: eventsData, error: eventsError } = useQuery({
    queryKey: ['analysis-events', run?.run_id],
    queryFn: () => agenticApi.getEvents(run.run_id).then(r => r.data),   // surface, don't mask
    refetchInterval: phase === 'awaiting_clarifications' ? 4000 : (liveAnalyzing ? 2500 : false),
    enabled: !!run?.run_id && (liveAnalyzing || phase === 'awaiting_clarifications'),
    retry: 1,
  })
  // Per-run token spend + estimated USD, from the llm_usage_records ledger. Rows accrue
  // per LLM call, so the number climbs live during the analysis. Polled while analysing;
  // one final fetch on completion so the ratified run keeps its total. Never masks — a
  // failed fetch just hides the line (catch → null) rather than blanking the panel.
  const { data: usage } = useQuery({
    queryKey: ['analysis-usage', run?.run_id],
    queryFn: () => agenticApi.runUsage(run.run_id).then(r => r.data).catch(() => null),
    refetchInterval: liveAnalyzing ? 3000 : false,
    enabled: !!run?.run_id && (liveAnalyzing || phase === 'completed'),
  })
  const { data: plan, error: planError } = useQuery({
    queryKey: ['analysis-plan', changeId, run?.version],
    queryFn: () => agenticApi.getAnalysis(changeId).then(r => r.data),  // surface, don't mask
    // Load for both the review gate AND the completed view (so the ratified plan
    // + the clarification Q&A remain visible after completion).
    enabled: !!changeId && (phase === 'awaiting_plan_approval' || phase === 'completed'),
    retry: 1,
  })
  // Surface a real backend failure (403/500/network) instead of masking it as "no run"/"no plan".
  const _loadErrObj = runsError || eventsError || planError
  const loadErr = _loadErrObj ? (_loadErrObj?.response?.data?.detail || _loadErrObj.message) : null

  // Pre-flight: warn UP FRONT if the agentic prerequisites (worker / Redis / git /
  // GITLAB_TOKEN / LLM key) are missing, so a run isn't started only to hang.
  const { data: preflight } = useQuery({
    queryKey: ['agentic-preflight'],
    queryFn: () => agenticApi.preflight().then(r => r.data).catch(() => null),
    staleTime: 30000, refetchInterval: 60000, enabled: !!changeId,
  })
  const preflightBanner = preflight && preflight.ready === false && (preflight.problems || []).length > 0 ? (
    <div style={{ padding: '10px 14px', marginBottom: 10, borderRadius: 8, fontSize: 12.5, lineHeight: 1.6,
      background: 'rgba(245,158,11,0.10)', border: '1px solid rgba(245,158,11,0.45)', color: '#b45309' }}>
      <strong>⚠ Agentic prerequisites not ready</strong> — a run may stall until these are fixed:
      <ul style={{ margin: '4px 0 0', paddingLeft: 18 }}>
        {preflight.problems.map((p, i) => <li key={i}>{p}</li>)}
      </ul>
    </div>
  ) : null

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['analysis-runs', changeId] })
    qc.invalidateQueries({ queryKey: ['analysis-plan', changeId] })
  }

  const submitClar = useMutation({
    mutationFn: (payload) => agenticApi.decideClarifications(run.run_id, payload),
    onSuccess: () => { setErr(null); setAnswers({}); setRectify(''); refresh() },
    onError: (e) => setErr(e?.response?.data?.detail || e.message),
  })
  const submitPlan = useMutation({
    mutationFn: (payload) => agenticApi.decidePlan(run.run_id, payload),
    onSuccess: () => { setErr(null); setFeedback(''); refresh() },
    onError: (e) => setErr(e?.response?.data?.detail || e.message),
  })
  // Retry a run that stopped before finishing — re-drives from its last working
  // phase (the workspace + chosen scope persist, so nothing is re-asked or lost).
  const retryRun = useMutation({
    mutationFn: () => agenticApi.resume(run.run_id),
    onSuccess: () => { setErr(null); refresh() },
    onError: (e) => setErr(e?.response?.data?.detail || e.message),
  })
  // Restart from scratch — discard THIS analysis run (plan, clarifications, transcript) and
  // start a brand-new one on the same repos. Server-side: ensure(restart:true) terminates the
  // existing run then creates a fresh one. Repeatable clean re-run for testing the flow.
  const restartRun = useMutation({
    mutationFn: () => agenticApi.ensureAnalysis(changeId, null, { restart: true }),
    onSuccess: () => { setErr(null); setAnswers({}); setFeedback(''); refresh() },
    onError: (e) => setErr(e?.response?.data?.detail || e.message),
  })
  const confirmRestart = () => {
    if (window.confirm('Restart the analysis from scratch?\n\nThis discards the current plan, clarification answers, and transcript, then re-runs the code-grounded analysis on the same repos. Cannot be undone.'))
      restartRun.mutate()
  }

  if (disabled) return null   // flow off → legacy clarification UI shows alone

  // No analysis run yet → ask which indexed code to analyse, then Start. The
  // chosen scope is what the plan is grounded in and what XSD/TSD inherit.
  if (!run) {
    const repos = repoOpts?.repos || []
    const sel = selectedRepos || []
    // Rule comes from the domain pack, not from UPI's core+app pair. Undeclared
    // topology → single-repo default (any one repo) + a notice. Mirrors the
    // server gate in repo_scope.validate_selection.
    const { valid, reason: blockReason, needsWarning } =
      repoOpts ? validateSelection(repos, sel, repoRoles) : { valid: false, reason: '', needsWarning: false }
    return (
      <div style={{ padding: '12px 16px' }}>
        <div style={{ fontSize: 13, color: 'var(--text-secondary)' }}>
          <strong>Change Analysis</strong> — choose the code to analyse
        </div>
        {(err || loadErr) && <div style={{ color: '#ef4444', fontSize: 12, marginTop: 6 }}>{err || `Couldn’t load the analysis: ${loadErr}`}</div>}
        {preflightBanner}
        <section style={card}>
          {needsWarning && (
            <div style={{ fontSize: 12, color: '#b45309', background: 'rgba(180,83,9,0.10)',
              border: '1px solid rgba(180,83,9,0.35)', borderRadius: 6, padding: '8px 10px', marginBottom: 8 }}>
              ⚠ {UNCONFIGURED_TOPOLOGY_NOTICE}
            </div>
          )}
          <div style={{ fontSize: 12.5, color: 'var(--text-secondary)', marginBottom: 8 }}>
            {topologyHint(repoRoles)}{' '}
            This scope is reused for the XSD and the rest of the change — you won't be asked again.
          </div>
          {!repoOpts && <div style={{ fontSize: 12.5, color: 'var(--text-muted)' }}>Loading repositories…</div>}
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            {repos.map(r => {
              const checked = sel.includes(r.id)
              // "builds first" is a pack-declared property of the role, not the
              // literal string "core" — a library domain's dependency repo may
              // be called anything.
              const isCore = Boolean(repoRoles.find(role => role.key === r.role)?.builds_first)
              return (
                <label key={r.id} style={{ display: 'flex', alignItems: 'center', gap: 10,
                  cursor: r.indexed ? 'pointer' : 'not-allowed', opacity: r.indexed ? 1 : 0.5,
                  padding: '6px 8px', borderRadius: 6,
                  border: '1px solid ' + (checked ? 'rgba(96,165,250,0.5)' : 'var(--border)'),
                  background: checked ? 'rgba(96,165,250,0.08)' : 'transparent' }}>
                  <input type="checkbox" checked={checked} disabled={!r.indexed} onChange={() => toggleRepo(r.id)} />
                  <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-primary)' }}>{r.label}</span>
                  <span style={{ marginLeft: 'auto', fontSize: 10, fontWeight: 700, padding: '2px 8px', borderRadius: 10,
                    background: isCore ? 'rgba(37,99,235,0.12)' : (r.role === 'app' ? 'rgba(52,211,153,0.12)' : 'rgba(127,127,127,0.15)'),
                    color: isCore ? 'var(--accent, #2563eb)' : (r.role === 'app' ? '#16a34a' : 'var(--text-muted)') }}>
                    {r.indexed ? (isCore ? `${(r.role || '').toUpperCase()} · builds first` : (r.role || 'no role')) : 'not indexed'}
                  </span>
                </label>
              )
            })}
          </div>
          <div style={{ fontSize: 11, color: blockReason ? '#b45309' : 'var(--text-muted)', marginTop: 8 }}>
            {blockReason || `✓ ${sel.length} repositor${sel.length === 1 ? 'y' : 'ies'} selected.`}
          </div>
          <button onClick={() => startAnalysis.mutate()} disabled={startAnalysis.isPending || !valid}
            style={{ marginTop: 10, padding: '8px 16px', fontWeight: 600, background: '#2563eb', color: '#fff',
              border: 'none', borderRadius: 6, cursor: (startAnalysis.isPending || !valid) ? 'not-allowed' : 'pointer',
              opacity: (startAnalysis.isPending || !valid) ? 0.6 : 1 }}>
            {startAnalysis.isPending ? 'Starting…' : 'Start analysis'}
          </button>
        </section>
      </div>
    )
  }

  const latestQuestions = (() => {
    const evs = (eventsData?.events || []).filter(e => e.kind === 'clarifications_requested')
    const last = evs[evs.length - 1]
    return (last?.payload?.questions) || []
  })()

  // A run that stopped before reaching a gate or completion — never a dead end:
  // show a calm message + a one-click retry (phase OR status, whichever the backend set).
  const terminal = ['failed', 'gave_up', 'cancelled'].includes(phase) ||
    ['failed', 'gave_up', 'cancelled'].includes(run?.status)

  return (
    <div style={{ padding: '12px 16px' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10 }}>
        <div style={{ fontSize: 13, color: 'var(--text-secondary)' }}>
          <strong>Change Analysis</strong> — {PHASE_FRIENDLY[phase] || phase}
        </div>
        <button onClick={confirmRestart} disabled={restartRun.isPending}
          title="Discard this analysis run and start a brand-new one on the same repos."
          style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '5px 10px', fontSize: 11.5,
            fontWeight: 600, background: 'var(--bg-base)', color: 'var(--text-secondary)',
            border: '1px solid var(--border)', borderRadius: 6, whiteSpace: 'nowrap',
            cursor: restartRun.isPending ? 'not-allowed' : 'pointer', opacity: restartRun.isPending ? 0.6 : 1 }}>
          ↻ {restartRun.isPending ? 'Restarting…' : 'Restart from scratch'}
        </button>
      </div>
      {(err || loadErr) && <div style={{ color: '#ef4444', fontSize: 12, marginTop: 6 }}>{err || `Couldn’t load the analysis: ${loadErr}`}</div>}
      {preflightBanner}

      {terminal && (
        <section style={{ ...card, background: 'rgba(245,158,11,0.07)', border: '1px solid rgba(245,158,11,0.35)' }}>
          <div style={{ display: 'flex', alignItems: 'flex-start', gap: 10 }}>
            <span style={{ fontSize: 18, lineHeight: 1.2 }}>↻</span>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontWeight: 700, fontSize: 14, color: 'var(--text-primary)' }}>
                {phase === 'cancelled' || run?.status === 'cancelled'
                  ? 'Analysis was cancelled'
                  : 'The analysis paused before finishing'}
              </div>
              <div style={{ fontSize: 12.5, color: 'var(--text-secondary)', marginTop: 3, lineHeight: 1.5 }}>
                This usually clears on a retry. Your requirement and the code scope you chose are saved —
                nothing is lost, and you won’t be asked anything again.
              </div>
            </div>
          </div>
          <div style={{ display: 'flex', gap: 8, marginTop: 12, flexWrap: 'wrap' }}>
            <button onClick={() => retryRun.mutate()} disabled={retryRun.isPending}
              style={{ padding: '8px 18px', fontWeight: 600, background: '#2563eb', color: '#fff', border: 'none',
                borderRadius: 6, cursor: retryRun.isPending ? 'not-allowed' : 'pointer', opacity: retryRun.isPending ? 0.6 : 1 }}>
              {retryRun.isPending ? 'Retrying…' : '↻ Retry analysis'}
            </button>
          </div>
          {run?.error && (
            <details style={{ marginTop: 10 }}>
              <summary style={{ fontSize: 11.5, color: 'var(--text-muted)', cursor: 'pointer' }}>Technical detail</summary>
              <div style={{ marginTop: 6, fontSize: 11.5, color: 'var(--text-muted)', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                {String(run.error).slice(0, 400)}
              </div>
            </details>
          )}
        </section>
      )}

      {phase === 'awaiting_clarifications' && (
        <section style={card}>
          <strong style={{ fontSize: 14 }}>❓ Questions before we draft the plan</strong>
          {latestQuestions.map(q => {
            const sel = answers[q.id] || {}
            // v3 — multi_select branch: checkboxes with `recommended_ids`
            // pre-checked, until the PM touches the question. Then their
            // selection wins.
            if (q.kind === 'multi_select') {
              const touched = Array.isArray(sel.chosen_option_ids)
              const selectedIds = touched
                ? sel.chosen_option_ids
                : (q.recommended_ids || [])
              const toggle = (oid) => {
                const cur = new Set(touched ? sel.chosen_option_ids : (q.recommended_ids || []))
                if (cur.has(oid)) cur.delete(oid); else cur.add(oid)
                setAnswers(a => ({ ...a, [q.id]: { ...(a[q.id] || {}), chosen_option_ids: Array.from(cur) } }))
              }
              const labels = (q.options || [])
                .filter(o => selectedIds.includes(o.id))
                .map(o => o.label)
              return (
                <div key={q.id} style={{ margin: '12px 0', paddingBottom: 8, borderBottom: '1px solid var(--border-subtle)' }}>
                  <div style={{ fontWeight: 600, fontSize: 13, whiteSpace: 'pre-wrap' }}>{q.text}</div>
                  {(q.options || []).map(o => {
                    const rec = (q.recommended_ids || []).includes(o.id)
                    const checked = selectedIds.includes(o.id)
                    return (
                      <label key={o.id} style={{ display: 'flex', gap: 8, alignItems: 'flex-start', margin: '6px 0', cursor: 'pointer' }}>
                        <input type="checkbox" checked={checked} onChange={() => toggle(o.id)} />
                        <span style={{ fontSize: 12.5 }}>
                          <span style={{ fontWeight: 600 }}>{o.label}</span>
                          {rec && <span style={{ fontSize: 10, fontWeight: 700, marginLeft: 6, padding: '1px 6px', borderRadius: 10, background: 'rgba(52,211,153,0.15)', color: '#16a34a' }}>SUGGESTED</span>}
                        </span>
                      </label>
                    )
                  })}
                  {labels.length ? (
                    <div style={{ marginTop: 6, fontSize: 12, color: '#16a34a' }}>✓ In scope: <strong>{labels.join(', ')}</strong></div>
                  ) : (
                    <div style={{ marginTop: 6, fontSize: 11.5, color: '#dc2626' }}>Select at least one option</div>
                  )}
                </div>
              )
            }
            // Legacy single-select / yes_no branch (unchanged).
            return (
              <div key={q.id} style={{ margin: '12px 0', paddingBottom: 8, borderBottom: '1px solid var(--border-subtle)' }}>
                <div style={{ fontWeight: 600, fontSize: 13 }}>{q.text}</div>
                {(q.options || []).map(o => {
                  const rec = o.id === q.recommended
                  return (
                    <label key={o.id} style={{ display: 'flex', gap: 8, alignItems: 'flex-start', margin: '6px 0', cursor: 'pointer' }}>
                      <input type="radio" name={`q-${q.id}`} checked={sel.chosen_option_id === o.id}
                        onChange={() => setAnswers(a => ({ ...a, [q.id]: { chosen_option_id: o.id } }))} />
                      <span style={{ fontSize: 12.5 }}>
                        <span style={{ fontWeight: 600 }}>{o.label}</span>
                        {rec && <span style={{ fontSize: 10, fontWeight: 700, marginLeft: 6, padding: '1px 6px', borderRadius: 10, background: 'rgba(52,211,153,0.15)', color: '#16a34a' }}>RECOMMENDED</span>}
                        {o.consequence && <span style={{ display: 'block', color: 'var(--text-muted)', fontSize: 11.5 }}>{o.consequence}</span>}
                      </span>
                    </label>
                  )
                })}
                <input type="text" placeholder="…or type your own answer"
                  value={sel.custom_answer || ''}
                  onChange={e => setAnswers(a => ({ ...a, [q.id]: { custom_answer: e.target.value } }))}
                  style={{ width: '100%', marginTop: 4, padding: 6, borderRadius: 6, border: '1px solid var(--border)', background: 'var(--bg-input, #0b1021)', color: 'var(--text-primary)', fontSize: 12 }} />
                {(() => {
                  // Echo the chosen answer back under the question so the PM can verify before submitting.
                  const opt = (q.options || []).find(o => o.id === sel.chosen_option_id)
                  const ans = opt ? opt.label : (sel.custom_answer || '').trim()
                  return ans ? (
                    <div style={{ marginTop: 6, fontSize: 12, color: '#16a34a' }}>✓ Your answer: <strong>{ans}</strong></div>
                  ) : (
                    <div style={{ marginTop: 6, fontSize: 11.5, color: 'var(--text-muted)' }}>No answer selected yet</div>
                  )
                })()}
              </div>
            )
          })}
          {/* Optional plan rectification: a binding FUNCTIONAL choice riding along with the
              answers — the agent re-checks technical feasibility, applies it as asked, and
              records the repercussions in the revised plan. */}
          <div style={{ marginTop: 12, paddingTop: 10, borderTop: '1px solid var(--border-subtle)' }}>
            <div style={{ fontWeight: 600, fontSize: 13 }}>✏️ Rectify the plan direction (optional)</div>
            <div style={{ fontSize: 11.5, color: 'var(--text-muted)', marginTop: 2 }}>
              e.g. “create a new dedicated API for this instead of reusing X”. The agent checks technical
              feasibility, applies your choice, and lists the repercussions in the revised plan — including,
              for a new API, who initiates it and how it routes through the four-party model.
            </div>
            <textarea value={rectify} onChange={e => setRectify(e.target.value)} rows={2}
              placeholder="…describe how the plan's direction should change (leave empty to keep it as proposed)"
              style={{ width: '100%', marginTop: 6, padding: 8, borderRadius: 6, border: '1px solid var(--border)',
                background: 'var(--bg-input, #0b1021)', color: 'var(--text-primary)', fontSize: 12.5 }} />
          </div>
          {(() => {
            // Every question must have a real answer before submit.
            //  - single_select / yes_no: chosen_option_id OR custom_answer.
            //  - multi_select (v3): chosen_option_ids has >=1 entry, OR the
            //    user hasn't touched it AND the builder pre-checked at least
            //    one option (recommended_ids) — that pre-check IS the answer.
            const isAnswered = (q) => {
              const a = answers[q.id] || {}
              if (q.kind === 'multi_select') {
                if (Array.isArray(a.chosen_option_ids)) return a.chosen_option_ids.length > 0
                return (q.recommended_ids || []).length > 0
              }
              return !!a.chosen_option_id || !!(a.custom_answer || '').trim()
            }
            const allAnswered = latestQuestions.every(isAnswered)
            const unanswered = latestQuestions.filter(q => !isAnswered(q)).length
            // Build the submit payload — for multi_select, if the PM never
            // touched the pre-checked recommendation, ship those ids so the
            // backend records the confirmed default.
            const payloadAnswers = latestQuestions.map(q => {
              const a = answers[q.id] || {}
              if (q.kind === 'multi_select') {
                const ids = Array.isArray(a.chosen_option_ids)
                  ? a.chosen_option_ids
                  : (q.recommended_ids || [])
                return { question_id: q.id, chosen_option_ids: ids }
              }
              return { question_id: q.id, ...a }
            })
            return (
          <button
            onClick={() => submitClar.mutate({
              answers: payloadAnswers,
              ...(rectify.trim() ? { plan_rectification: rectify.trim() } : {}),
            })}
            disabled={submitClar.isPending || !allAnswered}
            title={!allAnswered ? `Answer all questions first (${unanswered} remaining)` : ''}
            style={{ marginTop: 8, padding: '8px 16px', fontWeight: 600, background: '#2563eb', color: '#fff', border: 'none', borderRadius: 6, cursor: (submitClar.isPending || !allAnswered) ? 'not-allowed' : 'pointer', opacity: (submitClar.isPending || !allAnswered) ? 0.6 : 1 }}>
            {submitClar.isPending ? 'Submitting…' : allAnswered ? 'Submit answers' : `Answer all questions (${unanswered} left)`}
          </button>
            )
          })()}
        </section>
      )}

      {phase === 'awaiting_plan_approval' && plan?.exists && Array.isArray(plan.collisions) && plan.collisions.length > 0 && (
        <section style={{ ...card, background: 'rgba(245,158,11,0.08)', border: '1px solid rgba(245,158,11,0.4)' }}>
          <strong style={{ fontSize: 13 }}>⚠ Concurrent changes touch the same schemas</strong>
          <ul style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 4 }}>
            {plan.collisions.map((c, i) => (
              <li key={i}>Change <code>{String(c.change_request_id).slice(0, 8)}</code> also touches <code>{c.path}</code></li>
            ))}
          </ul>
        </section>
      )}

      {phase === 'awaiting_plan_approval' && plan?.exists && (
        <section style={card}>
          <strong style={{ fontSize: 14 }}>📋 Implementation plan</strong>
          <div style={{ fontSize: 12.5, color: 'var(--text-secondary)', marginTop: 6, whiteSpace: 'pre-wrap' }}>
            {asText(plan.functional_plan?.overview) || '(no overview)'}
          </div>
          {Array.isArray(plan.functional_plan?.steps) && plan.functional_plan.steps.length > 0 && (
            <ol style={{ fontSize: 12.5, color: 'var(--text-secondary)', marginTop: 6 }}>
              {plan.functional_plan.steps.map((s, i) => <li key={i}>{typeof s === 'string' ? s : (s.text || JSON.stringify(s))}</li>)}
            </ol>
          )}
          {plan.functional_plan?.compatibility && (
            <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 4 }}>
              {typeof plan.functional_plan.compatibility === 'string'
                ? `Compatibility: ${plan.functional_plan.compatibility}`
                : (
                  <>
                    <span>Compatibility:</span>
                    <ul style={{ margin: '2px 0 0', paddingLeft: 18 }}>
                      {Object.entries(plan.functional_plan.compatibility).map(([k, v]) => (
                        <li key={k}>{k.replace(/_/g, ' ')}: {typeof v === 'string' ? v : JSON.stringify(v)}</li>
                      ))}
                    </ul>
                  </>
                )}
            </div>
          )}
          {Array.isArray(plan.functional_plan?.assumptions) && plan.functional_plan.assumptions.length > 0 && (
            <div style={{ marginTop: 8, padding: '8px 10px', borderRadius: 6, background: 'rgba(232,164,74,0.08)', border: '1px solid rgba(232,164,74,0.3)' }}>
              <div style={{ fontSize: 11.5, fontWeight: 700, color: '#e8a44a' }}>Assumptions the agent made (confirm by ratifying, or request changes):</div>
              <ul style={{ fontSize: 12, color: 'var(--text-secondary)', margin: '4px 0 0', paddingLeft: 18 }}>
                {plan.functional_plan.assumptions.map((a, i) => (
                  <li key={i}>{typeof a === 'string' ? a : (a.text || a.statement || JSON.stringify(a))}</li>
                ))}
              </ul>
            </div>
          )}
          {plan.functional_plan?.implementation_approach && (
            <details style={{ marginTop: 8 }}>
              <summary style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)', cursor: 'pointer' }}>
                🛠 Technical implementation
              </summary>
              {Array.isArray(plan.functional_plan.implementation_approach) ? (
                <ol style={{ fontSize: 12.5, color: 'var(--text-secondary)', marginTop: 6 }}>
                  {plan.functional_plan.implementation_approach.map((s, i) => (
                    <li key={i}>{typeof s === 'string' ? s : (s.text || JSON.stringify(s))}</li>
                  ))}
                </ol>
              ) : (
                <div style={{ fontSize: 12.5, color: 'var(--text-secondary)', marginTop: 6, whiteSpace: 'pre-wrap' }}>
                  {asText(plan.functional_plan.implementation_approach)}
                </div>
              )}
            </details>
          )}
          <CriticalDecisionsView decisions={plan.technical_analysis?.critical_decisions} />
          <div style={{ marginTop: 6, fontSize: 11.5 }}>
            <span style={{ marginRight: 10 }}>{plan.pm_ratified ? '✅' : '⬜'} PM</span>
            <span>{plan.tech_ratified ? '✅' : '⬜'} Tech lead</span>
          </div>
          <details style={{ marginTop: 8 }} open={showTech} onToggle={e => setShowTech(e.target.open)}>
            <summary style={{ fontSize: 11.5, color: 'var(--text-muted)', cursor: 'pointer' }}>Show technical analysis</summary>
            <TechnicalAnalysisView ta={plan.technical_analysis} />
          </details>
          <div style={{ marginTop: 10, fontSize: 11.5, color: 'var(--text-muted)' }}>
            Not what you decided? Describe the correction — e.g. “create a new dedicated API instead of
            reusing X”. The agent checks technical feasibility, applies your choice (it may note
            repercussions, not refuse), and for a new API the revised plan will show who initiates it and
            how it routes through the four-party model.
          </div>
          <div style={{ display: 'flex', gap: 8, marginTop: 6, flexWrap: 'wrap' }}>
            <button onClick={() => submitPlan.mutate({ action: 'ratify' })} disabled={submitPlan.isPending}
              style={{ padding: '8px 16px', fontWeight: 600, background: '#16a34a', color: '#fff', border: 'none', borderRadius: 6, cursor: 'pointer' }}>
              Ratify plan
            </button>
            <input type="text" placeholder="…rectify the plan / request changes" value={feedback} onChange={e => setFeedback(e.target.value)}
              style={{ flex: 1, minWidth: 160, padding: 6, borderRadius: 6, border: '1px solid var(--border)', background: 'var(--bg-input, #0b1021)', color: 'var(--text-primary)', fontSize: 12 }} />
            <button onClick={() => submitPlan.mutate({ action: 'reopen', feedback })} disabled={submitPlan.isPending || !feedback.trim()}
              style={{ padding: '8px 16px', fontWeight: 600, background: 'transparent', color: 'var(--text-primary)', border: '1px solid var(--border)', borderRadius: 6, cursor: 'pointer' }}>
              Request changes
            </button>
          </div>
        </section>
      )}

      {liveAnalyzing && !terminal && (() => {
        const evs = eventsData?.events || []
        const tracked = evs.filter(e => ['tool_call', 'llm_turn', 'repo_cloning', 'repo_indexing',
          'repo_cloned', 'repo_indexed', 'workspace_ready', 'context_ready'].includes(e.kind))
        const last = tracked[tracked.length - 1]
        const cur = last ? analysisActivity(last) : null
        // The run is dispatched but parked behind the global concurrency cap — the
        // worker re-queues it every ~30s without starting a phase, so NO tracked
        // activity ever arrives. Without this the panel showed a spinner over
        // "Getting started…" indefinitely, which reads as "broken" rather than
        // "waiting its turn". Superseded as soon as real activity begins.
        const queued = !tracked.length
          ? [...evs].reverse().find(e => e.kind === 'queued_behind_cap')
          : null
        const step = [...evs].reverse().map(e => e.payload?.iteration).find(i => i != null)
        const filesRead = new Set(evs.filter(e => e.kind === 'tool_call' && e.payload?.name === 'read_file')
          .map(e => (String(e.payload?.input || '').match(/'path':\s*'([^']+)'/) || [])[1]).filter(Boolean)).size
        // Searches are the other half of discovery and were invisible: the agent
        // navigates by glob+read, and a terminal record_fact burst hid both.
        const searches = evs.filter(e => e.kind === 'tool_call' && _SEARCH_TOOLS.has(e.payload?.name)).length
        // Collapse FIRST, then window — so 12 batched record_facts occupy one slot,
        // not all six. 8 rows of distinct activity beats 6 rows of the same tool.
        const recent = _collapse(tracked.filter(e => e.kind === 'tool_call' || e.kind === 'llm_turn'))
          .slice(-8).reverse()
        return (
          <section style={card}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <span style={{ width: 14, height: 14, borderRadius: '50%', flexShrink: 0,
                border: '2px solid #60a5fa', borderTopColor: 'transparent', animation: 'spin 0.8s linear infinite' }} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontWeight: 600, fontSize: 13 }}>{PHASE_FRIENDLY[phase]}</div>
                <div style={{ fontSize: 12, color: 'var(--text-secondary)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                  {cur
                    ? <>{cur.icon} {cur.text}</>
                    : queued
                      ? `⏳ Waiting for a free slot — ${queued.payload?.running ?? '?'} of ${queued.payload?.cap ?? '?'} run slots busy. Starts automatically.`
                      : 'Getting started…'}
                </div>
              </div>
              <div style={{ flexShrink: 0, fontSize: 11, color: 'var(--text-muted)', textAlign: 'right' }}>
                {step != null && <div>step {step}</div>}
                {filesRead > 0 && <div>{filesRead} file{filesRead === 1 ? '' : 's'} read</div>}
                {searches > 0 && <div>{searches} search{searches === 1 ? '' : 'es'}</div>}
              </div>
            </div>
            {recent.length > 0 && (
              <div style={{ marginTop: 10, paddingTop: 8, borderTop: '1px solid var(--border-subtle, var(--border))' }}>
                {recent.map((g, i) => {
                  const a = analysisActivity(g.last)
                  // A collapsed run names the tool and the count instead of showing
                  // only the LAST item's text — "Read 6 files" is true; "Reading
                  // Loan.java" with 5 others hidden behind it is not.
                  const text = g.count > 1
                    ? (_TOOL_PLURAL[g.name]?.(g.count) ?? `${g.name} ×${g.count}`)
                    : a.text
                  return (
                    <div key={g.key ?? i} style={{ display: 'flex', gap: 8, alignItems: 'baseline', padding: '2px 0',
                      fontSize: 11.5, color: i === 0 ? 'var(--text-secondary)' : 'var(--text-muted)', opacity: i === 0 ? 1 : 0.75 }}>
                      <span style={{ flexShrink: 0 }}>{a.icon}</span>
                      <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{text}</span>
                    </div>
                  )
                })}
              </div>
            )}
            {usage && usage.total_tokens > 0 && (
              <div style={{ marginTop: 8, paddingTop: 8, borderTop: '1px solid var(--border-subtle, var(--border))',
                display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10, fontSize: 11, color: 'var(--text-muted)' }}>
                <span>💰 {fmtTokens(usage.total_tokens)} tokens · {usage.calls} LLM call{usage.calls === 1 ? '' : 's'}</span>
                <span title="Estimated cost, priced from provider-reported tokens. Through the AiNxt gateway the input and prompt-cache tokens are under-reported, so treat this as a lower bound.">
                  {fmtCost(usage.cost_usd, usage.cost_complete)}
                </span>
              </div>
            )}
          </section>
        )
      })()}

      {phase === 'completed' && (
        <>
          <section style={{ ...card, background: 'rgba(76,175,125,0.08)', border: '1px solid rgba(76,175,125,0.35)' }}>
            <strong style={{ fontSize: 14 }}>✅ Plan ratified — analysis complete</strong>
            {/* The "Proceed to BRD" action belongs only on the active workflow page,
                not the read-only change-detail overview (hideActions). */}
            {!hideActions && (
              <div style={{ marginTop: 10 }}>
                <button
                  onClick={async () => {
                    try { await api.post(`/changes/${changeId}/advance`, {}); navigate(`/changes/${changeId}/brd`) }
                    catch (e) { setErr(e?.response?.data?.detail || e.message) }
                  }}
                  style={{ padding: '8px 16px', fontWeight: 600, background: 'var(--accent, #2563eb)', color: '#fff', border: 'none', borderRadius: 6, cursor: 'pointer' }}>
                  Proceed to BRD →
                </button>
              </div>
            )}
          </section>

          {/* The questions the agent asked + the option you chose (what the plan was built on). */}
          {Array.isArray(plan?.clarifications) && plan.clarifications.length > 0 && (
            <section style={card}>
              <strong style={{ fontSize: 13 }}>Questions answered during analysis</strong>
              {plan.clarifications.map((c, i) => (
                <div key={i} style={{ margin: '10px 0', paddingBottom: 8, borderBottom: i < plan.clarifications.length - 1 ? '1px solid var(--border-subtle)' : 'none' }}>
                  <div style={{ fontWeight: 600, fontSize: 12.5, color: 'var(--text-primary)' }}>{i + 1}. {c.question}</div>
                  {Array.isArray(c.options) && c.options.length > 0 && (
                    <ul style={{ margin: '6px 0 0', paddingLeft: 18 }}>
                      {c.options.map((o, j) => {
                        const isChosen = (o.label || '') === c.chosen
                        const isRec = o.id && o.id === c.recommended
                        return (
                          <li key={j} style={{ fontSize: 12, marginBottom: 3, color: isChosen ? 'var(--text-primary)' : 'var(--text-muted)', fontWeight: isChosen ? 600 : 400 }}>
                            {o.label || String(o)}
                            {isRec && <span style={{ fontSize: 9.5, fontWeight: 700, marginLeft: 6, padding: '1px 6px', borderRadius: 10, background: 'rgba(52,211,153,0.15)', color: '#16a34a' }}>RECOMMENDED</span>}
                            {isChosen && <span style={{ fontSize: 9.5, fontWeight: 700, marginLeft: 6, padding: '1px 6px', borderRadius: 10, background: 'rgba(96,165,250,0.18)', color: '#2563eb' }}>YOUR CHOICE</span>}
                          </li>
                        )
                      })}
                    </ul>
                  )}
                  {c.chosen && (!Array.isArray(c.options) || !c.options.some(o => (o.label || '') === c.chosen)) && (
                    <div style={{ marginTop: 4, fontSize: 12, color: '#2563eb' }}>Your answer: <strong>{c.chosen}</strong></div>
                  )}
                </div>
              ))}
            </section>
          )}

          {/* The ratified implementation plan (what was produced after the clarifications). */}
          {plan?.exists && plan.functional_plan && (
            <section style={card}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                <strong style={{ fontSize: 13 }}>📋 Ratified implementation plan</strong>
                {plan.version != null && (
                  <span style={{ fontSize: 10, fontWeight: 700, padding: '1px 7px', borderRadius: 10,
                    background: 'rgba(96,165,250,0.18)', color: '#2563eb' }}>v{plan.version}</span>
                )}
              </div>
              {/* When a gate decision diverged from this plan, the plan was rolled to v+1 — show
                  the chosen approach + WHY here, so the page reflects what's actually being built. */}
              {(() => {
                const ad = plan.technical_analysis?.approach_decision
                if (!ad || !ad.approach) return null
                return (
                  <div style={{ marginTop: 8, padding: '8px 10px', borderRadius: 6,
                    background: 'rgba(245,158,11,0.08)', border: '1px solid rgba(245,158,11,0.4)' }}>
                    <div style={{ fontSize: 11.5, fontWeight: 700, color: '#d97706' }}>
                      🧭 Approach changed at the gate{ad.supersedes_version != null && plan.version != null
                        ? ` — plan updated v${ad.supersedes_version} → v${plan.version}` : ''}
                    </div>
                    <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 4 }}>
                      Chosen: <strong>{ad.chosen_title || ad.chosen_option_id}</strong>
                      <span style={{ fontSize: 10, fontWeight: 700, marginLeft: 6, padding: '1px 6px', borderRadius: 10,
                        background: 'rgba(148,163,184,0.15)', color: '#94a3b8' }}>{ad.approach}</span>
                      {ad.target_api && <> → <code style={{ color: '#60a5fa' }}>{ad.target_api}</code></>}
                    </div>
                    {ad.diverges_from_plan && ad.why && (
                      <div style={{ fontSize: 12, color: '#d97706', marginTop: 4 }}>
                        <strong>Why it diverges from the original plan:</strong> {ad.why}
                      </div>
                    )}
                  </div>
                )
              })()}
              {plan.functional_plan.overview && (
                <div style={{ fontSize: 12.5, color: 'var(--text-secondary)', marginTop: 6, whiteSpace: 'pre-wrap' }}>
                  {asText(plan.functional_plan.overview)}
                </div>
              )}
              {Array.isArray(plan.functional_plan.steps) && plan.functional_plan.steps.length > 0 && (
                <ol style={{ fontSize: 12.5, color: 'var(--text-secondary)', marginTop: 6, paddingLeft: 18 }}>
                  {plan.functional_plan.steps.map((s, i) => <li key={i} style={{ marginBottom: 3 }}>{typeof s === 'string' ? s : (s.text || JSON.stringify(s))}</li>)}
                </ol>
              )}
              {Array.isArray(plan.functional_plan.assumptions) && plan.functional_plan.assumptions.length > 0 && (
                <div style={{ marginTop: 8, padding: '8px 10px', borderRadius: 6, background: 'rgba(232,164,74,0.08)', border: '1px solid rgba(232,164,74,0.3)' }}>
                  <div style={{ fontSize: 11.5, fontWeight: 700, color: '#e8a44a' }}>Assumptions the plan was built on:</div>
                  <ul style={{ fontSize: 12, color: 'var(--text-secondary)', margin: '4px 0 0', paddingLeft: 18 }}>
                    {plan.functional_plan.assumptions.map((a, i) => (
                      <li key={i}>{typeof a === 'string' ? a : (a.text || a.statement || JSON.stringify(a))}</li>
                    ))}
                  </ul>
                </div>
              )}
              {/* Technical detail — mirror the review-gate view so the ratified change-details
                  page is not just the business flow. Opened by default (the change page is where
                  a tech reader looks for the "how"); the raw analysis stays collapsed. */}
              {plan.functional_plan.implementation_approach && (
                <details style={{ marginTop: 8 }} open>
                  <summary style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)', cursor: 'pointer' }}>
                    🛠 Technical implementation
                  </summary>
                  {Array.isArray(plan.functional_plan.implementation_approach) ? (
                    <ol style={{ fontSize: 12.5, color: 'var(--text-secondary)', marginTop: 6, paddingLeft: 18 }}>
                      {plan.functional_plan.implementation_approach.map((s, i) => (
                        <li key={i} style={{ marginBottom: 3 }}>{typeof s === 'string' ? s : (s.text || JSON.stringify(s))}</li>
                      ))}
                    </ol>
                  ) : (
                    <div style={{ fontSize: 12.5, color: 'var(--text-secondary)', marginTop: 6, whiteSpace: 'pre-wrap' }}>
                      {asText(plan.functional_plan.implementation_approach)}
                    </div>
                  )}
                </details>
              )}
              <CriticalDecisionsView decisions={plan.technical_analysis?.critical_decisions} />
              {plan.technical_analysis && (
                <details style={{ marginTop: 8 }} open={showTech} onToggle={e => setShowTech(e.target.open)}>
                  <summary style={{ fontSize: 11.5, color: 'var(--text-muted)', cursor: 'pointer' }}>Show technical analysis</summary>
                  <TechnicalAnalysisView ta={plan.technical_analysis} />
                </details>
              )}
            </section>
          )}
        </>
      )}
    </div>
  )
}
