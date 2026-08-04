import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dbio
import s0_beliefs
from fixtures import make_run_db


def test_panel_is_long_format_one_row_per_dim(tmp_path):
    db = make_run_db(tmp_path, "RN_COMM_ON", with_community=True)
    df = s0_beliefs.build_belief_panel(dbio.connect(db), "RN_COMM_ON")
    # STB 4행 × 6차원 + LTB 2행 × 6차원
    assert len(df) == 4 * 6 + 2 * 6
    assert set(df["dim"]) == {f"dim_{i}" for i in range(1, 7)}
    assert set(df["layer"]) == {"STB", "LTB"}


def test_burnin_flag_covers_turns_1_to_6(tmp_path):
    db = make_run_db(tmp_path, "RN_COMM_ON", with_community=True)
    df = s0_beliefs.build_belief_panel(dbio.connect(db), "RN_COMM_ON")
    assert df.loc[df["turn"] == 5, "is_burnin"].all()
    assert df.loc[df["turn"] == 6, "is_burnin"].all()


def test_ltb_keeps_source_stb_id_for_kind_lookup(tmp_path):
    """LTB에는 화이트리스트가 없으므로 source_stb_id가 반드시 보존돼야 한다."""
    db = make_run_db(tmp_path, "RN_COMM_ON", with_community=True)
    df = s0_beliefs.build_belief_panel(dbio.connect(db), "RN_COMM_ON")
    ltb = df[df["layer"] == "LTB"]
    assert ltb["source_stb_id"].notna().all()
    stb = df[df["layer"] == "STB"]
    assert stb["source_stb_id"].isna().all()


def test_text_is_carried_verbatim(tmp_path):
    db = make_run_db(tmp_path, "RN_COMM_ON", with_community=True)
    df = s0_beliefs.build_belief_panel(dbio.connect(db), "RN_COMM_ON")
    row = df[(df["layer"] == "STB") & (df["agent_id"] == "A001")
             & (df["turn"] == 5) & (df["dim"] == "dim_1")]
    assert row["text"].iloc[0] == "d1"
