#!/usr/bin/env python3
"""Launch treatment arms through the canonical numbered simulation runner.

The current executable baseline contains only ``RN_COMM_OFF`` and
``RN_COMM_ON``.  The four fake-news names are reserved extension points: they
are accepted only when the caller supplies a sealed profile which is also
accepted by the common StudySpec validator.  This launcher never translates a
fake treatment into the removed checkpoint-runner flags.
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config
from twinmarket_kr.experiment_runtime import (
    build_clean_experiment_base,
    validate_clean_experiment_base,
)
from twinmarket_kr.pair_evaluation import (
    PairEvaluationError,
    finalize_realnews_community_pair,
)
from twinmarket_kr.simulation import select_simulation_agents
from twinmarket_kr.study_spec import (
    IntegratedStudySpecError,
    ResolvedStudyProfile,
    validate_integrated_study_profile,
)


@dataclass(frozen=True)
class MatrixCondition:
    condition_id: str
    community_mode: str
    news_treatment: str
    profile_argument: str

    @property
    def runtime_condition_id(self) -> str:
        return (
            "RN_COMM_ON"
            if self.community_mode == "on"
            else "RN_COMM_OFF"
        )


CONDITIONS = (
    MatrixCondition(
        "RN_COMM_OFF",
        "off",
        "real_only",
        "real_profile_root",
    ),
    MatrixCondition(
        "RN_COMM_ON",
        "on",
        "real_only",
        "real_profile_root",
    ),
    MatrixCondition(
        "REAL_PLUS_BEARISH_FAKE_COMM_OFF",
        "off",
        "real_plus_bearish_fake",
        "bearish_profile_root",
    ),
    MatrixCondition(
        "REAL_PLUS_BEARISH_FAKE_COMM_ON",
        "on",
        "real_plus_bearish_fake",
        "bearish_profile_root",
    ),
    MatrixCondition(
        "REAL_PLUS_BULLISH_FAKE_COMM_OFF",
        "off",
        "real_plus_bullish_fake",
        "bullish_profile_root",
    ),
    MatrixCondition(
        "REAL_PLUS_BULLISH_FAKE_COMM_ON",
        "on",
        "real_plus_bullish_fake",
        "bullish_profile_root",
    ),
)
CONDITION_BY_ID = {
    condition.condition_id: condition for condition in CONDITIONS
}
BASELINE_CONDITION_IDS = ("RN_COMM_OFF", "RN_COMM_ON")


def _acquire_matrix_lock(output_root: Path):
    """Reject duplicate launchers targeting the same matrix root."""

    lock_path = output_root.parent / f".{output_root.name}.matrix.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RuntimeError(
            f"Another condition launcher is using {output_root}"
        ) from exc
    handle.seek(0)
    handle.truncate()
    handle.write(f"pid={os.getpid()}\n")
    handle.flush()
    return handle


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def _file_fingerprint(path: Path) -> tuple[int, int] | None:
    if not path.is_file():
        return None
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"{label} is missing or symlinked: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} is not valid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must be a JSON object: {path}")
    return value


def _restart_decision_from_checkpoint(
    *,
    paused_path: Path,
    checkpoint_path: Path,
    condition_id: str,
) -> dict[str, Any]:
    """Honor the canonical runner's restart classification.

    ``paused.json`` is a human-facing resume instruction.  The durable runtime
    checkpoint is the authority on whether the underlying failure is a
    transient provider/transport error that may be retried automatically.
    """

    paused = _read_json_object(
        paused_path,
        f"{condition_id} paused metadata",
    )
    checkpoint = _read_json_object(
        checkpoint_path,
        f"{condition_id} runtime checkpoint",
    )
    last_error = checkpoint.get("last_error")
    resume_instruction = (
        paused.get("status") == "paused"
        and "--resume" in str(paused.get("restart_command") or "")
    )
    matching_error = (
        isinstance(last_error, dict)
        and checkpoint.get("status") == "paused"
        and last_error.get("event_id") == paused.get("event_id")
    )
    if not resume_instruction or not matching_error:
        return {
            "auto_restart_allowed": False,
            "failure_class": "invalid_or_unbound_canonical_pause",
            "event_id": paused.get("event_id"),
            "error_type": paused.get("error_type"),
        }
    return {
        "auto_restart_allowed": bool(
            last_error.get("auto_restart_allowed", False)
        ),
        "failure_class": str(
            last_error.get("failure_class") or "unknown_or_local_error"
        ),
        "event_id": paused.get("event_id"),
        "error_type": paused.get("error_type"),
        "exception_chain": list(last_error.get("exception_chain") or []),
        "matched_markers": list(last_error.get("matched_markers") or []),
    }


def _selected_conditions(args: argparse.Namespace) -> list[MatrixCondition]:
    selected_ids = list(args.conditions or BASELINE_CONDITION_IDS)
    if len(selected_ids) != len(set(selected_ids)):
        raise ValueError("--conditions must not contain duplicates")
    return [CONDITION_BY_ID[condition_id] for condition_id in selected_ids]


def _profile_root(
    args: argparse.Namespace,
    condition: MatrixCondition,
) -> Path:
    value = getattr(args, condition.profile_argument)
    if value is None:
        raise RuntimeError(
            f"{condition.condition_id} is reserved but not runnable: "
            f"--{condition.profile_argument.replace('_', '-')} is required. "
            "Supply a provenance-sealed bundle/profile whose StudySpec declares "
            f"news_treatment={condition.news_treatment!r}; legacy fake-news "
            "flags are not accepted."
        )
    return Path(value).resolve()


def _validate_condition_profile(
    *,
    condition: MatrixCondition,
    root: Path,
    full_agents: list[dict[str, Any]],
) -> ResolvedStudyProfile:
    """Validate the profile before a run or matrix directory is created."""

    spec = _read_json_object(root / "study_spec.json", "study profile")
    treatments = spec.get("condition_treatments")
    if not isinstance(treatments, dict):
        raise RuntimeError(
            f"{condition.condition_id}: study_spec.condition_treatments "
            "must be an object"
        )
    treatment = treatments.get(condition.runtime_condition_id)
    if not isinstance(treatment, dict):
        raise RuntimeError(
            f"{condition.condition_id}: sealed profile has no "
            f"{condition.runtime_condition_id} treatment"
        )
    if treatment.get("community_mode") != condition.community_mode:
        raise RuntimeError(
            f"{condition.condition_id}: sealed community_mode differs from "
            f"{condition.community_mode!r}"
        )
    if treatment.get("news_treatment") != condition.news_treatment:
        raise RuntimeError(
            f"{condition.condition_id}: sealed news_treatment is "
            f"{treatment.get('news_treatment')!r}, expected "
            f"{condition.news_treatment!r}"
        )
    try:
        return validate_integrated_study_profile(
            root,
            agents=full_agents,
            condition_id=condition.runtime_condition_id,
            prompt_dir=config.PROMPT_DIR,
            expected_stock_code=None,
            expected_model=config.PAPER_OPENROUTER_MODEL,
            expected_provider=config.PAPER_OPENROUTER_PROVIDER,
        )
    except IntegratedStudySpecError as exc:
        raise RuntimeError(
            f"{condition.condition_id}: the supplied sealed profile is not "
            "accepted by the canonical common StudySpec. Extend and test the "
            "common StudySpec/05 runner before launching this treatment; do "
            f"not fall back to the old checkpoint runner. Cause: {exc}"
        ) from exc


def _build_condition_command(
    *,
    condition: MatrixCondition,
    profile_root: Path,
    run_dir: Path,
    args: argparse.Namespace,
    resume: bool,
) -> list[str]:
    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "05_run_simulation.py"),
        "--seed",
        str(args.seed),
        "--start-date",
        args.start_date,
        "--end-date",
        args.end_date,
        "--community-mode",
        condition.community_mode,
        "--run-dir",
        str(run_dir),
        "--base-db",
        str(Path(args.experiment_base_db).resolve()),
        "--news-bundle",
        str(profile_root / "news.json"),
        "--calendar-registry",
        str(profile_root / "calendar.json"),
        "--price-registry",
        str(profile_root / "prices.json"),
    ]
    if args.allow_paid_api:
        command.append("--allow-paid-api")
    if args.reasoning_off_canary_audit is not None:
        command.extend(
            [
                "--reasoning-off-canary-audit",
                str(Path(args.reasoning_off_canary_audit).resolve()),
            ]
        )
    if resume:
        command.append("--resume")
    return command


def _validate_existing_arm(
    *,
    run_dir: Path,
    condition: MatrixCondition,
    profile: ResolvedStudyProfile,
    profile_root: Path,
    args: argparse.Namespace,
    base_db: Path,
) -> None:
    """Reject reusing a condition directory for a different sealed run."""

    if not run_dir.exists():
        return
    metadata_path = run_dir / "run_metadata.json"
    if not metadata_path.is_file():
        # A first attempt can fail during fail-closed preflight before any
        # canonical metadata exists. The 05 runner will reject unsafe reuse.
        return
    metadata = _read_json_object(
        metadata_path,
        f"{condition.condition_id} run metadata",
    )
    calendar = _read_json_object(
        profile_root / "calendar.json",
        f"{condition.condition_id} calendar",
    )
    rows = calendar.get("dates")
    if not isinstance(rows, list):
        raise RuntimeError("calendar.dates must be an array")
    dates = [
        str(row.get("date"))
        for row in rows
        if isinstance(row, dict)
        and args.start_date <= str(row.get("date")) <= args.end_date
    ]
    if not dates:
        raise RuntimeError("Requested bounds select no sealed trading dates")
    expected = {
        "condition_id": condition.runtime_condition_id,
        "community_mode": condition.community_mode,
        "news_treatment": condition.news_treatment,
        "start_date": dates[0],
        "end_date": dates[-1],
        "trading_dates": dates,
        "agent_count": profile.required_agent_count,
        "seed": args.seed,
        "study_spec_sha256": profile.study_spec_sha256,
        "cohort_sha256": profile.cohort_sha256,
        "prompt_bundle_sha256": profile.prompt_bundle_sha256,
        "base_db_source": str(base_db.resolve()),
    }
    mismatches = [
        f"{field}: existing={metadata.get(field)!r} expected={value!r}"
        for field, value in expected.items()
        if metadata.get(field) != value
    ]
    if mismatches:
        raise RuntimeError(
            f"{condition.condition_id} cannot be resumed with different "
            "inputs:\n- " + "\n- ".join(mismatches)
        )


async def _run_condition(
    *,
    semaphore: asyncio.Semaphore,
    output_root: Path,
    condition: MatrixCondition,
    profile_root: Path,
    args: argparse.Namespace,
    environment: dict[str, str],
) -> dict[str, Any]:
    async with semaphore:
        run_dir = output_root / condition.condition_id
        console_log = output_root / (
            f"{condition.condition_id}.console.log"
        )
        if (
            (run_dir / "run_complete.json").is_file()
            or (run_dir / "segment_complete.json").is_file()
        ):
            return {
                "condition": condition.condition_id,
                "community_mode": condition.community_mode,
                "news_treatment": condition.news_treatment,
                "return_code": 0,
                "status": "already_complete",
                "launch_attempts": 0,
                "run_dir": str(run_dir),
                "console_log": str(console_log),
            }

        return_code = 1
        launch_attempt = 0
        restart_decision: dict[str, Any] = {
            "auto_restart_allowed": False,
            "failure_class": "not_evaluated",
        }
        for launch_attempt in range(1, args.max_process_restarts + 2):
            paused_path = run_dir / "paused.json"
            paused_before = _file_fingerprint(paused_path)
            command = _build_condition_command(
                condition=condition,
                profile_root=profile_root,
                run_dir=run_dir,
                args=args,
                resume=run_dir.exists(),
            )
            with console_log.open("a", encoding="utf-8") as handle:
                handle.write(
                    f"\n[launch_attempt={launch_attempt} "
                    f"started_at={datetime.now().isoformat()}]\n"
                )
                handle.flush()
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=handle,
                    stderr=asyncio.subprocess.STDOUT,
                    env=environment,
                    cwd=PROJECT_ROOT,
                )
                try:
                    await process.wait()
                except asyncio.CancelledError:
                    if process.returncode is None:
                        process.terminate()
                        try:
                            await asyncio.wait_for(
                                process.wait(),
                                timeout=10,
                            )
                        except asyncio.TimeoutError:
                            process.kill()
                            await process.wait()
                    raise
                handle.write(
                    f"[launch_attempt={launch_attempt} "
                    f"return_code={process.returncode} "
                    f"finished_at={datetime.now().isoformat()}]\n"
                )
                handle.flush()
            return_code = int(process.returncode or 0)
            if return_code == 0:
                break

            paused_after = _file_fingerprint(paused_path)
            runtime_checkpoint = (
                run_dir / ".runtime" / "checkpoint.json"
            )
            if (
                paused_after is not None
                and paused_after != paused_before
                and runtime_checkpoint.is_file()
            ):
                restart_decision = _restart_decision_from_checkpoint(
                    paused_path=paused_path,
                    checkpoint_path=runtime_checkpoint,
                    condition_id=condition.condition_id,
                )
            else:
                restart_decision = {
                    "auto_restart_allowed": False,
                    "failure_class": "no_new_canonical_pause",
                }
            if not restart_decision["auto_restart_allowed"]:
                break
            if launch_attempt <= args.max_process_restarts:
                await asyncio.sleep(args.restart_delay_seconds)
        return {
            "condition": condition.condition_id,
            "community_mode": condition.community_mode,
            "news_treatment": condition.news_treatment,
            "return_code": return_code,
            "status": (
                "complete"
                if return_code == 0
                else "paused_or_failed"
            ),
            "launch_attempts": launch_attempt,
            "restart_decision": restart_decision,
            "run_dir": str(run_dir),
            "console_log": str(console_log),
        }


async def _run(args: argparse.Namespace) -> None:
    if args.max_parallel_runs < 1 or args.max_process_restarts < 0:
        raise ValueError(
            "parallel runs must be positive and restarts non-negative"
        )
    if args.global_api_concurrency < 1:
        raise ValueError("--global-api-concurrency must be positive")

    offline_llm = os.getenv(
        "TWINMARKET_OFFLINE_LLM",
        "",
    ).strip().lower() in {"1", "true", "yes"}
    if not offline_llm:
        if not args.allow_paid_api:
            raise RuntimeError(
                "Live matrix execution requires explicit --allow-paid-api."
            )
        if not config.OPENROUTER_API_KEY:
            raise RuntimeError(
                "OPENROUTER_API_KEY is not set; refusing live execution."
            )
        if args.reasoning_off_canary_audit is None:
            raise RuntimeError(
                "Live matrix execution requires "
                "--reasoning-off-canary-audit."
            )
        if not Path(args.reasoning_off_canary_audit).is_file():
            raise RuntimeError(
                "Reasoning-off canary audit does not exist: "
                f"{args.reasoning_off_canary_audit}"
            )

    selected = _selected_conditions(args)
    available_agents = select_simulation_agents(None)
    profiles: dict[str, tuple[Path, ResolvedStudyProfile]] = {}
    for condition in selected:
        root = _profile_root(args, condition)
        profiles[condition.condition_id] = (
            root,
            _validate_condition_profile(
                condition=condition,
                root=root,
                full_agents=available_agents,
            ),
        )
    first_profile = profiles[selected[0].condition_id][1]
    if args.seed is None:
        args.seed = first_profile.study_seed
    args.start_date = (
        str(args.start_date)
        if args.start_date is not None
        else first_profile.schedule_date_ids[0]
    )
    args.end_date = (
        str(args.end_date)
        if args.end_date is not None
        else first_profile.schedule_date_ids[-1]
    )
    if args.start_date > args.end_date:
        raise ValueError("--start-date must not be later than --end-date")
    if (
        args.start_date not in first_profile.schedule_date_ids
        or args.end_date not in first_profile.schedule_date_ids
    ):
        raise RuntimeError(
            "Matrix start/end dates must be frozen trading dates in the profile"
        )
    if int(args.seed) != first_profile.study_seed:
        raise RuntimeError(
            f"--seed {args.seed} differs from sealed study seed "
            f"{first_profile.study_seed}"
        )
    shared_profile_fields = (
        "required_agent_count",
        "agent_ids",
        "stock_code",
        "instrument_name",
        "schedule_date_ids",
        "calendar_sha256",
        "price_registry_sha256",
        "per_arm_concurrency",
        "study_seed",
        "cohort_sha256",
        "prompt_bundle_sha256",
    )
    for condition in selected[1:]:
        profile = profiles[condition.condition_id][1]
        for field in shared_profile_fields:
            if getattr(profile, field) != getattr(first_profile, field):
                raise RuntimeError(
                    f"{condition.condition_id}.{field} differs from the "
                    "matrix reference profile"
                )

    cohort_agents = select_simulation_agents(
        agent_ids=first_profile.agent_ids,
        instrument_name=first_profile.instrument_name,
    )
    base_db = Path(args.experiment_base_db).resolve()
    if not base_db.exists():
        build_clean_experiment_base(
            Path(args.source_sim_db),
            base_db,
            initial_agents=cohort_agents,
            instrument_name=first_profile.instrument_name,
        )
    validate_clean_experiment_base(
        base_db,
        expected_agents=cohort_agents,
        expected_stock_code=first_profile.stock_code,
        expected_trading_dates=first_profile.schedule_date_ids,
    )

    output_root = (
        Path(args.output_root).resolve()
        if args.output_root
        else config.LOG_DIR
        / f"experiment_matrix_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    )
    matrix_lock = _acquire_matrix_lock(output_root)
    try:
        output_root.mkdir(parents=True, exist_ok=True)
        for condition in selected:
            _validate_existing_arm(
                run_dir=output_root / condition.condition_id,
                condition=condition,
                profile=profiles[condition.condition_id][1],
                profile_root=profiles[condition.condition_id][0],
                args=args,
                base_db=base_db,
            )

        environment = dict(os.environ)
        environment["OPENROUTER_GLOBAL_CONCURRENCY"] = str(
            args.global_api_concurrency
        )
        environment["PYTHONUNBUFFERED"] = "1"
        semaphore = asyncio.Semaphore(args.max_parallel_runs)
        manifest: dict[str, Any] = {
            "status": "running",
            "seed": args.seed,
            "agent_count": len(cohort_agents),
            "agent_ids": list(first_profile.agent_ids),
            "stock_code": first_profile.stock_code,
            "instrument_name": first_profile.instrument_name,
            "start_date": args.start_date,
            "end_date": args.end_date,
            "max_parallel_runs": args.max_parallel_runs,
            "global_api_concurrency": args.global_api_concurrency,
            "max_process_restarts": args.max_process_restarts,
            "output_root": str(output_root),
            "conditions": [
                condition.condition_id for condition in selected
            ],
            "profiles": {
                condition.condition_id: {
                    "root": str(profiles[condition.condition_id][0]),
                    "study_spec_sha256": profiles[
                        condition.condition_id
                    ][1].study_spec_sha256,
                    "cohort_sha256": profiles[
                        condition.condition_id
                    ][1].cohort_sha256,
                    "required_agent_count": profiles[
                        condition.condition_id
                    ][1].required_agent_count,
                    "prompt_bundle_sha256": profiles[
                        condition.condition_id
                    ][1].prompt_bundle_sha256,
                }
                for condition in selected
            },
        }
        _write_json(output_root / "matrix_manifest.json", manifest)
        results = await asyncio.gather(
            *(
                _run_condition(
                    semaphore=semaphore,
                    output_root=output_root,
                    condition=condition,
                    profile_root=profiles[condition.condition_id][0],
                    args=args,
                    environment=environment,
                )
                for condition in selected
            )
        )
        manifest["results"] = results
        manifest["status"] = (
            "complete"
            if all(result["return_code"] == 0 for result in results)
            else "partial"
        )
        selected_ids = {
            condition.condition_id for condition in selected
        }
        if (
            manifest["status"] == "complete"
            and selected_ids == set(BASELINE_CONDITION_IDS)
            and len(selected) == len(BASELINE_CONDITION_IDS)
        ):
            artifact_path = output_root / "pair_evaluation.json"
            try:
                artifact = finalize_realnews_community_pair(
                    off_run_dir=output_root / "RN_COMM_OFF",
                    on_run_dir=output_root / "RN_COMM_ON",
                    target_csv=args.evaluation_targets,
                    output_path=artifact_path,
                )
            except PairEvaluationError as exc:
                manifest["status"] = "finalization_failed"
                manifest["pair_evaluation"] = {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
                _write_json(
                    output_root / "matrix_manifest.json",
                    manifest,
                )
                raise
            manifest["pair_evaluation"] = {
                "status": "pass",
                "path": str(artifact_path),
                "content_sha256": artifact["content_sha256"],
            }
        else:
            manifest["pair_evaluation"] = {
                "status": "not_applicable",
                "reason": (
                    "pair finalization requires exactly the complete "
                    "RN_COMM_OFF/RN_COMM_ON baseline pair"
                ),
            }
        _write_json(output_root / "matrix_manifest.json", manifest)
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        if manifest["status"] != "complete":
            raise SystemExit(1)
    finally:
        fcntl.flock(matrix_lock, fcntl.LOCK_UN)
        matrix_lock.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run paired experiment arms through scripts/05_run_simulation.py. "
            "The default is the two real-news community baseline arms."
        )
    )
    parser.add_argument(
        "--start-date",
        help="Default: first frozen trading date in the sealed profile.",
    )
    parser.add_argument(
        "--end-date",
        help="Default: last frozen trading date in the sealed profile.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Default: the sealed study profile's study_seed.",
    )
    parser.add_argument("--output-root")
    parser.add_argument(
        "--experiment-base-db",
        default=str(config.EXPERIMENT_BASE_DB),
    )
    parser.add_argument(
        "--source-sim-db",
        type=Path,
        default=config.SIM_DB,
        help=(
            "StockData source used only when --experiment-base-db does not "
            "exist. For an existing base, this input is not read."
        ),
    )
    parser.add_argument(
        "--real-profile-root",
        type=Path,
        default=config.SEALED_REAL_NEWS_BUNDLE.parent,
    )
    parser.add_argument("--bearish-profile-root", type=Path)
    parser.add_argument("--bullish-profile-root", type=Path)
    parser.add_argument("--max-parallel-runs", type=int, default=2)
    parser.add_argument(
        "--global-api-concurrency",
        type=int,
        default=config.OPENROUTER_GLOBAL_CONCURRENCY,
    )
    parser.add_argument("--max-process-restarts", type=int, default=5)
    parser.add_argument(
        "--restart-delay-seconds",
        type=float,
        default=10.0,
    )
    parser.add_argument(
        "--conditions",
        nargs="+",
        choices=sorted(CONDITION_BY_ID),
        help=(
            "Default: RN_COMM_OFF RN_COMM_ON. Fake-news conditions additionally "
            "require their corresponding sealed profile root."
        ),
    )
    parser.add_argument(
        "--allow-paid-api",
        action="store_true",
        help="Explicitly authorize live OpenRouter calls in every arm.",
    )
    parser.add_argument(
        "--reasoning-off-canary-audit",
        type=Path,
        help=(
            "Live canary JSONL passed unchanged to every canonical 05 arm."
        ),
    )
    parser.add_argument(
        "--evaluation-targets",
        type=Path,
        default=PROJECT_ROOT / "validation" / "data_trading_value.csv",
        help=(
            "Evaluator-only CSV containing Date and Individuals. It is read "
            "only after both canonical baseline arms complete, and its file "
            "hash is pinned in pair_evaluation.json."
        ),
    )
    return parser


def main() -> None:
    asyncio.run(_run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
