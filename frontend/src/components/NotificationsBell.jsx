// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useState, useEffect, useRef, useCallback } from 'react'
import { createPortal } from 'react-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Bell, Check, CheckCheck, AlertTriangle, Ban, Info, X } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { notificationsApi } from '../services/api'

/**
 * NotificationsBell — sidebar entry point for operational alerts.
 *
 * These alerts exist because the failures they report used to be invisible: a Product Kit
 * that never reached a bank, or a partner counter-proposal auto-rejected for violating a
 * mandatory BRD requirement, produced nothing but a log line nobody reads. The bell is the
 * read side of that — an unread count that surfaces the problem without anyone having to
 * go looking in Admin → A2A Logs.
 *
 * Polls every 30s (same cadence as the A2A log stats). Styled to sit with the other
 * sidebar footer buttons and to collapse with the rail.
 */

const TYPE_META = {
  delivery_failed:     { icon: AlertTriangle, color: '#e06c6c', label: 'Delivery failed' },
  mandatory_rejection: { icon: Ban,           color: '#d9883b', label: 'BRD mandatory' },
  approval_request:    { icon: Info,          color: '#6ea8dc', label: 'Approval' },
  approval_done:       { icon: Check,         color: '#4caf7d', label: 'Approved' },
  revision_ready:      { icon: Info,          color: '#6ea8dc', label: 'Revision' },
  info:                { icon: Info,          color: '#8b93a7', label: 'Info' },
}

function timeAgo(iso) {
  if (!iso) return ''
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000)
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}

export default function NotificationsBell({ collapsed }) {
  const [open, setOpen] = useState(false)
  const [pos, setPos] = useState(null)     // viewport coords for the portalled panel
  const qc = useQueryClient()
  const navigate = useNavigate()
  const ref = useRef(null)
  const btnRef = useRef(null)

  const PANEL_W = 380
  const PANEL_MAX_H = 460

  // The panel is rendered in a PORTAL on document.body, not inside the sidebar. The
  // sidebar <aside> sets `overflowX: hidden` (to clip its collapse animation), which
  // clipped an absolutely-positioned child at the 220px rail — the panel was sliced down
  // its right edge. A portal escapes that ancestor's overflow and stacking context
  // entirely; we position it manually from the button's viewport rect.
  const place = useCallback(() => {
    const el = btnRef.current
    if (!el) return
    const r = el.getBoundingClientRect()
    // Sit just right of the rail, clamped inside the viewport.
    const left = Math.max(12, Math.min(r.right + 10, window.innerWidth - PANEL_W - 12))
    // Anchor the panel's BOTTOM to the button's bottom edge, so a short list stays
    // attached to the bell instead of floating away from it (top-anchoring with a fixed
    // max-height leaves a gap whenever the content is shorter than the cap).
    const bottom = Math.max(12, window.innerHeight - r.bottom)
    setPos({ bottom, left })
  }, [])

  const { data } = useQuery({
    queryKey: ['notifications'],
    queryFn: () => notificationsApi.list({ limit: 50 }).then(r => r.data),
    refetchInterval: 30000,
  })

  const items = data?.items || []
  const unread = data?.unread || 0

  // Close on outside click / Escape — a popover that traps the pointer is worse than none.
  // The panel lives in a portal, so "outside" must exclude BOTH the trigger and the panel;
  // checking only `ref` would close it on its own first click.
  useEffect(() => {
    if (!open) return
    const onDown = e => {
      const inTrigger = ref.current?.contains(e.target)
      const inPanel = e.target.closest?.('[data-notif-panel]')
      if (!inTrigger && !inPanel) setOpen(false)
    }
    const onKey = e => { if (e.key === 'Escape') setOpen(false) }
    // Reposition rather than drift: the portal is fixed to viewport coords, so a resize or
    // sidebar collapse would otherwise leave it detached from the button.
    const onMove = () => place()
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    window.addEventListener('resize', onMove)
    window.addEventListener('scroll', onMove, true)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
      window.removeEventListener('resize', onMove)
      window.removeEventListener('scroll', onMove, true)
    }
  }, [open, place])

  const refresh = () => qc.invalidateQueries({ queryKey: ['notifications'] })

  const openItem = async (n) => {
    if (!n.is_read) { try { await notificationsApi.markRead(n.id); refresh() } catch { /* non-fatal */ } }
    if (n.related_id) { setOpen(false); navigate(`/changes/${n.related_id}`) }
  }

  const markAll = async () => {
    try { await notificationsApi.markAllRead(); refresh() } catch { /* non-fatal */ }
  }

  return (
    <div ref={ref} style={{ position: 'relative' }}>
      <button
        ref={btnRef}
        onClick={() => { if (!open) place(); setOpen(o => !o) }}
        title={collapsed ? `Notifications${unread ? ` (${unread} unread)` : ''}` : undefined}
        style={{
          display: 'flex', alignItems: 'center',
          justifyContent: collapsed ? 'center' : 'flex-start', gap: '10px',
          padding: collapsed ? '9px 0' : '8px 12px', borderRadius: '6px',
          fontSize: '13px', color: 'var(--text-secondary)',
          background: 'transparent', border: 'none', cursor: 'pointer',
          width: '100%', transition: 'background 0.15s, color 0.15s',
        }}
        onMouseEnter={e => { e.currentTarget.style.background = 'var(--sidebar-hover)'; e.currentTarget.style.color = 'var(--text-primary)' }}
        onMouseLeave={e => { e.currentTarget.style.background = 'transparent'; e.currentTarget.style.color = 'var(--text-secondary)' }}
      >
        <span style={{ position: 'relative', display: 'inline-flex' }}>
          <Bell size={15} />
          {unread > 0 && (
            <span style={{
              position: 'absolute', top: -5, right: -6,
              minWidth: 15, height: 15, padding: '0 3px', borderRadius: 999,
              background: '#e06c6c', color: '#fff',
              fontSize: 9, fontWeight: 700, lineHeight: '15px', textAlign: 'center',
            }}>{unread > 99 ? '99+' : unread}</span>
          )}
        </span>
        {!collapsed && 'Notifications'}
      </button>

      {open && pos && createPortal(
        <div data-notif-panel style={{
          position: 'fixed', bottom: pos.bottom, left: pos.left,
          width: PANEL_W, maxHeight: PANEL_MAX_H, zIndex: 1000,
          display: 'flex', flexDirection: 'column',
          background: 'var(--bg-elevated)', border: '1px solid var(--border)',
          borderRadius: 8, boxShadow: '0 10px 30px rgba(0,0,0,0.35)', overflow: 'hidden',
        }}>
          <div style={{
            display: 'flex', alignItems: 'center', justifyContent: 'space-between',
            padding: '10px 12px', borderBottom: '1px solid var(--border)',
          }}>
            <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-primary)' }}>
              Notifications {unread > 0 && <span style={{ color: '#e06c6c' }}>({unread})</span>}
            </span>
            <span style={{ display: 'flex', gap: 6 }}>
              {unread > 0 && (
                <button onClick={markAll} title="Mark all read" style={iconBtn}>
                  <CheckCheck size={14} />
                </button>
              )}
              <button onClick={() => setOpen(false)} title="Close" style={iconBtn}>
                <X size={14} />
              </button>
            </span>
          </div>

          <div style={{ overflowY: 'auto', flex: 1 }}>
            {items.length === 0 && (
              <p style={{ margin: 0, padding: '28px 14px', textAlign: 'center', fontSize: 12, color: 'var(--text-muted)' }}>
                No notifications
              </p>
            )}
            {items.map(n => {
              const meta = TYPE_META[n.type] || TYPE_META.info
              const Icon = meta.icon
              return (
                <div
                  key={n.id}
                  onClick={() => openItem(n)}
                  style={{
                    display: 'flex', gap: 10, padding: '10px 12px',
                    borderBottom: '1px solid var(--border-subtle, var(--border))',
                    cursor: n.related_id ? 'pointer' : 'default',
                    background: n.is_read ? 'transparent' : 'rgba(110,168,220,0.06)',
                  }}
                >
                  <Icon size={14} style={{ color: meta.color, flexShrink: 0, marginTop: 2 }} />
                  <div style={{ minWidth: 0, flex: 1 }}>
                    <p style={{
                      margin: '0 0 2px', fontSize: 12,
                      fontWeight: n.is_read ? 500 : 700, color: 'var(--text-primary)',
                    }}>{n.title}</p>
                    <p style={{
                      margin: '0 0 3px', fontSize: 11, color: 'var(--text-muted)',
                      display: '-webkit-box', WebkitLineClamp: 3, WebkitBoxOrient: 'vertical',
                      overflow: 'hidden', whiteSpace: 'pre-wrap',
                    }}>{n.message}</p>
                    <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>
                      {meta.label} · {timeAgo(n.created_at)}
                    </span>
                  </div>
                  {!n.is_read && (
                    <button
                      title="Mark read"
                      onClick={async e => {
                        e.stopPropagation()
                        try { await notificationsApi.markRead(n.id); refresh() } catch { /* non-fatal */ }
                      }}
                      style={{ ...iconBtn, alignSelf: 'flex-start' }}
                    >
                      <Check size={12} />
                    </button>
                  )}
                </div>
              )
            })}
          </div>
        </div>,
        document.body,
      )}
    </div>
  )
}

const iconBtn = {
  display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
  width: 22, height: 22, borderRadius: 4, cursor: 'pointer',
  background: 'transparent', border: 'none', color: 'var(--text-muted)',
}
