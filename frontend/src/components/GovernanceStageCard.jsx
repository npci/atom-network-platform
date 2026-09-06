// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useCallback, useEffect, useRef, useState } from 'react'
import { agenticApi } from '../services/api'
import { wsUrl } from '../utils/basePath'
import { DiffBlock } from './AgenticPhasePanel'
import RunUsageBadge from './RunUsageBadge'
import TranscriptsDownloadButton from './TranscriptsDownloadButton'

// One governance review stage (EA or InfoSec) as a subtle in-flow card — the
// clean-variant look from AgenticPhasePanel (friendly headline + one changing
// action line + a tucked-away technical feed) with the stage-specific bits:
// rule-coverage line, findings table, and the approve-fixes / override gate.
// Live data rides the SAME agentic run machinery as codegen (WS + events).

const STAGE_ICON = { ea: '🏛', infosec: '🛡' }

const CHIP = {
  waiting:   { label: 'Waiting',               bg: 'rgba(148,163,184,0.15)', fg: '#94a3b8' },
  preparing: { label: 'Preparing',             bg: 'rgba(56,189,248,0.15)',  fg: '#38bdf8' },
  reviewing: { label: 'Reviewing',             bg: 'rgba(96,165,250,0.15)',  fg: '#60a5fa' },
  fixing:    { label: 'Fixing findings',       bg: 'rgba(167,139,250,0.15)', fg: '#a78bfa' },
  verifying: { label: 'Verifying (build+tests)', bg: 'rgba(34,211,238,0.15)', fg: '#22d3ee' },
  gate:      { label: 'Awaiting your approval', bg: 'rgba(245,158,11,0.15)', fg: '#f59e0b' },
  delivering:{ label: 'Delivering fixes',      bg: 'rgba(52,211,153,0.15)',  fg: '#34d399' },
  clean:     { label: 'Passed — compliant',    bg: 'rgba(52,211,153,0.15)',  fg: '#16a34a' },
  fixed:     { label: 'Passed — fixes delivered', bg: 'rgba(52,211,153,0.15)', fg: '#16a34a' },
  overridden:{ label: 'Passed with override',  bg: 'rgba(245,158,11,0.15)',  fg: '#d97706' },
  stopped:   { label: 'Stopped',               bg: 'rgba(239,68,68,0.15)',   fg: '#ef4444' },
}

function chipFor(view) {
  if (!view?.run_id) return CHIP.waiting
  if (view.status === 'completed') {
    return view.result === 'clean' ? CHIP.clean
      : view.result === 'overridden' ? CHIP.overridden : CHIP.fixed
  }
  if (['failed', 'gave_up', 'cancelled'].includes(view.status)) return CHIP.stopped
  switch (view.phase) {
    case 'review': return CHIP.reviewing
    case 'code_change': return CHIP.fixing
    case 'verification': return CHIP.verifying
    case 'awaiting_human_approval': return CHIP.gate
    case 'pushing': return CHIP.delivering
    default: return CHIP.preparing
  }
}

const HEADLINE = {
  waiting:   (label) => `Waiting for the previous stage before ${label} starts`,
  preparing: () => 'Locating the approved change…',
  reviewing: () => 'Reviewing the change rule-by-rule against the skill…',
  fixing:    () => 'Applying minimal fixes for the cited findings…',
  verifying: () => 'Re-running the build and tests — proving nothing broke…',
  gate:      () => 'Fixes staged — review them and approve to deliver',
  delivering:() => 'Pushing the approved fixes to the feature branch…',
  clean:     () => 'Compliant — nothing needed fixing',
  fixed:     () => 'Fixes approved and delivered',
  overridden:() => 'Completed with an audited override',
  stopped:   () => 'Stopped — needs your attention',
}

function SevBadge({ s }) {
  const c = { blocker: '#ef4444', error: '#f97316', warning: '#f59e0b', info: '#60a5fa' }[s] || '#94a3b8'
  return <span style={{ fontSize: 10, fontWeight: 700, color: c, border: `1px solid ${c}55`,
    background: `${c}18`, borderRadius: 4, padding: '1px 6px', textTransform: 'uppercase' }}>{s}</span>
}

export default function GovernanceStageCard({ stage, view, onChanged, changeId }) {
  const [events, setEvents] = useState([])
  const [error, setError] = useState(null)
  const [approving, setApproving] = useState(false)
  const [stopping, setStopping] = useState(false)
  const [showOverride, setShowOverride] = useState(false)
  const [overrideReason, setOverrideReason] = useState('')
  const wsRef = useRef(null)
  const streamedRunRef = useRef(null)

  // The stage's fix-delta diff (frozen with the stage manifest; served by the
  // same /runs/{id}/diff artifact endpoint codegen approval uses) — so the human
  // sees the actual before/after changes, not just file names.
  const [diffs, setDiffs] = useState(null)
  const [diffStats, setDiffStats] = useState(null)

  const runId = view?.run_id
  useEffect(() => { setStopping(false) }, [runId])   // fresh run → fresh Stop button
  const chip = chipFor(view)
  const running = !!runId && view.status === 'active' && view.phase !== 'awaiting_human_approval'
  const parked = view?.phase === 'awaiting_human_approval'
  // Must-fix is decided SERVER-SIDE (each review_item carries a must_fix flag from
  // is_must_block) — the single source of truth for the gate. The UI never
  // re-derives the predicate, so a backend policy change (e.g. a new sensitive
  // category) can't drift the gate into showing a plain Approve over must-fix
  // findings. Legacy items without the flag fall back to the old JS predicate.
  const isMustFix = (i) => (
    'must_fix' in i ? i.must_fix
      : i.severity === 'blocker' || ['security', 'auth', 'authentication', 'authorization',
          'financial', 'regulatory', 'compliance'].includes(String(i.category || '').toLowerCase()))
  const blockerItems = (view?.review_items || []).filter(isMustFix)
  const hasBlockers = blockerItems.length > 0

  const openStream = useCallback((rid) => {
    wsRef.current?.close()
    const ws = new WebSocket(wsUrl(`api/ws/agentic/runs/${rid}`))
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
    ws.onmessage = (evt) => {
      const msg = JSON.parse(evt.data)
      if (msg.type === 'event') {
        setEvents(prev => { const next = [...prev, msg]; return next.length > 1500 ? next.slice(-1000) : next })
        if (['phase_changed', 'governance_stage_parked', 'completed', 'run_terminal',
             'governance_review_verdict'].includes(msg.kind)) onChanged?.()
      } else if (msg.type === 'end') {
        onChanged?.()
      }
    }
    ws.onclose = () => {}
  }, [onChanged])

  useEffect(() => {
    if (runId && streamedRunRef.current !== runId) {
      streamedRunRef.current = runId
      setEvents([])
      openStream(runId)
    }
    return () => { if (!runId) { wsRef.current?.close(); wsRef.current = null } }
  }, [runId, openStream])

  const hasFixes = (view?.fix_files?.length || 0) > 0
  useEffect(() => {
    let alive = true
    setDiffs(null)
    setDiffStats(null)
    if (!runId || !hasFixes) return undefined
    agenticApi.getDiff(runId)
      .then(r => { if (alive) { setDiffs(r.data?.diffs || null); setDiffStats(r.data?.stats || null) } })
      .catch(() => {})
    return () => { alive = false }
  }, [runId, hasFixes, view?.manifest_hash])
  useEffect(() => () => wsRef.current?.close(), [])

  // Latest human-facing action line from the feed (the "what is it doing" pulse).
  const lastAction = [...events].reverse().map(e => e.payload?.action).find(Boolean)
  // Live rule coverage: the verdict event wins over the (post-freeze) status copy.
  const verdictEv = [...events].reverse().find(e => e.kind === 'governance_review_verdict')?.payload
  const cov = verdictEv
    ? { total: verdictEv.rules_total, passed: verdictEv.rules_passed, failed: verdictEv.rules_failed }
    : view?.rule_coverage

  const approve = async (override = false) => {
    if (!runId || !view?.manifest_hash || approving) return
    setApproving(true); setError(null)
    try {
      await agenticApi.approve(runId, view.manifest_hash, true, override, override ? overrideReason : null)
      setShowOverride(false)
      onChanged?.()
    } catch (e) {
      setError(e?.response?.data?.detail || e.message)
    } finally { setApproving(false) }
  }

  const label = view?.label || (stage === 'ea' ? 'EA Review' : 'InfoSec Review')
  const chipKey = Object.keys(CHIP).find(k => CHIP[k] === chip) || 'waiting'

  return (
    <div style={{ marginTop: 12, background: 'var(--bg-elevated)', border: '1px solid var(--border)',
      borderRadius: 12, padding: '14px 18px' }}>
      {/* Header row: icon · label · chip · coverage · usage */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
        <span style={{ fontSize: 15 }}>{STAGE_ICON[stage]}</span>
        <strong style={{ fontSize: 13.5 }}>{label}</strong>
        <span style={{ fontSize: 11, fontWeight: 600, padding: '2px 9px', borderRadius: 20,
          background: chip.bg, color: chip.fg }}>{chip.label}</span>
        {(view?.skills?.length || 0) > 1 ? (
          <span style={{ fontSize: 10.5, color: 'var(--text-muted)' }} title="Every enabled skill slot runs in this stage">
            skills: {view.skills.map(s => `${s.name || 'default'} v${s.version}`).join(' · ')}
          </span>
        ) : view?.skill_version ? (
          <span style={{ fontSize: 10.5, color: 'var(--text-muted)' }}>skill v{view.skill_version}</span>
        ) : null}
        {cov?.total ? (
          <span style={{ fontSize: 11.5, color: 'var(--text-secondary)' }}>
            {cov.passed}/{cov.total} rules compliant
            {cov.failed ? ` · ${cov.failed} violation(s)` : ''}
            {view?.fix_files?.length ? ` · ${view.fix_files.length} file(s) fixed` : ''}
          </span>
        ) : null}
        <span style={{ marginLeft: 'auto' }} />
        {runId && changeId && (
          <TranscriptsDownloadButton changeId={changeId}
            section={stage === 'ea' ? 'ea_review' : 'infosec_review'} label="Transcripts" />
        )}
        {runId && <RunUsageBadge runId={runId} active={running} />}
      </div>

      {/* Advisory smoke banner: the skill's scripts were NOT proven to run, so its
          automated findings may be unreliable. The stage still runs (user decision);
          this is the loud warning. */}
      {view?.smoke_ok === false && (
        <div style={{ marginTop: 10, padding: '9px 12px', borderRadius: 8, fontSize: 12,
          background: 'rgba(239,68,68,0.12)', border: '1px solid rgba(239,68,68,0.4)',
          color: '#ef4444', display: 'flex', gap: 8, alignItems: 'flex-start' }}>
          <span style={{ fontSize: 14, lineHeight: '16px' }}>⚠</span>
          <span style={{ color: 'var(--text-primary)' }}>
            <strong>Smoke test {view.smoke_status || 'not run'}.</strong>{' '}
            {view.smoke_warning?.message
              || `This skill's scripts were not proven to run correctly, so its automated `
                 + `findings may be unreliable. The review still runs — check the findings and `
                 + `fixes with extra care.`}
          </span>
        </div>
      )}

      {/* Failed skill-script executions: a script the skill's procedure needed did
          not run — the review may be incomplete. Persistent (gov json), shown live,
          at the gate, and after completion; never depends on the reviewer's prose. */}
      {(view?.script_failures?.length > 0) && (
        <div style={{ marginTop: 10, padding: '9px 12px', borderRadius: 8, fontSize: 12,
          background: 'rgba(245,158,11,0.12)', border: '1px solid rgba(245,158,11,0.45)',
          color: '#f59e0b', display: 'flex', gap: 8, alignItems: 'flex-start' }}>
          <span style={{ fontSize: 14, lineHeight: '16px' }}>⚠</span>
          <span style={{ color: 'var(--text-primary)' }}>
            <strong>{view.script_failures.length} skill-script execution(s) failed during this stage.</strong>{' '}
            The review may be incomplete — verify these checks by hand:
            <ul style={{ margin: '6px 0 0', paddingLeft: 18 }}>
              {view.script_failures.slice(0, 6).map((f, i) => (
                <li key={i} style={{ fontFamily: 'monospace', fontSize: 11, wordBreak: 'break-all' }}>
                  {f.script || f.command || '?'} — {f.error || `exit ${f.exit_code}`}
                </li>
              ))}
              {view.script_failures.length > 6 && (
                <li style={{ fontSize: 11 }}>…and {view.script_failures.length - 6} more</li>
              )}
            </ul>
          </span>
        </div>
      )}

      {/* Friendly status line (clean-variant look) */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 10, padding: '10px 12px',
        borderRadius: 8, background: 'var(--bg-input, rgba(127,127,127,0.05))', border: '1px solid var(--border)' }}>
        {running && <span style={{ width: 13, height: 13, borderRadius: '50%', flexShrink: 0,
          border: '2px solid var(--accent, #2563eb)', borderTopColor: 'transparent', animation: 'spin 0.8s linear infinite' }} />}
        {!running && <span style={{ flexShrink: 0 }}>{parked ? '🟠' : view?.passed ? '✅' : runId ? '⏸' : '⏳'}</span>}
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontWeight: 600, fontSize: 13, color: 'var(--text-primary)' }}>
            {(HEADLINE[chipKey] || HEADLINE.waiting)(label)}
          </div>
          {running && lastAction && (
            <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 2,
              whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{lastAction}</div>
          )}
        </div>
        {/* Stop: cooperative cancel — the driver honours it at its next check;
            progress so far (checkpointed review batches) survives a later retry. */}
        {running && (
          <button
            onClick={async () => {
              if (!window.confirm(
                `⏹ Stop the ${label}?\n\nThe stage will cancel at its next safe point. `
                + 'Completed review batches are checkpointed; you can re-run the stage later.'
              )) return
              setStopping(true)
              try { await agenticApi.cancel(runId) } catch { setStopping(false) }
            }}
            disabled={stopping}
            style={{ flexShrink: 0, padding: '5px 12px', borderRadius: 6, fontSize: 12,
              cursor: stopping ? 'default' : 'pointer', fontWeight: 600,
              background: 'transparent', border: '1px solid rgba(220,38,38,0.5)',
              color: '#dc2626', opacity: stopping ? 0.6 : 1 }}>
            {stopping ? 'Stopping…' : '⏹ Stop'}
          </button>
        )}
      </div>

      {/* Stopped runs */}
      {['failed', 'gave_up', 'cancelled'].includes(view?.status) && (
        <div style={{ marginTop: 8, padding: '10px 12px', borderRadius: 8, fontSize: 12.5,
          background: 'rgba(220,38,38,0.07)', border: '1px solid rgba(220,38,38,0.3)', color: '#dc2626' }}>
          {String(view?.error || 'The stage stopped.').slice(0, 240)} — use “Start governance reviews” to retry this stage.
        </div>
      )}

      {/* Review points the agent raised — the LEDGER: every point ever raised,
          each marked open|fixed. Live during the fix phase (no manifest needed)
          and preserved after completion, so what-was-fixed never vanishes. */}
      {(() => {
        const ledger = (view?.raised_findings?.length ? view.raised_findings
          : (view?.review_items || []).map(it => ({ ...it, status: 'open' })))
        if (!ledger.length) return null
        const fixed = ledger.filter(it => it.status === 'fixed').length
        const open = ledger.length - fixed
        return (
          <details open={parked || view?.phase === 'code_change'} style={{ marginTop: 8 }}>
            <summary style={{ fontSize: 12, color: 'var(--text-muted)', cursor: 'pointer' }}>
              {ledger.length} review point(s) raised by the agent
              {fixed ? ` · ${fixed} fixed ✓` : ''}{open ? ` · ${open} open` : ''}
            </summary>
            <div style={{ marginTop: 6, border: '1px solid var(--border)', borderRadius: 8, overflow: 'hidden' }}>
              {ledger.map((it, i) => (
                <div key={it.key ?? i} style={{ display: 'flex', gap: 8, alignItems: 'baseline', padding: '7px 10px',
                  borderBottom: i < ledger.length - 1 ? '1px solid var(--border)' : 'none', fontSize: 12,
                  opacity: it.status === 'fixed' ? 0.75 : 1 }}>
                  {it.status === 'fixed'
                    ? <span style={{ fontSize: 10, fontWeight: 700, color: '#16a34a', border: '1px solid #16a34a55',
                        background: '#16a34a18', borderRadius: 4, padding: '1px 6px', flexShrink: 0 }}>✓ FIXED</span>
                    : <span style={{ fontSize: 10, fontWeight: 700, color: '#f59e0b', border: '1px solid #f59e0b55',
                        background: '#f59e0b18', borderRadius: 4, padding: '1px 6px', flexShrink: 0 }}>OPEN</span>}
                  <SevBadge s={it.severity} />
                  <span style={{ color: 'var(--text-muted)', flexShrink: 0 }}>{it.category}</span>
                  <div style={{ minWidth: 0 }}>
                    <div style={{ color: 'var(--text-primary)' }}>{it.why}</div>
                    {it.file && <div style={{ color: 'var(--text-muted)', fontSize: 11 }}>{it.file}{it.line ? `:${it.line}` : ''}</div>}
                    {it.suggested_fix && it.status !== 'fixed' &&
                      <div style={{ color: 'var(--text-secondary)', fontSize: 11 }}>Fix: {it.suggested_fix}</div>}
                  </div>
                </div>
              ))}
            </div>
          </details>
        )
      })()}

      {/* The approval gate: fixes + files changed + approve / override */}
      {parked && (
        <div style={{ marginTop: 10, padding: '12px 14px', borderRadius: 8,
          background: 'rgba(245,158,11,0.07)', border: '1px solid rgba(245,158,11,0.35)' }}>
          <div style={{ fontSize: 12.5, color: 'var(--text-primary)' }}>
            {view.fix_files?.length
              ? <>The agent staged fixes in <strong>{view.fix_files.length} file(s)</strong>. Approving delivers them to the same feature branch{view.unverified_fixes ? ' — ⚠ these fixes did NOT verify (build/tests); approve only if you accept that risk' : ' (build + tests verified)'}. </>
              : <>No fixes were staged{view.review_items?.length ? ' but findings remain open' : ''}. </>}
          </div>
          {view.fix_files?.length > 0 && (
            <details open style={{ marginTop: 6 }}>
              <summary style={{ fontSize: 11.5, color: 'var(--text-muted)', cursor: 'pointer' }}>
                What the agent changed ({view.fix_files.length} file(s)) — review before approving
              </summary>
              {diffs ? (
                <div style={{ marginTop: 6 }}>
                  <DiffBlock diffs={diffs} stats={diffStats} kind="files" light />
                </div>
              ) : (
                <ul style={{ margin: '6px 0 0', paddingLeft: 18, fontSize: 11.5, color: 'var(--text-secondary)' }}>
                  {view.fix_files.map(f => <li key={f} style={{ fontFamily: 'monospace' }}>{f}</li>)}
                </ul>
              )}
            </details>
          )}
          {/* The exact unresolved must-fix findings — always visible at the gate,
              so the human knows precisely WHAT blocks delivery / what an override
              would sign off on. (review_items is capped server-side; the count
              banner covers any overflow.) */}
          {hasBlockers && (
            <div style={{ marginTop: 8, border: '1px solid rgba(239,68,68,0.35)', borderRadius: 8,
              background: 'rgba(239,68,68,0.05)', overflow: 'hidden' }}>
              <div style={{ padding: '7px 10px', fontSize: 12, fontWeight: 700, color: '#ef4444',
                borderBottom: '1px solid rgba(239,68,68,0.25)' }}>
                ⛔ {view?.blocking > blockerItems.length ? `${blockerItems.length} of ${view.blocking}` : blockerItems.length} unresolved
                must-fix finding(s) — these block delivery until fixed or overridden
              </div>
              {blockerItems.map((it, i) => (
                <div key={i} style={{ display: 'flex', gap: 8, alignItems: 'baseline', padding: '7px 10px',
                  borderBottom: i < blockerItems.length - 1 ? '1px solid rgba(239,68,68,0.15)' : 'none',
                  fontSize: 12 }}>
                  <SevBadge s={it.severity} />
                  <span style={{ color: 'var(--text-muted)', flexShrink: 0 }}>{it.category}</span>
                  <div style={{ minWidth: 0 }}>
                    <div style={{ color: 'var(--text-primary)' }}>{it.why}</div>
                    {it.file && (
                      <div style={{ color: 'var(--text-muted)', fontSize: 11, fontFamily: 'monospace' }}>
                        {it.file}{it.line ? `:${it.line}` : ''}
                      </div>
                    )}
                    {it.suggested_fix && (
                      <div style={{ color: 'var(--text-secondary)', fontSize: 11 }}>How to resolve: {it.suggested_fix}</div>
                    )}
                  </div>
                </div>
              ))}
              {view?.blocking > blockerItems.length && (
                <div style={{ padding: '6px 10px', fontSize: 11, color: 'var(--text-muted)' }}>
                  …plus {view.blocking - blockerItems.length} more — the full list is in “review point(s) raised by the agent” above.
                </div>
              )}
            </div>
          )}
          <div style={{ display: 'flex', gap: 8, marginTop: 10, flexWrap: 'wrap', alignItems: 'center' }}>
            {!hasBlockers && (
              <button onClick={() => approve(false)} disabled={approving} style={{
                padding: '7px 16px', background: 'var(--accent)', color: '#fff', border: 'none',
                borderRadius: 7, fontSize: 12.5, fontWeight: 600, cursor: approving ? 'wait' : 'pointer' }}>
                {approving ? 'Approving…' : 'Approve fixes'}
              </button>
            )}
            {hasBlockers && !showOverride && (
              <button onClick={() => setShowOverride(true)} style={{
                padding: '7px 16px', background: 'transparent', color: '#d97706',
                border: '1px solid #d97706', borderRadius: 7, fontSize: 12.5, fontWeight: 600, cursor: 'pointer' }}>
                Override & continue…
              </button>
            )}
          </div>
          {showOverride && (
            <div style={{ marginTop: 8 }}>
              <div style={{ fontSize: 11.5, color: 'var(--text-muted)', marginBottom: 4 }}>
                You are overriding the unresolved must-fix finding(s) listed above — a written
                reason is required and recorded for compliance audit.
              </div>
              <textarea value={overrideReason} onChange={e => setOverrideReason(e.target.value)}
                placeholder="Why is it acceptable to proceed? (min 8 characters)"
                style={{ width: '100%', minHeight: 54, fontSize: 12.5, padding: 8, borderRadius: 7,
                  border: '1px solid var(--border)', background: 'var(--bg-input)', color: 'var(--text-primary)' }} />
              <div style={{ display: 'flex', gap: 8, marginTop: 6 }}>
                <button onClick={() => approve(true)} disabled={approving || overrideReason.trim().length < 8} style={{
                  padding: '6px 14px', background: '#d97706', color: '#fff', border: 'none', borderRadius: 7,
                  fontSize: 12, fontWeight: 600,
                  cursor: overrideReason.trim().length < 8 ? 'not-allowed' : 'pointer',
                  opacity: overrideReason.trim().length < 8 ? 0.5 : 1 }}>
                  {approving ? 'Recording…' : 'Override with reason'}
                </button>
                <button onClick={() => setShowOverride(false)} style={{ padding: '6px 12px', background: 'none',
                  border: '1px solid var(--border)', borderRadius: 7, fontSize: 12, color: 'var(--text-muted)', cursor: 'pointer' }}>
                  Cancel
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {/* After delivery: the diff stays inspectable (frozen with the stage
          manifest — survives workspace cleanup and the push). */}
      {view?.status === 'completed' && hasFixes && (
        <details style={{ marginTop: 8 }}>
          <summary style={{ fontSize: 11.5, color: 'var(--text-muted)', cursor: 'pointer' }}>
            Delivered fixes — view the diff ({view.fix_files.length} file(s))
          </summary>
          {diffs ? (
            <div style={{ marginTop: 6 }}>
              <DiffBlock diffs={diffs} stats={diffStats} kind="files" light />
            </div>
          ) : (
            <div style={{ marginTop: 6, fontSize: 12, color: 'var(--text-muted)' }}>Loading diff…</div>
          )}
        </details>
      )}

      {error && (
        <div style={{ marginTop: 8, padding: '8px 12px', borderRadius: 8, fontSize: 12,
          background: 'rgba(239,68,68,0.08)', border: '1px solid rgba(239,68,68,0.4)', color: '#ef4444' }}>
          {String(error)}
        </div>
      )}

      {/* Technical activity — tucked away, same as the codegen clean variant */}
      {runId && (
        <details style={{ marginTop: 8 }}>
          <summary style={{ fontSize: 11.5, color: 'var(--text-muted)', cursor: 'pointer' }}>
            Show technical activity ({events.filter(e => !['llm_call_started', 'llm_usage'].includes(e.kind)).length})
          </summary>
          <div style={{ marginTop: 6, maxHeight: '32vh', overflowY: 'auto', borderRadius: 8,
            border: '1px solid var(--border)', padding: '6px 12px', fontSize: 12 }}>
            {events.filter(e => !['llm_call_started', 'llm_usage'].includes(e.kind)).map((e, i) => (
              <div key={e.seq ?? i} style={{ display: 'flex', gap: 8, alignItems: 'baseline',
                padding: '3px 0', borderBottom: '1px solid var(--border-subtle, var(--border))' }}>
                <span style={{ color: 'var(--text-secondary)', overflow: 'hidden', textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap' }}>{e.payload?.action || e.kind}</span>
                <span style={{ marginLeft: 'auto', color: 'var(--text-muted)', fontSize: 10, flexShrink: 0 }}>
                  {e.ts ? new Date(e.ts).toLocaleTimeString() : ''}</span>
              </div>
            ))}
          </div>
        </details>
      )}
    </div>
  )
}
