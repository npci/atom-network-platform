// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useState } from 'react';
import { Copy, Check } from 'lucide-react';

/**
 * ChangeIdChip — a small, always-visible pill showing a change's id.
 *
 * Why it exists: every transcript / log a change produces is now filed on disk under a
 * folder named after this exact id (logs/transcripts/<change_id>/...). Surfacing the id as
 * a click-to-copy chip makes it trivial to jump from a change in the UI to its log folder,
 * especially when several changes are running at once.
 *
 * Styled to match the app's inline-pill conventions (999px radius, .id-mono for the id,
 * CSS theme vars). Click copies the FULL id and briefly confirms.
 */
export default function ChangeIdChip({ id, label = 'CR', short = 8, style = {} }) {
  const [copied, setCopied] = useState(false);
  if (!id) return null;

  const shortId = short && id.length > short ? id.slice(0, short) : id;

  const copy = async (e) => {
    e.stopPropagation();
    e.preventDefault();
    try {
      await navigator.clipboard.writeText(id);
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    } catch {
      /* clipboard blocked (non-secure context) — ignore silently */
    }
  };

  return (
    <button
      type="button"
      onClick={copy}
      title={`${id}\nClick to copy • log folder: logs/transcripts/${id}/`}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: '5px',
        padding: '2px 8px',
        borderRadius: '999px',
        fontSize: '11px',
        fontWeight: 600,
        lineHeight: 1.4,
        cursor: 'pointer',
        color: copied ? 'var(--success, #16a34a)' : 'var(--text-muted)',
        background: 'var(--bg-subtle, rgba(127,127,127,0.10))',
        border: '1px solid var(--border-subtle, rgba(127,127,127,0.25))',
        transition: 'color 120ms ease',
        ...style,
      }}
    >
      <span style={{ opacity: 0.75, letterSpacing: '0.02em' }}>{label}</span>
      <span className="id-mono" style={{ fontWeight: 600 }}>{shortId}</span>
      {copied
        ? <Check size={11} aria-hidden />
        : <Copy size={11} aria-hidden style={{ opacity: 0.6 }} />}
      {copied && <span style={{ fontSize: '10px', fontWeight: 600 }}>Copied</span>}
    </button>
  );
}
