// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

/**
 * Single source of truth for every status enum used in the Certification UI.
 *
 * Vocabulary follows the backend (assignment_status, cert_run_status,
 * tc_result_status, triage_verdict, a2a_message_status). UI labels here are
 * the friendly forms — backend value is canonical, UI label is presentation.
 */
import {
  CheckCircle, Circle, Clock, AlertCircle, AlertTriangle, XCircle,
  Send, Inbox, FileWarning, ArrowRight, Hammer, FlaskConical, Award,
  Rocket, Activity, Ban, MessagesSquare, ShieldCheck,
} from 'lucide-react'

const COLOR = {
  green:   '#3fb950',
  amber:   '#d29922',
  red:     '#e06c6c',
  blue:    '#58a6ff',
  purple:  '#bc8cff',
  grey:    '#8b949e',
  emerald: '#2ea676',  // production live
  cyan:    '#7ed3e0',  // certified, pre-production
  rose:    '#ff7f9b',  // blocked
}

// ── ChangePartnerAssignment.status — the linear lifecycle ─────────────────
//
// The 10-step canonical flow plus WITHDRAWN (terminal off-shoot). Legacy
// values (communicated, acknowledged, in_progress, ready) are mapped here
// too so any unmigrated rows render gracefully — but they share order
// numbers with their replacements so the flow diagram doesn't get muddled.
export const ASSIGNMENT_STATUS = {
  assigned:                { label: 'Assigned',         color: COLOR.grey,    icon: Circle,        order: 0,  description: 'Partner added to this change request. Kit not yet sent.' },
  received:                { label: 'Received',         color: COLOR.blue,    icon: Inbox,         order: 1,  description: 'the Authority has delivered the Product Kit. Partner has visibility.' },
  accepted:                { label: 'Accepted',         color: COLOR.purple,  icon: CheckCircle,   order: 2,  description: 'Partner has formally acknowledged the change.' },
  applied:                 { label: 'Applied',          color: COLOR.amber,   icon: Hammer,        order: 3,  description: 'Partner has implemented the change in their stack (design + coding done).' },
  tested:                  { label: 'Tested',           color: COLOR.amber,   icon: FlaskConical,  order: 4,  description: 'Partner has completed their internal QA.' },
  ready_for_certification: { label: 'Ready for cert',   color: COLOR.blue,    icon: ShieldCheck,   order: 5,  description: 'Partner declared readiness; awaiting cert run.' },
  certifying:              { label: 'Certifying',       color: COLOR.amber,   icon: Activity,      order: 6,  description: 'Cert battery in flight or under triage.' },
  certified:               { label: 'Certified',        color: COLOR.cyan,    icon: Award,         order: 7,  description: 'All TCs passed. Awaiting the Authority sign-off for production.' },
  ready_for_production:    { label: 'Ready for prod',   color: COLOR.green,   icon: ShieldCheck,   order: 8,  description: 'the Authority signed off. Awaiting ops to mark live.' },
  in_production:           { label: 'In production',    color: COLOR.emerald, icon: Rocket,        order: 9,  description: 'Partner is processing real traffic on this change.' },
  withdrawn:               { label: 'Withdrawn',        color: COLOR.grey,    icon: Ban,           order: 99, description: 'Partner withdrawn from this change.' },
  // Legacy (dormant after migration 0028; kept for graceful rendering)
  communicated:            { label: 'Received',         color: COLOR.blue,    icon: Inbox,         order: 1,  description: 'Legacy alias of received.' },
  acknowledged:            { label: 'Accepted',         color: COLOR.purple,  icon: CheckCircle,   order: 2,  description: 'Legacy alias of accepted.' },
  in_progress:             { label: 'Building',         color: COLOR.amber,   icon: Hammer,        order: 3,  description: 'Legacy bucket; new lifecycle splits this into applied + tested.' },
  ready:                   { label: 'Ready for cert',   color: COLOR.blue,    icon: ShieldCheck,   order: 5,  description: 'Legacy alias of ready_for_certification.' },
}

// Linear flow steps for the per-partner diagram. WITHDRAWN excluded — it's
// a terminal off-flow state rendered separately.
export const ASSIGNMENT_FLOW = [
  'assigned',
  'received',
  'accepted',
  'applied',
  'tested',
  'ready_for_certification',
  'certifying',
  'certified',
  'ready_for_production',
  'in_production',
].map(key => ({ key, ...ASSIGNMENT_STATUS[key] }))

// Manual transitions available given a current status (admin-only).
export const MANUAL_TRANSITIONS = {
  certified:            ['approve_for_production'],
  ready_for_production: ['mark_live'],
}
// Always-available admin actions (gated separately on UI by current state).
export const ALWAYS_ADMIN_ACTIONS = ['block', 'withdraw']


// ── CertRun.status ────────────────────────────────────────────────────────
export const CERT_RUN_STATUS = {
  running:   { label: 'Running',   color: COLOR.amber, icon: Clock        },
  completed: { label: 'Completed', color: COLOR.green, icon: CheckCircle  },
}

// ── CertTestResult.status ─────────────────────────────────────────────────
export const TC_RESULT_STATUS = {
  pass:  { label: 'Pass',  color: COLOR.green, icon: CheckCircle },
  fail:  { label: 'Fail',  color: COLOR.red,   icon: XCircle     },
  skip:  { label: 'Skip',  color: COLOR.grey,  icon: Circle      },
  error: { label: 'Error', color: COLOR.red,   icon: AlertTriangle},
}

// ── CertTriage.ai_verdict ─────────────────────────────────────────────────
export const TRIAGE_VERDICT = {
  partner_code_bug: { label: 'Partner code bug', color: COLOR.red,    icon: FileWarning  },
  test_case_issue:  { label: 'Test case issue',  color: COLOR.amber,  icon: AlertCircle  },
  env_issue:        { label: 'Env issue',        color: COLOR.purple, icon: AlertCircle  },
}

// ── A2AMessage.status (outbound + inbound) ────────────────────────────────
export const A2A_MESSAGE_STATUS = {
  sent:                  { label: 'Sent',         color: COLOR.blue,  icon: Send         },
  delivered:             { label: 'Delivered',    color: COLOR.green, icon: CheckCircle  },
  pending:               { label: 'Pending',      color: COLOR.grey,  icon: Clock        },
  delivery_failed:       { label: 'Failed',       color: COLOR.red,   icon: XCircle      },
  submitted:             { label: 'Submitted',    color: COLOR.blue,  icon: Inbox        },
  completed:             { label: 'Completed',    color: COLOR.green, icon: CheckCircle  },
  completed_delivered:   { label: 'Done',         color: COLOR.green, icon: CheckCircle  },
  completed_undelivered: { label: 'No callback',  color: COLOR.amber, icon: AlertCircle  },
  failed:                { label: 'Failed',       color: COLOR.red,   icon: XCircle      },
}

// ── A2A direction labels + icons ──────────────────────────────────────────
export const A2A_DIRECTION = {
  inbound:  { label: 'Inbound',  color: COLOR.green, icon: Inbox     },
  outbound: { label: 'Outbound', color: COLOR.blue,  icon: ArrowRight},
}

// ── A2A task type — friendly labels for cert-engine traffic ───────────────
export const A2A_TASK_TYPE_LABEL = {
  cert_test_request:    { label: 'Test request',    desc: 'the Authority → Engine: run battery'    },
  cert_test_response:   { label: 'Test response',   desc: 'Engine → Authority — per-TC results' },
  defect_notice:        { label: 'Defect notice',   desc: 'Failure announced'              },
  defect_resolution:    { label: 'Defect resolved', desc: 'Failure resolved'               },
  cert_acknowledgement: { label: 'Cert ack',        desc: 'Partner acknowledged certification' },
  change_communication: { label: 'Change kit',      desc: 'Product Kit delivered'          },
  change_acknowledgement:{label: 'Change ack',      desc: 'Partner accepted the change'    },
  status_update:        { label: 'Status update',   desc: 'Partner reported progress'      },
  cert_readiness_declaration:{ label: 'Readiness',  desc: 'Partner declared ready'         },
  query:                { label: 'Query',           desc: 'Partner asked a question'       },
  clarification_response:{label: 'Clarification',   desc: 'the Authority responded to a query'      },
}

// ── Concurrent flag taxonomy ──────────────────────────────────────────────
//
// Flags ride alongside the main status — they're orthogonal. Renders as a
// chip rail next to the status badge.
export const ASSIGNMENT_FLAGS = {
  blocked:        { label: 'Blocked',        color: COLOR.rose,   icon: Ban },
  negotiating:    { label: 'Negotiating',    color: COLOR.purple, icon: MessagesSquare },
  triage_pending: { label: 'Triage pending', color: COLOR.amber,  icon: AlertCircle },
  stalled:        { label: 'Stalled',        color: COLOR.amber,  icon: Clock },
}

// Compute active flags for an assignment given derived inputs.
//   p:          one partners[] entry from /changes/{id}/cert-summary
//   ageDays:    threshold for the stalled flag (default 7)
// Returns ordered array of flag keys: ['blocked', 'negotiating', ...]
export function assignmentChips(p, { ageDays = 7 } = {}) {
  const flags = []
  if (p?.blocked) flags.push('blocked')
  if ((p?.open_threads || 0) > 0) flags.push('negotiating')
  const lr = p?.latest_run
  if (lr && (lr.failed || 0) > 0 && lr.status === 'completed') flags.push('triage_pending')

  // Stalled: current_state_since older than ageDays AND partner is in a
  // non-terminal main state.
  const terminal = new Set(['certified', 'in_production', 'withdrawn'])
  const stat = p?.assignment_status
  if (stat && !terminal.has(stat) && p?.current_state_since) {
    const ageMs = Date.now() - new Date(p.current_state_since).getTime()
    if (ageMs > ageDays * 86400 * 1000) flags.push('stalled')
  }
  return flags
}

// ── Generic resolver: get the {label,color,icon} for any status value ─────
export function resolveStatus(kind, value) {
  const lookup = {
    assignment:    ASSIGNMENT_STATUS,
    cert_run:      CERT_RUN_STATUS,
    tc_result:     TC_RESULT_STATUS,
    triage:        TRIAGE_VERDICT,
    a2a_message:   A2A_MESSAGE_STATUS,
    a2a_direction: A2A_DIRECTION,
    flag:          ASSIGNMENT_FLAGS,
  }[kind]
  if (!lookup) return null
  return lookup[value] || { label: value, color: COLOR.grey, icon: Circle }
}

// ── Dashboard row-status (CR-level) palette ───────────────────────────────
//
// Backend's /certification/dashboard endpoint returns the row-level pill in
// one of these values. UI maps to consistent colour/label.
export const DASHBOARD_ROW_STATUS = {
  live:             { label: 'Live',             color: COLOR.emerald, icon: Rocket       },
  awaiting_go_live: { label: 'Awaiting go-live', color: COLOR.green,   icon: ShieldCheck  },
  cert_done:        { label: 'Certified',        color: COLOR.cyan,    icon: Award        },
  cert_in_flight:   { label: 'Certifying',       color: COLOR.amber,   icon: Activity     },
  cert_pending:     { label: 'Cert pending',     color: COLOR.blue,    icon: Clock        },
  building:         { label: 'Building',         color: COLOR.amber,   icon: Hammer       },
  kickoff:          { label: 'Kickoff',          color: COLOR.grey,    icon: Inbox        },
  failed:           { label: 'Failed',           color: COLOR.red,     icon: XCircle      },
  blocked:          { label: 'Blocked',          color: COLOR.rose,    icon: Ban          },
  withdrawn:        { label: 'Withdrawn',        color: COLOR.grey,    icon: Ban          },
  // Legacy values (still emitted by old data; kept for graceful rendering)
  completed:        { label: 'Completed',        color: COLOR.green,   icon: CheckCircle  },
  in_progress:      { label: 'In progress',      color: COLOR.amber,   icon: Clock        },
  not_started:      { label: 'Not started',      color: COLOR.grey,    icon: AlertCircle  },
}

// ── Helper: pretty short timestamp like "4s ago" / "2 min ago" / "yesterday"
export function relativeTime(iso) {
  if (!iso) return ''
  const t = new Date(iso).getTime()
  if (!t) return ''
  const dms = Date.now() - t
  if (dms < 0) return 'just now'
  const s = Math.floor(dms / 1000)
  if (s < 5)        return 'just now'
  if (s < 60)       return `${s}s ago`
  if (s < 3600)     return `${Math.floor(s / 60)} min ago`
  if (s < 86400)    return `${Math.floor(s / 3600)} h ago`
  if (s < 86400*2)  return 'yesterday'
  if (s < 86400*30) return `${Math.floor(s / 86400)} d ago`
  return new Date(iso).toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' })
}

export function isStalledByActivity(assignmentStatus, latestRunCompletedAt, ageDays = 7) {
  if (!assignmentStatus) return false
  const status = assignmentStatus.toLowerCase()
  if (['certified', 'in_production', 'withdrawn'].includes(status)) return false
  if (!latestRunCompletedAt) return false
  const ageMs = Date.now() - new Date(latestRunCompletedAt).getTime()
  return ageMs > ageDays * 86400 * 1000
}
