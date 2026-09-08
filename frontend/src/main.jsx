// Copyright 2026 The ATOM Authors
// SPDX-License-Identifier: MIT

import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.jsx'
import { applyBrandChrome } from './brand.js'

// Tab title + icon, before first paint — see brand.js for why this is done
// here rather than substituted into index.html at build time.
applyBrandChrome()

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
