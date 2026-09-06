// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

/**
 * DataTable — thin wrapper over <table> with sticky header, zebra rows,
 * empty/loading/error states, and density toggle.
 *
 *   <DataTable
 *     columns={[
 *       { key: 'id',    label: 'CR Id',     width: 110, render: r => <span className="id-mono">{r.id.slice(0,8)}</span> },
 *       { key: 'title', label: 'Title' },
 *       { key: 'status',label: 'Status',    width: 120, render: r => <StatusBadge kind="assignment" value={r.status} /> },
 *     ]}
 *     rows={data}
 *     onRowClick={r => navigate(`/certification/changes/${r.id}`)}
 *     loading={isLoading}
 *     error={error}
 *     emptyTitle="No change requests yet"
 *     emptyDescription="Assign partners on a change request to see it here."
 *     density="comfortable"
 *   />
 *
 * Rows are clickable when onRowClick is provided. Keyboard: Enter activates,
 * arrow keys move focus.
 */
import { useState, useRef, useEffect } from 'react'
import { Loader, Inbox, AlertCircle, RefreshCw } from 'lucide-react'

export default function DataTable({
  columns,
  rows,
  loading,
  error,
  onRetry,
  onRowClick,
  emptyTitle = 'No data',
  emptyDescription,
  emptyAction,
  density = 'comfortable',
  rowKey = 'id',
  showDensityToggle = false,
  toolbar,
  // Optional row-expansion support. When provided, each row becomes
  // toggleable; the renderExpanded function returns the JSX for the
  // sub-row body.
  expandable,        // { isExpanded: row => bool, onToggle: row => void, renderExpanded: row => JSX }
}) {
  const [localDensity, setLocalDensity] = useState(density)
  const [focusIdx, setFocusIdx] = useState(-1)
  const tbodyRef = useRef(null)

  const padY = localDensity === 'compact' ? '8px' : '12px'
  const padX = '16px'

  function handleKey(e) {
    if (!rows || rows.length === 0) return
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setFocusIdx(i => Math.min(rows.length - 1, i + 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setFocusIdx(i => Math.max(0, i - 1))
    } else if (e.key === 'Enter' && focusIdx >= 0 && onRowClick) {
      e.preventDefault()
      onRowClick(rows[focusIdx])
    }
  }

  useEffect(() => {
    if (focusIdx < 0 || !tbodyRef.current) return
    const row = tbodyRef.current.children[focusIdx]
    if (row && row.scrollIntoView) row.scrollIntoView({ block: 'nearest' })
  }, [focusIdx])

  // Empty / loading / error states use the same outer wrapper for layout
  // consistency.
  return (
    <div
      style={{
        background: 'var(--bg-card)',
        border: '1px solid var(--border)',
        borderRadius: '10px',
        overflow: 'hidden',
      }}
    >
      {(toolbar || showDensityToggle) && (
        <div style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 'var(--space-3)',
          padding: 'var(--space-2) var(--space-4)',
          borderBottom: '1px solid var(--border-subtle)',
        }}>
          <div style={{ flex: 1, minWidth: 0 }}>{toolbar}</div>
          {showDensityToggle && (
            <div style={{ display: 'flex', gap: '4px' }}>
              {['comfortable', 'compact'].map(d => (
                <button
                  key={d}
                  onClick={() => setLocalDensity(d)}
                  style={{
                    padding: '4px 10px',
                    fontSize: '11px',
                    fontWeight: 500,
                    border: '1px solid var(--border)',
                    borderRadius: '6px',
                    background: localDensity === d ? 'var(--accent-subtle)' : 'transparent',
                    color: localDensity === d ? 'var(--accent)' : 'var(--text-muted)',
                    cursor: 'pointer',
                    textTransform: 'capitalize',
                  }}
                >
                  {d}
                </button>
              ))}
            </div>
          )}
        </div>
      )}

      <div style={{ overflowX: 'auto', maxHeight: '70vh' }}>
        <table style={{
          width: '100%',
          borderCollapse: 'separate',
          borderSpacing: 0,
          fontSize: '13px',
        }}>
          <thead style={{
            position: 'sticky',
            top: 0,
            background: 'var(--bg-card)',
            zIndex: 1,
          }}>
            <tr style={{ borderBottom: '1px solid var(--border)' }}>
              {columns.map(c => (
                <th
                  key={c.key}
                  scope="col"
                  style={{
                    padding: `${padY} ${padX}`,
                    fontSize: '11px',
                    fontWeight: 600,
                    textTransform: 'uppercase',
                    letterSpacing: '0.06em',
                    color: 'var(--text-muted)',
                    textAlign: c.align || 'left',
                    width: c.width,
                    borderBottom: '1px solid var(--border)',
                    whiteSpace: 'nowrap',
                  }}
                >
                  {c.label}
                </th>
              ))}
            </tr>
          </thead>

          <tbody
            ref={tbodyRef}
            tabIndex={0}
            onKeyDown={handleKey}
            style={{ outline: 'none' }}
          >
            {loading && (
              <tr>
                <td colSpan={columns.length}>
                  <SkeletonRows columns={columns} padX={padX} padY={padY} count={5} />
                </td>
              </tr>
            )}

            {!loading && error && (
              <tr>
                <td colSpan={columns.length}>
                  <ErrorState error={error} onRetry={onRetry} />
                </td>
              </tr>
            )}

            {!loading && !error && (rows == null || rows.length === 0) && (
              <tr>
                <td colSpan={columns.length}>
                  <EmptyState title={emptyTitle} description={emptyDescription} action={emptyAction} />
                </td>
              </tr>
            )}

            {!loading && !error && rows && rows.flatMap((r, idx) => {
              const isExpanded = expandable?.isExpanded?.(r) ?? false
              const isLast = idx === rows.length - 1
              const main = (
                <tr
                  key={`row-${r[rowKey] ?? idx}`}
                  onClick={() => {
                    if (expandable) expandable.onToggle?.(r)
                    if (onRowClick) onRowClick(r)
                  }}
                  onMouseEnter={() => setFocusIdx(idx)}
                  style={{
                    background: focusIdx === idx
                      ? 'var(--sidebar-hover)'
                      : (idx % 2 === 0 ? 'transparent' : 'var(--bg-elevated)'),
                    cursor: (onRowClick || expandable) ? 'pointer' : 'default',
                    borderBottom: (isExpanded || isLast) ? 'none' : '1px solid var(--border-subtle)',
                    transition: 'background 0.1s',
                  }}
                >
                  {columns.map(c => (
                    <td
                      key={c.key}
                      style={{
                        padding: `${padY} ${padX}`,
                        verticalAlign: 'middle',
                        textAlign: c.align || 'left',
                        color: 'var(--text-primary)',
                      }}
                    >
                      {c.render ? c.render(r, idx) : r[c.key]}
                    </td>
                  ))}
                </tr>
              )
              if (!isExpanded || !expandable?.renderExpanded) return [main]
              const expandedRow = (
                <tr
                  key={`expanded-${r[rowKey] ?? idx}`}
                  style={{
                    background: 'var(--bg-base)',
                    borderBottom: isLast ? 'none' : '1px solid var(--border-subtle)',
                  }}
                >
                  <td
                    colSpan={columns.length}
                    style={{ padding: 0, borderTop: '1px solid var(--border-subtle)' }}
                  >
                    {expandable.renderExpanded(r)}
                  </td>
                </tr>
              )
              return [main, expandedRow]
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ── Sub-components ──────────────────────────────────────────────────────

function SkeletonRows({ columns, padX, padY, count }) {
  return (
    <div>
      {Array.from({ length: count }).map((_, i) => (
        <div
          key={i}
          style={{
            display: 'flex',
            padding: `${padY} ${padX}`,
            gap: 'var(--space-3)',
            borderBottom: i < count - 1 ? '1px solid var(--border-subtle)' : 'none',
          }}
        >
          {columns.map((c, idx) => (
            <div
              key={c.key}
              className="skeleton"
              style={{
                height: 14,
                flex: c.width ? `0 0 ${c.width}px` : '1 1 0',
                opacity: 1 - (idx * 0.1),
              }}
            />
          ))}
        </div>
      ))}
    </div>
  )
}

function ErrorState({ error, onRetry }) {
  const message = typeof error === 'string'
    ? error
    : (error?.response?.data?.detail || error?.message || 'Something went wrong')
  return (
    <div style={{
      display: 'flex',
      flexDirection: 'column',
      alignItems: 'center',
      justifyContent: 'center',
      gap: 'var(--space-2)',
      padding: 'var(--space-7)',
      color: 'var(--text-muted)',
    }}>
      <AlertCircle size={28} style={{ color: 'var(--danger)' }} />
      <p style={{ margin: 0, fontSize: '14px', fontWeight: 600, color: 'var(--text-primary)' }}>
        Failed to load
      </p>
      <p style={{ margin: 0, fontSize: '12px', color: 'var(--text-muted)', maxWidth: '480px', textAlign: 'center' }}>
        {message}
      </p>
      {onRetry && (
        <button
          onClick={onRetry}
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: '6px',
            marginTop: 'var(--space-2)',
            padding: '6px 14px',
            background: 'var(--bg-elevated)',
            border: '1px solid var(--border)',
            borderRadius: '6px',
            color: 'var(--text-secondary)',
            fontSize: '12px',
            fontWeight: 500,
            cursor: 'pointer',
          }}
        >
          <RefreshCw size={12} /> Retry
        </button>
      )}
    </div>
  )
}

function EmptyState({ title, description, action }) {
  return (
    <div style={{
      display: 'flex',
      flexDirection: 'column',
      alignItems: 'center',
      justifyContent: 'center',
      gap: 'var(--space-2)',
      padding: 'var(--space-7)',
      color: 'var(--text-muted)',
    }}>
      <Inbox size={28} style={{ opacity: 0.5 }} />
      <p style={{ margin: 0, fontSize: '14px', fontWeight: 600, color: 'var(--text-secondary)' }}>
        {title}
      </p>
      {description && (
        <p style={{ margin: 0, fontSize: '12px', color: 'var(--text-muted)', maxWidth: '480px', textAlign: 'center' }}>
          {description}
        </p>
      )}
      {action && <div style={{ marginTop: 'var(--space-2)' }}>{action}</div>}
    </div>
  )
}
