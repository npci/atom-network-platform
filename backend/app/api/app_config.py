# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Admin App Configuration API — manage platform settings from the UI."""
import logging

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from app.core.deps import DbDep, AdminUser
from app.core.config import settings
from app.core.app_config_sync import (
    encrypt_secret,
    decrypt_secret,
    is_encrypted,
    set_live_setting,
)
from app.models.app_config import AppConfig
from app.models.base import utcnow

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/config", tags=["app-config"])

# Define all configurable settings with their categories and metadata
CONFIG_SCHEMA = [
    # AI Provider
    {"key": "llm_provider", "category": "ai", "label": "AI Provider", "is_secret": False,
     "placeholder": "claude", "description": "LLM provider: 'claude' (Anthropic), 'openai' (OpenAI), 'ainxt' (the Authority AiNxt Gateway), 'ollama' (local), or 'gemini' (Google AI Studio). Determines which API is used for all AI generation."},

    # Claude
    {"key": "anthropic_api_key", "category": "ai", "label": "Claude API Key", "is_secret": True,
     "placeholder": "sk-ant-...", "description": "Anthropic Claude API key (required if provider is 'claude')"},
    {"key": "claude_model", "category": "ai", "label": "Claude Model", "is_secret": False,
     "placeholder": "claude-sonnet-5", "description": "Claude model ID (e.g., claude-sonnet-5, claude-opus-4-8)"},

    # OpenAI
    {"key": "openai_api_key", "category": "ai", "label": "OpenAI API Key", "is_secret": True,
     "placeholder": "sk-...", "description": "OpenAI API key (required if provider is 'openai')"},
    {"key": "openai_model", "category": "ai", "label": "OpenAI Model", "is_secret": False,
     "placeholder": "gpt-4o", "description": "OpenAI model ID (e.g., gpt-4o, gpt-4o-mini, o1)"},

    # Gemini / Google AI Studio
    {"key": "gemini_api_key", "category": "ai", "label": "Gemini API Key", "is_secret": True,
     "placeholder": "AIza...", "description": "Google AI Studio / Gemini API key (required if provider is 'gemini')"},
    {"key": "gemini_model", "category": "ai", "label": "Gemini Model", "is_secret": False,
     "placeholder": "gemini-3.5-flash", "description": "Gemini model ID (e.g., gemini-3.5-flash, gemini-3.1-flash-lite, gemini-2.0-flash)"},
    {"key": "gemini_thinking_level", "category": "ai", "label": "Gemini Thinking Level", "is_secret": False,
     "placeholder": "minimal", "description": "Gemini 3.x thinking level (e.g., minimal, low, medium, high). Use minimal for low-latency local harness testing."},
    {"key": "gemini_thinking_budget", "category": "ai", "label": "Gemini Thinking Budget", "is_secret": False,
     "placeholder": "0", "description": "Gemini 2.5 thinking budget. 0 disables thinking for fast local testing; -1 enables dynamic thinking."},

    # AiNxt (the Authority internal gateway — OpenAI-compatible)
    {"key": "ainxt_base_url", "category": "ai", "label": "AiNxt Base URL", "is_secret": False,
     "placeholder": "https://gateway.example.com/v1/api", "description": "AiNxt gateway base URL (OpenAI-compatible API). Required if provider is 'ainxt'."},
    {"key": "ainxt_api_key", "category": "ai", "label": "AiNxt API Key", "is_secret": True,
     "placeholder": "", "description": "API key for the AiNxt gateway (required if provider is 'ainxt')"},
    {"key": "ainxt_model", "category": "ai", "label": "AiNxt Model", "is_secret": False,
     "placeholder": "gpt-4o", "description": "Model ID available on AiNxt gateway (e.g., gpt-4o, gpt-4, llama-3)"},
    {"key": "ainxt_compat_mode", "category": "ai", "label": "AiNxt Compat Mode", "is_secret": False,
     "placeholder": "openai", "description": "AiNxt API surface: 'openai' (OpenAI-compatible /chat/completions) or 'anthropic' (/v1/messages). Use 'anthropic' when the gateway serves Claude models via the Anthropic-compat path."},
    {"key": "ainxt_messages_model", "category": "ai", "label": "AiNxt Messages Model", "is_secret": False,
     "placeholder": "claude-sonnet-5", "description": "Model ID used on the AiNxt anthropic-compat (/v1/messages) path when AiNxt Compat Mode is 'anthropic'."},

    # NOTE: ollama_url is intentionally NOT here — it's an infrastructure
    # endpoint owned by .env / docker-compose (compose overrides it to the
    # in-network `ollama` host). Managing it here would make it triple-sourced.

    # Deep Research stage — per-stage model override (independent of the
    # platform's default LLM). Lets you point research at a frontier model
    # like GPT-5.4 while keeping BRD/TSD/Product Kit on Claude Sonnet 4.6.
    {"key": "deep_research_provider", "category": "ai", "label": "Deep Research Provider", "is_secret": False,
     "placeholder": "openai",
     "description": "Provider for the Deep Research stage only ('claude', 'openai', 'ainxt', 'ollama'). Leave blank to inherit the default AI Provider above. Defaults to 'openai' (paired with GPT-5.4)."},
    {"key": "deep_research_model", "category": "ai", "label": "Deep Research Model", "is_secret": False,
     "placeholder": "gpt-5.4",
     "description": "Model ID for the Deep Research stage only. Default: 'gpt-5.4' (frontier model optimised for research / agentic tasks). Leave blank to inherit the per-provider default model."},

    # GitLab
    {"key": "gitlab_url", "category": "gitlab", "label": "GitLab URL", "is_secret": False,
     "placeholder": "http://localhost:8080", "description": "GitLab instance URL"},
    {"key": "gitlab_token", "category": "gitlab", "label": "GitLab Access Token", "is_secret": True,
     "placeholder": "glpat-...", "description": "Personal Access Token with api scope"},
    {"key": "gitlab_repo", "category": "gitlab", "label": "Default Repository", "is_secret": False,
     "placeholder": "root/network-platform", "description": "Default GitLab project path"},
    {"key": "gitlab_branch", "category": "gitlab", "label": "Default Branch", "is_secret": False,
     "placeholder": "main", "description": "Default branch for code operations"},

    # Email / SMTP
    {"key": "smtp_host", "category": "email", "label": "SMTP Host", "is_secret": False,
     "placeholder": "smtp.yourserver.com", "description": "SMTP server hostname"},
    {"key": "smtp_port", "category": "email", "label": "SMTP Port", "is_secret": False,
     "placeholder": "587", "description": "SMTP server port"},
    {"key": "smtp_user", "category": "email", "label": "SMTP Username", "is_secret": False,
     "placeholder": "noreply@npci.org.in", "description": "SMTP authentication username"},
    {"key": "smtp_password", "category": "email", "label": "SMTP Password", "is_secret": True,
     "placeholder": "", "description": "SMTP authentication password"},
    {"key": "email_from", "category": "email", "label": "From Address", "is_secret": False,
     "placeholder": "noreply@npci.org.in", "description": "Sender email address"},

    # Jenkins
    {"key": "jenkins_url", "category": "jenkins", "label": "Jenkins URL", "is_secret": False,
     "placeholder": "http://localhost:8081", "description": "Jenkins CI server URL"},
    {"key": "jenkins_user", "category": "jenkins", "label": "Jenkins Username", "is_secret": False,
     "placeholder": "admin", "description": "Jenkins authentication username"},
    {"key": "jenkins_token", "category": "jenkins", "label": "Jenkins API Token", "is_secret": True,
     "placeholder": "", "description": "Jenkins API token for authentication"},
    {"key": "jenkins_job_name", "category": "jenkins", "label": "Jenkins Job Name", "is_secret": False,
     "placeholder": "network-build", "description": "Default Jenkins job to trigger"},

    # UAT Server
    {"key": "uat_server_host", "category": "uat", "label": "UAT Server Host", "is_secret": False,
     "placeholder": "192.168.1.100", "description": "UAT server hostname or IP"},
    {"key": "uat_server_user", "category": "uat", "label": "SSH Username", "is_secret": False,
     "placeholder": "deploy", "description": "SSH user for UAT deployment"},
    {"key": "uat_health_check_url", "category": "uat", "label": "Health Check URL", "is_secret": False,
     "placeholder": "http://uat-server:8080/actuator/health", "description": "URL to verify deployment health"},

    # External UI links — surfaced from the sidebar and other UI affordances.
    {"key": "authority_simulator_url", "category": "ui", "label": "the Authority Simulator URL", "is_secret": False,
     "placeholder": "http://localhost:5173",
     "description": "URL the sidebar's 'the Authority Simulator' button opens. Override for host-mode / staging / prod deployments where 5173 isn't reachable from operator browsers."},

    # Video generation (promo / explainer). Provider is chosen by video_provider;
    # each provider has its own model + endpoint. Gemini video reuses the Gemini
    # API key above (no separate video key).
    {"key": "video_provider", "category": "video", "label": "Video Provider", "is_secret": False,
     "placeholder": "ainxt", "description": "Video generation provider: 'ainxt', 'gemini', 'grok', or 'mock'."},
    {"key": "video_model", "category": "video", "label": "Video Model (AiNxt)", "is_secret": False,
     "placeholder": "veo-3.1-generate-preview", "description": "Model ID used for the AiNxt video path."},
    {"key": "gemini_video_model", "category": "video", "label": "Gemini Video Model", "is_secret": False,
     "placeholder": "veo-3.1-generate-preview", "description": "Model ID for the Gemini (Veo) video provider."},
    {"key": "gemini_video_base_url", "category": "video", "label": "Gemini Video Base URL", "is_secret": False,
     "placeholder": "https://generativelanguage.googleapis.com/v1beta", "description": "Base URL for the Gemini video API."},
    {"key": "grok_api_key", "category": "video", "label": "Grok API Key", "is_secret": True,
     "placeholder": "xai-...", "description": "xAI Grok API key (required if video provider is 'grok')."},
    {"key": "grok_base_url", "category": "video", "label": "Grok Base URL", "is_secret": False,
     "placeholder": "https://api.x.ai/v1", "description": "Base URL for the xAI Grok API."},
    {"key": "grok_video_model", "category": "video", "label": "Grok Video Model", "is_secret": False,
     "placeholder": "grok-imagine-video", "description": "Model ID for the Grok video provider."},

    # Per-purpose LLM routing model overrides (cost reduction). Only take effect
    # when USE_LLM_ROUTING is enabled (that toggle stays in .env as a feature flag).
    {"key": "routing_model_routing", "category": "routing", "label": "Routing Model (router)", "is_secret": False,
     "placeholder": "claude-haiku-4-5-20251001", "description": "Cheap/fast model for router/classification-style agent calls."},
    {"key": "routing_model_utility", "category": "routing", "label": "Routing Model (utility)", "is_secret": False,
     "placeholder": "claude-haiku-4-5-20251001", "description": "Model for utility/low-stakes agent calls."},
    {"key": "code_summarizer_model", "category": "routing", "label": "Code Summarizer Model", "is_secret": False,
     "placeholder": "claude-haiku-4-5-20251001", "description": "Model used by the code-RAG summarizer. Blank inherits the routing/default model."},
]

# Build lookup
_SCHEMA_MAP = {c["key"]: c for c in CONFIG_SCHEMA}


def _mask_secret(value: str) -> str:
    """Mask a secret value for display — show first 4 and last 4 chars."""
    if not value or len(value) <= 10:
        return "****" if value else ""
    return value[:4] + "****" + value[-4:]


def _get_env_value(key: str) -> str:
    """Get the current value from environment/settings."""
    return getattr(settings, key, "") or ""


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("")
def get_all_config(db: DbDep, _: AdminUser):
    """Get all configuration values grouped by category."""
    # Load DB overrides
    db_configs = {c.key: c for c in db.scalars(select(AppConfig)).all()}

    result = {}
    for schema in CONFIG_SCHEMA:
        key = schema["key"]
        cat = schema["category"]
        if cat not in result:
            result[cat] = []

        # DB value takes precedence, fall back to env
        db_row = db_configs.get(key)
        raw_value = db_row.value if db_row else _get_env_value(key)
        # Secrets are stored encrypted in the DB — decrypt so masking / has_value
        # reflect the real value (never returned to the client; masked below).
        if schema["is_secret"] and db_row and is_encrypted(raw_value):
            raw_value = decrypt_secret(raw_value)
        display_value = _mask_secret(raw_value) if schema["is_secret"] else raw_value

        result[cat].append({
            "key": key,
            "label": schema["label"],
            "value": display_value,
            "has_value": bool(raw_value),
            "is_secret": schema["is_secret"],
            "placeholder": schema["placeholder"],
            "description": schema["description"],
            "source": "database" if db_row else "environment",
        })

    return result


class UpdateConfigRequest(BaseModel):
    configs: dict[str, object]  # {key: value} — values coerced to str


@router.put("")
def update_config(body: UpdateConfigRequest, db: DbDep, _: AdminUser):
    """Update one or more configuration values."""
    updated = []
    for key, raw_value in body.configs.items():
        value = str(raw_value) if raw_value is not None else ""
        if key not in _SCHEMA_MAP:
            continue

        is_secret = _SCHEMA_MAP[key]["is_secret"]
        # Skip if secret field sent as masked value (UI re-sends the mask unchanged)
        if is_secret and "****" in value:
            continue

        # Secrets are encrypted at rest; non-secrets stored as-is.
        stored = encrypt_secret(value) if (is_secret and value) else value

        existing = db.get(AppConfig, key)
        if existing:
            existing.value = stored
            existing.updated_at = utcnow()
        else:
            db.add(AppConfig(
                key=key,
                value=stored,
                category=_SCHEMA_MAP[key]["category"],
                is_secret=is_secret,
            ))
        updated.append(key)

        # Apply the plaintext value to the live settings singleton (type-coerced)
        # so the change takes effect without a restart.
        set_live_setting(key, value)

    db.commit()
    logger.info("Config updated: keys=%s", updated)
    return {"updated": updated}


@router.post("/test-gitlab")
def test_gitlab_connection(db: DbDep, _: AdminUser):
    """Test GitLab connectivity with current settings."""
    url = _get_db_or_env(db, "gitlab_url")
    token = _get_db_or_env(db, "gitlab_token")

    if not url or not token:
        return {"status": "error", "message": "GitLab URL and token are required"}

    try:
        import gitlab
        # Inside Docker, localhost refers to the container
        docker_url = url.replace("://localhost", "://host.docker.internal") if "://localhost" in url else url
        gl = gitlab.Gitlab(docker_url, private_token=token, keep_base_url=True)
        gl.auth()
        user = gl.user
        return {"status": "ok", "message": f"Connected as {user.username}", "username": user.username}
    except Exception as e:
        logger.exception("GitLab connection test failed")
        return {"status": "error", "message": "Connection test failed"}


@router.post("/test-ollama")
def test_ollama_connection(db: DbDep, _: AdminUser):
    """Test Ollama connectivity."""
    url = _get_db_or_env(db, "ollama_url")
    if not url:
        return {"status": "error", "message": "Ollama URL is not configured"}

    try:
        import httpx
        docker_url = url.replace("://localhost", "://host.docker.internal") if "://localhost" in url else url
        resp = httpx.get(f"{docker_url.rstrip('/')}/api/tags", timeout=5.0)
        resp.raise_for_status()
        models = [m["name"] for m in resp.json().get("models", [])]
        has_embed = any("nomic-embed" in m for m in models)
        return {
            "status": "ok",
            "message": f"Connected. Models: {', '.join(models)}",
            "models": models,
            "has_embedding_model": has_embed,
        }
    except Exception as e:
        logger.exception("Ollama connection test failed")
        return {"status": "error", "message": "Connection test failed"}


def _get_db_or_env(db, key: str) -> str:
    """Get config value: DB override first, then env. Decrypts encrypted secrets."""
    row = db.get(AppConfig, key)
    if row and row.value:
        return decrypt_secret(row.value) if is_encrypted(row.value) else row.value
    return _get_env_value(key)
