"""evidence_panel: belief가 인용한 근거 1건 = 1행.

kind 판정의 두 함정 (2026-08-03 실측):

1. depth2_recent_search의 evidence_id는 뉴스 article_id와 형태가 같다
   (예: news_20260227_종목_a0bca00c). 접두어로는 구분 불가하므로
   반드시 그 (agent_id, turn)의 evidence_json 화이트리스트를 본다.
2. LTB에는 evidence_json 열이 없다. source_stb_id로 STB 행을 조인해
   그 화이트리스트를 빌려 쓴다. outcome:/community_claim: 은 접두어로 판정한다.
"""

import json

import pandas as pd

import guard  # noqa: F401

guard.enforce_no_paid_api()

import dbio
import paths

WHITELIST_KEYS = ("news", "community_claims", "depth2_search_results")

# sealed v10 run(2026-08-03)에서 실측한 값. 봉인된 이 데이터셋에 대해서만 참이다.
# 다른 run에 대해 이 스크립트를 돌리면 여기서 시끄럽게 죽어야 한다 —
# 조용히 다른 행 수를 들고 넘어가면 안 된다 (전역 제약 9).
EXPECTED_TOTAL_ROWS = 420_679
EXPECTED_ARM_ROWS = {"RN_COMM_ON": 221_681, "RN_COMM_OFF": 198_998}


def build_whitelist(conn) -> dict:
    """(agent_id, turn) -> {evidence_id: kind}"""
    out = {}
    by_stb_id = {}
    rows = conn.execute(
        "select stb_id, agent_id, turn, evidence_json from simulation_stb_states")
    for stb_id, agent_id, turn, ev_json in rows:
        mapping = {}
        payload = json.loads(ev_json) if ev_json else {}
        for key in WHITELIST_KEYS:
            for item in payload.get(key) or []:
                mapping[item["evidence_id"]] = item["kind"]
        out[(agent_id, int(turn))] = mapping
        by_stb_id[stb_id] = mapping
    out["__by_stb_id__"] = by_stb_id
    return out


def classify(evidence_id: str, whitelist: dict) -> str:
    if evidence_id.startswith("outcome:"):
        return "outcome"
    if evidence_id.startswith("community_claim:"):
        return "community_claim"
    return whitelist.get(evidence_id, "unknown")


def _explode(ev_json: str, layer: str, meta: dict, whitelist: dict) -> list:
    rows = []
    payload = json.loads(ev_json) if ev_json else {}
    for dim in paths.DIMS:
        entry = payload.get(dim) or {}
        for relation in ("support", "contradict"):
            for evidence_id in entry.get(relation) or []:
                rows.append({**meta, "layer": layer, "dim": dim,
                             "relation": relation, "evidence_id": evidence_id,
                             "kind": classify(evidence_id, whitelist)})
    return rows


def build_evidence_panel(conn, arm: str) -> pd.DataFrame:
    whitelists = build_whitelist(conn)
    by_stb_id = whitelists["__by_stb_id__"]
    rows = []

    stb = conn.execute("select stb_id, agent_id, turn, dimension_evidence_json "
                       "from simulation_stb_states")
    for stb_id, agent_id, turn, dim_json in stb:
        meta = {"arm": arm, "agent_id": agent_id, "turn": int(turn),
                "state_id": stb_id}
        rows += _explode(dim_json, "STB", meta,
                         whitelists.get((agent_id, int(turn)), {}))

    ltb = conn.execute("select ltb_id, agent_id, turn, source_stb_id, "
                       "integration_evidence_json from simulation_ltb_states")
    for ltb_id, agent_id, turn, source_stb_id, dim_json in ltb:
        meta = {"arm": arm, "agent_id": agent_id, "turn": int(turn),
                "state_id": ltb_id}
        rows += _explode(dim_json, "LTB", meta, by_stb_id.get(source_stb_id, {}))

    df = pd.DataFrame(rows, columns=["arm", "agent_id", "turn", "layer", "dim",
                                     "relation", "evidence_id", "kind", "state_id"])
    df["is_burnin"] = df["turn"].isin(paths.BURNIN_TURNS)
    return df


def _assert_unknown_kinds_absent(panel: pd.DataFrame) -> None:
    unknown = panel[panel["kind"] == "unknown"]
    if len(unknown):
        sample = unknown["evidence_id"].head(10).tolist()
        raise AssertionError(
            f"kind 미분류 {len(unknown)}건. 조용히 넘기지 않는다. 예: {sample}")


def _assert_row_counts_match_sealed_run(panel: pd.DataFrame) -> None:
    total = len(panel)
    if total != EXPECTED_TOTAL_ROWS:
        raise AssertionError(
            f"evidence_panel 총 행 수 기대 {EXPECTED_TOTAL_ROWS:,} != 실제 {total:,}")
    for arm, expected in EXPECTED_ARM_ROWS.items():
        actual = int((panel["arm"] == arm).sum())
        if actual == 0:
            raise AssertionError(f"{arm} 결과가 비어 있다. 빈 결과는 예외로 실패시킨다.")
        if actual != expected:
            raise AssertionError(
                f"{arm} 행 수 기대 {expected:,} != 실제 {actual:,}")


def _assert_off_arm_has_no_community_claims(panel: pd.DataFrame) -> None:
    off_claims = panel[(panel["arm"] == "RN_COMM_OFF")
                        & (panel["kind"] == "community_claim")]
    if len(off_claims):
        raise AssertionError(
            f"RN_COMM_OFF에 community_claim 근거 {len(off_claims)}건 존재 "
            "(기대 0건). 조건 분리 보장 위반 — 조용히 넘기지 않는다.")


def _validate_panel(panel: pd.DataFrame) -> None:
    _assert_unknown_kinds_absent(panel)
    _assert_row_counts_match_sealed_run(panel)
    _assert_off_arm_has_no_community_claims(panel)


def main() -> None:
    paths.PANELS.mkdir(parents=True, exist_ok=True)
    frames = [build_evidence_panel(dbio.connect(paths.db_path(arm)), arm)
              for arm in paths.ARMS]
    panel = pd.concat(frames, ignore_index=True)
    _validate_panel(panel)
    panel.to_parquet(paths.PANELS / "evidence_panel.parquet", index=False)
    print(f"evidence_panel {len(panel):,}행 저장")
    print(panel.groupby(["arm", "kind"]).size())


if __name__ == "__main__":
    main()
