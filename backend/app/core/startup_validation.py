# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Fail-fast startup validation for security-relevant configuration.

Closes:
  - A7 (architecture review Critical #13, "HMAC Fail-Open Toggle Exists") --
    an ACTIVE partner with no `signing_secret` bypasses HMAC envelope
    verification entirely. Previously the only signal was a once-per-partner
    WARNING log; this promotes it to a startup-blocking check, per the rule
    that applications MUST validate hostility-tier configuration at startup
    and "fail fast instead of starting insecurely".
  - S1 -- the platform previously
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
        # itself fatal. `validate_schema_up_to_date()` below is that
        # DB-specific check: it distinguishes "schema behind the code" from
        # "DB briefly unreachable", so a drift no longer hides in this
        # skip-and-warn path the way it did before 2026-09-06.
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
        # Configuration validation
        # failures MUST be emitted as structured security telemetry, not
        # only a startup log line, so a security dashboard can alert on it
        # even if the process is later forced up with fail_fast=False.
        logger.error(
            "SECURITY_EVENT event=config_validation_failure severity=critical "
            "check=a2a_hmac_required_for_active_partners unsecured_partner_count=%d",
            len(unsecured),
        )
    return issues


def _alembic_script_directory():
    """The migration scripts belonging to THIS checkout, or None.

    Shares `db_migrate`'s locator so this check and the optional auto-migrate
    can never disagree about which scripts are authoritative.
    """
    from app.core.db_migrate import script_directory
    return script_directory()


def validate_schema_up_to_date() -> list[ValidationIssue]:
    """Refuse to serve when the ORM models are AHEAD of the database schema.

    WHY THIS EXISTS. A column rename that lands in the models and in a revision
    that is never applied leaves the two out of step. SQLAlchemy emits every
    mapped column in its SELECT list, so EVERY read of an affected table raises
    UndefinedColumn — which `main.py`'s 500 handler collapses to the opaque
    "An internal error occurred". Whichever page reads that table first is the
    one that surfaces it; the rest are equally broken and simply have not been
    opened yet.

    Nothing escalated. The A7 check above DID notice — it logged "could not
    query PartnerAgent" at every boot — but it is deliberately non-fatal about
    an unreadable DB, so the backend came up "healthy" with two tables
    unreadable and reported the failure only as a 500 per request.

    WHY IT IS NOT REDUNDANT with the compose `alembic upgrade head` that
    already precedes uvicorn. That runs ONCE, at container start. In dev,
    `backend/app` and `backend/alembic` are bind-mounted and uvicorn runs
    with `--reload`: editing a model hot-reloads the code, and writing a new
    revision drops it straight into the container, but NEITHER re-runs
    Alembic. The schema and the code drift apart between restarts, which is
    exactly the window 0141 fell into. The check runs in the startup event, so
    a reload re-runs it and the drift is caught at the moment it opens.

    SEVERITY. "DB behind code" is critical — it is never benign, every
    affected query fails, and refusing to boot is both louder and safer than
    serving 500s. "DB ahead of code" is only "high": that is what a deliberate
    application rollback looks like, and blocking boot there would prevent the
    rollback from completing. An unreachable DB or unreadable scripts is a
    "warning" — this check cannot distinguish a real fault from a container
    that simply started before Postgres accepted connections, and the app has
    its own connectivity failure modes for that.
    """
    issues: list[ValidationIssue] = []

    script = _alembic_script_directory()
    if script is None:
        logger.debug("schema check: no alembic.ini/alembic dir alongside app/ -- skipping")
        return issues

    try:
        from app.core.database import engine
        from app.core.db_migrate import current_heads
        heads = set(script.get_heads())
        current = current_heads(engine)
    except Exception as e:  # noqa: BLE001 -- see SEVERITY note above
        return [ValidationIssue(
            check="schema_revision_unreadable",
            severity="warning",
            detail=(
                f"Could not compare the database schema against the migration scripts "
                f"({type(e).__name__}: {e}). If the database is simply not up yet this is "
                f"harmless; if it persists, the schema is unverified and a model/column "
                f"mismatch would surface only as a 500 per request."
            ),
        )]

    if not current:
        # No alembic_version row: a fresh database, or one built by
        # Base.metadata.create_all (how the test suite makes its DBs). The
        # compose `alembic upgrade head` stamps it; nothing to compare yet.
        logger.info("schema check: database carries no alembic revision yet -- skipping")
        return issues

    if current == heads:
        return issues

    try:
        pending = [r.revision for r in script.iterate_revisions(tuple(heads), tuple(current))]
    except Exception:  # noqa: BLE001 -- current revision unknown to this checkout
        pending = None

    if pending:
        issues.append(ValidationIssue(
            check="db_schema_behind_code",
            severity="critical",
            detail=(
                f"The database is at revision {sorted(current)} but this code expects "
                f"{sorted(heads)}; {len(pending)} migration(s) unapplied: "
                f"{', '.join(reversed(pending))}. The ORM models therefore reference "
                f"columns the database does not have, and every query touching them "
                f"fails as an opaque 500. Apply them: "
                f"`docker exec atom_backend alembic upgrade head` (or `alembic upgrade "
                f"head` from backend/ for a native run), then restart this process."
            ),
        ))
        # Same rationale as A7 above: a validation failure that can silently
        # degrade a running system belongs in structured telemetry, not only in
        # a startup log line.
        logger.error(
            "SECURITY_EVENT event=config_validation_failure severity=critical "
            "check=db_schema_behind_code pending_migrations=%d", len(pending),
        )
    elif pending is None:
        issues.append(ValidationIssue(
            check="db_schema_revision_unknown",
            severity="high",
            detail=(
                f"The database reports revision {sorted(current)}, which does not exist "
                f"in this checkout's migration scripts (heads: {sorted(heads)}). This "
                f"usually means the database was migrated by a NEWER version of the "
                f"application than the one now running. Deploy the matching version, or "
                f"downgrade the schema to {sorted(heads)}."
            ),
        ))
    else:
        issues.append(ValidationIssue(
            check="db_schema_ahead_of_code",
            severity="high",
            detail=(
                f"The database is at revision {sorted(current)}, ahead of this code's "
                f"{sorted(heads)}. Expected during a rollback; the extra migrations may "
                f"have dropped or renamed columns this version still selects."
            ),
        ))

    return issues


def validate_hostility_tier_config() -> list[ValidationIssue]:
    """S1 -- minimal hostility-tier config validator for the H3 (externally
    exposed / partner-facing) A2A boundary. Checks that the mandatory
    per-tier limits (max request size, rate limiting) are set to sane,
    non-zero values.

    This is deliberately narrow (A2A is the one interface with a fully
    externalized config surface today) rather than attempting to enumerate
    every interface in the platform in one pass; the validator is meant to
    be extended as each additional H1/H2/H3 boundary is classified."""
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
                "application-layer rate limit (relying on the gateway alone "
                "for this is not permitted)."
            ),
        ))

    return issues


def validate_precert_engine_tls() -> list[ValidationIssue]:
    """Refuse to run the precert engine without a CA certificate.

    `NfiniteConnector` dials `precert_engine_precert_url` over HTTPS to drive
    the network test transactions. The old `precert_engine_verify_peers=false` escape
    hatch (CERT_NONE) was removed (CWE-295) -- verification is now
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
    """Make a broken reranker LOUD.

    WHY THIS EXISTS. torch and sentence-transformers were removed from this
    image on 2026-08-28 (six dependency vulnerabilities, including a CVSS 9.8) and the model
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
    """CVE-2025-45768 (PyJWT, CVSS 6.3) -- prove the HMAC
    signing key is strong.

    WHY THIS EXISTS. The CVE says PyJWT does not enforce a minimum key length
    for HMAC signing, so an application with a weak secret mints weak tokens.
    It is widely DISPUTED (the maintainers hold that key strength is the
    application's job) and it has NO FIXED VERSION -- so no upgrade can clear
    it. The only way to close the finding honestly is to demonstrate that this
    application does the thing the CVE says the library fails to do. This
    function is that demonstration, and it is the control the project's VEX
    statement for this CVE points at.

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

    # cert_agent_internal_token used to default to a
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
        ("partner_agent_url", "partner-agent traffic"),
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


def validate_partner_tls_verification() -> list[ValidationIssue]:
    """No ACTIVE partner may have TLS certificate verification disabled in
    production.

    `services/a2a_client.partner_verify()` returns `verify=False` when a partner
    row sets `ssl_verify=False`, or when the global `partner_tls_verify` is off.
    Both default secure and a SECURITY_EVENT is logged once per partner — but
    nothing refused to BOOT on the combination, unlike the analogous
    `validate_precert_engine_tls` beside it. An unverified TLS connection to a
    partner is one machine-in-the-middle away from disclosing the A2A bearer
    token and the payload, so in production this is critical, not advisory.

    Non-production is untouched: local partners with self-signed certificates
    are the reason the flag exists.
    """
    from app.core.config import settings

    if not _is_production(settings):
        return []

    issues: list[ValidationIssue] = []
    if not getattr(settings, "partner_tls_verify", True):
        issues.append(ValidationIssue(
            check="partner_tls_verify_disabled_in_production",
            severity="critical",
            detail=("PARTNER_TLS_VERIFY=false with APP_ENV=production. Every outbound "
                    "A2A call would skip certificate verification, exposing the bearer "
                    "token and payload to a machine-in-the-middle. Remove the override."),
        ))

    try:
        from app.core.database import SessionLocal
        from app.models.phase_c import PartnerAgent, PartnerStatus

        db = SessionLocal()
        try:
            offenders = [
                p.name for p in db.query(PartnerAgent)
                .filter(PartnerAgent.status == PartnerStatus.ACTIVE)
                .filter(PartnerAgent.ssl_verify.is_(False)).all()
            ]
        finally:
            db.close()
        if offenders:
            issues.append(ValidationIssue(
                check="active_partner_ssl_verify_disabled_in_production",
                severity="critical",
                detail=(f"ACTIVE partner(s) with ssl_verify=false in production: "
                        f"{', '.join(sorted(offenders))}. Certificate verification is "
                        "skipped for their A2A traffic. Install the partner's CA or "
                        "correct the certificate, then re-enable verification."),
            ))
    except Exception as exc:  # noqa: BLE001
        # Same posture as the other DB-touching checks: an unreadable table is
        # reported, never a silent pass.
        issues.append(ValidationIssue(
            check="active_partner_ssl_verify_check_failed",
            severity="high",
            detail=f"Could not verify partner TLS settings: {exc}",
        ))
    return issues


def run_all(fail_fast: bool = True) -> list[ValidationIssue]:
    """Run every registered startup validation check. Returns the full list
    of issues found (across all checks, all severities). When `fail_fast`
    is True and any issue has severity "critical", raises
    `StartupValidationError` after logging every issue found (not just the
    first) so a single restart surfaces the whole remediation list at once."""
    issues: list[ValidationIssue] = []
    # Runs FIRST: a schema behind the code makes every other DB-touching check
    # unreliable (A7 below cannot query PartnerAgent at all in that state), so
    # the drift should be the finding an operator sees, not a downstream
    # symptom of it.
    issues += validate_schema_up_to_date()
    issues += _check_active_partners_have_hmac_secret()
    issues += validate_hostility_tier_config()
    issues += validate_precert_engine_tls()
    issues += validate_jwt_key_strength()
    issues += validate_reranker_backend()
    issues += validate_encryption_keys()
    issues += validate_hmac_fail_open()
    issues += validate_http_defaults()
    issues += validate_partner_tls_verification()

    for issue in issues:
        level = {"critical": logger.error, "high": logger.warning}.get(issue.severity, logger.info)
        level("STARTUP_VALIDATION [%s] %s: %s", issue.severity, issue.check, issue.detail)

    if fail_fast:
        criticals = [i for i in issues if i.severity == "critical"]
        if criticals:
            summary = "; ".join(f"{i.check}: {i.detail}" for i in criticals)
            raise StartupValidationError(
                f"{len(criticals)} critical startup validation failure(s) -- refusing to "
                f"start insecurely: {summary}"
            )
    return issues