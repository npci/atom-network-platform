# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Vision understanding of figures in uploaded source documents (fail-open by design)."""
import asyncio
import io
import zipfile

import pytest
from PIL import Image

from app.services import image_understanding as IU


def _png_bytes(w, h, color=(200, 30, 30)):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


# ── _prep_image: filtering + normalization ────────────────────────────────────

def test_icons_and_separators_are_filtered_out():
    assert IU._prep_image(Image.new("RGB", (48, 48))) is None          # icon
    assert IU._prep_image(Image.new("RGB", (1200, 8))) is None         # separator line
    assert IU._prep_image(Image.new("RGB", (150, 90))) is None         # below min dim


def test_real_figure_is_jpeg_normalized_and_downscaled():
    data, media = IU._prep_image(Image.new("RGB", (4000, 2500)))
    assert media == "image/jpeg"
    w, h = Image.open(io.BytesIO(data)).size
    assert max(w, h) <= IU._MAX_SEND_DIM


# ── docx extraction ───────────────────────────────────────────────────────────

def test_docx_media_images_are_extracted(tmp_path):
    p = tmp_path / "brd.docx"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("word/document.xml", "<doc/>")
        zf.writestr("word/media/image1.png", _png_bytes(600, 400))
        zf.writestr("word/media/logo.emf", b"\x01\x02not-an-image")   # unsupported → skipped
    images = IU._extract_docx_images(p)
    assert len(images) == 1 and images[0][0] == "image1.png"


def test_garbage_file_fails_open(tmp_path):
    p = tmp_path / "broken.docx"
    p.write_bytes(b"this is not a zip")
    assert IU._extract_docx_images(p) == []
    assert IU._extract_pdf_images(p) == []


# ── describe_document_images: captioning + caps + fail-open vision ────────────

def _fake_vision(monkeypatch, responses):
    calls = []

    async def fake(system, prompt, image_bytes, media_type="image/jpeg", **kw):
        calls.append({"system": system, "media": media_type})
        r = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr("app.core.llm.call_llm_vision", fake)
    return calls


def test_standalone_image_upload_is_described(tmp_path, monkeypatch):
    calls = _fake_vision(monkeypatch, ["Sequence diagram: PSP → the Authority ReqTransfer …"])
    p = tmp_path / "flow.png"
    p.write_bytes(_png_bytes(800, 600))
    caps = asyncio.run(IU.describe_document_images(p, ".png"))
    assert len(caps) == 1 and caps[0].startswith("[Figure 1")
    assert "Sequence diagram" in caps[0]
    assert "untrusted" in calls[0]["system"]        # anti-injection instruction present


def test_figure_cap_bounds_vision_calls(tmp_path, monkeypatch):
    calls = _fake_vision(monkeypatch, ["desc"])
    p = tmp_path / "brd.docx"
    with zipfile.ZipFile(p, "w") as zf:
        for i in range(12):
            zf.writestr(f"word/media/image{i}.png", _png_bytes(500, 400))
    caps = asyncio.run(IU.describe_document_images(p, ".docx", max_figures=3))
    assert len(caps) == 3 and len(calls) == 3


def test_vision_failure_skips_figure_not_upload(tmp_path, monkeypatch):
    _fake_vision(monkeypatch, [RuntimeError("gateway has no image support"), "ok desc"])
    p = tmp_path / "brd.docx"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("word/media/a.png", _png_bytes(500, 400))
        zf.writestr("word/media/b.png", _png_bytes(500, 400))
    caps = asyncio.run(IU.describe_document_images(p, ".docx"))
    assert len(caps) == 1 and "ok desc" in caps[0]   # figure 1 failed open, figure 2 landed


def test_unknown_extension_returns_empty():
    caps = asyncio.run(IU.describe_document_images(__import__("pathlib").Path("x.xlsx"), ".xlsx"))
    assert caps == []


# ── figures_block rendering ───────────────────────────────────────────────────

def test_figures_block_renders_section_or_nothing():
    assert IU.figures_block([]) == ""
    out = IU.figures_block(["[Figure 1 — page 2] flow"])
    assert "## Figures" in out and "[Figure 1 — page 2] flow" in out
