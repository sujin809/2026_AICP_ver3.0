from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from twinmarket_kr.run_logger import SimulationLogger


def _csv_row(path: Path) -> dict[str, str]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != 1:
        raise AssertionError(f"expected exactly one row in {path}, got {len(rows)}")
    return rows[0]


class LineageLoggingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.logger = SimulationLogger(
            root_dir=self.temporary.name,
            run_id="lineage-run",
        )
        self.lineage = {
            "source_ltb_id": "ltb-agent-1-t000",
            "source_stb_id": "stb-agent-1-t001",
            "analysis_id": "analysis-agent-1-t001",
            "decision_id": "decision-agent-1-t001",
            "fill_id": "fill-agent-1-t001",
            "post_fill_ltb_id": "ltb-agent-1-t001",
        }

    def test_existing_turn_order_fill_and_post_outputs_keep_lineage_ids(
        self,
    ) -> None:
        order = {
            "user_id": "agent-1",
            "stock_code": "005930",
            "direction": "buy",
            "quantity": 2,
            "reason": "test order",
            "subturn": "am",
        }
        self.logger.log_agent_turn(
            agent={"agent_id": "agent-1", "news_depth": 1},
            turn=1,
            date="2026-02-27",
            context={
                "subturn": "am",
                "news_context": {},
            },
            news_interpretation={"selected_news": []},
            belief={
                "stb_id": self.lineage["source_stb_id"],
                "belief_summary": "human log only",
                "view_change": "updated",
            },
            market_analysis={},
            decision={
                "action": "buy",
                "quantity": 2,
                "reason": "test order",
                "risk_control": "cash bound",
            },
            order=order,
            lineage=self.lineage,
        )
        self.logger.log_daily_exchange(
            date="2026-02-27",
            turn=1,
            orders=[{**order, **self.lineage}],
            results={
                "005930": {
                    "announced_price": 100,
                    "close_price": 101,
                    "volume": 2,
                    "transactions": [
                        {
                            "user_id": "agent-1",
                            "stock_code": "005930",
                            "direction": "buy",
                            "quantity": 2,
                            "executed_price": 100,
                            "status": "filled",
                            **self.lineage,
                        }
                    ],
                }
            },
        )
        self.logger.log_community_post(
            agent_id="agent-1",
            turn=1,
            date="2026-02-27",
            post={
                "post_id": 1,
                "post_type": "analysis",
                "title": "lineage",
                "content": "body",
                "source_ltb_id": self.lineage["post_fill_ltb_id"],
                "fill_id": self.lineage["fill_id"],
                "decision_id": self.lineage["decision_id"],
            },
        )

        agent_turn = _csv_row(self.logger.run_dir / "agent_turns.csv")
        submitted_order = _csv_row(
            self.logger.run_dir / "submitted_orders.csv"
        )
        exchange_fill = _csv_row(self.logger.run_dir / "exchange_fills.csv")
        community_post = _csv_row(
            self.logger.run_dir / "community_posts.csv"
        )
        for field, value in self.lineage.items():
            self.assertEqual(agent_turn[field], value)
            self.assertEqual(submitted_order[field], value)
            self.assertEqual(exchange_fill[field], value)
        self.assertEqual(
            community_post["source_ltb_id"],
            self.lineage["post_fill_ltb_id"],
        )
        self.assertEqual(
            community_post["source_fill_id"],
            self.lineage["fill_id"],
        )
        self.assertEqual(
            community_post["source_decision_id"],
            self.lineage["decision_id"],
        )

        with (self.logger.run_dir / "agent_turns.jsonl").open(
            encoding="utf-8"
        ) as handle:
            event = json.loads(handle.readline())
        self.assertEqual(event["lineage"], self.lineage)

    def test_logger_rejects_conflicting_lineage_instead_of_mixing_events(
        self,
    ) -> None:
        with self.assertRaisesRegex(ValueError, "lineage conflict"):
            self.logger.log_agent_turn(
                agent={"agent_id": "agent-1", "news_depth": 1},
                turn=1,
                date="2026-02-27",
                context={"subturn": "am", "news_context": {}},
                news_interpretation={"selected_news": []},
                belief={"stb_id": "stb-conflict"},
                market_analysis={},
                decision={"action": "buy", "quantity": 1},
                order=None,
                lineage=self.lineage,
            )


if __name__ == "__main__":
    unittest.main()
