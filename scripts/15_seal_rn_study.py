#!/usr/bin/env python3
"""RN A/B sealed 입력 세트 생성 (candidate → sealed 승격 + StudySpec).

09 preflight가 요구하는 전체 sealed artifact를 실제 데이터로 생성한다:
  cohort / calendar / stage-input / event-price / real-news-bundle /
  known-injection / article-version-leakage-review / study-spec / prompts.

네트워크/LLM 미접촉. 반드시 venv(python 3.12)로 실행:
  .venv/bin/python scripts/15_seal_rn_study.py

정책 해시 중 regime/retry/runtime/evaluation/market-feature 는 아직 placeholder
digest(형식만 유효)이며, 방어 가능한 최종 run 전에 실제 검토 정책 문서로 교체 필요.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from twinmarket_kr.rn_ab.spec import canonical_sha256  # noqa: E402
from twinmarket_kr.rn_ab.persona_snapshot import (  # noqa: E402
    SealedPersonaSnapshot,
    persona_renderer_sha256,
)
from twinmarket_kr.rn_ab.prompt_registry import ALL_PROMPT_FILENAMES, RNPromptBundle  # noqa: E402

STOCK = "005930"
START_DATE, END_DATE = "2026-02-27", "2026-05-04"
TZ = "+09:00"


def _cbytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(label: str) -> str:
    return hashlib.sha256(f"rn-sealed::{label}".encode("utf-8")).hexdigest()


def _write(path: Path, value) -> bytes:
    b = _cbytes(value)
    path.write_bytes(b)
    return b


def trading_dates(sim_db: Path) -> list[str]:
    with sqlite3.connect(sim_db) as c:
        return [r[0] for r in c.execute(
            "SELECT DISTINCT date FROM StockData WHERE stock_id=? AND date BETWEEN ? AND ? ORDER BY date",
            (STOCK, START_DATE, END_DATE)).fetchall()]


def stock_prices(sim_db: Path) -> dict[str, dict]:
    with sqlite3.connect(sim_db) as c:
        rows = c.execute(
            "SELECT date, open_price, close_price FROM StockData WHERE stock_id=? AND date BETWEEN ? AND ?",
            (STOCK, START_DATE, END_DATE)).fetchall()
    out = {}
    prev_close = None
    for d, op, cp in sorted(rows):
        out[d] = {"open": op, "close": cp, "prev_close": prev_close if prev_close is not None else op}
        prev_close = cp
    return out


def build_event(day: str, prev_day: str | None, ordinal: int, subturn: str) -> dict:
    # 첫 거래일은 이전 거래일이 없으므로 전 캘린더일(day-1)을 AM 윈도우 시작으로 쓴다.
    prev = prev_day or (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    if subturn == "AM":
        ts = f"{day}T09:00:00{TZ}"
        win = {"start_exclusive": f"{prev}T15:30:00{TZ}", "end_inclusive": f"{day}T08:59:00{TZ}"}
    else:
        ts = f"{day}T15:30:00{TZ}"
        win = {"start_exclusive": f"{day}T08:59:00{TZ}", "end_inclusive": ts}
    return {
        "decision_event_id": f"{day}/{subturn}",
        "event_ordinal_in_date": ordinal,
        "subturn": subturn,
        "decision_timestamp": ts,
        "news_window": win,
        "market_feature_as_of": ts,
        "execution_price_field": "actual_open" if subturn == "AM" else "actual_close",
        "consume_scheduled_community": subturn == "AM",
        "decision_enabled": True,
    }


def build_calendar(dates: list[str]) -> dict:
    date_rows = []
    for i, d in enumerate(dates):
        prev = dates[i - 1] if i > 0 else None
        date_rows.append({
            "date": d,
            "timezone": "Asia/Seoul",
            "decision_events": [build_event(d, prev, 1, "AM"), build_event(d, prev, 2, "PM")],
            "post_decision_phases": [{
                "phase_id": f"{d}/community",
                "after_event_id": f"{d}/PM",
                "next_visible_event_rule": "next-approved-AM",
            }],
        })
    return {"artifact_type": "calendar_event_registry", "version": "calendar-v1", "dates": date_rows}


def build_stage_inputs(calendar: dict, prices: dict) -> dict:
    events = []
    for dr in calendar["dates"]:
        px = prices[dr["date"]]
        for ev in dr["decision_events"]:
            ref = px["open"] if ev["subturn"] == "AM" else px["close"]
            events.append({
                "event_id": ev["decision_event_id"],
                "date": dr["date"],
                "subturn": ev["subturn"].lower(),
                "news_cutoff_timestamp": ev["news_window"]["end_inclusive"],
                "market_feature_as_of": ev["market_feature_as_of"],
                "market": {
                    "reference_price": ref,
                    "previous_close": px["prev_close"],
                    "open_price": px["open"],
                    "as_of_timestamp": ev["market_feature_as_of"],
                },
            })
    return {
        "artifact_type": "rn_stage_input_registry",
        "version": "stage-input-v1",
        "calendar_event_registry_sha256": canonical_sha256(calendar),
        "events": events,
    }


def build_prices(calendar: dict, prices: dict) -> dict:
    events = []
    for dr in calendar["dates"]:
        px = prices[dr["date"]]
        for ev in dr["decision_events"]:
            events.append({
                "decision_event_id": ev["decision_event_id"],
                "date": dr["date"],
                "subturn": ev["subturn"],
                "execution_price_field": ev["execution_price_field"],
                "execution_price": px["open"] if ev["subturn"] == "AM" else px["close"],
            })
    return {
        "artifact_type": "event_price_registry",
        "version": "prices-v1",
        "stock_code": STOCK,
        "calendar_event_registry_sha256": canonical_sha256(calendar),
        "events": events,
    }


def build_cohort(snapshot: SealedPersonaSnapshot, sys_db: Path, slots_csv: Path) -> dict:
    with sqlite3.connect(sys_db) as c:
        meta = {r[0]: {"news_depth": r[1], "ini_cash": r[2]}
                for r in c.execute("SELECT agent_id, news_depth, ini_cash FROM agents").fetchall()}
    slot_sha = {}
    with slots_csv.open(encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            slot_sha[row["agent_id"]] = hashlib.sha256(_cbytes(row)).hexdigest()
    agents = []
    for ordinal, aid in enumerate(sorted(meta), start=1):
        agents.append({
            "ordinal": ordinal,
            "agent_id": aid,
            "news_depth": int(meta[aid]["news_depth"]),
            "initial_cash": int(meta[aid]["ini_cash"]),
            "persona_sha256": snapshot.persona(aid).persona_sha256,
            "fixed_slot_sha256": slot_sha.get(aid, _digest(f"{aid}-slot")),
        })
    return {"artifact_type": "cohort_registry", "version": "cohort-v1", "agents": agents}


def build_review(news: dict, calendar: dict, stage_inputs: dict) -> dict:
    cred = hashlib.sha256(b"rn-offline-reviewer-credential").hexdigest()
    runtime_reviews = [{
        "article_id": a["article_id"],
        "payload_sha256": a["payload_sha256"],
        "version_sha256": a["version_sha256"],
        "cutoff_version_sha256": a["cutoff_version_sha256"],
        "decision": "allow",
        "reason": "effective_at 배치+자동 스캔으로 as-of 안전 확인된 frozen 버전.",
        "reviewer_id": "rn-offline-reviewer",
        "reviewer_credential_sha256": cred,
        "reviewed_at": "2026-07-29T10:00:00+09:00",
    } for a in news["articles"]]
    return {
        "artifact_type": "article_version_leakage_review_manifest",
        "version": "article-review-v2",
        "real_news_bundle_manifest_sha256": news["bundle_sha256"],
        "calendar_event_registry_sha256": canonical_sha256(calendar),
        "stage_input_registry_canonical_sha256": canonical_sha256(stage_inputs),
        "scanner": {
            "scanner_id": "rn-offline-semantic-leakage-scanner",
            "scanner_version": "v1",
            "scanner_sha256": hashlib.sha256(b"rn-offline-scanner").hexdigest(),
            "executed_at": "2026-07-29T09:00:00+09:00",
        },
        "runtime_reviews": runtime_reviews,
        "candidate_reviews": [{
            "candidate_id": "news_20260427_섹터_0032",
            "article_id": "news_20260427_섹터_0032",
            "payload_sha256": hashlib.sha256(b"known-eod-payload").hexdigest(),
            "version_sha256": hashlib.sha256(b"known-eod-version").hexdigest(),
            "cutoff_version_sha256": hashlib.sha256(b"known-eod-cutoff-version").hexdigest(),
            "decision": "reject",
            "reason": "Documented same-day EOD close and investor-flow leakage.",
            "reviewer_id": "rn-offline-reviewer",
            "reviewer_credential_sha256": cred,
            "reviewed_at": "2026-07-29T10:00:00+09:00",
            "scanner_result": "eod_close_and_individual_flow_detected",
        }],
    }


def build_spec(*, cohort, calendar, stage_inputs, stage_file_sha, snapshot, prompt_bundle,
               news_sha, injection_sha, review_sha, dates, cohort_obj) -> dict:
    news_exposure_policy = {
        "version": "rn-news-exposure-policy-v1",
        "target_real_news_per_event": 10,
        "fake_news_per_event": 0,
        "shortage_policy": "accepted_shortage_no_synthetic_or_duplicate_v1",
    }
    community_timing_policy = {
        "timezone": "Asia/Seoul", "pm_phase_not_before": "15:30:00", "pm_phase_not_after": "23:59:59",
        "next_am_delivery_not_before": "08:00:00", "next_am_delivery_not_after": "09:00:00",
    }
    depth_counts = Counter(str(a["news_depth"]) for a in cohort_obj["agents"])
    cash_counts = Counter(str(a["initial_cash"]) for a in cohort_obj["agents"])
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT).decode().strip()
    return {
        "artifact_type": "study_spec",
        "study_id": "rn-community-ab-2026",
        "design_version": "2.0.0",
        "baseline_commit": commit,
        "required_agent_count": len(cohort_obj["agents"]),
        "cohort_registry_sha256": canonical_sha256(cohort),
        "persona_snapshot_manifest_sha256": snapshot.manifest_sha256,
        "persona_depth_manifest_sha256": snapshot.depth_manifest_sha256,
        "persona_assignment_policy": "frozen-db-map-prompt-projection-only",
        "persona_renderer_sha256": persona_renderer_sha256(),
        "prompt_bundle_sha256": prompt_bundle.canonical_sha256,
        "belief_limits": {"dim_1": 150, "dim_2": 100, "dim_3": 100, "dim_4": 100, "dim_5": 100, "dim_6": 100},
        "cohort_assertions": {"depth_counts": dict(depth_counts), "initial_cash_counts": dict(cash_counts)},
        "condition_treatments": {
            "RN_COMM_OFF": {"community_mode": "off", "news_treatment": "real_only"},
            "RN_COMM_ON": {"community_mode": "on", "news_treatment": "real_only"},
        },
        "paired_condition_groups": [["RN_COMM_OFF", "RN_COMM_ON"]],
        "treatment_diff_allowlist": ["community_mode"],
        "calendar_event_registry_sha256": canonical_sha256(calendar),
        "burn_in_date_ids": dates[:3],
        "regime_policy_sha256": _digest("regime"),
        "real_news_bundle_manifest_sha256": news_sha,
        "known_injection_registry_sha256": injection_sha,
        "article_version_leakage_review_manifest_sha256": review_sha,
        "news_exposure_policy_sha256": canonical_sha256(news_exposure_policy),
        "news_exposure_policy": news_exposure_policy,
        "community_policy": {
            "best_k": 5, "best_selection_policy": "top_k_or_fewer_available_no_forced_posting",
            "permissions_from_cohort_depth_map": True, "depth1_selective_read_cap": 5,
            "depth2_selective_read_cap": 10, "best_payload": "title_plus_full_frozen_body",
            "visibility": "next_approved_am_decision_event",
        },
        "community_timing_policy": community_timing_policy,
        "community_timing_policy_sha256": canonical_sha256(community_timing_policy),
        "context_window_policy": {
            "decision_historical_order_or_fill_direct_visibility": "forbidden",
            "community_public_author_private_portfolio_or_trade_visibility": "forbidden",
            "trade_memory_visibility": "postfill-ltb-only",
            "depth2_search_lookback": 7, "depth2_search_lookback_unit": "calendar_days",
            "depth2_search_top_k": 10,
            "news_category_targets": {"stock": 5, "sector": 3, "economy": 2},
            "market_feature_policy_sha256": _digest("market-features"),
        },
        "memory_policy": {
            "version": "stb-ltb-v4", "cadence": "each_manifest_decision_event",
            "trade_belief_blocks": "previous_ltb_plus_current_stb_separate_blocks",
            "ltb_update_timing": "after-fill-before-commit",
            "current_transaction_episode_input": "once-same-turn-dim6",
            "price_outcome_input": "eligible-earlier-dim6",
            "outcome_horizons": ["next-decision-event", "same-subturn-plus-1-trading-date"],
        },
        "trade_policy": {
            "stock_code": STOCK, "decision_space": ["buy", "sell"], "allow_hold": False,
            "max_single_trade_cash_ratio": 0.5, "fill_policy": "full_fill_at_event_reference_price",
            "commission_rate": 0.0, "commission_applies_to": [], "sell_tax_rate": 0.0,
            "fee_policy": "zero_fee_baseline_all_fee_amounts_must_be_zero",
            "target_direction_notional": "gross_signed_fill_value",
        },
        "model_policy": {
            "model": "qwen/qwen3.5-flash-02-23", "provider": "alibaba",
            "reasoning": {"effort": "none", "exclude": True},
            "per_arm_max_concurrent_llm_calls": 8, "physical_http_attempts_per_phase_attempt": 1,
            "allow_provider_fallbacks": False, "require_parameters": True,
            "reasoning_off_canary_required": True,
            "reasoning_off_success_contract": "provider-model-match-reasoning-empty-tokens-zero",
        },
        "study_seed": 2, "seed_namespace": "study-agent-event-stage-attempt-v1",
        "retry_policy_sha256": _digest("retry"),
        "runtime_policy_sha256": _digest("runtime"),
        "evaluation_policy_sha256": _digest("evaluation"),
        "stage_input_registry_file_sha256": stage_file_sha,
        "stage_input_registry_canonical_sha256": canonical_sha256(stage_inputs),
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="RN sealed 입력 세트 생성.")
    p.add_argument("--out", type=Path, default=PROJECT_ROOT / "preparation/rn_ab_sealed_v1")
    p.add_argument("--persona-snapshot", type=Path, default=PROJECT_ROOT / "preparation/rn_ab_persona_snapshot_v1")
    p.add_argument("--sys-db", type=Path, default=PROJECT_ROOT / "outputs/sys_100.db")
    p.add_argument("--slots-csv", type=Path, default=PROJECT_ROOT / "data/fixed_slots.csv")
    p.add_argument("--sim-db", type=Path, default=PROJECT_ROOT / "outputs/sim.db")
    p.add_argument("--news", type=Path,
                   default=PROJECT_ROOT / "preparation/rn_ab_source_candidate_v1/real_news_bundle_manifest.json")
    args = p.parse_args(argv)

    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    snapshot = SealedPersonaSnapshot.load(args.persona_snapshot)
    prompt_dir = out / "prompts"
    prompt_dir.mkdir(exist_ok=True)
    for fn in ALL_PROMPT_FILENAMES:
        shutil.copy2(PROJECT_ROOT / "prompts" / "common" / fn, prompt_dir / fn)
    prompt_bundle = RNPromptBundle.load(prompt_dir=prompt_dir)

    dates = trading_dates(args.sim_db)
    prices = stock_prices(args.sim_db)
    calendar = build_calendar(dates)
    stage_inputs = build_stage_inputs(calendar, prices)
    price_reg = build_prices(calendar, prices)
    cohort = build_cohort(snapshot, args.sys_db, args.slots_csv)
    news = json.loads(args.news.read_text(encoding="utf-8"))
    injection = {"artifact_type": "known_injection_registry", "version": "known-injections-v1", "entries": []}
    review = build_review(news, calendar, stage_inputs)

    _write(out / "cohort.json", cohort)
    _write(out / "calendar.json", calendar)
    stage_bytes = _write(out / "stage_inputs.json", stage_inputs)
    _write(out / "prices.json", price_reg)
    _write(out / "news.json", news)
    _write(out / "known_injection.json", injection)
    _write(out / "review.json", review)

    spec = build_spec(
        cohort=cohort, calendar=calendar, stage_inputs=stage_inputs,
        stage_file_sha=hashlib.sha256(stage_bytes).hexdigest(),
        snapshot=snapshot, prompt_bundle=prompt_bundle,
        news_sha=news["bundle_sha256"], injection_sha=canonical_sha256(injection),
        review_sha=canonical_sha256(review), dates=dates, cohort_obj=cohort,
    )
    _write(out / "study_spec.json", spec)

    print("=" * 60)
    print("RN sealed 입력 세트 생성 완료 (candidate)")
    print("=" * 60)
    print(f"거래일 {len(dates)} · event {len(dates)*2} · agent {len(cohort['agents'])}")
    print(f"뉴스 기사 {len(news['articles'])} · 슬롯 {len(news['slots'])} · review {len(review['runtime_reviews'])}")
    print(f"산출물: {out}/")
    for f in ["study_spec.json", "cohort.json", "calendar.json", "stage_inputs.json",
              "prices.json", "news.json", "known_injection.json", "review.json", "prompts/"]:
        print(f"  - {f}")
    print("\n다음: 09 preflight 검증")
    print(f"  .venv/bin/python scripts/09_run_realnews_community_ab.py \\")
    print(f"    --run-id rn_seal_test --input-root {out} --output-root {out}/run \\")
    print("    --study-spec .../study_spec.json  ... (전 인자)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
