// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import js from '@eslint/js'
import globals from 'globals'
import react from 'eslint-plugin-react'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{js,jsx}'],
    extends: [
      js.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
      parserOptions: {
        ecmaVersion: 'latest',
        ecmaFeatures: { jsx: true },
        sourceType: 'module',
      },
    },
    plugins: { react },
    settings: { react: { version: 'detect' } },
    rules: {
      'no-unused-vars': ['error', { varsIgnorePattern: '^[A-Z_]' }],

      // ESLint's core rules CANNOT SEE JSX. espree emits a JSXIdentifier for
      // `<Icon />`, and core `no-undef` / `no-unused-vars` do not treat that as
      // a reference. Without these two rules the linter is wrong in BOTH
      // directions on the same code, which is not a theoretical concern — it
      // took the app down:
      //
      //   * `no-unused-vars` reports a correct `function NavItem({ icon: Icon })`
      //     as "'Icon' is defined but never used", because the only use is in
      //     JSX. The 133 -> 0 lint cleanup believed that report and deleted the
      //     binding in Sidebar.jsx and SyncDiffModal.jsx (85229da, d7a8897).
      //   * `no-undef` is then SILENT on the resulting `<Icon />`, and Vite
      //     builds it happily. The failure surfaced only in a browser, as a
      //     blank screen after login: "ReferenceError: Icon is not defined".
      //
      // jsx-uses-vars fixes the first (JSX counts as a use); jsx-no-undef fixes
      // the second (an unbound JSX component is an error). Both are needed —
      // either alone leaves one direction broken.
      'react/jsx-uses-vars': 'error',
      'react/jsx-no-undef': 'error',

      // React Compiler rules, deliberately WARN rather than error.
      //
      // These four are the residue of a 133 -> 12 lint cleanup. Everything
      // that could be fixed without changing observable behaviour was fixed,
      // including all 43 rules-of-hooks violations, which were real bugs.
      // What is left is 12 sites in ONE category: pre-existing patterns that
      // the compiler ruleset flags and whose repair changes runtime behaviour.
      //
      // Seven are "sync state from props/query in an effect" — resetting a
      // composer when the thread changes, seeding a selection from fetched
      // defaults, loading an editor once. React's recommended repairs (derive
      // during render, or remount via `key`) are behavioural changes.
      //
      // They stay VISIBLE as warnings rather than being switched off: `npm run
      // lint` still reports every one, and CI fails only on errors. Promote
      // each back to 'error' as its site is fixed and exercised in a browser.
      'react-hooks/set-state-in-effect': 'warn',
      'react-hooks/immutability': 'warn',
      'react-hooks/refs': 'warn',
      'react-hooks/static-components': 'warn',

      // Math.random() is a fast, seedable, NON-cryptographic PRNG: its output
      // is predictable from a handful of observed values. That is fine for
      // jitter or a demo label and catastrophic for anything an attacker must
      // not guess — tokens, session ids, nonces, password-reset links.
      //
      // The distinction is invisible at the call site, so the idiom spreads by
      // copy-paste from harmless code into code where it matters. This rule
      // removes that path: `crypto.getRandomValues` (or `crypto.randomUUID`)
      // is available in every browser this app supports and is always correct.
      // Recorded in the CBOM as CBOM-PRNG-JS-12 (CWE-338).
      'no-restricted-properties': ['error', {
        object: 'Math',
        property: 'random',
        message:
          'Math.random() is not cryptographically secure (CWE-338). Use ' +
          'crypto.getRandomValues() or crypto.randomUUID() for any id, token, ' +
          'or nonce. See frontend/src/lib/demoBuildLogs.js::randomHex.',
      }],
    },
  },
])
