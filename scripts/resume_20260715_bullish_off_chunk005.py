#!/usr/bin/env python3
"""Resume the interrupted 20260715 community-off bullish-fake run.

This script is intentionally narrow:
- resumes chunk_005_2026-03-30_2026-04-03 from the existing
  2026-04-01 PM partial agent logs;
- preserves the existing first 157 agent-turn rows;
- appends from the next missing agent row;
- completes chunk 005, merges it into the parent run, and marks chunk 005 complete.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import config
from twinmarket_kr.agents.exchange_agent import ExchangeAgent
from twinmarket_kr.agents.fundamental_agent import FundamentalAgent
from twinmarket_kr.agents.memory_agent import MemoryAgent, load_agents_from_sys100
from twinmarket_kr.agents.news_agent import NewsAgent
from twinmarket_kr.core.daily_cycle import run_agent_turn
from twinmarket_kr.llm.client import OpenRouterClient
from twinmarket_kr.run_logger import SimulationLogger
from twinmarket_kr.simulation import (
    _acquire_sim_db_lock,
    _load_execution_prices,
    _portfolio_snapshots,
    _previous_date_map,
    _run_subturn,
    _update_portfolios_from_results,
)


RUN_DIR = (
    PROJECT_ROOT
    / "outputs/logs/simulation_20260715_community_off_fake_on_bullish_30_20260227_20260601"
)
CHUNK_ID = "chunk_005_2026-03-30_2026-04-03"
CHUNK_DIR = RUN_DIR / "chunks" / CHUNK_ID
RUNTIME_DB = RUN_DIR / "runtime_sim.db"
BACKUP_DIR = RUN_DIR / "resume_backup_before_chunk005_resume"


def _load_checkpointed_module() -> Any:
    path = PROJECT_ROOT / "scripts" / "05_run_simulation_checkpointed.py"
    spec = importlib.util.spec_from_file_location("checkpointed_runner", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _existing_agent_events(day: str, turn: int, subturn: str) -> dict[str, dict[str, Any]]:
    events: dict[str, dict[str, Any]] = {}
    for event in _read_jsonl(CHUNK_DIR / "agent_turns.jsonl"):
        if event.get("event") != "agent_turn":
            continue
        if event.get("date") == day and int(event.get("turn")) == turn:
            context = event.get("context") or {}
            if context.get("subturn") == subturn:
                agent_id = str((event.get("agent") or {}).get("agent_id"))
                events[agent_id] = event
    return events


def _event_to_turn_result(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "agent": event["agent"],
        "context": event["context"],
        "belief": event["belief"],
        "decision": event["decision"],
        "order": event.get("submitted_order"),
        "news_interpretation": event.get("news_interpretation") or {},
        "market_analysis": event.get("market_analysis") or {},
    }


def _backup_once() -> None:
    if BACKUP_DIR.exists():
        return
    BACKUP_DIR.mkdir(parents=True)
    for path in [
        RUNTIME_DB,
        RUN_DIR / "checkpoint.json",
        RUN_DIR / "agent_turns.csv",
        RUN_DIR / "agent_turns.jsonl",
        RUN_DIR / "submitted_orders.csv",
        RUN_DIR / "exchange_fills.csv",
        RUN_DIR / "daily_exchange_summary.csv",
        RUN_DIR / "portfolio_updates.jsonl",
        RUN_DIR / "daily_exchange.jsonl",
        CHUNK_DIR / "agent_turns.csv",
        CHUNK_DIR / "agent_turns.jsonl",
        CHUNK_DIR / "submitted_orders.csv",
        CHUNK_DIR / "exchange_fills.csv",
        CHUNK_DIR / "daily_exchange_summary.csv",
        CHUNK_DIR / "portfolio_updates.jsonl",
        CHUNK_DIR / "daily_exchange.jsonl",
    ]:
        if path.exists():
            target = BACKUP_DIR / path.relative_to(RUN_DIR)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


async def _resume_chunk005() -> None:
    _backup_once()
    lock_file = _acquire_sim_db_lock(RUNTIME_DB)
    try:
        metadata = json.loads((CHUNK_DIR / "run_metadata.json").read_text(encoding="utf-8"))
        agents = load_agents_from_sys100(config.SYS_100_DB)[: int(metadata["max_agents"])]
        previous_by_date = _previous_date_map(RUNTIME_DB)
        memory = MemoryAgent(RUNTIME_DB)
        fundamental = FundamentalAgent(RUNTIME_DB)
        news = NewsAgent(
            processed_csv_path=metadata["processed_news_csv"],
            daily_csv_path=metadata["daily_news_csv"],
            include_fake_news=True,
        )
        exchange = ExchangeAgent(RUNTIME_DB)
        prices_by_date = _load_execution_prices(fundamental, ["2026-04-01", "2026-04-02", "2026-04-03"])
        client = OpenRouterClient()
        semaphore = asyncio.Semaphore(int(metadata.get("concurrency") or config.SIMULATION_CONCURRENCY))
        db_write_lock = asyncio.Lock()
        logger = SimulationLogger(
            root_dir=RUN_DIR / "chunks",
            run_id=CHUNK_ID,
            append_existing=True,
            metadata=metadata,
        )

        async def guarded_turn(
            agent: dict[str, Any],
            turn: int,
            day: str,
            market_features_date: str,
            news_max_date: str,
            news_start_date: str | None,
            news_start_time: str | None,
            news_end_time: str | None,
            subturn: str,
            open_price: float,
            previous_close: float,
            execution_reference: str,
        ) -> dict[str, Any] | None:
            async with semaphore:
                try:
                    return await run_agent_turn(
                        agent,
                        turn=turn,
                        date=day,
                        market_features_date=market_features_date,
                        news_max_date=news_max_date,
                        news_start_date=news_start_date,
                        news_start_time=news_start_time,
                        news_end_time=news_end_time,
                        execution_date=day,
                        information_mode="pre_close_cutoff",
                        subturn=subturn,
                        open_price=open_price,
                        previous_close=previous_close,
                        execution_reference=execution_reference,
                        decision_space="buy_sell_only",
                        memory_agent=memory,
                        fundamental_agent=fundamental,
                        news_agent=news,
                        client=client,
                        event_logger=logger,
                        db_write_lock=db_write_lock,
                        community_agent=None,
                    )
                except Exception as exc:
                    logger.log_agent_error(agent=agent, turn=turn, date=day, error=exc)
                    memory.save_system_message(
                        str(agent["agent_id"]),
                        turn,
                        day,
                        message_type="system_error",
                        message="이번 턴은 시스템 오류로 실패 처리되었습니다. 다음 턴에서는 이 실패를 고려해 다시 판단하세요.",
                    )
                    return None

        # Finish the interrupted 2026-04-01 PM turn 6.
        day = "2026-04-01"
        turn = 6
        existing = _existing_agent_events(day, turn, "pm")
        existing_results = [_event_to_turn_result(existing[str(agent["agent_id"])]) for agent in agents if str(agent["agent_id"]) in existing]
        missing_agents = [agent for agent in agents if str(agent["agent_id"]) not in existing]
        prices = prices_by_date[day]
        previous_close = float(fundamental.get_market_features(previous_by_date[day])["close"])
        new_results = [
            result
            for result in await asyncio.gather(
                *(
                    guarded_turn(
                        agent,
                        turn,
                        day,
                        day,
                        day,
                        day,
                        "08:59",
                        config.MARKET_CLOSE_TIME,
                        "pm",
                        prices["open"],
                        previous_close,
                        "close price",
                    )
                    for agent in missing_agents
                )
            )
            if result is not None
        ]
        turn_results = existing_results + new_results
        orders = [result["order"] for result in turn_results if result.get("order") is not None]
        portfolio_snapshots = _portfolio_snapshots(memory, agents, turn - 1)
        results = exchange.process_daily_orders(
            orders,
            {config.STOCK_CODE: float(prices["close"])},
            {config.STOCK_CODE: previous_close},
            current_date=day,
            day_number=3,
            portfolios=portfolio_snapshots,
        )
        for result in results.values():
            result["close_price"] = float(prices["close"])
        logger.log_daily_exchange(date=day, turn=turn, orders=orders, results=results)
        _update_portfolios_from_results(
            memory=memory,
            agents=agents,
            turn=turn,
            date=day,
            orders=orders,
            results=results,
            current_prices={config.STOCK_CODE: float(prices["close"])},
            logger=logger,
        )
        print(f"{day} resumed_pm_turn={turn} existing={len(existing_results)} new={len(new_results)} orders={len(orders)}")

        # Complete the rest of chunk 005 with original turn numbering.
        for day_index, day in [(4, "2026-04-02"), (5, "2026-04-03")]:
            prices = prices_by_date[day]
            previous_close = float(fundamental.get_market_features(previous_by_date[day])["close"])
            am_turn = (day_index - 1) * 2 + 1
            am_results = await _run_subturn(
                subturn="am",
                turn=am_turn,
                day_index=day_index,
                day=day,
                agents=agents,
                guarded_turn=guarded_turn,
                exchange=exchange,
                memory=memory,
                logger=logger,
                announced_price=prices["open"],
                close_price=prices["close"],
                last_price=previous_close,
                current_price=prices["open"],
                market_features_date=previous_by_date[day],
                news_max_date=day,
                news_start_date=previous_by_date[day],
                news_start_time=config.MARKET_CLOSE_TIME,
                news_end_time="08:59",
                open_price=prices["open"],
                previous_close=previous_close,
                execution_reference="open price",
            )
            pm_turn = am_turn + 1
            pm_results = await _run_subturn(
                subturn="pm",
                turn=pm_turn,
                day_index=day_index,
                day=day,
                agents=agents,
                guarded_turn=guarded_turn,
                exchange=exchange,
                memory=memory,
                logger=logger,
                announced_price=prices["close"],
                close_price=prices["close"],
                last_price=previous_close,
                current_price=prices["close"],
                market_features_date=day,
                news_max_date=day,
                news_start_date=day,
                news_start_time="08:59",
                news_end_time=config.MARKET_CLOSE_TIME,
                open_price=prices["open"],
                previous_close=previous_close,
                execution_reference="close price",
            )
            print(
                f"{day} turns={am_turn}/{pm_turn} "
                f"am_orders={am_results['order_count']} am_volume={am_results['volume']} "
                f"pm_orders={pm_results['order_count']} pm_volume={pm_results['volume']}"
            )

        logger.write_json(
            "run_complete.json",
            {
                "run_id": CHUNK_ID,
                "agent_count": len(agents),
                "date_count": 5,
                "information_mode": "pre_close_cutoff",
                "decision_space": "buy_sell_only",
                "log_dir": str(CHUNK_DIR),
            },
        )

        checkpointed = _load_checkpointed_module()
        for filename in checkpointed.CSV_FILES:
            checkpointed._merge_csv(RUN_DIR / filename, CHUNK_DIR / filename)
        for filename in checkpointed.JSONL_FILES:
            checkpointed._merge_jsonl(RUN_DIR / filename, CHUNK_DIR / filename)
        checkpoint_path = RUN_DIR / "checkpoint.json"
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        completed = {int(index) for index in checkpoint.get("completed_chunks", [])}
        completed.add(5)
        checkpoint["completed_chunks"] = sorted(completed)
        checkpointed._write_json(checkpoint_path, checkpoint)
        print("chunk_005 complete and merged")
    finally:
        lock_file.close()


def main() -> None:
    asyncio.run(_resume_chunk005())


if __name__ == "__main__":
    main()
