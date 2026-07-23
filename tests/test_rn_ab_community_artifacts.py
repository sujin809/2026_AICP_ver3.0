from __future__ import annotations

import hashlib
import json
import sqlite3
import unittest

from tests.test_rn_ab_preflight_bundle import RNPreflightBundleTests
from twinmarket_kr.rn_ab.community import (
    FrozenCommunityPost,
    RNCommunityService,
)
from twinmarket_kr.rn_ab.community_artifacts import (
    RNCommunityArtifactError,
    expected_community_request,
    export_community_mechanism_artifacts,
    validate_community_mechanism_artifacts,
)
from twinmarket_kr.rn_ab.community_provider import POST_TYPES_GUIDE
from twinmarket_kr.rn_ab.journal import LogicalCallKey
from twinmarket_kr.rn_ab.memory import RN_AUXILIARY_STAGE_SCHEMA_VERSIONS
from twinmarket_kr.rn_ab.prompt_registry import (
    COMMUNITY_INTERPRETATION_STAGE,
    COMMUNITY_POSTING_STAGE,
    COMMUNITY_READING_STAGE,
)
from twinmarket_kr.rn_ab.run_context import RNRunContext
from twinmarket_kr.rn_ab.spec import RN_COMM_OFF, RN_COMM_ON, canonical_sha256


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _observation(
    connection: sqlite3.Connection,
    *,
    context: RNRunContext,
    event_id: str,
    turn: int,
    date: str,
    stage: str,
    payload: dict,
    condition_id: str = RN_COMM_ON,
) -> None:
    raw = _canonical(payload)
    connection.execute(
        """
        INSERT INTO observation_events (
            observation_id, run_id, condition_id, event_id, turn, date,
            stage, payload_json, payload_sha256
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "obs:" + canonical_sha256(
                {
                    "run_id": context.run_id,
                    "condition_id": condition_id,
                    "event_id": event_id,
                    "stage": stage,
                }
            )[:40],
            context.run_id,
            condition_id,
            event_id,
            turn,
            date,
            stage,
            raw,
            canonical_sha256(payload),
        ),
    )


def _commit(
    journal: object,
    *,
    context: RNRunContext,
    agent_id: str,
    event_id: str,
    stage: str,
    request: dict,
    response: dict,
    attempt: int,
) -> str:
    logical_id = journal.begin_attempt(
        LogicalCallKey(
            context.run_id,
            RN_COMM_ON,
            agent_id,
            event_id,
            stage,
            RN_AUXILIARY_STAGE_SCHEMA_VERSIONS[stage],
        ),
        request,
        phase_attempt_id=f"fixture-{attempt}",
        attempt_number=1,
    )
    journal.record_success(
        logical_id,
        response,
        phase_attempt_id=f"fixture-{attempt}",
        attempt_number=1,
    )
    journal.mark_committed([logical_id])
    return logical_id


class RNCommunityArtifactTests(RNPreflightBundleTests):
    def _context(self, run_id: str) -> RNRunContext:
        return RNRunContext.load(self._preflight(run_id=run_id).run_dir)

    def _seed_nonempty_mechanism(self, context: RNRunContext) -> None:
        phase_spec = context.resolved.calendar.community_phases[0]
        source = next(
            event
            for event in context.resolved.decision_events
            if event.decision_event_id == phase_spec.after_event_id
        )
        next_am = next(
            event
            for event in context.resolved.decision_events
            if event.global_ordinal is not None
            and source.global_ordinal is not None
            and event.global_ordinal > source.global_ordinal
            and event.subturn.upper() == "AM"
        )
        phase = {
            "phase_id": phase_spec.phase_id,
            "after_event": {
                "event_id": source.decision_event_id,
                "turn": source.global_ordinal,
                "date": source.date,
                "subturn": "pm",
            },
            "observed_at": source.decision_timestamp,
            "next_am_event": {
                "event_id": next_am.decision_event_id,
                "turn": next_am.global_ordinal,
                "date": next_am.date,
                "subturn": "am",
            },
        }
        active = [
            agent_id
            for agent_id in context.agent_ids
            if context.personas.persona(agent_id).news_depth in {1, 2}
        ]
        author, reader = active
        profiles = context.generated_inputs.public_profiles
        body = "공개 게시글의 전체 본문입니다."
        post = FrozenCommunityPost.from_draft(
            {
                "author_agent_id": author,
                "title": "공개 제목",
                "body": body,
                "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                "post_type": "analysis",
                "score": 1,
                "like_count": 1,
            },
            run_id=context.run_id,
            condition_id=RN_COMM_ON,
            phase_id=phase_spec.phase_id,
            allowed_author_ids=frozenset(context.agent_ids),
            active_author_ids=frozenset(active),
        )
        timing_sha = context.resolved.spec.community_timing_policy_sha256
        candidate_title_sha = hashlib.sha256(
            post.title.encode("utf-8")
        ).hexdigest()
        candidate_id = "exp:" + canonical_sha256(
            {
                "run_id": context.run_id,
                "condition_id": RN_COMM_ON,
                "phase_id": phase_spec.phase_id,
                "reader_agent_id": reader,
                "post_id": post.post_id,
                "content_level": "title_only",
                "title_sha256": candidate_title_sha,
                "score": 0,
                "like_count": 0,
            }
        )[:40]
        reader_payload = {
            "schema_version": "rn-community-reader-trace-v1",
            "phase": phase,
            "visible_from_event_id": next_am.decision_event_id,
            "readers": [
                {"reader_agent_id": author, "candidates": []},
                {
                    "reader_agent_id": reader,
                    "candidates": [
                        {
                            "source_exposure_id": candidate_id,
                            "post_id": post.post_id,
                            "content_level": "title_only",
                            "title": post.title,
                            "title_sha256": candidate_title_sha,
                            "post_type": post.post_type,
                            "score": 0,
                            "like_count": 0,
                            "selected": True,
                            "reaction": "like",
                        }
                    ],
                },
            ],
            "community_timing_policy_sha256": timing_sha,
        }
        board_payload = {
            "schema_version": "rn-community-posts-v1",
            "phase": phase,
            "posts": [post.to_dict()],
            "community_timing_policy_sha256": timing_sha,
        }
        schedule_payload = {
            "schema_version": "rn-community-best-schedule-v1",
            "phase": phase,
            "status": "scheduled",
            "best_posts": [post.to_dict()],
            "audience_agent_ids": list(context.agent_ids),
            "community_timing_policy_sha256": timing_sha,
        }
        selected_payload = {
            "schema_version": "rn-community-selected-v1",
            "phase": phase,
            "selections": [
                {
                    "reader_agent_id": reader,
                    "post_id": post.post_id,
                }
            ],
            "community_timing_policy_sha256": timing_sha,
        }
        checkpoint_payload = {
            "schema_version": "rn-community-checkpoint-v1",
            "mode": "on",
            "phase": phase,
            "post_count": 1,
            "selected_exposure_count": 1,
            "best_post_ids": [post.post_id],
            "best_status": "scheduled",
            "community_timing_policy_sha256": timing_sha,
        }

        def exposure_id(agent_id: str, event_id: str, channel: str) -> str:
            return "exp:" + canonical_sha256(
                {
                    "run_id": context.run_id,
                    "condition_id": RN_COMM_ON,
                    "agent_id": agent_id,
                    "event_id": event_id,
                    "channel": channel,
                    "root_id": post.post_id,
                    "body_sha256": post.body_sha256,
                    "status": "delivered",
                }
            )[:40]

        selected_id = exposure_id(
            reader, source.decision_event_id, "community_selected"
        )
        best_ids = {
            agent_id: exposure_id(
                agent_id, next_am.decision_event_id, "community_best"
            )
            for agent_id in context.agent_ids
        }
        delivery_payload = {
            "schema_version": "rn-community-best-delivery-v1",
            "phase": phase,
            "delivered_at": next_am.decision_timestamp,
            "deliveries": [
                {
                    "agent_id": agent_id,
                    "exposure_id": best_ids[agent_id],
                    "post_id": post.post_id,
                    "body_sha256": post.body_sha256,
                    "rank": 1,
                }
                for agent_id in context.agent_ids
            ],
            "community_timing_policy_sha256": timing_sha,
        }

        journal = context.open_journal(RN_COMM_ON)
        posting_ids: dict[str, str] = {}
        posting_contexts: dict[str, dict[str, object]] = {}
        for attempt, agent_id in enumerate(active, start=1):
            ltb_dimensions = {
                f"dim_{dimension}": f"{agent_id} durable view {dimension}"
                for dimension in range(1, 7)
            }
            view_change: list[dict[str, str]] = []
            committed_fill = {
                "action": "buy",
                "requested_quantity": 1,
                "filled_quantity": 1,
                "executed_price": 70000.0,
                "fee_amount": 0.0,
                "pre_portfolio": {"cash": 1000000, "quantity": 0},
                "post_portfolio": {"cash": 930000, "quantity": 1},
            }
            prompt_values = {
                "persona_prompt": context.personas.persona(
                    agent_id
                ).persona_prompt,
                "ltb_dimensions": ltb_dimensions,
                "view_change": view_change,
                "committed_pm_fill": committed_fill,
                "date": source.date,
                "post_types_guide": POST_TYPES_GUIDE,
            }
            posting_contexts[agent_id] = {
                "ltb_dimensions": ltb_dimensions,
                "view_change": view_change,
                "committed_fill": committed_fill,
                "prompt_values": prompt_values,
            }
            response = (
                {
                    "will_post": True,
                    "title": post.title,
                    "content": post.body,
                    "post_type": post.post_type,
                }
                if agent_id == author
                else {"will_post": False}
            )
            posting_ids[agent_id] = _commit(
                journal,
                context=context,
                agent_id=agent_id,
                event_id=source.decision_event_id,
                stage="community_posting",
                request=expected_community_request(
                    context,
                    logical_stage="community_posting",
                    template_stage=COMMUNITY_POSTING_STAGE,
                    agent_id=agent_id,
                    event_id=source.decision_event_id,
                    prompt_values=prompt_values,
                ),
                response=response,
                attempt=attempt,
            )
        posting_records = journal.committed_request_response_records()

        with sqlite3.connect(
            context.condition_db_paths[RN_COMM_ON]
        ) as connection:
            _observation(
                connection,
                context=context,
                event_id=source.decision_event_id,
                turn=int(source.global_ordinal),
                date=source.date,
                stage=f"community_posts:{phase_spec.phase_id}",
                payload=board_payload,
            )
            _observation(
                connection,
                context=context,
                event_id=source.decision_event_id,
                turn=int(source.global_ordinal),
                date=source.date,
                stage=f"community_selected:{phase_spec.phase_id}",
                payload=selected_payload,
            )
            _observation(
                connection,
                context=context,
                event_id=source.decision_event_id,
                turn=int(source.global_ordinal),
                date=source.date,
                stage=f"community_reader_trace:{phase_spec.phase_id}",
                payload=reader_payload,
            )
            _observation(
                connection,
                context=context,
                event_id=source.decision_event_id,
                turn=int(source.global_ordinal),
                date=source.date,
                stage=f"community_best_schedule:{phase_spec.phase_id}",
                payload=schedule_payload,
            )
            _observation(
                connection,
                context=context,
                event_id=source.decision_event_id,
                turn=int(source.global_ordinal),
                date=source.date,
                stage=f"community_checkpoint:{phase_spec.phase_id}",
                payload=checkpoint_payload,
            )
            _observation(
                connection,
                context=context,
                event_id=next_am.decision_event_id,
                turn=int(next_am.global_ordinal),
                date=next_am.date,
                stage=f"community_best_delivery:{phase_spec.phase_id}",
                payload=delivery_payload,
            )

            for agent_id in active:
                is_posted = agent_id == author
                posting_context = posting_contexts[agent_id]
                ltb_dimensions = posting_context["ltb_dimensions"]
                view_change = posting_context["view_change"]
                committed_fill = posting_context["committed_fill"]
                prompt_values = posting_context["prompt_values"]
                ltb_id = f"ltb:{agent_id}"
                fill_id = f"fill:{agent_id}"
                ltb_sha = canonical_sha256(ltb_dimensions)
                view_change_sha = canonical_sha256(view_change)
                connection.execute(
                    """
                    INSERT INTO paper_ltb_states (
                        ltb_id, run_id, condition_id, manifest_sha256, agent_id,
                        event_id, turn, visible_from_turn, date, parent_ltb_id,
                        current_stb_id, dim_1, dim_2, dim_3, dim_4, dim_5, dim_6,
                        scientific_sha256, belief_summary, view_change_json,
                        human_log_renderer_version, human_log_renderer_sha256,
                        human_log_sha256
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        ltb_id,
                        context.run_id,
                        RN_COMM_ON,
                        context.manifest_sha256,
                        agent_id,
                        source.decision_event_id,
                        int(source.global_ordinal),
                        int(source.global_ordinal) + 1,
                        source.date,
                        *(
                            ltb_dimensions[f"dim_{dimension}"]
                            for dimension in range(1, 7)
                        ),
                        ltb_sha,
                        "fixture",
                        _canonical(view_change),
                        "fixture",
                        "6" * 64,
                        "7" * 64,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO paper_fill_ledger (
                        fill_id, run_id, condition_id, manifest_sha256, agent_id,
                        event_id, turn, date, subturn, stock_code, action,
                        requested_quantity, filled_quantity, executed_price,
                        fee_amount, source_ltb_id, source_stb_id, decision_id,
                        pre_portfolio_json, post_portfolio_json,
                        scientific_sha256
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, 'pm', '005930', 'buy',
                        1, 1, 70000.0, 0.0, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        fill_id,
                        context.run_id,
                        RN_COMM_ON,
                        context.manifest_sha256,
                        agent_id,
                        source.decision_event_id,
                        int(source.global_ordinal),
                        source.date,
                        ltb_id,
                        f"stb:{agent_id}",
                        f"decision:{agent_id}",
                        _canonical(committed_fill["pre_portfolio"]),
                        _canonical(committed_fill["post_portfolio"]),
                        "8" * 64,
                    ),
                )
                identity = {
                    "run_id": context.run_id,
                    "condition_id": RN_COMM_ON,
                    "phase_id": phase_spec.phase_id,
                    "author_agent_id": agent_id,
                }
                values = {
                    "trace_id": "post-trace:"
                    + canonical_sha256(identity)[:40],
                    **identity,
                    "manifest_sha256": context.manifest_sha256,
                    "event_id": source.decision_event_id,
                    "turn": int(source.global_ordinal),
                    "date": source.date,
                    "eligibility_status": "eligible",
                    "posting_status": "posted" if is_posted else "skipped",
                    "post_id": post.post_id if is_posted else None,
                    "ltb_id": ltb_id,
                    "ltb_sha256": ltb_sha,
                    "view_change_id": "view-change:"
                    + view_change_sha[:40],
                    "view_change_sha256": view_change_sha,
                    "fill_id": fill_id,
                    "prompt_template_sha256": context.prompt_bundle.support_template(
                        COMMUNITY_POSTING_STAGE
                    ).sha256,
                    "prompt_values_sha256": canonical_sha256(prompt_values),
                    "logical_call_id": posting_ids[agent_id],
                    "accepted_response_sha256": posting_records[
                        posting_ids[agent_id]
                    ]["response_sha256"],
                    "title_sha256": (
                        hashlib.sha256(post.title.encode("utf-8")).hexdigest()
                        if is_posted
                        else None
                    ),
                    "body_sha256": post.body_sha256 if is_posted else None,
                }
                values["trace_sha256"] = canonical_sha256(values)
                connection.execute(
                    f"INSERT INTO community_post_trace ({','.join(values)}) "
                    f"VALUES ({','.join('?' for _ in values)})",
                    tuple(values.values()),
                )

            selected_metadata = {
                "schema_version": "rn-community-exposure-v1",
                "source_phase_id": phase_spec.phase_id,
                "visible_from_event_id": next_am.decision_event_id,
                "content_level": "full_body",
                "post_id": post.post_id,
                "ledger_author_agent_id": author,
                "title": post.title,
                "full_body": post.body,
                "body_sha256": post.body_sha256,
                "content_version": post.content_version,
                "content_version_sha256": post.content_version_sha256,
                "post_type": post.post_type,
                "score": post.score,
                "like_count": post.like_count,
                "rank": None,
                "public_author_profile": profiles[author],
            }
            connection.execute(
                """
                INSERT INTO agent_exposures (
                    exposure_id, run_id, condition_id, agent_id, event_id,
                    channel, root_id, body_sha256, delivered_at, status,
                    metadata_json
                ) VALUES (?, ?, ?, ?, ?, 'community_selected', ?, ?, ?, 'delivered', ?)
                """,
                (
                    selected_id,
                    context.run_id,
                    RN_COMM_ON,
                    reader,
                    source.decision_event_id,
                    post.post_id,
                    post.body_sha256,
                    source.decision_timestamp,
                    _canonical(selected_metadata),
                ),
            )
            for agent_id in context.agent_ids:
                best_metadata = {
                    **selected_metadata,
                    "public_author_profile": None,
                    "rank": 1,
                }
                connection.execute(
                    """
                    INSERT INTO agent_exposures (
                        exposure_id, run_id, condition_id, agent_id, event_id,
                        channel, root_id, body_sha256, delivered_at, status,
                        metadata_json
                    ) VALUES (?, ?, ?, ?, ?, 'community_best', ?, ?, ?, 'delivered', ?)
                    """,
                    (
                        best_ids[agent_id],
                        context.run_id,
                        RN_COMM_ON,
                        agent_id,
                        next_am.decision_event_id,
                        post.post_id,
                        post.body_sha256,
                        next_am.decision_timestamp,
                        _canonical(best_metadata),
                    ),
                )
                _observation(
                    connection,
                    context=context,
                    event_id=next_am.decision_event_id,
                    turn=int(next_am.global_ordinal),
                    date=next_am.date,
                    stage=f"community_claims:{agent_id}",
                    payload={
                        "schema_version": "rn-community-claims-v1",
                        "agent_id": agent_id,
                        "event_id": next_am.decision_event_id,
                        "claims": [],
                    },
                )
            connection.commit()

        select_values = {
            "persona_prompt": context.personas.persona(reader).persona_prompt,
            "mode": "select",
            "post_list_str": [
                {
                    "post_id": post.post_id,
                    "title": post.title,
                    "post_type": post.post_type,
                    "score": 0,
                    "public_author_profile": profiles[author],
                }
            ],
            "read_limit": context.resolved.spec.community_policy[
                "depth2_selective_read_cap"
            ],
            "posts_content_str": [],
        }
        _commit(
            journal,
            context=context,
            agent_id=reader,
            event_id=source.decision_event_id,
            stage="community_read_select",
            request=expected_community_request(
                context,
                logical_stage="community_read_select",
                template_stage=COMMUNITY_READING_STAGE,
                agent_id=reader,
                event_id=source.decision_event_id,
                prompt_values=select_values,
            ),
            response={"selected_post_ids": [post.post_id]},
            attempt=1,
        )
        react_values = {
            "persona_prompt": context.personas.persona(reader).persona_prompt,
            "mode": "react",
            "post_list_str": [],
            "read_limit": 1,
            "posts_content_str": [
                {
                    "post_id": post.post_id,
                    "title": post.title,
                    "body": post.body,
                    "body_sha256": post.body_sha256,
                    "post_type": post.post_type,
                    "public_author_profile": profiles[author],
                    "untrusted_content_kind": "community_post",
                    "content_level": "full_body",
                }
            ],
        }
        _commit(
            journal,
            context=context,
            agent_id=reader,
            event_id=source.decision_event_id,
            stage="community_read_react",
            request=expected_community_request(
                context,
                logical_stage="community_read_react",
                template_stage=COMMUNITY_READING_STAGE,
                agent_id=reader,
                event_id=source.decision_event_id,
                prompt_values=react_values,
            ),
            response={
                "reactions": [
                    {"post_id": post.post_id, "reaction": "like"}
                ]
            },
            attempt=2,
        )
        service = RNCommunityService.from_resolved_manifest(
            context.open_store(RN_COMM_ON),
            context.resolved,
            public_profiles=context.generated_inputs.public_profiles,
        )
        for index, agent_id in enumerate(context.agent_ids, start=3):
            exposures = service.interpretation_payloads(
                agent_id=agent_id,
                event_id=next_am.decision_event_id,
            )
            interpretation_values = {
                "persona_prompt": context.personas.persona(
                    agent_id
                ).persona_prompt,
                "candidate_posts_summary": [
                    dict(item)
                    for item in exposures
                    if item.get("exposure_channel")
                    == "title_only_candidate"
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
            }
            _commit(
                journal,
                context=context,
                agent_id=agent_id,
                event_id=next_am.decision_event_id,
                stage="community_interpretation",
                request=expected_community_request(
                    context,
                    logical_stage="community_interpretation",
                    template_stage=COMMUNITY_INTERPRETATION_STAGE,
                    agent_id=agent_id,
                    event_id=next_am.decision_event_id,
                    prompt_values=interpretation_values,
                ),
                response={
                    "observed_sentiment": "mixed",
                    "claims": [],
                    "agreement_disagreement": "혼재",
                    "uncertainty": "불확실",
                },
                attempt=index,
            )

        for phase_index, empty_spec in enumerate(
            context.resolved.calendar.community_phases[1:],
            start=1,
        ):
            empty_source = next(
                event
                for event in context.resolved.decision_events
                if event.decision_event_id == empty_spec.after_event_id
            )
            empty_next = next(
                (
                    event
                    for event in context.resolved.decision_events
                    if event.global_ordinal is not None
                    and empty_source.global_ordinal is not None
                    and event.global_ordinal > empty_source.global_ordinal
                    and event.subturn.upper() == "AM"
                ),
                None,
            )
            empty_phase = {
                "phase_id": empty_spec.phase_id,
                "after_event": {
                    "event_id": empty_source.decision_event_id,
                    "turn": empty_source.global_ordinal,
                    "date": empty_source.date,
                    "subturn": "pm",
                },
                "observed_at": empty_source.decision_timestamp,
                "next_am_event": (
                    None
                    if empty_next is None
                    else {
                        "event_id": empty_next.decision_event_id,
                        "turn": empty_next.global_ordinal,
                        "date": empty_next.date,
                        "subturn": "am",
                    }
                ),
            }
            traces: list[dict[str, object]] = []
            with sqlite3.connect(
                context.condition_db_paths[RN_COMM_ON]
            ) as connection:
                for agent_offset, agent_id in enumerate(active, start=1):
                    suffix = canonical_sha256(
                        {
                            "phase_id": empty_spec.phase_id,
                            "agent_id": agent_id,
                        }
                    )[:16]
                    ltb_id = f"ltb:{suffix}"
                    fill_id = f"fill:{suffix}"
                    dimensions = {
                        f"dim_{dimension}": (
                            f"{agent_id} empty durable view {dimension}"
                        )
                        for dimension in range(1, 7)
                    }
                    ltb_sha = canonical_sha256(dimensions)
                    view_change: list[dict[str, str]] = []
                    view_sha = canonical_sha256(view_change)
                    committed_fill = {
                        "action": "buy",
                        "requested_quantity": 1,
                        "filled_quantity": 1,
                        "executed_price": 70000.0,
                        "fee_amount": 0.0,
                        "pre_portfolio": {
                            "cash": 1000000,
                            "quantity": 0,
                        },
                        "post_portfolio": {
                            "cash": 930000,
                            "quantity": 1,
                        },
                    }
                    prompt_values = {
                        "persona_prompt": context.personas.persona(
                            agent_id
                        ).persona_prompt,
                        "ltb_dimensions": dimensions,
                        "view_change": view_change,
                        "committed_pm_fill": committed_fill,
                        "date": empty_source.date,
                        "post_types_guide": POST_TYPES_GUIDE,
                    }
                    logical_id = _commit(
                        journal,
                        context=context,
                        agent_id=agent_id,
                        event_id=empty_source.decision_event_id,
                        stage="community_posting",
                        request=expected_community_request(
                            context,
                            logical_stage="community_posting",
                            template_stage=COMMUNITY_POSTING_STAGE,
                            agent_id=agent_id,
                            event_id=empty_source.decision_event_id,
                            prompt_values=prompt_values,
                        ),
                        response={"will_post": False},
                        attempt=100 + phase_index * 10 + agent_offset,
                    )
                    connection.execute(
                        """
                        INSERT INTO paper_ltb_states (
                            ltb_id, run_id, condition_id, manifest_sha256,
                            agent_id, event_id, turn, visible_from_turn, date,
                            parent_ltb_id, current_stb_id, dim_1, dim_2, dim_3,
                            dim_4, dim_5, dim_6, scientific_sha256,
                            belief_summary, view_change_json,
                            human_log_renderer_version,
                            human_log_renderer_sha256, human_log_sha256
                        ) VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL,
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                        )
                        """,
                        (
                            ltb_id,
                            context.run_id,
                            RN_COMM_ON,
                            context.manifest_sha256,
                            agent_id,
                            empty_source.decision_event_id,
                            int(empty_source.global_ordinal),
                            int(empty_source.global_ordinal) + 1,
                            empty_source.date,
                            *(
                                dimensions[f"dim_{dimension}"]
                                for dimension in range(1, 7)
                            ),
                            ltb_sha,
                            "fixture",
                            _canonical(view_change),
                            "fixture",
                            "6" * 64,
                            "7" * 64,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO paper_fill_ledger (
                            fill_id, run_id, condition_id, manifest_sha256,
                            agent_id, event_id, turn, date, subturn, stock_code,
                            action, requested_quantity, filled_quantity,
                            executed_price, fee_amount, source_ltb_id,
                            source_stb_id, decision_id, pre_portfolio_json,
                            post_portfolio_json, scientific_sha256
                        ) VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, 'pm', '005930', 'buy',
                            1, 1, 70000.0, 0.0, ?, ?, ?, ?, ?, ?
                        )
                        """,
                        (
                            fill_id,
                            context.run_id,
                            RN_COMM_ON,
                            context.manifest_sha256,
                            agent_id,
                            empty_source.decision_event_id,
                            int(empty_source.global_ordinal),
                            empty_source.date,
                            ltb_id,
                            f"stb:{suffix}",
                            f"decision:{suffix}",
                            _canonical(committed_fill["pre_portfolio"]),
                            _canonical(committed_fill["post_portfolio"]),
                            "8" * 64,
                        ),
                    )
                    traces.append(
                        {
                            "phase_id": empty_spec.phase_id,
                            "event_id": empty_source.decision_event_id,
                            "turn": int(empty_source.global_ordinal),
                            "date": empty_source.date,
                            "author_agent_id": agent_id,
                            "eligibility_status": "eligible",
                            "posting_status": "skipped",
                            "post_id": None,
                            "ltb_id": ltb_id,
                            "ltb_sha256": ltb_sha,
                            "view_change_id": "view-change:"
                            + view_sha[:40],
                            "view_change_sha256": view_sha,
                            "fill_id": fill_id,
                            "prompt_template_sha256": (
                                context.prompt_bundle.support_template(
                                    COMMUNITY_POSTING_STAGE
                                ).sha256
                            ),
                            "prompt_values_sha256": canonical_sha256(
                                prompt_values
                            ),
                            "logical_call_id": logical_id,
                            "accepted_response_sha256": canonical_sha256(
                                {"will_post": False}
                            ),
                            "title_sha256": None,
                            "body_sha256": None,
                        }
                    )
                connection.commit()
            with sqlite3.connect(
                context.condition_db_paths[RN_COMM_ON]
            ) as connection:
                for trace in traces:
                    identity = {
                        "run_id": context.run_id,
                        "condition_id": RN_COMM_ON,
                        "phase_id": trace["phase_id"],
                        "author_agent_id": trace["author_agent_id"],
                    }
                    values = {
                        "trace_id": "post-trace:"
                        + canonical_sha256(identity)[:40],
                        **identity,
                        "manifest_sha256": context.manifest_sha256,
                        **{
                            key: value
                            for key, value in trace.items()
                            if key
                            not in {"phase_id", "author_agent_id"}
                        },
                    }
                    values["trace_sha256"] = canonical_sha256(values)
                    connection.execute(
                        f"INSERT INTO community_post_trace ({','.join(values)}) "
                        f"VALUES ({','.join('?' for _ in values)})",
                        tuple(values.values()),
                    )
                base = {
                    "phase": empty_phase,
                    "community_timing_policy_sha256": timing_sha,
                }
                for stage, payload in (
                    (
                        f"community_posts:{empty_spec.phase_id}",
                        {
                            "schema_version": "rn-community-posts-v1",
                            **base,
                            "posts": [],
                        },
                    ),
                    (
                        f"community_selected:{empty_spec.phase_id}",
                        {
                            "schema_version": "rn-community-selected-v1",
                            **base,
                            "selections": [],
                        },
                    ),
                    (
                        f"community_reader_trace:{empty_spec.phase_id}",
                        {
                            "schema_version": "rn-community-reader-trace-v1",
                            **base,
                            "visible_from_event_id": (
                                None
                                if empty_next is None
                                else empty_next.decision_event_id
                            ),
                            "readers": [
                                {
                                    "reader_agent_id": agent_id,
                                    "candidates": [],
                                }
                                for agent_id in active
                            ],
                        },
                    ),
                    (
                        f"community_best_schedule:{empty_spec.phase_id}",
                        {
                            "schema_version": "rn-community-best-schedule-v1",
                            **base,
                            "status": "empty",
                            "best_posts": [],
                            "audience_agent_ids": list(context.agent_ids),
                        },
                    ),
                    (
                        f"community_checkpoint:{empty_spec.phase_id}",
                        {
                            "schema_version": "rn-community-checkpoint-v1",
                            **base,
                            "mode": "on",
                            "post_count": 0,
                            "selected_exposure_count": 0,
                            "best_post_ids": [],
                            "best_status": "empty",
                        },
                    ),
                ):
                    _observation(
                        connection,
                        context=context,
                        event_id=empty_source.decision_event_id,
                        turn=int(empty_source.global_ordinal),
                        date=empty_source.date,
                        stage=stage,
                        payload=payload,
                    )
                if empty_next is not None:
                    _observation(
                        connection,
                        context=context,
                        event_id=empty_next.decision_event_id,
                        turn=int(empty_next.global_ordinal),
                        date=empty_next.date,
                        stage=f"community_best_delivery:{empty_spec.phase_id}",
                        payload={
                            "schema_version": "rn-community-best-delivery-v1",
                            **base,
                            "delivered_at": empty_next.decision_timestamp,
                            "deliveries": [],
                        },
                    )
                connection.commit()

        off_service = RNCommunityService.from_resolved_manifest(
            context.open_store(RN_COMM_OFF),
            context.resolved,
            public_profiles=context.generated_inputs.public_profiles,
        )
        for registered in context.resolved.calendar.community_phases:
            registered_source = next(
                event
                for event in context.resolved.decision_events
                if event.decision_event_id == registered.after_event_id
            )
            registered_next = next(
                (
                    event
                    for event in context.resolved.decision_events
                    if event.global_ordinal is not None
                    and registered_source.global_ordinal is not None
                    and event.global_ordinal > registered_source.global_ordinal
                    and event.subturn.upper() == "AM"
                ),
                None,
            )
            off_service.run_pm_phase(
                {
                    "phase_id": registered.phase_id,
                    "after_event": {
                        "event_id": registered_source.decision_event_id,
                        "turn": registered_source.global_ordinal,
                        "date": registered_source.date,
                        "subturn": "pm",
                    },
                    "observed_at": registered_source.decision_timestamp,
                    "next_am_event": (
                        None
                        if registered_next is None
                        else {
                            "event_id": registered_next.decision_event_id,
                            "turn": registered_next.global_ordinal,
                            "date": registered_next.date,
                            "subturn": "am",
                        }
                    ),
                }
            )

    def test_nonempty_mechanism_exports_are_replayable_and_tamper_evident(
        self,
    ) -> None:
        context = self._context("rn-community-artifacts")
        self._seed_nonempty_mechanism(context)
        journals = {
            RN_COMM_OFF: context.open_journal(RN_COMM_OFF),
            RN_COMM_ON: context.open_journal(RN_COMM_ON),
        }
        metadata = export_community_mechanism_artifacts(
            context,
            journals=journals,
        )
        self.assertEqual(metadata["community_interactions"]["row_count"], 1)
        self.assertEqual(metadata["community_best_posts"]["row_count"], 1)
        self.assertEqual(metadata["community_exposure_trace"]["row_count"], 5)
        self.assertEqual(
            validate_community_mechanism_artifacts(
                context,
                journals=journals,
                metadata=metadata,
            ),
            metadata,
        )
        trace_path = context.run_dir / "traces" / "community_exposure_trace.jsonl"
        trace_path.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(
            RNCommunityArtifactError,
            "differs from canonical DB state",
        ):
            validate_community_mechanism_artifacts(
                context,
                journals=journals,
                metadata=metadata,
            )

    def test_phase_topology_full_body_and_auxiliary_sinks_fail_closed(
        self,
    ) -> None:
        for index, mutation in enumerate(
            (
                "missing_delivery",
                "foreign_full_body",
                "missing_post_trace",
                "trace_hash",
                "extra_aux",
            )
        ):
            with self.subTest(mutation=mutation):
                context = self._context(f"rn-community-p0-{index}")
                self._seed_nonempty_mechanism(context)
                journals = {
                    RN_COMM_OFF: context.open_journal(RN_COMM_OFF),
                    RN_COMM_ON: context.open_journal(RN_COMM_ON),
                }
                if mutation == "missing_delivery":
                    with sqlite3.connect(
                        context.condition_db_paths[RN_COMM_ON]
                    ) as connection:
                        connection.execute(
                            """
                            DELETE FROM observation_events
                            WHERE stage LIKE 'community_best_delivery:%'
                            """
                        )
                        connection.commit()
                elif mutation == "foreign_full_body":
                    with sqlite3.connect(
                        context.condition_db_paths[RN_COMM_ON]
                    ) as connection:
                        connection.row_factory = sqlite3.Row
                        row = connection.execute(
                            """
                            SELECT * FROM agent_exposures
                            WHERE channel = 'community_best'
                            ORDER BY exposure_id
                            LIMIT 1
                            """
                        ).fetchone()
                        metadata = json.loads(row["metadata_json"])
                        body = "스케줄에 없는 외부 본문"
                        body_sha = hashlib.sha256(
                            body.encode("utf-8")
                        ).hexdigest()
                        title = "외부 제목"
                        metadata.update(
                            {
                                "title": title,
                                "full_body": body,
                                "body_sha256": body_sha,
                            }
                        )
                        metadata["content_version_sha256"] = canonical_sha256(
                            {
                                "content_version": metadata[
                                    "content_version"
                                ],
                                "title": title,
                                "body": body,
                                "body_sha256": body_sha,
                                "post_type": metadata["post_type"],
                            }
                        )
                        post_id = "post:" + canonical_sha256(
                            {
                                "run_id": context.run_id,
                                "condition_id": RN_COMM_ON,
                                "phase_id": metadata["source_phase_id"],
                                "author_agent_id": metadata[
                                    "ledger_author_agent_id"
                                ],
                                "title": title,
                                "body_sha256": body_sha,
                                "content_version_sha256": metadata[
                                    "content_version_sha256"
                                ],
                                "post_type": metadata["post_type"],
                            }
                        )[:40]
                        metadata["post_id"] = post_id
                        exposure_id = "exp:" + canonical_sha256(
                            {
                                "run_id": context.run_id,
                                "condition_id": RN_COMM_ON,
                                "agent_id": row["agent_id"],
                                "event_id": row["event_id"],
                                "channel": row["channel"],
                                "root_id": post_id,
                                "body_sha256": body_sha,
                                "status": row["status"],
                            }
                        )[:40]
                        connection.execute(
                            """
                            UPDATE agent_exposures
                            SET exposure_id = ?, root_id = ?, body_sha256 = ?,
                                metadata_json = ?
                            WHERE exposure_id = ?
                            """,
                            (
                                exposure_id,
                                post_id,
                                body_sha,
                                _canonical(metadata),
                                row["exposure_id"],
                            ),
                        )
                        connection.commit()
                elif mutation == "missing_post_trace":
                    with sqlite3.connect(
                        context.condition_db_paths[RN_COMM_ON]
                    ) as connection:
                        connection.execute(
                            """
                            DELETE FROM community_post_trace
                            WHERE trace_id = (
                                SELECT trace_id FROM community_post_trace
                                ORDER BY trace_id LIMIT 1
                            )
                            """
                        )
                        connection.commit()
                elif mutation == "trace_hash":
                    with sqlite3.connect(
                        context.condition_db_paths[RN_COMM_ON]
                    ) as connection:
                        connection.execute(
                            """
                            UPDATE community_post_trace
                            SET trace_sha256 = ?
                            WHERE trace_id = (
                                SELECT trace_id FROM community_post_trace
                                ORDER BY trace_id LIMIT 1
                            )
                            """,
                            ("f" * 64,),
                        )
                        connection.commit()
                else:
                    phase = context.resolved.calendar.community_phases[0]
                    source = next(
                        event
                        for event in context.resolved.decision_events
                        if event.decision_event_id == phase.after_event_id
                    )
                    passive = next(
                        agent_id
                        for agent_id in context.agent_ids
                        if context.personas.persona(agent_id).news_depth == 0
                    )
                    _commit(
                        journals[RN_COMM_ON],
                        context=context,
                        agent_id=passive,
                        event_id=source.decision_event_id,
                        stage="community_read_select",
                        request={"self_consistent_but_extraneous": True},
                        response={"selected_post_ids": []},
                        attempt=99,
                    )
                with self.assertRaises(RNCommunityArtifactError):
                    export_community_mechanism_artifacts(
                        context,
                        journals=journals,
                    )

    def test_interpretation_request_is_bound_to_actual_reader_visibility(
        self,
    ) -> None:
        context = self._context("rn-community-interpretation-request")
        self._seed_nonempty_mechanism(context)
        journal_path = context.journal_paths[RN_COMM_ON]
        with sqlite3.connect(journal_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT logical_call_id, request_json
                FROM logical_responses
                WHERE logical_call_id LIKE '%|community_interpretation|%'
                ORDER BY logical_call_id
                LIMIT 1
                """
            ).fetchone()
            request = json.loads(row["request_json"])
            request["prompt_values"]["best_posts_summary"] = []
            request_json = _canonical(request)
            connection.execute(
                """
                UPDATE logical_responses
                SET request_json = ?, request_sha256 = ?
                WHERE logical_call_id = ?
                """,
                (
                    request_json,
                    canonical_sha256(request),
                    row["logical_call_id"],
                ),
            )
            connection.commit()
        with self.assertRaisesRegex(
            RNCommunityArtifactError,
            "request differs from actual reader visibility",
        ):
            export_community_mechanism_artifacts(
                context,
                journals={
                    RN_COMM_OFF: context.open_journal(RN_COMM_OFF),
                    RN_COMM_ON: context.open_journal(RN_COMM_ON),
                },
            )

    def test_posting_response_is_bound_to_private_trace_and_public_board(
        self,
    ) -> None:
        context = self._context("rn-community-posting-binding")
        self._seed_nonempty_mechanism(context)
        with sqlite3.connect(
            context.condition_db_paths[RN_COMM_ON]
        ) as connection:
            connection.row_factory = sqlite3.Row
            trace = connection.execute(
                """
                SELECT * FROM community_post_trace
                WHERE posting_status = 'posted'
                LIMIT 1
                """
            ).fetchone()
            false_response = {"will_post": False}
            false_sha = canonical_sha256(false_response)
            trace_values = {
                key: trace[key]
                for key in trace.keys()
                if key not in {"trace_sha256", "created_at"}
            }
            trace_values["accepted_response_sha256"] = false_sha
            connection.execute(
                """
                UPDATE community_post_trace
                SET accepted_response_sha256 = ?, trace_sha256 = ?
                WHERE trace_id = ?
                """,
                (
                    false_sha,
                    canonical_sha256(trace_values),
                    trace["trace_id"],
                ),
            )
            connection.commit()
        with sqlite3.connect(
            context.journal_paths[RN_COMM_ON]
        ) as connection:
            encoded = _canonical(false_response)
            connection.execute(
                """
                UPDATE logical_responses
                SET response_json = ?, response_sha256 = ?
                WHERE logical_call_id = ?
                """,
                (encoded, false_sha, trace["logical_call_id"]),
            )
            connection.execute(
                """
                UPDATE physical_attempts
                SET response_sha256 = ?
                WHERE logical_call_id = ? AND status = 'success'
                """,
                (false_sha, trace["logical_call_id"]),
            )
            connection.commit()
        with self.assertRaisesRegex(
            RNCommunityArtifactError,
            "will_post=false.*public post",
        ):
            export_community_mechanism_artifacts(
                context,
                journals={
                    RN_COMM_OFF: context.open_journal(RN_COMM_OFF),
                    RN_COMM_ON: context.open_journal(RN_COMM_ON),
                },
            )

    def test_runtime_response_normalization_matches_artifact_validation(
        self,
    ) -> None:
        context = self._context("rn-community-normalization")
        self._seed_nonempty_mechanism(context)
        journal_path = context.journal_paths[RN_COMM_ON]
        with sqlite3.connect(journal_path) as connection:
            connection.row_factory = sqlite3.Row
            reaction = connection.execute(
                """
                SELECT logical_call_id, response_json
                FROM logical_responses
                WHERE logical_call_id LIKE '%|community_read_react|%'
                LIMIT 1
                """
            ).fetchone()
            reaction_response = json.loads(reaction["response_json"])
            reaction_response["reactions"][0]["post_id"] = (
                " " + reaction_response["reactions"][0]["post_id"] + " "
            )
            reaction_response["reactions"][0]["reaction"] = " like "
            interpretation = connection.execute(
                """
                SELECT logical_call_id, response_json
                FROM logical_responses
                WHERE logical_call_id LIKE '%|community_interpretation|%'
                ORDER BY logical_call_id
                LIMIT 1
                """
            ).fetchone()
            interpretation_response = json.loads(
                interpretation["response_json"]
            )
            interpretation_response.update(
                {
                    "observed_sentiment": " mixed ",
                    "agreement_disagreement": " 혼재 ",
                    "uncertainty": " 불확실 ",
                }
            )
            for row, response in (
                (reaction, reaction_response),
                (interpretation, interpretation_response),
            ):
                encoded = _canonical(response)
                digest = canonical_sha256(response)
                connection.execute(
                    """
                    UPDATE logical_responses
                    SET response_json = ?, response_sha256 = ?
                    WHERE logical_call_id = ?
                    """,
                    (encoded, digest, row["logical_call_id"]),
                )
                connection.execute(
                    """
                    UPDATE physical_attempts
                    SET response_sha256 = ?
                    WHERE logical_call_id = ? AND status = 'success'
                    """,
                    (digest, row["logical_call_id"]),
                )
            connection.commit()
        metadata = export_community_mechanism_artifacts(
            context,
            journals={
                RN_COMM_OFF: context.open_journal(RN_COMM_OFF),
                RN_COMM_ON: context.open_journal(RN_COMM_ON),
            },
        )
        self.assertEqual(metadata["community_interactions"]["row_count"], 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
