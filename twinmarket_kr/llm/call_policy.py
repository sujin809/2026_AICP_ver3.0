"""Strict API-call policy for the canonical experiment runtime.

The generic OpenRouter client is shared with exploratory scripts. This module
keeps paper-run fail-closed rules in the common LLM boundary and makes the
policy serializable into a resolved study manifest.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Iterable

from twinmarket_kr.rn_model_pin import RN_PAPER_MODEL


class CallPolicyError(ValueError):
    """A policy or its runtime audit evidence is not safe for a paper run."""


# OpenRouter's actual reasoning-off switch is ``effort: "none"``.  ``exclude``
# alone only hides reasoning tokens that were already generated, so it is kept
# here as an audit/privacy companion rather than treated as the off switch.
OPENROUTER_REASONING_OFF = {"effort": "none", "exclude": True}

# Every strict RN stage is a deterministic JSON-object call.  Keep these
# request-shape values beside the reasoning/provider policy so the provider
# boundary, journal identity, and audit validator share one contract.
RN_STRICT_TEMPERATURE = 0.2
RN_STRICT_RESPONSE_FORMAT = MappingProxyType({"type": "json_object"})

# Versioned response budgets for the integrated numbered runner.  The values
# originate from the strict RN implementation, but now live at the common LLM
# boundary so production code does not import ``rn_ab``.  News helper stages
# receive explicit budgets as well; no paid production call may inherit a
# provider/model default.
INTEGRATED_STAGE_MAX_TOKENS_V1 = MappingProxyType(
    {
        "news_interpretation": 2048,
        "depth2_pre_search": 1024,
        "depth2_post_search": 2048,
        "short_term_belief": 3072,
        "market_analysis": 3072,
        "trading_decision": 1024,
        "post_fill_long_term_belief": 3072,
        "community_posting": 1024,
        "community_read_select": 512,
        "community_read_react": 1024,
        "community_thinking": 2048,
    }
)

INTEGRATED_STAGE_SCHEMA_VERSIONS_V1 = MappingProxyType(
    {
        "news_interpretation": "integrated-news-interpretation-v1",
        "depth2_pre_search": "integrated-depth2-pre-search-v1",
        "depth2_post_search": "integrated-depth2-post-search-v1",
        "short_term_belief": "integrated-stb-v1",
        "market_analysis": "integrated-analysis-v1",
        "trading_decision": "integrated-decision-v1",
        "post_fill_long_term_belief": "integrated-post-fill-ltb-v1",
        "community_posting": "integrated-community-posting-v1",
        "community_read_select": "integrated-community-read-select-v1",
        "community_read_react": "integrated-community-read-react-v1",
        "community_thinking": "integrated-community-thinking-v1",
    }
)

# Exact JSONL event schema written by ``OpenRouterClient``.  Every physical
# request produces a provider-attempt event; a validated/journaled response
# produces a second append-only experiment-acceptance event bound to the first.
# Keeping the schema here lets an offline canary plan state what future live
# telemetry must contain without inventing any telemetry itself.
RN_REASONING_AUDIT_FIELDS = frozenset(
    {
        "timestamp_utc",
        "pid",
        "label",
        "audit_event",
        "status",
        "requested_model",
        "returned_model",
        "provider",
        "request_id",
        "seed",
        "attempt",
        "latency_seconds",
        "prompt_sha256",
        "usage",
        "reasoning_tokens",
        "response_reasoning_present",
        "finish_reason",
        "provider_response_sha256",
        "provider_canonical_json_sha256",
        "accepted_response_sha256",
        "provider_attempt_sha256",
        "request_policy",
        "temperature",
        "response_format",
        "max_tokens",
        "request_policy_sha256",
        "logical_call_id",
        "phase_attempt_id",
        "audit_context",
        "error_type",
        "error",
    }
)

# ``RN_PAPER_MODEL`` is imported from an import-safe top-level module so both
# legacy ``config`` and this strict RN package share one code pin without a
# config → rn_ab package-initialization cycle.


@dataclass(frozen=True)
class StrictCallPolicy:
    model: str
    provider: str
    max_retries: int
    concurrency: int
    reasoning_effort: str = "none"
    reasoning_exclude: bool = True
    allow_fallbacks: bool = False
    require_parameters: bool = True

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "StrictCallPolicy":
        allowed = {
            "model",
            "provider",
            "max_retries",
            "concurrency",
            "reasoning_effort",
            "reasoning_exclude",
            "allow_fallbacks",
            "require_parameters",
        }
        unknown = set(value) - allowed
        if unknown:
            raise CallPolicyError(f"Unknown call-policy fields: {sorted(unknown)}")
        for field, default in (
            ("reasoning_exclude", True),
            ("allow_fallbacks", False),
            ("require_parameters", True),
        ):
            candidate = value.get(field, default)
            if not isinstance(candidate, bool):
                raise CallPolicyError(f"{field} must be a boolean")
        try:
            policy = cls(
                model=str(value["model"]).strip(),
                provider=str(value["provider"]).strip(),
                max_retries=int(value["max_retries"]),
                concurrency=int(value["concurrency"]),
                reasoning_effort=str(value.get("reasoning_effort", "none")).strip(),
                reasoning_exclude=value.get("reasoning_exclude", True),
                allow_fallbacks=value.get("allow_fallbacks", False),
                require_parameters=value.get("require_parameters", True),
            )
        except KeyError as exc:
            raise CallPolicyError(f"Missing required call-policy field: {exc.args[0]}") from exc
        policy.validate()
        return policy

    def validate(self) -> None:
        if not self.model:
            raise CallPolicyError("Paper call policy requires an explicit model")
        if not self.provider:
            raise CallPolicyError("Paper call policy requires exactly one pinned provider")
        if self.max_retries < 1:
            raise CallPolicyError("max_retries must be at least one")
        if self.concurrency < 1:
            raise CallPolicyError("concurrency must be at least one")
        if self.reasoning_effort != "none" or not self.reasoning_exclude:
            raise CallPolicyError("Paper calls require reasoning effort='none' and exclude=true")
        if self.allow_fallbacks:
            raise CallPolicyError("Paper calls must disable provider fallbacks")
        if not self.require_parameters:
            raise CallPolicyError("Paper calls must require provider parameters")

    def as_request_policy(self) -> dict[str, Any]:
        self.validate()
        return {
            "reasoning": dict(OPENROUTER_REASONING_OFF),
            "provider": {
                "only": [self.provider],
                "order": [self.provider],
                "allow_fallbacks": False,
                "require_parameters": True,
            },
        }

    def assert_final_request_policy(self, value: Any) -> None:
        """Fail closed if an option merge changes strict reasoning-off policy."""
        if not isinstance(value, dict) or value != self.as_request_policy():
            raise CallPolicyError("Final request policy differs from sealed strict reasoning-off policy")

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "provider": self.provider,
            "max_retries": self.max_retries,
            "concurrency": self.concurrency,
            "reasoning_effort": self.reasoning_effort,
            "reasoning_exclude": self.reasoning_exclude,
            "allow_fallbacks": self.allow_fallbacks,
            "require_parameters": self.require_parameters,
        }


def validate_reasoning_audit(
    rows: Iterable[dict[str, Any]],
    *,
    policy: StrictCallPolicy,
    require_success: bool = True,
) -> dict[str, int]:
    """Validate provider attempts and experiment-acceptance bindings.

    The function intentionally treats absent provider/token/policy fields as
    failures.  Provider-returned malformed JSON remains a valid physical
    attempt with a raw-response digest, but it cannot have an acceptance event.
    ``success_count`` therefore counts accepted logical responses, while
    ``attempt_count`` counts physical provider attempts including retries.
    """
    policy.validate()
    attempts = list(rows)
    if not attempts:
        raise CallPolicyError("Reasoning audit is empty")
    provider_attempts: list[dict[str, Any]] = []
    accepted_rows: list[dict[str, Any]] = []
    for index, row in enumerate(attempts, start=1):
        if not isinstance(row, dict) or set(row) != RN_REASONING_AUDIT_FIELDS:
            missing = sorted(RN_REASONING_AUDIT_FIELDS - set(row))
            extra = sorted(set(row) - RN_REASONING_AUDIT_FIELDS)
            raise CallPolicyError(
                f"Audit row {index} has an invalid exact schema; missing={missing}, extra={extra}"
            )
        _validate_common_audit_fields(row, index=index)
        request_policy = row.get("request_policy")
        if not isinstance(request_policy, dict):
            raise CallPolicyError(f"Audit row {index} has no request policy")
        if request_policy != policy.as_request_policy():
            raise CallPolicyError(f"Audit row {index} request policy differs from the sealed policy")
        expected_policy_sha256 = hashlib.sha256(
            json.dumps(
                request_policy,
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        if row.get("request_policy_sha256") != expected_policy_sha256:
            raise CallPolicyError(f"Audit row {index} request-policy hash is invalid")
        if str(row.get("requested_model") or "") != policy.model:
            raise CallPolicyError(f"Audit row {index} requested an unexpected model")
        if row.get("temperature") != RN_STRICT_TEMPERATURE:
            raise CallPolicyError(f"Audit row {index} temperature differs from the sealed strict value")
        if row.get("response_format") != dict(RN_STRICT_RESPONSE_FORMAT):
            raise CallPolicyError(f"Audit row {index} response format differs from the strict JSON contract")
        audit_event = str(row.get("audit_event") or "")
        status = str(row.get("status") or "")
        if audit_event == "provider_attempt":
            if status not in {"provider_returned", "error"}:
                raise CallPolicyError(
                    f"Audit row {index} has an invalid provider-attempt status"
                )
            if (
                row.get("accepted_response_sha256") is not None
                or row.get("provider_attempt_sha256") is not None
            ):
                raise CallPolicyError(
                    f"Audit row {index} provider attempt claims experiment acceptance"
                )
            provider_attempts.append(row)
        elif audit_event == "experiment_acceptance":
            if status != "accepted":
                raise CallPolicyError(
                    f"Audit row {index} has an invalid experiment-acceptance status"
                )
            accepted_rows.append(row)
        else:
            raise CallPolicyError(f"Audit row {index} has an invalid audit_event")
        if status == "error":
            if (
                row.get("provider_response_sha256") is not None
                or row.get("provider_canonical_json_sha256") is not None
            ):
                raise CallPolicyError(
                    f"Audit row {index} error unexpectedly contains a response digest"
                )
            continue
        if row.get("error_type") is not None or row.get("error") is not None:
            raise CallPolicyError(f"Audit row {index} returned but contains an error")
        if not isinstance(row.get("usage"), dict):
            raise CallPolicyError(f"Audit row {index} has no structured usage telemetry")
        if not isinstance(row.get("request_id"), str) or not row["request_id"].strip():
            raise CallPolicyError(f"Audit row {index} has no provider request ID")
        if str(row.get("returned_model") or "") != policy.model:
            raise CallPolicyError(f"Audit row {index} returned an unexpected model")
        # OpenRouter는 요청에 슬러그("alibaba")를 받고 응답에는 표시명("Alibaba")을
        # 돌려준다. 정확 문자열 비교는 정상 응답을 전부 거부하며, 이 검증기는 live
        # 실행의 시작 지점에서도 호출되므로 본실험이 첫 호출 전에 죽는다.
        # 라우팅 자체는 request_policy의 provider.only와 allow_fallbacks=false가
        # 이미 고정하므로, 여기서는 표기 차이만 흡수한다.
        if str(row.get("provider") or "").casefold() != policy.provider.casefold():
            raise CallPolicyError(f"Audit row {index} returned an unexpected provider")
        if row.get("response_reasoning_present") is not False:
            raise CallPolicyError(f"Audit row {index} contains response reasoning")
        if str(row.get("finish_reason") or "") != "stop":
            raise CallPolicyError(f"Audit row {index} did not finish normally")
        reasoning_tokens = row.get("reasoning_tokens")
        if (
            isinstance(reasoning_tokens, bool)
            or not isinstance(reasoning_tokens, int)
            or reasoning_tokens != 0
        ):
            raise CallPolicyError(f"Audit row {index} does not prove reasoning_tokens=0")
        provider_response_sha256 = row.get("provider_response_sha256")
        _validate_sha256(
            provider_response_sha256,
            label=f"Audit row {index} provider_response_sha256",
        )
        provider_canonical_json_sha256 = row.get("provider_canonical_json_sha256")
        if provider_canonical_json_sha256 is not None:
            _validate_sha256(
                provider_canonical_json_sha256,
                label=f"Audit row {index} provider_canonical_json_sha256",
            )
        if audit_event == "experiment_acceptance":
            accepted_response_sha256 = row.get("accepted_response_sha256")
            _validate_sha256(
                accepted_response_sha256,
                label=f"Audit row {index} accepted_response_sha256",
            )
            if accepted_response_sha256 != provider_canonical_json_sha256:
                raise CallPolicyError(
                    f"Audit row {index} accepted digest differs from canonical provider JSON"
                )
            _validate_sha256(
                row.get("provider_attempt_sha256"),
                label=f"Audit row {index} provider_attempt_sha256",
            )

    provider_rows_by_sha = {
        _canonical_sha256(row): row
        for row in provider_attempts
        if row.get("status") == "provider_returned"
    }
    accepted_logical_ids: set[str] = set()
    accepted_provider_attempts: set[str] = set()
    for index, row in enumerate(accepted_rows, start=1):
        provider_attempt_sha256 = str(row["provider_attempt_sha256"])
        provider_row = provider_rows_by_sha.get(provider_attempt_sha256)
        if provider_row is None:
            raise CallPolicyError(
                f"Acceptance row {index} does not bind an exact provider-returned row"
            )
        for field in (
            "logical_call_id",
            "phase_attempt_id",
            "attempt",
            "request_id",
            "prompt_sha256",
            "provider_response_sha256",
            "provider_canonical_json_sha256",
        ):
            if row.get(field) != provider_row.get(field):
                raise CallPolicyError(
                    f"Acceptance row {index} differs from its provider attempt at {field}"
                )
        logical_call_id = str(row["logical_call_id"])
        if logical_call_id in accepted_logical_ids:
            raise CallPolicyError(
                f"Reasoning audit repeats experiment acceptance for {logical_call_id}"
            )
        if provider_attempt_sha256 in accepted_provider_attempts:
            raise CallPolicyError(
                f"Reasoning audit accepts one provider attempt more than once"
            )
        accepted_logical_ids.add(logical_call_id)
        accepted_provider_attempts.add(provider_attempt_sha256)

    if require_success and not accepted_rows:
        raise CallPolicyError("Reasoning audit has no experiment-accepted response")
    return {
        "attempt_count": len(provider_attempts),
        "success_count": len(accepted_rows),
    }


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _validate_sha256(value: Any, *, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value.lower())
    ):
        raise CallPolicyError(f"{label} is invalid")


def _validate_common_audit_fields(row: dict[str, Any], *, index: int) -> None:
    timestamp = row.get("timestamp_utc")
    if not isinstance(timestamp, str):
        raise CallPolicyError(f"Audit row {index} has no UTC timestamp")
    try:
        parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CallPolicyError(f"Audit row {index} has an invalid UTC timestamp") from exc
    if parsed_timestamp.tzinfo is None or parsed_timestamp.utcoffset() is None:
        raise CallPolicyError(f"Audit row {index} timestamp is not timezone-aware")
    if parsed_timestamp.utcoffset().total_seconds() != 0:
        raise CallPolicyError(f"Audit row {index} timestamp is not UTC")
    for field in ("pid", "attempt", "max_tokens"):
        value = row.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise CallPolicyError(f"Audit row {index} has invalid {field}")
    seed = row.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise CallPolicyError(f"Audit row {index} has invalid seed")
    latency = row.get("latency_seconds")
    if (
        isinstance(latency, bool)
        or not isinstance(latency, (int, float))
        or float(latency) < 0
    ):
        raise CallPolicyError(f"Audit row {index} has invalid latency")
    for field in ("label", "logical_call_id", "phase_attempt_id"):
        value = row.get(field)
        if not isinstance(value, str) or not value.strip():
            raise CallPolicyError(f"Audit row {index} has invalid {field}")
    prompt_sha256 = row.get("prompt_sha256")
    if (
        not isinstance(prompt_sha256, str)
        or len(prompt_sha256) != 64
        or any(char not in "0123456789abcdef" for char in prompt_sha256.lower())
    ):
        raise CallPolicyError(f"Audit row {index} has invalid prompt_sha256")
    if not isinstance(row.get("audit_context"), dict):
        raise CallPolicyError(f"Audit row {index} has no audit context")
