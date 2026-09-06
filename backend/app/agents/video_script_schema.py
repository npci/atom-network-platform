# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Pydantic models for the segmented video script (Phase A Product Kit).

A video model caps a single clip at ~8 seconds, so the script for a
``promo_video`` / ``explainer_video`` is authored as an ordered list of
≤8-second **segments**. Each segment is generated as its own clip by the
configured video provider; the clips are then merged (ffmpeg concat) into one
final MP4. The structured shape lets the runner generate one clip per row
deterministically and lets the frontend render the script as formatted cards.

``VideoScript`` is serialized into ``ProductKitDocument.script_json``.
"""

from __future__ import annotations

import math

from pydantic import BaseModel, Field


class VideoSegment(BaseModel):
    """One ≤8-second beat of the video."""

    index: int                              # 1-based ordinal
    start_sec: int                          # inclusive start on the timeline
    end_sec: int                            # exclusive end
    duration_sec: int                       # end_sec - start_sec, ≤ segment cap
    visual_prompt: str                      # the text fed to the video model
    voiceover: str = ""                     # spoken line for this beat ([VO])
    on_screen_text: str = ""                # any overlaid caption
    continuity: str = ""                    # carry-over note for the next beat


class VideoScript(BaseModel):
    """The full segmented script for one product-kit video doc."""

    doc_type: str                           # promo_video | explainer_video
    provider: str = ""                      # video provider this was authored for
    model: str = ""                         # target video model
    duration_sec: int = 0                   # total intended duration
    aspect_ratio: str = "16:9"
    segments: list[VideoSegment] = Field(default_factory=list)


def segment_boundaries(duration_sec: int, segment_max_sec: int = 8) -> list[tuple[int, int]]:
    """Split a total duration into ≤segment_max_sec (start, end) windows.

    The final window absorbs the remainder, e.g. (30, 8) -> [(0,8),(8,16),
    (16,24),(24,30)] and (45, 8) -> five 8s windows + a trailing 5s one.
    """
    if duration_sec <= 0 or segment_max_sec <= 0:
        return []
    n = math.ceil(duration_sec / segment_max_sec)
    bounds: list[tuple[int, int]] = []
    for i in range(n):
        start = i * segment_max_sec
        end = min(start + segment_max_sec, duration_sec)
        bounds.append((start, end))
    return bounds
