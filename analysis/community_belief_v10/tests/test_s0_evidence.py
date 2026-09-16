import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dbio
import s0_evidence
from fixtures import make_run_db


def test_search_result_is_not_misread_as_news(tmp_path):
    """검색 결과의 evidence_id는 뉴스 article_id와 형태가 같다.
    화이트리스트를 봐야만 구분할 수 있다."""
    db = make_run_db(tmp_path, "RN_COMM_ON", with_community=True)
    df = s0_evidence.build_evidence_panel(dbio.connect(db), "RN_COMM_ON")
    row = df[df["evidence_id"] == "news_20260227_종목_ccc"]
    assert len(row) == 1
    assert row["kind"].iloc[0] == "depth2_recent_search"


def test_news_is_classified_as_news(tmp_path):
    db = make_run_db(tmp_path, "RN_COMM_ON", with_community=True)
    df = s0_evidence.build_evidence_panel(dbio.connect(db), "RN_COMM_ON")
    kinds = set(df.loc[df["evidence_id"] == "news_20260306_종목_aaa", "kind"])
    assert kinds == {"news"}


def test_community_claim_prefix(tmp_path):
    db = make_run_db(tmp_path, "RN_COMM_ON", with_community=True)
    df = s0_evidence.build_evidence_panel(dbio.connect(db), "RN_COMM_ON")
    row = df[df["evidence_id"] == "community_claim:A001:t005:01"]
    assert row["kind"].iloc[0] == "community_claim"
    assert row["dim"].iloc[0] == "dim_4"


def test_ltb_outcome_id_classified(tmp_path):
    db = make_run_db(tmp_path, "RN_COMM_ON", with_community=True)
    df = s0_evidence.build_evidence_panel(dbio.connect(db), "RN_COMM_ON")
    row = df[df["evidence_id"] == "outcome:fill_A001_t002:h1"]
    assert row["kind"].iloc[0] == "outcome"
    assert row["layer"].iloc[0] == "LTB"


def test_ltb_news_id_resolved_via_source_stb(tmp_path):
    """LTB에는 화이트리스트가 없다. source_stb_id로 STB 화이트리스트를 봐야 한다."""
    db = make_run_db(tmp_path, "RN_COMM_ON", with_community=True)
    df = s0_evidence.build_evidence_panel(dbio.connect(db), "RN_COMM_ON")
    ltb_news = df[(df["layer"] == "LTB")
                  & (df["evidence_id"] == "news_20260306_종목_aaa")]
    assert len(ltb_news) >= 1
    assert set(ltb_news["kind"]) == {"news"}


def test_relations_are_preserved(tmp_path):
    db = make_run_db(tmp_path, "RN_COMM_ON", with_community=True)
    df = s0_evidence.build_evidence_panel(dbio.connect(db), "RN_COMM_ON")
    contradict = df[(df["evidence_id"] == "news_20260306_경제_bbb")
                    & (df["layer"] == "STB")]
    assert contradict["relation"].iloc[0] == "contradict"


def test_unknown_kind_is_reported_not_silently_dropped(tmp_path):
    db = make_run_db(tmp_path, "RN_COMM_ON", with_community=True)
    wl = {"news_x": "news"}
    assert s0_evidence.classify("news_unlisted", wl) == "unknown"


def test_off_arm_community_claim_guard_fires():
    """RN_COMM_OFF는 조건상 community_claim 근거가 있어서는 안 된다
    (조건 분리 보장). main()이 파일을 쓰기 전에 이걸 예외로 잡아야 한다."""
    panel = pd.DataFrame([{
        "arm": "RN_COMM_OFF", "agent_id": "A001", "turn": 5, "layer": "STB",
        "dim": "dim_4", "relation": "support",
        "evidence_id": "community_claim:A001:t005:01",
        "kind": "community_claim", "state_id": "stb_A001_t005",
    }])
    with pytest.raises(AssertionError):
        s0_evidence._assert_off_arm_has_no_community_claims(panel)
