from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from twinmarket_kr.agents.memory_agent import MemoryAgent
from twinmarket_kr.community.agent import CommunityAgent
from twinmarket_kr.core.collect_context import collect_context
from twinmarket_kr.simulation import community_phase


class LegacyCommunityPhaseTests(unittest.IsolatedAsyncioTestCase):
    def test_run_scoped_community_on_delivers_d0_best_even_if_global_default_is_off(self) -> None:
        class FundamentalStub:
            def get_market_features(
                self,
                _date: str,
                _stock_code: str,
            ) -> dict[str, float]:
                return {"close": 70_000.0}

        class NewsStub:
            def build_event_context(
                self,
                _event_id: str,
                news_depth: int,
            ) -> dict[str, object]:
                return {"news_depth": news_depth, "daily_titles": []}

        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "simulation.sqlite"
            community = CommunityAgent(db_path)
            memory = MemoryAgent(db_path)
            agent = {
                "agent_id": "D0",
                "news_depth": 0,
                "ini_cash": 10_000_000,
            }
            memory.init_portfolio_t000([agent])
            community.save_community_log(
                agent_id="D0",
                turn=2,
                date="2026-02-27",
                best_posts=[
                    {
                        "post_id": 1,
                        "title": "Best title",
                        "content": "Best full body",
                    }
                ],
                posts_read=[],
                thinking="",
            )

            with patch("config.ENABLE_COMMUNITY", False):
                context = collect_context(
                    agent,
                    turn=3,
                    date="2026-03-02",
                    subturn="am",
                    memory_agent=memory,
                    fundamental_agent=FundamentalStub(),
                    news_agent=NewsStub(),
                    community_agent=community,
                )

        self.assertEqual(context["community_log_turn"], 2)
        self.assertEqual(
            context["community_log"]["best_posts_seen"][0]["content"],
            "Best full body",
        )

    async def test_depth_delivery_full_body_and_self_exclusion_use_one_legacy_flow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "simulation.sqlite"
            community = CommunityAgent(db_path)
            memory = MemoryAgent(db_path)
            agents = [
                {"agent_id": "D0", "news_depth": 0, "ini_cash": 10_000_000},
                {"agent_id": "D1", "news_depth": 1, "ini_cash": 10_000_000},
                {"agent_id": "D2", "news_depth": 2, "ini_cash": 10_000_000},
            ]
            memory.init_portfolio_t000(agents)
            d1_post_id = community.save_post(
                "D1",
                3,
                "2026-02-27",
                "analysis",
                "D1 title",
                "D1 full body",
            )
            d2_post_id = community.save_post(
                "D2",
                3,
                "2026-02-27",
                "analysis",
                "D2 title",
                "D2 full body",
            )

            async def select_side_effect(
                agent: dict[str, object],
                visible_posts: list[dict[str, object]],
                read_limit: int,
                **_: object,
            ) -> list[int]:
                self.assertTrue(all("content" not in post for post in visible_posts))
                self.assertTrue(all("anonymous_code" in post for post in visible_posts))
                self.assertTrue(all("author_badges" in post for post in visible_posts))
                if agent["agent_id"] == "D1":
                    self.assertEqual(read_limit, 5)
                    return [d2_post_id]
                self.assertEqual(read_limit, 5)
                return [d1_post_id]

            async def react_side_effect(
                _agent: dict[str, object],
                posts: list[dict[str, object]],
                **_: object,
            ) -> list[dict[str, object]]:
                self.assertTrue(all(str(post["content"]).endswith("full body") for post in posts))
                return [
                    {"post_id": int(post["post_id"]), "reaction": "like"}
                    for post in posts
                ]

            with (
                patch(
                    "twinmarket_kr.simulation.community_reading_select",
                    new=AsyncMock(side_effect=select_side_effect),
                ),
                patch(
                    "twinmarket_kr.simulation.community_reading_react",
                    new=AsyncMock(side_effect=react_side_effect),
                ),
            ):
                await community_phase(
                    agents=agents,
                    community_agent=community,
                    memory_agent=memory,
                    sim_db_path=db_path,
                    turn=3,
                    date="2026-02-27",
                    client=object(),  # mocked readers never use the network client
                    concurrency=3,
                )

            d0_log = community.get_community_log("D0", 3)
            d1_log = community.get_community_log("D1", 3)
            d2_log = community.get_community_log("D2", 3)
            self.assertIsNotNone(d0_log)
            self.assertIsNotNone(d1_log)
            self.assertIsNotNone(d2_log)
            assert d0_log is not None and d1_log is not None and d2_log is not None

            self.assertEqual(
                {post["post_id"] for post in d0_log["best_posts_seen"]},
                {d1_post_id, d2_post_id},
            )
            self.assertTrue(
                all(post["author_profile"] is None for post in d0_log["best_posts_seen"])
            )
            self.assertEqual(
                {post["content"] for post in d0_log["best_posts_seen"]},
                {"D1 full body", "D2 full body"},
            )
            self.assertEqual(
                [post["post_id"] for post in d1_log["best_posts_seen"]],
                [d2_post_id],
            )
            self.assertEqual(
                [post["post_id"] for post in d2_log["best_posts_seen"]],
                [d1_post_id],
            )
            self.assertIsNotNone(d2_log["best_posts_seen"][0]["author_profile"])

            self.assertEqual(d1_log["posts_read"][0]["content"], "D2 full body")
            self.assertIsNone(d1_log["posts_read"][0]["author_profile"])
            self.assertEqual(d2_log["posts_read"][0]["content"], "D1 full body")
            self.assertIsNotNone(d2_log["posts_read"][0]["author_profile"])


if __name__ == "__main__":
    unittest.main()
