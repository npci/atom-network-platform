# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""DocGen configuration — slim adapter that reads the platform's main settings.

The docgen pipeline's config.py was standalone (Ollama / OpenAI-compat). In the platform we proxy
through the platform's `app.core.config.settings` so we share the LLM
provider, model name, and artifacts directory with the rest of the app.

Only docgen-specific knobs (chunk sizes, content model split, parallelism,
font defaults) live here — and even those default to the platform's values.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from app.core.config import settings as _platform_settings


def _platform_model() -> str:
    """The model the platform will ACTUALLY call, resolved per provider.

    Imported lazily: app.core.llm pulls in the provider SDK shims, and docgen
    config is imported early. Returns "" if the resolver is unavailable, so the
    caller falls back to its own provider-correct default.
    """
    try:
        from app.core.llm import get_model
        return get_model()
    except Exception:  # pragma: no cover - defensive, keeps docgen importable
        # Deliberately NOT settings.claude_model. This branch fires when the
        # resolver is unavailable, and handing an Anthropic model id to
        # whatever provider is actually configured turns an import failure into
        # a confusing 404 from someone else's API.
        return ""


class _DocGenSettings:
    """Plain-attribute settings shim. Mirrors the docgen pipeline's `settings` interface so
    we don't need to patch every import site in pipeline.py / docx_builder.py.
    """

    # ── LLM provider routing ────────────────────────────────────────────────
    # The bridge in llm_bridge.py ignores `llm_provider` and `model_name` here
    # and calls the platform's app.core.llm.call_llm directly, so these fields
    # are nominal — but they are still introspected/logged by downstream docgen
    # code, so mirror the platform's ACTUAL provider rather than hardcoding
    # Claude (the platform routes to ollama/openai/gemini/ainxt just as often).
    llm_provider: str = getattr(_platform_settings, "llm_provider", "anthropic")
    model_name: str = _platform_model()
    temperature: float = float(os.getenv("DOCGEN_TEMPERATURE", "0.3"))

    # Empty → content uses the same model as planning
    content_model_name: str = os.getenv("DOCGEN_CONTENT_MODEL", "")

    # docgen-only fields (kept for compatibility — not used by the platform bridge)
    ollama_base_url: str = ""
    openai_base_url: str = ""
    openai_api_key: str = ""
    openai_model_name: str = ""

    # ── Filesystem ──────────────────────────────────────────────────────────
    # All docgen artifacts (per-job document_plan.json, generated_sections.json,
    # diagram PNGs, final .docx files) go under the platform's ARTIFACTS_DIR/docgen/.
    upload_dir: str = str(Path(getattr(_platform_settings, "artifacts_dir", "/app/artifacts")) / "docgen" / "uploads")
    output_dir: str = str(Path(getattr(_platform_settings, "artifacts_dir", "/app/artifacts")) / "docgen")
    vectorstore_dir: str = str(Path(getattr(_platform_settings, "artifacts_dir", "/app/artifacts")) / "docgen" / "vectorstore")

    # ── RAG knobs (for the docgen pipeline; the platform's hybrid_search has its own) ────
    chunk_size: int = 1500
    chunk_overlap: int = 200
    top_k_results: int = int(os.getenv("DOCGEN_TOP_K", "8"))
    rag_distance_threshold: float = 0.45
    rag_min_token_overlap: int = 2

    # ── Document styling ────────────────────────────────────────────────────
    default_font: str = "Calibri"
    default_font_size: int = 11

    # ── Default RAG collection labels (informational only — the platform RAG is
    # category-driven, not collection-name driven) ──────────────────────────
    domain_knowledge_collection: str = "domain_knowledge"
    domain_code_collection: str = "domain_code"

    # ── Concurrency for write_content ───────────────────────────────────────
    max_parallel_sections: int = int(os.getenv("DOCGEN_MAX_PARALLEL_SECTIONS", "3"))

    # ── Surgical (patch-based) editing ──────────────────────────────────────
    # The Revise / section-edit / consistency-repair paths apply a minimal set of
    # patch ops (block-id targeted, diff-gated) instead of regenerating whole
    # sections. Default ON — this is the primary editor. Set
    # DOCGEN_SURGICAL_EDIT=false to fall back to the legacy whole-section
    # regeneration path.
    surgical_edit: bool = os.getenv("DOCGEN_SURGICAL_EDIT", "true").strip().lower() in ("1", "true", "yes", "on")

    # ── Diagram engine ──────────────────────────────────────────────────────
    # "plantuml" (default) or "mermaid". Mermaid renders via the mmdc CLI
    # (mermaid-cli) when present; if the engine is "mermaid" but mmdc is
    # unavailable the pipeline falls back to the Pillow JSON renderer. The
    # diagram SOURCE is persisted (generated_diagram_sources.json) regardless of
    # engine, so diagrams become addressable/editable rather than write-once PNGs.
    diagram_engine: str = os.getenv("DOCGEN_DIAGRAM_ENGINE", "plantuml").strip().lower()

    # Server fields (unused — the platform owns the FastAPI app)
    host: str = "0.0.0.0"
    port: int = 8001

    def ensure_dirs(self) -> None:
        for d in (self.upload_dir, self.output_dir, self.vectorstore_dir):
            Path(d).mkdir(parents=True, exist_ok=True)

    # ── Computed properties (the docgen pipeline references these) ────────────────
    @property
    def effective_content_model(self) -> str:
        return (self.content_model_name or "").strip() or self.model_name

    @property
    def effective_openai_model(self) -> str:
        return (self.openai_model_name or "").strip() or self.model_name

    @property
    def normalized_llm_provider(self) -> str:
        # The platform bridge handles routing internally; report the provider it
        # will actually use so callers that log/introspect this aren't misled.
        provider = (self.llm_provider or "anthropic").strip().lower()
        return "anthropic" if provider == "claude" else provider


settings = _DocGenSettings()
settings.ensure_dirs()
