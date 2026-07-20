#!/usr/bin/env python3
"""Run one logically continuous simulation with restartable date checkpoints."""
from __future__ import annotations

import argparse
import asyncio
import csv
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
    backup_database,
    build_clean_experiment_base,
    classify_restart_safety,
    file_sha256,
    validate_clean_experiment_base,
)
from twinmarket_kr.run_integrity import (
    assert_runtime_state,
    summarize_api_audit,
    validate_log_bundle,
    validate_news_inputs,
)
from twinmarket_kr.llm.validation import summarize_validation_audit
from twinmarket_kr.simulation import run_simulation, select_simulation_agents, trading_dates_between


CSV_FILES = (
    "agent_turns.csv",
    "submitted_orders.csv",
    "exchange_fills.csv",
    "daily_exchange_summary.csv",
    "community_posts.csv",
    "community_interactions.csv",
    "community_logs.csv",
    "community_best_posts.csv",
    "community_selection_inputs.csv",
)
JSONL_FILES = (
    "agent_turns.jsonl",
    "portfolio_updates.jsonl",
    "daily_exchange.jsonl",
    "community_events.jsonl",
    "community_selection_inputs.jsonl",
    "errors.jsonl",
)
PHASES = ("am", "pm", "community")


def _acquire_run_lock(run_dir: Path) -> Any:
    """Prevent two checkpoint runners from mutating the same run directory."""
    lock_path = run_dir.parent / f".{run_dir.name}.runner.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RuntimeError(
            f"Another checkpoint runner is already using this output directory: {run_dir}"
        ) from exc
    handle.seek(0)
    handle.truncate()
    handle.write(f"pid={os.getpid()}\n")
    handle.flush()
    return handle


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _remove_sqlite_target(path: Path) -> None:
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        if candidate.exists():
            candidate.unlink()


def _restore_database(source: Path, target: Path) -> None:
    backup_database(source, target)


def _split_dates(dates: list[str], size: int) -> list[list[str]]:
    if size == 0:
        return [dates]
    if size < 0:
        raise ValueError("--chunk-days must be zero or a positive integer")
    return [dates[index : index + size] for index in range(0, len(dates), size)]


def _condition_files(fake_news_mode: str, variant: str) -> tuple[Path, Path]:
    if fake_news_mode == "off":
        return config.PROCESSED_NEWS_CSV, config.DAILY_NEWS_SELECTION_CSV
    if variant == "bearish":
        return config.PROCESSED_NEWS_INJECTION_BEARISH_CSV, config.DAILY_NEWS_SELECTION_INJECTION_BEARISH_CSV
    return config.PROCESSED_NEWS_INJECTION_BULLISH_CSV, config.DAILY_NEWS_SELECTION_INJECTION_BULLISH_CSV


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


def _prompt_tree_hash() -> str:
    digest = hashlib.sha256()
    for path in sorted(config.PROMPT_DIR.rglob("*")):
        if not path.is_file():
            continue
        digest.update(str(path.relative_to(PROJECT_ROOT)).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _code_tree_hash() -> str:
    digest = hashlib.sha256()
    paths = [PROJECT_ROOT / "config.py"]
    paths.extend(sorted((PROJECT_ROOT / "twinmarket_kr").rglob("*.py")))
    paths.extend(sorted((PROJECT_ROOT / "scripts").glob("*.py")))
    for path in paths:
        digest.update(str(path.relative_to(PROJECT_ROOT)).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _signature_payload(
    *,
    args: argparse.Namespace,
    base_db: Path,
    processed_news: Path,
    daily_news: Path,
    agent_ids: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "start_date": args.start_date,
        "end_date": args.end_date,
        "chunk_days": args.chunk_days,
        "max_agents": args.max_agents,
        "agent_ids": agent_ids,
        "agent_source_db": str(config.SYS_100_DB.resolve()),
        "agent_source_db_sha256": file_sha256(config.SYS_100_DB),
        "seed": args.seed,
        "community_mode": args.community_mode,
        "fake_news_mode": args.fake_news_mode,
        "fake_news_variant": args.fake_news_variant if args.fake_news_mode == "on" else None,
        "information_mode": args.information_mode,
        "concurrency": config.SIMULATION_CONCURRENCY,
        "global_api_concurrency": config.OPENROUTER_GLOBAL_CONCURRENCY,
        "openrouter_max_retries": config.OPENROUTER_MAX_RETRIES,
        "openrouter_retry_max_delay": config.OPENROUTER_RETRY_MAX_DELAY,
        "openrouter_require_parameters": config.OPENROUTER_REQUIRE_PARAMETERS,
        "openrouter_allow_fallbacks": config.OPENROUTER_ALLOW_FALLBACKS,
        "openrouter_provider_order": config.OPENROUTER_PROVIDER_ORDER,
        "primary_model": config.OPENROUTER_MODEL,
        "community_model": config.OPENROUTER_COMMUNITY_MODEL,
        "python_version": platform.python_version(),
        "openai_package_version": _package_version("openai"),
        "base_db": str(base_db.resolve()),
        "base_db_sha256": file_sha256(base_db),
        "processed_news_csv": str(processed_news.resolve()),
        "processed_news_sha256": file_sha256(processed_news),
        "daily_news_csv": str(daily_news.resolve()),
        "daily_news_sha256": file_sha256(daily_news),
        "prompt_tree_sha256": _prompt_tree_hash(),
        "code_tree_sha256": _code_tree_hash(),
        "git_commit_at_start": _git_commit(),
    }


def _signature(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _rebuild_master_logs(run_dir: Path, chunks: list[list[str]], completed: set[int]) -> None:
    for filename in (*CSV_FILES, *JSONL_FILES):
        path = run_dir / filename
        if path.exists():
            path.unlink()

    for filename in CSV_FILES:
        writer: csv.DictWriter[str] | None = None
        output_handle = None
        try:
            for index, chunk_dates in enumerate(chunks, start=1):
                if index not in completed:
                    continue
                chunk_dir = _chunk_dir(run_dir, index, chunk_dates)
                source = chunk_dir / filename
                if not source.exists():
                    continue
                with source.open(encoding="utf-8-sig", newline="") as handle:
                    reader = csv.DictReader(handle)
                    if writer is None:
                        output_handle = (run_dir / filename).open("w", encoding="utf-8-sig", newline="")
                        writer = csv.DictWriter(output_handle, fieldnames=reader.fieldnames or [])
                        writer.writeheader()
                    for row in reader:
                        row["run_id"] = run_dir.name
                        writer.writerow(row)
        finally:
            if output_handle is not None:
                output_handle.close()

    for filename in JSONL_FILES:
        output_handle = None
        try:
            for index, chunk_dates in enumerate(chunks, start=1):
                if index not in completed:
                    continue
                source = _chunk_dir(run_dir, index, chunk_dates) / filename
                if not source.exists():
                    continue
                if output_handle is None:
                    output_handle = (run_dir / filename).open("w", encoding="utf-8")
                with source.open(encoding="utf-8") as handle:
                    for line in handle:
                        if not line.strip():
                            continue
                        payload = json.loads(line)
                        payload["run_id"] = run_dir.name
                        output_handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        finally:
            if output_handle is not None:
                output_handle.close()


def _chunk_dir(run_dir: Path, index: int, dates: list[str]) -> Path:
    return run_dir / "chunks" / f"chunk_{index:03d}_{dates[0]}_{dates[-1]}"


def _phase_key(day: str, phase: str) -> str:
    return f"{day}:{phase}"


def _capture_log_state(root: Path) -> dict[str, int]:
    if not root.exists():
        return {}
    return {
        str(path.relative_to(root)): path.stat().st_size
        for path in root.rglob("*")
        if path.is_file()
    }


def _restore_log_state(root: Path, state: dict[str, Any]) -> None:
    if root.exists():
        for path in sorted(root.rglob("*"), reverse=True):
            if path.is_file() and str(path.relative_to(root)) not in state:
                path.unlink()
    for relative, raw_size in state.items():
        path = root / relative
        if not path.exists():
            raise RuntimeError(f"Cannot restore missing pre-phase log file: {path}")
        with path.open("r+b") as handle:
            handle.truncate(int(raw_size))


def _expected_turn_from_phases(dates: list[str], completed_phases: set[str]) -> int:
    sequence = [_phase_key(day, phase) for day in dates for phase in PHASES]
    expected_prefix = set(sequence[: len(completed_phases)])
    if completed_phases != expected_prefix:
        raise RuntimeError("Completed AM/PM/community phases are not a contiguous prefix")
    if not completed_phases:
        return 0
    last_key = sequence[len(completed_phases) - 1]
    day, phase = last_key.rsplit(":", 1)
    day_index = dates.index(day)
    return day_index * 2 + (1 if phase == "am" else 2)


async def _run(args: argparse.Namespace) -> None:
    if config.OPENROUTER_MODEL != config.PAPER_OPENROUTER_MODEL:
        raise RuntimeError(
            f"Paper runs require OPENROUTER_MODEL={config.PAPER_OPENROUTER_MODEL}; "
            f"got {config.OPENROUTER_MODEL}"
        )
    if config.OPENROUTER_COMMUNITY_MODEL != config.PAPER_OPENROUTER_MODEL:
        raise RuntimeError(
            f"Paper runs require OPENROUTER_COMMUNITY_MODEL={config.PAPER_OPENROUTER_MODEL}; "
            f"got {config.OPENROUTER_COMMUNITY_MODEL}"
        )
    run_dir = Path(args.output_run_dir) if args.output_run_dir else config.LOG_DIR / (
        f"simulation_checkpointed_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
    )
    # Keep this handle alive for the whole coroutine. The OS also releases the
    # advisory lock if the runner crashes or is killed.
    run_lock = _acquire_run_lock(run_dir)
    default_processed, default_daily = _condition_files(args.fake_news_mode, args.fake_news_variant)
    processed_news = Path(args.processed_news_csv) if args.processed_news_csv else default_processed
    daily_news = Path(args.daily_news_csv) if args.daily_news_csv else default_daily
    if not processed_news.exists() or not daily_news.exists():
        raise FileNotFoundError("Required news input CSV is missing.")

    base_db = Path(args.experiment_base_db)
    if args.rebuild_experiment_base:
        report = build_clean_experiment_base(config.SIM_DB, base_db, overwrite=True)
        _write_json(base_db.with_suffix(".report.json"), report)
    elif not base_db.exists():
        report = build_clean_experiment_base(config.SIM_DB, base_db)
        _write_json(base_db.with_suffix(".report.json"), report)
    else:
        validate_clean_experiment_base(base_db)

    agents = select_simulation_agents(args.max_agents)
    agent_ids = [str(agent["agent_id"]) for agent in agents]
    active_community_agent_ids = [
        str(agent["agent_id"]) for agent in agents if int(agent.get("news_depth") or 0) >= 1
    ]
    signature_payload = _signature_payload(
        args=args,
        base_db=base_db,
        processed_news=processed_news,
        daily_news=daily_news,
        agent_ids=agent_ids,
    )
    run_signature = _signature(signature_payload)

    checkpoint_path = run_dir / "checkpoint.json"
    runtime_db = Path(args.sim_db) if args.sim_db else run_dir / "runtime_sim.db"
    config.OPENROUTER_AUDIT_LOG = run_dir / "openrouter_calls.jsonl"

    if checkpoint_path.exists():
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if checkpoint.get("run_signature") != run_signature:
            raise RuntimeError(
                "Resume configuration differs from the original run. "
                "Use the same seed, cohort, inputs, models, prompts, concurrency, and condition."
            )
        if checkpoint.get("runtime_db") != str(runtime_db.resolve()):
            raise RuntimeError("--sim-db does not match the checkpointed run.")
    else:
        if run_dir.exists():
            raise FileExistsError(f"Output run directory already exists without checkpoint: {run_dir}")
        run_dir.mkdir(parents=True)
        if runtime_db.exists():
            raise FileExistsError(f"New experiment runtime DB already exists: {runtime_db}")
        backup_database(base_db, runtime_db)
        checkpoint = {
            "schema_version": 2,
            "status": "running",
            "completed_chunks": [],
            "completed_phases": [],
            "runtime_db": str(runtime_db.resolve()),
            "run_signature": run_signature,
            "signature_payload": signature_payload,
            "state_digests": {},
        }
        _write_json(checkpoint_path, checkpoint)

    dates = trading_dates_between(
        start_date=args.start_date,
        end_date=args.end_date,
        daily_news_csv_path=daily_news,
        sim_db_path=runtime_db,
    )
    chunks = _split_dates(dates, args.chunk_days)
    if not dates or not chunks:
        raise RuntimeError("No trading dates found for requested range.")
    news_input_audit = validate_news_inputs(
        processed_news_csv=processed_news,
        daily_news_csv=daily_news,
        baseline_processed_csv=config.PROCESSED_NEWS_CSV,
        baseline_daily_csv=config.DAILY_NEWS_SELECTION_CSV,
        sim_db_path=runtime_db,
        dates=dates,
        fake_news_mode=args.fake_news_mode,
        market_close_time=config.MARKET_CLOSE_TIME,
    )
    _write_json(run_dir / "news_input_audit.json", news_input_audit)

    completed = {int(index) for index in checkpoint.get("completed_chunks", [])}
    completed_phases = {str(value) for value in checkpoint.get("completed_phases", [])}
    if completed and max(completed) > len(chunks):
        raise RuntimeError("Checkpoint contains a chunk index outside the current date range.")
    if completed and completed != set(range(1, max(completed) + 1)):
        raise RuntimeError("Checkpoint completed chunks are not contiguous.")

    inflight = checkpoint.get("inflight_phase")
    if inflight:
        _restore_database(Path(inflight["db_snapshot"]), runtime_db)
        _restore_log_state(Path(inflight["chunk_dir"]), inflight.get("log_state") or {})
        checkpoint.pop("inflight_phase", None)
        checkpoint["status"] = "paused"
        checkpoint["recovered_interrupted_phase"] = inflight.get("phase_key")
        _write_json(checkpoint_path, checkpoint)

    _rebuild_master_logs(run_dir, chunks, completed)

    expected_resume_turn = _expected_turn_from_phases(dates, completed_phases)
    initial_digest = assert_runtime_state(
        runtime_db,
        agent_ids=agent_ids,
        expected_turn=expected_resume_turn,
        phase="resume boundary",
    )
    checkpoint["resume_state_sha256"] = initial_digest
    checkpoint["status"] = "running"
    checkpoint.pop("last_error", None)
    _write_json(checkpoint_path, checkpoint)

    for index, chunk_dates in enumerate(chunks, start=1):
        if index in completed:
            continue
        if completed != set(range(1, index)):
            raise RuntimeError("Chunks must be completed contiguously before resuming.")
        chunk_dir = _chunk_dir(run_dir, index, chunk_dates)
        chunk_day_offset = sum(len(chunk) for chunk in chunks[: index - 1])
        chunk_turn_offset = chunk_day_offset * 2
        before_digest = assert_runtime_state(
            runtime_db,
            agent_ids=agent_ids,
            expected_turn=_expected_turn_from_phases(dates, completed_phases),
            phase=f"chunk {index} before",
        )
        for day_index_within_chunk, day in enumerate(chunk_dates):
            global_day_offset = chunk_day_offset + day_index_within_chunk
            for phase in PHASES:
                phase_key = _phase_key(day, phase)
                if phase_key in completed_phases:
                    continue
                snapshot = (
                    run_dir
                    / "checkpoints"
                    / f"before_day_{global_day_offset + 1:03d}_{day}_{phase}.db"
                )
                if snapshot.exists():
                    _restore_database(snapshot, runtime_db)
                else:
                    backup_database(runtime_db, snapshot)
                log_state = _capture_log_state(chunk_dir)
                checkpoint["inflight_phase"] = {
                    "phase_key": phase_key,
                    "chunk": index,
                    "chunk_dir": str(chunk_dir.resolve()),
                    "db_snapshot": str(snapshot.resolve()),
                    "log_state": log_state,
                }
                checkpoint["status"] = "running"
                _write_json(checkpoint_path, checkpoint)
                try:
                    await run_simulation(
                        max_agents=args.max_agents,
                        random_seed=args.seed,
                        start_date=day,
                        end_date=day,
                        information_mode=args.information_mode,
                        processed_news_csv=processed_news,
                        daily_news_csv=daily_news,
                        fake_news_mode=args.fake_news_mode,
                        fake_news_variant=(
                            args.fake_news_variant if args.fake_news_mode == "on" else None
                        ),
                        community_mode=args.community_mode,
                        sim_db=runtime_db,
                        reset_runtime_tables=False,
                        turn_offset=global_day_offset * 2,
                        day_offset=global_day_offset,
                        log_root=run_dir / "chunks",
                        log_run_id=chunk_dir.name,
                        phases=(phase,),
                        append_existing_logs=bool(log_state),
                    )
                    expected_turn = global_day_offset * 2 + (1 if phase == "am" else 2)
                    phase_digest = assert_runtime_state(
                        runtime_db,
                        agent_ids=agent_ids,
                        expected_turn=expected_turn,
                        phase=f"{phase_key} complete",
                    )
                except Exception as exc:
                    _restore_database(snapshot, runtime_db)
                    _restore_log_state(chunk_dir, log_state)
                    restart_safety = classify_restart_safety(exc)
                    checkpoint.pop("inflight_phase", None)
                    checkpoint["status"] = "paused"
                    checkpoint["failed_chunk"] = index
                    checkpoint["failed_phase"] = phase_key
                    checkpoint["last_error"] = {
                        "type": type(exc).__name__,
                        "message": str(exc),
                        **restart_safety,
                    }
                    _write_json(checkpoint_path, checkpoint)
                    _write_json(
                        run_dir / "paused.json",
                        {
                            "status": "paused",
                            "failed_chunk": index,
                            "failed_phase": phase_key,
                            "restart_command": "rerun the identical command to resume",
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                            **restart_safety,
                        },
                    )
                    raise

                completed_phases.add(phase_key)
                checkpoint["completed_phases"] = [
                    _phase_key(sequence_day, sequence_phase)
                    for sequence_day in dates
                    for sequence_phase in PHASES
                    if _phase_key(sequence_day, sequence_phase) in completed_phases
                ]
                checkpoint["phase_state_digests"] = checkpoint.get("phase_state_digests") or {}
                checkpoint["phase_state_digests"][phase_key] = phase_digest
                checkpoint.pop("inflight_phase", None)
                checkpoint.pop("failed_phase", None)
                checkpoint.pop("last_error", None)
                _write_json(checkpoint_path, checkpoint)
                # Once the completed phase and its state digest are durable, its
                # pre-phase rollback image is no longer needed. Keeping all 189
                # images would consume several GB per condition.
                _remove_sqlite_target(snapshot)

        integrity = validate_log_bundle(
            chunk_dir,
            dates=chunk_dates,
            agent_ids=agent_ids,
            active_community_agent_ids=active_community_agent_ids,
            turn_offset=chunk_turn_offset,
            fake_news_mode=args.fake_news_mode,
            daily_news_csv=daily_news,
            community_mode=args.community_mode,
        )
        after_turn = chunk_turn_offset + len(chunk_dates) * 2
        after_digest = assert_runtime_state(
            runtime_db,
            agent_ids=agent_ids,
            expected_turn=after_turn,
            phase=f"chunk {index} after",
        )
        _write_json(
            chunk_dir / "integrity_report.json",
            {**integrity, "before_state_sha256": before_digest, "after_state_sha256": after_digest},
        )

        completed.add(index)
        checkpoint["completed_chunks"] = sorted(completed)
        checkpoint["state_digests"][str(index)] = after_digest
        checkpoint["status"] = "running"
        checkpoint.pop("failed_chunk", None)
        checkpoint.pop("last_error", None)
        _write_json(checkpoint_path, checkpoint)
        _rebuild_master_logs(run_dir, chunks, completed)

    final_integrity = validate_log_bundle(
        run_dir,
        dates=dates,
        agent_ids=agent_ids,
        active_community_agent_ids=active_community_agent_ids,
        turn_offset=0,
        fake_news_mode=args.fake_news_mode,
        daily_news_csv=daily_news,
        community_mode=args.community_mode,
    )
    final_digest = assert_runtime_state(
        runtime_db,
        agent_ids=agent_ids,
        expected_turn=len(dates) * 2,
        phase="run complete",
    )
    offline_llm = os.getenv("TWINMARKET_OFFLINE_LLM", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    if offline_llm:
        api_audit_summary = {
            "api_audit_mode": "offline_stub",
            "api_audit_count": 0,
            "api_audit_path": str(config.OPENROUTER_AUDIT_LOG),
        }
    else:
        api_audit_summary = summarize_api_audit(
            config.OPENROUTER_AUDIT_LOG,
            expected_model=config.PAPER_OPENROUTER_MODEL,
        )
    metadata = {
        **signature_payload,
        "run_id": run_dir.name,
        "run_signature": run_signature,
        "date_count": len(dates),
        "turn_count": len(dates) * 2,
        "chunk_count": len(chunks),
        "runtime_db": str(runtime_db.resolve()),
        "chunks": [
            {
                "index": index,
                "start_date": chunk_dates[0],
                "end_date": chunk_dates[-1],
                "turn_offset": sum(len(chunk) for chunk in chunks[: index - 1]) * 2,
                "status": "complete",
            }
            for index, chunk_dates in enumerate(chunks, start=1)
        ],
        "integrity": final_integrity,
        "news_input_audit": news_input_audit,
        **api_audit_summary,
        **summarize_validation_audit(),
        "final_state_sha256": final_digest,
    }
    _write_json(run_dir / "run_metadata.json", metadata)
    checkpoint["status"] = "complete"
    checkpoint["final_state_sha256"] = final_digest
    _write_json(checkpoint_path, checkpoint)
    paused_path = run_dir / "paused.json"
    if paused_path.exists():
        paused_path.unlink()
    _write_json(
        run_dir / "run_complete.json",
        {"run_id": run_dir.name, "status": "complete", "log_dir": str(run_dir)},
    )
    fcntl.flock(run_lock, fcntl.LOCK_UN)
    run_lock.close()
    print(f"log_dir={run_dir}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-agents", type=int, default=30)
    parser.add_argument("--seed", type=int, default=2)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument(
        "--chunk-days",
        type=int,
        default=1,
        help="Checkpoint interval in trading days; use 0 for one uninterrupted chunk.",
    )
    parser.add_argument("--information-mode", choices=("pre_close_cutoff", "same_day", "prior_close"), default="pre_close_cutoff")
    parser.add_argument("--community-mode", choices=("off", "on"), default="on")
    parser.add_argument("--fake-news-mode", choices=("off", "on"), default="on")
    parser.add_argument("--fake-news-variant", choices=("bearish", "bullish"), default="bearish")
    parser.add_argument("--processed-news-csv")
    parser.add_argument("--daily-news-csv")
    parser.add_argument("--sim-db")
    parser.add_argument("--output-run-dir")
    parser.add_argument("--experiment-base-db", default=str(config.EXPERIMENT_BASE_DB))
    parser.add_argument("--rebuild-experiment-base", action="store_true")
    asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    main()
