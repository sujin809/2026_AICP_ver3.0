"""Run-local current-evidence construction for the paired RN scheduler.

The provider never accepts caller-supplied news, timestamps, dates, or depth.
Those values are resolved from :class:`RNRunContext` and its sealed persona,
event, news, and timestamp registries.  No method in this module performs a
network request or model call.

The frozen event feed supports the approved D0 headline-only and D1/D2
title+summary projections.  It does *not* contain the separately required D2
recent-search registry.  ``missing_full_run_dependencies()`` keeps that gap
explicit so a local replay can run without pretending the live 100-agent
paper contract is GO.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from twinmarket_kr.rn_ab.community_lifecycle import RNCommunityLifecycleAdapter
from twinmarket_kr.rn_ab.run_context import RNRunContext
from twinmarket_kr.rn_ab.spec import RN_COMM_ON, RN_CONDITIONS
from twinmarket_kr.rn_ab.stages import CurrentEvidencePacket, StageContractError


class RNEvidenceProviderError(RuntimeError):
    """A current-evidence request falls outside the sealed run contract."""


class _ZeroCommunityClaimsVerifier:
    """Fail-closed verifier used when no durable community lifecycle exists."""

    def __init__(self, *, agent_id: str, event_id: str) -> None:
        self.agent_id = agent_id
        self.event_id = event_id

    def validate_claims_for_agent(
        self,
        *,
        agent_id: str,
        event_id: str,
        claims: Sequence[Mapping[str, Any]],
    ) -> tuple[Any, ...]:
        if agent_id != self.agent_id or event_id != self.event_id:
            raise RNEvidenceProviderError("Zero-claim verifier was rebound to another agent/event")
        if list(claims):
            raise RNEvidenceProviderError(
                "Community claims require an injected durable RN community lifecycle"
            )
        return ()


class RNRunContextEvidenceProvider:
    """Concrete local-only implementation of ``runner.CurrentEvidenceProvider``."""

    local_only = True
    network_requests = 0
    paid_api_calls = 0
    def __init__(
        self,
        context: RNRunContext,
        *,
        community_lifecycle: RNCommunityLifecycleAdapter | None = None,
    ) -> None:
        if not isinstance(context, RNRunContext):
            raise RNEvidenceProviderError("evidence provider requires an RNRunContext")
        try:
            context.personas.assert_agent_set(context.agent_ids)
            context.stage_inputs.assert_matches_schedule(context.event_schedule)
        except Exception as exc:
            raise RNEvidenceProviderError(f"Run context evidence dependencies are invalid: {exc}") from exc

        schedule_ids = tuple(str(event["event_id"]) for event in context.event_schedule.events)
        if set(context.news_registry.slots_by_event) != set(schedule_ids):
            raise RNEvidenceProviderError(
                "Sealed news slot events must exactly equal the frozen decision schedule"
            )
        for agent_id in context.agent_ids:
            depth = context.personas.persona(agent_id).news_depth
            if isinstance(depth, bool) or not isinstance(depth, int) or depth not in {0, 1, 2}:
                raise RNEvidenceProviderError(
                    f"Sealed persona has an unsupported news depth: {agent_id}={depth!r}"
                )

        if community_lifecycle is not None:
            self._validate_community_lifecycle(context, community_lifecycle)
        self.context = context
        self.community_lifecycle = community_lifecycle
        self.depth2_recent_search_registry_available = context.generated_inputs is not None

    async def current_evidence(
        self,
        *,
        condition_id: str,
        agent_id: str,
        event_id: str,
    ) -> CurrentEvidencePacket:
        """Build and re-validate one exact current-only evidence packet."""

        if condition_id not in RN_CONDITIONS:
            raise RNEvidenceProviderError(f"Unknown RN condition: {condition_id}")
        if agent_id not in self.context.agent_ids:
            raise RNEvidenceProviderError(f"Agent is absent from the frozen cohort: {agent_id}")
        try:
            event = self.context.event_schedule.event(event_id)
            stage_event = self.context.stage_inputs.event(event_id)
            persona = self.context.personas.persona(agent_id)
        except Exception as exc:
            raise RNEvidenceProviderError(f"Cannot resolve sealed agent/event evidence: {exc}") from exc
        if (
            stage_event.event_id != str(event["event_id"])
            or stage_event.date != str(event["date"])
            or stage_event.subturn != str(event["subturn"])
        ):
            raise RNEvidenceProviderError("Stage timestamp binding differs from the frozen event")

        try:
            slots = self.context.news_registry.slots_by_event[event_id]
            news = [
                self.context.news_registry.articles[slot.article_id].stage_projection(
                    news_depth=persona.news_depth
                )
                for slot in slots
            ]
            depth2_search_results = ()
            if persona.news_depth == 2 and self.context.generated_inputs is not None:
                # INTERIM (pre-canary): deterministic most-recent selection from the
                # sealed candidate pool.  Agent keyword search needs a paid call, and
                # this module is contractually call-free (local_only / paid_api_calls
                # = 0), so that selection must move to a scheduler phase before it can
                # replace this line.
                depth2_search_results = self.context.generated_inputs.depth2_select_recent(
                    event_id=event_id,
                    news=self.context.news_registry,
                )
        except Exception as exc:
            raise RNEvidenceProviderError(f"Cannot project sealed real-news slots: {exc}") from exc

        claims: Sequence[Mapping[str, Any]] = ()
        verifier: Any = _ZeroCommunityClaimsVerifier(agent_id=agent_id, event_id=event_id)
        if self.community_lifecycle is not None:
            try:
                claims = self.community_lifecycle.claim_projections(
                    condition_id=condition_id,
                    agent_id=agent_id,
                    event_id=event_id,
                )
                verifier = self.community_lifecycle.services[condition_id]
            except Exception as exc:
                raise RNEvidenceProviderError(
                    f"Cannot reload durable community claims: {exc}"
                ) from exc

        try:
            return CurrentEvidencePacket.from_mapping(
                {
                    "event_id": str(event["event_id"]),
                    "date": str(event["date"]),
                    "subturn": str(event["subturn"]),
                    "news": news,
                    "depth2_search_results": [
                        dict(item) for item in depth2_search_results
                    ],
                    "community_claims": [dict(item) for item in claims],
                },
                agent_id=agent_id,
                event_schedule=self.context.event_schedule,
                news_registry=self.context.news_registry,
                stage_input_registry=self.context.stage_inputs,
                claim_verifier=verifier,
                expected_news_depth=persona.news_depth,
                expected_depth2_search_results=depth2_search_results,
            )
        except StageContractError as exc:
            raise RNEvidenceProviderError(f"Sealed current-evidence validation failed: {exc}") from exc

    def missing_full_run_dependencies(self) -> tuple[str, ...]:
        """Report gaps that local construction must not silently synthesize."""

        missing: list[str] = []
        if self.context.generated_inputs is None and any(
            self.context.personas.persona(agent_id).news_depth == 2
            for agent_id in self.context.agent_ids
        ):
            missing.append("sealed_depth2_recent_search_registry_and_projection")
        if self.community_lifecycle is None:
            missing.append("verified_run_local_community_lifecycle_for_RN_COMM_ON")
        else:
            missing.extend(self.community_lifecycle.missing_full_run_dependencies())
        return tuple(missing)

    @staticmethod
    def _validate_community_lifecycle(
        context: RNRunContext,
        lifecycle: RNCommunityLifecycleAdapter,
    ) -> None:
        if not isinstance(lifecycle, RNCommunityLifecycleAdapter):
            raise RNEvidenceProviderError(
                "community_lifecycle must be the durable RNCommunityLifecycleAdapter"
            )
        if set(lifecycle.services) != set(RN_CONDITIONS):
            raise RNEvidenceProviderError("Community lifecycle has the wrong condition set")
        expected_depths = {
            agent_id: context.personas.persona(agent_id).news_depth
            for agent_id in context.agent_ids
        }
        generated = context.generated_inputs
        observed_bindings = (
            lifecycle.public_profile_registry_sha256,
            lifecycle.community_truth_policy_sha256,
            lifecycle.generated_input_manifest_sha256,
        )
        if generated is None:
            if any(value is not None for value in observed_bindings):
                raise RNEvidenceProviderError(
                    "Community lifecycle claims generated bindings absent from the run context"
                )
            expected_profiles = None
        else:
            expected_bindings = (
                str(generated.public_profile_registry["registry_sha256"]),
                str(generated.truth_policy["policy_sha256"]),
                generated.manifest_sha256,
            )
            if observed_bindings != expected_bindings:
                raise RNEvidenceProviderError(
                    "Community lifecycle generated hashes differ from the sealed run context"
                )
            expected_profiles = generated.public_profiles
        for condition_id in RN_CONDITIONS:
            service = lifecycle.services[condition_id]
            memory = service.memory
            if (
                memory.run_id != context.run_id
                or memory.condition_id != condition_id
                or memory.manifest_sha256 != context.manifest_sha256
                or tuple(memory.event_schedule.events) != tuple(context.event_schedule.events)
                or memory.news_registry.bundle_sha256 != context.news_registry.bundle_sha256
                or memory.stage_input_registry.canonical_sha256
                != context.stage_inputs.canonical_sha256
                or dict(service.cohort_depths) != expected_depths
            ):
                raise RNEvidenceProviderError(
                    "Community lifecycle differs from the sealed RN run context"
                )
            if service.enabled != (condition_id == RN_COMM_ON):
                raise RNEvidenceProviderError("Community lifecycle condition mode is inconsistent")
            if expected_profiles is not None:
                observed_profiles = {
                    agent_id: profile.to_dict()
                    for agent_id, profile in service.public_profiles.items()
                }
                if observed_profiles != expected_profiles:
                    raise RNEvidenceProviderError(
                        "Community lifecycle public profiles differ from generated inputs"
                    )


__all__ = ["RNEvidenceProviderError", "RNRunContextEvidenceProvider"]
