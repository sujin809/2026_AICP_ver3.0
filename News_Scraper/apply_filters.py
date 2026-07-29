#!/usr/bin/env python3
"""§5-3 풀 정제 필터를 적용해 최종 뉴스 DB 를 만든다.

원본 ``merged_news.jsonl`` 은 **건드리지 않는다**.  제외 사유를 각 기사에 기록한
정제본을 새 파일로 쓰고, 제외된 기사도 감사용으로 따로 남긴다.

적용 순서:
1. 본문 150자 미만 제외 (결정론적)
2. 관련성 필터 ``drop`` 제외 (반도체·거시경제 섹션 소싱분만 대상)

사용 예::

    python News_Scraper/apply_filters.py \
        --in outputs/crawl/merged_news.jsonl \
        --relevance outputs/crawl/relevance.jsonl \
        --out outputs/crawl/filtered_news.jsonl \
        --excluded outputs/crawl/excluded_news.jsonl
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

SECTION_BUCKETS = ("반도체", "거시경제")
# 대표 버킷 우선순위 후보 — 어느 쪽을 쓸지 판단할 수 있게 둘 다 리포트한다
PRIORITIES = {
    "현행(삼성 우선)": ("삼성전자", "반도체", "거시경제"),
    "섹션 우선": ("반도체", "거시경제", "삼성전자"),
}
# 결정 턴당 버킷 쿼터 (§5-2)
QUOTA = {"삼성전자": 10, "반도체": 6, "거시경제": 4}


def _dist(counter: Counter) -> dict:
    values = sorted(counter.values())
    if not values:
        return {"days": 0}
    return {
        "days": len(values),
        "min": values[0],
        "median": statistics.median(values),
        "mean": round(sum(values) / len(values), 1),
        "max": values[-1],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--in", dest="src", type=Path, required=True)
    parser.add_argument("--relevance", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--excluded", type=Path, default=None)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--min-body", type=int, default=150)
    args = parser.parse_args()

    rows = [json.loads(l) for l in args.src.open(encoding="utf-8") if l.strip()]

    verdicts: dict[str, dict] = {}
    rel_errors = 0
    for line in args.relevance.open(encoding="utf-8"):
        if not line.strip():
            continue
        rec = json.loads(line)
        if "error" in rec:
            rel_errors += 1
            continue
        verdicts[rec["url"]] = rec

    kept: list[dict] = []
    excluded: list[dict] = []
    unjudged = 0
    reasons = Counter()

    for row in rows:
        url = row.get("url")
        is_section = any(b in SECTION_BUCKETS for b in row.get("buckets", []))
        body_len = len(row.get("body") or "")

        if body_len < args.min_body:
            reasons["본문 150자 미만"] += 1
            excluded.append({**row, "exclude_reason": "short_body", "body_chars": body_len})
            continue

        if is_section:
            verdict = verdicts.get(url)
            if verdict is None:
                # 판정 누락(API 오류 등) → 보수적으로 유지하되 집계
                unjudged += 1
                row = {**row, "relevance": "keep", "relevance_reason": "판정 누락(보수적 유지)"}
            elif verdict["decision"] == "drop":
                reasons["삼성전자 무관"] += 1
                excluded.append({**row, "exclude_reason": "irrelevant", "relevance_reason": verdict.get("reason")})
                continue
            else:
                row = {**row, "relevance": "keep", "relevance_reason": verdict.get("reason")}
        else:
            row = {**row, "relevance": "keep", "relevance_reason": "삼성전자 키워드 소싱(판정 면제)"}

        kept.append(row)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as handle:
        for row in kept:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    if args.excluded:
        with args.excluded.open("w", encoding="utf-8") as handle:
            for row in excluded:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"원본 {len(rows)}건 → 최종 {len(kept)}건 (제외 {len(excluded)}건)")
    for reason, count in reasons.most_common():
        print(f"  - {reason}: {count}건")
    if rel_errors:
        print(f"  ! 관련성 판정 API 오류 {rel_errors}건")
    if unjudged:
        print(f"  ! 판정 누락 {unjudged}건 → 보수적으로 유지")

    # ---- 대표 버킷 우선순위별 일별 가용량 (쿼터 충족 가능성) ----
    report = {"total_in": len(rows), "total_out": len(kept), "excluded": dict(reasons)}
    for label, priority in PRIORITIES.items():
        per_day: dict[str, Counter] = defaultdict(Counter)
        totals = Counter()
        for row in kept:
            names = row.get("buckets", [])
            bucket = next((b for b in priority if b in names), (sorted(names) or ["기타"])[0])
            totals[bucket] += 1
            per_day[bucket][row["date"]] += 1
        print(f"\n[{label}] 버킷 총량: {dict(totals)}")
        section = {}
        for bucket, quota in QUOTA.items():
            counter = per_day.get(bucket, Counter())
            dist = _dist(counter)
            # 하루 2턴 → 일별 필요량 = quota*2
            need = quota * 2
            short = sum(1 for v in counter.values() if v < need)
            print(f"  {bucket:5s} 일별 {dist} · 하루 필요 {need}건 → 미달일 {short}/{dist.get('days', 0)}일")
            section[bucket] = {**dist, "need_per_day": need, "short_days": short}
        report[label] = {"totals": dict(totals), "per_day": section}

    if args.report:
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n리포트: {args.report}")
    print(f"최종 DB: {args.out}")
    if args.excluded:
        print(f"제외분(감사용): {args.excluded}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
