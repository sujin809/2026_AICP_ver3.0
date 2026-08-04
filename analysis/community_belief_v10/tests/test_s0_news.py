import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import s0_news


def test_parse_category():
    assert s0_news.parse_category("news_20260227_종목_4aa6a00f") == "종목"
    assert s0_news.parse_category("news_20260306_경제_79081fc2") == "경제"
    assert s0_news.parse_category("news_20260306_섹터_abc12345") == "섹터"


def test_parse_category_rejects_unknown():
    with pytest.raises(ValueError):
        s0_news.parse_category("news_20260306_이상한_abc")


def test_news_panel_from_sealed_bundle():
    """봉인 뉴스는 articles 760 / slots 760 / event 90이다."""
    df = s0_news.build_news_panel()
    assert len(df) == 760
    assert df["event_id"].nunique() == 90
    assert set(df["category"]) == {"종목", "섹터", "경제"}
    assert df["title"].notna().all()
