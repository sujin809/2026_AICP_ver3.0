#!/usr/bin/env python3
"""Reproducible audit of the post-0720 C00 result pushed on 2026-07-21.

The source checkout is intentionally a parameter because the active workspace
may be on another branch.  The script reads the declared root log bundle only;
partial files outside run_complete.json's post-hoc cutoff are not merged.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
from scipy import stats


HERE = Path(__file__).resolve().parent
OUTPUT_DIR = HERE / "outputs"
RUN_REL = Path("outputs/logs/paper_0721/c00_commoff_fakeoff")
VALIDATION_REL = Path("validation/outputs/c00_commoff_fakeoff_2026-05-04")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-root",
        type=Path,
        default=Path("/private/tmp/aicp_off_result_20260721"),
        help="Checkout of commit 5605732 / branch off_result_20260721",
    )
    parser.add_argument("--bootstrap-reps", type=int, default=20_000)
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def write_csv(name: str, rows: Iterable[dict[str, Any]]) -> None:
    rows = list(rows)
    path = OUTPUT_DIR / name
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(rows[0])
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def wilson_interval(successes: int, total: int, z: float = 1.959963984540054) -> tuple[float, float]:
    if total == 0:
        return (math.nan, math.nan)
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return center - half, center + half


def direction_metrics(frame: pd.DataFrame, pred_col: str = "llm_net") -> dict[str, Any]:
    pred = np.sign(frame[pred_col].to_numpy(dtype=float))
    actual = np.sign(frame["Individuals"].to_numpy(dtype=float))
    buy_mask = actual > 0
    sell_mask = actual < 0
    buy_recall = float((pred[buy_mask] > 0).mean()) if buy_mask.any() else math.nan
    sell_recall = float((pred[sell_mask] < 0).mean()) if sell_mask.any() else math.nan
    pearson = stats.pearsonr(frame[pred_col], frame["Individuals"]) if len(frame) >= 3 else None
    spearman = stats.spearmanr(frame[pred_col], frame["Individuals"]) if len(frame) >= 3 else None
    return {
        "days": len(frame),
        "direction_match_rate": float((pred == actual).mean()),
        "buy_recall": buy_recall,
        "sell_recall": sell_recall,
        "balanced_accuracy": (buy_recall + sell_recall) / 2,
        "predicted_buy_days": int((pred > 0).sum()),
        "predicted_sell_days": int((pred < 0).sum()),
        "actual_buy_days": int(buy_mask.sum()),
        "actual_sell_days": int(sell_mask.sum()),
        "pearson": float(pearson.statistic) if pearson else math.nan,
        "pearson_p_naive": float(pearson.pvalue) if pearson else math.nan,
        "spearman": float(spearman.statistic) if spearman else math.nan,
        "spearman_p_naive": float(spearman.pvalue) if spearman else math.nan,
    }


def phase_daily(fills: pd.DataFrame, actual: pd.DataFrame, subturn: str | None) -> pd.DataFrame:
    scoped = fills if subturn is None else fills.loc[fills["subturn"].eq(subturn)]
    daily = scoped.groupby("date", as_index=False).agg(
        llm_net=("signed_value", "sum"),
        llm_net_volume=("signed_quantity", "sum"),
    )
    return daily.merge(actual[["date", "Individuals", "market_return"]], on="date", how="inner")


def stratified_ba_bootstrap(frame: pd.DataFrame, reps: int) -> tuple[float, float, float]:
    pred = np.sign(frame["llm_net"].to_numpy(dtype=float))
    actual = np.sign(frame["Individuals"].to_numpy(dtype=float))
    buy = np.flatnonzero(actual > 0)
    sell = np.flatnonzero(actual < 0)
    rng = np.random.default_rng(20260722)
    values = np.empty(reps)
    for index in range(reps):
        b = rng.choice(buy, len(buy), replace=True)
        s = rng.choice(sell, len(sell), replace=True)
        values[index] = ((pred[b] > 0).mean() + (pred[s] < 0).mean()) / 2
    low, median, high = np.quantile(values, [0.025, 0.5, 0.975])
    return float(low), float(median), float(high)


def persona_rows(
    fills: pd.DataFrame,
    turns: pd.DataFrame,
    actual: pd.DataFrame,
    profiles: pd.DataFrame,
) -> list[dict[str, Any]]:
    merged_fills = fills.merge(profiles, on="agent_id", how="left")
    merged_turns = turns.merge(profiles, on="agent_id", how="left", suffixes=("", "_profile"))
    rows: list[dict[str, Any]] = []
    for field in ("strategy", "news_depth", "age_group", "gender", "user_type"):
        for value, group in merged_fills.groupby(field, dropna=False):
            daily = group.groupby("date", as_index=False)["signed_value"].sum().rename(
                columns={"signed_value": "llm_net"}
            )
            metrics = direction_metrics(daily.merge(actual[["date", "Individuals"]], on="date"))
            behavior = merged_turns.loc[merged_turns[field].eq(value)]
            rows.append(
                {
                    "persona_axis": field,
                    "persona_value": str(value),
                    "agent_count": int(group["agent_id"].nunique()),
                    "turn_rows": len(behavior),
                    "buy_share": float(behavior["action"].eq("buy").mean()),
                    "one_share_rate": float(behavior["quantity"].eq(1).mean()),
                    "decision_retry_rate": float(behavior["decision_retry_count"].gt(0).mean()),
                    **metrics,
                }
            )
    return rows


def persona_construct_rows(
    turns: pd.DataFrame,
    fills: pd.DataFrame,
    profiles: pd.DataFrame,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Measure prompt-trait enactment without treating persona cells as human effects.

    The disposition metric is computed per agent on turns where buy and sell are
    both feasible and the pre-trade holding is at an unrealized gain or loss.
    Average cost is reconstructed from the exact full-fill sequence.  The risk
    metric is the selected quantity divided by the feasible maximum for the
    selected direction, averaged per agent on two-way-feasible turns.
    """

    merged = turns.merge(
        fills[["agent_id", "date", "subturn", "executed_price"]],
        on=["agent_id", "date", "subturn"],
        how="left",
        validate="one_to_one",
    ).sort_values(["agent_id", "turn"])
    trace: list[dict[str, Any]] = []
    for agent_id, group in merged.groupby("agent_id", sort=True):
        quantity = 0
        average_cost = 0.0
        for row in group.itertuples(index=False):
            if int(row.current_quantity) != quantity:
                raise ValueError(
                    f"pre-trade quantity mismatch for {agent_id} turn {row.turn}: "
                    f"logged={row.current_quantity}, reconstructed={quantity}"
                )
            price = float(row.executed_price)
            state = "flat"
            if quantity > 0 and price > average_cost:
                state = "gain"
            elif quantity > 0 and price < average_cost:
                state = "loss"
            both_feasible = row.max_buy_quantity > 0 and row.max_sell_quantity > 0
            intensity = (
                float(row.quantity) / float(row.selected_action_max_quantity)
                if row.selected_action_max_quantity > 0
                else math.nan
            )
            trace.append(
                {
                    "agent_id": agent_id,
                    "turn": int(row.turn),
                    "both_feasible": both_feasible,
                    "pre_trade_state": state,
                    "action": row.action,
                    "order_intensity": intensity,
                }
            )
            if row.action == "buy":
                bought = int(row.quantity)
                average_cost = (average_cost * quantity + price * bought) / (quantity + bought)
                quantity += bought
            else:
                quantity -= int(row.quantity)
                if quantity == 0:
                    average_cost = 0.0

    trace_frame = pd.DataFrame(trace).merge(
        profiles[
            [
                "agent_id",
                "bh_disposition_effect_category",
                "bh_lottery_preference_category",
            ]
        ],
        on="agent_id",
        how="left",
        validate="many_to_one",
    )
    agent_rows: list[dict[str, Any]] = []
    for agent_id, group in trace_frame.groupby("agent_id", sort=True):
        two_way = group.loc[group["both_feasible"]]
        gain = two_way.loc[two_way["pre_trade_state"].eq("gain")]
        loss = two_way.loc[two_way["pre_trade_state"].eq("loss")]
        agent_rows.append(
            {
                "agent_id": agent_id,
                "disposition_category": group["bh_disposition_effect_category"].iloc[0],
                "risk_category": group["bh_lottery_preference_category"].iloc[0],
                "gain_turns": len(gain),
                "loss_turns": len(loss),
                "sell_rate_on_gain": float(gain["action"].eq("sell").mean()),
                "sell_rate_on_loss": float(loss["action"].eq("sell").mean()),
                "disposition_gap": float(
                    gain["action"].eq("sell").mean() - loss["action"].eq("sell").mean()
                ),
                "mean_order_intensity": float(two_way["order_intensity"].mean()),
            }
        )
    agent_frame = pd.DataFrame(agent_rows)

    group_rows: list[dict[str, Any]] = []
    for category, group in agent_frame.groupby("disposition_category", sort=False):
        group_rows.append(
            {
                "construct": "disposition_effect",
                "category": category,
                "agent_count": len(group),
                "metric": "mean_P_sell_gain_minus_P_sell_loss",
                "value": float(group["disposition_gap"].mean()),
            }
        )
    for category, group in agent_frame.groupby("risk_category", sort=False):
        group_rows.append(
            {
                "construct": "lottery_risk_preference",
                "category": category,
                "agent_count": len(group),
                "metric": "mean_quantity_over_feasible_max",
                "value": float(group["mean_order_intensity"].mean()),
            }
        )

    ordinal = {"low": 0, "medium": 1, "high": 2}
    disposition_test = stats.spearmanr(
        agent_frame["disposition_category"].map(ordinal), agent_frame["disposition_gap"]
    )
    risk_test = stats.spearmanr(
        agent_frame["risk_category"].map(ordinal), agent_frame["mean_order_intensity"]
    )
    tests = {
        "disposition_ordinal_spearman": {
            "rho": float(disposition_test.statistic),
            "p_exploratory": float(disposition_test.pvalue),
        },
        "risk_ordinal_spearman": {
            "rho": float(risk_test.statistic),
            "p_exploratory": float(risk_test.pvalue),
        },
        "interpretation": "Prompt-enactment construct checks; not human demographic effects or confirmatory tests.",
    }
    return agent_rows, group_rows, tests


def main() -> None:
    args = parse_args()
    root = args.source_root.resolve()
    run_dir = root / RUN_REL
    validation_dir = root / VALIDATION_REL
    for required in (
        run_dir / "run_complete.json",
        run_dir / "integrity_report.json",
        run_dir / "run_metadata.json",
        run_dir / "agent_turns.csv",
        run_dir / "agent_turns.jsonl",
        run_dir / "exchange_fills.csv",
        validation_dir / "summary_metrics.json",
        validation_dir / "daily_comparison_value.csv",
    ):
        if not required.exists():
            raise FileNotFoundError(required)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    run_complete = read_json(run_dir / "run_complete.json")
    integrity = read_json(run_dir / "integrity_report.json")
    metadata = read_json(run_dir / "run_metadata.json")
    paused = read_json(run_dir / "paused.json") if (run_dir / "paused.json").exists() else None
    validation = read_json(validation_dir / "summary_metrics.json")
    turns = pd.read_csv(run_dir / "agent_turns.csv", encoding="utf-8-sig")
    turn_json = read_jsonl(run_dir / "agent_turns.jsonl")
    fills = pd.read_csv(run_dir / "exchange_fills.csv", encoding="utf-8-sig")
    actual = pd.read_csv(validation_dir / "daily_comparison_value.csv", encoding="utf-8-sig")

    fills["signed_quantity"] = np.where(fills["action"].eq("buy"), fills["quantity"], -fills["quantity"])
    fills["signed_value"] = fills["signed_quantity"] * fills["executed_price"]
    overall_daily = phase_daily(fills, actual, None)

    # Core integrity checks at the intended agent x date x subturn grain.
    key_columns = ["agent_id", "date", "subturn"]
    duplicate_turn_keys = int(turns.duplicated(key_columns).sum())
    expected_keys = turns["agent_id"].nunique() * turns["date"].nunique() * turns["subturn"].nunique()
    observed_keys = int(turns[key_columns].drop_duplicates().shape[0])
    continuous_turns = sorted(int(value) for value in turns["turn"].unique())

    raw_items = mapped_items = unmapped_items = mapped_rows = 0
    exact_reference_matches = raw_id_strings = 0
    active_system_message_rows = 0
    for record in turn_json:
        system_message = ((record.get("context") or {}).get("system_message"))
        active_system_message_rows += int(bool(str(system_message).strip())) if system_message is not None else 0
        interpretation = record.get("news_interpretation") or {}
        raw = interpretation.get("selected_news_raw") or []
        mapped = interpretation.get("selected_news") or []
        unmapped = interpretation.get("unmapped_selected_news") or []
        raw_items += len(raw)
        mapped_items += len(mapped)
        unmapped_items += len(unmapped)
        mapped_rows += int(bool(mapped))
        news_context = ((record.get("context") or {}).get("news_context") or {})
        reference_universe: set[str] = set()
        for key in ("visible_news_ids", "read_news_ids", "search_result_ids", "influential_news_ids"):
            reference_universe.update(str(item) for item in (news_context.get(key) or []))

        def collect_reference_fields(value: Any) -> None:
            if isinstance(value, dict):
                for key in ("id", "title"):
                    if value.get(key):
                        reference_universe.add(str(value[key]))
                for nested in value.values():
                    collect_reference_fields(nested)
            elif isinstance(value, list):
                for nested in value:
                    collect_reference_fields(nested)

        for key in ("daily_titles", "read_contents", "search_results", "search_read_contents"):
            collect_reference_fields(news_context.get(key) or {})
        for item in raw:
            if isinstance(item, str):
                raw_id_strings += int(item.startswith("news_"))
                exact_reference_matches += int(item in reference_universe)

    max_buy = turns["max_buy_quantity"].fillna(0)
    max_sell = turns["max_sell_quantity"].fillna(0)
    feasible = np.select(
        [(max_buy > 0) & (max_sell > 0), (max_buy > 0) & (max_sell <= 0), (max_buy <= 0) & (max_sell > 0)],
        ["both", "buy_only", "sell_only"],
        default="neither",
    )
    mechanics_rows = []
    for label in ("both", "buy_only", "sell_only", "neither"):
        mask = feasible == label
        scoped = turns.loc[mask]
        mechanics_rows.append(
            {
                "feasible_action_set": label,
                "turn_rows": len(scoped),
                "buy_rows": int(scoped["action"].eq("buy").sum()),
                "sell_rows": int(scoped["action"].eq("sell").sum()),
                "mean_quantity": float(scoped["quantity"].mean()) if len(scoped) else math.nan,
                "max_quantity_selected_rate": float(
                    scoped["quantity"].eq(scoped["selected_action_max_quantity"]).mean()
                ) if len(scoped) else math.nan,
            }
        )

    sensitivity_rows: list[dict[str, Any]] = []
    for phase, subturn in (("AM only", "am"), ("PM only", "pm"), ("AM+PM", None)):
        daily = phase_daily(fills, actual, subturn)
        for skip in (0, 1, 3, 5, 10):
            metrics = direction_metrics(daily.iloc[skip:])
            sensitivity_rows.append({"phase": phase, "skip_initial_days": skip, **metrics})

    # Decision-time price-only baselines.  These are essential because Korean
    # individual flow in this window is strongly contrarian to contemporaneous
    # price moves, and those price signals are already present in the prompts.
    market_context_rows: list[dict[str, Any]] = []
    seen_market_contexts: set[tuple[str, str]] = set()
    for record in turn_json:
        context = record.get("context") or {}
        key = (str(record.get("date")), str(context.get("subturn")))
        if key in seen_market_contexts:
            continue
        seen_market_contexts.add(key)
        features = context.get("market_features") or {}
        market_context_rows.append(
            {
                "date": key[0],
                "subturn": key[1],
                "opening_gap": features.get("intraday_return_from_prev_close"),
                "current_day_return": features.get("pct_chg"),
            }
        )
    market_contexts = pd.DataFrame(market_context_rows)
    decision_time_baselines: list[dict[str, Any]] = []
    baseline_specs = (
        ("AM opening-gap contrarian", "am", "opening_gap"),
        ("PM current-return contrarian", "pm", "current_day_return"),
    )
    for label, subturn, feature in baseline_specs:
        baseline_frame = market_contexts.loc[
            market_contexts["subturn"].eq(subturn), ["date", feature]
        ].merge(actual[["date", "Individuals"]], on="date", how="inner")
        baseline_frame["baseline_signal"] = -baseline_frame[feature].astype(float)
        model_frame = phase_daily(fills, actual, subturn)[["date", "llm_net", "Individuals"]]
        for skip in (0, 5):
            scoped = baseline_frame.iloc[skip:].copy()
            baseline_metrics = direction_metrics(scoped, "baseline_signal")
            paired = scoped.merge(model_frame[["date", "llm_net"]], on="date", how="inner")
            actual_sign = np.sign(paired["Individuals"].to_numpy(dtype=float))
            baseline_correct = np.sign(paired["baseline_signal"].to_numpy(dtype=float)) == actual_sign
            model_correct = np.sign(paired["llm_net"].to_numpy(dtype=float)) == actual_sign
            model_only = int((model_correct & ~baseline_correct).sum())
            baseline_only = int((baseline_correct & ~model_correct).sum())
            discordant = model_only + baseline_only
            mcnemar_p = (
                float(
                    stats.binomtest(
                        min(model_only, baseline_only),
                        discordant,
                        0.5,
                        alternative="two-sided",
                    ).pvalue
                )
                if discordant
                else 1.0
            )
            decision_time_baselines.append(
                {
                    "baseline": label,
                    "subturn": subturn,
                    "feature": feature,
                    "skip_initial_days": skip,
                    **baseline_metrics,
                    "model_only_correct_days": model_only,
                    "baseline_only_correct_days": baseline_only,
                    "paired_mcnemar_exact_two_sided_p": mcnemar_p,
                }
            )

    primary = direction_metrics(overall_daily)
    match_successes = int(round(primary["direction_match_rate"] * primary["days"]))
    buy_successes = int(round(primary["buy_recall"] * primary["actual_buy_days"]))
    sell_successes = int(round(primary["sell_recall"] * primary["actual_sell_days"]))
    match_ci = wilson_interval(match_successes, primary["days"])
    buy_ci = wilson_interval(buy_successes, primary["actual_buy_days"])
    sell_ci = wilson_interval(sell_successes, primary["actual_sell_days"])
    ba_ci = stratified_ba_bootstrap(overall_daily, args.bootstrap_reps)
    predicted_sign = np.sign(overall_daily["llm_net"].to_numpy(dtype=float))
    actual_sign = np.sign(overall_daily["Individuals"].to_numpy(dtype=float))
    confusion = np.array(
        [
            [int(((actual_sign > 0) & (predicted_sign > 0)).sum()), int(((actual_sign > 0) & (predicted_sign < 0)).sum())],
            [int(((actual_sign < 0) & (predicted_sign > 0)).sum()), int(((actual_sign < 0) & (predicted_sign < 0)).sum())],
        ]
    )
    true_buy = int(confusion[0, 0])
    model_only_correct_vs_always_buy = int(confusion[1, 1])
    always_buy_only_correct = int(confusion[0, 1])
    statistical_tests = {
        "one_sided_binomial_match_vs_half_p": float(
            stats.binomtest(match_successes, primary["days"], 0.5, alternative="greater").pvalue
        ),
        "fixed_margins_hypergeometric_p": float(
            stats.hypergeom.sf(
                true_buy - 1,
                primary["days"],
                primary["actual_buy_days"],
                primary["predicted_buy_days"],
            )
        ),
        "fisher_exact_two_sided_p": float(stats.fisher_exact(confusion).pvalue),
        "mcnemar_vs_always_buy_exact_two_sided_p": float(
            stats.binomtest(
                min(model_only_correct_vs_always_buy, always_buy_only_correct),
                model_only_correct_vs_always_buy + always_buy_only_correct,
                0.5,
                alternative="two-sided",
            ).pvalue
        ),
        "confusion_actual_by_predicted_buy_sell": confusion.tolist(),
        "note": "Exploratory day-level diagnostics; serial dependence and single-seed selection remain.",
    }

    # Provider/API audit.
    api_rows = read_jsonl(run_dir / "openrouter_calls.jsonl")
    api_summary_rows = []
    for label in sorted({str(row.get("label")) for row in api_rows}):
        scoped = [row for row in api_rows if str(row.get("label")) == label]
        successes = [row for row in scoped if row.get("status") == "success"]
        errors = [row for row in scoped if row.get("status") == "error"]
        latencies = [float(row.get("latency_seconds") or 0) for row in successes]
        api_summary_rows.append(
            {
                "label": label,
                "requests": len(scoped),
                "successes": len(successes),
                "errors": len(errors),
                "error_rate": len(errors) / len(scoped),
                "cost_usd": sum(float((row.get("usage") or {}).get("cost") or 0) for row in successes),
                "total_tokens": sum(int((row.get("usage") or {}).get("total_tokens") or 0) for row in successes),
                "mean_latency_seconds": float(np.mean(latencies)) if latencies else math.nan,
                "p95_latency_seconds": float(np.quantile(latencies, 0.95)) if latencies else math.nan,
            }
        )

    # Cohort and modeled-persona descriptives.
    with sqlite3.connect(root / "outputs/sys_100.db") as connection:
        profiles = pd.read_sql_query(
            "SELECT agent_id, gender, age_group, ini_cash, strategy, news_depth, user_type, "
            "bh_disposition_effect_category, bh_lottery_preference_category, "
            "bh_total_return_category, bh_underdiversification_category "
            "FROM agents WHERE agent_id BETWEEN 'A001' AND 'A030' ORDER BY agent_id",
            connection,
        )
    persona = persona_rows(fills, turns, actual, profiles)
    persona_agent_rows, persona_construct_groups, persona_construct_tests = persona_construct_rows(
        turns, fills, profiles
    )

    # Final portfolios and turnover.
    portfolio_rows = read_jsonl(run_dir / "portfolio_updates.jsonl")
    final_state: dict[str, dict[str, Any]] = {}
    for row in portfolio_rows:
        final_state[str(row["agent_id"])] = row["state"]
    final_returns = np.array([float(state["total_return_rate"]) for state in final_state.values()])
    exchange = pd.read_csv(run_dir / "daily_exchange_summary.csv", encoding="utf-8-sig")
    buy_hold_return = float(exchange.iloc[-1]["close_price"] / exchange.iloc[0]["announced_price"] - 1)
    notional_by_agent = (
        fills.assign(notional=fills["quantity"] * fills["executed_price"])
        .groupby("agent_id")["notional"]
        .sum()
    )

    baseline = validation["value"]["baselines_vs_individuals"]
    headline_rows = [
        {"metric": "declared_scope_days", "value": run_complete["date_count"], "unit": "days"},
        {"metric": "direction_match_rate", "value": primary["direction_match_rate"], "unit": "rate"},
        {"metric": "balanced_accuracy", "value": primary["balanced_accuracy"], "unit": "rate"},
        {"metric": "daily_pearson", "value": primary["pearson"], "unit": "correlation"},
        {"metric": "daily_spearman", "value": primary["spearman"], "unit": "correlation"},
        {"metric": "always_buy_direction_match", "value": baseline["always_buy"]["direction_match_rate"], "unit": "rate"},
        {"metric": "prior_market_balanced_accuracy", "value": baseline["previous_day_market_return_direction"]["balanced_accuracy"], "unit": "rate"},
        {"metric": "first_five_day_match", "value": float(overall_daily.iloc[:5].pipe(direction_metrics)["direction_match_rate"]), "unit": "rate"},
        {"metric": "post_first_five_day_match", "value": float(overall_daily.iloc[5:].pipe(direction_metrics)["direction_match_rate"]), "unit": "rate"},
        {"metric": "post_first_five_day_pearson", "value": float(overall_daily.iloc[5:].pipe(direction_metrics)["pearson"]), "unit": "correlation"},
        {"metric": "mean_agent_return", "value": float(final_returns.mean()), "unit": "rate"},
        {"metric": "buy_hold_return", "value": buy_hold_return, "unit": "rate"},
        {"metric": "mean_turnover_multiple", "value": float((notional_by_agent / 100_000_000).mean()), "unit": "x initial capital"},
        {"metric": "one_share_rate", "value": float(turns["quantity"].eq(1).mean()), "unit": "rate"},
        {"metric": "belief_retry_row_rate", "value": float(turns["belief_generation_attempts"].gt(1).mean()), "unit": "rate"},
        {"metric": "decision_retry_row_rate", "value": float(turns["decision_retry_count"].gt(0).mean()), "unit": "rate"},
        {"metric": "influential_news_unmapped_item_rate", "value": unmapped_items / raw_items, "unit": "rate"},
    ]

    daily_output = overall_daily.copy()
    daily_output["match"] = (np.sign(daily_output["llm_net"]) == np.sign(daily_output["Individuals"])).astype(int)
    daily_output["llm_z"] = stats.zscore(daily_output["llm_net"])
    daily_output["individuals_z"] = stats.zscore(daily_output["Individuals"])
    daily_output.to_csv(OUTPUT_DIR / "daily_flow_comparison.csv", index=False)
    write_csv("headline_metrics.csv", headline_rows)
    write_csv("sensitivity_metrics.csv", sensitivity_rows)
    write_csv("decision_time_price_baselines.csv", decision_time_baselines)
    write_csv("decision_feasibility.csv", mechanics_rows)
    write_csv("persona_group_metrics.csv", persona)
    write_csv("persona_construct_agent_scores.csv", persona_agent_rows)
    write_csv("persona_construct_group_metrics.csv", persona_construct_groups)
    write_csv("llm_api_summary.csv", api_summary_rows)

    summary = {
        "source": {
            "branch": "off_result_20260721",
            "commit": "56057320474a59c3c60e0554d7a066110193b331",
            "git_commit_at_run_start": metadata["git_commit_at_start"],
            "run_id": metadata["run_id"],
            "scope": {key: run_complete[key] for key in ("start_date", "end_date", "configured_end_date", "date_count", "agent_count")},
        },
        "integrity_reconciliation": {
            "declared_status": integrity["status"],
            "scope_status": integrity["scope_status"],
            "expected_agent_turns": expected_keys,
            "observed_agent_turns": len(turns),
            "unique_agent_date_subturn_keys": observed_keys,
            "duplicate_agent_date_subturn_keys": duplicate_turn_keys,
            "turn_min": min(continuous_turns),
            "turn_max": max(continuous_turns),
            "turn_sequence_complete": continuous_turns == list(range(1, 91)),
            "first_turn_all_cash_100m": bool((turns.loc[turns["turn"].eq(1), "available_cash"] == 100_000_000).all()),
            "first_turn_all_no_holdings": bool((turns.loc[turns["turn"].eq(1), "current_quantity"] == 0).all()),
            "partial_chunk_after_declared_scope_exists": (run_dir / "chunks/chunk_046_2026-05-06_2026-05-06").exists(),
            "paused_file_failed_phase": paused.get("failed_phase") if paused else None,
            "paused_file_is_historical_recovered_trace": bool(
                paused and str(paused.get("failed_phase", "")).startswith("2026-04-29")
                and run_complete["end_date"] > "2026-04-29"
            ),
        },
        "primary_metrics": primary,
        "uncertainty": {
            "direction_match_wilson_95": match_ci,
            "buy_recall_wilson_95": buy_ci,
            "sell_recall_wilson_95": sell_ci,
            "balanced_accuracy_stratified_bootstrap_95": (ba_ci[0], ba_ci[2]),
            "note": "Day-level observations are serially ordered; these intervals are diagnostic, not a substitute for multi-seed/block-bootstrap inference.",
        },
        "statistical_tests": statistical_tests,
        "decision_time_price_baselines": {
            f"{row['subturn']}_skip_{row['skip_initial_days']}": row
            for row in decision_time_baselines
        },
        "decision_mechanics": {
            "buy_rows": int(turns["action"].eq("buy").sum()),
            "sell_rows": int(turns["action"].eq("sell").sum()),
            "one_share_rows": int(turns["quantity"].eq(1).sum()),
            "deterministic_fallback_rows": int(turns["deterministic_fallback_used"].fillna(False).astype(bool).sum()),
            "all_orders_filled": len(fills) == len(turns),
            "mean_agent_turnover_multiple": float((notional_by_agent / 100_000_000).mean()),
        },
        "belief_and_news_trace": {
            "blank_belief_summary_rows": int(turns["belief_summary"].fillna("").str.strip().eq("").sum()),
            "blank_view_change_rows": int(turns["view_change"].fillna("").str.strip().eq("").sum()),
            "exact_duplicate_agent_belief_summaries": int(turns.duplicated(["agent_id", "belief_summary"]).sum()),
            "raw_influential_news_items": raw_items,
            "mapped_influential_news_items": mapped_items,
            "unmapped_influential_news_items": unmapped_items,
            "rows_with_any_mapped_influential_news": mapped_rows,
            "raw_id_string_items": raw_id_strings,
            "exact_id_or_title_matches_to_turn_news": exact_reference_matches,
            "exact_id_or_title_match_rate": exact_reference_matches / raw_items,
            "active_system_message_rows": active_system_message_rows,
        },
        "llm_runtime": {
            "api_requests": len(api_rows),
            "api_errors": sum(row.get("status") == "error" for row in api_rows),
            "error_types": dict(Counter(str(row.get("error_type")) for row in api_rows if row.get("status") == "error")),
            "validation_retry_events": metadata["validation_retry_events"],
            "api_cost_usd": metadata["api_cost_usd"],
        },
        "cohort": {
            "initial_cash_counts": {str(key): int(value) for key, value in profiles["ini_cash"].value_counts().to_dict().items()},
            "strategy_counts": {str(key): int(value) for key, value in profiles["strategy"].value_counts().to_dict().items()},
            "news_depth_counts": {str(key): int(value) for key, value in profiles["news_depth"].value_counts().sort_index().to_dict().items()},
            "age_group_counts": {str(key): int(value) for key, value in profiles["age_group"].value_counts().to_dict().items()},
        },
        "persona_construct_validity": persona_construct_tests,
        "returns": {
            "mean_agent_return": float(final_returns.mean()),
            "median_agent_return": float(np.median(final_returns)),
            "min_agent_return": float(final_returns.min()),
            "max_agent_return": float(final_returns.max()),
            "buy_hold_return_open_to_final_close": buy_hold_return,
        },
    }
    (OUTPUT_DIR / "audit_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(OUTPUT_DIR)


if __name__ == "__main__":
    main()
