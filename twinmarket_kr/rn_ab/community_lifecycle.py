"""Local-only bridge between :class:`RNPairedRunner` and RN community state.

The core :mod:`twinmarket_kr.rn_ab.community` service owns the append-only
post, exposure, and claim ledgers.  This module owns only lifecycle ordering:

* after a sealed PM event, stage an ON-arm board or an OFF-arm no-op;
* before the registered next AM event, deliver the frozen Best payload;
* interpret only non-empty reader-owned exposures;
* commit claims in frozen cohort order after every local interpretation has
  succeeded; and
* reload those claims from the ledger after process restart.

There is deliberately no implicit OpenRouter client or prompt fallback here.
Local fixtures declare ``local_only = True``.  A real provider must instead
declare ``production_ready = True`` and implement the coordinator attempt /
logical-call handoff used below.
"""
from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol

from twinmarket_kr.rn_ab.community import (
    CommunityContractError,
    CommunityPhaseContext,
    RNCommunityService,
)
from twinmarket_kr.rn_ab.persona_snapshot import FrozenPersona, SealedPersonaSnapshot
from twinmarket_kr.rn_ab.spec import RN_COMM_OFF, RN_COMM_ON, RN_CONDITIONS

if TYPE_CHECKING:
    from twinmarket_kr.rn_ab.resolver import ResolvedStudyManifest


class CommunityLifecycleError(RuntimeError):
    """Raised when lifecycle orchestration would weaken the RN contract."""


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class LocalCommunityBoardProvider(Protocol):
    """Side-effect-free local source for one post-PM board.

    ``post_drafts`` runs first.  Candidate metadata is then derived by the
    server and supplied to ``selective_reads``; a provider never invents a
    post ID or receives another agent's private ledger state.
    """

    local_only: bool

    async def post_drafts(
        self,
        *,
        phase: CommunityPhaseContext,
        personas: Mapping[str, FrozenPersona],
    ) -> Sequence[Mapping[str, Any]]:
        ...

    async def selective_reads(
        self,
        *,
        phase: CommunityPhaseContext,
        candidates: Sequence[Mapping[str, Any]],
        personas: Mapping[str, FrozenPersona],
    ) -> Sequence[Mapping[str, Any]]:
        ...

    def finalized_reader_traces(
        self,
        *,
        phase: CommunityPhaseContext,
        drafts: Sequence[Mapping[str, Any]],
    ) -> Sequence[Mapping[str, Any]]:
        """Return the complete private title-candidate/reaction batch."""

        ...


class LocalCommunityInterpretationProvider(Protocol):
    """Interpret one reader's exact next-AM raw-body exposure locally.

    The result is only the claim projection accepted by
    :meth:`RNCommunityService.record_interpretation_claims`.  This protocol
    does not define or silently synthesize the study's still-unapproved LLM
    prompt bundle.
    """

    local_only: bool

    async def interpret(
        self,
        *,
        persona: FrozenPersona,
        event_id: str,
        exposures: Sequence[Mapping[str, Any]],
    ) -> Sequence[Mapping[str, Any]]:
        ...


class RNCommunityLifecycleAdapter:
    """Manifest-scoped local implementation of ``runner.CommunityLifecycle``."""

    def __init__(
        self,
        *,
        services: Mapping[str, RNCommunityService],
        personas: SealedPersonaSnapshot,
        phase_contexts: Sequence[CommunityPhaseContext | Mapping[str, Any]],
        delivery_timestamps_by_event: Mapping[str, str],
        board_provider: LocalCommunityBoardProvider | None,
        interpretation_provider: LocalCommunityInterpretationProvider | None,
        max_workers: int,
        public_profile_registry_sha256: str | None = None,
        community_truth_policy_sha256: str | None = None,
        generated_input_manifest_sha256: str | None = None,
    ) -> None:
        if set(services) != set(RN_CONDITIONS):
            raise CommunityLifecycleError(
                "RN community lifecycle requires exactly RN_COMM_OFF and RN_COMM_ON services"
            )
        if not isinstance(personas, SealedPersonaSnapshot):
            raise CommunityLifecycleError("RN community lifecycle requires a sealed persona snapshot")
        if isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers < 1:
            raise CommunityLifecycleError("community max_workers must be a positive integer")
        for name, provider in (
            ("board_provider", board_provider),
            ("interpretation_provider", interpretation_provider),
        ):
            if provider is not None and not (
                getattr(provider, "local_only", None) is True
                or getattr(provider, "production_ready", None) is True
            ):
                raise CommunityLifecycleError(
                    f"{name} must explicitly declare local_only=True or production_ready=True"
                )

        normalized_services: dict[str, RNCommunityService] = {}
        schedules: list[tuple[Mapping[str, Any], ...]] = []
        manifests: set[str] = set()
        run_ids: set[str] = set()
        db_paths: set[Path] = set()
        cohort_orders: list[tuple[str, ...]] = []
        depth_maps: list[dict[str, int]] = []
        public_profile_maps: list[dict[str, dict[str, Any]]] = []
        policy_hashes: set[str] = set()
        timing_policy_hashes: set[str] = set()
        for condition_id in RN_CONDITIONS:
            service = services[condition_id]
            if not isinstance(service, RNCommunityService):
                raise CommunityLifecycleError("services must contain RNCommunityService instances")
            if service.memory.condition_id != condition_id:
                raise CommunityLifecycleError("Community service key differs from its memory condition")
            if service.enabled != (condition_id == RN_COMM_ON):
                raise CommunityLifecycleError("Community service mode differs from its RN condition")
            if service.memory.event_schedule is None:  # pragma: no cover - service constructor already requires it.
                raise CommunityLifecycleError("Community service has no frozen event schedule")
            normalized_services[condition_id] = service
            schedules.append(tuple(service.memory.event_schedule.events))
            manifests.add(service.memory.manifest_sha256)
            run_ids.add(service.memory.run_id)
            db_paths.add(service.memory.db_path.resolve())
            cohort_orders.append(tuple(service.cohort_agent_ids))
            depth_maps.append(dict(service.cohort_depths))
            public_profile_maps.append(
                {
                    agent_id: profile.to_dict()
                    for agent_id, profile in service.public_profiles.items()
                }
            )
            policy_hashes.add(service.policy_sha256)
            timing_policy_hashes.add(service.timing_policy_sha256)
        if schedules[0] != schedules[1]:
            raise CommunityLifecycleError("Both community services must share the frozen event schedule")
        if len(manifests) != 1 or len(run_ids) != 1:
            raise CommunityLifecycleError("Both community services must share one run and manifest")
        if len(db_paths) != len(RN_CONDITIONS):
            raise CommunityLifecycleError("RN community arms must use distinct SQLite paths")
        if cohort_orders[0] != cohort_orders[1]:
            raise CommunityLifecycleError("Both community services must share one frozen cohort order")
        if depth_maps[0] != depth_maps[1]:
            raise CommunityLifecycleError("Both community services must share one frozen depth assignment")
        if public_profile_maps[0] != public_profile_maps[1]:
            raise CommunityLifecycleError("Both community services must share identical public profiles")
        if len(policy_hashes) != 1 or len(timing_policy_hashes) != 1:
            raise CommunityLifecycleError("Both community services must share community and timing policies")
        personas.assert_agent_set(cohort_orders[0])

        after_index: dict[str, CommunityPhaseContext] = {}
        target_index: dict[str, CommunityPhaseContext] = {}
        for raw_context in phase_contexts:
            context = (
                raw_context
                if isinstance(raw_context, CommunityPhaseContext)
                else CommunityPhaseContext.from_mapping(raw_context)
            )
            if context.after_event.event_id in after_index:
                raise CommunityLifecycleError("Community lifecycle repeats a PM source event")
            after_index[context.after_event.event_id] = context
            if context.next_am_event is not None:
                target = context.next_am_event.event_id
                if target in target_index:
                    raise CommunityLifecycleError("Community lifecycle repeats a next-AM target event")
                target_index[target] = context
        if not after_index:
            raise CommunityLifecycleError("Community lifecycle requires at least one resolved PM phase")
        if set(delivery_timestamps_by_event) != set(target_index):
            raise CommunityLifecycleError(
                "Delivery timestamp keys must exactly equal non-censored next-AM community targets"
            )
        delivery_timestamps: dict[str, str] = {}
        for event_id, value in delivery_timestamps_by_event.items():
            if not isinstance(value, str) or not value.strip():
                raise CommunityLifecycleError("Community delivery timestamps must be non-empty strings")
            delivery_timestamps[str(event_id)] = value.strip()

        schedule = normalized_services[RN_COMM_OFF].memory.event_schedule
        assert schedule is not None
        for source_event_id, context in after_index.items():
            source = schedule.event(source_event_id)
            if (
                str(source["subturn"]) != "pm"
                or int(source["turn"]) != context.after_event.turn
                or str(source["date"]) != context.after_event.date
            ):
                raise CommunityLifecycleError("Community phase differs from the frozen PM event")
            expected_next = next(
                (
                    item
                    for item in schedule.events
                    if int(item["turn"]) > int(source["turn"]) and str(item["subturn"]) == "am"
                ),
                None,
            )
            if context.next_am_event is None:
                if expected_next is not None:
                    raise CommunityLifecycleError("Community phase falsely censors an available next AM")
            elif expected_next is None or (
                str(expected_next["event_id"]) != context.next_am_event.event_id
                or int(expected_next["turn"]) != context.next_am_event.turn
                or str(expected_next["date"]) != context.next_am_event.date
            ):
                raise CommunityLifecycleError("Community phase skips or invents its next approved AM")

        self.services = MappingProxyType(normalized_services)
        self.personas = personas
        self._persona_view = MappingProxyType(dict(personas.personas))
        self._after_index = MappingProxyType(after_index)
        self._target_index = MappingProxyType(target_index)
        self._delivery_timestamps = MappingProxyType(delivery_timestamps)
        self.board_provider = board_provider
        self.interpretation_provider = interpretation_provider
        self.max_workers = max_workers
        self.public_profile_registry_sha256 = _optional_sha256(
            public_profile_registry_sha256,
            label="public profile registry",
        )
        self.community_truth_policy_sha256 = _optional_sha256(
            community_truth_policy_sha256,
            label="community truth policy",
        )
        self.generated_input_manifest_sha256 = _optional_sha256(
            generated_input_manifest_sha256,
            label="generated input manifest",
        )
        sealed_flags = {
            self.public_profile_registry_sha256 is not None,
            self.community_truth_policy_sha256 is not None,
            self.generated_input_manifest_sha256 is not None,
        }
        if len(sealed_flags) != 1:
            raise CommunityLifecycleError(
                "Generated community profile/truth bindings must be supplied together"
            )

    @classmethod
    def from_resolved_manifest(
        cls,
        manifest: "ResolvedStudyManifest",
        *,
        services: Mapping[str, RNCommunityService],
        personas: SealedPersonaSnapshot,
        board_provider: LocalCommunityBoardProvider | None,
        interpretation_provider: LocalCommunityInterpretationProvider | None,
        max_workers: int,
        public_profile_registry_sha256: str | None = None,
        community_truth_policy_sha256: str | None = None,
        generated_input_manifest_sha256: str | None = None,
    ) -> "RNCommunityLifecycleAdapter":
        """Derive lifecycle contexts only from the resolved calendar.

        PM observation and AM delivery timestamps are the calendar's sealed
        decision timestamps.  Runtime callers cannot move exposure to an
        arbitrary wall-clock time or choose a different next AM event.
        """

        events = tuple(manifest.decision_events)
        by_id = {event.decision_event_id: event for event in events}
        contexts: list[CommunityPhaseContext] = []
        deliveries: dict[str, str] = {}
        for phase in manifest.calendar.community_phases:
            if phase.next_visible_event_rule != "next-approved-AM":
                raise CommunityLifecycleError("Community phase next-visible rule is not approved")
            try:
                source = by_id[phase.after_event_id]
            except KeyError as exc:
                raise CommunityLifecycleError("Community phase source is not a decision event") from exc
            if source.subturn.upper() != "PM" or source.global_ordinal is None:
                raise CommunityLifecycleError("Community phase must follow a resolved PM decision event")
            next_am = next(
                (
                    event
                    for event in events
                    if event.global_ordinal is not None
                    and event.global_ordinal > source.global_ordinal
                    and event.subturn.upper() == "AM"
                ),
                None,
            )
            if next_am is not None and not next_am.consume_scheduled_community:
                raise CommunityLifecycleError("The resolved next AM event does not consume scheduled community")
            mapping = {
                "phase_id": phase.phase_id,
                "after_event": {
                    "event_id": source.decision_event_id,
                    "turn": source.global_ordinal,
                    "date": source.date,
                    "subturn": "pm",
                },
                "observed_at": source.decision_timestamp,
                "next_am_event": None
                if next_am is None
                else {
                    "event_id": next_am.decision_event_id,
                    "turn": next_am.global_ordinal,
                    "date": next_am.date,
                    "subturn": "am",
                },
            }
            context = CommunityPhaseContext.from_mapping(mapping)
            contexts.append(context)
            if next_am is not None:
                if next_am.decision_event_id in deliveries:
                    raise CommunityLifecycleError("Resolved calendar maps two community phases to one AM event")
                deliveries[next_am.decision_event_id] = next_am.decision_timestamp
        return cls(
            services=services,
            personas=personas,
            phase_contexts=contexts,
            delivery_timestamps_by_event=deliveries,
            board_provider=board_provider,
            interpretation_provider=interpretation_provider,
            max_workers=max_workers,
            public_profile_registry_sha256=public_profile_registry_sha256,
            community_truth_policy_sha256=community_truth_policy_sha256,
            generated_input_manifest_sha256=generated_input_manifest_sha256,
        )

    def missing_full_run_dependencies(self) -> tuple[str, ...]:
        """Name deliberate local-only gaps that keep a paid paper run NO-GO."""

        missing: list[str] = []
        if self.public_profile_registry_sha256 is None:
            missing.append("sealed_public_author_profile_registry_and_manifest_hash")
        if self.community_truth_policy_sha256 is None:
            missing.append("sealed_generated_community_post_truth_policy")
        if self.generated_input_manifest_sha256 is None:
            missing.append("sealed_generated_input_manifest_binding")
        if getattr(self.board_provider, "production_ready", None) is not True:
            missing.append("approved_journaled_community_posting_and_read_provider")
        if getattr(self.interpretation_provider, "production_ready", None) is not True:
            missing.append("approved_journaled_next_am_community_interpretation_provider")
        return tuple(missing)

    async def prepare_event(
        self,
        *,
        condition_id: str,
        event_id: str,
        phase_attempt_id: str | None = None,
        attempt_number: int | None = None,
    ) -> tuple[str, ...]:
        """Deliver/interpret the registered next-AM payload before STB."""

        service = self._service(condition_id)
        self._event(service, event_id)
        context = self._target_index.get(event_id)
        if context is None or condition_id == RN_COMM_OFF:
            return ()
        provider_started = False
        try:
            service.deliver_scheduled_best(
                context,
                delivered_at=self._delivery_timestamps[event_id],
            )
            payloads_by_agent = {
                agent_id: service.interpretation_payloads(agent_id=agent_id, event_id=event_id)
                for agent_id in service.cohort_agent_ids
            }
            pending = [
                (agent_id, payloads)
                for agent_id, payloads in payloads_by_agent.items()
                if payloads
            ]
            if not pending:
                return ()
            if self.interpretation_provider is None:
                raise CommunityLifecycleError(
                    "Non-empty community exposure requires an approved local interpretation provider"
                )
            provider_started = _begin_provider_attempt(
                self.interpretation_provider,
                phase_attempt_id=phase_attempt_id,
                attempt_number=attempt_number,
            )
            semaphore = asyncio.Semaphore(self.max_workers)

            async def interpret_one(
                agent_id: str,
                exposures: Sequence[Mapping[str, Any]],
            ) -> tuple[str, tuple[dict[str, Any], ...]]:
                async with semaphore:
                    raw = await self.interpretation_provider.interpret(
                        persona=self.personas.persona(agent_id),
                        event_id=event_id,
                        exposures=tuple(dict(item) for item in exposures),
                    )
                    return agent_id, _mapping_rows(raw, label="community interpretation claims")

            # Provider work is side-effect-free and gathered first.  Settle
            # every worker before aborting the shared provider attempt: a
            # default ``gather`` re-raises the first failure while sibling
            # coroutines may still append journal/audit state after the
            # coordinator has cleared that attempt for rollback/replay.
            # No claim row is written unless every local interpretation
            # succeeded.
            settled = await asyncio.gather(
                *(interpret_one(agent_id, payloads) for agent_id, payloads in pending),
                return_exceptions=True,
            )
            for result in settled:
                if isinstance(result, BaseException):
                    raise result
            staged = tuple(settled)
            staged_by_agent = dict(staged)
            for agent_id in service.cohort_agent_ids:
                if agent_id in staged_by_agent:
                    service.record_interpretation_claims(
                        agent_id=agent_id,
                        event_id=event_id,
                        claims=staged_by_agent[agent_id],
                    )
            return _finish_provider_attempt(
                self.interpretation_provider,
                provider_started=provider_started,
            )
        except (CommunityContractError, CommunityLifecycleError):
            _abort_provider_attempt(self.interpretation_provider, provider_started=provider_started)
            raise
        except Exception as exc:
            _abort_provider_attempt(self.interpretation_provider, provider_started=provider_started)
            raise CommunityLifecycleError(
                f"Community preparation failed for {condition_id}/{event_id}: {exc}"
            ) from exc
        except BaseException:
            # ``asyncio.CancelledError`` inherits BaseException.  A cancelled
            # coordinator must still release the production provider's
            # attempt-local journal state before the phase runner restores its
            # database snapshot and retries in this process.
            _abort_provider_attempt(
                self.interpretation_provider,
                provider_started=provider_started,
            )
            raise

    async def after_event(
        self,
        *,
        condition_id: str,
        event_id: str,
        phase_attempt_id: str | None = None,
        attempt_number: int | None = None,
    ) -> tuple[str, ...]:
        """Freeze the registered post-PM board after its LTB stage commits."""

        service = self._service(condition_id)
        self._event(service, event_id)
        context = self._after_index.get(event_id)
        if context is None:
            return ()
        provider_started = False
        try:
            if condition_id == RN_COMM_OFF:
                service.run_pm_phase(context)
                return ()
            if self.board_provider is None:
                raise CommunityLifecycleError(
                    "RN_COMM_ON PM phase requires an approved local board provider"
                )
            provider_started = _begin_provider_attempt(
                self.board_provider,
                phase_attempt_id=phase_attempt_id,
                attempt_number=attempt_number,
            )
            drafts = _mapping_rows(
                await self.board_provider.post_drafts(
                    phase=context,
                    personas=self._persona_view,
                ),
                label="community post drafts",
            )
            candidates = service.preview_candidate_posts(context, post_drafts=drafts)
            reads = _mapping_rows(
                await self.board_provider.selective_reads(
                    phase=context,
                    candidates=candidates,
                    personas=self._persona_view,
                ),
                label="community selective reads",
            )
            trace_finalizer = getattr(
                self.board_provider, "finalized_post_traces", None
            )
            private_post_traces = None
            if callable(trace_finalizer):
                private_post_traces = _mapping_rows(
                    trace_finalizer(phase=context, drafts=drafts),
                    label="private community post traces",
                )
            elif getattr(self.board_provider, "production_ready", None) is True:
                raise CommunityLifecycleError(
                    "Production community provider must supply private post traces"
                )
            reader_trace_finalizer = getattr(
                self.board_provider, "finalized_reader_traces", None
            )
            private_reader_traces = None
            if callable(reader_trace_finalizer):
                private_reader_traces = _mapping_rows(
                    reader_trace_finalizer(phase=context, drafts=drafts),
                    label="private community reader traces",
                )
            else:
                raise CommunityLifecycleError(
                    "Community provider must supply private candidate/reaction traces"
                )
            finalizer = getattr(self.board_provider, "finalized_post_drafts", None)
            if callable(finalizer):
                drafts = _mapping_rows(
                    finalizer(phase=context, drafts=drafts),
                    label="finalized community post drafts",
                )
            service.run_pm_phase(
                context,
                post_drafts=drafts,
                selective_reads=reads,
                private_post_traces=private_post_traces,
                private_reader_traces=private_reader_traces,
            )
            return _finish_provider_attempt(
                self.board_provider,
                provider_started=provider_started,
            )
        except (CommunityContractError, CommunityLifecycleError):
            _abort_provider_attempt(self.board_provider, provider_started=provider_started)
            raise
        except Exception as exc:
            _abort_provider_attempt(self.board_provider, provider_started=provider_started)
            raise CommunityLifecycleError(
                f"Community post-PM phase failed for {condition_id}/{event_id}: {exc}"
            ) from exc
        except BaseException:
            # See the matching AM boundary above.  Do not leave a cancelled
            # PM board provider in an active attempt that blocks retry.
            _abort_provider_attempt(
                self.board_provider,
                provider_started=provider_started,
            )
            raise

    def claim_projections(
        self,
        *,
        condition_id: str,
        agent_id: str,
        event_id: str,
    ) -> tuple[dict[str, Any], ...]:
        """Reload sanitized STB claim projections from durable state."""

        service = self._service(condition_id)
        event = self._event(service, event_id)
        if str(event["subturn"]) != "am" or condition_id == RN_COMM_OFF:
            return ()
        claims = service.recorded_claims_for_agent(agent_id=agent_id, event_id=event_id)
        return tuple(claim.stage_projection() for claim in claims)

    @staticmethod
    def _event(service: RNCommunityService, event_id: str) -> Mapping[str, Any]:
        if not isinstance(event_id, str) or not event_id.strip():
            raise CommunityLifecycleError("event_id must be a non-empty string")
        schedule = service.memory.event_schedule
        if schedule is None:  # pragma: no cover - constructor enforces this.
            raise CommunityLifecycleError("Community service has no event schedule")
        try:
            return schedule.event(event_id)
        except Exception as exc:
            raise CommunityLifecycleError("event_id is absent from the frozen schedule") from exc

    def _service(self, condition_id: str) -> RNCommunityService:
        if condition_id not in RN_CONDITIONS:
            raise CommunityLifecycleError("Unknown RN condition")
        return self.services[condition_id]


def _mapping_rows(value: Any, *, label: str) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise CommunityLifecycleError(f"{label} must be an ordered array")
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise CommunityLifecycleError(f"{label}[{index}] must be an object")
        rows.append(dict(item))
    return tuple(rows)


def _begin_provider_attempt(
    provider: Any,
    *,
    phase_attempt_id: str | None,
    attempt_number: int | None,
) -> bool:
    if getattr(provider, "production_ready", None) is not True:
        return False
    if (
        not isinstance(phase_attempt_id, str)
        or not phase_attempt_id.strip()
        or isinstance(attempt_number, bool)
        or not isinstance(attempt_number, int)
        or attempt_number < 1
    ):
        raise CommunityLifecycleError(
            "Production community provider requires coordinator phase attempt identity"
        )
    begin = getattr(provider, "begin_phase_attempt", None)
    finish = getattr(provider, "finish_phase_attempt", None)
    abort = getattr(provider, "abort_phase_attempt", None)
    if not callable(begin) or not callable(finish) or not callable(abort):
        raise CommunityLifecycleError(
            "Production community provider lacks journal attempt handoff methods"
        )
    begin(phase_attempt_id=phase_attempt_id, attempt_number=attempt_number)
    return True


def _finish_provider_attempt(provider: Any, *, provider_started: bool) -> tuple[str, ...]:
    if not provider_started:
        return ()
    raw = provider.finish_phase_attempt()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        raise CommunityLifecycleError("Community provider logical-call IDs must be an ordered array")
    values = tuple(str(item) for item in raw)
    if any(not item for item in values) or len(values) != len(set(values)):
        raise CommunityLifecycleError("Community provider logical-call IDs are invalid")
    return values


def _abort_provider_attempt(provider: Any, *, provider_started: bool) -> None:
    if provider_started:
        provider.abort_phase_attempt()


def _optional_sha256(value: Any, *, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise CommunityLifecycleError(f"{label} identity must be a SHA-256 digest")
    return value


__all__ = [
    "CommunityLifecycleError",
    "LocalCommunityBoardProvider",
    "LocalCommunityInterpretationProvider",
    "RNCommunityLifecycleAdapter",
]
