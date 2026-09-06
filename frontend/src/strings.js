// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

// UI label catalogue — neutral defaults in code, domain labels supplied by the
// deployment.
//
// WHY THIS EXISTS, and why the labels were not simply renamed:
//
// `frontend/src` carries ~218 domain terms. Most are not free-text — they are
// API response fields (`authority_simulator_url`), route paths (`/admin/authority-policy`)
// or user-visible copy. Renaming the copy outright would make the platform
// *look* generic while making the internal deployment's own UI vaguer: "the Authority
// Policy" is the correct label for the people using it. So the copy is
// EXTERNALISED instead. Core ships an ecosystem-neutral default; the deployment
// supplies its own wording and sees no change.
//
// Same shape as brand.js, which already does this for logos.
//
// Overrides are a JSON object in VITE_LABEL_OVERRIDES, applied at build time:
//
//   VITE_LABEL_OVERRIDES='{"nav.policy":"Authority Policy"}' npm run build
//
// docker-compose passes the network set through as a build arg, so the internal
// stack renders exactly what it rendered before this change.
//
// Build time rather than runtime on purpose: the app does fetch /api/config/ui,
// but that resolves after first paint, so runtime labels would flash the neutral
// default and then swap. A label that visibly changes half a second after load
// looks like a bug.

const DEFAULTS = {
  // Sidebar navigation
  'nav.policy':        'Policy',
  // Login chrome
  'login.footer':      'Internal Use Only',
  'login.tagline':     'Change management for your ecosystem — from idea to certified partner rollout.',
  // Admin page
  'page.policy.title': 'Policy',
  // Artifact + framework names (the authority's own document vocabulary)
  'artifact.circular': 'Circular',
  'canvas.framework':  'Build Framework',

  // Demo build-transcript stack profile (lib/demoBuildLogs.js)
  'demo.stack.appDesc':           'Edge Application',
  'demo.stack.coreDesc':          'Core Switch',
  'demo.stack.groupId':           'com.example.platform',
  'demo.stack.groupPath':         'com/example/platform',
  'demo.stack.registry':          'internal-nexus',
  'demo.stack.appModule':         'edge-app',
  'demo.stack.depProtocol':       'platform-protocol',
  'demo.stack.depCommon':         'platform-common',
  'demo.stack.appDir':            'edge',
  'demo.stack.appUnit':           'edge',
  'demo.stack.coreModule':        'core-service',
  'demo.stack.reactor':           'platform-stack',
  // Input placeholders — illustrative examples, never submitted as values
  'ph.a2a.agentFilter':           'e.g. Lite',
  'ph.codeIndex.repo':            'e.g. Core Platform',
  'ph.codeKnowledge.search':      'Search code knowledge (e.g. OrderService, rate limit)...',
  'ph.kb.search':                 'e.g. offline transaction limits…',
  'ph.policy.editor':             'Paste or type policy content here…',
  'ph.user.email':                'e.g. name@example.com',
  'ph.canvas.refine':             'e.g. Expand the regulatory section with the latest mandates…',
  'ph.research.refine':           'e.g. Add more detail on ecosystem constraints…',
  'ph.xsd.refine':                'e.g. Add a new optional element for the balance field…',
  'ph.newChange.description':     "Describe the feature idea in your own words. Don't worry about being formal \u2014 the AI will help refine it.\n\nExample: I want to introduce automatic top-up for wallets when the balance falls below a threshold, so users don't have to manually reload\u2026",
}

let overrides = {}
try {
  overrides = JSON.parse(import.meta.env.VITE_LABEL_OVERRIDES || '{}')
} catch (err) {
  // A malformed override blob must not take the UI down — every label falls
  // back to its neutral default, which is always a readable string.
  console.warn('VITE_LABEL_OVERRIDES is not valid JSON; using default labels', err)
  overrides = {}
}

/**
 * Look up a UI label.
 * Unknown keys return the key itself, so a typo shows up in the interface as
 * `nav.plicy` rather than silently rendering an empty element.
 */
export function t(key) {
  return overrides[key] ?? DEFAULTS[key] ?? key
}

export const LABEL_KEYS = Object.keys(DEFAULTS)
