#!/usr/bin/env python3
"""매일경제 뉴스 크롤러 — 섹션 / 키워드검색 두 모드.

각 기사의 **전문(body)** 과 schema.org `datePublished` / `dateModified` 를 함께 수집한다.
노출 기준 시각은 ``effective = max(published, modified)`` 이며, 이 값과
``gap_hours = modified - published`` 를 레코드에 함께 남긴다(필터는 후속 단계).

목록 페이징은 Cloudflare + JS(`_CP` AJAX) 때문에 Playwright(실제 브라우저)가 필요하고,
개별 기사 페이지는 requests 로 접근한다.  봇 감지를 피하려면 아래가 필수다(실측):
브라우저 컨텍스트에 user-agent/viewport/locale 지정 + 로드/클릭 사이 딜레이.

사용 예::

    # 섹션 (반도체·전자)
    python News_Scraper/mk_crawler.py section \
        --section-url https://www.mk.co.kr/news/business/semiconductors-electronics \
        --start 2026-02-01 --end 2026-06-30 --out outputs/crawl/semiconductor.jsonl

    # 키워드 검색 (삼성전자 버킷)
    python News_Scraper/mk_crawler.py search \
        --word 삼성전자 --word 삼전 --word 갤럭시 \
        --start 2026-02-01 --end 2026-06-30 --out outputs/crawl/samsung.jsonl
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scrape_mk import extract_body, get_json_ld_modified_at, get_json_ld_published_at

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
ARTICLE_RE = re.compile(r"/news/[a-z\-]+/\d{6,}$")


def _iso(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def _new_context(pw, headless: bool):
    browser = pw.chromium.launch(headless=headless)
    context = browser.new_context(
        user_agent=UA, viewport={"width": 1400, "height": 900}, locale="ko-KR"
    )
    return browser, context


def _click_more(page, *, delay_click: float, delay_between: float) -> bool:
    """Click the '더보기' button once; return False when no more items load."""
    btn = page.query_selector("[data-btn-more]")
    if not btn or not btn.is_visible():
        return False
    before = page.eval_on_selector_all("#list_area [data-li]", "e=>e.length")
    try:
        btn.scroll_into_view_if_needed()
        time.sleep(delay_click)
        btn.click(timeout=8000)
        page.wait_for_function(
            f"document.querySelectorAll('#list_area [data-li]').length > {before}",
            timeout=15000,
        )
    except Exception:
        return False
    time.sleep(delay_between)
    return True


def _list_hrefs(page) -> list[str]:
    hrefs = page.eval_on_selector_all(
        "#list_area [data-li] a[href]", "els=>[...new Set(els.map(e=>e.href))]"
    )
    return [h for h in hrefs if ARTICLE_RE.search(h)]


def fetch_article(session: requests.Session, url: str) -> dict:
    """Fetch one article page: full body + published/modified provenance."""
    response = session.get(url, timeout=20)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    published = get_json_ld_published_at(soup)
    modified = get_json_ld_modified_at(soup)
    og = soup.select_one('meta[property="og:title"]')
    title = og.get("content", "").strip() if og else ""
    body = extract_body(soup)
    dp, dm = _iso(published), _iso(modified)
    gap = (dm - dp).total_seconds() / 3600.0 if (dp and dm and dm > dp) else 0.0
    effective = max([d for d in (dp, dm) if d], default=dp)
    return {
        "url": url,
        "title": title,
        "body": body,
        "published_at": published,
        "modified_at": modified,
        "effective_at": effective.isoformat() if effective else None,
        "date": published[:10] if published else None,
        "time": published[11:16] if published and len(published) >= 16 else None,
        "gap_hours": round(gap, 2),
        "keep_12h": gap < 12.0,
        "source": "mk",
        "scraped_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def _oldest_date(session: requests.Session, hrefs: list[str]) -> str | None:
    """Probe the last (oldest) listed article for its published date."""
    for url in reversed(hrefs[-3:]):
        try:
            published = get_json_ld_published_at(
                BeautifulSoup(session.get(url, timeout=15).text, "html.parser")
            )
            if published:
                return published[:10]
        except Exception:
            continue
    return None


def collect_section(args, session) -> list[str]:
    """Page a section back until articles predate --start (no date param exists)."""
    with sync_playwright() as pw:
        browser, ctx = _new_context(pw, args.headless)
        page = ctx.new_page()
        page.goto(args.section_url, timeout=45000, wait_until="domcontentloaded")
        page.wait_for_selector("#list_area [data-li]", timeout=25000)
        time.sleep(args.delay_load)
        for index in range(args.max_clicks):
            if not _click_more(
                page, delay_click=args.delay_click, delay_between=args.delay_between
            ):
                print("  더보기 종료(버튼 없음/로드 실패)")
                break
            if (index + 1) % args.probe_every == 0:
                oldest = _oldest_date(session, _list_hrefs(page))
                print(f"  클릭 {index+1}회 · 목록 {len(_list_hrefs(page))}건 · 최하단 {oldest}")
                if oldest and oldest < args.start:
                    print("  시작일 이전 도달 → 페이징 종료")
                    break
        hrefs = _list_hrefs(page)
        ctx.close()
        browser.close()
    return hrefs


def collect_search(args, session) -> list[str]:
    """Search each keyword with mk's own date range, then page the results."""
    found: list[str] = []
    with sync_playwright() as pw:
        browser, ctx = _new_context(pw, args.headless)
        page = ctx.new_page()
        for word in args.word:
            url = (
                f"https://www.mk.co.kr/search/news?word={quote(word)}"
                f"&searchField={args.search_field}&startDate={args.start}"
                f"&endDate={args.end}&sort=desc"
            )
            page.goto(url, timeout=45000, wait_until="domcontentloaded")
            try:
                page.wait_for_selector("#list_area [data-li]", timeout=20000)
            except Exception:
                print(f"  '{word}': 결과 없음")
                continue
            time.sleep(args.delay_load)
            for _ in range(args.max_clicks):
                if not _click_more(
                    page, delay_click=args.delay_click, delay_between=args.delay_between
                ):
                    break
            hrefs = _list_hrefs(page)
            print(f"  '{word}': {len(hrefs)}건")
            found.extend(hrefs)
            time.sleep(args.delay_between)
        ctx.close()
        browser.close()
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="mode", required=True)

    def common(sp):
        sp.add_argument("--start", required=True, help="수집 시작일 YYYY-MM-DD")
        sp.add_argument("--end", required=True, help="수집 종료일 YYYY-MM-DD")
        sp.add_argument("--out", type=Path, required=True, help="결과 JSONL 경로")
        sp.add_argument("--max-clicks", type=int, default=400)
        sp.add_argument("--probe-every", type=int, default=10)
        sp.add_argument("--delay-load", type=float, default=3.0)
        sp.add_argument("--delay-click", type=float, default=0.8)
        sp.add_argument("--delay-between", type=float, default=1.5)
        sp.add_argument("--delay-article", type=float, default=0.3)
        sp.add_argument("--headless", action="store_true", default=True)

    sec = sub.add_parser("section", help="섹션 브라우징 크롤")
    sec.add_argument("--section-url", required=True)
    common(sec)

    sea = sub.add_parser("search", help="키워드 검색 크롤")
    sea.add_argument("--word", action="append", required=True, help="키워드(반복 지정 가능)")
    sea.add_argument("--search-field", default="all", choices=["all", "title"])
    common(sea)

    args = parser.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)

    # 재개: 이미 수집한 URL 은 건너뛴다
    done: set[str] = set()
    if args.out.exists():
        with args.out.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    done.add(json.loads(line)["url"])
                except Exception:
                    continue
        print(f"재개: 기존 {len(done)}건 스킵")

    session = requests.Session()
    session.headers.update({"User-Agent": UA})

    print(f"[1/2] 목록 수집 ({args.mode}) …")
    hrefs = collect_section(args, session) if args.mode == "section" else collect_search(args, session)
    hrefs = [h for h in dict.fromkeys(hrefs) if h not in done]
    print(f"  신규 URL {len(hrefs)}건")

    print("[2/2] 기사 본문/시각 수집 …")
    ok = fail = inrange = 0
    with args.out.open("a", encoding="utf-8") as handle:
        for i, url in enumerate(hrefs, start=1):
            try:
                record = fetch_article(session, url)
                ok += 1
                if record["date"] and args.start <= record["date"] <= args.end:
                    inrange += 1
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            except Exception as exc:  # noqa: BLE001
                fail += 1
                handle.write(
                    json.dumps({"url": url, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)
                    + "\n"
                )
            if i % 50 == 0:
                handle.flush()
                print(f"  … {i}/{len(hrefs)} (ok={ok}, fail={fail}, 구간내={inrange})")
            time.sleep(args.delay_article)

    print(f"완료: ok={ok}, fail={fail}, 구간({args.start}~{args.end})내={inrange}")
    print(f"저장: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
