// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

// Runtime context-path inference.
// <base href="/${CONTEXT_PATH}/"> is injected by nginx sub_filter at serve time.
// In dev (Vite dev server, no sub_filter) falls back to '/a2a/'.

function _getBasePath() {
  const base = document.querySelector('base')?.getAttribute('href');
  if (!base) return '/a2a/';
  return base.endsWith('/') ? base : base + '/';
}

export const BASE_PATH = _getBasePath();
export const API_BASE = BASE_PATH + 'api';

export function wsUrl(path) {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${window.location.host}${BASE_PATH}${path}`;
}

export const ROUTER_BASENAME = BASE_PATH.replace(/\/$/, '') || '/';
export const LOGIN_PATH = ROUTER_BASENAME + '/login';
