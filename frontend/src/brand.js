// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

// Branding: bundled default, operator-overridable.
//
// Two logo variants ship, chosen by the ACTIVE THEME rather than by preference:
//   *-logo-light.png  navy wordmark — for LIGHT surfaces
//   *-logo-dark.png   white wordmark — for DARK surfaces
// Both are transparent PNGs, so they sit on whatever colour is behind them.
// Picking the wrong one renders white-on-white and looks like a missing image.
//
// Imported (not referenced by path) so Vite fingerprints them into the build
// and a missing file becomes a BUILD error rather than a broken <img> nobody
// notices until it is in front of a customer.
//
// VITE_BRAND_LOGO_URL still overrides both, so a deployment can rebrand without
// touching source — see TRADEMARKS.md. The MIT License says nothing about
// trademarks, so it conveys none: the marks below are not licensed with the
// code, and TRADEMARKS.md is where that position is stated.
import logoDark from './assets/ATOM-logo-dark.png'
import logoLight from './assets/ATOM-logo-light.png'

export const BRAND_NAME = import.meta.env.VITE_BRAND_NAME || 'AtOM'

/**
 * Logo for the surface being rendered on.
 * @param {'dark'|'light'} theme  the theme of the SURFACE, not the user's saved
 *   preference — the login page renders light regardless of preference, so it
 *   passes 'light' explicitly.
 */
export function brandLogo(theme) {
  const override = import.meta.env.VITE_BRAND_LOGO_URL
  if (override) return override
  return theme === 'dark' ? logoDark : logoLight
}

// Kept for callers that only need a single asset (and for the override case).
export const BRAND_LOGO_URL = import.meta.env.VITE_BRAND_LOGO_URL || ''
