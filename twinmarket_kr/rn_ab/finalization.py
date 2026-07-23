"""Fail-closed handoff from completed RN state to reviewable final artifacts.

The execution runner owns stage calls and the phase coordinator owns recovery.
This module owns neither.  It is deliberately a narrow final gate: every arm
must have a complete scientific lineage and no pending/rolled-back journal
responses before immutable CSV exports and a finalization record are written.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from twinmarket_kr.db.connection import connect
from twinmarket_kr.experiment_runtime import file_sha256
from twinmarket_kr.rn_ab.exports import FinalFillExporter, export_final_fill_csvs
from twinmarket_kr.rn_ab.journal import ResponseJournal
from twinmarket_kr.rn_ab.run_context import RNRunContext
from twinmarket_kr.rn_ab.spec import (
    RN_COMM_OFF,
    RN_COMM_ON,
    RN_CONDITIONS,
    canonical_sha256,
)


class RNFinalizationError(RuntimeError):
    """A run is not complete enough to become a final artifact."""


class LineageCheckedStore(FinalFillExporter, Protocol):
    manifest_sha256: str

    def assert_complete_lineage(
        self,
        expected_keys: Iterable[tuple[str, str]],
        *,
        require_finalized_outcomes: bool = True,
    ) -> dict[str, int]: ...

    def phase_consumption_digests(self) -> dict[str, str]: ...


@dataclass(frozen=True)
class RNFinalizationArtifacts:
    run_record_path: Path
    export_index_path: Path
    export_index_sha256: str


@dataclass(frozen=True)
class RNFinalHandoffReadiness:
    """Reader-facing distinction between local finalization and paper GO."""

    status: str
    missing: tuple[str, ...]
    local_finalization_present: bool
    reasoning_off_live_telemetry_validated: bool
    final_two_trading_day_p1_validated: bool

    @property
    def ready(self) -> bool:
        return not self.missing


_PRIVATE_POST_TRACE_FILENAME = "community_post_trace.jsonl"
_PRIVATE_POST_TRACE_FORMAT = "rn_private_community_post_trace_jsonl_v1"
_PRIVATE_POST_TRACE_COLUMNS = (
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
    "trace_sha256",
)


def inspect_final_handoff_readiness(
    run_dir: Path | str,
    *,
    p1_canary_run_dir: Path | str | None = None,
) -> RNFinalHandoffReadiness:
    """Inspect final handoff gates without running models or inventing P1.

    Reasoning-off telemetry has an implemented exact validator.  The final P1
    canary is deliberately kept separate: until the repository has a sealed
    two-trading-day (U=4), 100-agent/community-boundary evidence validator, an
    arbitrary file named like P1 evidence cannot make this result ready.
    """

    root = Path(run_dir)
    if not root.is_dir():
        raise RNFinalizationError(f"run_dir must be an existing directory: {root}")
    from twinmarket_kr.rn_ab.execution import (
        validate_final_run_reasoning_audits,
        validate_reasoning_off_canary_evidence,
    )
    from twinmarket_kr.rn_ab.run_context import RNRunContext

    missing: list[str] = []
    try:
        context = RNRunContext.load(root)
    except Exception as exc:
        raise RNFinalizationError(f"Cannot load sealed run for final handoff: {exc}") from exc
    try:
        validate_reasoning_off_canary_evidence(context)
        reasoning_validated = True
    except Exception:
        reasoning_validated = False
        missing.append("validated_reasoning_off_live_telemetry")

    # The complete run audit is a distinct artifact from the first-event
    # telemetry subset.  It must remain run-local and pass the same strict
    # schema/policy validator before evaluator handoff.
    try:
        validate_final_run_reasoning_audits(context)
    except Exception:
        missing.append("validated_complete_run_local_reasoning_audits")

    try:
        validate_rn_finalization_artifacts(context)
        local_finalization_present = True
    except Exception:
        local_finalization_present = False
        missing.append("valid_local_RUN_FINALIZATION")

    # P1 is always reconstructed from the separate canary RunContext and its
    # completed databases/journals.  A boolean or a conveniently named JSON
    # file in the full-run directory is never accepted as evidence.
    if p1_canary_run_dir is None:
        p1_validated = False
        missing.append(
            "validated_two_trading_day_U4_100_agent_P1_community_boundary_evidence"
        )
    else:
        try:
            from twinmarket_kr.rn_ab.p1 import validate_p1_canary_for_full_run

            validate_p1_canary_for_full_run(
                context,
                p1_canary_run_dir=p1_canary_run_dir,
            )
            p1_validated = True
        except Exception:
            p1_validated = False
            missing.append(
                "validated_two_trading_day_U4_100_agent_P1_community_boundary_evidence"
            )
    unique_missing = tuple(dict.fromkeys(missing))
    return RNFinalHandoffReadiness(
        status="READY" if not unique_missing else "NO_GO",
        missing=unique_missing,
        local_finalization_present=local_finalization_present,
        reasoning_off_live_telemetry_validated=reasoning_validated,
        final_two_trading_day_p1_validated=p1_validated,
    )


def expected_agent_event_keys(
    context: RNRunContext,
) -> tuple[tuple[str, str], ...]:
    """Derive the only complete finalization key domain from RunContext."""

    if not isinstance(context, RNRunContext):
        raise RNFinalizationError("Finalization requires a validated RNRunContext")
    keys = tuple(
        (agent_id, str(event["event_id"]))
        for agent_id in context.agent_ids
        for event in context.event_schedule.events
    )
    if (
        not keys
        or len(keys) != len(set(keys))
        or len(keys) != len(context.agent_ids) * len(context.event_schedule.events)
    ):
        raise RNFinalizationError("RunContext has an invalid agent/event key domain")
    return keys


def finalize_rn_run_artifacts(
    context: RNRunContext,
    *,
    stores: Mapping[str, LineageCheckedStore] | None = None,
    journals: Mapping[str, ResponseJournal] | None = None,
    expected_committed_calls_by_condition: Mapping[str, int] | None = None,
) -> RNFinalizationArtifacts:
    """Create final CSV artifacts only after both arms pass all local gates.

    This function intentionally has no target registry or evaluator argument.
    The target remains evaluator-only and must be joined in a later, separately
    authorized evaluation step.  Keeping that boundary prevents the final
    runtime package from importing target labels merely to write its ledger.
    """
    if not isinstance(context, RNRunContext):
        raise RNFinalizationError("Finalization requires a validated RNRunContext")
    try:
        context.assert_execution_source_tree()
    except Exception as exc:
        raise RNFinalizationError(f"Finalization source provenance differs: {exc}") from exc
    root = context.run_dir
    run_id = context.run_id
    resolved_manifest_sha256 = context.manifest_sha256
    evaluator_contract_sha256 = context.evaluator_contract_sha256
    _require_sha256(resolved_manifest_sha256, "resolved_manifest_sha256")
    _require_sha256(evaluator_contract_sha256, "evaluator_contract_sha256")
    resolved_stores = (
        {condition_id: context.open_store(condition_id) for condition_id in RN_CONDITIONS}
        if stores is None
        else dict(stores)
    )
    resolved_journals = (
        {condition_id: context.open_journal(condition_id) for condition_id in RN_CONDITIONS}
        if journals is None
        else dict(journals)
    )
    stores = resolved_stores
    journals = resolved_journals
    if set(stores) != set(RN_CONDITIONS) or set(journals) != set(RN_CONDITIONS):
        raise RNFinalizationError("Finalization requires exactly both RN arm stores and journals")
    keys = expected_agent_event_keys(context)
    if expected_committed_calls_by_condition is not None and set(expected_committed_calls_by_condition) != set(RN_CONDITIONS):
        raise RNFinalizationError("expected committed-call counts must name both RN conditions")

    record_path = root / "RUN_FINALIZATION.json"
    if record_path.exists():
        return validate_rn_finalization_artifacts(
            context,
            stores=stores,
            journals=journals,
            expected_committed_calls_by_condition=expected_committed_calls_by_condition,
        )

    lineage, journal_summaries = _collect_integrity(
        context,
        stores=stores,
        journals=journals,
        keys=keys,
        expected_committed_calls_by_condition=expected_committed_calls_by_condition,
    )

    trace_dir = root / "traces"
    if trace_dir.is_symlink():
        raise RNFinalizationError("Final export directory may not be a symbolic link")
    export_index_path = export_final_fill_csvs(
        trace_dir,
        evaluator_contract_sha256=evaluator_contract_sha256,
        stores=stores,
    )
    private_trace = _export_private_community_post_trace(
        context,
        trace_dir / _PRIVATE_POST_TRACE_FILENAME,
    )
    try:
        from twinmarket_kr.rn_ab.community_artifacts import (
            export_community_mechanism_artifacts,
        )

        community_artifacts = export_community_mechanism_artifacts(
            context,
            journals=journals,
        )
    except Exception as exc:
        raise RNFinalizationError(
            f"Community mechanism artifact export failed: {exc}"
        ) from exc
    _bind_additional_artifacts_to_export_index(
        export_index_path,
        private_trace=private_trace,
        community_artifacts=community_artifacts,
    )
    export_index_hash = file_sha256(export_index_path)
    export_index = json.loads(export_index_path.read_text(encoding="utf-8"))
    record = {
        "artifact_type": "rn_ab_finalization_record",
        "version": "1",
        "run_id": run_id,
        "status": "local_integrity_passed_pending_evaluator_only_target_join",
        "resolved_manifest_sha256": resolved_manifest_sha256,
        "evaluator_contract_sha256": evaluator_contract_sha256,
        "expected_agent_event_key_count": len(keys),
        "expected_agent_ids_sha256": canonical_sha256(list(context.agent_ids)),
        "expected_event_ids_sha256": canonical_sha256(
            [str(event["event_id"]) for event in context.event_schedule.events]
        ),
        "lineage": lineage,
        "journals": journal_summaries,
        "private_community_post_trace": private_trace,
        "community_mechanism_artifacts": community_artifacts,
        "final_fill_export_index": {
            "path": str(export_index_path.relative_to(root)),
            "sha256": export_index_hash,
            "exports": export_index["exports"],
        },
    }
    _write_immutable_json(record_path, record)
    return RNFinalizationArtifacts(
        run_record_path=record_path,
        export_index_path=export_index_path,
        export_index_sha256=export_index_hash,
    )


def validate_rn_finalization_artifacts(
    context: RNRunContext,
    *,
    stores: Mapping[str, LineageCheckedStore] | None = None,
    journals: Mapping[str, ResponseJournal] | None = None,
    expected_committed_calls_by_condition: Mapping[str, int] | None = None,
) -> RNFinalizationArtifacts:
    """Recompute the complete local handoff; do not trust record presence."""

    if not isinstance(context, RNRunContext):
        raise RNFinalizationError("Finalization validation requires RNRunContext")
    keys = expected_agent_event_keys(context)
    resolved_stores = (
        {condition_id: context.open_store(condition_id) for condition_id in RN_CONDITIONS}
        if stores is None
        else dict(stores)
    )
    resolved_journals = (
        {condition_id: context.open_journal(condition_id) for condition_id in RN_CONDITIONS}
        if journals is None
        else dict(journals)
    )
    if set(resolved_stores) != set(RN_CONDITIONS) or set(resolved_journals) != set(RN_CONDITIONS):
        raise RNFinalizationError("Finalization validation requires exactly both RN arms")
    lineage, journal_summaries = _collect_integrity(
        context,
        stores=resolved_stores,
        journals=resolved_journals,
        keys=keys,
        expected_committed_calls_by_condition=expected_committed_calls_by_condition,
    )
    record_path = context.run_dir / "RUN_FINALIZATION.json"
    record = _read_json_object(record_path, label="RUN_FINALIZATION.json")
    if (
        record.get("artifact_type") != "rn_ab_finalization_record"
        or record.get("version") != "1"
        or record.get("status")
        != "local_integrity_passed_pending_evaluator_only_target_join"
        or record.get("run_id") != context.run_id
        or record.get("resolved_manifest_sha256") != context.manifest_sha256
        or record.get("evaluator_contract_sha256")
        != context.evaluator_contract_sha256
        or record.get("expected_agent_event_key_count") != len(keys)
        or record.get("expected_agent_ids_sha256")
        != canonical_sha256(list(context.agent_ids))
        or record.get("expected_event_ids_sha256")
        != canonical_sha256(
            [str(event["event_id"]) for event in context.event_schedule.events]
        )
        or record.get("lineage") != lineage
        or record.get("journals") != journal_summaries
    ):
        raise RNFinalizationError(
            "RUN_FINALIZATION.json differs from the current complete RunContext state"
        )
    index_record = record.get("final_fill_export_index")
    if not isinstance(index_record, Mapping) or set(index_record) != {
        "path",
        "sha256",
        "exports",
    }:
        raise RNFinalizationError("RUN_FINALIZATION.json has an invalid export-index binding")
    index_path = _safe_run_relative_file(
        context.run_dir,
        index_record["path"],
        label="final-fill export index",
    )
    actual_index_sha256 = file_sha256(index_path)
    if actual_index_sha256 != index_record["sha256"]:
        raise RNFinalizationError("Final-fill export index hash differs from finalization")
    index = _read_json_object(index_path, label="final-fill export index")
    if (
        index.get("artifact_type") != "rn_final_fill_export_index"
        or index.get("version") != "1"
        or index.get("evaluator_contract_sha256")
        != context.evaluator_contract_sha256
        or not isinstance(index.get("exports"), Mapping)
        or set(index["exports"]) != set(RN_CONDITIONS)
        or index_record["exports"] != index["exports"]
    ):
        raise RNFinalizationError("Final-fill export index differs from the sealed handoff")
    private_trace = _validate_private_community_post_trace(context, index_path, index)
    if record.get("private_community_post_trace") != private_trace:
        raise RNFinalizationError(
            "RUN_FINALIZATION.json private post trace differs from its canonical database export"
        )
    community_metadata = index.get("community_artifacts")
    if not isinstance(community_metadata, Mapping):
        raise RNFinalizationError(
            "Final-fill export index lacks community mechanism artifacts"
        )
    try:
        from twinmarket_kr.rn_ab.community_artifacts import (
            validate_community_mechanism_artifacts,
        )

        rebuilt_community = validate_community_mechanism_artifacts(
            context,
            journals=resolved_journals,
            metadata=community_metadata,
        )
    except Exception as exc:
        raise RNFinalizationError(
            f"Community mechanism artifact validation failed: {exc}"
        ) from exc
    if record.get("community_mechanism_artifacts") != rebuilt_community:
        raise RNFinalizationError(
            "RUN_FINALIZATION.json community mechanism artifacts differ from canonical state"
        )
    for condition_id in RN_CONDITIONS:
        entry = index["exports"][condition_id]
        if not isinstance(entry, Mapping) or set(entry) != {
            "path",
            "sha256",
            "row_count",
            "format",
        }:
            raise RNFinalizationError(f"{condition_id} final-fill export entry is invalid")
        if (
            entry["format"] != "rn_canonical_final_fill_csv_v1"
            or entry["row_count"] != len(keys)
        ):
            raise RNFinalizationError(f"{condition_id} final-fill export shape is incomplete")
        csv_path = _safe_child_file(
            index_path.parent,
            entry["path"],
            label=f"{condition_id} final-fill CSV",
        )
        if file_sha256(csv_path) != entry["sha256"]:
            raise RNFinalizationError(f"{condition_id} final-fill CSV hash differs")
        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            row_count = sum(1 for _ in csv.DictReader(handle))
        if row_count != len(keys):
            raise RNFinalizationError(f"{condition_id} final-fill CSV row count differs")
    return RNFinalizationArtifacts(
        run_record_path=record_path,
        export_index_path=index_path,
        export_index_sha256=actual_index_sha256,
    )


def _private_community_post_trace_rows(
    context: RNRunContext,
) -> tuple[dict[str, Any], ...]:
    select_columns = ", ".join(_PRIVATE_POST_TRACE_COLUMNS)
    rows: list[dict[str, Any]] = []
    for condition_id in RN_CONDITIONS:
        with connect(context.condition_db_paths[condition_id], read_only=True) as connection:
            selected = connection.execute(
                f"""
                SELECT {select_columns}
                FROM community_post_trace
                WHERE run_id = ?
                ORDER BY phase_id, author_agent_id
                """,
                (context.run_id,),
            ).fetchall()
        if condition_id == RN_COMM_OFF and selected:
            raise RNFinalizationError(
                "RN_COMM_OFF private community post trace must be empty"
            )
        for raw in selected:
            row = {column: raw[column] for column in _PRIVATE_POST_TRACE_COLUMNS}
            if (
                row["condition_id"] != RN_COMM_ON
                or row["run_id"] != context.run_id
                or row["manifest_sha256"] != context.manifest_sha256
                or row["eligibility_status"] != "eligible"
                or row["posting_status"] not in {"posted", "skipped"}
            ):
                raise RNFinalizationError(
                    "Canonical private community post trace contains a cross-run or malformed row"
                )
            posted_fields = (row["post_id"], row["title_sha256"], row["body_sha256"])
            if (row["posting_status"] == "posted") != all(
                value is not None for value in posted_fields
            ):
                raise RNFinalizationError(
                    "Canonical private community post trace posting status is inconsistent"
                )
            rows.append(row)
    return tuple(rows)


def _private_trace_bytes(rows: tuple[dict[str, Any], ...]) -> bytes:
    return "".join(
        json.dumps(
            row,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
        for row in rows
    ).encode("utf-8")


def _private_trace_metadata(
    path: Path,
    *,
    rows: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    return {
        "path": path.name,
        "run_relative_path": f"traces/{path.name}",
        "sha256": file_sha256(path),
        "row_count": len(rows),
        "condition_row_counts": {
            RN_COMM_OFF: 0,
            RN_COMM_ON: len(rows),
        },
        "format": _PRIVATE_POST_TRACE_FORMAT,
        "privacy": "run_local_private_handoff_not_public_community_payload",
        "source_table": "community_post_trace",
        "created_at_excluded": True,
    }


def _export_private_community_post_trace(
    context: RNRunContext,
    path: Path,
) -> dict[str, Any]:
    rows = _private_community_post_trace_rows(context)
    encoded = _private_trace_bytes(rows)
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != encoded:
            raise RNFinalizationError(
                "Private community post trace already exists with different content"
            )
    else:
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    return _private_trace_metadata(path, rows=rows)


def _bind_additional_artifacts_to_export_index(
    index_path: Path,
    *,
    private_trace: Mapping[str, Any],
    community_artifacts: Mapping[str, Mapping[str, Any]],
) -> None:
    index = _read_json_object(index_path, label="final-fill export index")
    if "private_artifacts" in index or "community_artifacts" in index:
        raise RNFinalizationError(
            "Final-fill export index unexpectedly already has additional artifacts"
        )
    index["private_artifacts"] = {
        "community_post_trace": dict(private_trace)
    }
    index["community_artifacts"] = {
        key: dict(value) for key, value in community_artifacts.items()
    }
    encoded = (
        json.dumps(
            index,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    )
    temporary = index_path.with_suffix(index_path.suffix + ".private.tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(index_path)


def _validate_private_community_post_trace(
    context: RNRunContext,
    index_path: Path,
    index: Mapping[str, Any],
) -> dict[str, Any]:
    private = index.get("private_artifacts")
    if not isinstance(private, Mapping) or set(private) != {"community_post_trace"}:
        raise RNFinalizationError("Final-fill export index lacks the private post trace")
    metadata = private["community_post_trace"]
    expected_metadata_keys = {
        "path",
        "run_relative_path",
        "sha256",
        "row_count",
        "condition_row_counts",
        "format",
        "privacy",
        "source_table",
        "created_at_excluded",
    }
    if not isinstance(metadata, Mapping) or set(metadata) != expected_metadata_keys:
        raise RNFinalizationError("Private community post trace metadata is invalid")
    path = _safe_child_file(
        index_path.parent,
        metadata["path"],
        label="private community post trace",
    )
    if metadata["run_relative_path"] != f"traces/{path.name}":
        raise RNFinalizationError("Private community post trace run-relative path is invalid")
    rows = _private_community_post_trace_rows(context)
    expected = _private_trace_bytes(rows)
    if path.read_bytes() != expected:
        raise RNFinalizationError(
            "Private community post trace bytes differ from the canonical SQLite table"
        )
    rebuilt = _private_trace_metadata(path, rows=rows)
    if dict(metadata) != rebuilt:
        raise RNFinalizationError(
            "Private community post trace metadata differs from canonical content"
        )
    return rebuilt


def _collect_integrity(
    context: RNRunContext,
    *,
    stores: Mapping[str, LineageCheckedStore],
    journals: Mapping[str, ResponseJournal],
    keys: tuple[tuple[str, str], ...],
    expected_committed_calls_by_condition: Mapping[str, int] | None,
) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    lineage: dict[str, dict[str, int]] = {}
    journal_summaries: dict[str, dict[str, int]] = {}
    for condition_id in RN_CONDITIONS:
        store = stores[condition_id]
        if store.manifest_sha256 != context.manifest_sha256:
            raise RNFinalizationError(f"{condition_id} store has a different resolved manifest hash")
        try:
            lineage[condition_id] = store.assert_complete_lineage(
                keys, require_finalized_outcomes=True
            )
        except Exception as exc:
            raise RNFinalizationError(f"{condition_id} has incomplete scientific lineage: {exc}") from exc
        summary = journals[condition_id].committed_summary()
        journal_summaries[condition_id] = summary
        if summary["pending"] or summary["rolled_back"]:
            raise RNFinalizationError(
                f"{condition_id} journal is not terminally committed: {summary}"
            )
        expected_calls = (
            None
            if expected_committed_calls_by_condition is None
            else expected_committed_calls_by_condition[condition_id]
        )
        if expected_calls is not None and summary["committed"] != int(expected_calls):
            raise RNFinalizationError(
                f"{condition_id} committed journal count={summary['committed']} expected={expected_calls}"
            )
        try:
            consumed = store.phase_consumption_digests()
            committed_records = (
                journals[condition_id].committed_request_response_records()
            )
            committed = {
                logical_call_id: str(record["response_sha256"])
                for logical_call_id, record in committed_records.items()
            }
        except Exception as exc:
            raise RNFinalizationError(
                f"{condition_id} journal/phase consumption verification failed: {exc}"
            ) from exc
        if consumed != committed:
            missing = sorted(set(committed) - set(consumed))
            extra = sorted(set(consumed) - set(committed))
            hash_mismatch = sorted(
                logical_call_id
                for logical_call_id in set(consumed) & set(committed)
                if consumed[logical_call_id] != committed[logical_call_id]
            )
            raise RNFinalizationError(
                f"{condition_id} journal/phase consumption mismatch: "
                f"missing={missing}, extra={extra}, hash_mismatch={hash_mismatch}"
            )
        _validate_community_posting_request_response_lineage(
            context,
            condition_id=condition_id,
            committed_records=committed_records,
        )
        lineage[condition_id]["journal_phase_consumptions_verified"] = len(committed)
    return lineage, journal_summaries


def _validate_community_posting_request_response_lineage(
    context: RNRunContext,
    *,
    condition_id: str,
    committed_records: Mapping[str, Mapping[str, Any]],
) -> None:
    """Bind every private posting trace to its sealed request and public result.

    Hash-format checks are insufficient here: a forged but well-formed digest
    could otherwise survive to handoff.  This validator reconstructs the exact
    community-posting prompt values from the sealed persona and committed
    LTB/fill rows, then compares the journal's canonical request, its accepted
    response, the private trace, and the atomic public-board observation.
    """

    posting_records = {
        logical_call_id: record
        for logical_call_id, record in committed_records.items()
        if _logical_call_stage(logical_call_id) == "community_posting"
    }
    with connect(context.condition_db_paths[condition_id], read_only=True) as connection:
        trace_rows = connection.execute(
            """
            SELECT *
            FROM community_post_trace
            WHERE run_id = ? AND condition_id = ?
            ORDER BY phase_id, author_agent_id
            """,
            (context.run_id, condition_id),
        ).fetchall()

        if condition_id == RN_COMM_OFF:
            if trace_rows or posting_records:
                raise RNFinalizationError(
                    "RN_COMM_OFF contains a community-posting request or private trace"
                )
            return
        if condition_id != RN_COMM_ON:
            raise RNFinalizationError(
                f"Unknown condition while validating community posting: {condition_id}"
            )

        trace_by_call = {
            str(row["logical_call_id"]): row for row in trace_rows
        }
        if len(trace_by_call) != len(trace_rows):
            raise RNFinalizationError(
                "Community posting trace repeats a logical call"
            )
        if set(trace_by_call) != set(posting_records):
            raise RNFinalizationError(
                "Community posting journal requests and private traces differ"
            )

        boards = _load_public_community_boards(
            connection,
            run_id=context.run_id,
            condition_id=condition_id,
        )
        traced_posted_authors: dict[str, set[str]] = {}
        for logical_call_id, row in trace_by_call.items():
            record = posting_records[logical_call_id]
            _validate_one_community_posting_binding(
                context,
                connection=connection,
                row=row,
                logical_call_id=logical_call_id,
                record=record,
                boards=boards,
                traced_posted_authors=traced_posted_authors,
            )

        traced_phases = {str(row["phase_id"]) for row in trace_rows}
        if traced_phases != set(boards):
            raise RNFinalizationError(
                "Community posting trace phases differ from public-board phases"
            )
        for phase_id, posts_by_author in boards.items():
            if set(posts_by_author) != traced_posted_authors.get(phase_id, set()):
                raise RNFinalizationError(
                    "Community public posts differ from accepted will_post=true responses"
                )


def _validate_one_community_posting_binding(
    context: RNRunContext,
    *,
    connection: Any,
    row: Mapping[str, Any],
    logical_call_id: str,
    record: Mapping[str, Any],
    boards: Mapping[str, Mapping[str, Mapping[str, Any]]],
    traced_posted_authors: dict[str, set[str]],
) -> None:
    from twinmarket_kr.rn_ab.call_policy import (
        RN_STRICT_RESPONSE_FORMAT,
        RN_STRICT_TEMPERATURE,
    )
    from twinmarket_kr.rn_ab.community_provider import (
        COMMUNITY_CALL_MAX_TOKENS,
        POST_TYPES_GUIDE,
        _validate_posting_response,
    )
    from twinmarket_kr.rn_ab.community import FrozenCommunityPost
    from twinmarket_kr.rn_ab.execution import strict_policy_from_context
    from twinmarket_kr.rn_ab.memory import RN_AUXILIARY_STAGE_SCHEMA_VERSIONS
    from twinmarket_kr.rn_ab.prompt_registry import COMMUNITY_POSTING_STAGE
    from twinmarket_kr.rn_ab.stage_adapter import (
        RN_TRUSTED_SYSTEM_INSTRUCTION_SHA256,
        RN_TRUSTED_SYSTEM_INSTRUCTION_VERSION,
    )

    components = logical_call_id.split("|")
    if len(components) != 6:
        raise RNFinalizationError("Community posting logical-call ID is malformed")
    run_id, condition_id, agent_id, event_id, stage, schema_version = components
    expected_schema = RN_AUXILIARY_STAGE_SCHEMA_VERSIONS["community_posting"]
    if (
        run_id != context.run_id
        or condition_id != RN_COMM_ON
        or agent_id != str(row["author_agent_id"])
        or event_id != str(row["event_id"])
        or stage != "community_posting"
        or schema_version != expected_schema
    ):
        raise RNFinalizationError(
            "Community posting journal identity differs from its private trace"
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
        raise RNFinalizationError(
            "Community posting request cannot resolve its committed LTB/fill"
        )
    for source, label in ((ltb, "LTB"), (fill, "fill")):
        if (
            str(source["run_id"]) != context.run_id
            or str(source["condition_id"]) != RN_COMM_ON
            or str(source["agent_id"]) != agent_id
            or str(source["event_id"]) != event_id
            or int(source["turn"]) != int(row["turn"])
            or str(source["date"]) != str(row["date"])
        ):
            raise RNFinalizationError(
                f"Community posting {label} is cross-scoped"
            )
    if str(fill["subturn"]) != "pm":
        raise RNFinalizationError("Community posting fill is not a PM fill")
    if str(ltb["scientific_sha256"]) != str(row["ltb_sha256"]):
        raise RNFinalizationError(
            "Community posting request trace has a forged LTB hash"
        )

    view_change = _load_canonical_json_text(
        ltb["view_change_json"],
        label="community posting LTB view_change_json",
        expected_type=list,
    )
    view_change_sha = canonical_sha256(view_change)
    if (
        view_change_sha != str(row["view_change_sha256"])
        or str(row["view_change_id"])
        != "view-change:" + view_change_sha[:40]
    ):
        raise RNFinalizationError(
            "Community posting request trace has a forged view-change binding"
        )
    pre_portfolio = _load_canonical_json_text(
        fill["pre_portfolio_json"],
        label="community posting pre_portfolio_json",
        expected_type=dict,
    )
    post_portfolio = _load_canonical_json_text(
        fill["post_portfolio_json"],
        label="community posting post_portfolio_json",
        expected_type=dict,
    )
    values = {
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
    template_sha = context.prompt_bundle.support_template(
        COMMUNITY_POSTING_STAGE
    ).sha256
    if (
        str(row["prompt_template_sha256"]) != template_sha
        or str(row["prompt_values_sha256"]) != canonical_sha256(values)
    ):
        raise RNFinalizationError(
            "Community posting trace prompt hashes do not bind the sealed prompt values"
        )

    policy = strict_policy_from_context(context)
    seed_material = "|".join(
        (
            str(context.resolved.spec.study_seed),
            context.resolved.spec.seed_namespace,
            "community_posting",
            agent_id,
            event_id,
        )
    ).encode("utf-8")
    seed = int.from_bytes(hashlib.sha256(seed_material).digest()[:4], "big")
    seed &= 0x7FFFFFFF
    expected_request = {
        "contract_version": "rn-community-stage-request-v1",
        "prompt_template_stage": COMMUNITY_POSTING_STAGE,
        "prompt_template_sha256": template_sha,
        "prompt_values": values,
        "response_schema_version": expected_schema,
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
        "max_tokens": COMMUNITY_CALL_MAX_TOKENS["community_posting"],
    }
    if record.get("request") != expected_request:
        raise RNFinalizationError(
            "Community posting journal request differs from sealed runtime inputs"
        )

    response = record.get("response")
    response_sha = record.get("response_sha256")
    if (
        not isinstance(response, Mapping)
        or response_sha != canonical_sha256(response)
        or str(row["accepted_response_sha256"]) != response_sha
    ):
        raise RNFinalizationError(
            "Community posting trace does not bind the accepted journal response"
        )
    try:
        normalized_response = _validate_posting_response(response)
    except Exception as exc:
        raise RNFinalizationError(
            f"Accepted community posting response violates its runtime schema: {exc}"
        ) from exc
    phase_id = str(row["phase_id"])
    public_post = boards.get(phase_id, {}).get(agent_id)
    if normalized_response["will_post"] is False:
        if str(row["posting_status"]) != "skipped" or public_post is not None:
            raise RNFinalizationError(
                "Accepted will_post=false response has a posted trace/public post"
            )
        if any(
            row[column] is not None
            for column in ("post_id", "title_sha256", "body_sha256")
        ):
            raise RNFinalizationError(
                "Accepted will_post=false response has public content hashes"
            )
        return
    if str(row["posting_status"]) != "posted" or public_post is None:
        raise RNFinalizationError(
            "Accepted will_post=true response has no posted trace/public post"
        )
    expected_title = normalized_response["title"]
    expected_body = normalized_response["content"]
    try:
        frozen_post = FrozenCommunityPost.from_ledger(public_post)
    except Exception as exc:
        raise RNFinalizationError(
            f"Accepted posted response has an invalid frozen public post: {exc}"
        ) from exc
    expected_post_id = "post:" + canonical_sha256(
        {
            "run_id": context.run_id,
            "condition_id": RN_COMM_ON,
            "phase_id": phase_id,
            "author_agent_id": agent_id,
            "title": expected_title,
            "body_sha256": hashlib.sha256(
                expected_body.encode("utf-8")
            ).hexdigest(),
            "content_version_sha256": frozen_post.content_version_sha256,
            "post_type": normalized_response["post_type"],
        }
    )[:40]
    if (
        frozen_post.author_agent_id != agent_id
        or frozen_post.post_id != expected_post_id
        or public_post.get("post_type") != normalized_response["post_type"]
        or public_post.get("title") != expected_title
        or public_post.get("body") != expected_body
        or str(row["post_id"]) != expected_post_id
        or str(row["title_sha256"])
        != hashlib.sha256(expected_title.encode("utf-8")).hexdigest()
        or str(row["body_sha256"])
        != hashlib.sha256(expected_body.encode("utf-8")).hexdigest()
    ):
        raise RNFinalizationError(
            "Accepted posted response differs from its trace/public board"
        )
    traced_posted_authors.setdefault(phase_id, set()).add(agent_id)


def _logical_call_stage(logical_call_id: str) -> str:
    components = str(logical_call_id).split("|")
    if len(components) != 6:
        raise RNFinalizationError(
            f"Committed journal has malformed logical-call ID: {logical_call_id}"
        )
    return components[-2]


def _load_public_community_boards(
    connection: Any,
    *,
    run_id: str,
    condition_id: str,
) -> dict[str, dict[str, Mapping[str, Any]]]:
    rows = connection.execute(
        """
        SELECT stage, payload_json, payload_sha256
        FROM observation_events
        WHERE run_id = ? AND condition_id = ?
          AND stage LIKE 'community_posts:%'
        ORDER BY stage
        """,
        (run_id, condition_id),
    ).fetchall()
    boards: dict[str, dict[str, Mapping[str, Any]]] = {}
    for row in rows:
        phase_id = str(row["stage"]).removeprefix("community_posts:")
        if not phase_id or phase_id in boards:
            raise RNFinalizationError("Community public-board phase is duplicated")
        payload = _load_canonical_json_text(
            row["payload_json"],
            label=f"community public board {phase_id}",
            expected_type=dict,
        )
        if (
            canonical_sha256(payload) != str(row["payload_sha256"])
            or payload.get("schema_version") != "rn-community-posts-v1"
            or not isinstance(payload.get("phase"), Mapping)
            or payload["phase"].get("phase_id") != phase_id
            or not isinstance(payload.get("posts"), list)
        ):
            raise RNFinalizationError(
                "Community public-board observation is malformed or unhashed"
            )
        by_author: dict[str, Mapping[str, Any]] = {}
        for post in payload["posts"]:
            if not isinstance(post, Mapping) or set(post) != {
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
            }:
                raise RNFinalizationError("Community public post is malformed")
            author = post.get("author_agent_id")
            if not isinstance(author, str) or not author or author in by_author:
                raise RNFinalizationError(
                    "Community public board has a missing or duplicate author"
                )
            by_author[author] = post
        boards[phase_id] = by_author
    return boards


def _load_canonical_json_text(
    raw: Any,
    *,
    label: str,
    expected_type: type,
) -> Any:
    if not isinstance(raw, str):
        raise RNFinalizationError(f"{label} must be canonical JSON text")

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
        raise RNFinalizationError(f"{label} is not strict canonical JSON") from exc
    if not isinstance(value, expected_type) or raw != canonical:
        raise RNFinalizationError(f"{label} is not strict canonical JSON")
    return value


def _require_sha256(value: str, label: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value.lower()):
        raise RNFinalizationError(f"{label} must be a 64-character SHA-256 digest")


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RNFinalizationError(f"{label} must be a regular non-symlink file: {path}")

    def exact_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=exact_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-standard JSON constant: {value}")
            ),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RNFinalizationError(f"{label} is not strict JSON: {path}") from exc
    if not isinstance(value, dict):
        raise RNFinalizationError(f"{label} must contain one JSON object")
    return value


def _safe_run_relative_file(root: Path, value: Any, *, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise RNFinalizationError(f"{label} path is missing")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise RNFinalizationError(f"{label} path must stay inside the run directory")
    candidate = root / relative
    try:
        candidate.relative_to(root)
    except ValueError as exc:  # pragma: no cover - guarded by relative path checks.
        raise RNFinalizationError(f"{label} path escapes the run directory") from exc
    if candidate.is_symlink() or not candidate.is_file():
        raise RNFinalizationError(f"{label} must be a regular non-symlink file")
    return candidate


def _safe_child_file(root: Path, value: Any, *, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise RNFinalizationError(f"{label} filename is missing")
    relative = Path(value)
    if relative.is_absolute() or len(relative.parts) != 1 or relative.name != value:
        raise RNFinalizationError(f"{label} must use one indexed filename")
    candidate = root / relative
    if candidate.is_symlink() or not candidate.is_file():
        raise RNFinalizationError(f"{label} must be a regular non-symlink file")
    return candidate


def _write_immutable_json(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise RNFinalizationError(
                f"Finalization record destination is not a regular file: {path}"
            )
        existing = path.read_text(encoding="utf-8")
        if existing != encoded:
            raise RNFinalizationError(f"Finalization record already exists with different content: {path}")
        return
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
