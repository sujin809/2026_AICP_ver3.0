from pathlib import Path

REPO = Path("/Users/sujinjung/Desktop/felab/aicp/2026_AICP_ver3.0")
RUN_ROOT = REPO / "outputs/logs/experiment_matrix_45day_v10"
NEWS_JSON = REPO / "preparation/rn_ab_sealed_v1/news.json"
OUT = REPO / "analysis/community_belief_v10"
PANELS = OUT / "panels"
FIGURES = OUT / "figures"

ARMS = ("RN_COMM_ON", "RN_COMM_OFF")

# 첫 3거래일 × AM/PM. 삭제하지 않고 플래그로만 표시한다.
BURNIN_TURNS = frozenset(range(1, 7))

DIMS = tuple(f"dim_{i}" for i in range(1, 7))


def db_path(arm: str) -> Path:
    return RUN_ROOT / arm / ".runtime" / "runtime_sim.db"


def csv_path(arm: str, name: str) -> Path:
    return RUN_ROOT / arm / name
