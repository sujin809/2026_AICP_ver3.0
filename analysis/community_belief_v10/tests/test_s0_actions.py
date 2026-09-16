import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dbio
import s0_actions
from fixtures import make_run_db


def test_signed_value_is_buy_positive_sell_negative(tmp_path):
    """fixture: dec_A001_t005 buy 10 @ 70000, dec_A002_t005 sell 5 @ 70000."""
    db = make_run_db(tmp_path, "RN_COMM_ON", with_community=False)
    df = s0_actions.build_action_panel(dbio.connect(db), "RN_COMM_ON")

    buy = df[df["agent_id"] == "A001"].iloc[0]
    sell = df[df["agent_id"] == "A002"].iloc[0]

    assert buy["signed_value"] > 0
    assert sell["signed_value"] < 0
    assert buy["signed_value"] == pytest.approx(10 * 70000.0)
    assert sell["signed_value"] == pytest.approx(-5 * 70000.0)


def test_burnin_flag_true_for_turn_5_rows_and_rows_kept(tmp_path):
    """burn-in은 turn 1~6. 삭제하지 않고 플래그로만 표시한다 (전역 제약 10)."""
    db = make_run_db(tmp_path, "RN_COMM_ON", with_community=False)
    df = s0_actions.build_action_panel(dbio.connect(db), "RN_COMM_ON")

    assert len(df) == 2
    assert df["is_burnin"].all()


def test_decision_and_fill_columns_stay_separate(tmp_path):
    """decision(의도)과 fill(실제 체결)을 한 열로 합치지 않는다."""
    db = make_run_db(tmp_path, "RN_COMM_ON", with_community=False)
    df = s0_actions.build_action_panel(dbio.connect(db), "RN_COMM_ON")

    assert "requested_quantity" in df.columns
    assert "filled_quantity" in df.columns
    buy = df[df["agent_id"] == "A001"].iloc[0]
    assert buy["requested_quantity"] == 10
    assert buy["filled_quantity"] == 10


def test_unexpected_action_raises(tmp_path):
    db = make_run_db(tmp_path, "RN_COMM_ON", with_community=False)
    # dbio.connect opens with immutable=1 (read-only); mutate via a plain writer connection first.
    import sqlite3
    writer = sqlite3.connect(db)
    writer.execute("update simulation_decisions set action = 'hold' "
                   "where decision_id = 'dec_A001_t005'")
    writer.commit()
    writer.close()

    with pytest.raises(AssertionError):
        s0_actions.build_action_panel(dbio.connect(db), "RN_COMM_ON")


def test_assert_every_decision_has_fill_raises_on_unmatched_decision():
    df = pd.DataFrame([
        {"decision_id": "dec_A001_t005", "fill_id": "fill_A001_t005"},
        {"decision_id": "dec_A999_t005", "fill_id": None},
    ])
    with pytest.raises(AssertionError):
        s0_actions._assert_every_decision_has_fill(df)
