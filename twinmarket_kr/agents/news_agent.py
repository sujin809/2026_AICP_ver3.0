from __future__ import annotations

import bisect
import csv
import hashlib
import json
import pickle
import random
import re
import sqlite3
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable

import config


# LEGACY: 아래 세 키워드 묶음과 _normalize_category()/_importance()는 원천 pkl을
# CSV로 전처리하던 구 경로(prepare_news)에서만 쓴다. 현재 JSON split 경로는 기사
# 내용이 아니라 수집 폴더(FOLDER_SECTOR)로 버킷을 정하므로 이 값들을 참조하지 않는다.
# 후속 가짜뉴스 주입 실험이 CSV 경로를 다시 쓸 수 있어 삭제하지 않고 보존한다.
STOCK_KEYWORDS = ("삼성전자", "005930", "갤럭시", "DS부문", "파운드리", "HBM", "메모리", "반도체")
SECTOR_KEYWORDS = ("반도체", "HBM", "메모리", "파운드리", "AI 반도체", "2나노", "장비", "낸드", "DRAM")
ECONOMY_KEYWORDS = ("금리", "환율", "수출", "물가", "경기", "정책", "원달러", "외국인", "코스피", "미국")
# 버킷별 쿼터 (턴당): 삼성전자(종목) 5, 반도체(섹터) 3, 거시경제(경제) 2
CATEGORY_TARGETS = {"종목": 5, "섹터": 3, "경제": 2}
# 시드는 config.RANDOM_SEED 하나에서만 온다. 뉴스 선정용 별도 시드를 두면 논문에
# "seed=N"이라고 적어도 실제 기사 추첨은 다른 값이었던 상태가 되어 재현이 깨진다.
DEFAULT_SEED = config.RANDOM_SEED
# 뉴스 노출 윈도우 경계 (양끝 포함 비교와 짝을 이룸).
#   AM 판단: 전 거래일 15:30 ~ 당일 08:59
#   PM 판단: 당일 09:00 ~ 당일 15:29
# 15:29 기사는 당일 PM에, 15:30 기사는 다음 거래일 AM에 들어가므로 겹침도 공백도 없다.
# 이 값은 뉴스 가시성 경계이며, 체결·주문 마감 시각(config.MARKET_CLOSE_TIME=15:30)과는
# 의미가 다르므로 별도 상수로 둔다.
MARKET_OPEN_TIME = "09:00"
AM_NEWS_WINDOW_START_TIME = "15:30"  # 전 거래일, 포함
AM_NEWS_WINDOW_END_TIME = "08:59"  # 당일, 포함
PM_NEWS_WINDOW_START_TIME = "09:00"  # 당일, 포함
PM_NEWS_WINDOW_END_TIME = "15:29"  # 당일, 포함
AM_WINDOW_START_TIME = AM_NEWS_WINDOW_START_TIME  # 하위 호환 alias
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


def stable_news_seed(base_seed: int, *parts: object) -> int:
    """불변 식별자에서 선정 시드를 파생한다.

    서수(day_index)에서 파생하면 기간의 앞부분을 하루만 늘리거나 줄여도 겹치는
    날짜의 시드가 전부 밀려 같은 이벤트의 기사 선정이 바뀐다. 날짜·subturn 같은
    불변 ID로 해싱하면 기간을 조정해도 겹치는 이벤트의 선정이 보존된다.
    """
    payload = "|".join([str(base_seed), *(str(part) for part in parts)])
    return int.from_bytes(hashlib.sha256(payload.encode("utf-8")).digest()[:4], "big") & 0x7FFFFFFF


def _select_daily(rows: list[dict[str, Any]], *, seed: int | None = None) -> list[dict[str, Any]]:
    """버킷별 쿼터만큼 균등 무작위로 뽑는다.

    가용량이 쿼터보다 적으면 **그만큼만** 노출하고 다른 버킷 기사로 메우지 않는다
    (NEWS_RECRAWL_PLAN §5-2 확정). 보충을 하면 부족한 버킷의 자리를 풍부한 버킷이
    차지해 버킷 구성비가 조용히 왜곡되기 때문이다. 그 대신 턴별 노출량이 변동하므로
    실제 노출 분포를 결과와 함께 보고해야 한다.
    """
    rng = random.Random(seed)
    selected: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for category, target in CATEGORY_TARGETS.items():
        pool = [row for row in rows if row["category"] == category and row["id"] not in used_ids]
        picks = rng.sample(pool, min(target, len(pool)))
        selected.extend(picks)
        used_ids.update(row["id"] for row in picks)
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
        # 달력일 단위 폴백은 AM/PM 구분을 없애 노출 구조를 통째로 바꾼다.
        # 조용히 넘어가면 다른 실험이 된 줄 모르고 진행되므로 여기서 멈춘다.
        raise RuntimeError(
            f"거래일 캘린더를 읽을 수 없어 AM/PM 뉴스 윈도우를 만들 수 없다: {sim_db_path} "
            f"(조회된 거래일 {len(trading_dates)}개). "
            "달력일 단위 선정이 정말 필요하면 _select_by_calendar_day()를 명시적으로 호출한다."
        )
    previous_by_date = {
        day: trading_dates[index - 1]
        for index, day in enumerate(trading_dates)
        if index > 0
    }
    # Precompute each row's datetime once and sort, so every AM/PM window below
    # is a bisect range lookup instead of a full O(n) rescan of `processed`.
    dated_rows = sorted(
        (
            (row_dt, row)
            for row in processed
            if (row_dt := _combine_datetime(str(row.get("date", "")), str(row.get("time", "")))) is not None
        ),
        key=lambda item: item[0],
    )
    row_datetimes = [item[0] for item in dated_rows]

    def _window_candidates(start_dt: datetime, end_dt: datetime) -> list[dict[str, Any]]:
        lo = bisect.bisect_left(row_datetimes, start_dt)
        hi = bisect.bisect_right(row_datetimes, end_dt)
        return [row for _, row in dated_rows[lo:hi]]

    selected: list[dict[str, Any]] = []
    for day in trading_dates[1:]:
        previous_day = previous_by_date[day]
        windows = [
            (
                "am",
                previous_day,
                AM_NEWS_WINDOW_START_TIME,
                day,
                AM_NEWS_WINDOW_END_TIME,
            ),
            (
                "pm",
                day,
                PM_NEWS_WINDOW_START_TIME,
                day,
                PM_NEWS_WINDOW_END_TIME,
            ),
        ]
        for slot, start_date, start_time, end_date, end_time in windows:
            start_dt = _combine_datetime(start_date, start_time)
            end_dt = _combine_datetime(end_date, end_time)
            candidates = _window_candidates(start_dt, end_dt) if start_dt and end_dt else []
            # 서수가 아니라 (거래일, subturn)에서 파생한다. §4.11 P1 참조.
            seed = None if daily_seed is None else stable_news_seed(daily_seed, day, slot)
            selected.extend(_select_daily(candidates, seed=seed))
    selected.sort(key=lambda row: (row["date"], row["time"], row["id"]))
    return selected


def _select_by_calendar_day(processed: list[dict[str, Any]], *, daily_seed: int | None = None) -> list[dict[str, Any]]:
    """LEGACY: 거래일 캘린더 없이 달력일 단위로 선정한다. AM/PM 구분이 없으므로
    본 실험 경로에서는 쓰지 않고, 호출하려면 명시적으로 불러야 한다.
    """
    selected: list[dict[str, Any]] = []
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in processed:
        by_day[row["date"]].append(row)
    for day, rows in sorted(by_day.items()):
        seed = None if daily_seed is None else stable_news_seed(daily_seed, day)
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
    """LEGACY: 구 CSV 전처리(prepare_news) 전용 키워드 버킷 판정.

    JSON split 경로는 FOLDER_SECTOR로 버킷을 정하므로 이 함수를 쓰지 않는다.
    """
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
    """LEGACY: 구 중요도 점수. 현재 선정은 seed 고정 균등 무작위이며 미사용."""
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


# 소스 폴더 -> 뉴스 섹터. 버킷은 기사 내용 키워드가 아니라 "어디서 수집했는가"로
# 정한다. macro_* 세 폴더는 모두 하나의 거시경제 섹터다.
# Depth 2 추가 탐색 상한. 프롬프트 문구·LLM 출력 검증기·검색 호출·컨텍스트 한도가
# 모두 이 두 값을 공유한다. 0은 "이번 턴에는 검색하지 않는다"는 유효한 선택이다.
DEPTH2_MAX_KEYWORDS = 5
DEPTH2_MAX_SEARCH_ARTICLES = 5

FOLDER_SECTOR = {
    "samsung_split": "종목",
    "semiconductor_split": "섹터",
    "macro_economic-policy_split": "경제",
    "macro_business-index_split": "경제",
    "macro_trade_split": "경제",
}

# 같은 기사가 여러 폴더에서 수집됐을 때 대표 섹터를 정하는 우선순위.
# 하위(더 구체적인) 섹터가 이긴다: 삼성전자 > 반도체 > 거시경제.
SECTOR_PRIORITY = {"종목": 0, "섹터": 1, "경제": 2}

# "필터링 여부"의 노출 대상 값. 2026-07-29 재판정으로 다섯 폴더가 모두
# N=유지 / Y=제외 로 통일됐다. 이전에는 samsung_split만 N(=정상 기사)이고
# 나머지는 Y(=관련성 있음)라 극성이 갈렸는데, 그 상태로 실행하면 유지할 기사를
# 버리고 버릴 기사를 노출하게 된다.
FOLDER_KEEP_VALUE = {
    "samsung_split": "N",
    "macro_economic-policy_split": "N",
    "macro_trade_split": "N",
    "semiconductor_split": "N",
    "macro_business-index_split": "N",
}


def load_news_from_json_splits(
    splits_dir: Path | str = "outputs",
    *,
    daily_seed: int | None = None,
    sim_db_path: Path | str = config.SIM_DB,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """JSON split 폴더에서 뉴스 데이터를 로드한다.

    버킷은 기사 내용이 아니라 수집 폴더로 정한다. 같은 기사가 여러 폴더에서
    수집됐으면 ``SECTOR_PRIORITY``에 따라 더 구체적인 섹터가 대표가 된다
    (삼성전자 > 반도체 > 거시경제).
    """
    splits_path = Path(splits_dir)

    # (제목, 날짜) -> 대표 기사. 같은 키가 다시 나오면 우선순위가 높은 섹터로 교체한다.
    by_key: dict[tuple[str, str], dict[str, Any]] = {}

    for folder, keep_value in FOLDER_KEEP_VALUE.items():
        folder_path = splits_path / folder
        if not folder_path.exists():
            continue
        sector = FOLDER_SECTOR[folder]

        for json_file in sorted(folder_path.glob("*.json"), key=lambda x: int(x.stem)):
            # 손상된 split 파일을 조용히 건너뛰면 노출 뉴스가 말없이 줄어든다.
            with json_file.open(encoding="utf-8") as handle:
                articles = json.load(handle)

            for idx, article in enumerate(articles):
                if not isinstance(article, dict):
                    continue

                title = str(article.get("제목", "")).strip()
                summary = str(article.get("요약", "")).strip()
                written = str(article.get("작성시각", ""))
                date_text = written[:10]
                time_text = written[11:16] if len(written) > 10 else "00:00"

                if not title or not summary or len(date_text) != 10:
                    continue
                if str(article.get("필터링 여부", "N")) != keep_value:
                    continue

                key = (title, date_text)
                incumbent = by_key.get(key)
                if incumbent is not None and (
                    SECTOR_PRIORITY[incumbent["category"]] <= SECTOR_PRIORITY[sector]
                ):
                    continue

                by_key[key] = {
                    "id": f"{folder}_{json_file.stem}_{idx}",
                    "title": title,
                    "summary": summary,
                    "date": date_text,
                    "time": time_text,
                    "category": sector,
                    "is_fake": False,
                }

    all_articles = sorted(by_key.values(), key=lambda x: (x["date"], x["time"], x["id"]))

    # 2026-02-27부터 필터링
    start_date = "2026-02-27"
    articles_from_start = [a for a in all_articles if a['date'] >= start_date]

    # 실거래일 기준 AM/PM 창(전거래일 마감 이후~당일 개장 전 / 당일 개장~마감) 단위로
    # 턴당 쿼터(CATEGORY_TARGETS)를 적용해 선택한다. sim.db가 없으면 달력일 단위로 폴백.
    selected = _select_pre_close_cutoff_daily(
        articles_from_start,
        daily_seed=daily_seed or DEFAULT_SEED,
        sim_db_path=sim_db_path,
    )

    return articles_from_start, selected


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
        processed_csv_path: Path | str | None = None,
        daily_csv_path: Path | str | None = None,
        *,
        include_fake_news: bool = False,
        use_json_splits: bool | None = None,
        splits_dir: Path | str = "outputs",
        daily_seed: int | None = None,
    ) -> None:
        # 명시적으로 건네진 CSV 경로는 절대 조용히 무시하지 않는다. 가짜뉴스 주입
        # 실험처럼 런타임 뉴스를 교체하는 경로가 여기에 의존한다.
        explicit_csv = processed_csv_path is not None or daily_csv_path is not None
        self.processed_csv_path = Path(processed_csv_path or config.PROCESSED_NEWS_CSV)
        self.daily_csv_path = Path(daily_csv_path or config.DAILY_NEWS_SELECTION_CSV)
        self.include_fake_news = include_fake_news

        if use_json_splits is None:
            use_json_splits = not explicit_csv
        elif use_json_splits and explicit_csv:
            raise ValueError(
                "use_json_splits=True와 명시적 CSV 경로를 함께 줄 수 없다. "
                "둘 중 어떤 뉴스 입력을 쓸지 호출부에서 정해야 한다."
            )

        # split 로드 실패는 노출 뉴스를 말없이 바꾸므로 CSV로 폴백하지 않고 실패한다.
        if use_json_splits:
            self._processed, self._daily = load_news_from_json_splits(
                splits_dir,
                daily_seed=DEFAULT_SEED if daily_seed is None else daily_seed,
            )
        else:
            self._processed = self._filter_fake_rows(self._load_csv(self.processed_csv_path))
            self._daily = self._filter_fake_rows(self._load_csv(self.daily_csv_path))
        self.news_source = "json_splits" if use_json_splits else "csv"

        self._processed = self._filter_fake_rows(self._processed)
        self._daily = self._filter_fake_rows(self._daily)

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
                include_start=True,
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
        top_n: int = DEPTH2_MAX_SEARCH_ARTICLES,
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
                "search_read_max": DEPTH2_MAX_SEARCH_ARTICLES if news_depth >= 2 else 0,
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
                "search_read_max": DEPTH2_MAX_SEARCH_ARTICLES if news_depth >= 2 else 0,
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
