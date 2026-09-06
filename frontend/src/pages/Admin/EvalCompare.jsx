// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { ArrowRight, GitCompare, Loader, ShieldCheck } from 'lucide-react'
import { evalApi } from '../../services/api'

function NumberRow({ label, a, b, delta, format = (v) => v, positiveIsGood = true }) {
  const d = (delta ?? 0)
  const arrow = d === 0 ? '·' : (d > 0 ? '▲' : '▼')
  const isGood = positiveIsGood ? d > 0 : d < 0
  const color = d === 0 ? 'var(--text-muted)' : (isGood ? 'var(--success)' : 'var(--danger)')
  return (
    <div style={{
      display: 'grid',
      gridTemplateColumns: '1.4fr 1fr 1fr 0.7fr',
      gap: '10px',
      padding: '10px 14px',
      borderBottom: '1px solid var(--border-subtle)',
      fontSize: '12px',
      alignItems: 'center',
    }}>
      <span style={{ color: 'var(--text-secondary)' }}>{label}</span>
      <span style={{ textAlign: 'right', fontFamily: 'ui-monospace,monospace', color: 'var(--text-secondary)' }}>{format(a)}</span>
      <span style={{ textAlign: 'right', fontFamily: 'ui-monospace,monospace', color: 'var(--text-primary)', fontWeight: 600 }}>{format(b)}</span>
      <span style={{ textAlign: 'right', color, fontWeight: 700 }}>{arrow} {format(Math.abs(d))}</span>
    </div>
  )
}

function ImpactCol({ data, badge }) {
  if (!data) return null
  return (
    <div style={{
      flex: 1,
      padding: '14px 16px',
      borderRadius: '8px',
      border: '1px solid var(--border)',
      background: 'var(--bg-elevated)',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '4px' }}>
        <span style={{
          padding: '2px 8px',
          fontSize: '10px',
          fontWeight: 700,
          letterSpacing: '0.05em',
          textTransform: 'uppercase',
          borderRadius: '4px',
          background: 'var(--bg-base)',
          border: '1px solid var(--border)',
          color: 'var(--text-muted)',
        }}>{badge}</span>
        <span style={{ fontSize: '11px', color: 'var(--text-muted)', fontFamily: 'ui-monospace,monospace' }}>
          {data.change_request_id.slice(0, 8)}
        </span>
      </div>
      <p style={{ margin: '4px 0 8px', fontSize: '14px', fontWeight: 700, color: 'var(--text-primary)' }}>
        {data.title || '(no title)'}
      </p>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: '6px' }}>
        <Stat label="Verdicts" value={data.verdicts.total} />
        <Stat label="Failures caught" value={data.verdicts.FAIL} color="var(--danger)" />
        <Stat label="Overrides" value={data.overrides} color="#6ea8dc" />
        <Stat label="Auto-fix retries" value={data.retry_runs} color="var(--accent)" />
      </div>
    </div>
  )
}

function Stat({ label, value, color = 'var(--text-primary)' }) {
  return (
    <div style={{
      padding: '8px 10px', borderRadius: '6px', background: 'var(--bg-base)',
      border: '1px solid var(--border-subtle)',
    }}>
      <p style={{ margin: 0, fontSize: '9px', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.05em', fontWeight: 700 }}>{label}</p>
      <p style={{ margin: '3px 0 0', fontSize: '16px', fontWeight: 700, color }}>{value}</p>
    </div>
  )
}

export default function EvalCompare() {
  const [changeA, setChangeA] = useState('')
  const [changeB, setChangeB] = useState('')

  const mutation = useMutation({
    mutationFn: ({ a, b }) => evalApi.compareChanges(a, b).then((r) => r.data),
  })

  const data = mutation.data
  const handleCompare = () => {
    mutation.mutate({ a: changeA.trim(), b: changeB.trim() })
  }
  const ready = changeA.trim().length > 6 && changeB.trim().length > 6 && changeA !== changeB

  return (
    <div style={{ padding: '32px 40px', maxWidth: 1300, margin: '0 auto' }}>
      <div style={{ marginBottom: '18px' }}>
        <h1 style={{ margin: '0 0 4px', fontSize: '20px', fontWeight: 600, color: 'var(--text-primary)' }}>
          Eval Compare (A/B)
        </h1>
        <p style={{ margin: 0, fontSize: '13px', color: 'var(--text-muted)' }}>
          Side-by-side comparison of two change requests. Run the same prompt twice — once with
          every checkpoint set to <code>disabled</code> (control) and once with the harness on
          (treatment) — paste both IDs below to see what the harness caught.
        </p>
      </div>

      {/* Picker */}
      <div style={{
        borderRadius: '8px', border: '1px solid var(--border)', background: 'var(--bg-elevated)',
        padding: '14px 16px', marginBottom: '14px',
      }}>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 24px 1fr auto', gap: '10px', alignItems: 'end' }}>
          <div>
            <label style={{ display: 'block', fontSize: '10px', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em', color: 'var(--text-muted)', marginBottom: '5px' }}>
              Control (A) — harness OFF
            </label>
            <input
              placeholder="Paste change ID (UUID)"
              value={changeA}
              onChange={(e) => setChangeA(e.target.value.trim())}
              style={{
                width: '100%',
                padding: '8px 10px',
                fontSize: '12px',
                fontFamily: 'ui-monospace,monospace',
                borderRadius: '6px',
                border: '1px solid var(--border)',
                background: 'var(--bg-input)',
                color: 'var(--text-primary)',
              }}
            />
          </div>
          <ArrowRight size={16} style={{ color: 'var(--text-muted)', alignSelf: 'center' }} />
          <div>
            <label style={{ display: 'block', fontSize: '10px', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em', color: 'var(--text-muted)', marginBottom: '5px' }}>
              Treatment (B) — harness ON
            </label>
            <input
              placeholder="Paste change ID (UUID)"
              value={changeB}
              onChange={(e) => setChangeB(e.target.value.trim())}
              style={{
                width: '100%',
                padding: '8px 10px',
                fontSize: '12px',
                fontFamily: 'ui-monospace,monospace',
                borderRadius: '6px',
                border: '1px solid var(--border)',
                background: 'var(--bg-input)',
                color: 'var(--text-primary)',
              }}
            />
          </div>
          <button
            onClick={handleCompare}
            disabled={!ready || mutation.isPending}
            style={{
              display: 'inline-flex', alignItems: 'center', gap: '6px',
              padding: '9px 14px', borderRadius: '6px',
              border: 'none', background: ready ? 'var(--accent)' : 'var(--bg-base)',
              color: ready ? 'white' : 'var(--text-muted)',
              fontSize: '12px', fontWeight: 700,
              cursor: (ready && !mutation.isPending) ? 'pointer' : 'not-allowed',
            }}
          >
            {mutation.isPending ? <Loader size={12} style={{ animation: 'spin 1s linear infinite' }} /> : <GitCompare size={12} />}
            Compare
          </button>
        </div>
      </div>

      {mutation.error && (
        <div style={{
          padding: '10px 12px',
          borderRadius: '6px',
          border: '1px solid rgba(224,108,108,0.30)',
          background: 'rgba(224,108,108,0.08)',
          color: 'var(--danger)',
          fontSize: '12px',
          marginBottom: '14px',
        }}>
          {mutation.error?.response?.data?.detail || 'Could not load comparison.'}
        </div>
      )}

      {data && (
        <>
          {/* Column summaries */}
          <div style={{ display: 'flex', gap: '12px', marginBottom: '14px' }}>
            <ImpactCol data={data.a} badge="A · control" />
            <ImpactCol data={data.b} badge="B · treatment" />
          </div>

          {/* Diff table */}
          <div style={{
            borderRadius: '8px', border: '1px solid var(--border)', background: 'var(--bg-elevated)',
            overflow: 'hidden',
          }}>
            <div style={{
              padding: '14px 16px', borderBottom: '1px solid var(--border-subtle)',
              fontSize: '13px', fontWeight: 700, color: 'var(--text-primary)',
            }}>
              <ShieldCheck size={13} style={{ verticalAlign: '-2px', marginRight: '6px', color: 'var(--accent)' }} />
              Harness impact — A vs B
            </div>
            <div style={{
              display: 'grid',
              gridTemplateColumns: '1.4fr 1fr 1fr 0.7fr',
              gap: '10px',
              padding: '9px 14px',
              borderBottom: '1px solid var(--border-subtle)',
              fontSize: '10px', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em',
              color: 'var(--text-muted)',
              background: 'var(--bg-base)',
            }}>
              <span>Metric</span>
              <span style={{ textAlign: 'right' }}>A (control)</span>
              <span style={{ textAlign: 'right' }}>B (treatment)</span>
              <span style={{ textAlign: 'right' }}>Δ (B − A)</span>
            </div>

            {/* Harness behaviour metrics (more is good when the harness is doing work) */}
            <NumberRow label="Verdicts written"         {...data.diff.verdicts_total}     positiveIsGood />
            <NumberRow label="Failures caught"          {...data.diff.verdicts_fail}      positiveIsGood />
            <NumberRow label="Warnings caught"          {...data.diff.verdicts_warn}      positiveIsGood />
            <NumberRow label="Passes recorded"          {...data.diff.verdicts_pass}      positiveIsGood />
            <NumberRow label="Reasons / findings (sum)" {...data.diff.reasons_total}      positiveIsGood />
            <NumberRow label="Auto-fix retries"         {...data.diff.retry_runs}         positiveIsGood />
            <NumberRow label="Manual overrides"         {...data.diff.overrides}          positiveIsGood={false} />

            <div style={{ padding: '10px 14px', background: 'var(--bg-base)', fontSize: '11px', color: 'var(--text-muted)', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em' }}>
              Artifact-level shape
            </div>
            <NumberRow label="BRD characters"           {...data.diff.brd_chars}          format={(v) => v.toLocaleString()} positiveIsGood />
            <NumberRow label="BRD FR count"             {...data.diff.brd_fr_count}       positiveIsGood />
            <NumberRow label="Tech Spec characters"     {...data.diff.tech_spec_chars}    format={(v) => v.toLocaleString()} positiveIsGood />
            <NumberRow label="Tech Spec FR count"       {...data.diff.tech_spec_fr_count} positiveIsGood />
            <NumberRow label="Tech Spec error codes"       {...data.diff.tech_spec_error_codes} positiveIsGood />
          </div>

          <p style={{ margin: '12px 0 0', fontSize: '11px', color: 'var(--text-muted)', lineHeight: 1.6 }}>
            Reading the table: <strong>green ▲</strong> means B improved over A on that metric;{' '}
            <strong>red ▼</strong> means B regressed. Manual overrides is treated as "less is better" —
            the harness was strong enough that operators didn't need to override.{' '}
            Auto-fix retries higher in B is a positive signal: the harness took action to repair
            the artifact instead of waiting for a human.
          </p>
        </>
      )}
    </div>
  )
}
