"""Manifest-bound construction for the RN paired execution graph.

This module intentionally separates *construction* from a paid launch.  It
opens only run-local databases and journals, consumes the policy frozen in
``RNRunContext``, and accepts injected stage/community providers for local
tests.  A caller must explicitly satisfy the remaining live-run gates before
it can ask for a paper execution graph.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from twinmarket_kr.experiment_runtime import file_sha256
from twinmarket_kr.rn_ab.call_policy import (
    RN_REASONING_AUDIT_FIELDS,
    RN_STRICT_RESPONSE_FORMAT,
    RN_STRICT_TEMPERATURE,
    StrictCallPolicy,
    validate_reasoning_audit,
)
from twinmarket_kr.rn_ab.community_lifecycle import RNCommunityLifecycleAdapter
from twinmarket_kr.rn_ab.evidence_provider import RNRunContextEvidenceProvider
from twinmarket_kr.rn_ab.memory import RN_STAGE_SCHEMA_VERSIONS
from twinmarket_kr.rn_ab.run_context import RNRunContext, RNRunContextError
from twinmarket_kr.rn_ab.runner import RNPairedRunner
from twinmarket_kr.rn_ab.spec import RN_CONDITIONS, canonical_json_bytes
from twinmarket_kr.rn_ab.stage_adapter import (
    RN_STAGE_MAX_TOKENS_V1,
    RNBeliefLimits,
    RNStageAdapter,
    StageModel,
    StrictOpenRouterStageModel,
)


class RNExecutionFactoryError(RuntimeError):
    """The sealed context or a supplied execution dependency is unsafe."""


_RUN_LOCAL_AUDIT_FILENAME = "openrouter_attempts.jsonl"
_CANARY_PLAN_FILENAME = "REASONING_OFF_TELEMETRY_PLAN.json"
_CANARY_EVIDENCE_FILENAME = "REASONING_OFF_TELEMETRY_EVIDENCE.json"
_CANARY_PLAN_VERSION = "rn-reasoning-off-telemetry-plan-v2"
_CANARY_EVIDENCE_VERSION = "rn-reasoning-off-telemetry-evidence-v2"
_MODEL_STAGES = tuple(RN_STAGE_SCHEMA_VERSIONS)
_RN_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


@dataclass(frozen=True)
class RNExecutionPrerequisites:
    """Explicit status of conditions that local construction cannot invent."""

    missing: tuple[str, ...]

    @property
    def ready_for_paid_execution(self) -> bool:
        return not self.missing

    def require_ready(self) -> None:
        if self.missing:
            raise RNExecutionFactoryError(
                "RN paid execution remains NO-GO; missing=" + ", ".join(self.missing)
            )


@dataclass(frozen=True)
class RNCanaryPreparation:
    """Offline artifacts needed immediately before a future live canary."""

    plan_path: Path
    plan_sha256: str
    expected_live_request_count: int


@dataclass(frozen=True)
class RNCanaryEvidence:
    """Validated live evidence; this type is never created from a dry run."""

    evidence_path: Path
    evidence_sha256: str
    summaries: Mapping[str, Mapping[str, int]]


@dataclass(frozen=True)
class RNReasoningOffTelemetryRunner:
    """Deliberately invoked paid boundary restricted to the planned event.

    Construction performs no API call.  ``run()`` is intentionally not
    exposed by the offline CLI; a future operator must invoke it explicitly
    after separately deciding to spend the planned request budget.
    """

    _context: RNRunContext
    _runner: RNPairedRunner
    plan_path: Path
    event_id: str

    async def run(self) -> RNCanaryEvidence:
        validate_reasoning_off_canary_plan(self._context, self.plan_path)
        # Preflight seeds LTB0 rows, but the coordinator still needs its
        # durable bootstrap phase before the first scientific event.  Keeping
        # that checkpoint in canonical order lets the same run resume into P1
        # or a full schedule without a special recovery path.
        await self._runner._bootstrap_phase()
        await self._runner.run_event(event_id=self.event_id)
        return capture_reasoning_off_canary_evidence(
            self._context,
            plan_path=self.plan_path,
        )


def strict_policy_from_context(context: RNRunContext) -> StrictCallPolicy:
    """Translate only the sealed StudySpec call policy into runtime controls."""

    _require_context(context)
    call = context.resolved.spec.call_policy
    if call.physical_http_attempts_per_phase_attempt != 1:
        raise RNExecutionFactoryError(
            "RN response journal owns retries; each phase attempt needs exactly one HTTP attempt"
        )
    policy = StrictCallPolicy(
        model=call.model,
        provider=call.provider,
        max_retries=call.physical_http_attempts_per_phase_attempt,
        concurrency=call.per_arm_max_concurrent_llm_calls,
        reasoning_effort=str(call.reasoning["effort"]),
        reasoning_exclude=bool(call.reasoning["exclude"]),
        allow_fallbacks=call.allow_provider_fallbacks,
        require_parameters=call.require_parameters,
    )
    policy.validate()
    if policy.as_request_policy() != call.http_request_policy():
        raise RNExecutionFactoryError("Runtime strict request policy differs from sealed StudySpec policy")
    return policy


def run_local_audit_path(context: RNRunContext, condition_id: str) -> Path:
    """Return the one audit file owned by a sealed run/condition pair."""

    _require_context(context)
    if condition_id not in RN_CONDITIONS:
        raise RNExecutionFactoryError(f"Unknown RN condition for audit path: {condition_id}")
    return context.run_dir / condition_id / _RUN_LOCAL_AUDIT_FILENAME


def prepare_reasoning_off_canary(context: RNRunContext) -> RNCanaryPreparation:
    """Write one deterministic, no-network plan for the first live event.

    The plan uses the complete sealed cohort, both arms, and all four model
    stages for the first decision event.  It authorizes nothing and cannot
    contact a model; a later operator must deliberately launch the paid
    canary through a separate execution path.
    """

    _require_context(context)
    path = context.run_dir / _CANARY_PLAN_FILENAME
    payload = _canary_plan_payload(context)
    _write_immutable_json(path, payload, label="canary plan")
    return RNCanaryPreparation(
        plan_path=path,
        plan_sha256=file_sha256(path),
        expected_live_request_count=int(
            payload["canary_scope"]["expected_physical_http_requests"]
        ),
    )


def validate_reasoning_off_canary_plan(
    context: RNRunContext,
    path: Path | str | None = None,
) -> dict[str, Any]:
    """Validate that a canary plan is exactly reproducible from RunContext."""

    _require_context(context)
    plan_path = _exact_run_artifact_path(
        context,
        path,
        filename=_CANARY_PLAN_FILENAME,
        label="canary plan",
    )
    payload = _read_exact_json_file(plan_path, label="canary plan")
    expected = _canary_plan_payload(context)
    if payload != expected:
        raise RNExecutionFactoryError(
            "Canary plan differs from the current sealed run context"
        )
    return payload


def capture_reasoning_off_canary_evidence(
    context: RNRunContext,
    *,
    plan_path: Path | str | None = None,
    output_path: Path | str | None = None,
) -> RNCanaryEvidence:
    """Validate real live rows and seal evidence; never synthesize a row.

    This function is intentionally unusable before a paid canary has produced
    the exact run-local JSONL rows.  It reads telemetry only and makes no
    model/network call.
    """

    plan = validate_reasoning_off_canary_plan(context, plan_path)
    actual_plan_path = _exact_run_artifact_path(
        context,
        plan_path,
        filename=_CANARY_PLAN_FILENAME,
        label="canary plan",
    )
    plan_sha256 = file_sha256(actual_plan_path)
    policy = strict_policy_from_context(context)
    expected_ids = _expected_canary_logical_ids(context)
    arm_evidence: dict[str, Any] = {}
    summaries: dict[str, Mapping[str, int]] = {}
    for condition_id in RN_CONDITIONS:
        path = run_local_audit_path(context, condition_id)
        rows = _read_run_local_audit(path)
        expected_context = _run_audit_context(context, condition_id)
        events_by_logical_id: dict[str, list[dict[str, Any]]] = {}
        for index, row in enumerate(rows, start=1):
            if row.get("audit_context") != expected_context:
                raise RNExecutionFactoryError(
                    f"Run-local audit context differs at {condition_id}:{index}"
                )
            logical_call_id = row.get("logical_call_id")
            if logical_call_id not in expected_ids[condition_id]:
                raise RNExecutionFactoryError(
                    f"Canary audit contains a non-canary logical call at {condition_id}:{index}"
                )
            events_by_logical_id.setdefault(str(logical_call_id), []).append(row)
        accepted_by_logical_id = _accepted_rows_by_logical_id(rows)
        missing = sorted(expected_ids[condition_id] - set(accepted_by_logical_id))
        if missing:
            raise RNExecutionFactoryError(
                f"Canary live telemetry is incomplete for {condition_id}; "
                f"missing_count={len(missing)}"
            )
        for logical_call_id, row in accepted_by_logical_id.items():
            _validate_canary_stage_row(row, logical_call_id=logical_call_id)
        try:
            summary = validate_reasoning_audit(rows, policy=policy)
        except Exception as exc:
            raise RNExecutionFactoryError(
                f"Canary reasoning audit failed for {condition_id}: {exc}"
            ) from exc
        if summary["success_count"] != len(expected_ids[condition_id]):
            raise RNExecutionFactoryError(
                f"Canary audit does not contain one acceptance per logical call for {condition_id}"
            )
        if summary["attempt_count"] != len(expected_ids[condition_id]):
            raise RNExecutionFactoryError(
                f"Canary audit did not complete in one provider attempt per logical call "
                f"for {condition_id}"
            )
        summaries[condition_id] = summary
        arm_evidence[condition_id] = {
            "audit_path": str(path.relative_to(context.run_dir)),
            "audit_file_sha256_at_capture": file_sha256(path),
            "expected_logical_call_count": len(expected_ids[condition_id]),
            "expected_logical_call_ids_sha256": _string_set_sha256(
                expected_ids[condition_id]
            ),
            "validated_event_sha256s_by_logical_call_id": {
                logical_call_id: [_canonical_sha256(row) for row in logical_rows]
                for logical_call_id, logical_rows in sorted(events_by_logical_id.items())
            },
            "summary": dict(summary),
        }
    evidence_payload = {
        "artifact_type": "rn_ab_reasoning_off_live_telemetry_evidence",
        "version": _CANARY_EVIDENCE_VERSION,
        "status": "live_telemetry_validated",
        "run_id": context.run_id,
        "resolved_manifest_sha256": context.manifest_sha256,
        "canary_plan": {
            "path": str(actual_plan_path.relative_to(context.run_dir)),
            "sha256": plan_sha256,
        },
        "canary_scope": dict(plan["canary_scope"]),
        "conditions": arm_evidence,
    }
    destination = _exact_run_artifact_path(
        context,
        output_path,
        filename=_CANARY_EVIDENCE_FILENAME,
        label="canary evidence",
    )
    _write_immutable_json(destination, evidence_payload, label="canary evidence")
    return RNCanaryEvidence(
        evidence_path=destination,
        evidence_sha256=file_sha256(destination),
        summaries=summaries,
    )


def validate_reasoning_off_canary_evidence(
    context: RNRunContext,
    path: Path | str | None = None,
) -> RNCanaryEvidence:
    """Revalidate sealed canary evidence against still-present raw telemetry."""

    _require_context(context)
    evidence_path = _exact_run_artifact_path(
        context,
        path,
        filename=_CANARY_EVIDENCE_FILENAME,
        label="canary evidence",
    )
    payload = _read_exact_json_file(evidence_path, label="canary evidence")
    expected_top_level = {
        "artifact_type",
        "version",
        "status",
        "run_id",
        "resolved_manifest_sha256",
        "canary_plan",
        "canary_scope",
        "conditions",
    }
    if set(payload) != expected_top_level:
        raise RNExecutionFactoryError("Canary evidence has an invalid exact schema")
    if (
        payload["artifact_type"] != "rn_ab_reasoning_off_live_telemetry_evidence"
        or payload["version"] != _CANARY_EVIDENCE_VERSION
        or payload["status"] != "live_telemetry_validated"
        or payload["run_id"] != context.run_id
        or payload["resolved_manifest_sha256"] != context.manifest_sha256
    ):
        raise RNExecutionFactoryError("Canary evidence is not bound to this sealed run")
    plan = validate_reasoning_off_canary_plan(context)
    plan_record = payload["canary_plan"]
    plan_path = context.run_dir / _CANARY_PLAN_FILENAME
    if (
        not isinstance(plan_record, dict)
        or set(plan_record) != {"path", "sha256"}
        or plan_record["path"] != _CANARY_PLAN_FILENAME
        or plan_record["sha256"] != file_sha256(plan_path)
        or payload["canary_scope"] != plan["canary_scope"]
    ):
        raise RNExecutionFactoryError("Canary evidence references a different canary plan")
    conditions = payload["conditions"]
    if not isinstance(conditions, dict) or set(conditions) != set(RN_CONDITIONS):
        raise RNExecutionFactoryError("Canary evidence must contain exactly both RN conditions")
    expected_ids = _expected_canary_logical_ids(context)
    policy = strict_policy_from_context(context)
    summaries: dict[str, Mapping[str, int]] = {}
    for condition_id in RN_CONDITIONS:
        record = conditions[condition_id]
        expected_record_fields = {
            "audit_path",
            "audit_file_sha256_at_capture",
            "expected_logical_call_count",
            "expected_logical_call_ids_sha256",
            "validated_event_sha256s_by_logical_call_id",
            "summary",
        }
        if not isinstance(record, dict) or set(record) != expected_record_fields:
            raise RNExecutionFactoryError(
                f"Canary evidence has an invalid condition record for {condition_id}"
            )
        expected_relative = f"{condition_id}/{_RUN_LOCAL_AUDIT_FILENAME}"
        if (
            record["audit_path"] != expected_relative
            or record["expected_logical_call_count"] != len(expected_ids[condition_id])
            or record["expected_logical_call_ids_sha256"]
            != _string_set_sha256(expected_ids[condition_id])
        ):
            raise RNExecutionFactoryError(
                f"Canary evidence expected-key contract differs for {condition_id}"
            )
        captured_hashes = record["validated_event_sha256s_by_logical_call_id"]
        if (
            not isinstance(captured_hashes, dict)
            or set(captured_hashes) != expected_ids[condition_id]
        ):
            raise RNExecutionFactoryError(
                f"Canary evidence row-digest set differs for {condition_id}"
            )
        raw_rows = _read_run_local_audit(run_local_audit_path(context, condition_id))
        current_by_id: dict[str, list[dict[str, Any]]] = {}
        for row in raw_rows:
            logical_call_id = row.get("logical_call_id")
            if logical_call_id in expected_ids[condition_id]:
                current_by_id.setdefault(str(logical_call_id), []).append(row)
        if set(current_by_id) != expected_ids[condition_id]:
            raise RNExecutionFactoryError(
                f"Current audit no longer contains all sealed canary rows for {condition_id}"
            )
        for logical_call_id, logical_rows in current_by_id.items():
            if [_canonical_sha256(row) for row in logical_rows] != captured_hashes[
                logical_call_id
            ]:
                raise RNExecutionFactoryError(
                    f"Canary audit events changed after capture: {logical_call_id}"
                )
            for row in logical_rows:
                if row.get("audit_context") != _run_audit_context(context, condition_id):
                    raise RNExecutionFactoryError(
                        f"Canary audit context changed after capture: {logical_call_id}"
                    )
        canary_rows = [
            row
            for logical_call_id in sorted(current_by_id)
            for row in current_by_id[logical_call_id]
        ]
        accepted_by_logical_id = _accepted_rows_by_logical_id(canary_rows)
        if set(accepted_by_logical_id) != expected_ids[condition_id]:
            raise RNExecutionFactoryError(
                f"Current audit has a wrong acceptance set for {condition_id}"
            )
        for logical_call_id, row in accepted_by_logical_id.items():
            _validate_canary_stage_row(row, logical_call_id=logical_call_id)
        try:
            summary = validate_reasoning_audit(
                canary_rows,
                policy=policy,
            )
        except Exception as exc:
            raise RNExecutionFactoryError(
                f"Sealed canary evidence no longer validates for {condition_id}: {exc}"
            ) from exc
        if record["summary"] != summary:
            raise RNExecutionFactoryError(
                f"Canary evidence summary differs for {condition_id}"
            )
        summaries[condition_id] = summary
    return RNCanaryEvidence(
        evidence_path=evidence_path,
        evidence_sha256=file_sha256(evidence_path),
        summaries=summaries,
    )


def validate_run_local_reasoning_audits(context: RNRunContext) -> dict[str, dict[str, int]]:
    """Validate strict telemetry without accepting a global or mixed-run audit.

    This checks evidence only; it never authorizes or launches paid work.  A
    future explicitly launched telemetry run must create one JSONL file per
    arm through :func:`build_live_stage_models`, then this function makes the
    evidence inspectable before any later execution gate is evaluated.
    """

    policy = strict_policy_from_context(context)
    summaries: dict[str, dict[str, int]] = {}
    for condition_id in RN_CONDITIONS:
        path = run_local_audit_path(context, condition_id)
        rows = _read_run_local_audit(path)
        expected_context = _run_audit_context(context, condition_id)
        for index, row in enumerate(rows, start=1):
            if row.get("audit_context") != expected_context:
                raise RNExecutionFactoryError(
                    f"Run-local audit context differs at {condition_id}:{index}"
                )
        try:
            summaries[condition_id] = validate_reasoning_audit(rows, policy=policy)
        except Exception as exc:
            raise RNExecutionFactoryError(
                f"Run-local reasoning audit failed for {condition_id}: {exc}"
            ) from exc
    return summaries


def validate_final_run_reasoning_audits(
    context: RNRunContext,
) -> dict[str, dict[str, int]]:
    """Bind every committed response digest to one experiment-acceptance row."""

    policy = strict_policy_from_context(context)
    expected_core_ids = _expected_full_core_logical_ids(context)
    summaries: dict[str, dict[str, int]] = {}
    for condition_id in RN_CONDITIONS:
        rows = _read_run_local_audit(run_local_audit_path(context, condition_id))
        expected_context = _run_audit_context(context, condition_id)
        accepted_by_id: dict[str, dict[str, Any]] = {}
        for index, row in enumerate(rows, start=1):
            if row.get("audit_context") != expected_context:
                raise RNExecutionFactoryError(
                    f"Final run audit context differs at {condition_id}:{index}"
                )
            if (
                row.get("audit_event") == "experiment_acceptance"
                and row.get("status") == "accepted"
            ):
                logical_call_id = str(row.get("logical_call_id") or "")
                if logical_call_id in accepted_by_id:
                    raise RNExecutionFactoryError(
                        f"Final run audit repeats experiment acceptance: {logical_call_id}"
                    )
                _validate_success_stage_budget(
                    row,
                    logical_call_id=logical_call_id,
                )
                accepted_by_id[logical_call_id] = row
        try:
            audit_summary = validate_reasoning_audit(rows, policy=policy)
            committed = context.open_journal(
                condition_id
            ).committed_accepted_response_digests()
        except Exception as exc:
            raise RNExecutionFactoryError(
                f"Final run reasoning handoff failed for {condition_id}: {exc}"
            ) from exc
        missing_core = sorted(expected_core_ids[condition_id] - set(committed))
        if missing_core:
            raise RNExecutionFactoryError(
                f"Final run journal is missing core model calls for {condition_id}; "
                f"missing_count={len(missing_core)}"
            )
        if set(accepted_by_id) != set(committed):
            missing_audit = sorted(set(committed) - set(accepted_by_id))
            uncommitted_audit = sorted(set(accepted_by_id) - set(committed))
            raise RNExecutionFactoryError(
                f"Final run live audit/journal mismatch for {condition_id}; "
                f"missing_audit={missing_audit}, uncommitted_audit={uncommitted_audit}"
            )
        mismatched_digests = sorted(
            logical_call_id
            for logical_call_id, journal_sha256 in committed.items()
            if accepted_by_id[logical_call_id].get("accepted_response_sha256")
            != journal_sha256
        )
        if mismatched_digests:
            raise RNExecutionFactoryError(
                f"Final run accepted-response digest differs from committed journal "
                f"for {condition_id}; mismatched={mismatched_digests}"
            )
        summaries[condition_id] = {
            **audit_summary,
            "committed_success_count": len(committed),
            "required_core_call_count": len(expected_core_ids[condition_id]),
        }
    return summaries


def execution_prerequisites(
    context: RNRunContext,
    *,
    community_lifecycle: RNCommunityLifecycleAdapter | None,
    canary_evidence_path: Path | str | None = None,
    p1_canary_run_dir: Path | str | None = None,
) -> RNExecutionPrerequisites:
    """Return, rather than hide, all known blockers to a paid RN run."""

    _require_context(context)
    missing: list[str] = []
    evidence_path = (
        Path(canary_evidence_path)
        if canary_evidence_path is not None
        else context.run_dir / _CANARY_EVIDENCE_FILENAME
    )
    if not evidence_path.exists():
        missing.append("validated_reasoning_off_live_telemetry")
    else:
        validate_reasoning_off_canary_evidence(context, evidence_path)
    # The telemetry subset cannot self-authorize a full run.  P1 is accepted
    # only by reopening and revalidating a separate completed two-day run.
    if p1_canary_run_dir is None:
        missing.append(
            "sealed_two_trading_day_p1_canary_spec_and_validated_community_boundary"
        )
    else:
        try:
            from twinmarket_kr.rn_ab.p1 import validate_p1_canary_for_full_run

            validate_p1_canary_for_full_run(
                context,
                p1_canary_run_dir=p1_canary_run_dir,
            )
        except Exception as exc:
            raise RNExecutionFactoryError(f"P1 canary validation failed: {exc}") from exc
    if community_lifecycle is None:
        missing.append("verified_run_local_community_lifecycle_for_RN_COMM_ON")
    else:
        _validate_lifecycle(context, community_lifecycle)
        provider = RNRunContextEvidenceProvider(context, community_lifecycle=community_lifecycle)
        missing.extend(provider.missing_full_run_dependencies())
    return RNExecutionPrerequisites(tuple(dict.fromkeys(missing)))


def build_paired_runner(
    context: RNRunContext,
    *,
    models: Mapping[str, StageModel],
    community_lifecycle: RNCommunityLifecycleAdapter,
    max_workers_per_arm: int | None = None,
    canary_evidence_path: Path | str | None = None,
    p1_canary_run_dir: Path | str | None = None,
) -> RNPairedRunner:
    """Build the only RN runner from one sealed context and injected models.

    This makes no network call, but it refuses to construct a callable paid
    graph while required community/D2/canary artifacts are absent.
    """

    _require_context(context)
    if set(models) != set(RN_CONDITIONS):
        raise RNExecutionFactoryError("Stage-model map must contain exactly both RN conditions")
    if not isinstance(community_lifecycle, RNCommunityLifecycleAdapter):
        raise RNExecutionFactoryError("Paper runner requires RNCommunityLifecycleAdapter, not an implicit no-op")
    if any(
        not isinstance(models[condition_id], StrictOpenRouterStageModel)
        for condition_id in RN_CONDITIONS
    ):
        raise RNExecutionFactoryError(
            "Paid RN runner requires strict OpenRouter stage models for both conditions"
        )
    _validate_live_model_bindings(context, models)
    _validate_lifecycle(context, community_lifecycle)
    prerequisites = execution_prerequisites(
        context,
        community_lifecycle=community_lifecycle,
        canary_evidence_path=canary_evidence_path,
        p1_canary_run_dir=p1_canary_run_dir,
    )
    prerequisites.require_ready()

    return _build_runner(
        context,
        models=models,
        community_lifecycle=community_lifecycle,
        max_workers_per_arm=max_workers_per_arm,
    )


def build_p1_canary_runner(
    context: RNRunContext,
    *,
    models: Mapping[str, StageModel],
    community_lifecycle: RNCommunityLifecycleAdapter,
    max_workers_per_arm: int | None = None,
    canary_evidence_path: Path | str | None = None,
) -> RNPairedRunner:
    """Build the separate exact P1 run without circularly requiring itself."""

    _require_context(context)
    try:
        from twinmarket_kr.rn_ab.p1 import assert_p1_candidate_context

        assert_p1_candidate_context(context)
    except Exception as exc:
        raise RNExecutionFactoryError(f"Invalid P1 candidate context: {exc}") from exc
    if set(models) != set(RN_CONDITIONS) or any(
        not isinstance(models[condition_id], StrictOpenRouterStageModel)
        for condition_id in RN_CONDITIONS
    ):
        raise RNExecutionFactoryError(
            "P1 canary requires strict OpenRouter stage models for both conditions"
        )
    _validate_live_model_bindings(context, models)
    if not isinstance(community_lifecycle, RNCommunityLifecycleAdapter):
        raise RNExecutionFactoryError("P1 canary requires the sealed RN community lifecycle")
    _validate_lifecycle(context, community_lifecycle)
    evidence_path = (
        Path(canary_evidence_path)
        if canary_evidence_path is not None
        else context.run_dir / _CANARY_EVIDENCE_FILENAME
    )
    validate_reasoning_off_canary_evidence(context, evidence_path)
    provider = RNRunContextEvidenceProvider(
        context,
        community_lifecycle=community_lifecycle,
    )
    missing = provider.missing_full_run_dependencies()
    if missing:
        raise RNExecutionFactoryError(
            "P1 canary is missing sealed runtime dependencies: " + ", ".join(missing)
        )
    return _build_runner(
        context,
        models=models,
        community_lifecycle=community_lifecycle,
        max_workers_per_arm=max_workers_per_arm,
    )


def build_reasoning_off_telemetry_runner(
    context: RNRunContext,
    *,
    models: Mapping[str, StageModel],
    community_lifecycle: RNCommunityLifecycleAdapter,
    plan_path: Path | str | None = None,
    max_workers_per_arm: int | None = None,
) -> RNReasoningOffTelemetryRunner:
    """Construct, but do not run, the first-event live telemetry boundary."""

    _require_context(context)
    if set(models) != set(RN_CONDITIONS) or any(
        not isinstance(models[condition_id], StrictOpenRouterStageModel)
        for condition_id in RN_CONDITIONS
    ):
        raise RNExecutionFactoryError(
            "Reasoning-off telemetry runner requires strict OpenRouter models for both arms"
        )
    if not isinstance(community_lifecycle, RNCommunityLifecycleAdapter):
        raise RNExecutionFactoryError(
            "Reasoning-off telemetry runner requires the sealed RN community lifecycle"
        )
    _validate_lifecycle(context, community_lifecycle)
    provider = RNRunContextEvidenceProvider(
        context,
        community_lifecycle=community_lifecycle,
    )
    missing_dependencies = provider.missing_full_run_dependencies()
    if missing_dependencies:
        raise RNExecutionFactoryError(
            "Reasoning-off telemetry runner is missing sealed runtime dependencies: "
            + ", ".join(missing_dependencies)
        )
    actual_plan_path = _exact_run_artifact_path(
        context,
        plan_path,
        filename=_CANARY_PLAN_FILENAME,
        label="canary plan",
    )
    plan = validate_reasoning_off_canary_plan(context, actual_plan_path)
    _validate_live_model_bindings(context, models)
    runner = _build_runner(
        context,
        models=models,
        community_lifecycle=community_lifecycle,
        max_workers_per_arm=max_workers_per_arm,
    )
    return RNReasoningOffTelemetryRunner(
        _context=context,
        _runner=runner,
        plan_path=actual_plan_path,
        event_id=str(plan["canary_scope"]["decision_event_id"]),
    )


def build_local_test_runner(
    context: RNRunContext,
    *,
    models: Mapping[str, StageModel],
    community_lifecycle: RNCommunityLifecycleAdapter,
    max_workers_per_arm: int | None = None,
) -> RNPairedRunner:
    """Build an explicitly local-only graph for deterministic test fixtures.

    This cannot be used to bypass live gates: every injected model must opt in
    to ``local_only=True``.  ``StrictOpenRouterStageModel`` deliberately lacks
    that marker and is rejected here.
    """

    if set(models) != set(RN_CONDITIONS):
        raise RNExecutionFactoryError("Stage-model map must contain exactly both RN conditions")
    for condition_id, model in models.items():
        if getattr(model, "local_only", None) is not True:
            raise RNExecutionFactoryError(
                f"Local test runner requires {condition_id} model.local_only=True"
            )
    _require_context(context)
    if not isinstance(community_lifecycle, RNCommunityLifecycleAdapter):
        raise RNExecutionFactoryError("Local test runner still requires the RN community lifecycle")
    _validate_lifecycle(context, community_lifecycle)
    return _build_runner(
        context,
        models=models,
        community_lifecycle=community_lifecycle,
        max_workers_per_arm=max_workers_per_arm,
    )


def _build_runner(
    context: RNRunContext,
    *,
    models: Mapping[str, StageModel],
    community_lifecycle: RNCommunityLifecycleAdapter,
    max_workers_per_arm: int | None,
) -> RNPairedRunner:

    policy = strict_policy_from_context(context)
    configured_workers = policy.concurrency
    if max_workers_per_arm is None:
        workers = configured_workers
    elif (
        isinstance(max_workers_per_arm, bool)
        or not isinstance(max_workers_per_arm, int)
        or max_workers_per_arm != configured_workers
    ):
        raise RNExecutionFactoryError(
            "max_workers_per_arm must equal the sealed model-policy concurrency"
        )
    else:
        workers = max_workers_per_arm

    adapters = {
        condition_id: RNStageAdapter(
            store=context.open_store(condition_id),
            journal=context.open_journal(condition_id),
            prompt_bundle=context.prompt_bundle,
            personas=context.personas,
            event_schedule=context.event_schedule,
            stage_inputs=context.stage_inputs,
            model=models[condition_id],
            call_policy=policy,
            belief_limits=RNBeliefLimits.from_mapping(context.belief_limits),
            study_seed=context.resolved.spec.study_seed,
            seed_namespace=context.resolved.spec.seed_namespace,
        )
        for condition_id in RN_CONDITIONS
    }
    evidence = RNRunContextEvidenceProvider(context, community_lifecycle=community_lifecycle)
    return RNPairedRunner(
        run_dir=context.run_dir,
        adapters=adapters,
        agent_ids=context.agent_ids,
        event_schedule=context.event_schedule,
        evidence_provider=evidence,
        community_lifecycle=community_lifecycle,
        max_workers_per_arm=workers,
    )


def build_live_stage_models(context: RNRunContext) -> Mapping[str, StrictOpenRouterStageModel]:
    """Construct strict per-arm OpenRouter boundaries without issuing a call.

    The run-specific namespaces make the manifest's per-arm concurrency cap
    effective at the physical HTTP boundary too.  This function intentionally
    does not run the required live reasoning-off canary.
    """

    _require_context(context)
    policy = strict_policy_from_context(context)
    from twinmarket_kr.llm.client import OpenRouterClient

    models: dict[str, StrictOpenRouterStageModel] = {}
    for condition_id in RN_CONDITIONS:
        client = OpenRouterClient(
            base_url=_RN_OPENROUTER_BASE_URL,
            model=policy.model,
            max_retries=policy.max_retries,
            concurrency_limit=policy.concurrency,
            slot_namespace=f"rn-ab:{context.run_id}:{condition_id}:{context.manifest_sha256}",
            audit_path=run_local_audit_path(context, condition_id),
            audit_context=_run_audit_context(context, condition_id),
        )
        if client.is_offline:
            raise RNExecutionFactoryError("RN paper path rejects offline OpenRouter clients")
        if client.model != policy.model:
            raise RNExecutionFactoryError("OpenRouter client model differs from the sealed StudySpec")
        if client.max_retries != 1 or client.concurrency_limit != policy.concurrency:
            raise RNExecutionFactoryError("OpenRouter retry/concurrency differs from the sealed model policy")
        models[condition_id] = StrictOpenRouterStageModel(client, policy=policy)
    _validate_live_model_bindings(context, models)
    return models


def _run_audit_context(context: RNRunContext, condition_id: str) -> dict[str, str]:
    return {
        "artifact": "rn_ab_strict_openrouter_attempt",
        "condition_id": condition_id,
        "manifest_sha256": context.manifest_sha256,
        "run_id": context.run_id,
    }


def _validate_live_model_bindings(
    context: RNRunContext,
    models: Mapping[str, StageModel],
) -> None:
    """Reject endpoint/audit/slot drift before the first paid request."""

    if set(models) != set(RN_CONDITIONS):
        raise RNExecutionFactoryError("Live model map must contain exactly both RN arms")
    policy = strict_policy_from_context(context)
    for condition_id in RN_CONDITIONS:
        model = models[condition_id]
        if not isinstance(model, StrictOpenRouterStageModel) or model.policy != policy:
            raise RNExecutionFactoryError(
                f"Live model policy/type differs for {condition_id}"
            )
        client = model.client
        expected_slot = (
            f"rn-ab:{context.run_id}:{condition_id}:{context.manifest_sha256}"
        )
        if (
            getattr(client, "is_offline", True)
            or getattr(client, "model", None) != policy.model
            or getattr(client, "max_retries", None) != 1
            or getattr(client, "concurrency_limit", None) != policy.concurrency
            or getattr(client, "base_url", None) != _RN_OPENROUTER_BASE_URL
            or getattr(client, "slot_namespace", None) != expected_slot
            or Path(getattr(client, "audit_path", ""))
            != run_local_audit_path(context, condition_id)
            or getattr(client, "audit_context", None)
            != _run_audit_context(context, condition_id)
        ):
            raise RNExecutionFactoryError(
                f"Live OpenRouter endpoint/audit/slot binding differs for {condition_id}"
            )


def _read_run_local_audit(path: Path) -> list[dict[str, Any]]:
    """Read a sealed-run audit conservatively; empty/malformed evidence fails."""

    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise RNExecutionFactoryError(f"Run-local reasoning audit is missing: {path}") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise RNExecutionFactoryError(f"Run-local reasoning audit is not a regular file: {path}")
    rows: list[dict[str, Any]] = []

    def exact_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        parsed: dict[str, Any] = {}
        for key, value in pairs:
            if key in parsed:
                raise ValueError(f"duplicate JSON key: {key}")
            parsed[key] = value
        return parsed

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-standard JSON constant: {value}")

    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                payload = line.strip()
                if not payload:
                    continue
                parsed = json.loads(
                    payload,
                    object_pairs_hook=exact_object,
                    parse_constant=reject_constant,
                )
                if not isinstance(parsed, dict):
                    raise RNExecutionFactoryError(
                        f"Run-local reasoning audit has a non-object row at {path}:{line_number}"
                    )
                rows.append(parsed)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RNExecutionFactoryError(f"Cannot read run-local reasoning audit: {path}") from exc
    if not rows:
        raise RNExecutionFactoryError(f"Run-local reasoning audit is empty: {path}")
    return rows


def _canary_plan_payload(context: RNRunContext) -> dict[str, Any]:
    policy = strict_policy_from_context(context)
    first_event = context.event_schedule.events[0]
    expected_ids = _expected_canary_logical_ids(context)
    per_condition = len(next(iter(expected_ids.values())))
    source_snapshot_sha256 = context.source_hashes.get("snapshot_sha256")
    if not isinstance(source_snapshot_sha256, str):
        raise RNExecutionFactoryError("Run context has no sealed source snapshot hash")
    return {
        "artifact_type": "rn_ab_reasoning_off_telemetry_plan",
        "version": _CANARY_PLAN_VERSION,
        "mode": "plan_only_no_network_no_paid_api",
        "execution_authorized": False,
        "run_id": context.run_id,
        "resolved_manifest_sha256": context.manifest_sha256,
        "source_snapshot_sha256": source_snapshot_sha256,
        "prompt_bundle_sha256": context.prompt_bundle.canonical_sha256,
        "call_policy": {
            **policy.as_dict(),
            "final_request_policy": policy.as_request_policy(),
            "temperature": RN_STRICT_TEMPERATURE,
            "response_format": dict(RN_STRICT_RESPONSE_FORMAT),
        },
        "canary_scope": {
            "classification": (
                "reasoning_off_telemetry_only_not_final_two_trading_day_p1_canary"
            ),
            "conditions": list(RN_CONDITIONS),
            "agent_count": len(context.agent_ids),
            "agent_ids_sha256": _string_sequence_sha256(context.agent_ids),
            "decision_event_id": str(first_event["event_id"]),
            "decision_turn": int(first_event["turn"]),
            "decision_date": str(first_event["date"]),
            "model_stages": list(_MODEL_STAGES),
            "stage_schema_versions": {
                stage: RN_STAGE_SCHEMA_VERSIONS[stage] for stage in _MODEL_STAGES
            },
            "stage_max_tokens": {
                stage: RN_STAGE_MAX_TOKENS_V1[stage] for stage in _MODEL_STAGES
            },
            "expected_logical_calls_per_condition": per_condition,
            "expected_physical_http_requests": per_condition * len(RN_CONDITIONS),
            "expected_logical_call_ids_sha256_by_condition": {
                condition_id: _string_set_sha256(expected_ids[condition_id])
                for condition_id in RN_CONDITIONS
            },
        },
        "expected_audit": {
            "format": "append_only_provider_attempt_and_experiment_acceptance_events",
            "exact_row_fields": sorted(RN_REASONING_AUDIT_FIELDS),
            "paths": {
                condition_id: str(
                    run_local_audit_path(context, condition_id).relative_to(context.run_dir)
                )
                for condition_id in RN_CONDITIONS
            },
            "success_contract": {
                "provider_status": "provider_returned",
                "acceptance_status": "accepted",
                "requested_and_returned_model_equal_sealed_model": True,
                "returned_provider_equal_sealed_provider": True,
                "reasoning_tokens": 0,
                "response_reasoning_present": False,
                "finish_reason": "stop",
                "provider_request_id_required": True,
                "structured_usage_required": True,
                "one_acceptance_per_expected_logical_call": True,
                "accepted_response_sha256_exactly_bound_to_provider_attempt": True,
            },
        },
        "readiness": {
            "status": "NO_GO_LIVE_TELEMETRY_MISSING",
            "blocking_requirement": "real_paid_live_canary_must_be_run_separately",
            "does_not_satisfy": (
                "final_P1_two_trading_day_U4_100_agent_community_boundary_canary"
            ),
            "network_requests_made_by_plan": 0,
            "paid_api_calls_made_by_plan": 0,
        },
    }


def _expected_canary_logical_ids(
    context: RNRunContext,
) -> dict[str, set[str]]:
    event_id = str(context.event_schedule.events[0]["event_id"])
    result: dict[str, set[str]] = {}
    for condition_id in RN_CONDITIONS:
        result[condition_id] = {
            "|".join(
                (
                    context.run_id,
                    condition_id,
                    agent_id,
                    event_id,
                    stage,
                    RN_STAGE_SCHEMA_VERSIONS[stage],
                )
            )
            for agent_id in context.agent_ids
            for stage in _MODEL_STAGES
        }
    return result


def _expected_full_core_logical_ids(
    context: RNRunContext,
) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for condition_id in RN_CONDITIONS:
        result[condition_id] = {
            "|".join(
                (
                    context.run_id,
                    condition_id,
                    agent_id,
                    str(event["event_id"]),
                    stage,
                    RN_STAGE_SCHEMA_VERSIONS[stage],
                )
            )
            for agent_id in context.agent_ids
            for event in context.event_schedule.events
            for stage in _MODEL_STAGES
        }
    return result


def _validate_canary_stage_row(
    row: Mapping[str, Any],
    *,
    logical_call_id: str,
) -> None:
    fields = logical_call_id.split("|")
    if len(fields) != 6 or fields[4] not in RN_STAGE_SCHEMA_VERSIONS:
        raise RNExecutionFactoryError(
            f"Canary logical-call ID is not one of the four core stages: {logical_call_id}"
        )
    _validate_success_stage_budget(row, logical_call_id=logical_call_id)


def _validate_success_stage_budget(
    row: Mapping[str, Any],
    *,
    logical_call_id: str,
) -> None:
    fields = logical_call_id.split("|")
    if len(fields) != 6:
        raise RNExecutionFactoryError(f"Logical-call ID has an invalid shape: {logical_call_id}")
    stage, schema_version = fields[4], fields[5]
    schema_versions = dict(RN_STAGE_SCHEMA_VERSIONS)
    max_tokens = dict(RN_STAGE_MAX_TOKENS_V1)
    try:
        from twinmarket_kr.rn_ab.community_provider import (
            COMMUNITY_CALL_MAX_TOKENS,
            COMMUNITY_CALL_SCHEMA_VERSIONS,
        )

        schema_versions.update(COMMUNITY_CALL_SCHEMA_VERSIONS)
        max_tokens.update(COMMUNITY_CALL_MAX_TOKENS)
    except ImportError as exc:  # pragma: no cover - paper package is complete.
        raise RNExecutionFactoryError(
            "Cannot load the sealed community call-policy registry"
        ) from exc
    if stage not in schema_versions:
        raise RNExecutionFactoryError(f"Logical-call ID has an unknown stage: {logical_call_id}")
    if schema_version != schema_versions[stage]:
        raise RNExecutionFactoryError(
            f"Logical-call ID has a stale schema version: {logical_call_id}"
        )
    if row.get("label") != "rn_ab_stage":
        raise RNExecutionFactoryError(
            f"Audit row has an unexpected label: {logical_call_id}"
        )
    if row.get("max_tokens") != max_tokens[stage]:
        raise RNExecutionFactoryError(
            f"Audit row has a wrong response budget: {logical_call_id}"
        )
    if (
        row.get("audit_event") != "experiment_acceptance"
        or row.get("status") != "accepted"
    ):
        raise RNExecutionFactoryError(
            f"Audit row is not an experiment acceptance: {logical_call_id}"
        )


def _accepted_rows_by_logical_id(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    accepted: dict[str, dict[str, Any]] = {}
    for row in rows:
        if (
            row.get("audit_event") != "experiment_acceptance"
            or row.get("status") != "accepted"
        ):
            continue
        logical_call_id = str(row.get("logical_call_id") or "")
        if logical_call_id in accepted:
            raise RNExecutionFactoryError(
                f"Audit repeats experiment acceptance: {logical_call_id}"
            )
        accepted[logical_call_id] = dict(row)
    return accepted


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _string_sequence_sha256(values: Any) -> str:
    return _canonical_sha256([str(value) for value in values])


def _string_set_sha256(values: Any) -> str:
    return _canonical_sha256(sorted(str(value) for value in values))


def _exact_run_artifact_path(
    context: RNRunContext,
    supplied: Path | str | None,
    *,
    filename: str,
    label: str,
) -> Path:
    expected = context.run_dir / filename
    candidate = Path(supplied) if supplied is not None else expected
    if candidate.resolve(strict=False) != expected.resolve(strict=False):
        raise RNExecutionFactoryError(
            f"{label} must use the exact run-local path: {expected}"
        )
    return expected


def _read_exact_json_file(path: Path, *, label: str) -> dict[str, Any]:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise RNExecutionFactoryError(f"{label} is missing: {path}") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise RNExecutionFactoryError(f"{label} is not a regular file: {path}")

    def exact_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        parsed: dict[str, Any] = {}
        for key, value in pairs:
            if key in parsed:
                raise ValueError(f"duplicate JSON key: {key}")
            parsed[key] = value
        return parsed

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-standard JSON constant: {value}")

    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=exact_object,
            parse_constant=reject_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RNExecutionFactoryError(f"Cannot read canonical {label}: {path}") from exc
    if not isinstance(payload, dict):
        raise RNExecutionFactoryError(f"{label} must contain one JSON object")
    return payload


def _write_immutable_json(path: Path, payload: Mapping[str, Any], *, label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    ) + "\n"
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise RNExecutionFactoryError(f"{label} destination is not a regular file: {path}")
        if path.read_text(encoding="utf-8") != encoded:
            raise RNExecutionFactoryError(f"{label} already exists with different content: {path}")
        return
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _require_context(context: Any) -> None:
    if not isinstance(context, RNRunContext):
        raise RNExecutionFactoryError("Execution factory requires a validated RNRunContext")
    try:
        context.assert_execution_source_tree()
    except RNRunContextError as exc:
        raise RNExecutionFactoryError(f"Execution source provenance differs from preflight: {exc}") from exc


def _validate_lifecycle(context: RNRunContext, lifecycle: RNCommunityLifecycleAdapter) -> None:
    try:
        # Reusing the concrete evidence provider's strict namespace checks
        # avoids a second, weaker comparison of community services to context.
        RNRunContextEvidenceProvider(context, community_lifecycle=lifecycle)
    except Exception as exc:
        raise RNExecutionFactoryError(f"Community lifecycle differs from the sealed run context: {exc}") from exc


def _cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare or validate RN reasoning-off telemetry artifacts "
            "(not the final P1 canary). This command never calls an API."
        )
    )
    parser.add_argument("--run-dir", required=True, type=Path)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--prepare-canary-plan", action="store_true")
    action.add_argument("--validate-canary-plan", action="store_true")
    action.add_argument("--capture-live-canary-evidence", action="store_true")
    action.add_argument("--validate-live-canary-evidence", action="store_true")
    args = parser.parse_args(argv)
    try:
        context = RNRunContext.load(args.run_dir)
        if args.prepare_canary_plan:
            prepared = prepare_reasoning_off_canary(context)
            output = {
                "status": "PREPARED_NO_NETWORK_NO_PAID_API",
                "execution_ready": False,
                "live_telemetry_status": "MISSING_NOT_FABRICATED",
                "blocking_requirements": [
                    "real_paid_reasoning_off_telemetry_must_be_run_separately",
                    "final_two_trading_day_P1_canary_is_a_separate_gate",
                ],
                "canary_plan_path": str(prepared.plan_path),
                "canary_plan_sha256": prepared.plan_sha256,
                "expected_live_request_count": prepared.expected_live_request_count,
                "network_requests": 0,
                "paid_api_calls": 0,
            }
        elif args.validate_canary_plan:
            plan = validate_reasoning_off_canary_plan(context)
            output = {
                "status": "PLAN_VALID_NO_NETWORK_NO_PAID_API",
                "execution_ready": False,
                "live_telemetry_status": "MISSING_NOT_FABRICATED",
                "blocking_requirements": [
                    "real_paid_reasoning_off_telemetry_must_be_run_separately",
                    "final_two_trading_day_P1_canary_is_a_separate_gate",
                ],
                "expected_live_request_count": plan["canary_scope"][
                    "expected_physical_http_requests"
                ],
                "network_requests": 0,
                "paid_api_calls": 0,
            }
        elif args.capture_live_canary_evidence:
            evidence = capture_reasoning_off_canary_evidence(context)
            output = {
                "status": "LIVE_TELEMETRY_VALIDATED_FROM_EXISTING_FILES",
                "canary_gate_ready": True,
                "full_execution_ready": False,
                "remaining_full_execution_gates": [
                    "sealed_two_trading_day_U4_100_agent_P1_canary",
                    "verified_run_local_community_and_D2_dependencies",
                    "explicit_separate_paid_full_run_launch",
                ],
                "canary_evidence_path": str(evidence.evidence_path),
                "canary_evidence_sha256": evidence.evidence_sha256,
                "summaries": evidence.summaries,
                "network_requests": 0,
                "paid_api_calls": 0,
            }
        else:
            evidence = validate_reasoning_off_canary_evidence(context)
            output = {
                "status": "SEALED_LIVE_TELEMETRY_EVIDENCE_VALID",
                "canary_gate_ready": True,
                "full_execution_ready": False,
                "remaining_full_execution_gates": [
                    "sealed_two_trading_day_U4_100_agent_P1_canary",
                    "verified_run_local_community_and_D2_dependencies",
                    "explicit_separate_paid_full_run_launch",
                ],
                "canary_evidence_path": str(evidence.evidence_path),
                "canary_evidence_sha256": evidence.evidence_sha256,
                "summaries": evidence.summaries,
                "network_requests": 0,
                "paid_api_calls": 0,
            }
    except Exception as exc:
        output = {
            "status": "NO_GO",
            "execution_ready": False,
            "live_telemetry_status": "MISSING_OR_INVALID",
            "error": str(exc),
            "network_requests": 0,
            "paid_api_calls": 0,
        }
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0


__all__ = [
    "RNCanaryEvidence",
    "RNCanaryPreparation",
    "RNExecutionFactoryError",
    "RNExecutionPrerequisites",
    "RNReasoningOffTelemetryRunner",
    "build_live_stage_models",
    "build_local_test_runner",
    "build_p1_canary_runner",
    "build_paired_runner",
    "build_reasoning_off_telemetry_runner",
    "capture_reasoning_off_canary_evidence",
    "execution_prerequisites",
    "prepare_reasoning_off_canary",
    "run_local_audit_path",
    "strict_policy_from_context",
    "validate_reasoning_off_canary_evidence",
    "validate_reasoning_off_canary_plan",
    "validate_final_run_reasoning_audits",
    "validate_run_local_reasoning_audits",
]


if __name__ == "__main__":  # pragma: no cover - exercised through subprocess.
    raise SystemExit(_cli_main())
