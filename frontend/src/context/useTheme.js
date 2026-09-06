// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

// Split out of ThemeContext.jsx so that file exports only components.
// react-refresh/only-export-components fires on a module that mixes component
// and non-component exports, because Fast Refresh cannot then hot-swap it.
import { createContext, useContext } from 'react'
// The context object lives here, not in ThemeContext.jsx, so that file
// exports only its provider component.
export const ThemeContext = createContext(null)

export function useTheme() {
  return useContext(ThemeContext)
}
