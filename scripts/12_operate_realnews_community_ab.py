#!/usr/bin/env python3
"""Dedicated RN run/resume/finalize entrypoint.

This is intentionally separate from the preflight-only ``09`` script.
"""
from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from twinmarket_kr.rn_ab.operations import main


if __name__ == "__main__":
    sys.exit(main())
