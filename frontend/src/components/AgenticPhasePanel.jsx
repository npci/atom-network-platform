// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useState, useRef, useEffect, useCallback } from 'react'
import { agenticApi } from '../services/api'
import { wsUrl } from '../utils/basePath'
import { safeHref } from '../utils/safeUrl'
import ChangeWalkthrough from './ChangeWalkthrough'
import StuckHelpCard from './StuckHelpCard'
import RunUsageBadge from './RunUsageBadge'

// Reusable agentic-run panel for the complete-flow stages (THE BOOK v3.4 Phase A/B split).
// Phase A (kind='xsd') runs in the XSD stage page and ends at an XSD-approval gate;
// Phase B (kind='code') runs in the Phase-B page, adopts Phase A's workspace, and ends
// at the push-approval gate (one combined MR). Streams the durable agentic_events feed.

const TERMINAL = ['completed', 'failed', 'cancelled', 'gave_up']

const PHASE_LABELS = {
  pending: 'Starting', workspace_ready: 'Workspace', context_ready: 'Context',
  xsd_discovery: 'XSD discovery', awaiting_xsd_approval: 'Awaiting XSD approval',
  awaiting_tsd_approval: 'Awaiting Tech Spec approval',
  code_change: 'Editing code', verification: 'Verifying', review: 'Reviewing',
  awaiting_verify_decision: 'Verification failed — your decision',
  awaiting_code_decision: 'Awaiting your decision',
  awaiting_schema_amendment: 'Awaiting your schema decision',
  awaiting_human_approval: 'Awaiting your approval', pushing: 'Pushing',
  completed: 'Completed', failed: 'Failed', cancelled: 'Cancelled', gave_up: 'Gave up',
}

const KIND_META = {
  run_created:            { icon: '🎬', color: '#a78bfa', label: 'Run created' },
  drive_started:          { icon: '⚙️', color: '#a78bfa', label: 'Worker picked up the run' },
  workspace_start:        { icon: '📦', color: '#38bdf8', label: 'Preparing sandbox' },
  repo_indexing:          { icon: '🧱', color: '#38bdf8', label: 'Indexing sandbox' },
  repo_cloning:           { icon: '📥', color: '#38bdf8', label: 'Creating sandbox + cloning' },
  repo_cloned:            { icon: '🗂',  color: '#38bdf8', label: 'Sandbox ready' },
  repo_recloning:         { icon: '📥', color: '#38bdf8', label: 'Re-cloning Phase-A workspace' },
  workspace_adopted:      { icon: '🔗', color: '#34d399', label: 'Adopted Phase-A workspace' },
  xsd_restored:           { icon: '♻️', color: '#a78bfa', label: 'Restored approved XSDs' },
  workspace_ready:        { icon: '✅', color: '#34d399', label: 'Workspace ready' },
  repo_indexed:           { icon: '🧱', color: '#38bdf8', label: 'Sandbox indexed' },
  java_version_warning:    { icon: '⚠️', color: '#f59e0b', label: 'Java version mismatch' },
  java_version_switch:     { icon: '♻️', color: '#34d399', label: 'Switched build JDK' },
  verify_decision_needed:  { icon: '⛔', color: '#f87171', label: 'Verification failed — your decision' },
  code_decision_needed:   { icon: '❓', color: '#f87171', label: 'The code agent needs a decision' },
  code_decision_answered: { icon: '✅', color: '#34d399', label: 'Decision recorded — resuming' },
  code_decision_loop:     { icon: '🔁', color: '#f87171', label: 'The agent is re-asking a question you already answered' },
  schema_amendment_needed:   { icon: '🧾', color: '#f59e0b', label: 'A schema change needs your approval' },
  schema_amendment_approved: { icon: '✅', color: '#34d399', label: 'Schema amendment applied — resuming' },
  schema_amendment_rejected: { icon: '🚫', color: '#f87171', label: 'Schema amendment rejected — implement around it' },
  schema_amendment_partial:  { icon: '⚠️', color: '#f59e0b', label: 'Schema amendment only partly applied' },
  verify_retry:            { icon: '🔁', color: '#60a5fa', label: 'Retrying verification' },
  verify_skipped:          { icon: '⚠️', color: '#f59e0b', label: 'Verification skipped — proceeding unverified' },
  verifier_degraded:      { icon: '⚠️', color: '#f59e0b', label: 'No local build — CI will verify' },
  reasoning:              { icon: '💭', color: '#a78bfa', label: 'Thinking' },
  loop_capped:            { icon: '⚠️', color: '#f59e0b', label: 'Work-step cap — continuing' },
  paused_transient:       { icon: '⏸', color: '#f59e0b', label: 'Paused (network) — auto-resuming' },
  lease_expired_recovered:{ icon: '♻️', color: '#a78bfa', label: 'Recovered — resuming' },
  resume_requested:       { icon: '▶️', color: '#a78bfa', label: 'Resume requested' },
  run_terminal:           { icon: '🛑', color: '#f87171', label: 'Run ended' },
  context_ready:          { icon: '🧭', color: '#a78bfa', label: 'Context assembled' },
  xsd_scope:              { icon: '📐', color: '#f59e0b', label: 'XSD scope decided' },
  xsd_handoff_ready:      { icon: '📦', color: '#34d399', label: 'XSDs ready for review' },
  approach_proposal:      { icon: '🧭', color: '#f59e0b', label: 'Choose an approach (reuse vs new)' },
  approach_decided:       { icon: '✅', color: '#34d399', label: 'Approach chosen' },
  xsd_changes_requested:  { icon: '📝', color: '#60a5fa', label: 'Applying your requested changes' },
  xsd_enum_occupancy:     { icon: '🔎', color: '#f59e0b', label: 'New schema values checked against the code' },
  needs_contract_amendment: { icon: '⚠️', color: '#f87171', label: 'Code phase needs a schema change Phase A did not make' },
  xsd_change_declined:    { icon: '⚠️', color: '#f59e0b', label: 'Concern recorded on a requested change' },
  plan_supersession_pending: { icon: '📝', color: '#60a5fa', label: 'Approving will also update the ratified plan' },
  plan_supersession_cleared: { icon: 'ℹ️', color: '#a78bfa', label: 'Pending plan update no longer applies — approval covers schemas only' },
  plan_revised:           { icon: '📝', color: '#60a5fa', label: 'Plan updated to a new version' },
  revision_proposal:      { icon: '⚠️', color: '#f87171', label: 'Disruptive change — safer alternatives proposed' },
  revision_chosen:        { icon: '✅', color: '#34d399', label: 'Safer alternative chosen' },
  risk_accepted:          { icon: '⛔', color: '#ef4444', label: 'Risk accepted — proceeding with disruptive change' },
  change_set:             { icon: '📦', color: '#34d399', label: 'Change set produced' },
  verification:           { icon: '✅', color: '#34d399', label: 'Verification' },
  review:                 { icon: '🔍', color: '#60a5fa', label: 'Review' },
  manifest_frozen:        { icon: '🧊', color: '#22d3ee', label: 'Manifest frozen — awaiting approval' },
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
  if (e.kind === 'phase_changed') return { icon: '➡️', color: '#64748b', label: `Phase → ${p.to || ''}`, detail: '' }
  if (e.kind === 'reasoning') return { icon: '💭', color: '#a78bfa', label: 'Thinking', detail: (p.action || '').replace(/^💭\s*/, '') }
  if (e.kind === 'verification') {
    const st = p.status
    const icon = st === 'verified' ? '✅' : st === 'needs_fix' ? '❌' : '⚠️'
    const color = st === 'verified' ? '#34d399' : st === 'needs_fix' ? '#f87171' : '#f59e0b'
    return { icon, color, label: p.action || 'Verification', detail: (p.errors || []).join('\n') }
  }
  if (e.kind === 'run_terminal') {
    const ok = p.status === 'completed'
    return { icon: ok ? '🎉' : '🛑', color: ok ? '#34d399' : '#f87171', label: `Run ${p.status}`, detail: p.error || '' }
  }
  const m = KIND_META[e.kind] || { icon: '•', color: '#94a3b8', label: e.kind }
  return { icon: m.icon, color: m.color, label: p.action || m.label, detail: e.kind === 'llm_turn' ? (p.text || '') : '' }
}

// Friendly, non-technical status headlines for the in-flow (clean) variant.
const PHASE_FRIENDLY = {
  pending: 'Getting ready…',
  workspace_ready: 'Preparing the repository…',
  context_ready: 'Reading your requirement…',
  xsd_discovery: 'Analyzing existing schemas and preparing changes…',
  awaiting_approach_decision: 'Your decision is needed below',
  awaiting_xsd_approval: 'Schema changes ready for your review',
  awaiting_tsd_approval: 'The Tech Spec is not approved yet — approve it, then resume this run',
  code_change: 'Generating code…',
  verification: 'Building and verifying…',
  awaiting_verify_decision: 'Verification keeps failing — your decision is needed below',
  awaiting_code_decision: 'The code agent needs a decision — your input is needed below',
  awaiting_schema_amendment: 'The code agent needs a schema change approved — review it below',
  review: 'Reviewing the changes…',
  awaiting_human_approval: 'Changes ready for your review',
  pushing: 'Publishing to git…',
  completed: 'Completed',
  failed: 'Stopped — needs your attention',
  cancelled: 'Cancelled',
  gave_up: 'Stopped — needs your attention',
}

// File paths touched, parsed from the unified diffs ("diff --git a/X b/X" headers).
function changedPaths(diffs) {
  const out = []
  for (const d of Object.values(diffs || {})) {
    for (const m of String(d || '').matchAll(/^diff --git a\/(.+?) b\//gm)) out.push(m[1])
  }
  return [...new Set(out)]
}

// Per-file change stats from the unified diffs: {path: {adds, dels, isNew}}.
function fileStats(diffs) {
  const out = {}
  for (const d of Object.values(diffs || {})) {
    for (const part of String(d || '').split(/^diff --git /m).slice(1)) {
      const m = part.match(/^a\/(.+?) b\//)
      if (!m) continue
      let adds = 0, dels = 0
      for (const line of part.split('\n')) {
        if (line.startsWith('+') && !line.startsWith('+++')) adds++
        else if (line.startsWith('-') && !line.startsWith('---')) dels++
      }
      out[m[1]] = { adds, dels, isNew: /\nnew file mode /.test(part) }
    }
  }
  return out
}

const _names = (arr, cap = 8) => {
  const a = (arr || []).map(String)
  return a.length <= cap ? a.join(', ') : a.slice(0, cap).join(', ') + ` +${a.length - cap} more`
}

// Shared button styling so every control in the panel reads as a real button
// (the theme's default <button> is near-invisible on the dark surface).
const BTN_BASE = {
  padding: '8px 16px', fontSize: 13, fontWeight: 600, borderRadius: 6,
  cursor: 'pointer', lineHeight: 1.2, border: '1px solid transparent',
}
const BTN_PRIMARY = { ...BTN_BASE, background: 'var(--accent, #2563eb)', color: '#fff' }
const BTN_SECONDARY = { ...BTN_BASE, background: '#334155', color: '#fff' }

// Per-file line-level change info from the unified diff hunks, so the readable schema
// view can mark changes without diff syntax. {path: {added: Set<newLineNo>,
// removed: Map<newLineNo, string[]>}} — removed lines are anchored to the new-file
// line they sat before (lines.length+1 = removed at end of file).
function diffLineInfo(diffs) {
  const map = {}
  for (const d of Object.values(diffs || {})) {
    for (const part of String(d || '').split(/^diff --git /m).slice(1)) {
      const m = part.match(/^a\/(.+?) b\//)
      if (!m) continue
      const info = (map[m[1]] = map[m[1]] || { added: new Set(), removed: new Map() })
      let newLn = 0
      for (const line of part.split('\n')) {
        const h = line.match(/^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@/)
        if (h) { newLn = Math.max(1, parseInt(h[1], 10)); continue }   // +0,0 = delete-only at top
        if (!newLn) continue                                  // preamble before first hunk
        if (line.startsWith('+') && !line.startsWith('+++')) { info.added.add(newLn); newLn++ }
        else if (line.startsWith('-')) {
          if (!info.removed.has(newLn)) info.removed.set(newLn, [])
          info.removed.get(newLn).push(line.slice(1))
        }
        else if (line.startsWith('\\')) { /* no-newline marker */ }
        else { newLn++ }                                      // context line
      }
    }
  }
  return map
}

// Readable XML viewer (NOT a diff): lightly syntax-highlighted schema content on the
// app theme — tags in accent, attribute values in green, comments muted. Lines in
// `highlights` (1-based) get a soft green wash + left bar marking what changed; lines
// in `removed` (anchor → old lines) render struck-through in red where they used to be.
function XmlView({ text, highlights = null, removed = null }) {
  const lines = String(text || '').split('\n')
  const removedRow = (r, key) => (
    <div key={key} style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word',
      background: 'rgba(239,68,68,0.10)', borderLeft: '3px solid #ef4444',
      paddingLeft: 6, marginLeft: -9, color: 'var(--text-muted)',
      textDecoration: 'line-through' }}>{r || ' '}</div>
  )
  const xmlRow = (line, i) => {
    const hl = highlights?.has(i + 1)
    const rowStyle = {
      whiteSpace: 'pre-wrap', wordBreak: 'break-word',
      ...(hl ? { background: 'rgba(34,197,94,0.13)', borderLeft: '3px solid #16a34a',
                 paddingLeft: 6, marginLeft: -9 } : { paddingLeft: 0 }),
    }
    const t = line.trim()
    if (t.startsWith('<!--')) {
      return <div key={i} style={{ ...rowStyle, color: 'var(--text-muted)', fontStyle: 'italic' }}>{line || ' '}</div>
    }
    const segs = line.split(/("[^"]*")/g)
    return (
      <div key={i} style={rowStyle}>
        {segs.map((s, j) => s.startsWith('"')
          ? <span key={j} style={{ color: '#16a34a' }}>{s}</span>
          : s.split(/(<\/?[\w:.-]+|\/?>)/g).map((tk, k) =>
              (/^<\/?[\w:.-]+$/.test(tk) || /^\/?>$/.test(tk))
                ? <span key={`${j}-${k}`} style={{ color: 'var(--accent, #2563eb)' }}>{tk}</span>
                : <span key={`${j}-${k}`} style={{ color: 'var(--text-secondary)' }}>{tk}</span>))}
      </div>
    )
  }
  const rows = []
  lines.forEach((line, i) => {
    for (const [k, r] of (removed?.get(i + 1) || []).entries()) rows.push(removedRow(r, `r${i}-${k}`))
    rows.push(xmlRow(line, i))
  })
  for (const [k, r] of (removed?.get(lines.length + 1) || []).entries()) rows.push(removedRow(r, `rt-${k}`))
  return (
    <pre style={{ background: 'var(--bg-input, rgba(127,127,127,0.05))', border: '1px solid var(--border)',
      borderRadius: 6, padding: 12, overflowX: 'auto', fontSize: 12, lineHeight: 1.55, maxHeight: 440 }}>
      {rows}
    </pre>
  )
}

// Split a repo's unified patch into per-file sub-diffs [{path, body}].
function splitDiffFiles(patch) {
  return String(patch || '').split(/^(?=diff --git )/m).filter(p => p.trim()).map(body => {
    const m = body.match(/^diff --git a\/(.+?) b\//)
    return { path: m ? m[1] : '(file)', body }
  })
}
const isSchemaFile = (p) => /\.(xsd|xjb)$/i.test(p)

// Parse XSD diff into PM-friendly plain-English summary
function analyzeXsdChanges(body) {
  const lines = (body || '').split('\n')
  const added = lines.filter(l => l.startsWith('+') && !l.startsWith('+++') && l.trim().length > 1)
  const removed = lines.filter(l => l.startsWith('-') && !l.startsWith('---') && l.trim().length > 1)

  // Extract structural changes: new/removed elements, types, attributes, restrictions
  const addedElems = added.filter(l => l.includes('xs:element')).map(l => {
    const m = l.match(/name="([^"]+)"/)
    return m ? m[1] : null
  }).filter(Boolean)

  const removedElems = removed.filter(l => l.includes('xs:element')).map(l => {
    const m = l.match(/name="([^"]+)"/)
    return m ? m[1] : null
  }).filter(Boolean)

  const addedTypes = added.filter(l => l.includes('complexType') || l.includes('simpleType')).map(l => {
    const m = l.match(/name="([^"]+)"/)
    return m ? m[1] : null
  }).filter(Boolean)

  const changedComments = added.filter(l => l.includes('<!--') || l.includes('-->')).length > 0

  // Build plain English summary
  const changes = []
  if (addedElems.length > 0) changes.push(`Added ${addedElems.length} new field${addedElems.length > 1 ? 's' : ''}: ${addedElems.slice(0, 3).join(', ')}${addedElems.length > 3 ? ', ...' : ''}`)
  if (removedElems.length > 0) changes.push(`Removed ${removedElems.length} field${removedElems.length > 1 ? 's' : ''}: ${removedElems.slice(0, 3).join(', ')}${removedElems.length > 3 ? ', ...' : ''}`)
  if (addedTypes.length > 0) changes.push(`Added ${addedTypes.length} new data type${addedTypes.length > 1 ? 's' : ''}`)
  if (changedComments) changes.push('Updated documentation/comments')

  return {
    summary: changes.length > 0 ? changes : ['Schema structure modified'],
    addedCount: added.length,
    removedCount: removed.length,
  }
}

// Per-file stats derived from the diff TEXT — the LEGACY fallback, used only when the
// exact stats sidecar (v2 artifact) is absent. A truncation marker in the text means
// these counts are not the real numbers — flag instead of showing a wrong count.
const fileStat = (body) => {
  const lines = String(body || '').split('\n')
  const add = lines.filter(l => l.startsWith('+') && !l.startsWith('+++')).length
  const del = lines.filter(l => l.startsWith('-') && !l.startsWith('---')).length
  const isNew = /^new file mode/m.test(body) || /^--- a\/\/dev\/null/m.test(body) || /^--- \/dev\/null/m.test(body)
  const isDel = /^deleted file mode/m.test(body) || /^\+\+\+ \/dev\/null/m.test(body)
  const partial = /diff is truncated — inspect the branch|diff omitted —|diff truncated — inspect the branch/.test(body)
  return { add, del, partial, type: isNew ? 'New' : isDel ? 'Deleted' : 'Modified' }
}
const typeColor = { New: '#34d399', Deleted: '#f87171', Modified: '#60a5fa' }

// One collapsible file row. The patch body is rendered ONLY while the row is open —
// eagerly mounting every patch would put hundreds of thousands of DOM nodes on the
// page for a big change-set. ``exact`` is this file's entry from the stats sidecar
// (v2 artifact): counts computed server-side from the FULL diff, so they are always
// real — ``truncated`` there means only the stored patch PREVIEW is shortened, never
// the numbers. Without a sidecar (legacy runs), counts come from the text and a
// truncation marker means they can't be trusted.
function FileDiffRow({ f, exact, renderPatch }) {
  const [open, setOpen] = useState(false)
  const local = fileStat(f.body)
  const type = exact ? ({ add: 'New', delete: 'Deleted' }[exact.op] || 'Modified') : local.type
  const add = exact ? exact.add : local.add
  const del = exact ? exact.del : local.del
  const name = f.path.split('/').pop()
  const dir = f.path.slice(0, f.path.length - name.length)
  return (
    <details onToggle={e => setOpen(e.currentTarget.open)}
      style={{ marginBottom: 6, border: '1px solid var(--border-subtle, var(--border))', borderRadius: 6, background: 'rgba(127,127,127,0.04)' }}>
      <summary style={{ cursor: 'pointer', padding: '8px 10px', display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, listStyle: 'none' }}>
        <span style={{ fontSize: 9.5, fontWeight: 700, padding: '2px 6px', borderRadius: 4, flexShrink: 0,
          color: typeColor[type], background: `${typeColor[type]}22` }}>{type.toUpperCase()}</span>
        <span style={{ fontFamily: 'monospace', color: 'var(--text-primary)', fontWeight: 600, wordBreak: 'break-all' }}>{name}</span>
        <span style={{ fontFamily: 'monospace', fontSize: 11, color: 'var(--text-muted)', wordBreak: 'break-all', flex: 1 }}>{dir}· {f.rid.slice(0, 8)}</span>
        <span style={{ flexShrink: 0, fontSize: 11, fontWeight: 700 }}>
          {!exact && local.partial
            ? <span style={{ color: '#f59e0b' }} title="Diff too large to store in full — counts are partial. Inspect the branch for the real numbers.">partial diff ⚠</span>
            : <>
                <span style={{ color: '#16a34a' }}>+{add}</span>{' '}<span style={{ color: '#dc2626' }}>−{del}</span>
                {exact?.truncated && <span style={{ color: '#f59e0b', fontWeight: 400, marginLeft: 6 }}
                  title="Counts are exact; only the stored patch preview is shortened. The pushed branch holds the full file.">preview cut</span>}
              </>}
        </span>
      </summary>
      <div style={{ padding: '0 10px 10px' }}>{open ? renderPatch(f.body) : null}</div>
    </details>
  )
}

// Exported for reuse: the governance stage card renders its fix-delta with the
// same viewer (same {rid: unified-diff-text} + stats shapes from /runs/{id}/diff).
export function DiffBlock({ diffs, stats = null, light = false, kind, xsdFiles = [] }) {
  if (!diffs) return null
  const palette = light
    ? { bg: 'var(--bg-input, rgba(127,127,127,0.06))', border: 'var(--border)', base: 'var(--text-secondary)',
        add: '#16a34a', del: '#dc2626', hunk: '#2563eb', meta: 'var(--text-muted)' }
    : { bg: '#0b1021', border: '#1e293b', base: '#94a3b8',
        add: '#4ade80', del: '#f87171', hunk: '#38bdf8', meta: '#a78bfa' }
  // A giant-file patch (25K-line rewrites store in full, ~50K+ diff lines) must not
  // mount 50K+ DOM nodes on expand: render the first RENDER_LINE_CAP lines and say
  // exactly how much more there is + where to get it (code ZIP carries the full diff).
  const RENDER_LINE_CAP = 20000
  const renderPatch = (body) => {
    const all = (body || '').split('\n')
    const lines = all.length > RENDER_LINE_CAP ? all.slice(0, RENDER_LINE_CAP) : all
    return (
      <pre style={{ background: palette.bg, border: `1px solid ${palette.border}`, borderRadius: 6, color: palette.base,
        padding: 12, overflowX: 'auto', fontSize: 12, lineHeight: 1.5, maxHeight: 480 }}>
        {lines.map((line, i) => {
          const c = line.startsWith('+') && !line.startsWith('+++') ? palette.add
            : line.startsWith('-') && !line.startsWith('---') ? palette.del
            : line.startsWith('@@') ? palette.hunk
            : line.startsWith('diff --git') || line.startsWith('index ') ? palette.meta : palette.base
          return <div key={i} style={{ color: c, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{line || ' '}</div>
        })}
        {all.length > RENDER_LINE_CAP && (
          <div style={{ color: '#f59e0b', marginTop: 8 }}>
            … {(all.length - RENDER_LINE_CAP).toLocaleString()} more lines — download the code ZIP for the full diff, or inspect the branch.
          </div>
        )}
      </pre>
    )
  }

  // Phase B (code) adopts Phase A's branch, so the combined diff mixes the
  // already-approved schema with the code generated this phase. Split them so it's
  // clear what's new vs what was approved at the XSD gate. (Phase A only ever edits
  // schema, Phase B only code, so file type is an accurate divider.)
  if (kind === 'code') {
    const files = []
    for (const [rid, d] of Object.entries(diffs)) for (const f of splitDiffFiles(d)) files.push({ ...f, rid })
    const schema = files.filter(f => isSchemaFile(f.path))
    const code = files.filter(f => !isSchemaFile(f.path))
    // Each FILE is its own collapsible row — collapsed by default so the user sees a
    // clean changed-files list and expands only what they want to read.
    const group = (title, hint, items, openDefault) => items.length === 0 ? null : (
      <details open={openDefault} style={{ marginBottom: 12 }}>
        <summary style={{ cursor: 'pointer', color: 'var(--text-secondary)', fontWeight: 600, fontSize: 13, marginBottom: 4 }}>
          {title} <span style={{ color: 'var(--text-muted)', fontWeight: 400 }}>({items.length})</span>
        </summary>
        <div style={{ color: 'var(--text-muted)', fontSize: 12, margin: '2px 0 8px' }}>{hint}</div>
        {items.map((f) => <FileDiffRow key={`${f.rid}:${f.path}`} f={f} exact={stats?.[f.rid]?.[f.path]} renderPatch={renderPatch} />)}
      </details>
    )
    return (
      <>
        {group('✓ Approved schema — carried from Phase A', 'Reviewed and approved at the XSD gate; shown here for context.', schema, false)}
        {group('New code — generated this phase', 'Written now against the approved schema.', code, true)}
      </>
    )
  }

  // Phase A (kind='xsd'): PM-friendly view — files + plain English changes + download + technical details hidden
  if (kind === 'xsd') {
    return Object.entries(diffs).map(([rid, d]) => {
      const files = splitDiffFiles(d).filter(f => isSchemaFile(f.path))
      if (files.length === 0) return null

      return (
        <div key={rid} style={{ marginBottom: 16 }}>
          <div style={{ color: 'var(--text-secondary)', fontSize: 11, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.5px', marginBottom: 8 }}>
            {files.length} Schema File{files.length > 1 ? 's' : ''} Modified
          </div>

          {files.map((f, i) => {
            const analysis = analyzeXsdChanges(f.body)
            const fileName = f.path.split('/').pop()
            // The full post-change schema (for download) lives in xsdFiles, NOT f.body
            // (which is the unified diff). Match by path so the download is the real .xsd.
            const fullFile = (xsdFiles || []).find(x =>
              x.path === f.path || x.path?.endsWith('/' + f.path) || f.path?.endsWith('/' + x.path))

            return (
              <details key={i} style={{ marginBottom: 10, padding: '12px 14px', borderRadius: 8, border: '1px solid var(--border)', background: 'var(--bg-elevated)', cursor: 'pointer' }}>
                <summary style={{ cursor: 'pointer', display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', fontSize: 13, color: 'var(--text-primary)', fontWeight: 600, userSelect: 'none' }}>
                  <div style={{ flex: 1 }}>
                    <div style={{ marginBottom: 4 }}>{fileName}</div>
                    <div style={{ fontSize: 11, color: 'var(--text-muted)', fontWeight: 400 }}>
                      {f.path.replace(fileName, '')}
                    </div>
                  </div>
                  <div style={{ flexShrink: 0, textAlign: 'right' }}>
                    <span style={{ display: 'inline-block', fontSize: 10, background: 'rgba(76, 175, 80, 0.15)', color: '#4caf50', padding: '2px 6px', borderRadius: 3, marginBottom: 4 }}>
                      +{analysis.addedCount}
                    </span>
                    <span style={{ display: 'inline-block', fontSize: 10, background: 'rgba(244, 67, 54, 0.15)', color: '#f44336', padding: '2px 6px', borderRadius: 3, marginLeft: 4 }}>
                      −{analysis.removedCount}
                    </span>
                  </div>
                </summary>

                <div style={{ marginTop: 12, paddingTop: 12, borderTop: '1px solid var(--border-subtle)' }}>
                  {/* Plain English Summary */}
                  <div style={{ marginBottom: 12 }}>
                    <div style={{ fontSize: 11, fontWeight: 600, color: 'var(--text-muted)', marginBottom: 6, textTransform: 'uppercase', letterSpacing: '0.5px' }}>
                      What Changed
                    </div>
                    <ul style={{ margin: 0, padding: '0 0 0 18px', listStyle: 'disc' }}>
                      {analysis.summary.map((change, j) => (
                        <li key={j} style={{ fontSize: 12, color: 'var(--text-secondary)', marginBottom: 4, lineHeight: 1.5 }}>
                          {change}
                        </li>
                      ))}
                    </ul>
                  </div>

                  {/* Download the FULL updated schema (not the diff). Only shown when the
                      full file content is available. */}
                  {fullFile?.content && (
                    <button
                      onClick={(e) => {
                        e.preventDefault()
                        const link = document.createElement('a')
                        link.href = `data:application/xml;charset=utf-8,${encodeURIComponent(fullFile.content)}`
                        link.download = fileName
                        link.click()
                      }}
                      style={{
                        fontSize: 11, padding: '6px 12px', borderRadius: 4, border: '1px solid var(--accent)',
                        background: 'transparent', color: 'var(--accent)', fontWeight: 600, cursor: 'pointer',
                        transition: 'all 0.2s', marginBottom: 12
                      }}
                      onMouseOver={(e) => { e.target.style.background = 'rgba(218, 119, 86, 0.1)' }}
                      onMouseOut={(e) => { e.target.style.background = 'transparent' }}
                    >
                      ⬇ Download Updated File
                    </button>
                  )}

                  {/* Technical Details (Hidden by Default) */}
                  <details style={{ marginTop: 12 }}>
                    <summary style={{ cursor: 'pointer', fontSize: 11, color: 'var(--text-muted)', fontWeight: 500, padding: '6px 0' }}>
                      Technical Details (Git Diff)
                    </summary>
                    <div style={{ marginTop: 8 }}>
                      {renderPatch(f.body)}
                    </div>
                  </details>
                </div>
              </details>
            )
          })}
        </div>
      )
    }).filter(Boolean)
  }

  // File-wise (kind='files'): a flat list of per-file rows, each its own
  // collapsible (collapsed by default) with a type badge + exact +/- counts —
  // the same FileDiffRow the code view uses, without the schema/code split.
  // Governance fix-deltas render this way so a human scans the changed files
  // and expands only the one they want to read.
  if (kind === 'files') {
    const files = []
    for (const [rid, d] of Object.entries(diffs)) for (const f of splitDiffFiles(d)) files.push({ ...f, rid })
    if (!files.length) return null
    return files.map((f) => (
      <FileDiffRow key={`${f.rid}:${f.path}`} f={f} exact={stats?.[f.rid]?.[f.path]} renderPatch={renderPatch} />
    ))
  }

  // Phase B (kind='code'): already handled above with schema/code split
  return Object.entries(diffs).map(([rid, d]) => (
    <details key={rid} open style={{ marginBottom: 10 }}>
      <summary style={{ cursor: 'pointer', color: 'var(--text-secondary)', fontFamily: 'monospace', fontSize: 12, marginBottom: 4 }}>
        repo {rid.slice(0, 8)}
      </summary>
      {renderPatch(d)}
    </details>
  ))
}

// Per-module build outcome from the verification gate: a collapsible Built /
// Failed / Skipped breakdown. ``modules`` is { name: { status, errors } }.
const MODULE_STATUS = {
  built:   { icon: '✅', color: '#34d399', label: 'Built' },
  failed:  { icon: '❌', color: '#f87171', label: 'Failed' },
  skipped: { icon: '⏭', color: '#94a3b8', label: 'Skipped' },
}
function ModuleReport({ modules, footer }) {
  const entries = Object.entries(modules || {})
  if (!entries.length && !footer) return null
  const groups = { failed: [], built: [], skipped: [] }
  for (const [name, m] of entries) (groups[m?.status] || groups.skipped).push([name, m])
  const counts = `${groups.built.length} built · ${groups.failed.length} failed · ${groups.skipped.length} skipped`
  // Collapsed by default (even on failure) so the build outcome isn't highlighted; the
  // verification-decision gate (footer), when present, lives inside this same collapsible.
  return (
    <details style={{ marginBottom: 8, padding: '8px 12px', borderRadius: 8,
      border: '1px solid var(--border)', background: 'rgba(100,116,139,0.06)' }}>
      <summary style={{ cursor: 'pointer', fontSize: 13, fontWeight: 600, color: 'var(--text-secondary)' }}>
        🧩 Module build report — {counts}
      </summary>
      <div style={{ marginTop: 8 }}>
        {['failed', 'built', 'skipped'].flatMap(st => groups[st].map(([name, m]) => {
          const meta = MODULE_STATUS[st] || MODULE_STATUS.skipped
          const errs = (m?.errors || []).filter(Boolean)
          return (
            <div key={name} style={{ padding: '4px 0', borderBottom: '1px solid var(--border-subtle, var(--border))' }}>
              <div style={{ display: 'flex', gap: 8, alignItems: 'baseline', fontSize: 12.5 }}>
                <span style={{ flexShrink: 0 }}>{meta.icon}</span>
                <code style={{ color: 'var(--text-primary)', wordBreak: 'break-all' }}>{name}</code>
                <span style={{ marginLeft: 'auto', flexShrink: 0, fontSize: 10, fontWeight: 700,
                  color: meta.color }}>{meta.label}</span>
              </div>
              {st === 'failed' && errs.length > 0 && (
                <ul style={{ margin: '2px 0 4px 26px', padding: 0, listStyle: 'disc', color: '#fca5a5', fontSize: 11.5 }}>
                  {errs.slice(0, 5).map((e, i) => <li key={i} style={{ wordBreak: 'break-word' }}>{e}</li>)}
                </ul>
              )}
            </div>
          )
        }))}
        {footer}
      </div>
    </details>
  )
}

/**
 * @param {object}   props
 * @param {string}   props.changeId
 * @param {'xsd'|'code'} props.kind
 * @param {string[]} props.repoIds   selected repo ids to start a fresh run
 * @param {string}   props.intent    plain-language change intent
 * @param {Function} [props.onApproved] called after a successful approval (Phase A: advance stage)
 * @param {'console'|'clean'} [props.variant] 'clean' = in-flow look: friendly one-line
 *   status ticker, technical log tucked behind an expander, schemas-changing summary,
 *   theme-styled diff (no terminal styling). Default 'console' (admin console look).
 */
export default function AgenticPhasePanel({ changeId, kind, repoIds = [], intent = '', onApproved, variant = 'console', startBlockedReason = '' }) {
  const [run, setRun] = useState(null)
  const [events, setEvents] = useState([])
  const [diffs, setDiffs] = useState(null)
  const [diffStats, setDiffStats] = useState(null)   // exact per-file counts sidecar (v2 artifact)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [expanded, setExpanded] = useState(() => new Set())
  const [selOpt, setSelOpt] = useState(null)        // chosen approach option id
  const [custom, setCustom] = useState('')          // free-text "exactly what I need"
  const [feedback, setFeedback] = useState('')      // XSD-gate refine request
  const [xsdFiles, setXsdFiles] = useState(null)    // [{path, content}] — readable schema view
  const [confirmApprove, setConfirmApprove] = useState(false)
  const [reverifying, setReverifying] = useState(false)  // on-demand re-verify in flight
  const [zipBusy, setZipBusy] = useState(false)     // workspace-ZIP download in flight
  const [health, setHealth] = useState(null)        // ops snapshot (admin console only)
  const wsRef = useRef(null)
  const logRef = useRef(null)
  // Operator health: surface stuck runs / low workspace disk on the admin console so an
  // operator notices subsystem trouble without DB spelunking. Console variant only.
  useEffect(() => {
    if (variant === 'clean') return
    let alive = true
    let t
    // /agentic/health is admin-only. If this variant is ever rendered for a non-admin,
    // stop polling after the first 403 instead of hammering it every 30s.
    const load = () => agenticApi.health()
      .then(({ data }) => { if (alive) setHealth(data) })
      .catch((e) => { if (e?.response?.status === 403 && t) clearInterval(t) })
    load(); t = setInterval(load, 30000)
    return () => { alive = false; clearInterval(t) }
  }, [variant])
  const healthBanner = health && (health.stuck_runs > 0 || health.workspace_disk_low) ? (
    <div style={{ padding: '8px 12px', marginBottom: 8, borderRadius: 8, fontSize: 12,
      background: 'rgba(245,158,11,0.10)', border: '1px solid rgba(245,158,11,0.45)', color: '#b45309' }}>
      ⚠ Agentic ops:
      {health.stuck_runs > 0 ? ` ${health.stuck_runs} stuck run(s)` : ''}
      {health.stuck_runs > 0 && health.workspace_disk_low ? ' ·' : ''}
      {health.workspace_disk_low ? ` workspace disk low (${health.workspace_disk_free_mb} MB free)` : ''}
    </div>
  ) : null
  const toggle = (key) => setExpanded(s => { const n = new Set(s); n.has(key) ? n.delete(key) : n.add(key); return n })

  const awaitingPhase = kind === 'xsd' ? 'awaiting_xsd_approval' : 'awaiting_human_approval'

  useEffect(() => { if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight }, [events])
  useEffect(() => () => wsRef.current?.close(), [])

  // Re-derive the parent's "XSD approved" signal from the RUN itself — not only the
  // in-session approve click. A Phase-A (kind=xsd) run that is `completed` IS approved,
  // so on a reload/revisit the parent still gets onApproved() and can show "Complete
  // Stage". Without this, refreshing after approval strands the change at the XSD stage.
  const approvedFired = useRef(false)
  useEffect(() => {
    if (kind === 'xsd' && run?.status === 'completed' && !approvedFired.current) {
      approvedFired.current = true
      onApproved?.()
    }
  }, [kind, run?.status, onApproved])

  const openStream = useCallback((runId) => {
    wsRef.current?.close()
    const ws = new WebSocket(wsUrl(`api/ws/agentic/runs/${runId}`))
    wsRef.current = ws
    // Auth in the FIRST FRAME, never the URL — a query-string token lands in
    // nginx access logs, proxy logs and browser history. Frame bodies do not.
    // `after_seq: -1` replays the full history, as the query-param path did.
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
          try { const r = (await agenticApi.getDiff(runId)).data; setDiffs(r.diffs); setDiffStats(r.stats || null) } catch { /* noop */ }
        }
      } else if (msg.type === 'end') {
        agenticApi.getRun(runId).then(r => setRun(r.data)).catch(() => {})
        // Load the durable changes artifact once the run finishes (push commits the tree).
        try { const r = (await agenticApi.getDiff(runId)).data; setDiffs(r.diffs); setDiffStats(r.stats || null) } catch { /* noop */ }
      } else if (msg.type === 'error') {
        setError(msg.detail)
      }
    }
    ws.onerror = () => setError('WebSocket connection error — check network and retry.')
    ws.onclose = (e) => { if (e.code !== 1000) setError('WebSocket disconnected unexpectedly.') }
  }, [])

  // On mount / change: restore the latest run of this kind for the change and stream it.
  useEffect(() => {
    let cancelled = false
    if (!changeId) return
    agenticApi.listChangeRuns(changeId, kind).then(async ({ data }) => {
      const latest = (data.runs || [])[0]
      if (cancelled || !latest) return
      setRun(latest)
      // Always load the changes artifact (stored on the manifest → inspectable forever).
      try { const r = (await agenticApi.getDiff(latest.run_id)).data; setDiffs(r.diffs); setDiffStats(r.stats || null) } catch { /* noop */ }
      openStream(latest.run_id)
    }).catch(() => { /* no runs yet */ })
    return () => { cancelled = true }
  }, [changeId, kind, awaitingPhase, openStream])

  const start = async () => {
    setError(null); setBusy(true); setEvents([]); setDiffs(null); setDiffStats(null); setXsdFiles(null); setRun(null); setExpanded(new Set())
    try {
      const { data } = await agenticApi.start(changeId, { repo_ids: repoIds, intent, kind })
      setRun(data)
      openStream(data.run_id)
    } catch (e) {
      setError(e?.response?.data?.detail || e.message)
    } finally { setBusy(false) }
  }

  const cancel = async () => { if (run) { try { await agenticApi.cancel(run.run_id) } catch { /* noop */ } } }

  // Discard the current run (hard-cancelled if parked at a gate) and start fresh.
  const retry = async () => {
    if (!run || busy) return
    if (!window.confirm(
      '⚠ Start over?\n\nThis will cancel the current run — including any in-progress push — '
      + 'and discard its progress. This cannot be undone.'
    )) return
    setError(null); setBusy(true)
    try {
      if (!TERMINAL.includes(run.status)) await agenticApi.cancel(run.run_id)
      wsRef.current?.close()
      setRun(null); setEvents([]); setDiffs(null); setDiffStats(null); setXsdFiles(null); setExpanded(new Set())
      setSelOpt(null); setCustom(''); setFeedback('')
      let { data } = await agenticApi.start(changeId, { repo_ids: repoIds, intent, kind })
      if (data.created === false) {
        // Plain cancel above didn't clear the block (e.g. the old run was wedged with a
        // dead lease and no worker left to honour the cooperative flag) — force it, then
        // retry the start once more so "Start over" still works instead of dead-ending.
        await agenticApi.cancel(data.run_id, true)
        ;({ data } = await agenticApi.start(changeId, { repo_ids: repoIds, intent, kind }))
        if (data.created === false) { setError('Previous run is still finishing — try again in a moment.'); setRun(data); return }
      }
      setRun(data)
      openStream(data.run_id)
    } catch (e) { setError(e?.response?.data?.detail || e.message) } finally { setBusy(false) }
  }

  // Re-run Phase B code-gen as a FRESH run from the APPROVED Phase-A baseline — clean workspace
  // (pinned base + approved XSDs, no carried edits, nothing cached) so you can test whether the
  // agent produces a consistent result. The server cancels the prior code run but keeps its result.
  const rerunCode = async () => {
    if (busy) return
    if (!window.confirm('Re-run code generation from the approved Phase-A baseline?\n\nThis starts a FRESH attempt on a clean workspace (the current code run is discarded) so you can compare outputs across runs.')) return
    setError(null); setBusy(true)
    try {
      wsRef.current?.close()
      setRun(null); setEvents([]); setDiffs(null); setDiffStats(null); setXsdFiles(null); setExpanded(new Set())
      const { data } = await agenticApi.rerunCode(changeId, intent)
      setRun(data); openStream(data.run_id)
    } catch (e) { setError(e?.response?.data?.detail || e.message) } finally { setBusy(false) }
  }

  const approve = async (pushNow = true, overrideBlockers = false) => {
    if (!run?.manifest_hash) return
    // XSD approval NEVER pushes (approve-xsd retains the workspace for Phase B), so skip
    // the "confirm push" double-click — only the code-phase push needs that guard.
    if (kind !== 'xsd' && pushNow && !confirmApprove) { setConfirmApprove(true); return }
    setConfirmApprove(false)
    setError(null)
    try {
      if (kind === 'xsd') {
        await agenticApi.approveXsd(run.run_id, run.manifest_hash)
        setRun(r => ({ ...r, status: 'completed', phase: 'completed' }))
      } else {
        // Blocker override requires a compliance reason (recorded as a durable audit event).
        let overrideReason = null
        if (overrideBlockers) {
          overrideReason = window.prompt(
            "⚠ You're pushing despite an unresolved blocker-severity finding. " +
            "This will be recorded for compliance review.\n\nReason (required, ≥8 chars):", "")
          if (!overrideReason || overrideReason.trim().length < 8) { setBusy?.(false); return }
        }
        await agenticApi.approve(run.run_id, run.manifest_hash, pushNow, overrideBlockers, overrideReason)
        setRun(r => pushNow ? { ...r, status: 'pushing' }
          : { ...r, status: 'completed', phase: 'completed', push_deferred: true })
      }
      onApproved?.()
    } catch (e) { setError(e?.response?.data?.detail || e.message) }
  }

  // Deferred half of "Approve — push later": push the approved branch now.
  const pushNow = async (overrideBlockers = false) => {
    if (!run) return
    setError(null)
    let overrideReason = null
    if (overrideBlockers) {
      overrideReason = window.prompt(
        "⚠ You're pushing despite an unresolved blocker-severity finding. " +
        "This will be recorded for compliance review.\n\nReason (required, ≥8 chars):", "")
      if (!overrideReason || overrideReason.trim().length < 8) return
    }
    try {
      await agenticApi.pushRun(run.run_id, overrideBlockers, overrideReason)
      setRun(r => ({ ...r, status: 'active', phase: 'pushing', push_deferred: false }))
      openStream(run.run_id)
    } catch (e) { setError(e?.response?.data?.detail || e.message) }
  }

  const resume = async () => {
    if (!run) return
    setError(null)
    try { await agenticApi.resume(run.run_id); openStream(run.run_id) }
    catch (e) { setError(e?.response?.data?.detail || e.message) }
  }

  // Download the generated code (every selected repo's working tree, e.g. network + network-2.0)
  // as a ZIP — so a developer can inspect the agent's changes locally BEFORE the push.
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

  // Verification gate (after 3 failed builds): retry once more, or skip + proceed unverified.
  const decideVerify = async (action) => {
    if (!run || busy) return                 // guard the duplicate-click window
    setError(null); setBusy(true)
    try {
      const { data } = await agenticApi.decideVerify(run.run_id, action)
      setRun(r => ({ ...r, ...data }))       // optimistic: phase moves off the gate immediately
      openStream(run.run_id)
    } catch (e) { setError(e?.response?.data?.detail || e.message) }
    finally { setBusy(false) }
  }
  // Re-verify an already-generated/approved change: rebuild it and show pass/fail,
  // without re-running the whole pipeline. The build runs in the worker; for a
  // terminal (completed) run the event WS is closed, so poll the run for the
  // last_reverify result, then refresh the event list for the per-module report.
  const reverify = async () => {
    if (!run || reverifying) return
    setError(null); setReverifying(true)
    try {
      await agenticApi.reverify(run.run_id)
      const deadline = Date.now() + 32 * 60 * 1000   // > build timeout (1800s) + margin
      while (Date.now() < deadline) {
        await new Promise(r => setTimeout(r, 2500))
        let data
        try { data = (await agenticApi.getRun(run.run_id)).data } catch { continue }
        setRun(r => ({ ...r, ...data }))
        if (data.last_reverify && data.last_reverify.status !== 'running') {
          try {
            const ev = (await agenticApi.getEvents(run.run_id)).data?.events || []
            setEvents(ev.map(e => ({ type: 'event', ...e })))
          } catch { /* keep existing events */ }
          break
        }
      }
    } catch (e) { setError(e?.response?.data?.detail || e.message) }
    finally { setReverifying(false) }
  }
  const atVerifyGate = run?.phase === 'awaiting_verify_decision'

  const running = run && !TERMINAL.includes(run.status)
  const awaiting = run?.phase === awaitingPhase
  const resumable = run && run.status !== 'completed' && run.phase !== awaitingPhase && !running
  const approveLabel = kind === 'xsd' ? 'Approve schema → continue to TSD' : 'Approve & push (XSD + code)'
  // Pre-push code download: the generated working trees (network + network-2.0 + …) as a ZIP.
  const zipButton = kind !== 'xsd' && run?.run_id ? (
    <button onClick={downloadZip} disabled={zipBusy}
      title="Download the code generated in this run (all selected repos) as a ZIP — inspect the agent's changes locally before pushing"
      style={{ padding: '8px 16px', fontWeight: 600, background: 'transparent', color: '#60a5fa',
        border: '1px solid #60a5fa', borderRadius: 6, cursor: zipBusy ? 'wait' : 'pointer', flexShrink: 0 }}>
      {zipBusy ? 'Preparing ZIP…' : '⬇ Download code (ZIP)'}
    </button>
  ) : null

  const phaseEvents = events.filter(e => e.kind === 'phase_changed')
  const curPhase = run ? (TERMINAL.includes(run.status) ? run.status
    : run.phase || (phaseEvents.length ? phaseEvents[phaseEvents.length - 1].payload?.to : 'pending')) : null
  // VERIFIED is earned, not assumed: reaching review/approval is NOT proof the build
  // passed — it can also be reached by SKIPPING verification at the gate. So the badge
  // requires an actual 'verified' verification outcome; a skipped/failed verify shows
  // an explicit UNVERIFIED badge instead, so an unbuilt change never reads as compiling.
  // Prefer the DURABLE run flags (_run_view exposes verified / verify_skipped from handoff)
  // so the badge survives reload / REST-only views; fall back to event replay for old runs.
  const lastVerifyEvt = [...events].reverse().find(e => e.kind === 'verification' || e.kind === 'verify_skipped')
  const verifySkipped = run?.verify_skipped === true || lastVerifyEvt?.kind === 'verify_skipped'
  const verifyPassed  = (run?.verified === true
    || (lastVerifyEvt?.kind === 'verification' && lastVerifyEvt.payload?.status === 'verified')) && !verifySkipped
  const atReviewPlus  = ['review', 'awaiting_human_approval', 'rebase_reverify', 'pushing', 'completed'].includes(curPhase)
  const verifiedBadge   = kind !== 'xsd' && atReviewPlus && verifyPassed
  const unverifiedBadge = kind !== 'xsd' && atReviewPlus && !verifyPassed
  // On-demand re-verify is offered exactly at the phases the backend accepts it
  // (change generated, no drive loop mid-flight on the tree) — notably NOT the
  // transient 'pushing' phase, where the build tree is being committed.
  const canReverify = kind !== 'xsd' && ['review', 'awaiting_verify_decision',
    'awaiting_human_approval', 'rebase_reverify', 'completed'].includes(curPhase)
  const editingLive = !!running && !['awaiting_approach_decision', 'awaiting_xsd_approval',
    'awaiting_human_approval', 'awaiting_code_decision', 'awaiting_schema_amendment',
    'pushing', 'rebase_reverify'].includes(run?.phase)
  const lastActive = [...events].reverse().find(e => e.kind !== 'phase_changed')
  const curAction = lastActive ? eventView(lastActive).label : 'starting…'
  // Git guardrail: the per-change feature branch the push targets (shown at approval),
  // and the branch+commit+MR it actually landed on (shown after push).
  const frozenBranch = [...events].reverse().find(e => e.kind === 'manifest_frozen')?.payload?.branch
  const pushTargets = [...events].reverse().find(e => e.kind === 'completed')?.payload?.targets || []
  // Latest verification result → per-module Built / Failed / Skipped breakdown.
  const lastVerification = [...events].reverse().find(e => e.kind === 'verification')?.payload
  const moduleResults = lastVerification?.modules || null
  // The change-set artifact is present whenever any repo has a real diff (not a placeholder).
  const hasDiffs = diffs && Object.values(diffs).some(d => d && !String(d).trim().startsWith('('))
  // Gates: reuse-first approach options OR a disruptive-revision conversation. The
  // LATEST gate event decides which card renders at awaiting_approach_decision.
  const gateEvent = [...events].reverse().find(e => e.kind === 'approach_proposal' || e.kind === 'revision_proposal')
  const proposal = gateEvent?.payload
  const atGate = run?.phase === 'awaiting_approach_decision'
  const deciding = atGate && gateEvent?.kind === 'approach_proposal'
  const revising = atGate && gateEvent?.kind === 'revision_proposal'
  // A3 gate: the code agent surfaced a decision it must not make itself (directive-vs-code
  // conflict, or a missing critical decision). The event payload IS the decision request
  // (question/blocked_item/options) — durable on `agentic_events`, so this renders correctly
  // even for a run that reached this gate before the UI existed to answer it.
  const codeDecisionEvent = [...events].reverse().find(e => e.kind === 'code_decision_needed')
  const atCodeDecisionGate = run?.phase === 'awaiting_code_decision'
  // Fix 2 — the code phase staged a change to the human-approved schema. The event payload
  // carries the exact before/after plus the platform's provenance verdict (was this text
  // added by Phase A during THIS change, or is it pre-existing production contract?).
  const schemaAmendEvent = [...events].reverse().find(e => e.kind === 'schema_amendment_needed')
  const atSchemaAmendGate = run?.phase === 'awaiting_schema_amendment'
  // A partial apply re-parks the run on the proposals that did NOT land (review finding 1).
  // When that is the newer event, the gate must render the REMAINDER — showing the original
  // list again would invite re-approving edits that are already on disk.
  const schemaAmendPartial = (() => {
    const iNeed = events.map(e => e.kind).lastIndexOf('schema_amendment_needed')
    const iPart = events.map(e => e.kind).lastIndexOf('schema_amendment_partial')
    return iPart > iNeed ? events[iPart]?.payload : null
  })()
  const schemaAmendments = schemaAmendEvent?.payload?.amendments || []
  // ADR-0005 / SDLC review gap 4 — TSD approval gate (shadow/enforce controlled by
  // agentic_tsd_approval_gate_enforce). The TSD itself is approved/regenerated on the
  // Tech Spec page, not here — this is just the "try again now" resume control.
  const atTsdApprovalGate = run?.phase === 'awaiting_tsd_approval'
  const codeDecisionOptions = codeDecisionEvent?.payload?.options || []
  // xsd_change_declined carries two modes: a genuine decline (payload.declined set) vs a
  // comply-first objection where the change WAS applied (declined:null) — split them so an
  // applied request is never shown to the PM under a "declined" banner.
  const concernEvents = events.filter(e => e.kind === 'xsd_change_declined').map(e => e.payload || {})
  const declines = concernEvents.filter(p => p.declined)
  const objections = concernEvents.filter(p => !p.declined)
  // Approving the XSDs also approves a pending PLAN update — surface it as its own banner.
  // The backend treats the approval as informed consent to it, so it must not sit only in the
  // collapsed technical timeline, where the fallback renderer clips it to one truncated line.
  // A later round can retract the pending update (files reverted / re-sanctioned by the
  // plan) — the newest of pending/cleared wins, so the PM is never told their approval
  // rolls a plan update the backend will not apply.
  const supersessionEvt = [...events].reverse().find(e =>
    e.kind === 'plan_supersession_pending' || e.kind === 'plan_supersession_cleared')
  const supersession = supersessionEvt?.kind === 'plan_supersession_pending' ? supersessionEvt.payload : null
  // ⛔ permanent danger flag: a disruptive change the human explicitly accepted.
  const acceptedRisk = !!run?.accepted_risk || events.some(e => e.kind === 'risk_accepted')
  // 🧠 LLM in-flight indicator: the agent loop emits `llm_call_started` JUST before each LLM
  // call. While that's the newest event on the stream, the agent is waiting on the model — NOT
  // hung. The banner replaces the silence so a slow model call (Anthropic side, network, retry)
  // stops looking identical to a stuck agent. Auto-hides the moment the next tool_call /
  // llm_turn / loop_done lands (those events have a higher seq, so this lookup returns undefined).
  const llmInFlight = (() => {
    if (!running) return null
    const last = events[events.length - 1]
    return last?.kind === 'llm_call_started' ? last : null
  })()
  // Wall-clock tick (1 Hz) so the in-flight banner's elapsed counter actually moves; we only
  // schedule the interval while a call is in flight to avoid useless re-renders the rest of the time.
  const [, setNowTick] = useState(0)
  useEffect(() => {
    if (!llmInFlight) return
    const id = setInterval(() => setNowTick(t => t + 1), 1000)
    return () => clearInterval(id)
  }, [llmInFlight])
  // An unresolved blocker-severity review finding: the agent spent its extra fix rounds and a
  // blocker is still open. The push is held; shipping it needs a deliberate override.
  const openBlocker = kind === 'code'
    ? ([...events].reverse().find(e => e.kind === 'review_blocked')?.payload || null) : null
  const blkItems = openBlocker?.items || []

  useEffect(() => {  // default the radio to the agent's recommended option
    if (atGate && proposal && selOpt === null) setSelOpt(proposal.recommended || proposal.options?.[0]?.id || null)
  }, [atGate, proposal, selOpt])

  // Readable schema content (frozen at the Phase-A handoff) for the PM view.
  useEffect(() => {
    if (variant !== 'clean' || kind !== 'xsd' || !run?.run_id || xsdFiles !== null || !hasDiffs) return
    let cancelled = false
    agenticApi.getXsdFiles(run.run_id)
      .then(({ data }) => { if (!cancelled) setXsdFiles(data.files || []) })
      .catch(() => { if (!cancelled) setXsdFiles([]) })
    return () => { cancelled = true }
  }, [variant, kind, run?.run_id, xsdFiles, hasDiffs])

  const decide = async () => {
    if (!run) return
    setError(null)
    const opt = (proposal?.options || []).find(o => o.id === selOpt)
    try {
      await agenticApi.decideApproach(run.run_id, {
        selected_option_id: selOpt || undefined, custom_direction: custom.trim() || undefined, option: opt })
      setRun(r => ({ ...r, phase: 'xsd_discovery', status: 'active' }))
      setCustom('')
    } catch (e) { setError(e?.response?.data?.detail || e.message) }
  }

  // A3 gate answer: a picked option, free-text, or both — the backend accepts either
  // (chosen_option_id resolves to the option's label server-side if answer is blank).
  // ask_decision only guarantees options are dicts, NOT that they carry an `id` — an
  // id-less option is submitted by its label as the free-text answer instead.
  const codeOptId = (o, i) => String(o.id ?? `opt-${i}`)
  // The option `selOpt` currently points at, but ONLY if it's a real, submittable option
  // of THIS gate (selOpt is shared state; a stale value from another gate must not count).
  // An option is submittable if it has an id, or a non-empty label/title to send as text.
  const codeSelOpt = codeDecisionOptions.find((o, i) => codeOptId(o, i) === selOpt) || null
  const codeOptText = (o) => String((o && (o.label ?? o.title)) || '').trim()
  const codeSubmittable = !!custom.trim() || !!(codeSelOpt && (codeSelOpt.id != null || codeOptText(codeSelOpt)))
  const decideCode = async () => {
    if (!run || busy || !codeSubmittable) return
    const answer = custom.trim()
    setError(null); setBusy(true)
    try {
      await agenticApi.decideCodeDecision(run.run_id, {
        answer: answer || (codeSelOpt && codeSelOpt.id == null ? codeOptText(codeSelOpt) : ''),
        chosen_option_id: codeSelOpt && codeSelOpt.id != null ? String(codeSelOpt.id) : undefined,
      })
      setRun(r => ({ ...r, phase: 'code_change', status: 'active' }))
      setCustom(''); setSelOpt(null)
      openStream(run.run_id)
    } catch (e) { setError(e?.response?.data?.detail || e.message) } finally { setBusy(false) }
  }

  // Fix 2 — the code phase needs a change to the approved schema and cannot make it itself.
  // Approving applies the staged hunk VERBATIM (no model redoes the edit) and resumes code
  // generation; rejecting records a binding directive to implement around it, which is what
  // stops the agent from simply re-proposing the same edit next round.
  const decideSchemaAmendment = async (approve) => {
    if (!run || busy) return
    if (!approve && !custom.trim()) {
      setError('A reason is required when rejecting — the agent uses it to decide what to do instead.')
      return
    }
    setError(null); setBusy(true)
    try {
      await agenticApi.decideSchemaAmendment(run.run_id, { approve, reason: custom.trim() || undefined })
      setRun(r => ({ ...r, phase: 'code_change', status: 'active' }))
      setCustom('')
      openStream(run.run_id)
    } catch (e) {
      setError(e?.response?.data?.detail || e.message)
      // A 409 means the approval did not fully land and the run is STILL at this gate on the
      // unapplied remainder. Re-stream rather than leaving the panel on stale state, so the
      // partial-failure banner and the re-staged proposals render.
      if (e?.response?.status === 409) openStream(run.run_id)
    } finally { setBusy(false) }
  }

  // ADR-0005 / SDLC review gap 4 — re-checks the TSD's status and resumes if it's now
  // APPROVED (e.g. after the user approves/regenerates it on the Tech Spec page in
  // another tab). A 409 means it's still not approved — surfaced as the error banner.
  const decideTsdApproval = async () => {
    if (!run || busy) return
    setError(null); setBusy(true)
    try {
      await agenticApi.decideTsdApproval(run.run_id)
      setRun(r => ({ ...r, phase: 'code_change', status: 'active' }))
      openStream(run.run_id)
    } catch (e) { setError(e?.response?.data?.detail || e.message) } finally { setBusy(false) }
  }

  const retryBtn = (canRetry) => canRetry && (
    <button onClick={retry} title="Discard this run and start a fresh one"
      style={{ flexShrink: 0, padding: '6px 12px', fontSize: 12, fontWeight: 600, cursor: 'pointer',
        background: 'transparent', color: 'var(--text-secondary)', border: '1px solid var(--border)', borderRadius: 6 }}>
      ↺ Start over
    </button>
  )

  // Phase-B only: re-run code-gen from the approved Phase-A baseline (clean workspace) to test
  // consistency. Shown when there IS a code run that's done or parked (nothing actively driving).
  const rerunCodeBtn = (kind === 'code' && run && !running) && (
    <button onClick={rerunCode} disabled={busy}
      title="Re-run code generation from the approved Phase-A baseline on a clean workspace (no cache) — to compare outputs across attempts"
      style={{ flexShrink: 0, padding: '6px 12px', fontSize: 12, fontWeight: 600, cursor: busy ? 'not-allowed' : 'pointer',
        background: 'transparent', color: 'var(--accent, #2563eb)', border: '1px solid var(--accent, #2563eb)',
        borderRadius: 6, opacity: busy ? 0.6 : 1 }}>
      🔄 Re-run code-gen
    </button>
  )

  // The verification-gate controls — shown after the build report when the run has
  // parked at awaiting_verify_decision (3 auto-retries spent).
  const verifyGate = atVerifyGate && (
    <section style={{ marginTop: 12, padding: '14px 16px', borderRadius: 8,
      background: 'rgba(245,158,11,0.08)', border: '1px solid rgba(245,158,11,0.4)' }}>
      <div style={{ fontWeight: 700, fontSize: 13, color: '#f59e0b' }}>⛔ Verification failed 3 times</div>
      <div style={{ fontSize: 12.5, color: 'var(--text-secondary)', margin: '6px 0 12px', lineHeight: 1.6 }}>
        The build didn’t pass after 3 automatic attempts. You can try once more, or skip
        verification and proceed — the change will be marked <strong>UNVERIFIED</strong> and must
        be verified another way (e.g. CI) before release.
      </div>
      <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
        <button onClick={() => decideVerify('retry')} disabled={busy}
          style={{ padding: '8px 16px', fontWeight: 600, fontSize: 12.5, cursor: busy ? 'not-allowed' : 'pointer',
            background: 'var(--accent, #2563eb)', color: '#fff', border: 'none', borderRadius: 6, opacity: busy ? 0.6 : 1 }}>
          🔁 Try verification once more
        </button>
        <button onClick={() => decideVerify('skip')} disabled={busy}
          style={{ padding: '8px 16px', fontWeight: 600, fontSize: 12.5, cursor: busy ? 'not-allowed' : 'pointer',
            background: 'transparent', color: '#f59e0b', border: '1px solid #f59e0b', borderRadius: 6, opacity: busy ? 0.6 : 1 }}>
          ⏭ Skip verification & proceed (unverified)
        </button>
      </div>
    </section>
  )

  const decideRev = async (proceedAnyway) => {
    if (!run) return
    setError(null)
    const opt = (proposal?.options || []).find(o => o.id === selOpt)
    try {
      await agenticApi.decideRevision(run.run_id, proceedAnyway
        ? { proceed_anyway: true }
        : { selected_option_id: selOpt || undefined, custom_direction: custom.trim() || undefined, option: opt })
      setRun(r => ({ ...r, phase: 'xsd_discovery', status: 'active', accepted_risk: r.accepted_risk || proceedAnyway }))
      setCustom(''); setDiffs(null); setDiffStats(null); setXsdFiles(null)
    } catch (e) { setError(e?.response?.data?.detail || e.message) }
  }

  const requestChanges = async () => {
    if (!run || !feedback.trim()) return
    setError(null)
    try {
      await agenticApi.requestXsdChanges(run.run_id, feedback.trim())
      setFeedback(''); setDiffs(null); setDiffStats(null); setXsdFiles(null)
      setRun(r => ({ ...r, phase: 'xsd_discovery', status: 'active' }))
    } catch (e) { setError(e?.response?.data?.detail || e.message) }
  }

  return (
    <div>
      {!run && (() => {
        const blocked = busy || !intent.trim() || repoIds.length === 0 || !!startBlockedReason
        return (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8, alignItems: 'flex-start' }}>
            <button onClick={start} disabled={blocked}
              style={{ ...BTN_PRIMARY, opacity: blocked ? 0.55 : 1, cursor: blocked ? 'not-allowed' : 'pointer' }}>
              {busy ? 'Starting…' : kind === 'xsd' ? 'Generate XSDs (Phase A)' : 'Generate code (Phase B)'}
            </button>
          </div>
        )
      })()}
      {!run && (startBlockedReason || repoIds.length === 0) && (
        <p style={{ color: '#b45309', fontSize: 13, marginTop: 8 }}>
          {startBlockedReason || 'Select at least one repository to run the agent.'}
        </p>
      )}
      {running && <button onClick={cancel} style={{ ...BTN_SECONDARY, marginLeft: 8 }}>Cancel</button>}
      {error && <div style={{ color: '#b91c1c', margin: '10px 0' }}>⚠ {error}</div>}

      {run && variant === 'clean' && (
        <section style={{ marginTop: 12 }}>
          {/* Friendly status — one headline + one changing update line, normal typography. */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '12px 14px',
            borderRadius: 8, background: 'var(--bg-input, rgba(127,127,127,0.05))', border: '1px solid var(--border)' }}>
            {running && <span style={{ width: 14, height: 14, borderRadius: '50%', flexShrink: 0,
              border: '2px solid var(--accent, #2563eb)', borderTopColor: 'transparent', animation: 'spin 0.8s linear infinite' }} />}
            {!running && <span style={{ flexShrink: 0 }}>{run.status === 'completed' ? '✅' : awaiting || atGate ? '🟠' : '⏸'}</span>}
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontWeight: 600, fontSize: 14, color: 'var(--text-primary)' }}>
                {PHASE_FRIENDLY[curPhase] || PHASE_LABELS[curPhase] || curPhase}
              </div>
              {running && (
                <div style={{ fontSize: 12.5, color: 'var(--text-muted)', marginTop: 2,
                  whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{curAction}</div>
              )}
            </div>
            {run?.run_id && <RunUsageBadge runId={run.run_id} active={!!running} />}
            {rerunCodeBtn}
            {retryBtn(!!running || ['failed', 'gave_up', 'cancelled', 'completed'].includes(run.status))}
          </div>

          {['failed', 'gave_up', 'cancelled'].includes(run.status) && (
            <div style={{ padding: '12px 14px', marginTop: 8, borderRadius: 8,
              background: 'rgba(220,38,38,0.07)', border: '1px solid rgba(220,38,38,0.3)' }}>
              <div style={{ fontWeight: 600, color: 'var(--danger, #dc2626)', fontSize: 13 }}>
                The run stopped{run.error ? ` — ${String(run.error).slice(0, 180)}` : '.'}
              </div>
              <div>
                <StuckHelpCard runId={run.run_id} onApplied={async () => {
                  // The recovery action resurrected/redirected the run on the backend — refetch
                  // so the stale 'Run failed' UI (banner, Resume button, Failed phase badge)
                  // updates to the new state instead of sitting there next to the success notice.
                  // Also clear any stale error from a prior action so the warning strip drops.
                  setError(null)
                  try { const { data } = await agenticApi.getRun(run.run_id); setRun(data) } catch { /* noop */ }
                  onApproved?.()
                }} />
              </div>
              {resumable && <button onClick={resume} style={{ marginTop: 8, padding: '7px 16px', fontWeight: 600,
                background: 'var(--accent, #2563eb)', color: '#fff', border: 'none', borderRadius: 6, cursor: 'pointer' }}>
                Resume from where it stopped</button>}
            </div>
          )}

          {(moduleResults || verifyGate) && <div style={{ marginTop: 8 }}><ModuleReport modules={moduleResults} footer={verifyGate} /></div>}

          {/* Technical activity — tucked away; for power users only. */}
          <details style={{ marginTop: 8 }}>
            <summary style={{ fontSize: 12, color: 'var(--text-muted)', cursor: 'pointer' }}>
              Show technical activity ({events.length})
            </summary>
            <div style={{ marginTop: 6, maxHeight: '38vh', overflowY: 'auto', borderRadius: 8,
              border: '1px solid var(--border)', padding: '8px 12px', fontSize: 12.5 }}>
              {events.filter(e => !['llm_call_started','llm_usage'].includes(e.kind)).map((e, i) => {
                const v = eventView(e)
                const t = e.ts ? new Date(e.ts).toLocaleTimeString() : ''
                return (
                  <div key={e.seq ?? i} style={{ display: 'flex', gap: 8, alignItems: 'baseline',
                    padding: '3px 0', borderBottom: '1px solid var(--border-subtle, var(--border))' }}>
                    <span style={{ flexShrink: 0 }}>{v.icon}</span>
                    <span style={{ color: 'var(--text-secondary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{v.label}</span>
                    <span style={{ marginLeft: 'auto', color: 'var(--text-muted)', fontSize: 10, flexShrink: 0 }}>{t}</span>
                  </div>
                )
              })}
            </div>
          </details>
        </section>
      )}

      {run && variant !== 'clean' && (
        <section style={{ marginTop: 12 }}>
          {healthBanner}
          <div style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '10px 14px', marginBottom: 8,
            borderRadius: 8, background: running ? 'rgba(56,189,248,0.08)' : 'rgba(100,116,139,0.08)',
            border: '1px solid ' + (running ? 'rgba(56,189,248,0.3)' : 'var(--border)') }}>
            {running && <span style={{ width: 12, height: 12, borderRadius: '50%', flexShrink: 0,
              border: '2px solid #38bdf8', borderTopColor: 'transparent', animation: 'spin 0.8s linear infinite' }} />}
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: 11, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>Phase</div>
              <div style={{ fontWeight: 700, fontSize: 15, color: 'var(--text-primary)' }}>{PHASE_LABELS[curPhase] || curPhase}</div>
            </div>
            <div style={{ flex: 2, minWidth: 0 }}>
              <div style={{ fontSize: 11, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>{running ? 'Now' : 'Last'}</div>
              <div style={{ fontSize: 13, color: 'var(--text-secondary)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{curAction}</div>
            </div>
            {rerunCodeBtn}
            {retryBtn(!!running || ['failed', 'gave_up', 'cancelled', 'completed'].includes(run.status))}
            <code style={{ fontSize: 10, color: 'var(--text-muted)', flexShrink: 0 }}>{run.run_id?.slice(0, 8)}</code>
          </div>

          {['failed', 'gave_up', 'cancelled'].includes(run.status) && (
            <div style={{ padding: '12px 14px', marginBottom: 8, borderRadius: 8,
              background: 'rgba(248,113,113,0.10)', border: '1px solid rgba(248,113,113,0.4)' }}>
              <div style={{ fontWeight: 700, color: '#fca5a5' }}>⛔ Run {run.status} — here's why:</div>
              <div style={{ color: '#fecaca', fontSize: 13, marginTop: 4, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                {run.error || 'No reason recorded. Check the activity log below.'}
              </div>
              {resumable && <button onClick={resume} style={{ marginTop: 10, padding: '7px 16px', fontWeight: 600,
                background: '#a78bfa', color: '#1e1b2e', border: 'none', borderRadius: 6, cursor: 'pointer' }}>
                ▶ Resume from where it stopped</button>}
              <StuckHelpCard runId={run.run_id} onApplied={async () => {
                  // The recovery action resurrected/redirected the run on the backend — refetch
                  // so the stale 'Run failed' UI (banner, Resume button, Failed phase badge)
                  // updates to the new state instead of sitting there next to the success notice.
                  // Also clear any stale error from a prior action so the warning strip drops.
                  setError(null)
                  try { const { data } = await agenticApi.getRun(run.run_id); setRun(data) } catch { /* noop */ }
                  onApproved?.()
                }} />
            </div>
          )}

          {llmInFlight && (() => {
            // Time the model has been thinking. Style escalates with elapsed so a normal call
            // looks calm but a stall stands out: gray <60s · amber <5min · red ≥5min.
            const startedAt = Number(llmInFlight.payload?.started_at) || (Date.now() / 1000)
            const elapsedSec = Math.max(0, Math.round(Date.now() / 1000 - startedAt))
            const mm = String(Math.floor(elapsedSec / 60)).padStart(1, '0')
            const ss = String(elapsedSec % 60).padStart(2, '0')
            const tone = elapsedSec >= 300 ? { fg: '#ef4444', bg: 'rgba(239,68,68,0.10)', bd: 'rgba(239,68,68,0.45)', icon: '⚠' }
                       : elapsedSec >=  60 ? { fg: '#d97706', bg: 'rgba(245,158,11,0.10)', bd: 'rgba(245,158,11,0.45)', icon: '⏳' }
                                           : { fg: '#60a5fa', bg: 'rgba(96,165,250,0.08)', bd: 'rgba(96,165,250,0.35)', icon: '🧠' }
            const iter = llmInFlight.payload?.iteration
            const stall = elapsedSec >= 300 ? ' — looks slow; the SDK auto-retries internally. Cancel only if it stays here past ~12 minutes.'
                       : elapsedSec >=  60 ? ' — model is taking a while; the agent is NOT stuck.'
                                           : ''
            return (
              <div style={{ marginBottom: 8, padding: '8px 12px', borderRadius: 6, fontSize: 12.5,
                background: tone.bg, border: `1px solid ${tone.bd}`, color: tone.fg, display: 'flex',
                alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                <span style={{ fontWeight: 700 }}>{tone.icon} Awaiting model response</span>
                <code style={{ fontFamily: 'ui-monospace, monospace', fontSize: 12 }}>{mm}:{ss}</code>
                {iter != null && <span style={{ color: 'var(--text-muted)', fontSize: 11.5 }}>iter {iter}</span>}
                <span style={{ color: 'var(--text-muted)', fontSize: 12 }}>{stall}</span>
              </div>
            )
          })()}
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8,
            flexWrap: 'wrap', marginBottom: 4 }}>
            <div style={{ fontSize: 11, color: '#64748b', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
              Code-gen activity · click a row to expand
            </div>
            {run?.run_id && <RunUsageBadge runId={run.run_id} active={!!running} />}
          </div>
          <div ref={logRef} style={{ background: '#0b1021', fontFamily: 'ui-monospace, monospace', fontSize: 12.5,
            padding: '10px 14px', borderRadius: 8, height: '48vh', minHeight: 320, overflowY: 'auto',
            border: '1px solid #1e293b', resize: 'vertical' }}>
            {events.length === 0 && <div style={{ color: '#64748b', padding: '8px 0' }}>waiting for activity…</div>}
            {events.filter(e => !['llm_call_started','llm_usage'].includes(e.kind)).map((e, i) => {
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
                    <span style={{ width: 14, textAlign: 'center', flexShrink: 0, color: '#475569' }}>{hasDetail ? (isOpen ? '▾' : '▸') : ''}</span>
                    <span style={{ width: 18, textAlign: 'center', flexShrink: 0 }}>{v.icon}</span>
                    <span style={{ color: v.color, fontWeight: 600, flexShrink: 0 }}>{v.label}</span>
                    {hasDetail && !isOpen && <span style={{ color: '#64748b', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{preview}…</span>}
                    <span style={{ marginLeft: 'auto', color: '#475569', fontSize: 10, flexShrink: 0, paddingLeft: 8 }}>{t}</span>
                  </div>
                  {hasDetail && isOpen && (
                    <pre style={{ margin: '6px 0 2px 40px', padding: '8px 10px', background: '#060912',
                      border: '1px solid #1e293b', borderRadius: 6, whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                      maxHeight: 420, overflowY: 'auto' }}>
                      {v.detail.split('\n').map((line, li) => {
                        const c = (line.startsWith('+')) ? '#4ade80' : (line.startsWith('-')) ? '#f87171'
                          : line.startsWith('@@') ? '#38bdf8' : '#cbd5e1'
                        return <div key={li} style={{ color: c }}>{line || ' '}</div>
                      })}
                    </pre>
                  )}
                </div>
              )
            })}
          </div>
          {(moduleResults || verifyGate) && <ModuleReport modules={moduleResults} footer={verifyGate} />}
        </section>
      )}

      {/* Gate parked but the proposal didn't arrive (lost/delayed event, or empty options) —
          never leave the user staring at a gate with nothing to click. */}
      {atGate && (!proposal || !(proposal.options || []).length) && (
        <section style={{ marginTop: 16, padding: '14px 16px', borderRadius: 8,
          background: 'rgba(245,158,11,0.06)', border: '1px solid rgba(245,158,11,0.35)' }}>
          <strong style={{ fontSize: 15 }}>🧭 Waiting for the agent's options…</strong>
          <p style={{ color: 'var(--text-secondary)', fontSize: 13, marginTop: 6 }}>
            The run is paused for your decision but the options haven't loaded
            {proposal && !(proposal.options || []).length ? ' (the agent returned none)' : ''}.
            They usually appear within a moment — if this persists, start over to re-run the analysis.
          </p>
          <div style={{ marginTop: 8 }}>{retryBtn(true)}</div>
        </section>
      )}

      {/* Reuse-first decision gate: choose how to accommodate the requirement BEFORE any schema is created. */}
      {deciding && proposal && (proposal.options || []).length > 0 && (
        <section style={{ marginTop: 16, padding: '14px 16px', borderRadius: 8,
          background: 'rgba(245,158,11,0.06)', border: '1px solid rgba(245,158,11,0.35)' }}>
          <strong style={{ fontSize: 15 }}>🧭 How should we accommodate this?</strong>
          {proposal.summary && <p style={{ color: 'var(--text-secondary)', fontSize: 13, marginTop: 4 }}>{proposal.summary}</p>}
          <p style={{ color: 'var(--text-muted)', fontSize: 12 }}>
            The agent analysed the existing flows. Pick an approach — the recommended one is pre-selected — or describe exactly what you need.
          </p>
          {[...(proposal.options || [])]
            .sort((a, b) => (b.id === proposal.recommended) - (a.id === proposal.recommended))
            .map(o => {
            const rec = o.id === proposal.recommended
            const hasDetail = Boolean(o.target_api || o.how_it_fits || o.tradeoffs)
            return (
              <div key={o.id} style={{ margin: '8px 0', padding: '10px 12px', borderRadius: 6,
                background: selOpt === o.id ? 'rgba(96,165,250,0.10)' : 'transparent',
                border: '1px solid ' + (selOpt === o.id ? 'rgba(96,165,250,0.5)' : 'var(--border)') }}>
                <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer', flexWrap: 'wrap' }}>
                  <input type="radio" name="approach" checked={selOpt === o.id} onChange={() => setSelOpt(o.id)} />
                  <span style={{ fontWeight: 700 }}>{o.title || o.id}</span>
                  <span style={{ fontSize: 10, fontWeight: 700, padding: '1px 6px', borderRadius: 10,
                    background: 'rgba(148,163,184,0.15)', color: '#94a3b8' }}>{o.approach}</span>
                  {rec && <span style={{ fontSize: 10, fontWeight: 700, padding: '1px 6px', borderRadius: 10,
                    background: 'rgba(52,211,153,0.15)', color: '#16a34a', border: '1px solid rgba(52,211,153,0.4)' }}>RECOMMENDED</span>}
                  {o.diverges_from_plan && <span title="This option differs from what the ratified analysis plan recommended"
                    style={{ fontSize: 10, fontWeight: 700, padding: '1px 6px', borderRadius: 10,
                    background: 'rgba(245,158,11,0.15)', color: '#d97706', border: '1px solid rgba(245,158,11,0.5)' }}>⚠ DIVERGES FROM PLAN</span>}
                </label>
                {o.diverges_from_plan && o.divergence_note && (
                  <div style={{ fontSize: 12, color: '#d97706', marginTop: 6, marginLeft: 26,
                    paddingLeft: 8, borderLeft: '2px solid rgba(245,158,11,0.5)' }}>
                    <strong>Why this differs from the plan:</strong> {o.divergence_note}
                  </div>
                )}
                {hasDetail && (
                  <details style={{ marginTop: 4, marginLeft: 26 }}>
                    <summary style={{ fontSize: 11.5, color: 'var(--text-muted)', cursor: 'pointer' }}>Details</summary>
                    {o.target_api && <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 4 }}>Fits into: <code style={{ color: '#60a5fa' }}>{o.target_api}</code></div>}
                    {o.how_it_fits && <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 2 }}>{o.how_it_fits}</div>}
                    {o.tradeoffs && <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 2 }}>Tradeoffs: {o.tradeoffs}</div>}
                  </details>
                )}
              </div>
            )
          })}
          <textarea value={custom} onChange={e => setCustom(e.target.value)} rows={2}
            placeholder="…or describe exactly what you need (overrides the selected option)"
            style={{ width: '100%', marginTop: 6, padding: 8, borderRadius: 6, border: '1px solid var(--border)',
              background: 'var(--bg-input, #0b1021)', color: 'var(--text-primary)', fontSize: 13 }} />
          <button onClick={decide} disabled={!selOpt && !custom.trim()}
            style={{ marginTop: 8, padding: '8px 16px', fontWeight: 600, background: '#2563eb',
              color: '#fff', border: 'none', borderRadius: 6, cursor: 'pointer' }}>Proceed with this approach</button>
        </section>
      )}

      {/* Disruptive-revision conversation: the requested change would break things — the
          agent explains why, offers safer alternatives, and the human decides (or accepts the risk). */}
      {revising && proposal && (
        <section style={{ marginTop: 16, padding: '14px 16px', borderRadius: 8,
          background: 'rgba(239,68,68,0.06)', border: '1px solid rgba(239,68,68,0.4)' }}>
          <strong style={{ fontSize: 15, color: '#f87171' }}>⚠ Your requested change is disruptive</strong>
          {proposal.original_request && (
            <div style={{ fontSize: 12.5, color: 'var(--text-muted)', marginTop: 6 }}>
              You asked: <em>“{proposal.original_request}”</em>
            </div>
          )}
          {proposal.summary && <p style={{ color: 'var(--text-secondary)', fontSize: 13, marginTop: 6 }}>{proposal.summary}</p>}
          <p style={{ color: 'var(--text-muted)', fontSize: 12 }}>
            Safer ways to achieve this (recommended pre-selected) — or proceed with your original request and accept the risk.
          </p>
          {[...(proposal.options || [])]
            .sort((a, b) => (b.id === proposal.recommended) - (a.id === proposal.recommended))
            .map(o => {
            const hasDetail = Boolean(o.how_it_fits || o.tradeoffs)
            return (
              <div key={o.id} style={{ margin: '8px 0', padding: '10px 12px', borderRadius: 6,
                background: selOpt === o.id ? 'rgba(96,165,250,0.10)' : 'transparent',
                border: '1px solid ' + (selOpt === o.id ? 'rgba(96,165,250,0.5)' : 'var(--border)') }}>
                <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer' }}>
                  <input type="radio" name="revision" checked={selOpt === o.id} onChange={() => setSelOpt(o.id)} />
                  <span style={{ fontWeight: 700 }}>{o.title || o.id}</span>
                  {o.id === proposal.recommended && <span style={{ fontSize: 10, fontWeight: 700, padding: '1px 6px', borderRadius: 10,
                    background: 'rgba(52,211,153,0.15)', color: '#16a34a', border: '1px solid rgba(52,211,153,0.4)' }}>RECOMMENDED</span>}
                </label>
                {hasDetail && (
                  <details style={{ marginTop: 4, marginLeft: 26 }}>
                    <summary style={{ fontSize: 11.5, color: 'var(--text-muted)', cursor: 'pointer' }}>Details</summary>
                    {o.how_it_fits && <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 4 }}>{o.how_it_fits}</div>}
                    {o.tradeoffs && <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 2 }}>Tradeoffs: {o.tradeoffs}</div>}
                  </details>
                )}
              </div>
            )
          })}
          <textarea value={custom} onChange={e => setCustom(e.target.value)} rows={2}
            placeholder="…or describe a different way you'd like it done"
            style={{ width: '100%', marginTop: 6, padding: 8, borderRadius: 6, border: '1px solid var(--border)',
              background: 'var(--bg-input, #0b1021)', color: 'var(--text-primary)', fontSize: 13 }} />
          <div style={{ display: 'flex', gap: 10, alignItems: 'center', marginTop: 10, flexWrap: 'wrap' }}>
            <button onClick={() => decideRev(false)} disabled={!selOpt && !custom.trim()}
              style={{ padding: '8px 16px', fontWeight: 600, background: '#2563eb',
                color: '#fff', border: 'none', borderRadius: 6, cursor: 'pointer' }}>Apply the selected alternative</button>
            <button onClick={() => decideRev(true)}
              style={{ padding: '8px 16px', fontWeight: 700, background: 'transparent', color: '#ef4444',
                border: '1.5px solid #ef4444', borderRadius: 6, cursor: 'pointer' }}>
              ⛔ Proceed with my original request anyway — I accept the risk</button>
          </div>
          <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 6 }}>
            Proceeding anyway is recorded permanently and this change will be marked as DANGER everywhere it is inspected.
          </div>
        </section>
      )}

      {/* A3 gate: the code agent needs a decision it must not make itself (e.g. a binding
          directive conflicts with what the code/schema actually provide). The agent isn't
          required to offer options — always show a free-text box in addition to any options. */}
      {atCodeDecisionGate && (
        <section style={{ marginTop: 16, padding: '14px 16px', borderRadius: 8,
          background: 'rgba(239,68,68,0.06)', border: '1px solid rgba(239,68,68,0.4)' }}>
          <strong style={{ fontSize: 15, color: '#f87171' }}>❓ The code agent needs your decision</strong>
          {codeDecisionEvent?.payload?.blocked_item && (
            <div style={{ fontSize: 12.5, color: 'var(--text-muted)', marginTop: 6 }}>
              Blocked on: <code>{codeDecisionEvent.payload.blocked_item}</code>
            </div>
          )}
          <p style={{ color: 'var(--text-secondary)', fontSize: 13, marginTop: 6, lineHeight: 1.6 }}>
            {codeDecisionEvent?.payload?.question || 'The agent needs a decision to continue — see the activity log above for context.'}
          </p>
          {codeDecisionOptions.length > 0 && codeDecisionOptions.map((o, i) => {
            const oid = codeOptId(o, i)
            return (
              <div key={oid} style={{ margin: '8px 0', padding: '10px 12px', borderRadius: 6,
                background: selOpt === oid ? 'rgba(96,165,250,0.10)' : 'transparent',
                border: '1px solid ' + (selOpt === oid ? 'rgba(96,165,250,0.5)' : 'var(--border)') }}>
                <label style={{ display: 'flex', alignItems: 'flex-start', gap: 8, cursor: 'pointer' }}>
                  <input type="radio" name="code-decision" checked={selOpt === oid} onChange={() => setSelOpt(oid)} style={{ marginTop: 3 }} />
                  <span>
                    <span style={{ fontWeight: 700 }}>{o.label || o.title || oid}</span>
                    {o.detail && <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 2 }}>{o.detail}</div>}
                  </span>
                </label>
              </div>
            )
          })}
          <textarea value={custom} onChange={e => setCustom(e.target.value)} rows={3}
            placeholder={codeDecisionOptions.length > 0
              ? '…or describe exactly what you want instead (overrides the selected option)'
              : 'Type your decision (required — no options were offered for this one)'}
            style={{ width: '100%', marginTop: 6, padding: 8, borderRadius: 6, border: '1px solid var(--border)',
              background: 'var(--bg-input, #0b1021)', color: 'var(--text-primary)', fontSize: 13 }} />
          <button onClick={decideCode} disabled={busy || !codeSubmittable}
            style={{ marginTop: 8, padding: '8px 16px', fontWeight: 600, fontSize: 12.5,
              cursor: (busy || !codeSubmittable) ? 'not-allowed' : 'pointer',
              background: '#2563eb', color: '#fff', border: 'none', borderRadius: 6,
              opacity: (busy || !codeSubmittable) ? 0.6 : 1 }}>
            Submit decision & resume code generation
          </button>
        </section>
      )}

      {/* Fix 2 — schema amendment gate. The code phase found the approved schema wrong on the
          wire and staged the exact edit rather than being refused outright (the old behaviour,
          which deadlocked: the reviewer kept demanding a fix the agent could not make). The
          `origin` verdict matters most — amending text Phase A added an hour ago is a very
          different decision from changing an interface other systems already speak. */}
      {atSchemaAmendGate && (
        <section style={{ marginTop: 16, padding: '14px 16px', borderRadius: 8,
          background: 'rgba(245,158,11,0.06)', border: '1px solid rgba(245,158,11,0.45)' }}>
          <strong style={{ fontSize: 15, color: '#f59e0b' }}>🧾 The code agent needs a schema change</strong>
          <p style={{ color: 'var(--text-secondary)', fontSize: 13, marginTop: 6, lineHeight: 1.6 }}>
            The schema was approved in Phase A and the code phase cannot change it on its own.
            It has staged the exact edit below. Approving applies it <strong>verbatim</strong> and
            resumes code generation; rejecting keeps the schema as-is and tells the agent to
            implement around it.
          </p>
          {schemaAmendPartial && (
            <div style={{ margin: '10px 0', padding: '10px 12px', borderRadius: 6,
              background: 'rgba(248,113,113,0.08)', border: '1px solid rgba(248,113,113,0.5)' }}>
              <div style={{ fontSize: 12.5, fontWeight: 700, color: '#f87171' }}>
                ⚠ A previous approval could not be fully applied
              </div>
              <p style={{ fontSize: 12.5, color: 'var(--text-secondary)', marginTop: 6, lineHeight: 1.6 }}>
                {(schemaAmendPartial.applied || []).length} edit(s) landed on disk;{' '}
                {(schemaAmendPartial.failed || []).length} could not be applied because the file
                changed after the proposal was staged, so the text to replace no longer matched.
                Code generation was <strong>not</strong> resumed — the schema would not have
                matched what the agent was told. Re-review the remaining proposal(s) below
                against the current file contents.
              </p>
              {(schemaAmendPartial.failed || []).map((f, i) => (
                <div key={i} style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 4 }}>
                  <code>{f.path}</code>{f.reason ? ` — ${f.reason}` : null}
                </div>
              ))}
            </div>
          )}
          {schemaAmendments.map((a, i) => (
            <div key={i} style={{ margin: '10px 0', padding: '10px 12px', borderRadius: 6,
              background: 'var(--bg-input, #0b1021)', border: '1px solid var(--border)' }}>
              <div style={{ fontSize: 12.5, fontWeight: 700 }}>
                {a.path}{a.line ? <span style={{ color: 'var(--text-muted)', fontWeight: 400 }}>:{a.line}</span> : null}
              </div>
              {a.origin && (
                <div style={{ fontSize: 12, marginTop: 4,
                  color: a.origin === 'baseline' ? '#f87171' : a.origin === 'unknown' ? '#f59e0b' : '#34d399' }}>
                  {a.origin === 'phase_a' ? '✓ Added by Phase A during this same change'
                    : a.origin === 'baseline' ? '⚠ Pre-existing in the approved baseline'
                    : a.origin === 'new_file' ? '＋ Would create a new schema file'
                    : '? Provenance could not be determined'}
                  {a.origin_note && (
                    <div style={{ color: 'var(--text-muted)', marginTop: 2, lineHeight: 1.5 }}>{a.origin_note}</div>
                  )}
                </div>
              )}
              {a.applicable === false && (
                <div style={{ fontSize: 12, color: '#f87171', marginTop: 4 }}>
                  ⚠ The text to replace is no longer present on disk — approving will report this
                  one as not applied.
                </div>
              )}
              {a.kind !== 'create' && (
                <pre style={{ margin: '8px 0 0', padding: 8, borderRadius: 4, overflowX: 'auto',
                  fontSize: 11.5, lineHeight: 1.5, background: 'rgba(0,0,0,0.25)' }}>
                  <div style={{ color: '#f87171' }}>− {a.old_string}</div>
                  <div style={{ color: '#34d399' }}>+ {a.new_string}</div>
                </pre>
              )}
              {a.context && (
                <details style={{ marginTop: 6 }}>
                  <summary style={{ cursor: 'pointer', fontSize: 12, color: 'var(--text-muted)' }}>
                    Surrounding lines
                  </summary>
                  <pre style={{ margin: '6px 0 0', padding: 8, borderRadius: 4, overflowX: 'auto',
                    fontSize: 11.5, lineHeight: 1.5, background: 'rgba(0,0,0,0.25)',
                    color: 'var(--text-secondary)' }}>{a.context}</pre>
                </details>
              )}
            </div>
          ))}
          <textarea value={custom} onChange={e => setCustom(e.target.value)} rows={2}
            placeholder="Reason (required to reject — the agent is told what to do instead; optional when approving)"
            style={{ width: '100%', marginTop: 6, padding: 8, borderRadius: 6, border: '1px solid var(--border)',
              background: 'var(--bg-input, #0b1021)', color: 'var(--text-primary)', fontSize: 13 }} />
          <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
            <button onClick={() => decideSchemaAmendment(true)} disabled={busy}
              style={{ padding: '8px 16px', fontWeight: 600, fontSize: 12.5,
                cursor: busy ? 'not-allowed' : 'pointer', background: '#2563eb', color: '#fff',
                border: 'none', borderRadius: 6, opacity: busy ? 0.6 : 1 }}>
              Approve & apply to the schema
            </button>
            <button onClick={() => decideSchemaAmendment(false)} disabled={busy}
              style={{ padding: '8px 16px', fontWeight: 600, fontSize: 12.5,
                cursor: busy ? 'not-allowed' : 'pointer', background: 'transparent',
                color: '#f87171', border: '1px solid rgba(248,113,113,0.5)', borderRadius: 6,
                opacity: busy ? 0.6 : 1 }}>
              Reject — implement around it
            </button>
          </div>
        </section>
      )}

      {/* ADR-0005 / SDLC review gap 4 — TSD approval gate. Approving/regenerating the TSD
          happens on the Tech Spec page (which auto-approves on generate by default —
          agentic_tsd_auto_approve_on_generate); this is just the "check again" resume
          control for a run that's already parked here. */}
      {atTsdApprovalGate && (
        <section style={{ marginTop: 16, padding: '14px 16px', borderRadius: 8,
          background: 'rgba(245,158,11,0.06)', border: '1px solid rgba(245,158,11,0.4)' }}>
          <strong style={{ fontSize: 15, color: '#f59e0b' }}>❓ Tech Spec approval needed</strong>
          <p style={{ color: 'var(--text-secondary)', fontSize: 13, marginTop: 6, lineHeight: 1.6 }}>
            This run needs the change's Tech Spec to be approved before it generates code.
            Approve or regenerate it on the Tech Spec page, then click below to resume.
          </p>
          <button onClick={decideTsdApproval} disabled={busy}
            style={{ marginTop: 4, padding: '8px 16px', fontWeight: 600, fontSize: 12.5,
              cursor: busy ? 'not-allowed' : 'pointer',
              background: '#2563eb', color: '#fff', border: 'none', borderRadius: 6,
              opacity: busy ? 0.6 : 1 }}>
            Check again & resume
          </button>
        </section>
      )}

      {/* XSD gate CTA — a prominent "approve → TSD" action placed ABOVE the (often tall)
          schema-diff section. The only XSD→TSD advance control used to live at the very
          bottom of the panel, below the diffs and below the fold, so it was easy to miss
          and the dev-only "Skip step" header button got clicked instead — silently skipping
          XSD approval and the TSD. This top banner makes the real action impossible to miss;
          the full approve + "request changes" controls still render below for review-first use. */}
      {/* Approving the manifest ALSO approves the pending plan update — the backend rolls the
          plan version on that consent, so the delta is stated here in full, directly above the
          approve button, rather than only as a clipped line in the collapsed activity list. */}
      {awaiting && kind === 'xsd' && supersession && (
        <section style={{ marginTop: 16, padding: '14px 16px', borderRadius: 8,
          background: 'rgba(96,165,250,0.10)', border: '1px solid rgba(96,165,250,0.5)' }}>
          <div style={{ fontWeight: 700, fontSize: 13.5, color: '#60a5fa' }}>
            📝 Approving this also updates the ratified plan
          </div>
          <div style={{ fontSize: 12.5, color: 'var(--text-secondary)', marginTop: 4, whiteSpace: 'pre-wrap' }}>
            {supersession.action}
          </div>
          {(supersession.new_files || []).length > 0 && (
            <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 6 }}>
              New schema: {(supersession.new_files || []).map((f, i) => (
                <code key={i} style={{ marginRight: 6 }}>{String(f).split('/').pop()}</code>
              ))}
            </div>
          )}
        </section>
      )}

      {awaiting && kind === 'xsd' && (
        <section style={{ marginTop: 16, padding: '14px 16px', borderRadius: 8,
          background: 'rgba(22,163,74,0.08)', border: '1px solid rgba(22,163,74,0.45)',
          display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
          <div style={{ flex: 1, minWidth: 220 }}>
            <div style={{ fontWeight: 700, fontSize: 14, color: '#16a34a' }}>
              Schema changes ready — approve to continue to the Tech Spec
            </div>
            <div style={{ fontSize: 12.5, color: 'var(--text-secondary)', marginTop: 2 }}>
              Review the schema changes below, then approve to advance to the TSD.
            </div>
          </div>
          <button onClick={() => approve(true)}
            style={{ padding: '9px 18px', fontWeight: 700, fontSize: 13.5, color: '#fff', border: 'none',
              borderRadius: 6, cursor: 'pointer', background: '#16a34a', flexShrink: 0 }}>
            {approveLabel}
          </button>
        </section>
      )}

      {/* Persistent CHANGES ARTIFACT — the git-diff of every change, inspectable during
          the run AND forever after (stored on the manifest; survives push + workspace GC). */}
      {hasDiffs && (
        <section style={{ marginTop: 16 }}>
          {declines.length > 0 && (
            <div style={{ marginBottom: 10, padding: '10px 12px', borderRadius: 6,
              background: 'rgba(245,158,11,0.08)', border: '1px solid rgba(245,158,11,0.4)' }}>
              <strong style={{ color: '#f59e0b', fontSize: 13 }}>⚠ Some requested changes were declined as disruptive:</strong>
              {declines.map((d, i) => (
                <div key={i} style={{ fontSize: 12.5, color: 'var(--text-secondary)', marginTop: 4 }}>
                  • <code>{d.declined}</code> — {(d.action || '').replace(/^⚠\s*/, '')}
                </div>
              ))}
            </div>
          )}
          {objections.length > 0 && (
            <div style={{ marginBottom: 10, padding: '10px 12px', borderRadius: 6,
              background: 'rgba(96,165,250,0.08)', border: '1px solid rgba(96,165,250,0.35)' }}>
              <strong style={{ color: '#60a5fa', fontSize: 13 }}>ℹ Applied as you asked — the agent noted these objections for the record:</strong>
              {objections.map((d, i) => (
                <div key={i} style={{ fontSize: 12.5, color: 'var(--text-secondary)', marginTop: 4 }}>
                  {/* Rows written before the comply-first split carry declined:null WITH the old
                      "⚠ Declined disruptive change:" text — strip that too, else a replayed
                      historical event reads as "declined" under the "applied" banner. */}
                  • {(d.action || '')
                      .replace(/^ℹ\s*Applied as requested, objection on record:\s*/, '')
                      .replace(/^⚠\s*Declined disruptive change:\s*/, '')}
                </div>
              ))}
            </div>
          )}
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8, flexWrap: 'wrap' }}>
            <strong style={{ fontSize: 15 }}>
              {variant === 'clean' && kind === 'xsd'
                ? (awaiting ? 'Schemas that will change' : editingLive ? 'Schema changes — drafting…' : 'Schema changes (saved)')
                : <>📦 Changes {awaiting ? (kind === 'xsd' ? '— proposed XSD' : '— proposed (XSD + code)')
                    : editingLive ? '— work in progress' : '(artifact)'}</>}
            </strong>
            {editingLive && <span style={{ fontSize: 11, fontWeight: 700, padding: '2px 8px', borderRadius: 12,
              background: 'rgba(245,158,11,0.12)', color: '#f59e0b', border: '1px solid rgba(245,158,11,0.45)' }}>
              ✍️ DRAFT — the agent is still editing; this preview updates live</span>}
            {verifiedBadge && <span style={{ fontSize: 11, fontWeight: 700, padding: '2px 8px', borderRadius: 12,
              background: 'rgba(52,211,153,0.15)', color: '#16a34a', border: '1px solid rgba(52,211,153,0.4)' }}>✅ VERIFIED — compiles</span>}
            {unverifiedBadge && <span title="Verification was skipped or did not pass — this change was not built-verified" style={{ fontSize: 11, fontWeight: 700, padding: '2px 8px', borderRadius: 12,
              background: 'rgba(245,158,11,0.15)', color: '#f59e0b', border: '1px solid rgba(245,158,11,0.5)' }}>⚠ UNVERIFIED — not built</span>}
            {/* Re-verify on demand: rebuild this change and show pass/fail, without re-running the whole pipeline. */}
            {canReverify && (
              <button onClick={reverify} disabled={reverifying}
                title="Re-run the build verification on this change and show pass/fail — without going through the whole change again"
                style={{ fontSize: 11, fontWeight: 700, padding: '2px 10px', borderRadius: 12, cursor: reverifying ? 'wait' : 'pointer',
                  background: 'transparent', color: 'var(--text-secondary)', border: '1px solid var(--border)', opacity: reverifying ? 0.7 : 1 }}>
                {reverifying ? '⏳ Re-verifying…' : '🔁 Re-verify'}
              </button>
            )}
            {canReverify && !reverifying && run?.last_reverify && run.last_reverify.status !== 'running' && (() => {
              const lr = run.last_reverify
              const color = lr.status === 'verified' ? '#16a34a' : lr.status === 'needs_fix' ? '#ef4444' : '#f59e0b'
              const label = lr.status === 'verified' ? '✅ Re-verify passed'
                : lr.status === 'needs_fix' ? '❌ Re-verify failed'
                : lr.status === 'expired' ? '⚠ Re-verify unavailable'
                : lr.status === 'error' ? '⚠ Re-verify errored'
                : '⚠ Re-verify — not built'
              let when = ''
              try { when = lr.at ? ` · ${new Date(lr.at).toLocaleTimeString()}` : '' } catch { /* noop */ }
              return (
                <span title={lr.reason || ''} style={{ fontSize: 11, fontWeight: 700, padding: '2px 8px', borderRadius: 12,
                  background: 'transparent', color, border: `1px solid ${color}` }}>{label}{when}</span>
              )
            })()}
            {acceptedRisk && <span style={{ fontSize: 11, fontWeight: 700, padding: '2px 8px', borderRadius: 12,
              background: 'rgba(239,68,68,0.15)', color: '#ef4444', border: '1px solid rgba(239,68,68,0.5)' }}>⛔ DANGER — disruptive change accepted</span>}
            {!awaiting && !editingLive && variant !== 'clean' && <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>saved · inspectable anytime</span>}
          </div>
          {variant === 'clean' && kind === 'xsd' ? (() => {
            // PM-friendly view: per-schema cards in plain language (element-level from the
            // engine's deterministic diff record); raw git diff demoted to an expander.
            const paths = changedPaths(diffs)
            const stats = fileStats(diffs)
            const lineInfo = diffLineInfo(diffs)
            const recs = run?.xsd_changes || {}
            const recFor = (p) => {
              for (const [k, v] of Object.entries(recs)) if (k === p || k.endsWith(':' + p)) return v
              return null
            }
            return (
              <>
                {paths.map(p => {
                  const name = p.split('/').pop()
                  const st = stats[p] || {}
                  const rec = recFor(p) || {}
                  const bullets = []
                  if (rec.new?.length) bullets.push(['Adds', rec.new])
                  if (rec.modified?.length) bullets.push(['Updates', rec.modified])
                  if (rec.deprecated?.length) bullets.push(['Deprecates', rec.deprecated])
                  return (
                    <div key={p} title={p} style={{ padding: '12px 14px', marginBottom: 8, borderRadius: 8,
                      background: 'var(--bg-input, rgba(127,127,127,0.04))', border: '1px solid var(--border)' }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                        <span>📐</span>
                        <strong style={{ fontSize: 13.5, color: 'var(--text-primary)' }}>{name}</strong>
                        <span style={{ fontSize: 10, fontWeight: 700, padding: '1px 8px', borderRadius: 10,
                          background: st.isNew ? 'rgba(37,99,235,0.10)' : 'rgba(52,211,153,0.12)',
                          color: st.isNew ? 'var(--accent, #2563eb)' : '#16a34a',
                          border: '1px solid ' + (st.isNew ? 'rgba(37,99,235,0.35)' : 'rgba(52,211,153,0.35)') }}>
                          {st.isNew ? 'NEW SCHEMA' : 'UPDATED'}
                        </span>
                      </div>
                      {bullets.length > 0 ? bullets.map(([label, arr]) => (
                        <div key={label} style={{ fontSize: 12.5, color: 'var(--text-secondary)', marginTop: 4 }}>
                          {label}: <span style={{ fontWeight: 600 }}>{_names(arr)}</span>
                        </div>
                      )) : (
                        <div style={{ fontSize: 12.5, color: 'var(--text-muted)', marginTop: 4 }}>
                          {st.isNew ? 'A new schema file is introduced.'
                            : `Content updated — ${st.adds || 0} line${(st.adds || 0) === 1 ? '' : 's'} added, ${st.dels || 0} removed.`}
                        </div>
                      )}
                      {(() => {
                        const file = (xsdFiles || []).find(f => f.path === p || f.path?.endsWith('/' + p) || p.endsWith('/' + f.path))
                        if (!file?.content) return null
                        const info = st.isNew ? null : lineInfo[p]   // new schema = everything is new; no per-line marks
                        const hl = info?.added?.size ? info.added : null
                        const rem = info?.removed?.size ? info.removed : null
                        return (
                          <details style={{ marginTop: 6 }}>
                            <summary style={{ fontSize: 12, color: 'var(--accent, #2563eb)', cursor: 'pointer' }}>
                              View schema
                            </summary>
                            {(hl || rem) && (
                              <div style={{ fontSize: 11.5, color: 'var(--text-muted)', margin: '6px 0 0 2px' }}>
                                {hl && <>
                                  <span style={{ display: 'inline-block', width: 10, height: 10, borderRadius: 2,
                                    background: 'rgba(34,197,94,0.35)', border: '1px solid #16a34a',
                                    marginRight: 6, verticalAlign: 'middle' }} />
                                  Highlighted lines are new or updated in this change.
                                </>}
                                {rem && <>
                                  <span style={{ display: 'inline-block', width: 10, height: 10, borderRadius: 2,
                                    background: 'rgba(239,68,68,0.30)', border: '1px solid #ef4444',
                                    margin: hl ? '0 6px 0 12px' : '0 6px 0 0', verticalAlign: 'middle' }} />
                                  <span style={{ textDecoration: 'line-through' }}>Struck-through</span> lines were removed.
                                </>}
                              </div>
                            )}
                            <div style={{ marginTop: 6 }}><XmlView text={file.content} highlights={hl} removed={rem} /></div>
                          </details>
                        )
                      })()}
                    </div>
                  )
                })}
                <details style={{ marginTop: 6 }}>
                  <summary style={{ fontSize: 12, color: 'var(--text-muted)', cursor: 'pointer' }}>
                    View technical diff (for engineers)
                  </summary>
                  <div style={{ marginTop: 8 }}><DiffBlock diffs={diffs} stats={diffStats} light kind={kind} xsdFiles={xsdFiles} /></div>
                </details>
              </>
            )
          })() : (
            <>
              {variant === 'clean' && (() => {
                const paths = changedPaths(diffs)
                if (!paths.length) return null
                return (
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 10 }}>
                    {paths.map(p => {
                      const name = p.split('/').pop()
                      const isSchema = /\.(xsd|xjb)$/i.test(name)
                      return (
                        <span key={p} title={p} style={{ fontSize: 12, padding: '3px 10px', borderRadius: 14,
                          border: '1px solid ' + (isSchema ? 'rgba(37,99,235,0.4)' : 'var(--border)'),
                          background: isSchema ? 'rgba(37,99,235,0.08)' : 'var(--bg-input, transparent)',
                          color: isSchema ? 'var(--accent, #2563eb)' : 'var(--text-secondary)', fontWeight: isSchema ? 600 : 400 }}>
                          {isSchema ? '📐 ' : ''}{name}
                        </span>
                      )
                    })}
                    <span style={{ fontSize: 12, color: 'var(--text-muted)', alignSelf: 'center' }}>
                      {paths.length} file{paths.length === 1 ? '' : 's'}
                    </span>
                  </div>
                )
              })()}
              <DiffBlock diffs={diffs} stats={diffStats} light={variant === 'clean'} kind={kind} xsdFiles={xsdFiles} />
            </>
          )}
          {run?.manifest_hash && <div style={{ color: '#666', fontSize: 12, marginTop: 6 }}>
            manifest_hash: <code>{run.manifest_hash?.slice(0, 16)}…</code>
          </div>}
          {kind === 'code' && run?.run_id && <ChangeWalkthrough runId={run.run_id} />}
        </section>
      )}

      {/* Approve / refine controls — only while awaiting the human. */}
      {awaiting && (
        <section style={{ marginTop: 12 }}>
          {openBlocker && (
            <div style={{ fontSize: 13, marginBottom: 10, padding: '10px 12px', borderRadius: 6,
              background: 'rgba(239,68,68,0.10)', border: '1px solid rgba(239,68,68,0.55)', color: '#ef4444' }}>
              <div style={{ fontWeight: 700 }}>⛔ Unresolved blocker — push is held</div>
              <div style={{ color: 'var(--text-secondary)', marginTop: 4 }}>
                The reviewer left a blocker-severity finding open after the agent's fix budget. Fix it
                (Start over / request changes), or override below to push anyway.
              </div>
              {blkItems.some(it => it.reviewer_gap) && (
                <div style={{ fontSize: 12, color: '#d97706', marginTop: 4 }}>
                  Items tagged REVIEWER GAP are verdicts the reviewer could not produce (not confirmed
                  code defects) — adjudicate these directly rather than re-running the agent on them.
                </div>
              )}
              <ul style={{ margin: '6px 0 0', paddingLeft: 18, color: 'var(--text-secondary)' }}>
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
                      <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 2 }}>✓ done when: {it.done_when}</div>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {acceptedRisk && (
            <div style={{ fontSize: 13, marginBottom: 10, padding: '8px 12px', borderRadius: 6, fontWeight: 600,
              background: 'rgba(239,68,68,0.10)', border: '1px solid rgba(239,68,68,0.5)', color: '#ef4444' }}>
              ⛔ This change-set includes a disruptive change you explicitly accepted after being warned.
              The risk acceptance is recorded on this run permanently.
            </div>
          )}
          {/* Git guardrail: tell the human exactly where approval will push — and,
              for a multi-repo change, WHICH repos get a branch + MR. */}
          {kind === 'code' && frozenBranch && (() => {
            const repos = changedPaths(diffs).map(p => p.split('/')[0])
            const uniqRepos = [...new Set(repos)]
            return (
              <div style={{ fontSize: 13, marginBottom: 10, padding: '8px 12px', borderRadius: 6,
                background: 'rgba(96,165,250,0.08)', border: '1px solid rgba(96,165,250,0.3)', color: 'var(--text-secondary)' }}>
                On approval the verified change is committed and pushed to the feature branch{' '}
                <code style={{ color: '#60a5fa' }}>{frozenBranch}</code>
                {uniqRepos.length > 1
                  ? <> in each of the {uniqRepos.length} affected repos, with one merge request per repo.</>
                  : <> (created for this change), and a merge request is opened.</>}
              </div>
            )
          })()}
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
            <button onClick={() => approve(true, !!openBlocker)} onBlur={() => setConfirmApprove(false)}
              style={{ padding: '8px 16px', fontWeight: 600, color: '#fff', border: 'none', borderRadius: 6, cursor: 'pointer',
                background: confirmApprove ? '#dc2626' : (openBlocker ? '#b91c1c' : '#16a34a') }}>
              {confirmApprove
                ? (openBlocker ? 'Click again to push despite the blocker' : 'Click again to confirm push')
                : (openBlocker ? '⛔ Override blocker & push' : approveLabel)}
            </button>
            {kind !== 'xsd' && (
              <button onClick={() => approve(false)}
                title="Approve the change and move the flow on — push the branch later from this page"
                style={{ padding: '8px 16px', fontWeight: 600, background: 'transparent', color: '#16a34a',
                  border: '1px solid #16a34a', borderRadius: 6, cursor: 'pointer' }}>
                Approve — push later
              </button>
            )}
            {zipButton}
          </div>

          {/* Refine loop (Phase A): request changes to the XSDs; the agent declines disruptive ones. */}
          {kind === 'xsd' && (
            <div style={{ marginTop: 14, paddingTop: 12, borderTop: '1px solid var(--border)' }}>
              <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 6 }}>Not quite right? Request changes</div>
              <textarea value={feedback} onChange={e => setFeedback(e.target.value)} rows={2}
                placeholder="e.g. add an optional remarks field to the request; rename element X to Y"
                style={{ width: '100%', padding: 8, borderRadius: 6, border: '1px solid var(--border)',
                  background: 'var(--bg-input, #0b1021)', color: 'var(--text-primary)', fontSize: 13 }} />
              <button onClick={requestChanges} disabled={!feedback.trim()}
                style={{ marginTop: 6, padding: '7px 14px', fontWeight: 600, background: '#334155',
                  color: '#fff', border: 'none', borderRadius: 6, cursor: 'pointer' }}>Request these changes</button>
              <span style={{ fontSize: 11, color: 'var(--text-muted)', marginLeft: 10 }}>
                The agent applies safe changes and warns + declines anything breaking.
              </span>
            </div>
          )}
        </section>
      )}

      {/* Deferred push — approved, branch not on origin yet; push whenever ready. */}
      {run?.push_deferred && pushTargets.length === 0 && (
        <section style={{ marginTop: 16, padding: '12px 14px', borderRadius: 8, display: 'flex',
          alignItems: 'center', gap: 12, flexWrap: 'wrap',
          background: 'rgba(245,158,11,0.08)', border: '1px solid rgba(245,158,11,0.4)' }}>
          <div style={{ flex: 1, minWidth: 240 }}>
            <strong style={{ fontSize: 14, color: '#f59e0b' }}>✓ Approved — not pushed to git yet</strong>
            <div style={{ fontSize: 12.5, color: 'var(--text-secondary)', marginTop: 4 }}>
              The change flow has moved on. The approved branch is kept ready
              {frozenBranch && <> (<code style={{ color: '#60a5fa' }}>{frozenBranch}</code>)</>} — push whenever you want.
            </div>
          </div>
          {zipButton}
          <button onClick={() => pushNow(!!openBlocker)} style={{ padding: '8px 16px', fontWeight: 600,
            background: openBlocker ? '#b91c1c' : '#16a34a',
            color: '#fff', border: 'none', borderRadius: 6, cursor: 'pointer', flexShrink: 0 }}>
            {openBlocker ? '⛔ Override blocker & push' : '⬆ Push to git now'}
          </button>
        </section>
      )}

      {/* STALE push — the branch on git was pushed under an OLDER manifest: the code was
          regenerated after the push, so what this panel shows is NOT what git holds. */}
      {run?.push_stale && (
        <section style={{ marginTop: 16, padding: '12px 14px', borderRadius: 8, display: 'flex',
          alignItems: 'center', gap: 12, flexWrap: 'wrap',
          background: 'rgba(239,68,68,0.08)', border: '1px solid rgba(239,68,68,0.5)' }}>
          <div style={{ flex: 1, minWidth: 240 }}>
            <strong style={{ fontSize: 14, color: '#ef4444' }}>⚠ Git is behind what you see here</strong>
            <div style={{ fontSize: 12.5, color: 'var(--text-secondary)', marginTop: 4 }}>
              The branch on git was pushed from an <strong>older</strong> version of this change — the code
              was regenerated afterwards. Approve the current changes and push again to publish them
              (a fresh branch is created; the old one stays untouched).
            </div>
          </div>
          <button onClick={() => pushNow(!!openBlocker)} style={{ padding: '8px 16px', fontWeight: 600,
            background: '#dc2626', color: '#fff', border: 'none', borderRadius: 6, cursor: 'pointer', flexShrink: 0 }}>
            ⬆ Push current changes
          </button>
        </section>
      )}

      {/* Pushed summary — branch + commit + MR the change actually landed on. */}
      {pushTargets.length > 0 && (
        <section style={{ marginTop: 16, padding: '12px 14px', borderRadius: 8,
          background: 'rgba(52,211,153,0.08)', border: '1px solid rgba(52,211,153,0.35)' }}>
          <strong style={{ fontSize: 14, color: '#16a34a' }}>🎉 Pushed to git</strong>
          {pushTargets.map(t => (
            <div key={t.repo_id} style={{ fontSize: 13, marginTop: 8, color: 'var(--text-secondary)' }}>
              <span style={{ fontFamily: 'monospace' }}>{t.repo || t.repo_id?.slice(0, 8)}</span>{' '}
              → branch <code style={{ color: '#60a5fa' }}>{t.branch}</code>
              {t.commit && <> @ commit <code>{t.commit.slice(0, 10)}</code></>}
              {t.mr_url && <> · <a href={safeHref(t.mr_url)} target="_blank" rel="noreferrer" style={{ color: '#60a5fa' }}>Open merge request →</a></>}
            </div>
          ))}
        </section>
      )}
    </div>
  )
}
