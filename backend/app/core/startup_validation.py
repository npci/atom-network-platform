# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Fail-fast startup validation for security-relevant configuration.

Closes:
  - A7 (architecture review Critical #13, "HMAC Fail-Open Toggle Exists") --
    an ACTIVE partner with no `signing_secret` bypasses HMAC envelope
    verification entirely. Previously the only signal was a once-per-partner
    WARNING log; this promotes it to a startup-blocking check per
    security_architecture_skills.md Section 4.3 ("Applications MUST validate
    hostility-tier configuration at startup ... fail fast instead of
    starting insecurely").
  - S1 (security_architecture_skills.md Section 4.1-4.3) -- the platform previously
    had no hostility-tier taxonomy or startup validator beyond a
    `secret_key` length check. `validate_hostility_tier_config()` is the
    first cut of that validator: it checks that every H3 (externally
    exposed) boundary's mandatory limits (max body size, rate limiting) are
    configured to a sane, non-zero value before the app is allowed to serve
    traffic.

Call `run_all(fail_fast=True)` once from `app.main`'s startup event, after
`load_db_overrides()` (DB-sourced config must be loaded before validation
runs, since an operator may have tuned these via the Admin UI rather than
`.env`).

`fail_fast=True` raises `StartupValidationError` (uncaught, so ASGI server
startup itself fails -- the intended "fail fast instead of starting
insecurely" behaviour). `fail_fast=False` (used by health/diagnostic
endpoints and by tests) returns the list of `ValidationIssue` without
raising, so an operator can inspect what would fail without crashing a
running process.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class StartupValidationError(RuntimeError):
    """Raised by `run_all(fail_fast=True)` when one or more Critical
    validation issues are found. Left uncaught by `app.main.on_startup`,
    this aborts ASGI server startup -- the platform will not begin serving
    traffic in a known-insecure configuration."""


@dataclass
class ValidationIssue:
    check: str
    severity: str          # "critical" | "high" | "warning"
    detail: str


def _is_production(settings) -> bool:
    """True when this process is configured as a production deployment.

    Read defensively: `app_env` arrives from the environment and DB-backed
    config overrides, so it can be an unexpected type or carry whitespace.
    """
    return (str(getattr(settings, "app_env", "") or "")).strip().lower() == "production"


def _check_active_partners_have_hmac_secret() -> list[ValidationIssue]:
    """A7 -- every ACTIVE PartnerAgent must have a signing_secret configured,
    unless the operator has explicitly opted out via
    `a2a_require_hmac_for_active_partners=False` (documented dev/staging
    escape hatch -- NOT recommended for production)."""
    from app.core.config import settings
    issues: list[ValidationIssue] = []
    if not getattr(settings, "a2a_require_hmac_for_active_partners", True):
        # AR-13 -- the opt-out is documented as a dev/staging convenience, but
        # nothing enforced that. Disabling it in production is precisely the
        # condition this check exists to catch: an ACTIVE partner with no
        # signing_secret takes the back-compat pass-through in
        # sdk_hmac_middleware and its traffic is never envelope-verified. So in
        # production the opt-out is itself the critical finding, rather than a
        # way to silence one. Returning [] here (the previous behaviour) let an
        # operator turn the boot-time guard off and get a clean startup.
        if _is_production(settings):
            return [ValidationIssue(
                check="a2a_require_hmac_for_active_partners_disabled_in_production",
                severity="critical",
                detail=(
                    "A2A_REQUIRE_HMAC_FOR_ACTIVE_PARTNERS=false with APP_ENV=production. "
                    "This disables the boot-time check that every ACTIVE partner has a "
                    "signing_secret, and an ACTIVE partner without one bypasses HMAC "
                    "envelope verification entirely. The opt-out exists for dev and "
                    "staging environments that provision partners before their secrets; "
                    "it is not a production configuration. Remove the override, or "
                    "configure the missing secrets via "
                    "POST /admin/partners/{id}/rotate-hmac-secret."
                ),
            )]
        return issues
    try:
        from app.core.database import SessionLocal
        from app.models.phase_c import PartnerAgent, PartnerStatus
        db = SessionLocal()
        try:
            unsecured = (
                db.query(PartnerAgent)
                .filter(PartnerAgent.status == PartnerStatus.ACTIVE)
                .filter(PartnerAgent.signing_secret.is_(None))
                .all()
            )
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001 -- a DB unavailable at import time
        # (e.g. `alembic upgrade head` running first, or a schema not yet
        # migrated) must not itself crash the check; log and let the
        # caller's fail_fast policy decide whether an unreadable DB is
        # itself fatal via a different, DB-connectivity-specific check.
        logger.warning("A7 startup check: could not query PartnerAgent (%s) -- skipping", e)
        return []
    if unsecured:
        names = ", ".join(f"{p.id}:{p.name}" for p in unsecured[:10])
        more = f" (+{len(unsecured) - 10} more)" if len(unsecured) > 10 else ""
        issues.append(ValidationIssue(
            check="a2a_hmac_required_for_active_partners",
            severity="critical",
            detail=(
                f"{len(unsecured)} ACTIVE partner(s) have no signing_secret and would "
                f"bypass HMAC envelope verification: {names}{more}. Configure via "
                f"POST /admin/partners/{{id}}/rotate-hmac-secret, or set "
                f"A2A_REQUIRE_HMAC_FOR_ACTIVE_PARTNERS=false to explicitly accept this "
                f"risk in a non-production environment."
            ),
        ))
        # security_architecture_skills.md Section 13.2 -- configuration validation
        # failures MUST be emitted as structured security telemetry, not
        # only a startup log line, so a security dashboard can alert on it
        # even if the process is later forced up with fail_fast=False.
        logger.error(
            "SECURITY_EVENT event=config_validation_failure severity=critical "
            "check=a2a_hmac_required_for_active_partners unsecured_partner_count=%d",
            len(unsecured),
        )
    return issues


def validate_hostility_tier_config() -> list[ValidationIssue]:
    """S1 -- minimal hostility-tier config validator for the H3 (externally
    exposed / partner-facing) A2A boundary. Checks that the mandatory
    per-tier limits security_architecture_skills.md Section 4.2 requires
    (max request size, rate limiting) are set to sane, non-zero values.

    This is deliberately narrow (A2A is the one interface with a fully
    externalized config surface today) rather than attempting to enumerate
    every interface in the platform in one pass -- see
    docs/ARCHITECTURE_REVIEW_REMEDIATION.md Section S1 for the full hostility-tier
    classification table and the plan to extend this validator as each
    additional H1/H2/H3 boundary is classified."""
    from app.core.config import settings
    issues: list[ValidationIssue] = []

    max_body = int(getattr(settings, "a2a_max_request_body_bytes", 0) or 0)
    if max_body <= 0:
        issues.append(ValidationIssue(
            check="h3_a2a_max_request_body_bytes",
            severity="critical",
            detail=(
                "a2a_max_request_body_bytes is 0/unset -- the H3 A2A boundary has no "
                "application-layer request size limit. Set A2A_MAX_REQUEST_BODY_BYTES "
                "(default 10MB)."
            ),
        ))

    if getattr(settings, "a2a_rate_limit_enabled", True):
        window = int(getattr(settings, "a2a_rate_limit_window_s", 0) or 0)
        if window <= 0:
            issues.append(ValidationIssue(
                check="h3_a2a_rate_limit_window",
                severity="high",
                detail=(
                    "a2a_rate_limit_enabled is True but a2a_rate_limit_window_s is "
                    "0/unset -- rate limiting cannot function without a window."
                ),
            ))
    else:
        issues.append(ValidationIssue(
            check="h3_a2a_rate_limit_enabled",
            severity="high",
            detail=(
                "a2a_rate_limit_enabled is False -- the H3 A2A boundary has no "
                "application-layer rate limit (security_architecture_skills.md Section 16 "
                "prohibits relying on the gateway alone for this)."
            ),
        ))

    return issues


def validate_precert_engine_tls() -> list[ValidationIssue]:
    """Refuse to run the precert engine without a CA certificate.

    `NfiniteConnector` dials `precert_engine_precert_url` over HTTPS to drive
    the network test transactions. The old `precert_engine_verify_peers=false` escape
    hatch (CERT_NONE) was removed (CBOM-TLS-CERTNONE-3) -- verification is now
    mandatory whenever the engine is enabled.

    Scoped to the engine actually being ENABLED: with
    `precert_engine_enabled=false` the connector is never constructed (every
    instantiation sits inside `orchestrate_cert_run_precert_engine`), so an
    unused, unverified setting is not a reason to block a boot. And scoped to
    production, so local experimentation is untouched.
    """
    from app.core.config import settings
    issues: list[ValidationIssue] = []

    if not getattr(settings, "precert_engine_enabled", False):
        return issues
    if (getattr(settings, "app_env", "") or "").strip().lower() != "production":
        return issues

    if not (getattr(settings, "precert_engine_ca_cert_path", "") or "").strip():
        issues.append(ValidationIssue(
            check="precert_engine_ca_cert_path",
            severity="critical",
            detail=(
                "precert_engine_enabled is true but precert_engine_ca_cert_path is "
                "unset -- the simulator uses a self-signed certificate, so every call "
                "would fail at connect time. "
                "Set PRECERT_ENGINE_CA_CERT_PATH to that certificate (PEM)."
            ),
        ))

    return issues


def validate_reranker_backend() -> list[ValidationIssue]:
    """SBOM findings 2 / 12-14 / 17-18 -- make a broken reranker LOUD.

    WHY THIS EXISTS. torch and sentence-transformers were removed from this
    image on 2026-08-28 (six SBOM findings, including a CVSS 9.8) and the model
    now runs in the `reranker` sidecar, reached over HTTP. The capability is
    preserved -- but so is a hazard, and it is a subtle one.

    THE RERANKER FAILS OPEN BY DESIGN. `app/rag/reranker.py` catches every
    failure -- missing package, model-load timeout, HTTP error, schema mismatch
    -- and returns the candidates in RRF order. That is the right behaviour: a
    reranker outage must not take down retrieval. But it means a
    MISCONFIGURATION IS INVISIBLE. Search still works; it just quietly loses
    the +5-15pp recall@10 the reranker was providing, and nothing complains.

    Two ways to land in that state after the split:

      1. `reranker_backend` left at its old default of "local" while
         `use_reranker` is on. The in-process code path still exists and is
         still supported, but the library it imports is no longer installed
         here, so every call fails open. Previously harmless, now guaranteed.
      2. `reranker_backend` correctly set to "remote" but `reranker_url` empty
         -- `_rerank_remote` logs one warning and falls back on every call.

    Both report "high": logged prominently, but NOT blocking. Refusing to boot
    because an optional search-quality enhancement is misconfigured would be a
    worse outcome than the degradation itself, and reranking is off by default
    anyway. The point is to convert a silent quality regression into a visible
    operational signal -- which is exactly what was missing before.

    Nothing is reported when `use_reranker` is false: an unused feature being
    unconfigured is not a problem, and warning about it every boot would train
    people to ignore startup output.
    """
    from app.core.config import settings
    issues: list[ValidationIssue] = []

    if not getattr(settings, "use_reranker", False):
        return issues

    backend = (getattr(settings, "reranker_backend", "local") or "local").strip().lower()

    if backend == "local":
        issues.append(ValidationIssue(
            check="reranker_backend_local_without_model_libs",
            severity="high",
            detail=(
                "use_reranker is ON with reranker_backend='local', but torch and "
                "sentence-transformers are NOT installed in this image -- they were "
                "moved to the reranker sidecar (services/reranker) to clear 6 SBOM "
                "findings. The local path will fail open on every call, so retrieval "
                "silently loses the reranker's +5-15pp recall@10 with no error. "
                "Set RERANKER_BACKEND=remote and RERANKER_URL=http://reranker:8200/rerank, "
                "then start the sidecar: docker compose --profile reranker up -d"
            ),
        ))
    elif backend == "remote":
        if not (getattr(settings, "reranker_url", "") or "").strip():
            issues.append(ValidationIssue(
                check="reranker_url_unset",
                severity="high",
                detail=(
                    "use_reranker is ON and reranker_backend='remote', but "
                    "reranker_url is empty. Every rerank call will fail open to RRF "
                    "order -- search keeps working but loses the reranker's benefit "
                    "silently. Set RERANKER_URL (compose default: "
                    "http://reranker:8200/rerank)."
                ),
            ))
    else:
        issues.append(ValidationIssue(
            check="reranker_backend_unknown",
            severity="warning",
            detail=(
                f"reranker_backend={backend!r} is not recognised; the code falls back "
                f"to 'local', which has no model library in this image. Use 'remote'."
            ),
        ))

    return issues


def validate_jwt_key_strength() -> list[ValidationIssue]:
    """SBOM finding 11 (CVE-2025-45768, PyJWT, CVSS 6.3) -- prove the HMAC
    signing key is strong.

    WHY THIS EXISTS. The CVE says PyJWT does not enforce a minimum key length
    for HMAC signing, so an application with a weak secret mints weak tokens.
    It is widely DISPUTED (the maintainers hold that key strength is the
    application's job) and it has NO FIXED VERSION -- so no upgrade can clear
    it. The only way to close the finding honestly is to demonstrate that this
    application does the thing the CVE says the library fails to do. This
    function is that demonstration, and it is referenced by the VEX statement
    in docs/sbom/vex.json.

    WHY IT IS NOT REDUNDANT with config.py::_check_secret_key_length, which
    already hard-blocks a <32-char secret_key outside development. Two reasons:

      1. THAT CHECK RUNS AT SETTINGS CONSTRUCTION, i.e. against .env only.
         `app.main` calls `load_db_overrides()` BEFORE `run_all()`, so an
         operator can set secret_key from the Admin UI and land a value the
         pydantic validator never saw. This runs after the overrides.
      2. LENGTH IS NOT ENTROPY. "aaaaaaaa...32 chars" passes a length test and
         is trivially guessable. The checks below catch the degenerate cases
         that a length gate cannot.

    SEVERITY IS DELIBERATELY CHOSEN TO AVOID AN OUTAGE. This is an
    authentication path: a new fail-closed check here would refuse to boot any
    environment holding a weak secret, turning a documentation gap into a
    production incident. So:

      - Length in non-dev is ALREADY a hard block, unchanged, in config.py.
        Nothing new becomes blocking, so no currently-booting environment can
        stop booting because of this change.
      - The new entropy checks report "high", which LOGS LOUDLY but does not
        block (`run_all` only raises on "critical").

    Promote these to "critical" once every environment is confirmed clean --
    that is a deliberate, scheduled follow-up, not something to do in the same
    change that introduces the check. Rotating secret_key invalidates every
    live token and signs out every logged-in user, so it needs a maintenance
    window rather than a surprise on deploy.
    """
    from app.core.config import settings
    issues: list[ValidationIssue] = []
    env = (getattr(settings, "app_env", "") or "").strip().lower()
    key = getattr(settings, "secret_key", "") or ""

    # 256 bits is the correct floor for HS256: the HMAC block size matches the
    # hash output, so a shorter key reduces the effective security of the
    # signature. This mirrors config.py's threshold rather than inventing a
    # second number.
    _MIN_LEN = 32

    if env == "development":
        # Dev is intentionally permissive (config.py skips its hard block
        # here too), but silence would let a weak key travel from a laptop
        # into a shared .env unnoticed.
        if key and len(key) < _MIN_LEN:
            issues.append(ValidationIssue(
                check="jwt_secret_key_length_dev",
                severity="warning",
                detail=(
                    f"secret_key is {len(key)} characters; {_MIN_LEN} is the minimum "
                    f"for HS256 and is ENFORCED outside development. This environment "
                    f"will not boot once APP_ENV is not 'development'."
                ),
            ))
        return issues

    # Non-development. Length is already a config.py hard block, so reaching
    # here with a short key means the value arrived via a DB override after
    # Settings was constructed -- worth surfacing explicitly.
    if len(key) < _MIN_LEN:
        issues.append(ValidationIssue(
            check="jwt_secret_key_length",
            severity="critical",
            detail=(
                f"secret_key is {len(key)} characters, below the {_MIN_LEN}-character "
                f"minimum for HS256 JWT signing. Because config.py's validator would "
                f"have caught this at .env load, the value most likely came from a DB "
                f"config override -- check the Admin UI as well as .env."
            ),
        ))
        return issues

    # Entropy sanity checks. Not a statistical test -- just the degenerate
    # cases that pass a length gate and still leave tokens forgeable.
    distinct = len(set(key))
    if distinct < 8:
        issues.append(ValidationIssue(
            check="jwt_secret_key_entropy",
            severity="high",
            detail=(
                f"secret_key is long enough but uses only {distinct} distinct "
                f"character(s), so its real entropy is far below its length. A padded "
                f"or repeated string is guessable regardless of length. Generate a "
                f"random value: python -c \"import secrets; print(secrets.token_urlsafe(48))\""
            ),
        ))

    lowered = key.lower()
    _PLACEHOLDERS = (
        "changeme", "change-me", "secret", "password", "please-change",
        "your-secret", "replace", "example", "dev-secret", "test-secret",
        "insecure", "placeholder", "todo",
    )
    hit = next((p for p in _PLACEHOLDERS if p in lowered), None)
    if hit:
        issues.append(ValidationIssue(
            check="jwt_secret_key_placeholder",
            severity="high",
            detail=(
                f"secret_key contains the placeholder text {hit!r}, which suggests a "
                f"template value was never replaced. Anything derived from published "
                f"boilerplate must be treated as public. Generate a random value: "
                f"python -c \"import secrets; print(secrets.token_urlsafe(48))\""
            ),
        ))

    return issues


def validate_encryption_keys() -> list[ValidationIssue]:
    """F-005 -- warn when encryption keys are unset or use defaults.

    config_encryption_key protects DB-stored secrets (API keys, tokens,
    passwords). mfa_encryption_key protects TOTP seeds.

    NOTE: In production, config_encryption_key is now enforced by a pydantic
    @model_validator in config.py (raises ValueError, blocking boot). This
    startup validator covers the mfa_encryption_key warning (which is not a
    hard block -- PBKDF2 derivation from secret_key is a reasonable fallback)
    and acts as belt-and-suspenders documentation for config_encryption_key.
    It also warns about the dev-internal token in non-production environments
    so operators are reminded to set a real value before going to production.
    """
    from app.core.config import settings
    issues: list[ValidationIssue] = []
    env = (getattr(settings, "app_env", "") or "").strip().lower()

    if env == "production":
        cek = (getattr(settings, "config_encryption_key", "") or "").strip()
        if not cek:
            issues.append(ValidationIssue(
                check="config_encryption_key",
                severity="critical",
                detail=(
                    "config_encryption_key is unset in production -- DB-stored secrets "
                    "(API keys, tokens, passwords) cannot be encrypted. Set "
                    "CONFIG_ENCRYPTION_KEY to a Fernet.generate_key() value. "
                    "(Also enforced by pydantic @model_validator in config.py.)"
                ),
            ))
        mek = (getattr(settings, "mfa_encryption_key", "") or "").strip()
        if not mek:
            issues.append(ValidationIssue(
                check="mfa_encryption_key",
                severity="high",
                detail=(
                    "mfa_encryption_key is unset in production -- TOTP seeds are "
                    "encrypted with a key derived from secret_key via PBKDF2, which "
                    "is better than the previous raw SHA-256 but still ties MFA "
                    "protection to the JWT signing secret. Set MFA_ENCRYPTION_KEY to "
                    "a Fernet.generate_key() value for full separation."
                ),
            ))
    else:
        # Non-production: still warn so operators don't accidentally go to
        # production without these configured.
        mek = (getattr(settings, "mfa_encryption_key", "") or "").strip()
        if not mek:
            issues.append(ValidationIssue(
                check="mfa_encryption_key",
                severity="warning",
                detail=(
                    "mfa_encryption_key is unset -- TOTP seeds are encrypted with "
                    "a key derived from secret_key. Set MFA_ENCRYPTION_KEY to a "
                    "Fernet.generate_key() value before deploying to production."
                ),
            ))

    # SCR findings #3/#9 -- cert_agent_internal_token used to default to a
    # hardcoded, published-in-source value ("dev-internal-token"). It now
    # defaults to "" in every environment (config.py raises in production if
    # unset/legacy); this warns in dev/UAT so an unset token doesn't go
    # unnoticed until cert-agent calls start failing with 401s.
    cat = (getattr(settings, "cert_agent_internal_token", "") or "").strip()
    if env != "production" and not cat:
        issues.append(ValidationIssue(
            check="cert_agent_internal_token",
            severity="warning",
            detail=(
                "cert_agent_internal_token is unset -- calls from this backend to "
                "the cert-agent will send no X-Internal-Token header. Set "
                "CERT_AGENT_INTERNAL_TOKEN (and the matching CERT_AGENT_INTERNAL_TOKEN "
                "on the cert-agent side) to a strong per-deployment value."
            ),
        ))
    return issues


def validate_hmac_fail_open() -> list[ValidationIssue]:
    """F-011 -- verify HMAC_FAIL_OPEN env var has been removed.

    The HMAC_FAIL_OPEN env var has been REMOVED from hmac_signer.py. A redis
    outage now ALWAYS causes the request to be rejected (fail-closed). This
    validator checks that no stale HMAC_FAIL_OPEN setting remains in the
    environment, which would have no effect but could confuse operators.
    """
    import os as _os
    raw = (_os.environ.get("HMAC_FAIL_OPEN", "") or "").strip().lower()
    if raw in ("1", "true", "yes"):
        issues: list[ValidationIssue] = []
        issues.append(ValidationIssue(
            check="hmac_fail_open",
            severity="warning",
            detail=(
                "HMAC_FAIL_OPEN is set in the environment but has been REMOVED from "
                "hmac_signer.py -- it no longer has any effect. The nonce uniqueness "
                "check is now always fail-closed. Remove HMAC_FAIL_OPEN from the "
                "environment to eliminate this warning."
            ),
        ))
        return issues
    return []


def validate_http_defaults() -> list[ValidationIssue]:
    """F-002 -- warn about HTTP default URLs that carry sensitive data.

    Several service URLs default to http://, meaning LLM prompts/responses,
    certification envelopes, and Redis session data travel in cleartext within
    the docker network.

    NOTE: In production, http:// URLs are now BLOCKED by a pydantic
    @model_validator in config.py (raises ValueError, blocking boot). This
    startup validator covers non-production environments where the pydantic
    validator does not apply but the warning is still useful.
    """
    from app.core.config import settings
    issues: list[ValidationIssue] = []
    env = (getattr(settings, "app_env", "") or "").strip().lower()

    # Check every service URL that carries sensitive data.
    _checks = [
        ("ollama_url", "LLM prompts and responses"),
        ("authority_simulator_url", "simulator traffic"),
        ("redis_url", "session data, rate-limit counters, and JWT denylist entries"),
        ("ainxt_base_url", "LLM prompts and responses (AiNxt gateway)"),
        ("grok_base_url", "LLM prompts and responses (Grok/xAI)"),
        ("gemini_video_base_url", "video generation API keys and prompts"),
        ("cert_agent_url", "certification traffic and internal tokens"),
        ("bank_agent_url", "bank-agent traffic"),
        ("authority_public_url", "published agent card URL"),
    ]
    for attr, label in _checks:
        val = (getattr(settings, attr, "") or "").strip()
        if val.lower().startswith("http://"):
            issues.append(ValidationIssue(
                check=f"{attr}_http_url",
                severity="warning",
                detail=(
                    f"{attr} ({val}) uses http:// -- {label} "
                    "travels in cleartext. Use https:// in production."
                ),
            ))
    return issues


def run_all(fail_fast: bool = True) -> list[ValidationIssue]:
    """Run every registered startup validation check. Returns the full list
    of issues found (across all checks, all severities). When `fail_fast`
    is True and any issue has severity "critical", raises
    `StartupValidationError` after logging every issue found (not just the
    first) so a single restart surfaces the whole remediation list at once."""
    issues: list[ValidationIssue] = []
    issues += _check_active_partners_have_hmac_secret()
    issues += validate_hostility_tier_config()
    issues += validate_precert_engine_tls()
    issues += validate_jwt_key_strength()
    issues += validate_reranker_backend()
    issues += validate_encryption_keys()
    issues += validate_hmac_fail_open()
    issues += validate_http_defaults()

    for issue in issues:
        level = {"critical": logger.error, "high": logger.warning}.get(issue.severity, logger.info)
        level("STARTUP_VALIDATION [%s] %s: %s", issue.severity, issue.check, issue.detail)

    if fail_fast:
        criticals = [i for i in issues if i.severity == "critical"]
        if criticals:
            summary = "; ".join(f"{i.check}: {i.detail}" for i in criticals)
            raise StartupValidationError(
                f"{len(criticals)} critical startup validation failure(s) -- refusing to "
                f"start insecurely (security_architecture_skills.md Section 4.3): {summary}"
            )
    return issues