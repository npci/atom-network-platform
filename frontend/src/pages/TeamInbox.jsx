// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useState, useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ShieldAlert, Cpu, Lock, Loader, CheckCircle, ChevronDown, ChevronUp, Send, Sparkles } from 'lucide-react'
import { escalationsApi } from '../services/api'
import StatTile, { StatTileRow } from '../components/common/StatTile'

const TEAM_META = {
  risk:    { label: 'Risk',    icon: ShieldAlert, color: '#e0a96c' },
  infosec: { label: 'InfoSec', icon: Lock,        color: '#6c9ce0' },
  tech:    { label: 'Tech',    icon: Cpu,         color: '#4caf7d' },
}

function StatusBadge({ status }) {
  const map = {
    open:      { label: 'Open',      bg: 'rgba(218,119,86,0.10)',  fg: 'var(--accent)',  bd: 'rgba(218,119,86,0.3)' },
    responded: { label: 'Responded', bg: 'rgba(76,175,125,0.10)',  fg: 'var(--success)', bd: 'rgba(76,175,125,0.3)' },
    closed:    { label: 'Closed',    bg: 'rgba(140,140,140,0.10)', fg: 'var(--text-muted)', bd: 'var(--border)' },
  }
  const s = map[status] || map.closed
  return (
    <span style={{ padding: '3px 12px', borderRadius: 20, fontSize: 11, fontWeight: 500, background: s.bg, color: s.fg, border: `1px solid ${s.bd}` }}>
      {s.label}
    </span>
  )
}

function TicketCard({ ticket, onResponded }) {
  const [expanded, setExpanded] = useState(ticket.status === 'open')
  // Pre-fill the response box with the concise AI comment draft; the reviewer
  // edits or replaces it. The full assessment below is read-only context.
  const [text, setText] = useState(ticket.ai_comment_draft || '')
  const [showAssessment, setShowAssessment] = useState(false)
  const [loading, setLoading] = useState(false)
  const meta = TEAM_META[ticket.team] || TEAM_META.tech
  const Icon = meta.icon
  const isOpen = ticket.status === 'open'
  // The ticket appears immediately on escalation; the AI draft lands a few
  // seconds later (separate LLM call). "drafting" = open ticket, no draft yet.
  const drafting = isOpen && !ticket.ai_comment_draft && !ticket.ai_suggestion

  // Auto-fill the comment box once the AI draft arrives on a later poll —
  // but only if the reviewer hasn't started typing their own.
  useEffect(() => {
    if (ticket.ai_comment_draft && text === '') setText(ticket.ai_comment_draft)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ticket.ai_comment_draft])

  const submit = async () => {
    if (!text.trim()) return
    setLoading(true)
    try {
      await escalationsApi.respond(ticket.id, text.trim())
      onResponded()
    } catch (err) {
      console.error('escalation respond failed', err)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)', borderRadius: 10, overflow: 'hidden', marginBottom: 16 }}>
      <div style={{ padding: '16px 20px', display: 'flex', alignItems: 'center', gap: 12, cursor: 'pointer', userSelect: 'none' }} onClick={() => setExpanded(e => !e)}>
        <Icon size={18} style={{ color: meta.color, flexShrink: 0 }} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <p style={{ margin: '0 0 2px', fontSize: 14, fontWeight: 600, color: 'var(--text-primary)' }}>
            {meta.label} review — {ticket.partner_name}
          </p>
          {ticket.change_title && (
            <p style={{ margin: '0 0 2px', fontSize: 12, fontWeight: 500, color: 'var(--text-secondary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              Change: {ticket.change_title}
            </p>
          )}
          <p style={{ margin: 0, fontSize: 12, color: 'var(--text-muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {ticket.escalation_reason || ticket.question_text}
          </p>
        </div>
        <StatusBadge status={ticket.status} />
        {expanded ? <ChevronUp size={15} style={{ color: 'var(--text-muted)' }} /> : <ChevronDown size={15} style={{ color: 'var(--text-muted)' }} />}
      </div>

      {expanded && (
        <div style={{ borderTop: '1px solid var(--border-subtle)' }}>
          <div style={{ padding: '18px 24px', background: 'var(--bg-base)' }}>
            <p style={{ margin: '0 0 6px', fontSize: 11, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
              Partner's query
            </p>
            <p style={{ margin: 0, fontSize: 13, lineHeight: 1.7, color: 'var(--text-primary)', whiteSpace: 'pre-wrap' }}>
              {ticket.question_text}
            </p>
          </div>

          {drafting && (
            <div style={{ padding: '12px 24px', borderTop: '1px solid var(--border-subtle)', background: 'rgba(108,156,224,0.06)', display: 'flex', alignItems: 'center', gap: 8 }}>
              <Loader size={13} style={{ color: 'var(--accent)', animation: 'spin 1s linear infinite' }} />
              <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                AI is drafting a suggested {meta.label} assessment… you can respond now or wait a moment.
              </span>
            </div>
          )}

          {ticket.ai_suggestion && (
            <div style={{ padding: '14px 24px', borderTop: '1px solid var(--border-subtle)', background: 'rgba(108,156,224,0.06)' }}>
              <div
                onClick={() => setShowAssessment(s => !s)}
                style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer', userSelect: 'none' }}
              >
                <Sparkles size={13} style={{ color: 'var(--accent)' }} />
                <p style={{ margin: 0, fontSize: 11, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
                  AI {meta.label} assessment <span style={{ fontWeight: 400, textTransform: 'none' }}>— full reasoning (reference)</span>
                </p>
                <span style={{ marginLeft: 'auto', color: 'var(--text-muted)' }}>
                  {showAssessment ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
                </span>
              </div>
              {showAssessment && (
                <p style={{ margin: '10px 0 0', fontSize: 13, lineHeight: 1.6, color: 'var(--text-secondary)', whiteSpace: 'pre-wrap' }}>
                  {ticket.ai_suggestion}
                </p>
              )}
            </div>
          )}

          {isOpen ? (
            <div style={{ padding: '18px 24px', borderTop: '1px solid var(--border-subtle)' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12 }}>
                <p style={{ margin: 0, fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
                  Your review comment <span style={{ fontWeight: 400, textTransform: 'none', color: 'var(--text-muted)' }}>— {ticket.ai_comment_draft ? 'pre-filled from AI; edit or replace' : 'write your input'}</span>
                </p>
                {ticket.ai_comment_draft && (
                  <button
                    onClick={() => setText(ticket.ai_comment_draft)}
                    style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 5, fontSize: 11, fontWeight: 600, padding: '3px 10px', borderRadius: 6, border: '1px solid var(--accent)', background: 'transparent', color: 'var(--accent)', cursor: 'pointer' }}
                  >
                    <Sparkles size={11} /> Reset to AI draft
                  </button>
                )}
              </div>
              <textarea
                value={text}
                onChange={e => setText(e.target.value)}
                placeholder="Your assessment / decision for the PM to fold into the partner reply…"
                rows={5}
                style={{ width: '100%', padding: '10px 14px', boxSizing: 'border-box', background: 'var(--bg-input)', border: '1px solid var(--border)', borderRadius: 6, color: 'var(--text-primary)', fontSize: 13, lineHeight: 1.5, resize: 'vertical', outline: 'none', fontFamily: 'inherit' }}
              />
              <button
                onClick={submit}
                disabled={!text.trim() || loading}
                style={{ marginTop: 12, display: 'flex', alignItems: 'center', gap: 8, padding: '10px 22px', background: meta.color, color: 'white', border: 'none', borderRadius: 8, fontSize: 13, fontWeight: 600, cursor: (!text.trim() || loading) ? 'not-allowed' : 'pointer', opacity: (!text.trim() || loading) ? 0.5 : 1 }}
              >
                {loading ? <Loader size={13} style={{ animation: 'spin 1s linear infinite' }} /> : <Send size={13} />}
                Submit to PM
              </button>
            </div>
          ) : ticket.team_response_text && (
            <div style={{ padding: '16px 24px', borderTop: '1px solid var(--border-subtle)' }}>
              <p style={{ margin: '0 0 6px', fontSize: 11, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
                Team response
              </p>
              <p style={{ margin: 0, fontSize: 13, color: 'var(--text-secondary)', lineHeight: 1.6, whiteSpace: 'pre-wrap' }}>
                {ticket.team_response_text}
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export default function TeamInbox() {
  const { data, isLoading, refetch } = useQuery({
    queryKey: ['escalations'],
    queryFn: () => escalationsApi.list(),
    refetchInterval: 6000,
  })

  const tickets = data || []
  const stats = {
    open:      tickets.filter(t => t.status === 'open').length,
    responded: tickets.filter(t => t.status === 'responded').length,
    total:     tickets.length,
  }

  return (
    <div style={{ padding: '32px 40px', maxWidth: 1600, margin: '0 auto' }}>
      <div style={{ marginBottom: 24 }}>
        <h1 style={{ margin: '0 0 6px', fontSize: 20, fontWeight: 600, color: 'var(--text-primary)' }}>
          Escalation Inbox
        </h1>
        <p style={{ margin: 0, fontSize: 13, color: 'var(--text-muted)' }}>
          Partner queries escalated to your team for sign-off. Your input loops back into the PM's reply.
        </p>
      </div>

      <StatTileRow>
        <StatTile label="Open"      value={stats.open}      accent="#da7756" hint={stats.open ? 'awaiting your input' : 'nothing pending'} />
        <StatTile label="Responded" value={stats.responded} accent="#4caf7d" />
        <StatTile label="Total"     value={stats.total}     accent="var(--text-secondary)" />
      </StatTileRow>

      {isLoading && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '48px 0', color: 'var(--text-muted)', fontSize: 13 }}>
          <Loader size={16} style={{ animation: 'spin 1s linear infinite' }} /> Loading escalations…
        </div>
      )}

      {!isLoading && tickets.length === 0 && (
        <div style={{ textAlign: 'center', padding: '64px 32px', background: 'var(--bg-elevated)', border: '1px solid var(--border)', borderRadius: 12 }}>
          <CheckCircle size={40} style={{ color: 'var(--success)', marginBottom: 16 }} />
          <h2 style={{ margin: '0 0 8px', fontSize: 18, fontWeight: 600, color: 'var(--text-primary)' }}>All clear</h2>
          <p style={{ margin: 0, fontSize: 13, color: 'var(--text-muted)' }}>No partner queries are currently escalated to your team.</p>
        </div>
      )}

      {tickets.map(t => (
        <TicketCard key={t.id} ticket={t} onResponded={() => refetch()} />
      ))}
    </div>
  )
}
