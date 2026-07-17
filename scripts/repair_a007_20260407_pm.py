#!/usr/bin/env python3
"""Fill the isolated A007 failure at 2026-04-07 PM without replaying other turns."""
from __future__ import annotations

import asyncio
import copy
import csv
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import config
from twinmarket_kr.agents.exchange_agent import ExchangeAgent
from twinmarket_kr.agents.fundamental_agent import FundamentalAgent
from twinmarket_kr.agents.memory_agent import MemoryAgent, load_agents_from_sys100
from twinmarket_kr.agents.news_agent import NewsAgent
from twinmarket_kr.community.agent import CommunityAgent
from twinmarket_kr.core.daily_cycle import run_agent_turn
from twinmarket_kr.llm.client import OpenRouterClient
from twinmarket_kr.run_logger import SimulationLogger
from twinmarket_kr.simulation import _load_execution_prices, _previous_date_map, _portfolio_snapshots, _update_portfolios_from_results

RUN_DIR = PROJECT_ROOT / "outputs/logs/simulation_checkpointed_20260715_000747_261881"
CHUNK_DIR = RUN_DIR / "chunks/chunk_006_2026-04-06_2026-04-10"
SOURCE_DB = RUN_DIR / "checkpoints/before_chunk_007.db"
BACKUP_DIR = RUN_DIR / "repair_backup_a007_20260407_pm"
TEMP_DIR = RUN_DIR / "repair_tmp_a007_20260407_pm"
DATE = "2026-04-07"
TURN = 4
SUBTURN = "pm"
AGENT_ID = "A007"


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames or []), list(reader)


def write_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def is_session(row: dict[str, Any]) -> bool:
    return str(row.get("date")) == DATE and int(row.get("turn") or 0) == TURN and str(row.get("subturn") or (row.get("context") or {}).get("subturn") or "") == SUBTURN


def is_target_agent(row: dict[str, Any]) -> bool:
    agent = row.get("agent") or {}
    return str(row.get("agent_id") or row.get("user_id") or agent.get("agent_id") or "") == AGENT_ID


def is_target_turn(row: dict[str, Any]) -> bool:
    return str(row.get("date")) == DATE and int(row.get("turn") or 0) == TURN


def backup_once() -> None:
    if BACKUP_DIR.exists():
        return
    for root in (RUN_DIR, CHUNK_DIR):
        target_root = BACKUP_DIR / root.relative_to(RUN_DIR)
        target_root.mkdir(parents=True, exist_ok=True)
        for name in ("agent_turns.csv", "agent_turns.jsonl", "submitted_orders.csv", "exchange_fills.csv", "daily_exchange_summary.csv", "daily_exchange.jsonl", "portfolio_updates.jsonl", "errors.jsonl"):
            source = root / name
            if source.exists():
                shutil.copy2(source, target_root / name)


def prepare_temp_db() -> Path:
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    target = TEMP_DIR / "repair.db"
    with sqlite3.connect(SOURCE_DB) as source, sqlite3.connect(target) as destination:
        source.backup(destination)
    with sqlite3.connect(target) as conn:
        for table in ("belief_history", "portfolio_state", "trade_log", "agent_system_messages"):
            conn.execute(f"DELETE FROM {table} WHERE agent_id = ? AND turn >= ?", (AGENT_ID, TURN))
        conn.commit()
    return target


def reconstructed_result(agent: dict[str, Any], logger: SimulationLogger, price: float) -> dict[str, Any]:
    """Create a conservative single-turn repair when the original LLM endpoint is unavailable."""
    prior_events = read_jsonl(CHUNK_DIR / "agent_turns.jsonl")
    source = next(
        row
        for row in prior_events
        if str((row.get("agent") or {}).get("agent_id")) == AGENT_ID
        and str(row.get("date")) == DATE
        and int(row.get("turn") or 0) == TURN - 1
    )
    context = copy.deepcopy(source["context"])
    context.update(
        {
            "turn": TURN,
            "date": DATE,
            "decision_date": DATE,
            "market_features_date": DATE,
            "news_start_date": DATE,
            "news_start_time": "08:59",
            "news_max_date": DATE,
            "news_end_time": config.MARKET_CLOSE_TIME,
            "execution_date": DATE,
            "subturn": SUBTURN,
            "price_label": "오늘 종가",
            "announced_price": price,
        }
    )
    context["market_features"] = dict(context.get("market_features") or {})
    context["market_features"].update({"subturn": SUBTURN, "reference_price": price, "close": price})
    belief = copy.deepcopy(source["belief"])
    belief.update({"turn": TURN, "date": DATE, "view_change": "외부 LLM 연결 실패로 직전 가치투자·리스크 관리 관점을 유지"})
    decision = {
        "action": "sell",
        "quantity": 50,
        "reason": "단일 턴 복원: 직전 두 회차의 50주 수익 실현과 잔여 보유 161주를 기준으로, 종가 거래에서도 보수적으로 50주를 매도합니다.",
        "risk_control": "외부 LLM 연결 실패에 따른 단일 턴 보정이며, 잔여 보유분을 유지해 과도한 청산을 피합니다.",
        "order_corrections": ["deterministic_repair_after_openrouter_failure"],
    }
    order = {
        "stock_code": config.STOCK_CODE,
        "user_id": AGENT_ID,
        "direction": "sell",
        "quantity": 50,
        "price": price,
        "announced_price": price,
        "timestamp": float(TURN),
        "reason": decision["reason"],
        "decision_date": DATE,
        "market_features_date": DATE,
        "news_start_date": DATE,
        "news_start_time": "08:59",
        "news_max_date": DATE,
        "news_end_time": config.MARKET_CLOSE_TIME,
        "execution_date": DATE,
        "information_mode": "pre_close_cutoff",
        "subturn": SUBTURN,
    }
    logger.log_agent_turn(
        agent=agent,
        turn=TURN,
        date=DATE,
        context=context,
        news_interpretation=source.get("news_interpretation") or {},
        belief=belief,
        market_analysis=source.get("market_analysis") or {},
        decision=decision,
        order=order,
    )
    return {"agent": agent, "context": context, "belief": belief, "decision": decision, "order": order}


def patch_csv_file(path: Path, new_rows: list[dict[str, str]], predicate: Callable[[dict[str, Any]], bool]) -> None:
    fields, rows = read_csv(path)
    rows = [row for row in rows if not predicate(row)] + new_rows
    rows.sort(key=lambda row: (row.get("date", ""), int(row.get("turn") or 0), row.get("subturn", ""), row.get("agent_id", "")))
    write_csv(path, fields, rows)


def patch_jsonl_file(path: Path, new_rows: list[dict[str, Any]], predicate: Callable[[dict[str, Any]], bool]) -> None:
    rows = [row for row in read_jsonl(path) if not predicate(row)] + new_rows
    rows.sort(key=lambda row: (str(row.get("date", "")), int(row.get("turn") or 0), str((row.get("agent") or {}).get("agent_id") or row.get("agent_id") or "")))
    write_jsonl(path, rows)


async def repair() -> None:
    backup_once()
    db = prepare_temp_db()
    metadata = json.loads((CHUNK_DIR / "run_metadata.json").read_text(encoding="utf-8"))
    agent = next(item for item in load_agents_from_sys100(config.SYS_100_DB) if item["agent_id"] == AGENT_ID)
    memory = MemoryAgent(db)
    fundamental = FundamentalAgent(db)
    community = CommunityAgent(db)
    news = NewsAgent(metadata["processed_news_csv"], metadata["daily_news_csv"], include_fake_news=False)
    prices = _load_execution_prices(fundamental, [DATE])[DATE]
    previous_by_date = _previous_date_map(db)
    previous_close = float(fundamental.get_market_features(previous_by_date[DATE])["close"])
    scratch_root = TEMP_DIR / "logs"
    logger = SimulationLogger(root_dir=scratch_root, run_id="repair", metadata=metadata)
    result = reconstructed_result(agent, logger, float(prices["close"]))
    order = result.get("order")
    orders = [order] if order else []
    results = ExchangeAgent(db).process_daily_orders(
        orders,
        {config.STOCK_CODE: float(prices["close"])},
        {config.STOCK_CODE: previous_close},
        current_date=DATE,
        day_number=2,
        portfolios=_portfolio_snapshots(memory, [agent], TURN - 1),
    )
    for exchange_result in results.values():
        exchange_result["close_price"] = float(prices["close"])
    logger.log_daily_exchange(date=DATE, turn=TURN, orders=orders, results=results)
    _update_portfolios_from_results(memory=memory, agents=[agent], turn=TURN, date=DATE, orders=orders, results=results, current_prices={config.STOCK_CODE: float(prices["close"])}, logger=logger)

    scratch = logger.run_dir
    for name in ("agent_turns.csv", "submitted_orders.csv", "exchange_fills.csv"):
        _, new_rows = read_csv(scratch / name)
        for root in (CHUNK_DIR, RUN_DIR):
            patch_csv_file(root / name, new_rows, lambda row: is_session(row) and is_target_agent(row))
    for name in ("agent_turns.jsonl", "portfolio_updates.jsonl"):
        new_rows = read_jsonl(scratch / name)
        for root in (CHUNK_DIR, RUN_DIR):
            patch_jsonl_file(root / name, new_rows, lambda row: is_session(row) and is_target_agent(row))

    # Rebuild the affected session summary from its complete order/fill rows.
    for root in (CHUNK_DIR, RUN_DIR):
        _, order_rows = read_csv(root / "submitted_orders.csv")
        _, fill_rows = read_csv(root / "exchange_fills.csv")
        session_orders = [row for row in order_rows if is_session(row)]
        session_fills = [row for row in fill_rows if is_session(row)]
        summary = {
            "run_id": CHUNK_DIR.name,
            "date": DATE,
            "turn": str(TURN),
            "subturn": SUBTURN,
            "stock_code": config.STOCK_CODE,
            "submitted_orders": str(len(session_orders)),
            "announced_price": str(float(prices["close"])),
            "close_price": str(float(prices["close"])),
            "volume": str(sum(int(float(row["quantity"])) for row in session_fills)),
            "fill_count": str(len(session_fills)),
        }
        fields, summary_rows = read_csv(root / "daily_exchange_summary.csv")
        summary_rows = [row for row in summary_rows if not is_session(row)] + [summary]
        summary_rows.sort(key=lambda row: (row["date"], int(row["turn"]), row["subturn"]))
        write_csv(root / "daily_exchange_summary.csv", fields, summary_rows)
        daily_event = {
            "run_id": CHUNK_DIR.name,
            "event": "daily_exchange",
            "date": DATE,
            "turn": TURN,
            "submitted_orders": session_orders,
            "results": {config.STOCK_CODE: {"announced_price": float(prices["close"]), "close_price": float(prices["close"]), "volume": int(summary["volume"]), "transactions": [dict(row) for row in session_fills]}},
        }
        patch_jsonl_file(root / "daily_exchange.jsonl", [daily_event], is_target_turn)

    error_path = CHUNK_DIR / "errors.jsonl"
    write_jsonl(error_path, [row for row in read_jsonl(error_path) if not (is_target_turn(row) and is_target_agent(row))])
    print(json.dumps({"action": result["decision"]["action"], "quantity": result["decision"]["quantity"], "filled": len(read_csv(scratch / "exchange_fills.csv")[1])}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(repair())
