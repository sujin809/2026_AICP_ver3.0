from __future__ import annotations

import unittest

from twinmarket_kr.rn_ab.call_policy import (
    OPENROUTER_REASONING_OFF,
    CallPolicyError,
    StrictCallPolicy,
)


class StrictCallPolicyTests(unittest.TestCase):
    def test_openrouter_reasoning_off_constant_uses_effort_none_not_hide_only(self) -> None:
        self.assertEqual(
            OPENROUTER_REASONING_OFF,
            {"effort": "none", "exclude": True},
        )

    def test_string_boolean_cannot_bypass_reasoning_or_provider_policy(self) -> None:
        with self.assertRaisesRegex(CallPolicyError, "reasoning_exclude must be a boolean"):
            StrictCallPolicy.from_mapping(
                {
                    "model": "model-x",
                    "provider": "provider-x",
                    "max_retries": 1,
                    "concurrency": 1,
                    "reasoning_exclude": "false",
                }
            )

    def test_exact_safe_policy_serializes_to_required_request_shape(self) -> None:
        policy = StrictCallPolicy.from_mapping(
            {
                "model": "model-x",
                "provider": "provider-x",
                "max_retries": 1,
                "concurrency": 1,
                "reasoning_effort": "none",
                "reasoning_exclude": True,
                "allow_fallbacks": False,
                "require_parameters": True,
            }
        )
        self.assertEqual(
            policy.as_request_policy(),
            {
                "reasoning": {"effort": "none", "exclude": True},
                "provider": {
                    "only": ["provider-x"],
                    "order": ["provider-x"],
                    "allow_fallbacks": False,
                    "require_parameters": True,
                },
            },
        )
        policy.assert_final_request_policy(policy.as_request_policy())
        with self.assertRaisesRegex(CallPolicyError, "Final request policy"):
            policy.assert_final_request_policy({"reasoning": {"effort": "low"}})


if __name__ == "__main__":
    unittest.main()
