// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useEffect, useMemo, useState, useRef } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Search, Upload, Loader, Paperclip, Package, AlertCircle, X, ChevronRight,
} from 'lucide-react'
import { changesApi, phaseCApi } from '../services/api'
import { kitDocLabel, formatKB } from '../lib/kitLabels'

// Phase-state palette — mirrors Dashboard's PhaseChip so the two surfaces
// read as one system. Kept local (small, one consumer) rather than
// promoted to a shared component until a third caller shows up.
const PHASE_STATE_STYLES = {
  not_started: { bg: 'rgba(154,154,150,0.12)', color: '#9a9a96', border: 'rgba(154,154,150,0.25)' },
  in_progress: { bg: 'rgba(218,119,86,0.12)',  color: '#da7756', border: 'rgba(218,119,86,0.3)' },
  completed:   { bg: 'rgba(76,175,125,0.15)',  color: '#4caf7d', border: 'rgba(76,175,125,0.3)' },
  blocked:     { bg: 'rgba(224,108,108,0.12)', color: '#e06c6c', border: 'rgba(224,108,108,0.3)' },
}

const STATE_OPTIONS = [
  { value: 'all',         label: 'All statuses' },
  { value: 'in_progress', label: 'In progress' },
  { value: 'completed',   label: 'Completed' },
  { value: 'blocked',     label: 'Blocked' },
  { value: 'not_started', label: 'Not started' },
]

// Roll the three phase summaries into ONE badge for the list row — the
// list is about "which change do I want to open," not per-phase status.
// Prefer blocked > in_progress > completed > not_started (loudest first)
// so a stuck change stands out at a glance.
function overallState(change) {
  const states = ['blocked', 'in_progress', 'completed', 'not_started']
  const seen = new Set([
    change?.phase_a?.state,
    change?.phase_b?.state,
    change?.phase_c?.state,
  ])
  for (const s of states) if (seen.has(s)) return s
  return 'not_started'
}

function OverallChip({ change }) {
  const state = overallState(change)
  const style = PHASE_STATE_STYLES[state]
  const label = STATE_OPTIONS.find(o => o.value === state)?.label || state
  return (
    <span
      title={`Phase A: ${change.phase_a?.label || '—'} · Phase B: ${change.phase_b?.label || '—'} · Phase C: ${change.phase_c?.label || '—'}`}
      style={{
        display: 'inline-flex', alignItems: 'center',
        padding: '2px 8px', borderRadius: '10px',
        fontSize: '10px', fontWeight: 500,
        background: style.bg, color: style.color,
        border: `1px solid ${style.border}`, whiteSpace: 'nowrap',
      }}
    >
      {label}
    </span>
  )
}

export default function ProductKitManager() {
  const { id: routeId } = useParams()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [search, setSearch] = useState('')
  const [stateFilter, setStateFilter] = useState('all')
  const [uploadingDocType, setUploadingDocType] = useState(null)
  const [error, setError] = useState(null)
  const fileInputRef = useRef(null)

  const { data: changesData, isLoading: changesLoading } = useQuery({
    queryKey: ['changes'],
    queryFn: () => changesApi.list({ limit: 200 }).then(r => r.data),
    refetchInterval: 15000,
    refetchOnWindowFocus: true,
  })

  // Base gate: only Phase-A-complete changes have a kit worth overriding.
  // change.status === 'completed' is the last stage in the Phase A pipeline
  // (see STAGES in ChangeDetail.jsx:168) — everything earlier means BRD/TSD/
  // XSD/product-kit are still being generated, so there's nothing to ship yet.
  const phaseACompleteChanges = useMemo(
    () => (changesData?.items || []).filter(c => c.status === 'completed'),
    [changesData],
  )

  const filteredChanges = useMemo(() => {
    const q = search.trim().toLowerCase()
    return phaseACompleteChanges.filter(c => {
      if (stateFilter !== 'all' && overallState(c) !== stateFilter) return false
      if (!q) return true
      return (c.title || '').toLowerCase().includes(q)
        || (c.initial_prompt || '').toLowerCase().includes(q)
        || String(c.id).toLowerCase().includes(q)
    })
  }, [phaseACompleteChanges, search, stateFilter])

  // Selected change — deep-link wins; otherwise pin to the first filtered row ONCE.
  //
  // `filteredChanges[0]` is not a selection, it is a rolling answer to "what sorts
  // first right now" — and it is recomputed on every search keystroke and on every
  // 15s refetch of a created_at-desc list. Deriving the selection from it meant the
  // right pane hopped between changes as you narrowed the search, and a newly
  // Phase-A-complete change could take the pane over while you were looking at it.
  // That pane is where files that ship to partners get uploaded, so it has to stay
  // where it was put. Clicking a row navigates (setting routeId), so this latch only
  // governs the first render of a bare /product-kit visit.
  const [autoPinnedId, setAutoPinnedId] = useState(null)
  useEffect(() => {
    if (!routeId && !autoPinnedId && filteredChanges.length) {
      setAutoPinnedId(filteredChanges[0].id)
    }
  }, [routeId, autoPinnedId, filteredChanges])

  const selectedId = routeId || autoPinnedId || null
  // Resolved against the FULL list, not the filtered one, so narrowing the search
  // does not blank out the pane for the change already open.
  const changeFromList = (changesData?.items || []).find(c => c.id === selectedId) || null

  // A deep link must NOT depend on the change being in the list above. That list is
  // capped (limit: 200) and role-scoped, so /product-kit/:id for an older change —
  // exactly what PhaseC's "Manage overrides in Product Kit →" link produces — resolved
  // to null and fell through to the generic "Select a change on the left" prompt, after
  // the user had already picked one. Fetch it by id when the list doesn't carry it.
  const { data: fetchedChange, isLoading: fetchingChange, error: fetchChangeError } = useQuery({
    queryKey: ['change', selectedId],
    queryFn: () => changesApi.get(selectedId).then(r => r.data),
    enabled: !!routeId && !changeFromList && !changesLoading,
    retry: false,
  })
  const selectedChange = changeFromList || fetchedChange || null
  const selectedIsPhaseAComplete = selectedChange?.status === 'completed'

  const { data: manifest, isLoading: manifestLoading, error: manifestError } = useQuery({
    queryKey: ['ship-manifest', selectedId],
    queryFn: () => phaseCApi.shipManifest(selectedId).then(r => r.data),
    // Don't hit the ship-manifest endpoint for a change whose Phase A isn't done
    // yet — the kit tables are empty and the response would be a misleading
    // "no items" that the user could mistake for "kit is empty and ready".
    enabled: !!selectedId && selectedIsPhaseAComplete,
    refetchOnWindowFocus: false,
    retry: false,        // a 403 is a verdict, not a blip — don't retry it three times
  })

  // Why the manifest can fail even though the change is listed: GET /changes lets the
  // read-all roles (admin + the review team) see EVERY change, while ship-manifest and
  // ship-override admit only ADMIN or change.created_by (change_requests.py). So a
  // reviewer opening someone else's change gets a 403 here — and rendering that as the
  // empty-state told them "no kit items yet" instead of "not yours to manage".
  const manifestStatus = manifestError?.response?.status
  const manifestErrorText = manifestStatus === 403
    ? 'Only this change\'s creator (or an admin) can manage its product-kit overrides.'
    : manifestError
      ? (manifestError.response?.data?.detail || 'Could not load this change\'s kit items.')
      : null

  const invalidateManifest = () => qc.invalidateQueries({ queryKey: ['ship-manifest', selectedId] })

  const handleUpload = async (docType, file) => {
    if (!file || !selectedId) return
    setUploadingDocType(docType); setError(null)
    try {
      await phaseCApi.uploadShipOverride(selectedId, docType, file)
      invalidateManifest()
    } catch (e) {
      setError(e.response?.data?.detail || 'Upload failed')
    } finally {
      setUploadingDocType(null)
    }
  }

  const handleClear = async (docType) => {
    if (!selectedId) return
    setError(null)
    try {
      await phaseCApi.clearShipOverride(selectedId, docType)
      invalidateManifest()
    } catch (e) {
      setError(e.response?.data?.detail || 'Clear failed')
    }
  }

  const items = manifest?.items || []
  const overrideCount = items.filter(it => !!it.override).length

  return (
    <div style={{ display: 'flex', height: 'calc(100vh - 0px)', overflow: 'hidden' }}>
      {/* ── Left list ────────────────────────────────────────────────── */}
      <aside style={{
        width: 320, flexShrink: 0, height: '100%',
        borderRight: '1px solid var(--border)',
        background: 'var(--bg-base)',
        display: 'flex', flexDirection: 'column',
      }}>
        <div style={{ padding: '16px 16px 10px', borderBottom: '1px solid var(--border-subtle)' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
            <Package size={16} style={{ color: 'var(--accent)' }} />
            <h2 style={{ margin: 0, fontSize: 14, fontWeight: 600, color: 'var(--text-primary)' }}>Product Kit</h2>
          </div>
          <div style={{ position: 'relative', marginBottom: 8 }}>
            <Search size={13} style={{ position: 'absolute', left: 10, top: '50%', transform: 'translateY(-50%)', color: 'var(--text-muted)' }} />
            <input
              type="text"
              placeholder="Search changes…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              style={{
                width: '100%', padding: '7px 10px 7px 30px',
                background: 'var(--bg-elevated)', border: '1px solid var(--border)',
                borderRadius: 6, color: 'var(--text-primary)', fontSize: 12, outline: 'none',
              }}
            />
          </div>
          <select
            value={stateFilter}
            onChange={(e) => setStateFilter(e.target.value)}
            style={{
              width: '100%', padding: '6px 8px', borderRadius: 6,
              border: '1px solid var(--border)', background: 'var(--bg-elevated)',
              color: 'var(--text-primary)', fontSize: 12, cursor: 'pointer',
            }}
          >
            {STATE_OPTIONS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
        </div>

        <div style={{ flex: 1, overflowY: 'auto' }}>
          {changesLoading && (
            <div style={{ padding: 16, fontSize: 12, color: 'var(--text-muted)' }}>Loading…</div>
          )}
          {!changesLoading && filteredChanges.length === 0 && (
            <div style={{ padding: 16, fontSize: 12, color: 'var(--text-muted)', lineHeight: 1.5 }}>
              {phaseACompleteChanges.length === 0
                ? 'No changes ready yet. A change appears here only after Phase A (BRD, TSD, XSD, Product Kit) is complete.'
                : 'No changes match this filter.'}
            </div>
          )}
          {filteredChanges.map(c => {
            const active = c.id === selectedId
            return (
              <button
                key={c.id}
                onClick={() => navigate(`/product-kit/${c.id}`)}
                style={{
                  display: 'block', width: '100%', textAlign: 'left',
                  padding: '10px 14px',
                  background: active ? 'var(--sidebar-active)' : 'transparent',
                  // `border` is a SHORTHAND — it resets all four sides, so it has to
                  // come first. It used to sit after borderLeft, which silently wiped
                  // the active-row accent bar (the borderBottom* longhands after it
                  // were there to restore the bottom edge; nothing restored the left).
                  border: 'none',
                  borderBottom: '1px solid var(--border-subtle)',
                  borderLeft: active ? '2px solid var(--accent)' : '2px solid transparent',
                  cursor: 'pointer', color: 'var(--text-primary)',
                }}
                onMouseEnter={e => { if (!active) e.currentTarget.style.background = 'var(--bg-card)' }}
                onMouseLeave={e => { if (!active) e.currentTarget.style.background = 'transparent' }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
                  <span style={{ fontSize: 10, fontWeight: 700, color: 'var(--text-muted)', letterSpacing: '0.04em' }}>
                    #{String(c.id).slice(0, 8)}
                  </span>
                  <OverallChip change={c} />
                </div>
                <div style={{
                  fontSize: 12, fontWeight: 500, color: 'var(--text-primary)',
                  overflow: 'hidden', textOverflow: 'ellipsis', display: '-webkit-box',
                  WebkitLineClamp: 2, WebkitBoxOrient: 'vertical',
                }}>
                  {c.title || c.initial_prompt || 'Untitled change'}
                </div>
              </button>
            )
          })}
        </div>
      </aside>

      {/* ── Right pane ───────────────────────────────────────────────── */}
      <main style={{ flex: 1, height: '100%', overflowY: 'auto', background: 'var(--bg-base)' }}>
        {!selectedChange && fetchingChange ? (
          <div style={{ padding: 40, color: 'var(--text-muted)', fontSize: 13, display: 'flex', alignItems: 'center', gap: 6 }}>
            <Loader size={13} style={{ animation: 'spin 1s linear infinite' }} /> Loading change…
          </div>
        ) : !selectedChange && routeId ? (
          // A deep link that resolved to nothing: say so. Falling through to the
          // generic "select a change" prompt read as though the user had picked
          // nothing, when in fact they had picked something we could not load.
          <div style={{ padding: 40, maxWidth: 640, margin: '0 auto', textAlign: 'center' }}>
            <div style={{
              display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
              width: 40, height: 40, borderRadius: '50%',
              background: 'rgba(224,108,108,0.12)', marginBottom: 12,
            }}>
              <AlertCircle size={18} style={{ color: 'var(--danger)' }} />
            </div>
            <h2 style={{ margin: '0 0 6px', fontSize: 15, fontWeight: 600, color: 'var(--text-primary)' }}>
              Change #{String(routeId).slice(0, 8)} could not be opened
            </h2>
            <p style={{ margin: 0, fontSize: 13, color: 'var(--text-muted)', lineHeight: 1.55 }}>
              {fetchChangeError?.response?.status === 404
                ? 'This change no longer exists.'
                : fetchChangeError?.response?.status === 403
                  ? 'You do not have access to this change.'
                  : (fetchChangeError?.response?.data?.detail
                     || 'Pick a change from the list on the left.')}
            </p>
          </div>
        ) : !selectedChange ? (
          <div style={{ padding: 40, color: 'var(--text-muted)', fontSize: 13 }}>
            Select a change on the left to manage its product-kit overrides.
          </div>
        ) : !selectedIsPhaseAComplete ? (
          <div style={{ padding: 40, maxWidth: 640, margin: '0 auto', textAlign: 'center' }}>
            <div style={{
              display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
              width: 40, height: 40, borderRadius: '50%',
              background: 'rgba(218,119,86,0.12)', marginBottom: 12,
            }}>
              <Package size={18} style={{ color: 'var(--accent)' }} />
            </div>
            <h2 style={{ margin: '0 0 6px', fontSize: 15, fontWeight: 600, color: 'var(--text-primary)' }}>
              Phase A isn't complete for this change
            </h2>
            <p style={{ margin: '0 0 16px', fontSize: 13, color: 'var(--text-muted)', lineHeight: 1.55 }}>
              Product-kit overrides only apply once the kit itself is generated.
              Finish Phase A (BRD, TSD, XSD, Product Kit) for this change first — it will
              then appear in the list on the left.
            </p>
            <button
              onClick={() => navigate(`/changes/${selectedChange.id}`)}
              style={{
                display: 'inline-flex', alignItems: 'center', gap: 4,
                padding: '8px 16px', background: 'var(--accent)', color: 'white',
                border: 'none', borderRadius: 6, fontSize: 12, fontWeight: 600, cursor: 'pointer',
              }}
            >
              Open change #{String(selectedChange.id).slice(0, 8)}
            </button>
          </div>
        ) : (
          <div style={{ padding: '24px 32px', maxWidth: 900, margin: '0 auto' }}>
            {/* Header */}
            <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12, marginBottom: 6 }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
                  <span style={{ fontSize: 11, fontWeight: 700, color: 'var(--text-muted)', letterSpacing: '0.04em' }}>
                    #{selectedChange.id}
                  </span>
                  <OverallChip change={selectedChange} />
                </div>
                <h1 style={{ margin: 0, fontSize: 18, fontWeight: 600, color: 'var(--text-primary)' }}>
                  {selectedChange.title || 'Untitled change'}
                </h1>
                {selectedChange.initial_prompt && selectedChange.title && (
                  <p style={{ margin: '4px 0 0', fontSize: 12, color: 'var(--text-muted)' }}>
                    {selectedChange.initial_prompt}
                  </p>
                )}
              </div>
              <button
                onClick={() => navigate(`/changes/${selectedChange.id}/phase-c`)}
                style={{
                  display: 'inline-flex', alignItems: 'center', gap: 4,
                  padding: '7px 12px', fontSize: 12, fontWeight: 500,
                  color: 'var(--text-secondary)', background: 'var(--bg-elevated)',
                  border: '1px solid var(--border)', borderRadius: 6, cursor: 'pointer',
                }}
                title="Open Phase C to communicate this change to partners"
              >
                Open Phase C <ChevronRight size={13} />
              </button>
            </div>

            {/* Suppressed on a failed load — telling someone how to upload against a
                kit they could not even read is instruction for an action they cannot
                take. The error block below says what actually happened. */}
            {!manifestErrorText && (
              <p style={{ margin: '16px 0 20px', fontSize: 12, color: 'var(--text-muted)', lineHeight: 1.55 }}>
                Upload a file against any item to substitute it for the generated
                version on the next shipment. Overrides persist across shipments —
                clearing an override reverts that item to the generated file.
                {overrideCount > 0 && (
                  <> <strong style={{ color: 'var(--accent)' }}>{overrideCount}</strong> override{overrideCount === 1 ? '' : 's'} currently active.</>
                )}
              </p>
            )}

            {error && (
              <div style={{
                display: 'flex', alignItems: 'center', gap: 8, marginBottom: 16,
                padding: '8px 12px', borderRadius: 6,
                background: 'rgba(224,108,108,0.08)', border: '1px solid rgba(224,108,108,0.25)',
              }}>
                <AlertCircle size={13} style={{ color: 'var(--danger)' }} />
                <span style={{ fontSize: 12, color: 'var(--danger)', flex: 1 }}>{error}</span>
                <button onClick={() => setError(null)} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-muted)' }}>
                  <X size={13} />
                </button>
              </div>
            )}

            {/* Item list */}
            {manifestLoading ? (
              <div style={{ padding: 20, fontSize: 12, color: 'var(--text-muted)', display: 'flex', alignItems: 'center', gap: 6 }}>
                <Loader size={12} style={{ animation: 'spin 1s linear infinite' }} /> Loading kit items…
              </div>
            ) : manifestErrorText ? (
              // NOT the empty-state: a failed load is not an empty kit. Rendering a
              // 403 as "no kit items yet" told the reader the wrong thing in the one
              // case this page makes routine (see the note on manifestError above).
              <div style={{
                display: 'flex', alignItems: 'flex-start', gap: 8, padding: '12px 16px',
                borderRadius: 8, background: 'rgba(224,108,108,0.08)',
                border: '1px solid rgba(224,108,108,0.25)',
              }}>
                <AlertCircle size={14} style={{ color: 'var(--danger)', flexShrink: 0, marginTop: 1 }} />
                <span style={{ fontSize: 12, color: 'var(--text-secondary)', lineHeight: 1.5 }}>
                  {manifestErrorText}
                </span>
              </div>
            ) : items.length === 0 ? (
              <div style={{ padding: 20, fontSize: 12, color: 'var(--text-muted)' }}>
                No kit items are available for this change yet.
              </div>
            ) : (
              <div style={{ border: '1px solid var(--border)', borderRadius: 8, background: 'var(--bg-elevated)', overflow: 'hidden' }}>
                {items.map((item, i) => {
                  const dt = item.doc_type
                  const label = kitDocLabel(dt)
                  const statusLabel = item.override
                    ? `uploaded: ${item.override.filename}${item.override.size_bytes ? ` · ${formatKB(item.override.size_bytes)}` : ''}`
                    : item.generated
                      ? `v${item.generated.version} · will ship generated`
                      : 'not generated — nothing to ship'
                  const isUploading = uploadingDocType === dt
                  return (
                    <div
                      key={dt}
                      style={{
                        display: 'flex', alignItems: 'center', gap: 12,
                        padding: '12px 16px',
                        borderBottom: i < items.length - 1 ? '1px solid var(--border-subtle)' : 'none',
                      }}
                    >
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ fontSize: 13, fontWeight: 500, color: 'var(--text-primary)' }}>{label}</div>
                        <div style={{
                          marginTop: 2, fontSize: 11,
                          color: item.override ? 'var(--accent)' : 'var(--text-muted)',
                          display: 'flex', alignItems: 'center', gap: 4,
                        }}>
                          {item.override && <Paperclip size={10} />}
                          {statusLabel}
                        </div>
                      </div>
                      <label style={{
                        display: 'inline-flex', alignItems: 'center', gap: 5,
                        padding: '5px 10px', fontSize: 11,
                        border: '1px solid var(--border)', borderRadius: 5,
                        background: 'var(--bg-base)', color: 'var(--text-secondary)',
                        cursor: isUploading ? 'wait' : 'pointer',
                        opacity: isUploading ? 0.7 : 1,
                      }}>
                        {isUploading
                          ? <Loader size={11} style={{ animation: 'spin 1s linear infinite' }} />
                          : <Upload size={11} />}
                        {item.override ? 'Replace' : 'Upload'}
                        <input
                          ref={isUploading ? fileInputRef : null}
                          type="file"
                          disabled={uploadingDocType != null}
                          onChange={(e) => {
                            const f = e.target.files?.[0]
                            e.target.value = ''
                            if (f) handleUpload(dt, f)
                          }}
                          style={{ display: 'none' }}
                        />
                      </label>
                      {item.override && (
                        <button
                          type="button"
                          onClick={() => handleClear(dt)}
                          disabled={uploadingDocType != null}
                          title="Remove the uploaded override; revert to the generated file on next ship"
                          style={{
                            padding: '5px 10px', fontSize: 11,
                            border: '1px solid var(--border)', borderRadius: 5,
                            background: 'transparent', color: 'var(--text-muted)',
                            cursor: uploadingDocType != null ? 'not-allowed' : 'pointer',
                          }}
                        >Clear</button>
                      )}
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        )}
      </main>
    </div>
  )
}
