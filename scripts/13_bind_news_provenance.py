#!/usr/bin/env python3
"""RN 뉴스 provenance 바인딩 (safe-subset candidate builder).

합의된 노출 규칙: 기사의 노출 시각 = effective_at = max(published_at, modified_at).
기사는 effective_at 슬롯에 배치되므로, 우리가 effective_at 이후에 관측한 본문은
그 시점 기준 as-of 안전하다(수정본이라도 수정시각에 배치되어 누출이 아니다).

이 스크립트는 네트워크/LLM을 접촉하지 않고, 소스 DB/CSV를 변경하지 않는다.
산출물은 execution_authorized=false candidate이며, 별도 승인·봉인 단계에서
calendar/stage-input/target/price registry + StudySpec과 함께 sealed 된다.

입력 join:
  - split JSON 5폴더  : 제목·본문·요약·작성시각(effective_at)·필터링여부(N만)
  - rescrape CSV       : article_id·url·published_at·modified_at·source (제목으로 join)
  - crawl *.jsonl      : scraped_at → observed_at (url로 join)

바인딩 조건(정직): 본문 존재 + observed_at 존재 + observed_at >= effective_at.
그 외는 quarantine(사유 기록). 부족 event는 shortage로 수용(부족 허용 정책).
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import pickle
import sys
from collections import Counter, defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from twinmarket_kr.rn_ab.news import (  # noqa: E402
    NewsBundleError,
    _reject_synthetic_marker,
    _scan_text,
    article_payload_sha256,
    canonical_sha256,
)

FOLDER_SECTOR = {
    "samsung_split": "종목",
    "semiconductor_split": "섹터",
    "macro_economic-policy_split": "경제",
    "macro_business-index_split": "경제",
    "macro_trade_split": "경제",
}
SECTOR_PRIORITY = {"종목": 0, "섹터": 1, "경제": 2}
CATEGORY_TARGETS = {"종목": 5, "섹터": 3, "경제": 2}  # 턴당, 합 10
START_DATE = "2026-02-27"
END_DATE = "2026-05-04"  # study window 종료일(현재 manifest 예시)


def _norm_ts(s: str | None) -> str | None:
    s = (s or "").strip()
    if not s:
        return None
    return s.replace(" ", "T").replace("Z", "+00:00")


def _event_of(effective_at: str) -> tuple[str, str]:
    """effective_at → (date, subturn). AM = ~08:59 이하, PM = 그 이후."""
    date = effective_at[:10]
    hhmm = effective_at[11:16]
    return date, ("AM" if hhmm <= "08:59" else "PM")


def load_curated_summaries(splits_dir: Path) -> dict[str, dict]:
    """필터링 N 기사의 큐레이션 요약. key=제목, 섹터 우선순위로 dedup."""
    by_title: dict[str, dict] = {}
    for folder, sector in FOLDER_SECTOR.items():
        fpath = splits_dir / folder
        if not fpath.exists():
            continue
        for jf in sorted(fpath.glob("*.json"), key=lambda x: int(x.stem)):
            for art in json.loads(jf.read_text(encoding="utf-8")):
                if not isinstance(art, dict) or str(art.get("필터링 여부", "N")) != "N":
                    continue
                title = str(art.get("제목", "")).strip()
                summary = str(art.get("요약", "")).strip()
                if not title or not summary:
                    continue
                cur = by_title.get(title)
                if cur is not None and SECTOR_PRIORITY[cur["category"]] <= SECTOR_PRIORITY[sector]:
                    continue
                by_title[title] = {"summary": summary, "category": sector}
    return by_title


def _file_category(path: str) -> str:
    if "macro" in path:
        return "경제"
    if "semiconductor" in path or "semi" in path:
        return "섹터"
    return "종목"


def load_crawl(crawl_dir: Path) -> dict[str, dict]:
    """제목 → 재크롤 provenance(+본문). effective_at 최신이 이기도록 dedup."""
    by_title: dict[str, dict] = {}
    for fp in glob.glob(str(crawl_dir / "*.jsonl")):
        cat = _file_category(fp)
        with open(fp, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                title = str(r.get("title", "")).strip()
                if not title or not r.get("url"):
                    continue
                entry = {
                    "url": r["url"].strip(),
                    "body": str(r.get("body", "")).strip(),
                    "published_at": _norm_ts(r.get("published_at")),
                    "modified_at": _norm_ts(r.get("modified_at")),
                    "effective_at": _norm_ts(r.get("effective_at")),
                    "observed_at": _norm_ts(r.get("scraped_at")),
                    "source": str(r.get("source", "")).strip(),
                    "file_category": cat,
                }
                cur = by_title.get(title)
                if cur is None or (entry["effective_at"] or "") > (cur["effective_at"] or ""):
                    by_title[title] = entry
    return by_title


def build(splits_dir: Path, crawl_dir: Path, out_dir: Path) -> None:
    curated = load_curated_summaries(splits_dir)
    crawl = load_crawl(crawl_dir)

    bound: list[dict] = []
    quarantine: list[dict] = []
    per_event: dict[tuple[str, str], Counter] = defaultdict(Counter)
    stats = Counter()

    for title, c in curated.items():
        # 봉인 anti-fake 가드: 제목/요약에 가짜·합성 마커가 있으면 봉인 불가 → 격리.
        # (fake 주입과 구분 불가하므로 실제 '가짜뉴스' 다룬 기사도 제외한다.)
        try:
            _reject_synthetic_marker(title, field="title")
            _reject_synthetic_marker(c["summary"], field="summary")
        except NewsBundleError:
            stats["fake_marker_in_text"] += 1
            quarantine.append({"title": title, "reason": "fake_marker_in_text"})
            continue
        # EOD/target 누출 가드: 요약/제목에 당일 종가·장 마감·개인 순매수 등이 있으면
        # agent-visible 텍스트가 미래/타깃을 누출 → 봉인 불가 → 격리.
        try:
            _scan_text(title, field="title")
            _scan_text(c["summary"], field="summary")
        except NewsBundleError:
            stats["eod_leakage_in_text"] += 1
            quarantine.append({"title": title, "reason": "eod_leakage_in_text"})
            continue
        prov = crawl.get(title)
        if prov is None:
            stats["no_crawl_provenance"] += 1
            quarantine.append({"title": title, "reason": "no_crawl_provenance"})
            continue
        effective_at = prov["effective_at"]
        body = prov["body"]
        if not effective_at or not (START_DATE <= effective_at[:10] <= END_DATE):
            stats["out_of_window"] += 1
            continue
        if not body:
            stats["no_body"] += 1
            quarantine.append({"title": title, "reason": "no_body"})
            continue
        # 방법론 확정(2026-07-29): observed_at = effective_at(=max(published,modified)).
        # 기사를 effective_at 윈도우 event에 배치하므로 cutoff >= effective_at 이고,
        # validate_delivery의 published/modified/observed <= cutoff 세 검증이 모두 통과한다.
        # (실제 7월 crawl scraped_at은 as-of 관측시각으로 쓰지 않는다.)
        obs = effective_at

        published_at = prov["published_at"] or effective_at
        # 크롤 데이터 모순(modified < published) 방어: 수정시각을 무효 처리.
        modified_at = prov["modified_at"]
        if modified_at and modified_at[:19] < published_at[:19]:
            modified_at = None
        # 섹터(버킷)는 split 수집 폴더 기준(권위); 그 외 file_category 폴백.
        category = c["category"]
        # 안정적 article_id: 날짜_섹터_제목해시8.
        aid = f"news_{effective_at[:10].replace('-', '')}_{category}_{canonical_sha256(title)[:8]}"

        # effective_at 배치 규칙 하에서 현재 본문 = as-of effective 버전.
        raw_body_sha256 = canonical_sha256(body)
        version_sha256 = canonical_sha256(
            {"body": body, "published_at": published_at, "modified_at": modified_at}
        )
        cutoff_version_sha256 = canonical_sha256({"body": body, "as_of": effective_at})

        payload_fields = {
            "article_id": aid,
            "title": title,
            "summary": c["summary"],
            "published_at": published_at,
            "observed_at": obs,
            "last_modified_at": modified_at,
            "source_url": prov["url"],
            "source": prov["source"],
            "raw_body_sha256": raw_body_sha256,
            "version_sha256": version_sha256,
            "cutoff_version_sha256": cutoff_version_sha256,
        }
        article = {"payload_sha256": article_payload_sha256(payload_fields), **payload_fields}
        bound.append(article)
        per_event[_event_of(effective_at)][category] += 1
        stats["bound"] += 1

    # 커버리지 (5/3/2 대비, 부족 허용)
    coverage = []
    for ev in sorted(per_event):
        bc = per_event[ev]
        got = sum(min(CATEGORY_TARGETS[c], bc.get(c, 0)) for c in CATEGORY_TARGETS)
        coverage.append(
            {"date": ev[0], "subturn": ev[1], "delivered": got, "by_bucket": dict(bc), "short": got < 10}
        )
    short_events = [c for c in coverage if c["short"]]

    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "artifact_type": "rn_news_provenance_bound_candidate",
        "version": "rn-news-provenance-bind-v1",
        "execution_authorized": False,
        "run_eligible": False,
        "exposure_rule": "effective_at = max(published_at, modified_at); body is as-of effective_at",
        "start_date": START_DATE,
        "target_real_news_per_event": 10,
        "category_targets": CATEGORY_TARGETS,
        "counts": {
            "bound": len(bound),
            "quarantined": len(quarantine),
            "events_with_articles": len(per_event),
            "short_events": len(short_events),
            **dict(stats),
        },
        "candidate_sha256": None,
    }
    manifest["candidate_sha256"] = canonical_sha256(
        {"manifest": {k: v for k, v in manifest.items() if k != "candidate_sha256"},
         "articles": [a["payload_sha256"] for a in bound]}
    )

    (out_dir / "provenance_bound_articles.json").write_text(
        json.dumps({"manifest": manifest, "articles": bound}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "quarantine_report.json").write_text(
        json.dumps({"count": len(quarantine), "entries": quarantine}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "coverage_report.json").write_text(
        json.dumps({"per_event": coverage, "short_events": short_events}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("=" * 60)
    print("RN 뉴스 provenance 바인딩 (candidate, 미승인)")
    print("=" * 60)
    print(f"바인딩됨          : {len(bound)}")
    print(f"격리(quarantine)  : {len(quarantine)}  {dict(stats)}")
    print(f"기사 보유 event   : {len(per_event)}/90")
    print(f"10개 미만 event   : {len(short_events)} (부족 허용 정책)")
    print(f"산출물            : {out_dir}/")
    print("  - provenance_bound_articles.json / quarantine_report.json / coverage_report.json")
    print("주의: execution_authorized=false. 승인·봉인 단계에서 calendar/stage-input/")
    print("      target/price registry + StudySpec과 함께 real_news_bundle로 sealed 필요.")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="RN 뉴스 provenance 바인딩 (crawl provenance + 큐레이션 요약).")
    p.add_argument("--splits-dir", type=Path, default=PROJECT_ROOT / "outputs")
    p.add_argument("--crawl-dir", type=Path, default=PROJECT_ROOT / "outputs/crawl")
    p.add_argument("--out-dir", type=Path,
                   default=PROJECT_ROOT / "preparation/rn_ab_source_candidate_v1/provenance_bound")
    args = p.parse_args(argv)
    build(args.splits_dir, args.crawl_dir, args.out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
