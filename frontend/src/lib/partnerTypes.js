// Copyright 2026 The ATOM Authors
// SPDX-License-Identifier: MIT

// Single source of truth for the partner-registry role vocabulary.
//
// The KEYS are wire/DB values — they must stay byte-identical to `PartnerType`
// in backend/app/models/phase_c.py, which is what `partner_agents.partner_type`
// stores and what the partners API accepts on create/update. Do not rename a
// key without a migration.
//
// The LABELS are display-only and deliberately domain-neutral: this platform is
// not payments-specific, and a deployment whose partners are libraries or
// charge-point operators should not read "Bank" in its registry.
//
// WHY THIS FILE EXISTS: five components each carried their own copy of this
// map, keyed on a vocabulary migration 0108 had already remapped away. Every
// lookup therefore missed and fell through to a default, so every partner
// rendered under the wrong role label. Keep this the only copy.
//
// ICONS live here for the same reason. The five copies survived the label
// consolidation and all picked a bank building for the settling roles and a
// phone for the provider roles — a payments picture drawn on every deployment,
// including the ones whose partners are libraries or charge-point operators.
// The glyphs below say only "sends" / "receives" / "machine", which is true in
// every domain.
//
// Each role is declared ONCE below. Deriving the lookups from this array keeps
// the domain tokens to a single occurrence apiece, which is also what the
// hygiene gate's domain-term ratchet counts.
import { ArrowUpRight, ArrowDownLeft, Send, Inbox, Cpu, Globe } from 'lucide-react'

const ROLES = [
  { key: 'payer_psp',   label: 'Originating Provider', color: '#4caf7d', icon: ArrowUpRight,  selectable: true },
  { key: 'payee_psp',   label: 'Receiving Provider',   color: '#3f9fd0', icon: ArrowDownLeft, selectable: true },
  { key: 'remitter',    label: 'Sending Partner',      color: '#58a6ff', icon: Send,          selectable: true },
  { key: 'beneficiary', label: 'Receiving Partner',    color: '#b388e8', icon: Inbox,         selectable: true },
  // Authority-internal (the cert-agent submodule); the backend enum marks it
  // as not operator-selectable, so it is hidden from the registry pickers.
  { key: 'cert_engine', label: 'Cert Engine',          color: '#e8b347', icon: Cpu,           selectable: false },
]

export const PARTNER_TYPE_LABEL = Object.fromEntries(ROLES.map(r => [r.key, r.label]))
export const PARTNER_TYPE_COLOR = Object.fromEntries(ROLES.map(r => [r.key, r.color]))
export const PARTNER_TYPE_ICON  = Object.fromEntries(ROLES.map(r => [r.key, r.icon]))
export const SELECTABLE_PARTNER_TYPES = ROLES.filter(r => r.selectable).map(r => r.key)

// Unknown keys return the raw value rather than a wrong-but-plausible label:
// a silent fallback to some default role is exactly the bug this file fixes.
export function partnerTypeLabel(type) {
  if (!type) return ''
  return PARTNER_TYPE_LABEL[String(type).toLowerCase()] || String(type)
}

export function partnerTypeColor(type, fallback = '#8b949e') {
  if (!type) return fallback
  return PARTNER_TYPE_COLOR[String(type).toLowerCase()] || fallback
}

// Globe is the fallback for the same reason partnerTypeLabel returns the raw
// key: an unknown role gets a neutral "some participant out there" glyph rather
// than a confidently wrong one.
export function partnerTypeIcon(type, fallback = Globe) {
  if (!type) return fallback
  return PARTNER_TYPE_ICON[String(type).toLowerCase()] || fallback
}

// `partner_type` is JSON — historically a bare string, now a list. Normalise to
// the primary role, matching the backend's `_normalize_types`.
export function primaryPartnerType(type) {
  const v = Array.isArray(type) ? type[0] : type
  return v ? String(v).toLowerCase() : ''
}
