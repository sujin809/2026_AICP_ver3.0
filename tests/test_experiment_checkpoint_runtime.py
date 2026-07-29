from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from twinmarket_kr.experiment_runtime import (
    EventCheckpointRuntime,
    ExperimentCheckpointError,
    build_run_signature_payload,
    file_sha256,
    run_directory_lock,
)


def _database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE scientific_state (event_id TEXT PRIMARY KEY)"
        )
        connection.commit()


def _insert(path: Path, event_id: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO scientific_state(event_id) VALUES (?)",
            (event_id,),
        )
        connection.commit()


def _rows(path: Path) -> list[str]:
    with sqlite3.connect(path) as connection:
        return [
            str(row[0])
            for row in connection.execute(
                "SELECT event_id FROM scientific_state ORDER BY event_id"
            )
        ]


def _signature(tmp_path: Path, database: Path) -> dict[str, object]:
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir(parents=True)
    (prompt_dir / "stage.txt").write_text("fixed prompt", encoding="utf-8")
    sealed_input = tmp_path / "sealed.json"
    sealed_input.write_text('{"sealed":true}', encoding="utf-8")
    code_file = tmp_path / "engine.py"
    code_file.write_text("ENGINE = 1\n", encoding="utf-8")
    return build_run_signature_payload(
        parameters={
            "agent_ids": ["A001"],
            "event_ids": ["2026-01-02/AM", "2026-01-02/PM"],
        },
        input_files={"sealed": sealed_input},
        prompt_dir=prompt_dir,
        code_files=[code_file],
        call_policy={
            "model": "fixed-model",
            "reasoning": {"effort": "none", "exclude": True},
            "provider": {"only": ["fixed-provider"]},
        },
        initial_database_sha256=file_sha256(database),
    )


def test_running_event_rolls_back_database_append_and_rewritten_artifact(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    database = run_dir / ".runtime" / "runtime.db"
    _database(database)
    signature = _signature(tmp_path, database)
    runtime = EventCheckpointRuntime(
        run_dir,
        runtime_db=database,
        event_ids=("2026-01-02/AM", "2026-01-02/PM"),
        signature_payload=signature,
    )
    runtime.initialize_new()
    runtime.begin_event("2026-01-02/AM")
    append_log = run_dir / "agent_turns.jsonl"
    rewrite_log = run_dir / "community_best_posts.csv"
    append_log.write_text('{"first":true}\n', encoding="utf-8")
    rewrite_log.write_text("old-header\nold-row\n", encoding="utf-8")
    _insert(database, "2026-01-02/AM")
    runtime.commit_event("2026-01-02/AM")

    runtime.begin_event("2026-01-02/PM")
    with append_log.open("a", encoding="utf-8") as handle:
        handle.write('{"failed":true}\n')
    rewrite_log.write_text("new-header\nnew-row\n", encoding="utf-8")
    _insert(database, "2026-01-02/PM")
    paused = runtime.pause_running_event(
        "2026-01-02/PM",
        RuntimeError("interrupted"),
    )
    assert paused["status"] == "paused"
    assert _rows(database) == ["2026-01-02/AM"]
    assert append_log.read_text(encoding="utf-8") == '{"first":true}\n'
    assert rewrite_log.read_text(encoding="utf-8") == "old-header\nold-row\n"


def test_commit_decided_recovery_finishes_without_reexecuting_event(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    database = run_dir / ".runtime" / "runtime.db"
    _database(database)
    signature = _signature(tmp_path, database)
    runtime = EventCheckpointRuntime(
        run_dir,
        runtime_db=database,
        event_ids=("2026-01-02/AM", "2026-01-02/PM"),
        signature_payload=signature,
    )
    runtime.initialize_new()
    runtime.begin_event("2026-01-02/AM")
    _insert(database, "2026-01-02/AM")
    (run_dir / "agent_turns.jsonl").write_text(
        '{"event":"AM"}\n',
        encoding="utf-8",
    )
    decision = runtime.prepare_event_commit("2026-01-02/AM")
    assert decision["state"] == "commit_decided"

    resumed = EventCheckpointRuntime(
        run_dir,
        runtime_db=database,
        event_ids=("2026-01-02/AM", "2026-01-02/PM"),
        signature_payload=signature,
    )
    checkpoint = resumed.open_for_resume()
    assert checkpoint["completed_events"] == ["2026-01-02/AM"]
    assert checkpoint["inflight_event"] is None
    assert _rows(database) == ["2026-01-02/AM"]
    assert checkpoint["event_attempt_counts"] == {"2026-01-02/AM": 1}


def test_completed_events_must_be_a_prefix_and_signature_is_immutable(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    database = run_dir / ".runtime" / "runtime.db"
    _database(database)
    signature = _signature(tmp_path, database)
    runtime = EventCheckpointRuntime(
        run_dir,
        runtime_db=database,
        event_ids=("2026-01-02/AM", "2026-01-02/PM"),
        signature_payload=signature,
    )
    runtime.initialize_new()
    checkpoint_path = run_dir / ".runtime" / "checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["completed_events"] = ["2026-01-02/PM"]
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    with pytest.raises(
        ExperimentCheckpointError,
        match="contiguous scheduled prefix",
    ):
        runtime.open_for_resume()

    changed = dict(signature)
    changed["parameters"] = {"different": True}
    mismatched = EventCheckpointRuntime(
        run_dir,
        runtime_db=database,
        event_ids=("2026-01-02/AM", "2026-01-02/PM"),
        signature_payload=changed,
    )
    with pytest.raises(ExperimentCheckpointError):
        mismatched.open_for_resume()


def test_run_directory_lock_is_released_after_exception(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    with pytest.raises(RuntimeError, match="boom"):
        with run_directory_lock(run_dir):
            raise RuntimeError("boom")
    with run_directory_lock(run_dir) as lock_path:
        assert lock_path.is_file()
