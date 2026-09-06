// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useState, useEffect, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Play, Wifi, Loader, CheckCircle, XCircle, AlertCircle, Server, Clock, Package, Activity,
} from 'lucide-react'
import { buildSmokeApi } from '../../services/api'

// Verify the Phase B build+deploy wiring WITHOUT a change request.
//
// The real Build step sits inside a change at Phase B BUILD behind the
// governance gate, so on a fresh environment there is no way to answer "does
// build_and_deploy.sh actually work here?" without walking a change all the way
// there. This page runs the same runner standalone.
//
// Preflight is a fast probe (is the script there, are git/mvn/java on PATH).
// Full run is the real clone+build+deploy — minutes — so it runs in the
// background and this page polls, which also means navigating away doesn't
// kill it.

const POLL_MS = 2000

function Row({ label, value, mono }) {
  return (
    <div style={{ display: 'flex', gap: '12px', padding: '5px 0', fontSize: '12px' }}>
      <span style={{ width: '150px', flexShrink: 0, color: 'var(--text-muted)' }}>{label}</span>
      <span style={{
        color: 'var(--text-primary)', wordBreak: 'break-all',
        fontFamily: mono ? 'var(--font-mono, monospace)' : 'inherit',
      }}>{value ?? '—'}</span>
    </div>
  )
}

function StatusPill({ status }) {
  const map = {
    running: { bg: 'rgba(90,150,220,0.12)', fg: 'var(--accent)',  Icon: Loader,      text: 'Running' },
    success: { bg: 'rgba(76,175,125,0.12)', fg: 'var(--success)', Icon: CheckCircle, text: 'Success' },
    failure: { bg: 'rgba(224,108,108,0.12)', fg: 'var(--danger)', Icon: XCircle,     text: 'Failed' },
    timeout: { bg: 'rgba(220,170,80,0.12)', fg: 'var(--warning, #d9a441)', Icon: Clock, text: 'Timed out' },
    error:   { bg: 'rgba(224,108,108,0.12)', fg: 'var(--danger)', Icon: AlertCircle, text: 'Error' },
  }
  const s = map[status] || map.error
  const { Icon } = s
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: '5px', padding: '3px 10px',
      borderRadius: '20px', background: s.bg, color: s.fg, fontSize: '11px', fontWeight: 600,
    }}>
      <Icon size={11} style={status === 'running' ? { animation: 'spin 1s linear infinite' } : undefined} />
      {s.text}
    </span>
  )
}

function LogView({ lines, truncated }) {
  const ref = useRef(null)
  const stick = useRef(true)

  const onScroll = () => {
    const el = ref.current
    if (!el) return
    stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40
  }
  useEffect(() => {
    if (stick.current && ref.current) ref.current.scrollTop = ref.current.scrollHeight
  }, [lines])

  if (!lines?.length) return null
  return (
    <div ref={ref} onScroll={onScroll} style={{
      maxHeight: '460px', overflowY: 'auto', overflowX: 'auto',
      background: 'var(--bg-base, #0d0f12)', border: '1px solid var(--border)',
      borderRadius: '6px', padding: '10px 12px', marginTop: '10px',
      fontFamily: 'var(--font-mono, monospace)', fontSize: '11.5px', lineHeight: 1.6,
    }}>
      {truncated && (
        <div style={{ color: 'var(--text-muted)', marginBottom: '6px' }}>
          … earlier lines trimmed — the full log is on disk (path below)
        </div>
      )}
      {lines.map((l, i) => (
        <div key={i} style={{
          whiteSpace: 'pre', color:
            l.includes('══ section:') ? 'var(--accent)'
            : l.includes('... alive') ? 'var(--text-muted)'
            : / ! /.test(l) ? 'var(--danger)'
            : 'var(--text-secondary, #c9d1d9)',
        }}>{l}</div>
      ))}
    </div>
  )
}

export default function BuildHost() {
  const [run, setRun]           = useState(null)
  const [since, setSince]       = useState(0)
  const [lines, setLines]       = useState([])
  const [busy, setBusy]         = useState(null)   // 'preflight' | 'full'
  const [error, setError]       = useState('')
  const [coreBranch, setCore]   = useState('master')
  const [appBranch, setAppBranch]   = useState('master')

  const { data, isLoading, refetch } = useQuery({
    queryKey: ['build-smoke-config'],
    queryFn: () => buildSmokeApi.getConfig().then(r => r.data),
  })
  const cfg = data?.config

  // Reattach after a reload. The run lives on the backend, not in this
  // component — without this, refreshing mid-build looks like the run vanished
  // even though it is still going. Prefer a live run; otherwise show the last
  // one so the previous result survives a refresh too.
  useEffect(() => {
    if (run || !data?.recent?.length) return
    const target = data.recent.find(r => r.status === 'running') || data.recent[0]
    if (!target) return
    let cancelled = false
    buildSmokeApi.pollRun(target.id, 0).then(({ data: d }) => {
      if (cancelled) return
      setRun(d); setLines(d.lines || []); setSince(d.next_index)
      if (d.status === 'running') setBusy(d.kind === 'preflight' ? 'preflight' : 'full')
    }).catch(() => {})
    return () => { cancelled = true }
  }, [data, run])

  // Poll while a run is live.
  useEffect(() => {
    if (!run || run.status !== 'running') return
    const t = setInterval(async () => {
      try {
        const res = await buildSmokeApi.pollRun(run.id, since)
        const d = res.data
        if (d.lines?.length) setLines(prev => [...prev, ...d.lines])
        setSince(d.next_index)
        setRun(d)
        if (d.status !== 'running') setBusy(null)
      } catch (e) {
        setError(e?.response?.data?.detail || e.message)
        setBusy(null)
      }
    }, POLL_MS)
    return () => clearInterval(t)
  }, [run, since])

  const reset = () => { setLines([]); setSince(0); setError(''); setRun(null) }

  const doPreflight = async () => {
    reset(); setBusy('preflight')
    try {
      const { data: d } = await buildSmokeApi.preflight()
      if (d.status === 'blocked') { setError(d.blocker); setBusy(null); refetch(); return }
      setLines(d.run.lines || []); setSince(d.run.next_index); setRun(d.run)
    } catch (e) {
      setError(e?.response?.data?.detail || e.message)
    } finally {
      setBusy(b => (b === 'preflight' ? null : b))
      refetch()
    }
  }

  const doFullRun = async () => {
    reset(); setBusy('full')
    try {
      const { data: d } = await buildSmokeApi.startRun({ core_branch: coreBranch, app_branch: appBranch })
      if (d.status === 'blocked') { setError(d.blocker); setBusy(null); refetch(); return }
      setRun(d.run); setSince(d.run.next_index); setLines(d.run.lines || [])
    } catch (e) {
      setError(e?.response?.data?.detail || e.message)
      setBusy(null)
    }
  }

  if (isLoading) return <div style={{ padding: '32px', fontSize: '13px', color: 'var(--text-muted)' }}>Loading…</div>

  const blocked = cfg && !cfg.ready
  const live = run?.status === 'running'

  return (
    <div style={{ padding: '28px 32px', maxWidth: '1100px' }}>
      <div style={{ marginBottom: '6px', display: 'flex', alignItems: 'center', gap: '10px' }}>
        <Server size={18} style={{ color: 'var(--accent)' }} />
        <h1 style={{ margin: 0, fontSize: '19px', fontWeight: 600, color: 'var(--text-primary)' }}>Build Host</h1>
      </div>
      <p style={{ margin: '0 0 22px', fontSize: '12.5px', color: 'var(--text-muted)' }}>
        Verify the Phase B build+deploy script end to end without creating a change request.
        Runs the same runner the Build step uses.
      </p>

      {/* Resolved wiring */}
      <div style={{
        borderRadius: '8px', background: 'var(--bg-elevated)',
        border: '1px solid var(--border)', overflow: 'hidden', marginBottom: '18px',
      }}>
        <div style={{
          padding: '13px 18px', borderBottom: '1px solid var(--border-subtle)',
          display: 'flex', alignItems: 'center', gap: '10px',
        }}>
          <div style={{ flex: 1 }}>
            <p style={{ margin: 0, fontSize: '13.5px', fontWeight: 600, color: 'var(--text-primary)' }}>
              Current wiring
            </p>
            <p style={{ margin: '1px 0 0', fontSize: '11px', color: 'var(--text-muted)' }}>
              Read-only — these come from PHASE_B_* env vars, not the config table
            </p>
          </div>
          <button onClick={doPreflight} disabled={!!busy || blocked} style={{
            display: 'flex', alignItems: 'center', gap: '5px', padding: '6px 13px',
            background: 'transparent', border: '1px solid var(--border)', borderRadius: '5px',
            fontSize: '11.5px', color: 'var(--text-muted)',
            cursor: busy || blocked ? 'not-allowed' : 'pointer', opacity: blocked ? 0.5 : 1,
          }}>
            {busy === 'preflight'
              ? <Loader size={11} style={{ animation: 'spin 1s linear infinite' }} />
              : <Wifi size={11} />}
            Run preflight
          </button>
        </div>
        <div style={{ padding: '12px 18px' }}>
          <Row label="Runner mode" value={cfg?.mode} mono />
          <Row label="Build script" value={cfg?.build_script} mono />
          {cfg?.mode === 'ssh' && <>
            <Row label="Host" value={cfg?.host} mono />
            <Row label="Host user" value={cfg?.host_user} mono />
            <Row label="SSH key" value={cfg?.host_key} mono />
            <Row label="Known hosts" value={cfg?.known_hosts || '(auto-discover)'} mono />
          </>}
          {cfg?.mode === 'local' && (
            <Row label="Runs as" value="a subprocess of the backend process" />
          )}
        </div>
        {blocked && (
          <div style={{
            padding: '11px 18px', background: 'rgba(220,170,80,0.07)',
            borderTop: '1px solid var(--border-subtle)', display: 'flex', gap: '9px',
          }}>
            <AlertCircle size={14} style={{ color: 'var(--warning, #d9a441)', flexShrink: 0, marginTop: '1px' }} />
            <span style={{ fontSize: '12px', color: 'var(--warning, #d9a441)', lineHeight: 1.55 }}>
              {cfg.blocker}
            </span>
          </div>
        )}
      </div>

      {/* Full run */}
      <div style={{
        borderRadius: '8px', background: 'var(--bg-elevated)',
        border: '1px solid var(--border)', overflow: 'hidden', marginBottom: '18px',
      }}>
        <div style={{
          padding: '13px 18px', borderBottom: '1px solid var(--border-subtle)',
          display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap',
        }}>
          <div style={{ flex: 1, minWidth: '220px' }}>
            <p style={{ margin: 0, fontSize: '13.5px', fontWeight: 600, color: 'var(--text-primary)' }}>
              Full build + deploy
            </p>
            <p style={{ margin: '1px 0 0', fontSize: '11px', color: 'var(--text-muted)' }}>
              Real clone, build and deploy — takes minutes. Keeps running if you navigate away.
            </p>
          </div>
          <input value={coreBranch} onChange={e => setCore(e.target.value)} placeholder="core branch"
            disabled={!!busy} style={{
              width: '120px', padding: '5px 9px', fontSize: '11.5px', borderRadius: '5px',
              border: '1px solid var(--border)', background: 'var(--bg-base)', color: 'var(--text-primary)',
            }} />
          <input value={appBranch} onChange={e => setAppBranch(e.target.value)} placeholder="app branch"
            disabled={!!busy} style={{
              width: '120px', padding: '5px 9px', fontSize: '11.5px', borderRadius: '5px',
              border: '1px solid var(--border)', background: 'var(--bg-base)', color: 'var(--text-primary)',
            }} />
          <button onClick={doFullRun} disabled={!!busy || blocked} style={{
            display: 'flex', alignItems: 'center', gap: '5px', padding: '6px 13px',
            background: busy || blocked ? 'transparent' : 'var(--accent)',
            border: '1px solid var(--border)', borderRadius: '5px', fontSize: '11.5px',
            color: busy || blocked ? 'var(--text-muted)' : '#fff',
            cursor: busy || blocked ? 'not-allowed' : 'pointer', opacity: blocked ? 0.5 : 1,
          }}>
            {busy === 'full'
              ? <Loader size={11} style={{ animation: 'spin 1s linear infinite' }} />
              : <Play size={11} />}
            Run full build
          </button>
        </div>

        <div style={{ padding: '12px 18px' }}>
          {error && (
            <div style={{
              padding: '9px 13px', borderRadius: '6px', marginBottom: '10px',
              background: 'rgba(224,108,108,0.08)', border: '1px solid rgba(224,108,108,0.25)',
              display: 'flex', gap: '8px',
            }}>
              <AlertCircle size={13} style={{ color: 'var(--danger)', flexShrink: 0, marginTop: '2px' }} />
              <span style={{ fontSize: '12px', color: 'var(--danger)', lineHeight: 1.5 }}>{error}</span>
            </div>
          )}

          {!run && !error && (
            <p style={{ margin: 0, fontSize: '12px', color: 'var(--text-muted)' }}>
              No run yet. Start with the preflight — it catches every wiring problem in seconds.
            </p>
          )}

          {run && (
            <>
              <div style={{ display: 'flex', alignItems: 'center', gap: '14px', flexWrap: 'wrap', marginBottom: '4px' }}>
                <StatusPill status={run.status} />
                <span style={{ fontSize: '11.5px', color: 'var(--text-muted)' }}>
                  {run.kind === 'preflight' ? 'Preflight' : 'Full build'} · {run.elapsed_seconds}s
                  {run.exit_code != null && ` · exit ${run.exit_code}`}
                  {live && ` · ${run.section}`}
                </span>
              </div>

              {run.kind === 'full' && (
                <div style={{ display: 'flex', gap: '18px', flexWrap: 'wrap', marginTop: '8px' }}>
                  <span style={{ fontSize: '11.5px', color: 'var(--text-muted)', display: 'flex', alignItems: 'center', gap: '5px' }}>
                    <Activity size={11} /> build {run.section_seconds?.build}s · deploy {run.section_seconds?.deploy}s · startup {run.section_seconds?.startup}s
                  </span>
                  <span style={{ fontSize: '11.5px', color: 'var(--text-muted)', display: 'flex', alignItems: 'center', gap: '5px' }}>
                    <Package size={11} /> {run.artifacts?.length || 0} artifact(s) · {run.services?.length || 0} service(s)
                  </span>
                </div>
              )}

              {run.note && (
                <div style={{
                  marginTop: '10px', padding: '9px 13px', borderRadius: '6px',
                  background: 'rgba(220,170,80,0.07)', border: '1px solid rgba(220,170,80,0.22)',
                  fontSize: '11.5px', color: 'var(--warning, #d9a441)', lineHeight: 1.55,
                }}>{run.note}</div>
              )}

              <LogView lines={lines} truncated={run.truncated} />

              {run.log_path && (
                <p style={{ margin: '8px 0 0', fontSize: '11px', color: 'var(--text-muted)' }}>
                  Full untruncated log on disk: <code>{run.log_path}</code>
                </p>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  )
}
