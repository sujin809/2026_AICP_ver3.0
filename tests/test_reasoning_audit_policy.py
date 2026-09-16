from __future__ import annotations

import hashlib
import json
import unittest

from twinmarket_kr.llm.call_policy import (
    RN_STRICT_RESPONSE_FORMAT,
    RN_STRICT_TEMPERATURE,
    CallPolicyError,
    StrictCallPolicy,
    _canonical_sha256,
    validate_reasoning_audit,
)


POLICY = StrictCallPolicy(
    model="qwen/qwen3.5-flash-02-23",
    provider="alibaba",
    max_retries=6,
    concurrency=8,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _audit_rows(*, provider: str) -> list[dict[str, object]]:
    """Build one provider-returned row and the acceptance row bound to it.

    ``provider`` is the *response* label. OpenRouter answers with a display
    name ("Alibaba") even though the request pins the slug ("alibaba").
    """

    request_policy = POLICY.as_request_policy()
    provider_row: dict[str, object] = {
        "audit_event": "provider_attempt",
        "status": "provider_returned",
        "timestamp_utc": "2026-07-30T06:00:00+00:00",
        "pid": 1234,
        "attempt": 1,
        "max_tokens": 64,
        "seed": 7,
        "latency_seconds": 0.9,
        "label": "reasoning_off_canary",
        "logical_call_id": "canary-logical-1",
        "phase_attempt_id": "canary-phase-1",
        "prompt_sha256": _sha("canary prompt"),
        "audit_context": {"mode": "canary"},
        "request_policy": request_policy,
        "request_policy_sha256": hashlib.sha256(
            json.dumps(request_policy, ensure_ascii=False, sort_keys=True).encode(
                "utf-8"
            )
        ).hexdigest(),
        "requested_model": POLICY.model,
        "returned_model": POLICY.model,
        "provider": provider,
        "temperature": RN_STRICT_TEMPERATURE,
        "response_format": dict(RN_STRICT_RESPONSE_FORMAT),
        "request_id": "req-canary-1",
        "usage": {"prompt_tokens": 34, "completion_tokens": 6, "total_tokens": 40},
        "reasoning_tokens": 0,
        "response_reasoning_present": False,
        "finish_reason": "stop",
        "error": None,
        "error_type": None,
        "provider_response_sha256": _sha("provider response"),
        "provider_canonical_json_sha256": _sha("canonical provider json"),
        "accepted_response_sha256": None,
        "provider_attempt_sha256": None,
    }
    acceptance_row = dict(provider_row)
    acceptance_row.update(
        {
            "audit_event": "experiment_acceptance",
            "status": "accepted",
            "accepted_response_sha256": provider_row["provider_canonical_json_sha256"],
            "provider_attempt_sha256": _canonical_sha256(provider_row),
        }
    )
    return [provider_row, acceptance_row]


class ReasoningAuditProviderCaseTests(unittest.TestCase):
    """provider 표기 차이로 정상 응답이 거부되면 본실험이 첫 호출 전에 죽는다.

    `validate_reasoning_audit`는 canary 캡처뿐 아니라 모든 live 실행의 시작
    지점에서도 호출된다. OpenRouter는 요청에 슬러그를 받고 응답에는 표시명을
    돌려주므로, 이 비교는 대소문자를 무시해야 한다. 라우팅 고정은 request_policy의
    provider.only와 allow_fallbacks=false가 담당한다.
    """

    def test_accepts_the_provider_display_name_returned_by_openrouter(self) -> None:
        for provider in ("Alibaba", "alibaba", "ALIBABA"):
            with self.subTest(provider=provider):
                summary = validate_reasoning_audit(
                    _audit_rows(provider=provider),
                    policy=POLICY,
                    require_success=True,
                )
                self.assertEqual(summary["attempt_count"], 1)
                self.assertEqual(summary["success_count"], 1)

    def test_still_rejects_a_different_provider(self) -> None:
        for provider in ("DeepInfra", "together", "alibaba-cloud"):
            with self.subTest(provider=provider):
                with self.assertRaisesRegex(CallPolicyError, "unexpected provider"):
                    validate_reasoning_audit(
                        _audit_rows(provider=provider),
                        policy=POLICY,
                        require_success=True,
                    )

    def test_still_rejects_nonzero_reasoning_tokens(self) -> None:
        rows = _audit_rows(provider="Alibaba")
        for row in rows:
            row["reasoning_tokens"] = 12
        with self.assertRaisesRegex(CallPolicyError, "reasoning_tokens=0"):
            validate_reasoning_audit(rows, policy=POLICY, require_success=True)

    def test_still_rejects_an_unexpected_model(self) -> None:
        rows = _audit_rows(provider="Alibaba")
        for row in rows:
            row["returned_model"] = "qwen/qwen3.5-plus"
        with self.assertRaisesRegex(CallPolicyError, "unexpected model"):
            validate_reasoning_audit(rows, policy=POLICY, require_success=True)


if __name__ == "__main__":
    unittest.main()
