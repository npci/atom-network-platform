// Copyright 2026 National Payments Corporation of India
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
