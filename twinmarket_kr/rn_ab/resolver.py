"""Resolution and sealing for RN Community ON/OFF study inputs.

The resolver consumes a validated :class:`~twinmarket_kr.rn_ab.spec.StudySpec`
and three immutable registries.  It is the sole place that derives dates,
decision turns, agent/event keys, and expected counts.  Runtime code should
consume :class:`ResolvedStudyManifest` rather than repeat any of this math.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from twinmarket_kr.rn_ab.spec import (
    RN_COMM_OFF,
    RN_COMM_ON,
    RN_CONDITIONS,
    ArmPairValidationError,
    StudySpec,
    StudySpecError,
    assert_only_community_mode_diff,
    canonical_json_bytes,
    canonical_sha256,
)
from twinmarket_kr.rn_ab.stage_inputs import SealedStageInputRegistry, StageInputRegistryError


class ResolutionError(StudySpecError):
    """Raised when an immutable registry cannot satisfy the authored spec."""


class PathSafetyError(ResolutionError):
    """Raised for an unsafe, aliased, or out-of-root input/output path."""


_AGENT_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_EVENT_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}/[A-Za-z][A-Za-z0-9_-]{0,63}$")
_SUBTURN_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,31}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
_KST_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+09:00$")
_LEGACY_COMPONENT_RE = re.compile(
    r"(?:^|[_-])(?:c00|c01|c02|c10|c11|c12|rn_c00|rn_c10)(?:[_-]|$)",
    re.IGNORECASE,
)
_FORBIDDEN_NAMESPACE_COMPONENTS: Final[frozenset[str]] = frozenset({"current", "latest"})

_COHORT_REGISTRY_FIELDS: Final[frozenset[str]] = frozenset({"artifact_type", "version", "agents"})
_COHORT_MEMBER_FIELDS: Final[frozenset[str]] = frozenset(
    {"ordinal", "agent_id", "news_depth", "initial_cash", "persona_sha256", "fixed_slot_sha256"}
)
_CALENDAR_REGISTRY_FIELDS: Final[frozenset[str]] = frozenset({"artifact_type", "version", "dates"})
_CALENDAR_DATE_FIELDS: Final[frozenset[str]] = frozenset(
    {"date", "timezone", "decision_events", "post_decision_phases"}
)
_DECISION_EVENT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "decision_event_id",
        "event_ordinal_in_date",
        "subturn",
        "decision_timestamp",
        "news_window",
        "market_feature_as_of",
        "execution_price_field",
        "consume_scheduled_community",
        "decision_enabled",
    }
)
_COMMUNITY_PHASE_FIELDS: Final[frozenset[str]] = frozenset(
    {"phase_id", "after_event_id", "next_visible_event_rule"}
)
_EVENT_PRICE_REGISTRY_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "artifact_type",
        "version",
        "stock_code",
        "calendar_event_registry_sha256",
        "events",
    }
)
_EVENT_PRICE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "decision_event_id",
        "date",
        "subturn",
        "execution_price_field",
        "execution_price",
    }
)
_EVALUATOR_HASH_EXCLUDED_FIELDS: Final[tuple[str, str]] = (
    "manifest_hash",
    "resolved_manifest_sha256",
)
_EVALUATOR_HASH_ALGORITHM: Final = (
    "sha256-utf8-compact-json-object-excluding-"
    "manifest_hash-and-resolved_manifest_sha256"
)
_EVALUATOR_CONTRACT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "artifact_type",
        "contract_version",
        "manifest_hash_algorithm",
        "authoritative_resolved_manifest_sha256",
        "source_study_spec_sha256",
        "condition_pair_id",
        "pair_invariant_hash",
        "conditions",
        "cohort",
        "event_calendar",
        "burn_in_dates",
        "evaluation_dates",
        "price_registry",
        "manifest_hash",
        "resolved_manifest_sha256",
    }
)


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResolutionError(f"{field} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ResolutionError(f"{field} keys must be strings")
    return value


def _exact_keys(value: Any, field: str, expected: frozenset[str]) -> Mapping[str, Any]:
    mapping = _mapping(value, field)
    unknown = sorted(set(mapping) - expected)
    missing = sorted(expected - set(mapping))
    if unknown or missing:
        details: list[str] = []
        if unknown:
            details.append(f"unknown={unknown}")
        if missing:
            details.append(f"missing={missing}")
        raise ResolutionError(f"{field} has invalid keys ({', '.join(details)})")
    return mapping


def _nonempty_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResolutionError(f"{field} must be a non-empty string")
    text = value.strip()
    if text.lower() in {"pending", "tbd", "null", "none"} or (text.startswith("<") and text.endswith(">")):
        raise ResolutionError(f"{field} contains an unresolved placeholder")
    return text


def _positive_int(value: Any, field: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ResolutionError(f"{field} must be an integer")
    if value < 0 or (value == 0 and not allow_zero):
        relation = "non-negative" if allow_zero else "positive"
        raise ResolutionError(f"{field} must be {relation}")
    return value


def _positive_price(value: Any, field: str) -> int | float:
    """Accept only a finite, positive JSON number for a sealed price row."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResolutionError(f"{field} must be a numeric JSON value")
    if isinstance(value, float) and not math.isfinite(value):
        raise ResolutionError(f"{field} must be finite")
    if value <= 0:
        raise ResolutionError(f"{field} must be positive")
    return value


def _strict_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ResolutionError(f"{field} must be a boolean")
    return value


def _sha256(value: Any, field: str) -> str:
    text = _nonempty_str(value, field)
    if not _SHA256_RE.fullmatch(text):
        raise ResolutionError(f"{field} must be a 64-character SHA-256 hex digest")
    return text.lower()


def _date_id(value: Any, field: str) -> str:
    text = _nonempty_str(value, field)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise ResolutionError(f"{field} must use YYYY-MM-DD")
    try:
        from datetime import date

        date.fromisoformat(text)
    except ValueError as exc:
        raise ResolutionError(f"{field} is not a valid calendar date: {text}") from exc
    return text


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _plain_json(value.to_dict())
    return value


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _lexical_absolute(path: Path | str) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _assert_no_symlink_components(path: Path, *, trusted_root: Path) -> None:
    """Reject symlinks at or below an explicitly declared trust boundary.

    The check intentionally starts at ``trusted_root`` rather than filesystem
    root.  macOS commonly exposes temporary files through ``/var`` even though
    that OS-owned ancestor is a symlink to ``/private/var``.  Rejecting it would
    make safe temp directories unusable, while checking from the declared
    input/output root still rejects every caller-controlled alias.
    """

    if not path.is_absolute() or not trusted_root.is_absolute():
        raise PathSafetyError("paths must be absolute after normalization")
    try:
        relative = path.relative_to(trusted_root)
    except ValueError as exc:
        raise PathSafetyError(f"path is outside its trusted root: {path}") from exc

    try:
        root_mode = trusted_root.lstat().st_mode
    except FileNotFoundError as exc:
        raise PathSafetyError(f"trusted root does not exist: {trusted_root}") from exc
    if stat.S_ISLNK(root_mode):
        raise PathSafetyError(f"symbolic links are forbidden in RN paths: {trusted_root}")

    cursor = trusted_root
    for component in relative.parts:
        cursor = cursor / component
        try:
            mode = cursor.lstat().st_mode
        except FileNotFoundError:
            # A missing suffix is allowed only for a newly-created output path.
            break
        if stat.S_ISLNK(mode):
            raise PathSafetyError(f"symbolic links are forbidden in RN paths: {cursor}")


def assert_safe_input_file(path: Path | str, *, allowed_root: Path | str) -> Path:
    """Return a regular, non-symlink file located lexically under ``allowed_root``."""

    root = _lexical_absolute(allowed_root)
    candidate = _lexical_absolute(path)
    if not root.exists() or not root.is_dir():
        raise PathSafetyError(f"allowed input root must be an existing directory: {root}")
    _assert_no_symlink_components(root, trusted_root=root)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise PathSafetyError(f"input path is outside allowed root: {candidate}") from exc
    _assert_no_symlink_components(candidate, trusted_root=root)
    try:
        mode = candidate.lstat().st_mode
    except FileNotFoundError as exc:
        raise PathSafetyError(f"input file does not exist: {candidate}") from exc
    if not stat.S_ISREG(mode):
        raise PathSafetyError(f"input must be a regular file: {candidate}")
    return candidate


def ensure_safe_run_directory(
    run_dir: Path | str,
    *,
    output_root: Path | str,
    allow_existing: bool = False,
) -> Path:
    """Create/validate a sealed output namespace without ``current`` aliases.

    It is intentionally conservative: a paper artifact must live below a new
    output root (or an explicitly exact-resume root), never below a symlink or
    legacy/current namespace.
    """

    root = _lexical_absolute(output_root)
    target = _lexical_absolute(run_dir)
    if not root.exists() or not root.is_dir():
        raise PathSafetyError(f"output_root must be an existing directory: {root}")
    _assert_no_symlink_components(root, trusted_root=root)
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise PathSafetyError(f"run directory is outside output_root: {target}") from exc
    if not relative.parts:
        raise PathSafetyError("run directory must be a child of output_root")
    for component in relative.parts:
        lowered = component.lower()
        if lowered in _FORBIDDEN_NAMESPACE_COMPONENTS or _LEGACY_COMPONENT_RE.search(component):
            raise PathSafetyError(f"forbidden RN output namespace component: {component}")
    _assert_no_symlink_components(target, trusted_root=root)
    if target.exists():
        if target.is_symlink() or not target.is_dir():
            raise PathSafetyError(f"run directory must be a real directory: {target}")
        if not allow_existing:
            raise PathSafetyError(f"run directory already exists: {target}")
    else:
        target.mkdir(parents=True, exist_ok=False)
        _assert_no_symlink_components(target, trusted_root=root)
    return target


@dataclass(frozen=True)
class CohortMember:
    ordinal: int
    agent_id: str
    news_depth: int
    initial_cash: int
    persona_sha256: str
    fixed_slot_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "agent_id": self.agent_id,
            "news_depth": self.news_depth,
            "initial_cash": self.initial_cash,
            "persona_sha256": self.persona_sha256,
            "fixed_slot_sha256": self.fixed_slot_sha256,
        }


@dataclass(frozen=True)
class CohortRegistry:
    version: str
    members: tuple[CohortMember, ...]
    canonical_sha256: str
    file_sha256: str

    @property
    def agent_ids(self) -> tuple[str, ...]:
        return tuple(member.agent_id for member in self.members)

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_type": "cohort_registry",
            "version": self.version,
            "agents": [member.to_dict() for member in self.members],
        }


@dataclass(frozen=True)
class DecisionEvent:
    date: str
    event_ordinal_in_date: int
    decision_event_id: str
    subturn: str
    decision_timestamp: str
    news_window: Mapping[str, Any]
    market_feature_as_of: str
    execution_price_field: str
    consume_scheduled_community: bool
    decision_enabled: bool
    global_ordinal: int | None = None

    def to_registry_dict(self) -> dict[str, Any]:
        """Return exactly the event shape pinned in a calendar registry."""

        return {
            "event_ordinal_in_date": self.event_ordinal_in_date,
            "decision_event_id": self.decision_event_id,
            "subturn": self.subturn,
            "decision_timestamp": self.decision_timestamp,
            "news_window": _plain_json(self.news_window),
            "market_feature_as_of": self.market_feature_as_of,
            "execution_price_field": self.execution_price_field,
            "consume_scheduled_community": self.consume_scheduled_community,
            "decision_enabled": self.decision_enabled,
        }

    def to_dict(self) -> dict[str, Any]:
        """Return the resolved event shape, including resolver-derived fields."""

        result = {"date": self.date, **self.to_registry_dict()}
        if self.global_ordinal is not None:
            result["global_ordinal"] = self.global_ordinal
        return result


@dataclass(frozen=True)
class CommunityPhase:
    date: str
    phase_id: str
    after_event_id: str
    next_visible_event_rule: str

    def to_dict(self) -> dict[str, str]:
        return {
            "date": self.date,
            "phase_id": self.phase_id,
            "after_event_id": self.after_event_id,
            "next_visible_event_rule": self.next_visible_event_rule,
        }


@dataclass(frozen=True)
class CalendarRegistry:
    version: str
    trading_dates: tuple[str, ...]
    all_events: tuple[DecisionEvent, ...]
    decision_events: tuple[DecisionEvent, ...]
    community_phases: tuple[CommunityPhase, ...]
    canonical_sha256: str
    file_sha256: str

    def to_dict(self) -> dict[str, Any]:
        events_by_date: dict[str, list[DecisionEvent]] = {day: [] for day in self.trading_dates}
        phases_by_date: dict[str, list[CommunityPhase]] = {day: [] for day in self.trading_dates}
        for event in self.all_events:
            events_by_date[event.date].append(event)
        for phase in self.community_phases:
            phases_by_date[phase.date].append(phase)
        return {
            "artifact_type": "calendar_event_registry",
            "version": self.version,
            "dates": [
                {
                    "date": day,
                    "timezone": "Asia/Seoul",
                    "decision_events": [event.to_registry_dict() for event in events_by_date[day]],
                    "post_decision_phases": [
                        {
                            "phase_id": phase.phase_id,
                            "after_event_id": phase.after_event_id,
                            "next_visible_event_rule": phase.next_visible_event_rule,
                        }
                        for phase in phases_by_date[day]
                    ],
                }
                for day in self.trading_dates
            ],
        }


@dataclass(frozen=True)
class EventPrice:
    """One frozen numeric execution price bound to one resolved decision event."""

    decision_event_id: str
    date: str
    subturn: str
    execution_price_field: str
    execution_price: int | float

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_event_id": self.decision_event_id,
            "date": self.date,
            "subturn": self.subturn,
            "execution_price_field": self.execution_price_field,
            "execution_price": self.execution_price,
        }


@dataclass(frozen=True)
class EventPriceRegistry:
    """Immutable, calendar-bound numeric price registry for evaluator use."""

    version: str
    stock_code: str
    calendar_event_registry_sha256: str
    prices: tuple[EventPrice, ...]
    canonical_sha256: str
    file_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_type": "event_price_registry",
            "version": self.version,
            "stock_code": self.stock_code,
            "calendar_event_registry_sha256": self.calendar_event_registry_sha256,
            "events": [price.to_dict() for price in self.prices],
        }


@dataclass(frozen=True)
class AgentEventKey:
    condition_id: str
    agent_id: str
    decision_event_id: str
    date: str
    subturn: str
    global_turn: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "agent_id": self.agent_id,
            "decision_event_id": self.decision_event_id,
            "date": self.date,
            "subturn": self.subturn,
            "global_turn": self.global_turn,
        }


@dataclass(frozen=True)
class CommunityPhaseKey:
    condition_id: str
    phase_id: str
    date: str
    after_event_id: str

    def to_dict(self) -> dict[str, str]:
        return {
            "condition_id": self.condition_id,
            "phase_id": self.phase_id,
            "date": self.date,
            "after_event_id": self.after_event_id,
        }


@dataclass(frozen=True)
class NewsSlotKey:
    decision_event_id: str
    slot_ordinal: int

    def to_dict(self) -> dict[str, Any]:
        return {"decision_event_id": self.decision_event_id, "slot_ordinal": self.slot_ordinal}


@dataclass(frozen=True)
class ResolvedStudyManifest:
    """Immutable resolver output used by scheduler, validator, and report code."""

    spec: StudySpec
    cohort: CohortRegistry
    calendar: CalendarRegistry
    burn_in_date_ids: tuple[str, ...]
    evaluation_date_ids: tuple[str, ...]
    logical_agent_event_keys: tuple[AgentEventKey, ...]
    community_phase_keys: tuple[CommunityPhaseKey, ...]
    planned_news_slot_keys: tuple[NewsSlotKey, ...]
    expected_key_set_hashes: Mapping[str, str]
    resolved_counts: Mapping[str, int]

    @property
    def conditions(self) -> tuple[str, str]:
        return RN_CONDITIONS

    @property
    def agent_ids(self) -> tuple[str, ...]:
        return self.cohort.agent_ids

    @property
    def trading_dates(self) -> tuple[str, ...]:
        return self.calendar.trading_dates

    @property
    def decision_events(self) -> tuple[DecisionEvent, ...]:
        return self.calendar.decision_events

    @property
    def source_study_spec_sha256(self) -> str:
        return self.spec.sha256

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.to_dict())

    def expected_keys(self, stage: str, *, condition_id: str | None = None) -> tuple[AgentEventKey, ...]:
        """Return the exact logical key set for a decision-stage ledger.

        STB, LTB transition, analysis, decision, and final fill all share the
        same resolved domain.  This method makes the shared-domain invariant
        explicit without duplicating thousands of rows in memory.
        """

        allowed = {"stb", "ltb_transition", "analysis", "decision", "fill"}
        if stage not in allowed:
            raise ResolutionError(f"unknown RN decision stage: {stage}")
        if condition_id is None:
            return self.logical_agent_event_keys
        if condition_id not in RN_CONDITIONS:
            raise ResolutionError(f"unknown RN condition: {condition_id}")
        return tuple(key for key in self.logical_agent_event_keys if key.condition_id == condition_id)

    def arm_execution_contract(self, condition_id: str) -> Mapping[str, Any]:
        """Material that a child process must receive unchanged except mode."""

        if condition_id not in RN_CONDITIONS:
            raise ResolutionError(f"unknown RN condition: {condition_id}")
        treatment = self.spec.condition_treatments[condition_id]
        return MappingProxyType(
            {
                "condition_id": condition_id,
                "community_mode": treatment["community_mode"],
                "news_treatment": treatment["news_treatment"],
                "source_study_spec_sha256": self.source_study_spec_sha256,
                "cohort_registry_sha256": self.cohort.canonical_sha256,
                "persona_snapshot_manifest_sha256": self.spec.persona_snapshot_manifest_sha256,
                "persona_depth_manifest_sha256": self.spec.persona_depth_manifest_sha256,
                "persona_renderer_sha256": self.spec.persona_renderer_sha256,
                "prompt_bundle_sha256": self.spec.prompt_bundle_sha256,
                "belief_limits": dict(self.spec.belief_limits),
                "calendar_event_registry_sha256": self.calendar.canonical_sha256,
                "agent_ids_sha256": canonical_sha256(list(self.agent_ids)),
                "decision_turn_map_sha256": self.expected_key_set_hashes["decision_events"],
                "logical_agent_event_keys_sha256": self.expected_key_set_hashes["logical_agent_events"],
                "burn_in_date_ids_sha256": canonical_sha256(list(self.burn_in_date_ids)),
                "evaluation_date_ids_sha256": canonical_sha256(list(self.evaluation_date_ids)),
                "trade_policy": self.spec.trade_policy.to_dict(),
                "call_policy": self.spec.call_policy.to_dict(),
                "study_seed": self.spec.study_seed,
                "seed_namespace": self.spec.seed_namespace,
                "real_news_bundle_manifest_sha256": self.spec.real_news_bundle_manifest_sha256,
                "known_injection_registry_sha256": self.spec.known_injection_registry_sha256,
                "article_version_leakage_review_manifest_sha256": (
                    self.spec.article_version_leakage_review_manifest_sha256
                ),
                "news_exposure_policy_sha256": self.spec.news_exposure_policy_sha256,
                "news_exposure_policy": _plain_json(self.spec.news_exposure_policy),
                "community_timing_policy": _plain_json(self.spec.community_timing_policy),
                "community_timing_policy_sha256": self.spec.community_timing_policy_sha256,
                "stage_input_registry_file_sha256": self.spec.stage_input_registry_file_sha256,
                "stage_input_registry_canonical_sha256": self.spec.stage_input_registry_canonical_sha256,
                "memory_policy": _plain_json(self.spec.memory_policy),
                "context_window_policy": _plain_json(self.spec.context_window_policy),
            }
        )

    def assert_pair_integrity(self) -> None:
        try:
            assert_only_community_mode_diff(
                self.arm_execution_contract(RN_COMM_OFF),
                self.arm_execution_contract(RN_COMM_ON),
            )
        except ArmPairValidationError as exc:
            raise ResolutionError(str(exc)) from exc

    def to_evaluator_contract(
        self,
        *,
        price_registry_path: Path | str,
        input_root: Path | str,
    ) -> dict[str, Any]:
        """Build a sealed, validator-facing envelope without changing ``to_dict``.

        The authoritative resolver manifest deliberately records a price-field
        *name* (for example ``actual_open``), not a mutable numeric price.  A
        final-fill evaluator needs the latter, so it consumes a separate,
        calendar-bound ``event_price_registry`` through this narrow adapter.

        ``manifest_hash`` and ``resolved_manifest_sha256`` are both the SHA-256
        of the returned envelope *excluding those two fields*.  This makes the
        value usable in ledgers without an impossible self-referential file
        hash.  The authoritative :meth:`to_dict` output remains unchanged.
        """

        safe_path = assert_safe_input_file(price_registry_path, allowed_root=input_root)
        price_registry = _load_event_price_registry(safe_path)
        _validate_price_registry_against_manifest(self, price_registry)

        pair_id = f"{self.spec.study_id}__rn_comm_pair"
        shared_inputs = _evaluator_pair_invariant_inputs(self, price_registry)
        pair_invariant_hash = canonical_sha256(shared_inputs)
        conditions: dict[str, dict[str, Any]] = {}
        for condition_id in RN_CONDITIONS:
            treatment = self.spec.condition_treatments[condition_id]
            conditions[condition_id] = {
                "condition_id": condition_id,
                "condition_pair_id": pair_id,
                "community_mode": treatment["community_mode"],
                "news_treatment": treatment["news_treatment"],
                "pair_invariant_hash": pair_invariant_hash,
                "input_hashes": dict(shared_inputs),
            }

        calendar_events = [
            {
                "event_id": price.decision_event_id,
                "date": price.date,
                "session": price.subturn,
                "execution_price_field": price.execution_price_field,
                "execution_price": price.execution_price,
            }
            for price in price_registry.prices
        ]
        initial_cash_by_agent = {
            member.agent_id: member.initial_cash for member in self.cohort.members
        }
        content = {
            "artifact_type": "rn_ab_evaluator_contract",
            # Version 2 adds the identity-keyed initial-capital map required
            # by the preregistered agent-first RQ2 estimand.  Version 1 must
            # not be accepted as if it carried those denominators.
            "contract_version": "2",
            "manifest_hash_algorithm": _EVALUATOR_HASH_ALGORITHM,
            "authoritative_resolved_manifest_sha256": self.sha256,
            "source_study_spec_sha256": self.source_study_spec_sha256,
            "condition_pair_id": pair_id,
            "pair_invariant_hash": pair_invariant_hash,
            "conditions": conditions,
            "cohort": {
                "agent_ids": list(self.agent_ids),
                "initial_cash_by_agent": initial_cash_by_agent,
                "canonical_sha256": self.cohort.canonical_sha256,
            },
            "event_calendar": {"events": calendar_events},
            "burn_in_dates": list(self.burn_in_date_ids),
            "evaluation_dates": list(self.evaluation_date_ids),
            "price_registry": {
                "version": price_registry.version,
                "stock_code": price_registry.stock_code,
                "calendar_event_registry_sha256": price_registry.calendar_event_registry_sha256,
                "canonical_sha256": price_registry.canonical_sha256,
                "file_sha256": price_registry.file_sha256,
            },
        }
        content_hash = canonical_sha256(content)
        envelope = dict(content)
        envelope["manifest_hash"] = content_hash
        envelope["resolved_manifest_sha256"] = content_hash
        verify_evaluator_contract_hash(
            envelope,
            expected_authoritative_manifest_sha256=self.sha256,
            expected_price_registry_sha256=price_registry.canonical_sha256,
        )
        return envelope

    def to_dict(self) -> dict[str, Any]:
        key_domain = [key.to_dict() for key in self.logical_agent_event_keys]
        return {
            "artifact_type": "resolved_study_manifest",
            "source_study_spec_sha256": self.source_study_spec_sha256,
            "study_id": self.spec.study_id,
            "design_version": self.spec.design_version,
            "baseline_commit": self.spec.baseline_commit,
            "conditions": list(self.conditions),
            "condition_treatments": _plain_json(self.spec.condition_treatments),
            "treatment_diff_allowlist": list(self.spec.treatment_diff_allowlist),
            "agent_count": len(self.cohort.members),
            "agent_ids": list(self.agent_ids),
            "agent_ids_sha256": canonical_sha256(list(self.agent_ids)),
            "cohort_registry": {
                "canonical_sha256": self.cohort.canonical_sha256,
                "file_sha256": self.cohort.file_sha256,
            },
            "persona_snapshot": {
                "manifest_sha256": self.spec.persona_snapshot_manifest_sha256,
                "depth_manifest_sha256": self.spec.persona_depth_manifest_sha256,
                "renderer_sha256": self.spec.persona_renderer_sha256,
            },
            "prompt_bundle_sha256": self.spec.prompt_bundle_sha256,
            "belief_limits": dict(self.spec.belief_limits),
            "calendar_event_registry": {
                "canonical_sha256": self.calendar.canonical_sha256,
                "file_sha256": self.calendar.file_sha256,
            },
            "depth_distribution": {
                str(depth): count
                for depth, count in sorted(Counter(member.news_depth for member in self.cohort.members).items())
            },
            "initial_cash_distribution": {
                str(cash): count
                for cash, count in sorted(Counter(member.initial_cash for member in self.cohort.members).items())
            },
            # The evaluator must normalise each agent before averaging.  A
            # distribution alone cannot prove which denominator belongs to
            # which fill row, so the authoritative resolver artifact seals
            # this complete identity-keyed map as well.
            "initial_cash_by_agent": {
                member.agent_id: member.initial_cash for member in self.cohort.members
            },
            "trading_dates": list(self.trading_dates),
            "burn_in_date_ids": list(self.burn_in_date_ids),
            "evaluation_date_ids": list(self.evaluation_date_ids),
            "decision_events": [event.to_dict() for event in self.decision_events],
            "community_phase_opportunities": [key.to_dict() for key in self.community_phase_keys],
            "planned_news_slot_keys": [key.to_dict() for key in self.planned_news_slot_keys],
            "logical_agent_event_keys": key_domain,
            "stage_key_domains": {
                "stb": "logical_agent_event_keys",
                "ltb_transition": "logical_agent_event_keys",
                "analysis": "logical_agent_event_keys",
                "decision": "logical_agent_event_keys",
                "fill": "logical_agent_event_keys",
            },
            "resolved_counts": dict(self.resolved_counts),
            "expected_key_set_hashes": dict(self.expected_key_set_hashes),
            "trade_policy": self.spec.trade_policy.to_dict(),
            "call_policy": self.spec.call_policy.to_dict(),
            "community_policy": _plain_json(self.spec.community_policy),
            "community_timing_policy": _plain_json(self.spec.community_timing_policy),
            "community_timing_policy_sha256": self.spec.community_timing_policy_sha256,
            "context_window_policy": _plain_json(self.spec.context_window_policy),
            "memory_policy": _plain_json(self.spec.memory_policy),
            "study_seed": self.spec.study_seed,
            "seed_namespace": self.spec.seed_namespace,
            "fake_news_mode": "off",
            "real_news_bundle_manifest_sha256": self.spec.real_news_bundle_manifest_sha256,
            "known_injection_registry_sha256": self.spec.known_injection_registry_sha256,
            "article_version_leakage_review_manifest_sha256": (
                self.spec.article_version_leakage_review_manifest_sha256
            ),
            "news_exposure_policy_sha256": self.spec.news_exposure_policy_sha256,
            "news_exposure_policy": _plain_json(self.spec.news_exposure_policy),
            "stage_input_registry": {
                "file_sha256": self.spec.stage_input_registry_file_sha256,
                "canonical_sha256": self.spec.stage_input_registry_canonical_sha256,
            },
        }


def load_study_spec(path: Path | str, *, input_root: Path | str) -> StudySpec:
    safe_path = assert_safe_input_file(path, allowed_root=input_root)
    try:
        with safe_path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ResolutionError(f"invalid study_spec JSON: {safe_path}") from exc
    return StudySpec.from_dict(_mapping(payload, "study_spec"))


def load_event_price_registry(
    path: Path | str,
    *,
    input_root: Path | str,
) -> EventPriceRegistry:
    """Load a regular, non-symlink evaluator price registry under ``input_root``.

    This validates the registry's standalone schema.  Binding it to a specific
    resolved calendar, including exact event order and AM/PM coverage, is done
    by :meth:`ResolvedStudyManifest.to_evaluator_contract`.
    """

    safe_path = assert_safe_input_file(path, allowed_root=input_root)
    return _load_event_price_registry(safe_path)


def verify_evaluator_contract_hash(
    contract: Mapping[str, Any],
    *,
    expected_authoritative_manifest_sha256: str | None = None,
    expected_price_registry_sha256: str | None = None,
) -> None:
    """Fail closed if an evaluator envelope no longer matches its content hash.

    A checksum alone is not provenance: callers loading a run-local envelope
    should also pass the already-sealed authoritative manifest hash and price
    registry hash they expect.  This function deliberately accepts neither a
    loose alias schema nor additional fields, so a new contract version must
    update its explicit verification rules.
    """

    mapping = _exact_keys(contract, "rn_ab_evaluator_contract", _EVALUATOR_CONTRACT_FIELDS)
    if _nonempty_str(mapping["artifact_type"], "rn_ab_evaluator_contract.artifact_type") != (
        "rn_ab_evaluator_contract"
    ):
        raise ResolutionError("evaluator contract artifact_type is invalid")
    if _nonempty_str(mapping["contract_version"], "rn_ab_evaluator_contract.contract_version") != "2":
        raise ResolutionError("unsupported evaluator contract version")
    if _nonempty_str(
        mapping["manifest_hash_algorithm"], "rn_ab_evaluator_contract.manifest_hash_algorithm"
    ) != _EVALUATOR_HASH_ALGORITHM:
        raise ResolutionError("evaluator contract hash algorithm is invalid")

    manifest_hash = _sha256(mapping["manifest_hash"], "rn_ab_evaluator_contract.manifest_hash")
    alias_hash = _sha256(
        mapping["resolved_manifest_sha256"], "rn_ab_evaluator_contract.resolved_manifest_sha256"
    )
    if manifest_hash != alias_hash:
        raise ResolutionError("evaluator contract manifest hash aliases disagree")
    content = {
        key: value
        for key, value in mapping.items()
        if key not in _EVALUATOR_HASH_EXCLUDED_FIELDS
    }
    actual_hash = canonical_sha256(content)
    if manifest_hash != actual_hash:
        raise ResolutionError(
            "evaluator contract content hash mismatch: "
            f"expected={manifest_hash} actual={actual_hash}"
        )

    authoritative_hash = _sha256(
        mapping["authoritative_resolved_manifest_sha256"],
        "rn_ab_evaluator_contract.authoritative_resolved_manifest_sha256",
    )
    price_registry = _mapping(mapping["price_registry"], "rn_ab_evaluator_contract.price_registry")
    cohort = _exact_keys(
        mapping["cohort"],
        "rn_ab_evaluator_contract.cohort",
        frozenset({"agent_ids", "initial_cash_by_agent", "canonical_sha256"}),
    )
    raw_agent_ids = cohort["agent_ids"]
    if isinstance(raw_agent_ids, (str, bytes)) or not isinstance(raw_agent_ids, Sequence):
        raise ResolutionError("rn_ab_evaluator_contract.cohort.agent_ids must be an ordered array")
    agent_ids = tuple(
        _nonempty_str(value, f"rn_ab_evaluator_contract.cohort.agent_ids[{index}]")
        for index, value in enumerate(raw_agent_ids)
    )
    if not agent_ids or len(agent_ids) != len(set(agent_ids)):
        raise ResolutionError(
            "rn_ab_evaluator_contract.cohort.agent_ids must contain unique agent IDs"
        )
    raw_initial_cash = _mapping(
        cohort["initial_cash_by_agent"],
        "rn_ab_evaluator_contract.cohort.initial_cash_by_agent",
    )
    if set(raw_initial_cash) != set(agent_ids):
        raise ResolutionError(
            "evaluator initial_cash_by_agent must exactly cover cohort.agent_ids; "
            f"missing={sorted(set(agent_ids) - set(raw_initial_cash))}, "
            f"extra={sorted(set(raw_initial_cash) - set(agent_ids))}"
        )
    for agent_id in agent_ids:
        _positive_int(
            raw_initial_cash[agent_id],
            f"rn_ab_evaluator_contract.cohort.initial_cash_by_agent[{agent_id!r}]",
        )
    price_hash = _sha256(
        price_registry.get("canonical_sha256"),
        "rn_ab_evaluator_contract.price_registry.canonical_sha256",
    )
    if expected_authoritative_manifest_sha256 is not None:
        expected = _sha256(
            expected_authoritative_manifest_sha256,
            "expected_authoritative_manifest_sha256",
        )
        if authoritative_hash != expected:
            raise ResolutionError(
                "evaluator contract is bound to a different authoritative manifest: "
                f"expected={expected} actual={authoritative_hash}"
            )
    if expected_price_registry_sha256 is not None:
        expected = _sha256(expected_price_registry_sha256, "expected_price_registry_sha256")
        if price_hash != expected:
            raise ResolutionError(
                "evaluator contract is bound to a different price registry: "
                f"expected={expected} actual={price_hash}"
            )


def resolve_study(
    spec: StudySpec,
    *,
    cohort_registry_path: Path | str,
    calendar_event_registry_path: Path | str,
    stage_input_registry_path: Path | str,
    input_root: Path | str,
) -> ResolvedStudyManifest:
    """Resolve frozen registries into one immutable RN run contract.

    The source files must be regular, non-symlink files inside ``input_root``.
    Their *canonical semantic hashes* must match the hashes pinned by the
    authored spec.  Formatting-only changes do not alter identity; every row
    value and order does.
    """

    if not isinstance(spec, StudySpec):
        raise TypeError("resolve_study requires a validated StudySpec")
    cohort_path = assert_safe_input_file(cohort_registry_path, allowed_root=input_root)
    calendar_path = assert_safe_input_file(calendar_event_registry_path, allowed_root=input_root)
    stage_input_path = assert_safe_input_file(stage_input_registry_path, allowed_root=input_root)
    cohort = _load_cohort_registry(cohort_path)
    calendar = _load_calendar_registry(calendar_path)

    if cohort.canonical_sha256 != spec.cohort_registry_sha256:
        raise ResolutionError(
            "cohort registry hash differs from StudySpec: "
            f"expected={spec.cohort_registry_sha256} actual={cohort.canonical_sha256}"
        )
    if calendar.canonical_sha256 != spec.calendar_event_registry_sha256:
        raise ResolutionError(
            "calendar-event registry hash differs from StudySpec: "
            f"expected={spec.calendar_event_registry_sha256} actual={calendar.canonical_sha256}"
        )
    stage_inputs = _load_stage_input_registry(
        stage_input_path,
        spec=spec,
        calendar=calendar,
        input_root=input_root,
    )
    _validate_stage_inputs_against_calendar(stage_inputs, calendar)
    _validate_cohort_against_spec(cohort, spec)

    trading_dates = calendar.trading_dates
    unknown_burn_in = sorted(set(spec.burn_in_date_ids) - set(trading_dates))
    if unknown_burn_in:
        raise ResolutionError(f"burn-in dates are absent from calendar registry: {unknown_burn_in}")
    evaluation_dates = tuple(day for day in trading_dates if day not in set(spec.burn_in_date_ids))
    if not evaluation_dates:
        raise ResolutionError("burn-in cannot exclude every approved trading date")
    if not calendar.decision_events:
        raise ResolutionError("calendar registry contains no decision-enabled events")

    logical_keys = _derive_agent_event_keys(cohort, calendar)
    phase_keys = _derive_community_phase_keys(calendar)
    news_slot_keys = _derive_planned_news_slot_keys(
        calendar,
        target_real_news_per_event=spec.news_exposure_policy["target_real_news_per_event"],
    )
    key_hashes = _derive_key_set_hashes(calendar, logical_keys, phase_keys, news_slot_keys)
    counts = _derive_counts(
        cohort,
        calendar,
        spec.burn_in_date_ids,
        evaluation_dates,
        phase_keys,
        news_slot_keys,
        target_real_news_per_event=spec.news_exposure_policy["target_real_news_per_event"],
    )

    resolved = ResolvedStudyManifest(
        spec=spec,
        cohort=cohort,
        calendar=calendar,
        burn_in_date_ids=spec.burn_in_date_ids,
        evaluation_date_ids=evaluation_dates,
        logical_agent_event_keys=logical_keys,
        community_phase_keys=phase_keys,
        planned_news_slot_keys=news_slot_keys,
        expected_key_set_hashes=MappingProxyType(key_hashes),
        resolved_counts=MappingProxyType(counts),
    )
    resolved.assert_pair_integrity()
    return resolved


def write_resolved_manifest(
    resolved: ResolvedStudyManifest,
    *,
    run_dir: Path | str,
    output_root: Path | str,
    allow_exact_resume: bool = False,
) -> Path:
    """Seal a canonical manifest into a safe new/exact-resume run directory."""

    if not isinstance(resolved, ResolvedStudyManifest):
        raise TypeError("write_resolved_manifest requires ResolvedStudyManifest")
    directory = ensure_safe_run_directory(
        run_dir,
        output_root=output_root,
        allow_existing=allow_exact_resume,
    )
    destination = directory / "resolved_study_manifest.json"
    _assert_no_symlink_components(destination, trusted_root=directory)
    payload = canonical_json_bytes(resolved.to_dict())
    if destination.exists():
        try:
            mode = destination.lstat().st_mode
        except FileNotFoundError as exc:  # pragma: no cover - defensive TOCTOU guard
            raise PathSafetyError(f"manifest disappeared during resume check: {destination}") from exc
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise PathSafetyError(f"resolved manifest path is not a regular file: {destination}")
        existing = destination.read_bytes()
        if not allow_exact_resume or existing != payload:
            raise PathSafetyError("resolved manifest exists but is not an exact resume match")
        return destination
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        file_descriptor = os.open(destination, flags, 0o600)
    except FileExistsError as exc:  # pragma: no cover - race-safe failure path
        raise PathSafetyError(f"resolved manifest already exists: {destination}") from exc
    try:
        with os.fdopen(file_descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            destination.unlink(missing_ok=True)
        finally:
            raise
    return destination


def _load_json(path: Path, label: str) -> tuple[Mapping[str, Any], str]:
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ResolutionError(f"invalid {label} JSON: {path}") from exc
    return _mapping(payload, label), _file_sha256(path)


def _load_cohort_registry(path: Path) -> CohortRegistry:
    payload, file_hash = _load_json(path, "cohort_registry")
    mapping = _exact_keys(payload, "cohort_registry", _COHORT_REGISTRY_FIELDS)
    if _nonempty_str(mapping["artifact_type"], "cohort_registry.artifact_type") != "cohort_registry":
        raise ResolutionError("cohort_registry.artifact_type must be 'cohort_registry'")
    agents = mapping["agents"]
    if not isinstance(agents, Sequence) or isinstance(agents, (str, bytes)) or not agents:
        raise ResolutionError("cohort_registry.agents must be a non-empty ordered array")
    members: list[CohortMember] = []
    agent_ids: set[str] = set()
    for index, raw_member in enumerate(agents, start=1):
        member = _exact_keys(raw_member, f"cohort_registry.agents[{index - 1}]", _COHORT_MEMBER_FIELDS)
        ordinal = _positive_int(member["ordinal"], f"cohort_registry.agents[{index - 1}].ordinal")
        if ordinal != index:
            raise ResolutionError("cohort registry ordinals must be continuous and start at 1")
        agent_id = _nonempty_str(member["agent_id"], f"cohort_registry.agents[{index - 1}].agent_id")
        if not _AGENT_ID_RE.fullmatch(agent_id):
            raise ResolutionError(f"invalid cohort agent_id: {agent_id!r}")
        if agent_id in agent_ids:
            raise ResolutionError(f"duplicate cohort agent_id: {agent_id}")
        agent_ids.add(agent_id)
        depth = _positive_int(member["news_depth"], f"cohort_registry.agents[{index - 1}].news_depth", allow_zero=True)
        if depth not in {0, 1, 2}:
            raise ResolutionError(f"cohort agent {agent_id} has invalid news_depth={depth}")
        cash = _positive_int(member["initial_cash"], f"cohort_registry.agents[{index - 1}].initial_cash")
        members.append(
            CohortMember(
                ordinal=ordinal,
                agent_id=agent_id,
                news_depth=depth,
                initial_cash=cash,
                persona_sha256=_sha256(member["persona_sha256"], f"cohort_registry.agents[{index - 1}].persona_sha256"),
                fixed_slot_sha256=_sha256(member["fixed_slot_sha256"], f"cohort_registry.agents[{index - 1}].fixed_slot_sha256"),
            )
        )
    canonical_payload = {
        "artifact_type": "cohort_registry",
        "version": _nonempty_str(mapping["version"], "cohort_registry.version"),
        "agents": [member.to_dict() for member in members],
    }
    return CohortRegistry(
        version=canonical_payload["version"],
        members=tuple(members),
        canonical_sha256=canonical_sha256(canonical_payload),
        file_sha256=file_hash,
    )


def _load_calendar_registry(path: Path) -> CalendarRegistry:
    payload, file_hash = _load_json(path, "calendar_event_registry")
    mapping = _exact_keys(payload, "calendar_event_registry", _CALENDAR_REGISTRY_FIELDS)
    if _nonempty_str(mapping["artifact_type"], "calendar_event_registry.artifact_type") != "calendar_event_registry":
        raise ResolutionError("calendar_event_registry.artifact_type must be 'calendar_event_registry'")
    raw_dates = mapping["dates"]
    if not isinstance(raw_dates, Sequence) or isinstance(raw_dates, (str, bytes)) or not raw_dates:
        raise ResolutionError("calendar_event_registry.dates must be a non-empty ordered array")

    dates: list[str] = []
    all_events: list[DecisionEvent] = []
    phases: list[CommunityPhase] = []
    event_ids: set[str] = set()
    phase_ids: set[str] = set()
    previous_date: str | None = None
    for date_index, raw_date in enumerate(raw_dates):
        date_row = _exact_keys(raw_date, f"calendar_event_registry.dates[{date_index}]", _CALENDAR_DATE_FIELDS)
        day = _date_id(date_row["date"], f"calendar_event_registry.dates[{date_index}].date")
        if previous_date is not None and day <= previous_date:
            raise ResolutionError("calendar registry dates must be strictly increasing and unique")
        previous_date = day
        dates.append(day)
        if _nonempty_str(date_row["timezone"], f"calendar_event_registry.dates[{date_index}].timezone") != "Asia/Seoul":
            raise ResolutionError("calendar registry timezone must be Asia/Seoul")
        raw_events = date_row["decision_events"]
        if not isinstance(raw_events, Sequence) or isinstance(raw_events, (str, bytes)) or not raw_events:
            raise ResolutionError(f"calendar date {day} must contain an ordered decision_events array")
        day_events: list[DecisionEvent] = []
        for event_index, raw_event in enumerate(raw_events, start=1):
            event = _parse_event(raw_event, day=day, event_index=event_index)
            if event.decision_event_id in event_ids:
                raise ResolutionError(f"duplicate decision_event_id: {event.decision_event_id}")
            event_ids.add(event.decision_event_id)
            day_events.append(event)
            all_events.append(event)
        if not any(event.decision_enabled for event in day_events):
            raise ResolutionError(f"calendar date {day} has no decision-enabled event")
        raw_phases = date_row["post_decision_phases"]
        if not isinstance(raw_phases, Sequence) or isinstance(raw_phases, (str, bytes)):
            raise ResolutionError(f"calendar date {day} post_decision_phases must be an array")
        day_event_ids = {event.decision_event_id for event in day_events}
        for phase_index, raw_phase in enumerate(raw_phases):
            phase = _parse_community_phase(raw_phase, day=day, phase_index=phase_index)
            if phase.phase_id in phase_ids:
                raise ResolutionError(f"duplicate community phase_id: {phase.phase_id}")
            if phase.after_event_id not in day_event_ids:
                raise ResolutionError(
                    f"community phase {phase.phase_id} references an event outside its date: {phase.after_event_id}"
                )
            source_event = next(event for event in day_events if event.decision_event_id == phase.after_event_id)
            if not source_event.decision_enabled:
                raise ResolutionError(f"community phase {phase.phase_id} must follow a decision-enabled event")
            phase_ids.add(phase.phase_id)
            phases.append(phase)

    decision_events: list[DecisionEvent] = []
    for global_ordinal, event in enumerate((item for item in all_events if item.decision_enabled), start=1):
        decision_events.append(
            DecisionEvent(
                date=event.date,
                event_ordinal_in_date=event.event_ordinal_in_date,
                decision_event_id=event.decision_event_id,
                subturn=event.subturn,
                decision_timestamp=event.decision_timestamp,
                news_window=event.news_window,
                market_feature_as_of=event.market_feature_as_of,
                execution_price_field=event.execution_price_field,
                consume_scheduled_community=event.consume_scheduled_community,
                decision_enabled=True,
                global_ordinal=global_ordinal,
            )
        )
    event_by_id = {event.decision_event_id: event for event in decision_events}
    all_events_with_turns = tuple(
        event_by_id.get(event.decision_event_id, event) for event in all_events
    )
    canonical_payload = {
        "artifact_type": "calendar_event_registry",
        "version": _nonempty_str(mapping["version"], "calendar_event_registry.version"),
        "dates": _canonical_calendar_dates(tuple(dates), all_events_with_turns, tuple(phases)),
    }
    return CalendarRegistry(
        version=canonical_payload["version"],
        trading_dates=tuple(dates),
        all_events=all_events_with_turns,
        decision_events=tuple(decision_events),
        community_phases=tuple(phases),
        canonical_sha256=canonical_sha256(canonical_payload),
        file_sha256=file_hash,
    )


def _load_stage_input_registry(
    path: Path,
    *,
    spec: StudySpec,
    calendar: CalendarRegistry,
    input_root: Path | str,
) -> SealedStageInputRegistry:
    """Load the authored timestamp/market registry under both file and semantic pins."""

    try:
        registry = SealedStageInputRegistry.load(
            path,
            expected_file_sha256=spec.stage_input_registry_file_sha256,
            expected_calendar_event_registry_sha256=calendar.canonical_sha256,
            allowed_root=input_root,
        )
    except StageInputRegistryError as exc:
        raise ResolutionError(f"sealed stage-input registry is invalid: {exc}") from exc
    if registry.canonical_sha256 != spec.stage_input_registry_canonical_sha256:
        raise ResolutionError(
            "stage-input registry canonical hash differs from StudySpec: "
            f"expected={spec.stage_input_registry_canonical_sha256} actual={registry.canonical_sha256}"
        )
    return registry


def _validate_stage_inputs_against_calendar(
    registry: SealedStageInputRegistry,
    calendar: CalendarRegistry,
) -> None:
    """Require event-level cutoff/as-of facts to equal the resolved calendar.

    Calendar parsing owns the temporal order and timestamp syntax.  The stage
    registry owns the immutable market snapshot.  Joining them here prevents a
    separately hash-pinned but semantically stale/future registry from changing
    the STB or decision boundary.
    """

    expected_events = calendar.decision_events
    if len(registry.events) != len(expected_events):
        raise ResolutionError("stage-input registry event count differs from resolved calendar")
    for event, stage_input in zip(expected_events, registry.events):
        if (
            stage_input.event_id != event.decision_event_id
            or stage_input.date != event.date
            or stage_input.subturn.upper() != event.subturn
        ):
            raise ResolutionError(
                "stage-input registry event identity/order differs from resolved calendar: "
                f"expected={event.decision_event_id} actual={stage_input.event_id}"
            )
        expected_cutoff = str(event.news_window["end_inclusive"])
        if stage_input.news_cutoff_timestamp != expected_cutoff:
            raise ResolutionError(
                "stage-input registry news cutoff differs from resolved calendar: "
                f"event_id={event.decision_event_id} expected={expected_cutoff} "
                f"actual={stage_input.news_cutoff_timestamp}"
            )
        if stage_input.market_feature_as_of != event.market_feature_as_of:
            raise ResolutionError(
                "stage-input registry market feature as-of differs from resolved calendar: "
                f"event_id={event.decision_event_id} expected={event.market_feature_as_of} "
                f"actual={stage_input.market_feature_as_of}"
            )


def _load_event_price_registry(path: Path) -> EventPriceRegistry:
    payload, file_hash = _load_json(path, "event_price_registry")
    mapping = _exact_keys(payload, "event_price_registry", _EVENT_PRICE_REGISTRY_FIELDS)
    if _nonempty_str(mapping["artifact_type"], "event_price_registry.artifact_type") != "event_price_registry":
        raise ResolutionError("event_price_registry.artifact_type must be 'event_price_registry'")
    stock_code = _nonempty_str(mapping["stock_code"], "event_price_registry.stock_code")
    if stock_code != "005930":
        raise ResolutionError("event_price_registry.stock_code must be the approved Samsung code '005930'")
    raw_events = mapping["events"]
    if not isinstance(raw_events, Sequence) or isinstance(raw_events, (str, bytes)) or not raw_events:
        raise ResolutionError("event_price_registry.events must be a non-empty ordered array")

    prices: list[EventPrice] = []
    seen_ids: set[str] = set()
    for index, raw_price in enumerate(raw_events):
        field = f"event_price_registry.events[{index}]"
        row = _exact_keys(raw_price, field, _EVENT_PRICE_FIELDS)
        event_id = _nonempty_str(row["decision_event_id"], f"{field}.decision_event_id")
        if not _EVENT_ID_RE.fullmatch(event_id):
            raise ResolutionError(f"{field}.decision_event_id must be a safe date/event ID")
        if event_id in seen_ids:
            raise ResolutionError(f"event_price_registry has duplicate decision_event_id={event_id!r}")
        seen_ids.add(event_id)
        day = _date_id(row["date"], f"{field}.date")
        if not event_id.startswith(day + "/"):
            raise ResolutionError(f"{field}.decision_event_id must be namespaced by its date")
        subturn = _nonempty_str(row["subturn"], f"{field}.subturn")
        if not _SUBTURN_RE.fullmatch(subturn):
            raise ResolutionError(f"{field}.subturn must be an uppercase registry label")
        prices.append(
            EventPrice(
                decision_event_id=event_id,
                date=day,
                subturn=subturn,
                execution_price_field=_nonempty_str(
                    row["execution_price_field"], f"{field}.execution_price_field"
                ),
                execution_price=_positive_price(row["execution_price"], f"{field}.execution_price"),
            )
        )

    canonical_payload = {
        "artifact_type": "event_price_registry",
        "version": _nonempty_str(mapping["version"], "event_price_registry.version"),
        "stock_code": stock_code,
        "calendar_event_registry_sha256": _sha256(
            mapping["calendar_event_registry_sha256"],
            "event_price_registry.calendar_event_registry_sha256",
        ),
        "events": [price.to_dict() for price in prices],
    }
    return EventPriceRegistry(
        version=canonical_payload["version"],
        stock_code=stock_code,
        calendar_event_registry_sha256=canonical_payload["calendar_event_registry_sha256"],
        prices=tuple(prices),
        canonical_sha256=canonical_sha256(canonical_payload),
        file_sha256=file_hash,
    )


def _parse_kst_timestamp(
    value: Any,
    field: str,
    *,
    expected_date: str | None = None,
) -> tuple[str, datetime]:
    """Accept one unambiguous KST timestamp in the calendar's canonical form."""

    text = _nonempty_str(value, field)
    if not _KST_TIMESTAMP_RE.fullmatch(text):
        raise ResolutionError(f"{field} must be ISO-8601 YYYY-MM-DDTHH:MM:SS+09:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:  # Regex is intentionally not the only date validation.
        raise ResolutionError(f"{field} is not a valid ISO-8601 timestamp") from exc
    if parsed.utcoffset() != timedelta(hours=9):  # pragma: no cover - regex guarantees this
        raise ResolutionError(f"{field} must carry the Asia/Seoul +09:00 offset")
    if expected_date is not None and parsed.date().isoformat() != expected_date:
        raise ResolutionError(f"{field} date must equal decision event date {expected_date}")
    return parsed.isoformat(), parsed


def _parse_event(raw_event: Any, *, day: str, event_index: int) -> DecisionEvent:
    field = f"calendar event {day}[{event_index - 1}]"
    mapping = _exact_keys(raw_event, field, _DECISION_EVENT_FIELDS)
    ordinal = _positive_int(mapping["event_ordinal_in_date"], f"{field}.event_ordinal_in_date")
    if ordinal != event_index:
        raise ResolutionError(f"{field}.event_ordinal_in_date must be {event_index}")
    event_id = _nonempty_str(mapping["decision_event_id"], f"{field}.decision_event_id")
    if not _EVENT_ID_RE.fullmatch(event_id) or not event_id.startswith(day + "/"):
        raise ResolutionError(f"{field}.decision_event_id must be a safe ID for date {day}")
    subturn = _nonempty_str(mapping["subturn"], f"{field}.subturn")
    if not _SUBTURN_RE.fullmatch(subturn):
        raise ResolutionError(f"{field}.subturn must be an uppercase registry label")
    decision_timestamp, decision_at = _parse_kst_timestamp(
        mapping["decision_timestamp"],
        f"{field}.decision_timestamp",
        expected_date=day,
    )
    news_window = _exact_keys(
        mapping["news_window"],
        f"{field}.news_window",
        frozenset({"start_exclusive", "end_inclusive"}),
    )
    start_exclusive, start_at = _parse_kst_timestamp(
        news_window["start_exclusive"],
        f"{field}.news_window.start_exclusive",
    )
    end_inclusive, end_at = _parse_kst_timestamp(
        news_window["end_inclusive"],
        f"{field}.news_window.end_inclusive",
        expected_date=day,
    )
    market_feature_as_of, market_at = _parse_kst_timestamp(
        mapping["market_feature_as_of"],
        f"{field}.market_feature_as_of",
        expected_date=day,
    )
    if start_at >= end_at:
        raise ResolutionError(f"{field}.news_window start_exclusive must be earlier than end_inclusive")
    if end_at > decision_at:
        raise ResolutionError(f"{field}.news_window end_inclusive may not be later than decision_timestamp")
    if end_at > market_at:
        raise ResolutionError(f"{field}.news_window end_inclusive may not be later than market_feature_as_of")
    if market_at > decision_at:
        raise ResolutionError(f"{field}.market_feature_as_of may not be later than decision_timestamp")
    return DecisionEvent(
        date=day,
        event_ordinal_in_date=ordinal,
        decision_event_id=event_id,
        subturn=subturn,
        decision_timestamp=decision_timestamp,
        news_window=_freeze_json(
            {
                "start_exclusive": start_exclusive,
                "end_inclusive": end_inclusive,
            }
        ),
        market_feature_as_of=market_feature_as_of,
        execution_price_field=_nonempty_str(
            mapping["execution_price_field"], f"{field}.execution_price_field"
        ),
        consume_scheduled_community=_strict_bool(
            mapping["consume_scheduled_community"], f"{field}.consume_scheduled_community"
        ),
        decision_enabled=_strict_bool(mapping["decision_enabled"], f"{field}.decision_enabled"),
    )


def _parse_community_phase(raw_phase: Any, *, day: str, phase_index: int) -> CommunityPhase:
    field = f"calendar community phase {day}[{phase_index}]"
    mapping = _exact_keys(raw_phase, field, _COMMUNITY_PHASE_FIELDS)
    phase_id = _nonempty_str(mapping["phase_id"], f"{field}.phase_id")
    if not phase_id.startswith(day + "/"):
        raise ResolutionError(f"{field}.phase_id must be namespaced by its date")
    return CommunityPhase(
        date=day,
        phase_id=phase_id,
        after_event_id=_nonempty_str(mapping["after_event_id"], f"{field}.after_event_id"),
        next_visible_event_rule=_nonempty_str(
            mapping["next_visible_event_rule"], f"{field}.next_visible_event_rule"
        ),
    )


def _canonical_calendar_dates(
    dates: tuple[str, ...],
    events: tuple[DecisionEvent, ...],
    phases: tuple[CommunityPhase, ...],
) -> list[dict[str, Any]]:
    events_by_date: dict[str, list[DecisionEvent]] = {day: [] for day in dates}
    phases_by_date: dict[str, list[CommunityPhase]] = {day: [] for day in dates}
    for event in events:
        events_by_date[event.date].append(event)
    for phase in phases:
        phases_by_date[phase.date].append(phase)
    return [
        {
            "date": day,
            "timezone": "Asia/Seoul",
            "decision_events": [
                event.to_registry_dict()
                for event in events_by_date[day]
            ],
            "post_decision_phases": [
                {
                    "phase_id": phase.phase_id,
                    "after_event_id": phase.after_event_id,
                    "next_visible_event_rule": phase.next_visible_event_rule,
                }
                for phase in phases_by_date[day]
            ],
        }
        for day in dates
    ]


def _validate_cohort_against_spec(cohort: CohortRegistry, spec: StudySpec) -> None:
    if len(cohort.members) != spec.required_agent_count:
        raise ResolutionError(
            "cohort cardinality differs from required_agent_count: "
            f"required={spec.required_agent_count} actual={len(cohort.members)}"
        )
    actual_depths = Counter(member.news_depth for member in cohort.members)
    expected_depths = dict(spec.cohort_assertions.depth_counts)
    if dict(actual_depths) != expected_depths:
        raise ResolutionError(
            f"cohort depth assertions differ: expected={expected_depths} actual={dict(actual_depths)}"
        )
    actual_cash = Counter(member.initial_cash for member in cohort.members)
    expected_cash = dict(spec.cohort_assertions.initial_cash_counts)
    if dict(actual_cash) != expected_cash:
        raise ResolutionError(
            f"cohort initial-cash assertions differ: expected={expected_cash} actual={dict(actual_cash)}"
        )


def _validate_price_registry_against_manifest(
    resolved: ResolvedStudyManifest,
    price_registry: EventPriceRegistry,
) -> None:
    """Require a numeric price row for every and only resolved AM/PM event."""

    if price_registry.stock_code != resolved.spec.trade_policy.stock_code:
        raise ResolutionError(
            "event price registry stock code differs from resolved trade policy: "
            f"expected={resolved.spec.trade_policy.stock_code} actual={price_registry.stock_code}"
        )
    if price_registry.calendar_event_registry_sha256 != resolved.calendar.canonical_sha256:
        raise ResolutionError(
            "event price registry is not bound to this calendar registry: "
            f"expected={resolved.calendar.canonical_sha256} "
            f"actual={price_registry.calendar_event_registry_sha256}"
        )

    expected_events = resolved.decision_events
    expected_ids = tuple(event.decision_event_id for event in expected_events)
    actual_ids = tuple(price.decision_event_id for price in price_registry.prices)
    if actual_ids != expected_ids:
        expected_set = set(expected_ids)
        actual_set = set(actual_ids)
        if expected_set == actual_set:
            raise ResolutionError("event price registry event order differs from the resolved calendar")
        raise ResolutionError(
            "event price registry event IDs differ from the resolved calendar: "
            f"missing={sorted(expected_set - actual_set)} extra={sorted(actual_set - expected_set)}"
        )

    events_by_date: dict[str, list[DecisionEvent]] = {day: [] for day in resolved.trading_dates}
    for event in expected_events:
        events_by_date[event.date].append(event)
    for day, events in events_by_date.items():
        sessions = tuple(event.subturn for event in events)
        if len(events) != 2 or set(sessions) != {"AM", "PM"}:
            raise ResolutionError(
                "evaluator contract requires exactly one AM and one PM decision event per date; "
                f"date={day} sessions={list(sessions)}"
            )

    expected_price_fields = {"AM": "actual_open", "PM": "actual_close"}
    for event, price in zip(expected_events, price_registry.prices):
        if price.date != event.date or price.subturn != event.subturn:
            raise ResolutionError(
                "event price registry date/subturn differs from resolved event: "
                f"event_id={event.decision_event_id} "
                f"expected=({event.date},{event.subturn}) actual=({price.date},{price.subturn})"
            )
        expected_field = expected_price_fields[event.subturn]
        if event.execution_price_field != expected_field:
            raise ResolutionError(
                "resolved calendar event does not declare the required actual AM/PM price field: "
                f"event_id={event.decision_event_id} expected={expected_field} "
                f"actual={event.execution_price_field}"
            )
        if price.execution_price_field != event.execution_price_field:
            raise ResolutionError(
                "event price registry execution_price_field differs from resolved event: "
                f"event_id={event.decision_event_id} expected={event.execution_price_field} "
                f"actual={price.execution_price_field}"
            )


def _evaluator_pair_invariant_inputs(
    resolved: ResolvedStudyManifest,
    price_registry: EventPriceRegistry,
) -> dict[str, Any]:
    """Return every evaluator-relevant input that must be identical across arms."""

    return {
        "authoritative_resolved_manifest_sha256": resolved.sha256,
        "source_study_spec_sha256": resolved.source_study_spec_sha256,
        "baseline_commit": resolved.spec.baseline_commit,
        "cohort_registry_sha256": resolved.cohort.canonical_sha256,
        "calendar_event_registry_sha256": resolved.calendar.canonical_sha256,
        "event_price_registry_sha256": price_registry.canonical_sha256,
        "agent_ids_sha256": canonical_sha256(list(resolved.agent_ids)),
        "initial_cash_by_agent_sha256": canonical_sha256(
            {
                member.agent_id: member.initial_cash
                for member in resolved.cohort.members
            }
        ),
        "decision_events_sha256": resolved.expected_key_set_hashes["decision_events"],
        "logical_agent_event_keys_sha256": resolved.expected_key_set_hashes["logical_agent_events"],
        "burn_in_date_ids_sha256": canonical_sha256(list(resolved.burn_in_date_ids)),
        "evaluation_date_ids_sha256": canonical_sha256(list(resolved.evaluation_date_ids)),
        "persona_snapshot_manifest_sha256": resolved.spec.persona_snapshot_manifest_sha256,
        "persona_depth_manifest_sha256": resolved.spec.persona_depth_manifest_sha256,
        "persona_renderer_sha256": resolved.spec.persona_renderer_sha256,
        "prompt_bundle_sha256": resolved.spec.prompt_bundle_sha256,
        "regime_policy_sha256": resolved.spec.regime_policy_sha256,
        "real_news_bundle_manifest_sha256": resolved.spec.real_news_bundle_manifest_sha256,
        "known_injection_registry_sha256": resolved.spec.known_injection_registry_sha256,
        "article_version_leakage_review_manifest_sha256": (
            resolved.spec.article_version_leakage_review_manifest_sha256
        ),
        "news_exposure_policy_sha256": resolved.spec.news_exposure_policy_sha256,
        "news_exposure_policy_sha256_recomputed": canonical_sha256(resolved.spec.news_exposure_policy),
        "community_timing_policy_sha256": resolved.spec.community_timing_policy_sha256,
        "community_timing_policy_sha256_recomputed": canonical_sha256(resolved.spec.community_timing_policy),
        "stage_input_registry_file_sha256": resolved.spec.stage_input_registry_file_sha256,
        "stage_input_registry_canonical_sha256": resolved.spec.stage_input_registry_canonical_sha256,
        "evaluation_policy_sha256": resolved.spec.evaluation_policy_sha256,
        "trade_policy_sha256": canonical_sha256(resolved.spec.trade_policy.to_dict()),
        "call_policy_sha256": canonical_sha256(resolved.spec.call_policy.to_dict()),
        "community_policy_sha256": canonical_sha256(resolved.spec.community_policy),
        "context_window_policy_sha256": canonical_sha256(resolved.spec.context_window_policy),
        "memory_policy_sha256": canonical_sha256(resolved.spec.memory_policy),
        "study_seed": resolved.spec.study_seed,
        "seed_namespace": resolved.spec.seed_namespace,
    }


def _derive_agent_event_keys(
    cohort: CohortRegistry,
    calendar: CalendarRegistry,
) -> tuple[AgentEventKey, ...]:
    keys: list[AgentEventKey] = []
    for condition in RN_CONDITIONS:
        for member in cohort.members:
            for event in calendar.decision_events:
                if event.global_ordinal is None:  # pragma: no cover - constructor invariant
                    raise ResolutionError(f"decision event has no global ordinal: {event.decision_event_id}")
                keys.append(
                    AgentEventKey(
                        condition_id=condition,
                        agent_id=member.agent_id,
                        decision_event_id=event.decision_event_id,
                        date=event.date,
                        subturn=event.subturn,
                        global_turn=event.global_ordinal,
                    )
                )
    return tuple(keys)


def _derive_community_phase_keys(calendar: CalendarRegistry) -> tuple[CommunityPhaseKey, ...]:
    return tuple(
        CommunityPhaseKey(
            condition_id=condition,
            phase_id=phase.phase_id,
            date=phase.date,
            after_event_id=phase.after_event_id,
        )
        for condition in RN_CONDITIONS
        for phase in calendar.community_phases
    )


def _derive_planned_news_slot_keys(
    calendar: CalendarRegistry,
    *,
    target_real_news_per_event: int,
) -> tuple[NewsSlotKey, ...]:
    # This is a target slot domain, not a claim that every slot was delivered.
    # Accepted shortage records are validated later against this planned domain.
    if isinstance(target_real_news_per_event, bool) or not isinstance(target_real_news_per_event, int):
        raise ResolutionError("resolved target_real_news_per_event must be an integer")
    if target_real_news_per_event < 1:
        raise ResolutionError("resolved target_real_news_per_event must be positive")
    return tuple(
        NewsSlotKey(decision_event_id=event.decision_event_id, slot_ordinal=slot)
        for event in calendar.decision_events
        for slot in range(1, target_real_news_per_event + 1)
    )


def _derive_key_set_hashes(
    calendar: CalendarRegistry,
    logical_keys: tuple[AgentEventKey, ...],
    phase_keys: tuple[CommunityPhaseKey, ...],
    news_slot_keys: tuple[NewsSlotKey, ...],
) -> dict[str, str]:
    decision_events = [event.to_dict() for event in calendar.decision_events]
    logical = [key.to_dict() for key in logical_keys]
    logical_hash = canonical_sha256(logical)
    return {
        "decision_events": canonical_sha256(decision_events),
        "logical_agent_events": logical_hash,
        "stb": logical_hash,
        "ltb_transitions": logical_hash,
        "analysis": logical_hash,
        "decisions": logical_hash,
        "fills": logical_hash,
        "community_phase_opportunities": canonical_sha256([key.to_dict() for key in phase_keys]),
        "news_slots_target": canonical_sha256([key.to_dict() for key in news_slot_keys]),
    }


def _derive_counts(
    cohort: CohortRegistry,
    calendar: CalendarRegistry,
    burn_in_dates: tuple[str, ...],
    evaluation_dates: tuple[str, ...],
    phase_keys: tuple[CommunityPhaseKey, ...],
    news_slot_keys: tuple[NewsSlotKey, ...],
    *,
    target_real_news_per_event: int,
) -> dict[str, int]:
    n_agents = len(cohort.members)
    n_conditions = len(RN_CONDITIONS)
    decision_turns = len(calendar.decision_events)
    phase_per_arm = len(calendar.community_phases)
    return {
        "conditions": n_conditions,
        "agents": n_agents,
        "trading_dates": len(calendar.trading_dates),
        "burn_in_dates": len(burn_in_dates),
        "primary_evaluation_dates": len(evaluation_dates),
        "decision_events": decision_turns,
        "decision_turns_per_agent": decision_turns,
        "stb_updates_per_agent": decision_turns,
        "ltb_updates_per_agent": decision_turns,
        "ltb_states_per_agent_including_ltb0": decision_turns + 1,
        "agent_event_keys_per_arm": n_agents * decision_turns,
        "agent_event_keys_all_arms": n_conditions * n_agents * decision_turns,
        "fills_per_arm": n_agents * decision_turns,
        "community_phase_keys_per_arm": phase_per_arm,
        "community_phase_keys_all_arms": len(phase_keys),
        "active_community_agents": sum(member.news_depth >= 1 for member in cohort.members),
        "best_audience_agents": n_agents,
        "target_real_news_articles_per_event": target_real_news_per_event,
        "target_real_news_article_slots": len(news_slot_keys),
    }
