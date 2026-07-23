from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from twinmarket_kr.db.schema import AGENTS_DDL
from twinmarket_kr.rn_ab.call_policy import StrictCallPolicy
from twinmarket_kr.rn_ab.journal import JournalIntegrityError, ResponseJournal
from twinmarket_kr.rn_ab.memory import EventSchedule, PaperMemoryError, PaperMemoryStore
from twinmarket_kr.rn_ab.news import (
    SealedNewsRegistry,
    article_payload_sha256,
    bundle_content_sha256,
    fake_registry_sha256,
)
from twinmarket_kr.rn_ab.persona_snapshot import SealedPersonaSnapshot, build_persona_snapshot
from twinmarket_kr.rn_ab.prompt_registry import RNPromptBundle
from twinmarket_kr.rn_ab.stage_adapter import (
    RNBeliefLimits,
    RN_STAGE_MAX_TOKENS_V1,
    RN_STAGE_RESPONSE_FORMAT,
    RN_STAGE_TEMPERATURE,
    RN_TRUSTED_SYSTEM_INSTRUCTION,
    RNStageAdapter,
    RNStageAdapterError,
    StrictOpenRouterStageModel,
    _parse_exact_json_object,
)
from twinmarket_kr.rn_ab.stage_inputs import SealedStageInputRegistry
from twinmarket_kr.rn_ab.stages import CurrentEvidencePacket


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _payload_from_rendered_prompt(prompt: str) -> dict:
    marker = "입력 정보(JSON):\n"
    if marker not in prompt:
        raise AssertionError("RN prompt is missing its canonical payload marker")
    return json.loads(prompt.rsplit(marker, 1)[1])


def _contains_mapping_key(value: object, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_mapping_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_mapping_key(item, key) for item in value)
    return False


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


class _FakeStageModel:
    def __init__(
        self,
        *,
        invalid_stb: bool = False,
        invalid_ltb_polarity: bool = False,
        inject_forbidden_belief_narrative: bool = False,
        analysis_stance: str = "buy",
    ) -> None:
        self.calls = 0
        self.invalid_stb = invalid_stb
        self.invalid_ltb_polarity = invalid_ltb_polarity
        self.inject_forbidden_belief_narrative = inject_forbidden_belief_narrative
        self.analysis_stance = analysis_stance
        self.prompts: list[str] = []
        self.max_tokens: list[int] = []

    async def complete(
        self,
        *,
        prompt: str,
        model: str,
        logical_call_id: str,
        phase_attempt_id: str,
        seed: int,
        max_tokens: int,
    ) -> str:
        self.calls += 1
        self.prompts.append(prompt)
        self.max_tokens.append(max_tokens)
        stage = logical_call_id.split("|")[4]
        if stage == "stb":
            evidence = "invented-evidence" if self.invalid_stb else "real-1"
            response = {
                **{f"dim_{index}": f"short current interpretation {index}" for index in range(1, 7)},
                "dimension_evidence": {
                    f"dim_{index}": {
                        "support": [evidence] if index == 1 else [],
                        "contradict": [],
                    }
                    for index in range(1, 7)
                },
            }
            if self.inject_forbidden_belief_narrative:
                response["belief_summary"] = "model-proposed narrative is forbidden"
        elif stage == "analysis":
            response = {
                "market_view": "Current price action is mixed.",
                "valuation_view": "Valuation is broadly fair.",
                "technical_view": "Short-term technical signals are mixed.",
                "news_view": "The visible news is mildly constructive.",
                "portfolio_view": "Available cash supports only a constrained position.",
                "key_risks": ["price volatility", "limited evidence"],
                "opportunity": "A limited entry remains feasible.",
                "caution": ["Avoid excessive concentration."],
                "confidence": "medium",
                "directional_stance": self.analysis_stance,
                "evidence_references": [
                    {"source": "previous_ltb", "field": "dim_1"},
                    {"source": "current_stb", "field": "dim_1"},
                    {"source": "market", "field": "reference_price"},
                    {"source": "execution_state", "field": "max_buy_quantity"},
                ],
            }
        elif stage == "decision":
            response = {
                "action": "buy",
                "requested_quantity": 5,
                "reason": "The typed analysis supports a limited buy within the sealed maximum.",
                "risk_control": "Keep quantity at the sealed maximum and use no unstated order controls.",
            }
        elif stage == "post_fill_ltb":
            response = {
                **{f"dim_{index}": f"recursive next-event belief {index}" for index in range(1, 7)},
                "integration_evidence": {
                    f"dim_{index}": {
                        "support": ["real-1"] if index == 1 and not self.invalid_ltb_polarity else [],
                        "contradict": ["real-1"] if index == 1 and self.invalid_ltb_polarity else [],
                    }
                    for index in range(1, 7)
                },
            }
            if self.inject_forbidden_belief_narrative:
                response["view_change"] = "model-proposed narrative is forbidden"
        else:  # pragma: no cover - exact stage set is the object under test.
            raise AssertionError(stage)
        return json.dumps(response, ensure_ascii=False)


class RNStageAdapterTests(unittest.TestCase):
    def test_strict_provider_request_shape_and_terminal_finish_are_sealed(self) -> None:
        policy = StrictCallPolicy(
            model="model-x", provider="provider-x", max_retries=1, concurrency=2
        )

        class Client:
            max_retries = 1
            model = "model-x"
            concurrency_limit = 2

            def __init__(self, finish_reason: str) -> None:
                self.finish_reason = finish_reason
                self.kwargs: dict[str, object] = {}
                self.messages: object | None = None

            async def chat_strict_reasoning_off(self, messages: object, **kwargs: object) -> object:
                self.messages = messages
                self.kwargs = kwargs
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            finish_reason=self.finish_reason,
                            message=SimpleNamespace(content="{}"),
                        )
                    ]
                )

        client = Client("stop")
        model = StrictOpenRouterStageModel(client, policy=policy)
        self.assertEqual(
            asyncio.run(
                model.complete(
                    prompt="{}",
                    model="model-x",
                    logical_call_id="id",
                    phase_attempt_id="phase",
                    seed=7,
                    max_tokens=1024,
                )
            ),
            "{}",
        )
        self.assertEqual(client.kwargs["temperature"], RN_STAGE_TEMPERATURE)
        self.assertEqual(client.kwargs["response_format"], dict(RN_STAGE_RESPONSE_FORMAT))
        self.assertEqual(
            client.messages,
            [
                {"role": "system", "content": RN_TRUSTED_SYSTEM_INSTRUCTION},
                {"role": "user", "content": "{}"},
            ],
        )
        with self.assertRaisesRegex(RNStageAdapterError, "temperature"):
            StrictOpenRouterStageModel(Client("stop"), policy=policy, temperature=0.7)
        with self.assertRaisesRegex(RNStageAdapterError, "concurrency"):
            bad_client = Client("stop")
            bad_client.concurrency_limit = 1
            StrictOpenRouterStageModel(bad_client, policy=policy)
        with self.assertRaisesRegex(RNStageAdapterError, "did not finish normally"):
            asyncio.run(
                StrictOpenRouterStageModel(Client("length"), policy=policy).complete(
                    prompt="{}",
                    model="model-x",
                    logical_call_id="id",
                    phase_attempt_id="phase",
                    seed=7,
                    max_tokens=1024,
                )
            )

    def test_belief_limits_are_copied_and_immutable(self) -> None:
        source = {"dim_1": 150, **{f"dim_{index}": 100 for index in range(2, 7)}}
        limits = RNBeliefLimits.from_mapping(source)
        source["dim_1"] = 1
        self.assertEqual(limits.values["dim_1"], 150)
        with self.assertRaises(TypeError):
            limits.values["dim_1"] = 1  # type: ignore[index]

    def test_belief_limits_reject_nonbaseline_values_before_any_model_call(self) -> None:
        with self.assertRaisesRegex(RNStageAdapterError, "approved RN baseline"):
            RNBeliefLimits.from_mapping(
                {"dim_1": 150, **{f"dim_{index}": 101 for index in range(2, 7)}}
            )

    def test_strict_parser_rejects_duplicate_keys_and_nonstandard_constants(self) -> None:
        with self.assertRaisesRegex(RNStageAdapterError, "duplicate JSON key"):
            _parse_exact_json_object('{"dim_1":"first","dim_1":"second"}', stage="stb")
        with self.assertRaisesRegex(RNStageAdapterError, "duplicate JSON key"):
            _parse_exact_json_object(
                '{"dimension_evidence":{"dim_1":{"support":[],"support":[]}}}',
                stage="stb",
            )
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(constant=constant), self.assertRaisesRegex(
                RNStageAdapterError,
                "non-standard JSON value",
            ):
                _parse_exact_json_object(f'{{"dim_1":{constant}}}', stage="stb")

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.schedule = EventSchedule.from_rows(
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
        self.news, self.stage_inputs = self._sealed_inputs()
        self.personas = self._persona_snapshot()
        self.prompt_bundle = RNPromptBundle.load(
            prompt_dir=Path(__file__).resolve().parents[1] / "prompts" / "common"
        )
        self.store = PaperMemoryStore(
            self.root / "paper.sqlite",
            run_id="run-stage",
            condition_id="RN_COMM_OFF",
            manifest_sha256="a" * 64,
            event_schedule=self.schedule,
            news_registry=self.news,
            stage_input_registry=self.stage_inputs,
            initial_portfolios={"A1": {"cash": 1000, "quantity": 0}},
            trade_policy=_trade_policy(),
            belief_limits={"dim_1": 150, **{f"dim_{index}": 100 for index in range(2, 7)}},
        )
        self.store.bootstrap_ltb(
            agent_id="A1",
            date="2026-02-27",
            dimensions={f"dim_{index}": f"initial long belief {index}" for index in range(1, 7)},
        )
        self.model = _FakeStageModel()
        self.adapter = self._adapter(self.model)

    def _adapter(
        self,
        model: _FakeStageModel,
        *,
        journal_filename: str = "journal.sqlite",
    ) -> RNStageAdapter:
        return RNStageAdapter(
            store=self.store,
            journal=ResponseJournal(self.root / journal_filename, manifest_sha256="a" * 64),
            prompt_bundle=self.prompt_bundle,
            personas=self.personas,
            event_schedule=self.schedule,
            stage_inputs=self.stage_inputs,
            model=model,
            call_policy=StrictCallPolicy(
                model="model-x",
                provider="provider-x",
                max_retries=1,
                concurrency=2,
            ),
            belief_limits=RNBeliefLimits.from_mapping(
                {"dim_1": 150, **{f"dim_{index}": 100 for index in range(2, 7)}}
            ),
            study_seed=7,
            seed_namespace="test-stage-adapter",
        )

    def _current_evidence(self) -> CurrentEvidencePacket:
        return CurrentEvidencePacket(
            event_id="e1",
            date="2026-02-27",
            subturn="am",
            news=(self.news.articles["real-1"].stage_projection(news_depth=0),),
            community_claims=(),
            news_depth=0,
        )

    def _run_full_turn(self) -> tuple:
        async def execute() -> tuple:
            stb = await self.adapter.run_stb(
                agent_id="A1", event_id="e1", phase_attempt_id="p-stb", attempt_number=1,
                current_evidence=self._current_evidence(),
            )
            analysis = await self.adapter.run_analysis(
                agent_id="A1", event_id="e1", phase_attempt_id="p-analysis", attempt_number=1,
            )
            decision = await self.adapter.run_decision(
                agent_id="A1", event_id="e1", phase_attempt_id="p-decision", attempt_number=1,
            )
            fill = self.adapter.run_fill(agent_id="A1", event_id="e1")
            ltb = await self.adapter.run_post_fill_ltb(
                agent_id="A1", event_id="e1", phase_attempt_id="p-ltb", attempt_number=1,
            )
            return stb, analysis, decision, fill, ltb

        return asyncio.run(execute())

    def _persona_snapshot(self) -> SealedPersonaSnapshot:
        source = self.root / "legacy-personas.sqlite"
        row = (
            "A1", "source-A1", "ordinary", "female", 31, "30대", "서울", "low", "low",
            "medium", "medium", "value", 0, '["전기전자","반도체"]', 1000, 0, "test", 1,
            "legacy prompt",
        )
        with sqlite3.connect(source) as connection:
            connection.execute(AGENTS_DDL)
            connection.execute("INSERT INTO agents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", row)
            connection.commit()
        snapshot = self.root / "persona-snapshot"
        build_persona_snapshot(
            source_db_path=source,
            snapshot_dir=snapshot,
            expected_agent_count=1,
            expected_depth_counts={0: 1},
        )
        return SealedPersonaSnapshot.load(snapshot)

    def _sealed_inputs(self) -> tuple[SealedNewsRegistry, SealedStageInputRegistry]:
        article = {
            "article_id": "real-1",
            "title": "Samsung operating update",
            "summary": "A cutoff-safe operating update.",
            "published_at": "2026-02-27T07:00:00+09:00",
            "observed_at": "2026-02-27T07:20:00+09:00",
            "last_modified_at": "2026-02-27T07:10:00+09:00",
            "source_url": "https://example.com/real-1",
            "source": "ExampleWire",
            "raw_body_sha256": _sha("raw"),
            "version_sha256": _sha("version"),
            "cutoff_version_sha256": _sha("cutoff"),
        }
        article["payload_sha256"] = article_payload_sha256(article)
        bundle = {
            "artifact_type": "real_news_bundle_manifest",
            "bundle_sha256": "0" * 64,
            "stock_code": "005930",
            "target_real_news_per_event": 1,
            "fake_news_per_event": 0,
            "articles": [article],
            "slots": [{"event_id": "e1", "slot_ordinal": 1, "article_id": "real-1", "payload_sha256": article["payload_sha256"]}],
            "accepted_shortages": {},
            "known_fake_ids": [],
            "known_fake_payload_hashes": [],
            "fake_registry_sha256": fake_registry_sha256(known_fake_ids=[], known_fake_payload_hashes=[]),
        }
        bundle["bundle_sha256"] = bundle_content_sha256(bundle)
        stage = {
            "artifact_type": "rn_stage_input_registry",
            "version": "v1",
            "calendar_event_registry_sha256": _sha("calendar"),
            "events": [{
                "event_id": "e1", "date": "2026-02-27", "subturn": "am",
                "news_cutoff_timestamp": "2026-02-27T09:00:00+09:00",
                "market_feature_as_of": "2026-02-27T09:00:00+09:00",
                "market": {"reference_price": 100, "previous_close": 99, "open_price": 100, "as_of_timestamp": "2026-02-27T09:00:00+09:00"},
            }],
        }
        return SealedNewsRegistry.from_mapping(bundle), SealedStageInputRegistry.from_mapping(stage)

    def test_full_typed_turn_uses_four_journaled_calls_and_one_deterministic_fill(self) -> None:
        results = self._run_full_turn()
        self.assertTrue(all(result.artifact_id for result in results))
        self.assertIsNone(results[3].logical_call_id)
        self.assertEqual(self.model.calls, 4)
        self.assertEqual(self.model.max_tokens, [3072, 3072, 1024, 3072])
        self.assertNotIn("A cutoff-safe operating update.", self.model.prompts[0])
        self.assertEqual(self.adapter.journal.committed_summary(), {"pending": 4, "committed": 0, "rolled_back": 0})
        with sqlite3.connect(self.store.db_path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM short_term_belief_history").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM paper_analyses").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM paper_decisions").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM paper_fill_ledger").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM turn_belief_trace").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM phase_consumptions").fetchone()[0], 4)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM memory_evidence_edges").fetchone()[0], 7)
            transition = connection.execute("SELECT integration_evidence_by_dimension_json FROM ltb_dimension_transitions").fetchone()[0]
        self.assertEqual(json.loads(transition)["dim_1"]["support"], ["real-1"])
        self.store.right_censor_unavailable_outcomes()
        self.assertEqual(
            self.store.assert_complete_lineage({("A1", "e1")}),
            {
                "expected_keys": 1,
                "short_term_belief_history": 1,
                "paper_analyses": 1,
                "paper_decisions": 1,
                "paper_fill_ledger": 1,
                "ltb_dimension_transitions": 1,
                "turn_belief_trace": 1,
                "memory_evidence_edges": 7,
                "phase_consumptions": 4,
                "community_post_traces": 0,
                "community_posts_traced": 0,
                "community_post_skips_traced": 0,
                "fills": 1,
                "outcomes": 3,
                "outcome_consumptions": 0,
                "finalized_outcomes": 3,
            },
        )

    def test_rendered_prompts_receive_the_versioned_contracts_their_text_requires(self) -> None:
        self._run_full_turn()
        stb, analysis, decision, ltb = [_payload_from_rendered_prompt(prompt) for prompt in self.model.prompts]

        self.assertEqual(stb["schema_version"], "rn-stb-input-v6")
        self.assertNotIn("previous_ltb", stb)
        self.assertNotIn("previous_ltb_id", stb.get("input_lineage", {}))
        self.assertEqual(
            {item["field"]: item["character_limit"] for item in stb["output_contract"]["dimension_contract"]},
            {"dim_1": 150, **{f"dim_{index}": 100 for index in range(2, 7)}},
        )
        self.assertEqual(
            stb["output_contract"]["required_top_level_keys"],
            [
                "dim_1", "dim_2", "dim_3", "dim_4", "dim_5", "dim_6",
                "dimension_evidence",
            ],
        )

        self.assertEqual(analysis["schema_version"], "rn-analysis-input-v3")
        self.assertEqual(
            analysis["output_contract"]["required_top_level_keys"],
            [
                "market_view",
                "valuation_view",
                "technical_view",
                "news_view",
                "portfolio_view",
                "key_risks",
                "opportunity",
                "caution",
                "confidence",
                "directional_stance",
                "evidence_references",
            ],
        )
        self.assertEqual(
            analysis["output_contract"]["directional_stance_values"], ["buy", "sell", "uncertain"]
        )
        self.assertEqual(
            analysis["output_contract"]["evidence_references"]["required_sources"],
            ["previous_ltb", "current_stb", "market", "execution_state"],
        )
        self.assertEqual(set(analysis["input_lineage"]), {"previous_ltb_id", "current_stb_id"})

        self.assertEqual(decision["schema_version"], "rn-decision-input-v3")
        self.assertEqual(
            set(decision["analysis"]),
            set(analysis["output_contract"]["required_top_level_keys"]),
        )
        self.assertNotIn("summary", decision["analysis"])
        self.assertEqual(
            decision["output_contract"]["required_top_level_keys"],
            ["action", "requested_quantity", "reason", "risk_control"],
        )

        self.assertEqual(ltb["schema_version"], "rn-post-fill-ltb-input-v4")
        rules = ltb["output_contract"]["ltb_integration_rules"]
        self.assertTrue(rules["transaction_episode_is_required_non_evidentiary_context"])
        self.assertTrue(rules["every_eligible_price_outcome_id_must_be_cited_once_in_dim_6"])
        self.assertEqual(rules["eligible_price_outcome_ids_dim_6_only"], [])
        self.assertEqual(
            ltb["output_contract"]["required_top_level_keys"],
            [
                "dim_1", "dim_2", "dim_3", "dim_4", "dim_5", "dim_6",
                "integration_evidence",
            ],
        )
        for payload in (stb, analysis, decision, ltb):
            with self.subTest(payload=payload["schema_version"]):
                self.assertFalse(_contains_mapping_key(payload, "belief_summary"))
                self.assertFalse(_contains_mapping_key(payload, "view_change"))

    def test_model_belief_narratives_are_rejected_and_never_journaled(self) -> None:
        self._run_full_turn()
        with sqlite3.connect(self.adapter.journal.path) as connection:
            rows = connection.execute(
                "SELECT logical_call_id, response_json FROM logical_responses ORDER BY logical_call_id"
            ).fetchall()
        journaled = {str(row[0]).split("|")[4]: json.loads(str(row[1])) for row in rows}
        for stage in ("stb", "post_fill_ltb"):
            with self.subTest(stage=stage):
                self.assertNotIn("belief_summary", journaled[stage])
                self.assertNotIn("view_change", journaled[stage])
        self.adapter = self._adapter(
            _FakeStageModel(inject_forbidden_belief_narrative=True),
            journal_filename="invalid-narrative.sqlite",
        )
        with self.assertRaisesRegex(RNStageAdapterError, "invalid exact key set"):
            asyncio.run(self.adapter.run_stb(
                agent_id="A1", event_id="e1", phase_attempt_id="bad-narrative", attempt_number=1,
                current_evidence=self._current_evidence(),
            ))

    def test_final_lineage_rejects_missing_memory_evidence_edge(self) -> None:
        self._run_full_turn()
        self.store.right_censor_unavailable_outcomes()
        with sqlite3.connect(self.store.db_path) as connection:
            connection.execute(
                "DELETE FROM memory_evidence_edges WHERE edge_id = "
                "(SELECT edge_id FROM memory_evidence_edges ORDER BY edge_id LIMIT 1)"
            )
            connection.commit()
        with self.assertRaisesRegex(PaperMemoryError, "Memory evidence edge set"):
            self.store.assert_complete_lineage({("A1", "e1")})

    def test_final_lineage_rejects_missing_phase_call_consumption(self) -> None:
        self._run_full_turn()
        self.store.right_censor_unavailable_outcomes()
        with sqlite3.connect(self.store.db_path) as connection:
            connection.execute("DELETE FROM phase_consumptions WHERE stage = 'analysis'")
            connection.commit()
        with self.assertRaisesRegex(PaperMemoryError, "Phase-call consumption set"):
            self.store.assert_complete_lineage({("A1", "e1")})

    def test_identical_replay_uses_accepted_stb_without_a_second_model_call(self) -> None:
        first = asyncio.run(self.adapter.run_stb(
            agent_id="A1", event_id="e1", phase_attempt_id="p1", attempt_number=1,
            current_evidence=self._current_evidence(),
        ))
        second = asyncio.run(self.adapter.run_stb(
            agent_id="A1", event_id="e1", phase_attempt_id="p2", attempt_number=2,
            current_evidence=self._current_evidence(),
        ))
        self.assertEqual(first, second)
        self.assertEqual(self.model.calls, 1)

    def test_output_token_budget_is_part_of_journal_replay_identity(self) -> None:
        asyncio.run(self.adapter.run_stb(
            agent_id="A1", event_id="e1", phase_attempt_id="p1", attempt_number=1,
            current_evidence=self._current_evidence(),
        ))
        changed = dict(RN_STAGE_MAX_TOKENS_V1)
        changed["stb"] = changed["stb"] + 1
        with (
            patch("twinmarket_kr.rn_ab.stage_adapter.RN_STAGE_MAX_TOKENS_V1", changed),
            self.assertRaisesRegex(JournalIntegrityError, "Different request payload"),
        ):
            asyncio.run(self.adapter.run_stb(
                agent_id="A1", event_id="e1", phase_attempt_id="p2", attempt_number=2,
                current_evidence=self._current_evidence(),
            ))
        self.assertEqual(self.model.calls, 1)

    def test_missing_or_invalid_output_token_budget_fails_before_model_call(self) -> None:
        for label, replacement, expected in (
            ("missing", {}, "no sealed max_tokens budget"),
            ("zero", {**RN_STAGE_MAX_TOKENS_V1, "stb": 0}, "positive integer"),
            ("bool", {**RN_STAGE_MAX_TOKENS_V1, "stb": True}, "positive integer"),
        ):
            with self.subTest(label=label):
                model = _FakeStageModel()
                adapter = self._adapter(model, journal_filename=f"{label}-budget.sqlite")
                with (
                    patch(
                        "twinmarket_kr.rn_ab.stage_adapter.RN_STAGE_MAX_TOKENS_V1",
                        replacement,
                    ),
                    self.assertRaisesRegex(RNStageAdapterError, expected),
                ):
                    asyncio.run(adapter.run_stb(
                        agent_id="A1",
                        event_id="e1",
                        phase_attempt_id=f"{label}-budget",
                        attempt_number=1,
                        current_evidence=self._current_evidence(),
                    ))
                self.assertEqual(model.calls, 0)

    def test_hallucinated_stb_evidence_id_is_rejected_before_persistence(self) -> None:
        adapter = self._adapter(_FakeStageModel(invalid_stb=True))
        with self.assertRaisesRegex(RNStageAdapterError, "unavailable or cross-dimension"):
            asyncio.run(adapter.run_stb(
                agent_id="A1", event_id="e1", phase_attempt_id="p1", attempt_number=1,
                current_evidence=self._current_evidence(),
            ))
        with sqlite3.connect(self.store.db_path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM short_term_belief_history").fetchone()[0], 0)

    def test_uncertain_analysis_still_allows_the_required_no_hold_decision(self) -> None:
        self.model = _FakeStageModel(analysis_stance="uncertain")
        self.adapter = self._adapter(self.model)
        self._run_full_turn()
        with sqlite3.connect(self.store.db_path) as connection:
            stance = connection.execute("SELECT directional_stance FROM paper_analyses").fetchone()[0]
            action = connection.execute("SELECT action FROM paper_decisions").fetchone()[0]
        self.assertEqual(stance, "uncertain")
        self.assertEqual(action, "buy")

    def test_analysis_rejects_any_key_drift_before_persistence(self) -> None:
        response = {
            "market_view": "Mixed.",
            "valuation_view": "Fair.",
            "technical_view": "Mixed.",
            "news_view": "Constructive.",
            "portfolio_view": "Constrained.",
            "key_risks": ["volatility"],
            "opportunity": "Limited entry.",
            "caution": ["Avoid concentration."],
            "confidence": "medium",
            "directional_stance": "buy",
            "evidence_references": [
                {"source": "previous_ltb", "field": "dim_1"},
                {"source": "current_stb", "field": "dim_1"},
                {"source": "market", "field": "reference_price"},
                {"source": "execution_state", "field": "max_buy_quantity"},
            ],
        }
        normalized = self.adapter._validate_analysis_response(response)
        self.assertEqual(set(normalized), set(response))
        with self.assertRaisesRegex(RNStageAdapterError, "invalid exact key set"):
            self.adapter._validate_analysis_response({**response, "summary": "not allowed"})
        without_legacy_field = dict(response)
        without_legacy_field.pop("market_view")
        with self.assertRaisesRegex(RNStageAdapterError, "invalid exact key set"):
            self.adapter._validate_analysis_response(without_legacy_field)

    def test_ltb_due_outcome_rule_matches_the_rendered_prompt_contract(self) -> None:
        parent = {f"dim_{index}": f"parent belief {index}" for index in range(1, 7)}
        evidence = {
            f"dim_{index}": {
                "support": ["real-1"] if index == 1 else [],
                "contradict": [],
            }
            for index in range(1, 7)
        }
        packet = {
            "previous_ltb": parent,
            "current_stb": {
                "dimensions": {f"dim_{index}": f"current belief {index}" for index in range(1, 7)},
                "dimension_evidence": evidence,
            },
            "sanitized_evidence_registry": [{"evidence_id": "real-1"}],
            "eligible_price_outcomes_dim_6_only": [{"outcome_id": "outcome-1"}],
        }
        response = {
            **{f"dim_{index}": f"rewritten belief {index}" for index in range(1, 7)},
            "integration_evidence": {
                f"dim_{index}": {
                    "support": ["real-1"] if index == 1 else ["outcome-1"] if index == 6 else [],
                    "contradict": [],
                }
                for index in range(1, 7)
            },
        }
        self.assertEqual(
            self.adapter._validate_ltb_response(response, packet=packet)["integration_evidence"]["dim_6"]["support"],
            ["outcome-1"],
        )
        with self.assertRaisesRegex(RNStageAdapterError, "invalid exact key set"):
            self.adapter._validate_ltb_response(
                {**response, "view_change": "model narratives are not accepted"}, packet=packet
            )
        response["integration_evidence"]["dim_6"]["support"] = []
        with self.assertRaisesRegex(RNStageAdapterError, "every and only due price outcome"):
            self.adapter._validate_ltb_response(response, packet=packet)

    def test_post_fill_ltb_rejects_stb_evidence_polarity_change(self) -> None:
        self.adapter = self._adapter(_FakeStageModel(invalid_ltb_polarity=True))
        with self.assertRaisesRegex(RNStageAdapterError, "polarity"):
            self._run_full_turn()


if __name__ == "__main__":
    unittest.main()
