# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""A generic, code-free `DomainPack` loaded from one YAML file.

Half of the contract is pure DATA (`prompt_blocks`, `participants`,
`cert_vocabulary`, `change_operations`, `risk_levels`, `compliance_levels`,
`change_types`, `repo_roles`, `combination_rules` — strings, dicts, lists of `{key, label}`
pairs). The other half is genuinely BEHAVIOUR: `certification()` calls an
external harness, `channel()` speaks a wire protocol, `validators()` run
logic against document text. Those three can never be a config file, so a
`ConfigPack` simply never defines them — which is exactly how the contract
already expresses "this domain has none" (see `contract.py`'s docstring on
optional-capability accessors; `MinimalPack` in `test_domain_contract.py` is
the same idiom).

This is the escape hatch for a domain that needs only vocabulary: no PR, no
Python file, no entry in `registry._PACKS` — just a YAML file and
`DOMAIN_PACK=/path/to/it.yaml`. A domain that also needs certification or a
partner channel still writes a real pack class (see the `upi`/`nlln` packs
under `app/packs/`) and registers it in `registry._PACKS` as before; this is
additive, not a replacement for that path.

SCHEMA (every key optional except `key`) — see `docs/domain_pack_config.md`
for the full reference:

    key: mydomain                        # required — becomes pack.key
    version: "1.0"
    prompt_blocks:
      platform_name: "..."
      authority: "..."
      # any of the named blocks agents read via prompt_block(name, default)
    participants:
      - {key: authority_org, label: "...", is_authority: true}
      - {key: participant_a, label: "..."}
    repo_roles:
      - key: core
        label: "Core / shared library"
        required: true
        builds_first: true
      - key: app
        required: true
    change_operations:
      - {key: reserve, label: "Reserve (place a hold)"}
    risk_levels:
      - {key: low, label: "Low", description: "..."}
    compliance_levels:
      - {key: standard, label: "Standard"}
    cert_vocabulary:
      role_scopes: {ROLE_A: ["SCOPE_A"]}
      role_labels: {ROLE_A: "Role A"}
      role_prefixes: {ROLE_A: "RA_"}
      role_test_data_fields: {ROLE_A: [field_one, field_two]}
    change_types:
      - key: new_feature
        label: "New feature"
        artifacts: [brd, tech_spec]
        requires_certification: false
        requires_publication: true
    artifacts:
      - {key: brd, label: "Business Requirement Document", renderer: docx, blueprint_doc_type: brd}
    combination_rules:
      - {api_name: ReqThing, kind: requires, fields: [ReqThing/A, ReqThing/B]}
    clarification_must_ask_keywords: [loan_period, recall_window]
    default_assumptions:
      retry_policy: "3 retries with exponential backoff"
    feature_taxonomy:
      - key: reservations
        label: "Reservations / holds"
        keywords: [reserve, hold]
        seed_queries: ["reservation hold placement flow"]
        required_fields: [patron_id, item_id]

`artifacts[].blueprint_doc_type` is optional and, if given, must name a doc
type `app.agents.blueprints.get()` recognises (today: canvas/brd/tech_spec/
xsd) — those section blueprints are platform document structure, not this
pack's content. Omit it for an artifact with no sectioned blueprint (e.g. a
test-case workbook).
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from app.core.domain.contract import (
    ArtifactSpec, CertVocabulary, ChangeType, CrossFieldRule, FeatureBucket,
    LabeledOption, Participant, RepoRole,
)


class ConfigPackError(RuntimeError):
    """The YAML file exists but its shape doesn't satisfy the schema above.

    Deliberately loud, same reasoning as `registry.UnknownPackError`: a
    malformed pack producing an empty/partial vocabulary would read as
    confusing wrong prose, not as an error.
    """


class ConfigPack:
    """Satisfies `app.core.domain.contract.DomainPack` structurally, sourcing
    everything from a dict parsed out of one YAML file. See module docstring
    for the schema."""

    def __init__(self, data: Mapping[str, Any], *, source_path: str):
        self._data = data
        self._source_path = source_path
        key = str(data.get("key") or "").strip()
        if not key:
            raise ConfigPackError(
                f"{source_path}: top-level `key:` is required and must be non-empty"
            )
        self.key = key
        self.version = str(data.get("version") or "1.0")

    def __repr__(self) -> str:
        return f"ConfigPack(key={self.key!r}, source={self._source_path!r})"

    def _section(self, name: str) -> list:
        value = self._data.get(name) or []
        if not isinstance(value, list):
            raise ConfigPackError(
                f"{self._source_path}: `{name}:` must be a list, got {type(value).__name__}"
            )
        return value

    def _build(self, name: str, model) -> tuple:
        try:
            return tuple(model(**item) for item in self._section(name))
        except (TypeError, ValueError) as exc:
            raise ConfigPackError(
                f"{self._source_path}: an entry under `{name}:` doesn't match the "
                f"expected fields for {model.__name__} — {exc}"
            ) from None

    # ── Required members ────────────────────────────────────────────────────

    def change_types(self) -> Sequence[ChangeType]:
        return self._build("change_types", ChangeType)

    def artifacts(self) -> Sequence[ArtifactSpec]:
        from app.agents.blueprints import get as get_blueprint

        out = []
        for item in self._section("artifacts"):
            item = dict(item)
            doc_type = item.pop("blueprint_doc_type", None)
            try:
                out.append(ArtifactSpec(
                    blueprint=get_blueprint(doc_type) if doc_type else None,
                    **item,
                ))
            except (TypeError, ValueError) as exc:
                raise ConfigPackError(
                    f"{self._source_path}: an entry under `artifacts:` doesn't match "
                    f"the expected fields — {exc}"
                ) from None
        return tuple(out)

    def prompt_blocks(self) -> Mapping[str, str]:
        blocks = self._data.get("prompt_blocks") or {}
        if not isinstance(blocks, dict):
            raise ConfigPackError(
                f"{self._source_path}: `prompt_blocks:` must be a mapping, "
                f"got {type(blocks).__name__}"
            )
        return {str(k): str(v) for k, v in blocks.items()}

    # ── Optional capabilities — all pure data, so all supported here.
    # certification()/channel()/validators() are NOT defined: those are
    # behaviour, not data, and omission is how the contract's accessors
    # (certification_of/channel_of/validators_of) correctly read "absent". ──

    def participants(self) -> Sequence[Participant]:
        return self._build("participants", Participant)

    def repo_roles(self) -> Sequence[RepoRole]:
        return self._build("repo_roles", RepoRole)

    def change_operations(self) -> Sequence[LabeledOption]:
        return self._build("change_operations", LabeledOption)

    def risk_levels(self) -> Sequence[LabeledOption]:
        return self._build("risk_levels", LabeledOption)

    def compliance_levels(self) -> Sequence[LabeledOption]:
        return self._build("compliance_levels", LabeledOption)

    def feature_taxonomy(self) -> Sequence[FeatureBucket]:
        return self._build("feature_taxonomy", FeatureBucket)

    def clarification_must_ask_keywords(self) -> Sequence[str]:
        """Gap-key substrings that force a clarification question to block the
        PM. Domain judgement calls, not platform mechanics — which is why they
        are pack data. Empty means no domain-specific blockers."""
        value = self._data.get("clarification_must_ask_keywords") or []
        if not isinstance(value, list):
            raise ConfigPackError(
                f"{self._source_path}: `clarification_must_ask_keywords:` must "
                f"be a list of strings, got {type(value).__name__}"
            )
        return tuple(str(v) for v in value)

    def default_assumptions(self) -> Mapping[str, str]:
        """gap-key -> the safe default the platform may assume (and surface)
        for a non-critical gap. Domain FACTS ("30 seconds (standard UPI SLA)"),
        so they live here. Empty means the platform invents no assumptions."""
        value = self._data.get("default_assumptions") or {}
        if not isinstance(value, dict):
            raise ConfigPackError(
                f"{self._source_path}: `default_assumptions:` must be a "
                f"mapping of gap-key to default text, got {type(value).__name__}"
            )
        return {str(k): str(v) for k, v in value.items()}

    def combination_rules(self) -> Sequence[CrossFieldRule]:
        return self._build("combination_rules", CrossFieldRule)

    def cert_vocabulary(self) -> CertVocabulary:
        cv = self._data.get("cert_vocabulary") or {}
        if not isinstance(cv, dict):
            raise ConfigPackError(
                f"{self._source_path}: `cert_vocabulary:` must be a mapping, "
                f"got {type(cv).__name__}"
            )
        try:
            return CertVocabulary(**cv)
        except (TypeError, ValueError) as exc:
            raise ConfigPackError(
                f"{self._source_path}: `cert_vocabulary:` doesn't match the "
                f"expected fields — {exc}"
            ) from None

    def wire_format(self) -> str | None:
        value = self._data.get("wire_format")
        return str(value) if value else None

    def partner_channel(self) -> str | None:
        """The NAME of the platform-registered partner channel this domain
        distributes over (e.g. ``a2a``) — or None when the domain has no
        machine-to-machine channel and publishes instead.

        Data, not behaviour, exactly like `certification_harness`: the string
        keys into `app.services.partner_channels`, which owns the transport.
        Omission means publish-and-notify (the OCPP shape), which is a true
        statement about some ecosystems, not a missing feature.
        """
        value = self._data.get("partner_channel")
        return str(value) if value else None

    def certification_harness(self) -> str | None:
        """The NAME of the platform-registered certification harness this
        domain certifies through (e.g. ``sim_pack``) — or None when the domain
        declares no certification body.

        This is data, not behaviour: the string keys into the platform's own
        harness registry (`app.services.cert_harnesses`), which supplies and
        owns every implementation. A YAML pack can therefore say "certify me
        with the platform's pack-driven simulator" without shipping a line of
        code — while a name the platform does not register is refused loudly
        at dispatch, never silently defaulted (`certification_dispatch`).

        Omission keeps its meaning: no key, no certification body, dispatch
        skips. That is the same absence-by-omission contract as every other
        optional capability here.
        """
        value = self._data.get("certification_harness")
        return str(value) if value else None

    def _validated_regex(self, key: str) -> str | None:
        """A single regex-valued key, validated at load time rather than at
        first use: a bad regex here is a typo in a config file, and the loader
        is where config typos should surface. Deferring it would turn a
        one-character mistake into a document that silently stops being
        checked."""
        value = self._data.get(key)
        if not value:
            return None
        pattern = str(value)
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ConfigPackError(
                f"{self._source_path}: `{key}:` is not a valid "
                f"regular expression — {exc}"
            ) from None
        return pattern

    def _str_mapping(self, key: str, *, regex_values: bool = False) -> Mapping[str, str]:
        """A `{str: str}` mapping key; `{}` when absent. With `regex_values`,
        every value must compile — same load-time-loudness reasoning as
        `_validated_regex`."""
        value = self._data.get(key) or {}
        if not isinstance(value, dict):
            raise ConfigPackError(
                f"{self._source_path}: `{key}:` must be a mapping, "
                f"got {type(value).__name__}"
            )
        out = {str(k): str(v) for k, v in value.items()}
        if regex_values:
            for k, v in out.items():
                try:
                    re.compile(v)
                except re.error as exc:
                    raise ConfigPackError(
                        f"{self._source_path}: `{key}.{k}:` is not a valid "
                        f"regular expression — {exc}"
                    ) from None
        return out

    def error_code_pattern(self) -> str | None:
        """Regex naming the shape of THIS ecosystem's error codes."""
        return self._validated_regex("error_code_pattern")

    def message_name_pattern(self) -> str | None:
        """Regex naming the shape of THIS ecosystem's wire message names
        (UPI: `ReqTransfer`/`RespTransfer`/`Ack`). None means the domain declares no
        message-name shape, and every consumer that scans text for message
        tokens must then find NONE rather than borrowing another domain's
        alphabet."""
        return self._validated_regex("message_name_pattern")

    def operation_patterns(self) -> Mapping[str, str]:
        """operation key -> regex matching prose that references it, for the
        heuristic FR taggers. `{}` when the domain declares none — taggers then
        tag nothing rather than matching another domain's verbs."""
        return self._str_mapping("operation_patterns", regex_values=True)

    def party_patterns(self) -> Mapping[str, str]:
        """party key -> regex matching prose that references it. `{}` when
        undeclared, same contract as `operation_patterns`."""
        return self._str_mapping("party_patterns", regex_values=True)

    def message_flows(self) -> Mapping[str, str]:
        """wire message name -> flow family code (UPI: ReqTransfer -> PAY), the
        offline baseline for cert-engine flow mapping. `{}` when undeclared —
        callers then surface "unknown flow" instead of guessing."""
        return self._str_mapping("message_flows")

    def party_aliases(self) -> Mapping[str, str]:
        """loose party spelling -> canonical party key (UPI: ISSUER ->
        REMITTER_BANK). `{}` when undeclared — unknown spellings are then
        dropped, never remapped through another domain's synonyms."""
        return self._str_mapping("party_aliases")

    def financial_operations(self) -> Sequence[str]:
        """The change-operation keys that move money (or the domain's
        equivalent of consequence-bearing flows). Empty when the domain
        declares none — every flow then classifies as "meta"."""
        return tuple(str(v) for v in self._section("financial_operations"))

    def domain_acronyms(self) -> Sequence[str]:
        """Short uppercase tokens that look like error codes but are the
        domain's own acronyms (UPI: VPA, MPIN). Empty when undeclared."""
        return tuple(str(v) for v in self._section("domain_acronyms"))

    def schema_namespaces(self) -> Sequence[Sequence[str]]:
        """Groups of semantically-equivalent schema namespace spellings; the
        first member of each group is the canonical spelling. Empty when the
        domain has no known variant spellings."""
        groups = self._section("schema_namespaces")
        out: list[tuple[str, ...]] = []
        for g in groups:
            if not isinstance(g, (list, tuple)) or not g:
                raise ConfigPackError(
                    f"{self._source_path}: every entry under `schema_namespaces:` "
                    f"must be a non-empty list of namespace spellings"
                )
            out.append(tuple(str(m) for m in g))
        return tuple(out)


def load(path: str) -> ConfigPack:
    """Parse `path` as YAML and return a `ConfigPack` over it.

    Raises `ConfigPackError` for anything that isn't a readable, parseable,
    top-level-mapping YAML file — never returns a partial/broken pack.
    """
    from pathlib import Path

    resolved = Path(path).expanduser()
    if not resolved.is_file():
        raise ConfigPackError(f"{path!r} looks like a file path but no file exists there")

    import yaml

    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigPackError(f"{path!r} could not be read: {exc}") from None

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigPackError(f"{path!r} is not valid YAML: {exc}") from None

    if not isinstance(data, dict):
        raise ConfigPackError(
            f"{path!r} must contain a YAML mapping at the top level, "
            f"got {type(data).__name__}"
        )

    return ConfigPack(data, source_path=str(resolved))
