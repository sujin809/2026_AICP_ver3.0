from __future__ import annotations

import csv
import pickle
import random
import re
import sqlite3
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable

import config


STOCK_KEYWORDS = ("삼성전자", "005930", "갤럭시", "DS부문", "파운드리", "HBM", "메모리", "반도체")
SECTOR_KEYWORDS = ("반도체", "HBM", "메모리", "파운드리", "AI 반도체", "2나노", "장비", "낸드", "DRAM")
ECONOMY_KEYWORDS = ("금리", "환율", "수출", "물가", "경기", "정책", "원달러", "외국인", "코스피", "미국")
CATEGORY_TARGETS = {"종목": 5, "섹터": 3, "경제": 2}
DEFAULT_DEPTH2_FIELDS = (
    {"field": "HBM", "keywords": ["HBM", "메모리", "고대역폭"]},
    {"field": "파운드리", "keywords": ["파운드리", "2나노", "수주"]},
    {"field": "반도체 업황", "keywords": ["반도체", "업황", "수출", "장비"]},
    {"field": "거시 수급", "keywords": ["금리", "환율", "외국인", "코스피"]},
)
BAD_SUMMARY_MARKERS = {"사진확대", "사진 확대"}
EXCLUDED_TITLE_PATTERNS = (
    re.compile(r"^\s*\[?표\]?\s*외국\s*환율\s*고시표?\s*$"),
)
TRUE_TEXTS = {"1", "true", "yes", "y"}


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in TRUE_TEXTS


def _public_title_item(row: dict[str, Any], *, include_time: bool = False) -> dict[str, str]:
    item = {
        "id": str(row.get("id", "")),
        "title": str(row.get("title", "")),
        "date": str(row.get("date", "")),
        "type": str(row.get("category", "")),
    }
    if include_time:
        item["time"] = str(row.get("time", ""))
    return item


def _public_content_item(row: dict[str, Any]) -> dict[str, str]:
    return {
        "id": str(row.get("id", "")),
        "title": str(row.get("title", "")),
        "date": str(row.get("date", "")),
        "content": str(row.get("summary", "")),
        "type": str(row.get("category", "")),
    }


def _public_search_item(row: dict[str, Any], score: float) -> dict[str, Any]:
    return {
        "id": str(row.get("id", "")),
        "title": str(row.get("title", "")),
        "date": str(row.get("date", "")),
        "category": str(row.get("category", "")),
        "summary": str(row.get("search_summary") or row.get("summary", "")),
        "relevance_score": score,
    }


def _select_daily(rows: list[dict[str, Any]], *, seed: int | None = None) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    selected: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for category, target in CATEGORY_TARGETS.items():
        pool = [row for row in rows if row["category"] == category and row["id"] not in used_ids]
        picks = rng.sample(pool, min(target, len(pool)))
        selected.extend(picks)
        used_ids.update(row["id"] for row in picks)
    if len(selected) < sum(CATEGORY_TARGETS.values()):
        remains = [row for row in rows if row["id"] not in used_ids]
        selected.extend(rng.sample(remains, min(sum(CATEGORY_TARGETS.values()) - len(selected), len(remains))))
    return selected


def _trading_dates_from_db(sim_db_path: Path | str = config.SIM_DB) -> list[str]:
    path = Path(sim_db_path)
    if not path.exists():
        return []
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT date FROM StockData WHERE stock_id = ? ORDER BY date",
            (config.STOCK_CODE,),
        ).fetchall()
    return [str(row[0]) for row in rows]


def _select_pre_close_cutoff_daily(
    processed: list[dict[str, Any]],
    *,
    daily_seed: int | None = None,
    sim_db_path: Path | str = config.SIM_DB,
) -> list[dict[str, Any]]:
    trading_dates = _trading_dates_from_db(sim_db_path)
    if len(trading_dates) < 2:
        return _select_by_calendar_day(processed, daily_seed=daily_seed)
    previous_by_date = {
        day: trading_dates[index - 1]
        for index, day in enumerate(trading_dates)
        if index > 0
    }
    selected: list[dict[str, Any]] = []
    for day_index, day in enumerate(trading_dates[1:]):
        previous_day = previous_by_date[day]
        windows = [
            ("am", previous_day, config.MARKET_CLOSE_TIME, day, "08:59"),
            ("pm", day, "08:59", day, config.MARKET_CLOSE_TIME),
        ]
        for slot_index, (slot, start_date, start_time, end_date, end_time) in enumerate(windows):
            candidates = [
                row
                for row in processed
                if _in_datetime_window(
                    row,
                    start_date=start_date,
                    start_time=start_time,
                    end_date=end_date,
                    end_time=end_time,
                )
            ]
            seed = None if daily_seed is None else daily_seed + day_index * 2 + slot_index
            selected.extend(_select_daily(candidates, seed=seed))
    selected.sort(key=lambda row: (row["date"], row["time"], row["id"]))
    return selected


def _select_by_calendar_day(processed: list[dict[str, Any]], *, daily_seed: int | None = None) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in processed:
        by_day[row["date"]].append(row)
    for day_index, (_, rows) in enumerate(sorted(by_day.items())):
        seed = None if daily_seed is None else daily_seed + day_index
        selected.extend(_select_daily(rows, seed=seed))
    return selected


def _parse_date(value: str) -> date:
    text = str(value).strip()[:10]
    return datetime.strptime(text, "%Y-%m-%d").date()


def _parse_time(value: str) -> time | None:
    text = str(value or "").strip()
    match = re.search(r"\d{2}:\d{2}", text)
    if not match:
        return None
    return datetime.strptime(match.group(0), "%H:%M").time()


def _combine_datetime(day: str, time_text: str) -> datetime | None:
    parsed_time = _parse_time(time_text)
    if parsed_time is None:
        return None
    return datetime.combine(_parse_date(day), parsed_time)


def _in_datetime_window(
    row: dict[str, Any],
    *,
    start_date: str,
    start_time: str,
    end_date: str,
    end_time: str,
    include_start: bool = False,
) -> bool:
    row_dt = _combine_datetime(str(row.get("date", "")), str(row.get("time", "")))
    start_dt = _combine_datetime(start_date, start_time)
    end_dt = _combine_datetime(end_date, end_time)
    if row_dt is None or start_dt is None or end_dt is None:
        return False
    if include_start:
        return start_dt <= row_dt <= end_dt
    return start_dt < row_dt <= end_dt


def _normalize_category(raw: str | None, title: str, summary: str) -> str:
    text = f"{raw or ''} {title} {summary}"
    if raw in {"종목", "stock"}:
        return "종목"
    if raw in {"섹터", "산업", "industry", "sector"}:
        return "섹터"
    if raw in {"경제", "economy", "macro"}:
        return "경제"
    if any(keyword in text for keyword in STOCK_KEYWORDS[:4]):
        return "종목"
    if any(keyword in text for keyword in SECTOR_KEYWORDS):
        return "섹터"
    return "경제"


def _summarize(text: str, limit: int = 220) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _clean_raw_summary(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if re.sub(r"\s+", "", text) in BAD_SUMMARY_MARKERS:
        return ""
    text = re.sub(r"\s*사진\s*확대\s*", " ", text).strip()
    text = re.sub(r"\s+", " ", text).strip()
    if re.sub(r"\s+", "", text) in BAD_SUMMARY_MARKERS:
        return ""
    return text


def _is_excluded_title(title: str) -> bool:
    return any(pattern.search(title) for pattern in EXCLUDED_TITLE_PATTERNS)


def _importance(title: str, summary: str, category: str, time_text: str) -> float:
    text = f"{title} {summary}"
    score = 0.0
    score += sum(text.count(keyword) for keyword in STOCK_KEYWORDS) * 3
    score += sum(text.count(keyword) for keyword in SECTOR_KEYWORDS) * 2
    score += sum(text.count(keyword) for keyword in ECONOMY_KEYWORDS)
    score += {"종목": 2.0, "섹터": 1.0, "경제": 0.5}.get(category, 0)
    if any(token in title for token in ("속보", "급등", "급락", "최대", "실적", "수주")):
        score += 2
    if time_text:
        try:
            hour = int(time_text[:2])
            score += max(0, 16 - hour) / 20
        except ValueError:
            pass
    return score


def _raw_records(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return [dict(item) for item in raw if isinstance(item, dict)]
    if isinstance(raw, dict):
        for key in ("records", "news", "data", "items"):
            if isinstance(raw.get(key), list):
                return [dict(item) for item in raw[key] if isinstance(item, dict)]
        return [dict(value) for value in raw.values() if isinstance(value, dict)]
    if hasattr(raw, "to_dict") and hasattr(raw, "columns"):
        return [dict(item) for item in raw.to_dict("records")]
    raise TypeError(f"unsupported raw news format: {type(raw)!r}")


def prepare_news(
    raw_pkl_path: Path | str = config.SAMSUNG_NEWS_RAW_PKL,
    processed_csv_path: Path | str = config.PROCESSED_NEWS_CSV,
    daily_csv_path: Path | str = config.DAILY_NEWS_SELECTION_CSV,
    *,
    daily_seed: int | None = None,
    sources: "Iterable[str] | None" = None,
) -> tuple[int, int]:
    raw_path = Path(raw_pkl_path)
    if not raw_path.exists():
        raise FileNotFoundError(f"raw news pkl not found: {raw_path}")
    with raw_path.open("rb") as f:
        raw = pickle.load(f)

    # Restrict the source pool (e.g. ``{"mk"}``) so a study can pin a single
    # publisher.  ``None`` keeps every source for backward compatibility.
    allowed_sources = (
        {str(s).strip().lower() for s in sources if str(s).strip()}
        if sources is not None
        else None
    )

    seen: set[tuple[str, str]] = set()
    processed: list[dict[str, Any]] = []
    per_day_counter: dict[str, int] = defaultdict(int)
    for item in _raw_records(raw):
        if allowed_sources is not None:
            if str(item.get("source") or "").strip().lower() not in allowed_sources:
                continue
        title = str(item.get("title") or item.get("headline") or "").strip()
        if not title or _is_excluded_title(title):
            continue
        date_text = str(item.get("date") or item.get("published_date") or item.get("datetime") or "")[:10]
        if not date_text:
            continue
        key = (date_text, title)
        if key in seen:
            continue
        seen.add(key)
        raw_time = str(item.get("time") or item.get("published_time") or item.get("datetime") or "")
        time_match = re.search(r"\d{2}:\d{2}", raw_time)
        time_text = time_match.group(0) if time_match else ""
        content = _clean_raw_summary(item.get("summary") or item.get("content") or "")
        if not content:
            continue
        summary = _summarize(str(content))
        category = _normalize_category(str(item.get("category") or item.get("type") or ""), title, summary)
        per_day_counter[date_text] += 1
        news_id = f"news_{date_text.replace('-', '')}_{category}_{per_day_counter[date_text]:04d}"
        processed.append(
            {
                "id": news_id,
                "title": title,
                "date": date_text,
                "time": time_text,
                "category": category,
                "summary": summary,
                "importance": _importance(title, summary, category, time_text),
            }
        )

    processed.sort(key=lambda row: (row["date"], -row["importance"], row["time"], row["id"]))
    processed_path = Path(processed_csv_path)
    daily_path = Path(daily_csv_path)
    processed_path.parent.mkdir(parents=True, exist_ok=True)
    with processed_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "title", "date", "time", "category", "summary"])
        writer.writeheader()
        for row in processed:
            writer.writerow({key: row[key] for key in writer.fieldnames or []})

    selected = _select_pre_close_cutoff_daily(processed, daily_seed=daily_seed)

    with daily_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "title", "date", "time", "category"])
        writer.writeheader()
        for row in selected:
            writer.writerow({key: row[key] for key in writer.fieldnames or []})
    return len(processed), len(selected)


class NewsAgent:
    def __init__(
        self,
        processed_csv_path: Path | str = config.PROCESSED_NEWS_CSV,
        daily_csv_path: Path | str = config.DAILY_NEWS_SELECTION_CSV,
        *,
        include_fake_news: bool = False,
    ) -> None:
        self.processed_csv_path = Path(processed_csv_path)
        self.daily_csv_path = Path(daily_csv_path)
        self.include_fake_news = include_fake_news
        self._processed = self._filter_fake_rows(self._load_csv(self.processed_csv_path))
        self._daily = self._filter_fake_rows(self._load_csv(self.daily_csv_path))
        self._by_id = {row["id"]: row for row in self._processed}
        self._by_title_all: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in self._processed:
            self._by_title_all[str(row["title"])].append(row)
        self._by_title = {
            title: sorted(rows, key=lambda item: str(item.get("id") or ""))[0]
            for title, rows in self._by_title_all.items()
        }

    def _filter_fake_rows(self, rows: list[dict[str, str]]) -> list[dict[str, str]]:
        if self.include_fake_news:
            return rows
        return [row for row in rows if not _truthy(row.get("is_fake"))]

    def get_daily_titles(self, target_date: str) -> list[dict[str, str]]:
        return [
            _public_title_item(row)
            for row in self._daily
            if row.get("date") == target_date
        ]

    def get_window_titles(
        self,
        *,
        start_date: str,
        start_time: str,
        end_date: str,
        end_time: str,
    ) -> list[dict[str, str]]:
        window_rows = [
            row
            for row in self._daily
            if _in_datetime_window(
                row,
                start_date=start_date,
                start_time=start_time,
                end_date=end_date,
                end_time=end_time,
            )
        ]
        return [
            _public_title_item(row, include_time=True)
            for row in self._limit_window_fake_rows(window_rows)
        ]

    def read_news(
        self,
        *,
        ids: list[str] | None = None,
        titles: list[str] | None = None,
        allowed_ids: set[str] | None = None,
        max_items: int | None = None,
    ) -> list[dict[str, str]]:
        rows = []
        for news_id in ids or []:
            row = self._by_id.get(news_id)
            if row:
                rows.append(row)
        for title in titles or []:
            row = self._by_title.get(title)
            if row:
                rows.append(row)
        deduped = []
        seen = set()
        for row in rows:
            if row["id"] in seen:
                continue
            if allowed_ids is not None and row["id"] not in allowed_ids:
                continue
            deduped.append(row)
            seen.add(row["id"])
            if max_items is not None and len(deduped) >= max_items:
                break
        return [_public_content_item(row) for row in deduped]

    def search_news(
        self,
        *,
        fields: list[dict[str, Any]],
        current_date: str,
        max_fields: int = 4,
        max_per_field: int = 5,
        lookback_days: int = 7,
    ) -> dict[str, list[dict[str, str]]]:
        end = _parse_date(current_date)
        start = end - timedelta(days=lookback_days - 1)
        candidates = [
            row for row in self._processed if start <= _parse_date(row["date"]) <= end
        ]
        result: dict[str, list[dict[str, str]]] = {}
        for field in fields[:max_fields]:
            field_name = str(field.get("field", "")).strip() or "unknown"
            keywords = [str(keyword).strip() for keyword in field.get("keywords", []) if str(keyword).strip()]
            scored = []
            for row in candidates:
                search_summary = row.get("search_summary") or row.get("summary", "")
                haystack = f"{row['title']} {search_summary}"
                score = sum(haystack.count(keyword) for keyword in keywords)
                if score > 0:
                    scored.append((score, row))
            scored.sort(key=lambda item: (-item[0], item[1]["date"], item[1]["title"]))
            result[field_name] = [
                _public_title_item(row)
                for _, row in scored[:max_per_field]
            ]
        return result

    def search_news_flat(
        self,
        *,
        keywords: list[str],
        current_date: str,
        as_of_time: str = "23:59",
        window_start_date: str | None = None,
        window_start_time: str | None = None,
        window_end_date: str | None = None,
        window_end_time: str | None = None,
        lookback_days: int = 7,
        top_n: int = 10,
    ) -> list[dict[str, Any]]:
        normalized_keywords = [str(keyword).strip() for keyword in keywords if str(keyword).strip()]
        if not normalized_keywords:
            return []
        # Depth 2 always searches the trailing lookback window as of the decision
        # cutoff. The feed's AM/PM window must not narrow this search to one subturn.
        end_dt = _combine_datetime(window_end_date or current_date, window_end_time or as_of_time)
        if end_dt is None:
            raise ValueError("Depth 2 search requires a valid as-of date and time")
        start_dt = end_dt - timedelta(days=lookback_days)
        candidates = []
        for row in self._processed:
            row_dt = _combine_datetime(str(row.get("date", "")), str(row.get("time", "")))
            if row_dt is not None and start_dt < row_dt <= end_dt:
                candidates.append(row)
        scored: list[tuple[float, dict[str, str]]] = []
        for row in candidates:
            search_summary = row.get("search_summary") or row.get("summary", "")
            haystack = f"{row['title']} {search_summary}"
            title_hits = sum(str(row["title"]).count(keyword) for keyword in normalized_keywords)
            body_hits = sum(str(search_summary).count(keyword) for keyword in normalized_keywords)
            score = title_hits * 2.0 + body_hits
            if score > 0:
                scored.append((score, row))
        scored.sort(key=lambda item: (-item[0], -_parse_date(item[1]["date"]).toordinal(), item[1]["title"]))
        return [_public_search_item(row, score) for score, row in scored[:top_n]]

    def build_base_context(self, target_date: str, news_depth: int = 1) -> dict[str, Any]:
        daily_titles = self.get_daily_titles(target_date)
        daily_read_max = 0 if news_depth <= 0 else self._daily_read_limit(daily_titles)
        return {
            "news_depth": news_depth,
            "daily_titles": daily_titles,
            "read_contents": [],
            "search_results": {},
            "search_read_contents": [],
            "limits": {
                "daily_read_max": daily_read_max,
                "search_fields_max": 0,
                "search_read_max": 10 if news_depth >= 2 else 0,
                "lookback_days": 7 if news_depth >= 2 else 0,
            },
        }

    def build_window_context(
        self,
        *,
        start_date: str,
        start_time: str,
        end_date: str,
        end_time: str,
        news_depth: int = 1,
    ) -> dict[str, Any]:
        daily_titles = self.get_window_titles(
            start_date=start_date,
            start_time=start_time,
            end_date=end_date,
            end_time=end_time,
        )
        daily_read_max = 0 if news_depth <= 0 else self._daily_read_limit(daily_titles)
        return {
            "news_depth": news_depth,
            "daily_titles": daily_titles,
            "read_contents": [],
            "search_results": {},
            "search_read_contents": [],
            "window": {
                "start_date": start_date,
                "start_time": start_time,
                "end_date": end_date,
                "end_time": end_time,
            },
            "limits": {
                "daily_read_max": daily_read_max,
                "search_fields_max": 0,
                "search_read_max": 10 if news_depth >= 2 else 0,
                "lookback_days": 0,
            },
        }

    def expand_context_from_selection(
        self,
        *,
        base_context: dict[str, Any],
        selected_news: list[Any] | None = None,
        current_date: str,
    ) -> dict[str, Any]:
        news_depth = 1 if base_context.get("news_depth") is None else int(base_context["news_depth"])
        daily_titles = base_context.get("daily_titles") or []
        allowed_daily_ids = {str(row.get("id")) for row in daily_titles if row.get("id")}
        if news_depth <= 0:
            read_contents: list[dict[str, str]] = []
        else:
            daily_read_max = int((base_context.get("limits") or {}).get("daily_read_max") or len(daily_titles) or 10)
            read_contents = self.read_news(
                ids=[str(row.get("id")) for row in daily_titles if row.get("id")],
                allowed_ids=allowed_daily_ids,
                max_items=daily_read_max,
            )

        expanded = dict(base_context)
        expanded["read_contents"] = read_contents
        expanded["visible_news_ids"] = [
            str(row.get("id")) for row in daily_titles if row.get("id")
        ]
        expanded["read_news_ids"] = [
            str(row.get("id")) for row in read_contents if row.get("id")
        ]
        expanded.setdefault("search_results", {})
        expanded.setdefault("search_read_contents", [])
        return expanded

    def _daily_read_limit(self, rows: list[dict[str, Any]]) -> int:
        if not self.include_fake_news:
            return min(10, len(rows))
        fake_count = sum(
            1
            for row in rows
            if _truthy((self._by_id.get(str(row.get("id") or "")) or {}).get("is_fake"))
        )
        return min(len(rows), 10 + fake_count)

    def _limit_window_fake_rows(self, rows: list[dict[str, str]]) -> list[dict[str, str]]:
        if not self.include_fake_news:
            return rows
        candidates: list[tuple[datetime, int, dict[str, str]]] = []
        for index, row in enumerate(rows):
            if not _truthy(row.get("is_fake")):
                continue
            fake_dt = _combine_datetime(row.get("date", ""), row.get("time", ""))
            if fake_dt is not None:
                candidates.append((fake_dt, index, row))
        if not candidates:
            return rows
        _, selected_index, selected_fake = max(candidates, key=lambda item: (item[0], item[1]))
        without_other_fakes = [
            row
            for index, row in enumerate(rows)
            if index == selected_index or not _truthy(row.get("is_fake"))
        ]
        return without_other_fakes

    def fake_audit_for_context(
        self,
        news_context: dict[str, Any],
        *,
        selected_news: list[Any] | None = None,
    ) -> dict[str, Any]:
        base_ids = self._ids_from_items(news_context.get("daily_titles") or [])
        read_ids = self._ids_from_items(news_context.get("read_contents") or [])
        search_ids = self._ids_from_items(news_context.get("search_read_contents") or [])
        selected_ids, selected_titles = self._normalize_selected_news(selected_news or [])
        selected_ids.extend(
            row["id"]
            for title in selected_titles
            for row in [self._by_title.get(title)]
            if row and row.get("id")
        )

        buckets = {
            "base": base_ids,
            "read": read_ids,
            "search": search_ids,
            "selected": selected_ids,
        }
        items_by_id: dict[str, dict[str, Any]] = {}
        sources_by_id: dict[str, set[str]] = defaultdict(set)
        for source, ids in buckets.items():
            for news_id in ids:
                row = self._by_id.get(news_id)
                if not row or not _truthy(row.get("is_fake")):
                    continue
                items_by_id[news_id] = row
                sources_by_id[news_id].add(source)

        items = [
            self._fake_audit_item(row, sorted(sources_by_id[news_id]))
            for news_id, row in sorted(items_by_id.items(), key=lambda item: item[0])
        ]
        fake_ids = [item["id"] for item in items]
        fake_base_count = self._fake_count(base_ids)
        fake_read_count = self._fake_count(read_ids)
        fake_search_count = self._fake_count(search_ids)
        fake_selected_count = self._fake_count(selected_ids)
        return {
            "fake_exposed": bool(items),
            # Explicit stages: visible/read are assignment and feed contact;
            # search/influential are post-exposure mechanisms.
            "fake_visible": fake_base_count > 0,
            "fake_read": fake_read_count > 0,
            "fake_searched": fake_search_count > 0,
            "fake_influential": fake_selected_count > 0,
            "fake_base_count": fake_base_count,
            "fake_read_count": fake_read_count,
            "fake_search_count": fake_search_count,
            "fake_selected_count": fake_selected_count,
            "fake_public_ids": fake_ids,
            "fake_synthetic_ids": [item.get("synthetic_id", "") for item in items if item.get("synthetic_id")],
            "fake_related_events": [item.get("related_event", "") for item in items if item.get("related_event")],
            "items": items,
        }

    def normalize_influential_news(
        self,
        selected_news: list[Any],
        news_context: dict[str, Any],
    ) -> tuple[list[dict[str, str]], list[str]]:
        """Resolve LLM influence references only against news actually available this turn."""
        allowed_ids = set(self._ids_from_items(news_context.get("daily_titles") or []))
        allowed_ids.update(self._ids_from_items(news_context.get("read_contents") or []))
        allowed_ids.update(self._ids_from_items(news_context.get("search_read_contents") or []))
        resolved: list[dict[str, str]] = []
        unresolved: list[str] = []
        seen: set[str] = set()
        for item in selected_news:
            raw_id = str(item.get("id") or "").strip() if isinstance(item, dict) else ""
            raw_title = (
                str(item.get("title") or "").strip()
                if isinstance(item, dict)
                else str(item).strip()
            )
            row = self._by_id.get(raw_id) if raw_id else None
            if row is None and raw_title:
                title_candidates = [
                    candidate
                    for candidate in self._by_title_all.get(raw_title, [])
                    if str(candidate.get("id") or "") in allowed_ids
                ]
                row = (
                    sorted(title_candidates, key=lambda item: str(item.get("id") or ""))[0]
                    if title_candidates
                    else None
                )
            if row is None or str(row.get("id") or "") not in allowed_ids:
                unresolved.append(raw_id or raw_title)
                continue
            news_id = str(row["id"])
            if news_id in seen:
                continue
            seen.add(news_id)
            resolved.append(_public_title_item(row, include_time=True))
        return resolved, unresolved

    @staticmethod
    def _normalize_selected_news(selected_news: list[Any]) -> tuple[list[str], list[str]]:
        ids: list[str] = []
        titles: list[str] = []
        for item in selected_news:
            if isinstance(item, dict):
                raw_id = item.get("id")
                raw_title = item.get("title")
                if raw_id:
                    ids.append(str(raw_id))
                elif raw_title:
                    titles.append(str(raw_title))
            else:
                text = str(item).strip()
                if not text:
                    continue
                if text.startswith("news_"):
                    ids.append(text)
                else:
                    titles.append(text)
        return list(dict.fromkeys(ids)), list(dict.fromkeys(titles))

    @staticmethod
    def _ids_from_items(items: Any) -> list[str]:
        result: list[str] = []
        if isinstance(items, dict):
            iterable = [entry for values in items.values() if isinstance(values, list) for entry in values]
        elif isinstance(items, list):
            iterable = items
        else:
            iterable = []
        for item in iterable:
            if isinstance(item, dict) and item.get("id"):
                result.append(str(item["id"]))
        return result

    def _fake_count(self, ids: list[str]) -> int:
        return sum(1 for news_id in ids if _truthy((self._by_id.get(news_id) or {}).get("is_fake")))

    @staticmethod
    def _fake_audit_item(row: dict[str, Any], sources: list[str]) -> dict[str, Any]:
        return {
            "id": row.get("id", ""),
            "synthetic_id": row.get("synthetic_id", ""),
            "title": row.get("title", ""),
            "date": row.get("date", ""),
            "time": row.get("time", ""),
            "category": row.get("category", ""),
            "sources": sources,
            "linked_event_id": row.get("linked_event_id", ""),
            "related_event": row.get("related_event", ""),
            "misinformation_type": row.get("misinformation_type", ""),
        }

    @staticmethod
    def _load_csv(path: Path) -> list[dict[str, str]]:
        if not path.exists():
            return []
        with path.open(encoding="utf-8-sig", newline="") as f:
            return [dict(row) for row in csv.DictReader(f)]
