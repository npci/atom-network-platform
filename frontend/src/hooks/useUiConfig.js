// Copyright 2026 The ATOM Authors
// SPDX-License-Identifier: MIT

import { useQuery } from '@tanstack/react-query'
import { uiConfigApi } from '../services/api'

// Fetch the platform's UI config once and cache it forever (until the SPA
// reloads). The endpoint is unauthenticated and returns:
//   { dev_mode: bool, app_env: str }
//
// Any component that needs to render dev-only widgets (e.g. the per-step
// Skip button) calls this hook and reads `data?.dev_mode`.
//
// staleTime: Infinity — the value is process-bound on the backend and
// changing it requires a deploy, so there's no point re-fetching.
export function useUiConfig() {
  return useQuery({
    queryKey: ['ui-config'],
    queryFn:  uiConfigApi.get,
    staleTime: Infinity,
    retry: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  })
}

export function useIsDevMode() {
  const { data } = useUiConfig()
  return Boolean(data?.dev_mode)
}

// The active domain pack's declared repo topology (see utils/repoTopology.js).
// [] is the meaningful default, NOT a loading artifact: it means the domain
// declares no topology, and the selection screens fall back to single-repo.
export function useRepoRoles() {
  const { data } = useUiConfig()
  return data?.repo_roles || []
}

// The active domain pack's certification role KEYS ("LENDING_LIBRARY", ...).
// Used where the UI needs an example of what a role key looks like —
// placeholders and hints. [] means the domain declares none, and callers must
// fall back to a neutral string, not to another domain's key.
export function useCertRoleKeys() {
  const { data } = useUiConfig()
  return (data?.cert_roles || []).map(r => r?.key ?? r?.[0]).filter(Boolean)
}

// The two initiator tokens for the active domain, as
// `{ authority: {token, label}, partner: {token, label} }`.
//
// `token` is a WIRE value: it is what the cert-push summary uses as the object
// KEY of each side's counts, and what `initiated_by` holds on a stored row.
// The SPA must therefore compare against it rather than a hardcoded pair —
// components used to test the value against one ecosystem's literal, which
// renders a silently-zero breakdown on any deployment whose pack names its
// sides differently.
//
// Empty tokens are the meaningful default (a domain with no certification
// body); callers should treat an unmatched value as "unknown", not as an error.
export function useCertInitiators() {
  const { data } = useUiConfig()
  const ci = data?.cert_initiators || {}
  return {
    authorityToken: ci.authority?.token || '',
    partnerToken:   ci.partner?.token   || '',
    authorityLabel: ci.authority?.label || 'Authority',
    partnerLabel:   ci.partner?.label   || 'Partner',
    // How this domain phrases "acting as <role>".
    // Empty when the pack declares none — callers must omit the prefix rather
    // than substitute one, for the same reason the tokens above are not
    // hardcoded.
    roleAsLabel:    ci.role_as_label    || '',
    // [{prefix, sheet}] — test-id prefix to workbook role sheet, from the pack's
    // `cert_vocabulary.role_prefixes` plus its `cert_extra_sheet_prefixes`.
    // Empty is legitimate: ProductKit then groups everything generically rather
    // than under another domain's tab names.
    certSheetPrefixes: data?.cert_sheet_prefixes || [],
  }
}
