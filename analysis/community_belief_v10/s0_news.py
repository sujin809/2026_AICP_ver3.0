"""news_panel: 봉인 뉴스 정본에서 event slot 단위 표를 만든다.

카테고리는 article_id에 인코딩되어 있다 (news_<yyyymmdd>_<카테고리>_<hash>).
event별 목표는 종목 5·섹터 3·경제 2이며, 부족분은 backfill하지 않는다
(EXPERIMENT_DESIGN.md §4). 따라서 event별 실제 수가 10 미만인 경우가 정상이다.
"""

import json

import pandas as pd

import guard  # noqa: F401

guard.enforce_no_paid_api()

import paths

VALID_CATEGORIES = {"종목", "섹터", "경제"}


def parse_category(article_id: str) -> str:
    parts = article_id.split("_")
    if len(parts) < 4 or parts[2] not in VALID_CATEGORIES:
        raise ValueError(f"카테고리를 읽을 수 없다: {article_id}")
    return parts[2]


def build_news_panel() -> pd.DataFrame:
    bundle = json.loads(paths.NEWS_JSON.read_text(encoding="utf-8"))
    articles = pd.DataFrame(bundle["articles"])
    slots = pd.DataFrame(bundle["slots"])
    df = slots.merge(articles, on="article_id", how="left",
                     suffixes=("", "_article"))
    if df["title"].isna().any():
        raise AssertionError("slot에 대응하는 article이 없다")
    df["category"] = df["article_id"].map(parse_category)
    df[["date", "subturn"]] = df["event_id"].str.split("/", expand=True)
    cols = ["article_id", "event_id", "date", "subturn", "slot_ordinal",
            "category", "title", "summary", "published_at", "observed_at", "source"]
    return df[cols].sort_values(["event_id", "slot_ordinal"]).reset_index(drop=True)


def main() -> None:
    paths.PANELS.mkdir(parents=True, exist_ok=True)
    df = build_news_panel()
    df.to_parquet(paths.PANELS / "news_panel.parquet", index=False)
    print(f"news_panel {len(df):,}행 / event {df['event_id'].nunique()}개")
    print(df.groupby("category").size())


if __name__ == "__main__":
    main()
