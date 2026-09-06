# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

import logging
import threading
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
from app.core import log_buffer, diag
from app.api.router import api_router

# Install in-memory log buffer before anything else emits log records
log_buffer.install(level=logging.DEBUG)
# Dedicated, findable, fail-open diagnostics files (code-gen + build).
diag.install()

logger = logging.getLogger("app")
_kb_startup_thread: threading.Thread | None = None
_excel_engine_registered = False


def _settings_bool(name: str, default: bool = False) -> bool:
    """Read a bool setting safely after env/DB overrides.

    WHY this helper exists:
    DB-backed config rows arrive as strings, so a literal "false" would be
    truthy if we used `if settings.some_flag:` directly. Startup flags decide
    whether expensive/fail-fast work runs, so string booleans must be parsed.
    """
    value = getattr(settings, name, default)
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _register_excel_testcase_engine_once() -> None:
    """Register Excel Testcase Engine after config overrides are loaded.

    WHY this runs from startup, not module import:
    DB config overrides are applied in `on_startup()`. Registering here lets
    `excel_engine_enabled=false` from either env or DB config actually disable
    the fail-fast engine wiring before the app starts serving traffic.
    """
    global _excel_engine_registered
    if _excel_engine_registered:
        return

    if not _settings_bool("excel_engine_enabled", True):
        logger.info("Excel Testcase Engine disabled by settings.excel_engine_enabled=false.")
        return

    try:
        from app.excel_testcase_engine import register_excel_testcase_engine
        from app.core.llm import call_llm, stream_llm
        from app.services import job_registry as _job_registry_module
        from app.core.database import SessionLocal as _SessionLocal

        # WHY under ARTIFACTS_DIR: docker-compose mounts ./artifacts at
        # /app/artifacts. Storing workbooks here keeps XLSX/DOCX/MD downloads
        # alive across container restarts/recreates.
        excel_root = Path(settings.artifacts_dir or "/app/artifacts") / "excel_engine"
        register_excel_testcase_engine(
            app,
            llm={"stream": stream_llm, "call": call_llm},
            job_registry=_job_registry_module,
            db_session_factory=_SessionLocal,
            artifacts_dir=excel_root / "artifacts",
            outputs_dir=excel_root / "workbooks",
        )
        _excel_engine_registered = True
        logger.info("Excel Testcase Engine registered.")
    except Exception as e:
        # Fail fast when the feature is enabled. Otherwise the UI can expose
        # Excel generation while the required endpoint/package is missing.
        logger.exception("Excel Testcase Engine registration failed: %s", e)
        raise RuntimeError(
            "Excel Testcase Engine registration failed. "
            "Fix the engine wiring or set EXCEL_ENGINE_ENABLED=false to disable it."
        ) from e


def _run_startup_kb_work():
    """Run KB ingest + BM25 build off the request-serving startup path.

    WHY this runs in a background thread:
    synchronous startup ingestion makes the ASGI app appear hung until the
    whole knowledge_base corpus finishes parsing, embedding, and indexing.
    Moving it off the startup event lets the UI load immediately while the
    corpus catches up in the background.
    """
    try:
        from app.core.database import SessionLocal
        from app.rag.ingestion import ingest_all
        from app.rag import bm25_search

        db = SessionLocal()
        try:
            if _settings_bool("auto_ingest_knowledge_base_on_startup", True):
                summary = ingest_all(db, force=_settings_bool("startup_ingest_force", False))
                logger.info(
                    "Background startup KB ingest complete: processed=%d updated=%d skipped=%d errors=%d chunks=%d orphans_removed=%d",
                    summary.get("processed", 0),
                    summary.get("updated", 0),
                    summary.get("skipped", 0),
                    summary.get("errors", 0),
                    summary.get("chunks_created", 0),
                    summary.get("orphans_removed", 0),
                )
            else:
                logger.info("Startup KB ingest skipped: auto_ingest_knowledge_base_on_startup=false")

            count = bm25_search.build_index(db)
            logger.info("BM25 index ready: %d chunks", count)
        finally:
            # Explicit rollback before close. Without this, if any of the
            # above queries failed (e.g. schema drift on document_chunks
            # missing a column), the underlying connection goes back to
            # the pool in a failed-transaction state and poisons every
            # subsequent session that checks it out — surfaces as
            # InFailedSqlTransaction on unrelated endpoints like the
            # negotiation auto-draft.
            try:
                db.rollback()
            except Exception:
                pass
            db.close()
    except Exception as e:
        logger.warning(
            "Startup KB ingest / BM25 build failed (hybrid search may degrade to vector-only): %s",
            e,
        )

_is_prod = settings.app_env == "production"

app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    docs_url=None if _is_prod else "/api/docs",
    redoc_url=None if _is_prod else "/api/redoc",
    openapi_url=None if _is_prod else "/api/openapi.json",
)

# Tag LLM usage with the change being served, for the whole lifecycle (prompt enhancement
# onward) — see app.core.observability.UsageContextMiddleware.
from app.core.observability import UsageContextMiddleware
app.add_middleware(UsageContextMiddleware)

# S1 hostility-tier follow-up — throttle /api/admin/* by caller identity.
# No-ops unless admin_rate_limit_enabled=true (see app.core.admin_rate_limit).
from app.core.admin_rate_limit import AdminRateLimitMiddleware
app.add_middleware(AdminRateLimitMiddleware)

# T8 (THREAT_MODEL.md) — record every successful mutating request that passed
# an admin authorization check, so audit coverage does not depend on each
# endpoint remembering an explicit admin_action_audit.record() call. Keyed on
# the marker require_admin() sets (NOT a URL prefix), because admin-gated
# routes also live outside /api/admin/* — see the module docstring. Fail-open:
# an audit write failure never affects the response.
from app.core.admin_action_audit_middleware import AdminActionAuditMiddleware
app.add_middleware(AdminActionAuditMiddleware)

# Finding #8 (architecture review) — read-through rehydration for artifact
# columns whose content has been moved to compressed cold storage and nulled to
# reclaim database space. Registered here (not lazily) so the ORM 'load' hook is
# attached before any request can read a tiered row. No-op for rows whose column
# is non-NULL, which is every row until artifact_coldstore_null_source is on.
from app.services.artifact_coldstore_read import register_coldstore_read_through
register_coldstore_read_through()

# HSTS header (F-003) — instruct browsers to always use HTTPS for this
# domain. Set in production only (dev serves plain HTTP). The max-age of
# one year (31536000s) is the OWASP-recommended minimum; includeSubDomains
# covers all services under the same domain. This is defence-in-depth:
# nginx should also set this header, but the app layer ensures it is
# present even when the proxy configuration is missed.

@app.middleware("http")
async def add_hsts_header(request: Request, call_next):
    response = await call_next(request)
    if _is_prod:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response

# Clickjacking defence-in-depth (SCR finding #16). frontend/nginx.conf.template
# already sets these on the SPA's own responses, but that is a single config
# file the edge proxy must remember to load — it was accidentally deleted from
# this repo once already (restored in the same change that added this
# middleware). Setting them here too means the header survives even if the
# proxy config is ever missing or misconfigured again. Unconditional (not
# prod-only, unlike HSTS) since framing is a risk in every environment.
@app.middleware("http")
async def add_frame_protection_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    return response

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_url],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # Expose the sliding-refresh header so the browser-side axios
    # interceptor can read it cross-origin (dev mode hits the backend
    # on a different port; in prod nginx is same-origin and this is a
    # no-op).
    expose_headers=["X-Refresh-Token"],
)

app.include_router(api_router, prefix="/api")

# Dev-only unified activity dashboard (app._devlog is git-ignored; this is a
# no-op when the package is absent, e.g. fresh clone / prod image).
try:
    from app._devlog import install as _devlog_install
    _devlog_install(app)
except Exception:
    pass


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=500, content={"detail": "An internal error occurred"})


# ── A2A SDK mount (Slice 3) ──────────────────────────────────────────────────
# Native JSON-RPC endpoint at /a2a-rpc/rpc + agent card at the SDK's standard
# /.well-known paths. Runs ALONGSIDE the legacy POST /api/a2a/tasks/send
# router (above) until Slice 6 flips registered partners over via the
# `protocol_version` field on PartnerAgent. Decommission lands in Slice 8.
#
# Lazy-imported so a missing `a2a-sdk` install (e.g. in trimmed CI envs)
# does not block app startup — the SDK path is unused until partners opt in.
try:
    from app.a2a_common import build_a2a_components
    from app.a2a_common.authority_card import AUTHORITY_AGENT_CARD
    from app.a2a_common.authority_executor import AuthorityAgentExecutor
    # Slice 2 of the security hardening — Bearer JWT validation +
    # A2ASession revocation check before the SDK handler runs.
    from app.a2a_common.sdk_auth_middleware import SdkAuthMiddleware
    # Slice 5 — HMAC envelope verification (X-NPCI-Signature etc.).
    # Wrapped OUTSIDE the auth middleware so the body buffer + HMAC
    # check happens before JWT decode.
    from app.a2a_common.sdk_hmac_middleware import SdkHmacMiddleware
except Exception as _a2a_import_err:  # noqa: BLE001
    logger.warning("a2a-sdk not importable; skipping /a2a-rpc mount: %s", _a2a_import_err)
else:
    try:
        _a2a_sub_app, _a2a_card_routes = build_a2a_components(
            agent_card=AUTHORITY_AGENT_CARD,
            executor=AuthorityAgentExecutor(),
            # In-memory store for now — Slice 5 of the SDK refactor swaps
            # in get_task_store() so Tasks survive worker restarts.
            task_store=None,
            rpc_url="/rpc",
            auth_middleware=SdkAuthMiddleware,
            hmac_middleware=SdkHmacMiddleware,
        )
        app.mount("/a2a-rpc", _a2a_sub_app)
        for _r in _a2a_card_routes:
            app.add_route(_r.path, _r.endpoint, methods=list(_r.methods))
        logger.info(
            "A2A SDK mount active: /a2a-rpc/rpc "
            "(HMAC envelope + Bearer JWT enforced via Slice 5 + Slice 2 middlewares)"
        )
    except Exception as _a2a_mount_err:  # noqa: BLE001
        logger.exception("A2A SDK mount failed; legacy path still serves: %s", _a2a_mount_err)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log every HTTP request/response through the buffer.

    URL paths are sanitised to remove resource identifiers (UUIDs, numeric IDs)
    before logging, preventing sensitive resource IDs from leaking into logs
    (F-004). The sanitised form shows the route pattern (e.g. /api/partners/{id})
    rather than the actual identifier value.
    """
    response = await call_next(request)
    sanitised_path = _sanitise_path_for_log(request.url.path)
    logger.info("%s %s → %s", request.method, sanitised_path, response.status_code)
    return response


def _sanitise_path_for_log(path: str) -> str:
    """Replace UUIDs and numeric IDs in URL paths with {id} placeholders.

    This prevents sensitive resource identifiers (partner IDs, user IDs,
    change request IDs, etc.) from leaking into application logs while
    preserving the route structure for monitoring and debugging.

    Examples:
        /api/partners/a1b2c3d4-e5f6-... -> /api/partners/{id}
        /api/users/42 -> /api/users/{id}
        /api/change_requests/123 -> /api/change_requests/{id}
    """
    import re as _re
    # Replace UUIDs (hex with hyphens)
    path = _re.sub(r'/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', '/{id}', path, flags=_re.I)
    # Replace standalone numeric IDs (not part of a version like v1, v2)
    path = _re.sub(r'/(\d+)(?=/|$)', '/{id}', path)
    return path


# CSRF guard for cookie-authenticated state-changing requests. Required
# alongside the session cookie: a cookie is attached by the browser
# automatically, so without this (plus SameSite=Strict on the cookie itself)
# moving off Bearer headers would trade an XSS hole for a CSRF one. Bearer
# callers are exempt — a cross-site page cannot set that header.
# See app.core.csrf for the full rationale.
from app.core.csrf import csrf_origin_middleware
app.middleware("http")(csrf_origin_middleware)


@app.middleware("http")
async def sliding_token_refresh(request: Request, call_next):
    """Slide an operator session forward when its token is past 50% of its TTL.

    WHY: operator tokens are 8h by default. An operator who keeps Agent
    Messaging open across a workday hits expiry mid-session; the next
    mutation 401s, the api.js silent probe to /auth/me also 401s
    (genuinely-dead token), and they get bounced to /login. This
    middleware slides the expiry forward on every authenticated request
    so an active session never expires under the user. Idle sessions
    still expire after the full TTL.

    HOW THE REFRESH IS DELIVERED depends on how the caller authenticated:

    * Cookie session (browsers) → a refreshed ``Set-Cookie``. The browser
      swaps it silently, so there is no client-side refresh code to write and
      nothing for a new call site to forget. This is the path the SPA uses.
    * Bearer header (non-browser callers) → the ``X-Refresh-Token`` response
      header, exactly as before. Kept so scripts and any not-yet-migrated
      client keep sliding rather than hard-expiring at 8h.

    Only fires on 2xx responses — we never want to leak a refreshed
    token alongside a 401/403. Only refreshes OPERATOR tokens (`type`
    claim absent or non-A2A); A2A partner tokens (`type=a2a`/
    `a2a_refresh`) have their own /a2a/auth/refresh path and are
    skipped.
    """
    response = await call_next(request)

    if not (200 <= response.status_code < 300):
        return response

    from app.core.session_cookie import COOKIE_NAME, set_session_cookie

    # Resolve the inbound credential and remember WHICH scheme carried it, so
    # the refresh goes back the same way it came in.
    cookie_token = request.cookies.get(COOKIE_NAME)
    if cookie_token:
        token, via_cookie = cookie_token, True
    else:
        auth_header = request.headers.get("authorization", "")
        if not auth_header.lower().startswith("bearer "):
            return response
        token, via_cookie = auth_header[7:].strip(), False

    if not token:
        return response

    try:
        from datetime import datetime, timezone
        import jwt
        from app.core.security import ALGORITHM, create_access_token

        claims = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
        # Partner A2A tokens have their own refresh endpoint; do not
        # mint a platform-secret operator token for them.
        if claims.get("type") in ("a2a", "a2a_refresh"):
            return response

        exp = claims.get("exp")
        sub = claims.get("sub")
        ttl_minutes = settings.access_token_expire_minutes
        if not (exp and sub) or ttl_minutes <= 0:
            return response

        remaining_s = exp - datetime.now(timezone.utc).timestamp()
        if remaining_s <= 0:
            return response

        if remaining_s < (ttl_minutes * 60) / 2:
            fresh = create_access_token(sub)
            if via_cookie:
                set_session_cookie(response, fresh)
            else:
                response.headers["X-Refresh-Token"] = fresh
    except Exception:  # noqa: BLE001
        # Decode failure / unexpected error: do not block the response.
        # Auth enforcement is handled by the route's CurrentUser dep,
        # not here. Catching bare Exception is deliberate and unchanged in
        # effect: every JWT error subclasses it, so the old
        # `(JWTError, Exception)` tuple only ever matched on Exception.
        pass

    return response


def _seed_authority_policy_from_file() -> None:
    """First-boot seed of the policy singleton row from the bind-mounted
    file at `settings.authority_policy_path`. No-op if the table already has a row.

    The file is the demo / bootstrap source; the DB row is the runtime source
    of truth once seeded. Admins maintain the row via the Admin UI thereafter.
    Re-seeding (after a wipe) just requires deleting the row and restarting.
    """
    from app.core.database import SessionLocal
    from app.models.authority_policy import AuthorityPolicy

    path = Path(settings.authority_policy_path)
    db = SessionLocal()
    try:
        existing = db.get(AuthorityPolicy, 1)
        if existing is not None:
            return
        if not path.exists():
            logger.info(
                "the Authority policy seed file missing at %s — leaving row empty; "
                "admin can paste content into the Admin UI",
                path,
            )
            content = ""
        else:
            content = path.read_text(encoding="utf-8")
        db.add(AuthorityPolicy(id=1, content=content))
        db.commit()
        logger.info(
            "Seeded the policy singleton from %s (%d bytes)",
            path if path.exists() else "<empty>", len(content),
        )
    finally:
        db.close()


@app.on_event("startup")
async def on_startup():
    # Re-apply the uvicorn logger alignment. log_buffer.install() already did
    # this at import time, but uvicorn runs its OWN dictConfig when the server
    # boots — i.e. AFTER this module is imported — which re-attaches its
    # hardcoded handlers (uvicorn.error → stderr, uvicorn.access → stdout,
    # propagate=False). Doing it again here, once the server is up, is what
    # actually makes uvicorn's lines follow the stdout/stderr severity split.
    log_buffer.align_uvicorn_loggers()

    # Load DB config overrides into in-memory settings. Only schema-managed
    # (operator-tunable / secret) keys may override runtime settings — infra and
    # feature-flag keys are owned by .env and must never be shadowed by a stray
    # DB row. Secrets are decrypted; all values are coerced to their field type.
    # The same loader runs on Celery worker init (see celery_tasks.py).
    from app.core.app_config_sync import load_db_overrides
    load_db_overrides()

    # A7 / S1 — fail-fast security config validation (security_architecture_
    # skills.md §4.3). Runs AFTER load_db_overrides() so an operator's
    # Admin-UI-set values are what gets validated, not just .env defaults.
    # Uncaught StartupValidationError aborts ASGI startup — the platform
    # will not begin serving traffic in a known-insecure configuration
    # (e.g. an ACTIVE partner with no HMAC secret, or the H3 A2A boundary
    # missing its mandatory body-size/rate-limit config).
    from app.core.startup_validation import run_all as _run_startup_validation
    _run_startup_validation(fail_fast=True)

    # Effective LLM/cache config AFTER .env + DB overrides — the value the running
    # process actually uses (a fresh `python -c` import sees .env only, not overrides).
    logger.info("LLM config: provider=%s model=%s prompt_cache_enabled=%r",
                settings.llm_provider, settings.claude_model, settings.prompt_cache_enabled)

    # Fail any agent_jobs row still marked running/pending. Every job in the
    # registry is executed inside THIS process (WS handlers + FastAPI
    # BackgroundTasks; celery only runs the periodic sweep), so a job that is
    # "active" at boot belonged to a process that no longer exists. Without
    # this, a backend restart mid-generation leaves a zombie the UI shows as
    # in-progress until the 30-minute idle sweep catches it.
    try:
        from app.core.database import SessionLocal
        from app.services.job_registry import sweep_orphan_jobs
        db = SessionLocal()
        try:
            n = sweep_orphan_jobs(
                db, max_idle_minutes=0,
                reason="Backend restarted while this job was running — re-run it.",
            )
            if n:
                logger.warning(
                    "Startup sweep: failed %d job(s) orphaned by the previous process", n,
                )
        finally:
            db.close()
    except Exception as e:
        logger.warning("Startup orphan-job sweep failed: %s", e)

    # Same reasoning for Phase B script runs: the build/UAT scripts execute as
    # in-process asyncio tasks, so a QUEUED/RUNNING row at boot is a zombie
    # from the previous process — fail it now instead of letting it block
    # re-triggers until the staleness window expires.
    try:
        from app.core.database import SessionLocal
        from app.services.phase_b_recovery import sweep_orphan_script_runs
        db = SessionLocal()
        try:
            n = sweep_orphan_script_runs(db)
            if n:
                logger.warning(
                    "Startup sweep: failed %d Phase B script run(s) orphaned by the previous process", n,
                )
        finally:
            db.close()
    except Exception as e:
        logger.warning("Startup Phase B script-run sweep failed: %s", e)

    _register_excel_testcase_engine_once()

    # Seed the policy singleton from the bind-mounted file on first
    # boot. After this, admins maintain content through the Admin UI;
    # the file is only consulted again if the row is wiped.
    try:
        _seed_authority_policy_from_file()
    except Exception as e:
        logger.warning("the Authority policy seeding skipped: %s", e)

    # Phase 3 Gap D — fire one cheap embed call so Ollama loads the model
    # into VRAM before the first user-facing request arrives. Background
    # thread so a slow / unreachable embedder doesn't block startup. Skips
    # if the operator sets `embed_warmup_on_startup=false`.
    if _settings_bool("embed_warmup_on_startup", True):
        try:
            from app.rag.embeddings import warm_up as _embed_warm_up
            threading.Thread(
                target=_embed_warm_up,
                kwargs={"timeout_sec": 10.0},
                name="embed-warmup",
                daemon=True,
            ).start()
            logger.info("Embed warmup scheduled in background thread.")
        except Exception as e:
            logger.warning("Could not schedule embed warmup: %s", e)

    # Kick off KB ingest/BM25 work after startup rather than blocking readiness.
    global _kb_startup_thread
    if _settings_bool("startup_ingest_background", True):
        _kb_startup_thread = threading.Thread(
            target=_run_startup_kb_work,
            name="startup-kb-ingest",
            daemon=True,
        )
        _kb_startup_thread.start()
        logger.info("Startup KB ingest scheduled in background thread.")
    else:
        _run_startup_kb_work()

    logger.info("AtOM A2A Platform started — log buffer active")


@app.get("/api/health")
def health_check():
    return {"status": "ok", "app": settings.app_name}


# ── A2A Agent Card (served at well-known URI, outside /api prefix) ────────────

@app.get("/.well-known/agent.json")
def agent_card():
    from app.api.a2a import get_agent_card
    return get_agent_card()
