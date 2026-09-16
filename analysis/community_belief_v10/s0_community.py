"""커뮤니티 패널 3종.

exposure_level(title_only / full_body)은 DB의 community_interactions에는
없고 CSV에만 있다(2026-08-03 실측). 노출 수준을 다루는 모든 분석은 CSV를 쓴다.
두 수준은 절대 합산하지 않는다 — ANALYSIS_FIELD_GUIDE.md §2-E.
"""

import json
import re

import pandas as pd

import guard  # noqa: F401

guard.enforce_no_paid_api()

import dbio
import paths

EXPOSURE_RE = re.compile(
    r"^community:(?P<source_date>\d{4}-\d{2}-\d{2}):t(?P<source_turn>\d+):"
    r"post:(?P<post_id>[^:]+):(?P<relation>[a-z_]+):"
    r"(?P<reader_agent_id>[^:]+):delivered_t(?P<delivery_turn>\d+)$")


def parse_exposure_id(exposure_id: str) -> dict:
    match = EXPOSURE_RE.match(exposure_id)
    if not match:
        raise ValueError(f"노출 ID 형식이 예상과 다르다: {exposure_id}")
    parsed = match.groupdict()
    parsed["source_turn"] = int(parsed["source_turn"])
    parsed["delivery_turn"] = int(parsed["delivery_turn"])
    return parsed


def build_post_panel(conn, arm: str) -> pd.DataFrame:
    df = dbio.read_table(conn, "community_posts")
    df.insert(0, "arm", arm)
    for col in ("turn", "like_count", "unlike_count", "score", "is_best"):
        df[col] = pd.to_numeric(df[col])
    df["is_burnin"] = df["turn"].isin(paths.BURNIN_TURNS)
    return df


def build_exposure_panel(arm: str) -> pd.DataFrame:
    path = paths.csv_path(arm, "community_interactions.csv")
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path, encoding="utf-8-sig", low_memory=False)
    df.insert(0, "arm", arm)
    if not set(df["exposure_level"].dropna()) <= {"title_only", "full_body"}:
        raise AssertionError(
            f"예상치 못한 exposure_level: {set(df['exposure_level'])}")
    df["is_burnin"] = df["turn"].isin(paths.BURNIN_TURNS)
    return df


def resolve_delivery_turn(log_turn: int, exposures: list) -> int:
    """claim이 소비된 turn을 정한다.

    community_logs.turn은 글이 올라온 PM turn이고, claim은 다음 AM에 소비된다.
    claim_id에 박히는 turn은 후자다. 노출 ID 끝의 delivered_t<N>이 정본이고,
    노출이 하나도 없는 claim만 log_turn + 1로 되돌린다.
    실측(2026-08-03): 두 방식 모두 15,536건 전부 일치했다.
    """
    turns = {e["delivery_turn"] for e in exposures}
    if len(turns) > 1:
        raise AssertionError(f"한 claim의 배달 turn이 갈린다: {sorted(turns)}")
    return turns.pop() if turns else int(log_turn) + 1


def build_claim_panel(conn, arm: str) -> pd.DataFrame:
    rows = []
    for agent_id, turn, thinking in conn.execute(
            "select agent_id, turn, community_thinking from community_logs"):
        if not thinking:
            continue
        payload = json.loads(thinking)
        for index, claim in enumerate(payload.get("claims") or [], start=1):
            exposures = [parse_exposure_id(e)
                         for e in claim.get("source_exposure_ids") or []]
            delivery_turn = resolve_delivery_turn(int(turn), exposures)
            rows.append({
                "arm": arm,
                "reader_agent_id": agent_id,
                "log_turn": int(turn),
                "delivery_turn": delivery_turn,
                "claim_index": index,
                "claim_id": (f"community_claim:{agent_id}"
                             f":t{delivery_turn:03d}:{index:02d}"),
                "claim_text": claim.get("claim_text"),
                "claim_stance": claim.get("claim_stance"),
                "supporting_quote": claim.get("supporting_quote"),
                "supporting_quote_ref": claim.get("supporting_quote_ref"),
                "source_post_ids": [e["post_id"] for e in exposures],
                "source_relations": sorted({e["relation"] for e in exposures}),
            })
    return pd.DataFrame(rows)


def main() -> None:
    paths.PANELS.mkdir(parents=True, exist_ok=True)
    posts, exposures, claims = [], [], []
    for arm in paths.ARMS:
        conn = dbio.connect(paths.db_path(arm))
        posts.append(build_post_panel(conn, arm))
        exposures.append(build_exposure_panel(arm))
        claims.append(build_claim_panel(conn, arm))
    post_panel = pd.concat(posts, ignore_index=True)
    exposure_panel = pd.concat([e for e in exposures if len(e)], ignore_index=True)
    claim_panel = pd.concat([c for c in claims if len(c)], ignore_index=True)

    # 인용 여부 표시: evidence_panel의 community_claim ID와 대조
    ev = pd.read_parquet(paths.PANELS / "evidence_panel.parquet")
    cited = set(ev.loc[ev["kind"] == "community_claim", "evidence_id"])
    claim_panel["cited"] = claim_panel["claim_id"].isin(cited)

    post_panel.to_parquet(paths.PANELS / "post_panel.parquet", index=False)
    exposure_panel.to_parquet(paths.PANELS / "exposure_panel.parquet", index=False)
    claim_panel.to_parquet(paths.PANELS / "claim_panel.parquet", index=False)
    print(f"post {len(post_panel):,} / exposure {len(exposure_panel):,} "
          f"/ claim {len(claim_panel):,} (인용됨 {claim_panel['cited'].sum():,})")


if __name__ == "__main__":
    main()
