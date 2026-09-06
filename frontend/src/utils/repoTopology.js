// Repo-selection rules, driven by the active domain pack's declared topology.
//
// WHY THIS EXISTS: the rule "exactly one core (framework/XSD) repo and one app
// (2.0) repo" used to be hardcoded in three places (AnalysisPanel, XSD, and the
// Code Indexing role dropdown). That is UPI's topology, not the platform's — a
// single-repo domain could never start a run, because the UI refused to submit a
// one-repo selection even though the BACKEND accepts it happily.
//
// These helpers are a direct port of `app/agents/repo_scope.validate_selection`.
// The server is authoritative and returns 400; this exists purely so the user
// sees the problem before clicking, and so the two never disagree. If you change
// a rule here, change it there in the same commit.
//
// THE DEFAULT MATTERS: an empty `repoRoles` is not an error or a missing
// fetch — it means the domain declares no topology. Both sides then fall back to
// "at least one repo selected" (the single-repo default). `needsWarning` tells
// the caller to surface that it is running unconfigured, so a domain that DOES
// need a topology (like UPI) isn't silently allowed to start a half-scoped run.

/** True when the pack declares no topology and we're on the permissive default. */
export function isUnconfigured(repoRoles) {
  return !Array.isArray(repoRoles) || repoRoles.length === 0
}

/**
 * Validate a repo selection against the declared topology.
 * @returns {{valid: boolean, reason: string, needsWarning: boolean}}
 */
export function validateSelection(repos, selectedIds, repoRoles) {
  const all = repos || []
  const sel = selectedIds || []
  const selected = all.filter(r => sel.includes(r.id))

  if (all.length === 0) {
    return {
      valid: false,
      reason: 'No indexed repositories — add + index them in Admin → Code Indexing.',
      needsWarning: false,
    }
  }

  // Mirrors the server's `if not repo_ids: raise` — the one rule that holds for
  // every domain, configured or not.
  if (selected.length === 0) {
    return { valid: false, reason: 'Select at least one repository.', needsWarning: isUnconfigured(repoRoles) }
  }

  // No declared topology → single-repo default: any non-empty selection is fine.
  if (isUnconfigured(repoRoles)) {
    return { valid: true, reason: '', needsWarning: true }
  }

  const declared = new Map(repoRoles.map(r => [r.key, r]))
  const expected = repoRoles.map(r => r.key).join(', ')

  // 1. Every selected repo must carry a declared role.
  const undeclared = selected.filter(r => !r.role || !declared.has(r.role))
  if (undeclared.length > 0) {
    const names = undeclared.map(r => `${r.label} (${r.role || 'no role'})`).join(', ')
    return {
      valid: false,
      reason: `${names} — role not declared by this domain. Expected: ${expected}. Set roles in Admin → Code Indexing.`,
      needsWarning: false,
    }
  }

  // 2. Required roles must be represented; 3. non-multiple roles only once.
  for (const role of repoRoles) {
    const matching = selected.filter(r => r.role === role.key)
    if (role.required && matching.length === 0) {
      return {
        valid: false,
        reason: `Missing a repo with role "${role.key}"${role.label ? ` (${role.label})` : ''}. Expected: ${expected}.`,
        needsWarning: false,
      }
    }
    if (!role.multiple && matching.length > 1) {
      return {
        valid: false,
        reason: `${matching.map(r => r.label).join(', ')} all have role "${role.key}", which allows only one.`,
        needsWarning: false,
      }
    }
  }

  return { valid: true, reason: '', needsWarning: false }
}

/**
 * The selection to pre-check on first paint: one repo per declared role, in
 * declaration order. Unconfigured → the sole repo when there is exactly one
 * (the single-repo default), otherwise nothing pre-selected rather than an
 * arbitrary guess the user then has to undo.
 */
export function defaultSelection(repos, repoRoles) {
  const all = repos || []
  if (isUnconfigured(repoRoles)) return all.length === 1 ? [all[0].id] : []
  return repoRoles
    .map(role => all.find(r => r.role === role.key)?.id)
    .filter(Boolean)
}

/** Options for the Admin role dropdown, from the pack. Falls back to the
 *  historical UPI vocabulary when the domain declares none, so the admin screen
 *  still offers something sensible on an unconfigured deployment. */
export function roleOptions(repoRoles) {
  if (isUnconfigured(repoRoles)) {
    return [
      { value: 'app', label: 'app' },
      { value: 'core', label: 'core (builds first)' },
      { value: 'legacy', label: 'legacy' },
    ]
  }
  return repoRoles.map(r => ({
    value: r.key,
    label: r.builds_first ? `${r.key} (builds first)` : r.key,
  }))
}

/** Prose describing the required selection, for the picker's instructions. */
export function topologyHint(repoRoles) {
  if (isUnconfigured(repoRoles)) return 'Select the repository to analyse.'
  const parts = repoRoles.map(r => {
    const name = r.label || r.key
    return r.multiple ? `one or more ${name} repos` : `one ${name} repo`
  })
  return `Select ${parts.join(' and ')}.`
}

export const UNCONFIGURED_TOPOLOGY_NOTICE =
  'No repository topology is configured for this domain — defaulting to single-repo ' +
  '(any one repository). Declare `repo_roles` in the domain pack to enforce a topology.'
