from __future__ import annotations

import json
import asyncio
from typing import Any

import config
from twinmarket_kr.agents.fundamental_agent import FundamentalAgent
from twinmarket_kr.agents.memory_agent import MemoryAgent
from twinmarket_kr.agents.news_agent import NewsAgent
from twinmarket_kr.community.thinking import community_thinking
from twinmarket_kr.core.collect_context import collect_context
from twinmarket_kr.llm.analysis import analyze_market, depth2_post_search, depth2_pre_search, interpret_news
from twinmarket_kr.llm.belief import update_belief
from twinmarket_kr.llm.client import OpenRouterClient, stable_llm_seed
from twinmarket_kr.llm.decision import build_trading_constraints, make_decision


def _portfolio_numbers(memory_agent: MemoryAgent, agent_id: str, turn: int) -> tuple[float, int]:
    row = memory_agent._latest_portfolio(agent_id, before_or_at_turn=turn)  # internal read for orchestration
    if row is None:
        return 0.0, 0
    current_quantity = 0
    for pos in json.loads(row["positions"]):
        if pos.get("stock_code") == config.STOCK_CODE:
            current_quantity = int(pos.get("quantity", 0))
            break
    return float(row["cash"]), current_quantity


async def run_agent_turn(
    agent: dict[str, Any],
    *,
    turn: int,
    date: str,
    market_features_date: str | None = None,
    news_max_date: str | None = None,
    news_start_date: str | None = None,
    news_start_time: str | None = None,
    news_end_time: str | None = None,
    execution_date: str | None = None,
    information_mode: str = "pre_close_cutoff",
    subturn: str = "full",
    open_price: float | None = None,
    previous_close: float | None = None,
    execution_reference: str | None = None,
    decision_space: str = "buy_sell_only",
    memory_agent: MemoryAgent,
    fundamental_agent: FundamentalAgent,
    news_agent: NewsAgent,
    client: OpenRouterClient | None = None,
    event_logger: Any | None = None,
    db_write_lock: asyncio.Lock | None = None,
    community_agent: Any | None = None,
    random_seed: int = config.RANDOM_SEED,
) -> dict[str, Any]:
    agent_id = str(agent["agent_id"])
    today_context = collect_context(
        agent,
        turn=turn,
        date=date,
        market_features_date=market_features_date,
        news_max_date=news_max_date,
        news_start_date=news_start_date,
        news_start_time=news_start_time,
        news_end_time=news_end_time,
        execution_date=execution_date,
        information_mode=information_mode,
        subturn=subturn,
        open_price=open_price,
        previous_close=previous_close,
        execution_reference=execution_reference,
        memory_agent=memory_agent,
        fundamental_agent=fundamental_agent,
        news_agent=news_agent,
        community_agent=community_agent,
    )
    today_context["news_context"] = news_agent.expand_context_from_selection(
        base_context=today_context["news_context"],
        current_date=today_context["news_max_date"],
    )
    depth2_flow = None
    if int(agent.get("news_depth") if agent.get("news_depth") is not None else 1) >= 2:
        pre_search = await depth2_pre_search(
            agent,
            today_context["news_context"],
            client=client,
            seed=stable_llm_seed(random_seed, agent_id, turn, "depth2_pre_search"),
        )
        search_results = []
        post_search = {
            "new_findings": [],
            "view_change": "유지",
            "view_change_detail": "추가 검색을 수행하지 않았습니다.",
            "unresolved_questions": [],
        }
        if pre_search.get("search_keywords"):
            search_results = news_agent.search_news_flat(
                keywords=list(pre_search.get("search_keywords") or []),
                current_date=today_context["news_max_date"],
                window_end_date=today_context.get("news_max_date"),
                window_end_time=today_context.get("news_end_time")
                or ("08:59" if subturn == "am" else config.MARKET_CLOSE_TIME),
                lookback_days=7,
                top_n=10,
            )
            post_search = await depth2_post_search(
                agent,
                today_context["news_context"],
                search_results,
                pre_search,
                client=client,
                seed=stable_llm_seed(random_seed, agent_id, turn, "depth2_post_search"),
            )
        depth2_flow = {
            "step1_base": {
                "headline_count": len(today_context["news_context"].get("daily_titles") or []),
                "summary_count": len(today_context["news_context"].get("read_contents") or []),
            },
            "step2_pre_search_thinking": pre_search,
            "step3_search": {
                "keywords": list(pre_search.get("search_keywords") or []),
                "result_count": len(search_results),
            },
            "step4_post_search_thinking": post_search,
        }
        today_context["news_context"]["search_results"] = search_results
        today_context["news_context"]["search_read_contents"] = search_results
        today_context["news_context"]["search_result_ids"] = [
            str(row.get("id")) for row in search_results if row.get("id")
        ]
        today_context["news_context"]["depth2_flow"] = depth2_flow
    depth = int(agent.get("news_depth") if agent.get("news_depth") is not None else 1)
    community_log = today_context.get("community_log")
    should_do_community_thinking = (
        config.ENABLE_COMMUNITY
        and depth >= 1
        and community_agent is not None
        and community_log is not None
    )
    if should_do_community_thinking:
        news_interpretation, community_thinking_text = await asyncio.gather(
            interpret_news(
                agent,
                today_context["news_context"],
                client=client,
                seed=stable_llm_seed(random_seed, agent_id, turn, "news_interpretation"),
            ),
            community_thinking(
                agent,
                community_log,
                client=client,
                seed=stable_llm_seed(random_seed, agent_id, turn, "community_thinking"),
            ),
        )
        if db_write_lock is not None:
            async with db_write_lock:
                community_agent.update_community_thinking(
                    agent_id,
                    int(today_context["community_log_turn"]),
                    community_thinking_text,
                )
        else:
            community_agent.update_community_thinking(
                agent_id,
                int(today_context["community_log_turn"]),
                community_thinking_text,
            )
    else:
        news_interpretation = await interpret_news(
            agent,
            today_context["news_context"],
            client=client,
            seed=stable_llm_seed(random_seed, agent_id, turn, "news_interpretation"),
        )
        community_thinking_text = None
    selected_news_raw = news_interpretation.get("selected_news") or []
    influential_news, unresolved_influential_news = news_agent.normalize_influential_news(
        selected_news_raw if isinstance(selected_news_raw, list) else [],
        today_context["news_context"],
    )
    news_interpretation["selected_news_raw"] = selected_news_raw
    news_interpretation["selected_news"] = influential_news
    news_interpretation["unmapped_selected_news"] = unresolved_influential_news
    today_context["news_context"]["influential_news_ids"] = [
        str(row["id"]) for row in influential_news
    ]
    today_context["news_interpretation"] = news_interpretation
    today_context["community_thinking"] = community_thinking_text
    today_belief = await update_belief(
        agent,
        today_context,
        client=client,
        seed=stable_llm_seed(random_seed, agent_id, turn, "belief_update"),
        memory=None,
    )
    if db_write_lock is not None:
        async with db_write_lock:
            memory_agent.save_belief(today_belief)
    else:
        memory_agent.save_belief(today_belief)
    current_price = float(today_context.get("announced_price") or today_context["market_features"]["close"])
    available_cash, current_quantity = _portfolio_numbers(memory_agent, agent["agent_id"], turn - 1)
    constraints = build_trading_constraints(
        available_cash=available_cash,
        current_quantity=current_quantity,
        current_price=current_price,
        price_label=str(today_context.get("price_label") or "공시가"),
        allow_hold=decision_space != "buy_sell_only",
    )
    today_context["trading_constraints"] = constraints
    market_analysis = await analyze_market(
        agent,
        today_belief=today_belief,
        market_features=today_context["market_features"],
        portfolio_summary=today_context["portfolio_summary"],
        news_interpretation=news_interpretation,
        client=client,
        seed=stable_llm_seed(random_seed, agent_id, turn, "market_analysis"),
    )
    decision = await make_decision(
        agent,
        today_belief,
        market_analysis,
        today_context["portfolio_summary"],
        today_context["order_history"],
        constraints,
        allow_hold=decision_space != "buy_sell_only",
        client=client,
        seed=stable_llm_seed(random_seed, agent_id, turn, "trading_decision"),
    )
    trade_log = {
        "agent_id": agent["agent_id"],
        "turn": turn,
        "date": execution_date or date,
        "action": decision["action"],
        "stock_code": config.STOCK_CODE,
        "quantity": decision["quantity"],
        "fee": 0,
        "action_reason": decision["reason"],
        "risk_control": decision["risk_control"],
        "order_type": "announced_price",
        "submitted_price": None,
        "status": "pending" if decision["action"] != "hold" and decision["quantity"] > 0 else "not_submitted",
        "filled_quantity": 0,
    }
    if db_write_lock is not None:
        async with db_write_lock:
            memory_agent.append_trade_log(trade_log)
    else:
        memory_agent.append_trade_log(trade_log)
    order = None
    if decision["action"] != "hold" and decision["quantity"] > 0:
        order = {
            "stock_code": config.STOCK_CODE,
            "user_id": agent["agent_id"],
            "direction": decision["action"],
            "quantity": decision["quantity"],
            "price": current_price,
            "announced_price": current_price,
            "timestamp": float(turn),
            "reason": decision["reason"],
            "decision_date": today_context["decision_date"],
            "market_features_date": today_context["market_features_date"],
            "news_start_date": today_context["news_start_date"],
            "news_start_time": today_context["news_start_time"],
            "news_max_date": today_context["news_max_date"],
            "news_end_time": today_context["news_end_time"],
            "execution_date": today_context["execution_date"],
            "information_mode": today_context["information_mode"],
            "subturn": today_context["subturn"],
            "decision_attempts": decision.get("decision_attempts"),
            "decision_origin": decision.get("decision_origin"),
            "one_share_reason": decision.get("one_share_reason"),
        }
    if event_logger is not None:
        fake_news_audit = news_agent.fake_audit_for_context(
            today_context["news_context"],
            selected_news=news_interpretation.get("selected_news") or [],
        )
        event_logger.log_agent_turn(
            agent=agent,
            turn=turn,
            date=date,
            context=today_context,
            news_interpretation=news_interpretation,
            belief=today_belief,
            market_analysis=market_analysis,
            decision=decision,
            order=order,
            depth2_flow=depth2_flow,
            fake_news_audit=fake_news_audit,
        )
    return {
        "agent": agent,
        "context": today_context,
        "belief": today_belief,
        "decision": decision,
        "order": order,
        "news_interpretation": news_interpretation,
        "market_analysis": market_analysis,
    }
