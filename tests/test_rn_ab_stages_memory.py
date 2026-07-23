from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from validation.validate_realnews_community_ab import (
    ABContract,
    Event,
    read_and_validate_final_fills,
)
from twinmarket_kr.db.connection import connect
from twinmarket_kr.rn_ab.memory import (
    HUMAN_LOG_RENDERER_CODE_SHA256,
    HUMAN_LOG_RENDERER_VERSION,
    EventSchedule,
    PaperMemoryError,
    PaperMemoryStore,
    canonical_json,
    human_log_sha256,
    scientific_sha256,
)
from twinmarket_kr.rn_ab.news import (
    SealedNewsRegistry,
    article_payload_sha256,
    bundle_content_sha256,
    fake_registry_sha256,
)
from twinmarket_kr.rn_ab.stage_inputs import SealedStageInputRegistry
from twinmarket_kr.rn_ab.stages import CurrentEvidencePacket, StageContractError, build_decision_packet


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _belief(label: str) -> dict[str, str]:
    return {f"dim_{index}": f"{label} dimension {index}" for index in range(1, 7)}


def _empty_dimension_evidence() -> dict[str, dict[str, list[str]]]:
    return {
        f"dim_{index}": {"support": [], "contradict": []}
        for index in range(1, 7)
    }


def _trade_policy() -> dict:
    return {
        "stock_code": "005930",
        "decision_space": ["buy", "sell"],
        "allow_hold": False,
        "max_single_trade_cash_ratio": 0.5,
        "fill_policy": "full_fill_at_sealed_event_price",
        "commission_rate": 0.0,
        "commission_applies_to": [],
        "sell_tax_rate": 0.0,
        "fee_policy": "zero_fee_v1",
        "target_direction_notional": "gross_signed_fill_value",
    }


def _schedule() -> EventSchedule:
    return EventSchedule.from_rows(
        [
            {
                "event_id": "e1",
                "turn": 1,
                "date": "2026-02-27",
                "subturn": "am",
                "execution_price": 100,
            }
        ]
    )


def _sealed_inputs(*, published_at: str = "2026-02-27T07:00:00+09:00", observed_at: str = "2026-02-27T07:30:00+09:00") -> tuple[SealedNewsRegistry, SealedStageInputRegistry]:
    article = {
        "article_id": "real-1",
        "title": "Samsung operating update",
        "summary": "A neutral update available before the AM decision.",
        "published_at": published_at,
        "observed_at": observed_at,
        "last_modified_at": observed_at,
        "source_url": "https://example.com/real-1",
        "source": "ExampleWire",
        "raw_body_sha256": _digest("raw-body"),
        "version_sha256": _digest("source-version"),
        "cutoff_version_sha256": _digest("cutoff-version"),
    }
    article["payload_sha256"] = article_payload_sha256(article)
    bundle = {
        "artifact_type": "real_news_bundle_manifest",
        "bundle_sha256": "0" * 64,
        "stock_code": "005930",
        "target_real_news_per_event": 1,
        "fake_news_per_event": 0,
        "articles": [article],
        "slots": [
            {
                "event_id": "e1",
                "slot_ordinal": 1,
                "article_id": article["article_id"],
                "payload_sha256": article["payload_sha256"],
            }
        ],
        "accepted_shortages": {},
        "known_fake_ids": [],
        "known_fake_payload_hashes": [],
        "fake_registry_sha256": fake_registry_sha256(
            known_fake_ids=[], known_fake_payload_hashes=[]
        ),
    }
    bundle["bundle_sha256"] = bundle_content_sha256(bundle)
    stage = {
        "artifact_type": "rn_stage_input_registry",
        "version": "v1",
        "calendar_event_registry_sha256": _digest("calendar"),
        "events": [
            {
                "event_id": "e1",
                "date": "2026-02-27",
                "subturn": "am",
                "news_cutoff_timestamp": "2026-02-27T09:00:00+09:00",
                "market_feature_as_of": "2026-02-27T09:00:00+09:00",
                "market": {
                    "reference_price": 100,
                    "previous_close": 99,
                    "open_price": 100,
                    "as_of_timestamp": "2026-02-27T09:00:00+09:00",
                },
            }
        ],
    }
    return SealedNewsRegistry.from_mapping(bundle), SealedStageInputRegistry.from_mapping(stage)


class _NoClaims:
    def validate_claims_for_agent(self, *, agent_id: str, event_id: str, claims: list[dict]) -> tuple[()]:
        if agent_id != "agent-1" or event_id != "e1" or claims:
            raise AssertionError("test claim verifier received an unexpected claim")
        return ()


class StagesAndMemoryBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.schedule = _schedule()
        self.news, self.stage_inputs = _sealed_inputs()
        self.store = PaperMemoryStore(
            Path(self.tempdir.name) / "run.sqlite",
            run_id="run-1",
            condition_id="RN_COMM_OFF",
            manifest_sha256=_digest("manifest"),
            event_schedule=self.schedule,
            news_registry=self.news,
            stage_input_registry=self.stage_inputs,
            initial_portfolios={"agent-1": {"cash": 1_000, "quantity": 0}},
            trade_policy=_trade_policy(),
            belief_limits={"dim_1": 150, **{f"dim_{index}": 100 for index in range(2, 7)}},
        )

    def _current_evidence(self) -> dict:
        return {
            "event_id": "e1",
            "date": "2026-02-27",
            "subturn": "am",
            "news": [self.news.articles["real-1"].stage_projection()],
            "depth2_search_results": [],
            "community_claims": [],
        }

    def _bootstrap_and_stb(self) -> tuple[str, str, dict[str, str]]:
        belief = _belief("initial")
        parent_ltb_id = self.store.bootstrap_ltb(
            agent_id="agent-1", date="2026-02-27", dimensions=belief
        )
        stb_id = self.store.save_stb(
            agent_id="agent-1",
            event_id="e1",
            turn=1,
            date="2026-02-27",
            dimensions=_belief("stb"),
            dimension_evidence=_empty_dimension_evidence(),
            current_evidence=self._current_evidence(),
        )
        return parent_ltb_id, stb_id, belief

    def _record_analysis(
        self,
        *,
        parent_ltb_id: str,
        stb_id: str,
        packet: dict,
        directional_stance: str = "buy",
        evidence_references: list[dict[str, str]] | None = None,
    ) -> str:
        return self.store.record_analysis(
            agent_id="agent-1",
            event_id="e1",
            turn=1,
            date="2026-02-27",
            subturn="am",
            source_ltb_id=parent_ltb_id,
            source_stb_id=stb_id,
            analysis_packet=packet,
            market_view="The sealed current market is mixed.",
            valuation_view="The available valuation signal is broadly fair.",
            technical_view="The short-term technical picture is mixed.",
            news_view="Visible news is mildly constructive.",
            portfolio_view="The sealed cash balance supports a constrained order.",
            key_risks=["price volatility", "limited evidence"],
            opportunity="A limited entry remains feasible.",
            caution=["Avoid excessive concentration."],
            directional_stance=directional_stance,
            confidence="medium",
            evidence_references=evidence_references
            or [
                {"source": "previous_ltb", "field": "dim_1"},
                {"source": "current_stb", "field": "dim_1"},
                {"source": "market", "field": "reference_price"},
                {"source": "execution_state", "field": "max_buy_quantity"},
            ],
        )

    def _record_valid_fill(
        self,
        *,
        parent_ltb_id: str,
        stb_id: str,
        previous_ltb: dict[str, str],
        current_stb: dict[str, str],
    ) -> str:
        packet = build_decision_packet(
            event_schedule=self.schedule,
            stage_input_registry=self.stage_inputs,
            store=self.store,
            agent_id="agent-1",
            event_id="e1",
            previous_ltb=previous_ltb,
            current_stb=current_stb,
        )
        analysis_id = self._record_analysis(
            parent_ltb_id=parent_ltb_id,
            stb_id=stb_id,
            packet=packet,
        )
        decision_id = self.store.record_decision(
            agent_id="agent-1",
            event_id="e1",
            turn=1,
            date="2026-02-27",
            subturn="am",
            action="buy",
            requested_quantity=5,
            source_ltb_id=parent_ltb_id,
            source_stb_id=stb_id,
            analysis_id=analysis_id,
            decision_packet=packet,
            decision_response=self._decision_response(action="buy", quantity=5),
        )
        return self.store.record_fill(
            agent_id="agent-1",
            event_id="e1",
            turn=1,
            date="2026-02-27",
            subturn="am",
            action="buy",
            requested_quantity=5,
            source_ltb_id=parent_ltb_id,
            source_stb_id=stb_id,
            decision_id=decision_id,
        )

    @staticmethod
    def _decision_response(*, action: str, quantity: int) -> dict[str, object]:
        return {
            "action": action,
            "requested_quantity": quantity,
            "reason": "Validated beliefs and current constraints support this order.",
            "risk_control": "Keep the order within the sealed maximum quantity.",
        }

    def test_stb_persistence_boundary_accepts_n_and_rejects_n_plus_one(self) -> None:
        exact = {
            "dim_1": "가" * 150,
            **{f"dim_{index}": "나" * 100 for index in range(2, 7)},
        }
        self.store.save_stb(
            agent_id="agent-1",
            event_id="e1",
            turn=1,
            date="2026-02-27",
            dimensions=exact,
            dimension_evidence=_empty_dimension_evidence(),
            current_evidence=self._current_evidence(),
        )
        too_long = {**exact, "dim_4": "나" * 101}
        with self.assertRaisesRegex(
            PaperMemoryError,
            r"stb\.dim_4 exceeds its sealed character limit 100",
        ):
            self.store.save_stb(
                agent_id="agent-1",
                event_id="e1",
                turn=1,
                date="2026-02-27",
                dimensions=too_long,
                dimension_evidence=_empty_dimension_evidence(),
                current_evidence=self._current_evidence(),
            )

    def test_ltb_persistence_boundary_accepts_n_and_rejects_n_plus_one(self) -> None:
        parent_ltb_id, stb_id, previous_ltb = self._bootstrap_and_stb()
        fill_id = self._record_valid_fill(
            parent_ltb_id=parent_ltb_id,
            stb_id=stb_id,
            previous_ltb=previous_ltb,
            current_stb=_belief("stb"),
        )
        exact = {
            "dim_1": "다" * 150,
            **{f"dim_{index}": "라" * 100 for index in range(2, 7)},
        }
        self.store.save_post_fill_ltb(
            agent_id="agent-1",
            event_id="e1",
            turn=1,
            date="2026-02-27",
            parent_ltb_id=parent_ltb_id,
            stb_id=stb_id,
            fill_id=fill_id,
            dimensions=exact,
            integration_evidence_by_dimension=_empty_dimension_evidence(),
        )
        too_long = {**exact, "dim_1": "다" * 151}
        with self.assertRaisesRegex(
            PaperMemoryError,
            r"ltb\.dim_1 exceeds its sealed character limit 150",
        ):
            self.store.save_post_fill_ltb(
                agent_id="agent-1",
                event_id="e1",
                turn=1,
                date="2026-02-27",
                parent_ltb_id=parent_ltb_id,
                stb_id=stb_id,
                fill_id=fill_id,
                dimensions=too_long,
                integration_evidence_by_dimension=_empty_dimension_evidence(),
            )

    def test_human_log_renderer_has_golden_bytes_and_order_independence(self) -> None:
        parent = {f"dim_{index}": f"before {index}" for index in range(1, 7)}
        current = {f"dim_{index}": f"after {index}" for index in range(1, 7)}
        evidence = {
            f"dim_{index}": {
                "support": ["source-1"] if index == 1 else [],
                "contradict": [],
            }
            for index in range(1, 7)
        }
        rendered = self.store._render_human_log(
            parent=parent,
            current=current,
            integration_evidence_by_dimension=evidence,
        )
        rendered_reordered = self.store._render_human_log(
            parent=dict(reversed(tuple(parent.items()))),
            current=dict(reversed(tuple(current.items()))),
            integration_evidence_by_dimension=dict(
                reversed(tuple(evidence.items()))
            ),
        )
        expected = (
            '{"belief_summary":"dim_1: after 1\\ndim_2: after 2\\ndim_3: after 3'
            '\\ndim_4: after 4\\ndim_5: after 5\\ndim_6: after 6",'
            '"renderer_sha256":"cc123dd95a67935606d881e3b71e10b614b04e8d1f7b5a6d3f64ae5af9522346",'
            '"renderer_version":"rn-human-log-v1","view_change":['
            '{"after_sha256":"09804a07dd2e79569d586b2cc7254be3fa17d9ee03c09022f8f9495d2331bb13",'
            '"before_sha256":"9b56b0545eb81757f481a6c3df508d5cbb6a5ba384e300b0e5ab099c1c7f35cc",'
            '"dimension":"dim_1","integration_evidence":{"contradict":[],"support":["source-1"]}},'
            '{"after_sha256":"dc2c18d07a238b077c3e451c85d7778a88a3c958fcec151258a0c65050722d01",'
            '"before_sha256":"8ee622606475178566dcd993bf6c31f87059eab089a18ed48a8add6891535a9e",'
            '"dimension":"dim_2","integration_evidence":{"contradict":[],"support":[]}},'
            '{"after_sha256":"cb7aa9533e0366abdcecb8972ff9b51f8bb0ce3a20c8ec880ec8d1503a6e2c49",'
            '"before_sha256":"5d5cf7a76f2899d74a6a9200ce8c7931853bc9f6951177f2b7bdad8d6e5f884e",'
            '"dimension":"dim_3","integration_evidence":{"contradict":[],"support":[]}},'
            '{"after_sha256":"dc048b73fb732916c79872dee3e5c62466e38fbf45cf06ed6b3dcaf10a902861",'
            '"before_sha256":"db53f1b719a99ff3ae95b110f551fae9641685d0343b2c8a960f073979725e23",'
            '"dimension":"dim_4","integration_evidence":{"contradict":[],"support":[]}},'
            '{"after_sha256":"359ea4e808f1d23718ae0c9895e17942d8e22962549532659ccf61f358ea5b9d",'
            '"before_sha256":"e8f48d41e4653ada5fddab79e79bd7d401095362dca68b517b4bbbebedf333ea",'
            '"dimension":"dim_5","integration_evidence":{"contradict":[],"support":[]}},'
            '{"after_sha256":"bb43d6f360cd3ab2a91d03adac292c3cd1d990cd61939aa1eeff0004d8059795",'
            '"before_sha256":"363b2a23c0145a62bde58ccb2d28d9f72c2fd394f2698db9a79e60f095107d95",'
            '"dimension":"dim_6","integration_evidence":{"contradict":[],"support":[]}}]}'
        )
        self.assertEqual(
            HUMAN_LOG_RENDERER_CODE_SHA256,
            "cc123dd95a67935606d881e3b71e10b614b04e8d1f7b5a6d3f64ae5af9522346",
        )
        self.assertEqual(canonical_json(rendered), expected)
        self.assertEqual(canonical_json(rendered_reordered), expected)
        self.assertEqual(
            human_log_sha256(rendered),
            "2dca262de190365eb3e27aa75fc8d31e48f7cdd7d711b34a30c9a321d72b7371",
        )

    def test_server_human_log_is_persisted_reconstructed_and_hash_separated(self) -> None:
        parent_ltb_id, stb_id, previous_ltb = self._bootstrap_and_stb()
        fill_id = self._record_valid_fill(
            parent_ltb_id=parent_ltb_id,
            stb_id=stb_id,
            previous_ltb=previous_ltb,
            current_stb=_belief("stb"),
        )
        ltb_id = self.store.save_post_fill_ltb(
            agent_id="agent-1",
            event_id="e1",
            turn=1,
            date="2026-02-27",
            parent_ltb_id=parent_ltb_id,
            stb_id=stb_id,
            fill_id=fill_id,
            dimensions=_belief("ltb"),
            integration_evidence_by_dimension=_empty_dimension_evidence(),
        )
        trace_id = self.store.write_completed_turn_trace(
            agent_id="agent-1", event_id="e1"
        )
        ltb_human_log = self.store.human_log_for_ltb(ltb_id=ltb_id)
        trace_human_log = self.store.human_log_for_trace(trace_id=trace_id)
        self.assertEqual(ltb_human_log, trace_human_log)
        self.assertEqual(
            ltb_human_log["renderer_version"], HUMAN_LOG_RENDERER_VERSION
        )
        self.assertEqual(
            ltb_human_log["renderer_sha256"], HUMAN_LOG_RENDERER_CODE_SHA256
        )
        with connect(self.store.db_path, read_only=True) as connection:
            ltb_row = connection.execute(
                """
                SELECT scientific_sha256, belief_summary, view_change_json,
                       human_log_renderer_version, human_log_renderer_sha256,
                       human_log_sha256
                FROM paper_ltb_states WHERE ltb_id = ?
                """,
                (ltb_id,),
            ).fetchone()
            trace_row = connection.execute(
                """
                SELECT scientific_sha256, human_log_json,
                       human_log_renderer_version, human_log_renderer_sha256,
                       human_log_sha256
                FROM turn_belief_trace WHERE trace_id = ?
                """,
                (trace_id,),
            ).fetchone()
        scientific_before = str(ltb_row["scientific_sha256"])
        self.assertEqual(ltb_row["belief_summary"], ltb_human_log["belief_summary"])
        self.assertEqual(
            json.loads(ltb_row["view_change_json"]), ltb_human_log["view_change"]
        )
        self.assertEqual(
            ltb_row["human_log_sha256"], human_log_sha256(ltb_human_log)
        )
        self.assertNotEqual(ltb_row["human_log_sha256"], scientific_before)
        self.assertEqual(
            json.loads(trace_row["human_log_json"]), ltb_human_log
        )
        self.assertEqual(
            trace_row["human_log_sha256"], ltb_row["human_log_sha256"]
        )
        self.assertNotEqual(
            trace_row["scientific_sha256"], trace_row["human_log_sha256"]
        )

        tampered_trace_log = copy.deepcopy(ltb_human_log)
        tampered_trace_log["belief_summary"] = "tampered trace text"
        with connect(self.store.db_path) as connection:
            connection.execute(
                "UPDATE turn_belief_trace SET human_log_json = ? WHERE trace_id = ?",
                (canonical_json(tampered_trace_log), trace_id),
            )
            connection.commit()
        with self.assertRaisesRegex(
            PaperMemoryError, "differs from its authoritative LTB"
        ):
            self.store.human_log_for_trace(trace_id=trace_id)
        with connect(self.store.db_path) as connection:
            connection.execute(
                "UPDATE turn_belief_trace SET human_log_json = ? WHERE trace_id = ?",
                (canonical_json(ltb_human_log), trace_id),
            )
            connection.commit()

        with connect(self.store.db_path) as connection:
            connection.execute(
                "UPDATE paper_ltb_states SET belief_summary = ? WHERE ltb_id = ?",
                ("tampered human text", ltb_id),
            )
            connection.commit()
        with connect(self.store.db_path, read_only=True) as connection:
            scientific_after = str(
                connection.execute(
                    "SELECT scientific_sha256 FROM paper_ltb_states WHERE ltb_id = ?",
                    (ltb_id,),
                ).fetchone()["scientific_sha256"]
            )
        self.assertEqual(scientific_after, scientific_before)
        with self.assertRaisesRegex(
            PaperMemoryError, "differs from deterministic reconstruction"
        ):
            self.store.human_log_for_ltb(ltb_id=ltb_id)

    def test_bootstrap_ltb_requires_bound_exact_positive_limits(self) -> None:
        unbound = PaperMemoryStore(
            Path(self.tempdir.name) / "unbound.sqlite",
            run_id="run-unbound",
            condition_id="RN_COMM_OFF",
            manifest_sha256=_digest("unbound-manifest"),
            event_schedule=self.schedule,
            news_registry=self.news,
            stage_input_registry=self.stage_inputs,
            initial_portfolios={"agent-1": {"cash": 1_000, "quantity": 0}},
            trade_policy=_trade_policy(),
        )
        with self.assertRaisesRegex(PaperMemoryError, "without sealed belief_limits"):
            unbound.bootstrap_ltb(
                agent_id="agent-1",
                date="2026-02-27",
                dimensions=_belief("initial"),
            )

        with self.assertRaisesRegex(PaperMemoryError, "exactly dim_1 through dim_6"):
            PaperMemoryStore(
                Path(self.tempdir.name) / "missing-limit.sqlite",
                run_id="run-missing-limit",
                condition_id="RN_COMM_OFF",
                manifest_sha256=_digest("missing-limit-manifest"),
                event_schedule=self.schedule,
                news_registry=self.news,
                stage_input_registry=self.stage_inputs,
                initial_portfolios={"agent-1": {"cash": 1_000, "quantity": 0}},
                trade_policy=_trade_policy(),
                belief_limits={f"dim_{index}": 100 for index in range(1, 6)},
            )
        with self.assertRaisesRegex(PaperMemoryError, r"belief_limits\.dim_6 must be a positive integer"):
            PaperMemoryStore(
                Path(self.tempdir.name) / "nonpositive-limit.sqlite",
                run_id="run-nonpositive-limit",
                condition_id="RN_COMM_OFF",
                manifest_sha256=_digest("nonpositive-limit-manifest"),
                event_schedule=self.schedule,
                news_registry=self.news,
                stage_input_registry=self.stage_inputs,
                initial_portfolios={"agent-1": {"cash": 1_000, "quantity": 0}},
                trade_policy=_trade_policy(),
                belief_limits={
                    **{f"dim_{index}": 100 for index in range(1, 6)},
                    "dim_6": 0,
                },
            )

    def test_current_evidence_and_persistence_reject_forged_news(self) -> None:
        forged = self._current_evidence()
        forged["news"][0] = {**forged["news"][0], "article_id": "forged", "payload_sha256": "f" * 64}
        with self.assertRaisesRegex(PaperMemoryError, "sealed delivery"):
            self.store.save_stb(
                agent_id="agent-1",
                event_id="e1",
                turn=1,
                date="2026-02-27",
                dimensions=_belief("forged"),
                dimension_evidence=_empty_dimension_evidence(),
                current_evidence=forged,
            )

        late_news, late_stage_inputs = _sealed_inputs(
            published_at="2026-02-27T10:00:00+09:00",
            observed_at="2026-02-27T10:00:00+09:00",
        )
        with self.assertRaisesRegex(StageContractError, "later than"):
            CurrentEvidencePacket.from_mapping(
                {
                    "event_id": "e1",
                    "date": "2026-02-27",
                    "subturn": "am",
                    "news": [late_news.articles["real-1"].stage_projection()],
                    "depth2_search_results": [],
                    "community_claims": [],
                },
                agent_id="agent-1",
                event_schedule=self.schedule,
                news_registry=late_news,
                stage_input_registry=late_stage_inputs,
                claim_verifier=_NoClaims(),
                expected_news_depth=1,
            )

    def test_analysis_requires_all_four_typed_sources_and_persists_uncertain_stance(self) -> None:
        parent_ltb_id, stb_id, belief = self._bootstrap_and_stb()
        packet = build_decision_packet(
            event_schedule=self.schedule,
            stage_input_registry=self.stage_inputs,
            store=self.store,
            agent_id="agent-1",
            event_id="e1",
            previous_ltb=belief,
            current_stb=_belief("stb"),
        )
        with self.assertRaisesRegex(PaperMemoryError, "every required source"):
            self._record_analysis(
                parent_ltb_id=parent_ltb_id,
                stb_id=stb_id,
                packet=packet,
                evidence_references=[
                    {"source": "previous_ltb", "field": "dim_1"},
                    {"source": "current_stb", "field": "dim_1"},
                    {"source": "market", "field": "reference_price"},
                ],
            )
        analysis_id = self._record_analysis(
            parent_ltb_id=parent_ltb_id,
            stb_id=stb_id,
            packet=packet,
            directional_stance="uncertain",
        )
        with connect(self.store.db_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT directional_stance, key_risks_json, opportunity_json, caution_json "
                "FROM paper_analyses WHERE analysis_id = ?",
                (analysis_id,),
            ).fetchone()
        self.assertEqual(row["directional_stance"], "uncertain")
        self.assertEqual(json.loads(row["key_risks_json"]), ["price volatility", "limited evidence"])
        self.assertEqual(json.loads(row["opportunity_json"]), "A limited entry remains feasible.")
        self.assertEqual(json.loads(row["caution_json"]), ["Avoid excessive concentration."])
        restored = self.store.current_analysis(agent_id="agent-1", event_id="e1", turn=1)
        self.assertEqual(restored["key_risks"], ["price volatility", "limited evidence"])
        self.assertEqual(restored["opportunity"], "A limited entry remains feasible.")
        self.assertNotIn("summary", restored)
        with connect(self.store.db_path) as connection:
            connection.execute(
                "UPDATE paper_analyses SET market_view = 'tampered' WHERE analysis_id = ?",
                (analysis_id,),
            )
            connection.commit()
        with self.assertRaisesRegex(PaperMemoryError, "response hash"):
            self.store.current_analysis(agent_id="agent-1", event_id="e1", turn=1)

    def test_sealed_decision_and_fill_enforce_cash_ratio_and_portfolio_transition(self) -> None:
        parent_ltb_id, stb_id, belief = self._bootstrap_and_stb()
        packet = build_decision_packet(
            event_schedule=self.schedule,
            stage_input_registry=self.stage_inputs,
            store=self.store,
            agent_id="agent-1",
            event_id="e1",
            previous_ltb=belief,
            current_stb=_belief("stb"),
        )
        self.assertEqual(packet["execution_state"]["max_buy_quantity"], 5)
        analysis_id = self._record_analysis(
            parent_ltb_id=parent_ltb_id, stb_id=stb_id, packet=packet
        )

        forged_packet = copy.deepcopy(packet)
        forged_packet["execution_state"]["max_buy_quantity"] = 999_999_999
        forged_packet["input_sha256"] = scientific_sha256(
            {key: value for key, value in forged_packet.items() if key != "input_sha256"}
        )
        with self.assertRaisesRegex(PaperMemoryError, "execution state"):
            self.store.record_decision(
                agent_id="agent-1",
                event_id="e1",
                turn=1,
                date="2026-02-27",
                subturn="am",
                action="buy",
                requested_quantity=6,
                source_ltb_id=parent_ltb_id,
                source_stb_id=stb_id,
                analysis_id=analysis_id,
                decision_packet=forged_packet,
                decision_response=self._decision_response(action="buy", quantity=6),
            )
        with self.assertRaisesRegex(PaperMemoryError, "quantity exceeds"):
            self.store.record_decision(
                agent_id="agent-1",
                event_id="e1",
                turn=1,
                date="2026-02-27",
                subturn="am",
                action="buy",
                requested_quantity=6,
                source_ltb_id=parent_ltb_id,
                source_stb_id=stb_id,
                analysis_id=analysis_id,
                decision_packet=packet,
                decision_response=self._decision_response(action="buy", quantity=6),
            )

        decision_id = self.store.record_decision(
            agent_id="agent-1",
            event_id="e1",
            turn=1,
            date="2026-02-27",
            subturn="am",
            action="buy",
            requested_quantity=5,
            source_ltb_id=parent_ltb_id,
            source_stb_id=stb_id,
            analysis_id=analysis_id,
            decision_packet=packet,
            decision_response=self._decision_response(action="buy", quantity=5),
        )
        self.store.record_fill(
            agent_id="agent-1",
            event_id="e1",
            turn=1,
            date="2026-02-27",
            subturn="am",
            action="buy",
            requested_quantity=5,
            source_ltb_id=parent_ltb_id,
            source_stb_id=stb_id,
            decision_id=decision_id,
        )
        with connect(self.store.db_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT stock_code, fee_amount, pre_portfolio_json, post_portfolio_json FROM paper_fill_ledger"
            ).fetchone()
        self.assertEqual(row["stock_code"], "005930")
        self.assertEqual(row["fee_amount"], 0)
        self.assertEqual(row["pre_portfolio_json"], '{"cash":1000.0,"quantity":0}')
        self.assertEqual(row["post_portfolio_json"], '{"cash":500.0,"quantity":5}')

    def test_canonical_fill_csv_is_directly_accepted_by_the_paper_validator(self) -> None:
        parent_ltb_id, stb_id, belief = self._bootstrap_and_stb()
        packet = build_decision_packet(
            event_schedule=self.schedule,
            stage_input_registry=self.stage_inputs,
            store=self.store,
            agent_id="agent-1",
            event_id="e1",
            previous_ltb=belief,
            current_stb=_belief("stb"),
        )
        analysis_id = self._record_analysis(
            parent_ltb_id=parent_ltb_id, stb_id=stb_id, packet=packet
        )
        decision_id = self.store.record_decision(
            agent_id="agent-1",
            event_id="e1",
            turn=1,
            date="2026-02-27",
            subturn="am",
            action="buy",
            requested_quantity=5,
            source_ltb_id=parent_ltb_id,
            source_stb_id=stb_id,
            analysis_id=analysis_id,
            decision_packet=packet,
            decision_response=self._decision_response(action="buy", quantity=5),
        )
        self.store.record_fill(
            agent_id="agent-1",
            event_id="e1",
            turn=1,
            date="2026-02-27",
            subturn="am",
            action="buy",
            requested_quantity=5,
            source_ltb_id=parent_ltb_id,
            source_stb_id=stb_id,
            decision_id=decision_id,
        )
        evaluator_hash = _digest("evaluator-contract")
        csv_path = self.store.export_canonical_final_fill_ledger(
            Path(self.tempdir.name) / "canonical_fills.csv",
            evaluator_contract_sha256=evaluator_hash,
        )
        fills = read_and_validate_final_fills(
            csv_path,
            contract=ABContract(
                manifest_hash=evaluator_hash,
                pair_id="pair-1",
                off_condition_id="RN_COMM_OFF",
                on_condition_id="RN_COMM_ON",
                cohort_agent_ids=("agent-1",),
                events=(Event("e1", "2026-02-27", "AM", Decimal("100")),),
                burn_in_dates=("2026-02-27",),
                evaluation_dates=("2026-02-27",),
                target_values=None,
            ),
            condition_id="RN_COMM_OFF",
        )
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].fill_status, "filled")
        self.assertEqual(fills[0].fill_price, Decimal("100"))


if __name__ == "__main__":
    unittest.main()
