"""S2 1층: 인용 원장 집계. 임베딩 없이도 확정적으로 나오는 경로다.

커뮤니티 claim은 번호 기반 인용이라 supporting_quote가 구조적으로 verbatim이고
(EXPERIMENT_DESIGN.md §8.5), dimension_evidence_json에 claim ID가 남는다.
따라서 "어느 토픽의 글이 어느 belief 차원에 들어갔는가"는 추정이 아니라 사실이다.

title_only 노출은 규정상 claim·STB 근거로 쓸 수 없으므로(§8.4) 애초에
evidence_panel에 나타나지 않는다.
"""

import pandas as pd

import guard  # noqa: F401

guard.enforce_no_paid_api(offline=True)

import paths


def attach_labels(evidence: pd.DataFrame, claims: pd.DataFrame,
                  corpus: pd.DataFrame, news: pd.DataFrame) -> pd.DataFrame:
    """근거 하나하나에 라벨을 붙인다.

    ⚠️ 2026-08-03 설계 변경 — 발견된 클러스터를 주축에서 내렸다.

    S1의 k-means 토픽 군집은 이 코퍼스에 **존재하지 않는다**. 모든 시도에서
    silhouette이 0.03~0.07 구간(=구조 없음)에 머물렀다:
      공동 군집 보정 전 0.040 / 보정 후 0.027 / 뉴스만 0.045~0.074 /
      게시글만 0.045~0.051 / 게시글 post_type 라벨 -0.021(음수)
    원인은 데이터의 성질이다. 게시글 3,150건 중 2,906건(92.2%)이 `trade_share`
    한 종류로 "오늘 샀다/팔았다 + 이유"이고, 45거래일간 단일 종목 텍스트라
    화제가 갈릴 재료가 없다. 게시글의 실제 변이는 화제가 아니라 방향과 근거다.

    그래서 주축을 **봉인 데이터에 이미 있는 라벨**로 바꾼다. 연구자가 구조를
    발명하지 않는다는 점이 이 선택의 핵심이다.

      community_claim          → claim_stance (bullish / bearish / neutral)
      news, depth2_recent_search → 뉴스 카테고리 (종목 / 섹터 / 경제)
      outcome                  → "outcome" (성과 피드백에는 토픽 축이 없다)

    k-means `cluster`는 exploratory 열로 함께 남기되 주분석에 쓰지 않는다.
    클러스터링 실패 자체는 REPORT에 음성 결과로 보고한다.
    """
    post_cluster = dict(zip(corpus.loc[corpus["doc_type"] == "post", "post_id"],
                            corpus.loc[corpus["doc_type"] == "post", "cluster"]))
    news_cluster = dict(zip(corpus.loc[corpus["doc_type"] == "news", "article_id"],
                            corpus.loc[corpus["doc_type"] == "news", "cluster"]))
    claim_posts = dict(zip(claims["claim_id"], claims["source_post_ids"]))
    claim_stance = dict(zip(claims["claim_id"], claims["claim_stance"]))
    news_category = dict(zip(news["article_id"], news["category"]))

    def resolve(row):
        kind, evidence_id = row["kind"], row["evidence_id"]
        if kind == "community_claim":
            posts = claim_posts.get(evidence_id)
            # source_post_ids는 parquet 왕복 후 numpy 배열로 돌아온다.
            # `posts or []`는 원소가 2개 이상이면 "truth value of an array
            # with more than one element is ambiguous"로 죽는다(실측 확인,
            # claim의 66%가 출처 2건 이상). None 체크로 명시한다.
            if posts is None:
                posts = []
            clusters = [post_cluster[p] for p in posts if p in post_cluster]
            # 한 claim이 여러 글을 인용할 수 있다. 첫 출처를 대표로 쓰고
            # 다중 출처 여부는 별도 열로 남긴다.
            return (claim_stance.get(evidence_id, pd.NA),
                    clusters[0] if clusters else pd.NA, len(set(clusters)))
        if kind in ("news", "depth2_recent_search"):
            return (news_category.get(evidence_id, pd.NA),
                    news_cluster.get(evidence_id, pd.NA), 1)
        if kind == "outcome":
            return ("outcome", pd.NA, 0)
        return (pd.NA, pd.NA, 0)

    resolved = evidence.apply(resolve, axis=1, result_type="expand")
    out = evidence.copy()
    out["topic_label"] = resolved[0]     # 주축
    out["cluster"] = resolved[1]         # exploratory only
    out["n_source_clusters"] = resolved[2]
    return out


def label_by_dimension(linked: pd.DataFrame) -> pd.DataFrame:
    """주 산출물: 라벨 × belief 차원 × 관계 교차표.

    커뮤니티 쪽은 stance이므로 "bullish 주장이 어느 차원의 support로 들어갔나",
    뉴스 쪽은 카테고리이므로 "경제 기사가 dim_3에 몰리는가"를 바로 읽을 수 있다.
    """
    subset = linked[linked["topic_label"].notna()]
    return (subset.groupby(["arm", "layer", "topic_label", "dim", "relation", "kind"])
            .size().reset_index(name="n"))


def cluster_by_dimension(linked: pd.DataFrame) -> pd.DataFrame:
    """exploratory 전용. 군집 구조가 없다는 것이 확인됐으므로 주장에 쓰지 않는다."""
    subset = linked[linked["cluster"].notna()]
    return (subset.groupby(["arm", "layer", "cluster", "dim", "relation", "kind"])
            .size().reset_index(name="n"))


def community_share(linked: pd.DataFrame) -> pd.DataFrame:
    total = linked.groupby(["arm", "layer", "dim"]).size().rename("n_total")
    community = (linked[linked["kind"] == "community_claim"]
                 .groupby(["arm", "layer", "dim"]).size().rename("n_community"))
    out = pd.concat([total, community], axis=1).fillna(0).reset_index()
    out["community_share"] = out["n_community"] / out["n_total"]
    return out


def main() -> None:
    evidence = pd.read_parquet(paths.PANELS / "evidence_panel.parquet")
    claims = pd.read_parquet(paths.PANELS / "claim_panel.parquet")
    corpus = pd.read_parquet(paths.PANELS / "corpus_clusters.parquet")
    news = pd.read_parquet(paths.PANELS / "news_panel.parquet")

    linked = attach_labels(evidence, claims, corpus, news)

    # 라벨이 반드시 붙어야 하는 kind에서 하나라도 비면 조인이 깨진 것이다.
    # depth2_recent_search도 포함한다 — 실측(2026-08-03) 결과 D2 검색이 반환한
    # article_id는 ON 377개·OFF 394개 모두 봉인 뉴스 760개 안에 있다.
    labelled_kinds = ["news", "community_claim", "depth2_recent_search"]
    unlabelled = linked[linked["kind"].isin(labelled_kinds)
                        & linked["topic_label"].isna()]
    if len(unlabelled):
        raise AssertionError(
            f"라벨 미할당 근거 {len(unlabelled)}건: "
            f"{unlabelled['evidence_id'].head(5).tolist()}")

    linked.to_parquet(paths.PANELS / "evidence_with_labels.parquet", index=False)
    label_by_dimension(linked).to_csv(paths.OUT / "s2_label_by_dimension.csv",
                                      index=False)
    cluster_by_dimension(linked).to_csv(
        paths.OUT / "s2_cluster_by_dimension_exploratory.csv", index=False)
    share = community_share(linked)
    share.to_csv(paths.OUT / "s2_community_share.csv", index=False)

    print("=== 커뮤니티 인용 비중 (ON arm, burn-in 포함) ===")
    print(share[share["arm"] == "RN_COMM_ON"].to_string(index=False))
    print("\n※ dim_4가 커뮤니티의 주 수신 차원이라는 가설의 직접 검증 지점")

    on_claims = linked[(linked["arm"] == "RN_COMM_ON")
                       & (linked["kind"] == "community_claim")]
    print("\n=== stance × 차원 × 관계 (커뮤니티 주장이 어디로 갔는가) ===")
    print(pd.crosstab([on_claims["topic_label"], on_claims["relation"]],
                      [on_claims["layer"], on_claims["dim"]]).to_string())

    news_rows = linked[linked["kind"].isin(["news", "depth2_recent_search"])]
    print("\n=== 뉴스 카테고리 × 차원 (arm별) ===")
    print(pd.crosstab([news_rows["arm"], news_rows["topic_label"]],
                      [news_rows["layer"], news_rows["dim"]]).to_string())


if __name__ == "__main__":
    main()
