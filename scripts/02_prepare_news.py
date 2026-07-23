#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import config
from twinmarket_kr.agents.news_agent import prepare_news


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=None, help="Fix daily news sampling for reproducible runs.")
    parser.add_argument(
        "--sources",
        default=None,
        help="Comma-separated news sources to keep (e.g. 'mk'). Default: keep all sources.",
    )
    args = parser.parse_args()
    sources = (
        [s.strip() for s in args.sources.split(",") if s.strip()] if args.sources else None
    )
    processed_count, selected_count = prepare_news(
        config.SAMSUNG_NEWS_RAW_PKL,
        config.PROCESSED_NEWS_CSV,
        config.DAILY_NEWS_SELECTION_CSV,
        daily_seed=args.seed,
        sources=sources,
    )
    print(f"sources={sources or 'ALL'}")
    print(f"processed={processed_count}, daily_selected={selected_count}")
    print(f"processed_csv={config.PROCESSED_NEWS_CSV}")
    print(f"daily_csv={config.DAILY_NEWS_SELECTION_CSV}")


if __name__ == "__main__":
    main()
