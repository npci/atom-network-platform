// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

// Split out of JobsContext.jsx so that file exports only components.
// react-refresh/only-export-components fires on a module that mixes component
// and non-component exports, because Fast Refresh cannot then hot-swap it.
import { createContext, useContext } from 'react'
// The context object lives here, not in JobsContext.jsx, so that file
// exports only its provider component.
export const JobsContext = createContext({
  activeJobs: {},
  activeJobsForChange: () => [],
  recordJob:   () => {},
  updateJob:   () => {},
  completeJob: () => {},
  cancelJob:   async () => {},
  refreshFromServer: async () => {},
  // True once the initial /jobs/active fetch has resolved (success OR error).
  // Consumers gate "no job running, safe to auto-start" decisions on this so
  // they don't race the network round-trip and accidentally start a duplicate.
  jobsLoaded: false,
})

export function useJobs() {
  return useContext(JobsContext)
}
