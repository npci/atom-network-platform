// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useState, useRef, useEffect, useMemo } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Send, Search, Building2, CheckCircle, Clock, AlertTriangle,
  Circle, ChevronDown, ChevronRight, Zap, RefreshCw, Loader,
  MessageSquare, Sparkles, X, Smartphone, Globe, Cpu, MessagesSquare,
  Inbox, Archive,
} from 'lucide-react'
import api, { silentApi, phaseCApi } from '../../services/api'
import { BASE_PATH } from '../../utils/basePath'
import ReactMarkdown from 'react-markdown'
import { useGateModal } from '../../context/useGateModal'
import { isEvalGateError } from '../../lib/evalGate'

import PageHeader  from '../../components/cert/PageHeader'
import StatusBadge from '../../components/cert/StatusBadge'
import StatusStrip from '../../components/conversation/StatusStrip'
import { ASSIGNMENT_STATUS, relativeTime } from '../../lib/certStatus'

// ─── Config ───────────────────────────────────────────────────────────────────

// Aligns with the canonical lifecycle taxonomy from lib/certStatus.js — every
// AssignmentStatus value renders correctly in thread cards, including the
// post-shipped values (received/accepted/applied/tested/ready_for_certification/
// certifying/certified/ready_for_production/in_production/withdrawn).
function statusCfg(s) {
  const meta = ASSIGNMENT_STATUS[s] || ASSIGNMENT_STATUS.assigned
  return { color: meta.color, label: meta.label, Icon: meta.icon }
}

const TYPE_META = {
  bank:        { color: '#58a6ff', icon: Building2  },
  psp:         { color: '#3fb950', icon: Smartphone },
  tpap:        { color: '#d29922', icon: Globe      },
  cert_engine: { color: '#e8b347', icon: Cpu        },
}
const typeColor = t => TYPE_META[(Array.isArray(t) ? t[0] : t || 'bank').toLowerCase()]?.color || '#58a6ff'
const typeIcon  = t => TYPE_META[(Array.isArray(t) ? t[0] : t || 'bank').toLowerCase()]?.icon  || Building2

const fmtTime = ts => ts ? new Date(ts).toLocaleString('en-IN', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }) : ''

// 6 lifecycle stages (collapsed from 10 main states for the conversation
// header — keeps the bar readable while remaining accurate.)
const STAGES = ['Assigned', 'In discussion', 'Building', 'Cert', 'Certified', 'Live']
function stageFor(s) {
  const v = (s || '').toLowerCase()
  if (['in_production'].includes(v))                                 return 5
  if (['certified', 'ready_for_production', 'completed'].includes(v))return 4
  if (['certifying', 'ready_for_certification', 'ready'].includes(v))return 3
  if (['applied', 'tested', 'in_progress'].includes(v))              return 2
  if (['accepted', 'acknowledged', 'communicated', 'received'].includes(v)) return 1
  return 0
}

// Effective open-thread state derivation: a thread is "awaiting the Authority reply"
// when its latest_role is 'partner' (last message was from the partner).
// "Awaiting partner" when latest_role is 'po_approved'/'npci'/'approved'.
// "AI draft pending" when latest_role is 'ai_draft'.
function threadState(t) {
  const lr = (t.latest_role || '').toLowerCase()
  if (t.thread_status === 'resolved') return 'resolved'
  if (lr === 'ai_draft') return 'draft_pending'
  if (lr === 'partner')  return 'awaiting_authority'
  if (['po_approved', 'npci', 'approved'].includes(lr)) return 'awaiting_partner'
  return 'open'
}

const THREAD_STATE_META = {
  awaiting_authority: { label: 'Awaiting Authority',     color: '#e06c6c' },
  awaiting_partner: { label: 'Awaiting partner',  color: '#58a6ff' },
  draft_pending:    { label: 'AI draft pending',  color: '#bc8cff' },
  open:             { label: 'Open',              color: '#8b949e' },
  resolved:         { label: 'Resolved',          color: '#3fb950' },
}

// ─── Stage progress bar ───────────────────────────────────────────────────────
function StageBar({ stage }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', padding: '10px 20px 12px', borderBottom: '1px solid var(--border)', background: 'var(--bg-card)', flexShrink: 0 }}>
      {STAGES.map((label, i) => {
        const done   = i < stage
        const active = i === stage
        const clr    = done ? '#16a34a' : active ? '#2563eb' : 'var(--border)'
        return (
          <div key={label} style={{ display: 'flex', alignItems: 'center', flex: i < STAGES.length - 1 ? 1 : 0 }}>
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 4 }}>
              <div style={{ width: 20, height: 20, borderRadius: '50%', border: `2px solid ${clr}`, background: done ? '#16a34a' : active ? '#2563eb' : 'transparent', display: 'flex', alignItems: 'center', justifyContent: 'center', transition: 'all 0.2s' }}>
                {done   && <CheckCircle size={11} color="white" strokeWidth={3} />}
                {active && <div style={{ width: 7, height: 7, borderRadius: '50%', background: 'white' }} />}
              </div>
              <span style={{ fontSize: 9, fontWeight: active ? 700 : 400, color: done || active ? clr : 'var(--text-muted)', whiteSpace: 'nowrap', letterSpacing: '0.02em' }}>{label}</span>
            </div>
            {i < STAGES.length - 1 && (
              <div style={{ flex: 1, height: 2, background: done ? '#16a34a' : 'var(--border)', marginBottom: 14, mx: 2, transition: 'background 0.2s' }} />
            )}
          </div>
        )
      })}
    </div>
  )
}

// ─── Message bubble ───────────────────────────────────────────────────────────
function Bubble({ msg, bankName, bankType }) {
  const tc   = typeColor(bankType)
  const time = fmtTime(msg.created_at)
  const role = msg.role

  // ai_draft messages are never rendered as chat bubbles —
  // they appear only in the AiSuggestionBanner above the input.
  if (role === 'ai_draft') return null

  // ── Received: Bank query — LEFT side ──────────────────────────────────────
  if (role === 'partner') return (
    <div style={{ display: 'flex', justifyContent: 'flex-start', gap: 10, margin: '10px 0' }}>
      {/* Bank avatar */}
      <div style={{
        width: 32, height: 32, borderRadius: '50%',
        background: `${tc}15`, border: `2px solid ${tc}40`,
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        fontSize: 12, fontWeight: 800, color: tc,
        flexShrink: 0, alignSelf: 'flex-end',
      }}>
        {(bankName || 'B')[0]}
      </div>
      <div style={{ maxWidth: '68%' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 5, marginBottom: 4 }}>
          <span style={{ fontSize: 10, fontWeight: 600, color: tc }}>{bankName}</span>
          <span style={{ fontSize: 9, color: 'var(--text-muted)', fontFamily: 'monospace' }}>· query</span>
        </div>
        <div style={{
          padding: '11px 15px',
          borderRadius: '4px 16px 16px 16px',
          background: 'var(--bg-elevated)',
          border: `1.5px solid ${tc}30`,
          fontSize: 13, lineHeight: 1.65,
          color: 'var(--text-primary)',
          boxShadow: '0 1px 4px rgba(0,0,0,0.06)',
        }}>
          {msg.content}
        </div>
        <div style={{ display: 'flex', gap: 5, marginTop: 4 }}>
          <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>{time}</span>
          <span style={{ fontSize: 9, color: 'var(--text-muted)', fontFamily: 'monospace', opacity: 0.6 }}>A2A</span>
        </div>
      </div>
    </div>
  )

  // ── Sent: The Authority response — RIGHT side ──────────────────────────────────────
  if (role === 'po_approved' || role === 'npci' || role === 'approved') return (
    <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, margin: '10px 0' }}>
      <div style={{ maxWidth: '68%' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 5, marginBottom: 4, justifyContent: 'flex-end' }}>
          <span style={{ fontSize: 9, color: 'var(--text-muted)', fontFamily: 'monospace' }}>response ·</span>
          <span style={{ fontSize: 10, fontWeight: 600, color: '#2563eb' }}>the Authority</span>
        </div>
        <div style={{
          padding: '11px 15px',
          borderRadius: '16px 4px 16px 16px',
          background: 'linear-gradient(135deg, #2563eb, #1d4ed8)',
          color: 'white', fontSize: 13, lineHeight: 1.65,
          boxShadow: '0 2px 8px rgba(37,99,235,0.25)',
        }}>
          <div className="md-content-white"><ReactMarkdown>{msg.content}</ReactMarkdown></div>
        </div>
        <div style={{ display: 'flex', gap: 5, marginTop: 4, justifyContent: 'flex-end' }}>
          <span style={{ fontSize: 9, color: 'var(--text-muted)', fontFamily: 'monospace', opacity: 0.6 }}>A2A</span>
          <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>{time}</span>
        </div>
      </div>
      {/* The Authority avatar */}
      <div style={{
        width: 32, height: 32, borderRadius: '50%',
        background: '#2563eb',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        fontSize: 12, fontWeight: 800, color: 'white',
        flexShrink: 0, alignSelf: 'flex-end',
      }}>
        N
      </div>
    </div>
  )

  // ── Counter-proposals and blockers ──────────────────────────────────────
  // Persistent chat bubbles for the non-Q&A conversation flow. Each is
  // rendered on the side matching its originator (partner=left,
  // npci=right) with a tag describing the round / severity / status so
  // operators can scan the audit trail in-line. Layout matches the
  // partner/po_approved bubbles for visual consistency.
  if (role === 'counter_proposal' || role === 'counter_resolution'
      || role === 'blocker' || role === 'blocker_resolution') {
    const fromPartner = (msg.originator === 'partner')
    const align = fromPartner ? 'flex-start' : 'flex-end'
    const bubbleRadius = fromPartner ? '4px 16px 16px 16px' : '16px 4px 16px 16px'
    const avatarBg = fromPartner ? tc : '#2563eb'
    const avatarText = fromPartner ? (bankName || 'B')[0] : 'N'
    const senderLabel = fromPartner ? bankName : 'NPCI'
    const senderColor = fromPartner ? tc : '#2563eb'

    // Per-role tag style — keeps the conversation type scannable.
    let tag = null
    let tagBg = '#6c757d'
    if (role === 'counter_proposal') {
      tag = `Counter · Round ${msg.round || 1}`
      tagBg = '#6ea8dc'
    } else if (role === 'counter_resolution') {
      const s = (msg.status || '').toLowerCase()
      tag = s === 'accepted'        ? `Accepted · Round ${msg.round || 1}` :
            s === 'rejected'        ? `Rejected · Round ${msg.round || 1}` :
            s === 'countered_back'  ? `Countered back · Round ${msg.round || 1}` :
                                      `Resolved · Round ${msg.round || 1}`
      tagBg = s === 'accepted' ? '#155724' : s === 'rejected' ? '#721c24' : '#0c5460'
    } else if (role === 'blocker') {
      tag = `Blocker · ${(msg.severity || 'high').toUpperCase()}`
      tagBg = msg.severity === 'critical' ? '#721c24'
            : msg.severity === 'high'     ? '#856404'
            : msg.severity === 'medium'   ? '#0c5460' : '#383d41'
    } else if (role === 'blocker_resolution') {
      tag = 'Blocker resolved'
      tagBg = '#155724'
    }

    const avatar = (
      <div style={{
        width: 32, height: 32, borderRadius: '50%',
        background: fromPartner ? `${avatarBg}15` : avatarBg,
        border: fromPartner ? `2px solid ${avatarBg}40` : 'none',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        fontSize: 12, fontWeight: 800,
        color: fromPartner ? avatarBg : 'white',
        flexShrink: 0, alignSelf: 'flex-end',
      }}>{avatarText}</div>
    )

    const body = (
      <div style={{ maxWidth: '68%' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4,
                      justifyContent: fromPartner ? 'flex-start' : 'flex-end' }}>
          <span style={{ fontSize: 10, fontWeight: 600, color: senderColor }}>{senderLabel}</span>
          {tag && (
            <span style={{
              fontSize: 9, fontWeight: 700, padding: '2px 7px', borderRadius: 10,
              background: tagBg, color: 'white', letterSpacing: 0.3,
            }}>{tag}</span>
          )}
        </div>
        <div style={{
          padding: '11px 15px',
          borderRadius: bubbleRadius,
          background: 'var(--bg-elevated)',
          border: `1.5px solid ${avatarBg}30`,
          fontSize: 13, lineHeight: 1.55,
          color: 'var(--text-primary)',
          boxShadow: '0 1px 4px rgba(0,0,0,0.06)',
          whiteSpace: 'pre-wrap',
        }}>{msg.content}</div>
        <div style={{ display: 'flex', gap: 5, marginTop: 4,
                      justifyContent: fromPartner ? 'flex-start' : 'flex-end' }}>
          <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>{time}</span>
          <span style={{ fontSize: 9, color: 'var(--text-muted)', fontFamily: 'monospace', opacity: 0.6 }}>A2A</span>
        </div>
      </div>
    )

    return (
      <div style={{ display: 'flex', justifyContent: align, gap: 10, margin: '10px 0' }}>
        {fromPartner ? <>{avatar}{body}</> : <>{body}{avatar}</>}
      </div>
    )
  }

  // ── System / informational messages ──────────────────────────────────────
  return (
    <div style={{ textAlign: 'center', margin: '12px 0' }}>
      <span style={{ fontSize: 11, color: 'var(--text-muted)', background: 'var(--bg-elevated)', padding: '3px 14px', borderRadius: 20, border: '1px solid var(--border)' }}>
        {msg.content}
      </span>
    </div>
  )
}

// ─── AI Suggestion banner (shown above input when draft exists & input is empty) ─
function AiSuggestionBanner({ draft, onUse, onDismiss }) {
  const [open, setOpen] = useState(false)
  if (!draft) return null
  return (
    <div style={{ margin: '0 16px 10px', borderRadius: 10, border: '1px solid #c4b5fd', background: 'linear-gradient(135deg, #faf5ff, #f3e8ff)', overflow: 'hidden' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '9px 14px' }}>
        <Sparkles size={14} color="#7c3aed" />
        <span style={{ fontSize: 12, fontWeight: 600, color: '#6d28d9', flex: 1 }}>
          AI drafted a response — review and send
        </span>
        <button onClick={() => setOpen(v => !v)} style={{ background: 'none', border: 'none', cursor: 'pointer', color: '#7c3aed', fontSize: 11, fontWeight: 600, padding: '2px 8px', borderRadius: 6, display: 'flex', alignItems: 'center', gap: 4 }}>
          {open ? 'Hide' : 'Preview'} <ChevronDown size={11} style={{ transform: open ? 'rotate(180deg)' : 'none', transition: '0.2s' }} />
        </button>
        <button onClick={() => onUse(draft.content)} style={{ background: '#7c3aed', border: 'none', cursor: 'pointer', color: 'white', fontSize: 11, fontWeight: 700, padding: '4px 12px', borderRadius: 6 }}>
          Use Draft
        </button>
        <button onClick={onDismiss} style={{ background: 'none', border: 'none', cursor: 'pointer', color: '#9ca3af', padding: 2, display: 'flex', alignItems: 'center' }}>
          <X size={13} />
        </button>
      </div>
      {open && (
        <div style={{ padding: '0 14px 10px', borderTop: '1px solid #e9d5ff', marginTop: 0 }}>
          <div className="md-content" style={{ fontSize: 12, color: '#374151', paddingTop: 10, maxHeight: 140, overflowY: 'auto' }}>
            <ReactMarkdown>{draft.content}</ReactMarkdown>
          </div>
        </div>
      )}
    </div>
  )
}

// ─── Thread item ──────────────────────────────────────────────────────────────
function ThreadItem({ t, isActive, onClick, unread }) {
  const col      = typeColor(t.partner_types)
  const Icon     = typeIcon(t.partner_types)
  const lastRole = t.latest_role || ''
  const ts       = t.latest_at ? relativeTime(t.latest_at) : ''
  const crShort  = (t.change_id || '').slice(0, 8).toUpperCase()
  const stateMeta = THREAD_STATE_META[threadState(t)] || THREAD_STATE_META.open

  return (
    <div
      onClick={onClick}
      style={{
        display: 'flex', alignItems: 'flex-start', gap: 10,
        padding: '12px 14px', cursor: 'pointer',
        borderBottom: '1px solid var(--border-subtle)',
        borderLeft: `3px solid ${isActive ? 'var(--accent)' : unread > 0 ? '#e06c6c' : 'transparent'}`,
        background: isActive ? 'var(--accent-subtle)' : 'transparent',
        transition: 'background 0.12s',
      }}
      onMouseEnter={e => { if (!isActive) e.currentTarget.style.background = 'var(--sidebar-hover)' }}
      onMouseLeave={e => { if (!isActive) e.currentTarget.style.background = 'transparent' }}
    >
      <div style={{ width: 38, height: 38, borderRadius: 10, background: `${col}1A`, border: `1.5px solid ${col}40`, display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
        <Icon size={16} color={col} />
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 2 }}>
          <span style={{ fontSize: 13, fontWeight: unread > 0 ? 700 : 600, color: 'var(--text-primary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 130 }}>
            {t.partner_name}
          </span>
          <span style={{ fontSize: 9, color: 'var(--text-muted)', flexShrink: 0, marginLeft: 4, marginTop: 2 }}>{ts}</span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 5, marginBottom: 3 }}>
          <span style={{ fontSize: 9, fontFamily: 'monospace', fontWeight: 700, color: '#2563eb', background: 'rgba(37,99,235,0.08)', padding: '1px 5px', borderRadius: 4, border: '1px solid rgba(37,99,235,0.2)', flexShrink: 0 }}>
            {crShort}
          </span>
          <span style={{ fontSize: 10, color: 'var(--text-muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {t.change_title}
          </span>
        </div>
        <p style={{ margin: 0, fontSize: 11, color: lastRole === 'ai_draft' ? '#bc8cff' : 'var(--text-muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontStyle: lastRole === 'ai_draft' ? 'italic' : 'normal', display: 'flex', alignItems: 'center', gap: 4 }}>
          {lastRole === 'ai_draft' && <Sparkles size={9} color="#bc8cff" />}
          {lastRole === 'ai_draft' ? 'AI draft ready to review' : lastRole === 'partner' ? `💬 ${t.latest_preview}` : t.latest_preview || 'No messages yet'}
        </p>
        {/* Thread-state badge */}
        <span style={{
          display: 'inline-flex', alignItems: 'center',
          marginTop: 4,
          padding: '1px 7px', borderRadius: 999,
          fontSize: 9, fontWeight: 600,
          color: stateMeta.color, background: `${stateMeta.color}1A`,
          border: `1px solid ${stateMeta.color}40`,
        }}>
          {stateMeta.label}
        </span>
      </div>
      {unread > 0 && (
        <span style={{ fontSize: 10, fontWeight: 800, minWidth: 19, height: 19, borderRadius: 10, background: '#dc2626', color: 'white', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '0 4px', flexShrink: 0, alignSelf: 'center' }}>
          {unread}
        </span>
      )}
    </div>
  )
}

// ─── Main ─────────────────────────────────────────────────────────────────────
export default function AgentMessaging() {
  const queryClient = useQueryClient()
  const [activeThread, setActiveThread] = useState(null)
  const [search, setSearch]             = useState('')
  const [input, setInput]               = useState('')
  const [dismissedDraft, setDismissedDraft] = useState(false)
  const { openGateModal } = useGateModal()
  const [stateFilter, setStateFilter]   = useState('all')   // all | awaiting_authority | awaiting_partner | draft_pending | resolved
  const [sortBy, setSortBy]             = useState('latest') // latest | unread | partner
  // Agent Messaging is the cert-channel inbox ONLY. General Phase C
  // clarifications live exclusively on each change request's Phase C
  // view (no overlap by design). The kind filter is a constant, not a
  // segment control.
  const kindFilter = 'cert'
  const bottomRef = useRef(null)

  // Lock the parent <main> overflow so AgentMessaging fully controls scrolling.
  // Restored on unmount so other pages are unaffected.
  useEffect(() => {
    const main = document.querySelector('main')
    if (!main) return
    const prev = main.style.overflow
    main.style.overflow = 'hidden'
    return () => { main.style.overflow = prev }
  }, [])

  // localStorage read-state (shared with Sidebar badge). Stored as a
  // {thread_id: read_at_iso} map so threads with new partner messages
  // arriving AFTER the user opened them are correctly counted as
  // unread again. Legacy flat-array shape is tolerated.
  const getReadMap = () => {
    const raw = JSON.parse(localStorage.getItem('cert_read_threads') || '{}')
    if (Array.isArray(raw)) {
      // Legacy migration: every previously-read thread becomes
      // "always read" for backward compat.
      return Object.fromEntries(raw.map(id => [id, '9999-12-31T00:00:00Z']))
    }
    return raw || {}
  }
  const isThreadRead = (t) => {
    const m = getReadMap()
    const readAt = m[t.thread_id]
    return !!(readAt && t.latest_at && t.latest_at <= readAt)
  }
  const markRead   = (id) => {
    const m = getReadMap()
    m[id] = new Date().toISOString()
    localStorage.setItem('cert_read_threads', JSON.stringify(m))
    window.dispatchEvent(new Event('cert_read_change'))
  }

  // Thread list — scoped to the active channel (kindFilter). Server-side
  // filter on /a2a/threads?kind=… so the inbox never shows the wrong
  // channel's threads.
  const { data: threadsData, isLoading } = useQuery({
    queryKey: ['a2a-threads', kindFilter],
    queryFn: () => silentApi.get(`${BASE_PATH}threads?kind=${kindFilter}`).then(r => r.data).catch(() => ({ threads: [], total_unread: 0 })),
    refetchInterval: 10000,
    retry: false,
  })
  const allThreads = threadsData?.threads || []

  // Negotiation messages for active thread — kind passed through so the
  // backend resolves the right (change, partner, kind) thread row.
  const { data: negotiation, isLoading: loadingMsgs, refetch: refetchNeg } = useQuery({
    queryKey: ['negotiation', activeThread?.changeId, activeThread?.partnerId, activeThread?.kind],
    queryFn: () => silentApi.get(`/changes/${activeThread.changeId}/partners/${activeThread.partnerId}/negotiation?kind=${activeThread.kind || 'general'}`)
                    .then(r => r.data).catch(() => undefined),
    enabled: !!activeThread,
    refetchInterval: 5000,
    retry: false,
    keepPreviousData: true,
  })

  const messages = negotiation?.messages || []

  // Find the index of the most recent partner query and the most recent
  // PO-approved reply. The AI suggestion banner should appear ONLY when:
  //   - there is at least one partner message,
  //   - the most-recent partner message is more recent than the most-recent
  //     PO-approved reply (i.e. genuinely awaiting a reply),
  //   - the user hasn't started typing or dismissed the suggestion,
  //   - and an ai_draft was generated AFTER that partner message (so we're
  //     showing a draft that actually corresponds to the unanswered query).
  // Without the chronology check the banner re-appears with a stale draft
  // each time the user replies.
  const REPLY_ROLES = ['po_approved', 'npci', 'approved']
  const lastPartnerIdx  = messages.reduce((acc, m, i) => m.role === 'partner' ? i : acc, -1)
  const lastReplyIdx    = messages.reduce((acc, m, i) => REPLY_ROLES.includes(m.role) ? i : acc, -1)
  const isAwaitingReply = lastPartnerIdx >= 0 && lastPartnerIdx > lastReplyIdx
  const lastAiDraftIdx  = messages.reduce((acc, m, i) => m.role === 'ai_draft' ? i : acc, -1)
  const draftMatchesUnanswered = lastAiDraftIdx > lastPartnerIdx && lastAiDraftIdx > lastReplyIdx
  const lastPartnerMsgId = lastPartnerIdx >= 0 ? messages[lastPartnerIdx]?.id : null
  // Track which partner message the operator has already responded
  // to / dismissed the draft for. Prevents the AI draft banner from
  // re-appearing on the polling tick after Send (which left the
  // operator briefly seeing a stale "draft ready" pill and tempted
  // them to click Use Draft a second time).
  const [addressedPartnerMsgId, setAddressedPartnerMsgId] = useState(null)
  const isDraftNewForPartnerMsg = lastPartnerMsgId && lastPartnerMsgId !== addressedPartnerMsgId
  const latestDraft = (
    isAwaitingReply
    && draftMatchesUnanswered
    && input === ''
    && !dismissedDraft
    && isDraftNewForPartnerMsg
  )
    ? messages[lastAiDraftIdx]
    : null
  const showBanner = !!latestDraft

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages.length, activeThread])

  // Reset per-thread state when switching threads.
  useEffect(() => {
    setDismissedDraft(false)
    setAddressedPartnerMsgId(null)
    setInput('')
  }, [activeThread?.threadId])

  // Send mutation — channel kind threads through so the backend respond
  // endpoint marks the correct thread+queries completed and ships the
  // outbound CLARIFICATION_RESPONSE with the right channel discriminator.
  const sendMut = useMutation({
    mutationFn: async (text) => {
      const hasDraft = messages.some(m => m.role === 'ai_draft' || m.role === 'partner')
      const kind = activeThread?.kind || 'general'
      if (hasDraft) {
        return api.post(`/changes/${activeThread.changeId}/partners/${activeThread.partnerId}/negotiation/respond?kind=${kind}`, { response_text: text })
      }
      try {
        return await phaseCApi.communicate(activeThread.changeId, {})
      } catch (err) {
        if (isEvalGateError(err)) {
          openGateModal({
            changeId: activeThread.changeId,
            detail: err.response?.data?.detail,
            actionLabel: 'Communicate change to partners',
            retryAction: (payload) => phaseCApi.communicate(activeThread.changeId, payload || {}),
            onSuccess: () => {
              queryClient.invalidateQueries(['negotiation', activeThread?.changeId, activeThread?.partnerId, activeThread?.kind])
              queryClient.invalidateQueries(['a2a-threads', kindFilter])
              setDismissedDraft(false)
            },
          })
          return { __gateBlocked: true }
        }
        throw err
      }
    },
    onSuccess: (res) => {
      if (res?.__gateBlocked) return
      queryClient.invalidateQueries(['negotiation', activeThread?.changeId, activeThread?.partnerId, activeThread?.kind])
      queryClient.invalidateQueries(['a2a-threads', kindFilter])
      setInput('')
      // Mark the partner message we just answered as "addressed" so
      // the AI suggestion banner doesn't bounce back on the next
      // polling refetch. It will return only when a NEW partner
      // message arrives (different id).
      if (lastPartnerMsgId) setAddressedPartnerMsgId(lastPartnerMsgId)
      setDismissedDraft(false)
    },
  })

  const handleSend = () => { if (input.trim() && !sendMut.isPending) sendMut.mutate(input.trim()) }

  // Thread list filtering + sort
  const effectiveUnread = t => isThreadRead(t) ? 0 : (t.unread_count || 0)
  const filtered = useMemo(() => {
    const q = search.toLowerCase()
    return allThreads
      .filter(t => {
        if (q && !(t.partner_name || '').toLowerCase().includes(q) && !(t.change_title || '').toLowerCase().includes(q)) return false
        if (stateFilter === 'all') return true
        return threadState(t) === stateFilter
      })
      .sort((a, b) => {
        if (sortBy === 'unread') {
          const ua = effectiveUnread(a), ub = effectiveUnread(b)
          if (ua !== ub) return ub - ua
          return new Date(b.latest_at || 0) - new Date(a.latest_at || 0)
        }
        if (sortBy === 'partner') return (a.partner_name || '').localeCompare(b.partner_name || '')
        return new Date(b.latest_at || 0) - new Date(a.latest_at || 0)
      })
  }, [allThreads, search, stateFilter, sortBy])
  const totalUnread = filtered.reduce((s, t) => s + effectiveUnread(t), 0)

  const partnerColor = activeThread ? typeColor(activeThread.bankType) : '#2563eb'
  const partnerCfg   = activeThread ? statusCfg(activeThread.status) : null
  const stage        = activeThread ? stageFor(activeThread.status) : 0

  return (
    <div style={{
      display: 'flex',
      flexDirection: 'column',
      height: '100%',
      minHeight: 0,
      overflow: 'hidden',
      background: 'var(--bg-base)',
    }}>

      {/* ══ Compact page header — sized to content, not half the viewport ═════ */}
      <div style={{
        padding: '14px 24px',
        borderBottom: '1px solid var(--border-subtle)',
        flexShrink: 0,
        background: 'var(--bg-card)',
        display: 'flex', alignItems: 'center', gap: 12,
      }}>
        <div style={{
          width: 32, height: 32, borderRadius: 8,
          background: 'rgba(37,99,235,0.10)', border: '1px solid rgba(37,99,235,0.25)',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
        }}>
          <MessagesSquare size={16} color="#2563eb" />
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <h1 style={{ margin: 0, fontSize: 16, fontWeight: 700, color: 'var(--text-primary)' }}>Agent Messaging — Cert Channel</h1>
          <p style={{ margin: 0, fontSize: 11, color: 'var(--text-muted)' }}>
            Cert-channel A2A threads only · general Phase C clarifications live on each change's Phase C view
          </p>
        </div>

        <button
          onClick={() => queryClient.invalidateQueries(['a2a-threads', kindFilter])}
          style={{
            display: 'inline-flex', alignItems: 'center', gap: 6,
            padding: '6px 11px',
            background: 'var(--bg-elevated)', border: '1px solid var(--border)',
            borderRadius: 6, color: 'var(--text-secondary)',
            fontSize: 12, fontWeight: 500, cursor: 'pointer',
          }}
        >
          <RefreshCw size={12} /> Refresh
        </button>
      </div>

      {/* ══ Two-pane chat layout ══════════════════════════════════════════════ */}
      <div style={{
        display: 'flex',
        flex: 1,
        minHeight: 0,
        overflow: 'hidden',
      }}>

      {/* ══ LEFT PANEL — fixed, independently scrollable ══════════════════════ */}
      <div style={{
        width: 288, flexShrink: 0,
        display: 'flex', flexDirection: 'column',
        borderRight: '1px solid var(--border)',
        background: 'var(--bg-card)',
        overflow: 'hidden',  /* clip children */
      }}>

        {/* Header — fixed inside left panel */}
        <div style={{ padding: '14px 14px 10px', borderBottom: '1px solid var(--border)', flexShrink: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 10 }}>
            <div>
              <h2 style={{ margin: 0, fontSize: 14, fontWeight: 700, color: 'var(--text-primary)' }}>Cert Threads</h2>
              <p style={{ margin: 0, fontSize: 10, color: 'var(--text-muted)', marginTop: 1 }}>Google A2A · task_type=query (phase=cert)</p>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              {totalUnread > 0 && (
                <span style={{ fontSize: 11, fontWeight: 800, padding: '2px 7px', borderRadius: 10, background: '#dc2626', color: 'white' }}>{totalUnread}</span>
              )}
              <button
                onClick={() => queryClient.invalidateQueries(['a2a-threads'])}
                style={{ padding: 5, borderRadius: 7, border: '1px solid var(--border)', background: 'transparent', cursor: 'pointer', display: 'flex', color: 'var(--text-muted)' }}
                title="Refresh"
              >
                <RefreshCw size={12} />
              </button>
            </div>
          </div>
          <div style={{ position: 'relative' }}>
            <Search size={13} style={{ position: 'absolute', left: 10, top: '50%', transform: 'translateY(-50%)', color: 'var(--text-muted)' }} />
            <input
              value={search} onChange={e => setSearch(e.target.value)}
              placeholder="Search bank or CR…"
              style={{ width: '100%', padding: '8px 10px 8px 30px', background: 'var(--bg-input)', border: '1px solid var(--border)', borderRadius: 8, color: 'var(--text-primary)', fontSize: 12, outline: 'none', boxSizing: 'border-box' }}
            />
          </div>

          {/* Thread state filter chips */}
          <div style={{ display: 'flex', gap: 4, marginTop: 8, flexWrap: 'wrap' }}>
            {[
              { v: 'all',              l: 'All',          c: 'var(--text-muted)' },
              { v: 'awaiting_authority', l: 'Needs reply',  c: '#e06c6c' },
              { v: 'draft_pending',    l: 'Drafts',       c: '#bc8cff' },
              { v: 'awaiting_partner', l: 'Sent',         c: '#58a6ff' },
              { v: 'resolved',         l: 'Resolved',     c: '#3fb950' },
            ].map(f => {
              const active = stateFilter === f.v
              return (
                <button
                  key={f.v}
                  onClick={() => setStateFilter(f.v)}
                  style={{
                    padding: '4px 9px',
                    borderRadius: '999px',
                    fontSize: 10,
                    fontWeight: 600,
                    border: `1px solid ${active ? f.c : 'var(--border)'}`,
                    background: active ? `${f.c}1A` : 'transparent',
                    color: active ? f.c : 'var(--text-muted)',
                    cursor: 'pointer',
                  }}
                >{f.l}</button>
              )
            })}
          </div>

          {/* Sort selector */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 8 }}>
            <span style={{ fontSize: 10, color: 'var(--text-muted)', fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Sort</span>
            <select
              value={sortBy}
              onChange={e => setSortBy(e.target.value)}
              style={{
                flex: 1, padding: '5px 8px', background: 'var(--bg-input)',
                border: '1px solid var(--border)', borderRadius: 6,
                color: 'var(--text-secondary)', fontSize: 11, cursor: 'pointer',
              }}
            >
              <option value="latest">Latest activity</option>
              <option value="unread">Unread first</option>
              <option value="partner">Partner name</option>
            </select>
          </div>
        </div>

        {/* Thread list — scrollable independently */}
        <div style={{ flex: 1, overflowY: 'auto', minHeight: 0 }}>
          {isLoading && (
            <div style={{ padding: 24, textAlign: 'center', color: 'var(--text-muted)', fontSize: 12 }}>
              <Loader size={16} style={{ display: 'block', margin: '0 auto 8px', animation: 'spin 1s linear infinite' }} />
              Loading threads…
            </div>
          )}
          {!isLoading && filtered.length === 0 && (
            <div style={{ padding: 24, textAlign: 'center', color: 'var(--text-muted)' }}>
              <MessageSquare size={28} style={{ margin: '0 auto 10px', opacity: 0.25 }} />
              <p style={{ margin: 0, fontSize: 13, fontWeight: 500 }}>No threads yet</p>
              <p style={{ margin: '4px 0 0', fontSize: 11 }}>Assign partners to a completed change to start.</p>
            </div>
          )}
          {filtered.map(t => (
            <ThreadItem
              key={t.thread_id}
              t={t}
              isActive={activeThread?.threadId === t.thread_id}
              unread={effectiveUnread(t)}
              onClick={() => {
                setActiveThread({ threadId: t.thread_id, kind: t.kind || kindFilter, changeId: t.change_id, partnerId: t.partner_id, crId: t.change_id, crTitle: t.change_title, bankName: t.partner_name, bankType: t.partner_types?.[0] || 'bank', status: t.thread_status })
                markRead(t.thread_id)
                setInput('')
              }}
            />
          ))}
        </div>
      </div>

      {/* ══ RIGHT PANEL — conversation ══════════════════════════════════════════ */}
      {activeThread ? (
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0, overflow: 'hidden' }}>

          {/* Conversation header — fixed */}
          <div style={{ padding: '12px 20px', borderBottom: '1px solid var(--border)', display: 'flex', alignItems: 'center', gap: 12, background: 'var(--bg-card)', flexShrink: 0 }}>
            <div style={{ width: 40, height: 40, borderRadius: 11, background: `${partnerColor}12`, border: `1.5px solid ${partnerColor}30`, display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
              <Building2 size={18} color={partnerColor} />
            </div>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                <span style={{ fontSize: 15, fontWeight: 700, color: 'var(--text-primary)' }}>{activeThread.bankName}</span>
                <span style={{ fontSize: 10, padding: '2px 8px', borderRadius: 8, color: partnerColor, background: `${partnerColor}12`, border: `1px solid ${partnerColor}25`, fontWeight: 600, textTransform: 'capitalize' }}>{activeThread.bankType}</span>
                {partnerCfg && (
                  <span style={{ fontSize: 10, display: 'flex', alignItems: 'center', gap: 3, color: partnerCfg.color, fontWeight: 600 }}>
                    <partnerCfg.Icon size={10} /> {partnerCfg.label}
                  </span>
                )}
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 2 }}>
                <span style={{ fontSize: 10, fontWeight: 700, color: '#2563eb', fontFamily: 'monospace', background: 'rgba(37,99,235,0.08)', padding: '1px 6px', borderRadius: 4 }}>
                  {activeThread.crId.slice(0, 8).toUpperCase()}
                </span>
                <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>·</span>
                <span style={{ fontSize: 11, color: 'var(--text-secondary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{activeThread.crTitle}</span>
              </div>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0 }}>
              <span style={{ fontSize: 10, padding: '3px 9px', borderRadius: 6, background: 'var(--bg-elevated)', border: '1px solid var(--border)', color: 'var(--text-muted)', fontFamily: 'monospace' }}>Google A2A</span>
              <button onClick={() => refetchNeg()} style={{ padding: 6, borderRadius: 7, border: '1px solid var(--border)', background: 'transparent', cursor: 'pointer', color: 'var(--text-muted)', display: 'flex', alignItems: 'center' }}>
                <RefreshCw size={12} />
              </button>
            </div>
          </div>

          {/* Stage bar — fixed */}
          <StageBar stage={stage} />

          {/* Status strip — "what needs my attention" header. Hidden on
              calm threads (zero counts + empty awaiting list). Cert-channel
              threads only carry the awaiting-reply signal; counters and
              blockers are general-channel only by design. */}
          <StatusStrip strip={negotiation?.status_strip} />

          {/* Messages — scrollable, fills remaining space */}
          <div style={{ flex: 1, overflowY: 'auto', minHeight: 0, padding: '16px 20px 8px', display: 'flex', flexDirection: 'column' }}>
            {loadingMsgs && messages.length === 0 && (
              <div style={{ textAlign: 'center', padding: 32, color: 'var(--text-muted)', fontSize: 13 }}>
                <Loader size={20} style={{ display: 'block', margin: '0 auto 10px', animation: 'spin 1s linear infinite', opacity: 0.4 }} />
                Loading messages…
              </div>
            )}
            {!loadingMsgs && messages.length === 0 && (
              <div style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', color: 'var(--text-muted)', gap: 8 }}>
                <MessageSquare size={32} style={{ opacity: 0.2 }} />
                <p style={{ margin: 0, fontSize: 13, fontWeight: 500 }}>No messages yet</p>
                <p style={{ margin: 0, fontSize: 11 }}>Messages appear when {activeThread.bankName} sends a query via A2A</p>
              </div>
            )}
            {messages.map((msg, i) => (
              <Bubble key={msg.id || i} msg={msg} bankName={activeThread.bankName} bankType={activeThread.bankType} />
            ))}
            {sendMut.isPending && (
              <div style={{ display: 'flex', gap: 8, marginTop: 4 }}>
                <div style={{ width: 30, height: 30, borderRadius: '50%', background: '#2563eb', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 11, fontWeight: 800, color: 'white', flexShrink: 0 }}>N</div>
                <div style={{ padding: '10px 14px', borderRadius: '4px 16px 16px 16px', background: 'var(--bg-elevated)', border: '1px solid var(--border)' }}>
                  <div style={{ display: 'flex', gap: 4 }}>
                    {[0,1,2].map(i => <div key={i} style={{ width: 5, height: 5, borderRadius: '50%', background: '#2563eb', animation: `bounce 1.2s ${i*0.2}s infinite` }} />)}
                  </div>
                </div>
              </div>
            )}
            <div ref={bottomRef} />
          </div>

          {/* AI suggestion banner — shown above input, only when draft exists & input empty */}
          {showBanner && (
            <AiSuggestionBanner
              draft={latestDraft}
              onUse={text => setInput(text)}
              onDismiss={() => setDismissedDraft(true)}
            />
          )}

          {/* Input area — fixed at bottom */}
          <div style={{ padding: '10px 16px 14px', borderTop: '1px solid var(--border)', background: 'var(--bg-card)', flexShrink: 0 }}>
            {sendMut.isError && (
              <p style={{ margin: '0 0 6px', fontSize: 11, color: '#dc2626', display: 'flex', alignItems: 'center', gap: 5 }}>
                ⚠ {sendMut.error?.response?.data?.detail || 'Send failed — please try again'}
              </p>
            )}
            <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end' }}>
              <textarea
                value={input}
                onChange={e => setInput(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend() } }}
                placeholder={messages.some(m => m.role === 'partner') ? `Reply to ${activeThread.bankName}'s query…` : `Message ${activeThread.bankName}…`}
                rows={2}
                style={{
                  flex: 1, padding: '10px 14px', borderRadius: 10,
                  border: '1.5px solid var(--border)',
                  background: 'var(--bg-elevated)',
                  color: 'var(--text-primary)', fontSize: 13,
                  resize: 'none', outline: 'none', fontFamily: 'inherit',
                  lineHeight: 1.5, boxSizing: 'border-box',
                  transition: 'border-color 0.15s',
                }}
                onFocus={e => e.target.style.borderColor = '#2563eb'}
                onBlur={e => e.target.style.borderColor = 'var(--border)'}
              />
              <button
                onClick={handleSend}
                disabled={!input.trim() || sendMut.isPending}
                style={{
                  width: 42, height: 42, borderRadius: 10,
                  background: input.trim() && !sendMut.isPending ? 'linear-gradient(135deg, #2563eb, #1d4ed8)' : 'var(--bg-elevated)',
                  color: input.trim() && !sendMut.isPending ? 'white' : 'var(--text-muted)',
                  cursor: input.trim() && !sendMut.isPending ? 'pointer' : 'not-allowed',
                  display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0,
                  boxShadow: input.trim() && !sendMut.isPending ? '0 2px 8px rgba(37,99,235,0.3)' : 'none',
                  transition: 'all 0.15s', border: '1.5px solid var(--border)',
                }}
              >
                <Send size={16} />
              </button>
            </div>
            <p style={{ margin: '6px 0 0', fontSize: 10, color: 'var(--text-muted)' }}>
              the Authority → {activeThread.bankName} · <span style={{ fontFamily: 'monospace' }}>TASK_SEND · query_response</span> · Google A2A · auto-refreshes every 5s
            </p>
          </div>
        </div>
      ) : (
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', color: 'var(--text-muted)', gap: 12 }}>
          <div style={{ width: 64, height: 64, borderRadius: 16, background: 'var(--bg-elevated)', border: '1px solid var(--border)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            <MessageSquare size={28} style={{ opacity: 0.3 }} />
          </div>
          <p style={{ margin: 0, fontSize: 15, fontWeight: 600, color: 'var(--text-secondary)' }}>Select a conversation</p>
          <p style={{ margin: 0, fontSize: 12 }}>Choose a bank thread from the left to view the A2A conversation</p>
        </div>
      )}

      </div>{/* end two-pane */}

      <style>{`
        @keyframes bounce { 0%,60%,100%{transform:translateY(0)} 30%{transform:translateY(-5px)} }
        @keyframes spin   { from{transform:rotate(0deg)} to{transform:rotate(360deg)} }
        .md-content p          { margin:0 0 6px }
        .md-content p:last-child { margin:0 }
        .md-content ul, .md-content ol { margin:4px 0; padding-left:18px }
        .md-content li         { margin-bottom:2px }
        .md-content strong     { font-weight:700 }
        .md-content code       { background:rgba(0,0,0,0.06); padding:1px 5px; border-radius:4px; font-size:11px }
        .md-content h2,.md-content h3 { margin:8px 0 4px; font-size:13px }
        .md-content-white p    { margin:0 0 6px; color:white }
        .md-content-white p:last-child { margin:0 }
        .md-content-white ul   { margin:4px 0; padding-left:18px; color:rgba(255,255,255,0.9) }
        .md-content-white strong { color:white; font-weight:700 }
        .md-content-white code { background:rgba(255,255,255,0.2); padding:1px 5px; border-radius:4px; font-size:11px; color:white }
      `}</style>
    </div>
  )
}
