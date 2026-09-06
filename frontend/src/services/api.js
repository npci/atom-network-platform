// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import axios from 'axios'
import { API_BASE, LOGIN_PATH } from '../utils/basePath'
import { useAuthStore } from '../store/useStore'

// SESSION TRANSPORT: the JWT lives in an httpOnly cookie set by the backend on
// login, NOT in localStorage. `withCredentials` makes the browser attach it to
// every request automatically, so there is no token for JavaScript to read and
// no Authorization header for this layer to build.
//
// WHY: a token in web storage is readable by any script in this origin, so one
// XSS yields a live privileged credential — and operator sessions are 8h and
// slide forward on every request, so a stolen one renews itself. httpOnly puts
// it out of reach of script entirely.
//
// The CSRF exposure that cookie auth would otherwise introduce is closed
// server-side by SameSite=Strict on the cookie plus an Origin check on
// state-changing methods (backend/app/core/csrf.py) — see that module for why
// both are needed together.
const api = axios.create({
  baseURL: API_BASE,
  headers: { 'Content-Type': 'application/json' },
  withCredentials: true,
})

// Sliding-window refresh is now handled entirely by the browser: when the
// current token passes 50% of its TTL the backend replies with a refreshed
// Set-Cookie, which the browser swaps in silently. There is nothing to read,
// store, or forget to wire up at a new call site — which is why the old
// `X-Refresh-Token` header handling is gone rather than ported.

// 401 interceptor — verify the session is actually dead before logging
// the operator out. A single 401 on any write request used to clear
// localStorage and bounce to /login, costing workflow context on any
// transient backend hiccup. Now we probe `/auth/me` via the no-redirect
// silentApi — only if THAT also 401s do we treat the session as dead.
api.interceptors.response.use(
  (res) => res,
  async (err) => {
    const status = err.response?.status
    const detail = err.response?.data?.detail || ''
    const method = (err.config?.method || 'get').toLowerCase()
    const isAuthFailure = status === 401 || (status === 403 && detail === 'Not authenticated')
    const isBackground  = err.config?._skipAuthRedirect || method === 'get'
    if (isAuthFailure && !isBackground) {
      let tokenIsDead = true
      try {
        await silentApi.get('/auth/me')
        tokenIsDead = false
      } catch  { /* probe failed too — token is dead */ }
      if (tokenIsDead) {
        // The session cookie is httpOnly and can only be cleared by the server
        // (POST /auth/logout does that). What remains client-side is the
        // non-sensitive `user` marker that ProtectedRoute gates on — clear it
        // through the store so BOTH the in-memory state and its persisted copy
        // go, not just localStorage. That matters when we are already on
        // /login and no reload follows to reset memory.
        useAuthStore.getState().clearAuth()
        if (window.location.pathname !== LOGIN_PATH) {
          window.location.href = LOGIN_PATH
        }
      }
    }
    return Promise.reject(err)
  }
)

// Silent API — same auth, never redirects on 401. Use for background polling.
// Same cookie transport as `api` above (`withCredentials`), no token handling.
export const silentApi = axios.create({
  baseURL: API_BASE,
  headers: { 'Content-Type': 'application/json' },
  withCredentials: true,
})

// Client-side UI config — drives dev-only widgets like the per-step Skip
// button. silentApi so a 401 here never logs the user out (it's loaded
// before auth completes on the login screen).
export const uiConfigApi = {
  get: () => silentApi.get('/config/ui').then(r => r.data),
}

// Auth
export const authApi = {
  login: (data) => api.post('/auth/login', data),
  // Self-hosted login CAPTCHA: mint a single-use image challenge. 404 = disabled.
  captcha: () => api.get('/auth/captcha'),
  // TOTP MFA. verify = second login step (mfa_token + code → session).
  // setup/activate accept an optional bridge token for forced enrolment at login
  // (no session yet); omit it for opt-in enrolment while already logged in.
  mfaVerify: (mfaToken, code) => api.post('/auth/mfa/verify', { mfa_token: mfaToken, code }),
  mfaSetup: (token) => api.post('/auth/mfa/setup', {},
    token ? { headers: { Authorization: `Bearer ${token}` } } : undefined),
  mfaActivate: (code, token) => api.post('/auth/mfa/activate', { code },
    token ? { headers: { Authorization: `Bearer ${token}` } } : undefined),
  mfaDisable: (password, code) => api.post('/auth/mfa/disable', { password, code }),
  me: () => api.get('/auth/me'),
  // Switch the caller's ACTIVE role (must be one of their assigned roles).
  switchRole: (role) => api.post('/auth/switch-role', { role }),
  logout: () => api.post('/auth/logout'),
  changePassword: (currentPassword, newPassword) =>
    api.post('/auth/change-password', { current_password: currentPassword, new_password: newPassword }),
}

// Users (admin)
export const usersApi = {
  list: (params) => api.get('/users', { params }),
  get: (id) => api.get(`/users/${id}`),
  create: (data) => api.post('/users', data),
  update: (id, data) => api.patch(`/users/${id}`, data),
  deactivate: (id) => api.delete(`/users/${id}`),
}

// Change Requests
export const changesApi = {
  list: (params) => api.get('/changes', { params }),
  get: (id) => api.get(`/changes/${id}`),
  advance: (id, payload) => api.post(`/changes/${id}/advance`, payload || {}),
  create: (data) => api.post('/changes', data),
  update: (id, data) => api.patch(`/changes/${id}`, data),
  // Optional SOURCE document (detailed BRD) attached at creation — seed material for
  // Phase A (enhancer/research/canvas/BRD gen), never a substitute for generated docs.
  uploadSourceDocument: (id, file) => {
    const fd = new FormData()
    fd.append('file', file)
    return api.post(`/changes/${id}/source-document`, fd,
                    { headers: { 'Content-Type': 'multipart/form-data' } })
  },
  // Admin one-click cascade delete. Caller must pass the exact CR title
  // as confirm_title — backend re-validates as a server-side guard.
  // Returns {deleted, summary: {...counts}}; on success, route the user
  // back to the dashboard and toast the summary.
  adminDelete: (id, confirmTitle) =>
    api.delete(`/changes/${id}`, { params: { confirm_title: confirmTitle } }),
}

// Agents — conversation history + research output
export const agentsApi = {
  conversation:    (changeId, module) => api.get(`/changes/${changeId}/conversation/${module}`),
  research:        (changeId)         => api.get(`/changes/${changeId}/research`),
  canvas:          (changeId)         => api.get(`/changes/${changeId}/canvas`),
  brd:             (changeId)         => api.get(`/changes/${changeId}/brd`),
  submitBRD:       (changeId, brdId)  => api.post(`/changes/${changeId}/brd/submit`, { brd_id: brdId }),
  brdApprovals:    (changeId)         => api.get(`/changes/${changeId}/brd/approvals`),
  // Every persisted BRD version, newest first — metadata only, for the history picker.
  listBRDVersions: (changeId)         => api.get(`/changes/${changeId}/brd/versions`),
  // Full content of one specific persisted BRD version (read-only history view).
  getBRDVersion:   (changeId, version) => api.get(`/changes/${changeId}/brd/versions/${version}`),
  respondApproval: (approvalId, data) => api.post(`/approvals/${approvalId}/respond`, data),
  pendingApprovals: ()                => api.get('/approvals/pending'),
  techSpec:           (changeId)          => api.get(`/changes/${changeId}/tech-spec`),
  xsd:                (changeId)          => api.get(`/changes/${changeId}/xsd`),
  assessXsd:          (changeId)          => api.post(`/changes/${changeId}/xsd/assess`),
  productKitAll:      (changeId, negotiationVersion)          => api.get(`/changes/${changeId}/product-kit`, negotiationVersion != null ? { params: { negotiation_version: negotiationVersion } } : undefined),
  productKitDoc:      (changeId, docType, negotiationVersion) => api.get(`/changes/${changeId}/product-kit/${docType}`, negotiationVersion != null ? { params: { negotiation_version: negotiationVersion } } : undefined),
  productKitComplete: (changeId)          => api.post(`/changes/${changeId}/product-kit/complete`),
  // Docgen merge (Session 20+) — DEV-only fast-path approval; backend gates
  // on APP_ENV != production. Surfaced from a dev-only button on BRD.jsx.
  devAutoApproveBRD:  (changeId)          => api.post(`/changes/${changeId}/brd/dev-auto-approve`),
  // Generate-or-Upload — upload a document in place of generating it. The
  // uploaded file is parsed to text and stored as the new latest version, so
  // it substitutes the generated doc for all downstream context. `subtype` is
  // the Product Kit doc_type (e.g. 'product_note', 'cert_test_cases').
  uploadArtifact:     (changeId, docType, file, subtype) => {
    const form = new FormData()
    form.append('file', file)
    if (subtype) form.append('subtype', subtype)
    return api.post(`/changes/${changeId}/artifacts/${docType}/upload`, form,
      { headers: { 'Content-Type': 'multipart/form-data' } })
  },
  // Revert an uploaded document back to the most recent generated version.
  revertArtifactToGenerated: (changeId, docType, subtype) =>
    api.post(`/changes/${changeId}/artifacts/${docType}/revert-to-generated`, null,
      { params: subtype ? { subtype } : undefined }),
  // Uploaded-doc ↔ ratified-plan reconciliation. getReconciliation returns the
  // pending conflicts (each with options + a free-text choice); decide records
  // the user's per-conflict resolutions and unblocks downstream generation.
  getReconciliation:    (changeId, docKind = 'brd') =>
    api.get(`/changes/${changeId}/reconciliation`, { params: { doc_kind: docKind } }),
  decideReconciliation: (changeId, resolutions, docKind = 'brd') =>
    api.post(`/changes/${changeId}/reconciliation/decide`, { resolutions }, { params: { doc_kind: docKind } }),
  dismissReconciliation: (changeId, docKind = 'brd') =>
    api.post(`/changes/${changeId}/reconciliation/dismiss`, null, { params: { doc_kind: docKind } }),
  // Answer a delta-grounding question (the code check on accepted changes) → Decision Ledger.
  answerGrounding: (changeId, index, question, answer, docKind = 'brd') =>
    api.post(`/changes/${changeId}/reconciliation/grounding-answer`, { index, question, answer },
             { params: { doc_kind: docKind } }),
  // Acknowledge that an accepted change overturns a ratified decision → clears the approval soft-gate.
  acknowledgeOverturns: (changeId, docKind = 'brd') =>
    api.post(`/changes/${changeId}/reconciliation/acknowledge-overturns`, null, { params: { doc_kind: docKind } }),
  // Plan-version history + a single version (compare previous vs current plan).
  listAnalysisVersions: (changeId) => api.get(`/changes/${changeId}/analysis/versions`),
  getAnalysisVersion:   (changeId, version) => api.get(`/changes/${changeId}/analysis/versions/${version}`),
  // Promo/explainer video — upload the PM-produced MP4 (built off the generated
  // script) that ships to the partner with the kit. docType ∈ promo_video|explainer_video.
  uploadProductKitVideo: (changeId, docType, file) => {
    const form = new FormData()
    form.append('file', file)
    return api.post(`/changes/${changeId}/product-kit/${docType}/video`, form,
      { headers: { 'Content-Type': 'multipart/form-data' } })
  },
  // Fetch the generated/uploaded MP4 as a blob (auth'd) so a <video> can play it.
  productKitVideoBlob: (changeId, docType) =>
    api.get(`/changes/${changeId}/product-kit/${docType}/video`, { responseType: 'blob' }),
  // Provider/model/duration options for the video-gen UI (driven from settings).
  getVideoGenOptions: (changeId, docType) =>
    api.get(`/changes/${changeId}/product-kit/${docType}/video/options`),
  // Kick off AI video generation (needs an existing segmented script). Returns {job_id}.
  generateProductKitVideo: (changeId, docType, provider, model) =>
    api.post(`/changes/${changeId}/product-kit/${docType}/video/generate`, { provider, model }),
  // Poll a background job's status/progress (video-gen + others).
  getJob: (jobId) => api.get(`/jobs/${jobId}`),
  // All saved script versions for a video doc (newest first).
  listVideoScriptVersions: (changeId, docType) =>
    api.get(`/changes/${changeId}/product-kit/${docType}/versions`),
  // Full content + structured script for one saved version (read-only view).
  getVideoScriptVersion: (changeId, docType, version) =>
    api.get(`/changes/${changeId}/product-kit/${docType}/versions/${version}`),
  // Non-blocking "source changed — regenerate recommended" signal per stage.
  artifactStaleness:  (changeId)          => api.get(`/changes/${changeId}/artifacts/staleness`),
  // Presence + provenance of documents available as downstream context.
  artifactSummary:    (changeId)          => api.get(`/changes/${changeId}/artifacts/summary`),
}

// Docgen merge (Session 20+) — section-list + section-wise edit endpoints
// for the LangGraph docgen pipeline. `sections()` returns `{job_id, sections[]}`;
// when `job_id === null` the page falls through to the existing WS full-doc
// revise flow. `editSection()` regenerates a single section and returns
// `{ok, job_id, section_heading, docx_path, full}`.
export const docgenApi = {
  sections:    (changeId, docType) =>
                  api.get(`/changes/${changeId}/docgen/sections`, { params: { doc_type: docType } }),
  editSection: (changeId, docType, heading, instruction) =>
                  api.post(`/changes/${changeId}/docgen/edit`, {
                    doc_type:         docType,
                    section_heading:  heading,
                    edit_instruction: instruction,
                  }),
}

// Phase B — Design to Build
export const phaseBApi = {
  start:           (changeId, data) => api.post(`/changes/${changeId}/phase-b/start`, data),
  get:             (changeId)       => api.get(`/changes/${changeId}/phase-b`),
  reset:           (changeId)       => api.delete(`/changes/${changeId}/phase-b`),
  ingestCodebase:  (changeId, data) => api.post(`/changes/${changeId}/phase-b/ingest-codebase`, data),
  listIterations:  (changeId)       => api.get(`/changes/${changeId}/phase-b/code/iterations`),
  getIteration:    (changeId, n)    => api.get(`/changes/${changeId}/phase-b/code/iterations/${n}`),
  approveIteration:(changeId, n)    => api.post(`/changes/${changeId}/phase-b/code/iterations/${n}/approve`),
  // Code Review
  triggerCodeReview:  (changeId)    => api.post(`/changes/${changeId}/phase-b/code-review`),
  getCodeReview:      (changeId)    => api.get(`/changes/${changeId}/phase-b/code-review/latest`),
  codeReviewLoopBack: (changeId)    => api.post(`/changes/${changeId}/phase-b/code-review/loop-back`),
  // IS Review
  triggerISReview:    (changeId)    => api.post(`/changes/${changeId}/phase-b/is-review`),
  getISReview:        (changeId)    => api.get(`/changes/${changeId}/phase-b/is-review/latest`),
  isReviewLoopBack:   (changeId)    => api.post(`/changes/${changeId}/phase-b/is-review/loop-back`),
  // Git / MR
  listGitRepos:       (changeId)    => api.get(`/changes/${changeId}/phase-b/git/repos`),
  triggerGitPush:     (changeId, data = {}) => api.post(`/changes/${changeId}/phase-b/git/push`, data),
  getGitEvent:        (changeId)    => api.get(`/changes/${changeId}/phase-b/git/latest`),
  // Build + Deploy (unified) — body carries optional branch overrides and,
  // in local runner mode, the build+deploy script path (validated server-side
  // against PHASE_B_SCRIPT_ROOT). Trigger returns the QUEUED run immediately;
  // poll getBuild for the live streaming log.
  triggerBuild:       (changeId, data = {}) => api.post(`/changes/${changeId}/phase-b/build/trigger`, data),
  getBuild:           (changeId)    => api.get(`/changes/${changeId}/phase-b/build/latest`),
  // UAT tests — ONE script-based step (gen + exec combined). Trigger returns
  // the RUNNING row; poll getLatestTestRun for the streaming log + counts.
  triggerUatTests:    (changeId, data) => api.post(`/changes/${changeId}/phase-b/test/trigger`, data),
  listTestCases:      (changeId)    => api.get(`/changes/${changeId}/phase-b/test-cases`),
  getLatestTestRun:   (changeId)    => api.get(`/changes/${changeId}/phase-b/test-runs/latest`),
  // Triage — AI triage over the build + UAT logs, plus the dev/tester walkthrough.
  runTriage:          (changeId)    => api.post(`/changes/${changeId}/phase-b/triage/run`),
  getTriage:          (changeId)    => api.get(`/changes/${changeId}/phase-b/triage/latest`),
  // Advance pipeline step (build → test_gen(UAT) → triage → completed)
  advanceStep:        (changeId)    => api.post(`/changes/${changeId}/phase-b/advance-step`),
  // Agentic handover: agentic run (code+review+git) approved → pipeline jumps to BUILD.
  agenticComplete:    (changeId)    => api.post(`/changes/${changeId}/phase-b/agentic-complete`),
}

// RAG / Knowledge Base (admin)
export const ragApi = {
  ingest:      (force = false) => api.post('/rag/ingest', { force }),
  // WHY keep a synchronous variant alongside the queued Celery path:
  // the restored doc-generation-wiring ingestion flow is especially useful
  // in local/admin sessions where users want KB changes visible immediately.
  reingest:    (force = false) => api.post('/rag/reingest', { force }),
  taskStatus:  (taskId)        => api.get(`/rag/task/${taskId}`),
  status:      ()              => api.get('/rag/status'),
  categories:  ()              => api.get('/rag/categories'),
  upload:      (file, category) => {
    const form = new FormData()
    form.append('file', file)
    form.append('category', category)
    return api.post('/rag/upload', form, { headers: { 'Content-Type': 'multipart/form-data' } })
  },
  deleteFile:  (fileName)      => api.delete(`/rag/file/${encodeURIComponent(fileName)}`),
  clearChunks: ()              => api.delete('/rag/chunks'),
  search:      (data)          => api.post('/rag/search', data),
  bm25Status:  ()              => api.get('/rag/bm25/status'),
  bm25Rebuild: ()              => api.post('/rag/bm25/rebuild'),
}

// Code Indexing (admin)
export const codeIndexingApi = {
  listRepos:  ()          => api.get('/admin/code-indexing/repos'),
  addRepo:    (data)      => api.post('/admin/code-indexing/repos', data),
  updateRepo: (repoId, data) => api.patch(`/admin/code-indexing/repos/${repoId}`, data),
  removeRepo: (repoId)    => api.delete(`/admin/code-indexing/repos/${repoId}`),
  indexRepo:  (repoId)    => api.post(`/admin/code-indexing/repos/${repoId}/index`),
  getContext: (repoId)    => api.get(`/admin/code-indexing/repos/${repoId}/context`),
  status:     ()          => api.get('/admin/code-indexing/status'),
}

// API Registry — canonical network wire-API field constraints (deterministic TSD specs)
export const apiRegistryApi = {
  listMessages: ()               => api.get('/api-registry/messages'),
  getMessage:   (id)             => api.get(`/api-registry/messages/${id}`),
  patchMessage: (id, data)       => api.patch(`/api-registry/messages/${id}`, data),
  patchField:   (fieldId, data)  => api.patch(`/api-registry/fields/${fieldId}`, data),
  ingest:       (xsdDir)         => api.post('/api-registry/ingest', { xsd_dir: xsdDir || null }),
  harvestCode:  (javaDir)        => api.post('/api-registry/harvest-code', { java_dir: javaDir || null }),
  getProductionSource: ()        => api.get('/api-registry/production-source'),
  // One baseline per repo: selecting a row clears only same-repo rows; gitlabRepo
  // scopes a clear (repoId null) to that repo's selection.
  setProductionSource: (repoId, gitlabRepo) => api.put('/api-registry/production-source',
    { repo_id: repoId || null, gitlab_repo: gitlabRepo || null }),
}

// Agentic XSD-driven codegen — chat-the-intent flow (THE BOOK §13).
export const agenticApi = {
  quickStart: (data)            => api.post('/agentic/quick-start', data),
  listRuns:   ()                => api.get('/agentic/runs'),
  getRun:     (runId)           => api.get(`/agentic/runs/${runId}`),
  getEvents:  (runId, after=-1) => api.get(`/agentic/runs/${runId}/events`, { params: { after_seq: after } }),
  getDiff:    (runId)           => api.get(`/agentic/runs/${runId}/diff`),
  // Generated code (all selected repos' working trees) as a ZIP — available from the
  // approval gate onward, before any push, so a developer can inspect the changes locally.
  workspaceZip: (runId)         => api.get(`/agentic/runs/${runId}/workspace-zip`, { responseType: 'blob' }),
  approve:    (runId, hash, push = true, overrideBlockers = false, overrideReason = null) => api.post(`/agentic/runs/${runId}/approve`, { manifest_hash: hash, push, override_blockers: overrideBlockers, override_reason: overrideReason }),
  pushRun:    (runId, overrideBlockers = false, overrideReason = null) => api.post(`/agentic/runs/${runId}/push`, null, overrideBlockers ? { params: { override_blockers: true, override_reason: overrideReason || '' } } : undefined),
  runUsage:    (runId)                  => api.get(`/agentic/runs/${runId}/usage`),
  usageByChange: ()                     => api.get(`/agentic/usage/changes`),
  usageChangeDetail: (changeId)         => api.get(`/agentic/usage/changes/${changeId}`),
  usageOther:  ()                       => api.get(`/agentic/usage/other`),
  stuckHelp:   (runId)                  => api.post(`/agentic/runs/${runId}/stuck-help`),
  stuckDecide: (runId, payload)         => api.post(`/agentic/runs/${runId}/stuck-decide`, payload),
  cancel:     (runId, force = false) => api.post(`/agentic/runs/${runId}/cancel`, null, force ? { params: { force: true } } : undefined),
  resume:     (runId)           => api.post(`/agentic/runs/${runId}/resume`),
  // Plain-language developer + tester walkthrough of the change (on-demand) + QA-sheet CSV.
  getWalkthrough:      (runId)  => api.get(`/agentic/runs/${runId}/walkthrough`),
  generateWalkthrough: (runId)  => api.post(`/agentic/runs/${runId}/walkthrough`),
  walkthroughCsv:      (runId)  => api.get(`/agentic/runs/${runId}/walkthrough.csv`, { responseType: 'blob' }),
  // Phase A/B split (THE BOOK v3.4)
  start:      (changeId, data)  => api.post(`/changes/${changeId}/agentic/start`, data),
  listChangeRuns: (changeId, kind) => api.get(`/changes/${changeId}/agentic/runs`, kind ? { params: { kind } } : undefined),
  // Transcript logs for a change as one ZIP — every pipeline section by default, or just
  // one when `section` is passed (the keys in the backend's _TRANSCRIPT_SECTIONS).
  transcriptsZip: (changeId, section) => api.get(`/changes/${changeId}/agentic/transcripts-zip`,
    { responseType: 'blob', ...(section ? { params: { section } } : {}) }),
  preflight:  ()                              => api.get(`/agentic/preflight`),
  health:     ()                              => api.get(`/agentic/health`),
  startXsd:   (changeId, repoIds, intent='')  => api.post(`/changes/${changeId}/agentic/start`, { repo_ids: repoIds, intent, kind: 'xsd' }),
  startCode:  (changeId, repoIds, intent='')  => api.post(`/changes/${changeId}/agentic/start`, { repo_ids: repoIds, intent, kind: 'code' }),
  // Re-run Phase B from the approved Phase-A baseline as a FRESH run (clean workspace, no cache) —
  // for testing code-gen consistency across attempts. Cancels the prior code run, keeps its result.
  rerunCode:  (changeId, intent='') => api.post(`/changes/${changeId}/agentic/rerun-code`, { repo_ids: [], intent, kind: 'code' }),
  approveXsd: (runId, hash)     => api.post(`/agentic/runs/${runId}/approve-xsd`, { manifest_hash: hash }),
  // Reuse-first approach gate + XSD refine loop (THE BOOK v3.4)
  decideApproach:   (runId, payload) => api.post(`/agentic/runs/${runId}/decide-approach`, payload),
  requestXsdChanges:(runId, feedback) => api.post(`/agentic/runs/${runId}/request-xsd-changes`, { feedback }),
  decideRevision:   (runId, payload) => api.post(`/agentic/runs/${runId}/decide-revision`, payload),
  getXsdFiles:      (runId)          => api.get(`/agentic/runs/${runId}/xsd-files`),
  // Change-Analysis stage (accuracy S2/S3)
  startAnalysis:        (changeId, repoIds, intent='') => api.post(`/changes/${changeId}/agentic/start`, { repo_ids: repoIds, intent, kind: 'analysis' }),
  decideClarifications: (runId, payload)  => api.post(`/agentic/runs/${runId}/decide-clarifications`,
    Array.isArray(payload) ? { answers: payload } : payload),
  decidePlan:           (runId, payload)  => api.post(`/agentic/runs/${runId}/decide-plan`, payload),
  decideVerify:         (runId, action)   => api.post(`/agentic/runs/${runId}/decide-verify`, { action }),
  decideCodeDecision:   (runId, payload)  => api.post(`/agentic/runs/${runId}/decide-code-decision`, payload),
  // Fix 2 — rule on a schema change the code phase staged but may not apply itself.
  // { approve: bool, reason?: string } — reason is required when rejecting.
  decideSchemaAmendment:(runId, payload)  => api.post(`/agentic/runs/${runId}/decide-schema-amendment`, payload),
  // ADR-0005 / SDLC review gap 4 — resume a run parked at the TSD approval gate.
  decideTsdApproval:    (runId)           => api.post(`/agentic/runs/${runId}/decide-tsd-approval`),
  // Re-run the build verification on an already-generated/approved change (pass/fail only).
  reverify:             (runId)            => api.post(`/agentic/runs/${runId}/reverify`),
  getAnalysis:          (changeId)        => api.get(`/changes/${changeId}/analysis`),
  ensureAnalysis:       (changeId, repoIds=null, opts={}) => api.post(`/changes/${changeId}/analysis/ensure`, { repo_ids: repoIds, ...(opts.restart ? { restart: true } : {}) }),
  getAnalysisRepos:     (changeId)        => api.get(`/changes/${changeId}/analysis/repos`),
}

// App Configuration (admin)
export const configApi = {
  getAll:       ()     => api.get('/admin/config'),
  update:       (data) => api.put('/admin/config', data),
  testGitlab:   ()     => api.post('/admin/config/test-gitlab'),
  testOllama:   ()     => api.post('/admin/config/test-ollama'),
}

// Phase B build host smoke test (admin) — verifies build_and_deploy.sh with no
// change request. The full run is a background task, hence the poll endpoint.
export const buildSmokeApi = {
  getConfig:  ()             => api.get('/admin/build-smoke'),
  preflight:  ()             => api.post('/admin/build-smoke/preflight'),
  startRun:   (body)         => api.post('/admin/build-smoke/run', body),
  pollRun:    (id, since=0)  => api.get(`/admin/build-smoke/run/${id}`, { params: { since } }),
}

// Push to cert-agent — handled by the streaming WebSocket at
// /a2a/api/ws/changes/{id}/cert-push opened directly by CertPushPanel.
// The legacy POST /changes/{id}/cert-push endpoint still exists on the
// backend for back-compat but is no longer called from the UI (its 180-s
// timeout tripped past ~80 test cases — that's why we moved to WS).

// Phase C
export const phaseCApi = {
  listAssignedPartners: (changeId)       => api.get(`/changes/${changeId}/partners`),
  assignPartners:       (changeId, ids)  => api.post(`/changes/${changeId}/partners`, { partner_ids: ids }),
  removePartner:        (changeId, pid)  => api.delete(`/changes/${changeId}/partners/${pid}`),
  communicate:          (changeId, body = {}, version) => api.post(`/changes/${changeId}/phase-c/communicate`, body, version != null ? { params: { negotiation_version: version } } : undefined),
  shipKit:              (changeId, partnerIds, version, includeDocTypes) => api.post(`/changes/${changeId}/phase-c/ship-kit`, {
    partner_ids:        partnerIds,
    negotiation_version: version ?? null,
    include_doc_types:  includeDocTypes ?? null,
  }),
  // Ship-time overrides (migration 0115). Per-doc-type file the PM uploads to
  // substitute the generated artifact on the next kit ship.
  shipManifest:         (changeId) => api.get(`/changes/${changeId}/artifacts/ship-manifest`),
  uploadShipOverride:   (changeId, docType, file) => {
    const form = new FormData()
    form.append('file', file)
    return api.put(`/changes/${changeId}/artifacts/${docType}/ship-override`, form,
      { headers: { 'Content-Type': 'multipart/form-data' } })
  },
  clearShipOverride:    (changeId, docType) =>
    api.delete(`/changes/${changeId}/artifacts/${docType}/ship-override`),
  status:               (changeId)       => api.get(`/changes/${changeId}/phase-c/status`),
  messages:             (changeId)       => api.get(`/changes/${changeId}/phase-c/messages`),
  // Negotiation
  getNegotiation:       (changeId, pid)  => api.get(`/changes/${changeId}/partners/${pid}/negotiation`),
  // Blockers live in their own thread (kind='blocker') so operational escalations don't
  // interleave with spec negotiation. Without this the blocker stream is unreachable.
  getBlockerThread:     (changeId, pid)  => api.get(`/changes/${changeId}/partners/${pid}/negotiation`, { params: { kind: 'blocker' } }),
  processQuery:         (changeId, pid)  => api.post(`/changes/${changeId}/partners/${pid}/negotiation/query`),
  // `counterProposalId` targets ONE counter (omit → legacy sweep of all open ones).
  // `kind='blocker'` posts a free-text note into the blocker thread as an interim
  // BLOCKER_STATUS_UPDATE — it does NOT close the blocker (only /resolve does).
  // `queryCorrelationId` names the partner query this reply answers; the backend echoes
  // it back as query_id. Omit -> the backend falls back to the newest open query.
  respondToQuery:       (changeId, pid, text, { counterProposalId = null, blockerId = null, kind = null, queryCorrelationId = null } = {}) =>
    api.post(`/changes/${changeId}/partners/${pid}/negotiation/respond`,
      { response_text: text, counter_proposal_id: counterProposalId, blocker_id: blockerId,
        query_correlation_id: queryCorrelationId },
      kind ? { params: { kind } } : undefined),
  // Counter-proposals (Tier 1 — structured negotiation)
  listCounters:         (changeId, pid)  => api.get(`/changes/${changeId}/partners/${pid}/counter-proposals`),
  acceptCounter:        (changeId, pid, cpId) => api.post(`/changes/${changeId}/partners/${pid}/counter-proposals/${cpId}/accept`),
  rejectCounter:        (changeId, pid, cpId, rationale) => api.post(`/changes/${changeId}/partners/${pid}/counter-proposals/${cpId}/reject`, { rationale }),
  counterBackCounter:   (changeId, pid, cpId, body)     => api.post(`/changes/${changeId}/partners/${pid}/counter-proposals/${cpId}/counter`, body),
  // Blockers (Tier 2 — partner reports obstacle, PM picks resolution option)
  listBlockers:         (changeId, pid)  => api.get(`/changes/${changeId}/partners/${pid}/blockers`),
  resolveBlocker:       (changeId, pid, bId, body) => api.post(`/changes/${changeId}/partners/${pid}/blockers/${bId}/resolve`, body),
  // Status Tracking
  getPartnerProgress:   (changeId, pid)  => api.get(`/changes/${changeId}/partners/${pid}/progress`),
  getProgressGrid:      (changeId)       => api.get(`/changes/${changeId}/phase-c/progress-grid`),
  // Certification
  // dispatchCert drives the harness-agnostic seam (the domain pack's declared
  // harness — sim_pack rounds, real A2A announce, join-finalized verdicts).
  // startCert below is the LEGACY cert-agent delegation kept for cert_engine
  // deployments; prefer dispatchCert.
  dispatchCert:         (changeId, pid, body) => api.post(`/changes/${changeId}/partners/${pid}/cert/dispatch`, body || {}),
  startCert:            (changeId, pid)  => api.post(`/changes/${changeId}/partners/${pid}/cert/start`),
  listCertRuns:         (changeId, pid)  => api.get(`/changes/${changeId}/partners/${pid}/cert/runs`),
  getCertRun:           (changeId, pid, runId) => api.get(`/changes/${changeId}/partners/${pid}/cert/runs/${runId}`),
  triggerTriage:        (changeId, pid)       => api.post(`/changes/${changeId}/partners/${pid}/cert/triage`),
  approveTriage:        (changeId, pid, triageId, verdict) => api.post(`/changes/${changeId}/partners/${pid}/cert/triage/${triageId}/approve`, { verdict }),
  // Precert engine — demo controls + the real network transactions behind a cert run
  demoRunCert:          (changeId, pid)  => api.post(`/changes/${changeId}/partners/${pid}/cert/demo-run`),
  demoResetCert:        (changeId, pid)  => api.post(`/changes/${changeId}/partners/${pid}/cert/demo-reset`),
  certTxns:          (changeId, pid)  => api.get(`/changes/${changeId}/partners/${pid}/cert/txns`),
  // Partner status transition audit trail (read-only) — see backend/app/api/assignment_actions.py
  statusHistory:        (changeId, pid)         => api.get(`/changes/${changeId}/partners/${pid}/status-history`),
  // ── Negotiation Hub (partner negotiation phase) ──────────────────────────
  // BRD Requirements
  listBRDRequirements:  (changeId)             => api.get(`/changes/${changeId}/brd-requirements`),
  createBRDRequirement: (changeId, body)       => api.post(`/changes/${changeId}/brd-requirements`, body),
  generateBRDRequirements: (changeId)          => api.post(`/changes/${changeId}/brd-requirements/generate`),
  updateBRDRequirement: (changeId, reqId, body) => api.put(`/changes/${changeId}/brd-requirements/${reqId}`, body),
  deleteBRDRequirement: (changeId, reqId)      => api.delete(`/changes/${changeId}/brd-requirements/${reqId}`),
  // Round states
  listRounds:           (changeId)             => api.get(`/changes/${changeId}/negotiation/rounds`),
  closeRound:           (changeId, pid)        => api.post(`/changes/${changeId}/negotiation/rounds/${pid}/close`),
  sweepSilentAccept:    (changeId)             => api.post(`/changes/${changeId}/negotiation/rounds/sweep`),
  // Cross-partner clusters
  listClusters:         (changeId)             => api.get(`/changes/${changeId}/negotiation/clusters`),
  decideCluster:        (changeId, cId, body)  => api.post(`/changes/${changeId}/negotiation/clusters/${cId}/decide`, body),
  refreshClusterAI:     (changeId, cId)        => api.post(`/changes/${changeId}/negotiation/clusters/${cId}/refresh-ai`),
  // Governance
  negotiationStatus:    (changeId)             => api.get(`/changes/${changeId}/negotiate/status`),
  finalizeNegotiation:  (changeId)             => api.post(`/changes/${changeId}/negotiate/finalize`),
  // Reviewed — no kit change this round: close the round + start the next
  // one (same version), or freeze once the round cap is reached.
  advanceRound:         (changeId)             => api.post(`/changes/${changeId}/negotiate/advance-round`),
  createNewVersion:     (changeId)             => api.post(`/changes/${changeId}/negotiate/new-version`),
  newVersionAndShip:    (changeId)             => api.post(`/changes/${changeId}/negotiate/new-version-and-ship`),
  // Round-close consolidation + editable revision plan → generate v(N+1)
  roundCloseSummary:    (changeId)             => api.get(`/changes/${changeId}/negotiate/round-close-summary`),
  getRevisionPlan:      (changeId)             => api.get(`/changes/${changeId}/negotiate/revision-plan`).then(r => r.data),
  draftRevisionPlan:    (changeId)             => api.post(`/changes/${changeId}/negotiate/revision-plan/draft`).then(r => r.data),
  saveRevisionPlan:     (changeId, body)       => api.put(`/changes/${changeId}/negotiate/revision-plan`, body).then(r => r.data),
  generateRevisionKit:  (changeId)             => api.post(`/changes/${changeId}/negotiate/revision-plan/generate`).then(r => r.data),
  downloadRevisionSummary: (changeId)          => api.get(`/changes/${changeId}/negotiate/revision-plan/summary.docx`, { responseType: 'blob' }).then(r => r.data),
}

// Eval harness APIs
export const evalApi = {
  latest: (changeId, checkpoint) => api.get(`/changes/${changeId}/eval/latest`, {
    params: checkpoint ? { checkpoint } : undefined,
  }),
  verdicts: (changeId, params = {}) => api.get(`/changes/${changeId}/eval/verdicts`, { params }),
  override: (changeId, data) => api.post(`/changes/${changeId}/eval/override`, data),
  listPolicies: () => api.get('/admin/eval/policies'),
  updatePolicies: (data) => api.put('/admin/eval/policies', data),
  listPolicyAudit: (params = {}) => api.get('/admin/eval/policy-audit', { params }),
  // Phase 7 — global eval log across all changes (admin only)
  listAllVerdicts: (params = {}) => api.get('/admin/eval/verdicts', { params }),
  // Phase A Excellence — Slice 5: aggregated metrics for the dashboard
  metrics: (params = {}) => api.get('/admin/eval/metrics', { params }),
  // Phase A Excellence — Slice 4: per-change impact + side-by-side comparison
  changeImpact: (changeId) => api.get(`/changes/${changeId}/eval/impact`),
  compareChanges: (changeA, changeB) => api.get('/admin/eval/compare', {
    params: { change_a: changeA, change_b: changeB },
  }),
  // Phase A Excellence — Slice 6: re-run advisory eval on an existing change
  rerunCheckpoint: (changeId, checkpoint) => api.post(
    `/changes/${changeId}/eval/rerun`, null,
    { params: { checkpoint } },
  ),
}

// Cert simulator sync (Phase A test cases → cert-agent's tc_store)
export const certSyncApi = {
  diff:  (changeId)            => api.post(`/changes/${changeId}/cert-simulator/diff`),
  // body: { decisions, flow_registrations }. flow_registrations carries the
  // operator's inline registrations for any unknown api_request the diff surfaced.
  apply: (changeId, decisions, flow_registrations = []) =>
    api.post(`/changes/${changeId}/cert-simulator/apply`, { decisions, flow_registrations }),
  log:   (changeId)            => api.get(`/changes/${changeId}/cert-simulator/log`),
}

// Standalone Certification dashboards (cross-change aggregations)
export const certificationApi = {
  dashboard:        ()         => api.get('/certification/dashboard'),
  changeSummary:    (changeId) => api.get(`/changes/${changeId}/cert-summary`),
  agentMessages:    (limit=100) => api.get('/certification/agent-messages', { params: { limit } }),
  // Cert lifecycle timeline aggregator
  timeline:         (params={}) => api.get('/cert-status/timeline', { params }),
  timelineChanges:  ()          => api.get('/cert-status/timeline/changes'),
}

// Partners (admin)
export const partnersApi = {
  list:       ()           => api.get('/admin/partners'),
  create:     (data)       => api.post('/admin/partners', data),
  update:     (id, data)   => api.put(`/admin/partners/${id}`, data),
  rotateKey:  (id)         => api.post(`/admin/partners/${id}/rotate-key`),
  deactivate: (id)         => api.delete(`/admin/partners/${id}`),
  activate:   (id)         => api.post(`/admin/partners/${id}/activate`),
  test:       (id)         => api.post(`/admin/partners/${id}/test`),
  stats:      ()           => api.get('/admin/partners/stats'),
  // Slice 6 — A2A wire selector. body: {protocol_version: 'legacy'|'a2a_sdk'}
  setProtocol: (id, protocol_version) =>
    api.patch(`/admin/partners/${id}/protocol`, { protocol_version }),

  // ── A2A security hardening — endpoints added by Slices 2/3/5/6/7 ──
  // Slice 3 — rotate the per-partner JWT signing secret (HS256, used
  // by the Authority to sign outbound Bearer JWTs to this partner).
  rotateJwtSecret:  (id) => api.post(`/admin/partners/${id}/rotate-jwt-secret`),
  // Slice 5 — rotate the per-partner HMAC envelope secret (signs
  // X-NPCI-Signature on every outbound body).
  rotateHmacSecret: (id) => api.post(`/admin/partners/${id}/rotate-hmac-secret`),
  // Slice 6 — bank-tier mTLS controls. tls_tier flips the partner
  // between :443 (jwt) and :8443 (mtls). client_cert_fingerprint is
  // the SHA-256 hex pinned at the nginx ingress.
  setTlsTier:        (id, tls_tier) =>
    api.patch(`/admin/partners/${id}/tls-tier`, { tls_tier }),
  setCertFingerprint: (id, client_cert_fingerprint) =>
    api.patch(`/admin/partners/${id}/cert-fingerprint`, { client_cert_fingerprint }),
  // Slice 7 — network controls. allowed_cidrs is null/[] for "no
  // enforcement" or a list of CIDR strings.
  setAllowedCidrs:   (id, allowed_cidrs) =>
    api.patch(`/admin/partners/${id}/allowed-cidrs`, { allowed_cidrs }),
  setRateLimit:      (id, rate_limit_rps) =>
    api.patch(`/admin/partners/${id}/rate-limit`, { rate_limit_rps }),
  // Slice 2 — revoke all active sessions for a partner (emergency lever).
  revokeSessions:    (id) => api.post(`/admin/partners/${id}/revoke-sessions`),
  // Outbound TLS — how the backend verifies THIS partner's HTTPS server cert
  // (Test-connectivity probe + real A2A calls). ssl_verify: true|false|null(inherit).
  setSslVerify:      (id, ssl_verify) =>
    api.patch(`/admin/partners/${id}/ssl-verify`, { ssl_verify }),
  getCaCert:         (id) => api.get(`/admin/partners/${id}/ca-cert`),
  uploadCaCert:      (id, ca_cert_pem) =>
    api.post(`/admin/partners/${id}/ca-cert`, { ca_cert_pem }),
  // Per-partner cap (wire bytes) on inline kit-attachment size; oversize
  // attachments are omitted from the A2A envelope. null=inherit global, 0=no limit.
  setMaxInlineAttachment: (id, max_inline_attachment_bytes) =>
    api.patch(`/admin/partners/${id}/max-inline-attachment`, { max_inline_attachment_bytes }),
}

// Logs (admin)
export const logsApi = {
  recent: (n = 200) => api.get('/logs', { params: { n } }),
}

// A2A communications log (admin) — Slice 25.
// `params` shape: { change_request_id?, change_title?, partner_name?,
//                   direction?, task_type?, success_only?, limit?, offset? }
// Operational notifications (delivery failures, BRD mandatory rejections, …).
// Scoped server-side to the calling user.
export const notificationsApi = {
  list:      (params = {}) => api.get('/notifications', { params }),
  markRead:  (id)          => api.post(`/notifications/${id}/read`),
  markAllRead: ()          => api.post('/notifications/read-all'),
}

export const a2aLogsApi = {
  list:  (params = {}) => api.get('/admin/a2a-logs', { params }),
  stats: ()            => api.get('/admin/a2a-logs/stats'),
  // Re-attempt delivery of a failed OUTBOUND message. Re-uses the SAME audit row
  // (attempts increments) instead of creating a new one, so the trail stays one
  // record per logical send. Complements the automatic retry sweeper for the cases
  // it won't touch: attempts exhausted, or a non-retryable 4xx an operator has fixed.
  resend: (id)         => api.post(`/admin/a2a-logs/${id}/resend`),
}

// Hand-fire Part B (certification) A2A messages for internal testing.
// `send` originates Authority→Partner / Either messages at a registered partner or a raw
// base URL; `simulateInbound` posts a Partner→Authority message at our own ingress so the
// receive path (HMAC → JWT → executor) runs for real.
export const certA2AApi = {
  templates:       ()     => api.get('/admin/cert-a2a/templates'),
  send:            (body) => api.post('/admin/cert-a2a/send', body),
  simulateInbound: (body) => api.post('/admin/cert-a2a/simulate-inbound', body),
}

// Clarifications (Sprint 5) — pre-generation gap questions for PM
export const clarifyApi = {
  trigger:  (changeId)          => api.post(`/changes/${changeId}/clarify`),
  get:      (changeId)          => api.get(`/changes/${changeId}/clarifications`),
  answer:   (changeId, answers) => api.post(`/changes/${changeId}/clarifications/answer`, { answers }),
  skip:     (changeId)          => api.post(`/changes/${changeId}/clarifications/skip`),
  rerun:    (changeId)          => api.post(`/changes/${changeId}/clarifications/rerun`),
}

// Validation (re-runs validator over stored content for detail views)
export const validationApi = {
  run: (changeId, docType, subtype) =>
    api.get(`/changes/${changeId}/validation/${docType}`, {
      params: subtype ? { subtype } : undefined,
    }),
}

// Artifact download — historically named "docxApi" (Sprint 5); now also
// serves .xsd / .zip for the XSD route (server picks based on doc_type).
// Returns both the blob and the server-suggested filename so the caller
// can save with the right extension.
export const docxApi = {
  url: (changeId, docType, subtype) => {
    const qs = subtype ? `?subtype=${encodeURIComponent(subtype)}` : ''
    return `${api.defaults.baseURL}/changes/${changeId}/artifacts/${docType}/download${qs}`
  },
  download: async (changeId, docType, subtype) => {
    const response = await api.get(
      `/changes/${changeId}/artifacts/${docType}/download`,
      { params: subtype ? { subtype } : undefined, responseType: 'blob' },
    )
    return { blob: response.data, filename: _filenameFromContentDisposition(response.headers) }
  },
}

// D8 — Product Deck companion. Same shape as docxApi; backed by
// a separate route that serves the pre-rendered .pptx (no on-demand
// build — if the LLM didn't emit a valid JSON outline at gen time,
// the route 404s and the operator must regenerate).
export const pptxApi = {
  download: async (changeId, docType, subtype) => {
    const response = await api.get(
      `/changes/${changeId}/artifacts/${docType}/download/pptx`,
      { params: subtype ? { subtype } : undefined, responseType: 'blob' },
    )
    return { blob: response.data, filename: _filenameFromContentDisposition(response.headers) }
  },
}


// RFC 6266 / 5987 lite parser — pulls `filename="..."` (or `filename*=...`)
// out of a Content-Disposition header. Returns null when the server
// didn't suggest one; caller falls back to its own naming.
function _filenameFromContentDisposition(headers) {
  if (!headers) return null
  const cd = headers['content-disposition'] || headers['Content-Disposition']
  if (!cd) return null
  // Prefer the RFC 5987 form (filename*=UTF-8''…) when present.
  const star = cd.match(/filename\*\s*=\s*[^']*''([^;]+)/i)
  if (star) {
    try { return decodeURIComponent(star[1]) } catch { /* fall through */ }
  }
  const plain = cd.match(/filename\s*=\s*"?([^";]+)"?/i)
  return plain ? plain[1] : null
}

// XLSX download — Excel Testcase Engine.
// WHY a separate API (vs. extending docxApi): the engine writes the .xlsx
// to its own outputs directory and exposes a dedicated endpoint
// (/changes/{id}/product-kit/cert_test_cases/xlsx). The DOCX endpoint
// reads the ProductKitDocument table; the XLSX endpoint reads the host
// job_registry entry that the engine attached file paths to. Different
// data path → different API namespace.
export const xlsxApi = {
  url: (changeId) =>
    `${api.defaults.baseURL}/changes/${changeId}/product-kit/cert_test_cases/xlsx`,
  download: async (changeId) => {
    const response = await api.get(
      `/changes/${changeId}/product-kit/cert_test_cases/xlsx`,
      { responseType: 'blob' },
    )
    return response.data
  },
  // User-uploaded override — replaces the currently-latest cert_test_cases
  // xlsx via a superseding AgentJob. Downstream (download + Product Kit
  // shipping) picks this up automatically because of latest-wins ordering.
  uploadOverride: async (changeId, file) => {
    const form = new FormData()
    form.append('file', file)
    const response = await api.post(
      `/changes/${changeId}/product-kit/cert_test_cases/xlsx/upload`,
      form,
      { headers: { 'Content-Type': 'multipart/form-data' } },
    )
    return response.data
  },
  // Revert to the last engine-generated pack (skips over prior user
  // uploads). 409 when there is no engine-generated predecessor.
  revertOverride: async (changeId) => {
    const response = await api.post(
      `/changes/${changeId}/product-kit/cert_test_cases/xlsx/revert`,
    )
    return response.data
  },
  // Small status probe — the UI uses this to show "v3 (uploaded by X)"
  // vs "engine-generated" and to enable/disable the revert button.
  status: async (changeId) => {
    try {
      const response = await api.get(
        `/changes/${changeId}/product-kit/cert_test_cases/xlsx/status`,
      )
      return response.data
    } catch (err) {
      if (err?.response?.status === 404) return null
      throw err
    }
  },
}

// JSON dump of the WorkbookPlan — same engine job, different export.
// Useful for downstream tooling that wants structured test cases without
// parsing the .xlsx (test-management imports, diffs, automation, etc.).
export const certTestCasesJsonApi = {
  url: (changeId) =>
    `${api.defaults.baseURL}/changes/${changeId}/product-kit/cert_test_cases/json`,
  download: async (changeId) => {
    const response = await api.get(
      `/changes/${changeId}/product-kit/cert_test_cases/json`,
      { responseType: 'blob' },
    )
    return response.data
  },
  // In-app fetch — same endpoint, parsed as JSON. Used by the Product Kit
  // table view to render structured test cases instead of rendered Markdown.
  //
  // Cache-busting: append a timestamp query param + Cache-Control: no-cache
  // header. Without this, the browser was serving stale responses after
  // Apply/Revert (the URL is identical between requests → cache hit even
  // when the backend has fresh data). This was the root cause behind
  // "changes not reflecting most of the time".
  get: async (changeId) => {
    const response = await api.get(
      `/changes/${changeId}/product-kit/cert_test_cases/json`,
      {
        params: { _t: Date.now() },
        headers: { 'Cache-Control': 'no-cache' },
      },
    )
    return response.data
  },
}

// ── the Authority Policy (admin) — feasibility resolver context source ───────────────
// Single-row table holding AUTHORITY_POLICY.md. Seeded from the bind-mounted
// file on first boot; thereafter maintained via this API.
export const authorityPolicyApi = {
  get: () => api.get('/admin/authority-policy').then(r => r.data),
  update: (content) => api.put('/admin/authority-policy', { content }).then(r => r.data),
  upload: async (file) => {
    const fd = new FormData()
    fd.append('file', file)
    const r = await api.post('/admin/authority-policy/upload', fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
    return r.data
  },
  resetToSeed: () => api.post('/admin/authority-policy/reset-to-seed').then(r => r.data),
}

// ── Governance reviews (EA → InfoSec pre-build stages) ──────────────────────
// Skill files are admin-uploaded, append-only versioned rulebooks; the stage
// flow runs as child agentic runs between code approval and Build.
export const governanceSkillsApi = {
  list:     ()             => api.get('/admin/governance-skills').then(r => r.data),
  versions: (stype)        => api.get(`/admin/governance-skills/${stype}/versions`).then(r => r.data),
  version:  (stype, v)     => api.get(`/admin/governance-skills/${stype}/versions/${v}`).then(r => r.data),
  upload: async (stype, file) => {
    const fd = new FormData()
    fd.append('file', file)
    const r = await api.post(`/admin/governance-skills/${stype}/upload`, fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
    return r.data
  },
  // Full skill BUNDLE (Agent-Skill shape: SKILL.md + scripts + references) as
  // .zip/.tar.gz, with an optional per-script exec-manifest JSON (text).
  uploadBundle: async (stype, file, execManifestText = null) => {
    const fd = new FormData()
    fd.append('file', file)
    if (execManifestText) fd.append('exec_manifest', execManifestText)
    const r = await api.post(`/admin/governance-skills/${stype}/bundle`, fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
    return r.data
  },
  // Prove-it-runs: execute every bundle script against known-bad/known-good
  // fixtures in the sandbox. A scripted bundle gates nothing until green.
  smoke: (stype, version) =>
    api.post(`/admin/governance-skills/${stype}/versions/${version}/smoke`).then(r => r.data),
  // Skill SLOTS (0118): a type holds several skills side by side; every enabled
  // slot executes in the stage. Toggling retires/reinstates a slot (audit rows stay).
  setSlotEnabled: (stype, name, enabled) =>
    api.post(`/admin/governance-skills/${stype}/slots/${encodeURIComponent(name)}/enabled`,
      { enabled }).then(r => r.data),
}

export const governanceApi = {
  status: (changeId) => api.get(`/changes/${changeId}/governance/status`).then(r => r.data),
  start:  (changeId) => api.post(`/changes/${changeId}/governance/start`).then(r => r.data),
}

// ── Feasibility Resolver (the Authority side) ────────────────────────────────────────
// Latest recommendation for a (change, partner) pair. 404 → no resolver
// run yet. The Phase C UI polls this to render the recommendation card.
export const resolverApi = {
  // Every recommendation for this (change, partner), newest first. `getLatest`
  // returns only the most recent one, which hid the suggestion for every query
  // but the newest once more than one was open.
  list: async (changeId, partnerId) => {
    try {
      const r = await silentApi.get(
        `/changes/${changeId}/resolver-recommendations`,
        { params: { partner_id: partnerId } },
      )
      return r.data?.recommendations || []
    } catch (err) {
      if (err?.response?.status === 404) return []
      throw err
    }
  },
  getLatest: async (changeId, partnerId) => {
    try {
      const r = await silentApi.get(
        `/changes/${changeId}/resolver-recommendation`,
        { params: { partner_id: partnerId } },
      )
      return r.data
    } catch (err) {
      if (err?.response?.status === 404) return null
      throw err
    }
  },
  run: (changeId, a2aMessageId) =>
    api.post(`/changes/${changeId}/resolve/${a2aMessageId}`).then(r => r.data),
}

// ── Escalations (Risk / InfoSec / Tech review-team inboxes) ─────────────────
export const escalationsApi = {
  list: (params = {}) => api.get('/escalations', { params }).then(r => r.data),
  respond: (ticketId, responseText) =>
    api.post(`/escalations/${ticketId}/respond`, { response_text: responseText }).then(r => r.data),
}

export default api
