"""S1: 봉인 뉴스 760 + 커뮤니티 글 3,150을 하나의 임베딩 공간에서 군집화한다.

비지도 군집은 가설 생성용이다(embedding_analysis_plan.md). 그래서 토픽 라벨뿐
아니라 날짜·doc_type·카테고리 라벨에 대한 silhouette도 함께 낸다. 클러스터가
화제가 아니라 시장 국면이나 날짜로 갈렸을 가능성을 독자가 판단할 수 있어야 한다.

라벨 이름은 사람이 붙인다. LLM 보조는 유료이므로 쓰지 않는다.
"""

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.metrics import adjusted_rand_score, silhouette_score

import guard  # noqa: F401

guard.enforce_no_paid_api(offline=True)

import embed
import paths

K_RANGE = list(range(6, 25, 2))
SEEDS = [0, 1, 2, 3, 4]


def build_corpus() -> pd.DataFrame:
    news = pd.read_parquet(paths.PANELS / "news_panel.parquet")
    posts = pd.read_parquet(paths.PANELS / "post_panel.parquet")
    posts = posts[posts["arm"] == "RN_COMM_ON"]

    news_docs = pd.DataFrame({
        "doc_id": "news:" + news["article_id"],
        "doc_type": "news",
        "date": news["date"],
        "turn": np.nan,
        "text": news["title"].fillna("") + "\n" + news["summary"].fillna(""),
        "category": news["category"],
        "post_type": pd.NA,
        "post_id": pd.NA,
        "article_id": news["article_id"],
    }).drop_duplicates("doc_id")

    post_docs = pd.DataFrame({
        "doc_id": "post:" + posts["post_id"].astype(str),
        "doc_type": "post",
        "date": posts["date"],
        "turn": posts["turn"],
        "text": posts["title"].fillna("") + "\n" + posts["content"].fillna(""),
        "category": pd.NA,
        "post_type": posts["post_type"],
        "post_id": posts["post_id"].astype(str),
        "article_id": pd.NA,
    })
    return pd.concat([news_docs, post_docs], ignore_index=True)


def fit(vectors: np.ndarray, k: int, seed: int):
    model = KMeans(n_clusters=k, random_state=seed, n_init=10)
    labels = model.fit_predict(vectors)
    return labels, model.cluster_centers_


def choose_k(vectors: np.ndarray, k_range=None, seeds=None) -> pd.DataFrame:
    k_range = list(k_range if k_range is not None else K_RANGE)
    seeds = list(seeds if seeds is not None else SEEDS)
    rows = []
    for k in k_range:
        runs = [fit(vectors, k, seed)[0] for seed in seeds]
        pairs = [adjusted_rand_score(runs[i], runs[j])
                 for i in range(len(runs)) for j in range(i + 1, len(runs))]
        rows.append({
            "k": k,
            "silhouette": float(silhouette_score(vectors, runs[0], metric="cosine")),
            "ari_stability": float(np.mean(pairs)) if pairs else 1.0,
        })
    return pd.DataFrame(rows)


def label_silhouettes(vectors: np.ndarray, corpus: pd.DataFrame) -> dict:
    """토픽이 아니라 날짜·문서종류로 갈렸을 가능성을 정량화한다."""
    out = {}
    for column in ("cluster", "date", "doc_type"):
        values = corpus[column].astype(str).values
        if len(set(values)) < 2:
            continue
        out[column] = float(silhouette_score(vectors, values, metric="cosine"))
    return out


def remove_style_axis(vectors: np.ndarray, doc_type: np.ndarray) -> np.ndarray:
    """뉴스와 게시글을 가르는 단일 방향 하나를 직교 제거한다.

    왜 필요한가 (2026-08-03 1차 실행 실측, k=18):
      doc_type silhouette 0.374  ≫  cluster 0.040  >  date 0.014
    임베딩 공간의 지배축이 화제가 아니라 문체였다. 뉴스는 헤드라인+기사체 요약,
    게시글은 1인칭 개인투자자 문장이다. 그 결과 18개 클러스터가 **전부** 순수
    뉴스이거나 순수 게시글이 되었고, echo_ratio와 transfer_lag이 전부 NaN이 됐다.
    "같은 화제를 기자가 말할 때와 개미가 말할 때"를 비교한다는 S1의 목적이
    성립하지 않는 상태였다.

    왜 그룹 평균을 통째로 빼지 않는가:
      각 집단의 평균 벡터를 각각 빼면 두 centroid가 원점에서 만나 문체 차이는
      확실히 사라진다. 그러나 그 방법은 "뉴스와 게시글이 원래 다른 화제를 다룬다"는
      **진짜 차이까지** 함께 지운다. 그래서 여기서는 두 평균의 차이 방향 하나만
      제거하는 보수적인 방법을 쓴다.

    남는 한계 (REPORT에 반드시 명시):
      단일 방향 제거도 '문체'와 '평균 화제차'를 완전히 분리하지 못한다. 이 축에
      실려 있던 진짜 화제 차이도 일부 함께 제거된다. 따라서 제거 전후 지표를
      모두 보고하고, 결론이 이 보정에 의존하는지 밝힌다.
    """
    news_mean = vectors[doc_type == "news"].mean(axis=0)
    post_mean = vectors[doc_type == "post"].mean(axis=0)
    axis = news_mean - post_mean
    norm = float(np.linalg.norm(axis))
    if norm == 0.0:
        raise AssertionError("문체 축이 영벡터다 — 두 집단의 평균이 동일하다")
    axis = axis / norm
    corrected = vectors - np.outer(vectors @ axis, axis)
    norms = np.linalg.norm(corrected, axis=1, keepdims=True)
    if float(norms.min()) == 0.0:
        raise AssertionError("문체 축 제거 후 영벡터가 생겼다")
    return corrected / norms


def mixed_cluster_share(corpus: pd.DataFrame) -> float:
    """뉴스와 게시글이 함께 들어 있는 클러스터의 비율.

    문체 축 제거가 성공했는지 판정하는 기준이다. 1차 실행에서는 0.0이었다
    (18개 클러스터 전부 한쪽 종류만). 이 값이 0에 가까우면 echo_ratio와
    transfer_lag은 계산해봐야 NaN이므로, 그 사실을 먼저 보고한다.
    """
    counts = (corpus.groupby(["cluster", "doc_type"]).size()
              .unstack(fill_value=0)
              .reindex(columns=["news", "post"], fill_value=0))
    mixed = int(((counts["news"] > 0) & (counts["post"] > 0)).sum())
    return mixed / len(counts)


def plot_diagnostics(vectors: np.ndarray, corpus: pd.DataFrame,
                     k_table: pd.DataFrame) -> list:
    """진단용 그림 4종. 주장의 근거가 아니라 진단 도구다.

    embedding_analysis_plan.md는 PCA/UMAP 지도를 '탐색 전용, 우선순위 4~5'로
    못박았다. 그래서 이 그림들은 결과 제시가 아니라 다음 질문에 답하기 위해 있다:
      - by_cluster: 토픽 지도 (군집이 실제로 분리되는가)
      - by_doctype: 뉴스와 게시글이 같은 자리에 겹치는가 (echo) 아니면 갈라지는가 (novel)
      - by_date:    ⚠️ 군집이 화제가 아니라 '시점'으로 갈렸는지 눈으로 판별.
                    label_silhouettes()의 날짜 점수와 반드시 함께 볼 것.
      - k_selection: silhouette과 시드 안정성이 어디서 만나는가

    그림 안의 글자는 영어로 쓴다. matplotlib 기본 폰트에 한글 글리프가 없어
    한글 라벨은 두부(□)로 깨진다. 해석은 REPORT.md의 한국어 본문이 담당한다.
    """
    import matplotlib
    matplotlib.use("Agg")          # 화면 없는 환경에서도 저장되도록
    import matplotlib.pyplot as plt
    from sklearn.decomposition import PCA

    paths.FIGURES.mkdir(parents=True, exist_ok=True)
    coords = PCA(n_components=2, random_state=0).fit_transform(vectors)
    written = []

    def scatter(values, title, filename, discrete):
        figure, axis = plt.subplots(figsize=(8, 7))
        if discrete:
            for value in sorted(pd.unique(values)):
                mask = values == value
                axis.scatter(coords[mask, 0], coords[mask, 1], s=6, alpha=0.5,
                             label=str(value))
            axis.legend(markerscale=3, fontsize=8, loc="best")
        else:
            mappable = axis.scatter(coords[:, 0], coords[:, 1], c=values, s=6,
                                    alpha=0.5, cmap="viridis")
            figure.colorbar(mappable, ax=axis, label="days since first article")
        axis.set_title(title)
        axis.set_xlabel("PC1")
        axis.set_ylabel("PC2")
        figure.tight_layout()
        target = paths.FIGURES / filename
        figure.savefig(target, dpi=150)
        plt.close(figure)
        written.append(target)

    scatter(corpus["cluster"].to_numpy(),
            "News + posts by cluster (topic map)",
            "s1_pca_by_cluster.png", True)
    scatter(corpus["doc_type"].to_numpy(),
            "News vs community posts (echo or novel)",
            "s1_pca_by_doctype.png", True)
    dates = pd.to_datetime(corpus["date"])
    scatter((dates - dates.min()).dt.days.to_numpy(),
            "By date - are clusters topics or just time periods?",
            "s1_pca_by_date.png", False)

    figure, axis = plt.subplots(figsize=(7, 5))
    axis.plot(k_table["k"], k_table["silhouette"], marker="o", label="silhouette")
    axis.plot(k_table["k"], k_table["ari_stability"], marker="s",
              label="seed stability (ARI)")
    axis.set_xlabel("k")
    axis.set_ylabel("score")
    axis.set_title("Cluster count selection")
    axis.legend()
    axis.grid(alpha=0.3)
    figure.tight_layout()
    target = paths.FIGURES / "s1_k_selection.png"
    figure.savefig(target, dpi=150)
    plt.close(figure)
    written.append(target)
    return written


def original_space_diagnostics(vectors: np.ndarray, labels: np.ndarray) -> tuple:
    """투영 없이 원공간(384차원) 기하를 보여주는 그림 3종과 그 수치.

    왜 산점도가 아닌가 (2026-08-03 실측):
      384차원을 종이에 그리려면 어떤 방법을 쓰든 투영해야 한다. PCA-3D는 분산의
      8.5%만 담는데 그 안에서 silhouette 0.248이 나온다 — 같은 라벨을 원공간에서
      다시 채점하면 **-0.015(음수)** 다. 차원을 올릴수록 이 착시분은 줄어든다
      (2D 0.341 → 50D 0.026). t-SNE·UMAP은 순수 노이즈에서도 덩어리를 만들어
      내므로 더 나쁘다. 그래서 여기서는 '그리지' 않고 '재서' 보여준다.

    세 그림:
      1) 유사도 분포 — 같은 클러스터 쌍과 다른 클러스터 쌍의 cosine 분포를 겹쳐
         그린다. 두 곡선이 포개지면 경계가 없다는 뜻이다.
      2) silhouette plot — 점별 silhouette을 클러스터별로 정렬. 음수 구간이 보인다.
      3) centroid 유사도 히트맵 — 중심끼리 얼마나 비슷한가. 1에 가까우면 같은 점이다.

    이 셋은 '군집이 없다'를 증명하는 그림이므로 음성 결과 절에 그대로 쓸 수 있다.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from sklearn.metrics import silhouette_samples

    paths.FIGURES.mkdir(parents=True, exist_ok=True)
    written = []
    ordered = sorted(set(labels))

    # --- 1) within/between 유사도 분포 ---
    compact = vectors.astype(np.float32)
    similarity = compact @ compact.T
    upper = np.triu_indices(len(compact), 1)
    same = labels[upper[0]] == labels[upper[1]]
    pair_similarity = similarity[upper]
    within, between = pair_similarity[same], pair_similarity[~same]

    figure, axis = plt.subplots(figsize=(8, 5))
    bins = np.linspace(float(pair_similarity.min()), float(pair_similarity.max()), 120)
    axis.hist(between, bins=bins, density=True, alpha=0.55, label="different clusters")
    axis.hist(within, bins=bins, density=True, alpha=0.55, label="same cluster")
    axis.set_xlabel("cosine similarity (original 384-dim space, no projection)")
    axis.set_ylabel("density")
    axis.set_title(f"Within vs between cluster similarity "
                   f"(gap = {within.mean() - between.mean():+.3f})")
    axis.legend()
    figure.tight_layout()
    target = paths.FIGURES / "s1_orig_similarity_distribution.png"
    figure.savefig(target, dpi=150); plt.close(figure); written.append(target)

    # --- 2) silhouette plot ---
    values = silhouette_samples(vectors, labels, metric="cosine")
    figure, axis = plt.subplots(figsize=(8, 6))
    offset = 0
    for cluster in ordered:
        group = np.sort(values[labels == cluster])
        axis.fill_betweenx(np.arange(offset, offset + len(group)), 0, group)
        axis.text(-0.02, offset + len(group) / 2, str(cluster),
                  va="center", ha="right", fontsize=8)
        offset += len(group) + 20
    axis.axvline(0.0, color="black", linewidth=0.8)
    axis.axvline(float(values.mean()), linestyle="--", linewidth=1,
                 label=f"mean = {values.mean():.3f}")
    axis.set_xlabel("silhouette (cosine, original space)")
    axis.set_ylabel("documents, grouped by cluster")
    axis.set_title(f"Per-document silhouette — {np.mean(values < 0):.1%} are negative")
    axis.legend()
    figure.tight_layout()
    target = paths.FIGURES / "s1_orig_silhouette_plot.png"
    figure.savefig(target, dpi=150); plt.close(figure); written.append(target)

    # --- 3) centroid 유사도 히트맵 ---
    centroids = np.vstack([vectors[labels == c].mean(axis=0) for c in ordered])
    centroids /= np.linalg.norm(centroids, axis=1, keepdims=True)
    matrix = centroids @ centroids.T
    figure, axis = plt.subplots(figsize=(6.5, 5.5))
    image = axis.imshow(matrix, vmin=float(matrix.min()), vmax=1.0, cmap="magma")
    figure.colorbar(image, ax=axis)
    for i in range(len(matrix)):
        for j in range(len(matrix)):
            axis.text(j, i, f"{matrix[i, j]:.2f}", ha="center", va="center",
                      fontsize=7, color="white")
    off_diagonal = matrix[~np.eye(len(matrix), dtype=bool)]
    axis.set_title(f"Cluster centroid similarity "
                   f"(off-diagonal mean = {off_diagonal.mean():.3f})")
    axis.set_xticks(range(len(ordered))); axis.set_yticks(range(len(ordered)))
    figure.tight_layout()
    target = paths.FIGURES / "s1_orig_centroid_heatmap.png"
    figure.savefig(target, dpi=150); plt.close(figure); written.append(target)

    stats = {
        "within_mean": float(within.mean()),
        "between_mean": float(between.mean()),
        "separation": float(within.mean() - between.mean()),
        "negative_silhouette_share": float(np.mean(values < 0)),
        "centroid_offdiag_mean": float(off_diagonal.mean()),
    }
    return written, stats


def echo_ratio(corpus: pd.DataFrame) -> pd.DataFrame:
    counts = (corpus.groupby(["cluster", "doc_type"]).size()
              .unstack(fill_value=0).reindex(columns=["news", "post"], fill_value=0))
    out = counts.rename(columns={"news": "n_news", "post": "n_post"}).reset_index()
    out["echo_ratio"] = out["n_post"] / out["n_news"].replace(0, np.nan)
    out["is_novel"] = out["n_news"] == 0
    return out


def transfer_lag(corpus: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for cluster, group in corpus.groupby("cluster"):
        news_dates = pd.to_datetime(group.loc[group["doc_type"] == "news", "date"])
        post_dates = pd.to_datetime(group.loc[group["doc_type"] == "post", "date"])
        if news_dates.empty or post_dates.empty:
            rows.append({"cluster": cluster, "median_lag_days": np.nan,
                         "n_post": len(post_dates)})
            continue
        first = news_dates.min()
        lags = (post_dates - first).dt.days
        rows.append({"cluster": cluster, "median_lag_days": float(lags.median()),
                     "n_post": len(post_dates)})
    return pd.DataFrame(rows)


def main() -> None:
    paths.FIGURES.mkdir(parents=True, exist_ok=True)
    corpus = build_corpus()
    raw_vectors = embed.encode(corpus["text"].tolist(), "s1_corpus")

    # --- 보정 전 상태를 먼저 기록한다 (결론이 보정에 의존하는지 밝히기 위해) ---
    raw_labels, _ = fit(raw_vectors, k=18, seed=0)
    before = corpus.assign(cluster=raw_labels)
    print("=== 문체 축 제거 전 (k=18 기준) ===")
    for name, score in label_silhouettes(raw_vectors, before).items():
        print(f"  {name}: {score:.3f}")
    print(f"  뉴스·게시글 혼합 클러스터 비율: {mixed_cluster_share(before):.1%}")

    # --- 문체 축 제거 ---
    vectors = remove_style_axis(raw_vectors, corpus["doc_type"].to_numpy())
    np.save(paths.PANELS / "corpus_vectors_style_removed.npy", vectors)

    table = choose_k(vectors)
    table.to_csv(paths.OUT / "s1_k_selection.csv", index=False)
    print("\n=== 문체 축 제거 후 k 선택 ===")
    print(table.to_string(index=False))

    # silhouette와 안정성을 동시에 만족하는 k를 고른다.
    # 자동 선택값은 제안일 뿐이며 사용자가 확정한다.
    table["score"] = table["silhouette"] * table["ari_stability"]
    k = int(table.loc[table["score"].idxmax(), "k"])
    print(f"\n제안 k = {k} (사용자 확정 필요)")

    labels, centroids = fit(vectors, k, seed=0)
    corpus["cluster"] = labels
    corpus.to_parquet(paths.PANELS / "corpus_clusters.parquet", index=False)
    np.save(paths.PANELS / "cluster_centroids.npy", centroids)

    diagnostics = label_silhouettes(vectors, corpus)
    print("\n=== 문체 축 제거 후 라벨별 silhouette ===")
    print("(날짜가 높으면 토픽이 아니라 시점으로 갈린 것,"
          " doc_type이 높으면 문체 제거가 부족한 것)")
    for name, score in diagnostics.items():
        print(f"  {name}: {score:.3f}")

    share = mixed_cluster_share(corpus)
    print(f"\n뉴스·게시글 혼합 클러스터 비율: {share:.1%}")
    if share == 0.0:
        print("  ⚠️ 혼합 클러스터가 하나도 없다. 아래 echo_ratio·transfer_lag은"
              " 구조적으로 전부 NaN이며 해석할 수 없다.")

    figures = plot_diagnostics(vectors, corpus, table)
    print(f"\n투영 기반 진단 그림 {len(figures)}장 저장:")
    for target in figures:
        print(f"  {target}")

    # 투영 없이 원공간 기하를 직접 재는 그림 3종. 산점도가 못 하는 일을 한다.
    original_figures, original_stats = original_space_diagnostics(
        vectors, corpus["cluster"].to_numpy())
    print(f"\n원공간 진단 그림 {len(original_figures)}장 저장:")
    for target in original_figures:
        print(f"  {target}")
    print("\n=== 원공간 기하 (투영 없음) ===")
    print(f"  같은 클러스터 평균 cosine : {original_stats['within_mean']:.3f}")
    print(f"  다른 클러스터 평균 cosine : {original_stats['between_mean']:.3f}")
    print(f"  분리도                    : {original_stats['separation']:+.3f}")
    print(f"  silhouette 음수인 점 비율 : {original_stats['negative_silhouette_share']:.1%}")
    print(f"  centroid 비대각 평균      : {original_stats['centroid_offdiag_mean']:.3f}")
    if original_stats["centroid_offdiag_mean"] > 0.9:
        print("  ⚠️ 클러스터 중심들이 서로 cosine 0.9 초과 — 사실상 한 덩어리다.")

    print("\n에코 비율:")
    print(echo_ratio(corpus).to_string(index=False))
    print("\n전이 지연:")
    print(transfer_lag(corpus).to_string(index=False))

    # 사람이 라벨을 채울 파일
    rows = []
    for cluster in sorted(corpus["cluster"].unique()):
        member_idx = np.where(labels == cluster)[0]
        distances = 1 - vectors[member_idx] @ centroids[cluster]
        for rank, position in enumerate(member_idx[np.argsort(distances)][:10], 1):
            rows.append({"cluster": cluster, "rank": rank,
                         "doc_type": corpus.iloc[position]["doc_type"],
                         "date": corpus.iloc[position]["date"],
                         "text": corpus.iloc[position]["text"][:200],
                         "label": ""})
    pd.DataFrame(rows).to_csv(paths.OUT / "cluster_labels.csv", index=False)
    print(f"\ncluster_labels.csv 생성 — 클러스터당 대표 10건. label 열을 채울 것")


if __name__ == "__main__":
    main()
