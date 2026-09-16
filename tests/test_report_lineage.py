from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.report_common import (
    summarize_canonical_lineage,
    summarize_community_exposures,
    summarize_reasoning_off,
)


class ReportLineageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.run_dir = Path(self.temporary.name) / "run"
        self.run_dir.mkdir()

    def _canonical_db(self) -> Path:
        path = self.run_dir / "simulation.sqlite"
        with sqlite3.connect(path) as connection:
            connection.executescript(
                """
                CREATE TABLE simulation_stb_states (
                    stb_id TEXT PRIMARY KEY,
                    agent_id TEXT,
                    turn INTEGER
                );
                CREATE TABLE simulation_ltb_states (
                    ltb_id TEXT PRIMARY KEY,
                    agent_id TEXT,
                    turn INTEGER,
                    parent_ltb_id TEXT,
                    source_stb_id TEXT,
                    source_decision_id TEXT,
                    source_fill_id TEXT
                );
                CREATE TABLE simulation_analyses (
                    analysis_id TEXT PRIMARY KEY,
                    agent_id TEXT,
                    turn INTEGER,
                    source_ltb_id TEXT,
                    source_stb_id TEXT
                );
                CREATE TABLE simulation_decisions (
                    decision_id TEXT PRIMARY KEY,
                    agent_id TEXT,
                    turn INTEGER,
                    source_ltb_id TEXT,
                    source_stb_id TEXT,
                    analysis_id TEXT
                );
                CREATE TABLE simulation_fills (
                    fill_id TEXT PRIMARY KEY,
                    agent_id TEXT,
                    turn INTEGER,
                    source_ltb_id TEXT,
                    source_stb_id TEXT,
                    decision_id TEXT
                );
                CREATE TABLE simulation_trade_outcomes (
                    outcome_id TEXT PRIMARY KEY,
                    fill_id TEXT,
                    horizon TEXT,
                    status TEXT
                );
                CREATE TABLE simulation_outcome_consumptions (
                    consumption_id TEXT PRIMARY KEY,
                    outcome_id TEXT,
                    fill_id TEXT,
                    horizon TEXT,
                    ltb_id TEXT
                );
                """
            )
            connection.execute(
                "INSERT INTO simulation_stb_states VALUES ('S1', 'A1', 1)"
            )
            connection.execute(
                """
                INSERT INTO simulation_ltb_states
                VALUES ('L0', 'A1', 0, NULL, NULL, NULL, NULL)
                """
            )
            connection.execute(
                """
                INSERT INTO simulation_analyses
                VALUES ('A1-ANALYSIS', 'A1', 1, 'L0', 'S1')
                """
            )
            connection.execute(
                """
                INSERT INTO simulation_decisions
                VALUES ('D1', 'A1', 1, 'L0', 'S1', 'A1-ANALYSIS')
                """
            )
            connection.execute(
                """
                INSERT INTO simulation_fills
                VALUES ('F1', 'A1', 1, 'L0', 'S1', 'D1')
                """
            )
            connection.execute(
                """
                INSERT INTO simulation_ltb_states
                VALUES ('L1', 'A1', 1, 'L0', 'S1', 'D1', 'F1')
                """
            )
            connection.execute(
                "INSERT INTO simulation_stb_states VALUES ('S2', 'A1', 2)"
            )
            connection.execute(
                """
                INSERT INTO simulation_analyses
                VALUES ('A1-ANALYSIS-2', 'A1', 2, 'L1', 'S2')
                """
            )
            connection.execute(
                """
                INSERT INTO simulation_decisions
                VALUES ('D2', 'A1', 2, 'L1', 'S2', 'A1-ANALYSIS-2')
                """
            )
            connection.execute(
                """
                INSERT INTO simulation_fills
                VALUES ('F2', 'A1', 2, 'L1', 'S2', 'D2')
                """
            )
            connection.execute(
                """
                INSERT INTO simulation_ltb_states
                VALUES ('L2', 'A1', 2, 'L1', 'S2', 'D2', 'F2')
                """
            )
            connection.executemany(
                "INSERT INTO simulation_trade_outcomes VALUES (?, ?, ?, ?)",
                (
                    ("O1", "F1", "next_turn", "matured"),
                    ("O2", "F1", "h1", "matured"),
                    ("O3", "F1", "h5", "matured"),
                    ("O4", "F2", "next_turn", "right_censored"),
                    ("O5", "F2", "h1", "right_censored"),
                    ("O6", "F2", "h5", "right_censored"),
                ),
            )
            connection.executemany(
                """
                INSERT INTO simulation_outcome_consumptions
                VALUES (?, ?, 'F1', ?, 'L1')
                """,
                (
                    ("C1", "O1", "next_turn"),
                    ("C2", "O2", "h1"),
                    ("C3", "O3", "h5"),
                ),
            )
            connection.commit()
        return path

    def test_canonical_summary_checks_the_recursive_chain_and_outcomes(
        self,
    ) -> None:
        db_path = self._canonical_db()
        summary = summarize_canonical_lineage(
            self.run_dir,
            metadata={
                "sim_db": str(db_path),
                "global_turn_start": 1,
                "global_turn_end": 2,
            },
            agent_ids=["A1"],
            turn_rows=[
                {
                    "agent": {"agent_id": "A1"},
                    "turn": 1,
                    "market_analysis": {"directional_stance": "bullish"},
                },
                {
                    "agent": {"agent_id": "A1"},
                    "turn": 2,
                    "market_analysis": {"directional_stance": "bearish"},
                },
            ],
            community_posts=[
                {
                    "post_id": "P1",
                    "source_ltb_id": "L1",
                    "source_fill_id": "F1",
                    "source_decision_id": "D1",
                }
            ],
        )

        self.assertEqual(summary["status"], "complete")
        self.assertEqual(summary["expected_event_rows"], 2)
        self.assertEqual(summary["analysis_count"], 2)
        self.assertEqual(summary["analysis_log_count"], 2)
        self.assertEqual(summary["complete_chain_count"], 2)
        self.assertEqual(summary["community_post_linked_count"], 1)
        self.assertEqual(summary["outcome_matured_count"], 3)
        self.assertEqual(summary["outcome_right_censored_count"], 3)
        self.assertEqual(summary["outcome_consumed_count"], 3)
        self.assertEqual(summary["outcome_consumption_linked_count"], 3)
        self.assertEqual(summary["expected_terminal_outcome_count"], 6)
        for horizon in ("next_turn", "h1", "h5"):
            counts = summary["outcome_horizon_counts"][horizon]
            self.assertEqual(counts["matured_count"], 1)
            self.assertEqual(counts["right_censored_count"], 1)
            self.assertEqual(counts["consumed_count"], 1)
            self.assertEqual(counts["consumption_linked_count"], 1)
            self.assertEqual(counts["total_count"], 2)
            self.assertEqual(counts["expected_terminal_count"], 2)

    def test_horizon_consumption_counts_come_from_consumption_ledger(
        self,
    ) -> None:
        db_path = self._canonical_db()
        with sqlite3.connect(db_path) as connection:
            connection.execute(
                "DELETE FROM simulation_outcome_consumptions WHERE horizon = 'h5'"
            )
            connection.commit()

        summary = summarize_canonical_lineage(
            self.run_dir,
            metadata={
                "sim_db": str(db_path),
                "global_turn_start": 1,
                "global_turn_end": 2,
            },
            agent_ids=["A1"],
            turn_rows=[
                {
                    "agent": {"agent_id": "A1"},
                    "turn": turn,
                    "market_analysis": {"directional_stance": "bullish"},
                }
                for turn in (1, 2)
            ],
            community_posts=[],
        )

        self.assertEqual(summary["outcome_matured_count"], 3)
        self.assertEqual(summary["outcome_consumed_count"], 2)
        self.assertEqual(
            summary["outcome_horizon_counts"]["h5"]["consumed_count"],
            0,
        )
        self.assertEqual(
            summary["outcome_horizon_counts"]["h5"][
                "consumption_linked_count"
            ],
            0,
        )

    def test_canonical_summary_prefers_integrated_committed_database(
        self,
    ) -> None:
        source = self._canonical_db()
        committed = self.run_dir / ".runtime" / "committed.db"
        committed.parent.mkdir(parents=True)
        committed.write_bytes(source.read_bytes())

        summary = summarize_canonical_lineage(
            self.run_dir,
            metadata={
                "runtime_db": str(self.run_dir / ".runtime" / "runtime_sim.db"),
                "global_turn_start": 1,
                "global_turn_end": 1,
            },
            agent_ids=["A1"],
            turn_rows=[
                {
                    "agent": {"agent_id": "A1"},
                    "turn": 1,
                    "market_analysis": {"directional_stance": "bullish"},
                }
            ],
            community_posts=[],
        )

        self.assertEqual(summary["status"], "complete")
        self.assertEqual(summary["complete_chain_count"], 1)
        self.assertEqual(summary["expected_terminal_outcome_count"], 3)
        self.assertEqual(
            summary["outcome_horizon_counts"]["h5"],
            {
                "matured_count": 1,
                "right_censored_count": 0,
                "consumed_count": 1,
                "consumption_linked_count": 1,
                "total_count": 1,
                "expected_terminal_count": 1,
            },
        )

    def test_canonical_summary_rejects_database_outside_the_run(self) -> None:
        source = self._canonical_db()
        external = self.run_dir.parent / "shared.sqlite"
        external.write_bytes(source.read_bytes())
        source.unlink()

        summary = summarize_canonical_lineage(
            self.run_dir,
            metadata={
                "sim_db": str(external),
                "global_turn_start": 1,
                "global_turn_end": 1,
            },
            agent_ids=["A1"],
            turn_rows=[
                {
                    "agent": {"agent_id": "A1"},
                    "turn": 1,
                    "market_analysis": {"directional_stance": "bullish"},
                }
            ],
            community_posts=[],
        )

        self.assertEqual(summary["status"], "missing_db")
        self.assertIsNone(summary["db_path"])

    def test_community_summary_keeps_exposure_relations_separate_and_checks_self(
        self,
    ) -> None:
        posts = [{"post_id": "7", "agent_id": "AUTHOR"}]
        best = [
            {
                "post_id": "7",
                "author_agent_id": "AUTHOR",
                "self_excluded_count": "1",
                "delivery_status": "delivered_am",
            }
        ]
        interactions = [
            {
                "post_id": "7",
                "agent_id": "READER",
                "exposure_level": "title_only",
                "delivery_status": "candidate_seen_pm",
                "provenance_id": "title",
            },
            {
                "post_id": "7",
                "agent_id": "READER",
                "exposure_level": "full_body",
                "delivery_status": "read_pm",
                "reaction": "like",
                "content": "body",
                "body_sha256": "a" * 64,
                "provenance_id": "selected",
            },
            {
                "post_id": "7",
                "agent_id": "READER",
                "exposure_level": "full_body",
                "delivery_status": "delivered_am",
                "is_best": "True",
                "content": "body",
                "body_sha256": "a" * 64,
                "provenance_id": "best",
            },
            {
                "post_id": "7",
                "agent_id": "READER",
                "exposure_level": "full_body",
                "delivery_status": "delivered_am",
                "replay": "True",
                "content": "body",
                "body_sha256": "a" * 64,
                "provenance_id": "replay",
            },
        ]

        summary = summarize_community_exposures(
            interactions,
            best,
            posts,
        )

        self.assertEqual(summary["title_only_count"], 1)
        self.assertEqual(summary["full_body_count"], 3)
        self.assertEqual(summary["pm_selected_full_body_count"], 1)
        self.assertEqual(summary["best_full_body_delivery_count"], 1)
        self.assertEqual(summary["selected_full_body_replay_count"], 1)
        self.assertEqual(summary["self_excluded_count"], 1)
        self.assertEqual(summary["self_delivery_violation_count"], 0)
        self.assertEqual(summary["duplicate_provenance_count"], 0)
        self.assertEqual(summary["missing_body_hash_count"], 0)
        self.assertEqual(summary["title_only_body_leak_count"], 0)
        self.assertEqual(summary["orphan_exposure_count"], 0)

        interactions[2]["agent_id"] = "AUTHOR"
        violated = summarize_community_exposures(
            interactions,
            best,
            posts,
        )
        self.assertEqual(violated["self_delivery_violation_count"], 1)

    def test_reasoning_summary_requires_run_local_zero_token_telemetry(
        self,
    ) -> None:
        policy = {
            "model": "qwen/test",
            "reasoning": {"effort": "none", "exclude": True},
            "provider": {
                "only": ["provider-a"],
                "order": ["provider-a"],
                "allow_fallbacks": False,
                "require_parameters": True,
            },
        }
        audit = {
            "audit_event": "provider_attempt",
            "status": "success",
            "requested_model": "qwen/test",
            "returned_model": "qwen/test",
            "provider": "provider-a",
            "reasoning_tokens": 0,
            "response_reasoning_present": False,
            "request_policy": {
                "reasoning": {"effort": "none", "exclude": True},
                "provider": policy["provider"],
            },
        }
        (self.run_dir / "openrouter_calls.jsonl").write_text(
            json.dumps(audit) + "\n",
            encoding="utf-8",
        )

        summary = summarize_reasoning_off(
            self.run_dir,
            {"openrouter_call_policy": policy},
        )

        self.assertEqual(summary["status"], "verified")
        self.assertEqual(summary["provider_return_count"], 1)
        self.assertEqual(summary["reasoning_zero_count"], 1)

        (self.run_dir / "openrouter_calls.jsonl").unlink()
        missing = summarize_reasoning_off(
            self.run_dir,
            {
                "openrouter_call_policy": policy,
                "openrouter_audit_path": "/tmp/shared-audit.jsonl",
            },
        )
        self.assertEqual(missing["status"], "missing_run_local_telemetry")
        self.assertEqual(missing["audit_paths"], [])


if __name__ == "__main__":
    unittest.main()
