import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import s2_citation


def _fixtures():
    evidence = pd.DataFrame([
        {"arm": "RN_COMM_ON", "agent_id": "A001", "turn": 5, "layer": "STB",
         "dim": "dim_4", "relation": "support",
         "evidence_id": "community_claim:A001:t005:01", "kind": "community_claim"},
        {"arm": "RN_COMM_ON", "agent_id": "A001", "turn": 5, "layer": "STB",
         "dim": "dim_1", "relation": "support",
         "evidence_id": "news_20260306_종목_aaa", "kind": "news"},
        {"arm": "RN_COMM_OFF", "agent_id": "A001", "turn": 5, "layer": "STB",
         "dim": "dim_1", "relation": "support",
         "evidence_id": "news_20260306_종목_aaa", "kind": "news"},
        {"arm": "RN_COMM_ON", "agent_id": "A001", "turn": 12, "layer": "LTB",
         "dim": "dim_6", "relation": "support",
         "evidence_id": "outcome:fill_A001_t010:h1", "kind": "outcome"},
    ])
    claims = pd.DataFrame([
        {"arm": "RN_COMM_ON", "claim_id": "community_claim:A001:t005:01",
         "source_post_ids": ["73"], "claim_stance": "bearish"},
    ])
    corpus = pd.DataFrame([
        {"doc_id": "post:73", "doc_type": "post", "post_id": "73",
         "article_id": None, "cluster": 2},
        {"doc_id": "news:news_20260306_종목_aaa", "doc_type": "news",
         "post_id": None, "article_id": "news_20260306_종목_aaa", "cluster": 5},
    ])
    news = pd.DataFrame([
        {"article_id": "news_20260306_종목_aaa", "category": "종목"},
    ])
    return evidence, claims, corpus, news


def test_community_claim_is_labelled_by_its_stance():
    """주축은 발견된 클러스터가 아니라 데이터에 내장된 stance다."""
    linked = s2_citation.attach_labels(*_fixtures())
    row = linked[linked["kind"] == "community_claim"]
    assert row["topic_label"].iloc[0] == "bearish"
    assert row["cluster"].iloc[0] == 2          # exploratory 열은 그대로 유지


def test_news_is_labelled_by_sealed_category():
    linked = s2_citation.attach_labels(*_fixtures())
    row = linked[(linked["kind"] == "news") & (linked["arm"] == "RN_COMM_ON")]
    assert row["topic_label"].iloc[0] == "종목"
    assert row["cluster"].iloc[0] == 5


def test_outcome_gets_its_own_label_not_na():
    """성과 피드백에는 토픽 축이 없다. NA로 두면 미할당과 구분되지 않는다."""
    linked = s2_citation.attach_labels(*_fixtures())
    row = linked[linked["kind"] == "outcome"]
    assert row["topic_label"].iloc[0] == "outcome"


def test_community_share_is_zero_for_off_arm():
    linked = s2_citation.attach_labels(*_fixtures())
    share = s2_citation.community_share(linked)
    off = share[share["arm"] == "RN_COMM_OFF"]
    assert (off["community_share"] == 0).all()


def test_label_by_dimension_crosstab():
    linked = s2_citation.attach_labels(*_fixtures())
    table = s2_citation.label_by_dimension(linked)
    hit = table[(table["topic_label"] == "bearish") & (table["dim"] == "dim_4")]
    assert hit["n"].iloc[0] == 1
