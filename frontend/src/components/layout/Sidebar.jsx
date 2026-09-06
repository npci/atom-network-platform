// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useState, useEffect } from 'react'
import { t } from '../../strings'
import { NavLink } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  LayoutDashboard, FilePlus, CheckSquare, Users, LogOut,
  Sun, Moon, BookOpen, ScrollText, PanelLeftClose, PanelLeftOpen, Code2, Settings, FileText,
  Award, MessageSquare, BadgeCheck, BarChart3, Building2, Network, KeyRound, ShieldAlert,
  ListChecks, Activity, GitCompare, Send, Braces, Package, Server,
} from 'lucide-react'
import { useAuth } from '../../hooks/useAuth'
import { useTheme } from '../../context/useTheme'
import { BRAND_NAME, brandLogo } from '../../brand'
import { silentApi } from '../../services/api'
import { BASE_PATH } from '../../utils/basePath'
import ActiveJobsTray from '../jobs/ActiveJobsTray'
import ChangePasswordModal from '../common/ChangePasswordModal'
import NotificationsBell from '../NotificationsBell'


const certificationItems = [
  { to: '/certification/dashboard',       icon: BarChart3,     label: 'CR Dashboard'    },
  { to: '/certification/partners',        icon: Building2,     label: 'Partner Entries' },
  { to: '/certification/agent-messaging', icon: MessageSquare, label: 'Agent Messaging' },
  { to: '/certification/status',          icon: BadgeCheck,    label: 'Cert Status'     },
]

const navItems = [
  { to: '/dashboard',   icon: LayoutDashboard, label: 'Dashboard' },
  { to: '/changes/new', icon: FilePlus,         label: 'New Change' },
  { to: '/product-kit', icon: Package,          label: 'Product Kit' },
  { to: '/approvals',   icon: CheckSquare,      label: 'Approvals' },
]

const adminItems = [
  { to: '/admin/partners',           icon: Users,      label: 'Partners' },
  { to: '/admin/users',              icon: Users,      label: 'Users' },
  { to: '/admin/product-knowledge',  icon: BookOpen,   label: 'Product Knowledge' },
  { to: '/admin/code-knowledge',     icon: FileText,   label: 'Code Knowledge' },
  { to: '/admin/code-indexing',      icon: Code2,      label: 'Code Indexing' },
  { to: '/admin/api-registry',       icon: Braces,     label: 'API Registry' },
  { to: '/admin/agentic',            icon: Code2,      label: 'Agentic Codegen' },
  { to: '/usage',                    icon: Activity,   label: 'LLM Usage' },
  { to: '/admin/configuration',      icon: Settings,   label: 'Configuration' },
  { to: '/admin/build-host',         icon: Server,     label: 'Build Host' },
  { to: '/admin/authority-policy',        icon: FileText,   label: t('nav.policy') },
  { to: '/admin/governance-skills',  icon: ShieldAlert, label: 'Governance Skills' },
  { to: '/admin/eval-policy',        icon: ShieldAlert, label: 'Eval Policy' },
  { to: '/admin/eval-logs',          icon: ListChecks,  label: 'Eval Logs' },
  { to: '/admin/eval-metrics',       icon: Activity,    label: 'Eval Metrics' },
  { to: '/admin/eval-compare',       icon: GitCompare,  label: 'Eval Compare' },
  { to: '/admin/a2a-logs',           icon: Network,    label: 'A2A Logs' },
  { to: '/admin/cert-a2a',           icon: Send,       label: 'Cert A2A Trigger' },
  { to: '/admin/logs',               icon: ScrollText, label: 'Logs' },
]

function NavItem({ to, icon: Icon, label, collapsed, badge }) {
  return (
    <NavLink
      to={to}
      title={collapsed ? label : undefined}
      style={({ isActive }) => ({
        display: 'flex',
        alignItems: 'center',
        justifyContent: collapsed ? 'center' : 'flex-start',
        gap: '10px',
        padding: collapsed ? '9px 0' : '8px 12px',
        borderRadius: '6px',
        fontSize: '13px',
        textDecoration: 'none',
        marginBottom: '2px',
        color: isActive ? 'var(--text-primary)' : 'var(--text-secondary)',
        background: isActive ? 'var(--sidebar-active)' : 'transparent',
        borderLeft: collapsed ? 'none' : (isActive ? '2px solid var(--accent)' : '2px solid transparent'),
        transition: 'background 0.15s, color 0.15s',
        fontWeight: isActive ? '500' : '400',
      })}
    >
      <div style={{ position: 'relative', display: 'flex', alignItems: 'center', flexShrink: 0 }}>
        <Icon size={15} />
        {badge > 0 && collapsed && (
          <span style={{ position: 'absolute', top: -5, right: -5, minWidth: 12, height: 12, borderRadius: 6, background: '#f85149', color: 'white', fontSize: 8, fontWeight: 800, display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '0 2px' }}>
            {badge > 9 ? '9+' : badge}
          </span>
        )}
      </div>
      {!collapsed && (
        <>
          <span style={{ flex: 1 }}>{label}</span>
          {badge > 0 && (
            <span style={{ fontSize: 10, fontWeight: 700, minWidth: 17, height: 17, borderRadius: 9, background: '#f85149', color: 'white', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '0 3px' }}>
              {badge > 9 ? '9+' : badge}
            </span>
          )}
        </>
      )}
    </NavLink>
  )
}

export default function Sidebar() {
  const { user, logout, switchRole }  = useAuth()
  const { theme, toggle } = useTheme()
  const isDark            = theme === 'dark'
  const [collapsed, setCollapsed] = useState(false)
  const [, forceRender] = useState(0)   // used to re-render when read-state changes
  const [showChangePassword, setShowChangePassword] = useState(false)

  // Re-render when AgentMessaging marks a thread as read
  useEffect(() => {
    const handler = () => forceRender(n => n + 1)
    window.addEventListener('cert_read_change', handler)
    return () => window.removeEventListener('cert_read_change', handler)
  }, [])

  // Agent Messaging is cert-channel only — sidebar badge mirrors the
  // cert inbox, not the full thread set (Phase C clarifications surface
  // on each change-request page, not here).
  const { data: threadsData } = useQuery({
    queryKey: ['sidebar-unread'],
    queryFn:  () => silentApi.get(`${BASE_PATH}threads?kind=cert`).then(r => r.data).catch(() => ({ threads: [], total_unread: 0 })),
    refetchInterval: 15000,
    retry: false,
    // Gate on the user, not on a stored token: the session is an httpOnly
    // cookie that JavaScript cannot see, so a localStorage probe here would
    // be permanently false and silently disable this poll.
    enabled: !!user,
  })


  // Read-state is stored as {thread_id: read_at_iso} so a thread that
  // was marked read but has since received a new partner message is
  // counted as unread again. The old shape (a flat array of thread
  // ids) is also tolerated to avoid one-time clear-on-upgrade UX.
  const rawRead = JSON.parse(localStorage.getItem('cert_read_threads') || '[]')
  const readMap = Array.isArray(rawRead)
    ? Object.fromEntries(rawRead.map(id => [id, '9999-12-31T00:00:00Z']))  // legacy: treat as "always read"
    : (rawRead || {})
  const messagingUnread = (threadsData?.threads || []).reduce((sum, t) => {
    const readAt = readMap[t.thread_id]
    const isRead = readAt && t.latest_at && t.latest_at <= readAt
    return sum + (isRead ? 0 : (t.unread_count || 0))
  }, 0)

  return (
    <aside style={{
      width: collapsed ? '56px' : '220px',
      height: '100vh',
      background: 'var(--sidebar-bg)',
      borderRight: '1px solid var(--border-subtle)',
      display: 'flex',
      flexDirection: 'column',
      flexShrink: 0,
      transition: 'width 0.22s ease',
      overflowX: 'hidden',  /* clip horizontal during collapse animation */
      /* no overflowY — let the nav child control vertical scroll */
    }}>

      {/* Brand */}
      <div style={{
        padding: collapsed ? '14px 0' : '16px 16px 12px',
        borderBottom: '1px solid var(--border-subtle)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: collapsed ? 'center' : 'space-between',
        gap: '8px',
        minHeight: '64px',
      }}>
        {!collapsed && (
          <div style={{ flex: 1, minWidth: 0 }}>
            {/* The wordmark is part of the artwork, so the text heading that
                used to sit under it is gone — keeping both would say the name
                twice. `alt` carries it for screen readers and for the case
                where the image fails to load. */}
            <img
              src={brandLogo(theme)}
              alt={BRAND_NAME}
              style={{
                height: '44px',
                width: 'auto',
                maxWidth: '100%',
                display: 'block',
                objectFit: 'contain',
              }}
            />
          </div>
        )}

        {/* Collapse / expand toggle */}
        <button
          onClick={() => setCollapsed(c => !c)}
          title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            width: '28px',
            height: '28px',
            borderRadius: '6px',
            border: 'none',
            background: 'transparent',
            color: 'var(--text-muted)',
            cursor: 'pointer',
            flexShrink: 0,
            transition: 'background 0.15s, color 0.15s',
          }}
          onMouseEnter={e => {
            e.currentTarget.style.background = 'var(--sidebar-hover)'
            e.currentTarget.style.color = 'var(--text-primary)'
          }}
          onMouseLeave={e => {
            e.currentTarget.style.background = 'transparent'
            e.currentTarget.style.color = 'var(--text-muted)'
          }}
        >
          {collapsed ? <PanelLeftOpen size={15} /> : <PanelLeftClose size={15} />}
        </button>
      </div>

      {/* Nav — scrollable so all items are reachable on small screens */}
      <nav style={{ flex: 1, minHeight: 0, overflowY: 'auto', overflowX: 'hidden', padding: collapsed ? '10px 6px' : '12px 8px' }}>
        {navItems.map((item) => (
          <NavItem key={item.to} {...item} collapsed={collapsed} />
        ))}

        {/* Escalation inbox — Risk/InfoSec/Tech reviewers + PM/PO/admin oversight */}
        {['risk_reviewer', 'infosec_reviewer', 'tech_lead', 'product_manager', 'product_owner', 'admin'].includes(user?.role) && (
          <NavItem to="/escalations" icon={ShieldAlert} label="Escalations" collapsed={collapsed} />
        )}

        {/* Certification section */}
        {!collapsed && (
          <div style={{ padding: '16px 12px 6px', fontSize: '10px', color: 'var(--text-muted)', letterSpacing: '0.08em', textTransform: 'uppercase', display: 'flex', alignItems: 'center', gap: '6px' }}>
            <Award size={10} /> Certification
          </div>
        )}
        {collapsed && <div style={{ height: '1px', background: 'var(--border-subtle)', margin: '10px 4px' }} />}
        {certificationItems.map((item) => (
          <NavItem key={item.to} {...item} collapsed={collapsed}
            badge={item.to === '/certification/agent-messaging' ? messagingUnread : 0}
          />
        ))}

        {user?.role === 'admin' && (
          <>
            {!collapsed && (
              <div style={{
                padding: '16px 12px 6px',
                fontSize: '10px',
                color: 'var(--text-muted)',
                letterSpacing: '0.08em',
                textTransform: 'uppercase',
              }}>
                Admin
              </div>
            )}
            {collapsed && (
              <div style={{
                height: '1px',
                background: 'var(--border-subtle)',
                margin: '10px 4px',
              }} />
            )}
            {adminItems.map((item) => (
              <NavItem key={item.to} {...item} collapsed={collapsed} />
            ))}
          </>
        )}
        {/* Agentic Codegen is admin + tech-lead. Admins see it in the Admin block above;
            tech-leads get a standalone link here (they don't have the rest of Admin). */}
        {user?.role === 'tech_lead' && (
          <>
            <NavItem to="/admin/agentic" icon={Code2} label="Agentic Codegen" collapsed={collapsed} />
            <NavItem to="/usage" icon={Activity} label="LLM Usage" collapsed={collapsed} />
          </>
        )}
      </nav>

      {/* Active jobs tray — only when sidebar is expanded (no room collapsed). */}
      {!collapsed && <ActiveJobsTray />}

      {/* Bottom: theme toggle + user + logout */}
      <div style={{
        padding: collapsed ? '8px 6px 12px' : '8px 8px 12px',
        borderTop: '1px solid var(--border-subtle)',
      }}>
        {/* Theme toggle */}
        <button
          onClick={toggle}
          title={isDark ? 'Switch to light mode' : 'Switch to dark mode'}
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: collapsed ? 'center' : 'flex-start',
            gap: '10px',
            padding: collapsed ? '9px 0' : '8px 12px',
            borderRadius: '6px',
            fontSize: '13px',
            color: 'var(--text-secondary)',
            background: 'transparent',
            border: 'none',
            cursor: 'pointer',
            width: '100%',
            marginBottom: '2px',
            transition: 'background 0.15s, color 0.15s',
          }}
          onMouseEnter={e => {
            e.currentTarget.style.background = 'var(--sidebar-hover)'
            e.currentTarget.style.color = 'var(--text-primary)'
          }}
          onMouseLeave={e => {
            e.currentTarget.style.background = 'transparent'
            e.currentTarget.style.color = 'var(--text-secondary)'
          }}
        >
          {isDark ? <Sun size={15} /> : <Moon size={15} />}
          {!collapsed && (isDark ? 'Light mode' : 'Dark mode')}
        </button>

        {/* User info */}
        <div style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: collapsed ? 'center' : 'flex-start',
          gap: '10px',
          padding: collapsed ? '6px 0' : '8px 12px',
          marginBottom: '2px',
        }}
          title={collapsed ? `${user?.full_name || user?.username} (${user?.role?.replace(/_/g, ' ')})` : undefined}
        >
          <div style={{
            width: '28px', height: '28px',
            borderRadius: '50%',
            background: 'var(--accent)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            fontSize: '11px', fontWeight: '700', color: 'white',
            flexShrink: 0,
          }}>
            {(user?.full_name?.[0] || user?.username?.[0] || '?').toUpperCase()}
          </div>
          {!collapsed && (
            <div style={{ flex: 1, minWidth: 0 }}>
              <p style={{
                margin: 0, fontSize: '12px',
                color: 'var(--text-primary)', fontWeight: '500',
                overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
              }}>
                {user?.full_name || user?.username}
              </p>
              <p style={{
                margin: 0, fontSize: '11px',
                color: 'var(--text-muted)', textTransform: 'capitalize',
              }}>
                {user?.role?.replace(/_/g, ' ')}
              </p>
            </div>
          )}
        </div>

        {/* Active-role switcher — only when the user is assigned more than one role.
            Changing it flips the active role (RBAC + pending approvals follow it). */}
        {!collapsed && (user?.roles?.length || 0) > 1 && (
          <div style={{ padding: '2px 12px 8px' }}>
            <label style={{
              display: 'block', fontSize: '10px', fontWeight: 600,
              color: 'var(--text-muted)', textTransform: 'uppercase',
              letterSpacing: '0.06em', marginBottom: '4px',
            }}>
              Active role
            </label>
            <select
              value={user?.active_role || user?.role || ''}
              onChange={async (e) => { try { await switchRole(e.target.value) } catch { /* role switch failed; the select simply stays on its old value */ } }}
              style={{
                width: '100%', fontSize: '12px', padding: '5px 8px',
                borderRadius: '6px', border: '1px solid var(--border)',
                background: 'var(--bg-elevated)', color: 'var(--text-primary)',
                textTransform: 'capitalize', cursor: 'pointer',
              }}
            >
              {(user?.roles || []).map(r => (
                <option key={r} value={r}>{r.replace(/_/g, ' ')}</option>
              ))}
            </select>
          </div>
        )}

        {/* Operational alerts — failed bank deliveries, BRD mandatory rejections. */}
        <NotificationsBell collapsed={collapsed} />

        {/* Change password */}
        <button
          onClick={() => setShowChangePassword(true)}
          title={collapsed ? 'Change password' : undefined}
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: collapsed ? 'center' : 'flex-start',
            gap: '10px',
            padding: collapsed ? '9px 0' : '8px 12px',
            marginBottom: '2px',
            borderRadius: '6px',
            fontSize: '13px', color: 'var(--text-secondary)',
            background: 'transparent', border: 'none', cursor: 'pointer',
            width: '100%', transition: 'background 0.15s, color 0.15s',
          }}
          onMouseEnter={e => {
            e.currentTarget.style.background = 'var(--sidebar-hover)'
            e.currentTarget.style.color = 'var(--text-primary)'
          }}
          onMouseLeave={e => {
            e.currentTarget.style.background = 'transparent'
            e.currentTarget.style.color = 'var(--text-secondary)'
          }}
        >
          <KeyRound size={15} />
          {!collapsed && 'Change password'}
        </button>

        {/* Sign out */}
        <button
          onClick={logout}
          title={collapsed ? 'Sign out' : undefined}
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: collapsed ? 'center' : 'flex-start',
            gap: '10px',
            padding: collapsed ? '9px 0' : '8px 12px',
            borderRadius: '6px',
            fontSize: '13px', color: 'var(--text-secondary)',
            background: 'transparent', border: 'none', cursor: 'pointer',
            width: '100%', transition: 'background 0.15s, color 0.15s',
          }}
          onMouseEnter={e => {
            e.currentTarget.style.background = 'var(--sidebar-hover)'
            e.currentTarget.style.color = 'var(--text-primary)'
          }}
          onMouseLeave={e => {
            e.currentTarget.style.background = 'transparent'
            e.currentTarget.style.color = 'var(--text-secondary)'
          }}
        >
          <LogOut size={15} />
          {!collapsed && 'Sign out'}
        </button>
      </div>

      <ChangePasswordModal
        open={showChangePassword}
        onClose={() => setShowChangePassword(false)}
      />
    </aside>
  )
}
