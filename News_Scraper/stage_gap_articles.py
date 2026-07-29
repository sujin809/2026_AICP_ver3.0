#!/usr/bin/env python3
"""공백 기간에 재크롤한 기사를 split JSON 형식으로 적재한다.

요약과 관련성 판정은 아직 수행하지 않았으므로 두 필드를 빈 문자열로 남긴다.
빈 값은 "아직 채우지 않았다"는 뜻이며, ``load_news_from_json_splits`` 는 요약이
빈 기사를 노출 대상에서 제외하므로 이 상태로는 실험에 쓰이지 않는다.

기존 파일을 덮어쓰지 않도록 새 번호대(기본 101~)에 쓴다.

    python News_Scraper/stage_gap_articles.py \
        --crawl outputs/crawl/semiconductor_gap.jsonl \
        --dest outputs/semiconductor_split \
        --window-start 2026-02-26 --window-end 2026-03-17
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

MIN_BODY_CHARS = 100
ARTICLES_PER_FILE = 20


def _effective_stamp(row: dict) -> str:
    raw = str(row.get("effective_at") or "")
    if len(raw) >= 16:
        return raw[:16].replace("T", " ")
    return f"{str(row.get('date') or '')[:10]} {str(row.get('time') or '00:00')[:5]}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--crawl", type=Path, required=True)
    parser.add_argument("--dest", type=Path, required=True)
    parser.add_argument("--window-start", required=True)
    parser.add_argument("--window-end", required=True)
    parser.add_argument("--start-index", type=int, default=101)
    args = parser.parse_args(argv)

    seen: set[str] = set()
    rows: list[dict] = []
    dropped = {"기간 밖": 0, "본문 짧음": 0, "12h 초과": 0, "중복 URL": 0, "제목/본문 없음": 0}
    for line in args.crawl.open(encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        url = str(row.get("url") or "")
        stamp = _effective_stamp(row)
        day = stamp[:10]
        if url in seen:
            dropped["중복 URL"] += 1
            continue
        if not (args.window_start <= day <= args.window_end):
            dropped["기간 밖"] += 1
            continue
        title = str(row.get("title") or "").strip()
        body = str(row.get("body") or "")
        if not title or not body:
            dropped["제목/본문 없음"] += 1
            continue
        if len(body) < MIN_BODY_CHARS:
            dropped["본문 짧음"] += 1
            continue
        if row.get("keep_12h") is False:
            dropped["12h 초과"] += 1
            continue
        seen.add(url)
        rows.append(
            {
                "제목": title,
                "작성시각": stamp,
                "본문": body,
                "요약": "",           # 미생성
                "필터링 여부": "",     # 미판정
            }
        )

    rows.sort(key=lambda a: (a["작성시각"], a["제목"]))
    args.dest.mkdir(parents=True, exist_ok=True)
    written = []
    for offset in range(0, len(rows), ARTICLES_PER_FILE):
        chunk = rows[offset : offset + ARTICLES_PER_FILE]
        index = args.start_index + offset // ARTICLES_PER_FILE
        path = args.dest / f"{index:03d}.json"
        if path.exists():
            parser.exit(2, f"이미 존재하는 파일을 덮어쓰려 했습니다: {path}\n")
        path.write_text(json.dumps(chunk, ensure_ascii=False, indent=1), encoding="utf-8")
        written.append(path)

    print(f"{args.crawl.name} -> {args.dest}")
    print(f"  적재 {len(rows)}건 · 파일 {len(written)}개 ({written[0].name}~{written[-1].name})"
          if written else "  적재 0건")
    for reason, count in dropped.items():
        if count:
            print(f"  제외 {reason}: {count}")
    if rows:
        print(f"  날짜 {rows[0]['작성시각'][:10]} ~ {rows[-1]['작성시각'][:10]}")
        print("  요약/필터링 여부는 빈 문자열입니다 (미생성·미판정)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
