#!/usr/bin/env python3
"""크롤 결과 병합 · URL 중복제거 · 버킷 배정 · 통계 리포트.

`mk_crawler.py` 가 버킷별로 남긴 JSONL 을 하나로 합치면서:

* **URL 기준 중복제거** — 같은 기사가 여러 버킷/섹션에 잡히는 경우가 있다
  (예: 홍해 기사가 경제정책+무역, 삼성 반도체 기사가 반도체+삼성전자).
* **소속 버킷 전체 보존** (`buckets`) + **대표 버킷 1개 배정** (`bucket`).
  대표 버킷 우선순위는 구체적인 것부터: 삼성전자 > 반도체 > 거시경제.
  전체 소속을 남겨두므로 우선순위를 바꿔도 **재크롤 없이 재배정**할 수 있다.
* 구간 필터 + 추출 실패(본문/날짜 없음) 분리 + `keep_12h` 집계.

사용 예::

    python News_Scraper/merge_crawl.py \
        --crawl-dir outputs/crawl --start 2026-02-01 --end 2026-06-30 \
        --out outputs/crawl/merged_news.jsonl \
        --report outputs/crawl/merge_report.json
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

# 파일명 → 버킷 (대표 버킷 우선순위 순서대로 정의)
BUCKET_PRIORITY = ("삼성전자", "반도체", "거시경제")
FILE_BUCKET = {
    "samsung": "삼성전자",
    "semiconductor": "반도체",
    "macro": "거시경제",
}


def _bucket_of(path: Path) -> tuple[str, str]:
    """Return (bucket, section-label) inferred from the crawl file name."""
    stem = path.stem
    for prefix, bucket in FILE_BUCKET.items():
        if stem.startswith(prefix):
            section = stem[len(prefix) :].lstrip("_") or stem
            return bucket, section
    return "기타", stem


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--crawl-dir", type=Path, default=Path("outputs/crawl"))
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    files = sorted(p for p in args.crawl_dir.glob("*.jsonl") if p.name != args.out.name)
    if not files:
        parser.exit(2, f"크롤 JSONL 을 찾을 수 없습니다: {args.crawl_dir}\n")

    merged: dict[str, dict] = {}
    buckets_of: dict[str, set[str]] = defaultdict(set)
    sections_of: dict[str, set[str]] = defaultdict(set)
    raw_rows = fetch_errors = 0
    per_file: dict[str, int] = {}

    for path in files:
        bucket, section = _bucket_of(path)
        count = 0
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                raw_rows += 1
                count += 1
                url = row.get("url")
                if not url:
                    continue
                if "error" in row:
                    fetch_errors += 1
                    continue
                buckets_of[url].add(bucket)
                sections_of[url].add(section)
                # 같은 URL 이면 본문이 더 온전한 레코드를 남긴다
                prev = merged.get(url)
                if prev is None or len(row.get("body") or "") > len(prev.get("body") or ""):
                    merged[url] = row
        per_file[path.name] = count

    # 대표 버킷 배정 + 구간 필터
    in_range: list[dict] = []
    no_date = no_body = 0
    for url, row in merged.items():
        names = buckets_of[url]
        row["buckets"] = sorted(names)
        row["sections"] = sorted(sections_of[url])
        row["bucket"] = next((b for b in BUCKET_PRIORITY if b in names), sorted(names)[0])
        if not row.get("date"):
            no_date += 1
            continue
        if not (row.get("body") or "").strip():
            no_body += 1
            continue
        if args.start <= row["date"] <= args.end:
            in_range.append(row)

    in_range.sort(key=lambda r: (r["date"], r.get("time") or "", r["url"]))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as handle:
        for row in in_range:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    # ---- 통계 ----
    by_bucket = Counter(r["bucket"] for r in in_range)
    overlap = Counter(tuple(r["buckets"]) for r in in_range if len(r["buckets"]) > 1)
    keep12 = sum(1 for r in in_range if r.get("keep_12h"))
    with_mod = sum(1 for r in in_range if r.get("modified_at"))
    per_day = Counter(r["date"] for r in in_range)
    per_day_bucket: dict[str, Counter] = defaultdict(Counter)
    for r in in_range:
        per_day_bucket[r["bucket"]][r["date"]] += 1

    def _dist(counter: Counter) -> dict:
        values = sorted(counter.values())
        if not values:
            return {"days": 0}
        return {
            "days": len(values),
            "min": values[0],
            "median": statistics.median(values),
            "max": values[-1],
            "mean": round(sum(values) / len(values), 1),
        }

    report = {
        "files": per_file,
        "raw_rows": raw_rows,
        "fetch_errors": fetch_errors,
        "unique_urls": len(merged),
        "dropped_no_date": no_date,
        "dropped_no_body": no_body,
        "in_range": len(in_range),
        "range": [args.start, args.end],
        "by_bucket": dict(by_bucket),
        "overlap_pairs": {" + ".join(k): v for k, v in overlap.most_common()},
        "overlap_total": sum(overlap.values()),
        "keep_12h": keep12,
        "dropped_12h": len(in_range) - keep12,
        "with_modified_at": with_mod,
        "per_day_all": _dist(per_day),
        "per_day_by_bucket": {b: _dist(c) for b, c in per_day_bucket.items()},
    }

    print(f"원본 행 {raw_rows} · fetch 실패 {fetch_errors} · 고유 URL {len(merged)}")
    print(f"제외: 날짜없음 {no_date}, 본문없음 {no_body}")
    print(f"구간({args.start}~{args.end}) 내 최종 {len(in_range)}건 → {args.out}")
    print(f"\n버킷별(대표): {dict(by_bucket)}")
    if overlap:
        print(f"버킷 중복 {sum(overlap.values())}건: {dict(list(overlap.most_common())[:5])}")
    print(f"12h 통과 {keep12} / 제외 {len(in_range)-keep12} · dateModified 보유 {with_mod}")
    print(f"\n일별 전체: {_dist(per_day)}")
    for bucket, counter in per_day_bucket.items():
        print(f"  {bucket}: {_dist(counter)}")

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n리포트: {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
