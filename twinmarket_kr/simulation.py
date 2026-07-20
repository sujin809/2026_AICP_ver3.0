from __future__ import annotations

import asyncio
import csv
import fcntl
import json
import os
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import config
from twinmarket_kr.agents.exchange_agent import ExchangeAgent
from twinmarket_kr.agents.fundamental_agent import FundamentalAgent
from twinmarket_kr.agents.memory_agent import MemoryAgent, load_agents_from_sys100
from twinmarket_kr.agents.news_agent import NewsAgent
from twinmarket_kr.community.agent import CommunityAgent
from twinmarket_kr.community.badge import calculate_badges
from twinmarket_kr.community.posting import posting_decision
from twinmarket_kr.community.reading import community_reading_react, community_reading_select
from twinmarket_kr.core.daily_cycle import run_agent_turn
from twinmarket_kr.db.connection import connect, init_sim_db
from twinmarket_kr.llm.client import OpenRouterClient, stable_llm_seed
from twinmarket_kr.run_logger import SimulationLogger


def trading_dates(limit: int | None = None) -> list[str]:
    return trading_dates_between(limit=limit)


def trading_dates_between(
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int | None = None,
    daily_news_csv_path: Path | str = config.DAILY_NEWS_SELECTION_CSV,
    sim_db_path: Path | str = config.SIM_DB,
) -> list[str]:
    with connect(sim_db_path) as conn:
        rows = conn.execute(
            "SELECT date FROM StockData WHERE stock_id = ? ORDER BY date",
            (config.STOCK_CODE,),
        ).fetchall()
    dates = [str(row["date"]) for row in rows]
    news_dates = _daily_news_dates(daily_news_csv_path)
    if news_dates:
        dates = [day for day in dates if day in news_dates]
    if start_date:
        dates = [day for day in dates if day >= start_date]
    if end_date:
        dates = [day for day in dates if day <= end_date]
    return dates[:limit] if limit else dates


def _stock_trading_dates(sim_db_path: Path | str = config.SIM_DB) -> list[str]:
    with connect(sim_db_path) as conn:
        rows = conn.execute(
            "SELECT date FROM StockData WHERE stock_id = ? ORDER BY date",
            (config.STOCK_CODE,),
        ).fetchall()
    return [str(row["date"]) for row in rows]


def _previous_date_map(sim_db_path: Path | str = config.SIM_DB) -> dict[str, str]:
    dates = _stock_trading_dates(sim_db_path)
    return {day: dates[index - 1] for index, day in enumerate(dates) if index > 0}


def _daily_news_dates(daily_news_csv_path: Path | str = config.DAILY_NEWS_SELECTION_CSV) -> set[str]:
    path = Path(daily_news_csv_path)
    if not path.exists():
        return set()
    with path.open(encoding="utf-8-sig", newline="") as f:
        return {row["date"] for row in csv.DictReader(f) if row.get("date")}


def _isolated_sim_db_path() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return config.OUTPUT_DIR / "runtime_dbs" / f"sim_{timestamp}_{os.getpid()}.db"


def _prepare_sim_db(sim_db: Path | str | None) -> Path:
    if sim_db:
        return Path(sim_db)
    source = config.SIM_DB
    if not source.exists():
        raise RuntimeError("outputs/sim.db not found. Run scripts/03_load_stock_data.py first.")
    target = _isolated_sim_db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    return target


def _acquire_sim_db_lock(sim_db_path: Path) -> Any:
    lock_path = sim_db_path.with_suffix(sim_db_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock_file.close()
        raise RuntimeError(
            f"Simulation DB is already in use: {sim_db_path}. "
            "Use a separate --sim-db path for each concurrent run."
        ) from exc
    lock_file.write(f"pid={os.getpid()}\n")
    lock_file.flush()
    return lock_file


def select_simulation_agents(max_agents: int | None = None) -> list[dict[str, Any]]:
    """Return the deterministic agent cohort used by the simulation."""
    all_agents = load_agents_from_sys100(config.SYS_100_DB)
    if max_agents is None:
        return all_agents
    agents = all_agents[:max_agents]
    return _ensure_depth2_agent(agents, all_agents)


async def run_simulation(
    *,
    max_agents: int | None = None,
    max_days: int | None = None,
    enable_logs: bool = True,
    random_seed: int = config.RANDOM_SEED,
    start_date: str | None = None,
    end_date: str | None = None,
    information_mode: str = "pre_close_cutoff",
    decision_space: str = "buy_sell_only",
    processed_news_csv: Path | str | None = None,
    daily_news_csv: Path | str | None = None,
    fake_news_mode: str = "off",
    fake_news_variant: str | None = None,
    community_mode: str | None = None,
    sim_db: Path | str | None = None,
    reset_runtime_tables: bool = True,
    turn_offset: int = 0,
    day_offset: int = 0,
    log_root: Path | str | None = None,
    log_run_id: str | None = None,
    phases: tuple[str, ...] | None = None,
    append_existing_logs: bool = False,
) -> None:
    if information_mode not in {"pre_close_cutoff", "same_day", "prior_close"}:
        raise ValueError("information_mode must be 'pre_close_cutoff', 'same_day', or 'prior_close'")
    if decision_space != "buy_sell_only":
        raise ValueError("decision_space must be 'buy_sell_only'")
    if fake_news_mode not in {"off", "on"}:
        raise ValueError("fake_news_mode must be 'off' or 'on'")
    if fake_news_variant is not None and fake_news_variant not in {"bearish", "bullish"}:
        raise ValueError("fake_news_variant must be 'bearish', 'bullish', or None")
    if community_mode is None:
        community_mode = "on" if config.ENABLE_COMMUNITY else "off"
    if community_mode not in {"off", "on"}:
        raise ValueError("community_mode must be 'off' or 'on'")
    if turn_offset < 0 or day_offset < 0:
        raise ValueError("turn_offset and day_offset must be non-negative")
    if turn_offset != day_offset * 2:
        raise ValueError("turn_offset must equal day_offset * 2 for the two-turn daily cycle")
    requested_phases = phases or ("am", "pm", "community")
    phase_order = {"am": 0, "pm": 1, "community": 2}
    if not requested_phases or any(phase not in phase_order for phase in requested_phases):
        raise ValueError("phases must contain am, pm, and/or community")
    if list(requested_phases) != sorted(requested_phases, key=phase_order.__getitem__):
        raise ValueError("phases must be ordered as am, pm, community")
    processed_news_path = Path(processed_news_csv) if processed_news_csv else config.PROCESSED_NEWS_CSV
    daily_news_path = Path(daily_news_csv) if daily_news_csv else config.DAILY_NEWS_SELECTION_CSV
    concurrency = config.SIMULATION_CONCURRENCY
    sim_db_path = _prepare_sim_db(sim_db)
    _sim_db_lock = _acquire_sim_db_lock(sim_db_path)
    agents = select_simulation_agents(max_agents)
    previous_by_date = _previous_date_map(sim_db_path)
    uses_previous_market = information_mode in {"pre_close_cutoff", "prior_close"}
    date_limit = None if max_days is None else max_days + (1 if uses_previous_market else 0)
    dates = trading_dates_between(
        start_date=start_date,
        end_date=end_date,
        limit=date_limit,
        daily_news_csv_path=daily_news_path,
        sim_db_path=sim_db_path,
    )
    if uses_previous_market:
        dates = [day for day in dates if day in previous_by_date]
        if max_days:
            dates = dates[:max_days]
    if not dates:
        raise RuntimeError("No StockData rows found. Run scripts/03_load_stock_data.py first.")
    if requested_phases != ("am", "pm", "community") and len(dates) != 1:
        raise ValueError("Phase-level execution accepts exactly one trading date per call")

    if reset_runtime_tables:
        _reset_runtime_tables(sim_db_path)
    memory = MemoryAgent(sim_db_path)
    fundamental = FundamentalAgent(sim_db_path)
    news = NewsAgent(
        processed_csv_path=processed_news_path,
        daily_csv_path=daily_news_path,
        include_fake_news=fake_news_mode == "on",
    )
    exchange = ExchangeAgent(sim_db_path)
    execution_prices = _load_execution_prices(fundamental, dates)
    community_enabled = community_mode == "on"
    community = CommunityAgent(sim_db_path) if community_enabled else None
    client = OpenRouterClient()
    semaphore = asyncio.Semaphore(concurrency)
    db_write_lock = asyncio.Lock()
    logger = (
        SimulationLogger(
            root_dir=log_root or config.LOG_DIR,
            run_id=log_run_id,
            metadata={
                "max_agents": max_agents,
                "max_days": max_days,
                "concurrency": concurrency,
                "agent_count": len(agents),
                "date_count": len(dates),
                "turn_count": len(dates) * 2,
                "sim_db": str(sim_db_path),
                "random_agents": False,
                "random_seed": random_seed,
                "start_date": start_date,
                "end_date": end_date,
                "information_mode": information_mode,
                "decision_space": decision_space,
                "limit_only_orders": False,
                "exchange_mode": "announced_price_binary",
                "agent_selection": "first_n",
                "processed_news_csv": str(processed_news_path),
                "daily_news_csv": str(daily_news_path),
                "fake_news_mode": fake_news_mode,
                "fake_news_variant": fake_news_variant if fake_news_mode == "on" else None,
                "community_mode": community_mode,
                "community_posting": bool(community_enabled and config.ENABLE_COMMUNITY_POSTING),
                "community_reading": bool(community_enabled and config.ENABLE_COMMUNITY_READING),
                "agent_ids": [agent["agent_id"] for agent in agents],
                "agent_depths": {agent["agent_id"]: int(agent.get("news_depth") or 0) for agent in agents},
                "subturns": ["am", "pm"],
                "turn_offset": turn_offset,
                "day_offset": day_offset,
                "global_turn_start": turn_offset + 1,
                "global_turn_end": turn_offset + len(dates) * 2,
                "phases": list(requested_phases),
            },
            append_existing=append_existing_logs,
        )
        if enable_logs
        else None
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
    ) -> dict[str, Any]:
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
                    information_mode=information_mode,
                    subturn=subturn,
                    open_price=open_price,
                    previous_close=previous_close,
                    execution_reference=execution_reference,
                    decision_space=decision_space,
                    memory_agent=memory,
                    fundamental_agent=fundamental,
                    news_agent=news,
                    client=client,
                    event_logger=logger,
                    db_write_lock=db_write_lock,
                    community_agent=community,
                    random_seed=random_seed,
                )
            except Exception as exc:
                if logger is not None:
                    logger.log_agent_error(agent=agent, turn=turn, date=day, error=exc)
                # A missing agent decision makes the subturn scientifically incomplete.
                # Let the checkpoint runner pause and restore the pre-chunk snapshot
                # instead of silently fabricating a system message or continuing.
                raise

    for day_index, day in enumerate(dates, start=1):
        global_day_index = day_offset + day_index
        previous_execution_date = previous_by_date.get(day)
        previous_close = (
            fundamental.get_market_features(previous_execution_date)["close"]
            if previous_execution_date
            else fundamental.get_market_features(day)["close"]
        )
        prices = execution_prices[day]
        if information_mode == "prior_close":
            am_news_max_date = previous_by_date[day]
            pm_news_max_date = previous_by_date[day]
        else:
            am_news_max_date = day
            pm_news_max_date = day

        am_turn = turn_offset + (day_index - 1) * 2 + 1
        am_results = None
        if "am" in requested_phases:
            am_results = await _run_subturn(
                subturn="am",
                turn=am_turn,
                day_index=global_day_index,
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
                market_features_date=previous_by_date[day] if information_mode != "same_day" else day,
                news_max_date=am_news_max_date,
                news_start_date=previous_by_date[day] if information_mode == "pre_close_cutoff" else None,
                news_start_time=config.MARKET_CLOSE_TIME if information_mode == "pre_close_cutoff" else None,
                news_end_time="08:59" if information_mode == "pre_close_cutoff" else None,
                open_price=prices["open"],
                previous_close=previous_close,
                execution_reference="open price",
            )

        pm_turn = am_turn + 1
        pm_results = None
        if "pm" in requested_phases:
            pm_results = await _run_subturn(
                subturn="pm",
                turn=pm_turn,
                day_index=global_day_index,
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
                news_max_date=pm_news_max_date,
                news_start_date=day if information_mode == "pre_close_cutoff" else None,
                news_start_time="08:59" if information_mode == "pre_close_cutoff" else None,
                news_end_time=config.MARKET_CLOSE_TIME if information_mode == "pre_close_cutoff" else None,
                open_price=prices["open"],
                previous_close=previous_close,
                execution_reference="close price",
            )
            if logger is not None:
                _write_pm_posting_payload(logger, day, pm_results)

        if "community" in requested_phases and pm_results is None and community_enabled:
            if logger is None:
                raise RuntimeError("Community-only resume requires experiment logs")
            pm_results = _load_pm_posting_payload(logger.run_dir, day)

        if (
            "community" in requested_phases
            and community_enabled
            and config.ENABLE_COMMUNITY_POSTING
            and community is not None
        ):
            if pm_results is None:
                raise RuntimeError("PM posting payload is unavailable for the community phase")
            await post_trade_posting_phase(
                turn_results=pm_results["turn_results"],
                community_agent=community,
                execution_by_agent=pm_results["execution_by_agent"],
                turn=pm_turn,
                date=day,
                client=client,
                concurrency=concurrency,
                event_logger=logger,
                random_seed=random_seed,
            )
        if "community" in requested_phases and community_enabled and community is not None:
            await community_phase(
                agents=agents,
                community_agent=community,
                memory_agent=memory,
                sim_db_path=sim_db_path,
                turn=pm_turn,
                date=day,
                client=client,
                concurrency=concurrency,
                event_logger=logger,
                random_seed=random_seed,
            )
        am_text = (
            f"am_orders={am_results['order_count']} am_volume={am_results['volume']}"
            if am_results is not None
            else "am=skipped"
        )
        pm_text = (
            f"pm_orders={pm_results['order_count']} pm_volume={pm_results['volume']}"
            if pm_results is not None and "order_count" in pm_results
            else "pm=skipped_or_loaded"
        )
        print(f"{day} turns={am_turn}/{pm_turn} phases={','.join(requested_phases)} {am_text} {pm_text}")
    if logger is not None:
        completion_filename = (
            "run_complete.json"
            if requested_phases == ("am", "pm", "community")
            else f"phase_complete_{dates[-1]}_{requested_phases[-1]}.json"
        )
        logger.write_json(
            completion_filename,
            {
                "run_id": logger.run_id,
                "agent_count": len(agents),
                "date_count": len(dates),
                "information_mode": information_mode,
                "decision_space": decision_space,
                "turn_offset": turn_offset,
                "day_offset": day_offset,
                "global_turn_start": turn_offset + 1,
                "global_turn_end": turn_offset + len(dates) * 2,
                "phases": list(requested_phases),
                "log_dir": str(logger.run_dir),
            },
        )
        print(f"log_dir={logger.run_dir}")


def _write_pm_posting_payload(
    logger: SimulationLogger,
    day: str,
    pm_results: dict[str, Any],
) -> None:
    logger.write_json(
        f"pm_posting_payload_{day}.json",
        {
            "turn_results": [
                {
                    "agent": result["agent"],
                    "belief": result["belief"],
                    "decision": result["decision"],
                }
                for result in pm_results["turn_results"]
            ],
            "execution_by_agent": pm_results["execution_by_agent"],
        },
    )


def _load_pm_posting_payload(run_dir: Path, day: str) -> dict[str, Any]:
    path = run_dir / f"pm_posting_payload_{day}.json"
    if not path.exists():
        raise RuntimeError(f"PM posting payload not found for community resume: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not payload.get("turn_results") or "execution_by_agent" not in payload:
        raise RuntimeError(f"PM posting payload is incomplete: {path}")
    return {
        "turn_results": payload["turn_results"],
        "execution_by_agent": payload["execution_by_agent"],
    }


def _load_execution_prices(fundamental: FundamentalAgent, dates: list[str]) -> dict[str, dict[str, float]]:
    prices: dict[str, dict[str, float]] = {}
    for day in dates:
        daily = fundamental.get_daily_prices(day)
        prices[day] = {
            "open": float(daily["open"]),
            "close": float(daily["close"]),
        }
    return prices


async def _run_subturn(
    *,
    subturn: str,
    turn: int,
    day_index: int,
    day: str,
    agents: list[dict[str, Any]],
    guarded_turn: Any,
    exchange: ExchangeAgent,
    memory: MemoryAgent,
    logger: SimulationLogger | None,
    announced_price: float,
    close_price: float,
    last_price: float,
    current_price: float,
    market_features_date: str,
    news_max_date: str,
    news_start_date: str | None,
    news_start_time: str | None,
    news_end_time: str | None,
    open_price: float,
    previous_close: float,
    execution_reference: str,
) -> dict[str, Any]:
    parallel_results = await asyncio.gather(
        *(
            guarded_turn(
                agent,
                turn,
                day,
                market_features_date,
                news_max_date,
                news_start_date,
                news_start_time,
                news_end_time,
                subturn,
                open_price,
                previous_close,
                execution_reference,
            )
            for agent in agents
        ),
        return_exceptions=True,
    )
    turn_errors = [result for result in parallel_results if isinstance(result, BaseException)]
    if turn_errors:
        details = "; ".join(
            f"{type(error).__name__}: {error}" for error in turn_errors[:5]
        )
        raise RuntimeError(
            f"{len(turn_errors)} agent turn(s) failed after all parallel tasks settled: {details}"
        ) from turn_errors[0]
    turn_results = [result for result in parallel_results if isinstance(result, dict)]
    if len(turn_results) != len(agents):
        raise RuntimeError(
            f"parallel turn result count mismatch: results={len(turn_results)} agents={len(agents)}"
        )
    orders = [result["order"] for result in turn_results if result.get("order") is not None]
    portfolio_snapshots = _portfolio_snapshots(memory, agents, turn - 1)
    results = exchange.process_daily_orders(
        orders,
        {config.STOCK_CODE: float(announced_price)},
        {config.STOCK_CODE: float(last_price)},
        current_date=day,
        day_number=day_index,
        portfolios=portfolio_snapshots,
    )
    for result in results.values():
        result["close_price"] = float(close_price)
    if logger is not None:
        logger.log_daily_exchange(date=day, turn=turn, orders=orders, results=results)
    execution_by_agent = _update_portfolios_from_results(
        memory=memory,
        agents=agents,
        turn=turn,
        date=day,
        orders=orders,
        results=results,
        current_prices={config.STOCK_CODE: float(current_price)},
        logger=logger,
    )
    return {
        "turn_results": turn_results,
        "execution_by_agent": execution_by_agent,
        "order_count": len(orders),
        "volume": results[config.STOCK_CODE]["volume"],
    }


def _portfolio_snapshots(memory: MemoryAgent, agents: list[dict[str, Any]], turn: int) -> dict[str, dict[str, Any]]:
    snapshots: dict[str, dict[str, Any]] = {}
    for agent in agents:
        agent_id = str(agent["agent_id"])
        row = memory._latest_portfolio(agent_id, before_or_at_turn=turn)
        if row is None:
            raise ValueError(f"portfolio not found for {agent_id} at turn {turn}")
        position = 0
        for pos in json.loads(row["positions"]):
            if pos.get("stock_code") == config.STOCK_CODE:
                position = int(pos.get("quantity", 0))
                break
        snapshots[agent_id] = {
            "cash": float(row["cash"]),
            "position": position,
        }
    return snapshots


def _ensure_depth2_agent(agents: list[dict[str, Any]], all_agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if any(int(agent.get("news_depth") or 0) >= 2 for agent in agents):
        return agents
    depth2_agents = [agent for agent in all_agents if int(agent.get("news_depth") or 0) >= 2]
    if not depth2_agents:
        raise RuntimeError("테스트 실행에 Depth 2 에이전트가 최소 1명 필요합니다. sys_100.db를 확인하세요.")
    if not agents:
        return depth2_agents[:1]
    replaced = [*agents[:-1], depth2_agents[0]]
    return sorted({agent["agent_id"]: agent for agent in replaced}.values(), key=lambda agent: agent["agent_id"])


def _update_portfolios_from_results(
    *,
    memory: MemoryAgent,
    agents: list[dict[str, Any]],
    turn: int,
    date: str,
    orders: list[dict[str, Any]],
    results: dict[str, dict[str, Any]],
    current_prices: dict[str, float],
    logger: SimulationLogger | None,
) -> dict[str, dict[str, Any]]:
    fills_by_agent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for stock_code, result in results.items():
        for tx in result.get("transactions") or []:
            user_id = str(tx.get("agent_id") or tx.get("user_id") or "")
            if not user_id:
                continue
            quantity = int(tx.get("quantity") or tx.get("executed_quantity", 0))
            price = float(tx.get("executed_price", 0))
            fills_by_agent[user_id].append(
                {
                    "user_id": user_id,
                    "stock_code": tx.get("stock_code", stock_code),
                    "direction": tx.get("action") or tx.get("direction"),
                    "quantity": quantity,
                    "price": price,
                    "fee": 0.0,
                }
            )
    submitted_agent_ids = {str(order.get("user_id")) for order in orders if order.get("user_id")}
    for agent_id in submitted_agent_ids:
        fills = fills_by_agent.get(agent_id, [])
        filled_quantity = sum(int(fill["quantity"]) for fill in fills)
        total_value = sum(float(fill["quantity"]) * float(fill["price"]) for fill in fills)
        total_fee = sum(float(fill.get("fee", 0)) for fill in fills)
        executed_price = total_value / filled_quantity if filled_quantity else None
        memory.update_trade_execution(
            agent_id,
            turn,
            filled_quantity=filled_quantity,
            executed_price=executed_price,
            fee=total_fee,
        )
    execution_by_agent: dict[str, dict[str, Any]] = {}
    for agent in agents:
        agent_id = str(agent["agent_id"])
        fills = fills_by_agent.get(agent_id, [])
        filled_quantity = sum(int(fill["quantity"]) for fill in fills)
        total_value = sum(float(fill["quantity"]) * float(fill["price"]) for fill in fills)
        total_fee = sum(float(fill.get("fee", 0)) for fill in fills)
        execution_by_agent[agent_id] = {
            "fills": fills,
            "filled_quantity": filled_quantity,
            "executed_price": total_value / filled_quantity if filled_quantity else None,
            "fee": total_fee,
        }
        state = memory.update_portfolio(
            agent_id,
            turn,
            date,
            fills,
            current_prices=current_prices,
        )
        if logger is not None:
            logger.write_jsonl(
                "portfolio_updates.jsonl",
                {
                    "run_id": logger.run_id,
                    "event": "portfolio_update",
                    "date": date,
                    "turn": turn,
                    "agent_id": agent_id,
                    "fills": fills,
                    "state": state,
                },
            )
    return execution_by_agent


async def post_trade_posting_phase(
    *,
    turn_results: list[dict[str, Any]],
    community_agent: CommunityAgent,
    execution_by_agent: dict[str, dict[str, Any]],
    turn: int,
    date: str,
    client: OpenRouterClient,
    concurrency: int = config.SIMULATION_CONCURRENCY,
    event_logger: SimulationLogger | None = None,
    random_seed: int = config.RANDOM_SEED,
) -> None:
    active_results = [
        result
        for result in turn_results
        if int(result.get("agent", {}).get("news_depth") or 0) >= 1
    ]
    if not active_results:
        return

    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def _one_post(result: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        async with semaphore:
            agent = result["agent"]
            agent_id = str(agent["agent_id"])
            post_result = await posting_decision(
                agent,
                today_belief=result["belief"],
                decision=result["decision"],
                date=date,
                execution_summary=execution_by_agent.get(agent_id, {}),
                client=client,
                seed=stable_llm_seed(random_seed, agent_id, turn, "community_posting"),
            )
            if post_result is None:
                return agent_id, None
            return agent_id, post_result

    parallel_posts = await asyncio.gather(
        *(_one_post(result) for result in active_results),
        return_exceptions=True,
    )
    posting_errors = [result for result in parallel_posts if isinstance(result, BaseException)]
    if posting_errors:
        details = "; ".join(
            f"{type(error).__name__}: {error}" for error in posting_errors[:5]
        )
        raise RuntimeError(
            f"{len(posting_errors)} community posting call(s) failed: {details}"
        ) from posting_errors[0]
    generated_posts = [result for result in parallel_posts if isinstance(result, tuple)]
    if len(generated_posts) != len(active_results):
        raise RuntimeError("community posting result count mismatch")
    # LLM calls remain parallel, but persistent IDs must not depend on response latency.
    for agent_id, post_result in sorted(generated_posts, key=lambda item: item[0]):
        if post_result is None:
            continue
        post_id = community_agent.save_post(
            agent_id=agent_id,
            turn=turn,
            date=date,
            post_type=post_result["post_type"],
            title=post_result["title"],
            content=post_result["content"],
        )
        if event_logger is not None:
            event_logger.log_community_post(
                agent_id=agent_id,
                turn=turn,
                date=date,
                post={**post_result, "post_id": post_id},
            )


async def community_phase(
    *,
    agents: list[dict[str, Any]],
    community_agent: CommunityAgent,
    memory_agent: MemoryAgent,
    sim_db_path: Path | str,
    turn: int,
    date: str,
    client: OpenRouterClient,
    concurrency: int = config.SIMULATION_CONCURRENCY,
    event_logger: SimulationLogger | None = None,
    random_seed: int = config.RANDOM_SEED,
) -> None:
    if not config.ENABLE_COMMUNITY_READING:
        best_posts = community_agent.mark_best_posts(date, config.COMMUNITY_BEST_POST_COUNT)
        if event_logger is not None:
            event_logger.log_community_best_posts(turn=turn, date=date, best_posts=best_posts)
        for agent in agents:
            if int(agent.get("news_depth") or 0) >= 1:
                community_agent.save_community_log(
                    agent_id=str(agent["agent_id"]),
                    turn=turn,
                    date=date,
                    best_posts=best_posts,
                    posts_read=[],
                    thinking="",
                )
                if event_logger is not None:
                    event_logger.log_community_log(
                        agent_id=str(agent["agent_id"]),
                        turn=turn,
                        date=date,
                        best_posts=best_posts,
                        posts_read=[],
                    )
        return

    badges = calculate_badges(agents, memory_agent, turn, str(sim_db_path))
    active_agents = [agent for agent in agents if int(agent.get("news_depth") or 0) >= 1]
    if not active_agents:
        community_agent.mark_best_posts(date, config.COMMUNITY_BEST_POST_COUNT)
        return

    semaphore = asyncio.Semaphore(max(1, concurrency))
    post_list_snapshot = community_agent.get_today_posts(date)

    async def _one_agent_reading(agent: dict[str, Any]) -> tuple[str, list[int], list[dict[str, Any]]]:
        async with semaphore:
            depth = int(agent.get("news_depth") or 0)
            read_limit = (
                config.COMMUNITY_DEPTH2_READ_LIMIT
                if depth >= 2
                else config.COMMUNITY_DEPTH1_READ_LIMIT
            )
            agent_id = str(agent["agent_id"])
            if not post_list_snapshot:
                return agent_id, [], []
            visible_posts = [
                {**post, "author_badges": badges.get(str(post["agent_id"]), [])}
                for post in post_list_snapshot
                if str(post["agent_id"]) != agent_id
            ]
            if event_logger is not None:
                event_logger.log_community_selection_input(
                    agent_id=agent_id,
                    turn=turn,
                    date=date,
                    depth=depth,
                    read_limit=read_limit,
                    visible_posts=visible_posts,
                )
            selected_ids = await community_reading_select(
                agent,
                visible_posts,
                read_limit,
                client=client,
                seed=stable_llm_seed(random_seed, agent_id, turn, "community_read_select"),
            )
            if not selected_ids:
                return agent_id, [], []

            posts_content: list[dict[str, Any]] = []
            for post_id in selected_ids:
                content = community_agent.get_post_content(post_id)
                if not content or str(content.get("agent_id")) == agent_id:
                    continue
                content["author_badges"] = badges.get(str(content.get("agent_id")), [])
                content["author_profile"] = (
                    community_agent.get_author_profile(str(content["agent_id"]), memory_agent, turn)
                    if depth >= 2
                    else None
                )
                posts_content.append(content)

            reactions = await community_reading_react(
                agent,
                posts_content,
                client=client,
                seed=stable_llm_seed(random_seed, agent_id, turn, "community_read_react"),
            )
            reaction_map = {int(item["post_id"]): item["reaction"] for item in reactions}
            posts_read: list[dict[str, Any]] = []
            for post in posts_content:
                post_id = int(post["post_id"])
                reaction = reaction_map.get(post_id, "read")
                posts_read.append(
                    {
                        "post_id": post_id,
                        "title": post.get("title", ""),
                        "post_type": post.get("post_type", ""),
                        "content": post.get("content", ""),
                        "reaction": "read" if reaction == "none" else reaction,
                        "author_badges": post.get("author_badges") or [],
                        "author_profile": post.get("author_profile"),
                    }
                )
            return agent_id, selected_ids, posts_read

    parallel_reading = await asyncio.gather(
        *(_one_agent_reading(agent) for agent in active_agents),
        return_exceptions=True,
    )
    reading_errors = [result for result in parallel_reading if isinstance(result, BaseException)]
    if reading_errors:
        details = "; ".join(
            f"{type(error).__name__}: {error}" for error in reading_errors[:5]
        )
        raise RuntimeError(
            f"{len(reading_errors)} community reading call(s) failed: {details}"
        ) from reading_errors[0]
    results = [result for result in parallel_reading if isinstance(result, tuple)]
    if len(results) != len(active_agents):
        raise RuntimeError("community reading result count mismatch")
    # Apply reactions only after every agent has seen the same frozen board.
    for agent_id, selected_ids, posts_read in sorted(results, key=lambda item: item[0]):
        for post in posts_read:
            reaction = str(post.get("reaction") or "read")
            post_id = int(post["post_id"])
            recorded = community_agent.record_reaction(agent_id, post_id, turn, date, reaction)
            if recorded and reaction in {"like", "unlike"}:
                community_agent.update_post_score_live(post_id, reaction)
        if event_logger is not None:
            event_logger.log_community_reading(
                agent_id=agent_id,
                turn=turn,
                date=date,
                selected_post_ids=selected_ids,
                posts_read=posts_read,
            )
    best_posts = community_agent.mark_best_posts(date, config.COMMUNITY_BEST_POST_COUNT)
    if event_logger is not None:
        event_logger.log_community_best_posts(turn=turn, date=date, best_posts=best_posts)
    for agent_id, _selected_ids, posts_read in results:
        community_agent.save_community_log(
            agent_id=agent_id,
            turn=turn,
            date=date,
            best_posts=best_posts,
            posts_read=posts_read,
            thinking="",
        )
        if event_logger is not None:
            event_logger.log_community_log(
                agent_id=agent_id,
                turn=turn,
                date=date,
                best_posts=best_posts,
                posts_read=posts_read,
            )
    print(f"  community_phase done: {len(active_agents)} agents, {len(best_posts)} best posts")


def _reset_runtime_tables(db_path: str) -> None:
    init_sim_db(db_path)
    with connect(db_path) as conn:
        conn.execute("DELETE FROM TradingDetails")
        conn.execute("DELETE FROM trade_log")
        conn.execute("DELETE FROM belief_history WHERE turn > 0")
        conn.execute("DELETE FROM portfolio_state WHERE turn > 0")
        conn.execute("DELETE FROM community_posts")
        conn.execute("DELETE FROM community_interactions")
        conn.execute("DELETE FROM community_logs WHERE turn > 0")
        conn.execute("DELETE FROM agent_system_messages")
        runtime_tables = (
            "TradingDetails",
            "trade_log",
            "community_posts",
            "community_interactions",
            "agent_system_messages",
        )
        placeholders = ",".join("?" for _ in runtime_tables)
        conn.execute(
            f"DELETE FROM sqlite_sequence WHERE name IN ({placeholders})",
            runtime_tables,
        )
        conn.commit()
