# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Video-generation building blocks: segment math + mock-provider → ffmpeg merge.

Exercises the real ffmpeg path locally (no external video APIs) via the mock
provider, so the segment-generate → concat-merge pipeline is verified end to end
without keys. Skipped if ffmpeg isn't on PATH.
"""
import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from app.agents.video_script_schema import segment_boundaries
from app.services.video.mock import MockVideoProvider
from app.services.video_gen_runner import _merge_clips

_HAS_FFMPEG = shutil.which("ffmpeg") is not None
_HAS_FFPROBE = shutil.which("ffprobe") is not None


def test_segment_boundaries_promo_and_explainer():
    # 30s promo -> 4 clips, last absorbs remainder
    assert segment_boundaries(30, 8) == [(0, 8), (8, 16), (16, 24), (24, 30)]
    # 45s explainer -> 6 clips
    assert segment_boundaries(45, 8) == [(0, 8), (8, 16), (16, 24), (24, 32), (32, 40), (40, 45)]
    # exact multiple + degenerate inputs
    assert segment_boundaries(16, 8) == [(0, 8), (8, 16)]
    assert segment_boundaries(0, 8) == []
    assert all((e - s) <= 8 for s, e in segment_boundaries(45, 8))


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not installed")
async def test_mock_provider_generates_and_merges(tmp_path: Path):
    prov = MockVideoProvider()
    clips = []
    for i, (start, end) in enumerate(segment_boundaries(24, 8), start=1):
        out = tmp_path / f"seg_{i}.mp4"
        res = await prov.generate_clip(
            prompt=f"segment {i}", duration_sec=end - start, out_path=out,
        )
        assert res.path.exists() and res.path.stat().st_size > 0
        clips.append(out)
    assert len(clips) == 3

    final = tmp_path / "final.mp4"
    await _merge_clips(clips, final)
    assert final.exists() and final.stat().st_size > 0

    if _HAS_FFPROBE:
        dur = float(subprocess.check_output([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(final),
        ]).decode().strip())
        # 3 clips of 8s each ≈ 24s (allow encoder slack)
        assert 22.0 <= dur <= 26.0
