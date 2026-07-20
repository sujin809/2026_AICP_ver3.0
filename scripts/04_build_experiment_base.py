#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import config
from twinmarket_kr.experiment_runtime import build_clean_experiment_base


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a clean experiment base with StockData and turn-zero state only."
    )
    parser.add_argument("--source", type=Path, default=config.SIM_DB)
    parser.add_argument("--output", type=Path, default=config.EXPERIMENT_BASE_DB)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    report = build_clean_experiment_base(args.source, args.output, overwrite=args.force)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
