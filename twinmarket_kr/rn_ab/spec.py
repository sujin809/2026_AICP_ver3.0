"""Authoring contract for the real-news Community ON/OFF baseline.

This module deliberately does not import the legacy ``config`` module.  The
paper path must be driven by one sealed :class:`StudySpec`, rather than by a
mixture of global defaults and command-line overrides.

Only the two approved clean-news conditions are representable here:
``RN_COMM_OFF`` and ``RN_COMM_ON``.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import time as clock_time
from types import MappingProxyType
from typing import Any, Final

from twinmarket_kr.rn_ab.belief_contract import (
    RN_BASELINE_BELIEF_LIMITS,
    has_rn_baseline_belief_limits,
)
from twinmarket_kr.rn_ab.call_policy import RN_PAPER_MODEL


RN_COMM_OFF: Final = "RN_COMM_OFF"
RN_COMM_ON: Final = "RN_COMM_ON"
RN_CONDITIONS: Final[tuple[str, str]] = (RN_COMM_OFF, RN_COMM_ON)
TREATMENT_DIFF_ALLOWLIST: Final[tuple[str, ...]] = ("community_mode",)


class StudySpecError(ValueError):
    """Base error for an invalid authored RN study specification."""


class ArmPairValidationError(StudySpecError):
    """Raised when a paired-arm contract differs outside community availability."""


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$", re.IGNORECASE)
_STUDY_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{2,127}$")
_PLACEHOLDER_RE = re.compile(r"^<[^>]+>$")
_LEGACY_IDENTIFIER_RE = re.compile(
    r"(?:^|[_-])(?:c00|c01|c02|c10|c11|c12|rn_c00|rn_c10)(?:[_-]|$)",
    re.IGNORECASE,
)

_DERIVED_TOP_LEVEL_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "agent_count",
        "agents",
        "trading_days",
        "trading_dates",
        "start_date",
        "end_date",
        "decision_turns",
        "turns",
        "primary_evaluation_days",
        "primary_evaluation_dates",
        "evaluation_dates",
        "resolved_counts",
        "expected_key_set_hashes",
        "expected_rows",
        "expected_calls",
    }
)

_STUDY_SPEC_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "artifact_type",
        "study_id",
        "design_version",
        "baseline_commit",
        "required_agent_count",
        "cohort_registry_sha256",
        "persona_snapshot_manifest_sha256",
        "persona_depth_manifest_sha256",
        "persona_assignment_policy",
        "persona_renderer_sha256",
        "prompt_bundle_sha256",
        "belief_limits",
        "cohort_assertions",
        "condition_treatments",
        "paired_condition_groups",
        "treatment_diff_allowlist",
        "calendar_event_registry_sha256",
        "burn_in_date_ids",
        "regime_policy_sha256",
        "real_news_bundle_manifest_sha256",
        "known_injection_registry_sha256",
        "article_version_leakage_review_manifest_sha256",
        "news_exposure_policy_sha256",
        "news_exposure_policy",
        "community_policy",
        "community_timing_policy",
        "community_timing_policy_sha256",
        "context_window_policy",
        "memory_policy",
        "trade_policy",
        "model_policy",
        "study_seed",
        "seed_namespace",
        "retry_policy_sha256",
        "runtime_policy_sha256",
        "evaluation_policy_sha256",
        "stage_input_registry_file_sha256",
        "stage_input_registry_canonical_sha256",
    }
)

_COMMUNITY_POLICY_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "best_k",
        "best_selection_policy",
        "permissions_from_cohort_depth_map",
        "depth1_selective_read_cap",
        "depth2_selective_read_cap",
        "best_payload",
        "visibility",
    }
)

_COMMUNITY_TIMING_POLICY_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "timezone",
        "pm_phase_not_before",
        "pm_phase_not_after",
        "next_am_delivery_not_before",
        "next_am_delivery_not_after",
    }
)

_NEWS_EXPOSURE_POLICY_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "version",
        "target_real_news_per_event",
        "fake_news_per_event",
        "shortage_policy",
    }
)

_CONTEXT_WINDOW_POLICY_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "decision_historical_order_or_fill_direct_visibility",
        "community_public_author_private_portfolio_or_trade_visibility",
        "trade_memory_visibility",
        "depth2_search_lookback",
        "depth2_search_lookback_unit",
        "depth2_search_top_k",
        "news_category_targets",
        "market_feature_policy_sha256",
    }
)

_MEMORY_POLICY_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "version",
        "cadence",
        "trade_belief_blocks",
        "ltb_update_timing",
        "current_transaction_episode_input",
        "price_outcome_input",
        "outcome_horizons",
    }
)

_TRADE_POLICY_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "stock_code",
        "decision_space",
        "allow_hold",
        "max_single_trade_cash_ratio",
        "fill_policy",
        "commission_rate",
        "commission_applies_to",
        "sell_tax_rate",
        "fee_policy",
        "target_direction_notional",
    }
)

_MODEL_POLICY_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "model",
        "provider",
        "reasoning",
        "per_arm_max_concurrent_llm_calls",
        "physical_http_attempts_per_phase_attempt",
        "allow_provider_fallbacks",
        "require_parameters",
        "reasoning_off_canary_required",
        "reasoning_off_success_contract",
    }
)

_BELIEF_LIMITS_FIELDS: Final[frozenset[str]] = frozenset(
    {"dim_1", "dim_2", "dim_3", "dim_4", "dim_5", "dim_6"}
)


def canonical_json_bytes(value: Any) -> bytes:
    """Return the one canonical JSON representation used for sealed hashes.

    The serializer is intentionally compact and stable.  It is used for
    registry identity and key-set identity, never as a transport format for
    human-editable input.
    """

    return json.dumps(
        _plain_json(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    if isinstance(value, list):
        return [_plain_json(item) for item in value]
    if isinstance(value, set):
        return [_plain_json(item) for item in sorted(value)]
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _plain_json(value.to_dict())
    return value


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list) or isinstance(value, tuple):
        return tuple(_freeze_json(item) for item in value)
    return value


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise StudySpecError(f"{field} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise StudySpecError(f"{field} keys must be strings")
    return value


def _exact_keys(value: Any, field: str, expected: frozenset[str]) -> Mapping[str, Any]:
    mapping = _mapping(value, field)
    actual = set(mapping)
    unknown = sorted(actual - expected)
    missing = sorted(expected - actual)
    if unknown or missing:
        details: list[str] = []
        if unknown:
            details.append(f"unknown={unknown}")
        if missing:
            details.append(f"missing={missing}")
        raise StudySpecError(f"{field} has invalid keys ({', '.join(details)})")
    return mapping


def _nonempty_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StudySpecError(f"{field} must be a non-empty string")
    text = value.strip()
    if text.lower() in {"pending", "tbd", "null"} or _PLACEHOLDER_RE.fullmatch(text):
        raise StudySpecError(f"{field} contains an unresolved placeholder")
    return text


def _sha256(value: Any, field: str) -> str:
    text = _nonempty_str(value, field)
    if not _SHA256_RE.fullmatch(text):
        raise StudySpecError(f"{field} must be a 64-character SHA-256 hex digest")
    return text.lower()


def _positive_int(value: Any, field: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise StudySpecError(f"{field} must be an integer")
    if value < 0 or (value == 0 and not allow_zero):
        relation = "non-negative" if allow_zero else "positive"
        raise StudySpecError(f"{field} must be {relation}")
    return value


def _strict_bool(value: Any, field: str, expected: bool | None = None) -> bool:
    if not isinstance(value, bool):
        raise StudySpecError(f"{field} must be a boolean")
    if expected is not None and value is not expected:
        raise StudySpecError(f"{field} must be {expected!r}")
    return value


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StudySpecError(f"{field} must be a finite number")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise StudySpecError(f"{field} must be finite")
    return numeric


def _date_id(value: Any, field: str) -> str:
    text = _nonempty_str(value, field)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise StudySpecError(f"{field} must use YYYY-MM-DD")
    try:
        # datetime is intentionally imported lazily: this is a strict parser,
        # not a string-only convention.
        from datetime import date

        date.fromisoformat(text)
    except ValueError as exc:
        raise StudySpecError(f"{field} is not a valid calendar date: {text}") from exc
    return text


def _assert_no_unresolved(value: Any, path: str = "study_spec") -> None:
    if value is None:
        raise StudySpecError(f"{path} must not be null")
    if isinstance(value, str):
        text = value.strip()
        # "none" is an intentional, required value for the provider's
        # reasoning-effort setting.  Treating it as a generic placeholder
        # would make a valid reasoning-off policy impossible to express.
        if not text or text.lower() in {"pending", "tbd", "null"} or _PLACEHOLDER_RE.fullmatch(text):
            raise StudySpecError(f"{path} contains an unresolved placeholder")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _assert_no_unresolved(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_no_unresolved(item, f"{path}[{index}]")


@dataclass(frozen=True)
class CohortAssertions:
    """Assertions over a registry; never a second source of assignment data."""

    depth_counts: Mapping[int, int]
    initial_cash_counts: Mapping[int, int]

    @classmethod
    def from_mapping(cls, value: Any) -> "CohortAssertions":
        mapping = _exact_keys(value, "cohort_assertions", frozenset({"depth_counts", "initial_cash_counts"}))
        raw_depths = _mapping(mapping["depth_counts"], "cohort_assertions.depth_counts")
        if set(raw_depths) != {"0", "1", "2"}:
            raise StudySpecError("cohort_assertions.depth_counts must have exactly 0, 1, and 2")
        depth_counts = {
            int(depth): _positive_int(count, f"cohort_assertions.depth_counts.{depth}", allow_zero=True)
            for depth, count in raw_depths.items()
        }
        raw_cash = _mapping(mapping["initial_cash_counts"], "cohort_assertions.initial_cash_counts")
        if not raw_cash:
            raise StudySpecError("cohort_assertions.initial_cash_counts must not be empty")
        cash_counts: dict[int, int] = {}
        for cash, count in raw_cash.items():
            if not isinstance(cash, str) or not re.fullmatch(r"[1-9]\d*", cash):
                raise StudySpecError(
                    "cohort_assertions.initial_cash_counts keys must use canonical positive integers"
                )
            try:
                numeric_cash = int(cash)
            except (TypeError, ValueError) as exc:
                raise StudySpecError(
                    f"cohort_assertions.initial_cash_counts key must be an integer: {cash!r}"
                ) from exc
            if numeric_cash <= 0:
                raise StudySpecError("cohort_assertions.initial_cash_counts cash keys must be positive")
            if numeric_cash in cash_counts:
                raise StudySpecError(
                    "cohort_assertions.initial_cash_counts contains duplicate normalized cash keys"
                )
            cash_counts[numeric_cash] = _positive_int(
                count,
                f"cohort_assertions.initial_cash_counts.{cash}",
                allow_zero=True,
            )
        return cls(
            depth_counts=MappingProxyType(dict(sorted(depth_counts.items()))),
            initial_cash_counts=MappingProxyType(dict(sorted(cash_counts.items()))),
        )

    def to_dict(self) -> dict[str, dict[str, int]]:
        return {
            "depth_counts": {str(key): value for key, value in self.depth_counts.items()},
            "initial_cash_counts": {
                str(key): value for key, value in self.initial_cash_counts.items()
            },
        }


@dataclass(frozen=True)
class TradePolicy:
    stock_code: str
    decision_space: tuple[str, ...]
    allow_hold: bool
    max_single_trade_cash_ratio: float
    fill_policy: str
    commission_rate: float
    commission_applies_to: tuple[str, ...]
    sell_tax_rate: float
    fee_policy: str
    target_direction_notional: str

    @classmethod
    def from_mapping(cls, value: Any) -> "TradePolicy":
        mapping = _exact_keys(value, "trade_policy", _TRADE_POLICY_FIELDS)
        stock_code = _nonempty_str(mapping["stock_code"], "trade_policy.stock_code")
        if stock_code != "005930":
            raise StudySpecError("trade_policy.stock_code must be the approved Samsung code '005930'")
        raw_space = mapping["decision_space"]
        if not isinstance(raw_space, Sequence) or isinstance(raw_space, (str, bytes)):
            raise StudySpecError("trade_policy.decision_space must be an ordered array")
        decision_space = tuple(str(item) for item in raw_space)
        if decision_space != ("buy", "sell"):
            raise StudySpecError("trade_policy.decision_space must be exactly ['buy', 'sell']")
        allow_hold = _strict_bool(mapping["allow_hold"], "trade_policy.allow_hold", expected=False)
        max_ratio = _finite_number(
            mapping["max_single_trade_cash_ratio"],
            "trade_policy.max_single_trade_cash_ratio",
        )
        if not 0 < max_ratio <= 1:
            raise StudySpecError("trade_policy.max_single_trade_cash_ratio must be in (0, 1]")
        commission = _finite_number(mapping["commission_rate"], "trade_policy.commission_rate")
        sell_tax = _finite_number(mapping["sell_tax_rate"], "trade_policy.sell_tax_rate")
        if commission != 0.0 or sell_tax != 0.0:
            raise StudySpecError("RN baseline requires commission_rate=0.0 and sell_tax_rate=0.0")
        applies = mapping["commission_applies_to"]
        if not isinstance(applies, Sequence) or isinstance(applies, (str, bytes)):
            raise StudySpecError("trade_policy.commission_applies_to must be an array")
        if tuple(applies):
            raise StudySpecError("RN baseline requires trade_policy.commission_applies_to to be empty")
        fee_policy = _nonempty_str(mapping["fee_policy"], "trade_policy.fee_policy")
        if "zero_fee" not in fee_policy:
            raise StudySpecError("trade_policy.fee_policy must explicitly declare zero_fee")
        return cls(
            stock_code=stock_code,
            decision_space=decision_space,
            allow_hold=allow_hold,
            max_single_trade_cash_ratio=max_ratio,
            fill_policy=_nonempty_str(mapping["fill_policy"], "trade_policy.fill_policy"),
            commission_rate=commission,
            commission_applies_to=(),
            sell_tax_rate=sell_tax,
            fee_policy=fee_policy,
            target_direction_notional=_nonempty_str(
                mapping["target_direction_notional"], "trade_policy.target_direction_notional"
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "stock_code": self.stock_code,
            "decision_space": list(self.decision_space),
            "allow_hold": self.allow_hold,
            "max_single_trade_cash_ratio": self.max_single_trade_cash_ratio,
            "fill_policy": self.fill_policy,
            "commission_rate": self.commission_rate,
            "commission_applies_to": list(self.commission_applies_to),
            "sell_tax_rate": self.sell_tax_rate,
            "fee_policy": self.fee_policy,
            "target_direction_notional": self.target_direction_notional,
        }


@dataclass(frozen=True)
class CallPolicy:
    """The only provider policy an RN paper runtime may turn into a request."""

    model: str
    provider: str
    reasoning: Mapping[str, Any]
    per_arm_max_concurrent_llm_calls: int
    physical_http_attempts_per_phase_attempt: int
    allow_provider_fallbacks: bool
    require_parameters: bool
    reasoning_off_canary_required: bool
    reasoning_off_success_contract: str
    retry_policy_sha256: str
    runtime_policy_sha256: str
    offline_llm: bool = False

    @classmethod
    def from_spec_fields(
        cls,
        model_policy: Any,
        *,
        retry_policy_sha256: str,
        runtime_policy_sha256: str,
    ) -> "CallPolicy":
        mapping = _exact_keys(model_policy, "model_policy", _MODEL_POLICY_FIELDS)
        reasoning = _exact_keys(
            mapping["reasoning"],
            "model_policy.reasoning",
            frozenset({"effort", "exclude"}),
        )
        if _nonempty_str(reasoning["effort"], "model_policy.reasoning.effort") != "none":
            raise StudySpecError("RN baseline requires model_policy.reasoning.effort='none'")
        _strict_bool(reasoning["exclude"], "model_policy.reasoning.exclude", expected=True)
        allow_fallbacks = _strict_bool(
            mapping["allow_provider_fallbacks"],
            "model_policy.allow_provider_fallbacks",
            expected=False,
        )
        require_parameters = _strict_bool(
            mapping["require_parameters"],
            "model_policy.require_parameters",
            expected=True,
        )
        concurrency = _positive_int(
            mapping["per_arm_max_concurrent_llm_calls"],
            "model_policy.per_arm_max_concurrent_llm_calls",
        )
        physical_attempts = _positive_int(
            mapping["physical_http_attempts_per_phase_attempt"],
            "model_policy.physical_http_attempts_per_phase_attempt",
        )
        if physical_attempts != 1:
            raise StudySpecError(
                "RN journal owns retries, so model_policy.physical_http_attempts_per_phase_attempt must be 1"
            )
        canary_required = _strict_bool(
            mapping["reasoning_off_canary_required"],
            "model_policy.reasoning_off_canary_required",
            expected=True,
        )
        model = _nonempty_str(mapping["model"], "model_policy.model")
        if model != RN_PAPER_MODEL:
            raise StudySpecError(
                f"RN baseline requires model_policy.model={RN_PAPER_MODEL!r}"
            )
        return cls(
            model=model,
            provider=_nonempty_str(mapping["provider"], "model_policy.provider"),
            reasoning=MappingProxyType({"effort": "none", "exclude": True}),
            per_arm_max_concurrent_llm_calls=concurrency,
            physical_http_attempts_per_phase_attempt=physical_attempts,
            allow_provider_fallbacks=allow_fallbacks,
            require_parameters=require_parameters,
            reasoning_off_canary_required=canary_required,
            reasoning_off_success_contract=_nonempty_str(
                mapping["reasoning_off_success_contract"],
                "model_policy.reasoning_off_success_contract",
            ),
            retry_policy_sha256=retry_policy_sha256,
            runtime_policy_sha256=runtime_policy_sha256,
            offline_llm=False,
        )

    def request_policy(self) -> dict[str, Any]:
        """Return the immutable audit/runtime policy for every physical attempt.

        ``offline_llm`` is an execution-environment assertion, not an OpenAI-
        compatible HTTP parameter.  HTTP clients must use
        :meth:`http_request_policy` instead of forwarding this whole object as
        ``extra_body``.
        """

        return {
            "reasoning": {"effort": "none", "exclude": True},
            "provider": {
                "only": [self.provider],
                "order": [self.provider],
                "allow_fallbacks": False,
                "require_parameters": True,
            },
            "offline_llm": False,
        }

    def http_request_policy(self) -> dict[str, Any]:
        """Return exactly the HTTP ``extra_body`` controls allowed for RN calls."""

        policy = self.request_policy()
        return {
            "reasoning": policy["reasoning"],
            "provider": policy["provider"],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "provider": self.provider,
            "reasoning": dict(self.reasoning),
            "per_arm_max_concurrent_llm_calls": self.per_arm_max_concurrent_llm_calls,
            "physical_http_attempts_per_phase_attempt": self.physical_http_attempts_per_phase_attempt,
            "allow_provider_fallbacks": self.allow_provider_fallbacks,
            "require_parameters": self.require_parameters,
            "reasoning_off_canary_required": self.reasoning_off_canary_required,
            "reasoning_off_success_contract": self.reasoning_off_success_contract,
            "retry_policy_sha256": self.retry_policy_sha256,
            "runtime_policy_sha256": self.runtime_policy_sha256,
            "offline_llm": self.offline_llm,
            "request_policy": self.request_policy(),
            "http_request_policy": self.http_request_policy(),
        }


@dataclass(frozen=True)
class StudySpec:
    """Validated authored input; all counts and key sets remain resolver-derived."""

    study_id: str
    design_version: str
    baseline_commit: str
    required_agent_count: int
    cohort_registry_sha256: str
    persona_snapshot_manifest_sha256: str
    persona_depth_manifest_sha256: str
    persona_assignment_policy: str
    persona_renderer_sha256: str
    prompt_bundle_sha256: str
    belief_limits: Mapping[str, int]
    cohort_assertions: CohortAssertions
    calendar_event_registry_sha256: str
    burn_in_date_ids: tuple[str, ...]
    condition_treatments: Mapping[str, Mapping[str, str]]
    paired_condition_groups: tuple[tuple[str, str], ...]
    treatment_diff_allowlist: tuple[str, ...]
    regime_policy_sha256: str
    real_news_bundle_manifest_sha256: str
    known_injection_registry_sha256: str
    article_version_leakage_review_manifest_sha256: str
    news_exposure_policy_sha256: str
    news_exposure_policy: Mapping[str, Any]
    community_policy: Mapping[str, Any]
    community_timing_policy: Mapping[str, Any]
    community_timing_policy_sha256: str
    context_window_policy: Mapping[str, Any]
    memory_policy: Mapping[str, Any]
    trade_policy: TradePolicy
    call_policy: CallPolicy
    study_seed: int
    seed_namespace: str
    retry_policy_sha256: str
    runtime_policy_sha256: str
    evaluation_policy_sha256: str
    stage_input_registry_file_sha256: str
    stage_input_registry_canonical_sha256: str

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "StudySpec":
        mapping = _mapping(value, "study_spec")
        _assert_no_unresolved(mapping)
        unknown_derived = sorted(set(mapping) & _DERIVED_TOP_LEVEL_FIELDS)
        if unknown_derived:
            raise StudySpecError(
                "StudySpec may not author resolver-derived fields: " + ", ".join(unknown_derived)
            )
        mapping = _exact_keys(mapping, "study_spec", _STUDY_SPEC_FIELDS)
        if _nonempty_str(mapping["artifact_type"], "artifact_type") != "study_spec":
            raise StudySpecError("artifact_type must be 'study_spec'")
        study_id = _nonempty_str(mapping["study_id"], "study_id")
        if not _STUDY_ID_RE.fullmatch(study_id):
            raise StudySpecError("study_id must be lowercase kebab/snake style and at least 3 characters")
        if study_id == "current" or _LEGACY_IDENTIFIER_RE.search(study_id):
            raise StudySpecError("study_id must not use a legacy/current namespace")
        baseline_commit = _nonempty_str(mapping["baseline_commit"], "baseline_commit")
        if not _COMMIT_RE.fullmatch(baseline_commit):
            raise StudySpecError("baseline_commit must be a 40- or 64-character git commit hex")

        required_agent_count = _positive_int(
            mapping["required_agent_count"], "required_agent_count"
        )
        cohort_assertions = CohortAssertions.from_mapping(mapping["cohort_assertions"])
        if sum(cohort_assertions.depth_counts.values()) != required_agent_count:
            raise StudySpecError("cohort depth-count assertion must sum to required_agent_count")
        if sum(cohort_assertions.initial_cash_counts.values()) != required_agent_count:
            raise StudySpecError("cohort initial-cash assertion must sum to required_agent_count")
        belief_limits = _parse_belief_limits(mapping["belief_limits"])

        condition_treatments = _parse_conditions(mapping["condition_treatments"])
        paired_groups = _parse_paired_groups(mapping["paired_condition_groups"])
        raw_allowlist = mapping["treatment_diff_allowlist"]
        if not isinstance(raw_allowlist, Sequence) or isinstance(raw_allowlist, (str, bytes)):
            raise StudySpecError("treatment_diff_allowlist must be an ordered array")
        allowlist = tuple(str(item) for item in raw_allowlist)
        if allowlist != TREATMENT_DIFF_ALLOWLIST:
            raise StudySpecError("treatment_diff_allowlist must be exactly ['community_mode']")

        burn_in = _parse_burn_in(mapping["burn_in_date_ids"])
        news_exposure_policy = _parse_news_exposure_policy(mapping["news_exposure_policy"])
        news_exposure_hash = _sha256(mapping["news_exposure_policy_sha256"], "news_exposure_policy_sha256")
        if news_exposure_hash != canonical_sha256(news_exposure_policy):
            raise StudySpecError("news_exposure_policy_sha256 must bind the exact news_exposure_policy")
        community_policy = _parse_community_policy(mapping["community_policy"])
        community_timing_policy = _parse_community_timing_policy(mapping["community_timing_policy"])
        community_timing_hash = _sha256(
            mapping["community_timing_policy_sha256"], "community_timing_policy_sha256"
        )
        if community_timing_hash != canonical_sha256(community_timing_policy):
            raise StudySpecError("community_timing_policy_sha256 must bind the exact community_timing_policy")
        context_window_policy = _parse_context_window_policy(mapping["context_window_policy"])
        memory_policy = _parse_memory_policy(mapping["memory_policy"])
        trade_policy = TradePolicy.from_mapping(mapping["trade_policy"])
        retry_hash = _sha256(mapping["retry_policy_sha256"], "retry_policy_sha256")
        runtime_hash = _sha256(mapping["runtime_policy_sha256"], "runtime_policy_sha256")
        call_policy = CallPolicy.from_spec_fields(
            mapping["model_policy"],
            retry_policy_sha256=retry_hash,
            runtime_policy_sha256=runtime_hash,
        )
        seed = _positive_int(mapping["study_seed"], "study_seed", allow_zero=True)

        return cls(
            study_id=study_id,
            design_version=_nonempty_str(mapping["design_version"], "design_version"),
            baseline_commit=baseline_commit.lower(),
            required_agent_count=required_agent_count,
            cohort_registry_sha256=_sha256(
                mapping["cohort_registry_sha256"], "cohort_registry_sha256"
            ),
            persona_snapshot_manifest_sha256=_sha256(
                mapping["persona_snapshot_manifest_sha256"], "persona_snapshot_manifest_sha256"
            ),
            persona_depth_manifest_sha256=_sha256(
                mapping["persona_depth_manifest_sha256"], "persona_depth_manifest_sha256"
            ),
            persona_assignment_policy=_nonempty_str(
                mapping["persona_assignment_policy"], "persona_assignment_policy"
            ),
            persona_renderer_sha256=_sha256(
                mapping["persona_renderer_sha256"], "persona_renderer_sha256"
            ),
            prompt_bundle_sha256=_sha256(
                mapping["prompt_bundle_sha256"], "prompt_bundle_sha256"
            ),
            belief_limits=belief_limits,
            cohort_assertions=cohort_assertions,
            calendar_event_registry_sha256=_sha256(
                mapping["calendar_event_registry_sha256"], "calendar_event_registry_sha256"
            ),
            burn_in_date_ids=burn_in,
            condition_treatments=condition_treatments,
            paired_condition_groups=paired_groups,
            treatment_diff_allowlist=allowlist,
            regime_policy_sha256=_sha256(mapping["regime_policy_sha256"], "regime_policy_sha256"),
            real_news_bundle_manifest_sha256=_sha256(
                mapping["real_news_bundle_manifest_sha256"], "real_news_bundle_manifest_sha256"
            ),
            known_injection_registry_sha256=_sha256(
                mapping["known_injection_registry_sha256"], "known_injection_registry_sha256"
            ),
            article_version_leakage_review_manifest_sha256=_sha256(
                mapping["article_version_leakage_review_manifest_sha256"],
                "article_version_leakage_review_manifest_sha256",
            ),
            news_exposure_policy_sha256=news_exposure_hash,
            news_exposure_policy=news_exposure_policy,
            community_policy=community_policy,
            community_timing_policy=community_timing_policy,
            community_timing_policy_sha256=community_timing_hash,
            context_window_policy=context_window_policy,
            memory_policy=memory_policy,
            trade_policy=trade_policy,
            call_policy=call_policy,
            study_seed=seed,
            seed_namespace=_nonempty_str(mapping["seed_namespace"], "seed_namespace"),
            retry_policy_sha256=retry_hash,
            runtime_policy_sha256=runtime_hash,
            evaluation_policy_sha256=_sha256(
                mapping["evaluation_policy_sha256"], "evaluation_policy_sha256"
            ),
            stage_input_registry_file_sha256=_sha256(
                mapping["stage_input_registry_file_sha256"], "stage_input_registry_file_sha256"
            ),
            stage_input_registry_canonical_sha256=_sha256(
                mapping["stage_input_registry_canonical_sha256"],
                "stage_input_registry_canonical_sha256",
            ),
        )

    @classmethod
    def from_json_text(cls, text: str) -> "StudySpec":
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise StudySpecError("study_spec JSON is invalid") from exc
        return cls.from_dict(_mapping(value, "study_spec"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_type": "study_spec",
            "study_id": self.study_id,
            "design_version": self.design_version,
            "baseline_commit": self.baseline_commit,
            "required_agent_count": self.required_agent_count,
            "cohort_registry_sha256": self.cohort_registry_sha256,
            "persona_snapshot_manifest_sha256": self.persona_snapshot_manifest_sha256,
            "persona_depth_manifest_sha256": self.persona_depth_manifest_sha256,
            "persona_assignment_policy": self.persona_assignment_policy,
            "persona_renderer_sha256": self.persona_renderer_sha256,
            "prompt_bundle_sha256": self.prompt_bundle_sha256,
            "belief_limits": dict(self.belief_limits),
            "cohort_assertions": self.cohort_assertions.to_dict(),
            "condition_treatments": _plain_json(self.condition_treatments),
            "paired_condition_groups": [list(group) for group in self.paired_condition_groups],
            "treatment_diff_allowlist": list(self.treatment_diff_allowlist),
            "calendar_event_registry_sha256": self.calendar_event_registry_sha256,
            "burn_in_date_ids": list(self.burn_in_date_ids),
            "regime_policy_sha256": self.regime_policy_sha256,
            "real_news_bundle_manifest_sha256": self.real_news_bundle_manifest_sha256,
            "known_injection_registry_sha256": self.known_injection_registry_sha256,
            "article_version_leakage_review_manifest_sha256": (
                self.article_version_leakage_review_manifest_sha256
            ),
            "news_exposure_policy_sha256": self.news_exposure_policy_sha256,
            "news_exposure_policy": _plain_json(self.news_exposure_policy),
            "community_policy": _plain_json(self.community_policy),
            "community_timing_policy": _plain_json(self.community_timing_policy),
            "community_timing_policy_sha256": self.community_timing_policy_sha256,
            "context_window_policy": _plain_json(self.context_window_policy),
            "memory_policy": _plain_json(self.memory_policy),
            "trade_policy": self.trade_policy.to_dict(),
            "model_policy": {
                "model": self.call_policy.model,
                "provider": self.call_policy.provider,
                "reasoning": dict(self.call_policy.reasoning),
                "per_arm_max_concurrent_llm_calls": (
                    self.call_policy.per_arm_max_concurrent_llm_calls
                ),
                "physical_http_attempts_per_phase_attempt": (
                    self.call_policy.physical_http_attempts_per_phase_attempt
                ),
                "allow_provider_fallbacks": self.call_policy.allow_provider_fallbacks,
                "require_parameters": self.call_policy.require_parameters,
                "reasoning_off_canary_required": self.call_policy.reasoning_off_canary_required,
                "reasoning_off_success_contract": self.call_policy.reasoning_off_success_contract,
            },
            "study_seed": self.study_seed,
            "seed_namespace": self.seed_namespace,
            "retry_policy_sha256": self.retry_policy_sha256,
            "runtime_policy_sha256": self.runtime_policy_sha256,
            "evaluation_policy_sha256": self.evaluation_policy_sha256,
            "stage_input_registry_file_sha256": self.stage_input_registry_file_sha256,
            "stage_input_registry_canonical_sha256": self.stage_input_registry_canonical_sha256,
        }

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.to_dict())


def assert_only_community_mode_diff(
    off_contract: Mapping[str, Any],
    on_contract: Mapping[str, Any],
) -> None:
    """Assert that two arm contracts differ only in ``community_mode``.

    ``condition_id`` is identity metadata rather than a treatment setting and
    is checked separately.  Every other leaf is compared recursively; this
    prevents a hidden news, cohort, model, seed, or path difference from being
    mistaken for a community effect.
    """

    off = _plain_json(off_contract)
    on = _plain_json(on_contract)
    if not isinstance(off, dict) or not isinstance(on, dict):
        raise ArmPairValidationError("arm contracts must be JSON objects")
    if off.get("condition_id") != RN_COMM_OFF or on.get("condition_id") != RN_COMM_ON:
        raise ArmPairValidationError("arm contracts must be RN_COMM_OFF and RN_COMM_ON in order")
    if off.get("community_mode") != "off" or on.get("community_mode") != "on":
        raise ArmPairValidationError("arm contracts must set community_mode off/on")

    left = {key: value for key, value in off.items() if key not in {"condition_id", "community_mode"}}
    right = {key: value for key, value in on.items() if key not in {"condition_id", "community_mode"}}
    diffs = _leaf_differences(left, right)
    if diffs:
        raise ArmPairValidationError(
            "RN paired arms differ outside community_mode: " + ", ".join(diffs[:10])
        )


def _leaf_differences(left: Any, right: Any, path: str = "") -> list[str]:
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        differences: list[str] = []
        for key in sorted(set(left) | set(right)):
            child = f"{path}.{key}" if path else str(key)
            if key not in left or key not in right:
                differences.append(child)
                continue
            differences.extend(_leaf_differences(left[key], right[key], child))
        return differences
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return [path or "<root>"]
        differences: list[str] = []
        for index, (first, second) in enumerate(zip(left, right)):
            differences.extend(_leaf_differences(first, second, f"{path}[{index}]"))
        return differences
    return [] if left == right else [path or "<root>"]


def _parse_conditions(value: Any) -> Mapping[str, Mapping[str, str]]:
    mapping = _mapping(value, "condition_treatments")
    if tuple(mapping) != RN_CONDITIONS:
        raise StudySpecError(
            "condition_treatments must contain only RN_COMM_OFF then RN_COMM_ON in canonical order"
        )
    parsed: dict[str, Mapping[str, str]] = {}
    for condition in RN_CONDITIONS:
        treatment = _exact_keys(
            mapping[condition],
            f"condition_treatments.{condition}",
            frozenset({"community_mode", "news_treatment"}),
        )
        parsed[condition] = MappingProxyType(
            {
                "community_mode": _nonempty_str(
                    treatment["community_mode"], f"condition_treatments.{condition}.community_mode"
                ),
                "news_treatment": _nonempty_str(
                    treatment["news_treatment"], f"condition_treatments.{condition}.news_treatment"
                ),
            }
        )
    if dict(parsed[RN_COMM_OFF]) != {"community_mode": "off", "news_treatment": "real_only"}:
        raise StudySpecError("RN_COMM_OFF must be community off with real_only news")
    if dict(parsed[RN_COMM_ON]) != {"community_mode": "on", "news_treatment": "real_only"}:
        raise StudySpecError("RN_COMM_ON must be community on with real_only news")
    return MappingProxyType(parsed)


def _parse_paired_groups(value: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise StudySpecError("paired_condition_groups must be an ordered array")
    groups: list[tuple[str, str]] = []
    for index, group in enumerate(value):
        if not isinstance(group, Sequence) or isinstance(group, (str, bytes)):
            raise StudySpecError(f"paired_condition_groups[{index}] must be an array")
        groups.append(tuple(str(item) for item in group))
    if tuple(groups) != ((RN_COMM_OFF, RN_COMM_ON),):
        raise StudySpecError("paired_condition_groups must be [[RN_COMM_OFF, RN_COMM_ON]]")
    return tuple(groups)


def _parse_burn_in(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise StudySpecError("burn_in_date_ids must be an ordered array")
    dates = tuple(_date_id(item, f"burn_in_date_ids[{index}]") for index, item in enumerate(value))
    if len(dates) != len(set(dates)):
        raise StudySpecError("burn_in_date_ids must be unique")
    return dates


def _parse_belief_limits(value: Any) -> Mapping[str, int]:
    """Freeze the inherited six-dimension output limits into the study input.

    The legacy project happens to define these values in ``config.py``.  The
    RN paper path must instead record them in the authored specification so a
    worker/restart cannot silently pick up a different process environment.
    """

    mapping = _exact_keys(value, "belief_limits", _BELIEF_LIMITS_FIELDS)
    normalized = MappingProxyType(
        {
            dimension: _positive_int(mapping[dimension], f"belief_limits.{dimension}")
            for dimension in sorted(_BELIEF_LIMITS_FIELDS)
        }
    )
    if not has_rn_baseline_belief_limits(normalized):
        raise StudySpecError(
            "belief_limits must exactly match the approved RN baseline "
            f"{dict(RN_BASELINE_BELIEF_LIMITS)}"
        )
    return normalized


def _parse_community_policy(value: Any) -> Mapping[str, Any]:
    mapping = _exact_keys(value, "community_policy", _COMMUNITY_POLICY_FIELDS)
    best_k = _positive_int(mapping["best_k"], "community_policy.best_k")
    if _nonempty_str(mapping["best_selection_policy"], "community_policy.best_selection_policy") != (
        "top_k_or_fewer_available_no_forced_posting"
    ):
        raise StudySpecError("community_policy.best_selection_policy must prohibit forced posting")
    _strict_bool(
        mapping["permissions_from_cohort_depth_map"],
        "community_policy.permissions_from_cohort_depth_map",
        expected=True,
    )
    depth1 = _positive_int(mapping["depth1_selective_read_cap"], "community_policy.depth1_selective_read_cap", allow_zero=True)
    depth2 = _positive_int(mapping["depth2_selective_read_cap"], "community_policy.depth2_selective_read_cap", allow_zero=True)
    if depth2 < depth1:
        raise StudySpecError("community_policy.depth2_selective_read_cap must be >= depth1 cap")
    if _nonempty_str(mapping["best_payload"], "community_policy.best_payload") != "title_plus_full_frozen_body":
        raise StudySpecError("community_policy.best_payload must include the full frozen body")
    if _nonempty_str(mapping["visibility"], "community_policy.visibility") != "next_approved_am_decision_event":
        raise StudySpecError("community_policy.visibility must be next_approved_am_decision_event")
    return _freeze_json(
        {
            "best_k": best_k,
            "best_selection_policy": "top_k_or_fewer_available_no_forced_posting",
            "permissions_from_cohort_depth_map": True,
            "depth1_selective_read_cap": depth1,
            "depth2_selective_read_cap": depth2,
            "best_payload": "title_plus_full_frozen_body",
            "visibility": "next_approved_am_decision_event",
        }
    )


def _parse_clock(value: Any, field: str) -> str:
    text = _nonempty_str(value, field)
    if not re.fullmatch(r"\d{2}:\d{2}:\d{2}", text):
        raise StudySpecError(f"{field} must use HH:MM:SS")
    try:
        return clock_time.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise StudySpecError(f"{field} must be a valid civil time") from exc


def _parse_community_timing_policy(value: Any) -> Mapping[str, Any]:
    """Parse the one manifest-pinned PM/next-AM timing policy.

    The service separately validates concrete phase timestamps.  This spec
    parser pins the only admissible windows into the authored/hash-sealed study
    contract so a caller cannot broaden them while constructing a service.
    """

    mapping = _exact_keys(value, "community_timing_policy", _COMMUNITY_TIMING_POLICY_FIELDS)
    if _nonempty_str(mapping["timezone"], "community_timing_policy.timezone") != "Asia/Seoul":
        raise StudySpecError("community_timing_policy.timezone must be Asia/Seoul")
    pm_start = _parse_clock(mapping["pm_phase_not_before"], "community_timing_policy.pm_phase_not_before")
    pm_end = _parse_clock(mapping["pm_phase_not_after"], "community_timing_policy.pm_phase_not_after")
    am_start = _parse_clock(
        mapping["next_am_delivery_not_before"],
        "community_timing_policy.next_am_delivery_not_before",
    )
    am_end = _parse_clock(
        mapping["next_am_delivery_not_after"],
        "community_timing_policy.next_am_delivery_not_after",
    )
    if clock_time.fromisoformat(pm_start) > clock_time.fromisoformat(pm_end):
        raise StudySpecError("community_timing_policy PM window is inverted")
    if clock_time.fromisoformat(am_start) > clock_time.fromisoformat(am_end):
        raise StudySpecError("community_timing_policy next-AM window is inverted")
    if clock_time.fromisoformat(pm_start) < clock_time(15, 30, 0):
        raise StudySpecError("community_timing_policy PM window may not begin before 15:30 Asia/Seoul")
    if clock_time.fromisoformat(am_end) > clock_time(9, 0, 0):
        raise StudySpecError("community_timing_policy next-AM window may not extend after 09:00 Asia/Seoul")
    return _freeze_json(
        {
            "timezone": "Asia/Seoul",
            "pm_phase_not_before": pm_start,
            "pm_phase_not_after": pm_end,
            "next_am_delivery_not_before": am_start,
            "next_am_delivery_not_after": am_end,
        }
    )


def _parse_news_exposure_policy(value: Any) -> Mapping[str, Any]:
    """Freeze the real-news target domain from authored policy, never a literal."""

    mapping = _exact_keys(value, "news_exposure_policy", _NEWS_EXPOSURE_POLICY_FIELDS)
    if _nonempty_str(mapping["version"], "news_exposure_policy.version") != "rn-news-exposure-policy-v1":
        raise StudySpecError("news_exposure_policy.version is not approved")
    target_count = _positive_int(
        mapping["target_real_news_per_event"],
        "news_exposure_policy.target_real_news_per_event",
    )
    fake_count = _positive_int(
        mapping["fake_news_per_event"],
        "news_exposure_policy.fake_news_per_event",
        allow_zero=True,
    )
    if fake_count != 0:
        raise StudySpecError("RN baseline news_exposure_policy.fake_news_per_event must be zero")
    if _nonempty_str(mapping["shortage_policy"], "news_exposure_policy.shortage_policy") != (
        "accepted_shortage_no_synthetic_or_duplicate_v1"
    ):
        raise StudySpecError("news_exposure_policy.shortage_policy is not approved")
    return _freeze_json(
        {
            "version": "rn-news-exposure-policy-v1",
            "target_real_news_per_event": target_count,
            "fake_news_per_event": 0,
            "shortage_policy": "accepted_shortage_no_synthetic_or_duplicate_v1",
        }
    )


def _parse_context_window_policy(value: Any) -> Mapping[str, Any]:
    mapping = _exact_keys(value, "context_window_policy", _CONTEXT_WINDOW_POLICY_FIELDS)
    if _nonempty_str(
        mapping["decision_historical_order_or_fill_direct_visibility"],
        "context_window_policy.decision_historical_order_or_fill_direct_visibility",
    ) != "forbidden":
        raise StudySpecError("historical orders/fills must be forbidden from direct decision context")
    if _nonempty_str(
        mapping["community_public_author_private_portfolio_or_trade_visibility"],
        "context_window_policy.community_public_author_private_portfolio_or_trade_visibility",
    ) != "forbidden":
        raise StudySpecError("community author private portfolio/trade visibility must be forbidden")
    if _nonempty_str(
        mapping["depth2_search_lookback_unit"], "context_window_policy.depth2_search_lookback_unit"
    ) != "calendar_days":
        raise StudySpecError("depth2 search lookback unit must be calendar_days")
    targets = _exact_keys(
        mapping["news_category_targets"],
        "context_window_policy.news_category_targets",
        frozenset({"stock", "sector", "economy"}),
    )
    normalized_targets = {
        key: _positive_int(value, f"context_window_policy.news_category_targets.{key}", allow_zero=True)
        for key, value in targets.items()
    }
    return _freeze_json(
        {
            "decision_historical_order_or_fill_direct_visibility": "forbidden",
            "community_public_author_private_portfolio_or_trade_visibility": "forbidden",
            "trade_memory_visibility": _nonempty_str(
                mapping["trade_memory_visibility"], "context_window_policy.trade_memory_visibility"
            ),
            "depth2_search_lookback": _positive_int(
                mapping["depth2_search_lookback"],
                "context_window_policy.depth2_search_lookback",
                allow_zero=True,
            ),
            "depth2_search_lookback_unit": "calendar_days",
            "depth2_search_top_k": _positive_int(
                mapping["depth2_search_top_k"], "context_window_policy.depth2_search_top_k", allow_zero=True
            ),
            "news_category_targets": normalized_targets,
            "market_feature_policy_sha256": _sha256(
                mapping["market_feature_policy_sha256"],
                "context_window_policy.market_feature_policy_sha256",
            ),
        }
    )


def _parse_memory_policy(value: Any) -> Mapping[str, Any]:
    mapping = _exact_keys(value, "memory_policy", _MEMORY_POLICY_FIELDS)
    if _nonempty_str(mapping["cadence"], "memory_policy.cadence") != "each_manifest_decision_event":
        raise StudySpecError("memory_policy.cadence must be each_manifest_decision_event")
    if _nonempty_str(mapping["trade_belief_blocks"], "memory_policy.trade_belief_blocks") != (
        "previous_ltb_plus_current_stb_separate_blocks"
    ):
        raise StudySpecError("memory policy must use separated previous LTB/current STB blocks")
    horizons = mapping["outcome_horizons"]
    if not isinstance(horizons, Sequence) or isinstance(horizons, (str, bytes)):
        raise StudySpecError("memory_policy.outcome_horizons must be an ordered array")
    normalized_horizons = tuple(_nonempty_str(item, "memory_policy.outcome_horizons") for item in horizons)
    if not normalized_horizons:
        raise StudySpecError("memory_policy.outcome_horizons must not be empty")
    return _freeze_json(
        {
            "version": _nonempty_str(mapping["version"], "memory_policy.version"),
            "cadence": "each_manifest_decision_event",
            "trade_belief_blocks": "previous_ltb_plus_current_stb_separate_blocks",
            "ltb_update_timing": _nonempty_str(
                mapping["ltb_update_timing"], "memory_policy.ltb_update_timing"
            ),
            "current_transaction_episode_input": _nonempty_str(
                mapping["current_transaction_episode_input"],
                "memory_policy.current_transaction_episode_input",
            ),
            "price_outcome_input": _nonempty_str(
                mapping["price_outcome_input"], "memory_policy.price_outcome_input"
            ),
            "outcome_horizons": normalized_horizons,
        }
    )
