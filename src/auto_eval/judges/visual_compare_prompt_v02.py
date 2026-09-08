"""Backward-compatible import for the prompt module renamed in v0.3."""

from .visual_compare_prompt_v03 import VISUAL_COMPARE_SYSTEM, VISUAL_COMPARE_USER

__all__ = ["VISUAL_COMPARE_SYSTEM", "VISUAL_COMPARE_USER"]
