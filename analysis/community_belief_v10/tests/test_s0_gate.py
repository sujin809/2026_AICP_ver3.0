import copy
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import s0_gate


def test_gate_detects_off_arm_community_leak():
    """OFF arm에 커뮤니티 근거가 있으면 설계 위반이다. 반드시 잡아야 한다."""
    evidence = pd.DataFrame([
        {"arm": "RN_COMM_OFF", "kind": "community_claim", "evidence_id": "c1"},
    ])
    with pytest.raises(AssertionError, match="OFF arm"):
        s0_gate.check_off_arm_is_clean(evidence)


def test_gate_detects_unknown_kind():
    evidence = pd.DataFrame([{"arm": "RN_COMM_ON", "kind": "unknown",
                              "evidence_id": "mystery"}])
    with pytest.raises(AssertionError, match="미분류"):
        s0_gate.check_no_unknown_kind(evidence)


def test_gate_detects_unpaired_keys():
    """ON/OFF의 (agent, turn) 집합이 다르면 paired 대조가 불가능하다."""
    belief = pd.DataFrame([
        {"arm": "RN_COMM_ON", "agent_id": "A001", "turn": 5, "layer": "STB", "dim": "dim_1"},
        {"arm": "RN_COMM_OFF", "agent_id": "A001", "turn": 6, "layer": "STB", "dim": "dim_1"},
    ])
    with pytest.raises(AssertionError, match="짝"):
        s0_gate.check_arms_are_paired(belief)


def test_gate_passes_on_clean_input():
    belief = pd.DataFrame([
        {"arm": a, "agent_id": "A001", "turn": 5, "layer": "STB", "dim": "dim_1"}
        for a in ("RN_COMM_ON", "RN_COMM_OFF")])
    s0_gate.check_arms_are_paired(belief)  # 예외 없이 통과


def test_gate_detects_unexpected_arm_label_as_assertion_not_keyerror():
    """arm 라벨이 기대와 다르면 KeyError가 아니라 AssertionError로 죽어야 한다."""
    belief = pd.DataFrame([
        {"arm": "RN_COMM_ON", "agent_id": "A001", "turn": 5, "layer": "STB", "dim": "dim_1"},
        {"arm": "RN_COMM_OFF_v2", "agent_id": "A001", "turn": 5, "layer": "STB", "dim": "dim_1"},
    ])
    with pytest.raises(AssertionError, match="라벨"):
        s0_gate.check_arms_are_paired(belief)


def test_gate_detects_empty_panel():
    """패널 전체가 비어 있으면 다른 게이트가 공허하게 통과하기 전에 여기서 잡아야 한다."""
    panels = {
        "belief_panel": pd.DataFrame({"arm": ["RN_COMM_ON"]}),
        "evidence_panel": pd.DataFrame(columns=["arm", "kind", "evidence_id"]),
    }
    with pytest.raises(AssertionError, match="비어"):
        s0_gate.check_panels_are_not_empty(panels)


def test_gate_passes_when_no_panel_is_empty():
    panels = {
        "belief_panel": pd.DataFrame({"arm": ["RN_COMM_ON"]}),
        "evidence_panel": pd.DataFrame({"arm": ["RN_COMM_ON"]}),
    }
    s0_gate.check_panels_are_not_empty(panels)  # 예외 없이 통과


def test_gate_detects_unexpected_exposure_level():
    exposure = pd.DataFrame([{"exposure_level": "summary_only"}])
    with pytest.raises(AssertionError, match="예상치 못한"):
        s0_gate.check_exposure_levels(exposure)


def test_gate_detects_row_count_mismatch(monkeypatch):
    """check_row_counts는 실제 DB를 읽는다. EXPECTED_COUNTS를 일부러 틀리게
    바꿔서 그 불일치를 잡아내는지 확인한다(가짜 DB 없이, 빠르게)."""
    bad_counts = copy.deepcopy(s0_gate.EXPECTED_COUNTS)
    bad_counts["RN_COMM_ON"]["simulation_stb_states"] = 1
    monkeypatch.setattr(s0_gate, "EXPECTED_COUNTS", bad_counts)
    with pytest.raises(AssertionError, match="행 수"):
        s0_gate.check_row_counts()
