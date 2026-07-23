from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from twinmarket_kr.rn_ab.exports import export_final_fill_csvs
from twinmarket_kr.rn_ab.journal import LogicalCallKey, ResponseJournal
from twinmarket_kr.rn_ab.phase_runner import (
    PhaseWorkItem,
    PhaseWorkResult,
    RNAtomicPhaseCoordinator,
    RNPhasePausedError,
)
from twinmarket_kr.rn_ab.spec import RN_COMM_OFF, RN_COMM_ON, RN_CONDITIONS


class RNAtomicPhaseCoordinatorTests(unittest.TestCase):
    def test_failure_restores_both_arms_and_retry_reuses_accepted_response(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = "a" * 64
            dbs = {condition: root / f"{condition}.sqlite" for condition in RN_CONDITIONS}
            for database in dbs.values():
                with sqlite3.connect(database) as connection:
                    connection.execute("CREATE TABLE phase_probe (agent_id TEXT PRIMARY KEY, response TEXT NOT NULL)")
            journals = {
                condition: ResponseJournal(root / f"{condition}.journal.sqlite", manifest_sha256=manifest)
                for condition in RN_CONDITIONS
            }
            coordinator = RNAtomicPhaseCoordinator(
                root / "run", manifest_sha256=manifest, condition_db_paths=dbs, journals=journals
            )
            items = [
                PhaseWorkItem(RN_COMM_OFF, "agent_001", "event_001", "belief"),
                PhaseWorkItem(RN_COMM_ON, "agent_001", "event_001", "belief"),
            ]
            physical_calls = {condition: 0 for condition in RN_CONDITIONS}
            fail = {"enabled": True}

            async def worker(item: PhaseWorkItem, phase_attempt_id: str, attempt_number: int) -> PhaseWorkResult:
                key = LogicalCallKey("run_001", item.condition_id, item.agent_id, item.event_id, item.stage, "v1")
                request = {"agent_id": item.agent_id, "event_id": item.event_id, "stage": item.stage}
                response = journals[item.condition_id].get_accepted(key, request)
                if response is None:
                    logical_id = journals[item.condition_id].begin_attempt(
                        key, request, phase_attempt_id=phase_attempt_id, attempt_number=attempt_number
                    )
                    physical_calls[item.condition_id] += 1
                    response = {"decision": "hold"}
                    journals[item.condition_id].record_success(
                        logical_id, response, phase_attempt_id=phase_attempt_id, attempt_number=attempt_number
                    )
                else:
                    logical_id = key.value()
                with sqlite3.connect(dbs[item.condition_id]) as connection:
                    connection.execute(
                        "INSERT OR REPLACE INTO phase_probe (agent_id, response) VALUES (?, ?)",
                        (item.agent_id, response["decision"]),
                    )
                if fail["enabled"] and item.condition_id == RN_COMM_ON:
                    raise TimeoutError("simulated local interruption after write")
                return PhaseWorkResult(item.condition_id, (logical_id,))

            with self.assertRaises(RNPhasePausedError):
                asyncio.run(coordinator.execute_phase("event_001:belief", items, worker))
            for database in dbs.values():
                with sqlite3.connect(database) as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM phase_probe").fetchone()[0], 0)
            self.assertEqual(journals[RN_COMM_OFF].committed_summary(), {"pending": 1, "committed": 0, "rolled_back": 0})
            self.assertEqual(journals[RN_COMM_ON].committed_summary(), {"pending": 1, "committed": 0, "rolled_back": 0})

            fail["enabled"] = False
            completed = asyncio.run(coordinator.execute_phase("event_001:belief", items, worker))
            self.assertEqual(completed.work_item_count, 2)
            self.assertEqual(physical_calls, {RN_COMM_OFF: 1, RN_COMM_ON: 1})
            for database in dbs.values():
                with sqlite3.connect(database) as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM phase_probe").fetchone()[0], 1)
            self.assertEqual(journals[RN_COMM_OFF].committed_summary()["committed"], 1)
            self.assertEqual(journals[RN_COMM_ON].committed_summary()["committed"], 1)

            again = asyncio.run(coordinator.execute_phase("event_001:belief", items, worker))
            self.assertEqual(again, completed)
            self.assertEqual(physical_calls, {RN_COMM_OFF: 1, RN_COMM_ON: 1})

    def test_second_arm_commit_failure_is_finished_on_restart_without_replaying_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = "c" * 64
            dbs = {condition: root / f"{condition}.sqlite" for condition in RN_CONDITIONS}
            for database in dbs.values():
                with sqlite3.connect(database) as connection:
                    connection.execute(
                        "CREATE TABLE phase_probe (agent_id TEXT PRIMARY KEY, response TEXT NOT NULL)"
                    )
            journals = {
                condition: ResponseJournal(root / f"{condition}.journal.sqlite", manifest_sha256=manifest)
                for condition in RN_CONDITIONS
            }
            run_dir = root / "run"
            coordinator = RNAtomicPhaseCoordinator(
                run_dir, manifest_sha256=manifest, condition_db_paths=dbs, journals=journals
            )
            items = [
                PhaseWorkItem(RN_COMM_OFF, "agent_001", "event_001", "belief"),
                PhaseWorkItem(RN_COMM_ON, "agent_001", "event_001", "belief"),
            ]
            physical_calls = {condition: 0 for condition in RN_CONDITIONS}

            async def worker(item: PhaseWorkItem, phase_attempt_id: str, attempt_number: int) -> PhaseWorkResult:
                key = LogicalCallKey(
                    "run_001", item.condition_id, item.agent_id, item.event_id, item.stage, "v1"
                )
                request = {"agent_id": item.agent_id, "event_id": item.event_id, "stage": item.stage}
                logical_id = journals[item.condition_id].begin_attempt(
                    key, request, phase_attempt_id=phase_attempt_id, attempt_number=attempt_number
                )
                physical_calls[item.condition_id] += 1
                response = {"decision": "hold"}
                journals[item.condition_id].record_success(
                    logical_id, response, phase_attempt_id=phase_attempt_id, attempt_number=attempt_number
                )
                with sqlite3.connect(dbs[item.condition_id]) as connection:
                    connection.execute(
                        "INSERT INTO phase_probe (agent_id, response) VALUES (?, ?)",
                        (item.agent_id, response["decision"]),
                    )
                return PhaseWorkResult(item.condition_id, (logical_id,))

            with patch.object(
                journals[RN_COMM_ON],
                "mark_committed",
                side_effect=RuntimeError("injected second-arm journal commit failure"),
            ):
                with self.assertRaisesRegex(RNPhasePausedError, "durable commit decision"):
                    asyncio.run(coordinator.execute_phase("event_001:belief", items, worker))

            for database in dbs.values():
                with sqlite3.connect(database) as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM phase_probe").fetchone()[0], 1)
            self.assertEqual(
                journals[RN_COMM_OFF].committed_summary(),
                {"pending": 0, "committed": 1, "rolled_back": 0},
            )
            self.assertEqual(
                journals[RN_COMM_ON].committed_summary(),
                {"pending": 1, "committed": 0, "rolled_back": 0},
            )
            checkpoint_path = run_dir / "rn_phase_checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            self.assertEqual(checkpoint["inflight_phase"]["state"], "commit_decided")
            self.assertEqual(checkpoint["inflight_phase"]["logical_call_count"], 2)

            restarted = RNAtomicPhaseCoordinator(
                run_dir, manifest_sha256=manifest, condition_db_paths=dbs, journals=journals
            )
            replayed_work = {"count": 0}

            async def worker_after_restart(
                item: PhaseWorkItem, phase_attempt_id: str, attempt_number: int
            ) -> PhaseWorkResult:
                replayed_work["count"] += 1
                raise AssertionError("commit recovery must not replay scientific work")

            completed = asyncio.run(
                restarted.execute_phase("event_001:belief", items, worker_after_restart)
            )
            self.assertEqual(completed.work_item_count, 2)
            self.assertEqual(completed.logical_call_count, 2)
            self.assertEqual(replayed_work["count"], 0)
            self.assertEqual(physical_calls, {RN_COMM_OFF: 1, RN_COMM_ON: 1})
            for database in dbs.values():
                with sqlite3.connect(database) as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM phase_probe").fetchone()[0], 1)
            for condition_id in RN_CONDITIONS:
                self.assertEqual(
                    journals[condition_id].committed_summary(),
                    {"pending": 0, "committed": 1, "rolled_back": 0},
                )
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            self.assertIsNone(checkpoint["inflight_phase"])
            self.assertEqual(checkpoint["last_recovery"]["mode"], "finish_commit_decided")
            self.assertEqual(len(checkpoint["completed_phases"]), 1)

    def test_same_process_concurrent_call_waits_asynchronously_and_reuses_completion(self) -> None:
        """A second scheduler coroutine must not block the event loop on flock."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = "d" * 64
            dbs = {condition: root / f"{condition}.sqlite" for condition in RN_CONDITIONS}
            for database in dbs.values():
                with sqlite3.connect(database) as connection:
                    connection.execute("CREATE TABLE phase_probe (agent_id TEXT PRIMARY KEY)")
            journals = {
                condition: ResponseJournal(root / f"{condition}.journal.sqlite", manifest_sha256=manifest)
                for condition in RN_CONDITIONS
            }
            coordinator = RNAtomicPhaseCoordinator(
                root / "run", manifest_sha256=manifest, condition_db_paths=dbs, journals=journals
            )
            items = [
                PhaseWorkItem(RN_COMM_OFF, "agent_001", "event_001", "belief"),
                PhaseWorkItem(RN_COMM_ON, "agent_001", "event_001", "belief"),
            ]
            started = asyncio.Event()
            release = asyncio.Event()
            work_calls = {"count": 0}

            async def worker(
                item: PhaseWorkItem, phase_attempt_id: str, attempt_number: int
            ) -> PhaseWorkResult:
                del phase_attempt_id, attempt_number
                work_calls["count"] += 1
                started.set()
                await release.wait()
                return PhaseWorkResult(item.condition_id, ())

            async def drive() -> tuple[object, object]:
                first = asyncio.create_task(coordinator.execute_phase("event_001:belief", items, worker))
                await started.wait()
                second = asyncio.create_task(coordinator.execute_phase("event_001:belief", items, worker))
                # Let the second coroutine reach the async lock while the
                # first is still awaiting workers.  A blocking flock here
                # would freeze this loop and make the test time out.
                await asyncio.sleep(0)
                release.set()
                return await asyncio.wait_for(asyncio.gather(first, second), timeout=2.0)

            first, second = asyncio.run(drive())
            self.assertEqual(first, second)
            self.assertEqual(work_calls["count"], 2)

    def test_human_readable_csvs_have_a_pinned_export_index(self) -> None:
        class FakeStore:
            def __init__(self, condition_id: str) -> None:
                self.condition_id = condition_id

            def export_canonical_final_fill_ledger(
                self, path: Path | str, *, evaluator_contract_sha256: str
            ) -> Path:
                destination = Path(path)
                destination.write_text(
                    "fill_id,condition_id,manifest_hash,fill_status\n"
                    f"fill-1,{self.condition_id},{evaluator_contract_sha256},filled\n",
                    encoding="utf-8",
                )
                return destination

        with tempfile.TemporaryDirectory() as temporary:
            index = export_final_fill_csvs(
                temporary,
                evaluator_contract_sha256="b" * 64,
                stores={condition: FakeStore(condition) for condition in RN_CONDITIONS},
            )
            payload = json.loads(index.read_text(encoding="utf-8"))
            self.assertEqual(payload["evaluator_contract_sha256"], "b" * 64)
            self.assertEqual(payload["exports"][RN_COMM_OFF]["row_count"], 1)
            self.assertEqual(
                payload["exports"][RN_COMM_ON]["format"], "rn_canonical_final_fill_csv_v1"
            )
