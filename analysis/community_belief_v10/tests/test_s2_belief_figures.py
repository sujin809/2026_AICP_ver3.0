import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import s2_belief_figures


def _paired_panel():
    rows = []
    for arm in ("RN_COMM_ON", "RN_COMM_OFF"):
        for turn in (7, 8):
            rows.append({"arm": arm, "agent_id": "A001", "turn": turn,
                         "layer": "STB", "dim": "dim_4", "date": "2026-03-05"})
    return pd.DataFrame(rows)


def test_arm_divergence_pairs_identical_keys():
    """같은 (agent, turn, layer, dim)의 ON/OFF 벡터 거리를 잰다."""
    panel = _paired_panel()
    # ON turn7 == OFF turn7 (거리 0), ON turn8 ⟂ OFF turn8 (거리 1)
    vectors = np.array([[1.0, 0.0], [1.0, 0.0],      # ON  t7, t8
                        [1.0, 0.0], [0.0, 1.0]])     # OFF t7, t8
    out = s2_belief_figures.arm_divergence(panel, vectors)
    assert len(out) == 2
    assert np.isclose(out.loc[out["turn"] == 7, "divergence"].iloc[0], 0.0)
    assert np.isclose(out.loc[out["turn"] == 8, "divergence"].iloc[0], 1.0)


def test_arm_divergence_fails_when_keys_do_not_pair():
    import pytest
    panel = _paired_panel().drop(index=3).reset_index(drop=True)
    vectors = np.eye(2)[[0, 0, 0]]
    with pytest.raises(AssertionError, match="짝"):
        s2_belief_figures.arm_divergence(panel, vectors)


def test_stance_axis_points_from_bearish_to_bullish():
    claims = pd.DataFrame({"claim_stance": ["bullish", "bullish",
                                            "bearish", "bearish"]})
    vectors = np.array([[1.0, 0.0], [1.0, 0.0], [-1.0, 0.0], [-1.0, 0.0]])
    axis = s2_belief_figures.stance_axis(claims, vectors)
    assert np.isclose(np.linalg.norm(axis), 1.0)
    assert axis[0] > 0.99          # bullish 쪽이 +


def test_stance_axis_requires_both_stances():
    import pytest
    claims = pd.DataFrame({"claim_stance": ["bullish", "neutral"]})
    vectors = np.array([[1.0, 0.0], [0.0, 1.0]])
    with pytest.raises(AssertionError, match="stance"):
        s2_belief_figures.stance_axis(claims, vectors)


def test_project_on_stance_returns_scalar_per_row():
    panel = _paired_panel()
    vectors = np.array([[1.0, 0.0], [0.0, 1.0], [1.0, 0.0], [0.0, 1.0]])
    axis = np.array([1.0, 0.0])
    out = s2_belief_figures.project_on_stance(panel, vectors, axis)
    assert len(out) == len(panel)
    assert np.isclose(out["stance_score"].iloc[0], 1.0)
    assert np.isclose(out["stance_score"].iloc[1], 0.0)


def test_all_three_figures_are_written(tmp_path, monkeypatch):
    monkeypatch.setattr(s2_belief_figures.paths, "FIGURES", tmp_path)
    divergence = pd.DataFrame({"turn": [7, 8, 7, 8],
                               "layer": ["STB"] * 4,
                               "dim": ["dim_1", "dim_1", "dim_4", "dim_4"],
                               "divergence": [0.1, 0.2, 0.3, 0.5]})
    stance = pd.DataFrame({"arm": ["RN_COMM_ON"] * 2 + ["RN_COMM_OFF"] * 2,
                           "date": ["2026-03-05", "2026-03-06"] * 2,
                           "layer": ["STB"] * 4, "dim": ["dim_1"] * 4,
                           "stance_score": [0.2, 0.3, 0.1, 0.1]})
    movement = pd.DataFrame({"arm": ["RN_COMM_ON"] * 4,
                             "agent_id": ["A001", "A001", "A002", "A002"],
                             "turn": [7, 8, 7, 8], "layer": ["STB"] * 4,
                             "dim": ["dim_4"] * 4,
                             "delta_semantic": [0.1, 0.4, 0.2, 0.0]})
    written = s2_belief_figures.plot_all(divergence, stance, movement)
    assert {t.name for t in written} == {
        "s2_arm_divergence.png", "s2_stance_axis_timeline.png",
        "s2_change_heatmap.png"}
    for target in written:
        assert target.exists() and target.stat().st_size > 0
