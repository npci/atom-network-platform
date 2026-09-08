# Copyright 2026 The ATOM Authors
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
partner channel still writes a real pack class (see the `network`/`nlln` packs
under `app/packs/`) and registers it in `registry._PACKS` as before; this is
additive, not a replacement for that path.

SCHEMA (every key optional except `key`):

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
        for a non-critical gap. Domain FACTS ("30 seconds (standard network SLA)"),
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

    # ── Certification wire identity ──────────────────────────────────────────
    #
    # The four keys below are REQUIRED once a pack declares
    # `certification_harness:`. They are not vocabulary — every one of them is
    # matched, byte for byte, by a counterparty this repository cannot see: the
    # cert agent, the partner platform, the participant simulator. A wrong value does
    # not raise; it produces a payload that is rejected, or a lookup that finds
    # nothing, with no code path looking wrong.
    #
    # So they are neither hardcoded (a sweep can change a constant) nor
    # defaulted (a placeholder that silently "works" is the same trap wearing a
    # different hat). A deployment that certifies MUST state them, and a pack
    # that forgets fails at LOAD — before the process serves a request, rather
    # than three days later as an unexplained certification failure.

    def _certifies(self) -> bool:
        return bool(self._data.get("certification_harness"))

    def _cert_mapping(self, key: str, required_fields: tuple[str, ...]) -> Mapping[str, str]:
        """A mapping key that is mandatory for a certifying pack.

        Absent, empty, or missing a field → `ConfigPackError` naming the pack
        file, the key, and exactly which fields are missing. A pack that does
        not certify may omit it entirely.
        """
        value = self._data.get(key)
        if value is None or value == {}:
            if not self._certifies():
                return {}
            raise ConfigPackError(
                f"{self._source_path}: `{key}:` is required because this pack "
                f"declares `certification_harness:` — add it with the fields "
                f"{', '.join(required_fields)}. These values are matched by the "
                f"certification counterparty, so there is no safe default for "
                f"the platform to invent."
            )
        if not isinstance(value, Mapping):
            raise ConfigPackError(
                f"{self._source_path}: `{key}:` must be a mapping, "
                f"got {type(value).__name__}")
        out = {str(k): str(v).strip() for k, v in value.items()}
        missing = [f for f in required_fields if not out.get(f)]
        if missing:
            raise ConfigPackError(
                f"{self._source_path}: `{key}:` is missing a value for "
                f"{', '.join(missing)} — every field is sent to, or matched by, "
                f"the certification counterparty and cannot be defaulted."
            )
        return out

    def cert_roles(self) -> Mapping[str, str]:
        """The tokens that name each side of a certification exchange.

        `authority` and `partner` are required. Stamped onto `initiated_by`,
        used as the keys of the per-side counts in the push summary, and
        compared against the counterparty's own test-case catalogue.

        `authority_wire` / `partner_wire` are OPTIONAL and default to the
        matching required token. They exist because the certification workbook
        carries a SECOND, differently-cased spelling of the same two roles:
        `txn_initiated_by` has always been `"Bank"`/`"NPCI"`, not
        `"BANK"`/`"NPCI"`, and cert-agent matches that spelling exactly (see
        excel_testcase_engine/domain_vocab.py and the testcase sheet builder,
        which both say so). Deriving it by upper-casing the canonical token
        would silently change a wire value, so the deployment states it.
        """
        roles = dict(self._cert_mapping("cert_roles", ("authority", "partner")))
        if not roles:
            # A pack that does not certify omits the whole section, and
            # `_cert_mapping` returns {} for it. Defaulting the wire spellings
            # off the required keys must not resurrect them here — indexing
            # `roles["authority"]` unconditionally turned "this domain has no
            # certification body" into a KeyError at pack load.
            return roles
        roles.setdefault("authority_wire", roles["authority"])
        roles.setdefault("partner_wire", roles["partner"])
        return roles

    def cert_test_defaults(self) -> Mapping[str, str]:
        """Default per-case test data (addresses, amount, currency) used when a
        stub supplies none. See `contract.cert_test_defaults_of`."""
        value = self._data.get("cert_test_defaults")
        if value is None or value == {}:
            if not self._certifies():
                return {}
            raise ConfigPackError(
                f"{self._source_path}: `cert_test_defaults:` is required "
                f"because this pack declares `certification_harness:` — the "
                f"addresses it carries are resolved against the counterparty's "
                f"seeded directory, so a default invented here turns a "
                f"happy-path case into an unknown-address failure."
            )
        if not isinstance(value, Mapping):
            raise ConfigPackError(
                f"{self._source_path}: `cert_test_defaults:` must be a mapping")
        return {str(k): str(v) for k, v in value.items()}

    def wire_envelope(self) -> Mapping[str, str]:
        """Root element / namespace / originator id stamped on generated
        certification payloads. See `contract.wire_envelope_of` for why this
        is pack DATA and not a repository constant."""
        return self._cert_mapping(
            "wire_envelope", ("root_element", "namespace", "org_id"))

    def wire_templates(self) -> Mapping[str, Any]:
        """Built-in request templates keyed by flow code, plus the placeholder
        names a generated template may use. See `contract.wire_templates_of`.

        Optional even for a certifying pack: a domain that ships no catalogue
        has every flow fall through to the LLM template generator, which is
        already what happens for any flow code the catalogue does not cover.
        """
        value = self._data.get("wire_templates") or {}
        if not isinstance(value, Mapping):
            raise ConfigPackError(
                f"{self._source_path}: `wire_templates:` must be a mapping")
        return value

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
        (the network: `ReqTransfer`/`RespTransfer`/`Ack`). None means the domain
        declares no message-name shape, and every consumer that scans text for message
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
        """wire message name -> flow family code (the network: ReqTransfer ->
        PAY), the offline baseline for cert-engine flow mapping. `{}` when undeclared —
        callers then surface "unknown flow" instead of guessing."""
        return self._str_mapping("message_flows")

    def party_aliases(self) -> Mapping[str, str]:
        """loose party spelling -> canonical party key (the network: ISSUER ->
        REMITTER_BANK). `{}` when undeclared — unknown spellings are then
        dropped, never remapped through another domain's synonyms."""
        return self._str_mapping("party_aliases")

    def primary_effect_operations(self) -> Sequence[str]:
        """The change-operation keys that carry the domain's PRIMARY EFFECT —
        the flows that actually do the consequential thing, as opposed to the
        metadata/initiation/status flows around them. A charging network: the
        energy-delivery flows. A payments network: the ones that move money.
        Empty when the domain declares none — every flow then classifies as
        "meta".

        Reads `primary_effect_operations:`, falling back to the DEPRECATED
        `financial_operations:` spelling so a pack written before the rename
        keeps working. Declare the new key in new packs.
        """
        section = (self._data.get("primary_effect_operations")
                   if self._data.get("primary_effect_operations") is not None
                   else self._data.get("financial_operations"))
        key = ("primary_effect_operations"
               if self._data.get("primary_effect_operations") is not None
               else "financial_operations")
        if section is not None and not isinstance(section, list):
            raise ConfigPackError(
                f"{self._source_path}: `{key}:` must be a list, "
                f"got {type(section).__name__}"
            )
        return tuple(str(v) for v in (section or ()))

    def pii_patterns(self) -> Mapping[str, str]:
        """redaction kind -> regex for a PII class this domain knows about.

        ADDITIVE ONLY. `core/pii_redaction.py` ships country-neutral built-ins
        (E.164 numbers, addressing handles, bare and label-anchored account
        runs) that always run; this block lets a domain add the classes only it
        can name — a national mobile format, a scheme-specific customer
        reference, a secret-entry token. The kind becomes the placeholder text,
        so `MOBILE` redacts to `[REDACTED-PII-MOBILE]`.

        `regex_values=True`, so an uncompilable pattern fails the pack at LOAD
        time rather than quietly never matching. `{}` when undeclared — the
        built-ins still run.
        """
        return self._str_mapping("pii_patterns", regex_values=True)

    def high_assurance_participants(self) -> Sequence[str]:
        """Participant keys whose channel must be mutually authenticated.

        Read by the partner-registry TLS-tier endpoint: a partner holding any
        of these roles belongs on the mTLS ingress, everyone else on the
        JWT-only one. Which roles carry that much trust is a DOMAIN judgement
        (a payments network: the account-holding banks; an aviation network:
        the design organisations) — the platform has no basis to guess it, so
        an undeclared key means "no role is special", i.e. nobody is forced
        onto mTLS by their role alone.
        """
        return tuple(str(v) for v in self._section("high_assurance_participants"))

    def domain_acronyms(self) -> Sequence[str]:
        """Short uppercase tokens that look like error codes but are the
        domain's own acronyms, whatever the pack declares (e.g. "IATA",
        "ICAO"). Empty when undeclared."""
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

    pack = ConfigPack(data, source_path=str(resolved))

    # Force the certification wire identity to validate NOW rather than at first
    # use. Every accessor on this class is lazy, so a pack missing a required
    # key would load clean and only fail when something first tried to certify —
    # which is a running deployment discovering its own misconfiguration in the
    # middle of a certification run, not at boot. Touching them here converts
    # that into a startup failure naming the file and the missing key.
    pack.cert_roles()
    pack.cert_test_defaults()
    pack.wire_envelope()

    return pack
