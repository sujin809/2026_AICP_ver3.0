#!/usr/bin/env python3
"""서브에이전트가 만든 요약을 기계적으로 검증한다.

에이전트의 자기보고를 신뢰하지 않고 원문과 직접 대조한다. 길이 규격과,
요약에 등장하는 수치가 실제로 원문(제목+본문)에 있는지를 확인한다.
수치 대조는 할루시네이션을 전부 잡아내지는 못하지만, 가장 흔하고
가장 해로운 형태인 '없는 숫자 지어내기'를 잡는다.

    python News_Scraper/verify_summaries.py \
        --batch-dir outputs/worklots --summaries outputs/crawl/summaries.jsonl
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

MIN_CHARS = 150
MAX_CHARS = 200  # 미만
NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
BANNED = ("매수", "매도 추천", "투자 권유", "사야", "팔아야")


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]


def _normalize_number(value: str) -> str:
    return value.replace(",", "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-dir", type=Path, required=True)
    parser.add_argument("--summaries", type=Path, required=True)
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args(argv)

    source: dict[str, dict] = {}
    for path in sorted(args.batch_dir.glob("summary_*.json")):
        for row in json.loads(path.read_text(encoding="utf-8")):
            source[str(row["url"])] = row

    problems: list[dict] = []
    stats = Counter()
    for row in _read_jsonl(args.summaries):
        url = str(row.get("url") or "")
        text = str(row.get("summary") or "").strip()
        origin = source.get(url)
        stats["요약 건수"] += 1
        if origin is None:
            problems.append({"url": url, "issue": "배치에 없는 URL"})
            continue
        if not (MIN_CHARS <= len(text) < MAX_CHARS):
            problems.append({"url": url, "issue": f"길이 {len(text)}자", "summary": text})
        haystack = f"{origin.get('title', '')} {origin.get('body', '')}"
        haystack_nums = {_normalize_number(n) for n in NUMBER.findall(haystack)}
        invented = sorted(
            {
                n
                for n in (_normalize_number(v) for v in NUMBER.findall(text))
                if n not in haystack_nums
            }
        )
        if invented:
            problems.append({"url": url, "issue": f"본문에 없는 수치 {invented}", "summary": text})
        hit = [word for word in BANNED if word in text]
        if hit:
            problems.append({"url": url, "issue": f"금지 표현 {hit}", "summary": text})

    missing = sorted(set(source) - {str(r.get("url")) for r in _read_jsonl(args.summaries)})
    stats["원문 건수"] = len(source)
    stats["요약 누락"] = len(missing)
    stats["문제 건수"] = len(problems)

    for key, value in stats.items():
        print(f"{key}: {value}")
    for problem in problems[:20]:
        print(f"  - {problem['url']}: {problem['issue']}")
    if len(problems) > 20:
        print(f"  … 외 {len(problems) - 20}건")

    if args.report:
        args.report.write_text(
            json.dumps(
                {"stats": dict(stats), "problems": problems, "missing_urls": missing},
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        print(f"보고서: {args.report}")
    return 1 if problems or missing else 0


if __name__ == "__main__":
    sys.exit(main())
