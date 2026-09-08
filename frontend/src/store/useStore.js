// Copyright 2026 The ATOM Authors
// SPDX-License-Identifier: MIT

import { create } from 'zustand'
import { persist } from 'zustand/middleware'

// The localStorage slot this store persists into, and the slot it used to use.
//
// A localStorage key is a VALUE, not a label: it names a slot in every existing
// user's browser. A bare rename does not migrate that slot, it orphans it — the
// store comes up empty and every signed-in operator is silently logged out on
// the deploy that lands the rename. So the rename ships with the migration.
const STORAGE_KEY = 'atom-auth'
const LEGACY_STORAGE_KEY = 'npci-auth'

/**
 * Move a pre-rename slot to the current key, once, before the store is created.
 *
 * This cannot be done with persist's `migrate` hook: that runs on the value
 * found UNDER THE CURRENT KEY, and after a key rename there is nothing there to
 * migrate. The key move has to happen first; `migrate` below then handles the
 * version stamp inside the blob.
 *
 * Idempotent, and never overwrites a newer slot: if the new key already holds
 * something, the legacy one is stale and is simply dropped.
 */
function adoptLegacyAuthSlot() {
  try {
    if (typeof localStorage === 'undefined') return
    const legacy = localStorage.getItem(LEGACY_STORAGE_KEY)
    if (legacy === null) return
    if (localStorage.getItem(STORAGE_KEY) === null) {
      localStorage.setItem(STORAGE_KEY, legacy)
    }
    localStorage.removeItem(LEGACY_STORAGE_KEY)
  } catch {
    // Storage can throw (Safari private mode, quota, a disabled-storage
    // policy). The worst case is that one operator logs in again, which must
    // not be allowed to take the whole SPA down on import.
  }
}

adoptLegacyAuthSlot()

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
    {
      name: STORAGE_KEY,
      partialize: (s) => ({ user: s.user }),
      version: 1,
      // The persisted SHAPE is unchanged across v0 -> v1; only the key moved.
      // The hook still has to exist: without a `migrate`, zustand discards any
      // blob whose stamped version is lower than `version`, which would undo
      // the whole point of adoptLegacyAuthSlot above and log everyone out
      // anyway. Returning the state untouched is the correct v0 -> v1 step.
      migrate: (persistedState) => persistedState,
    }
  )
)
