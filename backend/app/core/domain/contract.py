# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The domain pack contract.

A *domain pack* supplies everything the platform needs to run a change
lifecycle for one ecosystem — a payments network, OCPP, an internal API deprecation. The core
supplies the machinery (retrieval, agent loop, rendering, lifecycle); the pack
supplies the content (change ontology, artifact blueprints, prompt blocks,
validation rules) and, optionally, a partner channel and certification harness.

Nothing imports this module yet. It is landed on its own so the shape can be
reviewed before anything depends on it — see
docs/genericization/07-first-10-prs.md, PR #7.

────────────────────────────────────────────────────────────────────────────
REQUIRED vs OPTIONAL, and why that distinction is load-bearing

Three of the four members are required. The optional ones are optional because
a reference domain genuinely LACKS them, not as a courtesy:

  * An internal API deprecation has **no certification body** — not a different
    harness, none at all.
  * OCPP has **no machine-to-machine partner channel** — charge-point vendors
    are not going to run an agent; distribution is publish-and-notify.

A lifecycle that models certification as a mandatory phase deadlocks on the
first domain that has no certifier. See
docs/genericization/03-domain-coupling-audit.md §5.

────────────────────────────────────────────────────────────────────────────
CORRECTION to docs/genericization/04-target-architecture.md §2

That sketch declared the optional members inside the `DomainPack` Protocol with
default bodies (`def channel(self): return None`). **That does not work**, and
implementing it is how we found out:

    @runtime_checkable
    class P(Protocol):
        def optional(self) -> str | None: return None

    class Minimal:            # a pack that simply omits `optional`
        ...

    isinstance(Minimal(), P)  # -> False

Protocol default bodies apply only to classes that explicitly INHERIT the
Protocol. Under structural typing — the entire reason for using Protocol, so
pack authors need not import our base class — a pack that omits the member
does not satisfy the Protocol at all. The sketch would have forced every pack
to implement every capability and stub out the ones it lacks, which is exactly
the "lie about your domain" failure the optionality exists to prevent.

So: `DomainPack` declares only what every pack must have. Optional capabilities
are read through the accessor functions at the bottom of this module, which
tolerate absence. Core code must use those accessors and never `pack.channel()`
directly.

Note that `@runtime_checkable` checks only that attributes EXIST — never their
signatures. `isinstance(x, DomainPack)` is a smoke test, not a guarantee.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from pydantic import BaseModel, Field

from app.core.domain.types import Blueprint

logger = logging.getLogger(__name__)

__all__ = [
    "ChangeType", "ArtifactSpec", "Participant", "RepoRole",
    "Finding", "Validator",
    "Partner", "Delivery", "PartnerResponse", "OutboundMessage", "PartnerChannel",
    "CertResult", "CertificationHarness", "CrossFieldRule",
    "DomainPack",
    "participants_of", "repo_roles_of", "validators_of", "certification_of", "channel_of",
    "wire_format_of", "combination_rules_of", "error_code_pattern_of",
    "message_name_pattern_of", "operation_patterns_of", "party_patterns_of",
    "message_flows_of", "party_aliases_of", "financial_operations_of",
    "domain_acronyms_of", "schema_namespaces_of",
    "certification_harness_of",
    "CertVocabulary", "cert_vocabulary_of",
    "LabeledOption", "change_operations_of", "risk_levels_of", "compliance_levels_of",
    "FeatureBucket", "feature_taxonomy_of",
    "clarification_must_ask_keywords_of", "default_assumptions_of",
]


# ── Data ─────────────────────────────────────────────────────────────────────

class ChangeType(BaseModel):
    """One kind of change this domain recognises."""

    key: str                                   # "new_feature" | "deprecation"
    label: str
    artifacts: list[str] = Field(default_factory=list)   # ArtifactSpec.key values
    # These gate lifecycle states. They are independent on purpose: OCPP
    # publishes without a partner channel, an API deprecation distributes
    # without publishing. Fusing them would make one of those inexpressible.
    requires_certification: bool = False
    requires_publication: bool = False


class ArtifactSpec(BaseModel):
    """A document / deck / test-suite the platform can generate."""

    key: str                                   # "requirements_doc" | "faq"
    label: str
    renderer: str                              # "markdown" | "docx" | "pptx"
    # Single type, not a union: PR #6 established there is exactly one
    # generator per artifact. The two BRD blueprints in this codebase were a
    # generator and a drifted validation scaffold, not rival schemas.
    blueprint: Blueprint | None = None
    prompt_blocks: list[str] = Field(default_factory=list)  # assembled in order


class Participant(BaseModel):
    """An actor in the ecosystem. A payments network: PSP / Issuer / Authority. OCPP: CSMS vendor.

    `is_authority` marks the central body. A domain may have none — an internal
    API deprecation has no regulator and nothing to cite, so prompt blocks that
    demand regulatory citations must not be mandatory.
    """

    key: str
    label: str
    is_authority: bool = False


class RepoRole(BaseModel):
    """One repository role in a domain's declared topology."""

    key: str
    label: str = ""
    required: bool = False
    multiple: bool = False
    builds_first: bool = False


class LabeledOption(BaseModel):
    """A named choice a domain declares for a closed-set clarification question —
    a change operation (UPI: init/auth/debit/credit; a library domain: reserve/
    issue/renew), a risk tier, a compliance tier. Distinct from `Participant`
    (an ecosystem ACTOR) and `ChangeType` (a kind of CHANGE): this is vocabulary
    for a PM-facing multiple-choice question, and absence is legitimate — a
    domain with no operation-level scoping declares none rather than reusing
    another domain's list.
    """

    key: str
    label: str
    description: str = ""


class FeatureBucket(BaseModel):
    """One feature-classification bucket a domain declares.

    The taxonomy classifier (`app.agents.taxonomy`) buckets every change
    request to steer corpus retrieval; the buckets themselves are ecosystem
    vocabulary (UPI: payment initiation, mandates; a library network: loan
    lifecycle, reservations) and therefore pack content, not engine content.

      keywords        — fallback keyword-overlap classifier input
      seed_queries    — retrieval queries that surface similar past features
                        (stage 1 of the 3-stage hybrid-search context builder)
      required_fields — fields a complete spec MUST document for this bucket
                        (drives gap detection / clarification questions)
    """

    key: str
    label: str
    keywords: list[str] = Field(default_factory=list)
    seed_queries: list[str] = Field(default_factory=list)
    required_fields: list[str] = Field(default_factory=list)


class Finding(BaseModel):
    severity: str                              # "error" | "warning"
    message: str
    location: str | None = None


class Partner(BaseModel):
    key: str
    label: str
    endpoint: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class Delivery(BaseModel):
    partner_key: str
    delivered: bool
    reference: str | None = None               # message id, MR url, mail id
    error: str | None = None
    # Added when the kit-dispatch call site was migrated: it does not merely
    # branch on success, it records WHY a delivery failed and raises an operator
    # alert carrying the reason. Both are generic — every transport has a status
    # and a machine-readable failure code — so they belong here rather than
    # forcing callers back to the A2A row to find out what happened.
    status: str | None = None                  # transport's own status string
    error_code: str | None = None              # machine-readable failure code
    # Echoed back from the message so a failure alert can link to the change.
    # Without it the operator notification loses its "related change" link and
    # the resend deep-link degrades to "?".
    change_id: str | None = None


class PartnerResponse(BaseModel):
    partner_key: str
    kind: str                                  # "ack" | "counter" | "reject"
    body: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class CertResult(BaseModel):
    # None means "ran, outcome not yet determined" — a long-running external
    # certification may not have a verdict when the call returns. Conflating
    # that with False would report an in-flight run as a failure.
    passed: bool | None = None
    run_id: str | None = None
    report_uri: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class CrossFieldRule(BaseModel):
    """A declared business-validity rule over one API's fields.

    This is the input the request-variant generator REFUSES to invent
    (COMBINED_EXECUTION_PLAN §3.1/§6): field-level occurrence/datatype/enum
    constraints do not prove that a combination of values is a valid business
    request, and generating positives from guessed combinations creates tests
    that fail the partner for our imagination. A pack supplies the rules its
    domain has declared; ABSENCE of a rule is a reported coverage gap, never a
    licence to infer one.
    """

    api_name: str
    # requires | forbids | conditional | exactly_one | at_least_one | valid_tuple
    kind: str
    fields: list[str] = Field(default_factory=list)   # registry field paths
    # Per-kind payload: valid_tuple carries the enumerated tuples, conditional
    # carries the condition, etc. Kept open — rule vocabularies differ by domain.
    values: dict[str, Any] = Field(default_factory=dict)
    # Critical groups get three-way / enumerated coverage instead of pairwise.
    critical: bool = False


# ── Behaviour ────────────────────────────────────────────────────────────────

@runtime_checkable
class Validator(Protocol):
    key: str

    def check(self, artifact_key: str, text: str) -> Sequence[Finding]: ...


class OutboundMessage(BaseModel):
    """One message to a partner.

    CORRECTION to the first draft of this contract, found by reading the
    existing A2A dispatch before wiring anything to it. `deliver()` originally
    took `artifacts: Mapping[str, bytes]` — a one-way file drop. The real
    exchange is a TYPED, BIDIRECTIONAL protocol: `send_task_to_partner` takes a
    task type plus a structured `payload` dict, and the reply arrives as another
    typed message. Modelling it as files would have made the negotiation loop —
    counter-proposals, acknowledgements, status updates — inexpressible.
    """

    # Pack-declared, not a platform enum. The network pack uses "change_communication",
    # "counter_decision", …; an API deprecation would use "deprecation_notice".
    # Core must not own this vocabulary.
    kind: str
    # The change this message is about. Generic: every domain has a change
    # being communicated, and the transport's audit trail needs the linkage —
    # dropping it orphans the delivery record from the change it belongs to.
    change_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    # Binary documents kept SEPARATE from payload so each transport chooses its
    # own representation: the A2A adapter base64-embeds them into the wire
    # payload, an email adapter would attach them, a git adapter would commit
    # them. Forcing one representation into the contract would leak one domain's
    # wire format into every other domain.
    attachments: dict[str, bytes] = Field(default_factory=dict)
    correlation_id: str | None = None


@runtime_checkable
class PartnerChannel(Protocol):
    key: str
    # Not a convenience flag: it is the difference between a negotiation loop
    # and a broadcast. When False the lifecycle skips the negotiation states
    # rather than waiting for replies that will never arrive.
    supports_responses: bool

    async def deliver(self, partner: Partner, message: OutboundMessage) -> Delivery: ...

    async def poll_responses(self, partner: Partner) -> Sequence[PartnerResponse]: ...


@runtime_checkable
class CertificationHarness(Protocol):
    """Drive one partner's certification run for one change.

    CORRECTION to the first draft, and to 04-target-architecture §2. That
    sketched `submit(change_id, partner) -> run_id` plus `result(run_id)`:
    a queue-and-poll shape. Real certification here is not queue-and-poll — it
    is a single orchestration that configures per-case test data, executes, and
    interprets the outcome, returning a summary.

    The shape below was not invented. This codebase already had it, twice:
    `orchestrate_cert_run` (cert-agent REST) and
    `orchestrate_cert_run_precert_engine` (the in-process precert engine) share
    an identical signature and are selected by an `if settings.precert_engine_
    enabled`. Two working implementations behind a hardcoded branch is an
    extension point that had not been named yet.

    Contract inherited from those two, and worth keeping:
      * fire-and-forget — NEVER raises. Errors surface through the returned
        summary, assignment status and logs. A certification failure is a
        business outcome, not an exception.
      * `passed=None` is legitimate: the run happened, the verdict has not
        landed. Only `False` means failed.
    """

    key: str

    async def run(
        self,
        *,
        change_id: str,
        partner_id: str,
        role: str,
        test_data: dict[str, Any],
        test_data_per_case: dict[str, Any] | None = None,
        dispatch_meta: dict[str, Any] | None = None,
    ) -> CertResult: ...


@runtime_checkable
class DomainPack(Protocol):
    """What EVERY domain pack must supply. Optional capabilities are declared by
    defining the corresponding method; read them via the accessors below."""

    key: str
    version: str

    def change_types(self) -> Sequence[ChangeType]: ...

    def artifacts(self) -> Sequence[ArtifactSpec]: ...

    def prompt_blocks(self) -> Mapping[str, str]:
        """Named prose blocks injected into system prompts.

        The network pack returns what `app/packs/network/rules.py` holds. Core must
        treat every block as OPTIONAL and never KeyError on a missing name: a
        domain with no publishing authority supplies no "authority" block, and
        that is a valid domain, not a broken pack.
        """


# ── Optional-capability accessors ────────────────────────────────────────────
# Core reads optional capabilities through these, never off the pack directly.
# A pack declares a capability by defining the method; omitting it means the
# domain does not have that thing.

def participants_of(pack: DomainPack) -> Sequence[Participant]:
    fn = getattr(pack, "participants", None)
    return tuple(fn()) if callable(fn) else ()


def repo_roles_of(pack: DomainPack) -> Sequence[RepoRole]:
    fn = getattr(pack, "repo_roles", None)
    return tuple(fn()) if callable(fn) else ()


def validators_of(pack: DomainPack) -> Sequence[Validator]:
    """Empty is legitimate. The eval gate must not hard-fail a pack that
    supplies no validators — an internal deprecation notice has nothing of the
    kind to enforce."""
    fn = getattr(pack, "validators", None)
    return tuple(fn()) if callable(fn) else ()


def certification_of(pack: DomainPack,
                     key: str | None = None) -> CertificationHarness | None:
    """None means the domain has NO certification body — skip the state.

    `key` names ONE of the domain's harnesses (SIM S-6: "selection moves to a
    per-change/per-partner choice"). A dispatch that names a harness gets that
    one; `None` gets the deployment default. Naming a key a pack cannot honour
    raises, rather than silently certifying through a different engine than
    was asked for.
    """
    fn = getattr(pack, "certification", None)
    if not callable(fn):
        return None
    if key is None:
        return fn()
    try:
        return fn(key)
    except TypeError:
        raise ValueError(
            f"domain pack {getattr(pack, 'key', pack)!r} declares a single "
            f"certification harness and cannot select {key!r}")


def channel_of(pack: DomainPack) -> PartnerChannel | None:
    """None means there is no machine-to-machine partner channel (OCPP).
    Distribution degrades to publish-and-notify; it is not an error.

    Resolution mirrors `certification_of`: a Python pack supplies a channel
    OBJECT via `channel()`; a config pack can name a platform-registered
    channel via `partner_channel` (resolved through
    `app.services.partner_channels`). Core never imports the channel
    implementations — it resolves the name at the edge, as with harnesses."""
    fn = getattr(pack, "channel", None)
    if callable(fn):
        return fn()

    name_fn = getattr(pack, "partner_channel", None)
    name = name_fn() if callable(name_fn) else None
    if name:
        from app.services.partner_channels import channel_by_key

        return channel_by_key(str(name))
    return None


def wire_format_of(pack: DomainPack) -> str | None:
    """The domain's DEFAULT payload format — a key into
    `app.core.wire.registry.codec_for` (e.g. "xml", "json").

    None means the domain inspects no payloads at all — a certification that
    grades outcomes without reading message bodies is a valid domain, and the
    engine then simply generates no payload-level assertions.

    This is a default consulted at GENERATION time only: the case builder
    snapshots the key onto every stored assertion/variant row, and evaluation
    resolves the codec from the row. A stored round never depends on what the
    pack answers later.
    """
    fn = getattr(pack, "wire_format", None)
    return fn() if callable(fn) else None


def certification_harness_of(pack: DomainPack) -> str | None:
    """The NAME of a platform-registered certification harness, or None.

    The config-pack counterpart of `certification_of()`: a Python pack
    supplies a harness OBJECT (behaviour); a YAML pack can only supply the
    NAME of one the platform registers (`app.services.cert_harnesses`), and
    the dispatch seam resolves it there. Core never resolves the name itself
    — that would mean core importing the harness implementations.

    None means the domain declares no certification body. As everywhere in
    this contract, absence is a true statement, not an error.
    """
    fn = getattr(pack, "certification_harness", None)
    value = fn() if callable(fn) else None
    return str(value) if value else None


def error_code_pattern_of(pack: DomainPack) -> "re.Pattern[str] | None":
    """Compiled regex for the shape of this ecosystem's error codes.

    None means the domain declares no code shape, and a checker that would
    have asserted "this document names a recognised error code" must then
    SKIP rather than fall back to another domain's alphabet. Falling back is
    the failure this accessor exists to prevent: UPI's shape applied to a
    library-loan spec either rejects every valid document, or — worse —
    accepts one because an unrelated token happened to match.
    """
    fn = getattr(pack, "error_code_pattern", None)
    raw = fn() if callable(fn) else None
    if not raw:
        return None
    try:
        return re.compile(raw)
    except re.error:
        # A pack that ships an invalid regex has no opinion we can act on.
        # `ConfigPack` validates at load time, so reaching here means a Python
        # pack returned it; skipping beats crashing every document check.
        logger.warning("domain pack %r declares an invalid error_code_pattern; "
                       "error-code shape checks will be skipped",
                       getattr(pack, "key", pack))
        return None


def message_name_pattern_of(pack: DomainPack) -> "re.Pattern[str] | None":
    """Compiled regex for the shape of this ecosystem's wire message names
    (UPI: `\\b(?:Req|Resp)[A-Z]…\\b|\\bAck\\b`).

    None means the domain declares no message-name shape. Every consumer that
    scans prose/plans/schemas for message tokens must then find NONE — its
    behaviour degrades to "no messages found", never to another domain's
    naming convention matched by accident.
    """
    fn = getattr(pack, "message_name_pattern", None)
    raw = fn() if callable(fn) else None
    if not raw:
        return None
    try:
        return re.compile(raw)
    except re.error:
        # Same contract as error_code_pattern_of: ConfigPack validates at
        # load, so only a Python pack can reach here; skip rather than crash.
        logger.warning("domain pack %r declares an invalid message_name_pattern; "
                       "message-token scans will find nothing",
                       getattr(pack, "key", pack))
        return None


def _compiled_pattern_map(pack: DomainPack, method: str) -> "dict[str, re.Pattern[str]]":
    fn = getattr(pack, method, None)
    raw = fn() if callable(fn) else None
    out: dict[str, re.Pattern[str]] = {}
    for key, pattern in (raw or {}).items():
        try:
            out[str(key)] = re.compile(str(pattern))
        except re.error:
            logger.warning("domain pack %r: `%s.%s` is not a valid regex; "
                           "that key will never match",
                           getattr(pack, "key", pack), method, key)
    return out


def operation_patterns_of(pack: DomainPack) -> "dict[str, re.Pattern[str]]":
    """operation key -> compiled regex for heuristic FR tagging. Empty when
    the pack declares none — the tagger then tags no operations, rather than
    matching one ecosystem's verbs against another's documents."""
    return _compiled_pattern_map(pack, "operation_patterns")


def party_patterns_of(pack: DomainPack) -> "dict[str, re.Pattern[str]]":
    """party key -> compiled regex for heuristic FR tagging. Empty when the
    pack declares none; same skip-not-borrow contract as operation_patterns_of."""
    return _compiled_pattern_map(pack, "party_patterns")


def message_flows_of(pack: DomainPack) -> Mapping[str, str]:
    """wire message name -> flow family code (UPI: ReqTransfer -> PAY). Empty when
    the pack declares none — a message that maps to no flow is surfaced as
    unknown for the operator, never silently assigned a flow."""
    fn = getattr(pack, "message_flows", None)
    value = fn() if callable(fn) else None
    return {str(k): str(v) for k, v in (value or {}).items()}


def party_aliases_of(pack: DomainPack) -> Mapping[str, str]:
    """loose party spelling -> canonical party key (UPI: ISSUER ->
    REMITTER_BANK). Empty when undeclared — an unknown spelling is dropped by
    callers, never remapped through another domain's synonym table."""
    fn = getattr(pack, "party_aliases", None)
    value = fn() if callable(fn) else None
    return {str(k): str(v) for k, v in (value or {}).items()}


def financial_operations_of(pack: DomainPack) -> "frozenset[str]":
    """Change-operation keys that move money (or the domain's equivalent).
    Empty when the pack declares none — every flow then classifies as "meta",
    which is a true statement about a domain with no money movement."""
    fn = getattr(pack, "financial_operations", None)
    return frozenset(str(v) for v in (fn() if callable(fn) else ()) or ())


def domain_acronyms_of(pack: DomainPack) -> Sequence[str]:
    """The domain's own short uppercase acronyms (UPI: VPA, MPIN) — tokens an
    error-code-shaped scan must NOT mistake for codes. Empty when undeclared."""
    fn = getattr(pack, "domain_acronyms", None)
    return tuple(str(v) for v in (fn() if callable(fn) else ()) or ())


def schema_namespaces_of(pack: DomainPack) -> Sequence[Sequence[str]]:
    """Groups of semantically-equivalent schema-namespace spellings, first
    member canonical. Empty when the domain has no known variant spellings —
    namespace matching then compares raw strings, which is correct for a
    domain whose schemas never forked a spelling."""
    fn = getattr(pack, "schema_namespaces", None)
    groups = (fn() if callable(fn) else ()) or ()
    return tuple(tuple(str(m) for m in g) for g in groups if g)


class CertVocabulary(BaseModel):
    """The ecosystem's own words on a certificate — pack content, not engine.

    These are labels a certification BODY defines, not facts the engine can
    derive: which scope a role certifies for, what the product is called on
    the certificate, and how the ecosystem prefixes its case ids. They lived
    as module constants in `cert_orchestrator`, which made the engine carry
    one domain's taxonomy.

    Every field defaults EMPTY, and empty is a true statement: a domain whose
    certificates name no product taxonomy simply gets no product line, rather
    than the engine inventing one.
    """

    role_scopes: dict[str, list[str]] = Field(default_factory=dict)
    # Ordered (keyword, label) — first keyword found in the flow code wins,
    # so overlapping keywords resolve by declared priority, not dict order.
    product_labels: list[tuple[str, str]] = Field(default_factory=list)
    role_prefixes: dict[str, str] = Field(default_factory=dict)
    # Display label per role key ("PAYER_PSP" -> "Payer PSP"). Consolidates what
    # used to be five independent hardcoded copies of the same party list across
    # agents/services (question_generator, party_inference, brd_extractor, ...) —
    # each now derives its party choices from `role_scopes`/`role_labels` instead
    # of restating the enum.
    role_labels: dict[str, str] = Field(default_factory=dict)
    # role -> the test-data field names that role legitimately supplies, so a
    # payer-side payload cannot overwrite payee identifiers. The FIELD NAMES
    # are ecosystem vocabulary (`payer_vpa`, `ifsc`, `iin`), which is why the
    # map is pack content.
    role_test_data_fields: dict[str, list[str]] = Field(default_factory=dict)

    def scope_for(self, role: str | None) -> list[str]:
        return list(self.role_scopes.get((role or "").upper(), []))

    def product_for(self, flow: str | None) -> list[str]:
        haystack = (flow or "").upper()
        for keyword, label in self.product_labels:
            if keyword in haystack:
                return [label]
        return []

    def filter_test_data(self, role: str | None, test_data: dict) -> dict:
        """Keep only the fields this role legitimately supplies.

        A role the domain does not partition passes EVERYTHING through: the
        receiving side ignores keys it does not know, and silently dropping a
        partner's data because this pack has no opinion about their role
        would be a worse answer than passing it on.
        """
        if not test_data:
            return {}
        keys = self.role_test_data_fields.get((role or "").upper())
        if keys is None:
            return dict(test_data)
        allowed = set(keys)
        return {k: v for k, v in test_data.items()
                if k in allowed and v not in (None, "")}

    def parties(self) -> list[tuple[str, str]]:
        """(role_key, display_label) for every role this vocabulary scopes,
        in declaration order. The single source callers should build a party
        picker from, instead of restating `role_scopes.keys()` themselves."""
        return [(k, self.role_labels.get(k, k.replace("_", " ").title()))
                for k in self.role_scopes]


def cert_vocabulary_of(pack: DomainPack) -> CertVocabulary:
    """The domain's certificate vocabulary; EMPTY when it declares none.

    Empty rather than None: every caller wants to ask "what is the scope for
    this role" and get a real answer, and "no labels" is that answer for a
    domain that certifies without a product taxonomy.
    """
    fn = getattr(pack, "cert_vocabulary", None)
    return fn() if callable(fn) else CertVocabulary()


def combination_rules_of(pack: DomainPack) -> Sequence[CrossFieldRule]:
    """Declared cross-field business rules for request-variant generation.

    Empty is legitimate — and honest: with no declared rules the generator
    reports uncovered combinations as explicit gaps for operator review rather
    than inventing business validity from field-level constraints.
    """
    fn = getattr(pack, "combination_rules", None)
    return tuple(fn()) if callable(fn) else ()


def change_operations_of(pack: DomainPack) -> Sequence[LabeledOption]:
    """The domain's change-scoped operations (UPI: init/auth/debit/credit/...).

    Empty is legitimate — a domain with no operation-level scoping omits this,
    and callers that fan a question out per operation simply ask none rather
    than inventing UPI's list for a different ecosystem.
    """
    fn = getattr(pack, "change_operations", None)
    return tuple(fn()) if callable(fn) else ()


def risk_levels_of(pack: DomainPack) -> Sequence[LabeledOption]:
    """The domain's risk-tier vocabulary for scoping test-coverage depth.

    Empty when a pack declares none — callers fall back to a domain-neutral
    default rather than a KeyError, since "how risky is this change" is a
    generically useful signal even for a pack that hasn't named its own tiers.
    """
    fn = getattr(pack, "risk_levels", None)
    return tuple(fn()) if callable(fn) else ()


def compliance_levels_of(pack: DomainPack) -> Sequence[LabeledOption]:
    """The domain's compliance-sensitivity vocabulary (UPI: RBI-mandated,
    PMLA-touched). Empty when a pack declares none."""
    fn = getattr(pack, "compliance_levels", None)
    return tuple(fn()) if callable(fn) else ()


def feature_taxonomy_of(pack: DomainPack) -> Sequence[FeatureBucket]:
    """The domain's feature-classification buckets for retrieval steering.

    Empty is legitimate — a domain that declares no taxonomy gets a single
    generic bucket from the classifier (no seed queries, no required fields)
    rather than another domain's buckets. See `app.agents.taxonomy`.
    """
    fn = getattr(pack, "feature_taxonomy", None)
    return tuple(fn()) if callable(fn) else ()


def clarification_must_ask_keywords_of(pack: DomainPack) -> Sequence[str]:
    """Gap-key substrings that make a clarification question PM-BLOCKING.

    These are the things a domain says a PM must personally own — anything
    that changes compliance posture, API shape, or money/asset movement.
    Empty is legitimate and honest: a domain that declares none gets no
    domain-specific blockers (criticality flagged by the detector still
    blocks), rather than inheriting another ecosystem's list of worries.
    """
    fn = getattr(pack, "clarification_must_ask_keywords", None)
    return tuple(str(v) for v in fn()) if callable(fn) else ()


def default_assumptions_of(pack: DomainPack) -> Mapping[str, str]:
    """Safe default answers for common non-critical gap keys.

    The values are DOMAIN FACTS ("30 seconds (standard UPI SLA)"), which is
    exactly why they live in the pack: assuming one ecosystem's SLA or
    retention rule for another is an invented fact stated with confidence.
    Empty when a pack declares none — the platform then records the gap
    without inventing a default.
    """
    fn = getattr(pack, "default_assumptions", None)
    if not callable(fn):
        return {}
    value = fn() or {}
    return {str(k): str(v) for k, v in value.items()}
