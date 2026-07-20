from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from twinmarket_kr.agents.news_agent import NewsAgent
from twinmarket_kr.llm.decision import (
    DecisionValidationError,
    make_decision,
    parse_decision_json,
)
from twinmarket_kr.llm.analysis import _nonempty_text_or_string_list
from twinmarket_kr.llm.client import UnexpectedModelError, _is_retryable_error
from twinmarket_kr.run_integrity import summarize_api_audit
from twinmarket_kr.experiment_runtime import build_clean_experiment_base


class _InvalidDecisionClient:
    async def chat(self, *args, **kwargs):
        return json.dumps({"action": "hold", "quantity": 0})


class DecisionSafetyTest(unittest.IsolatedAsyncioTestCase):
    def test_fractional_or_unexplained_order_is_invalid(self) -> None:
        constraints = {
            "current_price": 100_000,
            "min_order_unit": 1,
            "max_buy_quantity": 500,
            "max_sell_quantity": 0,
            "allow_hold": False,
            "allowed_actions": ["buy"],
        }
        parsed = parse_decision_json(
            json.dumps({"action": "buy", "quantity": 1.5, "reason": "", "risk_control": ""}),
            constraints,
        )
        self.assertFalse(parsed["valid"])
        self.assertIn("invalid_quantity_format", parsed["validation_errors"])
        self.assertIn("missing_reason", parsed["validation_errors"])
        self.assertIn("missing_risk_control", parsed["validation_errors"])

    async def test_invalid_output_never_becomes_one_share_fallback(self) -> None:
        constraints = {
            "available_cash": 100_000_000,
            "current_quantity": 0,
            "current_price": 100_000,
            "min_order_unit": 1,
            "max_buy_quantity": 500,
            "max_sell_quantity": 0,
            "allow_hold": False,
            "allowed_actions": ["buy"],
        }
        with self.assertRaises(DecisionValidationError):
            await make_decision(
                {"persona_prompt": "test"},
                {"belief_summary": "test"},
                {},
                "test portfolio",
                "no history",
                constraints,
                client=_InvalidDecisionClient(),
                seed=2,
                validation_attempts=2,
            )


class NewsSafetyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.processed = root / "processed.csv"
        self.daily = root / "daily.csv"
        fields = ["id", "title", "date", "time", "category", "summary", "is_fake"]
        rows = [
            {
                "id": "news_old",
                "title": "HBM 과거",
                "date": "2026-03-01",
                "time": "10:00",
                "category": "종목",
                "summary": "HBM 과거 기사",
                "is_fake": "false",
            },
            {
                "id": "news_before",
                "title": "HBM 오전",
                "date": "2026-03-08",
                "time": "08:30",
                "category": "종목",
                "summary": "HBM 의사결정 전 기사",
                "is_fake": "false",
            },
            {
                "id": "news_future",
                "title": "HBM 미래",
                "date": "2026-03-08",
                "time": "09:30",
                "category": "종목",
                "summary": "HBM 의사결정 후 기사",
                "is_fake": "false",
            },
        ]
        for path in (self.processed, self.daily):
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_depth2_search_excludes_future_news(self) -> None:
        agent = NewsAgent(self.processed, self.daily, include_fake_news=False)
        results = agent.search_news_flat(
            keywords=["HBM"],
            current_date="2026-03-08",
            window_end_date="2026-03-08",
            window_end_time="08:59",
            lookback_days=7,
        )
        result_ids = [row["id"] for row in results]
        self.assertIn("news_before", result_ids)
        self.assertIn("news_old", result_ids)
        self.assertNotIn("news_future", result_ids)

    def test_influential_news_must_exist_in_visible_or_read_context(self) -> None:
        agent = NewsAgent(self.processed, self.daily, include_fake_news=False)
        context = {
            "daily_titles": [{"id": "news_before", "title": "HBM 오전"}],
            "read_contents": [],
            "search_read_contents": [],
        }
        resolved, unresolved = agent.normalize_influential_news(
            ["HBM 오전", "존재하지 않는 뉴스"],
            context,
        )
        self.assertEqual([row["id"] for row in resolved], ["news_before"])
        self.assertEqual(unresolved, ["존재하지 않는 뉴스"])


class ApiAuditSafetyTest(unittest.TestCase):
    def test_unexpected_returned_model_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            audit_path = Path(directory) / "openrouter_calls.jsonl"
            audit_path.write_text(
                json.dumps(
                    {
                        "status": "success",
                        "requested_model": "qwen/qwen3.5-flash-02-23",
                        "returned_model": "another/model",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(RuntimeError):
                summarize_api_audit(
                    audit_path,
                    expected_model="qwen/qwen3.5-flash-02-23",
                )

    def test_model_mismatch_is_not_retried(self) -> None:
        self.assertFalse(_is_retryable_error(UnexpectedModelError("wrong model")))


class AnalysisSchemaSafetyTest(unittest.TestCase):
    def test_market_analysis_accepts_explanatory_text_or_string_list(self) -> None:
        self.assertTrue(_nonempty_text_or_string_list("명확한 기회 설명"))
        self.assertTrue(_nonempty_text_or_string_list(["위험 1", "위험 2"]))
        self.assertFalse(_nonempty_text_or_string_list(""))
        self.assertFalse(_nonempty_text_or_string_list([]))
        self.assertFalse(_nonempty_text_or_string_list([1]))


class ExperimentBaseSafetyTest(unittest.TestCase):
    def test_source_database_cannot_be_overwritten_as_the_base(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "same.db"
            with self.assertRaises(ValueError):
                build_clean_experiment_base(path, path, overwrite=True)


if __name__ == "__main__":
    unittest.main()
