import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import s2_movement


def _panel():
    return pd.DataFrame([
        {"arm": "A", "agent_id": "A001", "turn": 1, "layer": "STB", "dim": "dim_1"},
        {"arm": "A", "agent_id": "A001", "turn": 2, "layer": "STB", "dim": "dim_1"},
        {"arm": "A", "agent_id": "A001", "turn": 3, "layer": "STB", "dim": "dim_1"},
    ])


def test_unchanged_text_gives_zero_delta():
    """패러프레이즈 강제가 없으므로 Δ≈0은 진짜 무변화다."""
    vectors = np.array([[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]])
    out = s2_movement.semantic_delta(_panel(), vectors)
    row2 = out[out["turn"] == 2].iloc[0]
    assert np.isclose(row2["delta_semantic"], 0.0, atol=1e-9)
    row3 = out[out["turn"] == 3].iloc[0]
    assert np.isclose(row3["delta_semantic"], 1.0, atol=1e-9)


def test_first_turn_has_no_delta():
    vectors = np.array([[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]])
    out = s2_movement.semantic_delta(_panel(), vectors)
    assert out[out["turn"] == 1]["delta_semantic"].isna().all()


def test_delta_is_computed_within_agent_layer_dim():
    """다른 agent/layer/dim의 값이 섞이면 안 된다."""
    panel = pd.DataFrame([
        {"arm": "A", "agent_id": "A001", "turn": 1, "layer": "STB", "dim": "dim_1"},
        {"arm": "A", "agent_id": "A002", "turn": 1, "layer": "STB", "dim": "dim_1"},
        {"arm": "A", "agent_id": "A002", "turn": 2, "layer": "STB", "dim": "dim_1"},
    ])
    vectors = np.array([[1.0, 0.0], [0.0, 1.0], [0.0, 1.0]])
    out = s2_movement.semantic_delta(panel, vectors)
    assert out[out["agent_id"] == "A001"]["delta_semantic"].isna().all()
    a002 = out[(out["agent_id"] == "A002") & (out["turn"] == 2)]
    assert np.isclose(a002["delta_semantic"].iloc[0], 0.0, atol=1e-9)


def test_topic_delta_sign():
    """belief가 centroid 쪽으로 가면 양수여야 한다."""
    panel = pd.DataFrame([
        {"arm": "A", "agent_id": "A001", "turn": 1, "layer": "STB", "dim": "dim_1"},
        {"arm": "A", "agent_id": "A001", "turn": 2, "layer": "STB", "dim": "dim_1"},
    ])
    vectors = np.array([[0.0, 1.0], [1.0, 0.0]])
    centroids = np.array([[1.0, 0.0]])
    out = s2_movement.topic_delta(panel, vectors, centroids)
    row = out[(out["turn"] == 2) & (out["cluster"] == 0)].iloc[0]
    assert row["delta_topic"] > 0
