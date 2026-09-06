<!--
GENERATED FILE -- DO NOT EDIT BY HAND.
Your change will be overwritten by the next regeneration, and CI compares
this file against a fresh run.

    Regenerate: bash scripts/wiki/regenerate.sh
    Generator:  scripts/wiki/generate_reference.py
-->
# HTTP API reference

> **Generated** from `the FastAPI OpenAPI schema`, against alembic head `0123_governance_skill_slots`.
> Do not edit by hand -- run `bash scripts/wiki/regenerate.sh`.

324 operations across 34 tags, taken from the running application rather than from the router source -- so anything mounted conditionally appears exactly as it is actually served.

## a2a

| Method | Path | Summary |
|---|---|---|
| `POST` | `/api/a2a/auth` | Authenticate Partner |
| `POST` | `/api/a2a/auth/refresh` | Refresh Partner Token |
| `GET` | `/api/a2a/tasks` | List Tasks |
| `GET` | `/api/a2a/tasks/{task_id}` | Get Task Status |
| `GET` | `/api/a2a/threads` | List All Threads |

## a2a-logs

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/admin/a2a-logs` | List A2A Messages |
| `GET` | `/api/admin/a2a-logs/stats` | A2A Logs Stats |
| `POST` | `/api/admin/a2a-logs/{message_id}/resend` | Resend A2A Message |

## admin-build-smoke

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/admin/build-smoke` | Get Config |
| `POST` | `/api/admin/build-smoke/preflight` | Run Preflight |
| `POST` | `/api/admin/build-smoke/run` | Start Full Run |
| `GET` | `/api/admin/build-smoke/run/{run_id}` | Poll Run |

## admin-authority-policy

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/admin/authority-policy` | Get Policy |
| `PUT` | `/api/admin/authority-policy` | Update Policy |
| `POST` | `/api/admin/authority-policy/reset-to-seed` | Reset To Seed |
| `POST` | `/api/admin/authority-policy/upload` | Upload Policy |

## agents

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/approvals/pending` | Get Pending Approvals |
| `POST` | `/api/approvals/{approval_id}/respond` | Respond Approval |
| `POST` | `/api/changes/{change_id}/advance` | Advance Status |
| `GET` | `/api/changes/{change_id}/brd` | Get Brd |
| `GET` | `/api/changes/{change_id}/brd/approvals` | Get Brd Approvals |
| `POST` | `/api/changes/{change_id}/brd/dev-auto-approve` | Dev Auto Approve Brd |
| `POST` | `/api/changes/{change_id}/brd/submit` | Submit Brd |
| `GET` | `/api/changes/{change_id}/brd/versions` | List Brd Versions |
| `GET` | `/api/changes/{change_id}/brd/versions/{version}` | Get Brd Version |
| `GET` | `/api/changes/{change_id}/canvas` | Get Canvas |
| `GET` | `/api/changes/{change_id}/canvas/download` | Download Canvas Docx |
| `GET` | `/api/changes/{change_id}/conversation/{module}` | Get Conversation |
| `POST` | `/api/changes/{change_id}/docgen/edit` | Docgen Edit Section |
| `GET` | `/api/changes/{change_id}/docgen/sections` | Docgen Sections |
| `GET` | `/api/changes/{change_id}/product-kit` | Get Product Kit All |
| `POST` | `/api/changes/{change_id}/product-kit/complete` | Complete Product Kit |
| `GET` | `/api/changes/{change_id}/product-kit/{doc_type}` | Get Product Kit Doc |
| `GET` | `/api/changes/{change_id}/research` | Get Research |
| `GET` | `/api/changes/{change_id}/tech-spec` | Get Tech Spec |
| `GET` | `/api/changes/{change_id}/xsd` | Get Xsd |
| `POST` | `/api/changes/{change_id}/xsd/assess` | Assess Xsd |
| `GET` | `/api/config/ui` | Get Ui Config |

## api-registry

| Method | Path | Summary |
|---|---|---|
| `PATCH` | `/api/api-registry/fields/{field_id}` | Patch Field |
| `POST` | `/api/api-registry/harvest-code` | Harvest Code |
| `POST` | `/api/api-registry/ingest` | Ingest |
| `GET` | `/api/api-registry/messages` | List Messages |
| `GET` | `/api/api-registry/messages/{message_id}` | Get Message |
| `PATCH` | `/api/api-registry/messages/{message_id}` | Patch Message |
| `GET` | `/api/api-registry/production-source` | Get Production Source |
| `PUT` | `/api/api-registry/production-source` | Set Production Source |

## app-config

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/admin/config` | Get All Config |
| `PUT` | `/api/admin/config` | Update Config |
| `POST` | `/api/admin/config/test-gitlab` | Test Gitlab Connection |
| `POST` | `/api/admin/config/test-ollama` | Test Ollama Connection |

## assignment-actions

| Method | Path | Summary |
|---|---|---|
| `POST` | `/api/changes/{change_id}/partners/{partner_id}/approve-for-production` | Approve For Production |
| `POST` | `/api/changes/{change_id}/partners/{partner_id}/block` | Block Partner |
| `POST` | `/api/changes/{change_id}/partners/{partner_id}/mark-live` | Mark Live |
| `GET` | `/api/changes/{change_id}/partners/{partner_id}/status-history` | Status History |
| `POST` | `/api/changes/{change_id}/partners/{partner_id}/unblock` | Unblock Partner |
| `POST` | `/api/changes/{change_id}/partners/{partner_id}/withdraw` | Withdraw Partner |

## auth

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/auth/audit` | Auth Audit Log |
| `GET` | `/api/auth/captcha` | Get Captcha |
| `POST` | `/api/auth/change-password` | Change Password |
| `POST` | `/api/auth/login` | Login |
| `POST` | `/api/auth/logout` | Logout |
| `GET` | `/api/auth/me` | Get Me |
| `POST` | `/api/auth/mfa/activate` | Mfa Activate |
| `POST` | `/api/auth/mfa/disable` | Mfa Disable |
| `POST` | `/api/auth/mfa/setup` | Mfa Setup |
| `POST` | `/api/auth/mfa/verify` | Mfa Verify |
| `POST` | `/api/auth/switch-role` | Switch Role |

## cert-a2a

| Method | Path | Summary |
|---|---|---|
| `POST` | `/api/admin/cert-a2a/send` | Cert Send |
| `POST` | `/api/admin/cert-a2a/simulate-inbound` | Cert Simulate Inbound |
| `GET` | `/api/admin/cert-a2a/templates` | Cert Templates |

## cert-push

| Method | Path | Summary |
|---|---|---|
| `POST` | `/api/changes/{change_id}/cert-push` | Push To Cert |

## cert-simulator-sync

| Method | Path | Summary |
|---|---|---|
| `POST` | `/api/changes/{change_id}/cert-simulator/apply` | Apply Test Cases |
| `POST` | `/api/changes/{change_id}/cert-simulator/diff` | Diff Test Cases |
| `GET` | `/api/changes/{change_id}/cert-simulator/log` | Get Sync Log |

## cert-timeline

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/cert-status/timeline` | Cert Timeline |
| `GET` | `/api/cert-status/timeline/changes` | Cert Timeline Changes |

## change-requests

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/changes` | List Change Requests |
| `POST` | `/api/changes` | Create Change Request |
| `GET` | `/api/changes/blueprints/{doc_type}` | Get Blueprint |
| `DELETE` | `/api/changes/{change_id}` | Admin Delete Change |
| `GET` | `/api/changes/{change_id}` | Get Change Request |
| `PATCH` | `/api/changes/{change_id}` | Update Change Request |
| `GET` | `/api/changes/{change_id}/artifacts/ship-manifest` | Get Ship Manifest |
| `GET` | `/api/changes/{change_id}/artifacts/staleness` | Artifact Staleness |
| `GET` | `/api/changes/{change_id}/artifacts/summary` | Artifact Summary |
| `GET` | `/api/changes/{change_id}/artifacts/{doc_type}/download` | Download Artifact |
| `GET` | `/api/changes/{change_id}/artifacts/{doc_type}/download/pptx` | Download Artifact Pptx |
| `POST` | `/api/changes/{change_id}/artifacts/{doc_type}/revert-to-generated` | Revert To Generated |
| `DELETE` | `/api/changes/{change_id}/artifacts/{doc_type}/ship-override` | Clear Ship Override |
| `PUT` | `/api/changes/{change_id}/artifacts/{doc_type}/ship-override` | Upload Ship Override |
| `POST` | `/api/changes/{change_id}/artifacts/{doc_type}/upload` | Upload Artifact |
| `GET` | `/api/changes/{change_id}/context` | Get Context |
| `POST` | `/api/changes/{change_id}/context/refresh` | Refresh Context |
| `GET` | `/api/changes/{change_id}/reconciliation` | Get Reconciliation |
| `POST` | `/api/changes/{change_id}/reconciliation/acknowledge-overturns` | Acknowledge Overturns |
| `POST` | `/api/changes/{change_id}/reconciliation/decide` | Decide Reconciliation |
| `POST` | `/api/changes/{change_id}/reconciliation/dismiss` | Dismiss Reconciliation |
| `POST` | `/api/changes/{change_id}/reconciliation/grounding-answer` | Answer Grounding |
| `POST` | `/api/changes/{change_id}/source-document` | Upload Source Document |
| `GET` | `/api/changes/{change_id}/validation/{doc_type}` | Validate Artifact |

## clarifications

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/changes/{change_id}/clarifications` | Get Clarifications |
| `POST` | `/api/changes/{change_id}/clarifications/answer` | Submit Answers |
| `POST` | `/api/changes/{change_id}/clarifications/rerun` | Rerun Clarifications |
| `POST` | `/api/changes/{change_id}/clarifications/skip` | Skip Clarifications |
| `POST` | `/api/changes/{change_id}/clarify` | Trigger Clarify |

## code-indexing

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/admin/code-indexing/repos` | List Repos |
| `POST` | `/api/admin/code-indexing/repos` | Add Repo |
| `DELETE` | `/api/admin/code-indexing/repos/{repo_id}` | Remove Repo |
| `PATCH` | `/api/admin/code-indexing/repos/{repo_id}` | Update Repo |
| `GET` | `/api/admin/code-indexing/repos/{repo_id}/context` | Get Repo Context |
| `POST` | `/api/admin/code-indexing/repos/{repo_id}/index` | Index Repo |
| `POST` | `/api/admin/code-indexing/repos/{repo_id}/index-polyglot` | Index Repo Polyglot |
| `POST` | `/api/admin/code-indexing/repos/{repo_id}/index-polyglot-incremental` | Index Repo Polyglot Incremental |
| `GET` | `/api/admin/code-indexing/status` | Indexing Status |

## emergency-issues

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/changes/{change_id}/emergency-issues` | List Emergency Issues |
| `POST` | `/api/emergency-issues/{issue_id}/resolve` | Resolve Emergency Issue |

## escalations

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/escalations` | List Escalations |
| `POST` | `/api/escalations/{ticket_id}/respond` | Respond Escalation |

## eval

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/admin/eval/compare` | Eval Compare |
| `GET` | `/api/admin/eval/metrics` | Eval Metrics |
| `GET` | `/api/admin/eval/policies` | List Eval Policies |
| `PUT` | `/api/admin/eval/policies` | Update Eval Policies |
| `GET` | `/api/admin/eval/policy-audit` | List Eval Policy Audit |
| `GET` | `/api/admin/eval/verdicts` | List All Eval Verdicts |
| `GET` | `/api/changes/{change_id}/eval/impact` | Change Impact |
| `GET` | `/api/changes/{change_id}/eval/latest` | Latest Verdict |
| `POST` | `/api/changes/{change_id}/eval/override` | Override Eval Verdict |
| `POST` | `/api/changes/{change_id}/eval/rerun` | Rerun Eval |
| `GET` | `/api/changes/{change_id}/eval/verdicts` | List Verdicts |

## governance

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/admin/governance-skills` | List Skills |
| `POST` | `/api/admin/governance-skills/{stype}/bundle` | Upload Skill Bundle |
| `POST` | `/api/admin/governance-skills/{stype}/slots/{name}/enabled` | Set Slot Enabled |
| `POST` | `/api/admin/governance-skills/{stype}/upload` | Upload Skill |
| `GET` | `/api/admin/governance-skills/{stype}/versions` | List Skill Versions |
| `GET` | `/api/admin/governance-skills/{stype}/versions/{version}` | Get Skill Version |
| `POST` | `/api/admin/governance-skills/{stype}/versions/{version}/smoke` | Smoke Skill Bundle |
| `POST` | `/api/changes/{change_id}/governance/reset` | Reset Governance Reviews |
| `POST` | `/api/changes/{change_id}/governance/start` | Start Governance |
| `GET` | `/api/changes/{change_id}/governance/status` | Get Governance Status |

## jobs

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/admin/jobs/stats` | Admin Jobs Stats |
| `GET` | `/api/changes/{change_id}/jobs/active` | List Change Jobs |
| `GET` | `/api/jobs/active` | List My Jobs |
| `GET` | `/api/jobs/{job_id}` | Get Job |
| `POST` | `/api/jobs/{job_id}/cancel` | Cancel Job |
| `GET` | `/api/jobs/{job_id}/chunks` | Get Job Chunks |

## kg-admin

| Method | Path | Summary |
|---|---|---|
| `POST` | `/api/admin/kg/impact` | Kg Impact |
| `POST` | `/api/admin/kg/ingest` | Kg Ingest |
| `POST` | `/api/admin/kg/initialise` | Kg Initialise |
| `GET` | `/api/admin/kg/status` | Kg Status |

## kit-publications

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/changes/{change_id}/kit-publications` | List Publications |
| `GET` | `/api/changes/{change_id}/kit-publications/{negotiation_version}` | Get Publication |

## logs

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/logs` | Get Logs |
| `GET` | `/api/logs/stream` | Stream Logs |

## negotiation-mgmt

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/changes/{change_id}/brd-requirements` | List Brd Requirements |
| `POST` | `/api/changes/{change_id}/brd-requirements` | Create Brd Requirement |
| `POST` | `/api/changes/{change_id}/brd-requirements/generate` | Generate Brd Requirements |
| `DELETE` | `/api/changes/{change_id}/brd-requirements/{req_id}` | Delete Brd Requirement |
| `PUT` | `/api/changes/{change_id}/brd-requirements/{req_id}` | Update Brd Requirement |
| `POST` | `/api/changes/{change_id}/negotiate/advance-round` | Advance Round |
| `POST` | `/api/changes/{change_id}/negotiate/finalize` | Finalize |
| `POST` | `/api/changes/{change_id}/negotiate/new-version` | New Version |
| `POST` | `/api/changes/{change_id}/negotiate/new-version-and-ship` | New Version And Ship |
| `GET` | `/api/changes/{change_id}/negotiate/revision-plan` | Get Revision Plan |
| `PUT` | `/api/changes/{change_id}/negotiate/revision-plan` | Update Revision Plan |
| `POST` | `/api/changes/{change_id}/negotiate/revision-plan/draft` | Draft Revision Plan |
| `POST` | `/api/changes/{change_id}/negotiate/revision-plan/generate` | Generate Revision Kit |
| `GET` | `/api/changes/{change_id}/negotiate/revision-plan/summary.docx` | Download Revision Summary |
| `GET` | `/api/changes/{change_id}/negotiate/round-close-summary` | Round Close Summary |
| `GET` | `/api/changes/{change_id}/negotiate/status` | Negotiation Status |
| `GET` | `/api/changes/{change_id}/negotiation/clusters` | List Clusters |
| `POST` | `/api/changes/{change_id}/negotiation/clusters/{cluster_id}/decide` | Decide Cluster |
| `POST` | `/api/changes/{change_id}/negotiation/clusters/{cluster_id}/refresh-ai` | Refresh Cluster Ai |
| `GET` | `/api/changes/{change_id}/negotiation/rounds` | List Round States |
| `POST` | `/api/changes/{change_id}/negotiation/rounds/sweep` | Sweep Silent Acceptances |
| `POST` | `/api/changes/{change_id}/negotiation/rounds/{partner_id}/close` | Close Round |

## notifications

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/notifications` | List Notifications |
| `POST` | `/api/notifications/read-all` | Mark All Read |
| `POST` | `/api/notifications/{notification_id}/read` | Mark Read |

## partners

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/admin/partners` | List Partners |
| `POST` | `/api/admin/partners` | Create Partner |
| `GET` | `/api/admin/partners/stats` | Partner Stats |
| `DELETE` | `/api/admin/partners/{partner_id}` | Deactivate Partner |
| `PUT` | `/api/admin/partners/{partner_id}` | Update Partner |
| `POST` | `/api/admin/partners/{partner_id}/activate` | Activate Partner |
| `PATCH` | `/api/admin/partners/{partner_id}/allowed-cidrs` | Update Allowed Cidrs |
| `GET` | `/api/admin/partners/{partner_id}/ca-cert` | Get Partner Ca Cert |
| `POST` | `/api/admin/partners/{partner_id}/ca-cert` | Upload Partner Ca Cert |
| `PATCH` | `/api/admin/partners/{partner_id}/cert-fingerprint` | Update Cert Fingerprint |
| `PATCH` | `/api/admin/partners/{partner_id}/max-inline-attachment` | Update Partner Max Inline Attachment |
| `PATCH` | `/api/admin/partners/{partner_id}/protocol` | Update Partner Protocol |
| `PATCH` | `/api/admin/partners/{partner_id}/rate-limit` | Update Rate Limit |
| `POST` | `/api/admin/partners/{partner_id}/revoke-sessions` | Revoke Partner Sessions |
| `POST` | `/api/admin/partners/{partner_id}/rotate-hmac-secret` | Rotate Hmac Signing Secret |
| `POST` | `/api/admin/partners/{partner_id}/rotate-jwt-secret` | Rotate Jwt Signing Secret |
| `POST` | `/api/admin/partners/{partner_id}/rotate-key` | Rotate Api Key |
| `PATCH` | `/api/admin/partners/{partner_id}/ssl-verify` | Update Partner Ssl Verify |
| `POST` | `/api/admin/partners/{partner_id}/test` | Test Partner Connectivity |
| `PATCH` | `/api/admin/partners/{partner_id}/tls-tier` | Update Tls Tier |

## phase-b

| Method | Path | Summary |
|---|---|---|
| `DELETE` | `/api/changes/{change_id}/phase-b` | Reset Phase B |
| `GET` | `/api/changes/{change_id}/phase-b` | Get Phase B |
| `POST` | `/api/changes/{change_id}/phase-b/advance-step` | Advance Phase B Step |
| `POST` | `/api/changes/{change_id}/phase-b/agentic-complete` | Agentic Complete |
| `GET` | `/api/changes/{change_id}/phase-b/build/latest` | Get Latest Build |
| `POST` | `/api/changes/{change_id}/phase-b/build/trigger` | Trigger Build |
| `POST` | `/api/changes/{change_id}/phase-b/code-review` | Trigger Code Review |
| `GET` | `/api/changes/{change_id}/phase-b/code-review/latest` | Get Latest Code Review |
| `POST` | `/api/changes/{change_id}/phase-b/code-review/loop-back` | Code Review Loop Back |
| `GET` | `/api/changes/{change_id}/phase-b/code/iterations` | List Code Iterations |
| `GET` | `/api/changes/{change_id}/phase-b/code/iterations/{iteration_number}` | Get Code Iteration |
| `POST` | `/api/changes/{change_id}/phase-b/code/iterations/{iteration_number}/approve` | Approve Code Iteration |
| `GET` | `/api/changes/{change_id}/phase-b/git/latest` | Get Latest Git Event |
| `POST` | `/api/changes/{change_id}/phase-b/git/push` | Trigger Git Push |
| `GET` | `/api/changes/{change_id}/phase-b/git/repos` | List Git Repos |
| `POST` | `/api/changes/{change_id}/phase-b/ingest-codebase` | Ingest Codebase Endpoint |
| `POST` | `/api/changes/{change_id}/phase-b/is-review` | Trigger Is Review |
| `GET` | `/api/changes/{change_id}/phase-b/is-review/latest` | Get Latest Is Review |
| `POST` | `/api/changes/{change_id}/phase-b/is-review/loop-back` | Is Review Loop Back |
| `POST` | `/api/changes/{change_id}/phase-b/start` | Start Phase B |
| `GET` | `/api/changes/{change_id}/phase-b/test-cases` | List Test Cases |
| `POST` | `/api/changes/{change_id}/phase-b/test-exec/trigger` | Trigger Test Exec |
| `POST` | `/api/changes/{change_id}/phase-b/test-gen/trigger` | Trigger Test Gen |
| `GET` | `/api/changes/{change_id}/phase-b/test-runs/latest` | Get Latest Test Run |

## phase-c

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/certification/agent-messages` | Certification Agent Messages |
| `GET` | `/api/certification/dashboard` | Certification Dashboard |
| `GET` | `/api/changes/{change_id}/cert-summary` | Change Cert Summary |
| `GET` | `/api/changes/{change_id}/partners` | List Assigned Partners |
| `POST` | `/api/changes/{change_id}/partners` | Assign Partners |
| `DELETE` | `/api/changes/{change_id}/partners/{partner_id}` | Remove Partner Assignment |
| `GET` | `/api/changes/{change_id}/partners/{partner_id}/blockers` | List Blockers |
| `POST` | `/api/changes/{change_id}/partners/{partner_id}/blockers/{blocker_id}/resolve` | Resolve Blocker |
| `POST` | `/api/changes/{change_id}/partners/{partner_id}/blockers/{blocker_id}/status` | Update Blocker Status |
| `GET` | `/api/changes/{change_id}/partners/{partner_id}/cert-waivers` | List Cert Waivers |
| `POST` | `/api/changes/{change_id}/partners/{partner_id}/cert-waivers/{waiver_id}/decide` | Decide Cert Waiver |
| `POST` | `/api/changes/{change_id}/partners/{partner_id}/cert/demo-reset` | Demo Reset Certification |
| `POST` | `/api/changes/{change_id}/partners/{partner_id}/cert/demo-run` | Demo Run Certification |
| `GET` | `/api/changes/{change_id}/partners/{partner_id}/cert/runs` | List Cert Runs |
| `GET` | `/api/changes/{change_id}/partners/{partner_id}/cert/runs/{run_id}` | Get Cert Run Detail |
| `POST` | `/api/changes/{change_id}/partners/{partner_id}/cert/start` | Start Certification |
| `POST` | `/api/changes/{change_id}/partners/{partner_id}/cert/triage` | Trigger Triage |
| `POST` | `/api/changes/{change_id}/partners/{partner_id}/cert/triage/{triage_id}/approve` | Approve Triage |
| `GET` | `/api/changes/{change_id}/partners/{partner_id}/cert/network-txns` | Cert Upi Txns |
| `GET` | `/api/changes/{change_id}/partners/{partner_id}/counter-proposals` | List Counter Proposals |
| `POST` | `/api/changes/{change_id}/partners/{partner_id}/counter-proposals/{cp_id}/accept` | Accept Counter Proposal |
| `POST` | `/api/changes/{change_id}/partners/{partner_id}/counter-proposals/{cp_id}/counter` | Counter Back Proposal |
| `POST` | `/api/changes/{change_id}/partners/{partner_id}/counter-proposals/{cp_id}/reject` | Reject Counter Proposal |
| `GET` | `/api/changes/{change_id}/partners/{partner_id}/negotiation` | Get Negotiation Thread |
| `POST` | `/api/changes/{change_id}/partners/{partner_id}/negotiation/query` | Receive Partner Query |
| `POST` | `/api/changes/{change_id}/partners/{partner_id}/negotiation/respond` | Approve And Respond |
| `GET` | `/api/changes/{change_id}/partners/{partner_id}/progress` | Get Partner Progress |
| `POST` | `/api/changes/{change_id}/phase-c/communicate` | Communicate Change |
| `GET` | `/api/changes/{change_id}/phase-c/messages` | List A2A Messages |
| `GET` | `/api/changes/{change_id}/phase-c/progress-grid` | Get Progress Grid |
| `POST` | `/api/changes/{change_id}/phase-c/ship-kit` | Ship Kit |
| `GET` | `/api/changes/{change_id}/phase-c/status` | Get Phase C Status |

## product-kit-video

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/changes/{change_id}/product-kit/{doc_type}/versions` | List Video Script Versions |
| `GET` | `/api/changes/{change_id}/product-kit/{doc_type}/versions/{version}` | Get Video Script Version |
| `GET` | `/api/changes/{change_id}/product-kit/{doc_type}/video` | Get Product Kit Video |
| `POST` | `/api/changes/{change_id}/product-kit/{doc_type}/video` | Upload Product Kit Video |
| `POST` | `/api/changes/{change_id}/product-kit/{doc_type}/video/generate` | Generate Product Kit Video |
| `GET` | `/api/changes/{change_id}/product-kit/{doc_type}/video/options` | Get Video Gen Options |

## rag

| Method | Path | Summary |
|---|---|---|
| `POST` | `/api/rag/bm25/rebuild` | Rebuild Bm25 |
| `GET` | `/api/rag/bm25/status` | Bm25 Status |
| `GET` | `/api/rag/categories` | List Categories |
| `DELETE` | `/api/rag/chunks` | Clear Chunks |
| `DELETE` | `/api/rag/file/{file_name}` | Delete File Chunks |
| `POST` | `/api/rag/ingest` | Trigger Ingest |
| `POST` | `/api/rag/reingest` | Reingest Now |
| `POST` | `/api/rag/search` | Test Search |
| `GET` | `/api/rag/status` | Knowledge Base Status |
| `GET` | `/api/rag/task/{task_id}` | Get Task Status |
| `POST` | `/api/rag/taxonomy/classify` | Taxonomy Classify |
| `POST` | `/api/rag/upload` | Upload Document |

## resolver

| Method | Path | Summary |
|---|---|---|
| `POST` | `/api/changes/{change_id}/resolve/{a2a_message_id}` | Run Resolver |
| `GET` | `/api/changes/{change_id}/resolver-recommendation` | Get Latest Recommendation |
| `GET` | `/api/changes/{change_id}/resolver-recommendations` | List Recommendations |

## untagged

| Method | Path | Summary |
|---|---|---|
| `GET` | `/.well-known/agent.json` | Agent Card |
| `GET` | `/api/agentic/health` | Agentic Health |
| `GET` | `/api/agentic/preflight` | Agentic Preflight |
| `POST` | `/api/agentic/quick-start` | Quick Start |
| `GET` | `/api/agentic/runs` | List Runs |
| `GET` | `/api/agentic/runs/{run_id}` | Get Run |
| `POST` | `/api/agentic/runs/{run_id}/approve` | Approve Run |
| `POST` | `/api/agentic/runs/{run_id}/approve-xsd` | Approve Xsd |
| `POST` | `/api/agentic/runs/{run_id}/cancel` | Cancel Run |
| `POST` | `/api/agentic/runs/{run_id}/challenge-plan` | Challenge Plan |
| `POST` | `/api/agentic/runs/{run_id}/decide-approach` | Decide Approach |
| `POST` | `/api/agentic/runs/{run_id}/decide-clarifications` | Decide Clarifications |
| `POST` | `/api/agentic/runs/{run_id}/decide-code-decision` | Decide Code Decision |
| `POST` | `/api/agentic/runs/{run_id}/decide-plan` | Decide Plan |
| `POST` | `/api/agentic/runs/{run_id}/decide-revision` | Decide Revision |
| `POST` | `/api/agentic/runs/{run_id}/decide-verify` | Decide Verify |
| `GET` | `/api/agentic/runs/{run_id}/diff` | Get Diff |
| `GET` | `/api/agentic/runs/{run_id}/events` | Get Events |
| `POST` | `/api/agentic/runs/{run_id}/push` | Push Run Now |
| `POST` | `/api/agentic/runs/{run_id}/request-xsd-changes` | Request Xsd Changes |
| `POST` | `/api/agentic/runs/{run_id}/resume` | Resume Run |
| `POST` | `/api/agentic/runs/{run_id}/reverify` | Reverify Run Endpoint |
| `POST` | `/api/agentic/runs/{run_id}/stuck-decide` | Stuck Decide |
| `POST` | `/api/agentic/runs/{run_id}/stuck-help` | Stuck Help |
| `GET` | `/api/agentic/runs/{run_id}/usage` | Get Run Usage |
| `GET` | `/api/agentic/runs/{run_id}/walkthrough` | Get Walkthrough |
| `POST` | `/api/agentic/runs/{run_id}/walkthrough` | Generate Run Walkthrough |
| `GET` | `/api/agentic/runs/{run_id}/walkthrough.csv` | Walkthrough Csv |
| `GET` | `/api/agentic/runs/{run_id}/workspace-zip` | Download Workspace Zip |
| `GET` | `/api/agentic/runs/{run_id}/xsd-files` | Get Xsd Files |
| `GET` | `/api/agentic/usage/changes` | Usage By Change |
| `GET` | `/api/agentic/usage/changes/{change_id}` | Usage Change Detail |
| `GET` | `/api/agentic/usage/other` | Usage Other |
| `POST` | `/api/changes/{change_id}/agentic/rerun-code` | Rerun Code |
| `GET` | `/api/changes/{change_id}/agentic/runs` | List Change Runs |
| `POST` | `/api/changes/{change_id}/agentic/start` | Start Agentic |
| `GET` | `/api/changes/{change_id}/agentic/transcripts-zip` | Download Transcripts Zip |
| `GET` | `/api/changes/{change_id}/analysis` | Get Change Analysis |
| `POST` | `/api/changes/{change_id}/analysis/ensure` | Ensure Analysis |
| `GET` | `/api/changes/{change_id}/analysis/repos` | Analysis Repo Options |
| `GET` | `/api/changes/{change_id}/analysis/versions` | List Change Analysis Versions |
| `GET` | `/api/changes/{change_id}/analysis/versions/{version}` | Get Change Analysis Version |
| `GET` | `/api/health` | Health Check |

## users

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/users` | List Users |
| `POST` | `/api/users` | Create User |
| `DELETE` | `/api/users/{user_id}` | Deactivate User |
| `GET` | `/api/users/{user_id}` | Get User |
| `PATCH` | `/api/users/{user_id}` | Update User |
| `POST` | `/api/users/{user_id}/mfa/reset` | Reset User Mfa |

