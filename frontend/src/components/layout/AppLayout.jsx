// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { Outlet } from 'react-router-dom'
import Sidebar from './Sidebar'
import { GateModalProvider } from '../../context/GateModalContext'

export default function AppLayout() {
  return (
    <div style={{ display: 'flex', height: '100vh', background: 'var(--bg-base)' }}>
      <Sidebar />
      {/* `overflowX: hidden` stops any inner widget (KPI tile row, wide
          tables, long PhaseChips strips) from triggering a body-level
          horizontal scrollbar on narrow viewports. Tier 1 of the
          dashboard-overflow fix — pairs with `min-width: 0` resets
          inside the grid wrappers so flex/grid children can shrink. */}
      <main style={{ flex: 1, overflowY: 'auto', overflowX: 'hidden', minHeight: 0, minWidth: 0 }}>
        <GateModalProvider>
          <Outlet />
        </GateModalProvider>
      </main>
    </div>
  )
}
