// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { agentsApi } from '../services/api'
import { CheckCircle, XCircle, Clock, FileText, ChevronDown, ChevronUp, Loader } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import StatTile, { StatTileRow } from '../components/common/StatTile'
import { DocxDownloadButton } from './ChangeRequest/BRD'

const ROLE_LABELS = {
  product_manager:  'Product Manager',
  tech_lead:        'Tech Lead',
  infosec_reviewer: 'InfoSec Reviewer',
  risk_reviewer:    'Risk Reviewer',
}

function ApprovalCard({ approval, onRespond }) {
  const [expanded, setExpanded] = useState(false)
  const [decision, setDecision]  = useState('')   // 'approved' | 'rejected'
  const [comments, setComments]  = useState('')
  const [loading, setLoading]    = useState(false)

  const handleSubmit = async () => {
    if (!decision) return
    setLoading(true)
    try {
      await agentsApi.respondApproval(approval.id, { status: decision, comments })
      onRespond()
    } catch (err) {
      console.error('respond failed', err)
    } finally {
      setLoading(false)
    }
  }

  const isPending = approval.status === 'pending'

  return (
    <div style={{
      background: 'var(--bg-elevated)', border: '1px solid var(--border)',
      borderRadius: '10px', overflow: 'hidden', marginBottom: '16px',
    }}>
      {/* Card header */}
      <div style={{
        padding: '16px 20px', display: 'flex', alignItems: 'center', gap: '12px',
        cursor: 'pointer', userSelect: 'none',
      }} onClick={() => setExpanded(e => !e)}>
        <FileText size={18} style={{ color: 'var(--accent)', flexShrink: 0 }} />
        <div style={{ flex: 1 }}>
          <p style={{ margin: '0 0 2px', fontSize: '14px', fontWeight: '600', color: 'var(--text-primary)' }}>
            {approval.change_title || `Change Request #${approval.change_id}`}
          </p>
          <p style={{ margin: 0, fontSize: '12px', color: 'var(--text-muted)' }}>
            {ROLE_LABELS[approval.reviewer_role] || approval.reviewer_role} review required
          </p>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
          {approval.status === 'pending' && (
            <span style={{
              padding: '3px 12px', borderRadius: '20px', fontSize: '11px', fontWeight: '500',
              background: 'rgba(218,119,86,0.10)', color: 'var(--accent)',
              border: '1px solid rgba(218,119,86,0.3)',
            }}>Awaiting Review</span>
          )}
          {approval.status === 'approved' && (
            <span style={{
              padding: '3px 12px', borderRadius: '20px', fontSize: '11px', fontWeight: '500',
              background: 'rgba(76,175,125,0.10)', color: 'var(--success)',
              border: '1px solid rgba(76,175,125,0.3)',
            }}>Approved</span>
          )}
          {approval.status === 'rejected' && (
            <span style={{
              padding: '3px 12px', borderRadius: '20px', fontSize: '11px', fontWeight: '500',
              background: 'rgba(224,108,108,0.10)', color: 'var(--danger)',
              border: '1px solid rgba(224,108,108,0.3)',
            }}>Rejected</span>
          )}
          {expanded ? <ChevronUp size={15} style={{ color: 'var(--text-muted)' }} /> : <ChevronDown size={15} style={{ color: 'var(--text-muted)' }} />}
        </div>
      </div>

      {/* Expanded BRD content + decision form */}
      {expanded && (
        <div style={{ borderTop: '1px solid var(--border-subtle)' }}>
          {/* BRD content */}
          <div style={{
            maxHeight: '500px', overflowY: 'auto',
            padding: '20px 24px',
            background: 'var(--bg-base)',
            borderBottom: isPending ? '1px solid var(--border-subtle)' : 'none',
          }}>
            <div style={{
              display: 'flex', alignItems: 'center', justifyContent: 'space-between',
              gap: 12, marginBottom: 12,
            }}>
              <p style={{ margin: 0, fontSize: '11px', fontWeight: '600', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
                BRD Content
              </p>
              {approval.change_id && (
                <DocxDownloadButton
                  changeId={approval.change_id}
                  docType="brd"
                  label="Download .docx"
                />
              )}
            </div>
            <div className="md-content" style={{ fontSize: '13px', lineHeight: '1.7', color: 'var(--text-primary)' }}>
              <ReactMarkdown>{approval.brd_content || '_No BRD content available._'}</ReactMarkdown>
            </div>
          </div>

          {/* Decision form — only for pending */}
          {isPending && (
            <div style={{ padding: '20px 24px' }}>
              <p style={{ margin: '0 0 14px', fontSize: '12px', fontWeight: '600', color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
                Your Decision
              </p>

              {/* Approve / Reject toggle */}
              <div style={{ display: 'flex', gap: '10px', marginBottom: '14px' }}>
                <button
                  onClick={() => setDecision('approved')}
                  style={{
                    flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '8px',
                    padding: '10px', borderRadius: '8px', fontSize: '13px', fontWeight: '600',
                    cursor: 'pointer', border: '2px solid',
                    borderColor: decision === 'approved' ? 'var(--success)' : 'var(--border)',
                    background: decision === 'approved' ? 'rgba(76,175,125,0.10)' : 'var(--bg-card)',
                    color: decision === 'approved' ? 'var(--success)' : 'var(--text-muted)',
                  }}
                >
                  <CheckCircle size={15} /> Approve
                </button>
                <button
                  onClick={() => setDecision('rejected')}
                  style={{
                    flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '8px',
                    padding: '10px', borderRadius: '8px', fontSize: '13px', fontWeight: '600',
                    cursor: 'pointer', border: '2px solid',
                    borderColor: decision === 'rejected' ? 'var(--danger)' : 'var(--border)',
                    background: decision === 'rejected' ? 'rgba(224,108,108,0.08)' : 'var(--bg-card)',
                    color: decision === 'rejected' ? 'var(--danger)' : 'var(--text-muted)',
                  }}
                >
                  <XCircle size={15} /> Request Changes
                </button>
              </div>

              {/* Comments */}
              <textarea
                value={comments}
                onChange={e => setComments(e.target.value)}
                placeholder={decision === 'rejected'
                  ? 'Please describe the changes required… (required for rejections)'
                  : 'Optional comments…'}
                rows={4}
                style={{
                  width: '100%', padding: '10px 14px', boxSizing: 'border-box',
                  background: 'var(--bg-input)', border: '1px solid var(--border)',
                  borderRadius: '6px', color: 'var(--text-primary)', fontSize: '13px',
                  lineHeight: '1.5', resize: 'vertical', outline: 'none',
                  fontFamily: 'inherit',
                }}
                onFocus={e => e.target.style.borderColor = 'var(--accent)'}
                onBlur={e => e.target.style.borderColor = 'var(--border)'}
              />

              <button
                onClick={handleSubmit}
                disabled={!decision || (decision === 'rejected' && !comments.trim()) || loading}
                style={{
                  marginTop: '12px', display: 'flex', alignItems: 'center', gap: '8px',
                  padding: '10px 22px',
                  background: decision === 'approved' ? 'var(--success)' : decision === 'rejected' ? 'var(--danger)' : 'var(--bg-elevated)',
                  color: decision ? 'white' : 'var(--text-muted)',
                  border: 'none', borderRadius: '8px', fontSize: '13px', fontWeight: '600',
                  cursor: (!decision || (decision === 'rejected' && !comments.trim()) || loading) ? 'not-allowed' : 'pointer',
                  opacity: (!decision || (decision === 'rejected' && !comments.trim()) || loading) ? 0.5 : 1,
                }}
              >
                {loading && <Loader size={13} style={{ animation: 'spin 1s linear infinite' }} />}
                Submit Decision
              </button>
            </div>
          )}

          {/* Already responded */}
          {!isPending && approval.comments && (
            <div style={{ padding: '16px 24px' }}>
              <p style={{ margin: '0 0 6px', fontSize: '11px', fontWeight: '600', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
                Your Comments
              </p>
              <p style={{ margin: 0, fontSize: '13px', color: 'var(--text-secondary)', lineHeight: '1.6' }}>
                {approval.comments}
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export default function Approvals() {

  const { data, isLoading, refetch } = useQuery({
    queryKey: ['pending-approvals'],
    queryFn: () => agentsApi.pendingApprovals().then(r => r.data),
  })

  const approvals = data?.approvals || []
  const stats = {
    pending:  approvals.filter(a => a.status === 'pending').length,
    approved: approvals.filter(a => a.status === 'approved').length,
    rejected: approvals.filter(a => a.status === 'rejected').length,
    total:    approvals.length,
  }

  return (
    <div style={{ padding: '32px 40px', maxWidth: 1600, margin: '0 auto' }}>
      <div style={{ marginBottom: '24px' }}>
        <h1 style={{ margin: '0 0 6px', fontSize: '20px', fontWeight: '600', color: 'var(--text-primary)' }}>
          Pending Approvals
        </h1>
        <p style={{ margin: 0, fontSize: '13px', color: 'var(--text-muted)' }}>
          BRDs awaiting your review and sign-off
        </p>
      </div>

      <StatTileRow>
        <StatTile label="Pending"  value={stats.pending}  accent="#da7756"
                  hint={stats.pending ? 'awaiting review' : 'nothing pending'} />
        <StatTile label="Approved" value={stats.approved} accent="#4caf7d" />
        <StatTile label="Rejected" value={stats.rejected} accent="#e06c6c" />
        <StatTile label="Total"    value={stats.total}    accent="var(--text-secondary)" />
      </StatTileRow>

      {isLoading && (
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px', padding: '48px 0', color: 'var(--text-muted)', fontSize: '13px' }}>
          <Loader size={16} style={{ animation: 'spin 1s linear infinite' }} /> Loading approvals…
        </div>
      )}

      {!isLoading && approvals.length === 0 && (
        <div style={{
          textAlign: 'center', padding: '64px 32px',
          background: 'var(--bg-elevated)', border: '1px solid var(--border)',
          borderRadius: '12px',
        }}>
          <CheckCircle size={40} style={{ color: 'var(--success)', marginBottom: '16px' }} />
          <h2 style={{ margin: '0 0 8px', fontSize: '18px', fontWeight: '600', color: 'var(--text-primary)' }}>
            All clear
          </h2>
          <p style={{ margin: 0, fontSize: '13px', color: 'var(--text-muted)' }}>
            No BRDs are currently awaiting your approval.
          </p>
        </div>
      )}

      {approvals.map(a => (
        <ApprovalCard
          key={a.id}
          approval={a}
          onRespond={() => refetch()}
        />
      ))}
    </div>
  )
}
