from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tests.test_rn_ab_community import (
    _phase,
    _phase_registry,
    _policy,
    _profiles,
    _reader_traces,
    _schedule,
    _sealed_inputs,
    _timing_policy,
    _trade_policy,
)
from twinmarket_kr.db.connection import connect
from twinmarket_kr.rn_ab.call_policy import StrictCallPolicy
from twinmarket_kr.rn_ab.community import CommunityPhaseContext, RNCommunityService
from twinmarket_kr.rn_ab.community_provider import (
    RNCommunityProviderError,
    RNJournaledCommunityProvider,
)
from twinmarket_kr.rn_ab.journal import ResponseJournal
from twinmarket_kr.rn_ab.memory import PaperMemoryStore, scientific_sha256
from twinmarket_kr.rn_ab.persona_snapshot import FrozenPersona, SealedPersonaSnapshot
from twinmarket_kr.rn_ab.prompt_registry import RNPromptBundle
from twinmarket_kr.rn_ab.spec import RN_COMM_ON
from twinmarket_kr.rn_ab.stage_adapter import (
    RN_TRUSTED_SYSTEM_INSTRUCTION_SHA256,
    RN_TRUSTED_SYSTEM_INSTRUCTION_VERSION,
)
from twinmarket_kr.rn_ab.stages import build_decision_packet


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class _LocalModel:
    local_only = True

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def complete(self, **kwargs: Any) -> str:
        self.calls.append(dict(kwargs))
        if not self.responses:
            raise AssertionError("unexpected local model call")
        return json.dumps(
            self.responses.pop(0),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


class RNJournaledCommunityProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        schedule = _schedule(include_next_am=True)
        news, stages = _sealed_inputs(schedule)
        store = PaperMemoryStore(
            self.root / "on.sqlite",
            run_id="provider-test",
            condition_id=RN_COMM_ON,
            manifest_sha256=_hash("manifest"),
            event_schedule=schedule,
            news_registry=news,
            stage_input_registry=stages,
            initial_portfolios={
                "A0": {"cash": 1_000_000, "quantity": 0},
                "A1": {"cash": 1_000_000, "quantity": 0},
                "A2": {"cash": 1_000_000, "quantity": 0},
            },
            trade_policy=_trade_policy(),
            belief_limits={"dim_1": 150, **{f"dim_{index}": 100 for index in range(2, 7)}},
        )
        self.service = RNCommunityService(
            store,
            cohort_depths={"A0": 0, "A1": 1, "A2": 2},
            public_profiles=_profiles(),
            community_policy=_policy(),
            community_phase_registry=_phase_registry(),
            community_timing_policy=_timing_policy(),
        )
        personas = {
            agent_id: FrozenPersona(
                agent_id=agent_id,
                news_depth=depth,
                initial_cash=1_000_000,
                persona_prompt=f"persona {agent_id}\n",
                persona_sha256=_hash(f"persona:{agent_id}"),
                structured_row_sha256=_hash(f"row:{agent_id}"),
            )
            for agent_id, depth in {"A0": 0, "A1": 1, "A2": 2}.items()
        }
        self.personas = SealedPersonaSnapshot(
            snapshot_dir=self.root / "personas",
            snapshot_db_path=self.root / "personas.sqlite",
            manifest_sha256=_hash("personas"),
            source_db_sha256=_hash("source"),
            snapshot_db_sha256=_hash("snapshot"),
            prompt_map_sha256=_hash("prompt-map"),
            depth_manifest_sha256=_hash("depth-map"),
            repair_manifest_sha256=_hash("repair"),
            personas=personas,
        )
        self.journal = ResponseJournal(
            self.root / "responses.sqlite", manifest_sha256=_hash("manifest")
        )
        self.policy = StrictCallPolicy(
            model="openai/gpt-5.2",
            provider="openai",
            max_retries=1,
            concurrency=2,
            reasoning_effort="none",
            reasoning_exclude=True,
            allow_fallbacks=False,
            require_parameters=True,
        )
        self.bundle = RNPromptBundle.load(
            prompt_dir=Path(__file__).resolve().parents[1] / "prompts" / "common"
        )

    def _provider(self, responses: list[dict[str, Any]]) -> tuple[RNJournaledCommunityProvider, _LocalModel]:
        model = _LocalModel(responses)
        provider = RNJournaledCommunityProvider(
            service=self.service,
            journal=self.journal,
            prompt_bundle=self.bundle,
            personas=self.personas,
            model=model,
            call_policy=self.policy,
            study_seed=23,
            seed_namespace="provider-tests",
            max_workers=2,
        )
        return provider, model

    def _commit_pm_posting_context(self, agent_id: str) -> None:
        previous_dimensions = {
            f"dim_{index}": f"{agent_id} initial long view {index}"
            for index in range(1, 7)
        }
        parent_ltb_id = self.service.memory.bootstrap_ltb(
            agent_id=agent_id,
            date="2026-02-27",
            dimensions=previous_dimensions,
        )
        for article_number, event_id, turn, subturn in (
            (1, "2026-02-27/am", 1, "am"),
            (2, "2026-02-27/pm", 2, "pm"),
        ):
            article = self.service.memory.news_registry.articles[
                f"article-{article_number}"
            ]
            stb_dimensions = {
                f"dim_{index}": (
                    f"{agent_id} short {subturn.upper()} view {index}"
                )
                for index in range(1, 7)
            }
            stb_id = self.service.memory.save_stb(
                agent_id=agent_id,
                event_id=event_id,
                turn=turn,
                date="2026-02-27",
                dimensions=stb_dimensions,
                dimension_evidence={
                    f"dim_{index}": {"support": [], "contradict": []}
                    for index in range(1, 7)
                },
                current_evidence={
                    "event_id": event_id,
                    "date": "2026-02-27",
                    "subturn": subturn,
                    "news": [article.stage_projection(news_depth=0)],
                    "depth2_search_results": [],
                    "community_claims": [],
                },
            )
            packet = build_decision_packet(
                event_schedule=self.service.memory.event_schedule,
                stage_input_registry=self.service.memory.stage_input_registry,
                store=self.service.memory,
                agent_id=agent_id,
                event_id=event_id,
                previous_ltb=previous_dimensions,
                current_stb=stb_dimensions,
            )
            analysis_id = self.service.memory.record_analysis(
                agent_id=agent_id,
                event_id=event_id,
                turn=turn,
                date="2026-02-27",
                subturn=subturn,
                source_ltb_id=parent_ltb_id,
                source_stb_id=stb_id,
                analysis_packet=packet,
                market_view="The sealed current market is mixed.",
                valuation_view="Valuation is broadly fair.",
                technical_view="Short-term technical signals are mixed.",
                news_view="The visible current news is neutral.",
                portfolio_view="Available cash supports a constrained position.",
                key_risks=["price volatility"],
                opportunity="A limited entry remains feasible.",
                caution=["Avoid concentration."],
                directional_stance="buy",
                confidence="medium",
                evidence_references=[
                    {"source": "previous_ltb", "field": "dim_1"},
                    {"source": "current_stb", "field": "dim_1"},
                    {"source": "market", "field": "reference_price"},
                    {"source": "execution_state", "field": "max_buy_quantity"},
                ],
            )
            decision_response = {
                "action": "buy",
                "requested_quantity": 1,
                "reason": "The typed analysis supports a constrained buy.",
                "risk_control": "Keep the order within the sealed maximum.",
            }
            decision_id = self.service.memory.record_decision(
                agent_id=agent_id,
                event_id=event_id,
                turn=turn,
                date="2026-02-27",
                subturn=subturn,
                action="buy",
                requested_quantity=1,
                source_ltb_id=parent_ltb_id,
                source_stb_id=stb_id,
                analysis_id=analysis_id,
                decision_packet=packet,
                decision_response=decision_response,
            )
            fill_id = self.service.memory.record_fill(
                agent_id=agent_id,
                event_id=event_id,
                turn=turn,
                date="2026-02-27",
                subturn=subturn,
                action="buy",
                requested_quantity=1,
                source_ltb_id=parent_ltb_id,
                source_stb_id=stb_id,
                decision_id=decision_id,
            )
            next_dimensions = {
                f"dim_{index}": (
                    f"{agent_id} durable {subturn.upper()} post-fill view {index}"
                )
                for index in range(1, 7)
            }
            parent_ltb_id = self.service.memory.save_post_fill_ltb(
                agent_id=agent_id,
                event_id=event_id,
                turn=turn,
                date="2026-02-27",
                parent_ltb_id=parent_ltb_id,
                stb_id=stb_id,
                fill_id=fill_id,
                dimensions=next_dimensions,
                integration_evidence_by_dimension={
                    f"dim_{index}": {"support": [], "contradict": []}
                    for index in range(1, 7)
                },
            )
            previous_dimensions = next_dimensions

    def test_post_drafts_use_only_post_fill_contract_and_commit_private_trace(self) -> None:
        for agent_id in ("A1", "A2"):
            self._commit_pm_posting_context(agent_id)
        provider, model = self._provider(
            [
                {
                    "will_post": True,
                    "post_type": "analysis",
                    "title": "오늘 체결 뒤 관점",
                    "content": "장기 관점과 실제 체결을 함께 보고 있습니다.",
                },
                {"will_post": False},
            ]
        )
        phase = CommunityPhaseContext.from_mapping(_phase())
        provider.begin_phase_attempt(
            phase_attempt_id="post-phase",
            attempt_number=1,
        )
        drafts = tuple(
            asyncio.run(
                provider.post_drafts(
                    phase=phase,
                    personas=self.personas.personas,
                )
            )
        )
        traces = provider.finalized_post_traces(phase=phase, drafts=drafts)
        tampered_traces = (
            {**traces[0], "ltb_sha256": "0" * 64},
            traces[1],
        )
        with self.assertRaisesRegex(
            Exception,
            "current post-fill LTB",
        ):
            self.service.run_pm_phase(
                phase,
                post_drafts=drafts,
                private_post_traces=tampered_traces,
            )
        with sqlite3.connect(self.service.memory.db_path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM community_post_trace"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM observation_events "
                    "WHERE stage = 'community_posts:2026-02-27/community'"
                ).fetchone()[0],
                0,
            )
        self.service.run_pm_phase(
            phase,
            post_drafts=drafts,
            private_post_traces=traces,
            private_reader_traces=_reader_traces(
                self.service,
                _phase(),
                drafts,
            ),
        )
        call_ids = provider.finish_phase_attempt()

        self.assertEqual(len(model.calls), 2)
        for call in model.calls:
            prompt = str(call["prompt"])
            self.assertIn("[현재 장기 투자 관점]", prompt)
            self.assertIn("[오늘 체결 내역 참고]", prompt)
            self.assertNotIn("belief_summary", prompt)
            self.assertNotIn("trade_summary", prompt)
        self.assertEqual(len(call_ids), 2)

        provider.begin_phase_attempt(
            phase_attempt_id="post-phase-replay",
            attempt_number=2,
        )
        replayed_drafts = tuple(
            asyncio.run(
                provider.post_drafts(
                    phase=phase,
                    personas=self.personas.personas,
                )
            )
        )
        replayed_traces = provider.finalized_post_traces(
            phase=phase,
            drafts=replayed_drafts,
        )
        self.service.run_pm_phase(
            phase,
            post_drafts=replayed_drafts,
            private_post_traces=replayed_traces,
            private_reader_traces=_reader_traces(
                self.service,
                _phase(),
                replayed_drafts,
            ),
        )
        self.assertEqual(provider.finish_phase_attempt(), call_ids)
        self.assertEqual(replayed_drafts, drafts)
        self.assertEqual(replayed_traces, traces)
        self.assertEqual(len(model.calls), 2)

        with sqlite3.connect(self.service.memory.db_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT * FROM community_post_trace
                ORDER BY author_agent_id
                """
            ).fetchall()
            public_payload = json.loads(
                connection.execute(
                    """
                    SELECT payload_json FROM observation_events
                    WHERE stage = 'community_posts:2026-02-27/community'
                    """
                ).fetchone()[0]
            )
        self.assertEqual(
            [str(row["posting_status"]) for row in rows],
            ["posted", "skipped"],
        )
        self.assertIsNotNone(rows[0]["post_id"])
        self.assertIsNone(rows[1]["post_id"])
        self.assertEqual(
            {str(row["logical_call_id"]) for row in rows},
            set(call_ids),
        )
        self.assertTrue(
            all(
                str(row["accepted_response_sha256"])
                == self.service.memory.phase_consumption_digests()[
                    str(row["logical_call_id"])
                ]
                for row in rows
            )
        )
        self.assertEqual(len(public_payload["posts"]), 1)
        public_post = public_payload["posts"][0]
        self.assertNotIn("ltb_id", public_post)
        self.assertNotIn("fill_id", public_post)
        self.assertNotIn("prompt_values_sha256", public_post)
        self.assertEqual(
            str(rows[0]["view_change_sha256"]),
            scientific_sha256(
                self.service.memory.human_log_for_ltb(
                    ltb_id=str(rows[0]["ltb_id"])
                )["view_change"]
            ),
        )
        with connect(self.service.memory.db_path, read_only=True) as connection:
            self.assertEqual(
                self.service.memory._assert_community_post_trace_lineage(
                    connection
                ),
                {
                    "community_post_traces": 2,
                    "community_posts_traced": 1,
                    "community_post_skips_traced": 1,
                },
            )

    def test_next_am_interpretation_is_strict_journaled_and_replayable(self) -> None:
        response = {
            "observed_sentiment": "mixed",
            "claims": [
                {
                    "claim_text": "낙관론과 가격 부담 경계가 함께 보인다.",
                    "claim_stance": "neutral",
                    "source_exposure_ids": ["exp:1"],
                    "supporting_quote": "full body",
                }
            ],
            "agreement_disagreement": "근거 있는 부분에는 공감하지만 추격 심리에는 반대한다.",
            "uncertainty": "게시글 주장의 외부 사실 여부는 확인되지 않았다.",
        }
        provider, model = self._provider([response])
        exposure = {
            "exposure_channel": "best_only_body",
            "source_exposure_ids": ["exp:1"],
            "title": "title",
            "body": "full body",
            "content_level": "full_body",
        }

        async def first() -> tuple[dict[str, Any], ...]:
            provider.begin_phase_attempt(phase_attempt_id="phase-1", attempt_number=1)
            claims = tuple(
                await provider.interpret(
                    persona=self.personas.persona("A0"),
                    event_id="2026-03-03/am",
                    exposures=(exposure,),
                )
            )
            call_ids = provider.finish_phase_attempt()
            self.assertEqual(len(call_ids), 1)
            self.assertEqual(
                set(self.service.memory.phase_consumption_digests()),
                set(call_ids),
            )
            return claims

        claims = asyncio.run(first())
        self.assertEqual(
            claims,
            (
                {
                    "claim_text": "낙관론과 가격 부담 경계가 함께 보인다.",
                    "stance": "neutral",
                    "source_exposure_ids": ["exp:1"],
                    "supporting_quote": "full body",
                },
            ),
        )
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(model.calls[0]["max_tokens"], 2048)
        self.assertNotIn("```", model.calls[0]["prompt"])
        with sqlite3.connect(self.journal.path) as connection:
            request = json.loads(
                connection.execute(
                    "SELECT request_json FROM logical_responses"
                ).fetchone()[0]
            )
        self.assertEqual(
            request["trusted_system_instruction"],
            {
                "version": RN_TRUSTED_SYSTEM_INSTRUCTION_VERSION,
                "sha256": RN_TRUSTED_SYSTEM_INSTRUCTION_SHA256,
            },
        )

        # The exact same logical request is read from the journal. No second
        # local/model call occurs, and the coordinator still receives its ID.
        provider.begin_phase_attempt(phase_attempt_id="phase-2", attempt_number=2)
        replayed = asyncio.run(
            provider.interpret(
                persona=self.personas.persona("A0"),
                event_id="2026-03-03/am",
                exposures=(exposure,),
            )
        )
        self.assertEqual(tuple(replayed), claims)
        self.assertEqual(len(provider.finish_phase_attempt()), 1)
        self.assertEqual(len(model.calls), 1)

    def test_extra_key_or_unknown_exposure_fails_closed_and_records_error(self) -> None:
        invalid = {
            "observed_sentiment": "mixed",
            "claims": [
                {
                    "claim_text": "invented source",
                    "claim_stance": "bullish",
                    "source_exposure_ids": ["exp:not-visible"],
                    "supporting_quote": "title",
                }
            ],
            "agreement_disagreement": "mixed",
            "uncertainty": "unknown",
            "extra": "forbidden",
        }
        provider, _model = self._provider([invalid])
        provider.begin_phase_attempt(phase_attempt_id="phase-invalid", attempt_number=1)
        with self.assertRaisesRegex(RNCommunityProviderError, "invalid exact key set"):
            asyncio.run(
                provider.interpret(
                    persona=self.personas.persona("A0"),
                    event_id="2026-03-03/am",
                    exposures=(
                        {
                            "exposure_channel": "best_only_body",
                            "source_exposure_ids": ["exp:1"],
                            "title": "title",
                            "body": "full body",
                            "content_level": "full_body",
                        },
                    ),
                )
            )
        provider.abort_phase_attempt()
        self.assertEqual(self.journal.committed_summary()["committed"], 0)

    def test_title_only_quote_preserves_provenance_without_truth_filtering_interpretation(self) -> None:
        provider, _model = self._provider(
            [
                {
                    "observed_sentiment": "optimistic",
                    "claims": [
                        {
                            "claim_text": "본문은 매출 2조원이라고 말했다.",
                            "claim_stance": "bullish",
                            "source_exposure_ids": ["exp:title"],
                            "supporting_quote": "삼성",
                        }
                    ],
                    "agreement_disagreement": "제목의 낙관 신호만 관찰했다.",
                    "uncertainty": "본문은 보지 못했다.",
                }
            ]
        )
        provider.begin_phase_attempt(
            phase_attempt_id="title-only-attack",
            attempt_number=1,
        )
        claims = asyncio.run(
            provider.interpret(
                persona=self.personas.persona("A2"),
                event_id="2026-03-03/am",
                exposures=(
                    {
                        "post_id": "post:1",
                        "title": "삼성 반도체 호재",
                        "post_type": "analysis",
                        "score": 0,
                        "like_count": 0,
                        "selected": False,
                        "reader_reaction": None,
                        "source_exposure_ids": ["exp:title"],
                        "exposure_channel": "title_only_candidate",
                        "untrusted_content_kind": "community_post",
                        "content_level": "title_only",
                    },
                ),
            )
        )
        provider.finish_phase_attempt()
        self.assertEqual(
            tuple(claims),
            (
                {
                    "claim_text": "본문은 매출 2조원이라고 말했다.",
                    "stance": "bullish",
                    "source_exposure_ids": ["exp:title"],
                    "supporting_quote": "삼성",
                },
            ),
        )
        self.assertEqual(self.journal.committed_summary()["pending"], 1)

    def test_provider_rejects_unsealed_concurrency_and_requires_attempt_identity(self) -> None:
        provider, _model = self._provider([])
        with self.assertRaisesRegex(RNCommunityProviderError, "outside a coordinator"):
            asyncio.run(
                provider.interpret(
                    persona=self.personas.persona("A0"),
                    event_id="2026-03-03/am",
                    exposures=(
                        {
                            "exposure_channel": "best_only_body",
                            "source_exposure_ids": ["exp:1"],
                            "title": "title",
                            "body": "full body",
                            "content_level": "full_body",
                        },
                    ),
                )
            )
        with self.assertRaisesRegex(RNCommunityProviderError, "concurrency"):
            RNJournaledCommunityProvider(
                service=self.service,
                journal=self.journal,
                prompt_bundle=self.bundle,
                personas=self.personas,
                model=_LocalModel([]),
                call_policy=self.policy,
                study_seed=23,
                seed_namespace="provider-tests",
                max_workers=1,
            )

    def test_select_and_react_use_string_ids_then_update_only_validated_score(self) -> None:
        phase = CommunityPhaseContext.from_mapping(_phase())
        # Deliberately false public speech: the containment contract validates
        # exact provenance/quote support, not whether a community assertion is
        # objectively true.
        body = "삼성전자 영업이익은 999조원이라는 확인되지 않은 주장이다."
        draft = {
            "author_agent_id": "A1",
            "title": "one post",
            "body": body,
            "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "post_type": "analysis",
            "score": 0,
            "like_count": 0,
        }
        candidate = self.service.preview_candidate_posts(phase, post_drafts=(draft,))[0]
        provider, model = self._provider(
            [
                {"selected_post_ids": [candidate["post_id"]]},
                {
                    "reactions": [
                        {"post_id": candidate["post_id"], "reaction": "like"}
                    ]
                },
            ]
        )
        provider._drafts_by_phase[phase.phase_id] = (draft,)
        provider._reaction_counts_by_phase[phase.phase_id] = {}
        provider.begin_phase_attempt(phase_attempt_id="community-phase", attempt_number=1)
        selections = asyncio.run(
            provider.selective_reads(
                phase=phase,
                candidates=(candidate,),
                personas=self.personas.personas,
            )
        )
        reader_traces = provider.finalized_reader_traces(
            phase=phase,
            drafts=(draft,),
        )
        finalized = provider.finalized_post_drafts(phase=phase, drafts=(draft,))
        call_ids = provider.finish_phase_attempt()
        self.assertEqual(
            tuple(selections),
            ({"reader_agent_id": "A2", "post_id": candidate["post_id"]},),
        )
        self.assertEqual(finalized[0]["like_count"], 1)
        self.assertEqual(finalized[0]["score"], 1)
        self.assertEqual(
            reader_traces,
            (
                {"reader_agent_id": "A1", "candidates": []},
                {
                    "reader_agent_id": "A2",
                    "candidates": [
                        {
                            "post_id": candidate["post_id"],
                            "title": "one post",
                            "post_type": "analysis",
                            "score": 0,
                            "like_count": 0,
                            "selected": True,
                            "reaction": "like",
                        }
                    ],
                },
            ),
        )
        self.assertEqual(len(call_ids), 2)
        self.assertEqual(
            set(self.service.memory.phase_consumption_digests()),
            set(call_ids),
        )
        self.assertEqual(len(model.calls), 2)
        self.assertIn(
            f'"post_id":"{candidate["post_id"]}"',
            model.calls[0]["prompt"],
        )
        self.assertIn(body, model.calls[1]["prompt"])

        self.service.run_pm_phase(
            phase,
            post_drafts=finalized,
            selective_reads=selections,
            private_reader_traces=reader_traces,
        )
        with connect(self.service.memory.db_path, read_only=True) as connection:
            public_board = str(
                connection.execute(
                    "SELECT payload_json FROM observation_events "
                    "WHERE stage = 'community_posts:2026-02-27/community'"
                ).fetchone()["payload_json"]
            )
        self.assertNotIn("reader_reaction", public_board)
        self.assertNotIn("source_exposure_id", public_board)
        restarted_service = RNCommunityService(
            self.service.memory,
            cohort_depths={"A0": 0, "A1": 1, "A2": 2},
            public_profiles=_profiles(),
            community_policy=_policy(),
            community_phase_registry=_phase_registry(),
            community_timing_policy=_timing_policy(),
        )
        restarted_service.deliver_scheduled_best(
            phase,
            delivered_at="2026-03-03T08:55:00+09:00",
        )
        a2_payloads = restarted_service.interpretation_payloads(
            agent_id="A2",
            event_id="2026-03-03/am",
        )
        a1_payloads = restarted_service.interpretation_payloads(
            agent_id="A1",
            event_id="2026-03-03/am",
        )
        d0_payloads = restarted_service.interpretation_payloads(
            agent_id="A0",
            event_id="2026-03-03/am",
        )
        title_only = next(
            item
            for item in a2_payloads
            if item["exposure_channel"] == "title_only_candidate"
        )
        full_body = next(
            item
            for item in a2_payloads
            if item["exposure_channel"] == "selected_and_best_overlap"
        )
        self.assertEqual(title_only["reader_reaction"], "like")
        self.assertEqual(title_only["content_level"], "title_only")
        self.assertNotIn("body", title_only)
        self.assertEqual(full_body["reader_reaction"], "like")
        self.assertEqual(full_body["body"], body)
        self.assertTrue(
            all(item.get("reader_reaction") is None for item in a1_payloads)
        )
        self.assertTrue(
            all(item.get("reader_reaction") is None for item in d0_payloads)
        )
        self.assertFalse(
            any(
                item["exposure_channel"] == "title_only_candidate"
                for item in d0_payloads
            )
        )

        # A false statement is accepted when its exact full-body quote and
        # reader-owned source ID are present.  No truth classifier runs.
        full_source_id = full_body["source_exposure_ids"][0]
        model.responses.append(
            {
                "observed_sentiment": "mixed",
                "claims": [
                    {
                        "claim_text": "커뮤니티에 확인되지 않은 999조원 주장이 있다.",
                        "claim_stance": "uncertain",
                        "source_exposure_ids": [full_source_id],
                        "supporting_quote": "999조원",
                    }
                ],
                "agreement_disagreement": "주장을 관찰했지만 사실로 확정하지 않는다.",
                "uncertainty": "외부 사실 여부는 검증되지 않았다.",
            }
        )
        provider.begin_phase_attempt(
            phase_attempt_id="interpretation-phase",
            attempt_number=1,
        )
        interpretation_claims = asyncio.run(
            provider.interpret(
                persona=self.personas.persona("A2"),
                event_id="2026-03-03/am",
                exposures=a2_payloads,
            )
        )
        provider.finish_phase_attempt()
        committed = restarted_service.record_interpretation_claims(
            agent_id="A2",
            event_id="2026-03-03/am",
            claims=interpretation_claims,
        )
        self.assertEqual(len(committed), 1)
        self.assertEqual(committed[0].supporting_quote, "999조원")
        self.assertNotIn("supporting_quote", committed[0].stage_projection())
        rendered_prompt = str(model.calls[-1]["prompt"])
        self.assertIn(body, rendered_prompt)
        self.assertIn(hashlib.sha256(body.encode("utf-8")).hexdigest(), rendered_prompt)
        self.assertIn(full_source_id, rendered_prompt)
        self.assertIn('"reader_reaction":"like"', rendered_prompt)


if __name__ == "__main__":
    unittest.main()
