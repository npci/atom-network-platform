// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useEffect, useMemo, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Braces, Database, Search, ChevronRight, ChevronDown, Pencil, Check, X,
  DownloadCloud, FileCode2, AlertTriangle, Code2, Regex,
} from 'lucide-react'
import { apiRegistryApi } from '../../services/api'

const cellStyle = {
  padding: '6px 10px', fontSize: '12px', color: 'var(--text-primary)',
  borderBottom: '1px solid var(--border-subtle)', verticalAlign: 'top',
}
const inputStyle = {
  width: '100%', padding: '4px 6px', background: 'var(--bg-input)',
  border: '1px solid var(--border)', borderRadius: '4px',
  color: 'var(--text-primary)', fontSize: '12px', outline: 'none',
}
const btnSecondary = {
  display: 'inline-flex', alignItems: 'center', gap: '6px', padding: '7px 12px',
  background: 'var(--bg-card)', border: '1px solid var(--border)', borderRadius: '6px',
  color: 'var(--text-secondary)', fontSize: '12px', fontWeight: 500, cursor: 'pointer',
}
const btnPrimary = {
  ...btnSecondary, background: 'var(--accent)', border: 'none', color: 'white',
}

function Badge({ color, children, title }) {
  return (
    <span title={title} style={{
      display: 'inline-block', padding: '1px 6px', borderRadius: '9px',
      fontSize: '10px', fontWeight: 600, background: `color-mix(in srgb, ${color} 15%, transparent)`,
      color, marginLeft: '4px', whiteSpace: 'nowrap',
    }}>{children}</span>
  )
}

// Human hint for the most common invalid-regex mistakes (e.g. a bare `*`).
function regexHint(errMsg) {
  if (/nothing to repeat/i.test(errMsg))
    return 'A quantifier (* + ? {…}) must follow a character or group — “*” alone is invalid. For “any characters”, use .* or .+'
  if (/unterminated|unmatched|missing|incomplete/i.test(errMsg))
    return 'Looks like an unclosed group “(” or character class “[”.'
  if (/invalid group|invalid flags/i.test(errMsg))
    return 'Check the group syntax / flags (valid flags: g i m s u y).'
  return null
}

// Standalone regex utility — one per page, not per row. Validates a pattern
// (compile is always safe) and, off the main thread, tests it against a sample
// so a catastrophic-backtracking regex can't freeze the tab.
function RegexTester() {
  const [pattern, setPattern] = useState('')
  const [flags, setFlags] = useState('')
  const [sample, setSample] = useState('')
  const [match, setMatch] = useState(null)   // {kind:'match'|'nomatch'|'timeout'|'error', text?}

  const validation = useMemo(() => {
    if (!pattern) return null
    try { new RegExp(pattern, flags); return { valid: true } }
    catch (e) { return { valid: false, error: e.message, hint: regexHint(e.message) } }
  }, [pattern, flags])

  useEffect(() => {
    if (!pattern || !sample || !validation?.valid) { setMatch(null); return }
    const worker = new Worker(new URL('./regexTester.worker.js', import.meta.url), { type: 'module' })
    let done = false
    const timer = setTimeout(() => {
      if (done) return
      done = true; worker.terminate(); setMatch({ kind: 'timeout' })
    }, 400)
    worker.onmessage = (e) => {
      if (done) return
      done = true; clearTimeout(timer); worker.terminate()
      const d = e.data
      if (!d.ok) setMatch({ kind: 'error', text: d.error })
      else setMatch(d.matched ? { kind: 'match', text: d.matchText } : { kind: 'nomatch' })
    }
    worker.postMessage({ pattern, flags, sample })
    return () => { clearTimeout(timer); worker.terminate() }
  }, [pattern, flags, sample, validation])

  const validColor = validation == null ? 'var(--text-muted)'
    : validation.valid ? 'var(--success)' : 'var(--danger)'
  const matchLine = !match ? null
    : match.kind === 'match' ? { c: 'var(--success)', t: `✓ matches${match.text ? ` — “${match.text}”` : ''}` }
    : match.kind === 'nomatch' ? { c: 'var(--danger)', t: '✗ no match' }
    : match.kind === 'timeout' ? { c: 'var(--warning, #b8860b)', t: '⏱ regex too slow — possible catastrophic backtracking' }
    : { c: 'var(--danger)', t: `error: ${match.text}` }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', padding: '12px 14px',
                  background: 'var(--bg-card)', border: '1px solid var(--border)', borderRadius: '8px',
                  marginBottom: 'var(--space-4)' }}>
      <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
        <input style={{ ...inputStyle, fontFamily: 'monospace', flex: 1 }} value={pattern}
               onChange={(e) => setPattern(e.target.value)}
               placeholder="Regex pattern, e.g. ^[A-Z0-9]{1,35}$" />
        <input style={{ ...inputStyle, fontFamily: 'monospace', width: '70px' }} value={flags}
               onChange={(e) => setFlags(e.target.value)} placeholder="flags" title="e.g. i g m s" />
      </div>
      {validation && (
        <div style={{ fontSize: '11px', color: validColor }}>
          {validation.valid ? '✓ Valid regex' : `✗ Invalid regex: ${validation.error}`}
          {validation.hint && (
            <div style={{ color: 'var(--text-secondary)', marginTop: '2px' }}>{validation.hint}</div>
          )}
        </div>
      )}
      <input style={inputStyle} value={sample} onChange={(e) => setSample(e.target.value)}
             placeholder="Test string (optional) — checked off-thread, safe against slow patterns" />
      {matchLine && <span style={{ fontSize: '11px', color: matchLine.c }}>{matchLine.t}</span>}
    </div>
  )
}

function FieldRow({ field, onSave, saving }) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState({})

  const startEdit = () => {
    setDraft({
      message_item: field.message_item || '',
      occurrence: field.occurrence || '',
      datatype: field.datatype || '',
      length_rule: field.length_rule || '',
      mandatory: field.mandatory || '',
      rules_ref: field.rules_ref || '',
      condition_text: field.condition_text || '',
      pattern_rule: field.pattern_rule || '',
    })
    setEditing(true)
  }
  const save = async () => {
    await onSave(field.id, draft)
    setEditing(false)
  }

  const indent = Math.max(0, field.depth) * 14
  const tagDisplay = field.is_attribute ? field.xml_tag : `<${field.xml_tag}>`
  const conflictText = field.has_conflict
    ? (field.constraint_sources?.code?.evidence || [])
        .map((e) => e.conflict_with_xsd).filter(Boolean).join('; ')
    : ''

  return (
    <tr style={{ background: editing ? 'var(--accent-subtle)' : 'transparent' }}>
      <td style={{ ...cellStyle, whiteSpace: 'nowrap', fontWeight: 600 }}>{field.tag_num}</td>
      <td style={{ ...cellStyle, minWidth: '180px' }}>
        {editing
          ? <input style={inputStyle} value={draft.message_item}
                   onChange={(e) => setDraft({ ...draft, message_item: e.target.value })}
                   placeholder="Description of the item" />
          : (field.message_item || <span style={{ color: 'var(--text-muted)' }}>—</span>)}
      </td>
      <td style={{ ...cellStyle, whiteSpace: 'nowrap' }}>
        <span className="id-mono" style={{ paddingLeft: `${indent}px`, fontSize: '12px' }}>
          {tagDisplay}
        </span>
        {field.edited && <Badge color="var(--accent)" title={`Edited by ${field.updated_by}`}>edited</Badge>}
        {field.has_code_evidence && (
          <Badge color="var(--success)" title="Code-constraint evidence attached (tier-1 harvest)">
            <Code2 size={9} style={{ verticalAlign: '-1px' }} /> code
          </Badge>
        )}
        {field.has_conflict && (
          <Badge color="var(--danger)" title={conflictText}>
            <AlertTriangle size={9} style={{ verticalAlign: '-1px' }} /> conflict
          </Badge>
        )}
      </td>
      <td style={cellStyle}>
        {editing
          ? <input style={{ ...inputStyle, width: '54px' }} value={draft.occurrence}
                   onChange={(e) => setDraft({ ...draft, occurrence: e.target.value })} />
          : field.occurrence}
      </td>
      <td style={cellStyle}>
        {editing
          ? <input style={{ ...inputStyle, width: '90px' }} value={draft.datatype}
                   onChange={(e) => setDraft({ ...draft, datatype: e.target.value })} />
          : field.datatype}
      </td>
      <td style={{ ...cellStyle, minWidth: '120px' }}>
        {editing
          ? <input style={inputStyle} value={draft.length_rule}
                   onChange={(e) => setDraft({ ...draft, length_rule: e.target.value })}
                   placeholder="e.g. Min Length : 1 Max Length : 35" />
          : field.length_rule}
      </td>
      <td style={{ ...cellStyle, textAlign: 'center' }}>
        {editing
          ? (
            <select style={{ ...inputStyle, width: '48px' }} value={draft.mandatory}
                    onChange={(e) => setDraft({ ...draft, mandatory: e.target.value })}>
              <option value="">—</option>
              <option value="Y">Y</option>
              <option value="N">N</option>
              <option value="C">C</option>
            </select>
          )
          : field.mandatory}
      </td>
      <td style={{ ...cellStyle, minWidth: '160px' }}>
        {editing
          ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
              <input style={inputStyle} value={draft.rules_ref}
                     onChange={(e) => setDraft({ ...draft, rules_ref: e.target.value })}
                     placeholder="Rule ref, e.g. 019_Head_Version" />
              <input style={inputStyle} value={draft.condition_text}
                     onChange={(e) => setDraft({ ...draft, condition_text: e.target.value })}
                     placeholder="Condition (when Mandatory = C)" />
              <input style={{ ...inputStyle, fontFamily: 'monospace' }} value={draft.pattern_rule}
                     onChange={(e) => setDraft({ ...draft, pattern_rule: e.target.value })}
                     placeholder="Validation regex, e.g. ^[A-Z0-9]{1,35}$" />
              {field.xsd_pattern && (
                <span style={{ fontSize: '10px', color: 'var(--text-muted)' }} title="Pattern facet from the XSD">
                  XSD pattern: <span className="id-mono">{field.xsd_pattern}</span>
                </span>
              )}
            </div>
          )
          : (
            <>
              {field.rules_ref && <div>{field.rules_ref}</div>}
              {field.condition_text && (
                <div style={{ color: 'var(--text-secondary)' }}>{field.condition_text}</div>
              )}
              {field.pattern_rule && (
                <div className="id-mono" style={{ color: 'var(--text-muted)', fontSize: '11px' }}
                     title="Manual validation regex (tier-3)">/{field.pattern_rule}/</div>
              )}
              {field.enum_values?.length > 0 && (
                <div style={{ color: 'var(--text-muted)', fontSize: '11px' }}
                     title={field.enum_values.join(' | ')}>
                  {field.enum_values.slice(0, 4).join('|')}{field.enum_values.length > 4 ? '|..' : ''}
                </div>
              )}
            </>
          )}
      </td>
      <td style={{ ...cellStyle, whiteSpace: 'nowrap' }}>
        {editing ? (
          <span style={{ display: 'inline-flex', gap: '4px' }}>
            <button style={{ ...btnSecondary, padding: '4px 6px' }} onClick={save} disabled={saving} title="Save">
              <Check size={13} color="var(--success)" />
            </button>
            <button style={{ ...btnSecondary, padding: '4px 6px' }} onClick={() => setEditing(false)} title="Cancel">
              <X size={13} />
            </button>
          </span>
        ) : (
          <button style={{ ...btnSecondary, padding: '4px 6px' }} onClick={startEdit} title="Edit constraints">
            <Pencil size={13} />
          </button>
        )}
      </td>
    </tr>
  )
}

export default function ApiRegistry() {
  const qc = useQueryClient()
  const [selectedId, setSelectedId] = useState(null)
  const [search, setSearch] = useState('')
  const [showXml, setShowXml] = useState(false)
  const [showRegexTester, setShowRegexTester] = useState(false)
  const [banner, setBanner] = useState(null)
  const [editingDesc, setEditingDesc] = useState(false)
  const [descDraft, setDescDraft] = useState('')

  const flash = (kind, text) => {
    setBanner({ kind, text })
    setTimeout(() => setBanner(null), 6000)
  }

  const { data: listData, isLoading: listLoading } = useQuery({
    queryKey: ['api-registry-messages'],
    queryFn: () => apiRegistryApi.listMessages().then((r) => r.data),
  })
  const messages = listData?.messages || []

  const { data: detail, isLoading: detailLoading } = useQuery({
    queryKey: ['api-registry-message', selectedId],
    queryFn: () => apiRegistryApi.getMessage(selectedId).then((r) => r.data),
    enabled: !!selectedId,
  })

  const ingestMutation = useMutation({
    mutationFn: () => apiRegistryApi.ingest(),
    onSuccess: (r) => {
      const d = r.data
      flash('ok', `Populated from ${d.xsd_dir}: ${d.messages_total} messages `
        + `(${d.messages_created} new, ${d.messages_updated} refreshed) · fields: `
        + `${d.fields_created} created, ${d.fields_updated} updated, ${d.fields_removed} removed, `
        + `${d.human_locked_skipped} human-edited preserved.`)
      qc.invalidateQueries({ queryKey: ['api-registry-messages'] })
      qc.invalidateQueries({ queryKey: ['api-registry-message'] })
    },
    onError: (e) => flash('err', e?.response?.data?.detail || 'Ingest failed'),
  })

  const descMutation = useMutation({
    mutationFn: () => apiRegistryApi.patchMessage(selectedId, { description: descDraft }),
    onSuccess: (r) => {
      setEditingDesc(false)
      qc.invalidateQueries({ queryKey: ['api-registry-message', selectedId] })
      flash('ok', r.data?.changed
        ? 'Description saved — it renders under the API heading in generated TSDs.'
        : 'No changes to save.')
    },
    onError: (e) => flash('err', e?.response?.data?.detail || 'Save failed (admin required)'),
  })

  const harvestMutation = useMutation({
    mutationFn: () => apiRegistryApi.harvestCode(),
    onSuccess: (r) => {
      const d = r.data
      flash('ok', `Code harvest from ${d.java_dir}: ${d.annotations_found} annotations, `
        + `${d.fields_updated} fields annotated, ${d.conflicts} conflict(s) flagged.`)
      qc.invalidateQueries({ queryKey: ['api-registry-message'] })
    },
    onError: (e) => flash('err', e?.response?.data?.detail || 'Code harvest failed'),
  })

  const { data: prodSource } = useQuery({
    queryKey: ['api-registry-production-source'],
    queryFn: () => apiRegistryApi.getProductionSource().then((r) => r.data),
  })

  const prodSourceMutation = useMutation({
    mutationFn: ({ repoId, gitlabRepo }) => apiRegistryApi.setProductionSource(repoId, gitlabRepo),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['api-registry-production-source'] })
      flash('ok', 'Production branch saved — XSD ingest and code harvest read every selected baseline.')
    },
    onError: (e) => flash('err', e?.response?.data?.detail || 'Failed to set production branch (admin required)'),
  })

  // One picker per repo (core + app both get a production branch).
  const repoGroups = useMemo(() => {
    const g = new Map()
    for (const r of (prodSource?.repos || [])) {
      if (!g.has(r.gitlab_repo)) g.set(r.gitlab_repo, [])
      g.get(r.gitlab_repo).push(r)
    }
    return [...g.entries()]
  }, [prodSource])

  const fieldMutation = useMutation({
    mutationFn: ({ fieldId, data }) => apiRegistryApi.patchField(fieldId, data),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ['api-registry-message', selectedId] })
      flash('ok', r.data.edited
        ? 'Constraint saved — this row is now locked against ingest overwrite.'
        : 'No changes to save — row left unlocked.')
    },
    onError: (e) => flash('err', e?.response?.data?.detail || 'Save failed (admin required)'),
  })

  const saveField = (fieldId, draft) => {
    // Send edited cells verbatim: empty string is the explicit "clear this cell"
    // signal server-side; absent keys mean "no change".
    return fieldMutation.mutateAsync({ fieldId, data: { ...draft } })
  }

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    if (!q) return messages
    return messages.filter((m) => m.api_name.toLowerCase().includes(q))
  }, [messages, search])

  const grouped = useMemo(() => {
    const g = { request: [], response: [], other: [] }
    filtered.forEach((m) => { (g[m.direction] || g.other).push(m) })
    return g
  }, [filtered])

  return (
    <div style={{ padding: 'var(--space-7)', maxWidth: '1400px' }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 'var(--space-5)' }}>
        <div>
          <h1 style={{ display: 'flex', alignItems: 'center', gap: '10px', fontSize: '20px', fontWeight: 700, color: 'var(--text-primary)', margin: 0 }}>
            <Braces size={20} style={{ color: 'var(--accent)' }} /> API Registry
          </h1>
          <p style={{ color: 'var(--text-secondary)', fontSize: '13px', marginTop: '6px', maxWidth: '640px' }}>
            Canonical network wire-API field constraints. TSD interface specifications are rendered
            verbatim from these rows — edit a constraint here and every future TSD uses it.
            Human edits are locked against re-ingest overwrites.
          </p>
        </div>
        <div style={{ display: 'flex', gap: '8px' }}>
          <button style={{ ...btnSecondary, ...(showRegexTester ? { borderColor: 'var(--accent)', color: 'var(--accent)' } : {}) }}
                  onClick={() => setShowRegexTester((v) => !v)} title="Validate & test a regex pattern">
            <Regex size={14} />
            Regex tester
          </button>
          <button style={btnSecondary} onClick={() => harvestMutation.mutate()}
                  disabled={harvestMutation.isPending}>
            <FileCode2 size={14} />
            {harvestMutation.isPending ? 'Harvesting…' : 'Harvest Code Constraints'}
          </button>
          <button style={btnPrimary} onClick={() => ingestMutation.mutate()}
                  disabled={ingestMutation.isPending}>
            <DownloadCloud size={14} />
            {ingestMutation.isPending ? 'Populating…' : 'Populate from XSDs'}
          </button>
        </div>
      </div>

      {banner && (
        <div style={{
          padding: '10px 14px', borderRadius: '8px', fontSize: '13px', marginBottom: 'var(--space-4)',
          background: banner.kind === 'ok' ? 'color-mix(in srgb, var(--success) 12%, transparent)'
                                           : 'color-mix(in srgb, var(--danger) 12%, transparent)',
          color: banner.kind === 'ok' ? 'var(--success)' : 'var(--danger)',
          border: `1px solid ${banner.kind === 'ok' ? 'var(--success)' : 'var(--danger)'}`,
        }}>{banner.text}</div>
      )}

      {showRegexTester && <RegexTester />}

      {/* Production baseline sources — one branch picker per repo. */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '14px', flexWrap: 'wrap',
                    marginBottom: 'var(--space-4)', padding: '8px 12px', background: 'var(--bg-card)',
                    border: '1px solid var(--border)', borderRadius: '8px', fontSize: '13px' }}>
        <span style={{ color: 'var(--text-secondary)', whiteSpace: 'nowrap' }}>Production branches:</span>
        {repoGroups.map(([repo, rows]) => (
          <label key={repo} style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
            <span className="id-mono" style={{ fontSize: '12px' }}>{repo.split('/').pop()}</span>
            {rows[0].role && (
              <Badge color={rows[0].role === 'core' ? 'var(--accent)' : 'var(--success)'}
                     title={rows[0].role === 'core' ? 'Holds the XSD schemas' : 'Application / validation code'}>
                {rows[0].role}
              </Badge>
            )}
            <select
              value={rows.find((r) => r.selected)?.id || ''}
              onChange={(e) => prodSourceMutation.mutate({ repoId: e.target.value || null, gitlabRepo: repo })}
              disabled={prodSourceMutation.isPending}
              style={{ ...inputStyle, width: 'auto', maxWidth: '240px', cursor: 'pointer' }}
            >
              <option value="">— none —</option>
              {rows.map((r) => (
                <option key={r.id} value={r.id} disabled={!r.indexed}>
                  {r.gitlab_branch}{r.indexed ? '' : '  (not indexed)'}
                </option>
              ))}
            </select>
          </label>
        ))}
        <span style={{ color: 'var(--text-muted)', fontSize: '11px' }}>
          ingest + harvest read every selected baseline (XSDs live in core; validation code in core and app)
        </span>
      </div>

      <div style={{ display: 'flex', gap: 'var(--space-5)', alignItems: 'flex-start' }}>
        {/* ── Message list ── */}
        <div style={{ width: '270px', flexShrink: 0, background: 'var(--bg-card)', border: '1px solid var(--border)', borderRadius: '10px', overflow: 'hidden' }}>
          <div style={{ padding: '10px', borderBottom: '1px solid var(--border-subtle)', position: 'relative' }}>
            <Search size={13} style={{ position: 'absolute', left: '18px', top: '18px', color: 'var(--text-muted)' }} />
            <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search APIs…"
                   style={{ ...inputStyle, padding: '6px 8px 6px 26px' }} />
          </div>
          <div style={{ maxHeight: '70vh', overflowY: 'auto' }}>
            {listLoading && <div style={{ padding: '14px', fontSize: '12px', color: 'var(--text-muted)' }}>Loading…</div>}
            {!listLoading && messages.length === 0 && (
              <div style={{ padding: '16px', fontSize: '12px', color: 'var(--text-muted)' }}>
                Registry is empty. Click <b>Populate from XSDs</b> to run the deterministic
                initial ingest from the platform schemas.
              </div>
            )}
            {['request', 'response', 'other'].map((dir) => grouped[dir].length > 0 && (
              <div key={dir}>
                <div style={{ padding: '7px 12px 3px', fontSize: '10px', fontWeight: 700, letterSpacing: '0.06em', textTransform: 'uppercase', color: 'var(--text-muted)' }}>
                  {dir === 'request' ? 'Requests' : dir === 'response' ? 'Responses' : 'Other'}
                </div>
                {grouped[dir].map((m) => (
                  <button key={m.id} onClick={() => { setSelectedId(m.id); setShowXml(false); setEditingDesc(false) }}
                          style={{
                            display: 'flex', width: '100%', alignItems: 'center', justifyContent: 'space-between',
                            padding: '7px 12px', background: m.id === selectedId ? 'var(--accent-subtle)' : 'transparent',
                            border: 'none', borderLeft: m.id === selectedId ? '3px solid var(--accent)' : '3px solid transparent',
                            cursor: 'pointer', color: 'var(--text-primary)', fontSize: '13px', textAlign: 'left',
                          }}>
                    <span className="id-mono" style={{ fontSize: '12px' }}>{m.api_name}</span>
                    <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>{m.field_count}</span>
                  </button>
                ))}
              </div>
            ))}
          </div>
        </div>

        {/* ── Detail ── */}
        <div style={{ flex: 1, minWidth: 0 }}>
          {!selectedId && (
            <div style={{ padding: '48px', textAlign: 'center', color: 'var(--text-muted)', background: 'var(--bg-card)', border: '1px dashed var(--border)', borderRadius: '10px' }}>
              <Database size={28} style={{ marginBottom: '10px', opacity: 0.5 }} />
              <div style={{ fontSize: '14px' }}>Select an API to view its full specification.</div>
            </div>
          )}
          {selectedId && detailLoading && (
            <div style={{ padding: '32px', color: 'var(--text-muted)', fontSize: '13px' }}>Loading specification…</div>
          )}
          {selectedId && detail && (
            <div style={{ background: 'var(--bg-card)', border: '1px solid var(--border)', borderRadius: '10px', overflow: 'hidden' }}>
              <div style={{ padding: '14px 16px', borderBottom: '1px solid var(--border-subtle)' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                  <span className="id-mono" style={{ fontSize: '16px', fontWeight: 700, color: 'var(--text-primary)' }}>
                    {detail.api_name}
                  </span>
                  <Badge color="var(--accent)">{detail.direction}</Badge>
                  <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>{detail.namespace}</span>
                </div>
                {detail.source_schema_path && (
                  <div style={{ fontSize: '11px', color: 'var(--text-muted)', marginTop: '4px' }}>
                    Source schema: <span className="id-mono">{detail.source_schema_path.split('/').slice(-1)[0]}</span>
                    {' · '}provenance: {detail.source}
                  </div>
                )}
                <div style={{ marginTop: '8px', display: 'flex', gap: '8px', alignItems: 'flex-start' }}>
                  {editingDesc ? (
                    <>
                      <textarea value={descDraft} onChange={(e) => setDescDraft(e.target.value)} rows={2}
                                style={{ ...inputStyle, resize: 'vertical' }}
                                placeholder="Message description (rendered under the API heading in generated TSDs)" />
                      <button style={{ ...btnSecondary, padding: '4px 6px' }} title="Save description"
                              disabled={descMutation.isPending} onClick={() => descMutation.mutate()}>
                        <Check size={13} color="var(--success)" />
                      </button>
                      <button style={{ ...btnSecondary, padding: '4px 6px' }} title="Cancel"
                              onClick={() => setEditingDesc(false)}>
                        <X size={13} />
                      </button>
                    </>
                  ) : (
                    <>
                      <span style={{ fontSize: '12px', color: detail.description ? 'var(--text-secondary)' : 'var(--text-muted)' }}>
                        {detail.description || 'No description yet — it renders under the API heading in generated TSDs.'}
                      </span>
                      <button style={{ ...btnSecondary, padding: '3px 5px' }} title="Edit description"
                              onClick={() => { setDescDraft(detail.description || ''); setEditingDesc(true) }}>
                        <Pencil size={12} />
                      </button>
                    </>
                  )}
                </div>
              </div>

              {detail.sample_xml && (
                <div style={{ borderBottom: '1px solid var(--border-subtle)' }}>
                  <button onClick={() => setShowXml(!showXml)}
                          style={{ ...btnSecondary, border: 'none', width: '100%', borderRadius: 0, justifyContent: 'flex-start', padding: '9px 16px' }}>
                    {showXml ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                    Message XML (deterministic, from schema)
                  </button>
                  {showXml && (
                    <pre className="id-mono" style={{ margin: 0, padding: '12px 16px', fontSize: '11px', lineHeight: 1.5, overflowX: 'auto', background: 'var(--bg-elevated)', color: 'var(--text-primary)', maxHeight: '320px', overflowY: 'auto' }}>
                      {detail.sample_xml}
                    </pre>
                  )}
                </div>
              )}

              {/* Own scroll box (both axes) so the horizontal scrollbar is reachable at
                  any vertical position — not stranded at the bottom of a 200+ row table.
                  Sticky header pins to the top of THIS box. */}
              <div style={{ overflow: 'auto', maxHeight: 'calc(100vh - 230px)' }}>
                <table style={{ width: '100%', borderCollapse: 'collapse', minWidth: '900px' }}>
                  <thead>
                    <tr>
                      {['Tag Num', 'Message Item', '<XMLTag>', 'Occurrence', 'Datatype', 'Length', 'M', 'Rules / Condition / Values', ''].map((h) => (
                        <th key={h} style={{ ...cellStyle, position: 'sticky', top: 0, background: 'var(--bg-elevated)', fontWeight: 600, fontSize: '11px', color: 'var(--text-secondary)', textAlign: 'left', whiteSpace: 'nowrap' }}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {detail.fields.map((f) => (
                      <FieldRow key={f.id} field={f} onSave={saveField} saving={fieldMutation.isPending} />
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
