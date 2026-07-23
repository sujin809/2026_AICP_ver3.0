"""Re-open one sealed RN preflight bundle as an execution-only context.

Preflight validates external inputs, but a scheduler must never go back to
those mutable paths.  ``RNRunContext`` reconstructs the resolver output from
the run-local input copies, verifies every recorded hash, and exposes typed
dependencies for the paired runner.  Loading it performs no model or network
operation.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from twinmarket_kr.rn_ab.journal import ResponseJournal
from twinmarket_kr.rn_ab.leakage_review import (
    ArticleVersionLeakageReviewManifest,
    load_article_version_leakage_review_manifest,
)
from twinmarket_kr.rn_ab.memory import EventSchedule, PaperMemoryStore
from twinmarket_kr.rn_ab.news import SealedNewsRegistry, load_sealed_news_registry
from twinmarket_kr.rn_ab.persona_snapshot import SealedPersonaSnapshot
from twinmarket_kr.rn_ab.prompt_registry import RNPromptBundle
from twinmarket_kr.rn_ab.provenance import (
    RNSourceProvenanceError,
    assert_current_source_matches,
    validate_source_snapshot,
)
from twinmarket_kr.rn_ab.preflight_inputs import (
    GeneratedPreflightInputs,
    load_generated_preflight_inputs,
)
from twinmarket_kr.rn_ab.resolver import (
    ResolvedStudyManifest,
    canonical_sha256,
    resolve_study,
    verify_evaluator_contract_hash,
)
from twinmarket_kr.rn_ab.spec import RN_CONDITIONS, StudySpec
from twinmarket_kr.rn_ab.stage_inputs import SealedStageInputRegistry


class RNRunContextError(RuntimeError):
    """A purported run-local execution bundle is incomplete or has drifted."""


_RUNTIME_INPUT_KEYS = frozenset(
    {
        "study_spec",
        "cohort_registry",
        "calendar_event_registry",
        "stage_input_registry",
        "event_price_registry",
        "real_news_bundle",
        "known_injection_registry",
        "article_version_leakage_review",
    }
)


@dataclass(frozen=True)
class RNRunContext:
    """All immutable dependencies needed by an RN paired execution runner."""

    run_dir: Path
    run_id: str
    resolved: ResolvedStudyManifest
    evaluator_contract: Mapping[str, Any]
    event_schedule: EventSchedule
    personas: SealedPersonaSnapshot
    prompt_bundle: RNPromptBundle
    news_registry: SealedNewsRegistry
    stage_inputs: SealedStageInputRegistry
    leakage_review: ArticleVersionLeakageReviewManifest
    initial_portfolios: Mapping[str, Mapping[str, int]]
    condition_db_paths: Mapping[str, Path]
    journal_paths: Mapping[str, Path]
    source_hashes: Mapping[str, Any]
    generated_inputs: GeneratedPreflightInputs | None = None

    @property
    def manifest_sha256(self) -> str:
        return self.resolved.sha256

    @property
    def evaluator_contract_sha256(self) -> str:
        return str(self.evaluator_contract["manifest_hash"])

    @property
    def agent_ids(self) -> tuple[str, ...]:
        return self.resolved.agent_ids

    @property
    def belief_limits(self) -> Mapping[str, int]:
        return self.resolved.spec.belief_limits

    @classmethod
    def load(cls, run_dir: Path | str) -> "RNRunContext":
        """Validate and load a preflight-created run directory without writes."""

        root = _run_directory(run_dir)
        record = _load_json(_regular_file(root, "RUN_RECORD.json"), "RUN_RECORD.json")
        if record.get("artifact_type") != "rn_ab_preflight_run_record":
            raise RNRunContextError("RUN_RECORD.json is not an RN preflight record")
        if record.get("mode") != "preflight_only_no_network_no_paid_api":
            raise RNRunContextError("Run record was not created by the no-network RN preflight")
        if record.get("execution_authorized") is not False:
            raise RNRunContextError("Preflight record must not self-authorize execution")
        if record.get("network_requests") != 0 or record.get("paid_api_calls") != 0:
            raise RNRunContextError("Preflight record reports unexpected network/API activity")
        run_id = _text(record.get("run_id"), "RUN_RECORD.run_id")
        source_hashes = _load_source_hashes(root, record)

        runtime_inputs = _runtime_input_records(record)
        runtime_root = _regular_directory(root, "inputs/runtime")
        source_paths = {
            key: _recorded_input_path(root, runtime_root, runtime_inputs[key], label=key)
            for key in _RUNTIME_INPUT_KEYS
        }

        spec_payload = _load_json(source_paths["study_spec"], "sealed study_spec")
        try:
            spec = StudySpec.from_dict(spec_payload)
        except Exception as exc:
            raise RNRunContextError(f"Sealed study_spec is invalid: {exc}") from exc
        try:
            resolved = resolve_study(
                spec,
                cohort_registry_path=source_paths["cohort_registry"],
                calendar_event_registry_path=source_paths["calendar_event_registry"],
                stage_input_registry_path=source_paths["stage_input_registry"],
                input_root=runtime_root,
            )
        except Exception as exc:
            raise RNRunContextError(f"Run-local resolver reconstruction failed: {exc}") from exc

        manifest_payload = _load_json(
            _regular_file(root, "resolved_study_manifest.json"),
            "resolved_study_manifest.json",
        )
        expected_manifest = _sha256_text(
            record.get("resolved_study_manifest_sha256"),
            "RUN_RECORD.resolved_study_manifest_sha256",
        )
        if canonical_sha256(manifest_payload) != expected_manifest or resolved.sha256 != expected_manifest:
            raise RNRunContextError("Resolved manifest hash does not match the sealed run record")
        if dict(manifest_payload) != resolved.to_dict():
            raise RNRunContextError("Resolved manifest cannot be reconstructed from run-local inputs")

        evaluator_payload = _load_json(
            _regular_file(root, "evaluator_contract.json"), "evaluator_contract.json"
        )
        try:
            rebuilt_evaluator = resolved.to_evaluator_contract(
                price_registry_path=source_paths["event_price_registry"], input_root=runtime_root
            )
            verify_evaluator_contract_hash(
                evaluator_payload,
                expected_authoritative_manifest_sha256=resolved.sha256,
                expected_price_registry_sha256=str(
                    rebuilt_evaluator["price_registry"]["canonical_sha256"]
                ),
            )
        except Exception as exc:
            raise RNRunContextError(f"Sealed evaluator contract is invalid: {exc}") from exc
        if dict(evaluator_payload) != rebuilt_evaluator:
            raise RNRunContextError("Evaluator contract differs from its run-local resolver reconstruction")
        evaluator_sha = _sha256_text(
            record.get("evaluator_contract_sha256"), "RUN_RECORD.evaluator_contract_sha256"
        )
        if str(evaluator_payload.get("manifest_hash")) != evaluator_sha:
            raise RNRunContextError("Evaluator contract hash differs from RUN_RECORD")
        schedule = _event_schedule_from_contract(evaluator_payload)

        persona_dir = _regular_directory(root, _recorded_relative_path(record, "persona_snapshot"))
        prompt_dir = _regular_directory(root, _recorded_relative_path(record, "prompt_bundle"))
        try:
            personas = SealedPersonaSnapshot.load(persona_dir)
            prompt_bundle = RNPromptBundle.load(prompt_dir=prompt_dir)
        except Exception as exc:
            raise RNRunContextError(f"Sealed persona or prompt bundle is invalid: {exc}") from exc
        if personas.manifest_sha256 != resolved.spec.persona_snapshot_manifest_sha256:
            raise RNRunContextError("Sealed persona manifest differs from resolved study")
        if personas.depth_manifest_sha256 != resolved.spec.persona_depth_manifest_sha256:
            raise RNRunContextError("Sealed persona depth manifest differs from resolved study")
        personas.assert_agent_set(resolved.agent_ids)
        if prompt_bundle.canonical_sha256 != resolved.spec.prompt_bundle_sha256:
            raise RNRunContextError("Sealed prompt bundle differs from resolved study")

        try:
            stage_inputs = SealedStageInputRegistry.load(
                source_paths["stage_input_registry"],
                expected_file_sha256=runtime_inputs["stage_input_registry"]["file_sha256"],
                expected_calendar_event_registry_sha256=resolved.calendar.canonical_sha256,
                allowed_root=runtime_root,
            )
            stage_inputs.assert_matches_schedule(schedule)
            if stage_inputs.canonical_sha256 != resolved.spec.stage_input_registry_canonical_sha256:
                raise RNRunContextError("Sealed stage-input registry canonical hash differs from resolved study")
            news = load_sealed_news_registry(
                source_paths["real_news_bundle"],
                expected_file_sha256=runtime_inputs["real_news_bundle"]["file_sha256"],
                expected_bundle_sha256=resolved.spec.real_news_bundle_manifest_sha256,
                allowed_root=runtime_root,
            )
            review = load_article_version_leakage_review_manifest(
                source_paths["article_version_leakage_review"],
                expected_sha256=resolved.spec.article_version_leakage_review_manifest_sha256,
                news=news,
                expected_calendar_event_registry_sha256=resolved.calendar.canonical_sha256,
                expected_stage_input_registry_canonical_sha256=stage_inputs.canonical_sha256,
                allowed_root=runtime_root,
            )
        except RNRunContextError:
            raise
        except Exception as exc:
            raise RNRunContextError(f"Run-local runtime input validation failed: {exc}") from exc
        try:
            generated_record = _generated_input_record(record)
            generated_root = _regular_directory(root, generated_record["path"])
            generated_inputs = load_generated_preflight_inputs(
                generated_root,
                resolved=resolved,
                personas=personas,
                stage_inputs=stage_inputs,
                news=news,
            )
            expected_manifest_path = _regular_file(root, generated_record["manifest"])
            if expected_manifest_path != generated_inputs.manifest_path:
                raise RNRunContextError(
                    "Generated input manifest path differs from RUN_RECORD"
                )
            if generated_inputs.manifest_sha256 != generated_record["manifest_sha256"]:
                raise RNRunContextError(
                    "Generated input manifest hash differs from RUN_RECORD"
                )
            if dict(generated_inputs.manifest["artifacts"]) != dict(
                generated_record["artifacts"]
            ):
                raise RNRunContextError(
                    "Generated input artifact hashes differ from RUN_RECORD"
                )
        except RNRunContextError:
            raise
        except Exception as exc:
            raise RNRunContextError(f"Run-local generated inputs are invalid: {exc}") from exc

        if dict(record.get("belief_limits") or {}) != dict(resolved.spec.belief_limits):
            raise RNRunContextError("RUN_RECORD belief limits differ from the resolved study")
        portfolios = {
            agent_id: {"cash": int(personas.persona(agent_id).initial_cash), "quantity": 0}
            for agent_id in resolved.agent_ids
        }
        condition_db_paths, journal_paths = _condition_paths(root, record, resolved.sha256)
        return cls(
            run_dir=root,
            run_id=run_id,
            resolved=resolved,
            evaluator_contract=dict(evaluator_payload),
            event_schedule=schedule,
            personas=personas,
            prompt_bundle=prompt_bundle,
            news_registry=news,
            stage_inputs=stage_inputs,
            leakage_review=review,
            initial_portfolios=portfolios,
            condition_db_paths=condition_db_paths,
            journal_paths=journal_paths,
            source_hashes=source_hashes,
            generated_inputs=generated_inputs,
        )

    def assert_execution_source_tree(self) -> None:
        """Require the current RN code/dependency bytes to match preflight."""

        try:
            assert_current_source_matches(self.source_hashes)
        except RNSourceProvenanceError as exc:
            raise RNRunContextError(f"RN execution source provenance failed: {exc}") from exc

    def open_store(self, condition_id: str) -> PaperMemoryStore:
        """Open one existing arm with only dependencies already in this context."""

        if condition_id not in RN_CONDITIONS:
            raise RNRunContextError(f"Unknown RN condition: {condition_id}")
        return PaperMemoryStore(
            self.condition_db_paths[condition_id],
            run_id=self.run_id,
            condition_id=condition_id,
            manifest_sha256=self.resolved.sha256,
            event_schedule=self.event_schedule,
            news_registry=self.news_registry,
            stage_input_registry=self.stage_inputs,
            initial_portfolios=self.initial_portfolios,
            trade_policy=self.resolved.spec.trade_policy,
            belief_limits=self.belief_limits,
            depth2_search_registry=(
                None
                if self.generated_inputs is None
                else self.generated_inputs.depth2_registry
            ),
        )

    def open_journal(self, condition_id: str) -> ResponseJournal:
        if condition_id not in RN_CONDITIONS:
            raise RNRunContextError(f"Unknown RN condition: {condition_id}")
        return ResponseJournal(self.journal_paths[condition_id], manifest_sha256=self.resolved.sha256)


def _runtime_input_records(record: Mapping[str, Any]) -> Mapping[str, Mapping[str, str]]:
    raw = record.get("runtime_inputs")
    if not isinstance(raw, Mapping) or set(raw) != set(_RUNTIME_INPUT_KEYS):
        raise RNRunContextError("RUN_RECORD runtime_inputs has an invalid exact key set")
    parsed: dict[str, Mapping[str, str]] = {}
    for key in _RUNTIME_INPUT_KEYS:
        item = raw[key]
        if not isinstance(item, Mapping) or set(item) != {"path", "file_sha256"}:
            raise RNRunContextError(f"RUN_RECORD runtime input {key} has an invalid schema")
        parsed[key] = {
            "path": _text(item.get("path"), f"runtime input {key}.path"),
            "file_sha256": _sha256_text(item.get("file_sha256"), f"runtime input {key}.file_sha256"),
        }
    return parsed


def _generated_input_record(record: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = record.get("generated_preflight_inputs")
    expected = {
        "path",
        "manifest",
        "manifest_sha256",
        "human_approval_claimed",
        "network_requests",
        "paid_api_calls",
        "execution_authorized",
        "artifacts",
    }
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise RNRunContextError(
            "RUN_RECORD generated_preflight_inputs has an invalid exact schema"
        )
    if (
        raw.get("human_approval_claimed") is not False
        or raw.get("network_requests") != 0
        or raw.get("paid_api_calls") != 0
        or raw.get("execution_authorized") is not False
    ):
        raise RNRunContextError(
            "RUN_RECORD generated inputs contain an unauthorized provenance claim"
        )
    artifacts = raw.get("artifacts")
    if not isinstance(artifacts, Mapping) or len(artifacts) != 3:
        raise RNRunContextError("RUN_RECORD generated input artifact map is invalid")
    return {
        "path": _text(raw.get("path"), "generated inputs.path"),
        "manifest": _text(raw.get("manifest"), "generated inputs.manifest"),
        "manifest_sha256": _sha256_text(
            raw.get("manifest_sha256"), "generated inputs.manifest_sha256"
        ),
        "human_approval_claimed": False,
        "network_requests": 0,
        "paid_api_calls": 0,
        "execution_authorized": False,
        "artifacts": {
            _text(name, "generated input artifact filename"): _sha256_text(
                digest, f"generated input artifact {name}"
            )
            for name, digest in artifacts.items()
        },
    }


def _load_source_hashes(root: Path, record: Mapping[str, Any]) -> Mapping[str, Any]:
    item = record.get("source_hashes")
    if not isinstance(item, Mapping) or set(item) != {
        "path",
        "snapshot_sha256",
        "source_tree_sha256",
        "dependency_tree_sha256",
    }:
        raise RNRunContextError("RUN_RECORD source_hashes has an invalid exact schema")
    path = _regular_file(root, _text(item.get("path"), "RUN_RECORD.source_hashes.path"))
    payload = _load_json(path, "source_hashes.json")
    try:
        validated = validate_source_snapshot(payload)
    except RNSourceProvenanceError as exc:
        raise RNRunContextError(f"source_hashes.json is invalid: {exc}") from exc
    for key in ("snapshot_sha256", "source_tree_sha256", "dependency_tree_sha256"):
        expected = _sha256_text(item.get(key), f"RUN_RECORD.source_hashes.{key}")
        if str(validated[key]) != expected:
            raise RNRunContextError(f"source_hashes.json {key} differs from RUN_RECORD")
    return validated


def _recorded_input_path(
    root: Path,
    runtime_root: Path,
    item: Mapping[str, str],
    *,
    label: str,
) -> Path:
    path = _regular_file(root, item["path"])
    try:
        path.relative_to(runtime_root)
    except ValueError as exc:
        raise RNRunContextError(f"Runtime input {label} is outside inputs/runtime") from exc
    actual = _file_sha256(path)
    if actual != item["file_sha256"]:
        raise RNRunContextError(f"Runtime input {label} file hash differs from RUN_RECORD")
    return path


def _condition_paths(
    root: Path,
    record: Mapping[str, Any],
    manifest_sha256: str,
) -> tuple[dict[str, Path], dict[str, Path]]:
    arms = record.get("arms")
    if not isinstance(arms, Mapping) or set(arms) != set(RN_CONDITIONS):
        raise RNRunContextError("RUN_RECORD must contain exactly both RN arms")
    db_paths: dict[str, Path] = {}
    journal_paths: dict[str, Path] = {}
    for condition_id in RN_CONDITIONS:
        arm = arms[condition_id]
        if not isinstance(arm, Mapping) or set(arm) != {"community_mode", "db", "journal"}:
            raise RNRunContextError(f"RUN_RECORD arm {condition_id} has an invalid exact schema")
        expected_mode = "off" if condition_id == "RN_COMM_OFF" else "on"
        if arm.get("community_mode") != expected_mode:
            raise RNRunContextError(f"RUN_RECORD arm {condition_id} has an unexpected community mode")
        db_paths[condition_id] = _regular_file(root, _text(arm.get("db"), f"{condition_id}.db"))
        journal_path = _regular_file(root, _text(arm.get("journal"), f"{condition_id}.journal"))
        try:
            journal = ResponseJournal(journal_path, manifest_sha256=manifest_sha256)
        except Exception as exc:
            raise RNRunContextError(f"Run journal is invalid for {condition_id}: {exc}") from exc
        if journal.manifest_sha256 != manifest_sha256:
            raise RNRunContextError(f"Run journal manifest differs for {condition_id}")
        journal_paths[condition_id] = journal_path
    return db_paths, journal_paths


def _event_schedule_from_contract(contract: Mapping[str, Any]) -> EventSchedule:
    calendar = contract.get("event_calendar")
    if not isinstance(calendar, Mapping) or not isinstance(calendar.get("events"), list):
        raise RNRunContextError("Evaluator contract does not contain an event calendar")
    rows: list[dict[str, Any]] = []
    for turn, event in enumerate(calendar["events"], start=1):
        if not isinstance(event, Mapping):
            raise RNRunContextError("Evaluator event calendar contains a non-object row")
        rows.append(
            {
                "event_id": event.get("event_id"),
                "turn": turn,
                "date": event.get("date"),
                "subturn": str(event.get("session") or "").lower(),
                "execution_price": event.get("execution_price"),
            }
        )
    try:
        return EventSchedule.from_rows(rows)
    except Exception as exc:
        raise RNRunContextError(f"Evaluator event schedule is invalid: {exc}") from exc


def _run_directory(value: Path | str) -> Path:
    root = Path(os.path.abspath(os.fspath(value)))
    try:
        mode = root.lstat().st_mode
    except FileNotFoundError as exc:
        raise RNRunContextError(f"RN run directory does not exist: {root}") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise RNRunContextError("RN run directory must be a real non-symlink directory")
    return root


def _regular_directory(root: Path, relative: str) -> Path:
    path = _safe_relative(root, relative)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise RNRunContextError(f"Sealed directory is missing: {relative}") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise RNRunContextError(f"Sealed path is not a real directory: {relative}")
    return path


def _regular_file(root: Path, relative: str) -> Path:
    path = _safe_relative(root, relative)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise RNRunContextError(f"Sealed file is missing: {relative}") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise RNRunContextError(f"Sealed path is not a regular file: {relative}")
    return path


def _safe_relative(root: Path, relative: str) -> Path:
    raw = Path(relative)
    if raw.is_absolute() or not raw.parts or any(part in {"", ".", ".."} for part in raw.parts):
        raise RNRunContextError("Run-record paths must be safe relative paths")
    cursor = root
    for part in raw.parts:
        cursor = cursor / part
        try:
            mode = cursor.lstat().st_mode
        except FileNotFoundError:
            break
        if stat.S_ISLNK(mode):
            raise RNRunContextError(f"Symbolic link is forbidden in sealed run path: {relative}")
    try:
        cursor.relative_to(root)
    except ValueError as exc:  # pragma: no cover - defensive after component checks.
        raise RNRunContextError("Run-record path escapes its run directory") from exc
    return cursor


def _recorded_relative_path(record: Mapping[str, Any], key: str) -> str:
    item = record.get(key)
    if not isinstance(item, Mapping):
        raise RNRunContextError(f"RUN_RECORD is missing {key}")
    return _text(item.get("path"), f"RUN_RECORD.{key}.path")


def _load_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RNRunContextError(f"Cannot read {label}") from exc
    if not isinstance(payload, Mapping):
        raise RNRunContextError(f"{label} must be a JSON object")
    return dict(payload)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RNRunContextError(f"{label} must be a non-empty string")
    return value.strip()


def _sha256_text(value: Any, label: str) -> str:
    text = _text(value, label).lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise RNRunContextError(f"{label} must be a SHA-256 hex digest")
    return text


__all__ = ["RNRunContext", "RNRunContextError"]
