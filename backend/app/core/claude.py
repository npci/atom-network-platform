# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Anthropic Claude client singleton."""
from functools import lru_cache
import anthropic
from app.core.config import settings


@lru_cache(maxsize=1)
def get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


@lru_cache(maxsize=1)
def get_async_client() -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)


MODEL = "claude-sonnet-5"
