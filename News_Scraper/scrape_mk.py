from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


SEARCH_URL = "https://www.mk.co.kr/search"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko",
}


@dataclass
class SearchItem:
    index: int
    article_id: str | None
    category: str | None
    title: str
    url: str
    listed_at: str | None


@dataclass
class Article:
    index: int
    article_id: str | None
    category: str | None
    title: str
    url: str
    listed_at: str | None
    published_at: str | None
    published_date: str | None
    published_time: str | None
    body: str
    # Optional (schema.org ``dateModified``); defaulted so the playwright
    # list-page scrapers that reuse this dataclass keep working unchanged.
    modified_at: str | None = None
    modified_date: str | None = None
    modified_time: str | None = None


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_input_time(text: str | None) -> tuple[str | None, str | None]:
    if not text:
        return None, None
    match = re.search(r"(\d{4})\.(\d{2})\.(\d{2})\s+(\d{2}:\d{2})", text)
    if match:
        year, month, day, time = match.groups()
        return f"{year}-{month}-{day}", time

    match = re.search(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}:\d{2})", text)
    if match:
        year, month, day, time = match.groups()
        return f"{year}-{month}-{day}", time

    return None, None


def get_json_ld_published_at(soup: BeautifulSoup) -> str | None:
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text("", strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            if isinstance(node, dict) and node.get("datePublished"):
                return str(node["datePublished"])
    return None


def get_json_ld_modified_at(soup: BeautifulSoup) -> str | None:
    """Return schema.org ``dateModified`` (매일경제 '수정' time) when present.

    News content is often edited after first publication (e.g. an EOD price or
    final investor-flow figure added after the market close).  A study that
    gates news by its final edited version must record this, not only the
    original ``datePublished``.
    """
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text("", strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            if isinstance(node, dict) and node.get("dateModified"):
                return str(node["dateModified"])
    return None


def extract_published_at(soup: BeautifulSoup) -> str | None:
    for selector in ("time", "li.lasttime"):
        node = soup.select_one(selector)
        if node:
            text = clean_text(node.get_text(" ", strip=True))
            if text:
                return text

    return get_json_ld_published_at(soup)


def extract_body(soup: BeautifulSoup) -> str:
    paragraphs = [
        clean_text(p.get_text(" ", strip=True))
        for p in soup.select("div.news_cnt_detail_wrap p")
        if clean_text(p.get_text(" ", strip=True))
    ]
    if paragraphs:
        return "\n".join(paragraphs)

    for selector in ("div.news_cnt_detail_wrap", "#article_body", ".article_body"):
        body_node = soup.select_one(selector)
        if body_node:
            body = clean_text(body_node.get_text("\n", strip=True))
            body = re.sub(r"^사진 확대\s*", "", body)
            return body

    description = soup.select_one('meta[property="og:description"]')
    if description and description.get("content"):
        return clean_text(description["content"])
    return ""


def get_soup(session: requests.Session, url: str, *, params: dict | None = None) -> BeautifulSoup:
    response = session.get(url, params=params, timeout=30)
    response.raise_for_status()
    return BeautifulSoup(response.text, "lxml")


def search_news(
    session: requests.Session,
    *,
    word: str,
    start_date: str,
    end_date: str,
    sort: str = "accuracy",
    search_field: str = "title",
    news_type: str = "all",
) -> tuple[list[SearchItem], int | None]:
    params = {
        "word": word,
        "sort": sort,
        "dateType": "direct",
        "startDate": start_date,
        "endDate": end_date,
        "searchField": search_field,
        "newsType": news_type,
    }
    soup = get_soup(session, SEARCH_URL, params=params)

    total = None
    total_input = soup.select_one("#api_243")
    if total_input and total_input.get("data-total", "").isdigit():
        total = int(total_input["data-total"])

    items: list[SearchItem] = []
    for index, node in enumerate(soup.select("#list_area li.news_node a.news_item"), start=1):
        title_node = node.select_one(".news_ttl")
        time_node = node.select_one(".time_info")
        category_node = node.select_one(".cate")
        url = urljoin(SEARCH_URL, node.get("href", ""))
        items.append(
            SearchItem(
                index=index,
                article_id=node.get("data-id"),
                category=clean_text(category_node.get_text(" ", strip=True)) if category_node else None,
                title=clean_text(title_node.get_text(" ", strip=True)) if title_node else "",
                url=url,
                listed_at=clean_text(time_node.get_text(" ", strip=True)) if time_node else None,
            )
        )

    return items, total


def parse_article(session: requests.Session, item: SearchItem) -> Article:
    soup = get_soup(session, item.url)

    og_title = soup.select_one('meta[property="og:title"]')
    title = item.title
    if og_title and og_title.get("content"):
        title = clean_text(og_title["content"].removesuffix("- 매일경제"))

    published_at = extract_published_at(soup)
    published_date, published_time = parse_input_time(published_at)
    modified_at = get_json_ld_modified_at(soup)
    modified_date, modified_time = parse_input_time(modified_at)
    body = extract_body(soup)

    return Article(
        index=item.index,
        article_id=item.article_id,
        category=item.category,
        title=title,
        url=item.url,
        listed_at=item.listed_at,
        published_at=published_at,
        published_date=published_date,
        published_time=published_time,
        modified_at=modified_at,
        modified_date=modified_date,
        modified_time=modified_time,
        body=body,
    )


def scrape(
    *,
    word: str,
    start_date: str,
    end_date: str,
    sort: str,
    search_field: str,
    news_type: str,
) -> tuple[list[Article], int | None]:
    with requests.Session() as session:
        session.headers.update(HEADERS)
        items, total = search_news(
            session,
            word=word,
            start_date=start_date,
            end_date=end_date,
            sort=sort,
            search_field=search_field,
            news_type=news_type,
        )
        articles = [parse_article(session, item) for item in items]
        return articles, total


def print_summary(articles: Iterable[Article], total: int | None) -> None:
    if total is not None:
        print(f"검색 결과 total: {total}")
    for article in articles:
        print(f"\n[{article.index:02d}] {article.title}")
        print(f"URL: {article.url}")
        print(f"목록 시간: {article.listed_at}")
        print(f"기사 시간: {article.published_at}")
        print(f"본문 글자 수: {len(article.body)}")
        print(f"본문 미리보기: {article.body[:180]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="매일경제 검색 결과와 기사 본문을 수집합니다.")
    parser.add_argument("--word", default="삼성전자")
    parser.add_argument("--start-date", default="2026-03-11")
    parser.add_argument("--end-date", default="2026-03-12")
    parser.add_argument("--sort", default="accuracy", choices=["accuracy", "desc", "asc"])
    parser.add_argument("--search-field", default="title", choices=["title", "body", "all"])
    parser.add_argument("--news-type", default="all")
    parser.add_argument("--json", default="mk_articles.json", help="저장할 JSON 파일 경로")
    args = parser.parse_args()

    articles, total = scrape(
        word=args.word,
        start_date=args.start_date,
        end_date=args.end_date,
        sort=args.sort,
        search_field=args.search_field,
        news_type=args.news_type,
    )

    output = {
        "scraped_at": datetime.now().isoformat(timespec="seconds"),
        "query": {
            "word": args.word,
            "startDate": args.start_date,
            "endDate": args.end_date,
            "sort": args.sort,
            "searchField": args.search_field,
            "newsType": args.news_type,
        },
        "total": total,
        "count": len(articles),
        "articles": [asdict(article) for article in articles],
    }
    with open(args.json, "w", encoding="utf-8") as file:
        json.dump(output, file, ensure_ascii=False, indent=2)

    print_summary(articles, total)
    print(f"\nJSON 저장: {args.json}")


if __name__ == "__main__":
    main()
