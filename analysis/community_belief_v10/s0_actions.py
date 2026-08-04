"""action_panel: decision과 fill을 turn 단위로 합친다.

decision(의도)과 fill(실제 체결)은 다른 것이다. 절대 합치지 않고 두 열로 둔다
(README.md '결과와 재현성' 절). signed_value는 매수 +, 매도 −로
일별 순매수 방향 집계에 쓴다.
"""

import pandas as pd

import guard  # noqa: F401

guard.enforce_no_paid_api()

import dbio
import paths


def _assert_every_decision_has_fill(df: pd.DataFrame) -> None:
    """decision과 fill은 이 데이터셋에서 1:1이어야 한다 (실측 확인됨).

    fillna(0)으로 조용히 미체결 취급하면 전역 제약 9(빈 결과는 예외로 실패)를
    위반한다. 매칭되는 fill이 없는 decision은 여기서 시끄럽게 죽어야 한다.
    """
    missing = df[df["fill_id"].isna()]
    if len(missing):
        sample = missing["decision_id"].head(5).tolist()
        raise AssertionError(
            f"fill이 없는 decision {len(missing)}건 (조용히 0으로 채우지 않는다). "
            f"예: {sample}")


def build_action_panel(conn, arm: str) -> pd.DataFrame:
    decisions = dbio.read_table(conn, "simulation_decisions")
    fills = dbio.read_table(conn, "simulation_fills")
    df = decisions.merge(
        fills[["decision_id", "fill_id", "filled_quantity", "executed_price", "fee"]],
        on="decision_id", how="left")
    _assert_every_decision_has_fill(df)
    df.insert(0, "arm", arm)
    df["turn"] = pd.to_numeric(df["turn"])
    df["filled_quantity"] = pd.to_numeric(df["filled_quantity"])
    df["executed_price"] = pd.to_numeric(df["executed_price"])
    sign = df["action"].map({"buy": 1, "sell": -1})
    if sign.isna().any():
        raise AssertionError(f"예상치 못한 action: {set(df['action'])}")
    df["signed_value"] = sign * df["filled_quantity"] * df["executed_price"]
    df["is_burnin"] = df["turn"].isin(paths.BURNIN_TURNS)
    cols = ["arm", "agent_id", "turn", "date", "action", "requested_quantity",
            "fill_id", "filled_quantity", "executed_price", "signed_value", "is_burnin"]
    return df[cols]


def main() -> None:
    paths.PANELS.mkdir(parents=True, exist_ok=True)
    panel = pd.concat(
        [build_action_panel(dbio.connect(paths.db_path(arm)), arm)
         for arm in paths.ARMS], ignore_index=True)
    if len(panel) != 9000 * len(paths.ARMS):
        raise AssertionError(f"action_panel 행 수 {len(panel)} != 18000")
    panel.to_parquet(paths.PANELS / "action_panel.parquet", index=False)
    print(f"action_panel {len(panel):,}행")


if __name__ == "__main__":
    main()
