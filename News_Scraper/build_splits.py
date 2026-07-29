#!/usr/bin/env python3
"""merged_news.jsonl -> 서브에이전트 작업 배치 -> *_split/*.json 조립.

네트워크·유료 API를 호출하지 않는다. 관련성 판정과 요약은 오케스트레이터가
서브에이전트로 수행하고, 그 결과 JSONL을 이 스크립트가 다시 받아 조립한다.

    # 1) 결정론적 필터 적용 후 배치 파일 생성
    python News_Scraper/build_splits.py prepare \
        --merged outputs/crawl/merged_news.jsonl \
        --work-dir outputs/worklots --batch-size 30

    # 2) 판정/요약 JSONL을 받아 split JSON 조립
    python News_Scraper/build_splits.py assemble \
        --merged outputs/crawl/merged_news.jsonl \
        --relevance outputs/crawl/relevance.jsonl \
        --summaries outputs/crawl/summaries.jsonl \
        --out-root outputs
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

# 사용자 확정 규격
MIN_BODY_CHARS = 150          # 본문 150자 미만은 제외 (원래 의도)
SUMMARY_MIN_CHARS = 150
SUMMARY_MAX_CHARS = 200       # 미만
WINDOW_START = "2026-02-26"   # AM 윈도우가 전 거래일을 보므로 하루 앞
WINDOW_END = "2026-05-04"
ARTICLES_PER_SPLIT_FILE = 20

# merge_crawl.py의 대표 버킷 -> split 폴더. news_agent.FOLDER_SECTOR와 짝을 이룬다.
BUCKET_FOLDER = {
    "삼성전자": "samsung_split",
    "반도체": "semiconductor_split",
    "거시경제": "macro_economic-policy_split",
}
# 관련성 판정 대상 버킷 (삼성전자 버킷은 키워드 검색이라 정의상 관련 있음 -> 면제)
JUDGED_BUCKETS = ("반도체", "거시경제")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{line_number} JSON 파싱 실패: {exc}") from exc
    return rows


def _effective_date(row: dict[str, Any]) -> str:
    """노출 기준 시각 = effective_at = max(발행, 수정). 사용자 확정."""
    return str(row.get("effective_at") or row.get("date") or "")[:10]


def _effective_stamp(row: dict[str, Any]) -> str:
    raw = str(row.get("effective_at") or "")
    if len(raw) >= 16:
        return raw[:16].replace("T", " ")
    return f"{_effective_date(row)} {str(row.get('time') or '00:00')[:5]}"


def eligible_rows(merged: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], Counter]:
    """결정론적 필터만 적용한다. 판단이 필요한 필터는 서브에이전트 몫."""
    kept: list[dict[str, Any]] = []
    dropped: Counter = Counter()
    for row in merged:
        day = _effective_date(row)
        if not (WINDOW_START <= day <= WINDOW_END):
            dropped["기간 밖"] += 1
            continue
        if len(str(row.get("body") or "")) < MIN_BODY_CHARS:
            dropped[f"본문 {MIN_BODY_CHARS}자 미만"] += 1
            continue
        if row.get("keep_12h") is False:
            dropped["수정-발행 12시간 초과"] += 1
            continue
        if not str(row.get("title") or "").strip():
            dropped["제목 없음"] += 1
            continue
        kept.append(row)
    return kept, dropped


def cmd_prepare(args: argparse.Namespace) -> int:
    merged = _read_jsonl(args.merged)
    rows, dropped = eligible_rows(merged)

    done_urls: set[str] = set()
    for path in (args.relevance, args.summaries):
        if path and path.exists():
            done_urls |= {str(r.get("url")) for r in _read_jsonl(path) if r.get("url")}

    judge_todo = [
        r for r in rows
        if any(b in JUDGED_BUCKETS for b in r.get("buckets", []))
        and r["url"] not in done_urls
    ]
    summary_todo = [r for r in rows if r["url"] not in done_urls]

    args.work_dir.mkdir(parents=True, exist_ok=True)
    written = []
    # 판정은 관련 있는 주제인지만 보면 되므로 발췌로 충분하다(filter_relevance.py도
    # 1200자 발췌를 썼다). 요약은 본문 전체를 근거로 삼아야 하므로 자르지 않는다.
    plan = (
        ("judge", judge_todo, args.judge_batch_size, args.judge_body_chars),
        ("summary", summary_todo, args.summary_batch_size, 0),
    )
    for label, todo, batch_size, body_limit in plan:
        if args.only and label != args.only:
            continue
        for index in range(0, len(todo), batch_size):
            chunk = todo[index : index + batch_size]
            path = args.work_dir / f"{label}_{index // batch_size:04d}.json"
            payload = [
                {
                    "url": r["url"],
                    "title": str(r.get("title") or ""),
                    "body": (
                        str(r.get("body") or "")[:body_limit]
                        if body_limit
                        else str(r.get("body") or "")
                    ),
                    "bucket": r.get("bucket"),
                    "date": _effective_date(r),
                }
                for r in chunk
            ]
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            written.append(path)

    print(f"merged {len(merged)}건 -> 대상 {len(rows)}건")
    for reason, count in dropped.most_common():
        print(f"  제외 {reason}: {count}")
    print(f"버킷(대표): {dict(Counter(r.get('bucket') for r in rows))}")
    print(f"이미 처리됨(스킵): {len(done_urls)}건")
    print(f"관련성 판정 배치: {sum(1 for p in written if p.name.startswith('judge'))}개 · {len(judge_todo)}건")
    print(f"요약 배치: {sum(1 for p in written if p.name.startswith('summary'))}개 · {len(summary_todo)}건")
    print(f"배치 디렉터리: {args.work_dir}")
    return 0


def cmd_assemble(args: argparse.Namespace) -> int:
    merged = _read_jsonl(args.merged)
    rows, _ = eligible_rows(merged)

    verdict = {str(r["url"]): str(r.get("decision") or "") for r in _read_jsonl(args.relevance)}
    summary = {str(r["url"]): str(r.get("summary") or "") for r in _read_jsonl(args.summaries)}

    by_folder: dict[str, list[dict[str, Any]]] = defaultdict(list)
    missing = Counter()
    length_warn = 0
    for row in rows:
        url = str(row["url"])
        text = summary.get(url, "").strip()
        if not text:
            missing["요약 없음"] += 1
            continue
        if not (SUMMARY_MIN_CHARS <= len(text) < SUMMARY_MAX_CHARS):
            length_warn += 1
        buckets = row.get("buckets", [])
        if any(b in JUDGED_BUCKETS for b in buckets):
            decision = verdict.get(url)
            if decision is None:
                missing["관련성 판정 없음"] += 1
                continue
            keep_flag = "Y" if decision == "keep" else "N"
        else:
            # 삼성전자 버킷은 판정 면제. split 스키마의 극성이 반대라 정상=N이다.
            keep_flag = "N"
        folder = BUCKET_FOLDER.get(str(row.get("bucket")))
        if folder is None:
            missing["버킷 미상"] += 1
            continue
        by_folder[folder].append(
            {
                "제목": str(row.get("title") or ""),
                "작성시각": _effective_stamp(row),
                "본문": str(row.get("body") or ""),
                "요약": text,
                "필터링 여부": keep_flag,
            }
        )

    for folder, articles in sorted(by_folder.items()):
        target = args.out_root / folder
        target.mkdir(parents=True, exist_ok=True)
        for stale in target.glob("*.json"):
            stale.unlink()
        articles.sort(key=lambda a: (a["작성시각"], a["제목"]))
        for index in range(0, len(articles), ARTICLES_PER_SPLIT_FILE):
            chunk = articles[index : index + ARTICLES_PER_SPLIT_FILE]
            name = f"{index // ARTICLES_PER_SPLIT_FILE + 1:03d}.json"
            (target / name).write_text(
                json.dumps(chunk, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        print(f"{folder}: {len(articles)}건 -> {target}")

    for reason, count in missing.most_common():
        print(f"  누락 {reason}: {count}")
    if length_warn:
        print(f"  경고: 요약 길이가 {SUMMARY_MIN_CHARS}~{SUMMARY_MAX_CHARS}자 범위 밖 {length_warn}건")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="mode", required=True)

    prep = sub.add_parser("prepare", help="결정론적 필터 후 서브에이전트 배치 생성")
    prep.add_argument("--merged", type=Path, required=True)
    prep.add_argument("--work-dir", type=Path, default=Path("outputs/worklots"))
    prep.add_argument("--judge-batch-size", type=int, default=100)
    prep.add_argument("--judge-body-chars", type=int, default=1200)
    prep.add_argument("--summary-batch-size", type=int, default=40)
    prep.add_argument("--only", choices=("judge", "summary"), default=None)
    prep.add_argument("--relevance", type=Path, default=Path("outputs/crawl/relevance.jsonl"))
    prep.add_argument("--summaries", type=Path, default=None)
    prep.set_defaults(func=cmd_prepare)

    asm = sub.add_parser("assemble", help="판정/요약 결과로 split JSON 생성")
    asm.add_argument("--merged", type=Path, required=True)
    asm.add_argument("--relevance", type=Path, required=True)
    asm.add_argument("--summaries", type=Path, required=True)
    asm.add_argument("--out-root", type=Path, default=Path("outputs"))
    asm.set_defaults(func=cmd_assemble)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
