"""Strict, local-only preflight factory for the RN Community A/B paper run.

This module deliberately creates a sealed *preflight* bundle only.  It does
not instantiate an LLM client, make a network request, or execute a paid
experiment.  A future atomic phase runner must consume this bundle rather than
the legacy scripts/``current`` namespace.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from twinmarket_kr.rn_ab.journal import ResponseJournal
from twinmarket_kr.rn_ab.leakage_review import (
    LeakageReviewError,
    load_article_version_leakage_review_manifest,
)
from twinmarket_kr.rn_ab.memory import EventSchedule, PaperMemoryStore
from twinmarket_kr.rn_ab.initial_state import deterministic_initial_ltb
from twinmarket_kr.rn_ab.news import SealedNewsRegistry, load_sealed_news_registry
from twinmarket_kr.rn_ab.persona_snapshot import (
    DEPTH_MANIFEST_FILENAME,
    REPAIR_MANIFEST_FILENAME,
    SNAPSHOT_DB_FILENAME,
    SNAPSHOT_MANIFEST_FILENAME,
    PersonaSnapshotError,
    SealedPersonaSnapshot,
    persona_renderer_sha256,
)
from twinmarket_kr.rn_ab.prompt_registry import (
    ALL_PROMPT_FILENAMES,
    RNPromptBundle,
    RNPromptError,
)
from twinmarket_kr.rn_ab.provenance import RNSourceProvenanceError, build_source_snapshot
from twinmarket_kr.rn_ab.preflight_inputs import (
    PreflightInputError,
    build_generated_preflight_inputs,
)
from twinmarket_kr.rn_ab.run_record import (
    RNRunRecordError,
    render_preflight_condition_record,
    render_preflight_pair_record,
)
from twinmarket_kr.rn_ab.resolver import (
    RN_CONDITIONS,
    PathSafetyError,
    ResolutionError,
    ResolvedStudyManifest,
    assert_safe_input_file,
    canonical_json_bytes,
    canonical_sha256,
    ensure_safe_run_directory,
    load_study_spec,
    resolve_study,
    write_resolved_manifest,
)
from twinmarket_kr.rn_ab.spec import StudySpec


class RunBundleError(RuntimeError):
    """Raised when a local RN preflight input/output is not sealed-safe."""


_RUN_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{2,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
_GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$", re.IGNORECASE)
_LEGACY_NAMESPACE_RE = re.compile(
    r"(?:^|[_-])(?:c00|c01|c02|c10|c11|c12|rn_c00|rn_c10)(?:[_-]|$)",
    re.IGNORECASE,
)
_FORBIDDEN_ROOT_COMPONENTS = frozenset({"current", "latest"})
_KNOWN_INJECTION_ROOT_FIELDS = {"artifact_type", "version", "entries"}
_KNOWN_INJECTION_ENTRY_FIELDS = {"injection_id", "title", "row_sha256"}


@dataclass(frozen=True)
class RNPreflightBundle:
    """Run-local artifacts produced with zero model/API calls."""

    run_id: str
    run_dir: Path
    resolved_manifest_path: Path
    evaluator_contract_path: Path
    leakage_review_path: Path
    persona_snapshot_dir: Path
    prompt_bundle_dir: Path
    runtime_inputs_dir: Path
    generated_inputs_dir: Path
    generated_input_manifest_path: Path
    run_record_path: Path
    run_record_markdown_path: Path
    source_hashes_path: Path
    condition_db_paths: Mapping[str, Path]
    journal_paths: Mapping[str, Path]
    resolved_manifest_sha256: str
    evaluator_contract_sha256: str
    persona_prompt_map_sha256: str
    prompt_bundle_sha256: str


def prepare_preflight_bundle(
    *,
    run_id: str,
    input_root: Path | str,
    output_root: Path | str,
    study_spec_path: Path | str,
    cohort_registry_path: Path | str,
    persona_snapshot_dir: Path | str,
    prompt_dir: Path | str,
    calendar_event_registry_path: Path | str,
    stage_input_registry_path: Path | str,
    event_price_registry_path: Path | str,
    real_news_bundle_path: Path | str,
    known_injection_registry_path: Path | str,
    article_version_leakage_review_manifest_path: Path | str,
) -> RNPreflightBundle:
    """Create sealed RN arm directories after all local provenance checks.

    The entry point is intentionally preflight-only.  It gives a reviewer
    deterministic run-local manifests/DB namespaces to inspect, while making
    an accidental paid invocation impossible from this module.
    """
    run = _validated_run_id(run_id)
    safe_input_root = _safe_artifact_root(input_root, label="input_root")
    safe_output_root = _safe_artifact_root(output_root, label="output_root")
    try:
        spec = load_study_spec(study_spec_path, input_root=safe_input_root)
        checked_out_baseline_commit = _verify_checked_out_baseline(spec.baseline_commit)
        resolved = resolve_study(
            spec,
            cohort_registry_path=cohort_registry_path,
            calendar_event_registry_path=calendar_event_registry_path,
            stage_input_registry_path=stage_input_registry_path,
            input_root=safe_input_root,
        )
        evaluator_contract = resolved.to_evaluator_contract(
            price_registry_path=event_price_registry_path,
            input_root=safe_input_root,
        )
        persona_snapshot = _load_and_validate_persona_snapshot(
            spec=spec,
            resolved=resolved,
            snapshot_dir=persona_snapshot_dir,
            input_root=safe_input_root,
        )
        prompt_bundle = _load_and_validate_prompt_bundle(
            spec=spec,
            prompt_dir=prompt_dir,
            input_root=safe_input_root,
        )
    except (PathSafetyError, ResolutionError, PersonaSnapshotError, RNPromptError, ValueError) as exc:
        raise RunBundleError(f"RN preflight resolver failure: {exc}") from exc

    news = _load_and_validate_news(
        spec=spec,
        resolved=resolved,
        path=real_news_bundle_path,
        input_root=safe_input_root,
    )
    known_injection_sha = _validate_known_injection_registry(
        spec=spec,
        path=known_injection_registry_path,
        input_root=safe_input_root,
        news=news,
    )
    review = _load_and_validate_article_version_review(
        spec=spec,
        resolved=resolved,
        path=article_version_leakage_review_manifest_path,
        input_root=safe_input_root,
        news=news,
    )
    schedule = _event_schedule_from_evaluator_contract(evaluator_contract)
    stage_inputs = _stage_inputs_from_resolved(
        resolved,
        stage_input_registry_path,
        safe_input_root,
        expected_schedule=schedule,
    )
    initial_portfolios = {
        member.agent_id: {"cash": member.initial_cash, "quantity": 0}
        for member in resolved.cohort.members
    }
    if len(initial_portfolios) != spec.required_agent_count:
        raise RunBundleError("Resolved cohort cannot produce one initial portfolio per required agent")

    root = safe_output_root
    run_dir = root / run
    try:
        write_resolved_manifest(resolved, run_dir=run_dir, output_root=root)
    except (PathSafetyError, ResolutionError, OSError) as exc:
        raise RunBundleError(f"Cannot create sealed RN run directory: {exc}") from exc

    manifest_path = run_dir / "resolved_study_manifest.json"
    run_inputs_dir = run_dir / "inputs"
    run_inputs_dir.mkdir(exist_ok=False)
    try:
        sealed_persona_snapshot = persona_snapshot.seal_into(run_inputs_dir / "persona")
        sealed_prompt_bundle = prompt_bundle.seal_into(run_inputs_dir / "prompts")
        generated_inputs = build_generated_preflight_inputs(
            destination_dir=run_inputs_dir / "generated",
            resolved=resolved,
            personas=sealed_persona_snapshot,
            stage_inputs=stage_inputs,
            news=news,
        )
        runtime_input_records = _seal_runtime_input_files(
            run_dir=run_dir,
            destination_root=run_inputs_dir / "runtime",
            input_root=safe_input_root,
            sources={
                "study_spec": (study_spec_path, "study_spec.json"),
                "cohort_registry": (cohort_registry_path, "cohort_registry.json"),
                "calendar_event_registry": (
                    calendar_event_registry_path,
                    "calendar_event_registry.json",
                ),
                "stage_input_registry": (stage_input_registry_path, "stage_input_registry.json"),
                "event_price_registry": (event_price_registry_path, "event_price_registry.json"),
                "real_news_bundle": (real_news_bundle_path, "real_news_bundle.json"),
                "known_injection_registry": (
                    known_injection_registry_path,
                    "known_injection_registry.json",
                ),
                "article_version_leakage_review": (
                    article_version_leakage_review_manifest_path,
                    "article_version_leakage_review_manifest.json",
                ),
            },
        )
    except (OSError, PersonaSnapshotError, RNPromptError, PreflightInputError) as exc:
        raise RunBundleError(
            f"Cannot seal RN persona/prompt/generated inputs into the run bundle: {exc}"
        ) from exc
    evaluator_path = _write_json_exclusive(run_dir / "evaluator_contract.json", evaluator_contract)
    review_path = _write_json_exclusive(
        run_dir / "article_version_leakage_review_manifest.json",
        review.to_dict(),
    )
    try:
        source_snapshot = build_source_snapshot(
            baseline_commit=spec.baseline_commit,
            checked_out_commit=checked_out_baseline_commit,
        )
        source_hashes_path = _write_json_exclusive(run_dir / "source_hashes.json", source_snapshot)
    except (OSError, RNSourceProvenanceError) as exc:
        raise RunBundleError(f"Cannot freeze the RN execution source tree: {exc}") from exc
    condition_dbs: dict[str, Path] = {}
    journals: dict[str, Path] = {}
    clean_base: dict[str, Mapping[str, Any]] = {}
    for condition_id in RN_CONDITIONS:
        condition_dir = ensure_safe_run_directory(
            run_dir / condition_id,
            output_root=run_dir,
            allow_existing=False,
        )
        db_path = condition_dir / "paper_run.sqlite"
        store = PaperMemoryStore(
            db_path,
            run_id=run,
            condition_id=condition_id,
            manifest_sha256=resolved.sha256,
            event_schedule=schedule,
            news_registry=news,
            stage_input_registry=stage_inputs,
            initial_portfolios=initial_portfolios,
            trade_policy=spec.trade_policy,
            belief_limits=spec.belief_limits,
            depth2_search_registry=generated_inputs.depth2_registry,
        )
        first_date = str(schedule.events[0]["date"])
        for agent_id in resolved.agent_ids:
            store.bootstrap_ltb(
                agent_id=agent_id,
                date=first_date,
                dimensions=deterministic_initial_ltb(sealed_persona_snapshot.persona(agent_id)),
            )
        clean_base[condition_id] = store.assert_clean_base()
        journal_path = condition_dir / "response_journal.sqlite"
        ResponseJournal(journal_path, manifest_sha256=resolved.sha256)
        condition_dbs[condition_id] = db_path
        journals[condition_id] = journal_path

    record = {
        "artifact_type": "rn_ab_preflight_run_record",
        "version": "1",
        "run_id": run,
        "mode": "preflight_only_no_network_no_paid_api",
        "network_requests": 0,
        "paid_api_calls": 0,
        "execution_authorized": False,
        "baseline_commit": spec.baseline_commit,
        "checked_out_baseline_commit": checked_out_baseline_commit,
        "resolved_study_manifest_sha256": resolved.sha256,
        "evaluator_contract_sha256": canonical_sha256(
            {key: value for key, value in evaluator_contract.items() if key not in {"manifest_hash", "resolved_manifest_sha256"}}
        ),
        "real_news_bundle_manifest_sha256": news.bundle_sha256,
        "known_injection_registry_sha256": known_injection_sha,
        "article_version_leakage_review_manifest_sha256": review.canonical_sha256,
        "article_version_leakage_review_manifest": str(review_path.relative_to(run_dir)),
        "stage_input_registry": {
            "file_sha256": spec.stage_input_registry_file_sha256,
            "canonical_sha256": spec.stage_input_registry_canonical_sha256,
        },
        "persona_snapshot": {
            "path": str(sealed_persona_snapshot.snapshot_dir.relative_to(run_dir)),
            "source_db_sha256": sealed_persona_snapshot.source_db_sha256,
            "snapshot_db_sha256": sealed_persona_snapshot.snapshot_db_sha256,
            "snapshot_manifest_sha256": sealed_persona_snapshot.manifest_sha256,
            "depth_manifest_sha256": sealed_persona_snapshot.depth_manifest_sha256,
            "repair_manifest_sha256": sealed_persona_snapshot.repair_manifest_sha256,
            "ordered_agent_prompt_map_sha256": sealed_persona_snapshot.prompt_map_sha256,
        },
        "prompt_bundle": {
            "path": str(sealed_prompt_bundle.prompt_dir.relative_to(run_dir)),
            "canonical_sha256": sealed_prompt_bundle.canonical_sha256,
            "templates": sealed_prompt_bundle.manifest()["templates"],
            "support_templates": sealed_prompt_bundle.manifest()["support_templates"],
        },
        "source_hashes": {
            "path": str(source_hashes_path.relative_to(run_dir)),
            "snapshot_sha256": source_snapshot["snapshot_sha256"],
            "source_tree_sha256": source_snapshot["source_tree_sha256"],
            "dependency_tree_sha256": source_snapshot["dependency_tree_sha256"],
        },
        "runtime_inputs": runtime_input_records,
        "generated_preflight_inputs": {
            "path": str(generated_inputs.root.relative_to(run_dir)),
            "manifest": str(generated_inputs.manifest_path.relative_to(run_dir)),
            "manifest_sha256": generated_inputs.manifest_sha256,
            "human_approval_claimed": False,
            "network_requests": 0,
            "paid_api_calls": 0,
            "execution_authorized": False,
            "artifacts": dict(generated_inputs.manifest["artifacts"]),
        },
        "belief_limits": dict(spec.belief_limits),
        "trade_policy_sha256": canonical_sha256(spec.trade_policy.to_dict()),
        "clean_base": clean_base,
        "community_timing_policy_sha256": spec.community_timing_policy_sha256,
        "arms": {
            condition_id: {
                "community_mode": spec.condition_treatments[condition_id]["community_mode"],
                "db": str(condition_dbs[condition_id].relative_to(run_dir)),
                "journal": str(journals[condition_id].relative_to(run_dir)),
            }
            for condition_id in RN_CONDITIONS
        },
    }
    record_path = _write_json_exclusive(run_dir / "RUN_RECORD.json", record)
    try:
        run_record_markdown_path = _write_bytes_exclusive(
            run_dir / "RUN_RECORD.md",
            render_preflight_pair_record(record).encode("utf-8"),
        )
        for condition_id in RN_CONDITIONS:
            _write_bytes_exclusive(
                run_dir / condition_id / "RUN_RECORD.md",
                render_preflight_condition_record(record, condition_id=condition_id).encode("utf-8"),
            )
    except (OSError, RNRunRecordError) as exc:
        raise RunBundleError(f"Cannot render RN run record index: {exc}") from exc
    return RNPreflightBundle(
        run_id=run,
        run_dir=run_dir,
        resolved_manifest_path=manifest_path,
        evaluator_contract_path=evaluator_path,
        leakage_review_path=review_path,
        persona_snapshot_dir=sealed_persona_snapshot.snapshot_dir,
        prompt_bundle_dir=sealed_prompt_bundle.prompt_dir,
        runtime_inputs_dir=run_inputs_dir / "runtime",
        generated_inputs_dir=generated_inputs.root,
        generated_input_manifest_path=generated_inputs.manifest_path,
        run_record_path=record_path,
        run_record_markdown_path=run_record_markdown_path,
        source_hashes_path=source_hashes_path,
        condition_db_paths=condition_dbs,
        journal_paths=journals,
        resolved_manifest_sha256=resolved.sha256,
        evaluator_contract_sha256=str(evaluator_contract["manifest_hash"]),
        persona_prompt_map_sha256=sealed_persona_snapshot.prompt_map_sha256,
        prompt_bundle_sha256=sealed_prompt_bundle.canonical_sha256,
    )


def _validated_run_id(value: str) -> str:
    if not isinstance(value, str) or not _RUN_ID_RE.fullmatch(value):
        raise RunBundleError("run_id must be a safe lowercase identifier of at least three characters")
    lowered = value.lower()
    if "current" in lowered or re.search(r"(?:^|[_-])(?:c00|c01|c02|c10|c11|c12)(?:[_-]|$)", lowered):
        raise RunBundleError("run_id may not use current or a legacy condition namespace")
    return value


def _load_and_validate_persona_snapshot(
    *,
    spec: StudySpec,
    resolved: ResolvedStudyManifest,
    snapshot_dir: Path | str,
    input_root: Path,
) -> SealedPersonaSnapshot:
    """Require the exact renderer-repaired snapshot, never a global DB fallback."""
    candidate = Path(snapshot_dir)
    checked = {
        name: assert_safe_input_file(candidate / name, allowed_root=input_root)
        for name in (
            SNAPSHOT_DB_FILENAME,
            SNAPSHOT_MANIFEST_FILENAME,
            DEPTH_MANIFEST_FILENAME,
            REPAIR_MANIFEST_FILENAME,
        )
    }
    parents = {path.parent for path in checked.values()}
    if len(parents) != 1:
        raise RunBundleError("RN persona snapshot artifacts must share one input directory")
    snapshot = SealedPersonaSnapshot.load(next(iter(parents)))
    if snapshot.manifest_sha256 != spec.persona_snapshot_manifest_sha256:
        raise RunBundleError("RN persona snapshot manifest differs from the StudySpec pin")
    if snapshot.depth_manifest_sha256 != spec.persona_depth_manifest_sha256:
        raise RunBundleError("RN persona depth manifest differs from the StudySpec pin")
    if persona_renderer_sha256() != spec.persona_renderer_sha256:
        raise RunBundleError("RN persona renderer differs from the StudySpec pin")
    snapshot.assert_agent_set(resolved.agent_ids)
    for member in resolved.cohort.members:
        persona = snapshot.persona(member.agent_id)
        if (
            persona.news_depth != member.news_depth
            or persona.initial_cash != member.initial_cash
            or persona.persona_sha256 != member.persona_sha256
        ):
            raise RunBundleError(
                "RN persona snapshot/cohort registry mismatch for " + member.agent_id
            )
    return snapshot


def _load_and_validate_prompt_bundle(
    *,
    spec: StudySpec,
    prompt_dir: Path | str,
    input_root: Path,
) -> RNPromptBundle:
    """Load and hash-seal all eleven reviewed common prompts as one bundle."""
    candidate = Path(prompt_dir)
    # Check every required source file through the same no-symlink input gate
    # used by registry artifacts before the bundle loader reads it.
    for filename in ALL_PROMPT_FILENAMES:
        assert_safe_input_file(candidate / filename, allowed_root=input_root)
    bundle = RNPromptBundle.load(prompt_dir=candidate)
    if bundle.canonical_sha256 != spec.prompt_bundle_sha256:
        raise RunBundleError("RN prompt bundle differs from the StudySpec pin")
    return bundle


def _safe_artifact_root(value: Path | str, *, label: str) -> Path:
    """Reject aliases and legacy namespaces before they become a trust root.

    ``Path.resolve()`` is deliberately not used here: resolving first would
    erase a caller-supplied ``current`` or symlink component before policy can
    inspect it.  Individual files are subsequently checked by
    :func:`assert_safe_input_file`; this gate protects the root itself.
    """

    candidate = Path(os.path.abspath(os.fspath(value)))
    try:
        mode = candidate.lstat().st_mode
    except FileNotFoundError as exc:
        raise RunBundleError(f"{label} must be an existing directory: {candidate}") from exc
    if stat.S_ISLNK(mode):
        raise RunBundleError(f"{label} may not be a symbolic link: {candidate}")
    if not stat.S_ISDIR(mode):
        raise RunBundleError(f"{label} must be a real directory: {candidate}")
    for component in candidate.parts:
        lowered = component.lower()
        if lowered in _FORBIDDEN_ROOT_COMPONENTS or _LEGACY_NAMESPACE_RE.search(component):
            raise RunBundleError(f"{label} contains a forbidden RN namespace component: {component}")
    return candidate


def _verify_checked_out_baseline(expected_commit: str) -> str:
    """Require the code checkout itself to be the StudySpec's claimed baseline."""

    repository_root = Path(__file__).resolve().parents[2]
    try:
        completed = subprocess.run(
            ["git", "-C", os.fspath(repository_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RunBundleError("Cannot verify the checked-out RN baseline commit") from exc
    actual = completed.stdout.strip().lower()
    if not _GIT_COMMIT_RE.fullmatch(actual):
        raise RunBundleError("Git did not return a valid checked-out baseline commit")
    if actual != expected_commit.lower():
        raise RunBundleError(
            "StudySpec baseline_commit differs from the checked-out repository commit: "
            f"expected={expected_commit.lower()} actual={actual}"
        )
    return actual


def _load_and_validate_news(
    *,
    spec: StudySpec,
    resolved: ResolvedStudyManifest,
    path: Path | str,
    input_root: Path | str,
) -> SealedNewsRegistry:
    try:
        news = load_sealed_news_registry(
            path,
            expected_bundle_sha256=spec.real_news_bundle_manifest_sha256,
            allowed_root=input_root,
        )
    except (NewsBundleError, OSError) as exc:  # type: ignore[name-defined]
        raise RunBundleError(f"Sealed real-news bundle is invalid: {exc}") from exc
    expected_event_ids = {event.decision_event_id for event in resolved.decision_events}
    actual_event_ids = set(news.slots_by_event)
    if actual_event_ids != expected_event_ids:
        raise RunBundleError(
            "Real-news bundle event coverage differs from resolved decision calendar: "
            f"missing={sorted(expected_event_ids - actual_event_ids)[:3]} "
            f"extra={sorted(actual_event_ids - expected_event_ids)[:3]}"
        )
    target = spec.news_exposure_policy["target_real_news_per_event"]
    if news.target_real_count != target:
        raise RunBundleError("Real-news target slot count differs from the sealed news exposure policy")
    expected_slots = {(key.decision_event_id, key.slot_ordinal) for key in resolved.planned_news_slot_keys}
    actual_slots = {
        (event_id, slot.slot_ordinal)
        for event_id, rows in news.slots_by_event.items()
        for slot in rows
    }
    if not actual_slots <= expected_slots:
        raise RunBundleError("Real-news slots contain an event/ordinal outside the resolved planned domain")
    return news


def _stage_inputs_from_resolved(
    resolved: ResolvedStudyManifest,
    path: Path | str,
    input_root: Path | str,
    *,
    expected_schedule: EventSchedule,
) -> Any:
    """Reuse resolver validation; return its typed stage registry for stores."""
    from twinmarket_kr.rn_ab.stage_inputs import SealedStageInputRegistry, StageInputRegistryError

    try:
        stage_inputs = SealedStageInputRegistry.load(
            path,
            expected_file_sha256=resolved.spec.stage_input_registry_file_sha256,
            expected_calendar_event_registry_sha256=resolved.calendar.canonical_sha256,
            allowed_root=input_root,
        )
    except StageInputRegistryError as exc:
        raise RunBundleError(f"Stage-input registry is invalid: {exc}") from exc
    if stage_inputs.canonical_sha256 != resolved.spec.stage_input_registry_canonical_sha256:
        raise RunBundleError("Stage-input registry canonical hash differs from StudySpec")
    stage_inputs.assert_matches_schedule(expected_schedule)
    return stage_inputs


def _event_schedule_from_evaluator_contract(contract: Mapping[str, Any]) -> EventSchedule:
    calendar = contract.get("event_calendar")
    if not isinstance(calendar, Mapping) or not isinstance(calendar.get("events"), list):
        raise RunBundleError("Evaluator contract has no exact numeric event calendar")
    rows: list[dict[str, Any]] = []
    for turn, raw in enumerate(calendar["events"], start=1):
        if not isinstance(raw, Mapping):
            raise RunBundleError("Evaluator event calendar contains a non-object row")
        rows.append(
            {
                "event_id": raw.get("event_id"),
                "turn": turn,
                "date": raw.get("date"),
                "subturn": str(raw.get("session") or "").lower(),
                "execution_price": raw.get("execution_price"),
            }
        )
    try:
        return EventSchedule.from_rows(rows)
    except (PaperMemoryError, ValueError) as exc:  # type: ignore[name-defined]
        raise RunBundleError(f"Evaluator event schedule is invalid: {exc}") from exc


def _validate_known_injection_registry(
    *,
    spec: StudySpec,
    path: Path | str,
    input_root: Path | str,
    news: SealedNewsRegistry,
) -> str:
    expected = getattr(spec, "known_injection_registry_sha256", None)
    if not isinstance(expected, str) or not _SHA256_RE.fullmatch(expected):
        raise RunBundleError(
            "StudySpec must pin known_injection_registry_sha256 before RN preflight can proceed"
        )
    try:
        safe_path = assert_safe_input_file(path, allowed_root=input_root)
        payload = json.loads(safe_path.read_text(encoding="utf-8"))
    except (PathSafetyError, OSError, json.JSONDecodeError) as exc:
        raise RunBundleError(f"Known-injection registry cannot be read safely: {exc}") from exc
    if not isinstance(payload, Mapping) or set(payload) != _KNOWN_INJECTION_ROOT_FIELDS:
        raise RunBundleError("Known-injection registry has an invalid exact schema")
    if payload.get("artifact_type") != "known_injection_registry" or not isinstance(payload.get("version"), str):
        raise RunBundleError("Known-injection registry artifact/version is invalid")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise RunBundleError("Known-injection registry entries must be an array")
    ids: list[str] = []
    titles: list[str] = []
    row_hashes: list[str] = []
    for index, raw in enumerate(entries):
        if not isinstance(raw, Mapping) or set(raw) != _KNOWN_INJECTION_ENTRY_FIELDS:
            raise RunBundleError(f"Known-injection registry entry {index} has an invalid exact schema")
        identifier = raw["injection_id"]
        title = raw["title"]
        row_hash = raw["row_sha256"]
        if (
            not isinstance(identifier, str)
            or not identifier.strip()
            or not isinstance(title, str)
            or not title.strip()
            or not isinstance(row_hash, str)
            or not _SHA256_RE.fullmatch(row_hash)
        ):
            raise RunBundleError(f"Known-injection registry entry {index} is malformed")
        ids.append(identifier.strip())
        titles.append(title.strip())
        row_hashes.append(row_hash.lower())
    if len(ids) != len(set(ids)) or len(titles) != len(set(titles)) or len(row_hashes) != len(set(row_hashes)):
        raise RunBundleError("Known-injection registry contains duplicate ID/title/row hash")
    actual = canonical_sha256(payload)
    if actual != expected.lower():
        raise RunBundleError("Known-injection registry canonical hash differs from StudySpec")
    article_ids = {article.article_id for article in news.articles.values()}
    article_titles = {article.title for article in news.articles.values()}
    article_hashes = {
        digest
        for article in news.articles.values()
        for digest in (
            article.payload_sha256,
            article.raw_body_sha256,
            article.version_sha256,
            article.cutoff_version_sha256,
        )
    }
    if article_ids & set(ids) or article_titles & set(titles) or article_hashes & set(row_hashes):
        raise RunBundleError("Clean real-news bundle overlaps the independent known-injection registry")
    if tuple(ids) != news.known_fake_ids or tuple(row_hashes) != news.known_fake_payload_hashes:
        raise RunBundleError("News fake-isolation closure differs from independent known-injection registry")
    return actual


def _load_and_validate_article_version_review(
    *,
    spec: StudySpec,
    resolved: ResolvedStudyManifest,
    path: Path | str,
    input_root: Path | str,
    news: SealedNewsRegistry,
) -> Any:
    """Require a pinned allow-review for every article the paper run can expose."""

    try:
        review = load_article_version_leakage_review_manifest(
            path,
            expected_sha256=spec.article_version_leakage_review_manifest_sha256,
            news=news,
            expected_calendar_event_registry_sha256=resolved.calendar.canonical_sha256,
            expected_stage_input_registry_canonical_sha256=(
                spec.stage_input_registry_canonical_sha256
            ),
            allowed_root=input_root,
        )
    except LeakageReviewError as exc:
        raise RunBundleError(f"Article-version leakage review is invalid: {exc}") from exc
    return review


def _seal_runtime_input_files(
    *,
    run_dir: Path,
    destination_root: Path,
    input_root: Path,
    sources: Mapping[str, tuple[Path | str, str]],
) -> dict[str, dict[str, str]]:
    """Copy every runtime-relevant input into the fresh run namespace.

    Persona and prompt have their own richer sealers.  The remaining JSON
    registries used by the runner used to stay at their mutable source paths,
    which meant a later stage adapter could not prove that it was reading the
    same registry that preflight approved.  The copy is byte-pinned here and
    each copy is re-hashed by :class:`RNRunContext` before execution.
    """

    if destination_root.exists() or destination_root.is_symlink():
        raise RunBundleError("RN runtime input sealing destination must be new")
    destination_root.mkdir(mode=0o700)
    records: dict[str, dict[str, str]] = {}
    for label, (raw_source, filename) in sources.items():
        if not isinstance(label, str) or not label or not isinstance(filename, str) or "/" in filename:
            raise RunBundleError("RN runtime input sealing received an unsafe artifact name")
        try:
            source = assert_safe_input_file(raw_source, allowed_root=input_root)
        except PathSafetyError as exc:
            raise RunBundleError(f"RN runtime input is unsafe ({label}): {exc}") from exc
        before = _sha256_file(source)
        payload = source.read_bytes()
        after = _sha256_file(source)
        payload_sha256 = hashlib.sha256(payload).hexdigest()
        if before != after or before != payload_sha256:
            raise RunBundleError(f"RN runtime input changed while sealing: {label}")
        destination = destination_root / filename
        _write_bytes_exclusive(destination, payload)
        records[label] = {
            "path": str(destination.relative_to(run_dir)),
            "file_sha256": payload_sha256,
        }
    return records


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_bytes_exclusive(path: Path, payload: bytes) -> Path:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:  # pragma: no cover - fresh run path is enforced above.
        raise RunBundleError(f"Run artifact already exists: {path}") from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            path.unlink(missing_ok=True)
        finally:
            raise
    return path


def _write_json_exclusive(path: Path, payload: Mapping[str, Any]) -> Path:
    body = canonical_json_bytes(payload)
    return _write_bytes_exclusive(path, body)


# Imports used only in exception clauses above remain local to keep the
# public module surface compact without masking provenance failures.
from twinmarket_kr.rn_ab.memory import PaperMemoryError  # noqa: E402
from twinmarket_kr.rn_ab.news import NewsBundleError  # noqa: E402


__all__ = ["RNPreflightBundle", "RunBundleError", "prepare_preflight_bundle"]
