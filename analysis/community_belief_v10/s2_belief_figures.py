"""belief 변화 시각화 3종. 전부 임의 차원축소 없이 그린다.

왜 산점도가 아닌가:
  raw belief '위치'를 2~3차원에 투영하면 S1 코퍼스와 같은 함정에 빠진다
  (PCA-3D는 분산 8.5%만 담고, 그 안에서 좋아 보이는 군집이 원공간에서는
  silhouette 음수였다). 그러나 이 연구가 보고 싶은 것은 위치가 아니라 **변화**이고,
  변화는 투영 없이 스칼라로 잴 수 있다.

세 그림:
  1) arm_divergence  — 같은 (agent, turn, layer, dim)에서 ON belief와 OFF belief
     사이의 cosine 거리. 두 arm은 seed·뉴스·cohort·prompt가 동일하고
     community_mode만 다르므로, 이 거리가 곧 커뮤니티가 만든 차이다. 투영 없음.
  2) stance_axis     — bullish claim 평균 − bearish claim 평균으로 만든 단일 축.
     연구자가 고른 축이 아니라 데이터가 스스로 라벨한 축이다(claim_stance).
     belief를 여기 투영하면 "얼마나 강세 쪽인가"라는 해석 가능한 스칼라가 된다.
  3) change heatmap  — agent × turn 격자에 Δ_semantic. 세로 줄무늬면 뉴스 충격에
     동조한 것이고, 흩어져 있으면 개인차다. 투영 없음.

그림 안 글자는 영어로 쓴다(matplotlib 기본 폰트에 한글 글리프 없음).
"""

import numpy as np
import pandas as pd

import guard  # noqa: F401

guard.enforce_no_paid_api(offline=True)

import paths

PAIR_KEYS = ["agent_id", "turn", "layer", "dim"]


def arm_divergence(panel: pd.DataFrame, vectors: np.ndarray) -> pd.DataFrame:
    work = panel.reset_index(drop=True).copy()
    work["_row"] = np.arange(len(work))
    on = work[work["arm"] == "RN_COMM_ON"].set_index(PAIR_KEYS)
    off = work[work["arm"] == "RN_COMM_OFF"].set_index(PAIR_KEYS)
    if set(on.index) != set(off.index):
        raise AssertionError(
            f"ON/OFF 짝이 맞지 않는다 — ON만 {len(set(on.index) - set(off.index))}, "
            f"OFF만 {len(set(off.index) - set(on.index))}")
    off = off.loc[on.index]
    similarity = np.einsum("ij,ij->i",
                           vectors[on["_row"].to_numpy()],
                           vectors[off["_row"].to_numpy()])
    out = on.reset_index()[PAIR_KEYS].copy()
    out["divergence"] = 1.0 - similarity
    return out


def stance_axis(claims: pd.DataFrame, vectors: np.ndarray) -> np.ndarray:
    bullish = claims["claim_stance"].to_numpy() == "bullish"
    bearish = claims["claim_stance"].to_numpy() == "bearish"
    if not bullish.any() or not bearish.any():
        raise AssertionError(
            "stance 축을 만들 수 없다 — bullish 또는 bearish claim이 없다")
    axis = vectors[bullish].mean(axis=0) - vectors[bearish].mean(axis=0)
    norm = float(np.linalg.norm(axis))
    if norm == 0.0:
        raise AssertionError("stance 축이 영벡터다")
    return axis / norm


def project_on_stance(panel: pd.DataFrame, vectors: np.ndarray,
                      axis: np.ndarray) -> pd.DataFrame:
    out = panel.reset_index(drop=True).copy()
    out["stance_score"] = vectors @ axis
    return out


def plot_all(divergence: pd.DataFrame, stance: pd.DataFrame,
             movement: pd.DataFrame) -> list:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    paths.FIGURES.mkdir(parents=True, exist_ok=True)
    written = []

    # 1) ON/OFF 짝 발산 곡선 — 차원별
    figure, axis_obj = plt.subplots(figsize=(9, 5.5))
    for (layer, dim), group in divergence.groupby(["layer", "dim"]):
        series = group.groupby("turn")["divergence"].mean().sort_index()
        axis_obj.plot(series.index, series.values, marker="", linewidth=1.4,
                      label=f"{layer}/{dim}")
    axis_obj.set_xlabel("turn (AM/PM events, 1-90)")
    axis_obj.set_ylabel("cosine distance between ON and OFF belief")
    axis_obj.set_title("Paired ON/OFF belief divergence "
                       "(same agent, same turn, same news)")
    axis_obj.legend(fontsize=8, ncol=2)
    axis_obj.grid(alpha=0.3)
    figure.tight_layout()
    target = paths.FIGURES / "s2_arm_divergence.png"
    figure.savefig(target, dpi=150); plt.close(figure); written.append(target)

    # 2) 강세-약세 축 타임라인 — arm별
    figure, axis_obj = plt.subplots(figsize=(9, 5.5))
    for arm, group in stance.groupby("arm"):
        series = group.groupby("date")["stance_score"].mean().sort_index()
        axis_obj.plot(range(len(series)), series.values, linewidth=1.6, label=arm)
    axis_obj.axhline(0.0, color="black", linewidth=0.8)
    axis_obj.set_xlabel("trading day")
    axis_obj.set_ylabel("projection on bullish(+) / bearish(-) axis")
    axis_obj.set_title("Belief position on the community stance axis")
    axis_obj.legend()
    axis_obj.grid(alpha=0.3)
    figure.tight_layout()
    target = paths.FIGURES / "s2_stance_axis_timeline.png"
    figure.savefig(target, dpi=150); plt.close(figure); written.append(target)

    # 3) agent × turn 변화량 히트맵 — arm 나란히
    arms = sorted(movement["arm"].unique())
    figure, axes = plt.subplots(1, len(arms), figsize=(7 * len(arms), 6),
                                squeeze=False)
    for index, arm in enumerate(arms):
        grid = (movement[movement["arm"] == arm]
                .pivot_table(index="agent_id", columns="turn",
                             values="delta_semantic", aggfunc="mean"))
        image = axes[0][index].imshow(grid.values, aspect="auto", cmap="magma")
        axes[0][index].set_title(f"{arm} — belief change per agent-turn")
        axes[0][index].set_xlabel("turn")
        axes[0][index].set_ylabel("agent")
        figure.colorbar(image, ax=axes[0][index])
    figure.tight_layout()
    target = paths.FIGURES / "s2_change_heatmap.png"
    figure.savefig(target, dpi=150); plt.close(figure); written.append(target)
    return written


def main() -> None:
    import embed

    panel = pd.read_parquet(paths.PANELS / "belief_panel.parquet")
    vectors = embed.encode(panel["text"].fillna("").tolist(), "s2_beliefs")

    divergence = arm_divergence(panel, vectors)
    divergence.to_parquet(paths.PANELS / "arm_divergence.parquet", index=False)

    claims = pd.read_parquet(paths.PANELS / "claim_panel.parquet")
    claim_vectors = embed.encode(claims["claim_text"].fillna("").tolist(),
                                 "s2_claims")
    axis = stance_axis(claims, claim_vectors)
    stance = project_on_stance(panel, vectors, axis)
    stance.to_parquet(paths.PANELS / "stance_projection.parquet", index=False)

    movement = pd.read_parquet(paths.PANELS / "movement_panel.parquet")
    written = plot_all(divergence, stance,
                       movement[movement["delta_semantic"].notna()])

    print(f"belief 변화 그림 {len(written)}장 저장:")
    for target in written:
        print(f"  {target}")
    print("\n=== 차원별 평균 ON/OFF 발산 (burn-in 제외) ===")
    main_window = divergence[~divergence["turn"].isin(paths.BURNIN_TURNS)]
    print(main_window.groupby(["layer", "dim"])["divergence"]
          .agg(["mean", "std"]).to_string())


if __name__ == "__main__":
    main()
