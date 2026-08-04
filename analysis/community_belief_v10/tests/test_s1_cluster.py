import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import s1_cluster


def _toy_corpus():
    return pd.DataFrame([
        {"doc_id": "n1", "doc_type": "news", "date": "2026-03-02", "cluster": 0},
        {"doc_id": "n2", "doc_type": "news", "date": "2026-03-02", "cluster": 0},
        {"doc_id": "p1", "doc_type": "post", "date": "2026-03-03", "cluster": 0},
        {"doc_id": "p2", "doc_type": "post", "date": "2026-03-05", "cluster": 0},
        {"doc_id": "p3", "doc_type": "post", "date": "2026-03-05", "cluster": 1},
    ])


def test_echo_ratio_counts_news_and_posts():
    out = s1_cluster.echo_ratio(_toy_corpus()).set_index("cluster")
    assert out.loc[0, "n_news"] == 2
    assert out.loc[0, "n_post"] == 2
    assert out.loc[0, "echo_ratio"] == 1.0
    # 뉴스가 없는 클러스터는 커뮤니티 고유 화제(novel)다
    assert out.loc[1, "n_news"] == 0
    assert bool(out.loc[1, "is_novel"])


def test_transfer_lag_measures_days_from_first_news():
    out = s1_cluster.transfer_lag(_toy_corpus()).set_index("cluster")
    assert out.loc[0, "median_lag_days"] == 2.0  # 03-03, 03-05 → median 1,3 = 2


def test_fit_returns_labels_and_centroids():
    rng = np.random.default_rng(0)
    vectors = np.vstack([rng.normal(1, 0.01, (20, 8)), rng.normal(-1, 0.01, (20, 8))])
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    labels, centroids = s1_cluster.fit(vectors, k=2, seed=0)
    assert labels.shape == (40,)
    assert centroids.shape == (2, 8)
    assert len(set(labels)) == 2


def test_remove_style_axis_collapses_the_group_difference():
    """두 집단이 한 방향으로 떨어져 있으면, 그 방향을 제거한 뒤
    두 집단의 평균이 그 방향 위에서 같아져야 한다."""
    rng = np.random.default_rng(7)
    topic = rng.normal(size=(40, 6))                 # 두 집단이 공유하는 화제 변동
    style = np.zeros(6)
    style[0] = 3.0                                   # 0번 축이 문체 축
    vectors = np.vstack([topic[:20] + style, topic[20:] - style])
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    doc_type = np.array(["news"] * 20 + ["post"] * 20)

    corrected = s1_cluster.remove_style_axis(vectors, doc_type)

    axis = vectors[doc_type == "news"].mean(0) - vectors[doc_type == "post"].mean(0)
    axis /= np.linalg.norm(axis)
    news_projection = (corrected[doc_type == "news"] @ axis).mean()
    post_projection = (corrected[doc_type == "post"] @ axis).mean()
    assert abs(news_projection - post_projection) < 1e-9


def test_remove_style_axis_keeps_unit_norm():
    rng = np.random.default_rng(8)
    vectors = rng.normal(size=(10, 5))
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    doc_type = np.array(["news"] * 5 + ["post"] * 5)
    corrected = s1_cluster.remove_style_axis(vectors, doc_type)
    assert np.allclose(np.linalg.norm(corrected, axis=1), 1.0, atol=1e-9)


def test_remove_style_axis_raises_when_axis_is_zero():
    """뉴스와 게시글의 평균이 동일하면 문체 축이 영벡터라 방향을 정할 수 없다."""
    rng = np.random.default_rng(11)
    base = rng.normal(size=(4, 5))
    base /= np.linalg.norm(base, axis=1, keepdims=True)
    vectors = np.vstack([base, base])  # news와 post의 평균이 완전히 같다
    doc_type = np.array(["news"] * 4 + ["post"] * 4)
    with pytest.raises(AssertionError, match="문체 축이 영벡터"):
        s1_cluster.remove_style_axis(vectors, doc_type)


def test_remove_style_axis_raises_when_a_vector_collapses():
    """문체 축과 정확히 나란한 문서는 투영 제거 후 영벡터가 된다."""
    news = np.array([
        [1.0, 0.0, 0.0],   # 문체 축과 정확히 나란함 -> 투영 제거 후 영벡터
        [1.0, 1.0, 0.0],
        [1.0, -1.0, 0.0],
    ])
    post = np.array([
        [-1.0, 0.0, 0.0],  # 이 벡터도 문체 축과 나란함 -> 역시 영벡터가 된다
        [-1.0, 1.0, 0.0],
        [-1.0, -1.0, 0.0],
    ])
    vectors = np.vstack([news, post])
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    doc_type = np.array(["news"] * 3 + ["post"] * 3)
    with pytest.raises(AssertionError, match="제거 후 영벡터"):
        s1_cluster.remove_style_axis(vectors, doc_type)


def test_mixed_cluster_share_counts_clusters_holding_both_types():
    corpus = pd.DataFrame([
        {"cluster": 0, "doc_type": "news"},
        {"cluster": 0, "doc_type": "post"},   # 혼합
        {"cluster": 1, "doc_type": "post"},   # 게시글만
        {"cluster": 2, "doc_type": "news"},   # 뉴스만
    ])
    assert s1_cluster.mixed_cluster_share(corpus) == 1 / 3


def test_plot_diagnostics_writes_four_figures(tmp_path, monkeypatch):
    """진단 그림 4종이 실제로 파일로 떨어지는지 확인한다.

    그림의 내용은 자동 검증하지 않는다(사람이 보는 물건이다). 다만 '그렸다고
    보고했는데 파일이 없다'는 조용한 실패는 막는다.
    """
    import s1_cluster as module

    monkeypatch.setattr(module.paths, "FIGURES", tmp_path)
    rng = np.random.default_rng(2)
    vectors = rng.normal(size=(12, 5))
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    corpus = pd.DataFrame({
        "cluster": [0] * 6 + [1] * 6,
        "doc_type": ["news"] * 6 + ["post"] * 6,
        "date": ["2026-03-02"] * 6 + ["2026-03-05"] * 6,
    })
    k_table = pd.DataFrame({"k": [2, 3], "silhouette": [0.4, 0.3],
                            "ari_stability": [0.9, 0.7]})
    written = module.plot_diagnostics(vectors, corpus, k_table)
    assert len(written) == 4
    for target in written:
        assert target.exists() and target.stat().st_size > 0
    assert {t.name for t in written} == {
        "s1_pca_by_cluster.png", "s1_pca_by_doctype.png",
        "s1_pca_by_date.png", "s1_k_selection.png"}


def test_original_space_diagnostics_writes_three_figures_and_real_numbers(
        tmp_path, monkeypatch):
    """원공간 진단 3종이 파일로 떨어지고, 수치가 실제 기하를 반영하는지 확인한다.

    잘 갈린 두 덩어리를 넣으면 분리도가 뚜렷하게 양수여야 하고,
    centroid 비대각 유사도는 1보다 확실히 낮아야 한다. 이 검사는
    함수가 상수를 뱉는 껍데기가 아님을 보장한다.
    """
    import s1_cluster as module

    monkeypatch.setattr(module.paths, "FIGURES", tmp_path)
    rng = np.random.default_rng(11)
    group_a = rng.normal(size=(30, 8)) * 0.05 + np.array([1, 0, 0, 0, 0, 0, 0, 0])
    group_b = rng.normal(size=(30, 8)) * 0.05 + np.array([0, 1, 0, 0, 0, 0, 0, 0])
    vectors = np.vstack([group_a, group_b])
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    labels = np.array([0] * 30 + [1] * 30)

    written, stats = module.original_space_diagnostics(vectors, labels)

    assert {t.name for t in written} == {
        "s1_orig_similarity_distribution.png",
        "s1_orig_silhouette_plot.png",
        "s1_orig_centroid_heatmap.png"}
    for target in written:
        assert target.exists() and target.stat().st_size > 0

    assert stats["separation"] > 0.5           # 뚜렷이 갈린 입력이므로
    assert stats["centroid_offdiag_mean"] < 0.5
    assert stats["negative_silhouette_share"] == 0.0


def test_choose_k_reports_stability_across_seeds():
    rng = np.random.default_rng(1)
    vectors = np.vstack([rng.normal(1, 0.01, (20, 8)), rng.normal(-1, 0.01, (20, 8))])
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    table = s1_cluster.choose_k(vectors, k_range=[2, 3], seeds=[0, 1, 2])
    assert set(table["k"]) == {2, 3}
    assert (table["ari_stability"] <= 1.0).all()
    assert table.loc[table["k"] == 2, "ari_stability"].iloc[0] > 0.9
