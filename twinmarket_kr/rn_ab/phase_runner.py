"""Crash-safe RN phase coordination for the paired community experiment.

This deliberately sits *above* the existing worker/stage implementations.  A
stage adapter may run any number of workers concurrently, but it must return
the logical journal calls whose scientific writes were made.  The coordinator
then commits both experimental arms together, or restores both arms from the
same phase snapshot.

It does not make model/API calls itself.  That keeps preflight and recovery
tests local, and makes the paid execution adapter an explicit later boundary.
"""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import threading
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from twinmarket_kr.experiment_runtime import backup_database, file_sha256
from twinmarket_kr.rn_ab.journal import ResponseJournal
from twinmarket_kr.rn_ab.spec import RN_CONDITIONS


# ``flock`` is process-wide and blocking.  The per-event-loop lock prevents a
# second coroutine in this process from synchronously blocking the loop while
# the first coordinator awaits 100 workers.  The file lock still serializes
# independent processes/restarts that share a run directory.
_IN_PROCESS_RUN_LOCKS: dict[tuple[int, str], asyncio.Lock] = {}
_IN_PROCESS_RUN_LOCKS_GUARD = threading.Lock()


class RNPhaseError(RuntimeError):
    """Base error for a paired RN phase."""


class RNPhasePausedError(RNPhaseError):
    """A recoverable phase stopped and must be retried or recovered."""


class RNPhaseContractError(RNPhaseError):
    """The run/checkpoint or phase inputs do not match the sealed contract."""


@dataclass(frozen=True)
class PhaseWorkItem:
    """One independently executable worker assignment within a named phase."""

    condition_id: str
    agent_id: str
    event_id: str
    stage: str

    def identity(self) -> str:
        values = (self.condition_id, self.agent_id, self.event_id, self.stage)
        if any(not value for value in values):
            raise RNPhaseContractError("Every PhaseWorkItem identity field is required")
        return "|".join(values)


@dataclass(frozen=True)
class PhaseWorkResult:
    """Worker result.  Calls remain pending until the whole paired phase passes."""

    condition_id: str
    logical_call_ids: tuple[str, ...]


@dataclass(frozen=True)
class CompletedPhase:
    phase_id: str
    phase_attempt_id: str
    work_item_count: int
    logical_call_count: int


PhaseWorker = Callable[[PhaseWorkItem, str, int], Awaitable[PhaseWorkResult]]
PhaseWorkflow = Callable[[str, int], Awaitable[Sequence[PhaseWorkResult]]]


class RNAtomicPhaseCoordinator:
    """Coordinates one all-or-nothing phase across ``RN_COMM_OFF`` and ``ON``.

    SQLite writes performed by existing stores are independently committed, so
    a process-wide SQL transaction cannot provide atomicity.  Instead this
    class takes a consistent SQLite backup of every arm before dispatching
    workers.  Before the durable commit decision, a failure restores *all* arm
    snapshots and accepted journal responses remain ``pending`` for replay
    without another provider call.  After the decision, database writes must
    not be rolled back; restart recovery idempotently finishes both journal
    commits from the checkpoint instead.
    """

    CHECKPOINT_VERSION = "rn-paired-phase-v1"

    def __init__(
        self,
        run_dir: Path | str,
        *,
        manifest_sha256: str,
        condition_db_paths: Mapping[str, Path | str],
        journals: Mapping[str, ResponseJournal],
    ) -> None:
        self.run_dir = Path(run_dir)
        self.manifest_sha256 = manifest_sha256
        self.condition_db_paths = {key: Path(value) for key, value in condition_db_paths.items()}
        self.journals = dict(journals)
        if not manifest_sha256:
            raise RNPhaseContractError("manifest_sha256 is required")
        expected = set(RN_CONDITIONS)
        if set(self.condition_db_paths) != expected or set(self.journals) != expected:
            raise RNPhaseContractError("Coordinator requires exactly RN_COMM_OFF and RN_COMM_ON DBs and journals")
        for condition_id in RN_CONDITIONS:
            if not self.condition_db_paths[condition_id].exists():
                raise FileNotFoundError(f"Missing condition database: {condition_id}")
            if self.journals[condition_id].manifest_sha256 != manifest_sha256:
                raise RNPhaseContractError(f"Journal manifest mismatch for {condition_id}")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoint_path = self.run_dir / "rn_phase_checkpoint.json"
        self.lock_path = self.run_dir / "rn_phase_runner.lock"
        self.snapshot_root = self.run_dir / "rn_phase_snapshots"

    async def execute_phase(
        self,
        phase_id: str,
        work_items: Sequence[PhaseWorkItem],
        worker: PhaseWorker,
    ) -> CompletedPhase:
        """Run one homogeneous worker phase with paired atomic commit."""

        async def workflow(phase_attempt_id: str, attempt_number: int) -> Sequence[PhaseWorkResult]:
            results = await asyncio.gather(
                *(worker(item, phase_attempt_id, attempt_number) for item in work_items),
                return_exceptions=True,
            )
            errors = [result for result in results if isinstance(result, BaseException)]
            if errors:
                raise errors[0]
            return [result for result in results if isinstance(result, PhaseWorkResult)]

        return await self.execute_workflow(phase_id, work_items, workflow)

    async def execute_workflow(
        self,
        phase_id: str,
        work_items: Sequence[PhaseWorkItem],
        workflow: PhaseWorkflow,
    ) -> CompletedPhase:
        """Run a multi-barrier scientific workflow under one paired snapshot.

        A real AM/PM turn has dependency barriers (community preparation,
        STB, analysis, decision, fill, post-fill LTB, post-PM community), but
        it must still commit or roll back as one scientific phase.  The
        workflow may run those barriers internally and returns exactly one
        aggregated result for each declared work item only after all barriers
        succeed.  Journals remain outside the snapshots and are committed
        once, at the end of the complete workflow.
        """
        self._validate_phase(phase_id, work_items)
        async with self._run_lock():
            checkpoint = self._load_checkpoint()
            self._recover_inflight_phase(checkpoint)
            checkpoint = self._load_checkpoint()
            completed = self._completed_phase(checkpoint, phase_id, work_items)
            if completed is not None:
                return completed
            phase_attempt_id = uuid.uuid4().hex
            attempt_number = self._next_attempt_number(checkpoint, phase_id)
            snapshots = self._create_snapshots(phase_id, phase_attempt_id)
            work_item_ids = [item.identity() for item in work_items]
            inflight_phase = {
                "state": "running",
                "phase_id": phase_id,
                "phase_attempt_id": phase_attempt_id,
                "attempt_number": attempt_number,
                "work_item_ids": work_item_ids,
                "work_item_count": len(work_items),
                "snapshots": snapshots,
            }
            self._write_checkpoint(
                {
                    "version": self.CHECKPOINT_VERSION,
                    "manifest_sha256": self.manifest_sha256,
                    "completed_phases": checkpoint.get("completed_phases", []),
                    "inflight_phase": inflight_phase,
                }
            )

            commit_decision_started = False
            commit_decision_durable = False
            try:
                results = await workflow(phase_attempt_id, attempt_number)
                typed_results = list(results)
                if any(not isinstance(result, PhaseWorkResult) for result in typed_results):
                    raise RNPhaseContractError("Workflow must return only PhaseWorkResult values")
                logical_ids = self._validate_results(work_items, typed_results)
                commit_decision = {
                    **inflight_phase,
                    "state": "commit_decided",
                    "logical_call_ids": logical_ids,
                    "logical_call_count": sum(map(len, logical_ids.values())),
                    "commit_decided_at": _utc_now(),
                }
                commit_decision_started = True
                self._write_checkpoint({
                    "version": self.CHECKPOINT_VERSION,
                    "manifest_sha256": self.manifest_sha256,
                    "completed_phases": checkpoint.get("completed_phases", []),
                    "inflight_phase": commit_decision,
                })
                commit_decision_durable = True
                for condition_id in RN_CONDITIONS:
                    self.journals[condition_id].mark_committed(logical_ids[condition_id])

                complete = CompletedPhase(
                    phase_id,
                    phase_attempt_id,
                    len(work_items),
                    sum(map(len, logical_ids.values())),
                )
                completed_phases = list(checkpoint.get("completed_phases", []))
                completed_phases.append({
                    "phase_id": complete.phase_id,
                    "phase_attempt_id": complete.phase_attempt_id,
                    "work_item_ids": work_item_ids,
                    "work_item_count": complete.work_item_count,
                    "logical_call_count": complete.logical_call_count,
                    "completed_at": _utc_now(),
                })
                self._write_checkpoint({
                    "version": self.CHECKPOINT_VERSION,
                    "manifest_sha256": self.manifest_sha256,
                    "completed_phases": completed_phases,
                    "inflight_phase": None,
                })
            except BaseException as exc:
                if commit_decision_started and not commit_decision_durable:
                    try:
                        commit_decision_durable = self._checkpoint_has_commit_outcome(
                            phase_id, phase_attempt_id
                        )
                    except BaseException as checkpoint_exc:
                        raise RNPhasePausedError(
                            f"Phase {phase_id} commit state could not be proven; "
                            "arm databases were left intact for safe recovery"
                        ) from checkpoint_exc
                if commit_decision_durable:
                    raise RNPhasePausedError(
                        f"Phase {phase_id} reached its durable commit decision and awaits "
                        f"journal recovery: {type(exc).__name__}: {exc}"
                    ) from exc
                self._restore_snapshots(snapshots)
                self._write_paused_checkpoint(
                    phase_id,
                    phase_attempt_id,
                    attempt_number,
                    work_items,
                    snapshots,
                    [exc],
                )
                raise RNPhasePausedError(
                    f"Phase {phase_id} restored after workflow failure: {type(exc).__name__}: {exc}"
                ) from exc

            self._remove_snapshots(snapshots)
            return complete

    def abort_pending_phase(self, logical_call_ids: Mapping[str, Sequence[str]]) -> None:
        """Terminally discard accepted calls only when an operator explicitly aborts."""
        if set(logical_call_ids) != set(RN_CONDITIONS):
            raise RNPhaseContractError("Explicit abort must name both conditions")
        for condition_id in RN_CONDITIONS:
            self.journals[condition_id].mark_rolled_back(list(logical_call_ids[condition_id]))

    def _validate_phase(self, phase_id: str, work_items: Sequence[PhaseWorkItem]) -> None:
        if not phase_id or not work_items:
            raise RNPhaseContractError("phase_id and at least one work item are required")
        identities = [item.identity() for item in work_items]
        if len(identities) != len(set(identities)):
            raise RNPhaseContractError("Duplicate PhaseWorkItem identities are not allowed")
        if {item.condition_id for item in work_items} != set(RN_CONDITIONS):
            raise RNPhaseContractError("Every paired phase must include both conditions")

    def _validate_results(
        self, work_items: Sequence[PhaseWorkItem], results: Sequence[PhaseWorkResult]
    ) -> dict[str, list[str]]:
        if len(results) != len(work_items):
            raise RNPhaseContractError("Every worker must return PhaseWorkResult")
        expected_counts = defaultdict(int)
        for item in work_items:
            expected_counts[item.condition_id] += 1
        observed_counts = defaultdict(int)
        logical_ids: dict[str, list[str]] = {condition_id: [] for condition_id in RN_CONDITIONS}
        for result in results:
            if result.condition_id not in logical_ids:
                raise RNPhaseContractError("Worker result has an invalid condition")
            observed_counts[result.condition_id] += 1
            logical_ids[result.condition_id].extend(result.logical_call_ids)
        if dict(observed_counts) != dict(expected_counts):
            raise RNPhaseContractError("Worker result conditions do not match phase assignments")
        for condition_id, ids in logical_ids.items():
            if any(not isinstance(logical_id, str) or not logical_id for logical_id in ids):
                raise RNPhaseContractError(f"Invalid logical call id in {condition_id}")
            if len(ids) != len(set(ids)):
                raise RNPhaseContractError(f"Duplicate logical call id in {condition_id}")
        return logical_ids

    def _completed_phase(
        self, checkpoint: Mapping[str, Any], phase_id: str, work_items: Sequence[PhaseWorkItem]
    ) -> CompletedPhase | None:
        identities = [item.identity() for item in work_items]
        for entry in checkpoint.get("completed_phases", []):
            if entry.get("phase_id") != phase_id:
                continue
            if entry.get("work_item_ids") != identities:
                raise RNPhaseContractError("Completed phase was requested with different work items")
            return CompletedPhase(
                phase_id=phase_id,
                phase_attempt_id=str(entry["phase_attempt_id"]),
                work_item_count=int(entry["work_item_count"]),
                logical_call_count=int(entry["logical_call_count"]),
            )
        return None

    def _recover_inflight_phase(self, checkpoint: Mapping[str, Any]) -> None:
        inflight = checkpoint.get("inflight_phase")
        if not inflight:
            return
        if not isinstance(inflight, dict):
            raise RNPhaseContractError("Invalid inflight phase checkpoint")
        snapshots = inflight.get("snapshots")
        if not isinstance(snapshots, dict):
            raise RNPhaseContractError("Inflight phase lacks trusted snapshots")
        state = inflight.get("state", "running")
        if state == "commit_decided":
            phase_id, phase_attempt_id, work_item_ids, logical_ids = self._validate_commit_decision(inflight)
            try:
                for condition_id in RN_CONDITIONS:
                    self.journals[condition_id].mark_committed(logical_ids[condition_id])
                completed_phases = list(checkpoint.get("completed_phases", []))
                completed_phases.append({
                    "phase_id": phase_id,
                    "phase_attempt_id": phase_attempt_id,
                    "work_item_ids": work_item_ids,
                    "work_item_count": len(work_item_ids),
                    "logical_call_count": sum(map(len, logical_ids.values())),
                    "completed_at": _utc_now(),
                })
                self._write_checkpoint({
                    "version": self.CHECKPOINT_VERSION,
                    "manifest_sha256": self.manifest_sha256,
                    "completed_phases": completed_phases,
                    "inflight_phase": None,
                    "last_recovery": {
                        "phase_id": phase_id,
                        "mode": "finish_commit_decided",
                        "recovered_at": _utc_now(),
                    },
                })
            except BaseException as exc:
                raise RNPhasePausedError(
                    f"Commit-decided phase {phase_id} still awaits journal recovery: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            self._remove_snapshots(snapshots)
            return
        if state != "running":
            raise RNPhaseContractError(f"Unknown inflight phase state: {state!r}")
        self._restore_snapshots(snapshots)
        self._write_checkpoint({
            "version": self.CHECKPOINT_VERSION,
            "manifest_sha256": self.manifest_sha256,
            "completed_phases": checkpoint.get("completed_phases", []),
            "inflight_phase": None,
            "last_recovery": {"phase_id": inflight.get("phase_id"), "recovered_at": _utc_now()},
        })

    def _validate_commit_decision(
        self, inflight: Mapping[str, Any]
    ) -> tuple[str, str, list[str], dict[str, list[str]]]:
        phase_id = inflight.get("phase_id")
        phase_attempt_id = inflight.get("phase_attempt_id")
        work_item_ids = inflight.get("work_item_ids")
        raw_logical_ids = inflight.get("logical_call_ids")
        if not isinstance(phase_id, str) or not phase_id:
            raise RNPhaseContractError("Commit decision lacks phase_id")
        if not isinstance(phase_attempt_id, str) or not phase_attempt_id:
            raise RNPhaseContractError("Commit decision lacks phase_attempt_id")
        if (
            not isinstance(work_item_ids, list)
            or not work_item_ids
            or any(not isinstance(value, str) or not value for value in work_item_ids)
            or len(work_item_ids) != len(set(work_item_ids))
            or inflight.get("work_item_count") != len(work_item_ids)
        ):
            raise RNPhaseContractError("Commit decision has invalid work items")
        if not isinstance(raw_logical_ids, dict) or set(raw_logical_ids) != set(RN_CONDITIONS):
            raise RNPhaseContractError("Commit decision must name both journal arms")
        logical_ids: dict[str, list[str]] = {}
        for condition_id in RN_CONDITIONS:
            values = raw_logical_ids[condition_id]
            if (
                not isinstance(values, list)
                or any(not isinstance(value, str) or not value for value in values)
                or len(values) != len(set(values))
            ):
                raise RNPhaseContractError(f"Invalid commit decision calls for {condition_id}")
            logical_ids[condition_id] = list(values)
        if inflight.get("logical_call_count") != sum(map(len, logical_ids.values())):
            raise RNPhaseContractError("Commit decision logical-call count mismatch")
        return phase_id, phase_attempt_id, list(work_item_ids), logical_ids

    def _checkpoint_has_commit_outcome(self, phase_id: str, phase_attempt_id: str) -> bool:
        checkpoint = self._load_checkpoint()
        inflight = checkpoint.get("inflight_phase")
        if (
            isinstance(inflight, dict)
            and inflight.get("state") == "commit_decided"
            and inflight.get("phase_id") == phase_id
            and inflight.get("phase_attempt_id") == phase_attempt_id
        ):
            return True
        return any(
            entry.get("phase_id") == phase_id and entry.get("phase_attempt_id") == phase_attempt_id
            for entry in checkpoint.get("completed_phases", [])
            if isinstance(entry, dict)
        )

    def _create_snapshots(self, phase_id: str, attempt_id: str) -> dict[str, dict[str, str]]:
        directory = self.snapshot_root / _safe_component(phase_id) / attempt_id
        snapshots: dict[str, dict[str, str]] = {}
        for condition_id in RN_CONDITIONS:
            source = self.condition_db_paths[condition_id]
            target = directory / f"{condition_id}.sqlite"
            backup_database(source, target)
            snapshots[condition_id] = {"path": str(target), "sha256": file_sha256(target)}
        return snapshots

    def _restore_snapshots(self, snapshots: Mapping[str, Mapping[str, str]]) -> None:
        if set(snapshots) != set(RN_CONDITIONS):
            raise RNPhaseContractError("Snapshot set must contain both conditions")
        for condition_id in RN_CONDITIONS:
            metadata = snapshots[condition_id]
            source = Path(str(metadata.get("path", "")))
            if not source.is_file() or file_sha256(source) != metadata.get("sha256"):
                raise RNPhaseContractError(f"Snapshot integrity failure for {condition_id}")
            destination = self.condition_db_paths[condition_id]
            self._remove_sqlite_companions(destination)
            backup_database(source, destination)

    def _write_paused_checkpoint(
        self, phase_id: str, attempt_id: str, attempt_number: int,
        work_items: Sequence[PhaseWorkItem], snapshots: Mapping[str, Mapping[str, str]], errors: Sequence[BaseException],
    ) -> None:
        prior = self._load_checkpoint()
        self._write_checkpoint({
            "version": self.CHECKPOINT_VERSION,
            "manifest_sha256": self.manifest_sha256,
            "completed_phases": prior.get("completed_phases", []),
            "inflight_phase": None,
            "paused_phase": {
                "phase_id": phase_id,
                "phase_attempt_id": attempt_id,
                "attempt_number": attempt_number,
                "work_item_ids": [item.identity() for item in work_items],
                "snapshots": snapshots,
                "errors": [f"{type(error).__name__}: {error}" for error in errors],
                "paused_at": _utc_now(),
            },
        })

    def _next_attempt_number(self, checkpoint: Mapping[str, Any], phase_id: str) -> int:
        paused = checkpoint.get("paused_phase")
        if isinstance(paused, dict) and paused.get("phase_id") == phase_id:
            return int(paused.get("attempt_number", 0)) + 1
        return 1

    def _load_checkpoint(self) -> dict[str, Any]:
        if not self.checkpoint_path.exists():
            return {"version": self.CHECKPOINT_VERSION, "manifest_sha256": self.manifest_sha256, "completed_phases": []}
        payload = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
        if payload.get("version") != self.CHECKPOINT_VERSION or payload.get("manifest_sha256") != self.manifest_sha256:
            raise RNPhaseContractError("Checkpoint does not belong to this sealed manifest")
        if not isinstance(payload.get("completed_phases", []), list):
            raise RNPhaseContractError("Invalid completed_phases checkpoint")
        return payload

    def _write_checkpoint(self, payload: Mapping[str, Any]) -> None:
        temporary = self.checkpoint_path.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(dict(payload), handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(self.checkpoint_path)
        directory_fd = os.open(self.checkpoint_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _remove_snapshots(self, snapshots: Mapping[str, Mapping[str, str]]) -> None:
        parents = {Path(metadata["path"]).parent for metadata in snapshots.values()}
        for parent in parents:
            if parent.is_relative_to(self.snapshot_root):
                shutil.rmtree(parent, ignore_errors=True)

    @staticmethod
    def _remove_sqlite_companions(path: Path) -> None:
        for candidate in (Path(str(path) + "-wal"), Path(str(path) + "-shm")):
            if candidate.exists():
                candidate.unlink()

    @asynccontextmanager
    async def _run_lock(self) -> Any:
        """Acquire an in-process async lock and cross-process file lock safely."""

        loop = asyncio.get_running_loop()
        key = (id(loop), str(self.lock_path.resolve()))
        with _IN_PROCESS_RUN_LOCKS_GUARD:
            local_lock = _IN_PROCESS_RUN_LOCKS.setdefault(key, asyncio.Lock())
        async with local_lock:
            handle = await asyncio.to_thread(_acquire_flock, self.lock_path)
            try:
                yield
            finally:
                await asyncio.to_thread(_release_flock, handle)


def _safe_component(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"phase-{digest}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _acquire_flock(path: Path) -> Any:
    handle = path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except BaseException:
        handle.close()
        raise
    return handle


def _release_flock(handle: Any) -> None:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
