# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The ratified plan's per-file change list — one reader for every consumer.

The analysis agent's output schema has drifted: the file list has appeared under five
different key names and each entry's path under three different field names. Every
hand-copied reader was a place the next drift could silently disarm a guard — a reader
that knows only one key returns [], and an empty list reads as "nothing planned" rather
than as an error (989aee7a dropped 13 planned files yet reported 0 missing; 215ead25's
``file_change_list`` disarmed the required-schema guard AND left the TSD's component
design section empty).

Consumers live in several packages (orchestrator, plan audit, TSD generation), so this
sits in its own module rather than in any one of them.
"""
from __future__ import annotations

PLAN_FILE_KEYS = ("files_to_modify", "files_to_change", "per_file_changes",
                  "per_file_change_list", "file_change_list")

def _msg_tokens(text: str) -> list[str]:
    """Wire-message names (UPI: ReqTransfer, RespBalEnq, …) as they appear in schema
    inventories, file intents, and flow routes. The shape is the active pack's
    ``message_name_pattern``; a pack that declares none finds no tokens, so the
    party-flow requirement demands nothing."""
    from app.core.domain.contract import message_name_pattern_of
    from app.core.domain.registry import get_active_pack

    pattern = message_name_pattern_of(get_active_pack())
    if pattern is None or not text:
        return []
    return [m.group(0) for m in pattern.finditer(text)]

# Java artefacts named after a message (RespBalEnqValidator, ReqTransferStageHandler…)
# match the token pattern but are NOT wire messages — a party-flow demand for a
# validator class is a false alarm. Wire messages are Req/Resp-PREFIXED, never
# suffixed with these, so blocking them costs no real message.
_NOT_A_MESSAGE_SUFFIX = ("Validator", "Handler", "Listener", "Listner", "Assembler",
                         "Controller", "Service", "Builder", "Factory", "Manager",
                         "Impl", "Util", "Test", "Dao", "Dto", "Stage", "Mapper",
                         "Processor", "Router", "Resolver", "Converter", "Filter",
                         "Interceptor", "Command", "Event", "Request", "Response",
                         "Adapter", "Client", "Repository", "Config", "Exception")

_PATH_FIELDS = ("path", "file", "filepath")

# Intent markers that mean "this file already exists" — a plan entry that edits an
# existing file is NOT introducing anything, so gates keyed on "what does this plan add"
# must not count it.
_EXISTING_INTENTS = ("extend", "modify", "edit", "update", "change", "amend", "reuse")


def plan_file_entries(ta: dict | None) -> list[tuple[str, dict]]:
    """The plan's per-file change entries as ``[(path, entry)]``. Pure; fail-open to []."""
    try:
        ta = ta or {}
        raw = next((ta[k] for k in PLAN_FILE_KEYS if isinstance(ta.get(k), list) and ta[k]), [])
        out = []
        for pf in raw:
            if not isinstance(pf, dict):
                continue
            p = str(next((pf[f] for f in _PATH_FIELDS if pf.get(f)), "")).strip()
            if p:
                out.append((p, pf))
        return out
    except Exception:  # noqa: BLE001 — plan reading is best-effort everywhere it's used
        return []


def touched_message_stems(ta: dict | None, flow: dict | None = None) -> list[str]:
    """UPI message names (ReqX/RespX) the plan's TECHNICAL surface touches — the
    input to the party-flow requirement. Detection is by message TOKEN, not file
    name: UPI schemas commonly bundle many messages in one file (network-meta.xsd), so
    a Req/Resp file-stem check silently misses e.g. an odLimit change to RespBalEnq.
    Scanned: schema_inventory, the per-file change entries (path + intent), and
    flow_spec's route fields — deliberately NOT functional prose or risks, which
    may name neighbouring messages the change does not touch. Pure; fail-open."""
    try:
        ta = ta or {}
        texts = [str(ta.get("schema_inventory") or "")]
        for p, pf in plan_file_entries(ta):
            texts.append(p)
            texts.append(" ".join(str(pf.get(k) or "") for k in ("intent", "action", "op", "change")))
        for k in ("steps", "flow", "messages", "actors"):
            texts.append(str((flow or {}).get(k) or ""))
        found = {m for t in texts for m in _msg_tokens(t)
                 if not m.endswith(_NOT_A_MESSAGE_SUFFIX)}
        return sorted(found)
    except Exception:  # noqa: BLE001
        return []


def entry_adds_file(entry: dict) -> bool:
    """Does this entry INTRODUCE the file, rather than edit an existing one? The intent /
    action text is free-form prose ("ADD", "EXTEND — add the new credential subType"), so
    an explicit existing-file marker is what disqualifies it; an unlabelled entry counts
    as an add, keeping the pre-existing behaviour for plans that omit the field."""
    try:
        txt = " ".join(str(entry.get(k) or "") for k in ("intent", "action", "op", "change")).lower()
        return not any(m in txt for m in _EXISTING_INTENTS)
    except Exception:  # noqa: BLE001
        return True
