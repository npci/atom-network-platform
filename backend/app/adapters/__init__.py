# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Pluggable adapters: transports, renderers, VCS.

An adapter implements a core Protocol against one concrete technology. Core
depends on the Protocol; a domain pack chooses the adapter.
"""
