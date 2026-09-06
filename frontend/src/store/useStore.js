// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { create } from 'zustand'
import { persist } from 'zustand/middleware'

// Auth store.
//
// THE SESSION TOKEN IS NOT HELD HERE, AND NOT IN localStorage.
//
// It lives in an httpOnly cookie set by the backend on login, which JavaScript
// cannot read by design. That closes the "sensitive data in web storage"
// weakness: previously any script running in this origin could read the JWT
// out of localStorage, and because operator tokens are 8h and slide forward on
// every authenticated request, a single XSS yielded a long-lived, privileged,
// self-renewing credential.
//
// What remains here is `user` — the non-sensitive identity payload. It is
// persisted so a page reload knows it is logged in without waiting on a
// network round-trip. It is a UX marker ONLY, never proof of a session:
// authority rests entirely with the cookie the server validates on each
// request. A stale `user` with a dead cookie simply means the next call 401s
// and the interceptor in services/api.js clears it and routes to /login.
export const useAuthStore = create(
  persist(
    (set) => ({
      user: null,
      // Set after a successful login/MFA. The token argument is accepted and
      // ignored: the server delivers the real credential as a Set-Cookie on
      // the same response, so there is nothing for the client to store.
      setAuth: (user) => set({ user }),
      // Update the identity without implying a new session — used by role
      // switching, which keeps the same underlying cookie.
      setUser: (user) => set({ user }),
      // Clears only client-side state. The cookie itself is httpOnly and can
      // only be removed by the server, which POST /auth/logout does.
      clearAuth: () => set({ user: null }),
    }),
    { name: 'npci-auth', partialize: (s) => ({ user: s.user }) }
  )
)
