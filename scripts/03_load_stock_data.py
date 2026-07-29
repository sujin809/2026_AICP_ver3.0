#!/usr/bin/env python3
"""Validate market data, or explicitly load it into the numbered pipeline DB."""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config
from twinmarket_kr.agents.fundamental_agent import FundamentalAgent
from twinmarket_kr.db.connection import connect
from twinmarket_kr.outcome_schedule import FrozenEventSchedule


def validate_market_data(
    *,
    target_db: Path,
    profile_root: Path,
) -> dict[str, Any]:
    """Verify that every sealed AM/open and PM/close price exists in the DB."""

    schedule = FrozenEventSchedule.from_sealed_files(
        profile_root / "calendar.json",
        profile_root / "prices.json",
        expected_stock_code=None,
    )
    if not target_db.is_file():
        raise FileNotFoundError(f"market-data database is missing: {target_db}")

    expected_by_date: dict[str, dict[str, float]] = {}
    for event in schedule.events:
        date = str(event["date"])
        field = "open_price" if str(event["subturn"]) == "am" else "close_price"
        expected_by_date.setdefault(date, {})[field] = float(
            event["execution_price"]
        )

    with connect(target_db) as connection:
        rows = connection.execute(
            """
            SELECT date, open_price, close_price
            FROM StockData
            WHERE stock_id = ?
            ORDER BY date
            """,
            (schedule.stock_code,),
        ).fetchall()
    observed_by_date = {
        str(row["date"]): {
            "open_price": float(row["open_price"]),
            "close_price": float(row["close_price"]),
        }
        for row in rows
    }
    missing_dates = sorted(set(expected_by_date) - set(observed_by_date))
    mismatches: list[dict[str, Any]] = []
    for date, expected in expected_by_date.items():
        observed = observed_by_date.get(date)
        if observed is None:
            continue
        for field in ("open_price", "close_price"):
            if not math.isclose(
                observed[field],
                expected[field],
                rel_tol=0.0,
                abs_tol=1e-9,
            ):
                mismatches.append(
                    {
                        "date": date,
                        "field": field,
                        "expected": expected[field],
                        "observed": observed[field],
                    }
                )
    if missing_dates or mismatches:
        raise RuntimeError(
            "market data differs from the sealed execution-price registry: "
            f"missing_dates={missing_dates}, mismatches={mismatches[:10]}"
        )
    return {
        "validation_pass": True,
        "mode": "read_only_market_data_validation",
        "target_db": str(target_db.resolve()),
        "stock_code": schedule.stock_code,
        "stock_row_count": len(rows),
        "sealed_trading_date_count": len(expected_by_date),
        "calendar_sha256": schedule.calendar_sha256,
        "prices_sha256": schedule.prices_sha256,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "기본 동작은 DB를 수정하지 않고 봉인 가격과 StockData를 검증합니다. "
            "CSV를 적재하려면 --write를 명시해야 합니다."
        )
    )
    parser.add_argument("--source", type=Path, default=config.STOCK_DATA_CSV)
    parser.add_argument("--target", type=Path, default=config.SIM_DB)
    parser.add_argument(
        "--profile-root",
        type=Path,
        default=config.SEALED_REAL_NEWS_BUNDLE.parent,
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="명시한 CSV를 target DB에 INSERT OR REPLACE로 적재합니다.",
    )
    args = parser.parse_args(argv)

    loaded_count = 0
    if args.write:
        loaded_count = FundamentalAgent(args.target).load_stock_data_csv(
            args.source,
            stock_code=FrozenEventSchedule.from_sealed_files(
                args.profile_root / "calendar.json",
                args.profile_root / "prices.json",
                expected_stock_code=None,
            ).stock_code,
        )
    report = validate_market_data(
        target_db=args.target,
        profile_root=args.profile_root,
    )
    report["loaded_count"] = loaded_count
    report["write_requested"] = bool(args.write)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
