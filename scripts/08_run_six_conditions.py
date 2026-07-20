#!/usr/bin/env python3
"""Launch the six paper conditions with one shared OpenRouter concurrency budget."""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import config
from twinmarket_kr.experiment_runtime import (
    build_clean_experiment_base,
    file_sha256,
    validate_clean_experiment_base,
)
from twinmarket_kr.simulation import select_simulation_agents


CONDITIONS = (
    ("c00_commoff_fakeoff", "off", "off", None),
    ("c10_common_fakeoff", "on", "off", None),
    ("c01_commoff_bearish", "off", "on", "bearish"),
    ("c11_common_bearish", "on", "on", "bearish"),
    ("c02_commoff_bullish", "off", "on", "bullish"),
    ("c12_common_bullish", "on", "on", "bullish"),
)
CONDITION_NAMES = {condition[0] for condition in CONDITIONS}
STUDY_INVARIANT_FIELDS = (
    "start_date",
    "end_date",
    "max_agents",
    "agent_ids",
    "agent_source_db_sha256",
    "seed",
    "information_mode",
    "concurrency",
    "global_api_concurrency",
    "openrouter_max_retries",
    "openrouter_retry_max_delay",
    "openrouter_require_parameters",
    "openrouter_allow_fallbacks",
    "openrouter_provider_order",
    "primary_model",
    "community_model",
    "python_version",
    "openai_package_version",
    "base_db_sha256",
    "prompt_tree_sha256",
    "code_tree_sha256",
    "git_commit_at_start",
)
def _acquire_matrix_lock(output_root: Path):
    """Reject accidental duplicate launchers targeting the same matrix root."""
    lock_path = output_root.parent / f".{output_root.name}.matrix.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RuntimeError(f"Another six-condition launcher is using {output_root}") from exc
    handle.seek(0)
    handle.truncate()
    handle.write(f"pid={os.getpid()}\n")
    handle.flush()
    return handle


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _file_fingerprint(path: Path) -> tuple[int, int, str] | None:
    if not path.exists():
        return None
    stat = path.stat()
    return (
        stat.st_size,
        stat.st_mtime_ns,
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def _tree_hash(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        if not path.is_file():
            continue
        digest.update(str(path.relative_to(PROJECT_ROOT)).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _git_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "not-installed"


def _current_study_invariants(args: argparse.Namespace, base_db: Path) -> dict[str, Any]:
    code_paths = [PROJECT_ROOT / "config.py"]
    code_paths.extend(sorted((PROJECT_ROOT / "twinmarket_kr").rglob("*.py")))
    code_paths.extend(sorted((PROJECT_ROOT / "scripts").glob("*.py")))
    prompt_paths = sorted(config.PROMPT_DIR.rglob("*"))
    return {
        "start_date": args.start_date,
        "end_date": args.end_date,
        "max_agents": 30,
        "agent_ids": [str(agent["agent_id"]) for agent in select_simulation_agents(30)],
        "agent_source_db_sha256": file_sha256(config.SYS_100_DB),
        "seed": 2,
        "information_mode": "pre_close_cutoff",
        "concurrency": args.per_run_concurrency,
        "global_api_concurrency": args.global_api_concurrency,
        "openrouter_max_retries": config.OPENROUTER_MAX_RETRIES,
        "openrouter_retry_max_delay": config.OPENROUTER_RETRY_MAX_DELAY,
        "openrouter_require_parameters": config.OPENROUTER_REQUIRE_PARAMETERS,
        "openrouter_allow_fallbacks": config.OPENROUTER_ALLOW_FALLBACKS,
        "openrouter_provider_order": config.OPENROUTER_PROVIDER_ORDER,
        "primary_model": config.OPENROUTER_MODEL,
        "community_model": config.OPENROUTER_COMMUNITY_MODEL,
        "python_version": platform.python_version(),
        "openai_package_version": _package_version("openai"),
        "base_db_sha256": file_sha256(base_db),
        "prompt_tree_sha256": _tree_hash(prompt_paths),
        "code_tree_sha256": _tree_hash(code_paths),
        "git_commit_at_start": _git_commit(),
    }


def _assert_current_matches_existing(
    existing_validation: dict[str, Any],
    current_invariants: dict[str, Any],
) -> None:
    reference = existing_validation.get("study_invariants") or {}
    if not reference:
        return
    mismatches = [
        f"{field}: current={current_invariants.get(field)!r} existing={reference.get(field)!r}"
        for field in STUDY_INVARIANT_FIELDS
        if current_invariants.get(field) != reference.get(field)
    ]
    if mismatches:
        raise RuntimeError(
            "Current code/config/base does not match the completed C00 study run:\n- "
            + "\n- ".join(mismatches[:20])
        )


def _validate_existing_study_runs(output_root: Path) -> dict[str, Any]:
    """Ensure completed conditions in one study root share every non-treatment input."""
    completed: dict[str, dict[str, Any]] = {}
    incomplete: list[str] = []
    for condition_name in sorted(CONDITION_NAMES):
        condition_dir = output_root / condition_name
        metadata_path = condition_dir / "run_metadata.json"
        complete_path = condition_dir / "run_complete.json"
        if not condition_dir.exists():
            continue
        if not metadata_path.exists() or not complete_path.exists():
            incomplete.append(condition_name)
            continue
        completed[condition_name] = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not completed:
        return {
            "completed_conditions": [],
            "incomplete_conditions": incomplete,
            "reference_condition": None,
        }
    reference_name = sorted(completed)[0]
    reference = completed[reference_name]
    mismatches: list[str] = []
    for condition_name, metadata in completed.items():
        for field in STUDY_INVARIANT_FIELDS:
            if metadata.get(field) != reference.get(field):
                mismatches.append(
                    f"{condition_name}.{field} differs from {reference_name}: "
                    f"{metadata.get(field)!r} != {reference.get(field)!r}"
                )
    if mismatches:
        raise RuntimeError(
            "Completed conditions do not belong to the same reproducible study:\n- "
            + "\n- ".join(mismatches[:20])
        )
    return {
        "completed_conditions": sorted(completed),
        "incomplete_conditions": incomplete,
        "reference_condition": reference_name,
        "study_invariants": {field: reference.get(field) for field in STUDY_INVARIANT_FIELDS},
    }


async def _run_condition(
    *,
    semaphore: asyncio.Semaphore,
    output_root: Path,
    name: str,
    community_mode: str,
    fake_news_mode: str,
    fake_news_variant: str | None,
    args: argparse.Namespace,
    environment: dict[str, str],
) -> dict[str, Any]:
    async with semaphore:
        run_dir = output_root / name
        console_log = output_root / f"{name}.console.log"
        command = [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "05_run_simulation_checkpointed.py"),
            "--max-agents",
            "30",
            "--seed",
            "2",
            "--start-date",
            args.start_date,
            "--end-date",
            args.end_date,
            "--chunk-days",
            str(args.chunk_days),
            "--community-mode",
            community_mode,
            "--fake-news-mode",
            fake_news_mode,
            "--output-run-dir",
            str(run_dir),
            "--experiment-base-db",
            str(Path(args.experiment_base_db)),
        ]
        if fake_news_variant:
            command.extend(["--fake-news-variant", fake_news_variant])
        return_code = 1
        launch_attempt = 0
        restart_decision: dict[str, Any] = {
            "auto_restart_allowed": False,
            "failure_class": "not_evaluated",
        }
        for launch_attempt in range(1, args.max_process_restarts + 2):
            paused_path = run_dir / "paused.json"
            paused_before = _file_fingerprint(paused_path)
            with console_log.open("a", encoding="utf-8") as console_handle:
                console_handle.write(
                    f"\n[launch_attempt={launch_attempt} started_at={datetime.now().isoformat()}]\n"
                )
                console_handle.flush()
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=console_handle,
                    stderr=asyncio.subprocess.STDOUT,
                    env=environment,
                    cwd=PROJECT_ROOT,
                )
                try:
                    await process.wait()
                except asyncio.CancelledError:
                    # Ctrl-C on the matrix launcher must not leave an orphaned child
                    # continuing to consume API slots and mutate its runtime DB.
                    if process.returncode is None:
                        process.terminate()
                        try:
                            await asyncio.wait_for(process.wait(), timeout=10)
                        except asyncio.TimeoutError:
                            process.kill()
                            await process.wait()
                    raise
                console_handle.write(
                    f"[launch_attempt={launch_attempt} return_code={process.returncode} "
                    f"finished_at={datetime.now().isoformat()}]\n"
                )
                console_handle.flush()
            return_code = int(process.returncode or 0)
            if return_code == 0:
                break
            paused_after = _file_fingerprint(paused_path)
            if paused_after is not None and paused_after != paused_before:
                try:
                    paused_payload = json.loads(paused_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    restart_decision = {
                        "auto_restart_allowed": False,
                        "failure_class": "invalid_paused_metadata",
                        "error": str(exc),
                    }
                else:
                    restart_decision = {
                        "auto_restart_allowed": paused_payload.get("auto_restart_allowed") is True,
                        "failure_class": paused_payload.get("failure_class", "unspecified"),
                        "matched_markers": paused_payload.get("matched_markers") or [],
                    }
            else:
                checkpoint_path = run_dir / "checkpoint.json"
                try:
                    checkpoint_payload = (
                        json.loads(checkpoint_path.read_text(encoding="utf-8"))
                        if checkpoint_path.exists()
                        else {}
                    )
                except (OSError, json.JSONDecodeError):
                    checkpoint_payload = {}
                interrupted_phase = checkpoint_payload.get("inflight_phase")
                restart_decision = {
                    "auto_restart_allowed": bool(interrupted_phase),
                    "failure_class": (
                        "interrupted_inflight_process"
                        if interrupted_phase
                        else "no_new_paused_metadata"
                    ),
                    "failed_phase": (
                        interrupted_phase.get("phase_key")
                        if isinstance(interrupted_phase, dict)
                        else None
                    ),
                }
            if restart_decision["auto_restart_allowed"] is not True:
                break
            if launch_attempt <= args.max_process_restarts:
                await asyncio.sleep(args.restart_delay_seconds)
        return {
            "condition": name,
            "community_mode": community_mode,
            "fake_news_mode": fake_news_mode,
            "fake_news_variant": fake_news_variant,
            "return_code": return_code,
            "status": "complete" if return_code == 0 else "paused_or_failed",
            "launch_attempts": launch_attempt,
            "restart_decision": restart_decision,
            "run_dir": str(run_dir),
            "console_log": str(console_log),
        }


async def _run(args: argparse.Namespace) -> None:
    if not config.OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is not set; refusing to create paper run directories.")
    if config.OPENROUTER_MODEL != config.PAPER_OPENROUTER_MODEL:
        raise RuntimeError(
            f"Set OPENROUTER_MODEL={config.PAPER_OPENROUTER_MODEL} for the six paper conditions."
        )
    if config.OPENROUTER_COMMUNITY_MODEL != config.PAPER_OPENROUTER_MODEL:
        raise RuntimeError(
            f"Set OPENROUTER_COMMUNITY_MODEL={config.PAPER_OPENROUTER_MODEL} for the six paper conditions."
        )
    if args.start_date > args.end_date:
        raise ValueError("--start-date must not be later than --end-date")
    if args.chunk_days < 0:
        raise ValueError("--chunk-days must be zero or a positive integer")
    if args.per_run_concurrency < 1 or args.global_api_concurrency < 1:
        raise ValueError("API concurrency values must be at least 1")
    if args.max_parallel_runs < 1 or args.max_process_restarts < 0:
        raise ValueError("parallel runs must be positive and process restarts non-negative")
    output_root = Path(args.output_root) if args.output_root else config.LOG_DIR / (
        f"paper_matrix_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    )
    matrix_lock = _acquire_matrix_lock(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    base_db = Path(args.experiment_base_db)
    if not base_db.exists():
        build_clean_experiment_base(config.SIM_DB, base_db)
    validate_clean_experiment_base(base_db)
    before_validation = _validate_existing_study_runs(output_root)
    current_invariants = _current_study_invariants(args, base_db)
    _assert_current_matches_existing(before_validation, current_invariants)
    selected_names = args.conditions or [condition[0] for condition in CONDITIONS]
    if len(selected_names) != len(set(selected_names)):
        raise ValueError("--conditions must not contain duplicate condition names")
    selected_conditions = [
        condition for condition in CONDITIONS if condition[0] in set(selected_names)
    ]
    if {condition[0] for condition in selected_conditions} != set(selected_names):
        unknown = sorted(set(selected_names) - CONDITION_NAMES)
        raise ValueError(f"Unknown condition names: {unknown}")
    incomplete_not_selected = sorted(
        set(before_validation.get("incomplete_conditions") or []) - set(selected_names)
    )
    if incomplete_not_selected:
        raise RuntimeError(
            "These earlier conditions are incomplete. Resume them before starting other treatments: "
            f"{incomplete_not_selected}"
        )
    environment = dict(os.environ)
    environment["OPENROUTER_GLOBAL_CONCURRENCY"] = str(args.global_api_concurrency)
    environment["SIMULATION_CONCURRENCY"] = str(args.per_run_concurrency)
    environment["PYTHONUNBUFFERED"] = "1"
    semaphore = asyncio.Semaphore(max(1, args.max_parallel_runs))
    launch_manifest = {
        "status": "running",
        "seed": 2,
        "agent_count": 30,
        "start_date": args.start_date,
        "end_date": args.end_date,
        "chunk_days": args.chunk_days,
        "max_parallel_runs": args.max_parallel_runs,
        "per_run_concurrency": args.per_run_concurrency,
        "global_api_concurrency": args.global_api_concurrency,
        "max_process_restarts": args.max_process_restarts,
        "output_root": str(output_root),
        "conditions": [condition[0] for condition in selected_conditions],
        "existing_study_validation": before_validation,
        "current_study_invariants": current_invariants,
    }
    _write_json(output_root / "matrix_manifest.json", launch_manifest)
    results = await asyncio.gather(
        *(
            _run_condition(
                semaphore=semaphore,
                output_root=output_root,
                name=name,
                community_mode=community_mode,
                fake_news_mode=fake_news_mode,
                fake_news_variant=fake_news_variant,
                args=args,
                environment=environment,
            )
            for name, community_mode, fake_news_mode, fake_news_variant in selected_conditions
        )
    )
    launch_manifest["results"] = results
    launch_manifest["status"] = (
        "complete" if all(result["return_code"] == 0 for result in results) else "partial"
    )
    if launch_manifest["status"] == "complete":
        launch_manifest["final_study_validation"] = _validate_existing_study_runs(output_root)
    _write_json(output_root / "matrix_manifest.json", launch_manifest)
    print(json.dumps(launch_manifest, ensure_ascii=False, indent=2))
    fcntl.flock(matrix_lock, fcntl.LOCK_UN)
    matrix_lock.close()
    if launch_manifest["status"] != "complete":
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--chunk-days", type=int, default=1)
    parser.add_argument("--output-root")
    parser.add_argument("--experiment-base-db", default=str(config.EXPERIMENT_BASE_DB))
    parser.add_argument("--max-parallel-runs", type=int, default=6)
    parser.add_argument("--per-run-concurrency", type=int, default=30)
    parser.add_argument("--global-api-concurrency", type=int, default=16)
    parser.add_argument("--max-process-restarts", type=int, default=5)
    parser.add_argument("--restart-delay-seconds", type=float, default=10.0)
    parser.add_argument(
        "--conditions",
        nargs="+",
        choices=sorted(CONDITION_NAMES),
        help="Run only these condition directories; omit to run all six.",
    )
    asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    main()
