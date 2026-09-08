// Copyright 2026 The ATOM Authors
// SPDX-License-Identifier: MIT

// Human labels for Product-Kit doc types shipped to partners.
// Shared by ProductKitManager (dedicated overrides page) and PhaseC's
// "Communicate Change" modal so both surfaces stay in lockstep when the
// backend adds a new type. Unknown keys fall back to the raw doc_type.
//
// `circular` is authority-document vocabulary, so its label comes from the
// override catalogue rather than being spelled here — the two surfaces this
// file promises to keep in lockstep were reading two different sources for it.
import { t } from '../strings'

export const KIT_DOC_LABEL = {
  product_note:      'Product Note',
  product_deck:      'Product Deck',
  faq:               'FAQ',
  circular:          t('artifact.circular'),
  cert_test_cases:   'Certification Test Cases',
  promo_video:       'Promo Video',
  explainer_video:   'Explainer Video',
  manifest:          'Manifest',
  prototype_screens: 'Prototype Screens',
  tsd:               'Tech Spec (TSD)',
  xsd:               'XSD Schema',
}

export function kitDocLabel(docType) {
  return KIT_DOC_LABEL[docType] || docType
}

export function formatKB(n) {
  if (!n && n !== 0) return ''
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / (1024 * 1024)).toFixed(1)} MB`
}
