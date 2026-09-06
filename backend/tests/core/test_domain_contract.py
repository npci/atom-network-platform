# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The pack contract, exercised by three packs shaped like the reference domains.

The point of these tests is the ABSENCE cases. A domain pack must be able to
say "my domain has no certification body" and "my domain has no machine-to-
machine partner channel" by simply not implementing those methods — because
those are real properties of the reference domains, not oversights:

  * internal API deprecation — no certifier, no publishing authority
  * OCPP                     — certification exists, but no M2M channel

If a pack were forced to stub out capabilities it lacks, the contract would be
making packs lie about their domain, which defeats the purpose.
"""
import pytest

from app.core.domain.contract import (
    ArtifactSpec, CertResult, CertificationHarness, ChangeType, CrossFieldRule,
    DomainPack, Delivery, Finding, Participant, Partner, PartnerChannel,
    PartnerResponse, RepoRole, certification_of, channel_of, combination_rules_of,
    participants_of, repo_roles_of, validators_of, wire_format_of,
)


# ── Packs shaped like the three reference domains ────────────────────────────

class MinimalPack:
    """Required members only — no participants, validators, cert or channel.

    This is the API-deprecation shape: no regulator, no certifier, and
    "partners" are consumer teams reached over GitHub.
    """

    key = "minimal"
    version = "0.1"

    def change_types(self):
        return [ChangeType(key="deprecation", label="API deprecation",
                           artifacts=["notice"])]

    def artifacts(self):
        return [ArtifactSpec(key="notice", label="Deprecation notice",
                             renderer="markdown")]

    def prompt_blocks(self):
        # No "authority" block: there is no regulator to cite.
        return {"hard_rules": "State the sunset date and the replacement."}


class _Harness:
    """Matches the corrected CertificationHarness: a single orchestration that
    configures, executes and interprets — not queue-and-poll. See contract.py."""

    key = "oca"

    async def run(self, *, change_id, partner_id, role, test_data,
                  test_data_per_case=None):
        return CertResult(passed=True, run_id="run-1")


class CertOnlyPack(MinimalPack):
    """OCPP shape: a real external certifier, but NO partner channel."""

    key = "cert_only"

    def certification(self):
        return _Harness()


class _Channel:
    key = "github"
    supports_responses = True

    async def deliver(self, partner: Partner, artifacts):
        return Delivery(partner_key=partner.key, delivered=True)

    async def poll_responses(self, partner: Partner):
        return [PartnerResponse(partner_key=partner.key, kind="ack")]


class _Validator:
    key = "sunset-date"

    def check(self, artifact_key: str, text: str):
        return [] if "sunset" in text.lower() else [
            Finding(severity="error", message="no sunset date")
        ]


class FullPack(MinimalPack):
    """the network shape: every capability present."""

    key = "full"

    def participants(self):
        return [Participant(key="authority", label="Authority", is_authority=True)]

    def validators(self):
        return [_Validator()]

    def certification(self):
        return _Harness()

    def channel(self):
        return _Channel()


ALL_PACKS = [MinimalPack(), CertOnlyPack(), FullPack()]


# ── The contract itself ──────────────────────────────────────────────────────

@pytest.mark.parametrize("pack", ALL_PACKS, ids=lambda p: p.key)
def test_every_pack_satisfies_the_required_surface(pack):
    """Including MinimalPack, which implements nothing optional.

    This is the assertion that would have failed under the 04/§2 sketch, where
    the optional members lived in the Protocol with default bodies.
    """
    assert isinstance(pack, DomainPack)


def test_minimal_pack_declares_absence_by_omission():
    pack = MinimalPack()
    assert certification_of(pack) is None, "no certification body in this domain"
    assert channel_of(pack) is None, "no machine-to-machine channel in this domain"
    assert participants_of(pack) == ()
    assert repo_roles_of(pack) == ()
    assert validators_of(pack) == ()


def test_ocpp_shape_has_certification_but_no_channel():
    """The combination that breaks a network-shaped design: publication and
    certification without any partner channel."""
    pack = CertOnlyPack()
    assert isinstance(certification_of(pack), CertificationHarness)
    assert channel_of(pack) is None


def test_full_pack_exposes_every_capability():
    pack = FullPack()
    assert isinstance(channel_of(pack), PartnerChannel)
    assert isinstance(certification_of(pack), CertificationHarness)
    assert [p.key for p in participants_of(pack)] == ["authority"]
    assert len(validators_of(pack)) == 1


def test_accessors_never_raise_on_a_pack_missing_the_member():
    """Core must never AttributeError its way through an unfamiliar pack."""
    class Bare:
        key = "bare"
        version = "0"
        def change_types(self): return []
        def artifacts(self): return []
        def prompt_blocks(self): return {}

    bare = Bare()
    for accessor in (participants_of, validators_of, certification_of, channel_of,
                     wire_format_of, combination_rules_of):
        accessor(bare)   # must not raise


def test_wire_format_and_combination_rules_absent_by_omission():
    """A domain that inspects no payloads declares that by omitting
    `wire_format`; one with no declared cross-field rules supplies none — the
    variant generator then reports coverage gaps rather than guessing."""
    pack = MinimalPack()
    assert wire_format_of(pack) is None
    assert combination_rules_of(pack) == ()


def test_wire_format_and_combination_rules_read_when_declared():
    class WirePack(MinimalPack):
        key = "wired"

        def wire_format(self):
            return "xml"

        def combination_rules(self):
            return [CrossFieldRule(api_name="ReqThing", kind="requires",
                                   fields=["ReqThing/A", "ReqThing/B"])]

    pack = WirePack()
    assert wire_format_of(pack) == "xml"
    rules = combination_rules_of(pack)
    assert len(rules) == 1 and rules[0].kind == "requires"
    assert rules[0].critical is False


def test_repo_roles_read_when_declared():
    class TopologyPack(MinimalPack):
        def repo_roles(self):
            return [RepoRole(key="shared", label="Shared", required=True,
                             builds_first=True)]

    roles = repo_roles_of(TopologyPack())
    assert len(roles) == 1
    assert roles[0].model_dump() == {
        "key": "shared",
        "label": "Shared",
        "required": True,
        "multiple": False,
        "builds_first": True,
    }


def test_publication_and_certification_are_independent_flags():
    """OCPP publishes and certifies; an API deprecation does neither; and a
    domain may distribute without publishing. Fusing these would make one of
    the reference domains inexpressible."""
    ocpp = ChangeType(key="release", label="Release",
                      requires_certification=True, requires_publication=True)
    depr = ChangeType(key="deprecation", label="Deprecation")
    assert (ocpp.requires_certification, ocpp.requires_publication) == (True, True)
    assert (depr.requires_certification, depr.requires_publication) == (False, False)


def test_prompt_blocks_may_omit_authority():
    """A domain with no publishing authority supplies no authority block; core
    must treat every block name as optional rather than KeyError."""
    blocks = MinimalPack().prompt_blocks()
    assert "authority" not in blocks
    assert blocks.get("authority", "") == ""


def test_artifact_spec_accepts_a_blueprint_or_none():
    from app.agents.blueprints import get as get_blueprint

    spec = ArtifactSpec(key="brd", label="BRD", renderer="docx",
                        blueprint=get_blueprint("brd"))
    assert spec.blueprint is not None
    assert spec.blueprint["sections"]
    # None is valid: not every artifact is a sectioned prose document (a
    # certification test-case workbook is not).
    assert ArtifactSpec(key="cases", label="Cases", renderer="xlsx").blueprint is None


def test_contract_module_carries_no_domain_content():
    """core/domain must stay domain-neutral. The docstrings legitimately name
    the network/OCPP as worked examples, so check executable code only."""
    import inspect
    import re

    from app.core.domain import contract

    src = inspect.getsource(contract)
    # Drop the module docstring and every triple-quoted docstring/comment line.
    body = src.split('"""', 2)[-1]
    body = re.sub(r'"""(?:.|\n)*?"""', "", body)
    body = "\n".join(ln for ln in body.splitlines() if not ln.strip().startswith("#"))
    for term in ("network", "npci", "psp", "reqtransfer", "ifsc"):
        assert term not in body.lower(), f"domain term {term!r} leaked into contract.py"


# ── certificate vocabulary is PACK content, not engine content ───────────────

class TestCertVocabulary:
    """The scope a role certifies for, the product printed on a certificate
    and the case-id prefixes are labels a certification BODY defines — not
    facts the engine can derive."""

    def test_a_domain_that_declares_none_gets_empty_not_none(self):
        from app.core.domain.contract import cert_vocabulary_of

        vocab = cert_vocabulary_of(object())
        assert vocab.scope_for("PAYER_PSP") == []
        assert vocab.product_for("UPI-LITE") == [], \
            "no taxonomy is a true statement, not a reason to invent one"

    def test_the_upi_pack_supplies_its_own_words(self):
        from app.core.domain.contract import cert_vocabulary_of
        from app.packs.network.pack import NetworkPack

        vocab = cert_vocabulary_of(NetworkPack())
        assert vocab.scope_for("payer_psp") == ["ACQUIRER"], "case-insensitive"
        assert vocab.scope_for("NOT_A_ROLE") == []
        assert vocab.role_prefixes["PAYER_PSP"] == "PR_"

    def test_product_labels_resolve_by_declared_priority(self):
        """LITE and AUTOPAY both name the Lite-Autopay product and a flow can
        contain both — first declared wins, which a dict could not express."""
        from app.core.domain.contract import cert_vocabulary_of
        from app.packs.network.pack import NetworkPack

        vocab = cert_vocabulary_of(NetworkPack())
        assert vocab.product_for("UPI_LITE_AUTOPAY") == ["Network -Lite Autopay Issuer"]
        assert vocab.product_for("VOUCHER_FLOW") == \
            ["Voucher (Creation + Redemption) - B2C"]
        assert vocab.product_for("SOMETHING_ELSE") == []

    def test_the_engine_no_longer_carries_the_taxonomy(self):
        import inspect

        from app.services import cert_orchestrator

        src = inspect.getsource(cert_orchestrator)
        for token in ("ACQUIRER", "UPI -Lite Autopay Issuer", "PAYEE_PSP",
                      "MOBILE+MMID"):
            assert token not in src, \
                f"{token!r} is domain vocabulary and belongs in a pack"
