// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { Navigate } from 'react-router-dom'
import { useAuthStore } from '../../store/useStore'

export default function ProtectedRoute({ children, requiredRole }) {
  const { user } = useAuthStore()

  // Gate on `user` alone, not on the token. The token is no longer persisted
  // (see store/useStore.js), so after a page reload it is null in memory even
  // though the session is perfectly valid — gating on it would bounce every
  // reload to /login. `user` is the persisted, non-sensitive marker of "we
  // believe we are logged in".
  //
  // This is a UX gate, never a security boundary: the server authorises every
  // request on its own, and the 401 interceptor in services/api.js clears the
  // session and redirects if the credential turns out to be dead.
  if (!user) return <Navigate to="/login" replace />

  if (requiredRole && user.role !== requiredRole && user.role !== 'admin') {
    return (
      <div className="flex items-center justify-center h-screen">
        <p className="text-red-500 text-lg">Access denied. Insufficient permissions.</p>
      </div>
    )
  }

  return children
}
