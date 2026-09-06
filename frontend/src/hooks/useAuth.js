// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useAuthStore } from '../store/useStore'
import { authApi } from '../services/api'
import { useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'

export function useAuth() {
  const { user, setAuth, setUser, clearAuth } = useAuthStore()
  const navigate = useNavigate()
  // Per-user data lives in the TanStack Query cache under global keys
  // like ['pending-approvals'] / ['changes'] / ['notifications'] (no
  // user_id in the key). Without clearing the cache on logout, user B
  // sees user A's cached data the moment they log in — until the next
  // refetch ticks. `clear()` also cancels in-flight queries so a
  // late-returning response from user A's session can't write into
  // user B's cache. The 401 interceptor in api.js already triggers a
  // full window.location reload which destroys the QueryClient natively,
  // so only the explicit-logout path needs this.
  const queryClient = useQueryClient()

  const login = async (username, password, extra = {}) => {
    // Clear any cache held from a prior session before swapping auth.
    // Belt-and-braces: covers the case where a user lands on /login
    // without first hitting logout (e.g. session expired, tab restored).
    // `extra` carries the CAPTCHA fields (captcha_id / captcha_answer) when
    // the server-side CAPTCHA gate is enabled.
    queryClient.clear()
    const res = await authApi.login({ username, password, ...extra })
    const d = res.data
    // Full session → finish. Otherwise it's an MFA challenge the Login page
    // completes (OTP verify, or forced enrolment) before calling completeAuth.
    //
    // `d.user` (not `d.access_token`) is the signal that a session was
    // established: the credential itself arrives as an httpOnly Set-Cookie on
    // this same response and is deliberately invisible here.
    if (d.user) {
      setAuth(d.user)
      navigate('/dashboard')
      return { done: true }
    }
    return {
      done: false,
      mfaRequired: !!d.mfa_required,
      mfaEnrollmentRequired: !!d.mfa_enrollment_required,
      mfaToken: d.mfa_token,
    }
  }

  // Finalize a session after the MFA step. The backend set the session
  // cookie on the verify/activate response; only the user is recorded here.
  const completeAuth = (u) => {
    setAuth(u)
    navigate('/dashboard')
  }

  const logout = async () => {
    await authApi.logout().catch(() => {})
    clearAuth()
    queryClient.clear()
    navigate('/login')
  }

  // Switch the active role. Updates the stored user (same session — the backend
  // reads the active role live from the DB) and invalidates cached queries,
  // since permissions and the pending-approvals list follow the active role.
  const switchRole = async (role) => {
    const res = await authApi.switchRole(role)
    setUser(res.data)
    queryClient.invalidateQueries()
    return res.data
  }

  // `isAuthenticated` follows `user`. The real credential is the httpOnly
  // cookie, which JavaScript cannot inspect — so this is a UX signal only, and
  // the server remains the sole authority on whether the session is valid.
  return { user, isAuthenticated: !!user, login, logout, completeAuth, switchRole }
}
