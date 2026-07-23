"""One import-safe model identifier for the RN paper execution path.

This module intentionally has no dependency on ``twinmarket_kr.rn_ab``.
``config`` is imported by legacy runtime code, so importing a submodule that
initializes the whole RN package would create a circular import.
"""
from __future__ import annotations


RN_PAPER_MODEL = "qwen/qwen3.5-flash-02-23"


__all__ = ["RN_PAPER_MODEL"]
