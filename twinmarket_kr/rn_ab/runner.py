"""Manifest-scoped paired RN scheduler with bounded concurrent workers.

The runner does not know how to create news, community claims, or paid model
responses.  Those are explicit injected boundaries.  What it owns is the
scientific order and recovery contract:

``bootstrap → due outcomes → community preparation → STB → analysis →
decision → deterministic fill → post-fill LTB → community post-phase``.

Every phase is coordinated across the two arms.  On a worker failure the
coordinator restores both arm databases and the next invocation replays
accepted journal responses without calling the model again.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence

from twinmarket_kr.rn_ab.memory import (
    RN_AUXILIARY_STAGE_SCHEMA_VERSIONS,
    EventSchedule,
    PaperMemoryError,
)
from twinmarket_kr.rn_ab.phase_runner import (
    CompletedPhase,
    PhaseWorkItem,
    PhaseWorkResult,
    RNAtomicPhaseCoordinator,
)
from twinmarket_kr.rn_ab.spec import RN_CONDITIONS
from twinmarket_kr.rn_ab.stage_adapter import RNStageAdapter
from twinmarket_kr.rn_ab.stages import CurrentEvidencePacket
from twinmarket_kr.rn_ab.persona_snapshot import FrozenPersona
from twinmarket_kr.rn_ab.initial_state import deterministic_initial_ltb


class RNRunnerError(RuntimeError):
    """The paired scheduler was configured outside its sealed contract."""


class CurrentEvidenceProvider(Protocol):
    """Return the already validated current-only evidence for one agent/event."""

    async def current_evidence(
        self,
        *,
        condition_id: str,
        agent_id: str,
        event_id: str,
    ) -> CurrentEvidencePacket:
        ...


class CommunityLifecycle(Protocol):
    """Required lifecycle for next-AM delivery and post-PM board/no-op handling."""

    async def prepare_event(
        self,
        *,
        condition_id: str,
        event_id: str,
        phase_attempt_id: str,
        attempt_number: int,
    ) -> Sequence[str] | None:
        ...

    async def after_event(
        self,
        *,
        condition_id: str,
        event_id: str,
        phase_attempt_id: str,
        attempt_number: int,
    ) -> Sequence[str] | None:
        ...


SyncSingletonCallback = Callable[[RNStageAdapter], Any]
AsyncSingletonCallback = Callable[[str], Awaitable[None]]


def _community_logical_call_owner(
    logical_call_id: str,
    *,
    expected_run_id: str,
    expected_condition_id: str,
    expected_event_id: str,
    allowed_agent_ids: Sequence[str],
) -> str:
    """Return the sealed author/reader owner encoded by a community call."""

    components = logical_call_id.split("|")
    if len(components) != 6:
        raise RNRunnerError(
            "Community lifecycle logical-call ID has invalid components"
        )
    (
        run_id,
        condition_id,
        agent_id,
        event_id,
        stage,
        schema_version,
    ) = components
    if (
        run_id != expected_run_id
        or condition_id != expected_condition_id
        or agent_id not in set(allowed_agent_ids)
        or event_id != expected_event_id
        or stage not in RN_AUXILIARY_STAGE_SCHEMA_VERSIONS
        or schema_version != RN_AUXILIARY_STAGE_SCHEMA_VERSIONS[stage]
    ):
        raise RNRunnerError(
            "Community lifecycle logical-call ID is outside its agent/event/stage"
        )
    return agent_id


@dataclass(frozen=True)
class RNRunProgress:
    """A checkpoint-friendly local summary; all details remain in run artifacts."""

    completed_phases: tuple[CompletedPhase, ...]
    right_censored_outcome_count: int


class RNPairedRunner:
    """Run a complete local or paid RN pair through atomic stage barriers."""

    def __init__(
        self,
        *,
        run_dir: Path | str,
        adapters: Mapping[str, RNStageAdapter],
        agent_ids: Sequence[str],
        event_schedule: EventSchedule,
        evidence_provider: CurrentEvidenceProvider,
        community_lifecycle: CommunityLifecycle,
        max_workers_per_arm: int,
    ) -> None:
        if set(adapters) != set(RN_CONDITIONS):
            raise RNRunnerError("RN runner requires exactly RN_COMM_OFF and RN_COMM_ON adapters")
        ordered_agents = tuple(str(agent_id) for agent_id in agent_ids)
        if not ordered_agents or len(ordered_agents) != len(set(ordered_agents)):
            raise RNRunnerError("RN runner requires a non-empty ordered unique agent list")
        if (
            isinstance(max_workers_per_arm, bool)
            or not isinstance(max_workers_per_arm, int)
            or max_workers_per_arm < 1
        ):
            raise RNRunnerError("max_workers_per_arm must be a positive integer")
        self.adapters = dict(adapters)
        self.agent_ids = ordered_agents
        self.event_schedule = event_schedule
        self.evidence_provider = evidence_provider
        if community_lifecycle is None:
            raise RNRunnerError(
                "RN runner requires an explicit community lifecycle; RN_COMM_OFF must record its no-op path"
            )
        self.community_lifecycle = community_lifecycle
        self.max_workers_per_arm = max_workers_per_arm
        manifests = {adapter.store.manifest_sha256 for adapter in self.adapters.values()}
        if len(manifests) != 1:
            raise RNRunnerError("Both RN adapters must share one resolved manifest hash")
        for condition_id, adapter in self.adapters.items():
            if adapter.store.condition_id != condition_id:
                raise RNRunnerError("RN adapter condition key differs from its memory store")
            if tuple(adapter.event_schedule.events) != tuple(event_schedule.events):
                raise RNRunnerError("RN adapters must share the same frozen event schedule")
            adapter.personas.assert_agent_set(self.agent_ids)
        self.coordinator = RNAtomicPhaseCoordinator(
            run_dir,
            manifest_sha256=next(iter(manifests)),
            condition_db_paths={key: adapter.store.db_path for key, adapter in self.adapters.items()},
            journals={key: adapter.journal for key, adapter in self.adapters.items()},
        )

    async def run_all(self) -> RNRunProgress:
        completed: list[CompletedPhase] = []
        completed.append(await self._bootstrap_phase())
        for event in self.event_schedule.events:
            completed.extend(await self.run_event(event_id=str(event["event_id"])))
        completed.append(await self._finalize_outcome_phase())
        right_censored = sum(
            adapter.store.outcome_status_counts()["right_censored"]
            for adapter in self.adapters.values()
        )
        return RNRunProgress(tuple(completed), right_censored)

    async def run_event(self, *, event_id: str) -> tuple[CompletedPhase, ...]:
        self.event_schedule.event(event_id)  # fail before creating any checkpoint.
        # Every state transition belonging to an AM/PM scientific turn shares
        # one snapshot.  In particular, a post-fill LTB failure must not leave
        # an apparently committed STB/analysis/decision/fill (or a prepared
        # community claim) behind for this event.
        return (await self._event_phase(event_id=event_id),)

    async def _bootstrap_phase(self) -> CompletedPhase:
        items = [
            PhaseWorkItem(condition_id, agent_id, "initial", "bootstrap")
            for condition_id in RN_CONDITIONS
            for agent_id in self.agent_ids
        ]
        semaphores = self._arm_semaphores()

        async def worker(item: PhaseWorkItem, _phase_attempt_id: str, _attempt_number: int) -> PhaseWorkResult:
            async with semaphores[item.condition_id]:
                adapter = self.adapters[item.condition_id]
                persona = adapter.personas.persona(item.agent_id)
                first_event = self.event_schedule.events[0]
                try:
                    adapter.store.bootstrap_ltb(
                        agent_id=item.agent_id,
                        date=str(first_event["date"]),
                        dimensions=deterministic_initial_ltb(persona),
                    )
                except PaperMemoryError as exc:
                    raise RNRunnerError(f"Cannot bootstrap deterministic LTB0: {exc}") from exc
                return PhaseWorkResult(item.condition_id, ())

        return await self.coordinator.execute_phase("bootstrap_ltb0", items, worker)

    async def _event_phase(self, *, event_id: str) -> CompletedPhase:
        """Execute all event barriers and commit them as one paired phase.

        The individual barriers keep same-turn state isolated: every STB is
        complete before analysis begins, every analysis before decision, and
        so on.  ``RNAtomicPhaseCoordinator.execute_workflow`` owns the single
        snapshot and commits all journaled calls only after community-after
        succeeds too.
        """
        items = [
            PhaseWorkItem(condition_id, agent_id, event_id, "event_turn")
            for condition_id in RN_CONDITIONS
            for agent_id in self.agent_ids
        ]
        semaphores = self._arm_semaphores()
        logical_ids: dict[tuple[str, str], list[str]] = {
            (condition_id, agent_id): []
            for condition_id in RN_CONDITIONS
            for agent_id in self.agent_ids
        }

        async def stage_worker(
            item: PhaseWorkItem,
            *,
            stage: str,
            phase_attempt_id: str,
            attempt_number: int,
        ) -> None:
            async with semaphores[item.condition_id]:
                adapter = self.adapters[item.condition_id]
                if stage == "stb":
                    evidence = await self.evidence_provider.current_evidence(
                        condition_id=item.condition_id,
                        agent_id=item.agent_id,
                        event_id=item.event_id,
                    )
                    result = await adapter.run_stb(
                        agent_id=item.agent_id,
                        event_id=item.event_id,
                        phase_attempt_id=phase_attempt_id,
                        attempt_number=attempt_number,
                        current_evidence=evidence,
                    )
                elif stage == "analysis":
                    result = await adapter.run_analysis(
                        agent_id=item.agent_id,
                        event_id=item.event_id,
                        phase_attempt_id=phase_attempt_id,
                        attempt_number=attempt_number,
                    )
                elif stage == "decision":
                    result = await adapter.run_decision(
                        agent_id=item.agent_id,
                        event_id=item.event_id,
                        phase_attempt_id=phase_attempt_id,
                        attempt_number=attempt_number,
                    )
                elif stage == "fill":
                    result = adapter.run_fill(agent_id=item.agent_id, event_id=item.event_id)
                elif stage == "post_fill_ltb":
                    result = await adapter.run_post_fill_ltb(
                        agent_id=item.agent_id,
                        event_id=item.event_id,
                        phase_attempt_id=phase_attempt_id,
                        attempt_number=attempt_number,
                    )
                else:  # pragma: no cover - fixed stage sequence above.
                    raise RNRunnerError(f"Unknown RN runner stage: {stage}")
                if result.logical_call_id is not None:
                    logical_ids[(item.condition_id, item.agent_id)].append(result.logical_call_id)

        async def barrier(stage: str, phase_attempt_id: str, attempt_number: int) -> None:
            results = await asyncio.gather(
                *(
                    stage_worker(
                        item,
                        stage=stage,
                        phase_attempt_id=phase_attempt_id,
                        attempt_number=attempt_number,
                    )
                    for item in items
                ),
                return_exceptions=True,
            )
            errors = [result for result in results if isinstance(result, BaseException)]
            if errors:
                raise errors[0]

        async def singleton_barrier(
            callback: Callable[[str], Awaitable[Sequence[str] | None]],
        ) -> dict[str, tuple[str, ...]]:
            results = await asyncio.gather(
                *(callback(condition_id) for condition_id in RN_CONDITIONS),
                return_exceptions=True,
            )
            errors = [result for result in results if isinstance(result, BaseException)]
            if errors:
                raise errors[0]
            normalized: dict[str, tuple[str, ...]] = {}
            for condition_id, result in zip(RN_CONDITIONS, results, strict=True):
                if result is None:
                    normalized[condition_id] = ()
                    continue
                if not isinstance(result, Sequence) or isinstance(
                    result, (str, bytes, bytearray)
                ):
                    raise RNRunnerError("Community lifecycle returned invalid logical-call IDs")
                values = tuple(str(item) for item in result)
                if any(not item for item in values) or len(values) != len(set(values)):
                    raise RNRunnerError("Community lifecycle logical-call IDs are invalid")
                normalized[condition_id] = values
            return normalized

        def attribute_community_calls(
            condition_id: str,
            call_ids: Sequence[str],
        ) -> None:
            """Bind each lifecycle call to the agent encoded by its sealed ID."""

            for logical_call_id in call_ids:
                agent_id = _community_logical_call_owner(
                    logical_call_id,
                    expected_run_id=self.adapters[condition_id].store.run_id,
                    expected_condition_id=condition_id,
                    expected_event_id=event_id,
                    allowed_agent_ids=self.agent_ids,
                )
                logical_ids[(condition_id, agent_id)].append(logical_call_id)

        async def workflow(phase_attempt_id: str, attempt_number: int) -> Sequence[PhaseWorkResult]:
            # Deterministic outcomes and community rows are part of this
            # event's scientific transaction, not earlier standalone commits.
            for condition_id in RN_CONDITIONS:
                self.adapters[condition_id].store.mature_outcomes_for_event(event_id=event_id)
            prepared_calls = await singleton_barrier(
                lambda condition_id: self.community_lifecycle.prepare_event(
                    condition_id=condition_id,
                    event_id=event_id,
                    phase_attempt_id=phase_attempt_id,
                    attempt_number=attempt_number,
                )
            )
            for condition_id, ids in prepared_calls.items():
                attribute_community_calls(condition_id, ids)
            for stage in ("stb", "analysis", "decision", "fill", "post_fill_ltb"):
                await barrier(stage, phase_attempt_id, attempt_number)
            community_calls = await singleton_barrier(
                lambda condition_id: self.community_lifecycle.after_event(
                    condition_id=condition_id,
                    event_id=event_id,
                    phase_attempt_id=phase_attempt_id,
                    attempt_number=attempt_number,
                )
            )
            for condition_id, ids in community_calls.items():
                attribute_community_calls(condition_id, ids)
            return [
                PhaseWorkResult(
                    item.condition_id,
                    tuple(logical_ids[(item.condition_id, item.agent_id)]),
                )
                for item in items
            ]

        return await self.coordinator.execute_workflow(f"{event_id}:scientific_turn", items, workflow)

    def _arm_semaphores(self) -> dict[str, asyncio.Semaphore]:
        """Create one sealed model-work cap per experimental arm.

        No semaphore is shared across ``RN_COMM_OFF`` and ``RN_COMM_ON``:
        ``max_workers_per_arm`` is the manifest-bound maximum for each arm.
        Database writes remain short and the outer paired snapshot provides
        recovery if a worker exhausts SQLite's bounded busy wait.
        """

        return {
            condition_id: asyncio.Semaphore(self.max_workers_per_arm)
            for condition_id in RN_CONDITIONS
        }

    async def _singleton_phase(
        self,
        *,
        phase_id: str,
        callback: SyncSingletonCallback | None = None,
        callback_async: AsyncSingletonCallback | None = None,
    ) -> CompletedPhase:
        if (callback is None) == (callback_async is None):
            raise RNRunnerError("Singleton phase requires exactly one synchronous or asynchronous callback")
        items = [
            PhaseWorkItem(condition_id, "__server__", phase_id, "server")
            for condition_id in RN_CONDITIONS
        ]

        async def worker(item: PhaseWorkItem, _phase_attempt_id: str, _attempt_number: int) -> PhaseWorkResult:
            if callback is not None:
                callback(self.adapters[item.condition_id])
            else:
                await callback_async(item.condition_id)  # type: ignore[misc]
            return PhaseWorkResult(item.condition_id, ())

        return await self.coordinator.execute_phase(phase_id, items, worker)

    async def _finalize_outcome_phase(self) -> CompletedPhase:
        return await self._singleton_phase(
            phase_id="finalize:right_censor_outcomes",
            callback=lambda adapter: adapter.store.right_censor_unavailable_outcomes(),
        )
