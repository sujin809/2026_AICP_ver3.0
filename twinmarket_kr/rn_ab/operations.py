"""Explicit operational CLI for RN telemetry, run/resume, and finalization.

Importing this module and every read-only command make zero model calls.
Only the four paid subcommands pass the deliberately redundant authorization
check and then construct the strict live providers.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Sequence

from twinmarket_kr.rn_ab.community_provider import (
    build_journaled_community_lifecycle,
)
from twinmarket_kr.rn_ab.execution import (
    build_live_stage_models,
    build_p1_canary_runner,
    build_paired_runner,
    build_reasoning_off_telemetry_runner,
    prepare_reasoning_off_canary,
    strict_policy_from_context,
    validate_final_run_reasoning_audits,
    validate_reasoning_off_canary_evidence,
    validate_reasoning_off_canary_plan,
)
from twinmarket_kr.rn_ab.finalization import (
    finalize_rn_run_artifacts,
    inspect_final_handoff_readiness,
    validate_rn_finalization_artifacts,
)
from twinmarket_kr.rn_ab.p1 import (
    assert_p1_candidate_context,
    validate_p1_canary_for_full_run,
    write_p1_recovery_evidence,
)
from twinmarket_kr.rn_ab.phase_runner import RNPhasePausedError
from twinmarket_kr.rn_ab.run_context import RNRunContext
from twinmarket_kr.rn_ab.spec import RN_COMM_ON


class RNOperationalError(RuntimeError):
    """An operator command is unsafe or inconsistent with run state."""


_PAID_ACTIONS = frozenset({"telemetry", "run-p1", "resume-p1", "run", "resume"})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Operate one sealed RN run. Paid actions require an explicit flag "
            "and exact run-id confirmation; status/P1/finalization are read-only "
            "with respect to model providers."
        )
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    prepare = subparsers.add_parser(
        "prepare-telemetry",
        help="Create the deterministic first-event telemetry plan; no API call.",
    )
    _add_run_dir(prepare)

    status = subparsers.add_parser("status", help="Inspect local gates; no API call.")
    _add_run_dir(status)
    status.add_argument("--p1-run-dir", type=Path)

    check_p1 = subparsers.add_parser(
        "check-p1",
        help="Revalidate a separate completed P1 run; no API call.",
    )
    _add_run_dir(check_p1)
    check_p1.add_argument("--p1-run-dir", type=Path, required=True)

    finalize = subparsers.add_parser(
        "finalize",
        help="Validate complete lineage and publish final CSVs; no API call.",
    )
    _add_run_dir(finalize)
    finalize.add_argument("--p1-run-dir", type=Path)

    validate_final = subparsers.add_parser(
        "validate-final",
        help="Recompute finalization and handoff integrity; no API call.",
    )
    _add_run_dir(validate_final)
    validate_final.add_argument("--p1-run-dir", type=Path)

    telemetry = subparsers.add_parser(
        "telemetry",
        help="PAID: run the sealed first-event reasoning-off telemetry boundary.",
    )
    _add_paid_arguments(telemetry)

    for name, help_text in (
        ("run-p1", "PAID: continue a valid P1 candidate after telemetry."),
        ("resume-p1", "PAID: recover a genuinely paused/interrupted P1 phase."),
    ):
        command = subparsers.add_parser(name, help=help_text)
        _add_paid_arguments(command)

    for name, help_text in (
        ("run", "PAID: start the complete paper run after validated P1."),
        ("resume", "PAID: resume/recover the complete paper run after validated P1."),
    ):
        command = subparsers.add_parser(name, help=help_text)
        _add_paid_arguments(command)
        command.add_argument("--p1-run-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        context = RNRunContext.load(args.run_dir)
        if args.action in _PAID_ACTIONS:
            _require_paid_authorization(context, args)
        if args.action == "prepare-telemetry":
            prepared = prepare_reasoning_off_canary(context)
            result = {
                "status": "PREPARED_NO_NETWORK_NO_PAID_API",
                "run_id": context.run_id,
                "plan_path": str(prepared.plan_path),
                "plan_sha256": prepared.plan_sha256,
                "expected_live_request_count": prepared.expected_live_request_count,
                "network_requests": 0,
                "paid_api_calls": 0,
            }
        elif args.action == "status":
            result = _status(context, p1_run_dir=args.p1_run_dir)
        elif args.action == "check-p1":
            result = _check_p1(context, p1_run_dir=args.p1_run_dir)
        elif args.action == "finalize":
            validate_final_run_reasoning_audits(context)
            artifacts = finalize_rn_run_artifacts(context)
            handoff = inspect_final_handoff_readiness(
                context.run_dir,
                p1_canary_run_dir=args.p1_run_dir,
            )
            result = {
                "status": "FINALIZED" if handoff.ready else "FINALIZED_HANDOFF_NO_GO",
                "run_id": context.run_id,
                "run_record": str(artifacts.run_record_path),
                "export_index": str(artifacts.export_index_path),
                "export_index_sha256": artifacts.export_index_sha256,
                "handoff_status": handoff.status,
                "handoff_missing": list(handoff.missing),
                "network_requests": 0,
                "paid_api_calls": 0,
            }
        elif args.action == "validate-final":
            artifacts = validate_rn_finalization_artifacts(context)
            handoff = inspect_final_handoff_readiness(
                context.run_dir,
                p1_canary_run_dir=args.p1_run_dir,
            )
            result = {
                "status": handoff.status,
                "run_id": context.run_id,
                "run_record": str(artifacts.run_record_path),
                "export_index_sha256": artifacts.export_index_sha256,
                "handoff_missing": list(handoff.missing),
                "network_requests": 0,
                "paid_api_calls": 0,
            }
        else:
            result = asyncio.run(_run_paid_action(context, args))
    except RNPhasePausedError as exc:
        result = {
            "status": "PAUSED_SAFE_TO_RESUME",
            "error": str(exc),
            "network_requests": "see_run_local_audit",
            "paid_api_calls": "see_run_local_audit",
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 3
    except Exception as exc:
        result = {
            "status": "NO_GO",
            "error": str(exc),
            "network_requests": 0 if args.action not in _PAID_ACTIONS else "not_inferred",
            "paid_api_calls": 0 if args.action not in _PAID_ACTIONS else "not_inferred",
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


async def _run_paid_action(
    context: RNRunContext,
    args: argparse.Namespace,
) -> dict[str, Any]:
    action = str(args.action)
    checkpoint = _checkpoint(context)
    if action == "telemetry":
        validate_reasoning_off_canary_plan(context)
        _require_telemetry_start_state(context, checkpoint)
        models, lifecycle = _live_dependencies(context)
        runner = build_reasoning_off_telemetry_runner(
            context,
            models=models,
            community_lifecycle=lifecycle,
        )
        evidence = await runner.run()
        return {
            "status": "LIVE_TELEMETRY_VALIDATED",
            "run_id": context.run_id,
            "evidence_path": str(evidence.evidence_path),
            "evidence_sha256": evidence.evidence_sha256,
            "summaries": evidence.summaries,
        }

    if action in {"run-p1", "resume-p1"}:
        assert_p1_candidate_context(context)
        validate_reasoning_off_canary_evidence(context)
        if action == "run-p1":
            _require_p1_continuation_state(context, checkpoint)
            recovery_checkpoint = None
        else:
            recovery_checkpoint = _require_recovery_state(context, checkpoint)
        models, lifecycle = _live_dependencies(context)
        runner = build_p1_canary_runner(
            context,
            models=models,
            community_lifecycle=lifecycle,
        )
        progress = await runner.run_all()
        recovery_path = None
        if recovery_checkpoint is not None:
            recovery_path = write_p1_recovery_evidence(
                context,
                pre_resume_checkpoint=recovery_checkpoint,
            )
        return _progress_result(
            context,
            progress,
            status="P1_RUN_COMPLETE_LOCAL_FINALIZATION_REQUIRED",
            recovery_evidence_path=recovery_path,
        )

    p1_validation = validate_p1_canary_for_full_run(
        context,
        p1_canary_run_dir=args.p1_run_dir,
    )
    if action == "run":
        _require_fresh_full_run_state(context, checkpoint)
    else:
        _require_incomplete_run_state(context, checkpoint)
    models, lifecycle = _live_dependencies(context)
    runner = build_paired_runner(
        context,
        models=models,
        community_lifecycle=lifecycle,
        p1_canary_run_dir=args.p1_run_dir,
    )
    progress = await runner.run_all()
    return {
        **_progress_result(
            context,
            progress,
            status="FULL_RUN_COMPLETE_LOCAL_FINALIZATION_REQUIRED",
        ),
        "validated_p1_run_id": p1_validation.canary_run_id,
        "validated_p1_manifest_sha256": p1_validation.canary_manifest_sha256,
    }


def _live_dependencies(context: RNRunContext) -> tuple[Mapping[str, Any], Any]:
    models = build_live_stage_models(context)
    policy = strict_policy_from_context(context)
    lifecycle = build_journaled_community_lifecycle(
        context,
        model=models[RN_COMM_ON],
        call_policy=policy,
    )
    return models, lifecycle


def _status(
    context: RNRunContext,
    *,
    p1_run_dir: Path | None,
) -> dict[str, Any]:
    checkpoint = _checkpoint(context)
    try:
        telemetry = validate_reasoning_off_canary_evidence(context)
        telemetry_status = "VALID"
        telemetry_sha256 = telemetry.evidence_sha256
    except Exception:
        telemetry_status = "MISSING_OR_INVALID"
        telemetry_sha256 = None
    try:
        handoff = inspect_final_handoff_readiness(
            context.run_dir,
            p1_canary_run_dir=p1_run_dir,
        )
        handoff_status = handoff.status
        handoff_missing = list(handoff.missing)
    except Exception as exc:
        handoff_status = "NO_GO"
        handoff_missing = [f"handoff_inspection_failed:{exc}"]
    return {
        "status": "INSPECTED_NO_NETWORK_NO_PAID_API",
        "run_id": context.run_id,
        "reasoning_off_telemetry": telemetry_status,
        "reasoning_off_telemetry_sha256": telemetry_sha256,
        "checkpoint": _checkpoint_summary(checkpoint),
        "handoff_status": handoff_status,
        "handoff_missing": handoff_missing,
        "network_requests": 0,
        "paid_api_calls": 0,
    }


def _check_p1(
    context: RNRunContext,
    *,
    p1_run_dir: Path,
) -> dict[str, Any]:
    validated = validate_p1_canary_for_full_run(
        context,
        p1_canary_run_dir=p1_run_dir,
    )
    return {
        "status": "P1_VALID_NO_NETWORK_NO_PAID_API",
        "run_id": context.run_id,
        "p1_run_id": validated.canary_run_id,
        "p1_manifest_sha256": validated.canary_manifest_sha256,
        "p1_finalization_sha256": validated.finalization_sha256,
        "compatibility_sha256": validated.compatibility_sha256,
        "community_summary": dict(validated.community_summary),
        "recovery_mode": validated.recovery_mode,
        "network_requests": 0,
        "paid_api_calls": 0,
    }


def _progress_result(
    context: RNRunContext,
    progress: Any,
    *,
    status: str,
    recovery_evidence_path: Path | None = None,
) -> dict[str, Any]:
    result = {
        "status": status,
        "run_id": context.run_id,
        "completed_phase_count": len(progress.completed_phases),
        "right_censored_outcome_count": progress.right_censored_outcome_count,
        "finalization_required": True,
    }
    if recovery_evidence_path is not None:
        result["recovery_evidence_path"] = str(recovery_evidence_path)
    return result


def _require_paid_authorization(
    context: RNRunContext,
    args: argparse.Namespace,
) -> None:
    if args.authorize_paid_api_calls is not True:
        raise RNOperationalError(
            "Paid action requires --authorize-paid-api-calls"
        )
    if args.confirm_run_id != context.run_id:
        raise RNOperationalError(
            "--confirm-run-id must exactly equal the sealed RunContext run_id"
        )


def _checkpoint(context: RNRunContext) -> dict[str, Any] | None:
    path = context.run_dir / "rn_phase_checkpoint.json"
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise RNOperationalError("Phase checkpoint is not a regular run-local file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RNOperationalError("Phase checkpoint is invalid JSON") from exc
    if (
        not isinstance(value, dict)
        or value.get("version") != "rn-paired-phase-v1"
        or value.get("manifest_sha256") != context.manifest_sha256
    ):
        raise RNOperationalError("Phase checkpoint differs from the sealed run")
    return value


def _checkpoint_summary(checkpoint: dict[str, Any] | None) -> dict[str, Any]:
    if checkpoint is None:
        return {"state": "not_started", "completed_phase_count": 0}
    if isinstance(checkpoint.get("paused_phase"), dict):
        state = "paused"
        phase_id = checkpoint["paused_phase"].get("phase_id")
    elif isinstance(checkpoint.get("inflight_phase"), dict):
        state = str(checkpoint["inflight_phase"].get("state") or "running")
        phase_id = checkpoint["inflight_phase"].get("phase_id")
    else:
        state = "checkpointed"
        phase_id = None
    return {
        "state": state,
        "phase_id": phase_id,
        "completed_phase_count": len(checkpoint.get("completed_phases", [])),
    }


def _require_telemetry_start_state(
    context: RNRunContext,
    checkpoint: dict[str, Any] | None,
) -> None:
    if checkpoint is None:
        return
    expected_prefix = {
        "bootstrap_ltb0",
        f"{context.event_schedule.events[0]['event_id']}:scientific_turn",
    }
    completed = {
        str(item.get("phase_id"))
        for item in checkpoint.get("completed_phases", [])
        if isinstance(item, dict)
    }
    if completed - expected_prefix or checkpoint.get("paused_phase") or checkpoint.get("inflight_phase"):
        raise RNOperationalError(
            "Telemetry may only start/revalidate the bootstrap plus first-event prefix"
        )


def _require_p1_continuation_state(
    context: RNRunContext,
    checkpoint: dict[str, Any] | None,
) -> None:
    if checkpoint is None:
        raise RNOperationalError("P1 run requires completed live telemetry first")
    expected_prefix = [
        "bootstrap_ltb0",
        f"{context.event_schedule.events[0]['event_id']}:scientific_turn",
    ]
    completed = [
        str(item.get("phase_id"))
        for item in checkpoint.get("completed_phases", [])
        if isinstance(item, dict)
    ]
    if (
        completed != expected_prefix
        or checkpoint.get("paused_phase") is not None
        or checkpoint.get("inflight_phase") is not None
    ):
        raise RNOperationalError(
            "run-p1 requires exactly the completed telemetry prefix; use resume-p1 after a failure"
        )


def _require_recovery_state(
    context: RNRunContext,
    checkpoint: dict[str, Any] | None,
) -> dict[str, Any]:
    del context
    if checkpoint is None or not (
        isinstance(checkpoint.get("paused_phase"), dict)
        or isinstance(checkpoint.get("inflight_phase"), dict)
    ):
        raise RNOperationalError(
            "resume-p1 requires an actual paused/interrupted phase for recovery evidence"
        )
    return dict(checkpoint)


def _require_fresh_full_run_state(
    context: RNRunContext,
    checkpoint: dict[str, Any] | None,
) -> None:
    del context
    if checkpoint is not None:
        raise RNOperationalError("run requires a fresh checkpoint; use resume for existing progress")


def _require_incomplete_run_state(
    context: RNRunContext,
    checkpoint: dict[str, Any] | None,
) -> None:
    if checkpoint is None:
        raise RNOperationalError("resume requires an existing phase checkpoint")
    expected_final = "finalize:right_censor_outcomes"
    completed = [
        str(item.get("phase_id"))
        for item in checkpoint.get("completed_phases", [])
        if isinstance(item, dict)
    ]
    if expected_final in completed and checkpoint.get("inflight_phase") is None:
        raise RNOperationalError("Run is already complete; use finalize")


def _add_run_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-dir", type=Path, required=True)


def _add_paid_arguments(parser: argparse.ArgumentParser) -> None:
    _add_run_dir(parser)
    parser.add_argument(
        "--authorize-paid-api-calls",
        action="store_true",
        help="Required explicit authorization for this paid command.",
    )
    parser.add_argument(
        "--confirm-run-id",
        required=True,
        help="Must exactly match RUN_RECORD.json run_id.",
    )


__all__ = ["RNOperationalError", "build_parser", "main"]
