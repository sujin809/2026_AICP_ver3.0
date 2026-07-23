from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from twinmarket_kr.db.connection import init_paper_sim_db
from twinmarket_kr.db.schema import (
    PAPER_ANALYSES_DDL,
    PAPER_RUNTIME_TABLES,
    SIM_SCHEMA_VERSION,
)


class RNPaperSchemaTests(unittest.TestCase):
    def test_fresh_paper_schema_has_analysis_and_dimension_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "fresh.sqlite"
            init_paper_sim_db(database)
            with sqlite3.connect(database) as connection:
                version = connection.execute(
                    "SELECT schema_value FROM rn_schema_meta WHERE schema_key = 'schema_version'"
                ).fetchone()
                self.assertEqual(version, (SIM_SCHEMA_VERSION,))

                stb_columns = _columns(connection, "short_term_belief_history")
                self.assertEqual(stb_columns["dimension_evidence_json"], ("TEXT", 1, None))

                transition_columns = _columns(connection, "ltb_dimension_transitions")
                self.assertEqual(
                    transition_columns["integration_evidence_by_dimension_json"],
                    ("TEXT", 1, None),
                )
                ltb_columns = _columns(connection, "paper_ltb_states")
                for name in (
                    "belief_summary",
                    "view_change_json",
                    "human_log_renderer_version",
                    "human_log_renderer_sha256",
                    "human_log_sha256",
                ):
                    self.assertEqual(ltb_columns[name], ("TEXT", 1, None))
                trace_columns = _columns(connection, "turn_belief_trace")
                for name in (
                    "human_log_json",
                    "human_log_renderer_version",
                    "human_log_renderer_sha256",
                    "human_log_sha256",
                ):
                    self.assertEqual(trace_columns[name], ("TEXT", 1, None))
                post_trace_columns = _columns(connection, "community_post_trace")
                self.assertEqual(
                    set(post_trace_columns),
                    {
                        "trace_id",
                        "run_id",
                        "condition_id",
                        "manifest_sha256",
                        "phase_id",
                        "event_id",
                        "turn",
                        "date",
                        "author_agent_id",
                        "eligibility_status",
                        "posting_status",
                        "post_id",
                        "ltb_id",
                        "ltb_sha256",
                        "view_change_id",
                        "view_change_sha256",
                        "fill_id",
                        "prompt_template_sha256",
                        "prompt_values_sha256",
                        "logical_call_id",
                        "accepted_response_sha256",
                        "title_sha256",
                        "body_sha256",
                        "trace_sha256",
                        "created_at",
                    },
                )

                analysis_columns = _columns(connection, "paper_analyses")
                self.assertEqual(
                    set(analysis_columns),
                    {
                        "analysis_id",
                        "run_id",
                        "condition_id",
                        "manifest_sha256",
                        "agent_id",
                        "event_id",
                        "turn",
                        "date",
                        "subturn",
                        "source_ltb_id",
                        "source_stb_id",
                        "input_sha256",
                        "response_sha256",
                        "market_view",
                        "valuation_view",
                        "technical_view",
                        "news_view",
                        "portfolio_view",
                        "key_risks_json",
                        "opportunity_json",
                        "caution_json",
                        "directional_stance",
                        "confidence",
                        "evidence_references_json",
                        "scientific_sha256",
                        "created_at",
                    },
                )
                decision_columns = _columns(connection, "paper_decisions")
                self.assertEqual(decision_columns["analysis_id"], ("TEXT", 1, None))
                self.assertEqual(decision_columns["response_sha256"], ("TEXT", 1, None))
                self.assertEqual(analysis_columns["confidence"], ("TEXT", 1, None))
                foreign_tables = {
                    str(row[2]) for row in connection.execute("PRAGMA foreign_key_list(paper_decisions)")
                }
                self.assertIn("paper_analyses", foreign_tables)

            self.assertIn("paper_analyses", PAPER_RUNTIME_TABLES)
            self.assertIn("community_post_trace", PAPER_RUNTIME_TABLES)

    def test_empty_existing_paper_tables_receive_only_additive_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "legacy-paper.sqlite"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "CREATE TABLE short_term_belief_history (stb_id TEXT PRIMARY KEY, evidence_json TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE ltb_dimension_transitions "
                    "(transition_id TEXT PRIMARY KEY, integration_evidence_json TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE paper_decisions (decision_id TEXT PRIMARY KEY, scientific_sha256 TEXT NOT NULL)"
                )
                connection.commit()

            init_paper_sim_db(database)
            init_paper_sim_db(database)

            with sqlite3.connect(database) as connection:
                self.assertIn(
                    "dimension_evidence_json",
                    _columns(connection, "short_term_belief_history"),
                )
                self.assertIn(
                    "integration_evidence_by_dimension_json",
                    _columns(connection, "ltb_dimension_transitions"),
                )
                self.assertIn("analysis_id", _columns(connection, "paper_decisions"))

    def test_empty_pre_v7_analysis_table_is_rebuilt_for_full_legacy_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "old-empty.sqlite"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "CREATE TABLE paper_analyses ("
                    "analysis_id TEXT PRIMARY KEY, "
                    "directional_stance TEXT NOT NULL "
                    "CHECK(directional_stance IN ('buy', 'sell'))"
                    ")"
                )
                connection.commit()

            init_paper_sim_db(database)
            with sqlite3.connect(database) as connection:
                definition = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'paper_analyses'"
                ).fetchone()[0]
                columns = _columns(connection, "paper_analyses")
            self.assertIn("'uncertain'", definition)
            self.assertIn("market_view", columns)
            self.assertIn("key_risks_json", columns)
            self.assertNotIn("summary", columns)

    def test_populated_pre_v7_analysis_table_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "old-populated.sqlite"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "CREATE TABLE paper_analyses ("
                    "analysis_id TEXT PRIMARY KEY, "
                    "directional_stance TEXT NOT NULL "
                    "CHECK(directional_stance IN ('buy', 'sell'))"
                    ")"
                )
                connection.execute(
                    "INSERT INTO paper_analyses (analysis_id, directional_stance) VALUES ('a1', 'buy')"
                )
                connection.commit()

            with self.assertRaisesRegex(RuntimeError, "create a new run-scoped database"):
                init_paper_sim_db(database)

    def test_pre_v7_rebuild_fails_when_any_other_scientific_table_has_a_row(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "old-other-row.sqlite"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "CREATE TABLE paper_analyses ("
                    "analysis_id TEXT PRIMARY KEY, "
                    "directional_stance TEXT NOT NULL "
                    "CHECK(directional_stance IN ('buy', 'sell', 'uncertain'))"
                    ")"
                )
                connection.execute(
                    "CREATE TABLE short_term_belief_history ("
                    "stb_id TEXT PRIMARY KEY, evidence_json TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO short_term_belief_history VALUES ('s1', '[]')"
                )
                connection.commit()

            with self.assertRaisesRegex(
                RuntimeError, "short_term_belief_history=1"
            ):
                init_paper_sim_db(database)

    def test_empty_pre_v8_human_log_tables_receive_additive_columns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "old-empty-human-log.sqlite"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "CREATE TABLE paper_ltb_states ("
                    "ltb_id TEXT PRIMARY KEY, human_log_sha256 TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE turn_belief_trace ("
                    "trace_id TEXT PRIMARY KEY, human_log_sha256 TEXT NOT NULL)"
                )
                connection.commit()

            init_paper_sim_db(database)
            with sqlite3.connect(database) as connection:
                ltb_columns = _columns(connection, "paper_ltb_states")
                trace_columns = _columns(connection, "turn_belief_trace")
            self.assertIn("belief_summary", ltb_columns)
            self.assertIn("view_change_json", ltb_columns)
            self.assertIn("human_log_renderer_sha256", ltb_columns)
            self.assertIn("human_log_json", trace_columns)
            self.assertIn("human_log_renderer_sha256", trace_columns)

    def test_populated_pre_v8_human_log_schema_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "old-populated-human-log.sqlite"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "CREATE TABLE paper_ltb_states ("
                    "ltb_id TEXT PRIMARY KEY, human_log_sha256 TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO paper_ltb_states VALUES ('l1', ?)",
                    ("0" * 64,),
                )
                connection.execute(PAPER_ANALYSES_DDL)
                connection.commit()

            with self.assertRaisesRegex(
                RuntimeError, "not sealed as the v8 deterministic human-log contract"
            ):
                init_paper_sim_db(database)

    def test_populated_rows_cannot_be_relabelled_from_v7_to_v8(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "mislabelled-v7.sqlite"
            init_paper_sim_db(database)
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    INSERT INTO paper_initial_portfolios (
                        initial_portfolio_id, run_id, condition_id,
                        manifest_sha256, agent_id, cash, quantity,
                        scientific_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "portfolio-1",
                        "run-1",
                        "RN_COMM_OFF",
                        "0" * 64,
                        "agent-1",
                        1000,
                        0,
                        "1" * 64,
                    ),
                )
                connection.execute(
                    """
                    UPDATE rn_schema_meta SET schema_value = 'rn_ab_v7'
                    WHERE schema_key = 'schema_version'
                    """
                )
                connection.commit()

            with self.assertRaisesRegex(
                RuntimeError, "instead of synthesizing or relabeling human logs"
            ):
                init_paper_sim_db(database)

    def test_populated_v8_database_cannot_synthesize_v9_post_trace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "populated-v8.sqlite"
            init_paper_sim_db(database)
            with sqlite3.connect(database) as connection:
                connection.execute("DROP TABLE community_post_trace")
                connection.execute(
                    """
                    INSERT INTO paper_initial_portfolios (
                        initial_portfolio_id, run_id, condition_id,
                        manifest_sha256, agent_id, cash, quantity,
                        scientific_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "portfolio-1",
                        "run-1",
                        "RN_COMM_ON",
                        "0" * 64,
                        "agent-1",
                        1000,
                        0,
                        "1" * 64,
                    ),
                )
                connection.execute(
                    """
                    UPDATE rn_schema_meta SET schema_value = 'rn_ab_v8'
                    WHERE schema_key = 'schema_version'
                    """
                )
                connection.commit()

            with self.assertRaisesRegex(
                RuntimeError,
                "predates the v9 private community-post trace",
            ):
                init_paper_sim_db(database)


def _columns(connection: sqlite3.Connection, table: str) -> dict[str, tuple[str, int, str | None]]:
    return {
        str(row[1]): (str(row[2]), int(row[3]), None if row[4] is None else str(row[4]))
        for row in connection.execute(f"PRAGMA table_info({table})")
    }


if __name__ == "__main__":
    unittest.main()
