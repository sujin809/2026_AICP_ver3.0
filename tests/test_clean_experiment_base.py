from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from twinmarket_kr.belief_projection import (
    BELIEF_DIMENSION_KEYS,
    render_belief_summary,
)
from twinmarket_kr.db.connection import init_sim_db
from twinmarket_kr.experiment_runtime import (
    RUNTIME_TABLES,
    build_clean_experiment_base,
    deterministic_initial_belief,
    validate_clean_experiment_base,
)
from twinmarket_kr.llm.belief import render_ltb_human_log


AGENT_ID = "A001"
INITIAL_CASH = 100_000_000.0


def _seed_agent_source(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE agents (
                agent_id TEXT PRIMARY KEY,
                ini_cash REAL NOT NULL,
                persona_prompt TEXT NOT NULL,
                news_depth INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO agents(agent_id, ini_cash, persona_prompt, news_depth)
            VALUES (?, ?, ?, ?)
            """,
            (AGENT_ID, INITIAL_CASH, "sealed persona", 1),
        )
        connection.commit()


def _insert_six_dims(prefix: str) -> tuple[str, ...]:
    return tuple(f"{prefix}-dim-{index}" for index in range(1, 7))


def _seed_simulation_source(path: Path) -> None:
    init_sim_db(path)
    initial_dims = _insert_six_dims("initial")
    runtime_dims = _insert_six_dims("runtime")
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO StockData(
                date, stock_id, open_price, high_price, low_price, close_price,
                volume, pct_chg, volume_chg, ma5, ma20, volatility_20d
            )
            VALUES ('2026-02-27', '005930', 100, 102, 99, 101,
                    1000, 1, 10, 100, 99, 0.02)
            """
        )
        connection.execute(
            """
            INSERT INTO belief_history(
                belief_id, agent_id, turn, date,
                dim_1, dim_2, dim_3, dim_4, dim_5, dim_6,
                belief_summary, view_change
            )
            VALUES (?, ?, 0, '2026-02-26', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "belief-initial",
                AGENT_ID,
                *initial_dims,
                "initial belief summary",
                "initial view",
            ),
        )
        connection.execute(
            """
            INSERT INTO belief_history(
                belief_id, agent_id, turn, date,
                dim_1, dim_2, dim_3, dim_4, dim_5, dim_6,
                belief_summary, view_change
            )
            VALUES (?, ?, 1, '2026-02-27', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "belief-runtime",
                AGENT_ID,
                *runtime_dims,
                "runtime belief summary",
                "runtime view",
            ),
        )
        connection.execute(
            """
            INSERT INTO portfolio_state(
                state_id, agent_id, turn, date, cash, positions, total_value,
                realized_pnl, total_return_rate
            )
            VALUES (?, ?, 0, '2026-02-26', ?, '[]', ?, 0, 0)
            """,
            ("portfolio-initial", AGENT_ID, INITIAL_CASH, INITIAL_CASH),
        )
        connection.execute(
            """
            INSERT INTO portfolio_state(
                state_id, agent_id, turn, date, cash, positions, total_value,
                realized_pnl, total_return_rate
            )
            VALUES (?, ?, 1, '2026-02-27', ?, ?, ?, 0, 0)
            """,
            (
                "portfolio-runtime",
                AGENT_ID,
                INITIAL_CASH - 100,
                json.dumps([{"stock_code": "005930", "quantity": 1}]),
                INITIAL_CASH + 1,
            ),
        )
        connection.execute(
            """
            INSERT INTO simulation_ltb_states(
                ltb_id, agent_id, turn, visible_from_turn, date, subturn,
                parent_ltb_id, source_stb_id, source_decision_id, source_fill_id,
                dim_1, dim_2, dim_3, dim_4, dim_5, dim_6,
                integration_evidence_json, scientific_sha256,
                belief_summary, view_change_json, human_log_sha256
            )
            VALUES (?, ?, 0, 1, '2026-02-26', 'initial',
                    NULL, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?,
                    '{}', ?, ?, '{}', ?)
            """,
            (
                "ltb-initial",
                AGENT_ID,
                *initial_dims,
                "sha-ltb-initial",
                "initial long-term belief",
                "sha-human-initial",
            ),
        )
        connection.execute(
            """
            INSERT INTO simulation_stb_states(
                stb_id, agent_id, turn, date, subturn,
                dim_1, dim_2, dim_3, dim_4, dim_5, dim_6,
                evidence_json, dimension_evidence_json, scientific_sha256
            )
            VALUES (?, ?, 1, '2026-02-27', 'am', ?, ?, ?, ?, ?, ?,
                    '[]', '{}', ?)
            """,
            ("stb-1", AGENT_ID, *runtime_dims, "sha-stb-1"),
        )
        connection.execute(
            """
            INSERT INTO simulation_analyses(
                analysis_id, agent_id, turn, date, subturn,
                source_ltb_id, source_stb_id, analysis_json, scientific_sha256
            )
            VALUES (?, ?, 1, '2026-02-27', 'am', ?, ?, '{}', ?)
            """,
            ("analysis-1", AGENT_ID, "ltb-initial", "stb-1", "sha-analysis-1"),
        )
        connection.execute(
            """
            INSERT INTO simulation_decisions(
                decision_id, agent_id, turn, date, subturn, action,
                requested_quantity, source_ltb_id, source_stb_id, analysis_id,
                decision_json, scientific_sha256
            )
            VALUES (?, ?, 1, '2026-02-27', 'am', 'buy',
                    1, ?, ?, ?, '{}', ?)
            """,
            (
                "decision-1",
                AGENT_ID,
                "ltb-initial",
                "stb-1",
                "analysis-1",
                "sha-decision-1",
            ),
        )
        connection.execute(
            """
            INSERT INTO simulation_fills(
                fill_id, agent_id, turn, date, subturn, stock_code, action,
                requested_quantity, filled_quantity, executed_price, fee,
                source_ltb_id, source_stb_id, decision_id,
                pre_portfolio_json, post_portfolio_json, scientific_sha256
            )
            VALUES (?, ?, 1, '2026-02-27', 'am', '005930', 'buy',
                    1, 1, 100, 0, ?, ?, ?, '{}', '{}', ?)
            """,
            (
                "fill-1",
                AGENT_ID,
                "ltb-initial",
                "stb-1",
                "decision-1",
                "sha-fill-1",
            ),
        )
        connection.execute(
            """
            INSERT INTO simulation_ltb_states(
                ltb_id, agent_id, turn, visible_from_turn, date, subturn,
                parent_ltb_id, source_stb_id, source_decision_id, source_fill_id,
                dim_1, dim_2, dim_3, dim_4, dim_5, dim_6,
                integration_evidence_json, scientific_sha256,
                belief_summary, view_change_json, human_log_sha256
            )
            VALUES (?, ?, 1, 2, '2026-02-27', 'am', ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, '{}', ?, ?, '{}', ?)
            """,
            (
                "ltb-1",
                AGENT_ID,
                "ltb-initial",
                "stb-1",
                "decision-1",
                "fill-1",
                *runtime_dims,
                "sha-ltb-1",
                "post-fill long-term belief",
                "sha-human-1",
            ),
        )
        for horizon, due_event_id, mark_price in (
            ("h1", "2026-03-02/am", 101.0),
            ("h5", "2026-03-06/am", 105.0),
        ):
            outcome_id = f"outcome-{horizon}"
            connection.execute(
                """
                INSERT INTO simulation_trade_outcomes(
                    outcome_id, fill_id, horizon, due_event_id,
                    available_from_event_id, observed_event_id, mark_price,
                    status, scientific_sha256
                )
                VALUES (?, 'fill-1', ?, ?, ?, ?, ?, 'matured', ?)
                """,
                (
                    outcome_id,
                    horizon,
                    due_event_id,
                    due_event_id,
                    due_event_id,
                    mark_price,
                    f"sha-{outcome_id}",
                ),
            )
            connection.execute(
                """
                INSERT INTO simulation_outcome_consumptions(
                    consumption_id, outcome_id, fill_id, horizon, ltb_id,
                    consumed_at_event_id
                )
                VALUES (?, ?, 'fill-1', ?, 'ltb-1', ?)
                """,
                (
                    f"consumption-{horizon}",
                    outcome_id,
                    horizon,
                    due_event_id,
                ),
            )
        connection.execute(
            """
            INSERT INTO trade_log(
                log_id, agent_id, turn, date, action, stock_code, quantity,
                executed_price, trade_value, fee, action_reason, risk_control,
                order_type, submitted_price, status, filled_quantity,
                analysis_id, decision_id, source_ltb_id, source_stb_id,
                fill_id, post_fill_ltb_id
            )
            VALUES ('trade-1', ?, 1, '2026-02-27', 'buy', '005930', 1,
                    100, 100, 0, 'reason', 'risk', 'market', 100, 'filled', 1,
                    'analysis-1', 'decision-1', 'ltb-initial', 'stb-1',
                    'fill-1', 'ltb-1')
            """,
            (AGENT_ID,),
        )
        connection.execute(
            """
            INSERT INTO TradingDetails(
                date, stock_id, user_id, trading_direction, price, volume
            )
            VALUES ('2026-02-27', '005930', ?, 'buy', 100, 1)
            """,
            (AGENT_ID,),
        )
        connection.execute(
            """
            INSERT INTO agent_system_messages(
                agent_id, turn, date, message_type, message
            )
            VALUES (?, 1, '2026-02-27', 'runtime', 'runtime-only message')
            """,
            (AGENT_ID,),
        )
        connection.execute(
            """
            INSERT INTO community_posts(
                post_id, agent_id, anonymous_code, turn, date, post_type,
                title, content, like_count, unlike_count, score, is_best,
                source_ltb_id, source_fill_id, source_decision_id
            )
            VALUES (1, ?, '익명001', 1, '2026-02-27', 'analysis',
                    'title', 'full body', 1, 0, 1, 1,
                    'ltb-1', 'fill-1', 'decision-1')
            """,
            (AGENT_ID,),
        )
        connection.execute(
            """
            INSERT INTO community_interactions(
                agent_id, post_id, turn, date, reaction
            )
            VALUES (?, 1, 1, '2026-02-27', 'like')
            """,
            (AGENT_ID,),
        )
        connection.execute(
            """
            INSERT INTO community_logs(
                agent_id, turn, date, best_posts_seen, posts_read,
                candidate_posts_seen, community_thinking
            )
            VALUES (?, 1, '2026-02-27', '[1]', '[1]', '[1]', 'thinking')
            """,
            (AGENT_ID,),
        )
        connection.commit()


class CleanExperimentBaseTest(unittest.TestCase):
    def test_canonical_builder_regenerates_turn_zero_without_an_llm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.db"
            target = root / "canonical.db"
            init_sim_db(source)
            with sqlite3.connect(source) as connection:
                connection.execute(
                    """
                    INSERT INTO StockData(
                        date, stock_id, open_price, high_price, low_price,
                        close_price, volume, pct_chg, volume_chg, ma5, ma20,
                        volatility_20d
                    )
                    VALUES ('2026-02-27', '005930', 100, 102, 99, 101,
                            1000, 1, 10, 100, 99, 0.02)
                    """
                )
                connection.commit()
            agents = [
                {
                    "agent_id": "A001",
                    "ini_cash": 100_000_000,
                    "strategy": "value",
                },
                {
                    "agent_id": "A002",
                    "ini_cash": 1_000_000_000,
                    "strategy": "technical",
                },
            ]

            report = build_clean_experiment_base(
                source,
                target,
                initial_agents=agents,
                instrument_name="삼성전자",
            )

            self.assertEqual(
                report["turn_zero_policy"],
                "deterministic_sealed_cohort_v1",
            )
            self.assertEqual(report["turn_zero_beliefs"], 2)
            self.assertEqual(report["turn_zero_portfolios"], 2)
            expected_value = deterministic_initial_belief(agents[0])
            expected_technical = deterministic_initial_belief(agents[1])
            with sqlite3.connect(target) as connection:
                beliefs = connection.execute(
                    """
                    SELECT agent_id, date, dim_1, dim_2, belief_summary,
                           view_change
                    FROM belief_history
                    ORDER BY agent_id
                    """
                ).fetchall()
                portfolios = connection.execute(
                    """
                    SELECT agent_id, date, cash, positions, total_value
                    FROM portfolio_state
                    ORDER BY agent_id
                    """
                ).fetchall()
            self.assertEqual(
                beliefs[0],
                (
                    "A001",
                    "t000",
                    expected_value["dim_1"],
                    expected_value["dim_2"],
                    expected_value["belief_summary"],
                    "initial",
                ),
            )
            self.assertEqual(
                beliefs[1][3],
                expected_technical["dim_2"],
            )
            self.assertEqual(
                portfolios,
                [
                    ("A001", "t000", 100_000_000.0, "[]", 100_000_000.0),
                    ("A002", "t000", 1_000_000_000.0, "[]", 1_000_000_000.0),
                ],
            )

    def test_initial_belief_summary_is_the_six_dimension_projection(self) -> None:
        """LTB₀의 사람용 summary는 자신의 6차원에서 결정론적으로 렌더링된다.

        EXPERIMENT_DESIGN.md 7.3절이 `belief_summary`를 저장된 여섯 차원의
        결정론적 projection으로 고정한다. 무호출 LTB₀ 경로가 별도 문장을 쓰면
        같은 필드가 post-fill LTB와 다른 규칙으로 채워진다.
        """

        for strategy in ("value", "technical"):
            with self.subTest(strategy=strategy):
                belief = deterministic_initial_belief(
                    {
                        "agent_id": AGENT_ID,
                        "ini_cash": INITIAL_CASH,
                        "strategy": strategy,
                    }
                )
                dimensions = {
                    key: belief[key] for key in BELIEF_DIMENSION_KEYS
                }
                self.assertEqual(
                    belief["belief_summary"],
                    render_belief_summary(dimensions),
                )
                # post-fill LTB 경로와 같은 renderer여야 한다.
                summary, _ = render_ltb_human_log(
                    previous_dimensions=dimensions,
                    current_dimensions=dimensions,
                )
                self.assertEqual(belief["belief_summary"], summary)
                # LTB₀은 parent가 없어 before/after 렌더링 대상이 아니다.
                self.assertEqual(belief["view_change"], "initial")

    def test_builder_removes_integrated_runtime_and_retains_static_turn_zero(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.db"
            target = root / "clean.db"
            sys_db = root / "sys_100.db"
            _seed_agent_source(sys_db)
            _seed_simulation_source(source)

            with sqlite3.connect(source) as connection:
                source_runtime_counts = {
                    table: int(
                        connection.execute(
                            f"SELECT COUNT(*) FROM {table}"
                        ).fetchone()[0]
                    )
                    for table in RUNTIME_TABLES
                }
                seeded_horizons = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT horizon FROM simulation_trade_outcomes"
                    )
                }
            self.assertTrue(all(source_runtime_counts.values()))
            self.assertEqual(seeded_horizons, {"h1", "h5"})

            with patch.object(config, "SYS_100_DB", sys_db):
                report = build_clean_experiment_base(source, target)

            self.assertEqual(report["status"], "pass")
            self.assertEqual(
                report["runtime_counts"],
                {table: 0 for table in RUNTIME_TABLES},
            )
            self.assertEqual(report["turn_zero_beliefs"], 1)
            self.assertEqual(report["turn_zero_portfolios"], 1)
            self.assertEqual(report["stock_rows"], 1)

            with sqlite3.connect(target) as connection:
                connection.row_factory = sqlite3.Row
                belief = connection.execute(
                    """
                    SELECT belief_id, agent_id, turn, belief_summary
                    FROM belief_history
                    """
                ).fetchone()
                portfolio = connection.execute(
                    """
                    SELECT state_id, agent_id, turn, cash, positions
                    FROM portfolio_state
                    """
                ).fetchone()
                stock = connection.execute(
                    "SELECT date, stock_id, close_price FROM StockData"
                ).fetchone()
                violations = connection.execute(
                    "PRAGMA foreign_key_check"
                ).fetchall()

            self.assertEqual(dict(belief), {
                "belief_id": "belief-initial",
                "agent_id": AGENT_ID,
                "turn": 0,
                "belief_summary": "initial belief summary",
            })
            self.assertEqual(portfolio["state_id"], "portfolio-initial")
            self.assertEqual(portfolio["agent_id"], AGENT_ID)
            self.assertEqual(portfolio["turn"], 0)
            self.assertEqual(portfolio["cash"], INITIAL_CASH)
            self.assertEqual(json.loads(portfolio["positions"]), [])
            self.assertEqual(dict(stock), {
                "date": "2026-02-27",
                "stock_id": "005930",
                "close_price": 101.0,
            })
            self.assertEqual(violations, [])

            with sqlite3.connect(sys_db) as connection:
                persona = connection.execute(
                    """
                    SELECT agent_id, ini_cash, persona_prompt, news_depth
                    FROM agents
                    """
                ).fetchone()
            self.assertEqual(
                persona,
                (AGENT_ID, INITIAL_CASH, "sealed persona", 1),
            )

    def test_validator_rejects_a_leaked_integrated_runtime_row(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.db"
            target = root / "clean.db"
            sys_db = root / "sys_100.db"
            _seed_agent_source(sys_db)
            _seed_simulation_source(source)
            with patch.object(config, "SYS_100_DB", sys_db):
                build_clean_experiment_base(source, target)
                with sqlite3.connect(target) as connection:
                    dims = _insert_six_dims("leaked")
                    connection.execute(
                        """
                        INSERT INTO simulation_stb_states(
                            stb_id, agent_id, turn, date, subturn,
                            dim_1, dim_2, dim_3, dim_4, dim_5, dim_6,
                            evidence_json, dimension_evidence_json,
                            scientific_sha256
                        )
                        VALUES ('leaked-stb', ?, 1, '2026-02-27', 'am',
                                ?, ?, ?, ?, ?, ?, '[]', '{}', 'sha-leaked')
                        """,
                        (AGENT_ID, *dims),
                    )
                    connection.commit()

                with self.assertRaisesRegex(
                    RuntimeError,
                    "simulation_stb_states",
                ):
                    validate_clean_experiment_base(target)


if __name__ == "__main__":
    unittest.main()
