from __future__ import annotations

import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

from tests.test_rn_ab_community import (
    _draft,
    _phase,
    _phase_registry,
    _policy,
    _profiles,
    _schedule,
    _sealed_inputs,
    _timing_policy,
    _trade_policy,
)
from twinmarket_kr.db.connection import connect
from twinmarket_kr.rn_ab.community import CommunityContractError, RNCommunityService
from twinmarket_kr.rn_ab.community_lifecycle import (
    CommunityLifecycleError,
    RNCommunityLifecycleAdapter,
)
from twinmarket_kr.rn_ab.memory import PaperMemoryStore
from twinmarket_kr.rn_ab.persona_snapshot import FrozenPersona, SealedPersonaSnapshot
from twinmarket_kr.rn_ab.resolver import CommunityPhase, DecisionEvent
from twinmarket_kr.rn_ab.spec import RN_COMM_OFF, RN_COMM_ON
from twinmarket_kr.rn_ab.stages import CurrentEvidencePacket


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


class _BoardProvider:
    local_only = True

    def __init__(self, *, empty: bool = False) -> None:
        self.empty = empty
        self.post_calls = 0
        self.read_calls = 0
        self.candidates = ()
        self.selections = ()

    async def post_drafts(self, *, phase, personas):
        self.post_calls += 1
        self.persona_ids = tuple(personas)
        if self.empty:
            return ()
        return (
            _draft(
                "A1",
                title="Frozen community source",
                body="FULL BODY: this exact text is visible only in community interpretation.",
                score=7,
            ),
        )

    async def selective_reads(self, *, phase, candidates, personas):
        self.read_calls += 1
        self.candidates = tuple(dict(item) for item in candidates)
        if not candidates:
            self.selections = ()
            return ()
        self.selections = (
            {"reader_agent_id": "A2", "post_id": candidates[0]["post_id"]},
        )
        return self.selections

    def finalized_reader_traces(self, *, phase, drafts):
        selected = {
            (row["reader_agent_id"], row["post_id"]) for row in self.selections
        }
        return tuple(
            {
                "reader_agent_id": reader,
                "candidates": [
                    {
                        "post_id": candidate["post_id"],
                        "title": candidate["title"],
                        "post_type": candidate["post_type"],
                        "score": candidate["score"],
                        "like_count": candidate["like_count"],
                        "selected": (reader, candidate["post_id"]) in selected,
                        "reaction": (
                            "none"
                            if (reader, candidate["post_id"]) in selected
                            else None
                        ),
                    }
                    for candidate in self.candidates
                    if not (reader == "A1" and candidate["title"] == "Frozen community source")
                ],
            }
            for reader in ("A1", "A2")
        )


class _BlockingProductionBoard:
    """Minimal production-shaped provider used only for cancellation cleanup."""

    production_ready = True

    def __init__(self) -> None:
        self.phase_attempt: tuple[str, int] | None = None
        self.begin_attempts: list[tuple[str, int]] = []
        self.abort_calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    def begin_phase_attempt(self, *, phase_attempt_id: str, attempt_number: int) -> None:
        if self.phase_attempt is not None:
            raise AssertionError("provider attempt leaked across a retry")
        self.phase_attempt = (phase_attempt_id, attempt_number)
        self.begin_attempts.append(self.phase_attempt)

    def finish_phase_attempt(self) -> tuple[str, ...]:
        if self.phase_attempt is None:
            raise AssertionError("provider attempt was already cleared")
        self.phase_attempt = None
        return ()

    def abort_phase_attempt(self) -> None:
        if self.phase_attempt is None:
            raise AssertionError("abort without an active provider attempt")
        self.phase_attempt = None
        self.abort_calls += 1

    async def post_drafts(self, *, phase, personas):
        self.entered.set()
        await self.release.wait()
        return ()


class _InterpretationProvider:
    local_only = True

    def __init__(
        self,
        *,
        fail_agent: str | None = None,
        delays: Mapping[str, float] | None = None,
    ) -> None:
        self.fail_agent = fail_agent
        self.delays = dict(delays or {})
        self.calls: list[str] = []
        self.completed: list[str] = []
        self.active = 0
        self.max_active = 0

    async def interpret(
        self,
        *,
        persona: FrozenPersona,
        event_id: str,
        exposures: Sequence[Mapping[str, Any]],
    ):
        self.calls.append(persona.agent_id)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.delays.get(persona.agent_id, 0.01))
            if persona.agent_id == self.fail_agent:
                raise RuntimeError("local fixture failure")
            self.asserted_full_bodies = tuple(
                item["body"]
                for item in exposures
                if item.get("content_level") == "full_body"
            )
            source_ids = sorted(
                {
                    exposure_id
                    for item in exposures
                    for exposure_id in item["source_exposure_ids"]
                }
            )
            quote = next(
                (
                    item["body"]
                    for item in exposures
                    if item.get("content_level") == "full_body"
                ),
                next(item["title"] for item in exposures),
            )
            return (
                {
                    "claim_text": f"{persona.agent_id}의 검증된 커뮤니티 해석",
                    "stance": "bullish",
                    "source_exposure_ids": source_ids,
                    "supporting_quote": quote,
                },
            )
        finally:
            self.active -= 1
            self.completed.append(persona.agent_id)


class RNCommunityLifecycleAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.schedule = _schedule(include_next_am=True)
        self.news_registry, self.stage_registry = _sealed_inputs(self.schedule)
        self.services = {
            condition_id: self._service(condition_id)
            for condition_id in (RN_COMM_OFF, RN_COMM_ON)
        }
        personas = {
            agent_id: FrozenPersona(
                agent_id=agent_id,
                news_depth=depth,
                initial_cash=1_000_000,
                persona_prompt=f"sealed persona for {agent_id}\n",
                persona_sha256=_hash(f"persona:{agent_id}"),
                structured_row_sha256=_hash(f"row:{agent_id}"),
            )
            for agent_id, depth in {"A0": 0, "A1": 1, "A2": 2}.items()
        }
        self.personas = SealedPersonaSnapshot(
            snapshot_dir=self.root / "personas",
            snapshot_db_path=self.root / "personas" / "persona.sqlite",
            manifest_sha256=_hash("persona-manifest"),
            source_db_sha256=_hash("persona-source"),
            snapshot_db_sha256=_hash("persona-db"),
            prompt_map_sha256=_hash("persona-map"),
            depth_manifest_sha256=_hash("depth-map"),
            repair_manifest_sha256=_hash("repair"),
            personas=personas,
        )

    def _service(self, condition_id: str) -> RNCommunityService:
        memory = PaperMemoryStore(
            self.root / f"{condition_id}.sqlite",
            run_id="paired-run",
            condition_id=condition_id,
            manifest_sha256=_hash("resolved-manifest"),
            event_schedule=self.schedule,
            news_registry=self.news_registry,
            stage_input_registry=self.stage_registry,
            initial_portfolios={
                "A0": {"cash": 1_000_000, "quantity": 0},
                "A1": {"cash": 1_000_000, "quantity": 0},
                "A2": {"cash": 1_000_000, "quantity": 0},
            },
            trade_policy=_trade_policy(),
            belief_limits={"dim_1": 150, **{f"dim_{index}": 100 for index in range(2, 7)}},
        )
        return RNCommunityService(
            memory,
            cohort_depths={"A0": 0, "A1": 1, "A2": 2},
            public_profiles=_profiles(),
            community_policy=_policy(),
            community_phase_registry=_phase_registry(),
            community_timing_policy=_timing_policy(),
        )

    def _adapter(
        self,
        *,
        board=None,
        interpretation=None,
        max_workers: int = 2,
        generated_bindings: bool = False,
    ):
        return RNCommunityLifecycleAdapter(
            services=self.services,
            personas=self.personas,
            phase_contexts=(_phase(),),
            delivery_timestamps_by_event={
                "2026-03-03/am": "2026-03-03T08:55:00+09:00"
            },
            board_provider=board,
            interpretation_provider=interpretation,
            max_workers=max_workers,
            public_profile_registry_sha256=_hash("profiles") if generated_bindings else None,
            community_truth_policy_sha256=_hash("truth") if generated_bindings else None,
            generated_input_manifest_sha256=_hash("generated") if generated_bindings else None,
        )

    def _claim_observation_count(self) -> int:
        with connect(self.services[RN_COMM_ON].memory.db_path, read_only=True) as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) AS n FROM observation_events WHERE stage LIKE 'community_claims:%'"
                ).fetchone()["n"]
            )

    def test_local_lifecycle_connects_pm_board_next_am_claims_and_restart_reload(self) -> None:
        board = _BoardProvider()
        interpretation = _InterpretationProvider()
        lifecycle = self._adapter(board=board, interpretation=interpretation, max_workers=2)

        async def run() -> None:
            # Non-community events remain explicit no-ops.
            await lifecycle.after_event(condition_id=RN_COMM_ON, event_id="2026-02-27/am")
            await lifecycle.after_event(condition_id=RN_COMM_OFF, event_id="2026-02-27/pm")
            await lifecycle.after_event(condition_id=RN_COMM_ON, event_id="2026-02-27/pm")
            await lifecycle.prepare_event(condition_id=RN_COMM_OFF, event_id="2026-03-03/am")
            await lifecycle.prepare_event(condition_id=RN_COMM_ON, event_id="2026-03-03/am")

        asyncio.run(run())
        self.assertEqual(board.post_calls, 1)
        self.assertEqual(board.read_calls, 1)
        self.assertEqual(set(interpretation.calls), {"A0", "A1", "A2"})
        self.assertLessEqual(interpretation.max_active, 2)
        self.assertEqual(self._claim_observation_count(), 3)

        for agent_id in ("A0", "A1", "A2"):
            projections = lifecycle.claim_projections(
                condition_id=RN_COMM_ON,
                agent_id=agent_id,
                event_id="2026-03-03/am",
            )
            self.assertEqual(len(projections), 1)
            self.assertEqual(
                set(projections[0]),
                {"claim_id", "claim_text", "stance", "source_exposure_ids"},
            )
            self.assertNotIn("body", projections[0])
            self.assertNotIn("title", projections[0])

        news = [
            self.news_registry.articles[slot.article_id].stage_projection(news_depth=0)
            for slot in self.news_registry.slots_by_event["2026-03-03/am"]
        ]
        packet = CurrentEvidencePacket.from_mapping(
            {
                "event_id": "2026-03-03/am",
                "date": "2026-03-03",
                "subturn": "am",
                "news": news,
                "depth2_search_results": [],
                "community_claims": list(
                    lifecycle.claim_projections(
                        condition_id=RN_COMM_ON,
                        agent_id="A0",
                        event_id="2026-03-03/am",
                    )
                ),
            },
            agent_id="A0",
            event_schedule=self.schedule,
            news_registry=self.news_registry,
            stage_input_registry=self.stage_registry,
            claim_verifier=self.services[RN_COMM_ON],
            expected_news_depth=0,
        )
        self.assertEqual(len(packet.community_claims), 1)
        self.assertEqual(
            lifecycle.claim_projections(
                condition_id=RN_COMM_OFF,
                agent_id="A0",
                event_id="2026-03-03/am",
            ),
            (),
        )

        # A fresh adapter reads the committed claims from SQLite.  It needs no
        # in-memory result from the preceding interpretation process.
        restarted = self._adapter(board=None, interpretation=None)
        self.assertEqual(
            restarted.claim_projections(
                condition_id=RN_COMM_ON,
                agent_id="A2",
                event_id="2026-03-03/am",
            ),
            lifecycle.claim_projections(
                condition_id=RN_COMM_ON,
                agent_id="A2",
                event_id="2026-03-03/am",
            ),
        )

    def test_missing_actual_prompt_adapters_fail_closed_but_empty_best_skips_interpretation(self) -> None:
        lifecycle = self._adapter(board=None, interpretation=None)
        self.assertEqual(
            lifecycle.missing_full_run_dependencies(),
            (
                "sealed_public_author_profile_registry_and_manifest_hash",
                "sealed_generated_community_post_truth_policy",
                "sealed_generated_input_manifest_binding",
                "approved_journaled_community_posting_and_read_provider",
                "approved_journaled_next_am_community_interpretation_provider",
            ),
        )
        asyncio.run(
            lifecycle.after_event(condition_id=RN_COMM_OFF, event_id="2026-02-27/pm")
        )
        with self.assertRaisesRegex(CommunityLifecycleError, "board provider"):
            asyncio.run(
                lifecycle.after_event(condition_id=RN_COMM_ON, event_id="2026-02-27/pm")
            )

        empty_board = _BoardProvider(empty=True)
        empty_lifecycle = self._adapter(board=empty_board, interpretation=None)

        async def run_empty() -> None:
            await empty_lifecycle.after_event(
                condition_id=RN_COMM_ON,
                event_id="2026-02-27/pm",
            )
            await empty_lifecycle.prepare_event(
                condition_id=RN_COMM_ON,
                event_id="2026-03-03/am",
            )

        asyncio.run(run_empty())
        self.assertEqual(self._claim_observation_count(), 0)

    def test_generated_profile_and_truth_bindings_remove_only_artifact_gaps(self) -> None:
        lifecycle = self._adapter(
            board=_BoardProvider(empty=True),
            interpretation=_InterpretationProvider(),
            generated_bindings=True,
        )
        self.assertEqual(
            lifecycle.missing_full_run_dependencies(),
            (
                "approved_journaled_community_posting_and_read_provider",
                "approved_journaled_next_am_community_interpretation_provider",
            ),
        )
        with self.assertRaisesRegex(CommunityLifecycleError, "supplied together"):
            RNCommunityLifecycleAdapter(
                services=self.services,
                personas=self.personas,
                phase_contexts=(_phase(),),
                delivery_timestamps_by_event={
                    "2026-03-03/am": "2026-03-03T08:55:00+09:00"
                },
                board_provider=_BoardProvider(empty=True),
                interpretation_provider=None,
                max_workers=2,
                public_profile_registry_sha256=_hash("profiles"),
            )

    def test_interpretation_failure_commits_no_partial_agent_claims(self) -> None:
        board = _BoardProvider()
        failing = _InterpretationProvider(fail_agent="A1")
        lifecycle = self._adapter(board=board, interpretation=failing, max_workers=3)
        asyncio.run(lifecycle.after_event(condition_id=RN_COMM_ON, event_id="2026-02-27/pm"))
        with self.assertRaisesRegex(CommunityLifecycleError, "local fixture failure"):
            asyncio.run(
                lifecycle.prepare_event(condition_id=RN_COMM_ON, event_id="2026-03-03/am")
            )
        self.assertEqual(self._claim_observation_count(), 0)
        with self.assertRaisesRegex(CommunityContractError, "no committed interpretation"):
            self.services[RN_COMM_ON].recorded_claims_for_agent(
                agent_id="A0",
                event_id="2026-03-03/am",
            )

    def test_interpretation_failure_settles_sibling_workers_before_abort(self) -> None:
        """A fast failure cannot leave delayed provider calls writing stale state."""

        board = _BoardProvider()
        failing = _InterpretationProvider(
            fail_agent="A1",
            delays={"A0": 0.05, "A1": 0.0, "A2": 0.05},
        )
        lifecycle = self._adapter(board=board, interpretation=failing, max_workers=3)
        asyncio.run(lifecycle.after_event(condition_id=RN_COMM_ON, event_id="2026-02-27/pm"))
        with self.assertRaisesRegex(CommunityLifecycleError, "local fixture failure"):
            asyncio.run(
                lifecycle.prepare_event(condition_id=RN_COMM_ON, event_id="2026-03-03/am")
            )
        self.assertEqual(failing.active, 0)
        self.assertEqual(set(failing.completed), {"A0", "A1", "A2"})
        self.assertEqual(self._claim_observation_count(), 0)

    def test_cancelled_production_board_attempt_is_released_before_retry(self) -> None:
        """Cancellation must clear provider-local state before a new attempt."""

        board = _BlockingProductionBoard()
        lifecycle = self._adapter(board=board, interpretation=None, max_workers=2)

        async def cancel_then_retry() -> None:
            first = asyncio.create_task(
                lifecycle.after_event(
                    condition_id=RN_COMM_ON,
                    event_id="2026-02-27/pm",
                    phase_attempt_id="attempt-one",
                    attempt_number=1,
                )
            )
            await board.entered.wait()
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            self.assertIsNone(board.phase_attempt)
            self.assertEqual(board.abort_calls, 1)

            second = asyncio.create_task(
                lifecycle.after_event(
                    condition_id=RN_COMM_ON,
                    event_id="2026-02-27/pm",
                    phase_attempt_id="attempt-two",
                    attempt_number=2,
                )
            )
            for _ in range(20):
                if len(board.begin_attempts) == 2:
                    break
                await asyncio.sleep(0)
            self.assertEqual(
                board.begin_attempts,
                [("attempt-one", 1), ("attempt-two", 2)],
            )
            second.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await second
            self.assertIsNone(board.phase_attempt)
            self.assertEqual(board.abort_calls, 2)

        asyncio.run(cancel_then_retry())

    def test_nonlocal_provider_marker_is_rejected_before_state_change(self) -> None:
        board = _BoardProvider()
        board.local_only = False
        with self.assertRaisesRegex(CommunityLifecycleError, "local_only=True"):
            self._adapter(board=board, interpretation=None)

    def test_resolved_manifest_factory_derives_only_next_approved_am_timestamps(self) -> None:
        events = (
            DecisionEvent(
                date="2026-02-27",
                event_ordinal_in_date=1,
                decision_event_id="2026-02-27/am",
                subturn="AM",
                decision_timestamp="2026-02-27T09:00:00+09:00",
                news_window={},
                market_feature_as_of="2026-02-27T09:00:00+09:00",
                execution_price_field="actual_open",
                consume_scheduled_community=True,
                decision_enabled=True,
                global_ordinal=1,
            ),
            DecisionEvent(
                date="2026-02-27",
                event_ordinal_in_date=2,
                decision_event_id="2026-02-27/pm",
                subturn="PM",
                decision_timestamp="2026-02-27T15:30:00+09:00",
                news_window={},
                market_feature_as_of="2026-02-27T15:30:00+09:00",
                execution_price_field="actual_close",
                consume_scheduled_community=False,
                decision_enabled=True,
                global_ordinal=2,
            ),
            DecisionEvent(
                date="2026-03-03",
                event_ordinal_in_date=1,
                decision_event_id="2026-03-03/am",
                subturn="AM",
                decision_timestamp="2026-03-03T09:00:00+09:00",
                news_window={},
                market_feature_as_of="2026-03-03T09:00:00+09:00",
                execution_price_field="actual_open",
                consume_scheduled_community=True,
                decision_enabled=True,
                global_ordinal=3,
            ),
        )
        phase = CommunityPhase(
            date="2026-02-27",
            phase_id="2026-02-27/community",
            after_event_id="2026-02-27/pm",
            next_visible_event_rule="next-approved-AM",
        )
        manifest = SimpleNamespace(
            decision_events=events,
            calendar=SimpleNamespace(community_phases=(phase,)),
        )
        lifecycle = RNCommunityLifecycleAdapter.from_resolved_manifest(
            manifest,
            services=self.services,
            personas=self.personas,
            board_provider=_BoardProvider(empty=True),
            interpretation_provider=None,
            max_workers=2,
        )

        async def run() -> None:
            await lifecycle.after_event(condition_id=RN_COMM_ON, event_id="2026-02-27/pm")
            await lifecycle.prepare_event(condition_id=RN_COMM_ON, event_id="2026-03-03/am")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
