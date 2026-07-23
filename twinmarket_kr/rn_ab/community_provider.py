"""Journaled prompt provider for the RN community lifecycle.

This module prepares the real provider boundary without issuing a request at
construction time.  It binds the reviewed common prompt bundle, strict call
policy, run-local response journal, frozen persona snapshot, and one
``RN_COMM_ON`` community service.  Local tests may inject a ``local_only``
model; a paid graph must inject ``StrictOpenRouterStageModel``.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any, Callable

from twinmarket_kr.db.connection import connect
from twinmarket_kr.rn_ab.call_policy import (
    RN_STRICT_RESPONSE_FORMAT,
    RN_STRICT_TEMPERATURE,
    StrictCallPolicy,
)
from twinmarket_kr.rn_ab.community import (
    CommunityPhaseContext,
    FrozenCommunityPost,
    RNCommunityService,
)
from twinmarket_kr.rn_ab.community_lifecycle import RNCommunityLifecycleAdapter
from twinmarket_kr.rn_ab.journal import LogicalCallKey, ResponseJournal
from twinmarket_kr.rn_ab.memory import (
    RN_AUXILIARY_STAGE_SCHEMA_VERSIONS,
    PaperMemoryError,
    PhaseCallConsumption,
    scientific_sha256,
)
from twinmarket_kr.rn_ab.persona_snapshot import FrozenPersona, SealedPersonaSnapshot
from twinmarket_kr.rn_ab.prompt_registry import (
    COMMUNITY_INTERPRETATION_STAGE,
    COMMUNITY_POSTING_STAGE,
    COMMUNITY_READING_STAGE,
    RNPromptBundle,
)
from twinmarket_kr.rn_ab.spec import RN_COMM_ON
from twinmarket_kr.rn_ab.stage_adapter import (
    RN_TRUSTED_SYSTEM_INSTRUCTION_SHA256,
    RN_TRUSTED_SYSTEM_INSTRUCTION_VERSION,
    StageModel,
    StrictOpenRouterStageModel,
)


class RNCommunityProviderError(RuntimeError):
    """A support prompt, response, or journal edge violated the RN contract."""


POST_TYPES = frozenset(
    {"impression", "question", "trade_share", "profit_share", "analysis", "column"}
)
POST_TYPES_GUIDE = """게시글 타입 6종 (하나를 선택):
- impression: 짧은 감탄, 단상, 느낌
- question: 다른 투자자에게 의견/정보 질문
- trade_share: 오늘 매수/매도 거래 소개
- profit_share: 수익/손실 공유
- analysis: 기술적/뉴스 분석
- column: 긴 호흡의 의견"""

COMMUNITY_CALL_SCHEMA_VERSIONS = MappingProxyType(
    dict(RN_AUXILIARY_STAGE_SCHEMA_VERSIONS)
)
COMMUNITY_CALL_MAX_TOKENS = MappingProxyType(
    {
        "community_posting": 1024,
        "community_read_select": 512,
        "community_read_react": 1024,
        "community_interpretation": 2048,
    }
)


class RNJournaledCommunityProvider:
    """One provider implementing posting, selective reading, reaction and AM interpretation."""

    def __init__(
        self,
        *,
        service: RNCommunityService,
        journal: ResponseJournal,
        prompt_bundle: RNPromptBundle,
        personas: SealedPersonaSnapshot,
        model: StageModel,
        call_policy: StrictCallPolicy,
        study_seed: int,
        seed_namespace: str,
        max_workers: int,
    ) -> None:
        call_policy.validate()
        if service.memory.condition_id != RN_COMM_ON or not service.enabled:
            raise RNCommunityProviderError("Community prompt provider requires RN_COMM_ON service")
        if journal.manifest_sha256 != service.memory.manifest_sha256:
            raise RNCommunityProviderError("Community provider journal/DB manifest mismatch")
        personas.assert_agent_set(service.cohort_agent_ids)
        if isinstance(model, StrictOpenRouterStageModel) and model.policy != call_policy:
            raise RNCommunityProviderError("Community provider model policy differs from sealed policy")
        if isinstance(study_seed, bool) or not isinstance(study_seed, int):
            raise RNCommunityProviderError("Community provider study_seed must be an integer")
        if not isinstance(seed_namespace, str) or not seed_namespace.strip():
            raise RNCommunityProviderError("Community provider seed_namespace is required")
        if isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers < 1:
            raise RNCommunityProviderError("Community provider max_workers must be positive")
        if max_workers != call_policy.concurrency:
            raise RNCommunityProviderError(
                "Community provider concurrency must equal the sealed call-policy concurrency"
            )
        self.service = service
        self.journal = journal
        self.prompt_bundle = prompt_bundle
        self.personas = personas
        self.model = model
        self.call_policy = call_policy
        self.study_seed = study_seed
        self.seed_namespace = seed_namespace.strip()
        self.max_workers = max_workers
        self.local_only = getattr(model, "local_only", None) is True
        self.production_ready = isinstance(model, StrictOpenRouterStageModel)
        if not (self.local_only or self.production_ready):
            raise RNCommunityProviderError(
                "Community provider model must be an explicit local fixture or strict OpenRouter model"
            )
        self._phase_attempt: tuple[str, int] | None = None
        self._logical_call_ids: list[str] = []
        self._response_digests: dict[str, str] = {}
        self._drafts_by_phase: dict[str, tuple[dict[str, Any], ...]] = {}
        self._post_traces_by_phase: dict[str, tuple[dict[str, Any], ...]] = {}
        self._reader_traces_by_phase: dict[str, tuple[dict[str, Any], ...]] = {}
        self._reaction_counts_by_phase: dict[str, dict[str, tuple[int, int]]] = {}

    def begin_phase_attempt(self, *, phase_attempt_id: str, attempt_number: int) -> None:
        if not isinstance(phase_attempt_id, str) or not phase_attempt_id.strip():
            raise RNCommunityProviderError("Community phase_attempt_id is required")
        if isinstance(attempt_number, bool) or not isinstance(attempt_number, int) or attempt_number < 1:
            raise RNCommunityProviderError("Community attempt_number must be positive")
        if self._phase_attempt is not None:
            raise RNCommunityProviderError("Community provider phase attempt is already active")
        self._phase_attempt = (phase_attempt_id.strip(), attempt_number)
        self._logical_call_ids = []
        self._response_digests = {}

    def finish_phase_attempt(self) -> tuple[str, ...]:
        if self._phase_attempt is None:
            raise RNCommunityProviderError("Community provider has no active phase attempt")
        result = tuple(sorted(self._logical_call_ids))
        if len(result) != len(set(result)):
            raise RNCommunityProviderError("Community phase repeated a logical call ID")
        try:
            self.service.memory.record_auxiliary_phase_calls(
                PhaseCallConsumption(
                    stage=logical_call_id.split("|")[-2],
                    logical_call_id=logical_call_id,
                    response_sha256=self._response_digests[logical_call_id],
                )
                for logical_call_id in result
            )
        except (KeyError, PaperMemoryError) as exc:
            raise RNCommunityProviderError(
                f"Cannot persist community journal consumption: {exc}"
            ) from exc
        self._phase_attempt = None
        self._logical_call_ids = []
        self._response_digests = {}
        return result

    def abort_phase_attempt(self) -> None:
        self._phase_attempt = None
        self._logical_call_ids = []
        self._response_digests = {}

    async def post_drafts(
        self,
        *,
        phase: CommunityPhaseContext,
        personas: Mapping[str, FrozenPersona],
    ) -> Sequence[Mapping[str, Any]]:
        self._require_attempt()
        eligible = tuple(
            agent_id
            for agent_id in self.service.cohort_agent_ids
            if self.service.cohort_depths[agent_id] in {1, 2}
        )
        if set(personas) != set(self.service.cohort_agent_ids):
            raise RNCommunityProviderError("Posting persona map differs from frozen cohort")

        async def one(
            agent_id: str,
        ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
            persona = self.personas.persona(agent_id)
            context = self._post_context(agent_id=agent_id, phase=phase)
            values = {
                "persona_prompt": persona.persona_prompt,
                "ltb_dimensions": context["ltb_dimensions"],
                "view_change": context["view_change"],
                "committed_pm_fill": context["committed_pm_fill"],
                "date": phase.after_event.date,
                "post_types_guide": POST_TYPES_GUIDE,
            }
            response, logical_call_id = await self._call(
                template_stage=COMMUNITY_POSTING_STAGE,
                logical_stage="community_posting",
                agent_id=agent_id,
                event_id=phase.after_event.event_id,
                values=values,
                validator=_validate_posting_response,
            )
            trace = {
                "phase_id": phase.phase_id,
                "event_id": phase.after_event.event_id,
                "turn": phase.after_event.turn,
                "date": phase.after_event.date,
                "author_agent_id": agent_id,
                "eligibility_status": "eligible",
                "posting_status": (
                    "skipped" if response["will_post"] is False else "posted"
                ),
                "post_id": None,
                "ltb_id": context["ltb_id"],
                "ltb_sha256": context["ltb_sha256"],
                "view_change_id": context["view_change_id"],
                "view_change_sha256": context["view_change_sha256"],
                "fill_id": context["fill_id"],
                "prompt_template_sha256": self.prompt_bundle.support_template(
                    COMMUNITY_POSTING_STAGE
                ).sha256,
                "prompt_values_sha256": scientific_sha256(values),
                "logical_call_id": logical_call_id,
                "accepted_response_sha256": self._response_digests[
                    logical_call_id
                ],
                "title_sha256": None,
                "body_sha256": None,
            }
            if response["will_post"] is False:
                return None, trace
            body = response["content"]
            title = response["title"]
            draft = {
                "author_agent_id": agent_id,
                "title": title,
                "body": body,
                "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                "post_type": response["post_type"],
                "score": 0,
                "like_count": 0,
            }
            return (
                draft,
                {
                    **trace,
                    "title_sha256": hashlib.sha256(
                        title.encode("utf-8")
                    ).hexdigest(),
                    "body_sha256": draft["body_sha256"],
                },
            )

        rows = await self._gather(*(one(agent_id) for agent_id in eligible))
        drafts = tuple(row[0] for row in rows if row[0] is not None)
        frozen_by_author = {
            post.author_agent_id: post
            for post in self._frozen_posts(phase=phase, drafts=drafts).values()
        }
        traces = tuple(
            {
                **row[1],
                "post_id": (
                    None
                    if row[1]["posting_status"] == "skipped"
                    else frozen_by_author[row[1]["author_agent_id"]].post_id
                ),
            }
            for row in rows
        )
        self._drafts_by_phase[phase.phase_id] = drafts
        self._post_traces_by_phase[phase.phase_id] = traces
        self._reader_traces_by_phase[phase.phase_id] = ()
        self._reaction_counts_by_phase[phase.phase_id] = {}
        return drafts

    async def selective_reads(
        self,
        *,
        phase: CommunityPhaseContext,
        candidates: Sequence[Mapping[str, Any]],
        personas: Mapping[str, FrozenPersona],
    ) -> Sequence[Mapping[str, Any]]:
        self._require_attempt()
        if set(personas) != set(self.service.cohort_agent_ids):
            raise RNCommunityProviderError("Reading persona map differs from frozen cohort")
        drafts = self._drafts_by_phase.get(phase.phase_id)
        if drafts is None:
            raise RNCommunityProviderError("Selective reading has no staged posting batch")
        frozen = self._frozen_posts(phase=phase, drafts=drafts)
        if tuple(item["post_id"] for item in candidates) != tuple(frozen):
            raise RNCommunityProviderError("Candidate order/IDs differ from staged post drafts")

        async def one(
            reader_id: str,
        ) -> tuple[tuple[dict[str, Any], ...], dict[str, Any]]:
            depth = self.service.cohort_depths[reader_id]
            if depth == 0:
                raise RNCommunityProviderError(
                    "Depth 0 reader reached the active selective-read provider path"
                )
            available = [
                post
                for post in frozen.values()
                if post.author_agent_id != reader_id
            ]
            if not available:
                return (), {"reader_agent_id": reader_id, "candidates": []}
            limit = self.service.selective_read_cap(reader_id)
            persona = self.personas.persona(reader_id)
            selected, _ = await self._call(
                template_stage=COMMUNITY_READING_STAGE,
                logical_stage="community_read_select",
                agent_id=reader_id,
                event_id=phase.after_event.event_id,
                values={
                    "persona_prompt": persona.persona_prompt,
                    "mode": "select",
                    "post_list_str": [
                        {
                            "post_id": post.post_id,
                            "title": post.title,
                            "post_type": post.post_type,
                            "score": post.score,
                            "public_author_profile": self.service.public_profiles[
                                post.author_agent_id
                            ].to_dict(),
                        }
                        for post in available
                    ],
                    "read_limit": limit,
                    "posts_content_str": [],
                },
                validator=lambda raw: _validate_selection_response(
                    raw,
                    allowed_ids={post.post_id for post in available},
                    limit=limit,
                ),
            )
            selected_posts = [frozen[post_id] for post_id in selected["selected_post_ids"]]
            reactions_by_post: dict[str, str] = {}
            if not selected_posts:
                reacted = {"reactions": []}
            else:
                reacted, _ = await self._call(
                    template_stage=COMMUNITY_READING_STAGE,
                    logical_stage="community_read_react",
                    agent_id=reader_id,
                    event_id=phase.after_event.event_id,
                    values={
                        "persona_prompt": persona.persona_prompt,
                        "mode": "react",
                        "post_list_str": [],
                        "read_limit": len(selected_posts),
                        "posts_content_str": [
                            {
                                "post_id": post.post_id,
                                "title": post.title,
                                "body": post.body,
                                "body_sha256": post.body_sha256,
                                "post_type": post.post_type,
                                "public_author_profile": self.service.public_profiles[
                                    post.author_agent_id
                                ].to_dict(),
                                "untrusted_content_kind": "community_post",
                                "content_level": "full_body",
                            }
                            for post in selected_posts
                        ],
                    },
                    validator=lambda raw: _validate_reaction_response(
                        raw, selected_ids=tuple(post.post_id for post in selected_posts)
                    ),
                )
            counts = self._reaction_counts_by_phase[phase.phase_id]
            for reaction in reacted["reactions"]:
                reactions_by_post[reaction["post_id"]] = reaction["reaction"]
                likes, unlikes = counts.get(reaction["post_id"], (0, 0))
                if reaction["reaction"] == "like":
                    likes += 1
                elif reaction["reaction"] == "unlike":
                    unlikes += 1
                counts[reaction["post_id"]] = (likes, unlikes)
            selected_ids = set(selected["selected_post_ids"])
            trace = {
                "reader_agent_id": reader_id,
                "candidates": [
                    {
                        "post_id": post.post_id,
                        "title": post.title,
                        "post_type": post.post_type,
                        "score": post.score,
                        "like_count": post.like_count,
                        "selected": post.post_id in selected_ids,
                        "reaction": reactions_by_post.get(post.post_id),
                    }
                    for post in available
                ],
            }
            return (
                tuple(
                    {"reader_agent_id": reader_id, "post_id": post.post_id}
                    for post in selected_posts
                ),
                trace,
            )

        readers = tuple(
            agent_id
            for agent_id in self.service.cohort_agent_ids
            if self.service.cohort_depths[agent_id] in {1, 2}
        )
        batches = await self._gather(*(one(reader_id) for reader_id in readers))
        self._reader_traces_by_phase[phase.phase_id] = tuple(
            batch[1] for batch in batches
        )
        return tuple(row for batch in batches for row in batch[0])

    def finalized_post_drafts(
        self,
        *,
        phase: CommunityPhaseContext,
        drafts: Sequence[Mapping[str, Any]],
    ) -> tuple[dict[str, Any], ...]:
        """Apply only validated reaction counts before deterministic Best ranking."""

        staged = self._drafts_by_phase.get(phase.phase_id)
        if staged is None or tuple(dict(row) for row in drafts) != staged:
            raise RNCommunityProviderError("Finalized posting batch differs from staged drafts")
        counts = self._reaction_counts_by_phase.get(phase.phase_id, {})
        frozen = self._frozen_posts(phase=phase, drafts=staged)
        output: list[dict[str, Any]] = []
        for post in frozen.values():
            likes, unlikes = counts.get(post.post_id, (0, 0))
            original = next(
                row for row in staged if row["author_agent_id"] == post.author_agent_id
            )
            output.append({**original, "score": likes - unlikes, "like_count": likes})
        return tuple(output)

    def finalized_post_traces(
        self,
        *,
        phase: CommunityPhaseContext,
        drafts: Sequence[Mapping[str, Any]],
    ) -> tuple[dict[str, Any], ...]:
        """Return private traces only to the atomic PM persistence boundary."""

        staged = self._drafts_by_phase.get(phase.phase_id)
        traces = self._post_traces_by_phase.get(phase.phase_id)
        if staged is None or traces is None or tuple(dict(row) for row in drafts) != staged:
            raise RNCommunityProviderError(
                "Private post-trace batch differs from staged post drafts"
            )
        return tuple(dict(trace) for trace in traces)

    def finalized_reader_traces(
        self,
        *,
        phase: CommunityPhaseContext,
        drafts: Sequence[Mapping[str, Any]],
    ) -> tuple[dict[str, Any], ...]:
        """Return private candidate/reaction traces to the PM atomic boundary."""

        staged = self._drafts_by_phase.get(phase.phase_id)
        traces = self._reader_traces_by_phase.get(phase.phase_id)
        if staged is None or traces is None or tuple(dict(row) for row in drafts) != staged:
            raise RNCommunityProviderError(
                "Private reader-trace batch differs from staged post drafts"
            )
        return tuple(
            {
                "reader_agent_id": str(trace["reader_agent_id"]),
                "candidates": [dict(candidate) for candidate in trace["candidates"]],
            }
            for trace in traces
        )

    async def interpret(
        self,
        *,
        persona: FrozenPersona,
        event_id: str,
        exposures: Sequence[Mapping[str, Any]],
    ) -> Sequence[Mapping[str, Any]]:
        self._require_attempt()
        available_ids = {
            str(exposure_id)
            for item in exposures
            for exposure_id in item.get("source_exposure_ids", ())
        }
        available_sources: dict[str, dict[str, Any]] = {}
        for item in exposures:
            exposure_ids = item.get("source_exposure_ids", ())
            if not isinstance(exposure_ids, Sequence) or isinstance(
                exposure_ids, (str, bytes, bytearray)
            ):
                raise RNCommunityProviderError(
                    "Community interpretation exposure IDs are malformed"
                )
            level = item.get("content_level")
            title = item.get("title")
            if level not in {"title_only", "full_body"} or not isinstance(title, str):
                raise RNCommunityProviderError(
                    "Community interpretation exposure has no trusted content-level boundary"
                )
            allowed_texts = [title]
            if level == "full_body":
                body = item.get("body")
                if not isinstance(body, str):
                    raise RNCommunityProviderError(
                        "Full-body community exposure is missing its exact body"
                    )
                allowed_texts.append(body)
            for exposure_id in exposure_ids:
                source_id = str(exposure_id)
                material = {
                    "content_level": level,
                    "allowed_texts": tuple(allowed_texts),
                }
                previous = available_sources.get(source_id)
                if previous is not None and previous != material:
                    raise RNCommunityProviderError(
                        "One community exposure ID maps to conflicting source material"
                    )
                available_sources[source_id] = material
        if set(available_sources) != available_ids:
            raise RNCommunityProviderError(
                "Community interpretation source-material map is incomplete"
            )
        response, _ = await self._call(
            template_stage=COMMUNITY_INTERPRETATION_STAGE,
            logical_stage="community_interpretation",
            agent_id=persona.agent_id,
            event_id=event_id,
            values={
                "persona_prompt": persona.persona_prompt,
                "candidate_posts_summary": [
                    dict(item)
                    for item in exposures
                    if item.get("exposure_channel") == "title_only_candidate"
                ],
                "best_posts_summary": [
                    dict(item)
                    for item in exposures
                    if item.get("exposure_channel")
                    in {"best_only_body", "selected_and_best_overlap"}
                ],
                "posts_read_summary": [
                    dict(item)
                    for item in exposures
                    if item.get("exposure_channel")
                    in {"selected_body", "selected_and_best_overlap"}
                ],
            },
            validator=lambda raw: _validate_interpretation_response(
                raw, available_sources=available_sources
            ),
        )
        return tuple(
            {
                "claim_text": claim["claim_text"],
                "stance": claim["claim_stance"],
                "source_exposure_ids": claim["source_exposure_ids"],
                "supporting_quote": claim["supporting_quote"],
            }
            for claim in response["claims"]
        )

    async def _call(
        self,
        *,
        template_stage: str,
        logical_stage: str,
        agent_id: str,
        event_id: str,
        values: Mapping[str, Any],
        validator: Callable[[Mapping[str, Any]], dict[str, Any]],
    ) -> tuple[dict[str, Any], str]:
        phase_attempt_id, attempt_number = self._require_attempt()
        schema_version = COMMUNITY_CALL_SCHEMA_VERSIONS[logical_stage]
        max_tokens = COMMUNITY_CALL_MAX_TOKENS[logical_stage]
        template = self.prompt_bundle.support_template(template_stage)
        seed = self._seed(stage=logical_stage, agent_id=agent_id, event_id=event_id)
        request = {
            "contract_version": "rn-community-stage-request-v1",
            "prompt_template_stage": template_stage,
            "prompt_template_sha256": template.sha256,
            "prompt_values": dict(values),
            "trusted_system_instruction": {
                "version": RN_TRUSTED_SYSTEM_INSTRUCTION_VERSION,
                "sha256": RN_TRUSTED_SYSTEM_INSTRUCTION_SHA256,
            },
            "response_schema_version": schema_version,
            "model": self.call_policy.model,
            "provider": self.call_policy.provider,
            "reasoning": {"effort": "none", "exclude": True},
            "provider_policy": self.call_policy.as_request_policy(),
            "temperature": RN_STRICT_TEMPERATURE,
            "response_format": dict(RN_STRICT_RESPONSE_FORMAT),
            "seed": seed,
            "max_tokens": max_tokens,
        }
        key = LogicalCallKey(
            run_id=self.service.memory.run_id,
            condition_id=RN_COMM_ON,
            agent_id=agent_id,
            event_id=event_id,
            stage=logical_stage,
            schema_version=schema_version,
        )
        existing = self.journal.get_accepted(key, request)
        if existing is not None:
            normalized = validator(existing)
            response_sha256 = scientific_sha256(existing)
            self._record_experiment_acceptance(
                logical_call_id=key.value(),
                phase_attempt_id=phase_attempt_id,
                response_sha256=response_sha256,
            )
            self._logical_call_ids.append(key.value())
            self._response_digests[key.value()] = response_sha256
            return normalized, key.value()
        logical_id = self.journal.begin_attempt(
            key,
            request,
            phase_attempt_id=phase_attempt_id,
            attempt_number=attempt_number,
        )
        journal_accepted = False
        try:
            prompt = self.prompt_bundle.render_support(template_stage, values)
            raw = await self.model.complete(
                prompt=prompt,
                model=self.call_policy.model,
                logical_call_id=logical_id,
                phase_attempt_id=phase_attempt_id,
                seed=seed,
                max_tokens=max_tokens,
            )
            parsed = _parse_exact_json_object(raw, stage=logical_stage)
            normalized = validator(parsed)
            response_sha256 = self.journal.record_success(
                logical_id,
                parsed,
                phase_attempt_id=phase_attempt_id,
                attempt_number=attempt_number,
            )
            journal_accepted = True
            self._record_experiment_acceptance(
                logical_call_id=logical_id,
                phase_attempt_id=phase_attempt_id,
                response_sha256=response_sha256,
            )
            self._logical_call_ids.append(logical_id)
            self._response_digests[logical_id] = response_sha256
            return normalized, logical_id
        except BaseException as exc:
            if journal_accepted:
                if isinstance(exc, RNCommunityProviderError):
                    raise
                raise RNCommunityProviderError(
                    f"Accepted {logical_stage} response could not be audit-bound: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            try:
                self.journal.record_error(
                    logical_id,
                    phase_attempt_id=phase_attempt_id,
                    attempt_number=attempt_number,
                    error=exc,
                )
            except Exception as journal_exc:
                raise RNCommunityProviderError(
                    f"Cannot record community provider error: {journal_exc}"
                ) from journal_exc
            if isinstance(exc, RNCommunityProviderError):
                raise
            raise RNCommunityProviderError(
                f"{logical_stage} failed: {type(exc).__name__}: {exc}"
            ) from exc

    def _record_experiment_acceptance(
        self,
        *,
        logical_call_id: str,
        phase_attempt_id: str,
        response_sha256: str,
    ) -> None:
        if isinstance(self.model, StrictOpenRouterStageModel):
            try:
                self.model.record_experiment_acceptance(
                    logical_call_id=logical_call_id,
                    phase_attempt_id=phase_attempt_id,
                    accepted_response_sha256=response_sha256,
                )
            except Exception as exc:
                raise RNCommunityProviderError(
                    f"Cannot bind community response to provider audit: {exc}"
                ) from exc

    async def _gather(self, *awaitables: Any) -> list[Any]:
        semaphore = asyncio.Semaphore(self.max_workers)

        async def bounded(awaitable: Any) -> Any:
            async with semaphore:
                return await awaitable

        results = await asyncio.gather(*(bounded(item) for item in awaitables), return_exceptions=True)
        errors = [result for result in results if isinstance(result, BaseException)]
        if errors:
            raise errors[0]
        return results

    def _post_context(
        self,
        *,
        agent_id: str,
        phase: CommunityPhaseContext,
    ) -> dict[str, Any]:
        try:
            ltb = self.service.memory.previous_ltb(
                agent_id=agent_id,
                decision_turn=phase.after_event.turn + 1,
            )
            if ltb["event_id"] != phase.after_event.event_id:
                raise RNCommunityProviderError("Posting context did not resolve the current post-fill LTB")
            human = self.service.memory.human_log_for_ltb(ltb_id=str(ltb["ltb_id"]))
        except PaperMemoryError as exc:
            raise RNCommunityProviderError(f"Cannot resolve posting belief context: {exc}") from exc
        with connect(self.service.memory.db_path, read_only=True) as connection:
            row = connection.execute(
                """
                SELECT fill_id, action, requested_quantity, filled_quantity, executed_price,
                       fee_amount, pre_portfolio_json, post_portfolio_json
                FROM paper_fill_ledger
                WHERE run_id = ? AND condition_id = ? AND agent_id = ? AND event_id = ?
                """,
                (
                    self.service.memory.run_id,
                    RN_COMM_ON,
                    agent_id,
                    phase.after_event.event_id,
                ),
            ).fetchone()
        if row is None:
            raise RNCommunityProviderError("Posting context has no committed current fill")
        committed_fill = {
            "action": str(row["action"]),
            "requested_quantity": int(row["requested_quantity"]),
            "filled_quantity": int(row["filled_quantity"]),
            "executed_price": float(row["executed_price"]),
            "fee_amount": float(row["fee_amount"]),
            "pre_portfolio": _load_canonical_object(row["pre_portfolio_json"], "pre_portfolio"),
            "post_portfolio": _load_canonical_object(row["post_portfolio_json"], "post_portfolio"),
        }
        view_change = human["view_change"]
        view_change_sha256 = scientific_sha256(view_change)
        return {
            "ltb_dimensions": {
                dimension: str(ltb["dimensions"][dimension])
                for dimension in ("dim_1", "dim_2", "dim_3", "dim_4", "dim_5", "dim_6")
            },
            "ltb_id": str(ltb["ltb_id"]),
            "ltb_sha256": str(ltb["scientific_sha256"]),
            "view_change": view_change,
            "view_change_id": "view-change:" + view_change_sha256[:40],
            "view_change_sha256": view_change_sha256,
            "fill_id": str(row["fill_id"]),
            "committed_pm_fill": committed_fill,
        }

    def _frozen_posts(
        self,
        *,
        phase: CommunityPhaseContext,
        drafts: Sequence[Mapping[str, Any]],
    ) -> dict[str, FrozenCommunityPost]:
        posts = {
            post.post_id: post
            for post in (
                FrozenCommunityPost.from_draft(
                    draft,
                    run_id=self.service.memory.run_id,
                    condition_id=RN_COMM_ON,
                    phase_id=phase.phase_id,
                    allowed_author_ids=frozenset(self.service.cohort_agent_ids),
                    active_author_ids=frozenset(
                        agent_id
                        for agent_id in self.service.cohort_agent_ids
                        if self.service.cohort_depths[agent_id] in {1, 2}
                    ),
                )
                for draft in drafts
            )
        }
        if len(posts) != len(drafts):
            raise RNCommunityProviderError("Staged community drafts have duplicate post identity")
        return dict(sorted(posts.items()))

    def _seed(self, *, stage: str, agent_id: str, event_id: str) -> int:
        payload = "|".join(
            (str(self.study_seed), self.seed_namespace, stage, agent_id, event_id)
        ).encode("utf-8")
        return int.from_bytes(hashlib.sha256(payload).digest()[:4], "big") & 0x7FFFFFFF

    def _require_attempt(self) -> tuple[str, int]:
        if self._phase_attempt is None:
            raise RNCommunityProviderError(
                "Community provider call is outside a coordinator-owned phase attempt"
            )
        return self._phase_attempt


def build_journaled_community_lifecycle(
    context: Any,
    *,
    model: StageModel,
    call_policy: StrictCallPolicy,
) -> RNCommunityLifecycleAdapter:
    """Construct the run-bound ON/OFF services and journaled prompt provider.

    This is construction only: no prompt is rendered and no model/network call
    occurs.  All public profiles and the truth-policy identity come from the
    preflight-generated, run-local artifact set.
    """

    from twinmarket_kr.rn_ab.run_context import RNRunContext
    from twinmarket_kr.rn_ab.spec import RN_CONDITIONS

    if not isinstance(context, RNRunContext):
        raise RNCommunityProviderError("Community lifecycle factory requires RNRunContext")
    generated = context.generated_inputs
    if generated is None:
        raise RNCommunityProviderError("Run context has no sealed generated community inputs")
    profiles = generated.public_profiles
    services = {
        condition_id: RNCommunityService.from_resolved_manifest(
            context.open_store(condition_id),
            context.resolved,
            public_profiles=profiles,
        )
        for condition_id in RN_CONDITIONS
    }
    provider = RNJournaledCommunityProvider(
        service=services[RN_COMM_ON],
        journal=context.open_journal(RN_COMM_ON),
        prompt_bundle=context.prompt_bundle,
        personas=context.personas,
        model=model,
        call_policy=call_policy,
        study_seed=context.resolved.spec.study_seed,
        seed_namespace=context.resolved.spec.seed_namespace,
        max_workers=call_policy.concurrency,
    )
    return RNCommunityLifecycleAdapter.from_resolved_manifest(
        context.resolved,
        services=services,
        personas=context.personas,
        board_provider=provider,
        interpretation_provider=provider,
        max_workers=call_policy.concurrency,
        public_profile_registry_sha256=str(
            generated.public_profile_registry["registry_sha256"]
        ),
        community_truth_policy_sha256=str(generated.truth_policy["policy_sha256"]),
        generated_input_manifest_sha256=generated.manifest_sha256,
    )


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise RNCommunityProviderError("Community provider value is not canonical JSON") from exc


def _load_canonical_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, str):
        raise RNCommunityProviderError(f"{label} is not stored JSON text")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise RNCommunityProviderError(f"{label} is not JSON") from exc
    if not isinstance(parsed, dict) or _canonical(parsed) != value:
        raise RNCommunityProviderError(f"{label} is not a canonical JSON object")
    return parsed


def _parse_exact_json_object(raw: Any, *, stage: str) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw.strip():
        raise RNCommunityProviderError(f"{stage} response must be a non-empty JSON object")

    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise RNCommunityProviderError(f"{stage} response has duplicate key {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise RNCommunityProviderError(f"{stage} response has non-standard number {value}")

    try:
        parsed = json.loads(raw, object_pairs_hook=pairs_hook, parse_constant=reject_constant)
    except json.JSONDecodeError as exc:
        raise RNCommunityProviderError(f"{stage} response is not strict JSON") from exc
    if not isinstance(parsed, dict):
        raise RNCommunityProviderError(f"{stage} response must be one JSON object")
    return parsed


def _nonempty_text(value: Any, label: str, *, max_chars: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RNCommunityProviderError(f"{label} must be non-empty text")
    text = value.strip()
    if len(text) > max_chars:
        raise RNCommunityProviderError(f"{label} exceeds {max_chars} characters")
    return text


def _validate_posting_response(raw: Mapping[str, Any]) -> dict[str, Any]:
    if set(raw) == {"will_post"} and raw.get("will_post") is False:
        return {"will_post": False}
    if set(raw) != {"will_post", "post_type", "title", "content"} or raw.get("will_post") is not True:
        raise RNCommunityProviderError("Posting response has an invalid exact conditional schema")
    post_type = _nonempty_text(raw["post_type"], "post_type", max_chars=32)
    if post_type not in POST_TYPES:
        raise RNCommunityProviderError("Posting response post_type is not approved")
    return {
        "will_post": True,
        "post_type": post_type,
        "title": _nonempty_text(raw["title"], "title", max_chars=300),
        "content": _nonempty_text(raw["content"], "content", max_chars=8_000),
    }


def _validate_selection_response(
    raw: Mapping[str, Any], *, allowed_ids: set[str], limit: int
) -> dict[str, Any]:
    if set(raw) != {"selected_post_ids"} or not isinstance(raw["selected_post_ids"], list):
        raise RNCommunityProviderError("Selection response must contain only selected_post_ids array")
    ids = raw["selected_post_ids"]
    if any(not isinstance(item, str) or not item for item in ids):
        raise RNCommunityProviderError("Selection response IDs must be non-empty strings")
    if len(ids) != len(set(ids)) or len(ids) > limit or not set(ids) <= allowed_ids:
        raise RNCommunityProviderError("Selection response repeats, exceeds, or invents a post ID")
    return {"selected_post_ids": list(ids)}


def _validate_reaction_response(
    raw: Mapping[str, Any], *, selected_ids: tuple[str, ...]
) -> dict[str, Any]:
    if set(raw) != {"reactions"} or not isinstance(raw["reactions"], list):
        raise RNCommunityProviderError("Reaction response must contain only reactions array")
    rows: list[dict[str, str]] = []
    for item in raw["reactions"]:
        if not isinstance(item, Mapping) or set(item) != {"post_id", "reaction"}:
            raise RNCommunityProviderError("Reaction item has an invalid exact schema")
        post_id = _nonempty_text(item["post_id"], "reaction.post_id", max_chars=160)
        reaction = _nonempty_text(item["reaction"], "reaction.reaction", max_chars=16)
        if reaction not in {"like", "unlike", "none"}:
            raise RNCommunityProviderError("Reaction value is invalid")
        rows.append({"post_id": post_id, "reaction": reaction})
    ids = tuple(row["post_id"] for row in rows)
    if len(ids) != len(set(ids)) or set(ids) != set(selected_ids):
        raise RNCommunityProviderError("Reaction response must cover each selected post exactly once")
    return {"reactions": rows}


def _validate_interpretation_response(
    raw: Mapping[str, Any], *, available_sources: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    required = {"observed_sentiment", "claims", "agreement_disagreement", "uncertainty"}
    if set(raw) != required:
        raise RNCommunityProviderError("Community interpretation has an invalid exact key set")
    sentiment = _nonempty_text(raw["observed_sentiment"], "observed_sentiment", max_chars=16)
    if sentiment not in {
        "optimistic",
        "pessimistic",
        "mixed",
        "overheated",
        "anxious",
        "uncertain",
    }:
        raise RNCommunityProviderError("Community interpretation sentiment is invalid")
    if not isinstance(raw["claims"], list):
        raise RNCommunityProviderError("Community interpretation claims must be an array")
    claims: list[dict[str, Any]] = []
    seen_claims: set[str] = set()
    for item in raw["claims"]:
        if not isinstance(item, Mapping) or set(item) != {
            "claim_text",
            "claim_stance",
            "source_exposure_ids",
            "supporting_quote",
        }:
            raise RNCommunityProviderError("Community interpretation claim schema is invalid")
        text = _nonempty_text(item["claim_text"], "claim_text", max_chars=2_000)
        stance = _nonempty_text(item["claim_stance"], "claim_stance", max_chars=16)
        if stance not in {"bullish", "bearish", "neutral", "uncertain"}:
            raise RNCommunityProviderError("Community interpretation claim stance is invalid")
        sources = item["source_exposure_ids"]
        if (
            not isinstance(sources, list)
            or not sources
            or any(not isinstance(source, str) or not source for source in sources)
            or len(sources) != len(set(sources))
            or not set(sources) <= set(available_sources)
        ):
            raise RNCommunityProviderError("Community interpretation claim source IDs are invalid")
        quote = _nonempty_text(
            item["supporting_quote"], "supporting_quote", max_chars=1_000
        )
        if not any(
            any(quote in text for text in available_sources[source]["allowed_texts"])
            for source in sources
        ):
            raise RNCommunityProviderError(
                "Community interpretation supporting_quote is not an exact substring "
                "of any cited source at its delivered content level"
            )
        claim_identity = scientific_sha256(
            {
                "claim_text": text,
                "claim_stance": stance,
                "source_exposure_ids": sorted(sources),
                "supporting_quote": quote,
            }
        )
        if claim_identity in seen_claims:
            raise RNCommunityProviderError("Community interpretation repeats an identical claim")
        seen_claims.add(claim_identity)
        claims.append(
            {
                "claim_text": text,
                "claim_stance": stance,
                "source_exposure_ids": sorted(sources),
                "supporting_quote": quote,
            }
        )
    return {
        "observed_sentiment": sentiment,
        "claims": claims,
        "agreement_disagreement": _nonempty_text(
            raw["agreement_disagreement"], "agreement_disagreement", max_chars=1_000
        ),
        "uncertainty": _nonempty_text(raw["uncertainty"], "uncertainty", max_chars=1_000),
    }


__all__ = [
    "COMMUNITY_CALL_MAX_TOKENS",
    "COMMUNITY_CALL_SCHEMA_VERSIONS",
    "RNCommunityProviderError",
    "RNJournaledCommunityProvider",
    "build_journaled_community_lifecycle",
]
