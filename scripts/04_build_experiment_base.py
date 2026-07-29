#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import config
from twinmarket_kr.agents.memory_agent import load_agents_from_sys100
from twinmarket_kr.experiment_runtime import (
    build_clean_experiment_base,
    validate_clean_experiment_base,
)
from twinmarket_kr.study_spec import validate_integrated_study_profile


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a clean experiment base with StockData and turn-zero state only."
    )
    parser.add_argument("--source", type=Path, default=config.SIM_DB)
    parser.add_argument("--output", type=Path, default=config.EXPERIMENT_BASE_DB)
    parser.add_argument("--sys-db", type=Path, default=config.SYS_100_DB)
    parser.add_argument(
        "--profile-root",
        type=Path,
        default=config.SEALED_REAL_NEWS_BUNDLE.parent,
        help=(
            "Cohort, instrument and schedule source. Defaults to the current "
            "sealed real-news baseline profile."
        ),
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    available_agents = load_agents_from_sys100(args.sys_db)
    profile = validate_integrated_study_profile(
        args.profile_root,
        agents=available_agents,
        condition_id="RN_COMM_OFF",
        prompt_dir=config.PROMPT_DIR,
        expected_stock_code=None,
        expected_model=config.PAPER_OPENROUTER_MODEL,
        expected_provider=config.PAPER_OPENROUTER_PROVIDER,
    )
    runtime_agents = load_agents_from_sys100(
        args.sys_db,
        instrument_name=profile.instrument_name,
    )
    by_id = {
        str(agent["agent_id"]): agent
        for agent in runtime_agents
    }
    agents = [by_id[agent_id] for agent_id in profile.agent_ids]
    report = build_clean_experiment_base(
        args.source,
        args.output,
        overwrite=args.force,
        initial_agents=agents,
        instrument_name=profile.instrument_name,
    )
    validate_clean_experiment_base(
        args.output,
        expected_agents=agents,
        expected_stock_code=profile.stock_code,
        expected_trading_dates=profile.schedule_date_ids,
    )
    report["study_spec_sha256"] = profile.study_spec_sha256
    report["stock_code"] = profile.stock_code
    report["instrument_name"] = profile.instrument_name
    report["schedule_date_count"] = len(profile.schedule_date_ids)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
