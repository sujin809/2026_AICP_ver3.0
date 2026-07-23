from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from twinmarket_kr.rn_ab.journal import LogicalCallKey, ResponseJournal
from twinmarket_kr.rn_ab.memory import EventSchedule
from twinmarket_kr.rn_ab.phase_runner import RNPhasePausedError
from twinmarket_kr.rn_ab.runner import (
    RNPairedRunner,
    RNRunnerError,
    _community_logical_call_owner,
)
from twinmarket_kr.rn_ab.spec import RN_COMM_OFF, RN_COMM_ON, RN_CONDITIONS
from twinmarket_kr.rn_ab.stages import CurrentEvidencePacket


@dataclass(frozen=True)
class _FakeStageWriteResult:
    artifact_id: str
    logical_call_id: str | None


class _FakePersonas:
    def __init__(self, agent_ids: tuple[str, ...]) -> None:
        self._agent_ids = set(agent_ids)

    def assert_agent_set(self, agent_ids: tuple[str, ...]) -> None:
        if set(agent_ids) != self._agent_ids:
            raise AssertionError("runner changed the sealed agent set")


class _LocalEvidenceProvider:
    async def current_evidence(
        self,
        *,
        condition_id: str,
        agent_id: str,
        event_id: str,
    ) -> CurrentEvidencePacket:
        del condition_id, agent_id
        return CurrentEvidencePacket(
            event_id=event_id,
            date="2026-01-02",
            subturn="am",
            news=(),
            community_claims=(),
        )


class _NoopCommunityLifecycle:
    """Explicit OFF/ON test fixture; production construction must inject RN lifecycle."""

    async def prepare_event(
        self,
        *,
        condition_id: str,
        event_id: str,
        phase_attempt_id: str,
        attempt_number: int,
    ):
        del condition_id, event_id, phase_attempt_id, attempt_number
        return ()

    async def after_event(
        self,
        *,
        condition_id: str,
        event_id: str,
        phase_attempt_id: str,
        attempt_number: int,
    ):
        del condition_id, event_id, phase_attempt_id, attempt_number
        return ()


class _ConcurrencyProbe:
    def __init__(self) -> None:
        self.active = 0
        self.peak = 0
        self.invocations = 0
        self.physical_calls = 0

    async def enter(self) -> None:
        self.active += 1
        self.invocations += 1
        self.peak = max(self.peak, self.active)
        # Keep the stage active across at least one event-loop handoff so the
        # runner's semaphore is measured rather than merely inferred.
        await asyncio.sleep(0.001)

    def leave(self) -> None:
        self.active -= 1


class _FakeStore:
    def __init__(self, db_path: Path, *, condition_id: str, manifest_sha256: str) -> None:
        self.db_path = db_path
        self.condition_id = condition_id
        self.manifest_sha256 = manifest_sha256

    def mature_outcomes_for_event(self, *, event_id: str) -> tuple[str, ...]:
        del event_id
        return ()


class _JournaledFakeAdapter:
    """Local composite-turn double preserving journal/write ordering."""

    def __init__(
        self,
        *,
        store: _FakeStore,
        journal: ResponseJournal,
        personas: _FakePersonas,
        event_schedule: EventSchedule,
        probe: _ConcurrencyProbe,
        failure_switch: dict[str, bool],
    ) -> None:
        self.store = store
        self.journal = journal
        self.personas = personas
        self.event_schedule = event_schedule
        self.probe = probe
        self.failure_switch = failure_switch

    async def run_stb(
        self,
        *,
        agent_id: str,
        event_id: str,
        phase_attempt_id: str,
        attempt_number: int,
        current_evidence: CurrentEvidencePacket,
    ) -> _FakeStageWriteResult:
        if current_evidence.event_id != event_id:
            raise AssertionError("runner supplied evidence for the wrong event")
        return await self._journaled_stage(
            agent_id=agent_id,
            event_id=event_id,
            phase_attempt_id=phase_attempt_id,
            attempt_number=attempt_number,
            stage="stb",
        )

    async def run_analysis(
        self,
        *,
        agent_id: str,
        event_id: str,
        phase_attempt_id: str,
        attempt_number: int,
    ) -> _FakeStageWriteResult:
        return await self._journaled_stage(
            agent_id=agent_id,
            event_id=event_id,
            phase_attempt_id=phase_attempt_id,
            attempt_number=attempt_number,
            stage="analysis",
        )

    async def run_decision(
        self,
        *,
        agent_id: str,
        event_id: str,
        phase_attempt_id: str,
        attempt_number: int,
    ) -> _FakeStageWriteResult:
        return await self._journaled_stage(
            agent_id=agent_id,
            event_id=event_id,
            phase_attempt_id=phase_attempt_id,
            attempt_number=attempt_number,
            stage="decision",
        )

    def run_fill(self, *, agent_id: str, event_id: str) -> _FakeStageWriteResult:
        with sqlite3.connect(self.store.db_path) as connection:
            connection.execute(
                "INSERT INTO phase_stage_probe (agent_id, stage, response) VALUES (?, ?, ?)",
                (agent_id, "fill", f"fill-{self.store.condition_id}-{event_id}"),
            )
        return _FakeStageWriteResult(
            artifact_id=f"fill-{self.store.condition_id}-{agent_id}",
            logical_call_id=None,
        )

    async def run_post_fill_ltb(
        self,
        *,
        agent_id: str,
        event_id: str,
        phase_attempt_id: str,
        attempt_number: int,
    ) -> _FakeStageWriteResult:
        return await self._journaled_stage(
            agent_id=agent_id,
            event_id=event_id,
            phase_attempt_id=phase_attempt_id,
            attempt_number=attempt_number,
            stage="post_fill_ltb",
        )

    async def _journaled_stage(
        self,
        *,
        agent_id: str,
        event_id: str,
        phase_attempt_id: str,
        attempt_number: int,
        stage: str,
    ) -> _FakeStageWriteResult:
        await self.probe.enter()
        try:
            key = LogicalCallKey(
                "local_run",
                self.store.condition_id,
                agent_id,
                event_id,
                stage,
                f"rn-{stage}-v1",
            )
            request: dict[str, Any] = {
                "agent_id": agent_id,
                "event_id": event_id,
                "schema_version": f"rn-{stage}-v1",
            }
            response = self.journal.get_accepted(key, request)
            if response is None:
                logical_call_id = self.journal.begin_attempt(
                    key,
                    request,
                    phase_attempt_id=phase_attempt_id,
                    attempt_number=attempt_number,
                )
                self.probe.physical_calls += 1
                response = {"belief": f"sealed-{self.store.condition_id}-{agent_id}"}
                self.journal.record_success(
                    logical_call_id,
                    response,
                    phase_attempt_id=phase_attempt_id,
                    attempt_number=attempt_number,
                )
            else:
                logical_call_id = key.value()

            with sqlite3.connect(self.store.db_path) as connection:
                connection.execute(
                    "INSERT INTO phase_stage_probe (agent_id, stage, response) VALUES (?, ?, ?)",
                    (agent_id, stage, str(response["belief"])),
                )
                if stage == "stb":
                    connection.execute(
                        "INSERT INTO phase_probe (agent_id, response) VALUES (?, ?)",
                        (agent_id, str(response["belief"])),
                    )

            # Fail only after the accepted response and scientific write.  The
            # paired coordinator must restore both arm DBs while retaining the
            # journal response for a no-provider-call retry.
            if (
                self.failure_switch["enabled"]
                and self.store.condition_id == RN_COMM_ON
                and agent_id == "A100"
                and stage == "post_fill_ltb"
            ):
                raise TimeoutError("injected local LTB interruption after every prior stage wrote")
            return _FakeStageWriteResult(
                artifact_id=f"{stage}-{self.store.condition_id}-{agent_id}",
                logical_call_id=logical_call_id,
            )
        finally:
            self.probe.leave()


class RNPairedRunnerConcurrencyTests(unittest.TestCase):
    def test_community_calls_are_attributed_to_the_encoded_agent(self) -> None:
        logical_id = (
            "local_run|RN_COMM_ON|A002|E001|community_posting|"
            "rn-community-posting-response-v1"
        )
        self.assertEqual(
            _community_logical_call_owner(
                logical_id,
                expected_run_id="local_run",
                expected_condition_id=RN_COMM_ON,
                expected_event_id="E001",
                allowed_agent_ids=("A001", "A002"),
            ),
            "A002",
        )
        for invalid in (
            logical_id.replace("|A002|", "|A999|"),
            logical_id.replace("|RN_COMM_ON|", "|RN_COMM_OFF|"),
            logical_id.replace("|E001|", "|E999|"),
            logical_id.replace(
                "|community_posting|",
                "|unapproved_community_stage|",
            ),
        ):
            with self.subTest(invalid=invalid), self.assertRaises(RNRunnerError):
                _community_logical_call_owner(
                    invalid,
                    expected_run_id="local_run",
                    expected_condition_id=RN_COMM_ON,
                    expected_event_id="E001",
                    allowed_agent_ids=("A001", "A002"),
                )

    def test_100_agents_are_bounded_and_retry_replays_after_paired_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_sha256 = "c" * 64
            agent_ids = tuple(f"A{ordinal:03d}" for ordinal in range(1, 101))
            event_schedule = EventSchedule.from_rows(
                [
                    {
                        "event_id": "E001",
                        "turn": 1,
                        "date": "2026-01-02",
                        "subturn": "am",
                        "execution_price": 70_000,
                    }
                ]
            )
            personas = _FakePersonas(agent_ids)
            probe = _ConcurrencyProbe()
            failure_switch = {"enabled": True}
            adapters: dict[str, _JournaledFakeAdapter] = {}
            journals: dict[str, ResponseJournal] = {}
            db_paths: dict[str, Path] = {}
            for condition_id in RN_CONDITIONS:
                db_path = root / f"{condition_id}.sqlite"
                with sqlite3.connect(db_path) as connection:
                    connection.execute(
                        "CREATE TABLE phase_probe ("
                        "agent_id TEXT PRIMARY KEY, response TEXT NOT NULL)"
                    )
                    connection.execute(
                        "CREATE TABLE phase_stage_probe ("
                        "agent_id TEXT NOT NULL, stage TEXT NOT NULL, response TEXT NOT NULL, "
                        "PRIMARY KEY (agent_id, stage))"
                    )
                db_paths[condition_id] = db_path
                journal = ResponseJournal(
                    root / f"{condition_id}.journal.sqlite",
                    manifest_sha256=manifest_sha256,
                )
                journals[condition_id] = journal
                adapters[condition_id] = _JournaledFakeAdapter(
                    store=_FakeStore(
                        db_path,
                        condition_id=condition_id,
                        manifest_sha256=manifest_sha256,
                    ),
                    journal=journal,
                    personas=personas,
                    event_schedule=event_schedule,
                    probe=probe,
                    failure_switch=failure_switch,
                )

            worker_limit = 7
            runner = RNPairedRunner(
                run_dir=root / "run",
                adapters=adapters,  # type: ignore[arg-type]
                agent_ids=agent_ids,
                event_schedule=event_schedule,
                evidence_provider=_LocalEvidenceProvider(),
                community_lifecycle=_NoopCommunityLifecycle(),
                max_workers_per_arm=worker_limit,
            )

            with self.assertRaisesRegex(
                RNPhasePausedError,
                "injected local LTB interruption after every prior stage wrote",
            ):
                asyncio.run(runner._event_phase(event_id="E001"))

            self.assertEqual(probe.active, 0)
            self.assertEqual(probe.peak, worker_limit * len(RN_CONDITIONS))
            self.assertEqual(probe.invocations, 800)
            self.assertEqual(probe.physical_calls, 800)
            for condition_id in RN_CONDITIONS:
                with sqlite3.connect(db_paths[condition_id]) as connection:
                    row_count = connection.execute(
                        "SELECT COUNT(*) FROM phase_probe"
                    ).fetchone()[0]
                    staged_row_count = connection.execute(
                        "SELECT COUNT(*) FROM phase_stage_probe"
                    ).fetchone()[0]
                self.assertEqual(row_count, 0, condition_id)
                self.assertEqual(staged_row_count, 0, condition_id)
                self.assertEqual(
                    journals[condition_id].committed_summary(),
                    {"pending": 400, "committed": 0, "rolled_back": 0},
                )

            failure_switch["enabled"] = False
            completed = asyncio.run(runner._event_phase(event_id="E001"))

            self.assertEqual(completed.work_item_count, 200)
            self.assertEqual(completed.logical_call_count, 800)
            self.assertEqual(probe.active, 0)
            self.assertLessEqual(probe.peak, worker_limit * len(RN_CONDITIONS))
            self.assertEqual(probe.invocations, 1600)
            # Every accepted response from the failed composite phase was
            # replayed.  A retry therefore performs no additional model call.
            self.assertEqual(probe.physical_calls, 800)
            for condition_id in RN_CONDITIONS:
                with sqlite3.connect(db_paths[condition_id]) as connection:
                    row_count = connection.execute(
                        "SELECT COUNT(*) FROM phase_probe"
                    ).fetchone()[0]
                    staged_row_count = connection.execute(
                        "SELECT COUNT(*) FROM phase_stage_probe"
                    ).fetchone()[0]
                self.assertEqual(row_count, 100, condition_id)
                self.assertEqual(staged_row_count, 500, condition_id)
                self.assertEqual(
                    journals[condition_id].committed_summary(),
                    {"pending": 0, "committed": 400, "rolled_back": 0},
                )

            # Re-entering an already completed phase is checkpoint-idempotent:
            # no worker and no provider call runs a third time.
            again = asyncio.run(runner._event_phase(event_id="E001"))
            self.assertEqual(again, completed)
            self.assertEqual(probe.invocations, 1600)
            self.assertEqual(probe.physical_calls, 800)
            self.assertEqual(set(adapters), {RN_COMM_OFF, RN_COMM_ON})


if __name__ == "__main__":
    unittest.main()
