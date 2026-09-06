# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The change's own case catalogue, from its published cert workbook.

`cert_pack_run` needs a case catalogue — which cases exist, which side
initiates each, what outcome each expects. Until now the only sources were an
operator-supplied `test_data["case_catalogue"]` or the labelled demo fallback,
so a readiness-triggered dispatch certified a real change against demo cases.

The REAL catalogue already exists: Phase A's excel testcase engine generates a
certification workbook per change and publishes it as the `cert_test_cases`
product-kit document — the artifact partners are told they will be certified
against. This module reads THAT (the durable, DB-stored, human-reviewable
form; the engine's sidecar files live in a scratch artifacts dir and do not
survive host restarts).

Domain knowledge comes from the active pack, never from code:

* which case-id prefixes belong to PARTNER roles — `cert_vocabulary.role_prefixes`
  (`LL_`/`BL_` for a library network, `PR_`/`PE_`/`RE_`/`BE_` for payments).
  A prefix not in the map is the authority's own (`NLLC_`, `MT_`).
* what an error code looks like — `error_code_pattern` (E0xx/Lxx vs U##/Z#).

The parse is deliberately conservative: a case whose block names no known API
is returned with `api=None` and the caller's scope derivation drops it as out
of scope (recorded, not guessed).
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Mapping

from app.core.domain.contract import cert_vocabulary_of, error_code_pattern_of
from app.core.domain.registry import get_active_pack

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

__all__ = ["case_catalogue_for_change"]

# "### LL_1 — Success" / "### NLLC_3 — Failure" — the workbook renderer's
# per-case heading. The em-dash is what the renderer emits; a plain hyphen is
# accepted for hand-edited docs.
_CASE_HEADING = re.compile(
    r"^###\s+([A-Za-z][A-Za-z0-9]*_[A-Za-z0-9]+)\s*[—-]+\s*(\w+)\s*$",
    re.MULTILINE)

# "## Lending Library (anna-library) (C1)" — a per-actor SHEET heading. The
# workbook groups cases by the ecosystem role that executes them; each role's
# cases certify THAT actor. The heading text carries the role's display label.
_SHEET_HEADING = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def _role_for_sheet(sheet_title: str, role_labels: Mapping[str, str]) -> str | None:
    """Match a sheet heading to a domain role by its declared display label.

    `role_labels` is `{LENDING_LIBRARY: "Lending Library", ...}` from the pack.
    A sheet titled "Lending Library (anna-library) (C1)" resolves to
    LENDING_LIBRARY. Longest label first, so "Borrowing Library" is not
    shadowed by a shorter substring. None when the sheet names no known role
    (e.g. "Schema and Contract" — a build-time check owned by no participant)."""
    title_l = sheet_title.lower()
    for role_key, label in sorted(role_labels.items(),
                                  key=lambda kv: -len(kv[1])):
        if label.lower() in title_l:
            return role_key
    return None


def _initiator_for(case_id: str, partner_prefixes: set[str]) -> str:
    """A case whose id carries a PARTNER role's prefix is initiated (executed
    and reported) by the partner; everything else is the authority's own.
    Mirrors `tc_store_sync`'s prefix convention, sourced from the pack."""
    for prefix in partner_prefixes:
        if case_id.startswith(prefix):
            return "bank"
    return "npci"


def case_catalogue_for_change(db: "Session", change_id: str,
                              *, role: str | None = None) -> list[dict]:
    """Parse the change's published cert workbook into run_round's catalogue.

    When `role` is given (the role the partner under test certifies for, e.g.
    LENDING_LIBRARY), the catalogue is SCOPED to the cases that role executes —
    the workbook's sheet for that actor. A partner certifies its OWN role's
    cases, exactly as a UPI remitter bank does not run the payee PSP's cases;
    the switch's and other participants' sheets belong to their own runs.
    A case under a role's sheet is that role's to execute (initiator=bank for a
    non-authority role), overriding the id-prefix heuristic.

    Returns [] when the change has no cert_test_cases document — the caller
    keeps its existing fallbacks, and the round's coverage note says what was
    (not) covered.
    """
    from sqlalchemy import inspect as _sa_inspect

    from app.models.product_kit import ProductKitDocType, ProductKitDocument

    # Inspector-gated: cert_pack_run runs in unit tests that build a MINIMAL
    # schema (just the cert/sim tables) and never create product_kit_documents.
    # A missing table there is not an error — it means "no workbook", the same
    # answer as an empty table — so probe before querying rather than letting a
    # bare OperationalError escape and mask the empty-round path under test.
    if not _sa_inspect(db.get_bind()).has_table(
            ProductKitDocument.__tablename__):
        return []

    doc = (db.query(ProductKitDocument)
           .filter(ProductKitDocument.change_request_id == change_id,
                   ProductKitDocument.doc_type == ProductKitDocType.CERT_TEST_CASES)
           .order_by(ProductKitDocument.created_at.desc())
           .first())
    if doc is None or not (doc.content or "").strip():
        return []

    pack = get_active_pack()
    vocab = cert_vocabulary_of(pack)
    partner_prefixes = set(vocab.role_prefixes.values())
    role_labels = dict(vocab.role_labels)
    code_pattern = error_code_pattern_of(pack)
    want_role = (role or "").strip().upper() or None

    from app.models.api_registry import ApiMessage

    api_names = [m.api_name for m in
                 db.query(ApiMessage).filter(ApiMessage.status == "active")]

    text = doc.content
    # Index sheet boundaries so each case knows which actor's sheet it is under.
    sheets = [(m.start(), m.group(1)) for m in _SHEET_HEADING.finditer(text)]

    def _sheet_role_at(pos: int) -> str | None:
        title = next((t for start, t in reversed(sheets) if start < pos), "")
        return _role_for_sheet(title, role_labels)

    headings = list(_CASE_HEADING.finditer(text))
    catalogue: list[dict] = []
    skipped_other_role = 0
    for i, m in enumerate(headings):
        case_id, outcome = m.group(1), m.group(2).strip().lower()
        block = text[m.end(): headings[i + 1].start() if i + 1 < len(headings)
                     else len(text)]
        case_role = _sheet_role_at(m.start())

        # Role scoping: certify only the cases the partner's own role executes.
        # A case under another participant's sheet belongs to that actor's run.
        # A case under no role's sheet (schema/contract checks) is not a single
        # participant's to answer for and is left out of a per-partner round.
        if want_role is not None and case_role != want_role:
            skipped_other_role += 1
            continue

        mentioned = [name for name in api_names if name in block]
        # Prefer an API the CHANGE actually touched: a case narrative often
        # walks the surrounding flow ("…echoed via RespLoanReport…") before
        # naming the message under test, and first-mention alone then binds
        # the case to an out-of-delta API — silently dropping it from scope.
        # (Found live: CR-2's LL_6 mentioned RespLoanReport first and vanished
        # from a 7/8 round, reported only as a coverage note.)
        from app.services.cert_case_builder import delta_messages

        delta = {m.lower() for m in delta_messages(db, change_id)}
        api = (next((n for n in mentioned if n.lower() in delta), None)
               or (mentioned[0] if mentioned else None))

        # The workbook's Success/Failure names the SCENARIO outcome the case
        # exercises, not the certification verdict — a Failure case that
        # produces its specified error code PASSES certification. The
        # expected code is what the scenario (and the response-code
        # assertion) should carry.
        if outcome == "success":
            expected_rc = "SUCCESS"
        else:
            found = code_pattern.findall(block) if code_pattern else []
            expected_rc = found[0] if found else "FAILURE"

        # A case under the partner's own (non-authority) role sheet is the
        # partner's to execute, whatever its id prefix; fall back to the prefix
        # heuristic when the sheet names no role.
        is_authority_role = any(
            p.is_authority for p in _participants(pack) if p.key.upper() == (case_role or ""))
        if case_role and not is_authority_role:
            initiator = "bank"
        else:
            initiator = _initiator_for(case_id, partner_prefixes)

        entry = {
            "case_id": case_id,
            "api": api,
            "initiator": initiator,
            "expected_status": "PASS",
            "authority_batch": {"expected_rc": expected_rc},
            "_origin": "cert_test_cases_kit_doc",
            "_role": case_role,
        }
        if api is None:
            entry["_gap"] = "case block names no active registry API"
        catalogue.append(entry)

    logger.info("cert_catalogue: change=%s role=%s -> %d case(s) from the "
                "published workbook (%d skipped as other-role, %d with no API "
                "match)", change_id, want_role, len(catalogue),
                skipped_other_role,
                sum(1 for c in catalogue if c.get("_gap")))
    return catalogue


def _participants(pack):
    from app.core.domain.contract import participants_of

    return participants_of(pack)
