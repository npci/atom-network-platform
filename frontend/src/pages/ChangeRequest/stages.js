// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

// Stage metadata for the change-request views.
//
// Extracted from ChangeDetail.jsx so that file exports only components:
// react-refresh/only-export-components fires when a module mixes component and
// non-component exports, because Fast Refresh then cannot hot-swap it reliably.
import { BookOpen, Code2, FileCode, FileText, Layout, MessageCircleQuestion, MessageSquare, Package } from 'lucide-react'

export const STAGES = [
  {
    key:        'prompt_enhancement',
    label:      'Prompt Enhancement',
    shortLabel: 'Prompt',
    desc:       'AI refines your idea into a well-scoped specification',
    module:     'prompt_enhancer',
    icon:       MessageSquare,
  },
  {
    key:        'research',
    label:      'Deep Research',
    shortLabel: 'Research',
    desc:       'Market analysis · product context · RBI compliance',
    module:     'researcher',
    icon:       BookOpen,
  },
  { key: 'canvas',       label: 'Product Canvas',     shortLabel: 'Canvas',   desc: 'Structured feature canvas',                icon: Layout },
  { key: 'clarification', label: 'Clarification',     shortLabel: 'Clarify',  desc: 'Resolve spec gaps with the PM before BRD',  icon: MessageCircleQuestion },
  { key: 'brd',          label: 'BRD',                shortLabel: 'BRD',      desc: 'Business requirement document + approvals', icon: FileText },
  { key: 'tech_spec',    label: 'Tech Specification', shortLabel: 'Tech Spec', desc: 'Technical design document',                icon: Code2 },
  { key: 'xsd',          label: 'XSD Update',         shortLabel: 'XSD',      desc: 'Schema changes (if required)',              icon: FileCode },
  { key: 'product_kit',  label: 'Product Kit',        shortLabel: 'Kit',      desc: '9 partner-ready documents',                icon: Package },
]

export const EXPANDABLE_STAGES = ['prompt_enhancement', 'research', 'canvas', 'clarification', 'brd', 'tech_spec', 'xsd', 'product_kit']
