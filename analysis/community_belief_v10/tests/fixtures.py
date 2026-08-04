"""실제 run DB와 같은 스키마의 소형 sqlite를 만든다.

실 DB(388MB)를 테스트에 쓰지 않기 위한 것이다. 컬럼 구성은
2026-08-03 실측 스키마를 그대로 따른다.
"""

import json
import sqlite3
from pathlib import Path

STB_COLS = ("stb_id, agent_id, turn, date, subturn, dim_1, dim_2, dim_3, dim_4, "
            "dim_5, dim_6, evidence_json, dimension_evidence_json, "
            "scientific_sha256, created_at")
LTB_COLS = ("ltb_id, agent_id, turn, visible_from_turn, date, subturn, parent_ltb_id, "
            "source_stb_id, source_decision_id, source_fill_id, dim_1, dim_2, dim_3, "
            "dim_4, dim_5, dim_6, integration_evidence_json, scientific_sha256, "
            "belief_summary, view_change_json, human_log_sha256, created_at")
DECISIONS_COLS = ("decision_id, agent_id, turn, date, subturn, action, requested_quantity, "
                   "source_ltb_id, source_stb_id, analysis_id, decision_json, "
                   "scientific_sha256, created_at")
FILLS_COLS = ("fill_id, agent_id, turn, date, subturn, stock_code, action, "
              "requested_quantity, filled_quantity, executed_price, fee, "
              "source_ltb_id, source_stb_id, decision_id, pre_portfolio_json, "
              "post_portfolio_json, scientific_sha256, created_at")


def _whitelist(news_ids, claim_ids=(), search_ids=()):
    return json.dumps({
        "news": [{"evidence_id": i, "kind": "news", "title": f"t-{i}",
                  "published_at": "2026-03-06T09:00:00+09:00",
                  "payload_sha256": "x"} for i in news_ids],
        "community_claims": [{"evidence_id": i, "kind": "community_claim",
                              "claim_text": f"claim-{i}", "stance": "bullish",
                              "supporting_quote": f"quote-{i}",
                              "source_exposure_ids": [
                                  "community:2026-03-03:t4:post:73:best_full_body:A001:delivered_t5"]}
                             for i in claim_ids],
        "depth2_search_results": [{"evidence_id": i, "kind": "depth2_recent_search",
                                   "title": f"s-{i}", "summary": "s",
                                   "published_at": "2026-02-27T16:00:00+09:00",
                                   "payload_sha256": "y"} for i in search_ids],
    }, ensure_ascii=False)


def _dim_ev(mapping):
    """mapping: {"dim_1": (support_ids, contradict_ids), ...}"""
    out = {}
    for i in range(1, 7):
        key = f"dim_{i}"
        support, contradict = mapping.get(key, ([], []))
        out[key] = {"support": list(support), "contradict": list(contradict)}
    return json.dumps(out, ensure_ascii=False)


def make_run_db(tmp_path: Path, arm: str, *, with_community: bool) -> Path:
    db = tmp_path / f"{arm}.db"
    conn = sqlite3.connect(db)
    conn.execute(f"create table simulation_stb_states ({STB_COLS})")
    conn.execute(f"create table simulation_ltb_states ({LTB_COLS})")
    conn.execute(f"create table simulation_decisions ({DECISIONS_COLS})")
    conn.execute(f"create table simulation_fills ({FILLS_COLS})")
    conn.execute("create table community_posts "
                 "(post_id, agent_id, anonymous_code, turn, date, post_type, title, "
                 "content, like_count, unlike_count, score, is_best, source_ltb_id, "
                 "source_fill_id, source_decision_id)")
    conn.execute("create table community_interactions "
                 "(interaction_id, agent_id, post_id, turn, date, reaction)")
    conn.execute("create table community_logs "
                 "(log_id, agent_id, turn, date, best_posts_seen, posts_read, "
                 "community_thinking, candidate_posts_seen)")

    news_a, news_b = "news_20260306_종목_aaa", "news_20260306_경제_bbb"
    search_c = "news_20260227_종목_ccc"   # 검색 결과지만 뉴스와 형태가 같다
    claim_d = "community_claim:A001:t005:01"

    stb_rows = [
        # (agent, turn) = 두 에이전트 × 두 turn
        ("stb_A001_t005", "A001", 5, "2026-03-03", "AM", "d1", "d2", "d3", "d4", "d5", "d6",
         _whitelist([news_a, news_b], [claim_d] if with_community else [], [search_c]),
         _dim_ev({"dim_1": ([news_a], [news_b]),
                  "dim_4": ([claim_d] if with_community else [], []),
                  "dim_5": ([search_c], [])}), "h", "2026-03-03"),
        ("stb_A001_t006", "A001", 6, "2026-03-03", "PM", "d1b", "d2", "d3", "d4", "d5", "d6",
         _whitelist([news_a]), _dim_ev({"dim_1": ([news_a], [])}), "h", "2026-03-03"),
        ("stb_A002_t005", "A002", 5, "2026-03-03", "AM", "e1", "e2", "e3", "e4", "e5", "e6",
         _whitelist([news_a]), _dim_ev({"dim_2": ([news_a], [])}), "h", "2026-03-03"),
        ("stb_A002_t006", "A002", 6, "2026-03-03", "PM", "e1", "e2", "e3", "e4", "e5", "e6",
         _whitelist([news_a]), _dim_ev({"dim_2": ([news_a], [])}), "h", "2026-03-03"),
    ]
    conn.executemany(
        f"insert into simulation_stb_states ({STB_COLS}) values ({','.join('?' * 15)})",
        stb_rows)

    ltb_rows = [
        ("ltb_A001_t005", "A001", 5, 6, "2026-03-03", "AM", None, "stb_A001_t005",
         "dec_A001_t005", "fill_A001_t005", "L1", "L2", "L3", "L4", "L5", "L6",
         _dim_ev({"dim_1": ([news_a], []),
                  "dim_6": (["outcome:fill_A001_t002:h1"], [])}),
         "h", "요약", "{}", "h", "2026-03-03"),
        ("ltb_A001_t006", "A001", 6, 7, "2026-03-03", "PM", "ltb_A001_t005",
         "stb_A001_t006", "dec_A001_t006", "fill_A001_t006",
         "L1b", "L2", "L3", "L4", "L5", "L6",
         _dim_ev({"dim_1": ([news_a], [])}), "h", "요약", "{}", "h", "2026-03-03"),
    ]
    conn.executemany(
        f"insert into simulation_ltb_states ({LTB_COLS}) values ({','.join('?' * 22)})",
        ltb_rows)

    conn.executemany(
        f"insert into simulation_decisions ({DECISIONS_COLS}) values ({','.join('?' * 13)})",
        [("dec_A001_t005", "A001", 5, "2026-03-03", "AM", "buy", 10,
          None, "stb_A001_t005", "an_1", "{}", "h", "2026-03-03"),
         ("dec_A002_t005", "A002", 5, "2026-03-03", "AM", "sell", 5,
          None, "stb_A002_t005", "an_2", "{}", "h", "2026-03-03")])
    conn.executemany(
        f"insert into simulation_fills ({FILLS_COLS}) values ({','.join('?' * 18)})",
        [("fill_A001_t005", "A001", 5, "2026-03-03", "AM", "005930", "buy", 10,
          10, 70000.0, 0.0, None, "stb_A001_t005", "dec_A001_t005", "{}", "{}",
          "h", "2026-03-03"),
         ("fill_A002_t005", "A002", 5, "2026-03-03", "AM", "005930", "sell", 5,
          5, 70000.0, 0.0, None, "stb_A002_t005", "dec_A002_t005", "{}", "{}",
          "h", "2026-03-03")])

    if with_community:
        conn.execute(
            "insert into community_posts values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("73", "A003", "곰-1801", 4, "2026-03-03", "trade_share", "제목",
             "본문 첫 문장. 본문 둘째 문장.", 3, 1, 2, 1,
             "ltb_A003_t004", "fill_A003_t004", "dec_A003_t004"))
        conn.execute(
            "insert into community_interactions values (?,?,?,?,?,?)",
            ("i1", "A001", "73", 4, "2026-03-03", "like"))
        thinking = json.dumps({
            "agreement_disagreement": "동의",
            "delivery_date": "2026-03-03",
            "claims": [{"claim_stance": "bullish", "claim_text": "claim-1",
                        "supporting_quote": "본문 첫 문장.", "supporting_quote_ref": 1,
                        "source_exposure_ids": [
                            "community:2026-03-03:t4:post:73:best_full_body:A001:delivered_t5"]}],
        }, ensure_ascii=False)
        conn.execute("insert into community_logs values (?,?,?,?,?,?,?,?)",
                     ("l1", "A001", 4, "2026-03-03", "[]", "[]", thinking, "[]"))

    conn.commit()
    conn.close()
    return db
