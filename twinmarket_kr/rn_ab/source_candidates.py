"""Local-only source-input candidate preparation for the RN Community AB study.

This module is deliberately *not* a StudySpec author or a RunBundle writer.
The repository contains useful historical source files, but the current news
tables do not retain immutable article-version/as-of evidence and include a
documented same-day EOD leakage counterexample.  Turning those files directly
into a sealed runtime bundle would overclaim what they prove.

Instead, this module makes the useful parts inspectable and reproducible:

* hashes every declared source file before and after reading it;
* builds deterministic, non-runtime candidate descriptions for the calendar,
  cohort, prices/stage inputs, evaluator target, and known-fake closure;
* inventories the legacy real-news rows while preserving their missing as-of
  fields as missing (never inventing timestamps or article versions); and
* writes a strict quarantine report for the documented ``2026-04-27`` leak
  and all semantic scanner candidates.

Every output explicitly has ``execution_authorized=false`` and
``run_eligible=false``.  A future approved source snapshot may be converted
to the strict RN registries through a separate, reviewable process; these
candidate files cannot be supplied to :mod:`twinmarket_kr.rn_ab.run_bundle`.
No network, provider, or model client is imported here.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import pickle
import re
import shutil
import stat
import tempfile
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from twinmarket_kr.rn_ab.news import fake_registry_sha256
from twinmarket_kr.rn_ab.spec import canonical_sha256


class SourceCandidateError(RuntimeError):
    """Raised when a local source candidate cannot be safely prepared."""


CANDIDATE_VERSION = "rn-source-input-candidate-v1"
AUDIT_VERSION = "rn-source-input-candidate-audit-v1"
QUARANTINE_VERSION = "rn-news-leakage-quarantine-v1"

DEFAULT_STUDY_ID = "realnews_comm_ab_100agent_source_candidate_v1"
DEFAULT_START_DATE = "2026-02-27"
DEFAULT_END_DATE = "2026-05-04"
DEFAULT_BURN_IN_DATES = ("2026-02-27", "2026-03-03", "2026-03-04")
DEFAULT_EXPECTED_AGENT_COUNT = 100
DEFAULT_EXPECTED_DEPTH_COUNTS = {0: 30, 1: 55, 2: 15}
DEFAULT_EXPECTED_TRADING_DATE_COUNT = 45

_CANDIDATE_FILENAMES = {
    "calendar": "calendar_candidate.json",
    "cohort": "cohort_candidate.json",
    "event_price": "event_price_candidate.json",
    "stage_input": "stage_input_candidate.json",
    "evaluator_target": "evaluator_target_candidate.json",
    "known_fake_closure": "known_fake_closure_candidate.json",
    "news_inventory": "news_inventory_candidate.json",
    "leakage_quarantine": "leakage_quarantine_report.json",
}
_AUDIT_FILENAME = "SOURCE_INPUT_CANDIDATE_AUDIT.json"
_SHA256SUMS_FILENAME = "SOURCE_INPUT_CANDIDATE_SHA256SUMS.txt"

_CANDIDATE_FIELDS = frozenset(
    {
        "artifact_type",
        "version",
        "candidate_kind",
        "candidate_status",
        "execution_authorized",
        "run_eligible",
        "source_file_sha256s",
        "blocking_reasons",
        "findings",
        "proposed_runtime_shape",
        "candidate_sha256",
    }
)
_AUDIT_FIELDS = frozenset(
    {
        "artifact_type",
        "version",
        "study_id",
        "candidate_status",
        "execution_authorized",
        "run_eligible",
        "generation_mode",
        "network_requests",
        "paid_api_calls",
        "source_files",
        "candidate_files",
        "candidate_canonical_sha256s",
        "calendar_contract",
        "blocking_reasons",
        "audit_sha256",
    }
)
_FILE_RECORD_FIELDS = frozenset({"label", "path", "bytes", "sha256"})

# These are deliberately conservative *review* triggers, not automatic
# assertions that every match leaks.  The documented counterexample is added
# independently below and is always quarantined.
_SEMANTIC_LEAKAGE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "market_outcome_or_intraday_lexeme_requires_review",
        # Broad by design: a reference to a previous session can be safe, but
        # without immutable version/as-of evidence this candidate builder must
        # quarantine it for review rather than infer the referent automatically.
        re.compile(
            r"당일\s*(?:종가|고가|저가)|장\s*마감(?:\s*후|값)?|마감했다|"
            r"(?:종가|고가|저가|장중)",
            re.IGNORECASE,
        ),
    ),
    (
        "investor_flow_lexeme_requires_review",
        re.compile(r"(?:개인|개미|외국인|기관)?\s*(?:투자자)?\s*(?:순매수|순매도|순거래)", re.IGNORECASE),
    ),
    (
        "english_eod_or_flow",
        re.compile(r"final\s*(?:close|high|low)|individuals?\s*(?:net\s*)?(?:buy|sell|flow)", re.IGNORECASE),
    ),
)
_DOCUMENTED_LEAK_ID = "news_20260427_섹터_0032"

_GLOBAL_BLOCKERS = (
    "immutable_article_version_and_as_of_provenance_missing",
    "confirmed_2026_04_27_same_day_eod_leakage_requires_quarantine",
    "candidate_artifacts_are_not_a_sealed_study_spec_or_run_bundle",
)


@dataclass(frozen=True)
class CandidateBuildConfig:
    """The documented source window, kept distinct from runtime authorization."""

    study_id: str = DEFAULT_STUDY_ID
    start_date: str = DEFAULT_START_DATE
    end_date: str = DEFAULT_END_DATE
    burn_in_dates: tuple[str, ...] = DEFAULT_BURN_IN_DATES
    expected_agent_count: int = DEFAULT_EXPECTED_AGENT_COUNT
    expected_depth_counts: Mapping[int, int] | None = None
    expected_trading_date_count: int = DEFAULT_EXPECTED_TRADING_DATE_COUNT

    def __post_init__(self) -> None:
        _parse_iso_date(self.start_date, "study start_date")
        _parse_iso_date(self.end_date, "study end_date")
        if self.start_date > self.end_date:
            raise SourceCandidateError("study start_date may not be later than end_date")
        if not self.study_id or not self.study_id.strip():
            raise SourceCandidateError("study_id must be non-empty")
        if self.expected_agent_count < 1:
            raise SourceCandidateError("expected_agent_count must be positive")
        if self.expected_trading_date_count < 1:
            raise SourceCandidateError("expected_trading_date_count must be positive")
        if not self.burn_in_dates:
            raise SourceCandidateError("burn_in_dates must be non-empty")
        for item in self.burn_in_dates:
            _parse_iso_date(item, "burn_in date")
        if tuple(sorted(set(self.burn_in_dates))) != self.burn_in_dates:
            raise SourceCandidateError("burn_in_dates must be sorted and unique")
        counts = self.expected_depth_counts or DEFAULT_EXPECTED_DEPTH_COUNTS
        if set(counts) != {0, 1, 2} or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in counts.values()
        ):
            raise SourceCandidateError("expected_depth_counts must contain non-negative D0/D1/D2 counts")
        if sum(counts.values()) != self.expected_agent_count:
            raise SourceCandidateError("expected_depth_counts must add up to expected_agent_count")

    @property
    def depth_counts(self) -> dict[int, int]:
        return dict(self.expected_depth_counts or DEFAULT_EXPECTED_DEPTH_COUNTS)


@dataclass(frozen=True)
class SourceInputPaths:
    """All local sources read by the candidate builder.

    Paths remain explicit so a caller cannot accidentally use an old runtime
    database, glob-selected ``latest`` directory, or an unrecorded download.
    """

    project_root: Path
    stock_data_csv: Path
    macro_data_csv: Path
    processed_news_csv: Path
    daily_news_selection_csv: Path
    raw_news_pickle: Path
    legacy_samsung_news_pickle: Path
    target_value_csv: Path
    target_volume_csv: Path
    persona_source_db: Path
    fixed_slots_csv: Path
    persona_snapshot_manifest: Path
    persona_depth_manifest: Path
    persona_repair_manifest: Path
    persona_snapshot_db: Path
    fake_bearish_pickle: Path
    fake_bullish_pickle: Path
    fake_event_pickle: Path
    fake_bearish_processed_csv: Path
    fake_bullish_processed_csv: Path
    fake_bearish_daily_csv: Path
    fake_bullish_daily_csv: Path
    fake_bearish_manifest: Path
    fake_bullish_manifest: Path

    @classmethod
    def from_project_root(cls, project_root: Path | str) -> "SourceInputPaths":
        root = Path(project_root).resolve()
        snapshot = root / "preparation" / "rn_ab_source_candidate_v1" / "persona_snapshot"
        return cls(
            project_root=root,
            stock_data_csv=root / "data" / "stock_data.csv",
            macro_data_csv=root / "data" / "macro_data.csv",
            processed_news_csv=root / "outputs" / "processed_news.csv",
            daily_news_selection_csv=root / "outputs" / "daily_news_selection.csv",
            raw_news_pickle=root / "data" / "samsung_news_raw.pkl",
            legacy_samsung_news_pickle=root / "data" / "samsung_news.pkl",
            target_value_csv=root / "validation" / "data_trading_value.csv",
            target_volume_csv=root / "validation" / "data_trading_volume.csv",
            persona_source_db=root / "outputs" / "sys_100.db",
            fixed_slots_csv=root / "data" / "fixed_slots.csv",
            persona_snapshot_manifest=snapshot / "persona_snapshot_manifest.json",
            persona_depth_manifest=snapshot / "persona_depth_manifest.json",
            persona_repair_manifest=snapshot / "persona_repair_manifest.json",
            persona_snapshot_db=snapshot / "persona_snapshot.sqlite",
            fake_bearish_pickle=root / "data" / "fake_news_bearish_phase_review.pkl",
            fake_bullish_pickle=root / "data" / "fake_news_bullish_phase_review.pkl",
            fake_event_pickle=root / "data" / "event.pkl",
            fake_bearish_processed_csv=root / "outputs" / "processed_news_injection_bearish.csv",
            fake_bullish_processed_csv=root / "outputs" / "processed_news_injection_bullish.csv",
            fake_bearish_daily_csv=root / "outputs" / "daily_news_selection_injection_bearish.csv",
            fake_bullish_daily_csv=root / "outputs" / "daily_news_selection_injection_bullish.csv",
            fake_bearish_manifest=root / "outputs" / "fake_news_injection_manifest_bearish.json",
            fake_bullish_manifest=root / "outputs" / "fake_news_injection_manifest_bullish.json",
        )

    def labelled_files(self) -> tuple[tuple[str, Path], ...]:
        return (
            ("stock_data_csv", self.stock_data_csv),
            ("macro_data_csv", self.macro_data_csv),
            ("processed_news_csv", self.processed_news_csv),
            ("daily_news_selection_csv", self.daily_news_selection_csv),
            ("raw_news_pickle", self.raw_news_pickle),
            ("legacy_samsung_news_pickle", self.legacy_samsung_news_pickle),
            ("target_value_csv", self.target_value_csv),
            ("target_volume_csv", self.target_volume_csv),
            ("persona_source_db", self.persona_source_db),
            ("fixed_slots_csv", self.fixed_slots_csv),
            ("persona_snapshot_manifest", self.persona_snapshot_manifest),
            ("persona_depth_manifest", self.persona_depth_manifest),
            ("persona_repair_manifest", self.persona_repair_manifest),
            ("persona_snapshot_db", self.persona_snapshot_db),
            ("fake_bearish_pickle", self.fake_bearish_pickle),
            ("fake_bullish_pickle", self.fake_bullish_pickle),
            ("fake_event_pickle", self.fake_event_pickle),
            ("fake_bearish_processed_csv", self.fake_bearish_processed_csv),
            ("fake_bullish_processed_csv", self.fake_bullish_processed_csv),
            ("fake_bearish_daily_csv", self.fake_bearish_daily_csv),
            ("fake_bullish_daily_csv", self.fake_bullish_daily_csv),
            ("fake_bearish_manifest", self.fake_bearish_manifest),
            ("fake_bullish_manifest", self.fake_bullish_manifest),
        )


@dataclass(frozen=True)
class SourceCandidateArtifacts:
    """Paths and no-go status returned after an atomic candidate build."""

    root: Path
    audit_path: Path
    candidate_paths: Mapping[str, Path]
    execution_authorized: bool
    run_eligible: bool


def build_source_input_candidates(
    *,
    destination_dir: Path | str,
    paths: SourceInputPaths,
    config: CandidateBuildConfig | None = None,
) -> SourceCandidateArtifacts:
    """Build deterministic source candidates without authorizing execution.

    ``destination_dir`` must not exist.  The source set is byte-hashed before
    and after all reads, so a concurrently modified source cannot be quietly
    represented by a mixed manifest.
    """

    config = config or CandidateBuildConfig()
    _validate_paths(paths)
    destination = Path(destination_dir)
    if destination.exists() or destination.is_symlink():
        raise SourceCandidateError(f"candidate destination already exists: {destination}")
    if not destination.parent.is_dir():
        raise SourceCandidateError("candidate destination parent must already exist")

    source_before = _source_records(paths)
    source_by_label = {record["label"]: record for record in source_before}

    stock_rows = _read_stock_rows(paths.stock_data_csv)
    macro_rows = _read_macro_rows(paths.macro_data_csv)
    processed_rows = _read_csv_rows(paths.processed_news_csv, "processed news")
    daily_rows = _read_csv_rows(paths.daily_news_selection_csv, "daily news selection")
    raw_news_rows, raw_news_probe = _load_raw_news_pickle(paths.raw_news_pickle)
    target_values = _read_target_values(paths.target_value_csv, "target value")
    target_volumes = _read_target_values(paths.target_volume_csv, "target volume")
    snapshot_manifest = _read_json(paths.persona_snapshot_manifest, "persona snapshot manifest")
    depth_manifest = _read_json(paths.persona_depth_manifest, "persona depth manifest")
    repair_manifest = _read_json(paths.persona_repair_manifest, "persona repair manifest")
    fixed_slots = _read_fixed_slots(paths.fixed_slots_csv)
    fake_data = _collect_known_fake_closure(paths)

    stock_by_date = {str(row["date"]): row for row in stock_rows}
    macro_by_date = {str(row["date"]): row for row in macro_rows}
    candidate_dates = tuple(
        day
        for day in sorted(stock_by_date)
        if config.start_date <= day <= config.end_date
    )
    expected_date_set = set(candidate_dates)
    selected_target_dates = tuple(candidate_dates)
    missing_target_dates = sorted(set(selected_target_dates) - set(target_values))
    missing_volume_dates = sorted(set(selected_target_dates) - set(target_volumes))
    missing_macro_dates = sorted(set(selected_target_dates) - set(macro_by_date))
    missing_burn_in_dates = sorted(set(config.burn_in_dates) - set(candidate_dates))

    calendar_candidate = _build_calendar_candidate(
        source_by_label=source_by_label,
        config=config,
        candidate_dates=candidate_dates,
        missing_burn_in_dates=missing_burn_in_dates,
    )
    cohort_candidate = _build_cohort_candidate(
        source_by_label=source_by_label,
        config=config,
        snapshot_manifest=snapshot_manifest,
        depth_manifest=depth_manifest,
        repair_manifest=repair_manifest,
        fixed_slots=fixed_slots,
        persona_source_sha256=str(source_by_label["persona_source_db"]["sha256"]),
    )
    event_price_candidate = _build_event_price_candidate(
        source_by_label=source_by_label,
        candidate_dates=candidate_dates,
        stock_by_date=stock_by_date,
    )
    stage_input_candidate = _build_stage_input_candidate(
        source_by_label=source_by_label,
        candidate_dates=candidate_dates,
        stock_calendar_dates=tuple(sorted(stock_by_date)),
        stock_by_date=stock_by_date,
        macro_by_date=macro_by_date,
        missing_macro_dates=missing_macro_dates,
    )
    target_candidate = _build_target_candidate(
        source_by_label=source_by_label,
        config=config,
        candidate_dates=candidate_dates,
        target_values=target_values,
        target_volumes=target_volumes,
        missing_target_dates=missing_target_dates,
        missing_volume_dates=missing_volume_dates,
    )
    fake_candidate = _build_fake_closure_candidate(
        source_by_label=source_by_label,
        fake_data=fake_data,
        processed_rows=processed_rows,
        daily_rows=daily_rows,
    )
    news_candidate, quarantine_report = _build_news_candidates(
        source_by_label=source_by_label,
        config=config,
        candidate_dates=candidate_dates,
        stock_calendar_dates=tuple(sorted(stock_by_date)),
        processed_rows=processed_rows,
        daily_rows=daily_rows,
        raw_news_rows=raw_news_rows,
        raw_news_probe=raw_news_probe,
        fake_data=fake_data,
    )

    candidates = {
        "calendar": calendar_candidate,
        "cohort": cohort_candidate,
        "event_price": event_price_candidate,
        "stage_input": stage_input_candidate,
        "evaluator_target": target_candidate,
        "known_fake_closure": fake_candidate,
        "news_inventory": news_candidate,
        "leakage_quarantine": quarantine_report,
    }

    source_after = _source_records(paths)
    if source_after != source_before:
        raise SourceCandidateError("source input changed during candidate preparation; nothing was written")

    temporary = Path(tempfile.mkdtemp(prefix=".rn-source-candidates-", dir=destination.parent))
    try:
        candidate_paths: dict[str, Path] = {}
        for kind, payload in candidates.items():
            path = temporary / _CANDIDATE_FILENAMES[kind]
            _write_json(path, payload)
            candidate_paths[kind] = path

        candidate_file_records = [
            _generated_file_record(kind, path)
            for kind, path in sorted(candidate_paths.items())
        ]
        candidate_hashes = {
            kind: str(candidates[kind]["candidate_sha256"])
            for kind in sorted(candidates)
        }
        audit = _build_audit(
            config=config,
            source_records=source_before,
            candidate_file_records=candidate_file_records,
            candidate_hashes=candidate_hashes,
            candidate_dates=candidate_dates,
        )
        audit_path = temporary / _AUDIT_FILENAME
        _write_json(audit_path, audit)
        _write_sha256sums(
            path=temporary / _SHA256SUMS_FILENAME,
            paths=[*candidate_paths.values(), audit_path],
        )

        validate_source_input_candidates(
            root=temporary,
            project_root=paths.project_root,
            check_source_files=True,
        )
        os.replace(temporary, destination)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise

    return SourceCandidateArtifacts(
        root=destination,
        audit_path=destination / _AUDIT_FILENAME,
        candidate_paths={kind: destination / path.name for kind, path in candidate_paths.items()},
        execution_authorized=False,
        run_eligible=False,
    )


def validate_source_input_candidates(
    *,
    root: Path | str,
    project_root: Path | str,
    check_source_files: bool = True,
) -> Mapping[str, Any]:
    """Validate a candidate directory without treating it as execution input."""

    base = Path(root)
    project = Path(project_root).resolve()
    _assert_real_directory(base, "candidate root")
    _assert_real_directory(project, "project root")
    audit_path = _safe_child(base, _AUDIT_FILENAME)
    audit = _read_json(audit_path, "candidate audit")
    if set(audit) != _AUDIT_FIELDS:
        raise SourceCandidateError("candidate audit has an invalid exact key set")
    if audit.get("artifact_type") != "rn_source_input_candidate_audit" or audit.get("version") != AUDIT_VERSION:
        raise SourceCandidateError("candidate audit has an unsupported artifact/version")
    if audit.get("candidate_status") != "no_go_source_candidate":
        raise SourceCandidateError("candidate audit must remain a no-go source candidate")
    if audit.get("execution_authorized") is not False or audit.get("run_eligible") is not False:
        raise SourceCandidateError("candidate audit may never authorize execution")
    if audit.get("generation_mode") != "local_only_no_network_no_paid_api":
        raise SourceCandidateError("candidate audit generation mode is invalid")
    if audit.get("network_requests") != 0 or audit.get("paid_api_calls") != 0:
        raise SourceCandidateError("candidate audit may not claim network or paid API activity")
    _verify_self_hash(audit, "audit_sha256", "candidate audit")

    source_records = _validate_file_records(audit.get("source_files"), "source_files")
    if check_source_files:
        for record in source_records:
            source_path = _safe_project_file(project, str(record["path"]))
            actual = _file_sha256(source_path)
            if actual != record["sha256"]:
                raise SourceCandidateError(
                    f"candidate source hash drifted: {record['path']}"
                )
            if source_path.stat().st_size != record["bytes"]:
                raise SourceCandidateError(
                    f"candidate source size drifted: {record['path']}"
                )

    candidate_records = _validate_file_records(audit.get("candidate_files"), "candidate_files")
    expected_names = {_CANDIDATE_FILENAMES[kind] for kind in _CANDIDATE_FILENAMES}
    if {record["path"] for record in candidate_records} != expected_names:
        raise SourceCandidateError("candidate audit does not index the complete fixed candidate set")
    hashes = audit.get("candidate_canonical_sha256s")
    if not isinstance(hashes, Mapping) or set(hashes) != set(_CANDIDATE_FILENAMES):
        raise SourceCandidateError("candidate audit has an invalid candidate canonical hash map")

    for kind, filename in _CANDIDATE_FILENAMES.items():
        path = _safe_child(base, filename)
        payload = _read_json(path, f"{kind} candidate")
        if kind == "leakage_quarantine":
            _validate_quarantine(payload)
        else:
            _validate_candidate(payload, kind=kind)
        if payload["candidate_sha256"] != hashes[kind]:
            raise SourceCandidateError(f"{kind} candidate hash differs from audit")
        indexed = next(record for record in candidate_records if record["path"] == filename)
        if _file_sha256(path) != indexed["sha256"] or path.stat().st_size != indexed["bytes"]:
            raise SourceCandidateError(f"{kind} candidate file hash differs from audit")

    sums_path = _safe_child(base, _SHA256SUMS_FILENAME)
    expected_sum_paths = [
        *_CANDIDATE_FILENAMES.values(),
        _AUDIT_FILENAME,
    ]
    _validate_sha256sums(base=base, path=sums_path, expected_paths=expected_sum_paths)

    reasons = audit.get("blocking_reasons")
    if not isinstance(reasons, list) or not set(_GLOBAL_BLOCKERS).issubset(reasons):
        raise SourceCandidateError("candidate audit is missing required no-go blockers")
    return {
        "candidate_root": str(base),
        "audit_sha256": audit["audit_sha256"],
        "execution_authorized": False,
        "run_eligible": False,
        "candidate_file_count": len(candidate_records),
    }


def _build_calendar_candidate(
    *,
    source_by_label: Mapping[str, Mapping[str, Any]],
    config: CandidateBuildConfig,
    candidate_dates: Sequence[str],
    missing_burn_in_dates: Sequence[str],
) -> dict[str, Any]:
    date_rows = []
    for ordinal, day in enumerate(candidate_dates, start=1):
        date_rows.append(
            {
                "date": day,
                "date_ordinal": ordinal,
                "decision_event_ids": [f"{day}/AM", f"{day}/PM"],
                "event_policy": {
                    "AM": {
                        "execution_price_field": "actual_open",
                        "news_window_intent": "previous_trading_date_15_30_exclusive_to_current_08_59_inclusive",
                        "market_feature_as_of_intent": "current_date_09_00_kst",
                        "consume_scheduled_community": True,
                    },
                    "PM": {
                        "execution_price_field": "actual_close",
                        "news_window_intent": "current_date_08_59_exclusive_to_current_15_30_inclusive",
                        "market_feature_as_of_intent": "current_date_15_30_kst",
                        "consume_scheduled_community": False,
                    },
                },
                "post_decision_phase_intent": {
                    "phase_id": f"{day}/community",
                    "after_event_id": f"{day}/PM",
                    "next_visible_event_rule": "next-approved-AM",
                },
            }
        )
    findings = {
        "requested_date_range": {"start_date": config.start_date, "end_date": config.end_date},
        "stock_calendar_date_count": len(candidate_dates),
        "expected_trading_date_count": config.expected_trading_date_count,
        "calendar_count_matches_documented_candidate": len(candidate_dates)
        == config.expected_trading_date_count,
        "burn_in_date_ids": list(config.burn_in_dates),
        "missing_burn_in_date_ids": list(missing_burn_in_dates),
        "decision_event_count": len(candidate_dates) * 2,
        "calendar_is_not_a_runtime_registry": True,
    }
    proposed = {
        "target_runtime_artifact_type": "calendar_event_registry",
        "timezone": "Asia/Seoul",
        "date_rows": date_rows,
        "runtime_serialization_with_exact_iso_timestamps": "blocked_pending_approved_temporal_authoring",
        "reason": "The legacy source files do not provide an approved immutable event/window registry.",
    }
    return _candidate(
        kind="calendar",
        source_by_label=source_by_label,
        source_labels=("stock_data_csv",),
        findings=findings,
        proposed_runtime_shape=proposed,
    )


def _build_cohort_candidate(
    *,
    source_by_label: Mapping[str, Mapping[str, Any]],
    config: CandidateBuildConfig,
    snapshot_manifest: Mapping[str, Any],
    depth_manifest: Mapping[str, Any],
    repair_manifest: Mapping[str, Any],
    fixed_slots: Mapping[str, Mapping[str, Any]],
    persona_source_sha256: str,
) -> dict[str, Any]:
    raw_agents = snapshot_manifest.get("agents")
    if not isinstance(raw_agents, list):
        raise SourceCandidateError("persona snapshot manifest has no ordered agents array")
    normalized_agents: list[dict[str, Any]] = []
    ids: set[str] = set()
    depth_counts: Counter[int] = Counter()
    mismatch_ids: list[str] = []
    for ordinal, raw in enumerate(raw_agents, start=1):
        if not isinstance(raw, Mapping):
            raise SourceCandidateError("persona snapshot agent must be an object")
        agent_id = _required_text(raw.get("agent_id"), "persona snapshot agent_id")
        if agent_id in ids:
            raise SourceCandidateError("persona snapshot contains duplicate agent IDs")
        ids.add(agent_id)
        if raw.get("ordinal") != ordinal:
            raise SourceCandidateError("persona snapshot ordinals must be continuous")
        depth = _required_int(raw.get("news_depth"), "persona snapshot news_depth")
        initial_cash = _required_int(raw.get("initial_cash"), "persona snapshot initial_cash")
        persona_hash = _required_sha256(raw.get("persona_sha256"), "persona snapshot persona_sha256")
        fixed = fixed_slots.get(agent_id)
        if fixed is None or fixed["initial_cash"] != initial_cash:
            mismatch_ids.append(agent_id)
        fixed_hash = canonical_sha256(
            {
                "agent_id": agent_id,
                "gender": fixed["gender"] if fixed else None,
                "age": fixed["age"] if fixed else None,
                "age_group": fixed["age_group"] if fixed else None,
                "initial_cash": fixed["initial_cash"] if fixed else None,
            }
        )
        normalized_agents.append(
            {
                "ordinal": ordinal,
                "agent_id": agent_id,
                "news_depth": depth,
                "initial_cash": initial_cash,
                "persona_sha256": persona_hash,
                "fixed_slot_sha256": fixed_hash,
            }
        )
        depth_counts[depth] += 1
    expected_depth_counts = {str(key): value for key, value in sorted(config.depth_counts.items())}
    actual_depth_counts = {str(key): depth_counts.get(key, 0) for key in (0, 1, 2)}
    source_hash_from_snapshot = snapshot_manifest.get("source_db_sha256")
    source_hash_matches = isinstance(source_hash_from_snapshot, str) and source_hash_from_snapshot == persona_source_sha256
    findings = {
        "snapshot_agent_count": len(normalized_agents),
        "expected_agent_count": config.expected_agent_count,
        "snapshot_agent_count_matches": len(normalized_agents) == config.expected_agent_count,
        "depth_counts": actual_depth_counts,
        "expected_depth_counts": expected_depth_counts,
        "depth_counts_match": actual_depth_counts == expected_depth_counts,
        "fixed_slot_agent_count": len(fixed_slots),
        "fixed_slot_initial_cash_mismatch_agent_ids": mismatch_ids,
        "snapshot_source_db_sha256": source_hash_from_snapshot,
        "source_db_sha256": persona_source_sha256,
        "snapshot_source_db_hash_matches": source_hash_matches,
        "persona_snapshot_manifest_canonical_sha256": canonical_sha256(snapshot_manifest),
        "persona_depth_manifest_canonical_sha256": canonical_sha256(depth_manifest),
        "persona_repair_manifest_canonical_sha256": canonical_sha256(repair_manifest),
    }
    proposed = {
        "target_runtime_artifact_type": "cohort_registry",
        "version": "candidate-unapproved-v1",
        "agents": normalized_agents,
        "candidate_runtime_registry_canonical_sha256": canonical_sha256(
            {
                "artifact_type": "cohort_registry",
                "version": "candidate-unapproved-v1",
                "agents": normalized_agents,
            }
        ),
        "approval_state": "not_a_runtime_registry",
    }
    return _candidate(
        kind="cohort",
        source_by_label=source_by_label,
        source_labels=(
            "persona_source_db",
            "fixed_slots_csv",
            "persona_snapshot_manifest",
            "persona_depth_manifest",
            "persona_repair_manifest",
            "persona_snapshot_db",
        ),
        findings=findings,
        proposed_runtime_shape=proposed,
    )


def _build_event_price_candidate(
    *,
    source_by_label: Mapping[str, Mapping[str, Any]],
    candidate_dates: Sequence[str],
    stock_by_date: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    missing_dates: list[str] = []
    for day in candidate_dates:
        row = stock_by_date.get(day)
        if row is None:
            missing_dates.append(day)
            continue
        events.extend(
            (
                {
                    "decision_event_id": f"{day}/AM",
                    "date": day,
                    "subturn": "AM",
                    "execution_price_field": "actual_open",
                    "execution_price": row["open"],
                    "source_row_sha256": row["row_sha256"],
                    "field_level_as_of_provenance": "missing",
                },
                {
                    "decision_event_id": f"{day}/PM",
                    "date": day,
                    "subturn": "PM",
                    "execution_price_field": "actual_close",
                    "execution_price": row["close"],
                    "source_row_sha256": row["row_sha256"],
                    "field_level_as_of_provenance": "missing",
                },
            )
        )
    candidate_registry = {
        "artifact_type": "event_price_registry",
        "version": "candidate-unapproved-v1",
        "stock_code": "005930",
        "calendar_event_registry_sha256": None,
        "events": events,
    }
    findings = {
        "stock_code": "005930",
        "source_date_count": len(candidate_dates),
        "event_price_count": len(events),
        "missing_stock_dates": missing_dates,
        "field_level_as_of_provenance": "missing_for_all_candidate_prices",
        "runtime_price_registry_binding": "unavailable_without_approved_calendar_hash",
    }
    proposed = {
        "target_runtime_artifact_type": "event_price_registry",
        "candidate_registry": candidate_registry,
        "candidate_registry_canonical_sha256_excluding_unbound_calendar": canonical_sha256(candidate_registry),
        "approval_state": "not_a_runtime_registry",
    }
    return _candidate(
        kind="event_price",
        source_by_label=source_by_label,
        source_labels=("stock_data_csv",),
        findings=findings,
        proposed_runtime_shape=proposed,
    )


def _build_stage_input_candidate(
    *,
    source_by_label: Mapping[str, Mapping[str, Any]],
    candidate_dates: Sequence[str],
    stock_calendar_dates: Sequence[str],
    stock_by_date: Mapping[str, Mapping[str, Any]],
    macro_by_date: Mapping[str, Mapping[str, Any]],
    missing_macro_dates: Sequence[str],
) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    previous_by_date = {
        day: stock_calendar_dates[index - 1]
        for index, day in enumerate(stock_calendar_dates)
        if index > 0
    }
    for day in candidate_dates:
        stock = stock_by_date[day]
        macro = macro_by_date.get(day)
        previous_day = previous_by_date.get(day)
        previous_stock = stock_by_date.get(previous_day) if previous_day else None
        previous_close = previous_stock["close"] if previous_stock else None
        base = {
            "date": day,
            "source_stock_row_sha256": stock["row_sha256"],
            "source_macro_row_sha256": macro["row_sha256"] if macro else None,
            "market_candidate": {
                "previous_close": previous_close,
                "open_price": stock["open"],
                "close_price": stock["close"],
                "macro": {
                    "kospi_close": macro["kospi_close"] if macro else None,
                    "usdkrw": macro["usdkrw"] if macro else None,
                },
            },
            "field_level_as_of_provenance": "missing",
        }
        events.append(
            {
                **base,
                "event_id": f"{day}/AM",
                "subturn": "am",
                "reference_price_candidate": stock["open"],
                "market_feature_as_of_intent": f"{day}T09:00:00+09:00",
                "news_cutoff_intent": f"{day}T08:59:00+09:00",
            }
        )
        events.append(
            {
                **base,
                "event_id": f"{day}/PM",
                "subturn": "pm",
                "reference_price_candidate": stock["close"],
                "market_feature_as_of_intent": f"{day}T15:30:00+09:00",
                "news_cutoff_intent": f"{day}T15:30:00+09:00",
            }
        )
    findings = {
        "candidate_event_count": len(events),
        "missing_macro_dates": list(missing_macro_dates),
        "first_event_previous_close_is_missing": bool(events)
        and events[0]["market_candidate"]["previous_close"] is None,
        "stage_as_of_status": "unverified",
        "reason": "CSV rows do not contain field-level observation/as-of provenance.",
    }
    proposed = {
        "target_runtime_artifact_type": "rn_stage_input_registry",
        "calendar_event_registry_sha256": None,
        "events": events,
        "approval_state": "not_a_runtime_registry",
        "conversion_requirement": "approved calendar plus field-level price/macro as-of evidence",
    }
    return _candidate(
        kind="stage_input",
        source_by_label=source_by_label,
        source_labels=("stock_data_csv", "macro_data_csv"),
        findings=findings,
        proposed_runtime_shape=proposed,
    )


def _build_target_candidate(
    *,
    source_by_label: Mapping[str, Mapping[str, Any]],
    config: CandidateBuildConfig,
    candidate_dates: Sequence[str],
    target_values: Mapping[str, Decimal],
    target_volumes: Mapping[str, Decimal],
    missing_target_dates: Sequence[str],
    missing_volume_dates: Sequence[str],
) -> dict[str, Any]:
    value_rows = [
        {
            "date": day,
            "individual_net_trading_value": _decimal_text(target_values[day]),
            "direction": _direction(target_values[day]),
        }
        for day in candidate_dates
        if day in target_values
    ]
    divergences = [
        day
        for day in candidate_dates
        if day in target_values
        and day in target_volumes
        and _direction(target_values[day]) != _direction(target_volumes[day])
    ]
    evaluation_dates = [day for day in candidate_dates if day not in set(config.burn_in_dates)]
    findings = {
        "target_namespace": "evaluator_only",
        "input_date_count": len(candidate_dates),
        "evaluation_date_count": len(evaluation_dates),
        "missing_value_target_dates": list(missing_target_dates),
        "missing_volume_target_dates": list(missing_volume_dates),
        "flat_value_target_dates": [
            day for day in candidate_dates if day in target_values and target_values[day] == 0
        ],
        "value_volume_direction_divergence_dates": divergences,
        "runtime_visibility": "forbidden",
        "authoritative_resolved_manifest_binding": "missing",
    }
    proposed = {
        "target_runtime_artifact_type": "rn_ab_evaluator_target_registry",
        "evaluator_only": True,
        "authoritative_resolved_manifest_sha256": None,
        "price_registry_sha256": None,
        "input_dates": list(candidate_dates),
        "evaluation_dates": evaluation_dates,
        "target_values": value_rows,
        "approval_state": "not_a_runtime_registry",
    }
    return _candidate(
        kind="evaluator_target",
        source_by_label=source_by_label,
        source_labels=("target_value_csv", "target_volume_csv"),
        findings=findings,
        proposed_runtime_shape=proposed,
    )


def _build_fake_closure_candidate(
    *,
    source_by_label: Mapping[str, Mapping[str, Any]],
    fake_data: Mapping[str, Any],
    processed_rows: Sequence[Mapping[str, str]],
    daily_rows: Sequence[Mapping[str, str]],
) -> dict[str, Any]:
    fake_ids = tuple(fake_data["known_fake_ids"])
    fake_hashes = tuple(fake_data["known_fake_row_hashes"])
    processed_ids = {_required_text(row.get("id"), "processed news id") for row in processed_rows}
    daily_ids = {_required_text(row.get("id"), "daily news id") for row in daily_rows}
    findings = {
        "known_fake_id_count": len(fake_ids),
        "known_fake_row_hash_count": len(fake_hashes),
        "known_fake_registry_candidate_sha256": fake_registry_sha256(
            known_fake_ids=fake_ids,
            known_fake_payload_hashes=fake_hashes,
        ),
        "baseline_processed_id_overlap": sorted(set(fake_ids) & processed_ids),
        "baseline_daily_id_overlap": sorted(set(fake_ids) & daily_ids),
        "raw_fake_pickle_probes": fake_data["pickle_probes"],
        "closure_scope": "current_bearish_and_bullish_injection_exports_only",
        "approval_state": "candidate_only_no_fake_run_authorized",
    }
    proposed = {
        "target_runtime_artifact_type": "known_injection_registry",
        "known_fake_ids": list(fake_ids),
        "known_fake_row_hashes": list(fake_hashes),
        "source_variants": fake_data["variants"],
        "approval_state": "not_a_runtime_registry",
    }
    return _candidate(
        kind="known_fake_closure",
        source_by_label=source_by_label,
        source_labels=(
            "fake_bearish_pickle",
            "fake_bullish_pickle",
            "fake_event_pickle",
            "fake_bearish_processed_csv",
            "fake_bullish_processed_csv",
            "fake_bearish_daily_csv",
            "fake_bullish_daily_csv",
            "fake_bearish_manifest",
            "fake_bullish_manifest",
        ),
        findings=findings,
        proposed_runtime_shape=proposed,
    )


def _build_news_candidates(
    *,
    source_by_label: Mapping[str, Mapping[str, Any]],
    config: CandidateBuildConfig,
    candidate_dates: Sequence[str],
    stock_calendar_dates: Sequence[str],
    processed_rows: Sequence[Mapping[str, str]],
    daily_rows: Sequence[Mapping[str, str]],
    raw_news_rows: Sequence[Mapping[str, Any]] | None,
    raw_news_probe: Mapping[str, Any],
    fake_data: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    processed_by_id: dict[str, Mapping[str, str]] = {}
    for row in processed_rows:
        article_id = _required_text(row.get("id"), "processed news id")
        if article_id in processed_by_id:
            raise SourceCandidateError(f"processed news has duplicate id: {article_id}")
        # The full legacy processed corpus has a small number of blank time
        # cells.  They are retained in the audit as a source-quality finding;
        # selected rows themselves still require an exact usable time below.
        _require_fields(row, ("title", "date", "time", "category", "summary"), "processed news")
        _parse_iso_date(_required_text(row["date"], "processed news date"), "processed news date")
        if str(row["time"]).strip():
            _parse_clock(_required_text(row["time"], "processed news time"), "processed news time")
        processed_by_id[article_id] = row

    raw_by_key: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    if raw_news_rows is not None:
        for index, raw in enumerate(raw_news_rows):
            if not isinstance(raw, Mapping):
                raise SourceCandidateError(f"raw news row[{index}] is not an object")
            date_value = _required_text(raw.get("date"), "raw news date")
            title = _required_text(raw.get("title"), "raw news title")
            raw_by_key[(date_value, title)].append(raw)

    candidate_date_set = set(candidate_dates)
    selected_rows: list[tuple[str, str, Mapping[str, str]]] = []
    seen_selection_ids: set[str] = set()
    previous_by_date = {
        day: stock_calendar_dates[index - 1]
        for index, day in enumerate(stock_calendar_dates)
        if index > 0 and day in candidate_date_set
    }
    for row in daily_rows:
        article_id = _required_text(row.get("id"), "daily news id")
        if article_id in seen_selection_ids:
            raise SourceCandidateError(f"daily news selection has duplicate id: {article_id}")
        seen_selection_ids.add(article_id)
        _require_fields(row, ("title", "date", "time", "category"), "daily news selection")
        day = _required_text(row["date"], "daily news selection date")
        _parse_iso_date(day, "daily news selection date")
        _parse_clock(_required_text(row["time"], "daily news selection time"), "daily news selection time")
        # Mirror the documented legacy pre-close candidate windows only for
        # inventory purposes.  This does *not* approve the rows as RN slots:
        # their article versions and as-of evidence are still absent.
        for event_day in candidate_dates:
            previous_day = previous_by_date.get(event_day)
            if previous_day is not None and (
                (day == previous_day and row["time"] > "15:30")
                or (day == event_day and row["time"] <= "08:59")
            ):
                selected_rows.append((f"{event_day}/AM", "AM", row))
            if day == event_day and "08:59" < row["time"] <= "15:30":
                selected_rows.append((f"{event_day}/PM", "PM", row))
    selected_rows.sort(key=lambda item: (item[0], item[2]["date"], item[2]["time"], item[2]["id"]))

    inventory: list[dict[str, Any]] = []
    orphan_ids: list[str] = []
    public_field_mismatch_ids: list[str] = []
    raw_missing_ids: list[str] = []
    raw_ambiguous_ids: list[str] = []
    per_date_counts: Counter[str] = Counter()
    per_event_counts: Counter[str] = Counter()
    for legacy_event_id, event_subturn, selected in selected_rows:
        article_id = selected["id"]
        processed = processed_by_id.get(article_id)
        if processed is None:
            orphan_ids.append(article_id)
            continue
        if any(processed[field] != selected[field] for field in ("title", "date", "time", "category")):
            public_field_mismatch_ids.append(article_id)
        matches = raw_by_key.get((processed["date"], processed["title"]), [])
        if not matches:
            raw_missing_ids.append(article_id)
        if len(matches) != 1:
            raw_ambiguous_ids.append(article_id)
        raw_records = [_raw_article_projection(raw) for raw in matches]
        entry = {
            "article_id": article_id,
            "legacy_candidate_event_id": legacy_event_id,
            "legacy_candidate_subturn": event_subturn,
            "date": processed["date"],
            "time": processed["time"],
            "legacy_time_bucket": "AM" if processed["time"] <= "08:59" else "PM",
            "title_sha256": _text_sha256(processed["title"]),
            "summary_sha256": _text_sha256(processed["summary"]),
            # Retained only while this local function performs its scanner.
            # ``_json_plain`` strips underscore-prefixed keys before any
            # candidate artifact is serialized, so no raw article text is
            # accidentally made into a new agent-visible input.
            "_scan_text": _normalise_scan_text(
                f"{processed['title']}\n{processed['summary']}"
            ),
            "raw_match_count": len(matches),
            "raw_article_candidates": raw_records,
            "observed_at": None,
            "last_modified_at": None,
            "cutoff_version_sha256": None,
            "article_version_as_of_status": "unproven",
        }
        inventory.append(entry)
        event_day = legacy_event_id.split("/", 1)[0]
        per_date_counts[event_day] += 1
        per_event_counts[legacy_event_id] += 1

    missing_selection_dates = [day for day in candidate_dates if per_date_counts.get(day, 0) == 0]
    selected_ids = {row["article_id"] for row in inventory}
    fake_overlap = sorted(selected_ids & set(fake_data["known_fake_ids"]))
    scanner_candidates = _scan_inventory(inventory)
    confirmed = _documented_counterexample(inventory)
    quarantine = _build_quarantine_report(
        source_by_label=source_by_label,
        config=config,
        inventory=inventory,
        scanner_candidates=scanner_candidates,
        confirmed=confirmed,
    )
    findings = {
        "processed_news_row_count": len(processed_rows),
        "daily_news_selection_row_count": len(daily_rows),
        "candidate_window_daily_selection_count": len(selected_rows),
        "candidate_window_inventory_count": len(inventory),
        "candidate_window_date_count": len(candidate_dates),
        "candidate_window_per_date_counts": {
            day: per_date_counts.get(day, 0) for day in candidate_dates
        },
        "candidate_window_per_event_counts": {
            f"{day}/{subturn}": per_event_counts.get(f"{day}/{subturn}", 0)
            for day in candidate_dates
            for subturn in ("AM", "PM")
        },
        "missing_selection_dates": missing_selection_dates,
        "daily_selection_orphan_ids": orphan_ids,
        "daily_selection_public_field_mismatch_ids": public_field_mismatch_ids,
        "raw_lineage_missing_ids": raw_missing_ids,
        "raw_lineage_ambiguous_ids": raw_ambiguous_ids,
        "raw_news_pickle_probe": raw_news_probe,
        "known_fake_id_overlap": fake_overlap,
        "immutable_article_version_fields": {
            "observed_at": "missing",
            "last_modified_at": "missing",
            "cutoff_version_sha256": "missing",
        },
        "leakage_quarantine_candidate_sha256": quarantine["candidate_sha256"],
        "runtime_bundle_construction": "blocked",
    }
    proposed = {
        "target_runtime_artifact_type": "real_news_bundle_manifest",
        "inventory": inventory,
        "slot_assignment": "not_created; legacy date/time rows are not approved article-version slots",
        "accepted_shortages": "not_created",
        "approval_state": "not_a_runtime_registry",
    }
    candidate = _candidate(
        kind="news_inventory",
        source_by_label=source_by_label,
        source_labels=(
            "processed_news_csv",
            "daily_news_selection_csv",
            "raw_news_pickle",
            "legacy_samsung_news_pickle",
        ),
        findings=findings,
        proposed_runtime_shape=proposed,
    )
    return candidate, quarantine


def _build_quarantine_report(
    *,
    source_by_label: Mapping[str, Mapping[str, Any]],
    config: CandidateBuildConfig,
    inventory: Sequence[Mapping[str, Any]],
    scanner_candidates: Sequence[Mapping[str, Any]],
    confirmed: Mapping[str, Any],
) -> dict[str, Any]:
    candidate_ids = {str(row["article_id"]) for row in scanner_candidates}
    candidate_ids.add(str(confirmed["article_id"]))
    findings = {
        "scan_scope": "legacy_selected_news_inventory_in_documented_candidate_window",
        "scanned_article_count": len(inventory),
        "semantic_review_candidate_count": len(scanner_candidates),
        "quarantined_article_count": len(candidate_ids),
        "documented_counterexample_id": _DOCUMENTED_LEAK_ID,
        "documented_counterexample_present": bool(confirmed["present_in_inventory"]),
        "eligible_article_count": 0,
        "release_rule": "no_article_is_runtime_eligible_until_immutable_as_of_version_evidence_and_review_are_bound",
    }
    proposed = {
        "target_runtime_artifact_type": "article_version_leakage_review_manifest",
        "decision": "quarantine_required",
        "confirmed_counterexamples": [dict(confirmed)],
        "semantic_review_candidates": [dict(item) for item in scanner_candidates],
        "quarantined_article_ids": sorted(candidate_ids),
        "automatic_allow_decisions": [],
        "approval_state": "not_a_runtime_review_manifest",
    }
    candidate = _candidate(
        kind="leakage_quarantine",
        source_by_label=source_by_label,
        source_labels=("processed_news_csv", "daily_news_selection_csv", "raw_news_pickle"),
        findings=findings,
        proposed_runtime_shape=proposed,
        artifact_type="rn_news_leakage_quarantine_report",
        version=QUARANTINE_VERSION,
    )
    return candidate


def _candidate(
    *,
    kind: str,
    source_by_label: Mapping[str, Mapping[str, Any]],
    source_labels: Iterable[str],
    findings: Mapping[str, Any],
    proposed_runtime_shape: Mapping[str, Any],
    artifact_type: str = "rn_source_input_candidate",
    version: str = CANDIDATE_VERSION,
) -> dict[str, Any]:
    bindings = {
        label: str(source_by_label[label]["sha256"])
        for label in sorted(source_labels)
    }
    payload: dict[str, Any] = {
        "artifact_type": artifact_type,
        "version": version,
        "candidate_kind": kind,
        "candidate_status": "not_authorized_candidate",
        "execution_authorized": False,
        "run_eligible": False,
        "source_file_sha256s": bindings,
        "blocking_reasons": list(_GLOBAL_BLOCKERS),
        "findings": _json_plain(findings),
        "proposed_runtime_shape": _json_plain(proposed_runtime_shape),
    }
    payload["candidate_sha256"] = canonical_sha256(payload)
    return payload


def _build_audit(
    *,
    config: CandidateBuildConfig,
    source_records: Sequence[Mapping[str, Any]],
    candidate_file_records: Sequence[Mapping[str, Any]],
    candidate_hashes: Mapping[str, str],
    candidate_dates: Sequence[str],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "artifact_type": "rn_source_input_candidate_audit",
        "version": AUDIT_VERSION,
        "study_id": config.study_id,
        "candidate_status": "no_go_source_candidate",
        "execution_authorized": False,
        "run_eligible": False,
        "generation_mode": "local_only_no_network_no_paid_api",
        "network_requests": 0,
        "paid_api_calls": 0,
        "source_files": [dict(record) for record in source_records],
        "candidate_files": [dict(record) for record in candidate_file_records],
        "candidate_canonical_sha256s": dict(sorted(candidate_hashes.items())),
        "calendar_contract": {
            "start_date": config.start_date,
            "end_date": config.end_date,
            "stock_calendar_date_ids": list(candidate_dates),
            "burn_in_date_ids": list(config.burn_in_dates),
            "expected_trading_date_count": config.expected_trading_date_count,
        },
        "blocking_reasons": list(_GLOBAL_BLOCKERS),
    }
    payload["audit_sha256"] = canonical_sha256(payload)
    return payload


def _collect_known_fake_closure(paths: SourceInputPaths) -> dict[str, Any]:
    variants = {
        "bearish": {
            "processed": paths.fake_bearish_processed_csv,
            "daily": paths.fake_bearish_daily_csv,
            "manifest": paths.fake_bearish_manifest,
            "pickle": paths.fake_bearish_pickle,
        },
        "bullish": {
            "processed": paths.fake_bullish_processed_csv,
            "daily": paths.fake_bullish_daily_csv,
            "manifest": paths.fake_bullish_manifest,
            "pickle": paths.fake_bullish_pickle,
        },
    }
    known_ids: set[str] = set()
    known_hashes: set[str] = set()
    variant_summary: dict[str, Any] = {}
    pickle_probes: dict[str, Any] = {}
    for name, config in variants.items():
        processed_rows = _read_csv_rows(config["processed"], f"{name} fake processed export")
        daily_rows = _read_csv_rows(config["daily"], f"{name} fake daily export")
        manifest = _read_json(config["manifest"], f"{name} fake manifest")
        processed_fake = _fake_rows(processed_rows, f"{name} fake processed export")
        daily_fake = _fake_rows(daily_rows, f"{name} fake daily export")
        processed_ids = {row["id"] for row in processed_fake}
        daily_ids = {row["id"] for row in daily_fake}
        known_ids.update(processed_ids)
        known_ids.update(daily_ids)
        known_hashes.update(canonical_sha256(row) for row in processed_fake)
        known_hashes.update(canonical_sha256(row) for row in daily_fake)
        expected_count = manifest.get("fake_count")
        variant_summary[name] = {
            "processed_fake_count": len(processed_fake),
            "daily_fake_count": len(daily_fake),
            "processed_daily_id_sets_match": processed_ids == daily_ids,
            "manifest_fake_count": expected_count,
            "manifest_count_matches_processed": expected_count == len(processed_fake),
            "manifest_count_matches_daily": expected_count == len(daily_fake),
            "manifest_canonical_sha256": canonical_sha256(manifest),
        }
        pickle_probes[name] = _probe_pickle(config["pickle"])
    pickle_probes["event_schedule"] = _probe_pickle(paths.fake_event_pickle)
    return {
        "known_fake_ids": tuple(sorted(known_ids)),
        "known_fake_row_hashes": tuple(sorted(known_hashes)),
        "variants": variant_summary,
        "pickle_probes": pickle_probes,
    }


def _fake_rows(rows: Sequence[Mapping[str, str]], label: str) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for row in rows:
        if not _is_true(row.get("is_fake")):
            continue
        article_id = _required_text(row.get("id"), f"{label} fake id")
        result.append({str(key): str(value) for key, value in sorted(row.items())})
        if result[-1]["id"] != article_id:
            raise SourceCandidateError(f"{label} fake row has invalid id")
    return result


def _probe_pickle(path: Path) -> dict[str, Any]:
    """Record a bounded local probe without treating pickle metadata as approval.

    Historical pandas pickles can be unreadable under a different pandas
    release.  That compatibility issue is a transparent finding, not a reason
    to substitute data or make an unearned closure claim.
    """

    try:
        with path.open("rb") as handle:
            value = pickle.load(handle)
    except Exception as exc:  # noqa: BLE001 - record compatibility evidence, not stack traces.
        return {
            "parse_status": "unparsed",
            "python_pickle_error": type(exc).__name__,
            "row_count": None,
        }
    if isinstance(value, list):
        return {"parse_status": "parsed_list", "row_count": len(value)}
    if hasattr(value, "shape") and hasattr(value, "columns"):
        return {
            "parse_status": "parsed_tabular_object",
            "row_count": int(value.shape[0]),
            "column_count": int(len(value.columns)),
        }
    return {"parse_status": "parsed_unknown_object", "python_type": type(value).__name__}


def _load_raw_news_pickle(path: Path) -> tuple[list[Mapping[str, Any]] | None, Mapping[str, Any]]:
    try:
        with path.open("rb") as handle:
            value = pickle.load(handle)
    except Exception as exc:  # noqa: BLE001 - reflected in no-go audit.
        return None, {
            "parse_status": "unparsed",
            "python_pickle_error": type(exc).__name__,
            "row_count": None,
        }
    if not isinstance(value, list):
        return None, {
            "parse_status": "unsupported_non_list",
            "python_type": type(value).__name__,
            "row_count": None,
        }
    return value, {"parse_status": "parsed_list", "row_count": len(value)}


def _read_stock_rows(path: Path) -> list[dict[str, Any]]:
    rows = _read_csv_rows(path, "stock data")
    required = ("date", "open", "high", "low", "close")
    parsed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        _require_fields(row, required, "stock data")
        day = _required_text(row["date"], "stock date")
        _parse_iso_date(day, "stock date")
        if day in seen:
            raise SourceCandidateError(f"stock data has duplicate date: {day}")
        seen.add(day)
        normalized = {
            "date": day,
            "open": _positive_number(row["open"], "stock open"),
            "high": _positive_number(row["high"], "stock high"),
            "low": _positive_number(row["low"], "stock low"),
            "close": _positive_number(row["close"], "stock close"),
            "row_sha256": canonical_sha256({str(key): str(value) for key, value in sorted(row.items())}),
        }
        if normalized["low"] > normalized["high"]:
            raise SourceCandidateError(f"stock row {day} has low above high")
        parsed.append(normalized)
    parsed.sort(key=lambda item: str(item["date"]))
    return parsed


def _read_macro_rows(path: Path) -> list[dict[str, Any]]:
    rows = _read_csv_rows(path, "macro data")
    required = ("date", "kospi_close", "usdkrw")
    parsed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        _require_fields(row, required, "macro data")
        day = _required_text(row["date"], "macro date")
        _parse_iso_date(day, "macro date")
        if day in seen:
            raise SourceCandidateError(f"macro data has duplicate date: {day}")
        seen.add(day)
        parsed.append(
            {
                "date": day,
                # The legacy macro CSV contains blank values on some dates.
                # Preserve those as missing candidate evidence instead of
                # inventing a carry-forward value or aborting the audit.
                "kospi_close": _optional_positive_number(row["kospi_close"], "macro kospi_close"),
                "usdkrw": _optional_positive_number(row["usdkrw"], "macro usdkrw"),
                "row_sha256": canonical_sha256(
                    {str(key): str(value) for key, value in sorted(row.items())}
                ),
            }
        )
    parsed.sort(key=lambda item: str(item["date"]))
    return parsed


def _read_target_values(path: Path, label: str) -> dict[str, Decimal]:
    rows = _read_csv_rows(path, label)
    result: dict[str, Decimal] = {}
    for row in rows:
        _require_fields(row, ("Date", "Individuals"), label)
        raw_date = _required_text(row["Date"], f"{label} Date")
        try:
            day = datetime.strptime(raw_date, "%Y/%m/%d").date().isoformat()
        except ValueError:
            day = _parse_iso_date(raw_date, f"{label} Date").isoformat()
        if day in result:
            raise SourceCandidateError(f"{label} has duplicate date: {day}")
        try:
            value = Decimal(_required_text(row["Individuals"], f"{label} Individuals"))
        except InvalidOperation as exc:
            raise SourceCandidateError(f"{label} Individuals is not numeric on {day}") from exc
        if not value.is_finite():
            raise SourceCandidateError(f"{label} Individuals is non-finite on {day}")
        result[day] = value
    return result


def _read_fixed_slots(path: Path) -> dict[str, dict[str, Any]]:
    rows = _read_csv_rows(path, "fixed slots")
    required = ("agent_id", "성별", "나이", "연령대", "운용자산")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        _require_fields(row, required, "fixed slots")
        agent_id = _required_text(row["agent_id"], "fixed slot agent_id")
        if agent_id in result:
            raise SourceCandidateError(f"fixed slots has duplicate agent_id: {agent_id}")
        try:
            age = int(_required_text(row["나이"], "fixed slot age"))
        except ValueError as exc:
            raise SourceCandidateError(f"fixed slot has invalid age: {agent_id}") from exc
        result[agent_id] = {
            "gender": _required_text(row["성별"], "fixed slot gender"),
            "age": age,
            "age_group": _required_text(row["연령대"], "fixed slot age group"),
            "initial_cash": _korean_asset_to_cash(_required_text(row["운용자산"], "fixed slot asset")),
        }
    return result


def _scan_inventory(inventory: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for entry in inventory:
        text = _normalise_scan_text(str(entry["article_id"]))
        # We intentionally scan hashes' source titles/summaries by obtaining the
        # original text from the inventory's compact projections is impossible.
        # ``_raw_article_projection`` carries no summary; therefore each entry
        # may attach scan text below through private keys while building.
        scan_text = str(entry.get("_scan_text", text))
        matches = [
            label for label, pattern in _SEMANTIC_LEAKAGE_PATTERNS if pattern.search(scan_text)
        ]
        if matches:
            rows.append(
                {
                    "article_id": entry["article_id"],
                    "date": entry["date"],
                    "time": entry["time"],
                    "title_sha256": entry["title_sha256"],
                    "summary_sha256": entry["summary_sha256"],
                    "scanner_matches": matches,
                    "review_status": "quarantine_pending_blinded_article_version_review",
                }
            )
    rows.sort(key=lambda row: (str(row["date"]), str(row["time"]), str(row["article_id"])))
    return rows


def _documented_counterexample(inventory: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    matches = [entry for entry in inventory if entry["article_id"] == _DOCUMENTED_LEAK_ID]
    if len(matches) > 1:
        raise SourceCandidateError("documented leakage counterexample appears more than once")
    if matches:
        entry = matches[0]
        return {
            "article_id": _DOCUMENTED_LEAK_ID,
            "present_in_inventory": True,
            "date": entry["date"],
            "time": entry["time"],
            "title_sha256": entry["title_sha256"],
            "summary_sha256": entry["summary_sha256"],
            "decision": "quarantine",
            "reason": "documented_09_11_payload_contains_same_day_eod_price_and_final_investor_flow",
        }
    return {
        "article_id": _DOCUMENTED_LEAK_ID,
        "present_in_inventory": False,
        "decision": "quarantine",
        "reason": "documented_counterexample_must_remain_in_candidate_review_ledger_even_if_not_selected",
    }


def _raw_article_projection(raw: Mapping[str, Any]) -> dict[str, Any]:
    title = _required_text(raw.get("title"), "raw news title")
    body = _required_text(raw.get("body"), "raw news body")
    summary = _required_text(raw.get("summary"), "raw news summary")
    url = _required_text(raw.get("url"), "raw news url")
    source = _required_text(raw.get("source"), "raw news source")
    return {
        "source": source,
        "source_url": url,
        "raw_body_sha256": _text_sha256(body),
        "raw_summary_sha256": _text_sha256(summary),
        "raw_title_sha256": _text_sha256(title),
        "raw_record_sha256": canonical_sha256(_json_plain(raw)),
    }


def _normalise_scan_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _source_records(paths: SourceInputPaths) -> list[dict[str, Any]]:
    records = []
    for label, path in paths.labelled_files():
        _assert_real_file(path, label)
        try:
            relative = path.resolve().relative_to(paths.project_root.resolve()).as_posix()
        except ValueError as exc:
            raise SourceCandidateError(f"source path is outside project root: {path}") from exc
        records.append(
            {
                "label": label,
                "path": relative,
                "bytes": path.stat().st_size,
                "sha256": _file_sha256(path),
            }
        )
    return sorted(records, key=lambda item: str(item["label"]))


def _validate_paths(paths: SourceInputPaths) -> None:
    _assert_real_directory(paths.project_root, "project root")
    labels: set[str] = set()
    seen_paths: set[Path] = set()
    for label, path in paths.labelled_files():
        if label in labels:
            raise SourceCandidateError(f"duplicate source label: {label}")
        labels.add(label)
        if path in seen_paths:
            raise SourceCandidateError(f"source path is declared twice: {path}")
        seen_paths.add(path)
        _assert_real_file(path, label)
        try:
            path.resolve().relative_to(paths.project_root.resolve())
        except ValueError as exc:
            raise SourceCandidateError(f"source path is outside project root: {path}") from exc


def _read_csv_rows(path: Path, label: str) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise SourceCandidateError(f"{label} has no header")
            headers = [str(item).strip() for item in reader.fieldnames]
            if not all(headers) or len(headers) != len(set(headers)):
                raise SourceCandidateError(f"{label} has blank or duplicate headers")
            result: list[dict[str, str]] = []
            for index, raw in enumerate(reader, start=2):
                if None in raw:
                    raise SourceCandidateError(f"{label} has too many columns at row {index}")
                row = {str(key).strip(): "" if value is None else str(value) for key, value in raw.items()}
                if not any(value.strip() for value in row.values()):
                    continue
                result.append(row)
            return result
    except UnicodeDecodeError as exc:
        raise SourceCandidateError(f"{label} is not UTF-8 text") from exc


def _read_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SourceCandidateError(f"{label} is not valid JSON") from exc
    if not isinstance(value, Mapping):
        raise SourceCandidateError(f"{label} must be a JSON object")
    return value


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(_json_plain(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n",
        encoding="utf-8",
    )


def _write_sha256sums(*, path: Path, paths: Sequence[Path]) -> None:
    rows = [f"{_file_sha256(item)}  {item.name}" for item in sorted(paths, key=lambda item: item.name)]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _validate_candidate(value: Mapping[str, Any], *, kind: str) -> None:
    if set(value) != _CANDIDATE_FIELDS:
        raise SourceCandidateError(f"{kind} candidate has an invalid exact key set")
    if value.get("artifact_type") != "rn_source_input_candidate" or value.get("version") != CANDIDATE_VERSION:
        raise SourceCandidateError(f"{kind} candidate has an unsupported artifact/version")
    if value.get("candidate_kind") != kind:
        raise SourceCandidateError(f"{kind} candidate kind differs from filename")
    _validate_no_go_candidate(value, kind)


def _validate_quarantine(value: Mapping[str, Any]) -> None:
    if set(value) != _CANDIDATE_FIELDS:
        raise SourceCandidateError("leakage quarantine report has an invalid exact key set")
    if value.get("artifact_type") != "rn_news_leakage_quarantine_report" or value.get("version") != QUARANTINE_VERSION:
        raise SourceCandidateError("leakage quarantine report has an unsupported artifact/version")
    if value.get("candidate_kind") != "leakage_quarantine":
        raise SourceCandidateError("leakage quarantine report kind differs from filename")
    _validate_no_go_candidate(value, "leakage quarantine report")
    proposed = value.get("proposed_runtime_shape")
    if not isinstance(proposed, Mapping):
        raise SourceCandidateError("leakage quarantine report has no proposed review shape")
    counterexamples = proposed.get("confirmed_counterexamples")
    if not isinstance(counterexamples, list) or not any(
        isinstance(row, Mapping)
        and row.get("article_id") == _DOCUMENTED_LEAK_ID
        and row.get("decision") == "quarantine"
        for row in counterexamples
    ):
        raise SourceCandidateError("leakage quarantine report omits the documented counterexample")


def _validate_no_go_candidate(value: Mapping[str, Any], label: str) -> None:
    if value.get("candidate_status") != "not_authorized_candidate":
        raise SourceCandidateError(f"{label} is not explicitly non-authorized")
    if value.get("execution_authorized") is not False or value.get("run_eligible") is not False:
        raise SourceCandidateError(f"{label} may not authorize execution")
    _verify_self_hash(value, "candidate_sha256", label)
    source_map = value.get("source_file_sha256s")
    if not isinstance(source_map, Mapping) or not source_map:
        raise SourceCandidateError(f"{label} has no source hash bindings")
    for source_label, digest in source_map.items():
        if not isinstance(source_label, str) or not source_label:
            raise SourceCandidateError(f"{label} has an invalid source label")
        _required_sha256(digest, f"{label} source hash")
    reasons = value.get("blocking_reasons")
    if not isinstance(reasons, list) or not set(_GLOBAL_BLOCKERS).issubset(reasons):
        raise SourceCandidateError(f"{label} is missing required no-go reasons")


def _verify_self_hash(value: Mapping[str, Any], key: str, label: str) -> None:
    digest = _required_sha256(value.get(key), f"{label} {key}")
    body = dict(value)
    body.pop(key, None)
    if canonical_sha256(body) != digest:
        raise SourceCandidateError(f"{label} self hash differs")


def _validate_file_records(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise SourceCandidateError(f"{label} must be a non-empty array")
    records: list[dict[str, Any]] = []
    seen_labels: set[str] = set()
    seen_paths: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) != _FILE_RECORD_FIELDS:
            raise SourceCandidateError(f"{label} contains an invalid file record")
        record_label = _required_text(item.get("label"), f"{label} record label")
        path = _safe_relative_path(item.get("path"), f"{label} record path")
        size = item.get("bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise SourceCandidateError(f"{label} record byte count is invalid")
        digest = _required_sha256(item.get("sha256"), f"{label} record hash")
        if record_label in seen_labels or path in seen_paths:
            raise SourceCandidateError(f"{label} has duplicate labels or paths")
        seen_labels.add(record_label)
        seen_paths.add(path)
        records.append({"label": record_label, "path": path, "bytes": size, "sha256": digest})
    return records


def _generated_file_record(kind: str, path: Path) -> dict[str, Any]:
    return {
        "label": kind,
        "path": path.name,
        "bytes": path.stat().st_size,
        "sha256": _file_sha256(path),
    }


def _validate_sha256sums(*, base: Path, path: Path, expected_paths: Sequence[str]) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise SourceCandidateError("candidate SHA256SUMS is not UTF-8") from exc
    actual: dict[str, str] = {}
    for line in lines:
        if not re.fullmatch(r"[0-9a-f]{64}  [A-Za-z0-9_.-]+", line):
            raise SourceCandidateError("candidate SHA256SUMS has an invalid line")
        digest, filename = line.split("  ", 1)
        if filename in actual:
            raise SourceCandidateError("candidate SHA256SUMS has duplicate filenames")
        actual[filename] = digest
    if set(actual) != set(expected_paths):
        raise SourceCandidateError("candidate SHA256SUMS does not cover the fixed output set")
    for filename, digest in actual.items():
        if _file_sha256(_safe_child(base, filename)) != digest:
            raise SourceCandidateError(f"candidate SHA256SUMS hash differs: {filename}")


def _safe_project_file(project_root: Path, relative: str) -> Path:
    path = _safe_relative_path(relative, "source record path")
    candidate = project_root / path
    _assert_real_file(candidate, "candidate source")
    try:
        candidate.resolve().relative_to(project_root.resolve())
    except ValueError as exc:
        raise SourceCandidateError("candidate source escaped project root") from exc
    return candidate


def _safe_child(root: Path, filename: str) -> Path:
    if Path(filename).name != filename:
        raise SourceCandidateError("candidate path must be a basename")
    path = root / filename
    _assert_real_file(path, f"candidate file {filename}")
    return path


def _safe_relative_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value.startswith("/"):
        raise SourceCandidateError(f"{label} must be a safe relative path")
    path = Path(value)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise SourceCandidateError(f"{label} must be a safe relative path")
    return path.as_posix()


def _assert_real_directory(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise SourceCandidateError(f"{label} must be a real directory: {path}")


def _assert_real_file(path: Path, label: str) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise SourceCandidateError(f"required {label} is missing: {path}") from exc
    if path.is_symlink() or not stat.S_ISREG(mode):
        raise SourceCandidateError(f"{label} must be a regular non-symlink file: {path}")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_fields(row: Mapping[str, Any], fields: Iterable[str], label: str) -> None:
    missing = [field for field in fields if field not in row]
    if missing:
        raise SourceCandidateError(f"{label} is missing required fields: {missing}")


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceCandidateError(f"{label} must be a non-empty string")
    return value.strip()


def _required_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SourceCandidateError(f"{label} must be an integer")
    return value


def _required_sha256(value: Any, label: str) -> str:
    text = _required_text(value, label)
    if not re.fullmatch(r"[0-9a-f]{64}", text):
        raise SourceCandidateError(f"{label} must be a lowercase SHA-256")
    return text


def _parse_iso_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SourceCandidateError(f"{label} must use YYYY-MM-DD") from exc


def _parse_clock(value: str, label: str) -> None:
    try:
        datetime.strptime(value, "%H:%M")
    except ValueError as exc:
        raise SourceCandidateError(f"{label} must use HH:MM") from exc


def _positive_number(value: Any, label: str) -> int | float:
    text = _required_text(value, label)
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:
        raise SourceCandidateError(f"{label} must be numeric") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise SourceCandidateError(f"{label} must be finite and positive")
    if parsed == parsed.to_integral_value():
        return int(parsed)
    numeric = float(parsed)
    if not math.isfinite(numeric):
        raise SourceCandidateError(f"{label} must be finite")
    return numeric


def _optional_positive_number(value: Any, label: str) -> int | float | None:
    if isinstance(value, str) and not value.strip():
        return None
    return _positive_number(value, label)


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _direction(value: Decimal) -> str:
    if value > 0:
        return "BUY"
    if value < 0:
        return "SELL"
    return "FLAT"


def _korean_asset_to_cash(value: str) -> int:
    cleaned = value.replace(",", "").replace("원", "").strip()
    if cleaned.endswith("억") and cleaned[:-1].isdigit():
        return int(cleaned[:-1]) * 100_000_000
    if cleaned.isdigit():
        parsed = int(cleaned)
        if parsed > 0:
            return parsed
    raise SourceCandidateError(f"unsupported fixed-slot asset value: {value!r}")


def _is_true(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "y"}


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_plain(item) for key, item in value.items() if not str(key).startswith("_")}
    if isinstance(value, (list, tuple)):
        return [_json_plain(item) for item in value]
    if isinstance(value, Decimal):
        return _decimal_text(value)
    return value


__all__ = [
    "AUDIT_VERSION",
    "CANDIDATE_VERSION",
    "CandidateBuildConfig",
    "SourceCandidateArtifacts",
    "SourceCandidateError",
    "SourceInputPaths",
    "build_source_input_candidates",
    "validate_source_input_candidates",
]
