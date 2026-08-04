"""S2 2층: belief 텍스트의 이동량.

2026-07-31에 "여섯 차원을 매번 새 문장으로 재서술" 규칙이 제거되었으므로,
관점이 안 변한 차원은 이전 문장이 그대로 유지된다. 따라서 Δ≈0은 측정 노이즈가
아니라 진짜 무변화다 (ANALYSIS_FIELD_GUIDE.md §2-C). 이 성질이 이 분석의 근거다.

STB와 LTB는 층이 다르고, dim_6은 층 간 의미가 다르므로 절대 합치지 않는다.
"""

import numpy as np
import pandas as pd

import guard  # noqa: F401

guard.enforce_no_paid_api(offline=True)

import embed
import paths

GROUP_KEYS = ["arm", "agent_id", "layer", "dim"]


def _sorted_with_vectors(panel: pd.DataFrame, vectors: np.ndarray):
    """그룹키+turn으로 정렬하고, 같은 순서로 재배열한 벡터와
    '직전 행이 같은 그룹인가' 마스크를 함께 돌려준다."""
    work = panel.reset_index(drop=True).copy()
    work["_row"] = np.arange(len(work))
    work = work.sort_values(GROUP_KEYS + ["turn"]).reset_index(drop=True)
    ordered = vectors[work["_row"].to_numpy()]
    same_group = (work[GROUP_KEYS].shift().eq(work[GROUP_KEYS])
                  .all(axis=1).to_numpy())
    return work.drop(columns=["_row"]), ordered, same_group


def semantic_delta(panel: pd.DataFrame, vectors: np.ndarray) -> pd.DataFrame:
    work, ordered, same_group = _sorted_with_vectors(panel, vectors)
    cosine = np.full(len(work), np.nan)
    if len(work) > 1:
        cosine[1:] = np.einsum("ij,ij->i", ordered[1:], ordered[:-1])
    delta = 1.0 - cosine
    delta[~same_group] = np.nan          # 그룹의 첫 turn은 직전이 없다
    work["delta_semantic"] = delta
    work["prev_turn"] = work["turn"].shift().where(same_group)
    return work


def topic_delta(panel: pd.DataFrame, vectors: np.ndarray,
                centroids: np.ndarray) -> pd.DataFrame:
    work, ordered, same_group = _sorted_with_vectors(panel, vectors)
    similarity = ordered @ centroids.T          # (N, K)
    diff = np.full_like(similarity, np.nan)
    if len(work) > 1:
        diff[1:] = similarity[1:] - similarity[:-1]
    diff[~same_group] = np.nan
    wide = pd.DataFrame(diff, columns=list(range(centroids.shape[0])))
    keys = ["arm", "agent_id", "turn", "layer", "dim"]
    out = pd.concat([work[keys].reset_index(drop=True), wide], axis=1)
    out = out.melt(id_vars=keys, var_name="cluster", value_name="delta_topic")
    out["cluster"] = out["cluster"].astype(int)
    return out.dropna(subset=["delta_topic"]).reset_index(drop=True)


def main() -> None:
    panel = pd.read_parquet(paths.PANELS / "belief_panel.parquet")
    vectors = embed.encode(panel["text"].fillna("").tolist(), "s2_beliefs")
    centroids = np.load(paths.PANELS / "cluster_centroids.npy")

    movement = semantic_delta(panel, vectors)
    movement.to_parquet(paths.PANELS / "movement_panel.parquet", index=False)

    zero_rate = (movement["delta_semantic"].dropna() < 1e-9).mean()
    print(f"movement_panel {len(movement):,}행 | Δ=0(진짜 무변화) 비율 {zero_rate:.1%}")

    topic = topic_delta(panel, vectors, centroids)
    topic.to_parquet(paths.PANELS / "topic_movement_panel.parquet", index=False)
    print(f"topic_movement_panel {len(topic):,}행")


if __name__ == "__main__":
    main()
