import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dbio
import s0_community
from fixtures import make_run_db

EXPOSURE = "community:2026-03-03:t4:post:73:best_full_body:A002:delivered_t5"


def test_parse_exposure_id():
    parsed = s0_community.parse_exposure_id(EXPOSURE)
    assert parsed["source_date"] == "2026-03-03"
    assert parsed["source_turn"] == 4
    assert parsed["post_id"] == "73"
    assert parsed["relation"] == "best_full_body"
    assert parsed["reader_agent_id"] == "A002"
    assert parsed["delivery_turn"] == 5


def test_parse_exposure_id_rejects_garbage():
    import pytest
    with pytest.raises(ValueError):
        s0_community.parse_exposure_id("not-an-exposure-id")


def test_claim_panel_uses_delivery_turn_not_log_turn(tmp_path):
    """claim_id의 turn은 community_logs.turn이 아니라 '배달 turn'이다.

    실측(2026-08-03): community_logs.turn은 글이 올라온 PM turn이고,
    claim은 다음 AM에 소비되므로 claim_id의 turn은 그보다 1 크다.
    노출 ID 끝의 delivered_t<N>이 그 배달 turn이다.
    fixture의 community_logs 행은 turn=4이고 노출은 delivered_t5이므로
    claim_id는 t005여야 한다. log turn을 그대로 쓰면 t004가 되어 틀린다.
    """
    db = make_run_db(tmp_path, "RN_COMM_ON", with_community=True)
    df = s0_community.build_claim_panel(dbio.connect(db), "RN_COMM_ON")
    assert df["claim_id"].iloc[0] == "community_claim:A001:t005:01"
    assert df["delivery_turn"].iloc[0] == 5
    assert df["log_turn"].iloc[0] == 4
    assert df["source_post_ids"].iloc[0] == ["73"]
    assert df["claim_stance"].iloc[0] == "bullish"


def test_delivery_turn_falls_back_to_log_turn_plus_one(tmp_path):
    """노출 ID가 없는 claim은 log turn + 1로 배달 turn을 정한다."""
    assert s0_community.resolve_delivery_turn(4, []) == 5
    assert s0_community.resolve_delivery_turn(
        4, [{"delivery_turn": 5}, {"delivery_turn": 5}]) == 5


def test_post_panel_has_body_and_best_flag(tmp_path):
    db = make_run_db(tmp_path, "RN_COMM_ON", with_community=True)
    df = s0_community.build_post_panel(dbio.connect(db), "RN_COMM_ON")
    assert df["is_best"].iloc[0] == 1
    assert "본문 첫 문장" in df["content"].iloc[0]
