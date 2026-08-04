"""belief_panel: agent × turn × layer × dim 하나당 한 행.

STB(당일 정보 반응)와 LTB(사후 통합)는 의미가 다르므로 layer 열로 구분만 하고
절대 합산하지 않는다. dim_6은 STB와 LTB의 의미 자체가 다르므로
(STB=정보 한계, LTB=누적 자기평가) 층 간 비교를 금지한다 —
ANALYSIS_FIELD_GUIDE.md §1 참조.
"""

import pandas as pd

import guard  # noqa: F401

guard.enforce_no_paid_api()

import dbio
import paths


def _melt(df: pd.DataFrame, arm: str, layer: str, state_col: str) -> pd.DataFrame:
    keep = ["agent_id", "turn", "date", state_col]
    if layer == "LTB":
        keep.append("source_stb_id")
    out = df[keep + list(paths.DIMS)].melt(
        id_vars=keep, value_vars=list(paths.DIMS),
        var_name="dim", value_name="text")
    out = out.rename(columns={state_col: "state_id"})
    if layer == "STB":
        out["source_stb_id"] = pd.NA
    out.insert(0, "layer", layer)
    out.insert(0, "arm", arm)
    return out


def build_belief_panel(conn, arm: str) -> pd.DataFrame:
    stb = dbio.read_table(conn, "simulation_stb_states")
    ltb = dbio.read_table(conn, "simulation_ltb_states")
    panel = pd.concat(
        [_melt(stb, arm, "STB", "stb_id"), _melt(ltb, arm, "LTB", "ltb_id")],
        ignore_index=True)
    panel["is_burnin"] = panel["turn"].isin(paths.BURNIN_TURNS)
    cols = ["arm", "agent_id", "turn", "date", "layer", "dim", "text",
            "state_id", "source_stb_id", "is_burnin"]
    return panel[cols].sort_values(
        ["arm", "agent_id", "turn", "layer", "dim"]).reset_index(drop=True)


def main() -> None:
    paths.PANELS.mkdir(parents=True, exist_ok=True)
    frames = [build_belief_panel(dbio.connect(paths.db_path(arm)), arm)
              for arm in paths.ARMS]
    panel = pd.concat(frames, ignore_index=True)
    expected = (9000 + 9100) * 6 * len(paths.ARMS)
    if len(panel) != expected:
        raise AssertionError(f"belief_panel 행 수 {len(panel)} != 기대 {expected}")
    panel.to_parquet(paths.PANELS / "belief_panel.parquet", index=False)
    print(f"belief_panel {len(panel):,}행 저장")


if __name__ == "__main__":
    main()
