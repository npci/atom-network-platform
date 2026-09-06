// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

// Admin → Governance Skills
//
// The two authoritative pre-build review rulebooks (EA + InfoSec), one per
// team. Uploads are APPEND-ONLY versions — the active skill is the highest
// version, older versions stay as the audit trail, and every governance run
// pins the exact {version, checksum} it enforced. A skill must parse into an
// unambiguous rule list (## RULE <id>: <title> headings) or the upload is
// rejected with the reasons listed.

import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertCircle, CheckCircle, ChevronDown, ChevronUp, Loader2, ShieldCheck, Upload,
} from 'lucide-react'

import { governanceSkillsApi } from '../../services/api'

const TYPES = [
  { key: 'ea', title: 'EA Review Skill', icon: '🏛',
    hint: 'Enterprise Architecture rulebook — layering, reuse-before-new, integration patterns, naming, NFRs.' },
  { key: 'infosec', title: 'InfoSec Review Skill', icon: '🛡',
    hint: 'Information Security rulebook — OWASP-class rules: injection, authn/authz, crypto, secrets, logging.' },
]

// How the parser derived the enforceable units — strongest first.
const MODE_LABEL = {
  rule_headings:  { text: 'explicit rules (## RULE)',      color: '#16a34a' },
  sections:       { text: 'standard SKILL.md sections',    color: '#60a5fa' },
  whole_document: { text: 'whole document — single unit',  color: '#f59e0b' },
}

function ModeChip({ mode }) {
  const m = MODE_LABEL[mode]
  if (!m) return null
  return <span style={{ fontSize: 10.5, fontWeight: 600, padding: '1px 8px', borderRadius: 20,
    border: `1px solid ${m.color}55`, background: `${m.color}18`, color: m.color }}>{m.text}</span>
}

// Prove-it-runs state for a scripted bundle: green = its scripts executed and
// parsed against known-bad/known-good fixtures; anything else gates nothing.
function SmokeChip({ status }) {
  if (!status) return null
  const m = { green: { t: 'smoke: green ✓', c: '#16a34a' },
              pending: { t: 'smoke: pending — run it', c: '#f59e0b' },
              failed: { t: 'smoke: FAILED', c: '#ef4444' } }[status] || { t: `smoke: ${status}`, c: '#94a3b8' }
  return <span style={{ fontSize: 10.5, fontWeight: 700, padding: '1px 8px', borderRadius: 20,
    border: `1px solid ${m.c}55`, background: `${m.c}18`, color: m.c }}>{m.t}</span>
}

function SkillSection({ type, title, icon, hint, active, slots = [], onBanner }) {
  const qc = useQueryClient()
  const fileRef = useRef(null)
  const bundleRef = useRef(null)
  const [showHistory, setShowHistory] = useState(false)
  const [preview, setPreview] = useState(null)   // parsed rules from the last upload

  const { data: history } = useQuery({
    queryKey: ['gov-skill-versions', type],
    queryFn: () => governanceSkillsApi.versions(type),
    enabled: showHistory,
  })

  const _onUploaded = (resp, what) => {
    qc.invalidateQueries({ queryKey: ['gov-skills'] })
    qc.invalidateQueries({ queryKey: ['gov-skill-versions', type] })
    setPreview(resp)
    // A new-slot-beside-existing upload is the 0118 upgrade trap: warn loudly
    // (both rulebooks now run) instead of the usual success banner.
    if (resp.slot_warning) onBanner('err', `⚠ ${resp.slot_warning}`)
    else onBanner('ok', `${title} “${resp.name}” v${resp.version} ${what}`)
  }

  const uploadMutation = useMutation({
    mutationFn: (file) => governanceSkillsApi.upload(type, file),
    onSuccess: (resp) => _onUploaded(resp, `uploaded — ${resp.rule_count} rule(s) parsed`),
    onError: (e) => onBanner('err', e?.response?.data?.detail || 'Upload failed'),
  })

  const bundleMutation = useMutation({
    mutationFn: (file) => governanceSkillsApi.uploadBundle(type, file),
    onSuccess: (resp) => _onUploaded(resp,
      `bundle uploaded — ${resp.file_count} file(s), ${resp.script_count} script(s)`
      + (resp.script_count ? '; run the smoke check before it can gate' : '')),
    onError: (e) => onBanner('err', e?.response?.data?.detail || 'Bundle upload failed'),
  })

  const smokeMutation = useMutation({
    mutationFn: (version) => governanceSkillsApi.smoke(type, version ?? active.version),
    onSuccess: (resp) => {
      qc.invalidateQueries({ queryKey: ['gov-skills'] })
      onBanner(resp.status === 'green' ? 'ok' : 'err',
        `Smoke ${resp.status}${resp.status !== 'green'
          ? ' — ' + (resp.scripts || []).filter(s => s.verdict !== 'ok')
              .map(s => `${s.script}: ${s.verdict}`).join('; ').slice(0, 160) : ''}`)
    },
    onError: (e) => onBanner('err', e?.response?.data?.detail || 'Smoke run failed'),
  })

  // Skill SLOTS (0118): every ENABLED slot executes in the stage; toggling
  // retires/reinstates a slot without touching the append-only audit rows.
  const toggleMutation = useMutation({
    mutationFn: ({ name, enabled }) => governanceSkillsApi.setSlotEnabled(type, name, enabled),
    onSuccess: (resp) => {
      qc.invalidateQueries({ queryKey: ['gov-skills'] })
      onBanner('ok', `Slot “${resp.name}” ${resp.enabled ? 'enabled' : 'disabled'}`
        + (resp.warning ? ` — ${resp.warning}` : ''))
    },
    onError: (e) => onBanner('err', e?.response?.data?.detail || 'Slot toggle failed'),
  })

  const onPickFile = (e) => {
    const file = e.target.files?.[0]
    if (file) uploadMutation.mutate(file)
    e.target.value = ''
  }

  const onPickBundle = (e) => {
    const file = e.target.files?.[0]
    if (file) bundleMutation.mutate(file)
    e.target.value = ''
  }

  return (
    <div style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)',
      borderRadius: 12, padding: '16px 20px', marginBottom: 16 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
        <span style={{ fontSize: 17 }}>{icon}</span>
        <strong style={{ fontSize: 15 }}>{title}</strong>
        {active ? (
          <>
            <span style={{ fontSize: 11, fontWeight: 600, padding: '2px 9px', borderRadius: 20,
              background: 'rgba(52,211,153,0.15)', color: '#16a34a' }}>
              {slots.length > 1
                ? `${slots.filter(s => s.enabled).length}/${slots.length} skill slot(s) enabled`
                : `v${active.version} active · ${active.rule_count} unit(s)`}
            </span>
            {slots.length <= 1 && <ModeChip mode={active.mode} />}
            {slots.length <= 1 && active.is_bundle && (
              <span style={{ fontSize: 10.5, fontWeight: 600, padding: '1px 8px', borderRadius: 20,
                border: '1px solid #a78bfa55', background: '#a78bfa18', color: '#a78bfa' }}>
                bundle · {active.file_count} file(s) · {active.script_count} script(s)
              </span>
            )}
            {slots.length <= 1 && <SmokeChip status={active.smoke_status} />}
          </>
        ) : (
          <span style={{ fontSize: 11, fontWeight: 600, padding: '2px 9px', borderRadius: 20,
            background: 'rgba(239,68,68,0.12)', color: '#ef4444' }}>
            {slots.length > 0
              ? `all ${slots.length} slot(s) disabled — enable one below to start`
              : 'not uploaded — reviews cannot start'}
          </span>
        )}
        <div style={{ flex: 1 }} />
        {active?.is_bundle && active?.script_count > 0 && active?.smoke_status !== 'green' && (
          <button onClick={() => smokeMutation.mutate()} disabled={smokeMutation.isPending} style={{
            display: 'inline-flex', alignItems: 'center', gap: 6, padding: '7px 14px', fontSize: 13,
            fontWeight: 600, background: '#f59e0b', color: '#fff', border: 'none',
            borderRadius: 6, cursor: smokeMutation.isPending ? 'wait' : 'pointer' }}>
            {smokeMutation.isPending
              ? <><Loader2 size={14} className="pp-spin" /> Running smoke…</>
              : <>Run smoke check</>}
          </button>
        )}
        <button onClick={() => bundleRef.current?.click()} disabled={bundleMutation.isPending} style={{
          display: 'inline-flex', alignItems: 'center', gap: 6, padding: '7px 14px', fontSize: 13,
          fontWeight: 600, background: 'var(--bg-elevated)', color: 'var(--text-primary)',
          border: '1px solid var(--border)', borderRadius: 6,
          cursor: bundleMutation.isPending ? 'wait' : 'pointer' }}>
          {bundleMutation.isPending
            ? <><Loader2 size={14} className="pp-spin" /> Uploading…</>
            : <><Upload size={14} /> Upload bundle (.zip/.tar.gz)</>}
        </button>
        <button onClick={() => fileRef.current?.click()} disabled={uploadMutation.isPending} style={{
          display: 'inline-flex', alignItems: 'center', gap: 6, padding: '7px 14px', fontSize: 13,
          fontWeight: 600, background: 'var(--accent)', color: '#fff', border: 'none',
          borderRadius: 6, cursor: uploadMutation.isPending ? 'wait' : 'pointer' }}>
          {uploadMutation.isPending
            ? <><Loader2 size={14} className="pp-spin" /> Uploading…</>
            : <><Upload size={14} /> Upload new version (.md)</>}
        </button>
        <input ref={fileRef} type="file" accept=".md,text/markdown,text/plain"
          onChange={onPickFile} style={{ display: 'none' }} />
        <input ref={bundleRef} type="file" accept=".zip,.tar.gz,.tgz,application/zip,application/gzip"
          onChange={onPickBundle} style={{ display: 'none' }} />
      </div>
      <p style={{ margin: '8px 0 0', fontSize: 12.5, color: 'var(--text-muted)' }}>{hint}</p>
      {active && (
        <div style={{ marginTop: 6, fontSize: 11.5, color: 'var(--text-muted)' }}>
          {active.name && <span>“{active.name}” · </span>}
          checksum <code style={{ fontSize: 10.5 }}>{String(active.checksum).slice(0, 12)}…</code>
          {active.uploaded_by && <span> · by {active.uploaded_by}</span>}
          {active.created_at && <span> · {new Date(active.created_at).toLocaleString()}</span>}
          {active.filename && <span> · {active.filename}</span>}
        </div>
      )}

      {/* Skill SLOTS (0118): a type holds several skills side by side — each
          upload lands in the slot named by its SKILL.md frontmatter, and EVERY
          enabled slot executes in the {type} review stage. */}
      {slots.length > 0 && (slots.length > 1 || !active) && (
        <div style={{ marginTop: 10, border: '1px solid var(--border)', borderRadius: 8, overflow: 'hidden' }}>
          {slots.map((s, i) => (
            <div key={s.name} style={{ display: 'flex', gap: 10, alignItems: 'center',
              padding: '8px 12px', fontSize: 12, opacity: s.enabled ? 1 : 0.55,
              borderBottom: i < slots.length - 1 ? '1px solid var(--border)' : 'none' }}>
              <strong style={{ minWidth: 150 }}>{s.name}</strong>
              <span style={{ color: 'var(--text-secondary)', flexShrink: 0 }}>
                v{s.version} · {s.rule_count} unit(s)
              </span>
              <ModeChip mode={s.mode} />
              {s.is_bundle && (
                <span style={{ fontSize: 10.5, fontWeight: 600, padding: '1px 8px', borderRadius: 20,
                  border: '1px solid #a78bfa55', background: '#a78bfa18', color: '#a78bfa' }}>
                  {s.script_count} script(s)
                </span>
              )}
              <SmokeChip status={s.smoke_status} />
              {!s.enabled && (
                <span style={{ fontSize: 10.5, fontWeight: 700, color: '#94a3b8' }}>disabled — does not run</span>
              )}
              <div style={{ flex: 1 }} />
              {s.is_bundle && s.script_count > 0 && s.smoke_status !== 'green' && s.enabled && (
                <button onClick={() => smokeMutation.mutate(s.version)} disabled={smokeMutation.isPending}
                  style={{ padding: '4px 10px', fontSize: 11.5, fontWeight: 600, background: '#f59e0b',
                    color: '#fff', border: 'none', borderRadius: 6,
                    cursor: smokeMutation.isPending ? 'wait' : 'pointer' }}>
                  Run smoke
                </button>
              )}
              <button onClick={() => toggleMutation.mutate({ name: s.name, enabled: !s.enabled })}
                disabled={toggleMutation.isPending}
                style={{ padding: '4px 10px', fontSize: 11.5, fontWeight: 600,
                  background: 'transparent', color: s.enabled ? '#ef4444' : '#16a34a',
                  border: `1px solid ${s.enabled ? '#ef444455' : '#16a34a55'}`, borderRadius: 6,
                  cursor: toggleMutation.isPending ? 'wait' : 'pointer' }}>
                {s.enabled ? 'Disable' : 'Enable'}
              </button>
            </div>
          ))}
        </div>
      )}

      {/* Parsed-rules confirmation from the last upload in this session */}
      {preview && (
        <details open style={{ marginTop: 10 }}>
          <summary style={{ fontSize: 12, color: 'var(--text-muted)', cursor: 'pointer' }}>
            Parsed enforceable units in v{preview.version} ({preview.rules.length}) — <ModeChip mode={preview.mode} />
          </summary>
          <ul style={{ margin: '6px 0 0', paddingLeft: 18, fontSize: 12.5, color: 'var(--text-secondary)' }}>
            {preview.rules.map(r => <li key={r.id}><code>{r.id}</code> — {r.title}</li>)}
          </ul>
        </details>
      )}

      {/* Version history (append-only audit trail) */}
      <button onClick={() => setShowHistory(s => !s)} style={{
        marginTop: 10, display: 'inline-flex', alignItems: 'center', gap: 5, padding: 0,
        background: 'none', border: 'none', fontSize: 12, color: 'var(--text-muted)', cursor: 'pointer' }}>
        {showHistory ? <ChevronUp size={13} /> : <ChevronDown size={13} />} Version history
      </button>
      {showHistory && (
        <div style={{ marginTop: 6, border: '1px solid var(--border)', borderRadius: 8, overflow: 'hidden' }}>
          {(history?.versions || []).map((v, i) => (
            <div key={v.version} style={{ display: 'flex', gap: 10, alignItems: 'baseline',
              padding: '7px 12px', fontSize: 12,
              borderBottom: i < (history.versions.length - 1) ? '1px solid var(--border)' : 'none' }}>
              <strong style={{ flexShrink: 0 }}>v{v.version}</strong>
              <span style={{ color: 'var(--text-secondary)' }}>{v.rule_count} rule(s)</span>
              <code style={{ fontSize: 10.5, color: 'var(--text-muted)' }}>{String(v.checksum).slice(0, 12)}…</code>
              <span style={{ marginLeft: 'auto', color: 'var(--text-muted)', flexShrink: 0 }}>
                {v.uploaded_by || ''}{v.created_at ? ` · ${new Date(v.created_at).toLocaleString()}` : ''}
              </span>
            </div>
          ))}
          {history && !history.versions?.length && (
            <div style={{ padding: '8px 12px', fontSize: 12, color: 'var(--text-muted)' }}>No versions yet.</div>
          )}
        </div>
      )}
    </div>
  )
}

export default function GovernanceSkills() {
  const [banner, setBanner] = useState(null)
  const { data: skills, isLoading } = useQuery({
    queryKey: ['gov-skills'],
    queryFn: () => governanceSkillsApi.list(),
  })

  const flashBanner = (kind, text) => {
    setBanner({ kind, text })
    setTimeout(() => setBanner(null), 4000)
  }

  return (
    <div style={{ padding: '20px 24px', maxWidth: 1100 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 6 }}>
        <ShieldCheck size={22} color="var(--accent)" />
        <h1 style={{ margin: 0, fontSize: 22, fontWeight: 700 }}>Governance Skills</h1>
      </div>
      <p style={{ margin: '0 0 18px 0', color: 'var(--text-muted)', fontSize: 13, maxWidth: 820 }}>
        The EA and InfoSec teams' authoritative review rulebooks. After a code change is
        approved, the EA review runs first, then InfoSec — each applies its skill rule-by-rule,
        fixes what it finds, and parks for approval before the fixes land. The standard
        SKILL.md shape (YAML frontmatter + markdown, as used by Claude Code / grok / Codex)
        works as-is: <code style={{ margin: '0 4px' }}>## RULE &lt;id&gt;: &lt;title&gt;</code>
        headings give the strongest per-rule enforcement, plain <code>## sections</code> each
        become one enforceable unit, and a heading-less document is enforced as a single unit.
        Only empty files and duplicate rule ids are rejected.
      </p>

      {banner && (
        <div style={{
          marginBottom: 14, padding: '8px 14px', borderRadius: 8,
          fontSize: 13, display: 'flex', alignItems: 'center', gap: 8,
          background: banner.kind === 'ok' ? 'rgba(16,185,129,0.10)' : 'rgba(239,68,68,0.10)',
          color: banner.kind === 'ok' ? '#10b981' : '#ef4444',
          border: `1px solid ${banner.kind === 'ok' ? 'rgba(16,185,129,0.30)' : 'rgba(239,68,68,0.30)'}`,
        }}>
          {banner.kind === 'ok' ? <CheckCircle size={14} /> : <AlertCircle size={14} />}
          {banner.text}
        </div>
      )}

      {isLoading ? (
        <div style={{ padding: 14, fontSize: 13, color: 'var(--text-muted)',
          display: 'flex', alignItems: 'center', gap: 8,
          border: '1px solid var(--border)', borderRadius: 8 }}>
          <Loader2 size={16} className="pp-spin" /> Loading…
        </div>
      ) : (
        TYPES.map(t => (
          <SkillSection key={t.key} type={t.key} title={t.title} icon={t.icon} hint={t.hint}
            active={skills?.[t.key]} slots={skills?.[`${t.key}_skills`] || []}
            onBanner={flashBanner} />
        ))
      )}
    </div>
  )
}
