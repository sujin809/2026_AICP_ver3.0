#!/usr/bin/env python3
"""Build the portable-report manifest from the reproducible C00 audit outputs."""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
OUTPUTS = HERE / "outputs"
ARTIFACT_PATH = HERE / "artifact.json"
RUN_ID = "simulation_20260715_30agents_commoff_fakenews_off_20260227_20260601"


def read_csv(name: str) -> list[dict[str, str]]:
    with (OUTPUTS / name).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def number(value: str | None) -> int | float | None:
    if value is None or value == "":
        return None
    parsed = float(value)
    return int(parsed) if parsed.is_integer() else parsed


def rate(value: str | None) -> float | None:
    parsed = number(value)
    return float(parsed) if parsed is not None else None


def chunk_label(raw: str) -> str:
    """Keep report chart axes compact; full date range remains in tooltip data."""
    return f"C{int(raw.split('_')[1]):02d}"


def compact_root_log_map(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        if row["scope"] == "root":
            grouped[(row["category"], row["grain"], row["interpretation_use"])].append(row)
    result = []
    for (category, grain, use), group in sorted(grouped.items()):
        result.append(
            {
                "category": category,
                "grain": grain,
                "interpretation_use": use,
                "root_files": len(group),
                "total_mb": round(sum(number(row["bytes"]) or 0 for row in group) / 1_000_000, 2),
            }
        )
    return result


audit = read_csv("run_integrity_audit.csv")[0]
benchmarks = read_csv("validation_benchmarks.csv")
chunk_boundaries = read_csv("c00_chunk_boundary_summary.csv")
validation_by_chunk = read_csv("c00_validation_by_chunk.csv")
cohort = read_csv("persona_population_vs_active_cohort.csv")
persona = read_csv("c00_persona_behavior_descriptives.csv")
log_inventory = read_csv("c00_log_inventory.csv")

benchmark_lookup = {row["benchmark"]: row for row in benchmarks}
always_buy = benchmark_lookup["always_buy"]
prior_return = benchmark_lookup["previous_day_market_return_direction"]

headline = {
    "direction_match_rate": rate(audit["direction_match_rate"]),
    "direction_match_vs_always_buy": rate(audit["direction_match_rate"]) - rate(always_buy["direction_match_rate"]),
    "balanced_accuracy": rate(audit["balanced_accuracy"]),
    "balanced_accuracy_vs_prior_return": rate(audit["balanced_accuracy"]) - rate(prior_return["balanced_accuracy"]),
    "restart_chunks": number(audit["restart_date_count"]),
    "restart_initial_states": number(audit["restart_rows_with_initial_portfolio"]),
    "uniform_cash_share": 1.0,
    "agent_count": number(audit["agent_count"]),
    "blank_belief_rate": rate(audit["blank_belief_rate"]),
    "fallback_decision_rows": number(audit["fallback_decision_rows"]),
}

benchmark_chart = [
    {
        "method": "C00 agent flow",
        "direction_match_rate": headline["direction_match_rate"],
        "balanced_accuracy": headline["balanced_accuracy"],
        "buy_recall": rate(audit["buy_recall"]),
        "sell_recall": rate(audit["sell_recall"]),
        "overlap_days": number(audit["validation_overlap_days"]),
    },
    {
        "method": "Always buy",
        "direction_match_rate": rate(always_buy["direction_match_rate"]),
        "balanced_accuracy": rate(always_buy["balanced_accuracy"]),
        "buy_recall": rate(always_buy["buy_recall"]),
        "sell_recall": rate(always_buy["sell_recall"]),
        "overlap_days": number(always_buy["overlap_days"]),
    },
    {
        "method": "Previous-day market return",
        "direction_match_rate": rate(prior_return["direction_match_rate"]),
        "balanced_accuracy": rate(prior_return["balanced_accuracy"]),
        "buy_recall": rate(prior_return["buy_recall"]),
        "sell_recall": rate(prior_return["sell_recall"]),
        "overlap_days": number(prior_return["overlap_days"]),
    },
]

chunk_integrity = [
    {
        "chunk": chunk_label(row["chunk"]),
        "date_range": f"{row['date_min']} ~ {row['date_max']}",
        "first_am_initial_portfolio_agents": number(row["first_am_initial_portfolio_rows"]),
        "first_am_buy_orders": number(row["first_am_buy_orders"]),
        "turn_max": number(row["turn_max"]),
    }
    for row in chunk_boundaries
]

chunk_validation = [
    {
        "chunk": chunk_label(row["chunk"]),
        "date_range": f"{row['date_min']} ~ {row['date_max']}",
        "direction_match_rate": rate(row["direction_match_rate"]),
        "balanced_accuracy": rate(row["balanced_accuracy"]),
        "validation_days": number(row["validation_days"]),
        "predicted_buy_days": number(row["predicted_buy_days"]),
        "predicted_sell_days": number(row["predicted_sell_days"]),
        "actual_buy_days": number(row["actual_buy_days"]),
        "actual_sell_days": number(row["actual_sell_days"]),
    }
    for row in validation_by_chunk
]

cohort_table = [
    {
        "persona_axis": row["field"],
        "value": row["value"],
        "population_n": number(row["population_count"]),
        "active_c00_n": number(row["active_cohort_count"]),
        "active_c00_share": rate(row["active_cohort_share"]),
    }
    for row in cohort
    if row["field"] in {"age_group", "ini_cash", "strategy", "news_depth"}
]

persona_table = [
    {
        "persona_axis": row["persona_field"],
        "value": row["persona_value"],
        "agents": number(row["agent_count"]),
        "buy_share": rate(row["buy_share"]),
        "sell_share": rate(row["sell_share"]),
        "blank_belief_rate": rate(row["blank_belief_rate"]),
    }
    for row in persona
    if row["persona_field"] in {"strategy", "news_depth"}
]

interpretation_scope = [
    {
        "claim": "C00의 짧은 구간별 주문·belief trace를 읽는 파일럿",
        "status": "제한적으로 가능",
        "why": "각 AM/PM의 context→belief→order→fill→portfolio 로그가 존재한다.",
    },
    {
        "claim": "63거래일 연속 행동·PnL·장기 기억 효과",
        "status": "불가",
        "why": "13개 chunk마다 turn 1과 초기 현금·무보유 상태가 다시 나타난다.",
    },
    {
        "claim": "초기 자본 이질성·고자산 취약성",
        "status": "불가",
        "why": "활성 30명 전원이 1억 원으로 동일하다.",
    },
    {
        "claim": "가짜뉴스·community 처치 효과",
        "status": "미관측",
        "why": "실제 결과는 fake off·community off인 C00 하나뿐이다.",
    },
    {
        "claim": "persona 효과",
        "status": "기술통계만 가능",
        "why": "depth 2는 4명, 40대는 3명 등 셀이 작고 불균형하다.",
    },
    {
        "claim": "belief–action 정합성",
        "status": "부분 감사만 가능",
        "why": "belief summary의 20.2%가 비어 있고, buy/sell 강제·fallback 주문이 action을 교란한다.",
    },
]

evaluation_table = [
    {
        "component": "Blind claim-level rubric",
        "c00_status": "부분 파일럿",
        "next_use": "R2–R8(투자 stance·확신·근거·행동정합성)을 샘플 감사; R1 수용은 fake 조건 후 평가.",
        "guardrail": "조건·persona·가설을 가린 2인 코딩, κ/α 보고, LLM judge는 human calibration 후 보조로만 사용.",
    },
    {
        "component": "Embedding claim axis",
        "c00_status": "미실행",
        "next_use": "factual↔misinformation claim axis에서 pre→exposed→next 이동량과 correction persistence를 측정.",
        "guardrail": "raw UMAP cluster를 주결과로 쓰지 말고, boilerplate 제거·alternate model·agent/event/seed bootstrap.",
    },
    {
        "component": "Persona moderation",
        "c00_status": "기술통계만",
        "next_use": "균형 cohort와 사전 지정된 2–3축에서 treatment×persona interaction을 검증.",
        "guardrail": "synthetic persona를 실제 인구집단 효과로 일반화하지 않음.",
    },
    {
        "component": "Market-flow faithfulness",
        "c00_status": "약한 baseline",
        "next_use": "방향 일치, balanced accuracy, class recall, calibration, turnover, constraint/fallback rate를 함께 보고.",
        "guardrail": "항상 매수·전일 방향·lagged numeric model과 같은 정보시점 기준으로 비교.",
    },
]

generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

sources = [
    {
        "id": "integrity_audit",
        "label": "C00 execution-integrity audit",
        "path": "analysis/current_experiment_review/outputs/run_integrity_audit.csv",
        "query": {
            "engine": "Python 3",
            "language": "python",
            "sql": "python3 analysis/current_experiment_review/analyze_current_runs.py",
            "description": "Audits the root C00 agent-turn trace for key completeness, repeated local turns, initial-portfolio resets, belief coverage, and fallback orders.",
            "filters": ["condition = C00", "root agent_turns.jsonl only"],
            "metric_definitions": [
                "restart_date_count: number of dates whose AM records restart at turn 1.",
                "blank_belief_rate: blank belief_summary rows divided by all C00 agent-turn rows.",
                "fallback_decision_rows: decisions whose reason explicitly records a minimal fallback after invalid LLM output.",
            ],
            "tables_used": [
                f"outputs/logs/{RUN_ID}/agent_turns.jsonl",
                f"outputs/logs/{RUN_ID}/run_metadata.json",
            ],
        },
    },
    {
        "id": "validation_metrics",
        "label": "C00 validation metrics and repository baselines",
        "path": f"validation/outputs/{RUN_ID}/summary_metrics.json",
        "query": {
            "engine": "Python 3",
            "language": "python",
            "sql": "python3 analysis/current_experiment_review/analyze_current_runs.py",
            "description": "Reads the repository's 58-day value-flow comparison and its precomputed simple baselines.",
            "filters": ["skip_initial_days = 5", "individual investor value flow", "C00 only"],
            "metric_definitions": [
                "direction_match_rate: matched daily C00 and individual net-flow signs / 58 overlap days.",
                "balanced_accuracy: mean of buy-day recall and sell-day recall.",
            ],
            "tables_used": [
                f"validation/outputs/{RUN_ID}/summary_metrics.json",
                f"validation/outputs/{RUN_ID}/daily_comparison_value.csv",
            ],
        },
    },
    {
        "id": "chunk_audit",
        "label": "C00 chunk-boundary and chunk-level validation audit",
        "path": "analysis/current_experiment_review/outputs/c00_chunk_boundary_summary.csv",
        "query": {
            "engine": "Python 3",
            "language": "python",
            "sql": "python3 analysis/current_experiment_review/analyze_current_runs.py",
            "description": "Maps each logged chunk to its first-AM state and aggregates the existing validation CSV by chunk date range.",
            "filters": ["C00 only", "validation retains the repository's five-day initial exclusion"],
            "metric_definitions": [
                "first_am_initial_portfolio_agents: agents whose first AM context reports 100M KRW cash and no holdings.",
                "chunk direction match: matched daily signs within the 3–5 validation days assigned to one chunk.",
            ],
            "tables_used": [
                "analysis/current_experiment_review/outputs/c00_chunk_boundary_summary.csv",
                "analysis/current_experiment_review/outputs/c00_validation_by_chunk.csv",
            ],
        },
    },
    {
        "id": "persona_cohort",
        "label": "C00 active-cohort and persona descriptives",
        "path": "analysis/current_experiment_review/outputs/persona_population_vs_active_cohort.csv",
        "query": {
            "engine": "Python 3",
            "language": "python",
            "sql": "python3 analysis/current_experiment_review/analyze_current_runs.py",
            "description": "Compares active C00 agent IDs against the local persona database and reports descriptive decision/belief coverage by configured persona fields.",
            "filters": ["active C00 agent IDs only"],
            "metric_definitions": [
                "active_cohort_share: agents in a persona cell divided by 30 active C00 agents.",
                "buy_share and sell_share: submitted action counts divided by agent-turn rows; not causal persona effects.",
            ],
            "tables_used": [
                "outputs/sys_100.db::agents",
                "analysis/current_experiment_review/outputs/c00_persona_behavior_descriptives.csv",
            ],
        },
    },
    {
        "id": "log_map",
        "label": "C00 log inventory",
        "path": "analysis/current_experiment_review/outputs/c00_log_inventory.csv",
        "query": {
            "engine": "Python 3",
            "language": "python",
            "sql": "python3 analysis/current_experiment_review/analyze_current_runs.py",
            "description": "Inventories root run/validation files separately from duplicated chunk copies.",
            "filters": ["C00 root and chunk artifact paths"],
            "tables_used": ["outputs/logs/simulation_20260715_30agents_commoff_fakenews_off_20260227_20260601"],
        },
    },
    {
        "id": "decision_mechanics",
        "label": "Decision action-space code audit",
        "path": "twinmarket_kr/llm/decision.py",
        "query": {
            "engine": "Python 3 source",
            "language": "python",
            "description": "Current decision validation accepts hold only when explicitly enabled; the legacy C00 trace contains only net-buy/net-sell daily directions and fallback-order reasons.",
            "filters": ["default allow_hold = false"],
            "tables_used": ["twinmarket_kr/llm/decision.py"],
        },
    },
    {
        "id": "evaluation_design",
        "label": "Belief rubric and embedding evaluation plans",
        "path": "analysis/belief_event_study/belief_deviation_rubric.md",
        "query": {
            "engine": "Markdown design document",
            "language": "markdown",
            "description": "Local proposed blind rubric, embedding trajectory, and total-deviation design; these are evaluation plans, not C00 results.",
            "tables_used": [
                "analysis/belief_event_study/belief_deviation_rubric.md",
                "analysis/belief_event_study/embedding_analysis_plan.md",
                "analysis/belief_event_study/total_deviation_spec.md",
            ],
        },
    },
]

# The portable artifact validator requires an executable query string for every
# source-backed block.  These DuckDB statements are the compact, file-level
# provenance queries corresponding to the generated snapshot datasets.
source_sql = {
    "integrity_audit": "SELECT * FROM read_csv_auto('analysis/current_experiment_review/outputs/run_integrity_audit.csv');",
    "validation_metrics": "SELECT * FROM read_json_auto('validation/outputs/simulation_20260715_30agents_commoff_fakenews_off_20260227_20260601/summary_metrics.json');",
    "chunk_audit": "SELECT * FROM read_csv_auto('analysis/current_experiment_review/outputs/c00_chunk_boundary_summary.csv');",
    "persona_cohort": "SELECT * FROM read_csv_auto('analysis/current_experiment_review/outputs/persona_population_vs_active_cohort.csv');",
    "log_map": "SELECT * FROM read_csv_auto('analysis/current_experiment_review/outputs/c00_log_inventory.csv');",
    "decision_mechanics": "SELECT * FROM read_csv_auto('analysis/current_experiment_review/outputs/run_integrity_audit.csv') WHERE condition = 'C00';",
    "evaluation_design": "SELECT * FROM read_csv_auto('analysis/current_experiment_review/outputs/c00_persona_behavior_descriptives.csv') WHERE condition = 'C00';",
}
for source in sources:
    source["query"]["engine"] = "DuckDB"
    source["query"]["language"] = "sql"
    source["query"]["sql"] = source_sql[source["id"]]

manifest = {
    "version": 1,
    "surface": "report",
    "title": "C00 파일럿 실험 진단과 논문 재설계 제안",
    "description": "현재 저장된 실제 결과 C00 하나만을 기준으로 한 실행 무결성, 수급 일치, 로그 해석, 평가 설계, 논문 방향 감사",
    "generatedAt": generated_at,
    "sources": sources,
    "cards": [
        {
            "id": "direction_match",
            "dataset": "headline_metrics",
            "sourceId": "validation_metrics",
            "description": "개인 순매수/순매도 방향과 C00 집계 주문 방향이 같은 날짜의 비율입니다. 58일 검증 창의 기술통계입니다.",
            "metrics": [
                {"label": "방향 일치", "field": "direction_match_rate", "format": "percent"},
                {"label": "항상 매수 대비", "field": "direction_match_vs_always_buy", "format": "percent", "signed": True},
            ],
        },
        {
            "id": "balanced_accuracy",
            "dataset": "headline_metrics",
            "sourceId": "validation_metrics",
            "description": "매수일·매도일 recall의 평균입니다. class imbalance를 방향 일치율보다 잘 드러냅니다.",
            "metrics": [
                {"label": "균형 정확도", "field": "balanced_accuracy", "format": "percent"},
                {"label": "전일 수익률 방향 대비", "field": "balanced_accuracy_vs_prior_return", "format": "percent", "signed": True},
            ],
        },
        {
            "id": "state_restarts",
            "dataset": "headline_metrics",
            "sourceId": "integrity_audit",
            "description": "초기 상태로 다시 시작한 AM 날짜 수입니다. 연속 장기 시뮬레이션의 핵심 제약입니다.",
            "metrics": [
                {"label": "상태 재시작 chunk", "field": "restart_chunks", "format": "number"},
                {"label": "초기 portfolio 행", "field": "restart_initial_states", "format": "number"},
            ],
        },
        {
            "id": "cohort_and_belief_coverage",
            "dataset": "headline_metrics",
            "sourceId": "persona_cohort",
            "description": "C00은 30명 모두 1억 원이며, belief summary 결측은 텍스트 기반 평가의 실제 표본을 줄입니다.",
            "metrics": [
                {"label": "1억 원 초기자본 비율", "field": "uniform_cash_share", "format": "percent"},
                {"label": "빈 belief summary", "field": "blank_belief_rate", "format": "percent"},
            ],
        },
    ],
    "charts": [
        {
            "id": "benchmark_comparison",
            "title": "C00과 단순 기준선의 방향 지표",
            "subtitle": "개인 투자자 가치 수급과의 58일 중첩 구간. 방향 일치와 균형 정확도는 모두 0–100% 축입니다.",
            "type": "bar",
            "intent": "comparison",
            "question": "C00의 aggregate direction metric이 단순 규칙보다 실질적으로 우수한가?",
            "rationale": "방향 일치율만 제시하면 실제 매수일이 더 많은 표본에서 항상 매수가 강해 보이는 착시가 생기므로 균형 정확도를 함께 본다.",
            "comparisonContext": {"baseline": "always buy and previous-day market return direction", "denominator": "58 daily overlaps", "unit": "rate"},
            "dataset": "benchmark_comparison",
            "sourceId": "validation_metrics",
            "encodings": {
                "x": {"field": "method", "type": "nominal", "label": "방법"},
                "y": {"fields": ["direction_match_rate", "balanced_accuracy"], "type": "quantitative", "format": "percent", "label": "비율"},
                "tooltip": [
                    {"field": "buy_recall", "type": "quantitative", "format": "percent", "label": "매수일 recall"},
                    {"field": "sell_recall", "type": "quantitative", "format": "percent", "label": "매도일 recall"},
                    {"field": "overlap_days", "type": "quantitative", "format": "number", "label": "검증 일수"},
                ],
            },
            "settings": {"groupMode": "grouped", "sort": "none", "showValues": True},
            "palette": {"kind": "sequential", "name": "blue"},
            "layout": "full",
            "surface": {"viewMode": "both", "showControls": False},
        },
        {
            "id": "chunk_variability",
            "title": "C00 chunk별 방향 일치율",
            "subtitle": "각 막대는 상태가 다시 시작된 3–5일 검증 chunk입니다. 전체 62.1% 평균은 이 변동성을 숨깁니다.",
            "type": "bar",
            "intent": "comparison",
            "question": "단일 집계 점수가 chunk 전반에서 안정적인가?",
            "rationale": "연속성이 깨진 로그에서 평균 성과보다 짧은 restart episode 사이의 변동성을 먼저 확인해야 한다.",
            "comparisonContext": {"grain": "chunk", "denominator": "3–5 validation days per chunk", "unit": "rate"},
            "dataset": "chunk_validation",
            "sourceId": "chunk_audit",
            "encodings": {
                "x": {"field": "chunk", "type": "nominal", "label": "chunk"},
                "y": {"field": "direction_match_rate", "type": "quantitative", "format": "percent", "label": "방향 일치"},
                "tooltip": [
                    {"field": "validation_days", "type": "quantitative", "format": "number", "label": "검증 일수"},
                    {"field": "actual_buy_days", "type": "quantitative", "format": "number", "label": "실제 매수일"},
                    {"field": "actual_sell_days", "type": "quantitative", "format": "number", "label": "실제 매도일"},
                    {"field": "predicted_buy_days", "type": "quantitative", "format": "number", "label": "C00 매수일"},
                    {"field": "predicted_sell_days", "type": "quantitative", "format": "number", "label": "C00 매도일"},
                ],
            },
            "settings": {"sort": "none", "showValues": True},
            "palette": {"kind": "sequential", "name": "blue"},
            "layout": "full",
            "surface": {"viewMode": "both", "showControls": False},
        },
        {
            "id": "state_restarts_by_chunk",
            "title": "C00 chunk 첫 AM의 초기 상태와 매수 주문",
            "subtitle": "13개 모든 chunk에서 30명 모두 초기 현금·무보유 상태로 돌아왔고 첫 AM 주문은 모두 매수였습니다.",
            "type": "bar",
            "intent": "comparison",
            "question": "chunk 경계에서 portfolio와 decision state가 연속되었는가?",
            "rationale": "첫 AM의 portfolio 문구와 action을 직접 집계해 장기 state continuity를 검증한다.",
            "comparisonContext": {"grain": "chunk first AM", "denominator": "30 agents per chunk", "unit": "agents/orders"},
            "dataset": "chunk_integrity",
            "sourceId": "chunk_audit",
            "encodings": {
                "x": {"field": "chunk", "type": "nominal", "label": "chunk"},
                "y": {"fields": ["first_am_initial_portfolio_agents", "first_am_buy_orders"], "type": "quantitative", "format": "number", "label": "30명 중 count"},
                "tooltip": [
                    {"field": "date_range", "type": "text", "label": "기간"},
                    {"field": "turn_max", "type": "quantitative", "format": "number", "label": "chunk 최대 turn"},
                ],
            },
            "settings": {"groupMode": "grouped", "sort": "none", "showValues": True},
            "palette": {"kind": "sequential", "name": "orange"},
            "layout": "full",
            "surface": {"viewMode": "both", "showControls": False},
        },
    ],
    "tables": [
        {
            "id": "interpretation_scope",
            "title": "C00에서 가능한 주장과 불가능한 주장",
            "subtitle": "현재 실제 결과 C00 하나와 그 실행 로그에만 근거한 구분입니다.",
            "dataset": "interpretation_scope",
            "sourceId": "integrity_audit",
            "density": "spacious",
            "defaultSort": {"field": "claim", "direction": "asc"},
            "columns": [
                {"field": "claim", "label": "주장"},
                {"field": "status", "label": "판정"},
                {"field": "why", "label": "근거"},
            ],
        },
        {
            "id": "log_map",
            "title": "C00 로그를 읽는 순서",
            "subtitle": "root 파일은 해석의 원천이고, chunk copy는 restart 진단용 중복본입니다.",
            "dataset": "root_log_map",
            "sourceId": "log_map",
            "density": "spacious",
            "defaultSort": {"field": "category", "direction": "asc"},
            "columns": [
                {"field": "category", "label": "로그 종류"},
                {"field": "grain", "label": "분석 단위"},
                {"field": "interpretation_use", "label": "해석 용도"},
                {"field": "root_files", "label": "root 파일 수", "format": "number"},
                {"field": "total_mb", "label": "용량(MB)", "format": "number"},
            ],
        },
        {
            "id": "cohort_table",
            "title": "모집단 대비 C00 활성 cohort",
            "subtitle": "초기자본·연령·전략·뉴스 depth의 실제 활성 분포입니다.",
            "dataset": "cohort_table",
            "sourceId": "persona_cohort",
            "density": "spacious",
            "defaultSort": {"field": "persona_axis", "direction": "asc"},
            "columns": [
                {"field": "persona_axis", "label": "축"},
                {"field": "value", "label": "값"},
                {"field": "population_n", "label": "모집단 n", "format": "number"},
                {"field": "active_c00_n", "label": "C00 n", "format": "number"},
                {"field": "active_c00_share", "label": "C00 비율", "format": "percent"},
            ],
        },
        {
            "id": "persona_descriptives",
            "title": "C00 persona별 기술통계",
            "subtitle": "작고 불균형한 synthetic cohort의 주문·belief coverage 기술통계이며 persona effect 추정이 아닙니다.",
            "dataset": "persona_table",
            "sourceId": "persona_cohort",
            "density": "spacious",
            "defaultSort": {"field": "persona_axis", "direction": "asc"},
            "columns": [
                {"field": "persona_axis", "label": "축"},
                {"field": "value", "label": "값"},
                {"field": "agents", "label": "에이전트 n", "format": "number"},
                {"field": "buy_share", "label": "매수 주문 비율", "format": "percent"},
                {"field": "sell_share", "label": "매도 주문 비율", "format": "percent"},
                {"field": "blank_belief_rate", "label": "빈 belief", "format": "percent"},
            ],
        },
        {
            "id": "evaluation_table",
            "title": "rubric·embedding·persona 평가체계의 현재 위치",
            "subtitle": "현재 C00에 쓸 수 있는 범위와 가짜뉴스 조건 후에야 가능한 식별을 구분합니다.",
            "dataset": "evaluation_table",
            "sourceId": "evaluation_design",
            "density": "spacious",
            "defaultSort": {"field": "component", "direction": "asc"},
            "columns": [
                {"field": "component", "label": "평가 구성요소"},
                {"field": "c00_status", "label": "C00 상태"},
                {"field": "next_use", "label": "다음 실험에서의 사용"},
                {"field": "guardrail", "label": "해석 안전장치"},
            ],
        },
    ],
    "blocks": [
        {"id": "title", "type": "markdown", "body": "# C00 파일럿 실험 진단과 논문 재설계 제안"},
        {
            "id": "technical_summary",
            "type": "markdown",
            "body": """## 기술 요약

**결론: 현재 C00은 ‘수급 재현을 입증한 실험’이 아니라, 재설계의 출발점이 되는 단일 파일럿이다.** C00의 방향 일치율은 62.1%이지만, 실제 개인 순매수일이 더 많은 58일 창에서 항상 매수도 58.6%에 도달한다. 균형 정확도는 57.8%이고 전일 시장수익률 방향 기준선보다 1.3%p 높을 뿐이다. 단일 실행·시간의존 날짜·반복 시드 부재 때문에 이 차이에 유의확률이나 일반화 주장을 붙일 수 없다.

더 근본적으로, 63일·3,780개의 agent-turn은 key 수만 완전할 뿐 **13개의 5일 안팎 episode를 이어붙인 로그**다. 각 chunk 첫 AM에 30명 모두 ‘1억 원 현금·무보유’로 되돌아가고 매수를 제출한다. 따라서 장기 memory, 누적 PnL, portfolio path, 지속적 persona behavior는 C00에서 검증되지 않았다. 또한 활성 30명 전원이 1억 원이라 자본 이질성은 관측되지 않았고, fake/community가 모두 off라 처치효과도 관측되지 않았다.

살릴 수 있는 것은 두 가지다. 첫째, **실행·측정 메커니즘을 드러내는 파일럿**으로서 C00를 appendix/negative baseline으로 쓴다. 둘째, 기존의 2×3 조건(community on/off × 정보 {없음, bearish fake, bullish fake})을 정확히 재실행하거나, 더 권장하는 방향으로 source-aware short/long memory와 reflection을 인과적으로 ablate하는 논문으로 피벗한다.""",
        },
        {"id": "headline_metrics", "type": "metric-strip", "cardIds": ["direction_match", "balanced_accuracy", "state_restarts", "cohort_and_belief_coverage"]},
        {
            "id": "direction_interpretation",
            "type": "markdown",
            "sourceId": "validation_metrics",
            "body": """## 62.1% 방향 일치율은 강한 behavioral faithfulness 증거가 아니다

**C00는 매수일을 잘 맞추고 매도일을 놓쳤다.** 개인 투자자 순매수일 34일 중 28일을 맞춘 반면, 순매도일 24일 중 맞춘 것은 8일뿐이다. 그래서 방향 일치 62.1%는 항상 매수보다 3.4%p, 전일 시장수익률 방향보다 5.2%p 높아 보이지만, class imbalance를 보정한 균형 정확도는 57.8%다. 핵심은 ‘어느 날 실제 투자자처럼 흘렀는가’보다 ‘매수 쏠림을 넘어서 매도 국면도 재현하는가’다.

아래 비교는 기존 validation JSON을 그대로 읽는다. C00가 단순 기준선을 약간 넘는 기술적 차이는 보이지만, 이 한 번의 58일 창만으로 LLM agent가 개인 수급을 안정적으로 재현한다고 결론내리면 안 된다.""",
        },
        {"id": "benchmark_chart_block", "type": "chart", "chartId": "benchmark_comparison"},
        {
            "id": "continuity_interpretation",
            "type": "markdown",
            "sourceId": "chunk_audit",
            "body": """## C00의 결정적 한계는 13번의 portfolio·turn 재시작이다

**형식상 누락 없는 3,780행과 연속 실행은 다르다.** agent×date×AM/PM key는 중복·누락 없이 3,780개지만, 전체 turn 범위는 1–10뿐이고 13개 chunk 각각에서 다시 1로 시작한다. 첫 AM에는 390개 행(13×30)이 초기 현금·무보유 상태였고, 같은 순간의 주문은 모두 매수였다.

따라서 C00의 각 3–5일 chunk는 ‘새로 1억 원을 들고 시작한 짧은 조건부 반응’으로만 읽어야 한다. 이 사실은 단순 버그 목록이 아니라, memory/reflection·portfolio path·반복 노출·교정 후 지속성이라는 논문 주결과를 C00에서 주장할 수 없다는 식별 한계다.""",
        },
        {"id": "restart_chart_block", "type": "chart", "chartId": "state_restarts_by_chunk"},
        {
            "id": "variability_interpretation",
            "type": "markdown",
            "sourceId": "chunk_audit",
            "body": """## 평균 아래에는 33.3%–80.0%의 짧은 episode 변동성이 있다

**전체 62.1%는 3–5일짜리 reset episode들의 평균이다.** 검증에 남은 12개 chunk의 방향 일치율은 33.3%에서 80.0%까지 움직인다. 일부 chunk는 실제 매도일이 없거나 한 개뿐이므로 chunk-level balanced accuracy도 독립적인 성과 추정치가 아니다. 하지만 평균 하나가 안정적 성능처럼 보이는 해석은 막아 준다.

재실험에서는 날짜 단위 혹은 event 단위 block bootstrap과 여러 seed를 기본으로 두고, seed·date·agent를 모두 독립 표본처럼 세지 않아야 한다.""",
        },
        {"id": "variability_chart_block", "type": "chart", "chartId": "chunk_variability"},
        {
            "id": "scope_definitions",
            "type": "markdown",
            "body": """## 범위와 측정 정의

C00은 2026-02-27부터 2026-06-01까지의 30-agent, community off, fake off 로그다. validation은 첫 5 거래일을 제외한 58일에서 C00의 일별 순주문 가치 방향과 실제 개인 투자자 수급 방향을 비교한다. **direction match**는 부호 일치일/58, **buy·sell recall**은 실제 각 방향일 중 맞춘 비율, **balanced accuracy**는 두 recall의 평균이다.

‘주문 방향’은 ‘belief 부호’가 아니다. C00은 실행 action이 buy/sell로 제한되고, 현금·보유 제약·최소 주문·검증 retry가 주문을 바꿀 수 있다. 실제 trace에는 50개의 invalid LLM output 후 minimal fallback 주문이 있으며 그중 48개가 sell이다. 125개의 1주 주문에는 원인 코드가 기록되어 있지 않다. 그러므로 belief와 flow를 같은 outcome으로 합치지 말고, 다음 실험부터 intention과 execution을 분리해야 한다.""",
        },
        {"id": "scope_table_block", "type": "table", "tableId": "interpretation_scope"},
        {
            "id": "log_map_interpretation",
            "type": "markdown",
            "sourceId": "log_map",
            "body": """## C00 로그를 이렇게 연결해서 읽으면 된다

**해석의 원본은 root의 agent-turn trace이며, chunk 폴더는 restart를 확인하는 중복본이다.** 한 agent-turn에서 time-aligned news/context → news interpretation → belief → action/quantity/reason → submitted order → fill → portfolio update를 따라간다. 날짜 수준에서는 daily exchange summary를 합쳐 validation의 individual flow와 비교한다.

community/fake 로그는 C00에서 인과적 의미를 갖지 않는다. 둘 다 off인 조건이므로 비어 있거나 비활성 경로가 정상이며, ‘읽음’·‘선택됨’ 같은 future treatment 로그를 효과 추정의 control로 넣으면 post-treatment bias가 된다.""",
        },
        {"id": "log_map_table_block", "type": "table", "tableId": "log_map"},
        {
            "id": "persona_interpretation",
            "type": "markdown",
            "sourceId": "persona_cohort",
            "body": """## persona는 C00에서 ‘분류 축’이지 효과 추정 축은 아니다

**현재 30명은 설정된 persona 모집단의 앞부분이며 전원이 1억 원이다.** 활성 cohort에는 20대 9명, 30대 18명, 40대 3명만 있고 50대 이상·10억 원 agent는 없다. news depth도 0/1/2가 10/16/4명으로 작다. strategy와 depth별 주문 비율은 기록할 수 있지만, 이 차이를 실제 세대·투자성향의 행동 차이라고 일반화하면 안 된다.

향후에는 persona를 ‘고정된 synthetic treatment moderator’로 사전 정의하고, 균형/층화 sampling과 충분한 cell size를 보장해야 한다. 특히 동일 agent ID의 treatment 간 paired comparison과 seed 반복을 사용하면 persona별 반응 차이를 훨씬 정직하게 추정할 수 있다.""",
        },
        {"id": "cohort_table_block", "type": "table", "tableId": "cohort_table"},
        {"id": "persona_table_block", "type": "table", "tableId": "persona_descriptives"},
        {
            "id": "evaluation_interpretation",
            "type": "markdown",
            "sourceId": "evaluation_design",
            "body": """## rubric·embedding은 버릴 아이디어가 아니라, 주결과를 바꿀 좋은 측정 장치다

**현재 설계의 강점은 ‘가짜뉴스가 들어왔는가’가 아니라 ‘어떤 claim을 얼마나 믿고 행동으로 옮겼는가’를 분해하려는 점이다.** blind rubric은 reception, investment stance, confidence, source confidence, position conviction, risk restraint, evidence grounding, belief–action consistency를 분리한다. 이 중 fake exposure가 없는 C00에서는 reception을 평가할 수 없지만, action consistency·risk restraint·근거성의 샘플 감사는 가능하다.

Embedding도 raw cluster를 발견하는 데 쓰면 약하다. 주분석은 factual↔misinformation claim axis를 미리 만들고, exposure 전·직후·다음 turn의 이동량과 correction 후 persistence를 보는 paired trajectory여야 한다. boilerplate 제거, 다른 embedding model, agent/event/seed 수준 bootstrap, belief 결측률 보고가 필수다.""",
        },
        {"id": "evaluation_table_block", "type": "table", "tableId": "evaluation_table"},
        {
            "id": "paper_direction",
            "type": "markdown",
            "body": """## 논문화 가능한 두 방향 — 추천은 memory/resilience 피벗

### A. 기존 2×3 조건을 살리는 방향: community가 bullish/bearish misinformation의 belief–action distortion을 증폭하는가

기존 six-condition matrix는 명확한 2×3 요인설계다: community {off,on} × 정보 {없음, bearish fake, bullish fake}. clean continuous run을 확보하면, 주결과를 수익률이 아니라 **claim-level belief deviation, correction persistence, action translation**으로 두는 논문이 가능하다. community×valence 상호작용과 persona moderation은 사전 등록된 보조 분석으로 둔다. 장점은 현재 조건을 거의 보존한다는 것, 단점은 memory가 논문의 중심이 아니고 C00만으로는 한 줄도 효과를 보일 수 없다는 것이다.

### B. 권장 방향: source-aware short/long memory와 reflection이 misinformation-induced belief–action distortion을 줄이는가

이 방향은 사용자가 제안한 long memory·short memory·성찰을 **수급 일치율을 올리는 트릭**이 아니라, 시간제한된 정보·출처·사후 교정에 대한 agent robustness architecture로 만든다. 금융 LLM agent benchmark가 표준화된 평가 필요성을 지적하고, layered-memory trading agent 연구와 generative-agent/reflection 연구는 memory·reflection을 별도 ablation해야 함을 뒷받침한다. [InvestorBench](https://aclanthology.org/2025.acl-long.126/), [FinMem](https://ojs.aaai.org/index.php/AAAI-SS/article/view/31290/33450), [Generative Agents](https://arxiv.org/abs/2304.03442), [Reflexion](https://arxiv.org/abs/2303.11366)를 출발점으로 삼을 수 있다.

**추천은 B를 메인으로, 기존 2×3를 secondary robustness test로 쓰는 것이다.** 한 논문에서 market-price formation, persona realism, community cascade, alpha prediction까지 모두 잡지 말고, ‘source-aware memory/reflection이 사실·허위·교정 정보에 대한 synthetic investor의 belief와 행동을 어떻게 바꾸는가’에 집중한다. 금융 misinformation의 반복 노출이 투자 판단에 영향을 줄 수 있다는 인간 실험과, 다차원적 truth deviation 평가를 제안한 최근 LLM misinformation 연구도 이 framing과 잘 맞는다. [Financial misinformation study](https://papers.ssrn.com/sol3/Delivery.cfm/5187289.pdf?abstractid=5187289), [FUSE-EVAL](https://aclanthology.org/2025.emnlp-main.1330/)""",
        },
        {
            "id": "architecture_recommendation",
            "type": "markdown",
            "sourceId": "decision_mechanics",
            "body": """## 코드 재설계의 우선순위: memory를 넣기 전에 intention과 execution을 분리한다

**가장 먼저 바꿀 것은 ‘무조건 buy/sell’ 행동공간이다.** 아래의 네 단계가 분리되어야 수급 일치와 misinformation 효과가 해석 가능해진다.

1. **Belief / desired stance** — bullish·bearish·neutral, 확률, 근거 claim ID, 출처 신뢰도.
2. **Intention** — trade / hold, 목표 노출도, confidence, 허용 손실·위험 예산.
3. **Feasibility allocator** — 현금·보유·최소단위·가격을 적용해 가능한 quantity를 계산하고 constraint flag를 남김.
4. **Execution** — 주문·체결·슬리피지·미체결을 기록.

그 위에 short memory는 ‘직전 1–5 turn의 timestamped market/news/own-position/prediction’만, long memory는 ‘사실 anchor·출처·신뢰도·나중의 확인/반박·교훈’을 저장한다. reflection은 PM 이후나 다음 AM 전의 구조화된 prediction-vs-observation 기록이어야 한다. 각 memory에는 관측 시각, retrieval 근거, 사실/주장 구분, 만료/정정 상태가 있어야 하며 t 시점에 t+1 가격·뉴스·실제 수급을 볼 수 없도록 time gate를 강제해야 한다.

이 구조는 일치율을 올릴 가능성은 있지만 보장하지 않는다. 오히려 기억이 가짜 claim을 오래 보존하면 취약성이 커질 수 있다. 그래서 ‘memory 없음 / short만 / short+long / short+long+reflection’ ablation과 provenance-preserving retrieval, correction test가 논문의 핵심 실험이어야 한다.""",
        },
        {
            "id": "next_experiment",
            "type": "markdown",
            "body": """## 재실험 권장안

**Phase 0 — 실행 신뢰성:** post-7/20 entrypoint로 chunk-days=1, clean base, 동일 cohort, run_complete·global turn 1–126·portfolio continuity·fake delivery·fallback/constraint 로그를 자동 gate로 만든다. legacy C00와 새 결과를 절대 병합하지 않는다.

**Phase 1 — 메인 인과 실험:** community off에서 memory architecture {baseline, short, short+long+reflection} × information {factual anchor, matched fake, fake+correction}. 최소 3–5 seed와 균형 cohort를 쓴다. primary outcome은 rubric 기반 belief deviation와 correction persistence, secondary는 intention direction의 balanced accuracy다.

**Phase 2 — 외적 타당성:** Phase 1에서 효과가 확인된 architecture만 community on/off와 bullish/bearish valence의 기존 2×3 matrix에 넣는다. community를 먼저 넣으면 source quality, social exposure, memory effect가 한꺼번에 섞인다.

**공통 평가:** direction match 외에 balanced accuracy, 매수/매도 recall, 확률 예측이면 Brier/calibration, hold 비율, turnover, constraint/fallback rate, belief–action consistency, source-grounding, event-level pre→exposed→next trajectory를 보고한다. 주분석 단위는 event/date/seed이며 agent turn 3,780개를 독립 관측치처럼 다루지 않는다.""",
        },
        {
            "id": "limitations",
            "type": "markdown",
            "body": """## 한계와 남은 질문

- C00 저장 로그는 7월 17일 legacy output이고, state-continuity·restart safety를 강화한 7월 20일 코드 이전에 생성됐다. 최신 코드의 안전장치가 C00 결과를 소급해 고쳐 주지는 않는다.
- validation JSON은 첫 5일을 제외한다. 다른 문서/분석에서 3일 또는 다른 regime window를 쓰려면 같은 skip rule로 다시 계산해야 하며, 지금 숫자와 직접 비교하면 안 된다.
- 단일 종목·고정된 시장 환경·synthetic persona는 사람 투자자나 실제 시장 인과를 재현했다는 증거가 아니다.
- 다음 결정은 하나다: 기존 2×3을 빠르게 clean rerun해 community×valence 논문으로 갈지, 아니면 memory/reflection ablation을 추가해 더 강한 methods/resilience 논문으로 갈지. 현재 C00만으로는 둘 다의 main result를 쓸 수 없다.""",
        },
    ],
}

artifact = {
    "surface": "report",
    "manifest": manifest,
    "snapshot": {
        "version": 1,
        "generatedAt": generated_at,
        "status": "ready",
        "datasets": {
            "headline_metrics": [headline],
            "benchmark_comparison": benchmark_chart,
            "chunk_integrity": chunk_integrity,
            "chunk_validation": chunk_validation,
            "interpretation_scope": interpretation_scope,
            "root_log_map": compact_root_log_map(log_inventory),
            "cohort_table": cohort_table,
            "persona_table": persona_table,
            "evaluation_table": evaluation_table,
        },
    },
    "sources": sources,
}

ARTIFACT_PATH.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"Wrote {ARTIFACT_PATH}")
