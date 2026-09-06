# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The SPA's <base href> must be injected by the frontend IMAGE, exactly once.

index.html references its assets relatively ("./assets/x.js") and Vite emits no
<base> tag, so something has to anchor them at the public context path. Get it
wrong and a deep-link refresh (e.g. /a2a/changes/123) resolves assets to
/a2a/changes/assets/x.js.

THAT REQUEST RETURNS 200, NOT 404 — the SPA fallback answers it with
index.html. The browser asks for JavaScript, gets HTML, the module fails to
parse, and the page is blank with nothing red in the Network tab. Nothing about
the failure points at nginx, which is why this is pinned in source rather than
left to be rediscovered.

Two ways to get it wrong, both of which this file catches:

  1. Injecting nowhere. The image then serves a blank page to anyone who runs
     it directly, even while the deployed stack looks fine because the outer
     proxy patched over it.
  2. Injecting in BOTH the image and the outer proxy — two <base> tags. The
     first wins in every browser, so it "works", and the duplicate survives
     review to confuse the next debugging session.

These are file assertions, not HTTP ones: the templates are what ships, and a
running stack is not available in unit tests.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_FRONTEND_CONF = _REPO / "frontend" / "nginx.conf.template"
_OUTER_CONF = _REPO / "nginx" / "nginx.dev.conf.template"

# A sub_filter line that injects a <base> tag, ignoring the quoting style.
_BASE_INJECTION = re.compile(r"^\s*sub_filter\s+.*<base\s+href=", re.MULTILINE)


def _body(path: Path) -> str:
    """The file with comment lines stripped — the comments here DISCUSS
    `sub_filter` and `<base href>` at length, and would match otherwise."""
    text = path.read_text(encoding="utf-8")
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


def test_the_frontend_image_config_exists():
    """Without this file the frontend image cannot build at all — the Dockerfile
    COPYs it to /etc/nginx/conf.d/default.conf.template."""
    assert _FRONTEND_CONF.is_file(), f"{_FRONTEND_CONF} is missing"


def test_the_frontend_image_injects_the_base_tag():
    injections = _BASE_INJECTION.findall(_body(_FRONTEND_CONF))
    assert len(injections) == 1, (
        "frontend/nginx.conf.template must inject <base href> exactly once — "
        f"found {len(injections)}. Without it, this image serves a blank page "
        "on every deep-link refresh when run without the outer proxy."
    )


def test_the_injection_uses_the_context_path_not_a_hardcoded_prefix():
    """A literal '/a2a/' here silently breaks every deployment that renames the
    context — and basePath.js's own fallback is '/a2a/', so the failure would
    only show up on the non-default path."""
    body = _body(_FRONTEND_CONF)
    line = next(ln for ln in body.splitlines() if "<base" in ln)
    assert "$CONTEXT_PATH" in line, \
        f"the injected prefix must follow $CONTEXT_PATH, got: {line.strip()}"


def test_the_outer_proxy_does_not_inject_it_too():
    """Two sub_filters produce two <base> tags. The injection belongs in the
    image so the image is correct standalone."""
    if not _OUTER_CONF.is_file():
        pytest.skip(f"{_OUTER_CONF} not present in this checkout")
    injections = _BASE_INJECTION.findall(_body(_OUTER_CONF))
    assert injections == [], (
        "nginx/nginx.dev.conf.template also injects <base href> — that is a "
        "duplicate now that frontend/nginx.conf.template does it. Remove it "
        "there, not here."
    )


def test_any_proxy_that_rewrites_a_body_clears_accept_encoding():
    """sub_filter cannot rewrite a gzipped body: nginx passes it through
    UNMODIFIED, so the fix looks applied and does nothing. Any sub_filter in a
    proxying location must be paired with `proxy_set_header Accept-Encoding ""`.

    The frontend image is exempt — it serves from disk, with no upstream."""
    if not _OUTER_CONF.is_file():
        pytest.skip(f"{_OUTER_CONF} not present in this checkout")
    body = _body(_OUTER_CONF)
    if "sub_filter" not in body:
        return  # nothing is rewritten at this hop
    assert re.search(r'proxy_set_header\s+Accept-Encoding\s+""', body), (
        "nginx.dev.conf.template rewrites a proxied body with sub_filter but "
        'never clears Accept-Encoding — a gzipped upstream response passes '
        "through unmodified and the rewrite silently does nothing."
    )
