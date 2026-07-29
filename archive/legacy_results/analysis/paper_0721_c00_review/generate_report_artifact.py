#!/usr/bin/env python3
"""Build the portable HTML-report artifact for the latest C00 review."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
OUTPUTS = HERE / "outputs"
ARTIFACT_PATH = HERE / "artifact.json"


def read_csv(name: str) -> list[dict[str, str]]:
    with (OUTPUTS / name).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def number(value: str | int | float | None) -> int | float | None:
    if value is None or value == "" or str(value).lower() == "nan":
        return None
    parsed = float(value)
    return int(parsed) if parsed.is_integer() else parsed


audit = json.loads((OUTPUTS / "audit_summary.json").read_text(encoding="utf-8"))
headline_rows = read_csv("headline_metrics.csv")
sensitivity_rows = read_csv("sensitivity_metrics.csv")
price_baseline_rows = read_csv("decision_time_price_baselines.csv")
feasibility_rows = read_csv("decision_feasibility.csv")
construct_rows = read_csv("persona_construct_group_metrics.csv")

headline_lookup = {row["metric"]: number(row["value"]) for row in headline_rows}
primary = audit["primary_metrics"]
scope = audit["source"]["scope"]
integrity = audit["integrity_reconciliation"]
trace = audit["belief_and_news_trace"]
runtime = audit["llm_runtime"]
construct_tests = audit["persona_construct_validity"]

headline = {
    "sealed_days": scope["date_count"],
    "configured_coverage": scope["date_count"] / 63,
    "direction_match_rate": primary["direction_match_rate"],
    "direction_match_vs_always_buy": primary["direction_match_rate"] - headline_lookup["always_buy_direction_match"],
    "balanced_accuracy": primary["balanced_accuracy"],
    "balanced_accuracy_vs_prior_market": primary["balanced_accuracy"] - headline_lookup["prior_market_balanced_accuracy"],
    "am_c00_balanced_accuracy": next(
        number(row["balanced_accuracy"])
        for row in sensitivity_rows
        if row["phase"] == "AM only" and int(row["skip_initial_days"]) == 0
    ),
    "am_price_baseline_balanced_accuracy": next(
        number(row["balanced_accuracy"])
        for row in price_baseline_rows
        if row["subturn"] == "am" and int(row["skip_initial_days"]) == 0
    ),
    "pm_c00_balanced_accuracy": next(
        number(row["balanced_accuracy"])
        for row in sensitivity_rows
        if row["phase"] == "PM only" and int(row["skip_initial_days"]) == 0
    ),
    "pm_price_baseline_balanced_accuracy": next(
        number(row["balanced_accuracy"])
        for row in price_baseline_rows
        if row["subturn"] == "pm" and int(row["skip_initial_days"]) == 0
    ),
    "post_five_day_pearson": headline_lookup["post_first_five_day_pearson"],
    "unmapped_news_rate": headline_lookup["influential_news_unmapped_item_rate"],
}
headline["am_c00_vs_price_baseline"] = (
    headline["am_c00_balanced_accuracy"] - headline["am_price_baseline_balanced_accuracy"]
)
headline["pm_c00_vs_price_baseline"] = (
    headline["pm_c00_balanced_accuracy"] - headline["pm_price_baseline_balanced_accuracy"]
)

benchmark_comparison = [
    {
        "method": "C00 AM+PM",
        "direction_match_rate": primary["direction_match_rate"],
        "balanced_accuracy": primary["balanced_accuracy"],
        "buy_recall": primary["buy_recall"],
        "sell_recall": primary["sell_recall"],
    },
    {
        "method": "Always buy",
        "direction_match_rate": headline_lookup["always_buy_direction_match"],
        "balanced_accuracy": 0.5,
        "buy_recall": 1.0,
        "sell_recall": 0.0,
    },
    {
        "method": "Prior-market direction",
        "direction_match_rate": 0.5555555555555556,
        "balanced_accuracy": headline_lookup["prior_market_balanced_accuracy"],
        "buy_recall": 0.5,
        "sell_recall": 0.6470588235294118,
    },
]

sensitivity = [
    {
        "phase": row["phase"],
        "skip_initial_days": number(row["skip_initial_days"]),
        "days": number(row["days"]),
        "direction_match_rate": number(row["direction_match_rate"]),
        "buy_recall": number(row["buy_recall"]),
        "sell_recall": number(row["sell_recall"]),
        "balanced_accuracy": number(row["balanced_accuracy"]),
        "pearson": number(row["pearson"]),
        "spearman": number(row["spearman"]),
    }
    for row in sensitivity_rows
]

price_baselines = [
    {
        "baseline": row["baseline"],
        "subturn": row["subturn"],
        "skip_initial_days": number(row["skip_initial_days"]),
        "days": number(row["days"]),
        "direction_match_rate": number(row["direction_match_rate"]),
        "buy_recall": number(row["buy_recall"]),
        "sell_recall": number(row["sell_recall"]),
        "balanced_accuracy": number(row["balanced_accuracy"]),
        "model_only_correct_days": number(row["model_only_correct_days"]),
        "baseline_only_correct_days": number(row["baseline_only_correct_days"]),
        "paired_p": number(row["paired_mcnemar_exact_two_sided_p"]),
    }
    for row in price_baseline_rows
]

phase_comparison = []
for phase in ("AM only", "PM only", "AM+PM"):
    lookup = {int(row["skip_initial_days"]): row for row in sensitivity if row["phase"] == phase}
    phase_comparison.append(
        {
            "phase": phase,
            "balanced_accuracy_all": lookup[0]["balanced_accuracy"],
            "balanced_accuracy_skip5": lookup[5]["balanced_accuracy"],
            "pearson_skip5": lookup[5]["pearson"],
        }
    )

phase_lookup = {
    (row["phase"], int(row["skip_initial_days"])): row
    for row in sensitivity
}
price_lookup = {
    (row["subturn"], int(row["skip_initial_days"])): row
    for row in price_baselines
}
decision_time_comparison = [
    {
        "method": "C00 AM",
        "information": "AM prompt",
        "direction_match_rate": phase_lookup[("AM only", 0)]["direction_match_rate"],
        "balanced_accuracy": phase_lookup[("AM only", 0)]["balanced_accuracy"],
        "buy_recall": phase_lookup[("AM only", 0)]["buy_recall"],
        "sell_recall": phase_lookup[("AM only", 0)]["sell_recall"],
    },
    {
        "method": "AM gap contrarian",
        "information": "AM prompt-visible opening gap",
        "direction_match_rate": price_lookup[("am", 0)]["direction_match_rate"],
        "balanced_accuracy": price_lookup[("am", 0)]["balanced_accuracy"],
        "buy_recall": price_lookup[("am", 0)]["buy_recall"],
        "sell_recall": price_lookup[("am", 0)]["sell_recall"],
    },
    {
        "method": "C00 PM",
        "information": "PM prompt",
        "direction_match_rate": phase_lookup[("PM only", 0)]["direction_match_rate"],
        "balanced_accuracy": phase_lookup[("PM only", 0)]["balanced_accuracy"],
        "buy_recall": phase_lookup[("PM only", 0)]["buy_recall"],
        "sell_recall": phase_lookup[("PM only", 0)]["sell_recall"],
    },
    {
        "method": "PM return contrarian",
        "information": "PM prompt-visible current return",
        "direction_match_rate": price_lookup[("pm", 0)]["direction_match_rate"],
        "balanced_accuracy": price_lookup[("pm", 0)]["balanced_accuracy"],
        "buy_recall": price_lookup[("pm", 0)]["buy_recall"],
        "sell_recall": price_lookup[("pm", 0)]["sell_recall"],
    },
]

correlation_sensitivity = [
    {
        "excluded": f"skip {int(row['skip_initial_days'])}",
        "pearson": row["pearson"],
        "spearman": row["spearman"],
        "days": row["days"],
    }
    for row in sensitivity
    if row["phase"] == "AM+PM"
]

decision_mechanics = [
    {
        "feasible_action_set": row["feasible_action_set"],
        "turns": number(row["turn_rows"]),
        "buy": number(row["buy_rows"]),
        "sell": number(row["sell_rows"]),
        "mean_quantity": number(row["mean_quantity"]),
        "max_quantity_rate": number(row["max_quantity_selected_rate"]),
    }
    for row in feasibility_rows
]

construct_order = {"low": 0, "medium": 1, "high": 2}
persona_construct = sorted(
    [
        {
            "construct": "처분효과" if row["construct"] == "disposition_effect" else "위험선호",
            "category": row["category"],
            "agents": number(row["agent_count"]),
            "metric": row["metric"],
            "value": number(row["value"]),
        }
        for row in construct_rows
    ],
    key=lambda row: (row["construct"], construct_order.get(row["category"], 99)),
)

scope_table = [
    {"item": "Branch / push commit", "value": "off_result_20260721 / 5605732", "interpretation": "최신 push 결과"},
    {"item": "Run-start commit", "value": "8604f9", "interpretation": "81f35f8 이후 restart-safety 포함"},
    {"item": "Sealed scope", "value": "2026-02-27–2026-05-04 / 45일", "interpretation": "설정 종료 2026-06-01보다 짧음"},
    {"item": "Scope status", "value": "truncated_posthoc", "interpretation": "63일 정상완주로 표현 금지"},
    {"item": "Agent-turn integrity", "value": "2,700 unique / duplicate 0", "interpretation": "45일 범위 안 state 연속"},
    {"item": "Out-of-scope partial", "value": "2026-05-06 chunk 존재", "interpretation": "main analysis에서 제외"},
]

interpretation_scope = [
    {"claim": "45일 state-continuous operational baseline", "status": "가능", "reason": "turn 1–90, portfolio와 belief transition이 연속"},
    {"claim": "63일 완주 결과", "status": "불가", "reason": "45일 post-hoc sealed partial run"},
    {"claim": "개인 수급 방향의 양방향 주문 variation", "status": "기술통계로 가능", "reason": "BA 60.9%, sell recall 64.7%"},
    {"claim": "단순 기준선보다 유의하게 우수", "status": "불가", "reason": "항상 매수 raw match가 더 높고 단일 seed 진단검정 비유의"},
    {"claim": "Prompt-visible 가격정보 이상의 정합성", "status": "반대 증거", "reason": "AM gap BA 82.8%, PM current-return BA 92.9%로 C00을 크게 상회"},
    {"claim": "시장 가격형성·수급균형 재현", "status": "불가", "reason": "외생가격, 전량체결, agent 주문 간 clearing 없음"},
    {"claim": "persona prompt construct enactment", "status": "탐색적으로 가능", "reason": "처분효과와 위험선호에서 단조 신호"},
    {"claim": "community/fake treatment effect", "status": "미관측", "reason": "실제 결과는 community off, fake off C00 하나"},
]

evaluation_table = [
    {"component": "Flow alignment", "primary": "BA, buy/sell recall, AM-only, residual flow", "guardrail": "AM gap·PM current-return contrarian와 incremental 성과 보고"},
    {"component": "Rubric", "primary": "stance, confidence, grounding, desired-action consistency", "guardrail": "두 blind coder, κ/α, forced-trade conflict 분리"},
    {"component": "Embedding", "primary": "paired claim-axis trajectory", "guardrail": "UMAP cluster를 주결과로 사용 금지"},
    {"component": "Persona", "primary": "counterfactual trait construct score", "guardrail": "human demographic effect로 일반화 금지"},
    {"component": "Memory", "primary": "correction uptake, stale reuse, reflection groundedness", "guardrail": "source/timestamp/invalidation을 보존"},
    {"component": "Runtime", "primary": "retry, timeout, latency, token/cost", "guardrail": "긴 memory prompt의 운영비용 포함"},
]

experiment_plan = [
    {"phase": "0. Gate", "comparison": "코드·데이터 무결성", "primary_outcome": "continuity, provenance, time gate", "decision": "gate 실패 시 API 본실험 중단"},
    {"phase": "1. Clean memory", "comparison": "M0 / M1 / M2 / M3", "primary_outcome": "price-only 대비 incremental BA, grounding", "decision": "best memory architecture 선택"},
    {"phase": "2. Resilience", "comparison": "factual / fake / fake→correction", "primary_outcome": "belief shift, persistence, recovery", "decision": "memory가 도움/해로움 판정"},
    {"phase": "3. Community", "comparison": "community off/on", "primary_outcome": "diffusion, polarization, excessive trading", "decision": "기존 2×3 조건 확장"},
]

generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

sources: list[dict[str, Any]] = [
    {
        "id": "latest_c00_audit",
        "label": "Latest C00 execution and metric audit",
        "path": "analysis/paper_0721_c00_review/outputs/audit_summary.json",
        "query": {
            "engine": "Python 3",
            "language": "python",
            "sql": "WITH audit AS (SELECT * FROM read_json_auto('analysis/paper_0721_c00_review/outputs/audit_summary.json')), headline AS (SELECT * FROM read_csv_auto('analysis/paper_0721_c00_review/outputs/headline_metrics.csv')), price_baselines AS (SELECT * FROM read_csv_auto('analysis/paper_0721_c00_review/outputs/decision_time_price_baselines.csv')) SELECT * FROM audit CROSS JOIN (SELECT list(struct_pack(metric := metric, value := value, unit := unit)) AS headline_metrics FROM headline) h CROSS JOIN (SELECT list(struct_pack(baseline := baseline, balanced_accuracy := balanced_accuracy, direction_match_rate := direction_match_rate)) AS price_baselines FROM price_baselines) p;",
            "description": "Recomputes the sealed-scope execution integrity, daily flow metrics, uncertainty diagnostics, decision mechanics, news mapping, runtime, and returns.",
            "filters": ["run_id = c00_commoff_fakeoff", "sealed dates only", "exclude partial 2026-05-06 chunk"],
            "metric_definitions": ["See analysis/paper_0721_c00_review/source_notes.md"],
            "tables_used": [
                "outputs/logs/paper_0721/c00_commoff_fakeoff/agent_turns.jsonl",
                "outputs/logs/paper_0721/c00_commoff_fakeoff/exchange_fills.csv",
                "validation/outputs/c00_commoff_fakeoff_2026-05-04/daily_comparison_value.csv",
            ],
        },
    },
    {
        "id": "persona_construct",
        "label": "Persona prompt-enactment construct audit",
        "path": "analysis/paper_0721_c00_review/outputs/persona_construct_group_metrics.csv",
        "query": {
            "engine": "Python 3",
            "language": "python",
            "sql": "SELECT * FROM read_csv_auto('analysis/paper_0721_c00_review/outputs/persona_construct_group_metrics.csv');",
            "description": "Reconstructs pre-trade average cost and computes per-agent disposition and normalized-order-intensity construct scores.",
            "filters": ["A001-A030", "both buy and sell feasible", "exploratory"],
            "metric_definitions": ["Disposition gap and order intensity are defined in source_notes.md"],
            "tables_used": ["agent_turns.csv", "exchange_fills.csv", "outputs/sys_100.db::agents"],
        },
    },
    {
        "id": "evaluation_design",
        "label": "Belief rubric and embedding plans",
        "path": "analysis/belief_event_study/belief_deviation_rubric.md",
        "query": {
            "engine": "Markdown review",
            "language": "text",
            "sql": "SELECT * FROM (VALUES ('Flow alignment'), ('Rubric'), ('Embedding'), ('Persona'), ('Memory'), ('Runtime')) AS evaluation(component);",
            "description": "Maps existing rubric and embedding plans to what C00 can support and what requires treatment data.",
            "tables_used": [
                "analysis/belief_event_study/belief_deviation_rubric.md",
                "analysis/belief_event_study/embedding_analysis_plan.md",
                "analysis/belief_event_study/total_deviation_spec.md",
            ],
        },
    },
]

cards = [
    {
        "id": "scope_card",
        "dataset": "headline",
        "sourceId": "latest_c00_audit",
        "description": "설정된 63거래일 중 사후 봉인되어 실제 분석에 사용한 범위입니다.",
        "metrics": [
            {"label": "봉인 거래일", "field": "sealed_days", "format": "number"},
            {"label": "설정 대비 coverage", "field": "configured_coverage", "format": "percent"},
        ],
    },
    {
        "id": "direction_card",
        "dataset": "headline",
        "sourceId": "latest_c00_audit",
        "description": "실제 개인 순수급 방향과 C00 aggregate signed flow 방향의 일치입니다.",
        "metrics": [
            {"label": "방향 일치", "field": "direction_match_rate", "format": "percent"},
            {"label": "항상 매수 대비", "field": "direction_match_vs_always_buy", "format": "percent", "signed": True},
        ],
    },
    {
        "id": "balanced_card",
        "dataset": "headline",
        "sourceId": "latest_c00_audit",
        "description": "매수일·매도일 recall을 동일 가중한 지표입니다.",
        "metrics": [
            {"label": "Balanced accuracy", "field": "balanced_accuracy", "format": "percent"},
            {"label": "전일 시장방향 대비", "field": "balanced_accuracy_vs_prior_market", "format": "percent", "signed": True},
        ],
    },
    {
        "id": "am_price_card",
        "dataset": "headline",
        "sourceId": "latest_c00_audit",
        "description": "AM prompt에 보이는 시가갭만 역방향으로 사용한 규칙의 BA는 82.8%입니다.",
        "metrics": [
            {"label": "AM C00 BA", "field": "am_c00_balanced_accuracy", "format": "percent"},
            {"label": "시가갭 규칙 대비", "field": "am_c00_vs_price_baseline", "format": "percent", "signed": True},
        ],
    },
    {
        "id": "pm_price_card",
        "dataset": "headline",
        "sourceId": "latest_c00_audit",
        "description": "PM prompt에 보이는 당일수익률만 역방향으로 사용한 규칙의 BA는 92.9%입니다.",
        "metrics": [
            {"label": "PM C00 BA", "field": "pm_c00_balanced_accuracy", "format": "percent"},
            {"label": "당일수익률 규칙 대비", "field": "pm_c00_vs_price_baseline", "format": "percent", "signed": True},
        ],
    },
    {
        "id": "quality_card",
        "dataset": "headline",
        "sourceId": "latest_c00_audit",
        "description": "초기 5일을 빼면 size correlation은 거의 사라지고 뉴스 provenance mapping은 대부분 실패합니다.",
        "metrics": [
            {"label": "5일 제외 Pearson", "field": "post_five_day_pearson", "format": "number"},
            {"label": "Unmapped news", "field": "unmapped_news_rate", "format": "percent"},
        ],
    },
]

charts = [
    {
        "id": "benchmark_chart",
        "title": "C00은 단순 기준선을 뚜렷하게 넘지 못한다",
        "subtitle": "45거래일 개인투자자 value-flow 방향. 방향 일치와 balanced accuracy를 함께 표시합니다.",
        "type": "bar",
        "intent": "comparison",
        "question": "C00의 방향 성능이 항상 매수와 전일 시장방향보다 실질적으로 우수한가?",
        "rationale": "실제 순매수일이 더 많으므로 raw match만 보면 class imbalance의 영향을 받는다.",
        "comparisonContext": {"grain": "trading date", "denominator": "45 days", "unit": "rate"},
        "dataset": "benchmark_comparison",
        "sourceId": "latest_c00_audit",
        "encodings": {
            "x": {"field": "method", "type": "nominal", "label": "방법"},
            "y": {"fields": ["direction_match_rate", "balanced_accuracy"], "type": "quantitative", "format": "percent", "label": "비율"},
            "tooltip": [
                {"field": "buy_recall", "type": "quantitative", "format": "percent", "label": "매수일 recall"},
                {"field": "sell_recall", "type": "quantitative", "format": "percent", "label": "매도일 recall"},
            ],
        },
        "settings": {"groupMode": "grouped", "sort": "none", "showValues": True},
        "palette": {"kind": "sequential", "name": "blue"},
        "layout": "full",
        "surface": {"viewMode": "both", "showControls": False},
    },
    {
        "id": "decision_time_chart",
        "title": "Prompt-visible 가격만 쓴 역행 규칙이 C00을 크게 상회",
        "subtitle": "AM은 시가갭, PM은 당일 종가수익률의 부호를 반대로 사용합니다. 같은 decision-time 정보 비교입니다.",
        "type": "bar",
        "intent": "comparison",
        "question": "복잡한 agent pipeline이 이미 prompt에 있는 가격정보보다 incremental signal을 만드는가?",
        "rationale": "개인투자자 flow의 강한 contrarian relation을 보존하는지 같은 정보집합에서 검증한다.",
        "comparisonContext": {"grain": "trading date", "denominator": "45 days", "unit": "rate"},
        "dataset": "decision_time_comparison",
        "sourceId": "latest_c00_audit",
        "encodings": {
            "x": {"field": "method", "type": "nominal", "label": "방법"},
            "y": {"fields": ["direction_match_rate", "balanced_accuracy"], "type": "quantitative", "format": "percent", "label": "비율"},
            "tooltip": [
                {"field": "information", "type": "text", "label": "정보시점"},
                {"field": "buy_recall", "type": "quantitative", "format": "percent", "label": "매수일 recall"},
                {"field": "sell_recall", "type": "quantitative", "format": "percent", "label": "매도일 recall"},
            ],
        },
        "settings": {"groupMode": "grouped", "sort": "none", "showValues": True},
        "palette": {"kind": "sequential", "name": "orange"},
        "layout": "full",
        "surface": {"viewMode": "both", "showControls": False},
    },
    {
        "id": "phase_chart",
        "title": "AM 신호는 burn-in 제외 후 거의 chance, PM은 유지",
        "subtitle": "전체 45일과 첫 5일 제외 40일의 balanced accuracy. PM은 same-day context를 사용합니다.",
        "type": "bar",
        "intent": "comparison",
        "question": "결과가 초기 자금 배치와 정보시점에 얼마나 민감한가?",
        "rationale": "AM-only는 ex-ante 진단에 가깝고 PM-only는 same-day reconstruction에 가깝다.",
        "comparisonContext": {"baseline": "0.5 chance BA", "grain": "trading date", "unit": "rate"},
        "dataset": "phase_comparison",
        "sourceId": "latest_c00_audit",
        "encodings": {
            "x": {"field": "phase", "type": "nominal", "label": "집계 단계"},
            "y": {"fields": ["balanced_accuracy_all", "balanced_accuracy_skip5"], "type": "quantitative", "format": "percent", "label": "Balanced accuracy"},
            "tooltip": [{"field": "pearson_skip5", "type": "quantitative", "format": "number", "label": "5일 제외 Pearson"}],
        },
        "settings": {"groupMode": "grouped", "sort": "none", "showValues": True},
        "palette": {"kind": "sequential", "name": "orange"},
        "layout": "full",
        "surface": {"viewMode": "both", "showControls": False},
    },
    {
        "id": "correlation_chart",
        "title": "Pearson 0.503은 초기 배치일 제외에 매우 민감하다",
        "subtitle": "AM+PM signed value-flow의 초기일 제외 민감도. 5일 제외 Pearson은 0.018입니다.",
        "type": "bar",
        "intent": "comparison",
        "question": "크기 상관이 synchronized initial deployment 밖에서도 유지되는가?",
        "rationale": "첫날 30명 전원이 buy-only로 시작해 공통 대형 순매수 outlier가 발생한다.",
        "comparisonContext": {"baseline": "zero correlation", "grain": "trading date", "unit": "correlation"},
        "dataset": "correlation_sensitivity",
        "sourceId": "latest_c00_audit",
        "encodings": {
            "x": {"field": "excluded", "type": "nominal", "label": "초기 제외"},
            "y": {"fields": ["pearson", "spearman"], "type": "quantitative", "format": "number", "label": "상관계수"},
            "tooltip": [{"field": "days", "type": "quantitative", "format": "number", "label": "남은 거래일"}],
        },
        "settings": {"groupMode": "grouped", "sort": "none", "showValues": True},
        "palette": {"kind": "sequential", "name": "blue"},
        "layout": "full",
        "surface": {"viewMode": "both", "showControls": False},
    },
]

tables = [
    {
        "id": "scope_table",
        "title": "실험 identity와 사용 가능한 범위",
        "subtitle": "최신 push와 실제 봉인된 분석 scope를 분리합니다.",
        "dataset": "scope_table",
        "sourceId": "latest_c00_audit",
        "density": "spacious",
        "defaultSort": {"field": "item", "direction": "asc"},
        "columns": [{"field": "item", "label": "항목"}, {"field": "value", "label": "값"}, {"field": "interpretation", "label": "해석"}],
    },
    {
        "id": "interpretation_table",
        "title": "현재 C00에서 가능한 주장과 불가능한 주장",
        "subtitle": "C00 하나의 결과를 논문 문장으로 옮길 때의 경계입니다.",
        "dataset": "interpretation_scope",
        "sourceId": "latest_c00_audit",
        "density": "spacious",
        "defaultSort": {"field": "claim", "direction": "asc"},
        "columns": [{"field": "claim", "label": "주장"}, {"field": "status", "label": "판정"}, {"field": "reason", "label": "근거"}],
    },
    {
        "id": "price_baseline_table",
        "title": "Decision-time price-only contrarian baseline",
        "subtitle": "같은 정보시점 가격만 사용한 규칙과 C00의 paired 진단입니다.",
        "dataset": "price_baselines",
        "sourceId": "latest_c00_audit",
        "density": "spacious",
        "defaultSort": {"field": "baseline", "direction": "asc"},
        "columns": [
            {"field": "baseline", "label": "규칙"},
            {"field": "skip_initial_days", "label": "초기 제외", "format": "number"},
            {"field": "direction_match_rate", "label": "방향 일치", "format": "percent"},
            {"field": "balanced_accuracy", "label": "BA", "format": "percent"},
            {"field": "model_only_correct_days", "label": "C00만 정답", "format": "number"},
            {"field": "baseline_only_correct_days", "label": "규칙만 정답", "format": "number"},
            {"field": "paired_p", "label": "paired exact p", "format": "number"},
        ],
    },
    {
        "id": "decision_table",
        "title": "강제 buy/sell 행동공간의 실제 영향",
        "subtitle": "hold가 없고 모든 주문이 전량 체결됩니다.",
        "dataset": "decision_mechanics",
        "sourceId": "latest_c00_audit",
        "density": "spacious",
        "defaultSort": {"field": "turns", "direction": "desc"},
        "columns": [
            {"field": "feasible_action_set", "label": "가능 행동"},
            {"field": "turns", "label": "turns", "format": "number"},
            {"field": "buy", "label": "buy", "format": "number"},
            {"field": "sell", "label": "sell", "format": "number"},
            {"field": "mean_quantity", "label": "평균 수량", "format": "number"},
            {"field": "max_quantity_rate", "label": "최대수량 선택", "format": "percent"},
        ],
    },
    {
        "id": "construct_table",
        "title": "Persona prompt construct enactment",
        "subtitle": f"Agent-level 탐색 상관: 처분효과 rho={construct_tests['disposition_ordinal_spearman']['rho']:.3f}, 위험선호 rho={construct_tests['risk_ordinal_spearman']['rho']:.3f}. 인간집단 효과가 아닙니다.",
        "dataset": "persona_construct",
        "sourceId": "persona_construct",
        "density": "spacious",
        "defaultSort": {"field": "construct", "direction": "asc"},
        "columns": [
            {"field": "construct", "label": "Construct"},
            {"field": "category", "label": "Prompt 수준"},
            {"field": "agents", "label": "agents", "format": "number"},
            {"field": "value", "label": "평균 score", "format": "number"},
        ],
    },
    {
        "id": "evaluation_table",
        "title": "평가체계 재설계",
        "subtitle": "flow, rubric, embedding, persona, memory, runtime을 서로 다른 outcome으로 유지합니다.",
        "dataset": "evaluation_table",
        "sourceId": "evaluation_design",
        "density": "spacious",
        "defaultSort": {"field": "component", "direction": "asc"},
        "columns": [{"field": "component", "label": "구성"}, {"field": "primary", "label": "Primary 측정"}, {"field": "guardrail", "label": "해석 안전장치"}],
    },
    {
        "id": "experiment_table",
        "title": "권장 단계별 재실험",
        "subtitle": "복잡한 full factorial보다 순차적 gate와 ablation을 사용합니다.",
        "dataset": "experiment_plan",
        "sourceId": "evaluation_design",
        "density": "spacious",
        "defaultSort": {"field": "phase", "direction": "asc"},
        "columns": [{"field": "phase", "label": "단계"}, {"field": "comparison", "label": "비교"}, {"field": "primary_outcome", "label": "Primary outcome"}, {"field": "decision", "label": "판정"}],
    },
]

blocks = [
    {"id": "title", "type": "markdown", "body": "# 최신 C00 실험 재분석과 논문 재설계 제안"},
    {
        "id": "technical_summary",
        "type": "markdown",
        "body": (
            "## 기술 요약\n\n"
            "**최신 C00은 이전처럼 chunk마다 초기화된 로그가 아니라, 45거래일 범위 안에서 state가 연속된 operational baseline이다.** "
            "그러나 설정상 63일 중 45일만 사후 봉인된 partial run이고, 방향 일치 60.0%는 항상 매수 62.2%보다 낮다. "
            "더 중요한 것은 AM 시가갭 역행 규칙의 BA 82.8%, PM 당일수익률 역행 규칙의 BA 92.9%에 비해 C00이 각각 55.6%, 59.8%에 그친다는 점이다. 복잡한 agent pipeline이 prompt에 이미 있는 강한 개인투자자 역행 패턴을 희석한다. 일별 Pearson 0.503도 첫 자금 배치 outlier를 제외하면 0.018이다.\n\n"
            "현재 결과를 수급 예측 성공으로 포장하기보다, **source-aware short/long memory와 outcome-based reflection이 price-only behavior 이상의 정합성을 높이는 조건과 misinformation을 고착시키는 조건**을 인과적으로 ablate하는 논문으로 피벗하는 것이 가장 강하다."
        ),
    },
    {"id": "headline_strip", "type": "metric-strip", "cardIds": ["scope_card", "direction_card", "balanced_card", "am_price_card", "pm_price_card", "quality_card"]},
    {"id": "scope_heading", "type": "markdown", "body": "## 범위와 실험 identity\n\n실행 시작 commit `8604f9`는 지정된 `81f35f8` 이후이므로 7월 20일 restart-safety 변경을 반영한다. 다만 완료 상태는 `truncated_posthoc`이며 5월 6일 partial chunk는 분석 범위 밖이다."},
    {"id": "scope_table_block", "type": "table", "tableId": "scope_table"},
    {"id": "findings_heading", "type": "markdown", "body": "## 핵심 결과\n\n**C00은 양방향 주문 variation을 만들지만, 같은 decision-time 가격정보만 쓴 역행 규칙보다 크게 못한다.** 방향 일치 Wilson 95% 구간은 45.5%–73.0%이고 항상 매수보다 raw match가 낮다. AM과 PM의 가격 역행 규칙은 burn-in 이후에도 각각 BA 81.8%, 93.5%를 유지한다."},
    {"id": "benchmark_chart_block", "type": "chart", "chartId": "benchmark_chart"},
    {"id": "decision_time_heading", "type": "markdown", "body": "## 결정적인 same-information benchmark\n\nAM에는 시가 대 전일종가 gap, PM에는 당일 종가수익률이 이미 prompt에 있다. 그 부호를 반대로만 쓰면 개인투자자 수급 방향을 훨씬 잘 맞힌다. 따라서 LLM의 가치는 price-only baseline 대비 incremental improvement 또는 price-only residual flow 설명력으로 평가해야 한다."},
    {"id": "decision_time_chart_block", "type": "chart", "chartId": "decision_time_chart"},
    {"id": "price_baseline_table_block", "type": "table", "tableId": "price_baseline_table"},
    {"id": "timing_heading", "type": "markdown", "body": "## 초기 배치와 정보시점\n\n첫날은 30명 전원이 1억 원 현금·무보유여서 buy-only다. PM은 당일 종가와 뉴스 맥락을 사용하므로 prediction보다 same-day behavioral reconstruction으로 해석해야 한다."},
    {"id": "phase_chart_block", "type": "chart", "chartId": "phase_chart"},
    {"id": "correlation_chart_block", "type": "chart", "chartId": "correlation_chart"},
    {"id": "interpretation_heading", "type": "markdown", "body": "## 주장 범위\n\n현재 engine은 외생 실제가격에 모든 주문을 전량 체결한다. 따라서 market clearing이나 endogenous price formation이 아니라 individual-investor net-flow direction alignment를 측정한다."},
    {"id": "interpretation_table_block", "type": "table", "tableId": "interpretation_table"},
    {"id": "mechanics_heading", "type": "markdown", "body": "## 행동공간과 belief 해석\n\n**hold 부재가 가장 큰 구조적 confounder다.** 306 turns는 한 방향만 가능하고 589 turns는 1주 주문이다. belief가 관망이어도 환경이 buy/sell을 강제하므로 desired stance, feasibility, allocation, execution을 분리해야 한다."},
    {"id": "decision_table_block", "type": "table", "tableId": "decision_table"},
    {"id": "belief_quality", "type": "markdown", "body": f"## Belief·news·runtime 품질\n\n최종 belief와 state continuity는 완전하지만 belief retry는 20.0%, decision retry는 7.7%다. API {runtime['api_requests']:,}회 중 error는 {runtime['api_errors']:,}회다. 더 큰 문제는 raw selected-news {trace['raw_influential_news_items']:,}개 중 {trace['unmapped_influential_news_items']:,}개가 mapping되지 않아 source-grounding과 fake-influential 평가가 막힌다는 점이다. Fake 조건 전에 ID normalization과 offline backfill이 필요하다."},
    {"id": "persona_heading", "type": "markdown", "body": "## Persona 해석\n\n30명 전원 1억 원은 wealth heterogeneity 실험으로는 실패지만 homogeneous-endowment descriptive baseline으로는 사용할 수 있다. Persona trait는 공동으로 비무작위 설정되어 있어 내부 타당성을 제공하지 않는다. 집단별 수급 일치 차이보다, 단일 trait만 무작위로 바꾼 counterfactual clone이 의도한 construct를 enact하는지 검증하는 것이 더 강하다."},
    {"id": "construct_table_block", "type": "table", "tableId": "construct_table"},
    {"id": "methods_heading", "type": "markdown", "body": "## 평가 방법\n\nRubric과 embedding은 하나의 점수로 합치지 않는다. Rubric은 stance·confidence·source grounding·desired-action consistency를, embedding은 사전 정의한 factual↔misinformation claim axis의 paired trajectory를, 행동 로그는 feasibility와 execution을 각각 측정한다."},
    {"id": "evaluation_table_block", "type": "table", "tableId": "evaluation_table"},
    {"id": "paper_direction", "type": "markdown", "body": "## 권장 논문 방향\n\n가장 추천하는 질문은 **When Memory Helps—and Hurts LLM Investor Agents**다. C00을 M0 shallow-memory baseline으로 두고, structured short memory, source-aware long memory, outcome-based reflection을 단계적으로 ablate한다. Memory가 price-only contrarian behavior 이상의 flow alignment와 grounding을 개선할 수도 있지만 misinformation·stale regime·자기확신을 더 오래 보존할 수도 있다는 양면 가설을 검증한다.\n\n기존 6조건은 community off/on × clean/bearish/bullish information의 extension으로 살릴 수 있다. 다만 C00 하나에서는 어떤 condition effect도 추정할 수 없다."},
    {"id": "experiment_table_block", "type": "table", "tableId": "experiment_table"},
    {"id": "methodology", "type": "markdown", "body": "## 방법론\n\n주분석 단위는 event/date/seed다. 모든 arm에 AM opening-gap과 PM current-return contrarian baseline을 포함하고, raw flow와 price-only model residual을 함께 평가한다. 2,700 agent-turn을 독립 표본으로 세지 않고 동일 agent·date·event의 paired contrast와 date moving-block bootstrap, multi-seed mixed model을 사용한다. PM과 AM은 다른 estimand로 보고한다."},
    {"id": "limitations", "type": "markdown", "body": "## 한계와 robustness\n\n- 45일 partial run, 단일 seed, 단일 종목이다.\n- A001–A030 비무작위 cohort이며 전원 1억 원이다.\n- hold가 없고 full fill이라 turnover와 aggregate flow가 구조적으로 왜곡된다.\n- PM은 same-day context를 사용한다.\n- C00은 prompt-visible price contrarian baselines에 크게 뒤진다.\n- news provenance mapping이 깨져 있다.\n- persona 결과는 synthetic prompt enactment이지 human demographic effect가 아니다.\n- C00은 market clearing이나 price formation을 구현하지 않는다."},
    {"id": "recommended_next_steps", "type": "markdown", "body": "## 권장 다음 단계\n\n1. AM/PM price-only baseline과 residual target을 평가 protocol에 고정.\n2. selected-news ID normalization과 C00 backfill.\n3. hold, target exposure, deterministic allocator, constraint logging.\n4. AM/PM time contract와 leakage tests.\n5. M0–M3 memory schema 구현.\n6. clean-news smoke test 후 3–5 seed ablation.\n7. human-calibrated rubric과 claim-axis embedding.\n8. factual/fake/correction 후 community extension."},
    {"id": "further_questions", "type": "markdown", "body": "## 더 확인할 질문\n\n- Memory는 alignment보다 belief inertia를 먼저 키우는가?\n- Hold 추가는 direction quality를 높이는가, turnover만 줄이는가?\n- Reflection은 calibration을 개선하면서 persona consistency를 약화시키는가?\n- Correction을 받은 long memory는 최초 fake와 correction 중 무엇을 더 오래 보존하는가?\n- Source provenance를 강제하면 grounding은 개선되지만 행동 다양성이 줄어드는가?"},
]

artifact = {
    "surface": "report",
    "manifest": {
        "version": 1,
        "surface": "report",
        "title": "최신 C00 실험 재분석과 논문 재설계 제안",
        "description": "off_result_20260721의 실제 C00 하나를 기준으로 한 실행 무결성, 수급 지표, belief/persona/news 품질, 평가체계, memory 논문 피벗",
        "generatedAt": generated_at,
        "sources": sources,
        "cards": cards,
        "charts": charts,
        "tables": tables,
        "blocks": blocks,
    },
    "snapshot": {
        "version": 1,
        "generatedAt": generated_at,
        "status": "ready",
        "datasets": {
            "headline": [headline],
            "benchmark_comparison": benchmark_comparison,
            "decision_time_comparison": decision_time_comparison,
            "price_baselines": price_baselines,
            "phase_comparison": phase_comparison,
            "correlation_sensitivity": correlation_sensitivity,
            "decision_mechanics": decision_mechanics,
            "persona_construct": persona_construct,
            "scope_table": scope_table,
            "interpretation_scope": interpretation_scope,
            "evaluation_table": evaluation_table,
            "experiment_plan": experiment_plan,
        },
    },
    "sources": sources,
}

ARTIFACT_PATH.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
print(ARTIFACT_PATH)
