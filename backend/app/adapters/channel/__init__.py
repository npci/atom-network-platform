# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Partner channels — how a change reaches the people who must implement it.

Two ship today and they are deliberately very different, because an abstraction
validated against one transport is just that transport with extra steps:

  a2a         bidirectional, authenticated, machine-to-machine (the network)
  publish     one-way file drop, no responses at all (the OCPP shape)
"""
