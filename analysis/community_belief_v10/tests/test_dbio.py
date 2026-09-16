import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dbio
from fixtures import make_run_db


def test_connect_is_readonly(tmp_path):
    db = make_run_db(tmp_path, "RN_COMM_ON", with_community=True)
    conn = dbio.connect(db)
    with pytest.raises(Exception):
        conn.execute("insert into simulation_stb_states (stb_id) values ('x')")


def test_read_table_returns_all_rows(tmp_path):
    db = make_run_db(tmp_path, "RN_COMM_ON", with_community=True)
    conn = dbio.connect(db)
    df = dbio.read_table(conn, "simulation_stb_states")
    assert len(df) == 4
    assert set(["stb_id", "agent_id", "turn", "dim_1", "evidence_json",
                "dimension_evidence_json"]).issubset(df.columns)


def test_table_count_matches(tmp_path):
    db = make_run_db(tmp_path, "RN_COMM_OFF", with_community=False)
    conn = dbio.connect(db)
    assert dbio.table_count(conn, "community_posts") == 0
