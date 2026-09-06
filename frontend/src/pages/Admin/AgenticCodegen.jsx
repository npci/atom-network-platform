// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useState, useRef, useEffect, useCallback } from 'react'
import { useQuery } from '@tanstack/react-query'
import { codeIndexingApi, agenticApi } from '../../services/api'
import RunUsageBadge from '../../components/RunUsageBadge'
import { wsUrl } from '../../utils/basePath'

// Agentic XSD-driven codegen — "chat the change" console (THE BOOK §13).
// Pick indexed repo(s), describe the change in plain language, and watch the
// durable event stream. No BRD/TSD needed. Inspect the diff, then approve.

const TERMINAL = ['completed', 'failed', 'cancelled', 'gave_up']

// Friendly phase labels for the "current phase" indicator.
const PHASE_LABELS = {
  pending: 'Starting', workspace_ready: 'Workspace', context_ready: 'Context',
  xsd_discovery: 'XSD discovery', code_change: 'Editing code', verification: 'Verifying',
  review: 'Reviewing', awaiting_human_approval: 'Awaiting your approval',
  awaiting_approach_decision: 'Choose an approach below',
  awaiting_verify_decision: 'Verification failed — your decision below',
  awaiting_code_decision: 'The agent needs your decision below',
  awaiting_schema_amendment: 'The agent needs a schema change approved — review it below',
  pushing: 'Pushing to git',
  completed: 'Completed', failed: 'Failed', cancelled: 'Cancelled', gave_up: 'Gave up',
}

// Per-kind presentation for the code-gen activity feed (icon + accent + label).
const KIND_META = {
  run_created:            { icon: '🎬', color: '#a78bfa', label: 'Run created' },
  drive_started:          { icon: '⚙️', color: '#a78bfa', label: 'Worker picked up the run' },
  workspace_start:        { icon: '📦', color: '#38bdf8', label: 'Preparing sandbox' },
  repo_indexing:          { icon: '🧱', color: '#38bdf8', label: 'Indexing sandbox' },
  repo_cloning:           { icon: '📥', color: '#38bdf8', label: 'Creating sandbox + cloning' },
  repo_cloned:            { icon: '🗂',  color: '#38bdf8', label: 'Sandbox ready' },
  workspace_ready:        { icon: '✅', color: '#34d399', label: 'Workspace ready' },
  repo_indexed:           { icon: '🧱', color: '#38bdf8', label: 'Sandbox indexed' },
  verifier_degraded:      { icon: '⚠️', color: '#f59e0b', label: 'No local build — CI will verify' },
  reasoning:              { icon: '💭', color: '#a78bfa', label: 'Thinking' },
  paused_transient:       { icon: '⏸', color: '#f59e0b', label: 'Paused (network) — auto-resuming' },
  lease_expired_recovered:{ icon: '♻️', color: '#a78bfa', label: 'Recovered — resuming' },
  resume_requested:       { icon: '▶️', color: '#a78bfa', label: 'Resume requested' },
  run_terminal:           { icon: '🛑', color: '#f87171', label: 'Run ended' },
  phase_changed:          { icon: '➡️', color: '#64748b', label: 'Phase' },
  context_ready:          { icon: '🧭', color: '#a78bfa', label: 'Context assembled' },
  xsd_scope:              { icon: '📐', color: '#f59e0b', label: 'XSD scope decided' },
  xsd_enum_occupancy:     { icon: '🔎', color: '#f59e0b', label: 'New schema values checked against the code' },
  needs_contract_amendment: { icon: '⚠️', color: '#f87171', label: 'Code phase needs a schema change Phase A did not make' },
  approach_proposal:      { icon: '🧭', color: '#f59e0b', label: 'Choose an approach (reuse vs new)' },
  approach_decided:       { icon: '✅', color: '#34d399', label: 'Approach chosen' },
  code_decision_needed:   { icon: '❓', color: '#f87171', label: 'The code agent needs a decision' },
  code_decision_answered: { icon: '✅', color: '#34d399', label: 'Decision recorded — resuming' },
  code_decision_loop:     { icon: '🔁', color: '#f87171', label: 'The agent is re-asking a question you already answered' },
  schema_amendment_needed:   { icon: '🧾', color: '#f59e0b', label: 'A schema change needs your approval' },
  schema_amendment_approved: { icon: '✅', color: '#34d399', label: 'Schema amendment applied — resuming' },
  schema_amendment_rejected: { icon: '🚫', color: '#f87171', label: 'Schema amendment rejected — implement around it' },
  schema_amendment_partial:  { icon: '⚠️', color: '#f59e0b', label: 'Schema amendment only partly applied' },
  change_set:             { icon: '📦', color: '#34d399', label: 'Change set produced' },
  verification:           { icon: '✅', color: '#34d399', label: 'Verification' },
  review:                 { icon: '🔍', color: '#60a5fa', label: 'Review' },
  manifest_frozen:        { icon: '🧊', color: '#22d3ee', label: 'Manifest frozen — awaiting approval' },
  llm_turn:               { icon: '🤖', color: '#94a3b8', label: 'Thinking' },
  loop_done:              { icon: '🏁', color: '#94a3b8', label: 'Loop done' },
  loop_capped:            { icon: '🛑', color: '#f59e0b', label: 'Iteration cap hit' },
  loop_cancelled:         { icon: '🚫', color: '#f87171', label: 'Cancelled' },
  push_started:           { icon: '🚀', color: '#34d399', label: 'Push started' },
  completed:              { icon: '🎉', color: '#34d399', label: 'Completed' },
  push_failed:            { icon: '⚠️', color: '#f87171', label: 'Push failed' },
  push_skipped:           { icon: '⏭',  color: '#f59e0b', label: 'Push skipped' },
  push_preflight_failed:  { icon: '⚠️', color: '#f87171', label: 'Push preflight failed' },
}

function eventView(e) {
  const p = e.payload || {}
  if (e.kind === 'tool_call') {
    const ok = !p.is_error
    return { icon: ok ? '🛠' : '⚠️', color: ok ? '#7dd3fc' : '#f87171',
             label: p.action || p.name || 'tool', detail: (p.detail || p.result || '').toString() }
  }
  if (e.kind === 'phase_changed') {
    return { icon: '➡️', color: '#64748b', label: `Phase → ${p.to || ''}`, detail: '' }
  }
  if (e.kind === 'reasoning') {
    return { icon: '💭', color: '#a78bfa', label: 'Thinking', detail: (p.action || '').replace(/^💭\s*/, '') }
  }
  if (e.kind === 'verification') {
    const st = p.status
    const icon = st === 'verified' ? '✅' : st === 'needs_fix' ? '❌' : '⚠️'
    const color = st === 'verified' ? '#34d399' : st === 'needs_fix' ? '#f87171' : '#f59e0b'
    return { icon, color, label: p.action || 'Verification', detail: (p.errors || []).join('\n') }
  }
  if (e.kind === 'run_terminal') {
    const ok = p.status === 'completed'
    return { icon: ok ? '🎉' : '🛑', color: ok ? '#34d399' : '#f87171',
             label: `Run ${p.status}`, detail: p.error || '' }
  }
  const m = KIND_META[e.kind] || { icon: '•', color: '#94a3b8', label: e.kind }
  return { icon: m.icon, color: m.color, label: p.action || m.label,
           detail: e.kind === 'llm_turn' ? (p.text || '') : '' }
}

export default function AgenticCodegen() {
  const [selected, setSelected] = useState([])
  const [intent, setIntent] = useState('')
  const [run, setRun] = useState(null)          // {run_id, phase, status, manifest_hash}
  const [events, setEvents] = useState([])
  const [diffs, setDiffs] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [expanded, setExpanded] = useState(() => new Set())
  const [selOpt, setSelOpt] = useState(null)     // chosen approach option id
  const [custom, setCustom] = useState('')       // free-text "exactly what I need"
  const [confirmApprove, setConfirmApprove] = useState(false)
  const [zipBusy, setZipBusy] = useState(false)  // code-ZIP download in flight
  const wsRef = useRef(null)
  const logRef = useRef(null)
  const toggle = (key) => setExpanded(s => {
    const n = new Set(s); n.has(key) ? n.delete(key) : n.add(key); return n
  })

  const { data: repos, isLoading } = useQuery({
    queryKey: ['code-repos'],
    queryFn: () => codeIndexingApi.listRepos().then(r => r.data),
  })
  const indexed = (Array.isArray(repos) ? repos : []).filter(
    r => r.last_indexed_at && (r.chunks_count ?? 0) > 0)

  // Run history — newest first; refetched whenever the active run's status changes.
  const { data: history, refetch: refetchHistory } = useQuery({
    queryKey: ['agentic-runs'],
    queryFn: () => agenticApi.listRuns().then(r => r.data.runs),
  })
  useEffect(() => { refetchHistory() }, [run?.status, refetchHistory])

  useEffect(() => { if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight }, [events])
  useEffect(() => () => wsRef.current?.close(), [])

  const openStream = useCallback((runId) => {
    wsRef.current?.close()
    const ws = new WebSocket(wsUrl(`api/ws/agentic/runs/${runId}`))
    wsRef.current = ws
    // Auth in the FIRST FRAME, never the URL. A token in the query string is
    // written to nginx's access log (the default `combined` format logs the
    // whole request line), and to any intermediate proxy log and browser
    // history — so every stream open used to persist a live, privileged JWT
    // somewhere with weaker access control than the app itself. Frame bodies
    // are not logged. `after_seq: -1` replays the full history, matching what
    // the server assumed for the query-param path.
    ws.onopen = () => {
      // Auth rides the handshake: the session cookie is httpOnly and the
      // browser attaches it to the WebSocket upgrade automatically, so no
      // token is sent (or readable) here. The hello frame is still sent —
      // the server reads one frame before streaming.
      ws.send(JSON.stringify({ after_seq: -1 }))
    }
    ws.onmessage = async (evt) => {
      const msg = JSON.parse(evt.data)
      if (msg.type === 'event') {
        setEvents(prev => { const next = [...prev, msg]; return next.length > 2000 ? next.slice(-1500) : next })
        if (msg.kind === 'manifest_frozen' || msg.kind === 'phase_changed') {
          try { setRun((await agenticApi.getRun(runId)).data) } catch { /* noop */ }
        }
        if (msg.kind === 'manifest_frozen') {
          try { setDiffs((await agenticApi.getDiff(runId)).data.diffs) } catch { /* noop */ }
        }
      } else if (msg.type === 'end') {
        agenticApi.getRun(runId).then(r => setRun(r.data)).catch(() => {})
        try { setDiffs((await agenticApi.getDiff(runId)).data.diffs) } catch { /* noop */ }   // durable artifact
      } else if (msg.type === 'error') {
        setError(msg.detail)
      }
    }
    ws.onerror = () => setError('WebSocket connection error — check network and retry.')
    ws.onclose = (e) => { if (e.code !== 1000) setError('WebSocket disconnected unexpectedly.') }
  }, [])

  const start = async () => {
    setError(null); setBusy(true); setEvents([]); setDiffs(null); setRun(null); setExpanded(new Set())
    setSelOpt(null); setCustom('')
    try {
      const { data } = await agenticApi.quickStart({ repo_ids: selected, intent })
      setRun(data)
      openStream(data.run_id)
    } catch (e) {
      setError(e?.response?.data?.detail || e.message)
    } finally { setBusy(false) }
  }

  // Revisit a past run: replay its full event history (WS sends from seq -1) + diff.
  const openRun = useCallback(async (runId) => {
    setError(null); setEvents([]); setDiffs(null); setExpanded(new Set()); setSelOpt(null); setCustom('')
    try {
      setRun((await agenticApi.getRun(runId)).data)
      try { setDiffs((await agenticApi.getDiff(runId)).data.diffs) } catch { /* noop */ }
      openStream(runId)
    } catch (e) { setError(e?.response?.data?.detail || e.message) }
  }, [openStream])

  const approve = async (pushNow = true, overrideBlockers = false) => {
    if (!run?.manifest_hash) return
    if (pushNow && !confirmApprove) { setConfirmApprove(true); return }
    setConfirmApprove(false)
    try {
      await agenticApi.approve(run.run_id, run.manifest_hash, pushNow, overrideBlockers)
      setRun(r => pushNow ? { ...r, status: 'pushing' }
        : { ...r, status: 'completed', phase: 'completed', push_deferred: true })
    } catch (e) { setError(e?.response?.data?.detail || e.message) }
  }

  const pushNow = async (overrideBlockers = false) => {
    if (!run) return
    try {
      await agenticApi.pushRun(run.run_id, overrideBlockers)
      setRun(r => ({ ...r, status: 'active', phase: 'pushing', push_deferred: false }))
      openStream(run.run_id)
    } catch (e) { setError(e?.response?.data?.detail || e.message) }
  }

  const cancel = async () => {
    if (!run) return
    try { await agenticApi.cancel(run.run_id) } catch { /* noop */ }
  }

  const resume = useCallback(async (runId) => {
    setError(null)
    try {
      await agenticApi.resume(runId)
      openRun(runId)                 // reload + restream the now-active run
      refetchHistory()
    } catch (e) { setError(e?.response?.data?.detail || e.message) }
  }, [openRun, refetchHistory])

  const running = run && !TERMINAL.includes(run.status)
  const awaiting = run?.phase === 'awaiting_human_approval'
  // The change-set artifact is present whenever any repo has a real diff (not a placeholder).
  const hasDiffs = diffs && Object.values(diffs).some(d => d && !String(d).trim().startsWith('('))
  // A run can be resumed if it isn't completed and isn't sitting at approval.
  const resumable = (r) => r && r.status !== 'completed' && r.phase !== 'awaiting_human_approval'

  // Reuse-first approach gate (also fires in this console for full runs).
  const proposal = [...events].reverse().find(e => e.kind === 'approach_proposal')?.payload
  const deciding = run?.phase === 'awaiting_approach_decision'
  // Unresolved blocker-severity review finding → push is held; shipping needs a deliberate override.
  const openBlocker = [...events].reverse().find(e => e.kind === 'review_blocked')?.payload || null
  const blkItems = openBlocker?.items || []
  useEffect(() => {
    if (deciding && proposal && selOpt === null) setSelOpt(proposal.recommended || proposal.options?.[0]?.id || null)
  }, [deciding, proposal, selOpt])
  const decide = async () => {
    if (!run) return
    setError(null)
    const opt = (proposal?.options || []).find(o => o.id === selOpt)
    try {
      await agenticApi.decideApproach(run.run_id, {
        selected_option_id: selOpt || undefined, custom_direction: custom.trim() || undefined, option: opt })
      setRun(r => ({ ...r, phase: 'xsd_discovery', status: 'active' })); setCustom('')
    } catch (e) { setError(e?.response?.data?.detail || e.message) }
  }

  // Verification gate (after repeated failed builds): one more attempt, or accept unverified.
  const atVerifyGate = run?.phase === 'awaiting_verify_decision'
  const decideVerify = async (action) => {
    if (!run) return
    setError(null)
    try {
      await agenticApi.decideVerify(run.run_id, action)
      setRun(r => ({ ...r, phase: action === 'retry' ? 'code_change' : 'review', status: 'active' }))
      openStream(run.run_id)
    } catch (e) { setError(e?.response?.data?.detail || e.message) }
  }

  // A3 gate: the code agent surfaced a decision it must not make itself (ask_decision).
  const atCodeDecisionGate = run?.phase === 'awaiting_code_decision'
  const codeDecisionEvent = [...events].reverse().find(e => e.kind === 'code_decision_needed')
  const codeDecisionOptions = codeDecisionEvent?.payload?.options || []
  const codeOptId = (o, i) => String(o.id ?? `opt-${i}`)
  const codeSelOpt = codeDecisionOptions.find((o, i) => codeOptId(o, i) === selOpt) || null
  const codeOptText = (o) => String((o && (o.label ?? o.title)) || '').trim()
  const codeSubmittable = !!custom.trim() || !!(codeSelOpt && (codeSelOpt.id != null || codeOptText(codeSelOpt)))
  const decideCode = async () => {
    if (!run || !codeSubmittable) return
    setError(null)
    try {
      await agenticApi.decideCodeDecision(run.run_id, {
        answer: custom.trim() || (codeSelOpt && codeSelOpt.id == null ? codeOptText(codeSelOpt) : ''),
        chosen_option_id: codeSelOpt && codeSelOpt.id != null ? String(codeSelOpt.id) : undefined,
      })
      setRun(r => ({ ...r, phase: 'code_change', status: 'active' }))
      setCustom(''); setSelOpt(null)
      openStream(run.run_id)
    } catch (e) { setError(e?.response?.data?.detail || e.message) }
  }

  // Download the generated code (every selected repo's working tree) as a ZIP —
  // same artifact Phase B offers, so the change can be inspected locally before/after push.
  const downloadZip = async () => {
    if (!run || zipBusy) return
    setError(null); setZipBusy(true)
    try {
      const res = await agenticApi.workspaceZip(run.run_id)
      const url = URL.createObjectURL(res.data)
      const a = document.createElement('a')
      a.href = url
      a.download = `agent-code-${run.run_id.slice(0, 8)}.zip`
      a.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      // responseType:'blob' wraps the JSON error body in a Blob — unwrap for the message.
      let detail = e?.response?.data?.detail
      if (!detail && e?.response?.data instanceof Blob) {
        try { detail = JSON.parse(await e.response.data.text())?.detail } catch { /* noop */ }
      }
      setError(detail || e.message)
    } finally { setZipBusy(false) }
  }

  return (
    <div style={{ padding: 24, maxWidth: 1320, margin: '0 auto' }}>
      <h1 style={{ marginBottom: 4 }}>Agentic Codegen</h1>
      <p style={{ color: '#666', marginTop: 0 }}>
        Pick indexed repo(s), describe the change in plain language, and the agent discovers
        the XSD scope, edits the code, compiles + verifies, reviews, and freezes a manifest for
        your approval. No BRD/TSD required.
      </p>

      {/* repo picker */}
      <section style={{ marginBottom: 16 }}>
        <strong>1. Repositories</strong>
        {isLoading ? <p>Loading repos…</p> : indexed.length === 0 ? (
          <p style={{ color: '#b45309' }}>
            No indexed repos. Go to <a href="admin/code-indexing">Code Indexing</a>, add a repo,
            and index it first.
          </p>
        ) : (
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginTop: 6 }}>
            {indexed.map(r => (
              <label key={r.id} style={{
                border: '1px solid #ccc', borderRadius: 6, padding: '6px 10px', cursor: 'pointer',
                background: selected.includes(r.id) ? '#e0f2fe' : '#fff',
              }}>
                <input type="checkbox" checked={selected.includes(r.id)}
                  onChange={e => setSelected(s => e.target.checked ? [...s, r.id] : s.filter(x => x !== r.id))} />
                {' '}{r.label} <span style={{ color: '#888' }}>({r.chunks_count} chunks · {r.role || 'app'})</span>
              </label>
            ))}
          </div>
        )}
      </section>

      {/* intent */}
      <section style={{ marginBottom: 16 }}>
        <strong>2. What should change?</strong>
        <textarea value={intent} onChange={e => setIntent(e.target.value)} rows={3}
          placeholder='e.g. "Add a `status` field (APPROVED/DECLINED) to RefundRequest in the XSD and have RefundService read it."'
          style={{ width: '100%', marginTop: 6, padding: 10, fontFamily: 'inherit' }} />
        <button onClick={start} disabled={busy || running || !intent.trim() || selected.length === 0}
          style={{ padding: '8px 16px', fontWeight: 600, fontSize: 13, borderRadius: 6, border: '1px solid transparent',
            background: 'var(--accent, #2563eb)', color: '#fff', cursor: 'pointer',
            opacity: (busy || running || !intent.trim() || selected.length === 0) ? 0.55 : 1 }}>
          {busy ? 'Starting…' : running ? 'Running…' : 'Run change'}
        </button>
        {running && <button onClick={cancel} style={{ marginLeft: 8, padding: '8px 16px', fontSize: 13, fontWeight: 600,
          borderRadius: 6, border: '1px solid transparent', background: '#334155', color: '#fff', cursor: 'pointer' }}>Cancel</button>}
      </section>

      {error && <div style={{ color: '#b91c1c', marginBottom: 12 }}>⚠ {error}</div>}

      {/* run status + event stream */}
      {run && (() => {
        const phaseEvents = events.filter(e => e.kind === 'phase_changed')
        const curPhase = (TERMINAL.includes(run.status) ? run.status
          : run.phase || (phaseEvents.length ? phaseEvents[phaseEvents.length - 1].payload?.to : 'pending'))
        const lastActive = [...events].reverse().find(e => e.kind !== 'phase_changed')
        const curAction = lastActive ? eventView(lastActive).label : 'starting…'
        return (
        <section style={{ marginBottom: 16 }}>
          {/* current phase + action banner */}
          <div style={{
            display: 'flex', alignItems: 'center', gap: 12, padding: '10px 14px', marginBottom: 8,
            borderRadius: 8, background: running ? 'rgba(56,189,248,0.08)' : 'rgba(100,116,139,0.08)',
            border: '1px solid ' + (running ? 'rgba(56,189,248,0.3)' : 'var(--border)'),
          }}>
            {running && <span style={{
              width: 12, height: 12, borderRadius: '50%', flexShrink: 0,
              border: '2px solid #38bdf8', borderTopColor: 'transparent',
              animation: 'spin 0.8s linear infinite',
            }} />}
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: 11, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
                Phase
              </div>
              <div style={{ fontWeight: 700, fontSize: 15, color: 'var(--text-primary)' }}>
                {PHASE_LABELS[curPhase] || curPhase}
              </div>
            </div>
            <div style={{ flex: 2, minWidth: 0 }}>
              <div style={{ fontSize: 11, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
                {running ? 'Now' : 'Last'}
              </div>
              <div style={{ fontSize: 13, color: 'var(--text-secondary)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                {curAction}
              </div>
            </div>
            {run?.run_id && <RunUsageBadge runId={run.run_id} active={!!running} />}
            <code style={{ fontSize: 10, color: 'var(--text-muted)', flexShrink: 0 }}>{run.run_id?.slice(0, 8)}</code>
          </div>

          {['failed', 'gave_up', 'cancelled'].includes(run.status) && (
            <div style={{
              padding: '12px 14px', marginBottom: 8, borderRadius: 8,
              background: 'rgba(248,113,113,0.10)', border: '1px solid rgba(248,113,113,0.4)',
            }}>
              <div style={{ fontWeight: 700, color: '#fca5a5' }}>
                ⛔ Run {run.status} — here's why:
              </div>
              <div style={{ color: '#fecaca', fontSize: 13, marginTop: 4, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                {run.error || 'No reason recorded. Check the activity log below.'}
              </div>
              {resumable(run) && (
                <button onClick={() => resume(run.run_id)} style={{
                  marginTop: 10, padding: '7px 16px', fontWeight: 600, background: '#a78bfa',
                  color: '#1e1b2e', border: 'none', borderRadius: 6, cursor: 'pointer',
                }}>▶ Resume from where it stopped</button>
              )}
            </div>
          )}
          <div style={{ fontSize: 11, color: '#64748b', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 4 }}>
            Code-gen activity · click a row to expand
          </div>
          <div ref={logRef} style={{
            background: '#0b1021', fontFamily: 'ui-monospace, monospace', fontSize: 12.5,
            padding: '10px 14px', borderRadius: 8, height: '60vh', minHeight: 420,
            overflowY: 'auto', border: '1px solid #1e293b', resize: 'vertical',
          }}>
            {events.length === 0 && <div style={{ color: '#64748b', padding: '8px 0' }}>waiting for activity…</div>}
            {events.map((e, i) => {
              const v = eventView(e)
              const key = e.seq ?? i
              const t = e.ts ? new Date(e.ts).toLocaleTimeString() : ''
              const hasDetail = Boolean(v.detail && v.detail.trim())
              const isOpen = expanded.has(key)
              const preview = hasDetail ? v.detail.trim().split('\n')[0].slice(0, 90) : ''
              return (
                <div key={key} style={{ padding: '4px 0', borderBottom: '1px solid #131a33' }}>
                  <div onClick={hasDetail ? () => toggle(key) : undefined}
                    style={{ display: 'flex', gap: 8, alignItems: 'baseline', cursor: hasDetail ? 'pointer' : 'default' }}>
                    <span style={{ width: 14, textAlign: 'center', flexShrink: 0, color: '#475569' }}>
                      {hasDetail ? (isOpen ? '▾' : '▸') : ''}
                    </span>
                    <span style={{ width: 18, textAlign: 'center', flexShrink: 0 }}>{v.icon}</span>
                    <span style={{ color: v.color, fontWeight: 600, flexShrink: 0 }}>{v.label}</span>
                    {hasDetail && !isOpen && (
                      <span style={{ color: '#64748b', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                        {preview}…
                      </span>
                    )}
                    <span style={{ marginLeft: 'auto', color: '#475569', fontSize: 10, flexShrink: 0, paddingLeft: 8 }}>{t}</span>
                  </div>
                  {hasDetail && isOpen && (
                    <pre style={{
                      margin: '6px 0 2px 40px', padding: '8px 10px', background: '#060912',
                      border: '1px solid #1e293b', borderRadius: 6,
                      whiteSpace: 'pre-wrap', wordBreak: 'break-word', maxHeight: 420, overflowY: 'auto',
                    }}>
                      {v.detail.split('\n').map((line, li) => {
                        const c = (line.startsWith('+') || line.startsWith('+++')) ? '#4ade80'
                          : (line.startsWith('-') || line.startsWith('---')) ? '#f87171'
                          : line.startsWith('@@') ? '#38bdf8' : '#cbd5e1'
                        return <div key={li} style={{ color: c }}>{line || ' '}</div>
                      })}
                    </pre>
                  )}
                </div>
              )
            })}
          </div>
        </section>
        )
      })()}

      {/* reuse-first decision gate — choose how to accommodate the requirement before any edit */}
      {deciding && proposal && (
        <section style={{ marginTop: 16, padding: '14px 16px', borderRadius: 8,
          background: 'rgba(245,158,11,0.06)', border: '1px solid rgba(245,158,11,0.35)' }}>
          <strong style={{ fontSize: 15 }}>🧭 How should we accommodate this?</strong>
          {proposal.summary && <p style={{ color: '#94a3b8', fontSize: 13, marginTop: 4 }}>{proposal.summary}</p>}
          <p style={{ color: '#64748b', fontSize: 12 }}>
            The agent analysed the existing flows. Pick an approach (recommended pre-selected) or describe exactly what you need.
          </p>
          {[...(proposal.options || [])]
            .sort((a, b) => (b.id === proposal.recommended) - (a.id === proposal.recommended))
            .map(o => (
            <label key={o.id} style={{ display: 'block', margin: '8px 0', padding: '10px 12px', borderRadius: 6, cursor: 'pointer',
              background: selOpt === o.id ? 'rgba(96,165,250,0.10)' : 'transparent',
              border: '1px solid ' + (selOpt === o.id ? 'rgba(96,165,250,0.5)' : '#1e293b') }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                <input type="radio" name="approach" checked={selOpt === o.id} onChange={() => setSelOpt(o.id)} />
                <span style={{ fontWeight: 700 }}>{o.title || o.id}</span>
                <span style={{ fontSize: 10, fontWeight: 700, padding: '1px 6px', borderRadius: 10, background: 'rgba(148,163,184,0.15)', color: '#94a3b8' }}>{o.approach}</span>
                {o.id === proposal.recommended && <span style={{ fontSize: 10, fontWeight: 700, padding: '1px 6px', borderRadius: 10, background: 'rgba(52,211,153,0.15)', color: '#16a34a', border: '1px solid rgba(52,211,153,0.4)' }}>RECOMMENDED</span>}
                {o.diverges_from_plan && <span title="Differs from what the ratified analysis plan recommended" style={{ fontSize: 10, fontWeight: 700, padding: '1px 6px', borderRadius: 10, background: 'rgba(245,158,11,0.15)', color: '#d97706', border: '1px solid rgba(245,158,11,0.5)' }}>⚠ DIVERGES FROM PLAN</span>}
              </div>
              {o.diverges_from_plan && o.divergence_note && (
                <div style={{ fontSize: 12, color: '#d97706', marginTop: 6, paddingLeft: 8, borderLeft: '2px solid rgba(245,158,11,0.5)' }}>
                  <strong>Why this differs from the plan:</strong> {o.divergence_note}
                </div>
              )}
              {o.target_api && <div style={{ fontSize: 12, color: '#94a3b8', marginTop: 4 }}>Fits into: <code style={{ color: '#60a5fa' }}>{o.target_api}</code></div>}
              {o.how_it_fits && <div style={{ fontSize: 12, color: '#94a3b8', marginTop: 2 }}>{o.how_it_fits}</div>}
              {o.tradeoffs && <div style={{ fontSize: 12, color: '#64748b', marginTop: 2 }}>Tradeoffs: {o.tradeoffs}</div>}
            </label>
          ))}
          <textarea value={custom} onChange={e => setCustom(e.target.value)} rows={2}
            placeholder="…or describe exactly what you need (overrides the selected option)"
            style={{ width: '100%', marginTop: 6, padding: 8, borderRadius: 6, border: '1px solid #1e293b', background: '#0b1021', color: '#cbd5e1', fontSize: 13 }} />
          <button onClick={decide} disabled={!selOpt && !custom.trim()}
            style={{ marginTop: 8, padding: '8px 16px', fontWeight: 600, background: '#2563eb', color: '#fff', border: 'none', borderRadius: 6, cursor: 'pointer' }}>
            Proceed with this approach</button>
        </section>
      )}

      {/* Verification gate — repeated build failures parked the run for a human call. */}
      {atVerifyGate && (
        <section style={{ marginTop: 16, padding: '14px 16px', borderRadius: 8,
          background: 'rgba(248,113,113,0.06)', border: '1px solid rgba(248,113,113,0.4)' }}>
          <strong style={{ fontSize: 15, color: '#f87171' }}>🧪 Verification keeps failing — your call</strong>
          <p style={{ color: '#94a3b8', fontSize: 13, marginTop: 6 }}>
            The build/tests failed repeatedly (details in the activity log above). Retry runs exactly ONE
            more fix→verify cycle; accepting proceeds to review with the change marked UNVERIFIED.
          </p>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            <button onClick={() => decideVerify('retry')}
              style={{ padding: '8px 16px', fontWeight: 600, background: '#2563eb', color: '#fff',
                border: 'none', borderRadius: 6, cursor: 'pointer' }}>↻ Retry once more</button>
            <button onClick={() => decideVerify('skip')}
              style={{ padding: '8px 16px', fontWeight: 600, background: 'transparent', color: '#f59e0b',
                border: '1px solid #f59e0b', borderRadius: 6, cursor: 'pointer' }}>Accept unverified → review</button>
          </div>
        </section>
      )}

      {/* A3 gate: the code agent surfaced a decision it must not make itself. The agent isn't
          required to offer options — always show a free-text box in addition to any options. */}
      {atCodeDecisionGate && (
        <section style={{ marginTop: 16, padding: '14px 16px', borderRadius: 8,
          background: 'rgba(239,68,68,0.06)', border: '1px solid rgba(239,68,68,0.4)' }}>
          <strong style={{ fontSize: 15, color: '#f87171' }}>❓ The code agent needs your decision</strong>
          {codeDecisionEvent?.payload?.blocked_item && (
            <div style={{ fontSize: 12.5, color: '#94a3b8', marginTop: 6 }}>
              Blocked on: <code>{codeDecisionEvent.payload.blocked_item}</code>
            </div>
          )}
          <p style={{ color: '#cbd5e1', fontSize: 13, marginTop: 6, lineHeight: 1.6 }}>
            {codeDecisionEvent?.payload?.question || 'The agent needs a decision to continue — see the activity log above for context.'}
          </p>
          {codeDecisionOptions.map((o, i) => {
            const oid = codeOptId(o, i)
            return (
              <label key={oid} style={{ display: 'flex', alignItems: 'flex-start', gap: 8, cursor: 'pointer',
                margin: '8px 0', padding: '10px 12px', borderRadius: 6,
                background: selOpt === oid ? 'rgba(96,165,250,0.10)' : 'transparent',
                border: '1px solid ' + (selOpt === oid ? 'rgba(96,165,250,0.5)' : '#1e293b') }}>
                <input type="radio" name="code-decision" checked={selOpt === oid}
                  onChange={() => setSelOpt(oid)} style={{ marginTop: 3 }} />
                <span>
                  <span style={{ fontWeight: 700 }}>{o.label || o.title || oid}</span>
                  {(o.consequence || o.detail) && (
                    <div style={{ fontSize: 12, color: '#94a3b8', marginTop: 2 }}>{o.consequence || o.detail}</div>
                  )}
                </span>
              </label>
            )
          })}
          <textarea value={custom} onChange={e => setCustom(e.target.value)} rows={3}
            placeholder={codeDecisionOptions.length > 0
              ? '…or describe exactly what you want instead (overrides the selected option)'
              : 'Type your decision (required — no options were offered for this one)'}
            style={{ width: '100%', marginTop: 6, padding: 8, borderRadius: 6, border: '1px solid #1e293b',
              background: '#0b1021', color: '#cbd5e1', fontSize: 13 }} />
          <button onClick={decideCode} disabled={!codeSubmittable}
            style={{ marginTop: 8, padding: '8px 16px', fontWeight: 600, background: '#2563eb', color: '#fff',
              border: 'none', borderRadius: 6, cursor: codeSubmittable ? 'pointer' : 'not-allowed',
              opacity: codeSubmittable ? 1 : 0.6 }}>
            Submit decision & resume code generation
          </button>
        </section>
      )}

      {/* Persistent CHANGES ARTIFACT — full git-diff, inspectable during the run AND
          after (stored on the manifest; survives push + workspace GC). */}
      {hasDiffs && (
        <section style={{ marginTop: 16 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
            <strong style={{ fontSize: 15 }}>📦 Changes {awaiting ? '(proposed)' : '(artifact)'}</strong>
            {(awaiting || TERMINAL.includes(run?.status)) && <span style={{
              fontSize: 11, fontWeight: 700, padding: '2px 8px', borderRadius: 12,
              background: 'rgba(52,211,153,0.15)', color: '#16a34a', border: '1px solid rgba(52,211,153,0.4)',
            }}>✅ VERIFIED — compiles</span>}
            <span style={{ color: '#94a3b8', fontSize: 12 }}>
              {Object.values(diffs).reduce((n, d) => n + ((d.match(/^diff --git/gm) || []).length || (d && d !== '(no changes)' ? 1 : 0)), 0)} file(s) changed
              {!awaiting && ' · saved'}
            </span>
            <button onClick={downloadZip} disabled={zipBusy}
              title="Download every selected repo's working tree (the agent's edits applied) as a ZIP — inspect locally before or after the push"
              style={{ marginLeft: 'auto', padding: '6px 14px', fontSize: 12.5, fontWeight: 600,
                background: 'transparent', color: '#38bdf8', border: '1px solid #38bdf8',
                borderRadius: 6, cursor: zipBusy ? 'wait' : 'pointer', flexShrink: 0 }}>
              {zipBusy ? 'Zipping…' : '⬇ Download code (ZIP)'}
            </button>
          </div>
          {Object.entries(diffs).map(([rid, d]) => (
            <details key={rid} open style={{ marginBottom: 10 }}>
              <summary style={{ cursor: 'pointer', color: 'var(--text-secondary)', fontFamily: 'monospace', fontSize: 12, marginBottom: 4 }}>
                repo {rid.slice(0, 8)}
              </summary>
              <pre style={{
                background: '#0b1021', border: '1px solid #1e293b', borderRadius: 6, color: '#cbd5e1',
                padding: 12, overflowX: 'auto', fontSize: 12, lineHeight: 1.5, maxHeight: 480,
              }}>
                {(d || '').split('\n').map((line, i) => {
                  const c = line.startsWith('+') && !line.startsWith('+++') ? '#4ade80'
                    : line.startsWith('-') && !line.startsWith('---') ? '#f87171'
                    : line.startsWith('@@') ? '#38bdf8'
                    : line.startsWith('diff --git') || line.startsWith('index ') ? '#a78bfa' : '#94a3b8'
                  return <div key={i} style={{ color: c, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{line || ' '}</div>
                })}
              </pre>
            </details>
          ))}
          <div style={{ color: '#666', fontSize: 12, marginBottom: 8 }}>
            manifest_hash: <code>{run.manifest_hash?.slice(0, 16)}…</code>
          </div>
          {awaiting && openBlocker && (
            <div style={{ fontSize: 13, marginBottom: 10, padding: '10px 12px', borderRadius: 6,
              background: 'rgba(239,68,68,0.10)', border: '1px solid rgba(239,68,68,0.55)', color: '#ef4444' }}>
              <div style={{ fontWeight: 700 }}>⛔ Unresolved blocker — push is held</div>
              {blkItems.some(it => it.reviewer_gap) && (
                <div style={{ fontSize: 12, color: '#d97706', marginTop: 4 }}>
                  Some items are REVIEWER GAPS — the reviewer could not produce a verdict for them
                  (not confirmed code defects). Adjudicate these directly rather than sending the
                  agent to fix them.
                </div>
              )}
              <ul style={{ margin: '6px 0 0', paddingLeft: 18, color: '#94a3b8' }}>
                {blkItems.slice(0, 6).map((it, i) => (
                  <li key={i} style={{ marginBottom: 4 }}>
                    {it.reviewer_gap && (
                      <span title="Verdict gap — the reviewer could not verify this; adjudicate directly"
                        style={{ fontSize: 10, fontWeight: 700, padding: '0 5px', borderRadius: 8,
                          background: 'rgba(245,158,11,0.2)', color: '#d97706', marginRight: 5 }}>
                        ⚠ REVIEWER GAP
                      </span>
                    )}
                    <code style={{ color: '#f87171' }}>{(it.file || '?').split('/').pop()}:{it.line || '?'}</code> — {it.why}
                    {it.done_when && (
                      <div style={{ fontSize: 11, color: '#94a3b8', marginTop: 2 }}>✓ done when: {it.done_when}</div>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {awaiting && (
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              <button onClick={() => approve(true, !!openBlocker)} onBlur={() => setConfirmApprove(false)}
                style={{ padding: '8px 16px', fontWeight: 600, color: '#fff', border: 'none', borderRadius: 6, cursor: 'pointer',
                  background: confirmApprove ? '#dc2626' : (openBlocker ? '#b91c1c' : '#16a34a') }}>
                {confirmApprove
                  ? (openBlocker ? 'Click again to push despite the blocker' : 'Click again to confirm push')
                  : (openBlocker ? '⛔ Override blocker & push' : 'Approve & push')}
              </button>
              <button onClick={() => approve(false)}
                title="Approve the change now — push the branch later from this page"
                style={{ padding: '8px 16px', fontWeight: 600, background: 'transparent', color: '#16a34a',
                  border: '1px solid #16a34a', borderRadius: 6, cursor: 'pointer' }}>
                Approve — push later
              </button>
              {/* Same ZIP as the section header, repeated at the decision point — with a long
                  diff the header button scrolls out of view, so surface it next to Approve too. */}
              <button onClick={downloadZip} disabled={zipBusy}
                title="Download every selected repo's working tree (the agent's edits applied) as a ZIP — inspect locally before approving/pushing"
                style={{ padding: '8px 16px', fontWeight: 600, background: 'transparent', color: '#38bdf8',
                  border: '1px solid #38bdf8', borderRadius: 6, cursor: zipBusy ? 'wait' : 'pointer' }}>
                {zipBusy ? 'Zipping…' : '⬇ Download code (ZIP)'}
              </button>
            </div>
          )}
          {run?.push_deferred && (
            <div style={{ marginTop: 10, padding: '10px 12px', borderRadius: 8, display: 'flex',
              alignItems: 'center', gap: 12, flexWrap: 'wrap',
              background: 'rgba(245,158,11,0.08)', border: '1px solid rgba(245,158,11,0.4)' }}>
              <span style={{ flex: 1, minWidth: 200, fontSize: 13, color: '#f59e0b', fontWeight: 600 }}>
                ✓ Approved — not pushed to git yet
              </span>
              <button onClick={() => pushNow(!!openBlocker)} style={{ padding: '7px 14px', fontWeight: 600,
                background: openBlocker ? '#b91c1c' : '#16a34a',
                color: '#fff', border: 'none', borderRadius: 6, cursor: 'pointer', flexShrink: 0 }}>
                {openBlocker ? '⛔ Override blocker & push' : '⬆ Push to git now'}
              </button>
            </div>
          )}
        </section>
      )}

      {/* run history — revisit any past run (replays its events + diff) */}
      {Array.isArray(history) && history.length > 0 && (
        <section style={{ marginTop: 24 }}>
          <strong style={{ fontSize: 14 }}>History</strong>
          <div style={{ marginTop: 8, border: '1px solid var(--border)', borderRadius: 8, overflow: 'hidden' }}>
            {history.map(h => {
              const term = ['completed', 'failed', 'cancelled', 'gave_up']
              const isTerm = term.includes(h.status)
              const color = h.status === 'completed' ? '#16a34a'
                : (h.status === 'failed' || h.status === 'gave_up') ? '#dc2626'
                : h.status === 'cancelled' ? '#94a3b8' : '#2563eb'
              const label = isTerm ? h.status : (h.phase || h.status)
              const active = run?.run_id === h.run_id
              return (
                <div key={h.run_id} onClick={() => openRun(h.run_id)}
                  style={{
                    display: 'flex', alignItems: 'center', gap: 10, padding: '8px 12px', cursor: 'pointer',
                    borderBottom: '1px solid var(--border)',
                    background: active ? 'rgba(56,189,248,0.08)' : 'transparent',
                  }}>
                  <span style={{
                    fontSize: 10, fontWeight: 700, textTransform: 'uppercase', color: '#fff',
                    background: color, padding: '2px 7px', borderRadius: 10, flexShrink: 0, minWidth: 64, textAlign: 'center',
                  }}>{label}</span>
                  <span style={{ flex: 1, minWidth: 0, fontSize: 13, color: 'var(--text-primary)',
                    whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                    {h.intent}
                  </span>
                  <span style={{ fontSize: 11, color: 'var(--text-muted)', flexShrink: 0 }}>
                    {h.created_at ? new Date(h.created_at).toLocaleString() : ''}
                  </span>
                  {resumable(h) && (
                    <button
                      onClick={(e) => { e.stopPropagation(); resume(h.run_id) }}
                      title="Resume from where it stopped"
                      style={{
                        fontSize: 11, fontWeight: 600, padding: '3px 9px', borderRadius: 6,
                        background: 'rgba(167,139,250,0.15)', color: '#a78bfa',
                        border: '1px solid rgba(167,139,250,0.4)', cursor: 'pointer', flexShrink: 0,
                      }}>▶ Resume</button>
                  )}
                  <code style={{ fontSize: 10, color: 'var(--text-muted)', flexShrink: 0 }}>{h.run_id.slice(0, 8)}</code>
                </div>
              )
            })}
          </div>
        </section>
      )}
    </div>
  )
}
