// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useState } from 'react'
import { t } from '../../strings'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { usersApi } from '../../services/api'
import { UserPlus, Edit2, UserX, CheckCircle, XCircle, Loader, X } from 'lucide-react'
import StatTile, { StatTileRow } from '../../components/common/StatTile'

const ROLES = [
  { value: 'product_manager',  label: 'Product Manager' },
  { value: 'tech_lead',        label: 'Tech Lead' },
  { value: 'infosec_reviewer', label: 'InfoSec Reviewer' },
  { value: 'risk_reviewer',    label: 'Risk Reviewer' },
  { value: 'product_owner',    label: 'Product Owner' },
  { value: 'admin',            label: 'Admin' },
]

const ROLE_COLORS = {
  admin:            { bg: 'rgba(218,119,86,0.10)', color: 'var(--accent)',  border: 'rgba(218,119,86,0.3)' },
  product_manager:  { bg: 'rgba(76,175,125,0.08)', color: 'var(--success)', border: 'rgba(76,175,125,0.3)' },
  tech_lead:        { bg: 'rgba(100,149,237,0.10)', color: '#6495ed',       border: 'rgba(100,149,237,0.3)' },
  infosec_reviewer: { bg: 'rgba(186,85,211,0.10)', color: '#ba55d3',        border: 'rgba(186,85,211,0.3)' },
  risk_reviewer:    { bg: 'rgba(255,165,0,0.10)',  color: '#ffa500',        border: 'rgba(255,165,0,0.3)' },
  product_owner:    { bg: 'rgba(70,130,180,0.10)', color: '#4682b4',        border: 'rgba(70,130,180,0.3)' },
}

function RoleBadge({ role }) {
  const s = ROLE_COLORS[role] || ROLE_COLORS.product_owner
  const label = ROLES.find(r => r.value === role)?.label || role
  return (
    <span style={{
      padding: '2px 10px', borderRadius: '20px', fontSize: '11px', fontWeight: '500',
      background: s.bg, color: s.color, border: `1px solid ${s.border}`,
    }}>{label}</span>
  )
}

// All assigned roles as badges; the active role is full-opacity, the rest dimmed.
function RolesBadges({ roles, active }) {
  const list = roles && roles.length ? roles : (active ? [active] : [])
  return (
    <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap', justifyContent: 'flex-end' }}>
      {list.map(r => (
        <span key={r} title={r === active ? 'Active role' : 'Assigned role'}
              style={{ opacity: r === active ? 1 : 0.5 }}>
          <RoleBadge role={r} />
        </span>
      ))}
    </div>
  )
}

const EMPTY_FORM = { username: '', email: '', full_name: '', password: '', roles: ['product_manager'] }

function UserModal({ user, onClose, onSaved }) {
  const isEdit = !!user
  const [form, setForm] = useState(
    isEdit
      ? { username: user.username, email: user.email, full_name: user.full_name || '', roles: user.roles || [user.role], password: '' }
      : { ...EMPTY_FORM }
  )
  const [loading, setLoading] = useState(false)
  const [error, setError]     = useState('')

  const set = (k, v) => setForm(f => ({ ...f, [k]: v }))

  const handleSave = async () => {
    setError('')
    if (!form.username || !form.email || (!isEdit && !form.password)) {
      setError('Username, email, and password are required.')
      return
    }
    if (!form.roles || form.roles.length === 0) {
      setError('Select at least one role.')
      return
    }
    // Mirror the backend's password_min_length validator — catches the most
    // common 422 cause before round-tripping to the server, and gives the
    // user a precise message instead of a generic FastAPI validation array.
    if (!isEdit && form.password.length < 8) {
      setError('Password must be at least 8 characters.')
      return
    }
    setLoading(true)
    try {
      if (isEdit) {
        const payload = { full_name: form.full_name, email: form.email, roles: form.roles }
        await usersApi.update(user.id, payload)
      } else {
        await usersApi.create({
          username: form.username,
          email: form.email,
          full_name: form.full_name || null,
          password: form.password,
          roles: form.roles,
        })
      }
      onSaved()
      onClose()
    } catch (err) {
      // FastAPI 422 returns `detail` as an ARRAY of {loc, msg, type} objects;
      // earlier we passed it straight to setError, so React rendered
      // nothing (arrays of objects render as silent garbage). Flatten to a
      // single readable string before display.
      const detail = err.response?.data?.detail
      let msg = 'An error occurred.'
      if (typeof detail === 'string') {
        msg = detail
      } else if (Array.isArray(detail) && detail.length > 0) {
        msg = detail
          .map(d => {
            const field = Array.isArray(d?.loc) ? d.loc[d.loc.length - 1] : '?'
            return `${field}: ${d?.msg || 'invalid'}`
          })
          .join(' · ')
      }
      setError(msg)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={{
      position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.5)',
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      zIndex: 1000,
    }} onClick={e => e.target === e.currentTarget && onClose()}>
      <div style={{
        background: 'var(--bg-elevated)', border: '1px solid var(--border)',
        borderRadius: '12px', padding: '28px', width: '440px', maxWidth: '90vw',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '24px' }}>
          <h2 style={{ margin: 0, fontSize: '16px', fontWeight: '600', color: 'var(--text-primary)' }}>
            {isEdit ? 'Edit User' : 'Create User'}
          </h2>
          <button onClick={onClose} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-muted)' }}>
            <X size={18} />
          </button>
        </div>

        {error && (
          <div style={{
            padding: '10px 14px', borderRadius: '6px', marginBottom: '16px',
            background: 'rgba(224,108,108,0.10)', border: '1px solid rgba(224,108,108,0.3)',
            fontSize: '13px', color: 'var(--danger)',
          }}>{error}</div>
        )}

        <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
          {[
            { key: 'full_name', label: 'Full Name', placeholder: 'e.g. Priya Sharma' },
            { key: 'username',  label: 'Username *', placeholder: 'e.g. priya.sharma', disabled: isEdit },
            { key: 'email',     label: 'Email *', placeholder: t('ph.user.email'), type: 'email' },
            ...(!isEdit ? [{ key: 'password', label: 'Password *', placeholder: 'Min 8 characters', type: 'password' }] : []),
          ].map(({ key, label, placeholder, type = 'text', disabled = false }) => (
            <div key={key}>
              <label style={{ display: 'block', fontSize: '12px', fontWeight: '500', color: 'var(--text-secondary)', marginBottom: '6px' }}>
                {label}
              </label>
              <input
                type={type}
                value={form[key] || ''}
                onChange={e => set(key, e.target.value)}
                placeholder={placeholder}
                disabled={disabled}
                style={{
                  width: '100%', boxSizing: 'border-box',
                  padding: '9px 12px', background: disabled ? 'var(--bg-card)' : 'var(--bg-input)',
                  border: '1px solid var(--border)', borderRadius: '6px',
                  color: disabled ? 'var(--text-muted)' : 'var(--text-primary)',
                  fontSize: '13px', outline: 'none',
                }}
                onFocus={e => { if (!disabled) e.target.style.borderColor = 'var(--accent)' }}
                onBlur={e => e.target.style.borderColor = 'var(--border)'}
              />
            </div>
          ))}

          <div>
            <label style={{ display: 'block', fontSize: '12px', fontWeight: '500', color: 'var(--text-secondary)', marginBottom: '6px' }}>
              Roles * <span style={{ color: 'var(--text-muted)', fontWeight: 400 }}>(the user can switch between assigned roles)</span>
            </label>
            <div style={{
              display: 'flex', flexDirection: 'column', gap: '8px',
              padding: '10px 12px', border: '1px solid var(--border)',
              borderRadius: '6px', background: 'var(--bg-input)',
            }}>
              {ROLES.map(r => (
                <label key={r.value} style={{
                  display: 'flex', alignItems: 'center', gap: '8px',
                  fontSize: '13px', color: 'var(--text-primary)', cursor: 'pointer',
                }}>
                  <input
                    type="checkbox"
                    checked={form.roles.includes(r.value)}
                    onChange={() => set('roles',
                      form.roles.includes(r.value)
                        ? form.roles.filter(x => x !== r.value)
                        : [...form.roles, r.value])}
                  />
                  {r.label}
                </label>
              ))}
            </div>
          </div>
        </div>

        <div style={{ display: 'flex', gap: '10px', justifyContent: 'flex-end', marginTop: '24px' }}>
          <button onClick={onClose} style={{
            padding: '9px 18px', background: 'var(--bg-card)', border: '1px solid var(--border)',
            borderRadius: '6px', fontSize: '13px', color: 'var(--text-secondary)', cursor: 'pointer',
          }}>Cancel</button>
          <button onClick={handleSave} disabled={loading} style={{
            padding: '9px 18px', background: 'var(--accent)', color: 'white',
            border: 'none', borderRadius: '6px', fontSize: '13px', fontWeight: '600',
            cursor: loading ? 'not-allowed' : 'pointer', opacity: loading ? 0.7 : 1,
            display: 'flex', alignItems: 'center', gap: '8px',
          }}>
            {loading && <Loader size={13} style={{ animation: 'spin 1s linear infinite' }} />}
            {isEdit ? 'Save Changes' : 'Create User'}
          </button>
        </div>
      </div>
    </div>
  )
}

export default function UserManagement() {
  const qc = useQueryClient()
  const [modal, setModal] = useState(null)   // null | 'create' | user-object

  const { data, isLoading } = useQuery({
    queryKey: ['users'],
    queryFn: () => usersApi.list().then(r => r.data),
  })

  const users = data?.items || []

  const handleDeactivate = async (userId) => {
    if (!window.confirm('Deactivate this user? They will lose login access.')) return
    await usersApi.deactivate(userId)
    qc.invalidateQueries({ queryKey: ['users'] })
  }

  const handleReactivate = async (userId) => {
    await usersApi.update(userId, { is_active: true })
    qc.invalidateQueries({ queryKey: ['users'] })
  }

  const onSaved = () => qc.invalidateQueries({ queryKey: ['users'] })

  // Group by active / inactive
  const active   = users.filter(u => u.is_active)
  const inactive = users.filter(u => !u.is_active)
  const admins   = users.filter(u => u.role === 'admin')

  return (
    <div style={{ padding: '32px 40px', maxWidth: 1600, margin: '0 auto' }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '24px' }}>
        <div>
          <h1 style={{ margin: '0 0 4px', fontSize: '20px', fontWeight: '600', color: 'var(--text-primary)' }}>
            User Management
          </h1>
          <p style={{ margin: 0, fontSize: '13px', color: 'var(--text-muted)' }}>
            Create and manage reviewer accounts for the approval workflow
          </p>
        </div>
        <button
          onClick={() => setModal('create')}
          style={{
            display: 'flex', alignItems: 'center', gap: '8px',
            padding: '9px 18px', background: 'var(--accent)', color: 'white',
            border: 'none', borderRadius: '8px', fontSize: '13px', fontWeight: '600', cursor: 'pointer',
          }}
        >
          <UserPlus size={15} /> Add User
        </button>
      </div>

      <StatTileRow>
        <StatTile label="Total Users" value={users.length}    accent="var(--text-secondary)" />
        <StatTile label="Active"      value={active.length}   accent="#4caf7d" />
        <StatTile label="Inactive"    value={inactive.length} accent="var(--text-muted)"
                  hint={inactive.length ? 'no login allowed' : null} />
        <StatTile label="Admins"      value={admins.length}   accent="#da7756"
                  hint="full access" />
      </StatTileRow>

      {/* Role guide */}
      <div style={{
        padding: '14px 18px', borderRadius: '8px', marginBottom: '24px',
        background: 'rgba(218,119,86,0.06)', border: '1px solid rgba(218,119,86,0.2)',
      }}>
        <p style={{ margin: '0 0 6px', fontSize: '12px', fontWeight: '600', color: 'var(--accent)' }}>
          BRD Approval Roles
        </p>
        <p style={{ margin: 0, fontSize: '12px', color: 'var(--text-muted)', lineHeight: '1.6' }}>
          BRD submissions require sign-off from all four reviewer roles:&nbsp;
          <strong style={{ color: 'var(--text-secondary)' }}>Product Manager, Tech Lead, InfoSec Reviewer, Risk Reviewer</strong>.
          A user can be assigned multiple roles and switch between them; to cast an
          approval they switch to the applicable role.
        </p>
      </div>

      {isLoading && (
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px', padding: '48px 0', color: 'var(--text-muted)', fontSize: '13px' }}>
          <Loader size={16} style={{ animation: 'spin 1s linear infinite' }} /> Loading users…
        </div>
      )}

      {/* Active users table */}
      {active.length > 0 && (
        <div style={{
          background: 'var(--bg-elevated)', border: '1px solid var(--border)',
          borderRadius: '10px', overflow: 'hidden', marginBottom: '24px',
        }}>
          <div style={{ padding: '12px 20px', borderBottom: '1px solid var(--border-subtle)' }}>
            <p style={{ margin: 0, fontSize: '11px', fontWeight: '600', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
              Active Users ({active.length})
            </p>
          </div>
          {active.map((u, i) => (
            <div key={u.id} style={{
              display: 'flex', alignItems: 'center', gap: '14px',
              padding: '14px 20px',
              borderBottom: i < active.length - 1 ? '1px solid var(--border-subtle)' : 'none',
            }}>
              {/* Avatar */}
              <div style={{
                width: '36px', height: '36px', borderRadius: '50%',
                background: ROLE_COLORS[u.role]?.bg || 'var(--bg-card)',
                border: `1px solid ${ROLE_COLORS[u.role]?.border || 'var(--border)'}`,
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                fontSize: '13px', fontWeight: '700',
                color: ROLE_COLORS[u.role]?.color || 'var(--text-muted)',
                flexShrink: 0,
              }}>
                {(u.full_name || u.username).charAt(0).toUpperCase()}
              </div>

              <div style={{ flex: 1 }}>
                <p style={{ margin: '0 0 2px', fontSize: '14px', fontWeight: '500', color: 'var(--text-primary)' }}>
                  {u.full_name || u.username}
                  {u.full_name && (
                    <span style={{ fontSize: '12px', color: 'var(--text-muted)', marginLeft: '6px' }}>@{u.username}</span>
                  )}
                </p>
                <p style={{ margin: 0, fontSize: '12px', color: 'var(--text-muted)' }}>{u.email}</p>
              </div>

              <RolesBadges roles={u.roles} active={u.active_role || u.role} />

              <div style={{ display: 'flex', gap: '8px', flexShrink: 0 }}>
                <button
                  onClick={() => setModal(u)}
                  style={{
                    display: 'flex', alignItems: 'center', gap: '5px',
                    padding: '6px 12px', background: 'var(--bg-card)',
                    border: '1px solid var(--border)', borderRadius: '6px',
                    fontSize: '12px', color: 'var(--text-secondary)', cursor: 'pointer',
                  }}
                >
                  <Edit2 size={12} /> Edit
                </button>
                {u.role !== 'admin' && (
                  <button
                    onClick={() => handleDeactivate(u.id)}
                    style={{
                      display: 'flex', alignItems: 'center', gap: '5px',
                      padding: '6px 12px', background: 'rgba(224,108,108,0.08)',
                      border: '1px solid rgba(224,108,108,0.3)', borderRadius: '6px',
                      fontSize: '12px', color: 'var(--danger)', cursor: 'pointer',
                    }}
                  >
                    <UserX size={12} /> Deactivate
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Inactive users */}
      {inactive.length > 0 && (
        <div style={{
          background: 'var(--bg-elevated)', border: '1px solid var(--border)',
          borderRadius: '10px', overflow: 'hidden', opacity: 0.7,
        }}>
          <div style={{ padding: '12px 20px', borderBottom: '1px solid var(--border-subtle)' }}>
            <p style={{ margin: 0, fontSize: '11px', fontWeight: '600', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
              Inactive Users ({inactive.length})
            </p>
          </div>
          {inactive.map((u, i) => (
            <div key={u.id} style={{
              display: 'flex', alignItems: 'center', gap: '14px',
              padding: '14px 20px',
              borderBottom: i < inactive.length - 1 ? '1px solid var(--border-subtle)' : 'none',
            }}>
              <div style={{
                width: '36px', height: '36px', borderRadius: '50%',
                background: 'var(--bg-card)', border: '1px solid var(--border)',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                fontSize: '13px', fontWeight: '700', color: 'var(--text-muted)', flexShrink: 0,
              }}>
                {(u.full_name || u.username).charAt(0).toUpperCase()}
              </div>
              <div style={{ flex: 1 }}>
                <p style={{ margin: '0 0 2px', fontSize: '14px', fontWeight: '500', color: 'var(--text-muted)' }}>
                  {u.full_name || u.username}
                </p>
                <p style={{ margin: 0, fontSize: '12px', color: 'var(--text-muted)' }}>{u.email}</p>
              </div>
              <RolesBadges roles={u.roles} active={u.active_role || u.role} />
              <button
                onClick={() => handleReactivate(u.id)}
                style={{
                  display: 'flex', alignItems: 'center', gap: '5px',
                  padding: '6px 12px', background: 'rgba(76,175,125,0.08)',
                  border: '1px solid rgba(76,175,125,0.3)', borderRadius: '6px',
                  fontSize: '12px', color: 'var(--success)', cursor: 'pointer',
                }}
              >
                <CheckCircle size={12} /> Reactivate
              </button>
            </div>
          ))}
        </div>
      )}

      {/* Modals */}
      {modal === 'create' && (
        <UserModal onClose={() => setModal(null)} onSaved={onSaved} />
      )}
      {modal && modal !== 'create' && (
        <UserModal user={modal} onClose={() => setModal(null)} onSaved={onSaved} />
      )}
    </div>
  )
}
