#!/usr/bin/env python3
"""Reproducible audit of the six committed 2026-07-15 condition outputs.

This script deliberately separates execution-integrity evidence from behavioral
metrics.  It does not attempt to infer a causal effect from selected/read
news fields, which are post-treatment mechanisms.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = Path(__file__).resolve().parent / "outputs"

OFFICIAL_RUNS = [
    {
        "condition": "C00",
        "label": "community off · fake off",
        "run_id": "simulation_20260715_30agents_commoff_fakenews_off_20260227_20260601",
        "community": "off",
        "fake": "off",
    },
    {
        "condition": "C01",
        "label": "community off · bearish fake",
        "run_id": "simulation_20260715_30agents_commoff_fakenews_bearish",
        "community": "off",
        "fake": "bearish",
    },
    {
        "condition": "C02",
        "label": "community off · bullish fake",
        "run_id": "simulation_20260715_community_off_fake_on_bullish_30_20260227_20260601",
        "community": "off",
        "fake": "bullish",
    },
    {
        "condition": "C10",
        "label": "community on · fake off",
        "run_id": "simulation_checkpointed_20260715_000747_261881",
        "community": "on",
        "fake": "off",
    },
    {
        "condition": "C11",
        "label": "community on · bearish fake",
        "run_id": "simulation_20260715_bearish_30_20260227_20260601",
        "community": "on",
        "fake": "bearish",
    },
    {
        "condition": "C12",
        "label": "community on · bullish fake",
        "run_id": "simulation_20260715_000826_158697_23249",
        "community": "on",
        "fake": "bullish",
    },
]

INITIAL_PORTFOLIO_PHRASE = "보유 현금 100,000,000원, 현재 보유 종목 없음"


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def get(mapping: dict[str, Any], *keys: str, default: Any = None) -> Any:
    value: Any = mapping
    for key in keys:
        if not isinstance(value, dict):
            return default
        value = value.get(key)
    return default if value is None else value


def as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def write_csv(name: str, rows: list[dict[str, Any]]) -> None:
    path = OUTPUT_DIR / name
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(rows[0])
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def primary_metrics(summary: dict[str, Any]) -> dict[str, Any]:
    return (
        get(summary, "primary_metrics", "value", default={})
        or get(summary, "value", "primary_metrics", default={})
        or {}
    )


def baseline_metrics(summary: dict[str, Any]) -> dict[str, Any]:
    return get(summary, "value", "baselines_vs_individuals", default={}) or {}


def audit_run(spec: dict[str, str]) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    run_dir = PROJECT_ROOT / "outputs" / "logs" / spec["run_id"]
    validation_dir = PROJECT_ROOT / "validation" / "outputs" / spec["run_id"]
    records = load_jsonl(run_dir / "agent_turns.jsonl")
    metadata = load_json(run_dir / "run_metadata.json")
    summary = load_json(validation_dir / "summary_metrics.json")

    key_counts: Counter[tuple[str, str, str]] = Counter()
    agents: set[str] = set()
    dates: set[str] = set()
    turns: list[int] = []
    restart_dates: set[str] = set()
    restart_rows: list[dict[str, Any]] = []
    fake_event_rows: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    daily_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    missing_belief_by_agent: Counter[str] = Counter()
    total_by_agent: Counter[str] = Counter()

    buy_count = sell_count = one_share_count = missing_belief_count = 0
    fallback_count = fallback_buy_count = fallback_sell_count = 0
    one_share_reason_missing_count = 0
    fake_exposure_count = fake_read_count = fake_search_count = fake_selected_count = 0
    exposed_nonblank_belief = exposure_buy = exposure_sell = 0

    for record in records:
        context = record.get("context") or {}
        agent = record.get("agent") or {}
        decision = record.get("decision") or {}
        belief = record.get("belief") or {}
        audit = record.get("fake_news_audit") or {}
        date = str(record.get("date") or context.get("date") or "")
        subturn = str(context.get("subturn") or "")
        agent_id = str(agent.get("agent_id") or context.get("agent_id") or "")
        turn = as_int(record.get("turn"))
        portfolio = str(context.get("portfolio_summary") or "")
        action = str(decision.get("action") or "")
        quantity = as_int(decision.get("quantity"))
        belief_summary = str(belief.get("belief_summary") or "").strip()

        if agent_id and date and subturn:
            key_counts[(agent_id, date, subturn)] += 1
        agents.add(agent_id)
        dates.add(date)
        if turn is not None:
            turns.append(turn)
        daily_rows[date].append(record)
        total_by_agent[agent_id] += 1

        if action == "buy":
            buy_count += 1
        elif action == "sell":
            sell_count += 1
        if quantity == 1:
            one_share_count += 1
            if not decision.get("one_share_reason"):
                one_share_reason_missing_count += 1
        if not belief_summary:
            missing_belief_count += 1
            missing_belief_by_agent[agent_id] += 1

        if str(decision.get("reason") or "").startswith("fallback_decision_after_invalid_llm_output"):
            fallback_count += 1
            if action == "buy":
                fallback_buy_count += 1
            elif action == "sell":
                fallback_sell_count += 1

        if turn == 1 and subturn == "am":
            restart_dates.add(date)
            restart_rows.append(record)

        if bool(audit.get("fake_exposed")):
            fake_exposure_count += 1
            fake_event_rows[(date, subturn)].append(record)
            if belief_summary:
                exposed_nonblank_belief += 1
            if action == "buy":
                exposure_buy += 1
            elif action == "sell":
                exposure_sell += 1
        fake_read_count += as_int(audit.get("fake_read_count")) or 0
        fake_search_count += as_int(audit.get("fake_search_count")) or 0
        fake_selected_count += as_int(audit.get("fake_selected_count")) or 0

    expected_keys = {(agent, date, subturn) for agent in agents for date in dates for subturn in ("am", "pm")}
    observed_keys = set(key_counts)
    duplicate_keys = sorted(key for key, count in key_counts.items() if count > 1)
    missing_keys = sorted(expected_keys - observed_keys)
    restart_initial_count = sum(
        INITIAL_PORTFOLIO_PHRASE in str((record.get("context") or {}).get("portfolio_summary") or "")
        for record in restart_rows
    )

    primary = primary_metrics(summary)
    validation_value = summary.get("value") or {}
    overlap_days = as_int(validation_value.get("overlap_days")) or as_int(get(validation_value, "llm_vs_individuals", "days")) or 0
    direction_match = as_float(primary.get("direction_match_rate"))
    summary_row = {
        **spec,
        "raw_rows": len(records),
        "expected_agent_date_subturn_rows": len(expected_keys),
        "unique_agent_date_subturn_rows": len(observed_keys),
        "duplicate_key_rows": sum(count - 1 for count in key_counts.values() if count > 1),
        "duplicate_key_examples": json.dumps(duplicate_keys[:5], ensure_ascii=False),
        "missing_key_rows": len(missing_keys),
        "missing_key_examples": json.dumps(missing_keys[:5], ensure_ascii=False),
        "agent_count": len(agents),
        "date_count": len(dates),
        "turn_min": min(turns) if turns else None,
        "turn_max": max(turns) if turns else None,
        "distinct_turn_count": len(set(turns)),
        "restart_date_count": len(restart_dates),
        "restart_dates": "; ".join(sorted(restart_dates)),
        "restart_rows_with_initial_portfolio": restart_initial_count,
        "buy_orders": buy_count,
        "sell_orders": sell_count,
        "one_share_orders": one_share_count,
        "one_share_rate": round(one_share_count / len(records), 4) if records else None,
        "one_share_orders_without_reason_code": one_share_reason_missing_count,
        "fallback_decision_rows": fallback_count,
        "fallback_decision_rate": round(fallback_count / len(records), 4) if records else None,
        "fallback_buy_orders": fallback_buy_count,
        "fallback_sell_orders": fallback_sell_count,
        "blank_belief_rows": missing_belief_count,
        "blank_belief_rate": round(missing_belief_count / len(records), 4) if records else None,
        "fake_exposure_rows": fake_exposure_count,
        "fake_exposure_slots": len(fake_event_rows),
        "fake_expected_rows_if_30_slots": len(agents) * 30 if spec["fake"] != "off" else 0,
        "fake_read_count": fake_read_count,
        "fake_search_count": fake_search_count,
        "fake_selected_count": fake_selected_count,
        "exposed_rows_with_nonblank_belief": exposed_nonblank_belief,
        "exposure_buy": exposure_buy,
        "exposure_sell": exposure_sell,
        "metadata_concurrency": metadata.get("concurrency"),
        "metadata_chunk_days": metadata.get("chunk_days"),
        "validation_overlap_days": overlap_days,
        "direction_match_rate": direction_match,
        "direction_match_days": round(direction_match * overlap_days) if direction_match is not None else None,
        "buy_recall": as_float(primary.get("buy_recall")),
        "sell_recall": as_float(primary.get("sell_recall")),
        "balanced_accuracy": as_float(primary.get("balanced_accuracy")),
        "daily_pearson": as_float(get(summary, "reference_metrics", "value", "pearson_daily"))
        or as_float(get(summary, "value", "reference_metrics", "pearson_daily")),
    }

    benchmark_rows: list[dict[str, Any]] = []
    for baseline_name, values in baseline_metrics(summary).items():
        benchmark_rows.append(
            {
                "condition": spec["condition"],
                "run_id": spec["run_id"],
                "benchmark": baseline_name,
                "overlap_days": overlap_days,
                "direction_match_rate": as_float(values.get("direction_match_rate")),
                "balanced_accuracy": as_float(values.get("balanced_accuracy")),
                "buy_recall": as_float(values.get("buy_recall")),
                "sell_recall": as_float(values.get("sell_recall")),
            }
        )

    event_rows: list[dict[str, Any]] = []
    for (date, subturn), event_records in sorted(fake_event_rows.items()):
        item_ids = sorted(
            {
                item_id
                for record in event_records
                for item_id in ((record.get("fake_news_audit") or {}).get("fake_public_ids") or [])
            }
        )
        event_rows.append(
            {
                "condition": spec["condition"],
                "run_id": spec["run_id"],
                "date": date,
                "subturn": subturn,
                "fake_ids": "; ".join(item_ids),
                "exposed_agents": len(event_records),
                "fake_read_count": sum(as_int((record.get("fake_news_audit") or {}).get("fake_read_count")) or 0 for record in event_records),
                "fake_selected_count": sum(as_int((record.get("fake_news_audit") or {}).get("fake_selected_count")) or 0 for record in event_records),
                "nonblank_belief_count": sum(bool(str((record.get("belief") or {}).get("belief_summary") or "").strip()) for record in event_records),
                "buy_count": sum((record.get("decision") or {}).get("action") == "buy" for record in event_records),
                "sell_count": sum((record.get("decision") or {}).get("action") == "sell" for record in event_records),
            }
        )

    belief_rows = [
        {
            "condition": spec["condition"],
            "run_id": spec["run_id"],
            "agent_id": agent_id,
            "turn_rows": total_by_agent[agent_id],
            "blank_belief_rows": missing_belief_by_agent[agent_id],
            "blank_belief_rate": round(missing_belief_by_agent[agent_id] / total_by_agent[agent_id], 4),
        }
        for agent_id in sorted(agents)
    ]
    return summary_row, benchmark_rows, event_rows, belief_rows


def persona_rows(active_agent_ids: set[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    database = PROJECT_ROOT / "outputs" / "sys_100.db"
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        population = [dict(row) for row in connection.execute(
            "SELECT agent_id, gender, age_group, ini_cash, strategy, news_depth, user_type, "
            "bh_disposition_effect_category, bh_lottery_preference_category FROM agents"
        )]
    finally:
        connection.close()

    active = [row for row in population if row["agent_id"] in active_agent_ids]
    cohort = [
        {
            "agent_id": row["agent_id"],
            "gender": row["gender"],
            "age_group": row["age_group"],
            "initial_cash_krw": row["ini_cash"],
            "strategy": row["strategy"],
            "news_depth": row["news_depth"],
            "user_type": row["user_type"],
            "disposition_effect": row["bh_disposition_effect_category"],
            "lottery_preference": row["bh_lottery_preference_category"],
        }
        for row in sorted(active, key=lambda item: item["agent_id"])
    ]
    comparison: list[dict[str, Any]] = []
    for field in ("gender", "age_group", "ini_cash", "strategy", "news_depth", "user_type"):
        population_counts = Counter(str(row[field]) for row in population)
        active_counts = Counter(str(row[field]) for row in active)
        for value in sorted(set(population_counts) | set(active_counts)):
            comparison.append(
                {
                    "field": field,
                    "value": value,
                    "population_count": population_counts[value],
                    "population_share": round(population_counts[value] / len(population), 4),
                    "active_cohort_count": active_counts[value],
                    "active_cohort_share": round(active_counts[value] / len(active), 4),
                }
            )
    return cohort, comparison


def classify_log(relative_path: Path) -> tuple[str, str, str]:
    """Return a reader-facing category, grain, and use for a C00 artifact."""
    name = relative_path.name
    text = str(relative_path)
    if name == "agent_turns.jsonl" or name == "agent_turns.csv":
        return ("decision trace", "agent × AM/PM turn", "news/context, belief, rationale, action, quantity")
    if name == "submitted_orders.csv":
        return ("orders", "submitted order", "requested buy/sell orders before fills")
    if name == "exchange_fills.csv":
        return ("fills", "executed fill", "prices, quantities, and execution")
    if name == "daily_exchange_summary.csv" or name == "daily_exchange.jsonl":
        return ("daily market summary", "date × subturn", "aggregate order/fill counts, price, and volume")
    if name == "portfolio_updates.jsonl":
        return ("portfolio state", "agent × post-fill turn", "cash, holdings, asset value, and PnL")
    if name == "run_metadata.json":
        return ("run specification", "run", "period, condition flags, concurrency, and input paths")
    if name == "run_complete.json":
        return ("completion marker", "run", "whether the recorded run completed")
    if name == "summary_metrics.json":
        return ("validation summary", "run", "direction metrics and repository baseline comparisons")
    if name.startswith("daily_comparison_"):
        return ("validation daily comparison", "date", "C00 net flow versus real investor flow and direction match")
    if name.startswith("normalized_comparison_"):
        return ("validation normalized comparison", "date", "scale-normalized C00 and real investor flow comparison")
    if name == "validation_report.pdf":
        return ("validation report", "run", "rendered validation summary; inspect against underlying CSV/JSON")
    if "up_market_" in text or "down_market_" in text:
        return ("validation regime subset", "date", "exploratory market-regime subset; confirm skip-window consistency")
    if name == "checkpoint.json":
        return ("checkpoint", "chunk", "resume state; not a behavioral outcome")
    if "checkpoints" in text or name.endswith(".db") or name.endswith(".db-wal") or name.endswith(".db-shm"):
        return ("runtime/checkpoint state", "internal", "recovery artifact; inspect only for integrity diagnosis")
    if name.startswith("community_"):
        return ("community trace", "post/read/reaction", "should be empty or irrelevant under C00 community off")
    return ("supporting artifact", "file", "auxiliary run evidence")


def line_count(path: Path) -> int | None:
    if path.suffix not in {".csv", ".jsonl", ".json", ".log", ".txt"}:
        return None
    try:
        with path.open("rb") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return None


def c00_log_inventory(spec: dict[str, str]) -> list[dict[str, Any]]:
    run_dir = PROJECT_ROOT / "outputs" / "logs" / spec["run_id"]
    validation_dir = PROJECT_ROOT / "validation" / "outputs" / spec["run_id"]
    rows: list[dict[str, Any]] = []
    for surface, base in (("run", run_dir), ("validation", validation_dir)):
        for path in sorted(candidate for candidate in base.rglob("*") if candidate.is_file()):
            relative = path.relative_to(PROJECT_ROOT)
            category, grain, use = classify_log(relative)
            scope = "root" if "chunks/" not in str(relative) else "chunk copy"
            rows.append(
                {
                    "surface": surface,
                    "scope": scope,
                    "path": str(relative),
                    "category": category,
                    "grain": grain,
                    "interpretation_use": use,
                    "bytes": path.stat().st_size,
                    "line_count": line_count(path),
                }
            )
    return rows


def c00_daily_decision_rows(spec: dict[str, str]) -> list[dict[str, Any]]:
    run_dir = PROJECT_ROOT / "outputs" / "logs" / spec["run_id"]
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in load_jsonl(run_dir / "agent_turns.jsonl"):
        context = record.get("context") or {}
        grouped[(str(record.get("date") or context.get("date")), str(context.get("subturn") or ""))].append(record)

    rows: list[dict[str, Any]] = []
    for (date, subturn), records in sorted(grouped.items()):
        first = records[0]
        actions = Counter(str((record.get("decision") or {}).get("action") or "") for record in records)
        sentiments = Counter(str((record.get("news_interpretation") or {}).get("news_sentiment") or "missing") for record in records)
        confidences = Counter(str((record.get("news_interpretation") or {}).get("confidence") or "missing") for record in records)
        quantities = [as_int((record.get("decision") or {}).get("quantity")) or 0 for record in records]
        signed_quantity = sum(
            quantity if str((record.get("decision") or {}).get("action") or "") == "buy" else -quantity
            for record, quantity in zip(records, quantities)
        )
        initial_portfolios = sum(
            INITIAL_PORTFOLIO_PHRASE in str((record.get("context") or {}).get("portfolio_summary") or "")
            for record in records
        )
        rows.append(
            {
                "date": date,
                "turn": as_int(first.get("turn")),
                "subturn": subturn,
                "agent_rows": len(records),
                "buy_orders": actions["buy"],
                "sell_orders": actions["sell"],
                "net_submitted_quantity": signed_quantity,
                "one_share_orders": sum(quantity == 1 for quantity in quantities),
                "initial_portfolio_agents": initial_portfolios,
                "blank_belief_rows": sum(not str((record.get("belief") or {}).get("belief_summary") or "").strip() for record in records),
                "positive_news_sentiment": sentiments["positive"],
                "negative_news_sentiment": sentiments["negative"],
                "mixed_news_sentiment": sentiments["mixed"],
                "neutral_news_sentiment": sentiments["neutral"],
                "high_news_confidence": confidences["high"],
                "medium_news_confidence": confidences["medium"],
                "low_news_confidence": confidences["low"],
            }
        )
    return rows


def c00_chunk_boundary_rows(spec: dict[str, str]) -> list[dict[str, Any]]:
    run_dir = PROJECT_ROOT / "outputs" / "logs" / spec["run_id"]
    rows: list[dict[str, Any]] = []
    for chunk_dir in sorted(path for path in (run_dir / "chunks").glob("chunk_*") if path.is_dir()):
        records = load_jsonl(chunk_dir / "agent_turns.jsonl")
        first_am = [
            record
            for record in records
            if as_int(record.get("turn")) == 1 and str((record.get("context") or {}).get("subturn") or "") == "am"
        ]
        rows.append(
            {
                "chunk": chunk_dir.name,
                "agent_turn_rows": len(records),
                "date_min": min(str(record.get("date") or "") for record in records),
                "date_max": max(str(record.get("date") or "") for record in records),
                "turn_min": min(as_int(record.get("turn")) or 0 for record in records),
                "turn_max": max(as_int(record.get("turn")) or 0 for record in records),
                "first_am_rows": len(first_am),
                "first_am_initial_portfolio_rows": sum(
                    INITIAL_PORTFOLIO_PHRASE in str((record.get("context") or {}).get("portfolio_summary") or "")
                    for record in first_am
                ),
                "first_am_buy_orders": sum((record.get("decision") or {}).get("action") == "buy" for record in first_am),
            }
        )
    return rows


def c00_persona_behavior_rows(spec: dict[str, str], cohort: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Descriptive C00 decision and belief coverage by configured persona fields.

    These rows deliberately do not estimate a persona effect: the active cohort
    is small, imbalanced, and has a single initial-cash level.  They make the
    missingness/action mechanics visible before any future statistical model is
    attempted.
    """
    run_dir = PROJECT_ROOT / "outputs" / "logs" / spec["run_id"]
    cohort_by_agent = {row["agent_id"]: row for row in cohort}
    fields = ("gender", "age_group", "strategy", "news_depth", "user_type", "initial_cash_krw")
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)

    for record in load_jsonl(run_dir / "agent_turns.jsonl"):
        context = record.get("context") or {}
        agent = record.get("agent") or {}
        agent_id = str(agent.get("agent_id") or context.get("agent_id") or "")
        profile = cohort_by_agent.get(agent_id)
        if not profile:
            continue
        for field in fields:
            grouped[(field, str(profile[field]))].append(record)

    rows: list[dict[str, Any]] = []
    for (field, value), records in sorted(grouped.items()):
        actions = Counter(str((record.get("decision") or {}).get("action") or "") for record in records)
        quantities = [as_int((record.get("decision") or {}).get("quantity")) or 0 for record in records]
        agent_ids = {
            str((record.get("agent") or {}).get("agent_id") or (record.get("context") or {}).get("agent_id") or "")
            for record in records
        }
        rows.append(
            {
                "condition": spec["condition"],
                "persona_field": field,
                "persona_value": value,
                "agent_count": len(agent_ids),
                "decision_rows": len(records),
                "buy_orders": actions["buy"],
                "sell_orders": actions["sell"],
                "buy_share": round(actions["buy"] / len(records), 4) if records else None,
                "sell_share": round(actions["sell"] / len(records), 4) if records else None,
                "one_share_rate": round(sum(quantity == 1 for quantity in quantities) / len(records), 4) if records else None,
                "blank_belief_rows": sum(
                    not str((record.get("belief") or {}).get("belief_summary") or "").strip()
                    for record in records
                ),
                "blank_belief_rate": round(
                    sum(not str((record.get("belief") or {}).get("belief_summary") or "").strip() for record in records)
                    / len(records),
                    4,
                ) if records else None,
            }
        )
    return rows


def c00_validation_by_chunk_rows(spec: dict[str, str]) -> list[dict[str, Any]]:
    """Expose the volatility hidden by a single aggregate direction-match rate."""
    run_dir = PROJECT_ROOT / "outputs" / "logs" / spec["run_id"]
    validation_path = (
        PROJECT_ROOT / "validation" / "outputs" / spec["run_id"] / "daily_comparison_value.csv"
    )
    chunk_ranges = []
    for chunk_dir in sorted(path for path in (run_dir / "chunks").glob("chunk_*") if path.is_dir()):
        records = load_jsonl(chunk_dir / "agent_turns.jsonl")
        chunk_ranges.append(
            (
                chunk_dir.name,
                min(str(record.get("date") or "") for record in records),
                max(str(record.get("date") or "") for record in records),
            )
        )
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    with validation_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            date = str(row.get("date") or "")
            matching_chunk = next(
                (chunk for chunk, start, end in chunk_ranges if start <= date <= end),
                "unmapped",
            )
            grouped[matching_chunk].append(row)

    rows: list[dict[str, Any]] = []
    for chunk, records in sorted(grouped.items()):
        predicted = [str(record.get("llm_direction") or "") for record in records]
        actual = [str(record.get("Individuals_direction") or "") for record in records]
        buy_recall = (
            sum(pred == "net_buy" for pred, truth in zip(predicted, actual) if truth == "net_buy")
            / sum(truth == "net_buy" for truth in actual)
            if any(truth == "net_buy" for truth in actual)
            else None
        )
        sell_recall = (
            sum(pred == "net_sell" for pred, truth in zip(predicted, actual) if truth == "net_sell")
            / sum(truth == "net_sell" for truth in actual)
            if any(truth == "net_sell" for truth in actual)
            else None
        )
        recalls = [value for value in (buy_recall, sell_recall) if value is not None]
        rows.append(
            {
                "condition": spec["condition"],
                "chunk": chunk,
                "validation_days": len(records),
                "date_min": min(str(record.get("date") or "") for record in records),
                "date_max": max(str(record.get("date") or "") for record in records),
                "direction_match_rate": round(
                    sum(pred == truth for pred, truth in zip(predicted, actual)) / len(records), 4
                ) if records else None,
                "predicted_buy_days": sum(pred == "net_buy" for pred in predicted),
                "predicted_sell_days": sum(pred == "net_sell" for pred in predicted),
                "actual_buy_days": sum(truth == "net_buy" for truth in actual),
                "actual_sell_days": sum(truth == "net_sell" for truth in actual),
                "buy_recall": round(buy_recall, 4) if buy_recall is not None else None,
                "sell_recall": round(sell_recall, 4) if sell_recall is not None else None,
                "balanced_accuracy": round(sum(recalls) / len(recalls), 4) if recalls else None,
            }
        )
    return rows


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    audit_rows: list[dict[str, Any]] = []
    benchmark_rows: list[dict[str, Any]] = []
    fake_event_rows: list[dict[str, Any]] = []
    belief_rows: list[dict[str, Any]] = []
    active_agents: set[str] = set()

    for spec in OFFICIAL_RUNS:
        audit_row, baselines, events, beliefs = audit_run(spec)
        audit_rows.append(audit_row)
        benchmark_rows.extend(baselines)
        fake_event_rows.extend(events)
        belief_rows.extend(beliefs)
        active_agents.update(row["agent_id"] for row in beliefs)

    cohort, cohort_comparison = persona_rows(active_agents)
    c00_spec = OFFICIAL_RUNS[0]
    write_csv("run_integrity_audit.csv", audit_rows)
    write_csv("validation_benchmarks.csv", benchmark_rows)
    write_csv("fake_event_delivery.csv", fake_event_rows)
    write_csv("belief_missingness_by_agent.csv", belief_rows)
    write_csv("active_cohort.csv", cohort)
    write_csv("persona_population_vs_active_cohort.csv", cohort_comparison)
    write_csv("c00_log_inventory.csv", c00_log_inventory(c00_spec))
    write_csv("c00_daily_decision_summary.csv", c00_daily_decision_rows(c00_spec))
    write_csv("c00_chunk_boundary_summary.csv", c00_chunk_boundary_rows(c00_spec))
    write_csv("c00_persona_behavior_descriptives.csv", c00_persona_behavior_rows(c00_spec, cohort))
    write_csv("c00_validation_by_chunk.csv", c00_validation_by_chunk_rows(c00_spec))
    (OUTPUT_DIR / "audit_summary.json").write_text(
        json.dumps(
            {
                "source_commit_expected": "8604f9aec041c9929e327a90cc9025b650e9fab6",
                "run_audit": audit_rows,
                "notes": [
                    "All six committed 2026-07-15 conditions are audited as observed outputs.",
                    "Chunk-state evidence is read separately from each condition root agent_turns.jsonl.",
                    "Fake selected/read fields are treatment mechanisms, not causal controls.",
                    "Validation metrics are reproduced from each committed summary_metrics.json file.",
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Wrote audit artifacts to {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
