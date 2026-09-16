"""S0 검증 게이트. 조용한 오류를 여기서 전부 죽인다.

D2 검색이 60/60회 0건이면서 에러 없이 조용히 죽어 있던 사고(2026-07-31)가
있었다. 그래서 이 분석은 "결과가 비어 있음"을 성공으로 취급하지 않는다.
"""

import pandas as pd

import guard  # noqa: F401

guard.enforce_no_paid_api()

import dbio
import paths

EXPECTED_COUNTS = {
    "RN_COMM_ON": {"simulation_stb_states": 9000, "simulation_ltb_states": 9100,
                   "simulation_decisions": 9000, "simulation_fills": 9000,
                   "simulation_trade_outcomes": 27000,
                   "community_posts": 3150, "community_interactions": 15745,
                   "community_logs": 4500},
    "RN_COMM_OFF": {"simulation_stb_states": 9000, "simulation_ltb_states": 9100,
                    "simulation_decisions": 9000, "simulation_fills": 9000,
                    "simulation_trade_outcomes": 27000,
                    "community_posts": 0, "community_interactions": 0,
                    "community_logs": 0},
}


def check_panels_are_not_empty(panels: dict) -> None:
    """빈 패널은 조용히 통과시키지 않는다.

    OFF arm 격리/kind 분류/exposure_level 게이트는 필터링된 부분집합이
    비어 있는지만 보므로, 패널 전체가 비어 있으면 세 게이트 모두 공허하게
    PASS해 버린다. 그래서 이 게이트를 다른 모든 게이트보다 먼저 돌린다.
    """
    empty = [name for name, df in panels.items() if len(df) == 0]
    if empty:
        raise AssertionError(f"패널이 비어 있다: {', '.join(empty)}")


def check_row_counts() -> None:
    for arm, expected in EXPECTED_COUNTS.items():
        conn = dbio.connect(paths.db_path(arm))
        for table, want in expected.items():
            got = dbio.table_count(conn, table)
            if got != want:
                raise AssertionError(f"{arm}.{table} 행 수 {got} != 기대 {want}")


def check_off_arm_is_clean(evidence: pd.DataFrame) -> None:
    leak = evidence[(evidence["arm"] == "RN_COMM_OFF")
                    & (evidence["kind"] == "community_claim")]
    if len(leak):
        raise AssertionError(
            f"OFF arm에 커뮤니티 근거 {len(leak)}건 — 조건 격리 위반")


def check_no_unknown_kind(evidence: pd.DataFrame) -> None:
    unknown = evidence[evidence["kind"] == "unknown"]
    if len(unknown):
        raise AssertionError(
            f"kind 미분류 {len(unknown)}건: {unknown['evidence_id'].head(5).tolist()}")


def check_arms_are_paired(belief: pd.DataFrame) -> None:
    arms = set(belief["arm"].unique())
    expected_arms = {"RN_COMM_ON", "RN_COMM_OFF"}
    if arms != expected_arms:
        raise AssertionError(
            f"arm 라벨이 예상과 다르다: {sorted(arms)} (기대: {sorted(expected_arms)})")
    keys = {arm: set(map(tuple, belief.loc[belief["arm"] == arm,
                                           ["agent_id", "turn", "layer", "dim"]].values))
            for arm in arms}
    on, off = keys["RN_COMM_ON"], keys["RN_COMM_OFF"]
    if on != off:
        raise AssertionError(
            f"ON/OFF 짝이 맞지 않는다. ON에만 {len(on - off)}개, OFF에만 {len(off - on)}개")


def check_exposure_levels(exposure: pd.DataFrame) -> None:
    levels = set(exposure["exposure_level"].dropna())
    if not levels <= {"title_only", "full_body"}:
        raise AssertionError(f"예상치 못한 exposure_level: {levels}")


def check_posting_rate(post: pd.DataFrame) -> str:
    """게시율 100%면 '게시 여부 판단' 설계 요소의 변별력이 없다 → 논문 한계."""
    on = post[post["arm"] == "RN_COMM_ON"]
    eligible_per_day, days = 70, 45
    rate = len(on) / (eligible_per_day * days)
    return f"게시율 {rate:.1%} ({len(on)}/{eligible_per_day * days})"


def run_gates() -> dict:
    belief = pd.read_parquet(paths.PANELS / "belief_panel.parquet")
    evidence = pd.read_parquet(paths.PANELS / "evidence_panel.parquet")
    exposure = pd.read_parquet(paths.PANELS / "exposure_panel.parquet")
    post = pd.read_parquet(paths.PANELS / "post_panel.parquet")
    panels = {
        "belief_panel": belief,
        "evidence_panel": evidence,
        "exposure_panel": exposure,
        "post_panel": post,
    }

    results = {}
    check_panels_are_not_empty(panels); results["패널 비어있지 않음"] = "PASS"
    check_row_counts(); results["행 수"] = "PASS"
    check_off_arm_is_clean(evidence); results["OFF arm 격리"] = "PASS"
    check_no_unknown_kind(evidence); results["kind 분류"] = "PASS"
    check_arms_are_paired(belief); results["ON/OFF 짝"] = "PASS"
    check_exposure_levels(exposure); results["exposure_level"] = "PASS"
    results["게시율"] = check_posting_rate(post)
    return results


if __name__ == "__main__":
    for name, status in run_gates().items():
        print(f"[{name}] {status}")
