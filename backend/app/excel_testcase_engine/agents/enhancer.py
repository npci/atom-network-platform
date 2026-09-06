# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Enhancer agent: convert a one-line user brief into a typed EnrichedBrief.

Always LLM-driven. Raises ``LLMProviderError`` (which wraps the underlying
provider failure) if the configured ``enhancer`` role's provider rejects the
call after retries — there is no deterministic fallback.

BRD/TSD-only refactor: the enhancer no longer consults canonical network API
lists or the XSD diff. The API set comes from the TSD Interface Specification
verbatim; classification collapses to "tsd_driven".
"""

from __future__ import annotations

import re

from pydantic import ValidationError

from app.excel_testcase_engine.adapters.llm import get_client
from app.excel_testcase_engine.schemas.llm import Message, SystemBlock
from app.excel_testcase_engine.observability import LLMProviderError, get_logger
from app.excel_testcase_engine.schemas.enriched_brief import EnrichedBrief

from ._runtime import load_prompt, parse_json_response, retry_message

LOGGER = get_logger("network.agent.enhancer")


def _archetype_for_role_count(n: int) -> str:
    """Derive archetype from role count:
    1 role → A (single-sheet)
    2-3 roles → B (per-role sheets + version log)
    4+ roles → C (full annexure).
    """
    if n <= 1:
        return "A"
    if n <= 3:
        return "B"
    return "C"


def _format_user_message(brief: str, options: dict) -> str:
    return (
        f"User brief: ```{brief}```\n\n"
        f"User options: {options}\n\n"
        "Return the EnrichedBrief JSON object. Do NOT include any prose or markdown fences."
    )


# Matches API tokens shaped like Req…/Resp… with CamelCase remainder ≥ 3 chars.
# In BRD/TSD-only mode any such token from the TSD Interface Specification is
# accepted verbatim — no filtering against a canonical list.
_API_TOKEN_RE = re.compile(r"\b(?:Req|Resp)[A-Z][A-Za-z0-9]{2,}\b")


def _union_tsd_interface_apis(
    enriched: EnrichedBrief, tsd_sections: dict | None,
) -> None:
    """Union APIs named in the TSD Interface Specification into enriched.apis.

    BRD/TSD-only: all TSD-declared APIs are in scope regardless of whether
    they appear in the canonical historic set. No new/existing bucketing.
    No-op when the Interface Specification section is missing.
    """
    if not tsd_sections:
        return
    interface_text = tsd_sections.get("interface_spec") or ""
    if not interface_text.strip():
        return
    candidates = set(_API_TOKEN_RE.findall(interface_text))
    if not candidates:
        return
    existing_apis = set(enriched.apis)
    added: list[str] = []
    for name in sorted(candidates):
        if name not in existing_apis:
            enriched.apis.append(name)
            existing_apis.add(name)
            added.append(name)
    if added:
        LOGGER.info("enhancer.tsd_interface_apis_union", added=added)
        enriched.assumptions.append(
            f"TSD Interface Specification union: added={added}"
        )


async def enhance(brief: str, options: dict | None = None) -> EnrichedBrief:
    """Convert a one-line brief into an EnrichedBrief via the LLM."""

    options = dict(options or {})
    forced_archetype = options.get("archetype")
    if forced_archetype not in {None, "A", "B", "C"}:
        forced_archetype = None
    client = get_client("enhancer")

    system = [SystemBlock(text=load_prompt("enhancer.md"), cache=True)]
    base_msg = _format_user_message(brief, options)
    user_msg = base_msg

    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = await client.complete(
                system=system,
                messages=[Message(role="user", content=user_msg)],
                # 2000 was too small and truncated the response on EVERY run:
                # the reply was cut at ~8.8k chars mid-string, so attempt 0
                # always failed with `Invalid control character` at the same
                # offset and the whole 3-attempt budget was spent recovering
                # from a self-inflicted wound. The prompt no longer asks the
                # model to echo `original_brief` back (see enhancer.md — the
                # caller fills that field below), which is what made the
                # output so large, but the cap needs headroom for a brief
                # naming many APIs and roles regardless.
                max_tokens=8000,
                response_format="json",
            )
            data = parse_json_response(response.text)
            data.setdefault("original_brief", brief)
            data["options"] = data.get("options") or options
            # Respect the caller's explicit archetype choice. The LLM may
            # offer an opinion, but options.archetype is the user's contract
            # with the API and must not be silently downgraded.
            if forced_archetype:
                if data.get("archetype") != forced_archetype:
                    LOGGER.info(
                        "enhancer.archetype_forced",
                        llm_proposed=data.get("archetype"),
                        forced=forced_archetype,
                    )
                data["archetype"] = forced_archetype
            # BRD/TSD-only: collapse api_classification to a single value.
            data["api_classification"] = "tsd_driven"
            enriched = EnrichedBrief.model_validate(data)

            # TSD Interface Specification union — the authoritative source for
            # the API set in BRD/TSD-only mode.
            _union_tsd_interface_apis(enriched, options.get("tsd_sections") or {})

            # NO archetype-driven role padding here any more, deliberately.
            #
            # This used to overwrite the roles read out of the BRD/TSD with the
            # pack's default list whenever the archetype "needed" more (B<2,
            # C<3). Two things were wrong with it. It inverted the source of
            # truth — the archetype is a presentation choice, so letting it
            # rewrite the document's own party list put ungrounded roles into a
            # certification pack. And it could not even deliver on its promise:
            # padding reads from `role_sheet_names()`, so on a pack that
            # declares fewer roles than the archetype demands (ADCN: Operator,
            # Maintenance Organisation) the count came back short anyway and
            # the planner rejected the plan downstream — which is exactly how
            # cert_test_cases failed on 2026-09-05.
            #
            # The role list is now whatever the documents name: two if they
            # name two, six if they name six. Only an empty list is an error,
            # caught in planner._assert_brief_has_roles.
            LOGGER.info(
                "enhancer.ok", attempt=attempt, archetype=enriched.archetype,
                apis=len(enriched.apis), roles=len(enriched.roles),
                role_names=list(enriched.roles),
            )
            return enriched
        except (ValidationError, ValueError) as exc:
            LOGGER.warning("enhancer.invalid_json", attempt=attempt, error=repr(exc)[:200])
            user_msg = retry_message(
                base_msg,
                f"Your previous response was not valid: {exc}\n"
                "Return ONLY the EnrichedBrief JSON object matching the schema "
                "exactly. Omit `original_brief` — the caller fills it in.",
            )
            last_error = exc
        except Exception as exc:
            LOGGER.error("enhancer.provider_error", error=repr(exc))
            last_error = exc
            break

    raise LLMProviderError(f"Enhancer failed after retries: {last_error!r}")
