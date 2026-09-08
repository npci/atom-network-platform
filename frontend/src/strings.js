// Copyright 2026 The ATOM Authors
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
  // What this deployment calls its certification body, for the few places that
  // cannot reach useCertInitiators() — lib/certStatus.js is plain data with no
  // hook available. Carries its own article so a deployment can substitute a
  // bare proper noun ("Acme Registry") and the surrounding sentences still read.
  'role.authority':    'the authority',

  // Demo build-transcript stack profile (lib/demoBuildLogs.js)
  'demo.stack.appDesc':           'Edge Application',
  'demo.stack.coreDesc':          'Core Service',
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
  // Regulator-facing copy. The platform has no regulator of its own; a
  // deployment that answers to one overrides these with its name. These used to
  // read "RBI compliance" and "review RBI guidelines" as literals in JSX, which
  // put one country's regulator on the Deep Research stage of every deployment.
  'stage.research.desc':          'Market analysis · product context · regulatory compliance',
  'research.intro.sources':       'review the applicable regulatory guidelines',
  'kb.upload.docTypes':           'regulatory guidelines, BRDs, API specs, XSDs',
  // Partner registry prose. The role vocabulary itself lives in
  // lib/partnerTypes.js — the single source of truth — but this sentence
  // restated four payment roles that that file had already relabelled.
  'partners.empty.body':          'Register partners by their role in this ecosystem to enable A2A communication for change management.',
  // Input placeholders — illustrative examples, never submitted as values
  'ph.a2a.agentFilter':           'e.g. Lite',
  'ph.messaging.search':          'Search partner or CR…',
  'ph.promptEnhance.describe':    'Describe the change — e.g. "cap the note field at 50 characters and call out the downstream impact"',
  'ph.a2a.partnerFilter':         'e.g. Northwind',
  'ph.partners.certAgentId':      'e.g. NORTHWIND',
  'ph.codeIndex.repo':            'e.g. Core Platform',
  'ph.codeKnowledge.search':      'Search code knowledge (e.g. OrderService, rate limit)...',
  'ph.kb.search':                 'e.g. offline processing limits…',
  'ph.policy.editor':             'Paste or type policy content here…',
  'ph.user.email':                'e.g. name@example.com',
  'ph.canvas.refine':             'e.g. Expand the regulatory section with the latest mandates…',
  'ph.research.refine':           'e.g. Add more detail on ecosystem constraints…',
  'ph.xsd.refine':                'e.g. Add a new optional element for the balance field…',
  'ph.newChange.description':     "Describe the feature idea in your own words. Don't worry about being formal \u2014 the AI will help refine it.\n\nExample: I want the system to renew a held item automatically when its hold window is about to expire, so users don't have to re-request it\u2026",
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
