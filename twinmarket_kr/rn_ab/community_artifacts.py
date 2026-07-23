"""Deterministic reviewer artifacts for the RN community mechanism.

The community board is public, but reader-level exposure, reaction, and claim
lineage is private run evidence.  This module keeps that boundary explicit:

* ``community_best_posts.csv`` contains only frozen public posts.
* ``community_interactions.csv`` contains the title-only candidate snapshot
  and the reader's selection/reaction, never a persona or portfolio.
* ``traces/community_exposure_trace.jsonl`` contains hashes and causal IDs for
  title/full-body delivery, claims, and STB evidence edges; it deliberately
  omits full bodies, free-form private beliefs, and truth verdicts.

Every byte is rebuilt from canonical SQLite observations and committed journal
records.  No model or network call occurs here.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from twinmarket_kr.db.connection import connect
from twinmarket_kr.experiment_runtime import file_sha256
from twinmarket_kr.rn_ab.journal import ResponseJournal
from twinmarket_kr.rn_ab.run_context import RNRunContext
from twinmarket_kr.rn_ab.spec import RN_COMM_OFF, RN_COMM_ON, RN_CONDITIONS, canonical_sha256


class RNCommunityArtifactError(RuntimeError):
    """Canonical community mechanism evidence is incomplete or inconsistent."""


INTERACTIONS_FILENAME = "community_interactions.csv"
BEST_POSTS_FILENAME = "community_best_posts.csv"
EXPOSURE_TRACE_FILENAME = "community_exposure_trace.jsonl"

INTERACTION_COLUMNS = (
    "schema_version",
    "run_id",
    "condition_id",
    "manifest_sha256",
    "phase_id",
    "event_id",
    "turn",
    "date",
    "visible_from_event_id",
    "reader_agent_id",
    "source_exposure_id",
    "post_id",
    "title",
    "title_sha256",
    "post_type",
    "score_snapshot",
    "like_count_snapshot",
    "selected",
    "reaction",
)

BEST_POST_COLUMNS = (
    "schema_version",
    "run_id",
    "condition_id",
    "manifest_sha256",
    "phase_id",
    "source_event_id",
    "source_turn",
    "source_date",
    "visible_from_event_id",
    "schedule_status",
    "rank",
    "post_id",
    "author_agent_id",
    "title",
    "body",
    "body_sha256",
    "content_version",
    "content_version_sha256",
    "post_type",
    "score",
    "like_count",
    "audience_count",
)

_EXPOSURE_FORMAT = "rn_private_community_exposure_trace_jsonl_v1"
_INTERACTION_FORMAT = "rn_community_interactions_csv_v1"
_BEST_FORMAT = "rn_community_best_posts_csv_v1"


def export_community_mechanism_artifacts(
    context: RNRunContext,
    *,
    journals: Mapping[str, ResponseJournal],
) -> dict[str, dict[str, Any]]:
    """Write all three mechanism artifacts from canonical run-local state."""

    rows = _canonical_rows(context, journals=journals)
    interaction_path = context.run_dir / INTERACTIONS_FILENAME
    best_path = context.run_dir / BEST_POSTS_FILENAME
    trace_path = context.run_dir / "traces" / EXPOSURE_TRACE_FILENAME
    trace_path.parent.mkdir(parents=True, exist_ok=True)

    _write_immutable(interaction_path, _csv_bytes(INTERACTION_COLUMNS, rows["interactions"]))
    _write_immutable(best_path, _csv_bytes(BEST_POST_COLUMNS, rows["best_posts"]))
    _write_immutable(trace_path, _jsonl_bytes(rows["exposures"]))
    return _metadata(
        context,
        interaction_path=interaction_path,
        best_path=best_path,
        trace_path=trace_path,
        rows=rows,
    )


def validate_community_mechanism_artifacts(
    context: RNRunContext,
    *,
    journals: Mapping[str, ResponseJournal],
    metadata: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Rebuild canonical bytes and compare every indexed file and hash."""

    if set(metadata) != {
        "community_interactions",
        "community_best_posts",
        "community_exposure_trace",
    }:
        raise RNCommunityArtifactError(
            "Community mechanism artifact index has an invalid exact key set"
        )
    rows = _canonical_rows(context, journals=journals)
    paths = {
        "community_interactions": context.run_dir / INTERACTIONS_FILENAME,
        "community_best_posts": context.run_dir / BEST_POSTS_FILENAME,
        "community_exposure_trace": context.run_dir
        / "traces"
        / EXPOSURE_TRACE_FILENAME,
    }
    expected_bytes = {
        "community_interactions": _csv_bytes(
            INTERACTION_COLUMNS, rows["interactions"]
        ),
        "community_best_posts": _csv_bytes(BEST_POST_COLUMNS, rows["best_posts"]),
        "community_exposure_trace": _jsonl_bytes(rows["exposures"]),
    }
    for name, path in paths.items():
        if path.is_symlink() or not path.is_file():
            raise RNCommunityArtifactError(
                f"Community mechanism artifact is missing or not regular: {name}"
            )
        if path.read_bytes() != expected_bytes[name]:
            raise RNCommunityArtifactError(
                f"Community mechanism artifact differs from canonical DB state: {name}"
            )
    rebuilt = _metadata(
        context,
        interaction_path=paths["community_interactions"],
        best_path=paths["community_best_posts"],
        trace_path=paths["community_exposure_trace"],
        rows=rows,
    )
    if {key: dict(value) for key, value in metadata.items()} != rebuilt:
        raise RNCommunityArtifactError(
            "Community mechanism artifact metadata differs from canonical state"
        )
    return rebuilt


def expected_community_request(
    context: RNRunContext,
    *,
    logical_stage: str,
    template_stage: str,
    agent_id: str,
    event_id: str,
    prompt_values: Mapping[str, Any],
) -> dict[str, Any]:
    """Reconstruct one exact strict community request from sealed inputs."""

    from twinmarket_kr.rn_ab.call_policy import (
        RN_STRICT_RESPONSE_FORMAT,
        RN_STRICT_TEMPERATURE,
    )
    from twinmarket_kr.rn_ab.community_provider import COMMUNITY_CALL_MAX_TOKENS
    from twinmarket_kr.rn_ab.execution import strict_policy_from_context
    from twinmarket_kr.rn_ab.memory import RN_AUXILIARY_STAGE_SCHEMA_VERSIONS
    from twinmarket_kr.rn_ab.stage_adapter import (
        RN_TRUSTED_SYSTEM_INSTRUCTION_SHA256,
        RN_TRUSTED_SYSTEM_INSTRUCTION_VERSION,
    )

    try:
        schema_version = RN_AUXILIARY_STAGE_SCHEMA_VERSIONS[logical_stage]
        max_tokens = COMMUNITY_CALL_MAX_TOKENS[logical_stage]
    except KeyError as exc:
        raise RNCommunityArtifactError(
            f"Unknown community logical stage: {logical_stage}"
        ) from exc
    template = context.prompt_bundle.support_template(template_stage)
    policy = strict_policy_from_context(context)
    material = "|".join(
        (
            str(context.resolved.spec.study_seed),
            context.resolved.spec.seed_namespace,
            logical_stage,
            agent_id,
            event_id,
        )
    ).encode("utf-8")
    seed = int.from_bytes(hashlib.sha256(material).digest()[:4], "big")
    seed &= 0x7FFFFFFF
    return {
        "contract_version": "rn-community-stage-request-v1",
        "prompt_template_stage": template_stage,
        "prompt_template_sha256": template.sha256,
        "prompt_values": dict(prompt_values),
        "response_schema_version": schema_version,
        "trusted_system_instruction": {
            "version": RN_TRUSTED_SYSTEM_INSTRUCTION_VERSION,
            "sha256": RN_TRUSTED_SYSTEM_INSTRUCTION_SHA256,
        },
        "model": policy.model,
        "provider": policy.provider,
        "reasoning": {"effort": "none", "exclude": True},
        "provider_policy": policy.as_request_policy(),
        "temperature": RN_STRICT_TEMPERATURE,
        "response_format": dict(RN_STRICT_RESPONSE_FORMAT),
        "seed": seed,
        "max_tokens": max_tokens,
    }


def _canonical_rows(
    context: RNRunContext,
    *,
    journals: Mapping[str, ResponseJournal],
) -> dict[str, tuple[dict[str, Any], ...]]:
    if set(journals) != set(RN_CONDITIONS):
        raise RNCommunityArtifactError(
            "Community artifact validation requires both response journals"
        )
    interactions: list[dict[str, Any]] = []
    best_posts: list[dict[str, Any]] = []
    exposures: list[dict[str, Any]] = []
    for condition_id in RN_CONDITIONS:
        try:
            journal_records = journals[
                condition_id
            ].committed_request_response_records()
        except Exception as exc:
            raise RNCommunityArtifactError(
                f"{condition_id} journal cannot support community artifacts: {exc}"
            ) from exc
        with connect(
            context.condition_db_paths[condition_id], read_only=True
        ) as connection:
            if condition_id == RN_COMM_OFF:
                _assert_off_containment(
                    context,
                    connection,
                    run_id=context.run_id,
                    journal_records=journal_records,
                )
                continue
            arm = _on_rows(
                context,
                connection=connection,
                journal_records=journal_records,
            )
            interactions.extend(arm["interactions"])
            best_posts.extend(arm["best_posts"])
            exposures.extend(arm["exposures"])
    return {
        "interactions": tuple(
            sorted(
                interactions,
                key=lambda row: (
                    row["phase_id"],
                    row["reader_agent_id"],
                    row["post_id"],
                ),
            )
        ),
        "best_posts": tuple(
            sorted(
                best_posts,
                key=lambda row: (row["phase_id"], int(row["rank"])),
            )
        ),
        "exposures": tuple(
            sorted(
                exposures,
                key=lambda row: (
                    row["condition_id"],
                    row["reader_agent_id"],
                    row["visible_from_event_id"] or "",
                    row["channel"],
                    row["exposure_id"],
                ),
            )
        ),
    }


def _assert_off_containment(
    context: RNRunContext,
    connection: Any,
    *,
    run_id: str,
    journal_records: Mapping[str, Mapping[str, Any]],
) -> None:
    table_count = connection.execute(
        """
        SELECT COUNT(*) AS count
        FROM agent_exposures
        WHERE run_id = ? AND channel IN ('community_best', 'community_selected')
        """,
        (run_id,),
    ).fetchone()
    observation_rows = connection.execute(
        """
        SELECT condition_id, event_id, turn, date, stage, payload_json, payload_sha256
        FROM observation_events
        WHERE run_id = ? AND stage LIKE 'community_%'
        ORDER BY event_id, stage
        """,
        (run_id,),
    ).fetchall()
    journal_stages = {
        _logical_components(logical_call_id)[4]
        for logical_call_id in journal_records
        if _logical_components(logical_call_id)[4].startswith("community_")
    }
    if int(table_count["count"]) or journal_stages:
        raise RNCommunityArtifactError(
            "RN_COMM_OFF contains reader/Best/exposure/claim mechanism state"
        )
    phases = _canonical_phase_contexts(context)
    expected_stages = {
        f"community_checkpoint:{phase_id}" for phase_id in phases
    }
    if {str(row["stage"]) for row in observation_rows} != expected_stages:
        raise RNCommunityArtifactError(
            "RN_COMM_OFF must contain exactly one no-op checkpoint per phase"
        )
    timing_sha = context.resolved.spec.community_timing_policy_sha256
    for row in observation_rows:
        stage = str(row["stage"])
        phase_id = stage.removeprefix("community_checkpoint:")
        phase = phases[phase_id]
        payload = _strict_observation_payload(row)
        expected = {
            "schema_version": "rn-community-checkpoint-v1",
            "mode": "off",
            "phase": phase,
            "post_count": 0,
            "selected_exposure_count": 0,
            "best_post_ids": [],
            "best_status": "no_op",
            "community_timing_policy_sha256": timing_sha,
        }
        source = phase["after_event"]
        if (
            str(row["condition_id"]) != RN_COMM_OFF
            or payload != expected
            or str(row["event_id"]) != source["event_id"]
            or int(row["turn"]) != source["turn"]
            or str(row["date"]) != source["date"]
        ):
            raise RNCommunityArtifactError(
                "RN_COMM_OFF no-op checkpoint differs from its sealed phase"
            )


def _canonical_phase_contexts(
    context: RNRunContext,
) -> dict[str, dict[str, Any]]:
    events = tuple(context.resolved.decision_events)
    by_id = {event.decision_event_id: event for event in events}
    phases: dict[str, dict[str, Any]] = {}
    for registered in context.resolved.calendar.community_phases:
        try:
            source = by_id[registered.after_event_id]
        except KeyError as exc:  # Resolver should already make this impossible.
            raise RNCommunityArtifactError(
                "Resolved community phase cites an unknown PM event"
            ) from exc
        if source.subturn.upper() != "PM" or source.global_ordinal is None:
            raise RNCommunityArtifactError(
                "Resolved community phase source is not an ordered PM event"
            )
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
        phase = {
            "phase_id": registered.phase_id,
            "after_event": {
                "event_id": source.decision_event_id,
                "turn": source.global_ordinal,
                "date": source.date,
                "subturn": "pm",
            },
            "observed_at": source.decision_timestamp,
            "next_am_event": (
                None
                if next_am is None
                else {
                    "event_id": next_am.decision_event_id,
                    "turn": next_am.global_ordinal,
                    "date": next_am.date,
                    "subturn": "am",
                }
            ),
        }
        if registered.phase_id in phases:
            raise RNCommunityArtifactError(
                "Resolved calendar repeats a community phase ID"
            )
        phases[registered.phase_id] = phase
    if not phases:
        raise RNCommunityArtifactError(
            "RN finalization has no resolved community phase"
        )
    return phases


def _event_coordinates(context: RNRunContext, event_id: str) -> tuple[int, str]:
    match = next(
        (
            event
            for event in context.resolved.decision_events
            if event.decision_event_id == event_id
        ),
        None,
    )
    if match is None or match.global_ordinal is None:
        raise RNCommunityArtifactError(
            "Community artifact cites an unknown or unordered decision event"
        )
    return int(match.global_ordinal), str(match.date)


def _strict_observation_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    stage = str(row["stage"])
    payload = _strict_json(
        row["payload_json"],
        label=f"community observation {stage}",
        expected_type=dict,
    )
    if canonical_sha256(payload) != str(row["payload_sha256"]):
        raise RNCommunityArtifactError(
            f"Community observation hash mismatch: {stage}"
        )
    return payload


def _validate_on_observation_topology(
    context: RNRunContext,
    connection: Any,
) -> dict[str, Any]:
    """Require the exact ON phase-state machine observation topology."""

    phases = _canonical_phase_contexts(context)
    rows = connection.execute(
        """
        SELECT condition_id, event_id, turn, date, stage, payload_json, payload_sha256
        FROM observation_events
        WHERE run_id = ? AND stage LIKE 'community_%'
        ORDER BY event_id, stage
        """,
        (context.run_id,),
    ).fetchall()
    phase_prefixes = {
        "community_posts:": "posts",
        "community_selected:": "selected",
        "community_reader_trace:": "reader_trace",
        "community_best_schedule:": "best_schedule",
        "community_best_delivery:": "best_delivery",
        "community_checkpoint:": "checkpoint",
    }
    mapped: dict[str, dict[str, dict[str, Any]]] = {
        name: {} for name in phase_prefixes.values()
    }
    claims: list[dict[str, Any]] = []
    seen_event_stage: set[tuple[str, str]] = set()
    timing_sha = context.resolved.spec.community_timing_policy_sha256
    next_events = {
        str(phase["next_am_event"]["event_id"])
        for phase in phases.values()
        if phase["next_am_event"] is not None
    }
    for row in rows:
        if str(row["condition_id"]) != RN_COMM_ON:
            raise RNCommunityArtifactError(
                "RN_COMM_ON database contains cross-condition community state"
            )
        stage = str(row["stage"])
        event_id = str(row["event_id"])
        identity = (event_id, stage)
        if identity in seen_event_stage:
            raise RNCommunityArtifactError(
                "Community observation repeats an event/stage identity"
            )
        seen_event_stage.add(identity)
        payload = _strict_observation_payload(row)
        if stage.startswith("community_claims:"):
            agent_id = stage.removeprefix("community_claims:")
            if agent_id not in context.agent_ids or event_id not in next_events:
                raise RNCommunityArtifactError(
                    "Community claim observation has a foreign agent/event"
                )
            claims.append(
                {
                    "event_id": event_id,
                    "turn": int(row["turn"]),
                    "date": str(row["date"]),
                    "stage": stage,
                    "payload": payload,
                }
            )
            continue
        match = next(
            (
                (prefix, name)
                for prefix, name in phase_prefixes.items()
                if stage.startswith(prefix)
            ),
            None,
        )
        if match is None:
            raise RNCommunityArtifactError(
                f"Unknown RN community observation stage: {stage}"
            )
        prefix, name = match
        phase_id = stage.removeprefix(prefix)
        if phase_id not in phases or phase_id in mapped[name]:
            raise RNCommunityArtifactError(
                "Community observation phase is unknown or duplicated"
            )
        phase = phases[phase_id]
        expected_event = (
            phase["next_am_event"]
            if name == "best_delivery"
            else phase["after_event"]
        )
        if expected_event is None:
            raise RNCommunityArtifactError(
                "Right-censored community phase has a delivery observation"
            )
        if (
            payload.get("phase") != phase
            or payload.get("community_timing_policy_sha256") != timing_sha
            or event_id != expected_event["event_id"]
            or int(row["turn"]) != expected_event["turn"]
            or str(row["date"]) != expected_event["date"]
        ):
            raise RNCommunityArtifactError(
                "Community observation is not bound to its sealed phase/event/timing policy"
            )
        mapped[name][phase_id] = payload

    expected_phase_ids = set(phases)
    for name in ("posts", "selected", "reader_trace", "best_schedule", "checkpoint"):
        if set(mapped[name]) != expected_phase_ids:
            raise RNCommunityArtifactError(
                f"RN_COMM_ON lacks exact {name} coverage for every phase"
            )
    expected_delivery = {
        phase_id
        for phase_id, phase in phases.items()
        if phase["next_am_event"] is not None
    }
    if set(mapped["best_delivery"]) != expected_delivery:
        raise RNCommunityArtifactError(
            "RN_COMM_ON lacks exact Best delivery/right-censor phase coverage"
        )
    return {**mapped, "phases": phases, "claims": tuple(claims)}


def _on_rows(
    context: RNRunContext,
    *,
    connection: Any,
    journal_records: Mapping[str, Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    topology = _validate_on_observation_topology(context, connection)
    board_observations = {
        f"community_posts:{phase_id}": payload
        for phase_id, payload in topology["posts"].items()
    }
    boards: dict[str, dict[str, Any]] = {}
    phases: dict[str, Mapping[str, Any]] = {}
    for stage, payload in board_observations.items():
        phase_id = stage.removeprefix("community_posts:")
        if (
            set(payload)
            != {
                "schema_version",
                "phase",
                "posts",
                "community_timing_policy_sha256",
            }
            or payload.get("schema_version") != "rn-community-posts-v1"
            or not isinstance(payload.get("phase"), Mapping)
            or payload["phase"].get("phase_id") != phase_id
            or not isinstance(payload.get("posts"), list)
        ):
            raise RNCommunityArtifactError("Community public board is malformed")
        by_id: dict[str, Mapping[str, Any]] = {}
        authors: set[str] = set()
        for raw_post in payload["posts"]:
            post = _validated_post(
                context,
                phase_id=phase_id,
                raw=raw_post,
            )
            if (
                post["post_id"] in by_id
                or post["author_agent_id"] in authors
            ):
                raise RNCommunityArtifactError(
                    "Community board repeats a post ID or author"
                )
            by_id[post["post_id"]] = post
            authors.add(str(post["author_agent_id"]))
        boards[phase_id] = by_id
        phases[phase_id] = payload["phase"]

    _validate_posting_lineage(
        context,
        connection=connection,
        journal_records=journal_records,
        boards=boards,
    )

    reader_observations = {
        f"community_reader_trace:{phase_id}": payload
        for phase_id, payload in topology["reader_trace"].items()
    }
    interactions: list[dict[str, Any]] = []
    candidates_by_exposure: dict[str, dict[str, Any]] = {}
    selected_by_reader_post: dict[tuple[str, str], dict[str, Any]] = {}
    reader_rows_by_phase: dict[str, tuple[dict[str, Any], ...]] = {}
    for stage, payload in reader_observations.items():
        phase_id = stage.removeprefix("community_reader_trace:")
        rows = _reader_trace_rows(
            context,
            phase_id=phase_id,
            payload=payload,
            board=boards.get(phase_id, {}),
            journal_records=journal_records,
        )
        reader_rows_by_phase[phase_id] = tuple(rows)
        for row in rows:
            interactions.append(
                {key: row[key] for key in INTERACTION_COLUMNS}
            )
            if row["source_exposure_id"] in candidates_by_exposure:
                raise RNCommunityArtifactError(
                    "Title-only candidate exposure ID is duplicated"
                )
            candidates_by_exposure[row["source_exposure_id"]] = row
            if row["selected"] == "true":
                selected_by_reader_post[
                    (row["reader_agent_id"], row["post_id"])
                ] = row
        _verify_reaction_score_semantics(
            phase_id=phase_id,
            board=boards.get(phase_id, {}),
            interaction_rows=rows,
        )

    expected_interpretations = _validate_exact_phase_bindings(
        context,
        connection=connection,
        topology=topology,
        boards=boards,
        reader_rows_by_phase=reader_rows_by_phase,
    )
    best_posts = _best_rows(
        context,
        connection=connection,
        boards=boards,
        schedules=topology["best_schedule"],
    )
    claims_by_exposure, claim_rows = _claim_lineage(
        context,
        connection=connection,
        candidates_by_exposure=candidates_by_exposure,
        journal_records=journal_records,
        observations=topology["claims"],
    )
    _validate_auxiliary_journal_sinks(
        context,
        connection=connection,
        journal_records=journal_records,
        reader_rows_by_phase=reader_rows_by_phase,
        expected_interpretations=expected_interpretations,
    )
    edges_by_claim = _community_edges(
        connection,
        run_id=context.run_id,
        condition_id=RN_COMM_ON,
    )

    exposures: list[dict[str, Any]] = []
    for exposure_id, candidate in candidates_by_exposure.items():
        claims = claims_by_exposure.get(exposure_id, ())
        exposures.append(
            _exposure_row(
                context,
                exposure_id=exposure_id,
                reader_agent_id=candidate["reader_agent_id"],
                source_phase_id=candidate["phase_id"],
                source_event_id=candidate["event_id"],
                visible_from_event_id=candidate["visible_from_event_id"],
                root_post_id=candidate["post_id"],
                channel="title_only_candidate",
                content_level="title_only",
                status="delivered",
                delivered_at=phases.get(candidate["phase_id"], {}).get(
                    "observed_at"
                ),
                title_sha256=candidate["title_sha256"],
                body_sha256=None,
                rank=None,
                score=int(candidate["score_snapshot"]),
                like_count=int(candidate["like_count_snapshot"]),
                selected=candidate["selected"] == "true",
                reaction=candidate["reaction"] or None,
                claims=claims,
                edges_by_claim=edges_by_claim,
                prompt_call_ids=_prompt_call_ids(
                    journal_records,
                    agent_id=candidate["reader_agent_id"],
                    source_event_id=candidate["event_id"],
                    visible_event_id=candidate["visible_from_event_id"],
                    include_select=True,
                    include_react=candidate["selected"] == "true",
                ),
            )
        )

    full_body_rows = connection.execute(
        """
        SELECT *
        FROM agent_exposures
        WHERE run_id = ? AND condition_id = ?
          AND channel IN ('community_best', 'community_selected')
        ORDER BY exposure_id
        """,
        (context.run_id, RN_COMM_ON),
    ).fetchall()
    seen_full_exposure_ids: set[str] = set()
    for stored in full_body_rows:
        exposure_id = str(stored["exposure_id"])
        if exposure_id in seen_full_exposure_ids or exposure_id in candidates_by_exposure:
            raise RNCommunityArtifactError(
                "Community full-body exposure identity is duplicated"
            )
        seen_full_exposure_ids.add(exposure_id)
        metadata = _strict_json(
            stored["metadata_json"],
            label=f"community exposure {exposure_id}",
            expected_type=dict,
        )
        _validate_full_body_metadata(stored, metadata)
        channel = str(stored["channel"])
        reader = str(stored["agent_id"])
        post_id = str(stored["root_id"])
        expected_exposure_id = "exp:" + canonical_sha256(
            {
                "run_id": context.run_id,
                "condition_id": RN_COMM_ON,
                "agent_id": reader,
                "event_id": str(stored["event_id"]),
                "channel": channel,
                "root_id": post_id,
                "body_sha256": str(stored["body_sha256"]),
                "status": str(stored["status"]),
            }
        )[:40]
        if exposure_id != expected_exposure_id:
            raise RNCommunityArtifactError(
                "Community full-body exposure ID is non-canonical"
            )
        selected = selected_by_reader_post.get((reader, post_id))
        if channel == "community_selected" and selected is None:
            raise RNCommunityArtifactError(
                "Selected full-body exposure has no sealed reader selection"
            )
        if channel == "community_best" and selected is not None:
            rendered_channel = "best_body_overlap_with_selected"
        elif channel == "community_best":
            rendered_channel = "best_only_body"
        else:
            rendered_channel = "selected_body"
        claims = claims_by_exposure.get(exposure_id, ())
        exposures.append(
            _exposure_row(
                context,
                exposure_id=exposure_id,
                reader_agent_id=reader,
                source_phase_id=str(metadata["source_phase_id"]),
                source_event_id=(
                    str(stored["event_id"])
                    if channel == "community_selected"
                    else str(phases.get(str(metadata["source_phase_id"]), {}).get(
                        "after_event", {}
                    ).get("event_id", ""))
                ),
                visible_from_event_id=metadata["visible_from_event_id"],
                root_post_id=post_id,
                channel=rendered_channel,
                content_level="full_body",
                status=str(stored["status"]),
                delivered_at=stored["delivered_at"],
                title_sha256=hashlib.sha256(
                    str(metadata["title"]).encode("utf-8")
                ).hexdigest(),
                body_sha256=str(stored["body_sha256"]),
                rank=metadata["rank"],
                score=int(metadata["score"]),
                like_count=int(metadata["like_count"]),
                selected=selected is not None,
                reaction=None if selected is None else selected["reaction"] or None,
                claims=claims,
                edges_by_claim=edges_by_claim,
                prompt_call_ids=_prompt_call_ids(
                    journal_records,
                    agent_id=reader,
                    source_event_id=(
                        str(stored["event_id"])
                        if channel == "community_selected"
                        else str(phases.get(str(metadata["source_phase_id"]), {}).get(
                            "after_event", {}
                        ).get("event_id", ""))
                    ),
                    visible_event_id=metadata["visible_from_event_id"],
                    include_select=False,
                    include_react=channel == "community_selected",
                ),
            )
        )

    known_exposures = set(candidates_by_exposure) | seen_full_exposure_ids
    if set(claims_by_exposure) - known_exposures:
        raise RNCommunityArtifactError(
            "Community claim cites an exposure absent from canonical artifacts"
        )
    # ``claim_rows`` is evaluated for its strict journal/quote validation.  It
    # is not exported as a truth verdict or a second free-form memory.
    del claim_rows
    return {
        "interactions": interactions,
        "best_posts": best_posts,
        "exposures": exposures,
    }


def _validate_posting_lineage(
    context: RNRunContext,
    *,
    connection: Any,
    journal_records: Mapping[str, Mapping[str, Any]],
    boards: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> None:
    """Bind posting request/response, private trace, and frozen public post."""

    from twinmarket_kr.rn_ab.community_provider import (
        POST_TYPES_GUIDE,
        _validate_posting_response,
    )
    from twinmarket_kr.rn_ab.prompt_registry import COMMUNITY_POSTING_STAGE

    rows = connection.execute(
        """
        SELECT *
        FROM community_post_trace
        WHERE run_id = ? AND condition_id = ?
        ORDER BY phase_id, author_agent_id
        """,
        (context.run_id, RN_COMM_ON),
    ).fetchall()
    trace_by_call = {str(row["logical_call_id"]): row for row in rows}
    if len(trace_by_call) != len(rows):
        raise RNCommunityArtifactError(
            "Community posting traces repeat a logical call"
        )
    posting_records = {
        logical_call_id: record
        for logical_call_id, record in journal_records.items()
        if _logical_components(logical_call_id)[4] == "community_posting"
    }
    if set(trace_by_call) != set(posting_records):
        raise RNCommunityArtifactError(
            "Community posting journal and private trace sets differ"
        )

    posted_authors: dict[str, set[str]] = {}
    phases = _canonical_phase_contexts(context)
    for logical_call_id, row in trace_by_call.items():
        record = posting_records[logical_call_id]
        components = _logical_components(logical_call_id)
        agent_id = components[2]
        event_id = components[3]
        phase_id = str(row["phase_id"])
        phase = phases.get(phase_id)
        if (
            str(row["run_id"]) != context.run_id
            or str(row["condition_id"]) != RN_COMM_ON
            or str(row["manifest_sha256"]) != context.manifest_sha256
            or str(row["author_agent_id"]) != agent_id
            or str(row["event_id"]) != event_id
            or str(row["eligibility_status"]) != "eligible"
            or phase is None
            or phase_id not in boards
            or event_id != phase["after_event"]["event_id"]
            or int(row["turn"]) != phase["after_event"]["turn"]
            or str(row["date"]) != phase["after_event"]["date"]
        ):
            raise RNCommunityArtifactError(
                "Community posting trace is cross-scoped or malformed"
            )
        trace_columns = (
            "trace_id",
            "run_id",
            "condition_id",
            "manifest_sha256",
            "phase_id",
            "event_id",
            "turn",
            "date",
            "author_agent_id",
            "eligibility_status",
            "posting_status",
            "post_id",
            "ltb_id",
            "ltb_sha256",
            "view_change_id",
            "view_change_sha256",
            "fill_id",
            "prompt_template_sha256",
            "prompt_values_sha256",
            "logical_call_id",
            "accepted_response_sha256",
            "title_sha256",
            "body_sha256",
        )
        trace_values = {column: row[column] for column in trace_columns}
        trace_identity = {
            key: trace_values[key]
            for key in (
                "run_id",
                "condition_id",
                "phase_id",
                "author_agent_id",
            )
        }
        if (
            str(row["trace_id"])
            != "post-trace:" + canonical_sha256(trace_identity)[:40]
            or str(row["trace_sha256"]) != canonical_sha256(trace_values)
        ):
            raise RNCommunityArtifactError(
                "Community posting trace identity/hash is non-canonical"
            )
        ltb = connection.execute(
            """
            SELECT run_id, condition_id, agent_id, event_id, turn, date,
                   dim_1, dim_2, dim_3, dim_4, dim_5, dim_6,
                   scientific_sha256, view_change_json
            FROM paper_ltb_states
            WHERE ltb_id = ?
            """,
            (row["ltb_id"],),
        ).fetchone()
        fill = connection.execute(
            """
            SELECT run_id, condition_id, agent_id, event_id, turn, date, subturn,
                   action, requested_quantity, filled_quantity, executed_price,
                   fee_amount, pre_portfolio_json, post_portfolio_json
            FROM paper_fill_ledger
            WHERE fill_id = ?
            """,
            (row["fill_id"],),
        ).fetchone()
        if ltb is None or fill is None:
            raise RNCommunityArtifactError(
                "Community posting trace cannot resolve its LTB/fill"
            )
        for source in (ltb, fill):
            if (
                str(source["run_id"]) != context.run_id
                or str(source["condition_id"]) != RN_COMM_ON
                or str(source["agent_id"]) != agent_id
                or str(source["event_id"]) != event_id
                or int(source["turn"]) != int(row["turn"])
                or str(source["date"]) != str(row["date"])
            ):
                raise RNCommunityArtifactError(
                    "Community posting LTB/fill is cross-scoped"
                )
        if (
            str(fill["subturn"]) != "pm"
            or str(ltb["scientific_sha256"]) != str(row["ltb_sha256"])
        ):
            raise RNCommunityArtifactError(
                "Community posting LTB/fill type or hash differs"
            )
        view_change = _strict_json(
            ltb["view_change_json"],
            label="community posting LTB view_change_json",
            expected_type=list,
        )
        view_change_sha = canonical_sha256(view_change)
        if (
            str(row["view_change_sha256"]) != view_change_sha
            or str(row["view_change_id"])
            != "view-change:" + view_change_sha[:40]
        ):
            raise RNCommunityArtifactError(
                "Community posting view-change binding differs"
            )
        pre_portfolio = _strict_json(
            fill["pre_portfolio_json"],
            label="community posting pre_portfolio_json",
            expected_type=dict,
        )
        post_portfolio = _strict_json(
            fill["post_portfolio_json"],
            label="community posting post_portfolio_json",
            expected_type=dict,
        )
        prompt_values = {
            "persona_prompt": context.personas.persona(agent_id).persona_prompt,
            "ltb_dimensions": {
                f"dim_{index}": str(ltb[f"dim_{index}"])
                for index in range(1, 7)
            },
            "view_change": view_change,
            "committed_pm_fill": {
                "action": str(fill["action"]),
                "requested_quantity": int(fill["requested_quantity"]),
                "filled_quantity": int(fill["filled_quantity"]),
                "executed_price": float(fill["executed_price"]),
                "fee_amount": float(fill["fee_amount"]),
                "pre_portfolio": pre_portfolio,
                "post_portfolio": post_portfolio,
            },
            "date": str(row["date"]),
            "post_types_guide": POST_TYPES_GUIDE,
        }
        expected_request = expected_community_request(
            context,
            logical_stage="community_posting",
            template_stage=COMMUNITY_POSTING_STAGE,
            agent_id=agent_id,
            event_id=event_id,
            prompt_values=prompt_values,
        )
        if (
            record.get("request") != expected_request
            or str(row["prompt_template_sha256"])
            != expected_request["prompt_template_sha256"]
            or str(row["prompt_values_sha256"])
            != canonical_sha256(prompt_values)
        ):
            raise RNCommunityArtifactError(
                "Community posting request differs from sealed LTB/fill/persona inputs"
            )
        response = record.get("response")
        if (
            not isinstance(response, Mapping)
            or record.get("response_sha256") != canonical_sha256(response)
            or str(row["accepted_response_sha256"])
            != record.get("response_sha256")
        ):
            raise RNCommunityArtifactError(
                "Community posting response differs from its accepted trace"
            )
        try:
            normalized = _validate_posting_response(response)
        except Exception as exc:
            raise RNCommunityArtifactError(
                f"Community posting response violates runtime semantics: {exc}"
            ) from exc
        by_author = {
            str(post["author_agent_id"]): post
            for post in boards[phase_id].values()
        }
        public_post = by_author.get(agent_id)
        if normalized["will_post"] is False:
            if (
                str(row["posting_status"]) != "skipped"
                or public_post is not None
                or any(
                    row[column] is not None
                    for column in ("post_id", "title_sha256", "body_sha256")
                )
            ):
                raise RNCommunityArtifactError(
                    "Accepted will_post=false response has a public post"
                )
            continue
        if str(row["posting_status"]) != "posted" or public_post is None:
            raise RNCommunityArtifactError(
                "Accepted will_post=true response lacks its frozen public post"
            )
        title = normalized["title"]
        body = normalized["content"]
        if (
            public_post["title"] != title
            or public_post["body"] != body
            or public_post["post_type"] != normalized["post_type"]
            or str(row["post_id"]) != public_post["post_id"]
            or str(row["title_sha256"])
            != hashlib.sha256(title.encode("utf-8")).hexdigest()
            or str(row["body_sha256"])
            != hashlib.sha256(body.encode("utf-8")).hexdigest()
        ):
            raise RNCommunityArtifactError(
                "Accepted posting response differs from trace/public content"
            )
        posted_authors.setdefault(phase_id, set()).add(agent_id)

    for phase_id, board in boards.items():
        board_authors = {
            str(post["author_agent_id"]) for post in board.values()
        }
        if board_authors != posted_authors.get(phase_id, set()):
            raise RNCommunityArtifactError(
                "Community board authors differ from will_post=true responses"
            )


def _reader_trace_rows(
    context: RNRunContext,
    *,
    phase_id: str,
    payload: Mapping[str, Any],
    board: Mapping[str, Mapping[str, Any]],
    journal_records: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    expected_payload_keys = {
        "schema_version",
        "phase",
        "visible_from_event_id",
        "readers",
        "community_timing_policy_sha256",
    }
    if (
        set(payload) != expected_payload_keys
        or payload.get("schema_version") != "rn-community-reader-trace-v1"
        or not isinstance(payload.get("phase"), Mapping)
        or payload["phase"].get("phase_id") != phase_id
        or not isinstance(payload.get("readers"), list)
    ):
        raise RNCommunityArtifactError("Private community reader trace is malformed")
    phase = payload["phase"]
    event = phase.get("after_event")
    if not isinstance(event, Mapping):
        raise RNCommunityArtifactError("Private reader trace lacks its PM event")
    active_agents = {
        agent_id
        for agent_id in context.agent_ids
        if context.personas.persona(agent_id).news_depth in {1, 2}
    }
    observed_agents: set[str] = set()
    output: list[dict[str, Any]] = []
    for reader_raw in payload["readers"]:
        if not isinstance(reader_raw, Mapping) or set(reader_raw) != {
            "reader_agent_id",
            "candidates",
        }:
            raise RNCommunityArtifactError("Private reader row is malformed")
        reader = reader_raw["reader_agent_id"]
        if (
            not isinstance(reader, str)
            or reader not in active_agents
            or reader in observed_agents
            or not isinstance(reader_raw["candidates"], list)
        ):
            raise RNCommunityArtifactError(
                "Private reader identity/candidate array is invalid"
            )
        observed_agents.add(reader)
        expected_post_ids = {
            post_id
            for post_id, post in board.items()
            if post["author_agent_id"] != reader
        }
        seen_post_ids: set[str] = set()
        reader_rows: list[dict[str, Any]] = []
        for candidate in reader_raw["candidates"]:
            if not isinstance(candidate, Mapping) or set(candidate) != {
                "source_exposure_id",
                "post_id",
                "content_level",
                "title",
                "title_sha256",
                "post_type",
                "score",
                "like_count",
                "selected",
                "reaction",
            }:
                raise RNCommunityArtifactError(
                    "Private title-only candidate has an invalid schema"
                )
            post_id = candidate["post_id"]
            if post_id not in expected_post_ids or post_id in seen_post_ids:
                raise RNCommunityArtifactError(
                    "Private title-only candidate is foreign/self-authored/duplicate"
                )
            seen_post_ids.add(str(post_id))
            post = board[str(post_id)]
            title = candidate["title"]
            title_sha = hashlib.sha256(str(title).encode("utf-8")).hexdigest()
            if (
                candidate["content_level"] != "title_only"
                or title != post["title"]
                or candidate["post_type"] != post["post_type"]
                or candidate["title_sha256"] != title_sha
                or not isinstance(candidate["selected"], bool)
                or (
                    candidate["selected"]
                    and candidate["reaction"] not in {"like", "unlike", "none"}
                )
                or (not candidate["selected"] and candidate["reaction"] is not None)
            ):
                raise RNCommunityArtifactError(
                    "Private candidate content/selection/reaction is invalid"
                )
            expected_exposure_id = "exp:" + canonical_sha256(
                {
                    "run_id": context.run_id,
                    "condition_id": RN_COMM_ON,
                    "phase_id": phase_id,
                    "reader_agent_id": reader,
                    "post_id": post_id,
                    "content_level": "title_only",
                    "title_sha256": title_sha,
                    "score": int(candidate["score"]),
                    "like_count": int(candidate["like_count"]),
                }
            )[:40]
            if candidate["source_exposure_id"] != expected_exposure_id:
                raise RNCommunityArtifactError(
                    "Private candidate exposure ID is non-canonical"
                )
            reader_rows.append(
                {
                    "schema_version": "rn-community-interaction-v1",
                    "run_id": context.run_id,
                    "condition_id": RN_COMM_ON,
                    "manifest_sha256": context.manifest_sha256,
                    "phase_id": phase_id,
                    "event_id": str(event["event_id"]),
                    "turn": int(event["turn"]),
                    "date": str(event["date"]),
                    "visible_from_event_id": payload["visible_from_event_id"] or "",
                    "reader_agent_id": reader,
                    "source_exposure_id": expected_exposure_id,
                    "post_id": str(post_id),
                    "title": str(title),
                    "title_sha256": title_sha,
                    "post_type": str(candidate["post_type"]),
                    "score_snapshot": int(candidate["score"]),
                    "like_count_snapshot": int(candidate["like_count"]),
                    "selected": "true" if candidate["selected"] else "false",
                    "reaction": candidate["reaction"] or "",
                }
            )
        if seen_post_ids != expected_post_ids:
            raise RNCommunityArtifactError(
                "Private reader trace omits a candidate post"
            )
        _verify_reader_journal(
            context,
            reader_id=reader,
            event_id=str(event["event_id"]),
            rows=reader_rows,
            board=board,
            journal_records=journal_records,
        )
        output.extend(reader_rows)
    if observed_agents != active_agents:
        raise RNCommunityArtifactError(
            "Private reader trace does not cover the complete active cohort"
        )
    return output


def _verify_reader_journal(
    context: RNRunContext,
    *,
    reader_id: str,
    event_id: str,
    rows: Sequence[Mapping[str, Any]],
    board: Mapping[str, Mapping[str, Any]],
    journal_records: Mapping[str, Mapping[str, Any]],
) -> None:
    from twinmarket_kr.rn_ab.community_provider import (
        _validate_reaction_response,
        _validate_selection_response,
    )
    from twinmarket_kr.rn_ab.prompt_registry import COMMUNITY_READING_STAGE

    by_stage = _journal_stage_records(
        journal_records,
        run_id=context.run_id,
        condition_id=RN_COMM_ON,
        agent_id=reader_id,
        event_id=event_id,
    )
    if not rows:
        if {"community_read_select", "community_read_react"} & set(by_stage):
            raise RNCommunityArtifactError(
                "Reader with no candidates has a select/react journal response"
            )
        return
    select_record = by_stage.get("community_read_select")
    if select_record is None:
        raise RNCommunityArtifactError(
            "Candidate reader trace has no committed selection response"
        )
    depth = context.personas.persona(reader_id).news_depth
    cap = int(
        context.resolved.spec.community_policy[
            f"depth{depth}_selective_read_cap"
        ]
    )
    profiles = (
        {}
        if context.generated_inputs is None
        else context.generated_inputs.public_profiles
    )
    ordered_rows = sorted(rows, key=lambda row: row["post_id"])
    select_values = {
        "persona_prompt": context.personas.persona(reader_id).persona_prompt,
        "mode": "select",
        "post_list_str": [
            {
                "post_id": row["post_id"],
                "title": row["title"],
                "post_type": row["post_type"],
                "score": int(row["score_snapshot"]),
                "public_author_profile": profiles[
                    str(board[row["post_id"]]["author_agent_id"])
                ],
            }
            for row in ordered_rows
        ],
        "read_limit": cap,
        "posts_content_str": [],
    }
    expected_select_request = expected_community_request(
        context,
        logical_stage="community_read_select",
        template_stage=COMMUNITY_READING_STAGE,
        agent_id=reader_id,
        event_id=event_id,
        prompt_values=select_values,
    )
    selected_response = select_record.get("response")
    if not isinstance(selected_response, Mapping):
        raise RNCommunityArtifactError(
            "Selection journal response is not an object"
        )
    try:
        normalized_selection = _validate_selection_response(
            selected_response,
            allowed_ids={str(row["post_id"]) for row in rows},
            limit=cap,
        )
    except Exception as exc:
        raise RNCommunityArtifactError(
            f"Selection journal response violates runtime semantics: {exc}"
        ) from exc
    selected_ids = normalized_selection["selected_post_ids"]
    selected_set = {
        row["post_id"] for row in rows if row["selected"] == "true"
    }
    if (
        select_record.get("request") != expected_select_request
        or set(selected_ids) != selected_set
    ):
        raise RNCommunityArtifactError(
            "Selection journal output does not reproduce the sealed reader trace"
        )

    react_record = by_stage.get("community_read_react")
    if not selected_ids:
        if react_record is not None:
            raise RNCommunityArtifactError(
                "Empty selection unexpectedly has a reaction journal response"
            )
        return
    if react_record is None:
        raise RNCommunityArtifactError(
            "Selected full-body reads have no committed reaction response"
        )
    selected_rows = {row["post_id"]: row for row in rows if row["selected"] == "true"}
    react_values = {
        "persona_prompt": context.personas.persona(reader_id).persona_prompt,
        "mode": "react",
        "post_list_str": [],
        "read_limit": len(selected_ids),
        "posts_content_str": [
            {
                "post_id": post_id,
                "title": board[post_id]["title"],
                "body": board[post_id]["body"],
                "body_sha256": board[post_id]["body_sha256"],
                "post_type": board[post_id]["post_type"],
                "public_author_profile": profiles[
                    str(board[post_id]["author_agent_id"])
                ],
                "untrusted_content_kind": "community_post",
                "content_level": "full_body",
            }
            for post_id in selected_ids
        ],
    }
    expected_react_request = expected_community_request(
        context,
        logical_stage="community_read_react",
        template_stage=COMMUNITY_READING_STAGE,
        agent_id=reader_id,
        event_id=event_id,
        prompt_values=react_values,
    )
    reaction_response = react_record.get("response")
    if not isinstance(reaction_response, Mapping):
        raise RNCommunityArtifactError(
            "Reaction journal response is not an object"
        )
    try:
        normalized_reaction = _validate_reaction_response(
            reaction_response,
            selected_ids=tuple(selected_ids),
        )
    except Exception as exc:
        raise RNCommunityArtifactError(
            f"Reaction journal response violates runtime semantics: {exc}"
        ) from exc
    reaction_rows = normalized_reaction["reactions"]
    if (
        react_record.get("request") != expected_react_request
        or {
            (item.get("post_id"), item.get("reaction"))
            for item in reaction_rows
            if isinstance(item, Mapping)
        }
        != {
            (post_id, selected_rows[post_id]["reaction"])
            for post_id in selected_ids
        }
        or len(reaction_rows) != len(selected_ids)
    ):
        raise RNCommunityArtifactError(
            "Reaction journal output does not reproduce the sealed reader trace"
        )


def _verify_reaction_score_semantics(
    *,
    phase_id: str,
    board: Mapping[str, Mapping[str, Any]],
    interaction_rows: Sequence[Mapping[str, Any]],
) -> None:
    snapshots: dict[str, tuple[int, int]] = {}
    counts: dict[str, tuple[int, int]] = {}
    for row in interaction_rows:
        post_id = str(row["post_id"])
        snapshot = (int(row["score_snapshot"]), int(row["like_count_snapshot"]))
        if post_id in snapshots and snapshots[post_id] != snapshot:
            raise RNCommunityArtifactError(
                f"Candidate score snapshot differs across readers in {phase_id}"
            )
        snapshots[post_id] = snapshot
        likes, unlikes = counts.get(post_id, (0, 0))
        if row["reaction"] == "like":
            likes += 1
        elif row["reaction"] == "unlike":
            unlikes += 1
        counts[post_id] = (likes, unlikes)
    for post_id, post in board.items():
        if post_id not in snapshots:
            if interaction_rows:
                raise RNCommunityArtifactError(
                    "A public post is absent from all candidate snapshots"
                )
            continue
        base_score, base_likes = snapshots[post_id]
        likes, unlikes = counts.get(post_id, (0, 0))
        if (
            int(post["score"]) != base_score + likes - unlikes
            or int(post["like_count"]) != base_likes + likes
        ):
            raise RNCommunityArtifactError(
                "Public post score/like_count is not reproduced by reactions"
            )


def _validate_exact_phase_bindings(
    context: RNRunContext,
    *,
    connection: Any,
    topology: Mapping[str, Any],
    boards: Mapping[str, Mapping[str, Mapping[str, Any]]],
    reader_rows_by_phase: Mapping[str, Sequence[Mapping[str, Any]]],
) -> frozenset[tuple[str, str]]:
    """Cross-bind every phase observation and full-body exposure."""

    timing_sha = context.resolved.spec.community_timing_policy_sha256
    best_k = int(context.resolved.spec.community_policy["best_k"])
    profiles = (
        {}
        if context.generated_inputs is None
        else context.generated_inputs.public_profiles
    )
    active_agents = {
        agent_id
        for agent_id in context.agent_ids
        if context.personas.persona(agent_id).news_depth in {1, 2}
    }
    if set(profiles) != active_agents:
        raise RNCommunityArtifactError(
            "Community exposure validation lacks the sealed public profiles"
        )

    expected_exposures: dict[str, dict[str, Any]] = {}
    expected_interpretations: set[tuple[str, str]] = set()
    for phase_id, phase in topology["phases"].items():
        board = boards[phase_id]
        reader_rows = tuple(reader_rows_by_phase[phase_id])
        selected_rows = tuple(
            row for row in reader_rows if row["selected"] == "true"
        )
        selected_payload = topology["selected"][phase_id]
        expected_selections = [
            {
                "reader_agent_id": row["reader_agent_id"],
                "post_id": row["post_id"],
            }
            for row in sorted(
                selected_rows,
                key=lambda row: (row["reader_agent_id"], row["post_id"]),
            )
        ]
        if selected_payload != {
            "schema_version": "rn-community-selected-v1",
            "phase": phase,
            "selections": expected_selections,
            "community_timing_policy_sha256": timing_sha,
        }:
            raise RNCommunityArtifactError(
                "Community selected-read observation differs from reader decisions"
            )

        ordered_board = sorted(
            board.values(),
            key=lambda post: (
                -int(post["score"]),
                -int(post["like_count"]),
                post["post_id"],
            ),
        )
        expected_best = ordered_board[:best_k]
        next_am = phase["next_am_event"]
        expected_status = (
            "empty"
            if not expected_best
            else "right_censored"
            if next_am is None
            else "scheduled"
        )
        schedule = topology["best_schedule"][phase_id]
        if schedule != {
            "schema_version": "rn-community-best-schedule-v1",
            "phase": phase,
            "status": expected_status,
            "best_posts": expected_best,
            "audience_agent_ids": list(context.agent_ids),
            "community_timing_policy_sha256": timing_sha,
        }:
            raise RNCommunityArtifactError(
                "Community Best schedule differs from deterministic board ranking"
            )
        checkpoint = topology["checkpoint"][phase_id]
        if checkpoint != {
            "schema_version": "rn-community-checkpoint-v1",
            "mode": "on",
            "phase": phase,
            "post_count": len(board),
            "selected_exposure_count": len(selected_rows),
            "best_post_ids": [post["post_id"] for post in expected_best],
            "best_status": expected_status,
            "community_timing_policy_sha256": timing_sha,
        }:
            raise RNCommunityArtifactError(
                "Community checkpoint does not reproduce its complete phase"
            )

        source = phase["after_event"]
        for row in selected_rows:
            post = board[row["post_id"]]
            metadata = _expected_exposure_metadata(
                phase=phase,
                post=post,
                rank=None,
                public_author_profile=profiles[post["author_agent_id"]],
            )
            spec = _expected_full_exposure(
                context,
                agent_id=str(row["reader_agent_id"]),
                event_id=str(source["event_id"]),
                channel="community_selected",
                post=post,
                delivered_at=phase["observed_at"],
                status="delivered",
                metadata=metadata,
            )
            if spec["exposure_id"] in expected_exposures:
                raise RNCommunityArtifactError(
                    "Community selected exposure identity collides"
                )
            expected_exposures[spec["exposure_id"]] = spec

        if next_am is None:
            for agent_id in context.agent_ids:
                for rank, post in enumerate(expected_best, start=1):
                    metadata = _expected_exposure_metadata(
                        phase=phase,
                        post=post,
                        rank=rank,
                        public_author_profile=None,
                    )
                    spec = _expected_full_exposure(
                        context,
                        agent_id=agent_id,
                        event_id=str(source["event_id"]),
                        channel="community_best",
                        post=post,
                        delivered_at=None,
                        status="right_censored",
                        metadata=metadata,
                    )
                    expected_exposures[spec["exposure_id"]] = spec
        else:
            delivered_at = str(next_am["event_id"])
            # The resolved lifecycle pins delivery to the next-AM decision
            # timestamp, never to a caller-selected wall-clock instant.
            delivered_timestamp = next(
                event.decision_timestamp
                for event in context.resolved.decision_events
                if event.decision_event_id == delivered_at
            )
            expected_deliveries: list[dict[str, Any]] = []
            for agent_id in context.agent_ids:
                if expected_best:
                    expected_interpretations.add(
                        (agent_id, str(next_am["event_id"]))
                    )
                for rank, post in enumerate(expected_best, start=1):
                    metadata = _expected_exposure_metadata(
                        phase=phase,
                        post=post,
                        rank=rank,
                        public_author_profile=None,
                    )
                    spec = _expected_full_exposure(
                        context,
                        agent_id=agent_id,
                        event_id=str(next_am["event_id"]),
                        channel="community_best",
                        post=post,
                        delivered_at=delivered_timestamp,
                        status="delivered",
                        metadata=metadata,
                    )
                    expected_exposures[spec["exposure_id"]] = spec
                    expected_deliveries.append(
                        {
                            "agent_id": agent_id,
                            "exposure_id": spec["exposure_id"],
                            "post_id": post["post_id"],
                            "body_sha256": post["body_sha256"],
                            "rank": rank,
                        }
                    )
            delivery = topology["best_delivery"][phase_id]
            if delivery != {
                "schema_version": "rn-community-best-delivery-v1",
                "phase": phase,
                "delivered_at": delivered_timestamp,
                "deliveries": expected_deliveries,
                "community_timing_policy_sha256": timing_sha,
            }:
                raise RNCommunityArtifactError(
                    "Community Best delivery differs from schedule/audience/exposures"
                )
            for row in reader_rows:
                if row["visible_from_event_id"] == next_am["event_id"]:
                    expected_interpretations.add(
                        (str(row["reader_agent_id"]), str(next_am["event_id"]))
                    )

    stored_rows = connection.execute(
        """
        SELECT *
        FROM agent_exposures
        WHERE run_id = ? AND condition_id = ?
          AND channel IN ('community_best', 'community_selected')
        ORDER BY exposure_id
        """,
        (context.run_id, RN_COMM_ON),
    ).fetchall()
    stored_by_id = {str(row["exposure_id"]): row for row in stored_rows}
    if len(stored_by_id) != len(stored_rows) or set(stored_by_id) != set(
        expected_exposures
    ):
        raise RNCommunityArtifactError(
            "Community full-body exposure set differs from phase semantics"
        )
    for exposure_id, expected in expected_exposures.items():
        stored = stored_by_id[exposure_id]
        metadata = _strict_json(
            stored["metadata_json"],
            label=f"community exposure {exposure_id}",
            expected_type=dict,
        )
        if (
            str(stored["run_id"]) != context.run_id
            or str(stored["condition_id"]) != RN_COMM_ON
            or str(stored["agent_id"]) != expected["agent_id"]
            or str(stored["event_id"]) != expected["event_id"]
            or str(stored["channel"]) != expected["channel"]
            or str(stored["root_id"]) != expected["root_id"]
            or str(stored["body_sha256"]) != expected["body_sha256"]
            or stored["delivered_at"] != expected["delivered_at"]
            or str(stored["status"]) != expected["status"]
            or metadata != expected["metadata"]
        ):
            raise RNCommunityArtifactError(
                "Community full-body exposure differs from board/schedule/delivery"
            )

    claim_pairs = {
        (
            str(observation["payload"].get("agent_id")),
            str(observation["payload"].get("event_id")),
        )
        for observation in topology["claims"]
    }
    if len(claim_pairs) != len(topology["claims"]) or claim_pairs != expected_interpretations:
        raise RNCommunityArtifactError(
            "Community interpretation claim-observation set differs from actual exposure visibility"
        )
    return frozenset(expected_interpretations)


def _expected_exposure_metadata(
    *,
    phase: Mapping[str, Any],
    post: Mapping[str, Any],
    rank: int | None,
    public_author_profile: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "schema_version": "rn-community-exposure-v1",
        "source_phase_id": phase["phase_id"],
        "visible_from_event_id": (
            None
            if phase["next_am_event"] is None
            else phase["next_am_event"]["event_id"]
        ),
        "content_level": "full_body",
        "post_id": post["post_id"],
        "ledger_author_agent_id": post["author_agent_id"],
        "title": post["title"],
        "full_body": post["body"],
        "body_sha256": post["body_sha256"],
        "content_version": post["content_version"],
        "content_version_sha256": post["content_version_sha256"],
        "post_type": post["post_type"],
        "score": post["score"],
        "like_count": post["like_count"],
        "rank": rank,
        "public_author_profile": (
            None
            if public_author_profile is None
            else dict(public_author_profile)
        ),
    }


def _expected_full_exposure(
    context: RNRunContext,
    *,
    agent_id: str,
    event_id: str,
    channel: str,
    post: Mapping[str, Any],
    delivered_at: str | None,
    status: str,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    identity = {
        "run_id": context.run_id,
        "condition_id": RN_COMM_ON,
        "agent_id": agent_id,
        "event_id": event_id,
        "channel": channel,
        "root_id": post["post_id"],
        "body_sha256": post["body_sha256"],
        "status": status,
    }
    return {
        "exposure_id": "exp:" + canonical_sha256(identity)[:40],
        "agent_id": agent_id,
        "event_id": event_id,
        "channel": channel,
        "root_id": post["post_id"],
        "body_sha256": post["body_sha256"],
        "delivered_at": delivered_at,
        "status": status,
        "metadata": dict(metadata),
    }


def _validate_auxiliary_journal_sinks(
    context: RNRunContext,
    *,
    connection: Any,
    journal_records: Mapping[str, Mapping[str, Any]],
    reader_rows_by_phase: Mapping[str, Sequence[Mapping[str, Any]]],
    expected_interpretations: frozenset[tuple[str, str]],
) -> None:
    """Every committed community call must have exactly one semantic sink."""

    from twinmarket_kr.rn_ab.memory import RN_AUXILIARY_STAGE_SCHEMA_VERSIONS

    auxiliary_stages = set(RN_AUXILIARY_STAGE_SCHEMA_VERSIONS)
    committed_aux = {
        logical_call_id
        for logical_call_id in journal_records
        if _logical_components(logical_call_id)[4] in auxiliary_stages
    }
    expected: set[str] = set()
    phases = _canonical_phase_contexts(context)
    active_agents = {
        agent_id
        for agent_id in context.agent_ids
        if context.personas.persona(agent_id).news_depth in {1, 2}
    }
    trace_rows = connection.execute(
        """
        SELECT phase_id, event_id, author_agent_id, logical_call_id
        FROM community_post_trace
        WHERE run_id = ? AND condition_id = ?
        ORDER BY phase_id, author_agent_id
        """,
        (context.run_id, RN_COMM_ON),
    ).fetchall()
    trace_sinks: dict[tuple[str, str], str] = {}
    for row in trace_rows:
        key = (str(row["phase_id"]), str(row["author_agent_id"]))
        if key in trace_sinks:
            raise RNCommunityArtifactError(
                "Community posting trace repeats a phase/author sink"
            )
        phase = phases.get(key[0])
        if (
            phase is None
            or key[1] not in active_agents
            or str(row["event_id"]) != phase["after_event"]["event_id"]
        ):
            raise RNCommunityArtifactError(
                "Community posting trace has a foreign phase/author/event"
            )
        logical_call_id = str(row["logical_call_id"])
        components = _logical_components(logical_call_id)
        if components != (
            context.run_id,
            RN_COMM_ON,
            key[1],
            str(row["event_id"]),
            "community_posting",
            RN_AUXILIARY_STAGE_SCHEMA_VERSIONS["community_posting"],
        ):
            raise RNCommunityArtifactError(
                "Community posting trace cites a non-canonical logical call"
            )
        trace_sinks[key] = logical_call_id
        expected.add(logical_call_id)
    expected_trace_keys = {
        (phase_id, agent_id)
        for phase_id in phases
        for agent_id in active_agents
    }
    if set(trace_sinks) != expected_trace_keys:
        raise RNCommunityArtifactError(
            "Community posting traces do not cover every eligible phase/author"
        )

    for phase_id, rows in reader_rows_by_phase.items():
        event_id = phases[phase_id]["after_event"]["event_id"]
        by_reader: dict[str, list[Mapping[str, Any]]] = {}
        for row in rows:
            by_reader.setdefault(str(row["reader_agent_id"]), []).append(row)
        for reader, candidate_rows in by_reader.items():
            if candidate_rows:
                expected.add(
                    _logical_call_id(
                        context,
                        agent_id=reader,
                        event_id=event_id,
                        stage="community_read_select",
                        schemas=RN_AUXILIARY_STAGE_SCHEMA_VERSIONS,
                    )
                )
            if any(row["selected"] == "true" for row in candidate_rows):
                expected.add(
                    _logical_call_id(
                        context,
                        agent_id=reader,
                        event_id=event_id,
                        stage="community_read_react",
                        schemas=RN_AUXILIARY_STAGE_SCHEMA_VERSIONS,
                    )
                )
    for agent_id, event_id in expected_interpretations:
        expected.add(
            _logical_call_id(
                context,
                agent_id=agent_id,
                event_id=event_id,
                stage="community_interpretation",
                schemas=RN_AUXILIARY_STAGE_SCHEMA_VERSIONS,
            )
        )
    if committed_aux != expected:
        raise RNCommunityArtifactError(
            "Committed community journal calls differ from exact semantic sinks: "
            f"missing={sorted(expected - committed_aux)} "
            f"extra={sorted(committed_aux - expected)}"
        )


def _logical_call_id(
    context: RNRunContext,
    *,
    agent_id: str,
    event_id: str,
    stage: str,
    schemas: Mapping[str, str],
) -> str:
    return "|".join(
        (
            context.run_id,
            RN_COMM_ON,
            agent_id,
            event_id,
            stage,
            schemas[stage],
        )
    )


def _best_rows(
    context: RNRunContext,
    *,
    connection: Any,
    boards: Mapping[str, Mapping[str, Mapping[str, Any]]],
    schedules: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for phase_id, payload in schedules.items():
        if (
            payload.get("schema_version") != "rn-community-best-schedule-v1"
            or not isinstance(payload.get("phase"), Mapping)
            or payload["phase"].get("phase_id") != phase_id
            or payload.get("status") not in {"empty", "scheduled", "right_censored"}
            or not isinstance(payload.get("best_posts"), list)
            or payload.get("audience_agent_ids") != list(context.agent_ids)
        ):
            raise RNCommunityArtifactError("Community Best schedule is malformed")
        posts = [
            _validated_post(context, phase_id=phase_id, raw=raw)
            for raw in payload["best_posts"]
        ]
        expected_order = sorted(
            posts,
            key=lambda post: (
                -int(post["score"]),
                -int(post["like_count"]),
                post["post_id"],
            ),
        )
        if posts != expected_order or len(posts) > int(
            context.resolved.spec.community_policy["best_k"]
        ):
            raise RNCommunityArtifactError(
                "Community Best schedule violates deterministic rank/order"
            )
        if (payload["status"] == "empty") != (len(posts) == 0):
            raise RNCommunityArtifactError(
                "Community Best status/count is inconsistent"
            )
        phase = payload["phase"]
        event = phase.get("after_event")
        if not isinstance(event, Mapping):
            raise RNCommunityArtifactError("Community Best phase lacks PM event")
        next_event = phase.get("next_am_event")
        visible = (
            ""
            if next_event is None
            else str(next_event.get("event_id") or "")
        )
        for rank, post in enumerate(posts, start=1):
            if (
                post["post_id"] not in boards.get(phase_id, {})
                or post != boards[phase_id][post["post_id"]]
            ):
                raise RNCommunityArtifactError(
                    "Community Best post differs from its frozen public board"
                )
            output.append(
                {
                    "schema_version": "rn-community-best-post-v1",
                    "run_id": context.run_id,
                    "condition_id": RN_COMM_ON,
                    "manifest_sha256": context.manifest_sha256,
                    "phase_id": phase_id,
                    "source_event_id": str(event["event_id"]),
                    "source_turn": int(event["turn"]),
                    "source_date": str(event["date"]),
                    "visible_from_event_id": visible,
                    "schedule_status": str(payload["status"]),
                    "rank": rank,
                    "post_id": post["post_id"],
                    "author_agent_id": post["author_agent_id"],
                    "title": post["title"],
                    "body": post["body"],
                    "body_sha256": post["body_sha256"],
                    "content_version": post["content_version"],
                    "content_version_sha256": post["content_version_sha256"],
                    "post_type": post["post_type"],
                    "score": int(post["score"]),
                    "like_count": int(post["like_count"]),
                    "audience_count": len(context.agent_ids),
                }
            )
    return output


def _claim_lineage(
    context: RNRunContext,
    *,
    connection: Any,
    candidates_by_exposure: Mapping[str, Mapping[str, Any]],
    journal_records: Mapping[str, Mapping[str, Any]],
    observations: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, tuple[dict[str, Any], ...]], tuple[dict[str, Any], ...]]:
    from twinmarket_kr.rn_ab.community import RNCommunityService
    from twinmarket_kr.rn_ab.community_provider import (
        _validate_interpretation_response,
    )
    from twinmarket_kr.rn_ab.prompt_registry import (
        COMMUNITY_INTERPRETATION_STAGE,
    )

    if context.generated_inputs is None:
        raise RNCommunityArtifactError(
            "Community interpretation validation lacks sealed generated inputs"
        )
    service = RNCommunityService.from_resolved_manifest(
        context.open_store(RN_COMM_ON),
        context.resolved,
        public_profiles=context.generated_inputs.public_profiles,
    )
    full_rows = connection.execute(
        """
        SELECT exposure_id, agent_id, event_id, root_id, metadata_json
        FROM agent_exposures
        WHERE run_id = ? AND condition_id = ?
          AND channel IN ('community_best', 'community_selected')
          AND status = 'delivered'
        """,
        (context.run_id, RN_COMM_ON),
    ).fetchall()
    full_by_id = {str(row["exposure_id"]): row for row in full_rows}
    claims_by_exposure: dict[str, list[dict[str, Any]]] = {}
    all_claims: list[dict[str, Any]] = []
    for observation in observations:
        stage = str(observation["stage"])
        payload = observation["payload"]
        reader = stage.removeprefix("community_claims:")
        if (
            set(payload) != {"schema_version", "agent_id", "event_id", "claims"}
            or payload.get("schema_version") != "rn-community-claims-v1"
            or payload.get("agent_id") != reader
            or not isinstance(payload.get("claims"), list)
        ):
            raise RNCommunityArtifactError(
                "Community claim observation is malformed"
            )
        event_id = str(payload["event_id"])
        if (
            event_id != str(observation["event_id"])
            or int(observation["turn"])
            != _event_coordinates(context, event_id)[0]
            or str(observation["date"])
            != _event_coordinates(context, event_id)[1]
        ):
            raise RNCommunityArtifactError(
                "Community claim observation event coordinates are inconsistent"
            )
        stage_records = _journal_stage_records(
            journal_records,
            run_id=context.run_id,
            condition_id=RN_COMM_ON,
            agent_id=reader,
            event_id=event_id,
        )
        interpretation = stage_records.get("community_interpretation")
        if interpretation is None:
            raise RNCommunityArtifactError(
                "Community claims have no committed interpretation response"
            )
        exposures = service.interpretation_payloads(
            agent_id=reader,
            event_id=event_id,
        )
        if not exposures:
            raise RNCommunityArtifactError(
                "Community interpretation journal has no reader-visible exposure"
            )
        prompt_values = {
            "persona_prompt": context.personas.persona(reader).persona_prompt,
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
        }
        expected_request = expected_community_request(
            context,
            logical_stage="community_interpretation",
            template_stage=COMMUNITY_INTERPRETATION_STAGE,
            agent_id=reader,
            event_id=event_id,
            prompt_values=prompt_values,
        )
        if interpretation.get("request") != expected_request:
            raise RNCommunityArtifactError(
                "Community interpretation journal request differs from actual reader visibility"
            )
        available_sources = _available_interpretation_sources(exposures)
        response = interpretation.get("response")
        if not isinstance(response, Mapping):
            raise RNCommunityArtifactError(
                "Community interpretation response has no exact claim array"
            )
        try:
            normalized_interpretation = _validate_interpretation_response(
                response,
                available_sources=available_sources,
            )
        except Exception as exc:
            raise RNCommunityArtifactError(
                f"Community interpretation response violates runtime semantics: {exc}"
            ) from exc
        response_claims = normalized_interpretation["claims"]
        normalized_response = [
            {
                "claim_text": item.get("claim_text"),
                "stance": item.get("claim_stance"),
                "source_exposure_ids": sorted(item.get("source_exposure_ids", [])),
                "supporting_quote": item.get("supporting_quote"),
            }
            for item in response_claims
            if isinstance(item, Mapping)
        ]
        normalized_observation: list[dict[str, Any]] = []
        for raw in payload["claims"]:
            if not isinstance(raw, Mapping) or set(raw) != {
                "claim_id",
                "claim_text",
                "stance",
                "source_exposure_ids",
                "supporting_quote",
                "source_roots",
            }:
                raise RNCommunityArtifactError(
                    "Community claim row has an invalid private schema"
                )
            sources = raw["source_exposure_ids"]
            if (
                not isinstance(sources, list)
                or not sources
                or sources != sorted(set(sources))
            ):
                raise RNCommunityArtifactError(
                    "Community claim source exposures are invalid"
                )
            quote = raw["supporting_quote"]
            if not isinstance(quote, str) or not quote.strip():
                raise RNCommunityArtifactError(
                    "Community claim has no exact supporting quote"
                )
            source_roots: set[str] = set()
            supported = False
            for exposure_id in sources:
                if exposure_id in candidates_by_exposure:
                    source = candidates_by_exposure[exposure_id]
                    if source["reader_agent_id"] != reader:
                        raise RNCommunityArtifactError(
                            "Community claim cites another reader's candidate"
                        )
                    source_roots.add(str(source["post_id"]))
                    supported = supported or quote in str(source["title"])
                elif exposure_id in full_by_id:
                    source = full_by_id[exposure_id]
                    metadata = _strict_json(
                        source["metadata_json"],
                        label=f"claim source exposure {exposure_id}",
                        expected_type=dict,
                    )
                    if str(source["agent_id"]) != reader:
                        raise RNCommunityArtifactError(
                            "Community claim cites another reader's full-body exposure"
                        )
                    source_roots.add(str(source["root_id"]))
                    supported = supported or quote in str(
                        metadata.get("title", "")
                    ) or quote in str(metadata.get("full_body", ""))
                else:
                    raise RNCommunityArtifactError(
                        "Community claim cites an unknown exposure ID"
                    )
            if not supported or sorted(source_roots) != raw["source_roots"]:
                raise RNCommunityArtifactError(
                    "Community claim quote/root provenance is invalid"
                )
            expected_claim_id = "community_claim:" + canonical_sha256(
                {
                    "run_id": context.run_id,
                    "condition_id": RN_COMM_ON,
                    "agent_id": reader,
                    "event_id": event_id,
                    "claim_text": raw["claim_text"],
                    "stance": raw["stance"],
                    "source_exposure_ids": sources,
                    "supporting_quote": quote,
                }
            )[:40]
            if raw["claim_id"] != expected_claim_id:
                raise RNCommunityArtifactError(
                    "Community claim identity is non-canonical"
                )
            normalized_observation.append(
                {
                    "claim_text": raw["claim_text"],
                    "stance": raw["stance"],
                    "source_exposure_ids": sources,
                    "supporting_quote": quote,
                }
            )
            record = {
                "claim_id": expected_claim_id,
                "reader_agent_id": reader,
                "event_id": event_id,
                "stance": raw["stance"],
                "source_exposure_ids": list(sources),
                "supporting_quote_sha256": hashlib.sha256(
                    quote.encode("utf-8")
                ).hexdigest(),
            }
            all_claims.append(record)
            for exposure_id in sources:
                claims_by_exposure.setdefault(exposure_id, []).append(record)
        if normalized_response != normalized_observation:
            raise RNCommunityArtifactError(
                "Interpretation journal output differs from committed claim lineage"
            )
    return (
        {
            exposure_id: tuple(
                sorted(rows, key=lambda row: row["claim_id"])
            )
            for exposure_id, rows in claims_by_exposure.items()
        },
        tuple(sorted(all_claims, key=lambda row: row["claim_id"])),
    )


def _available_interpretation_sources(
    exposures: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    available: dict[str, dict[str, Any]] = {}
    for item in exposures:
        source_ids = item.get("source_exposure_ids")
        level = item.get("content_level")
        title = item.get("title")
        if (
            not isinstance(source_ids, Sequence)
            or isinstance(source_ids, (str, bytes, bytearray))
            or level not in {"title_only", "full_body"}
            or not isinstance(title, str)
        ):
            raise RNCommunityArtifactError(
                "Community interpretation exposure projection is malformed"
            )
        allowed_texts = [title]
        if level == "full_body":
            body = item.get("body")
            if not isinstance(body, str):
                raise RNCommunityArtifactError(
                    "Community full-body interpretation exposure lacks its body"
                )
            allowed_texts.append(body)
        for source_id in source_ids:
            if not isinstance(source_id, str) or not source_id:
                raise RNCommunityArtifactError(
                    "Community interpretation exposure ID is malformed"
                )
            material = {
                "content_level": level,
                "allowed_texts": tuple(allowed_texts),
            }
            previous = available.get(source_id)
            if previous is not None and previous != material:
                raise RNCommunityArtifactError(
                    "Community interpretation exposure ID maps to conflicting text"
                )
            available[source_id] = material
    return available


def _community_edges(
    connection: Any,
    *,
    run_id: str,
    condition_id: str,
) -> dict[str, tuple[dict[str, Any], ...]]:
    rows = connection.execute(
        """
        SELECT edge_id, agent_id, event_id, target_id, source_id, dimension
        FROM memory_evidence_edges
        WHERE run_id = ? AND condition_id = ?
          AND target_kind = 'stb' AND source_kind = 'community_claim'
        ORDER BY source_id, edge_id
        """,
        (run_id, condition_id),
    ).fetchall()
    by_claim: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_claim.setdefault(str(row["source_id"]), []).append(
            {
                "edge_id": str(row["edge_id"]),
                "agent_id": str(row["agent_id"]),
                "event_id": str(row["event_id"]),
                "stb_id": str(row["target_id"]),
                "dimension": row["dimension"],
            }
        )
    return {
        claim_id: tuple(entries) for claim_id, entries in by_claim.items()
    }


def _exposure_row(
    context: RNRunContext,
    *,
    exposure_id: str,
    reader_agent_id: str,
    source_phase_id: str,
    source_event_id: str,
    visible_from_event_id: Any,
    root_post_id: str,
    channel: str,
    content_level: str,
    status: str,
    delivered_at: Any,
    title_sha256: str,
    body_sha256: str | None,
    rank: Any,
    score: int,
    like_count: int,
    selected: bool,
    reaction: str | None,
    claims: Sequence[Mapping[str, Any]],
    edges_by_claim: Mapping[str, Sequence[Mapping[str, Any]]],
    prompt_call_ids: Sequence[str],
) -> dict[str, Any]:
    claim_ids = [str(claim["claim_id"]) for claim in claims]
    edges = [
        dict(edge)
        for claim_id in claim_ids
        for edge in edges_by_claim.get(claim_id, ())
    ]
    row = {
        "schema_version": "rn-community-exposure-trace-v1",
        "run_id": context.run_id,
        "condition_id": RN_COMM_ON,
        "manifest_sha256": context.manifest_sha256,
        "exposure_id": exposure_id,
        "reader_agent_id": reader_agent_id,
        "source_phase_id": source_phase_id,
        "source_event_id": source_event_id,
        "visible_from_event_id": visible_from_event_id,
        "root_post_id": root_post_id,
        "channel": channel,
        "content_level": content_level,
        "delivery_status": status,
        "delivered_at": delivered_at,
        "title_sha256": title_sha256,
        "body_sha256": body_sha256,
        "rank": rank,
        "score_snapshot": score,
        "like_count_snapshot": like_count,
        "selected": selected,
        "reaction": reaction,
        "prompt_consumption_logical_call_ids": sorted(set(prompt_call_ids)),
        "interpretation_claim_ids": claim_ids,
        "claim_stances": {
            str(claim["claim_id"]): str(claim["stance"]) for claim in claims
        },
        "claim_supporting_quote_sha256": {
            str(claim["claim_id"]): str(claim["supporting_quote_sha256"])
            for claim in claims
        },
        "stb_evidence_edge_ids": sorted(
            str(edge["edge_id"]) for edge in edges
        ),
        "stb_ids": sorted({str(edge["stb_id"]) for edge in edges}),
        "truth_verdict": None,
        "privacy_boundary": (
            "no_persona_no_portfolio_no_private_belief_no_full_body"
        ),
    }
    return {**row, "trace_sha256": canonical_sha256(row)}


def _prompt_call_ids(
    records: Mapping[str, Mapping[str, Any]],
    *,
    agent_id: str,
    source_event_id: str,
    visible_event_id: Any,
    include_select: bool,
    include_react: bool,
) -> tuple[str, ...]:
    allowed: set[tuple[str, str]] = set()
    if include_select:
        allowed.add((source_event_id, "community_read_select"))
    if include_react:
        allowed.add((source_event_id, "community_read_react"))
    if visible_event_id:
        allowed.add((str(visible_event_id), "community_interpretation"))
    return tuple(
        sorted(
            logical_call_id
            for logical_call_id in records
            if (
                (_logical_components(logical_call_id)[2] == agent_id)
                and (
                    _logical_components(logical_call_id)[3],
                    _logical_components(logical_call_id)[4],
                )
                in allowed
            )
        )
    )


def _journal_stage_records(
    records: Mapping[str, Mapping[str, Any]],
    *,
    run_id: str,
    condition_id: str,
    agent_id: str,
    event_id: str,
) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for logical_call_id, record in records.items():
        components = _logical_components(logical_call_id)
        if components[:4] != (run_id, condition_id, agent_id, event_id):
            continue
        stage = components[4]
        if stage in result:
            raise RNCommunityArtifactError(
                "Journal repeats a logical stage for one agent/event"
            )
        result[stage] = record
    return result


def _logical_components(logical_call_id: str) -> tuple[str, str, str, str, str, str]:
    components = tuple(str(logical_call_id).split("|"))
    if len(components) != 6:
        raise RNCommunityArtifactError(
            f"Malformed community journal logical-call ID: {logical_call_id}"
        )
    return components  # type: ignore[return-value]


def _validated_post(
    context: RNRunContext,
    *,
    phase_id: str,
    raw: Any,
) -> dict[str, Any]:
    from twinmarket_kr.rn_ab.community import FrozenCommunityPost

    try:
        post = FrozenCommunityPost.from_ledger(raw)
    except Exception as exc:
        raise RNCommunityArtifactError(
            f"Frozen community post is invalid: {exc}"
        ) from exc
    expected_post_id = "post:" + canonical_sha256(
        {
            "run_id": context.run_id,
            "condition_id": RN_COMM_ON,
            "phase_id": phase_id,
            "author_agent_id": post.author_agent_id,
            "title": post.title,
            "body_sha256": post.body_sha256,
            "content_version_sha256": post.content_version_sha256,
            "post_type": post.post_type,
        }
    )[:40]
    if post.post_id != expected_post_id:
        raise RNCommunityArtifactError(
            "Frozen community post has a non-canonical deterministic post ID"
        )
    return post.to_dict()


def _validate_full_body_metadata(stored: Mapping[str, Any], metadata: Mapping[str, Any]) -> None:
    required = {
        "schema_version",
        "source_phase_id",
        "visible_from_event_id",
        "content_level",
        "post_id",
        "ledger_author_agent_id",
        "title",
        "full_body",
        "body_sha256",
        "content_version",
        "content_version_sha256",
        "post_type",
        "score",
        "like_count",
        "rank",
        "public_author_profile",
    }
    body = metadata.get("full_body")
    if (
        set(metadata) != required
        or metadata.get("schema_version") != "rn-community-exposure-v1"
        or metadata.get("content_level") != "full_body"
        or metadata.get("post_id") != stored["root_id"]
        or metadata.get("body_sha256") != stored["body_sha256"]
        or not isinstance(body, str)
        or hashlib.sha256(body.encode("utf-8")).hexdigest()
        != stored["body_sha256"]
        or (
            stored["status"] == "delivered" and stored["delivered_at"] is None
        )
        or (
            stored["status"] == "right_censored"
            and stored["delivered_at"] is not None
        )
    ):
        raise RNCommunityArtifactError(
            "Community full-body exposure metadata is malformed"
        )


def _observations(
    connection: Any,
    *,
    run_id: str,
    condition_id: str,
    stage_prefix: str,
) -> dict[str, dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT stage, payload_json, payload_sha256
        FROM observation_events
        WHERE run_id = ? AND condition_id = ? AND stage LIKE ?
        ORDER BY stage
        """,
        (run_id, condition_id, stage_prefix + "%"),
    ).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        stage = str(row["stage"])
        if stage in result:
            raise RNCommunityArtifactError(
                f"Duplicate community observation stage: {stage}"
            )
        payload = _strict_json(
            row["payload_json"],
            label=f"community observation {stage}",
            expected_type=dict,
        )
        if canonical_sha256(payload) != str(row["payload_sha256"]):
            raise RNCommunityArtifactError(
                f"Community observation hash mismatch: {stage}"
            )
        result[stage] = payload
    return result


def _strict_json(raw: Any, *, label: str, expected_type: type) -> Any:
    if not isinstance(raw, str):
        raise RNCommunityArtifactError(f"{label} must be canonical JSON text")

    def exact_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw,
            object_pairs_hook=exact_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-standard JSON constant: {value}")
            ),
        )
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (
        json.JSONDecodeError,
        TypeError,
        ValueError,
        OverflowError,
        RecursionError,
        UnicodeError,
    ) as exc:
        raise RNCommunityArtifactError(f"{label} is not strict JSON") from exc
    if not isinstance(value, expected_type) or canonical != raw:
        raise RNCommunityArtifactError(f"{label} is not canonical JSON")
    return value


def _csv_bytes(
    columns: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
) -> bytes:
    from io import StringIO

    buffer = StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=list(columns),
        extrasaction="raise",
        lineterminator="\n",
    )
    writer.writeheader()
    for row in rows:
        if set(row) != set(columns):
            raise RNCommunityArtifactError(
                "Community CSV row differs from its exact schema"
            )
        writer.writerow(dict(row))
    return buffer.getvalue().encode("utf-8")


def _jsonl_bytes(rows: Sequence[Mapping[str, Any]]) -> bytes:
    return "".join(
        json.dumps(
            dict(row),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
        for row in rows
    ).encode("utf-8")


def _write_immutable(path: Path, content: bytes) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
            raise RNCommunityArtifactError(
                f"Community artifact already exists with different bytes: {path}"
            )
        return
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _metadata(
    context: RNRunContext,
    *,
    interaction_path: Path,
    best_path: Path,
    trace_path: Path,
    rows: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, dict[str, Any]]:
    return {
        "community_interactions": {
            "path": INTERACTIONS_FILENAME,
            "run_relative_path": INTERACTIONS_FILENAME,
            "sha256": file_sha256(interaction_path),
            "row_count": len(rows["interactions"]),
            "condition_row_counts": {
                RN_COMM_OFF: 0,
                RN_COMM_ON: len(rows["interactions"]),
            },
            "format": _INTERACTION_FORMAT,
            "columns": list(INTERACTION_COLUMNS),
            "privacy": "title_only_reader_mechanism_no_persona_or_portfolio",
            "source": "observation_events:community_reader_trace",
        },
        "community_best_posts": {
            "path": BEST_POSTS_FILENAME,
            "run_relative_path": BEST_POSTS_FILENAME,
            "sha256": file_sha256(best_path),
            "row_count": len(rows["best_posts"]),
            "condition_row_counts": {
                RN_COMM_OFF: 0,
                RN_COMM_ON: len(rows["best_posts"]),
            },
            "format": _BEST_FORMAT,
            "columns": list(BEST_POST_COLUMNS),
            "privacy": "public_frozen_board_content_only",
            "source": "observation_events:community_best_schedule",
        },
        "community_exposure_trace": {
            "path": EXPOSURE_TRACE_FILENAME,
            "run_relative_path": f"traces/{EXPOSURE_TRACE_FILENAME}",
            "sha256": file_sha256(trace_path),
            "row_count": len(rows["exposures"]),
            "condition_row_counts": {
                RN_COMM_OFF: 0,
                RN_COMM_ON: len(rows["exposures"]),
            },
            "format": _EXPOSURE_FORMAT,
            "privacy": (
                "private_reader_lineage_no_persona_no_portfolio_no_full_body"
            ),
            "source": (
                "community_reader_trace+agent_exposures+community_claims+"
                "memory_evidence_edges+response_journal"
            ),
            "truth_semantics": (
                "no_truth_verdict_only_exact_exposure_quote_lineage"
            ),
        },
    }


__all__ = [
    "BEST_POSTS_FILENAME",
    "EXPOSURE_TRACE_FILENAME",
    "INTERACTIONS_FILENAME",
    "RNCommunityArtifactError",
    "expected_community_request",
    "export_community_mechanism_artifacts",
    "validate_community_mechanism_artifacts",
]
