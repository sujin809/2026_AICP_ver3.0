from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from twinmarket_kr.agents.memory_agent import MemoryAgent
from twinmarket_kr.community.agent import CommunityAgent
from twinmarket_kr.community.reading import (
    _format_post_list,
    _format_posts_content,
    community_reading_select,
)
from twinmarket_kr.community.thinking import _format_best_posts, _format_posts_read
from twinmarket_kr.community.validation import (
    CommunityValidationError,
    expected_selective_read_limit,
    validate_post_body,
    validate_selective_read_limits,
)
from twinmarket_kr.run_logger import (
    SimulationLogger,
    finalize_community_delivery_counts,
)
from twinmarket_kr.run_integrity import validate_community_artifacts
from twinmarket_kr.simulation import validate_community_runtime_policy


class CommunityPolicyTests(unittest.TestCase):
    def test_community_on_rejects_partial_global_feature_flags(self) -> None:
        validate_community_runtime_policy("on")
        validate_community_runtime_policy("off")

        for posting, reading in ((False, True), (True, False), (False, False)):
            with (
                self.subTest(posting=posting, reading=reading),
                patch("config.ENABLE_COMMUNITY_POSTING", posting),
                patch("config.ENABLE_COMMUNITY_READING", reading),
            ):
                with self.assertRaisesRegex(ValueError, "partial community arms"):
                    validate_community_runtime_policy("on")
                validate_community_runtime_policy("off")

    def test_integrity_contract_accepts_d0_full_best_and_d1_d2_writers(self) -> None:
        body = "Best 게시글 원문"
        body_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
        agent_ids = ["D0", "D1", "D2"]
        depth_by_agent = {"D0": 0, "D1": 1, "D2": 2}
        best_row = {
            "date": "2026-02-27",
            "turn": "2",
            "post_id": "1",
            "rank": "1",
            "author_agent_id": "D1",
            "content": body,
            "body_sha256": body_sha256,
            "score": "3",
            "like_count": "3",
            "scheduled_delivery_count": "2",
            "actual_delivery_count": "2",
            "delivery_status": "delivered_am",
            "self_exclusion_policy": "exclude_author_no_backfill",
        }

        def delivery(agent_id: str, author_profile: str, profile_scope: str) -> dict[str, str]:
            return {
                "date": "2026-03-02",
                "turn": "3",
                "source_date": "2026-02-27",
                "source_turn": "2",
                "agent_id": agent_id,
                "post_id": "1",
                "exposure_level": "full_body",
                "content": body,
                "body_sha256": body_sha256,
                "author_profile": author_profile,
                "profile_scope": profile_scope,
                "delivery_status": "delivered_am",
                "is_best": "True",
                "selected": "False",
                "replay": "False",
            }

        errors = validate_community_artifacts(
            community_mode="on",
            agent_ids=agent_ids,
            depth_by_agent=depth_by_agent,
            community_rows=[
                {
                    "agent_id": "D0",
                    "posts_read_json": "[]",
                    "best_posts_json": json.dumps(
                        [{"post_id": 1, "content": body}],
                        ensure_ascii=False,
                    ),
                },
                {"agent_id": "D1"},
                {"agent_id": "D2"},
            ],
            post_rows=[
                {"agent_id": "D1", "content": "D1 글"},
                {"agent_id": "D2", "content": "D2 글"},
            ],
            interaction_rows=[
                delivery("D0", "null", ""),
                delivery("D2", '{"snapshot_turn": 2}', "detailed"),
            ],
            best_rows=[best_row],
            selection_rows=[],
        )

        self.assertEqual(errors, [])

    def test_integrity_contract_rejects_d0_post_and_title_only_best(self) -> None:
        errors = validate_community_artifacts(
            community_mode="on",
            agent_ids=["D0"],
            depth_by_agent={"D0": 0},
            community_rows=[],
            post_rows=[{"agent_id": "D0", "content": "금지된 글"}],
            interaction_rows=[
                {
                    "date": "2026-03-02",
                    "turn": "3",
                    "agent_id": "D0",
                    "post_id": "1",
                    "exposure_level": "title_only",
                    "delivery_status": "delivered_am",
                }
            ],
            best_rows=[],
            selection_rows=[],
        )

        self.assertTrue(any("ineligible community post author=D0" in error for error in errors))
        self.assertTrue(any("Depth 0 title-only exposure agent=D0" in error for error in errors))

    def test_approved_depth_limits_are_exact(self) -> None:
        validate_selective_read_limits(depth1=5, depth2=5)
        self.assertEqual(expected_selective_read_limit(0), 0)
        self.assertEqual(expected_selective_read_limit(1), 5)
        self.assertEqual(expected_selective_read_limit(2), 5)

        for depth1, depth2 in ((4, 5), (6, 5), (5, 4), (5, 6)):
            with self.subTest(depth1=depth1, depth2=depth2):
                with self.assertRaisesRegex(
                    CommunityValidationError,
                    "D1=5 and D2=5",
                ):
                    validate_selective_read_limits(
                        depth1=depth1,
                        depth2=depth2,
                    )

    def test_integrity_contract_rejects_legacy_d2_ten_limit(self) -> None:
        errors = validate_community_artifacts(
            community_mode="on",
            agent_ids=["D2"],
            depth_by_agent={"D2": 2},
            community_rows=[],
            post_rows=[],
            interaction_rows=[],
            best_rows=[],
            selection_rows=[
                {
                    "agent_id": "D2",
                    "read_limit": "10",
                }
            ],
        )

        self.assertTrue(
            any(
                "depth=2 limit=10" in error
                for error in errors
            )
        )

    def test_post_body_boundary_is_not_silently_truncated(self) -> None:
        body = "가" * 500
        self.assertEqual(validate_post_body(body), body)
        with self.assertRaisesRegex(CommunityValidationError, "500 characters"):
            validate_post_body("가" * 501)

    def test_legacy_database_write_rechecks_post_body_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            community = CommunityAgent(Path(temporary) / "community.sqlite")
            post_id = community.save_post(
                "A001",
                1,
                "2026-02-27",
                "analysis",
                "500 boundary",
                "가" * 500,
            )
            self.assertGreater(post_id, 0)
            self.assertEqual(len(community.get_post_content(post_id)["content"]), 500)

            with self.assertRaisesRegex(CommunityValidationError, "500 characters"):
                community.save_post(
                    "A001",
                    1,
                    "2026-02-27",
                    "analysis",
                    "501 rejected",
                    "가" * 501,
                )

    def test_neutral_reaction_is_preserved_as_none_for_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            community = CommunityAgent(Path(temporary) / "community.sqlite")
            post_id = community.save_post(
                "AUTHOR",
                2,
                "2026-02-27",
                "analysis",
                "neutral",
                "body",
            )

            recorded = community.record_reaction(
                "READER",
                post_id,
                2,
                "2026-02-27",
                "none",
            )

            with sqlite3.connect(community._db) as connection:
                stored = connection.execute(
                    "SELECT reaction FROM community_interactions"
                ).fetchone()[0]
                score = connection.execute(
                    "SELECT score FROM community_posts WHERE post_id = ?",
                    (post_id,),
                ).fetchone()[0]

        self.assertTrue(recorded)
        self.assertEqual(stored, "none")
        self.assertEqual(score, 0)

    def test_legacy_selector_enforces_depth_boundaries(self) -> None:
        class FakeClient:
            def __init__(self, selected_ids: list[int]) -> None:
                self.selected_ids = selected_ids

            async def chat(self, *_: object, **__: object) -> dict[str, object]:
                return {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {"selected_post_ids": self.selected_ids}
                                )
                            }
                        }
                    ]
                }

        posts = [
            {
                "post_id": index,
                "post_type": "analysis",
                "title": f"title-{index}",
                "anonymous_code": f"곰-{1000 + index}",
            }
            for index in range(1, 12)
        ]
        for limit in (5,):
            with self.subTest(limit=limit, boundary="accepted"):
                selected = asyncio.run(
                    community_reading_select(
                        {"persona_prompt": "persona"},
                        posts,
                        limit,
                        client=FakeClient(list(range(1, limit + 1))),
                    )
                )
                self.assertEqual(len(selected), limit)
            with self.subTest(limit=limit, boundary="rejected"):
                with self.assertRaises(CommunityValidationError):
                    asyncio.run(
                        community_reading_select(
                            {"persona_prompt": "persona"},
                            posts,
                            limit,
                            client=FakeClient(list(range(1, limit + 2))),
                        )
                    )


class CommunityBestPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self._temporary.name) / "community.sqlite"
        self.community = CommunityAgent(self.db_path)
        self.memory = MemoryAgent(self.db_path)
        self.memory.init_portfolio_t000(
            [{"agent_id": "A001", "ini_cash": 10_000_000}],
        )

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _save_ranked_posts(self) -> list[int]:
        post_ids = [
            self.community.save_post(
                f"A{index:03d}",
                3,
                "2026-02-27",
                "analysis",
                f"title-{index}",
                f"body-{index}",
            )
            for index in range(1, 7)
        ]
        for score, post_id in zip((6, 5, 4, 3, 2, 1), post_ids, strict=True):
            for _ in range(score):
                self.community.update_post_score_live(post_id, "like")
        return post_ids

    def test_best_is_full_body_depth_projected_and_self_excluded_without_backfill(self) -> None:
        post_ids = self._save_ranked_posts()
        frozen = self.community.freeze_best_posts(
            date="2026-02-27",
            turn=3,
            n=5,
            memory_agent=self.memory,
            badges={"A001": ["상위 수익자"]},
        )
        self.assertEqual([post["post_id"] for post in frozen], post_ids[:5])
        self.assertEqual(frozen[0]["content"], "body-1")
        self.assertEqual(len(frozen[0]["body_sha256"]), 64)

        for depth in (0, 1):
            with self.subTest(depth=depth):
                projected = self.community.project_best_posts_for_reader(
                    frozen,
                    recipient_agent_id="A001",
                    depth=depth,
                )
                self.assertEqual(
                    [post["post_id"] for post in projected],
                    post_ids[1:5],
                )
                self.assertNotIn(post_ids[5], [post["post_id"] for post in projected])
                self.assertTrue(all(post["content"].startswith("body-") for post in projected))
                self.assertTrue(all(post["author_profile"] is None for post in projected))
                self.assertTrue(all("author_agent_id" not in post for post in projected))

        d2 = self.community.project_best_posts_for_reader(
            frozen,
            recipient_agent_id="A002",
            depth=2,
        )
        self.assertEqual([post["post_id"] for post in d2], [post_ids[0], *post_ids[2:5]])
        self.assertEqual(d2[0]["profile_scope"], "detailed")
        self.assertEqual(d2[0]["author_profile"]["snapshot_turn"], 3)

    def test_d2_profile_is_frozen_at_pm_and_excludes_private_reason_and_future_trade(self) -> None:
        for turn in range(1, 5):
            self.memory.append_trade_log(
                {
                    "agent_id": "A001",
                    "turn": turn,
                    "date": f"2026-02-{24 + turn:02d}",
                    "action": "buy",
                    "stock_code": "005930",
                    "quantity": turn,
                    "executed_price": 70_000 + turn,
                    "trade_value": (70_000 + turn) * turn,
                    "status": "filled",
                    "filled_quantity": turn,
                    "action_reason": f"private-reason-{turn}",
                }
            )
        self.memory.append_trade_log(
            {
                "log_id": "unfilled-at-snapshot",
                "agent_id": "A001",
                "turn": 3,
                "date": "2026-02-27",
                "action": "sell",
                "stock_code": "005930",
                "quantity": 99,
                "status": "unfilled",
                "filled_quantity": 0,
                "action_reason": "private-unfilled-reason",
            }
        )
        self.community.save_post(
            "A001",
            3,
            "2026-02-27",
            "analysis",
            "profile freeze",
            "frozen body",
        )
        frozen = self.community.freeze_best_posts(
            date="2026-02-27",
            turn=3,
            n=5,
            memory_agent=self.memory,
            badges={"A001": ["자산가"]},
        )
        profile = frozen[0]["author_profile_snapshot"]
        self.assertEqual(
            [trade["turn"] for trade in profile["recent_trades"]],
            [3, 2, 1],
        )
        self.assertTrue(
            all(
                trade["status"] == "filled" and trade["filled_quantity"] > 0
                for trade in profile["recent_trades"]
            )
        )
        self.assertTrue(
            all("action_reason" not in trade for trade in profile["recent_trades"])
        )
        self.assertNotIn("agent_id", profile["portfolio_summary"])

        self.memory.append_trade_log(
            {
                "log_id": "future-overwrite-proof",
                "agent_id": "A001",
                "turn": 5,
                "date": "2026-03-02",
                "action": "sell",
                "stock_code": "005930",
                "quantity": 1,
                "status": "filled",
                "filled_quantity": 1,
            }
        )
        projected = self.community.project_best_posts_for_reader(
            frozen,
            recipient_agent_id="A999",
            depth=2,
        )
        self.assertEqual(
            [trade["turn"] for trade in projected[0]["author_profile"]["recent_trades"]],
            [3, 2, 1],
        )

    def test_full_body_format_does_not_truncate_and_deduplicates_best_overlap(self) -> None:
        long_body = "본문" * 220
        selected = {
            "post_id": 1,
            "post_type": "analysis",
            "title": "same",
            "content": long_body,
            "reaction": "like",
            "anonymous_code": "황소-1001",
            "author_badges": ["자산가"],
        }
        best_text, _best_sources = _format_best_posts(
            [{**selected, "rank": 1, "score": 2}],
            selected_by_id={1: selected},
            agent_id="A999",
            source_turn=2,
            source_date="2026-02-27",
            delivery_turn=3,
        )
        selected_text, _selected_sources = _format_posts_read(
            [selected],
            excluded_post_ids={1},
            agent_id="A999",
            source_turn=2,
            source_date="2026-02-27",
            delivery_turn=3,
        )
        self.assertIn(long_body, best_text)
        self.assertIn("내 반응: like", best_text)
        self.assertNotIn(long_body, selected_text)

    def test_select_and_react_prompts_preserve_legacy_visibility_rules(self) -> None:
        profile = {
            "portfolio_summary": {"cash": 10_000_000},
            "recent_trades": [
                {"turn": turn, "action": "buy", "quantity": turn}
                for turn in range(1, 4)
            ],
            "note": "프로필" * 400,
        }
        post = {
            "post_id": 1,
            "post_type": "analysis",
            "title": "title only",
            "content": "본문 전체",
            "anonymous_code": "곰-1001",
            "author_badges": ["자산가"],
            "like_count": 2,
            "unlike_count": 1,
            "score": 1,
            "author_profile": profile,
        }
        candidate_text = _format_post_list([post])
        self.assertNotIn("본문 전체", candidate_text)
        self.assertIn("곰-1001", candidate_text)
        self.assertIn("score 1", candidate_text)

        full_text = _format_posts_content([post])
        self.assertIn("본문 전체", full_text)
        self.assertIn("곰-1001", full_text)
        self.assertIn("자산가", full_text)
        self.assertIn(profile["note"], full_text)

    def test_legacy_artifacts_distinguish_title_full_body_and_am_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            logger = SimulationLogger(root_dir=temporary, run_id="community-artifacts")
            candidate = {
                "post_id": 7,
                "anonymous_code": "황소-1007",
                "post_type": "analysis",
                "title": "candidate",
                "like_count": 0,
                "unlike_count": 0,
                "score": 0,
                "author_badges": ["자산가"],
            }
            logger.log_community_selection_input(
                agent_id="D1",
                turn=3,
                date="2026-02-27",
                depth=1,
                read_limit=5,
                visible_posts=[candidate],
                selected_post_ids=[7],
            )
            full_body = {
                **candidate,
                "content": "full body",
                "body_sha256": "a" * 64,
                "reaction": "like",
                "author_profile": None,
                "profile_scope": "minimal",
                "exposure_level": "full_body",
            }
            logger.log_community_reading(
                agent_id="D1",
                turn=3,
                date="2026-02-27",
                selected_post_ids=[7],
                posts_read=[full_body],
            )
            frozen_best = {
                **full_body,
                "rank": 1,
                "is_best": True,
                "author_agent_id": "AUTHOR",
                "scheduled_delivery_count": 1,
                "actual_delivery_count": None,
                "delivery_status": "scheduled_next_am",
            }
            logger.log_community_best_posts(
                turn=3,
                date="2026-02-27",
                best_posts=[frozen_best],
            )
            logger.log_community_delivery(
                agent_id="D1",
                source_turn=3,
                delivery_turn=4,
                source_date="2026-02-27",
                delivery_date="2026-03-02",
                best_posts=[frozen_best],
                posts_read=[full_body],
            )
            logger.log_community_delivery(
                agent_id="D1",
                source_turn=3,
                delivery_turn=4,
                source_date="2026-02-27",
                delivery_date="2026-03-02",
                best_posts=[frozen_best],
                posts_read=[full_body],
            )
            finalize_community_delivery_counts(logger.run_dir)

            with (logger.run_dir / "community_interactions.csv").open(
                "r",
                encoding="utf-8-sig",
                newline="",
            ) as f:
                rows = list(csv.DictReader(f))
            with (logger.run_dir / "community_best_posts.csv").open(
                "r",
                encoding="utf-8-sig",
                newline="",
            ) as f:
                best_rows = list(csv.DictReader(f))
            trace_rows = [
                json.loads(line)
                for line in (
                    logger.run_dir / "traces" / "community_exposure_trace.jsonl"
                ).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

        self.assertEqual(
            [row["exposure_level"] for row in rows],
            ["title_only", "full_body", "full_body", "full_body"],
        )
        self.assertEqual(rows[0]["content"], "")
        self.assertEqual(rows[1]["content"], "full body")
        self.assertEqual(rows[2]["delivery_status"], "delivered_am")
        self.assertEqual(rows[2]["is_best"], "True")
        self.assertEqual(rows[3]["replay"], "True")
        self.assertEqual(best_rows[0]["actual_delivery_count"], "1")
        self.assertEqual(best_rows[0]["delivery_status"], "delivered_am")
        self.assertEqual(len(trace_rows), 4)
        self.assertTrue(
            all(row["artifact"] == "community_exposure_trace" for row in trace_rows)
        )

    def test_common_logger_connects_delivery_and_right_censors_final_pm(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            logger = SimulationLogger(
                root_dir=temporary,
                run_id="event-checkpoint-run",
            )
            previous_pm_best = {
                "post_id": 1,
                "rank": 1,
                "author_agent_id": "AUTHOR",
                "title": "best",
                "post_type": "analysis",
                "content": "body",
                "body_sha256": "b" * 64,
                "scheduled_delivery_count": 1,
                "actual_delivery_count": None,
                "delivery_status": "scheduled_next_am",
            }
            logger.log_community_best_posts(
                turn=2,
                date="2026-02-27",
                best_posts=[previous_pm_best],
            )

            logger.log_community_delivery(
                agent_id="D0",
                source_turn=2,
                delivery_turn=3,
                source_date="2026-02-27",
                delivery_date="2026-03-02",
                best_posts=[previous_pm_best],
                posts_read=[],
            )
            final_pm_best = {
                **previous_pm_best,
                "post_id": 2,
                "rank": 1,
                "title": "final-day-best",
                "content": "final-day-body",
                "body_sha256": "c" * 64,
            }
            logger.log_community_best_posts(
                turn=4,
                date="2026-03-02",
                best_posts=[final_pm_best],
            )

            summary = finalize_community_delivery_counts(logger.run_dir)

            with (logger.run_dir / "community_interactions.csv").open(
                "r",
                encoding="utf-8-sig",
                newline="",
            ) as f:
                master_interaction_rows = list(csv.DictReader(f))
            with (logger.run_dir / "community_best_posts.csv").open(
                "r",
                encoding="utf-8-sig",
                newline="",
            ) as f:
                master_best_rows = list(csv.DictReader(f))

            self.assertEqual(len(master_interaction_rows), 1)
            self.assertEqual(
                master_interaction_rows[0]["source_date"],
                "2026-02-27",
            )
            self.assertEqual(
                master_interaction_rows[0]["delivery_date"],
                "2026-03-02",
            )
            best_by_post_id = {
                row["post_id"]: row
                for row in master_best_rows
            }
            self.assertEqual(
                best_by_post_id["1"]["actual_delivery_count"],
                "1",
            )
            self.assertEqual(
                best_by_post_id["1"]["delivery_status"],
                "delivered_am",
            )
            self.assertEqual(
                best_by_post_id["2"]["actual_delivery_count"],
                "0",
            )
            self.assertEqual(
                best_by_post_id["2"]["delivery_status"],
                "right_censored",
            )
            self.assertEqual(summary["delivered"], 1)
            self.assertEqual(summary["right_censored"], 1)

    def test_best_with_no_eligible_recipient_is_not_reported_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            logger = SimulationLogger(
                root_dir=temporary,
                run_id="single-agent-community",
            )
            logger.log_community_best_posts(
                turn=2,
                date="2026-02-27",
                best_posts=[
                    {
                        "post_id": 1,
                        "rank": 1,
                        "author_agent_id": "ONLY_AGENT",
                        "title": "self-authored best",
                        "post_type": "analysis",
                        "content": "body",
                        "body_sha256": "d" * 64,
                        "scheduled_delivery_count": 0,
                        "actual_delivery_count": None,
                        "delivery_status": "scheduled_next_am",
                    }
                ],
            )

            summary = finalize_community_delivery_counts(logger.run_dir)

            with (logger.run_dir / "community_best_posts.csv").open(
                "r",
                encoding="utf-8-sig",
                newline="",
            ) as f:
                rows = list(csv.DictReader(f))

        self.assertEqual(summary["no_eligible_recipient"], 1)
        self.assertEqual(rows[0]["actual_delivery_count"], "0")
        self.assertEqual(rows[0]["delivery_status"], "no_eligible_recipient")


if __name__ == "__main__":
    unittest.main()
