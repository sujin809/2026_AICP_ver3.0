"""Evidence-derived P1 gate for the RN real-news community experiment.

P1 is a separate completed RN run, never a boolean approval file.  This
module reloads that run from its sealed inputs and rechecks the two-day,
100-agent shape, compatibility with the proposed full run, complete
lineage/finalization, recovery exercise, and the actual OFF/ON community
boundary recorded in the arm databases.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from twinmarket_kr.db.connection import connect
from twinmarket_kr.experiment_runtime import file_sha256
from twinmarket_kr.rn_ab.community import RNCommunityService
from twinmarket_kr.rn_ab.finalization import validate_rn_finalization_artifacts
from twinmarket_kr.rn_ab.run_context import RNRunContext
from twinmarket_kr.rn_ab.spec import (
    RN_COMM_OFF,
    RN_COMM_ON,
    RN_CONDITIONS,
    canonical_sha256,
)


class RNP1ValidationError(RuntimeError):
    """A candidate does not prove the final live P1 contract."""


P1_REQUIRED_AGENT_COUNT = 100
P1_REQUIRED_TRADING_DAY_COUNT = 2
P1_REQUIRED_DECISION_EVENT_COUNT = 4
P1_RECOVERY_EVIDENCE_FILENAME = "P1_RECOVERY_EVIDENCE.json"
_P1_RECOVERY_VERSION = "rn-p1-recovery-evidence-v1"


@dataclass(frozen=True)
class RNP1CanaryValidation:
    canary_run_dir: Path
    canary_run_id: str
    canary_manifest_sha256: str
    finalization_sha256: str
    compatibility_sha256: str
    community_summary: Mapping[str, int]
    recovery_mode: str


def assert_p1_candidate_context(context: RNRunContext) -> None:
    """Require the exact separately resolved 100×2-day×AM/PM canary shape."""

    if not isinstance(context, RNRunContext):
        raise RNP1ValidationError("P1 candidate requires a validated RNRunContext")
    try:
        context.assert_execution_source_tree()
        context.resolved.assert_pair_integrity()
    except Exception as exc:
        raise RNP1ValidationError(f"P1 candidate source/pair integrity failed: {exc}") from exc
    if (
        context.resolved.spec.required_agent_count != P1_REQUIRED_AGENT_COUNT
        or len(context.agent_ids) != P1_REQUIRED_AGENT_COUNT
    ):
        raise RNP1ValidationError(
            f"P1 requires exactly {P1_REQUIRED_AGENT_COUNT} frozen agents"
        )
    dates = tuple(context.resolved.trading_dates)
    events = tuple(context.resolved.decision_events)
    if len(dates) != P1_REQUIRED_TRADING_DAY_COUNT:
        raise RNP1ValidationError("P1 requires exactly two approved trading dates")
    if len(events) != P1_REQUIRED_DECISION_EVENT_COUNT:
        raise RNP1ValidationError("P1 requires exactly four decision events")
    for date in dates:
        daily = tuple(event for event in events if event.date == date)
        if len(daily) != 2 or tuple(str(event.subturn).lower() for event in daily) != (
            "am",
            "pm",
        ):
            raise RNP1ValidationError(
                "Every P1 trading date must contain one ordered AM/PM pair"
            )
    phases = tuple(context.resolved.calendar.community_phases)
    # Every PM closes with a community phase.  The final one is still authored
    # and sealed; it simply has no later AM at which a Best post could be
    # delivered, so the community service records that Best as right-censored.
    # Omitting the phase would silently remove a real PM community opportunity
    # from one arm of the canary.
    pm_event_ids = tuple(
        event.decision_event_id
        for event in events
        if str(event.subturn).lower() == "pm"
    )
    if (
        len(phases) != len(pm_event_ids)
        or tuple(phase.after_event_id for phase in phases)
        != pm_event_ids
        or any(phase.next_visible_event_rule != "next-approved-AM" for phase in phases)
    ):
        raise RNP1ValidationError(
            "P1 community phases must follow every PM; the final PM is right-censored"
        )
    if context.generated_inputs is None:
        raise RNP1ValidationError("P1 has no sealed generated community/D2 inputs")


def validate_p1_canary_for_full_run(
    full_context: RNRunContext,
    *,
    p1_canary_run_dir: Path | str,
) -> RNP1CanaryValidation:
    """Reconstruct and validate one completed P1 run for a full-run launch."""

    if not isinstance(full_context, RNRunContext):
        raise RNP1ValidationError("Full run requires a validated RNRunContext")
    try:
        full_context.assert_execution_source_tree()
    except Exception as exc:
        raise RNP1ValidationError(f"Full-run source provenance failed: {exc}") from exc
    if (
        full_context.resolved.spec.required_agent_count != P1_REQUIRED_AGENT_COUNT
        or len(full_context.agent_ids) != P1_REQUIRED_AGENT_COUNT
    ):
        raise RNP1ValidationError("The full RN study must use the same exact 100-agent cohort")
    p1_root = Path(p1_canary_run_dir)
    if p1_root.resolve(strict=False) == full_context.run_dir.resolve(strict=False):
        raise RNP1ValidationError("P1 must be a separate run directory")
    try:
        p1_context = RNRunContext.load(p1_root)
    except Exception as exc:
        raise RNP1ValidationError(f"Cannot reload sealed P1 run: {exc}") from exc
    if p1_context.run_id == full_context.run_id:
        raise RNP1ValidationError("P1 and full run must use distinct run IDs")
    assert_p1_candidate_context(p1_context)
    if tuple(p1_context.agent_ids) != tuple(full_context.agent_ids):
        raise RNP1ValidationError("P1 and full run cohort order/identity differ")
    p1_compatibility = _compatibility_contract(p1_context)
    full_compatibility = _compatibility_contract(full_context)
    if p1_compatibility != full_compatibility:
        differing = sorted(
            key
            for key in set(p1_compatibility) | set(full_compatibility)
            if p1_compatibility.get(key) != full_compatibility.get(key)
        )
        raise RNP1ValidationError(
            "P1/full-run policy compatibility differs: " + ", ".join(differing)
        )

    try:
        from twinmarket_kr.rn_ab.execution import validate_final_run_reasoning_audits

        validate_final_run_reasoning_audits(p1_context)
        finalized = validate_rn_finalization_artifacts(p1_context)
    except Exception as exc:
        raise RNP1ValidationError(
            f"P1 complete reasoning/lineage/finalization validation failed: {exc}"
        ) from exc
    _validate_completed_checkpoint(p1_context)
    recovery_mode = _validate_recovery_evidence(p1_context)
    community_summary = _validate_community_boundary(p1_context)
    return RNP1CanaryValidation(
        canary_run_dir=p1_context.run_dir,
        canary_run_id=p1_context.run_id,
        canary_manifest_sha256=p1_context.manifest_sha256,
        finalization_sha256=file_sha256(finalized.run_record_path),
        compatibility_sha256=canonical_sha256(p1_compatibility),
        community_summary=community_summary,
        recovery_mode=recovery_mode,
    )


def write_p1_recovery_evidence(
    context: RNRunContext,
    *,
    pre_resume_checkpoint: Mapping[str, Any],
) -> Path:
    """Seal a completed recovery exercise observed by the resume command."""

    assert_p1_candidate_context(context)
    recovery_mode, recovered_phase_id = _pre_recovery_identity(
        pre_resume_checkpoint,
        manifest_sha256=context.manifest_sha256,
    )
    checkpoint_path = context.run_dir / "rn_phase_checkpoint.json"
    post_checkpoint = _read_json(checkpoint_path, label="post-resume phase checkpoint")
    _validate_completed_checkpoint_payload(context, post_checkpoint)
    payload = {
        "artifact_type": "rn_p1_recovery_evidence",
        "version": _P1_RECOVERY_VERSION,
        "run_id": context.run_id,
        "resolved_manifest_sha256": context.manifest_sha256,
        "recovery_mode": recovery_mode,
        "recovered_phase_id": recovered_phase_id,
        "pre_resume_checkpoint_sha256": canonical_sha256(dict(pre_resume_checkpoint)),
        "pre_resume_completed_phase_ids_sha256": canonical_sha256(
            [
                str(item.get("phase_id"))
                for item in pre_resume_checkpoint.get("completed_phases", [])
                if isinstance(item, Mapping)
            ]
        ),
        "post_resume_checkpoint_sha256": file_sha256(checkpoint_path),
        "post_resume_completed_phase_ids_sha256": canonical_sha256(
            [str(item["phase_id"]) for item in post_checkpoint["completed_phases"]]
        ),
    }
    path = context.run_dir / P1_RECOVERY_EVIDENCE_FILENAME
    _write_immutable_json(path, payload)
    return path


def _compatibility_contract(context: RNRunContext) -> dict[str, Any]:
    spec = context.resolved.spec
    generated = context.generated_inputs
    if generated is None:
        raise RNP1ValidationError("RunContext lacks generated input bindings")
    truth = {
        key: value
        for key, value in generated.truth_policy.items()
        if key not in {"study_id", "policy_sha256"}
    }
    profiles = [
        {
            "agent_id": row["agent_id"],
            "profile": row["profile"],
        }
        for row in generated.public_profile_registry["profiles"]
    ]
    return {
        "source_tree_sha256": context.source_hashes["source_tree_sha256"],
        "dependency_tree_sha256": context.source_hashes["dependency_tree_sha256"],
        "baseline_commit": spec.baseline_commit,
        "design_version": spec.design_version,
        "agent_ids": list(context.agent_ids),
        "cohort_registry_sha256": spec.cohort_registry_sha256,
        "persona_snapshot_manifest_sha256": spec.persona_snapshot_manifest_sha256,
        "persona_depth_manifest_sha256": spec.persona_depth_manifest_sha256,
        "persona_assignment_policy": spec.persona_assignment_policy,
        "persona_renderer_sha256": spec.persona_renderer_sha256,
        "prompt_bundle_sha256": spec.prompt_bundle_sha256,
        "belief_limits": dict(spec.belief_limits),
        "condition_treatments": {
            key: dict(value) for key, value in spec.condition_treatments.items()
        },
        "paired_condition_groups": [list(value) for value in spec.paired_condition_groups],
        "treatment_diff_allowlist": list(spec.treatment_diff_allowlist),
        "regime_policy_sha256": spec.regime_policy_sha256,
        "known_injection_registry_sha256": spec.known_injection_registry_sha256,
        "news_exposure_policy": dict(spec.news_exposure_policy),
        "news_exposure_policy_sha256": spec.news_exposure_policy_sha256,
        "community_policy": dict(spec.community_policy),
        "community_timing_policy": dict(spec.community_timing_policy),
        "context_window_policy": dict(spec.context_window_policy),
        "memory_policy": dict(spec.memory_policy),
        "trade_policy": spec.trade_policy.to_dict(),
        "call_policy": spec.call_policy.to_dict(),
        "study_seed": spec.study_seed,
        "seed_namespace": spec.seed_namespace,
        "retry_policy_sha256": spec.retry_policy_sha256,
        "runtime_policy_sha256": spec.runtime_policy_sha256,
        "evaluation_policy_sha256": spec.evaluation_policy_sha256,
        "public_profiles": profiles,
        "community_truth_policy": truth,
    }


def _validate_completed_checkpoint(context: RNRunContext) -> None:
    checkpoint = _read_json(
        context.run_dir / "rn_phase_checkpoint.json",
        label="P1 phase checkpoint",
    )
    _validate_completed_checkpoint_payload(context, checkpoint)


def _validate_completed_checkpoint_payload(
    context: RNRunContext,
    checkpoint: Mapping[str, Any],
) -> None:
    expected_phase_ids = [
        "bootstrap_ltb0",
        *[
            f"{event['event_id']}:scientific_turn"
            for event in context.event_schedule.events
        ],
        "finalize:right_censor_outcomes",
    ]
    completed = checkpoint.get("completed_phases")
    if (
        checkpoint.get("version") != "rn-paired-phase-v1"
        or checkpoint.get("manifest_sha256") != context.manifest_sha256
        or checkpoint.get("inflight_phase") is not None
        or checkpoint.get("paused_phase") is not None
        or not isinstance(completed, list)
        or [item.get("phase_id") for item in completed if isinstance(item, Mapping)]
        != expected_phase_ids
        or len(completed) != len(expected_phase_ids)
    ):
        raise RNP1ValidationError("P1 phase checkpoint is not exactly complete")
    for index, item in enumerate(completed):
        if not isinstance(item, Mapping):
            raise RNP1ValidationError("P1 completed phase entry is malformed")
        expected_work_items = (
            2 if index == len(completed) - 1 else 2 * len(context.agent_ids)
        )
        if item.get("work_item_count") != expected_work_items:
            raise RNP1ValidationError("P1 completed phase has a wrong paired work-item count")


def _pre_recovery_identity(
    checkpoint: Mapping[str, Any],
    *,
    manifest_sha256: str,
) -> tuple[str, str]:
    if (
        checkpoint.get("version") != "rn-paired-phase-v1"
        or checkpoint.get("manifest_sha256") != manifest_sha256
        or not isinstance(checkpoint.get("completed_phases", []), list)
    ):
        raise RNP1ValidationError("Pre-resume checkpoint is not bound to the P1 manifest")
    paused = checkpoint.get("paused_phase")
    inflight = checkpoint.get("inflight_phase")
    if isinstance(paused, Mapping) and inflight is None:
        phase_id = paused.get("phase_id")
        mode = "retry_paused_phase"
    elif isinstance(inflight, Mapping) and inflight.get("state") == "commit_decided":
        phase_id = inflight.get("phase_id")
        mode = "finish_commit_decided"
    elif isinstance(inflight, Mapping) and inflight.get("state", "running") == "running":
        phase_id = inflight.get("phase_id")
        mode = "restore_interrupted_running_phase"
    else:
        raise RNP1ValidationError(
            "P1 resume proof requires an actual paused or interrupted phase"
        )
    if not isinstance(phase_id, str) or not phase_id:
        raise RNP1ValidationError("P1 pre-resume checkpoint has no phase identity")
    return mode, phase_id


def _validate_recovery_evidence(context: RNRunContext) -> str:
    path = context.run_dir / P1_RECOVERY_EVIDENCE_FILENAME
    evidence = _read_json(path, label="P1 recovery evidence")
    expected_keys = {
        "artifact_type",
        "version",
        "run_id",
        "resolved_manifest_sha256",
        "recovery_mode",
        "recovered_phase_id",
        "pre_resume_checkpoint_sha256",
        "pre_resume_completed_phase_ids_sha256",
        "post_resume_checkpoint_sha256",
        "post_resume_completed_phase_ids_sha256",
    }
    checkpoint_path = context.run_dir / "rn_phase_checkpoint.json"
    checkpoint = _read_json(checkpoint_path, label="P1 final phase checkpoint")
    if (
        set(evidence) != expected_keys
        or evidence.get("artifact_type") != "rn_p1_recovery_evidence"
        or evidence.get("version") != _P1_RECOVERY_VERSION
        or evidence.get("run_id") != context.run_id
        or evidence.get("resolved_manifest_sha256") != context.manifest_sha256
        or evidence.get("recovery_mode")
        not in {
            "retry_paused_phase",
            "finish_commit_decided",
            "restore_interrupted_running_phase",
        }
        or evidence.get("post_resume_checkpoint_sha256") != file_sha256(checkpoint_path)
        or evidence.get("post_resume_completed_phase_ids_sha256")
        != canonical_sha256(
            [str(item["phase_id"]) for item in checkpoint["completed_phases"]]
        )
    ):
        raise RNP1ValidationError("P1 recovery evidence does not bind the completed checkpoint")
    for key in (
        "pre_resume_checkpoint_sha256",
        "pre_resume_completed_phase_ids_sha256",
        "post_resume_checkpoint_sha256",
        "post_resume_completed_phase_ids_sha256",
    ):
        value = evidence.get(key)
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise RNP1ValidationError(f"P1 recovery evidence has invalid {key}")
    return str(evidence["recovery_mode"])


def _validate_community_boundary(context: RNRunContext) -> dict[str, int]:
    generated = context.generated_inputs
    if generated is None:
        raise RNP1ValidationError("P1 has no generated community profile registry")
    services = {
        condition_id: RNCommunityService.from_resolved_manifest(
            context.open_store(condition_id),
            context.resolved,
            public_profiles=generated.public_profiles,
        )
        for condition_id in RN_CONDITIONS
    }
    first_date, second_date = context.resolved.trading_dates
    events = tuple(context.event_schedule.events)
    first_pm = next(
        event for event in events if event["date"] == first_date and event["subturn"] == "pm"
    )
    second_am = next(
        event for event in events if event["date"] == second_date and event["subturn"] == "am"
    )
    first_phase = next(
        phase
        for phase in context.resolved.calendar.community_phases
        if phase.after_event_id == first_pm["event_id"]
    )

    with connect(context.condition_db_paths[RN_COMM_OFF], read_only=True) as connection:
        off_exposures = _scalar_count(
            connection,
            """
            SELECT COUNT(*) FROM agent_exposures
            WHERE run_id = ? AND condition_id = ?
              AND channel IN ('community_best', 'community_selected')
            """,
            (context.run_id, RN_COMM_OFF),
        )
        if off_exposures:
            raise RNP1ValidationError("P1 RN_COMM_OFF contains community exposure")
        off_rows = connection.execute(
            """
            SELECT event_id, stage, payload_json
            FROM observation_events
            WHERE run_id = ? AND condition_id = ? AND stage LIKE 'community_%'
            ORDER BY event_id, stage
            """,
            (context.run_id, RN_COMM_OFF),
        ).fetchall()
        if len(off_rows) != len(context.resolved.calendar.community_phases):
            raise RNP1ValidationError("P1 RN_COMM_OFF lacks exact PM no-op checkpoints")
        for row in off_rows:
            payload = _strict_json_text(row["payload_json"], label="OFF community checkpoint")
            if (
                not str(row["stage"]).startswith("community_checkpoint:")
                or payload.get("mode") != "off"
                or payload.get("post_count") != 0
                or payload.get("selected_exposure_count") != 0
                or payload.get("best_post_ids") != []
                or payload.get("best_status") != "no_op"
            ):
                raise RNP1ValidationError("P1 RN_COMM_OFF community no-op is malformed")

    with connect(context.condition_db_paths[RN_COMM_ON], read_only=True) as connection:
        schedule = _one_observation(
            connection,
            run_id=context.run_id,
            condition_id=RN_COMM_ON,
            event_id=str(first_pm["event_id"]),
            stage=f"community_best_schedule:{first_phase.phase_id}",
        )
        best_posts = schedule.get("best_posts")
        if (
            schedule.get("status") != "scheduled"
            or not isinstance(best_posts, list)
            or not best_posts
            or len(best_posts) > int(context.resolved.spec.community_policy["best_k"])
            or schedule.get("audience_agent_ids") != list(context.agent_ids)
        ):
            raise RNP1ValidationError(
                "P1 must contain a non-empty first-PM Best schedule for the full cohort"
            )
        best_ids = tuple(str(post.get("post_id")) for post in best_posts)
        if len(best_ids) != len(set(best_ids)) or any(
            not isinstance(post.get("body"), str) or not post["body"]
            for post in best_posts
        ):
            raise RNP1ValidationError("P1 Best schedule lacks unique frozen full bodies")
        delivered = connection.execute(
            """
            SELECT agent_id, event_id, root_id, status, metadata_json
            FROM agent_exposures
            WHERE run_id = ? AND condition_id = ? AND channel = 'community_best'
              AND status = 'delivered'
            ORDER BY agent_id, root_id
            """,
            (context.run_id, RN_COMM_ON),
        ).fetchall()
        expected_delivered = {
            (agent_id, str(second_am["event_id"]), post_id)
            for agent_id in context.agent_ids
            for post_id in best_ids
        }
        observed_delivered = {
            (str(row["agent_id"]), str(row["event_id"]), str(row["root_id"]))
            for row in delivered
        }
        if observed_delivered != expected_delivered:
            raise RNP1ValidationError(
                "P1 first-PM Best was not delivered exactly once to all agents at second-day AM"
            )
        for row in delivered:
            metadata = _strict_json_text(
                row["metadata_json"], label="P1 Best exposure metadata"
            )
            if (
                metadata.get("content_level") != "full_body"
                or metadata.get("visible_from_event_id") != second_am["event_id"]
                or not isinstance(metadata.get("full_body"), str)
                or not metadata["full_body"]
            ):
                raise RNP1ValidationError("P1 Best exposure lost its next-AM full-body boundary")
        depth_zero = {
            persona.agent_id
            for persona in context.personas.personas.values()
            if persona.news_depth == 0
        }
        if not depth_zero:
            raise RNP1ValidationError("P1 cohort has no Depth-0 passive-exposure agents")
        selected_depth_zero = _scalar_count(
            connection,
            """
            SELECT COUNT(*) FROM agent_exposures
            WHERE run_id = ? AND condition_id = ? AND channel = 'community_selected'
              AND agent_id IN ({})
            """.format(",".join("?" for _ in depth_zero)),
            (context.run_id, RN_COMM_ON, *sorted(depth_zero)),
        )
        if selected_depth_zero:
            raise RNP1ValidationError("P1 Depth-0 agent received a selective community exposure")

    on_service = services[RN_COMM_ON]
    for agent_id in context.agent_ids:
        payloads = on_service.interpretation_payloads(
            agent_id=agent_id,
            event_id=str(second_am["event_id"]),
        )
        if not payloads:
            raise RNP1ValidationError("P1 next-AM interpretation payload is missing")
        if agent_id in depth_zero and any(
            payload.get("exposure_channel") != "best_only_body" for payload in payloads
        ):
            raise RNP1ValidationError("P1 Depth-0 payload contains non-Best community content")
        on_service.recorded_claims_for_agent(
            agent_id=agent_id,
            event_id=str(second_am["event_id"]),
        )
    return {
        "off_community_exposure_count": off_exposures,
        "on_first_pm_best_post_count": len(best_ids),
        "on_next_am_best_delivery_count": len(delivered),
        "on_next_am_interpretation_agent_count": len(context.agent_ids),
        "depth_zero_agent_count": len(depth_zero),
    }


def _one_observation(
    connection: Any,
    *,
    run_id: str,
    condition_id: str,
    event_id: str,
    stage: str,
) -> dict[str, Any]:
    rows = connection.execute(
        """
        SELECT payload_json FROM observation_events
        WHERE run_id = ? AND condition_id = ? AND event_id = ? AND stage = ?
        """,
        (run_id, condition_id, event_id, stage),
    ).fetchall()
    if len(rows) != 1:
        raise RNP1ValidationError(f"P1 observation is missing or duplicated: {stage}")
    return _strict_json_text(rows[0]["payload_json"], label=stage)


def _scalar_count(connection: Any, sql: str, parameters: tuple[Any, ...]) -> int:
    row = connection.execute(sql, parameters).fetchone()
    if row is None:
        raise RNP1ValidationError("P1 count query returned no row")
    return int(row[0])


def _strict_json_text(value: Any, *, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError as exc:
        raise RNP1ValidationError(f"{label} is not JSON") from exc
    if not isinstance(parsed, dict):
        raise RNP1ValidationError(f"{label} must be a JSON object")
    return parsed


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RNP1ValidationError(f"{label} is missing or not a regular file")
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RNP1ValidationError(f"{label} is invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise RNP1ValidationError(f"{label} must contain one JSON object")
    return parsed


def _write_immutable_json(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    ) + "\n"
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise RNP1ValidationError(f"P1 evidence destination is unsafe: {path}")
        if path.read_text(encoding="utf-8") != encoded:
            raise RNP1ValidationError(f"P1 evidence already exists with different content: {path}")
        return
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


__all__ = [
    "P1_RECOVERY_EVIDENCE_FILENAME",
    "P1_REQUIRED_AGENT_COUNT",
    "P1_REQUIRED_DECISION_EVENT_COUNT",
    "P1_REQUIRED_TRADING_DAY_COUNT",
    "RNP1CanaryValidation",
    "RNP1ValidationError",
    "assert_p1_candidate_context",
    "validate_p1_canary_for_full_run",
    "write_p1_recovery_evidence",
]
