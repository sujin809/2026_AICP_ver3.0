from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import config
from twinmarket_kr.llm.client import (
    OpenRouterClient,
    ReasoningPolicyError,
    UnexpectedModelError,
    _enforce_paper_reasoning_off,
)
from twinmarket_kr.rn_ab.call_policy import RN_STRICT_RESPONSE_FORMAT, StrictCallPolicy
from twinmarket_kr.rn_ab.call_policy import validate_reasoning_audit


class StrictReasoningOffClientTests(unittest.TestCase):
    def _policy(self, model: str) -> StrictCallPolicy:
        return StrictCallPolicy.from_mapping(
            {"model": model, "provider": "provider-x", "max_retries": 1, "concurrency": 1}
        )

    def test_other_model_is_rejected_before_http_request(self) -> None:
        client = OpenRouterClient.__new__(OpenRouterClient)
        client.offline = False
        with self.assertRaisesRegex(UnexpectedModelError, "only permits"):
            asyncio.run(
                client.chat_strict_reasoning_off(
                    [{"role": "user", "content": "test"}],
                    policy=self._policy("other-model"),
                    max_tokens=1024,
                    audit_label="test",
                    logical_call_id="id",
                    phase_attempt_id="phase",
                )
            )

    def test_paper_model_always_gets_openrouter_effort_none_body(self) -> None:
        body = _enforce_paper_reasoning_off(
            config.PAPER_REASONING_DISABLED_MODEL,
            {"provider": {"allow_fallbacks": False}},
        )
        self.assertEqual(body["reasoning"], {"effort": "none", "exclude": True})
        with self.assertRaisesRegex(ReasoningPolicyError, "requires reasoning"):
            _enforce_paper_reasoning_off(
                config.PAPER_REASONING_DISABLED_MODEL,
                {"reasoning": {"effort": "high", "exclude": True}},
            )

    def test_generic_chat_transmits_reasoning_off_in_the_final_extra_body(self) -> None:
        sent: dict[str, object] = {}

        class FakeCompletions:
            async def create(self, **kwargs: object) -> object:
                sent.update(kwargs)
                return SimpleNamespace(
                    model=config.PAPER_REASONING_DISABLED_MODEL,
                    choices=[],
                )

        @asynccontextmanager
        async def no_op_slot(_: int, *, slot_namespace: str | None = None):
            del slot_namespace
            yield

        client = OpenRouterClient.__new__(OpenRouterClient)
        client.offline = False
        client.model = config.PAPER_REASONING_DISABLED_MODEL
        client.max_retries = 1
        client.timeout = 1.0
        client.concurrency_limit = 1
        client.slot_namespace = None
        client.client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
        with (
            patch("twinmarket_kr.llm.client._global_openrouter_slot", no_op_slot),
            patch("twinmarket_kr.llm.client._record_api_audit"),
        ):
            asyncio.run(client.chat([{"role": "user", "content": "local test"}]))
        self.assertEqual(
            sent["extra_body"],
            {
                "reasoning": {"effort": "none", "exclude": True},
                "provider": {
                    "require_parameters": config.OPENROUTER_REQUIRE_PARAMETERS,
                    "allow_fallbacks": config.OPENROUTER_ALLOW_FALLBACKS,
                    **(
                        {"order": list(config.OPENROUTER_PROVIDER_ORDER)}
                        if config.OPENROUTER_PROVIDER_ORDER
                        else {}
                    ),
                },
            },
        )

    def test_offline_stub_is_rejected_before_any_api_request(self) -> None:
        previous = os.environ.get("TWINMARKET_OFFLINE_LLM")
        os.environ["TWINMARKET_OFFLINE_LLM"] = "1"
        try:
            client = OpenRouterClient(api_key="not-used")
            with self.assertRaisesRegex(RuntimeError, "reject offline"):
                asyncio.run(
                    client.chat_strict_reasoning_off(
                        [{"role": "user", "content": "test"}],
                        policy=self._policy(config.PAPER_REASONING_DISABLED_MODEL),
                        max_tokens=1024,
                        audit_label="test",
                        logical_call_id="id",
                        phase_attempt_id="phase",
                    )
                )
        finally:
            if previous is None:
                os.environ.pop("TWINMARKET_OFFLINE_LLM", None)
            else:
                os.environ["TWINMARKET_OFFLINE_LLM"] = previous

    def test_explicit_run_audit_path_and_context_are_written_locally(self) -> None:
        sent: dict[str, object] = {}

        class FakeCompletions:
            async def create(self, **kwargs: object) -> object:
                sent.update(kwargs)
                return SimpleNamespace(
                    model=config.PAPER_REASONING_DISABLED_MODEL,
                    provider="provider-x",
                    usage={"reasoning_tokens": 0},
                    choices=[],
                )

        @asynccontextmanager
        async def no_op_slot(_: int, *, slot_namespace: str | None = None):
            del slot_namespace
            yield

        with tempfile.TemporaryDirectory() as directory:
            audit_path = Path(directory) / "RN_COMM_OFF" / "openrouter_attempts.jsonl"
            client = OpenRouterClient.__new__(OpenRouterClient)
            client.offline = False
            client.model = config.PAPER_REASONING_DISABLED_MODEL
            client.max_retries = 1
            client.timeout = 1.0
            client.concurrency_limit = 1
            client.slot_namespace = None
            client.audit_path = audit_path
            client.audit_context = {
                "artifact": "rn_ab_strict_openrouter_attempt",
                "condition_id": "RN_COMM_OFF",
                "manifest_sha256": "a" * 64,
                "run_id": "rn-local-audit",
            }
            client.client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
            with patch("twinmarket_kr.llm.client._global_openrouter_slot", no_op_slot):
                asyncio.run(
                    client.chat(
                        [{"role": "user", "content": "local test"}],
                        max_tokens=2048,
                    )
                )

            rows = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["audit_context"], client.audit_context)
        self.assertEqual(rows[0]["reasoning_tokens"], 0)
        self.assertEqual(rows[0]["max_tokens"], 2048)
        self.assertEqual(sent["max_tokens"], 2048)
        self.assertEqual(
            sent["extra_body"]["reasoning"],  # type: ignore[index]
            {"effort": "none", "exclude": True},
        )

    def test_provider_return_is_not_accepted_until_validated_digest_is_bound(self) -> None:
        responses = iter(
            (
                '{"value": "first","value": "duplicate-key-invalid"}',
                '{"value": NaN}',
                '["non-object"]',
                '{"truncated":',
                '{"value": "experiment-accepted"}',
            )
        )

        class FakeCompletions:
            async def create(self, **_kwargs: object) -> object:
                content = next(responses)
                return SimpleNamespace(
                    id=f"request-{hash(content)}",
                    model=config.PAPER_REASONING_DISABLED_MODEL,
                    provider="provider-x",
                    usage={"reasoning_tokens": 0, "total_tokens": 10},
                    choices=[
                        SimpleNamespace(
                            finish_reason="stop",
                            message=SimpleNamespace(content=content),
                        )
                    ],
                )

        @asynccontextmanager
        async def no_op_slot(_: int, *, slot_namespace: str | None = None):
            del slot_namespace
            yield

        with tempfile.TemporaryDirectory() as directory:
            audit_path = Path(directory) / "RN_COMM_OFF" / "openrouter_attempts.jsonl"
            client = OpenRouterClient.__new__(OpenRouterClient)
            client.offline = False
            client.model = config.PAPER_REASONING_DISABLED_MODEL
            client.max_retries = 1
            client.timeout = 1.0
            client.concurrency_limit = 1
            client.slot_namespace = None
            client.audit_path = audit_path
            client.audit_context = {
                "artifact": "rn_ab_strict_openrouter_attempt",
                "condition_id": "RN_COMM_OFF",
                "manifest_sha256": "a" * 64,
                "run_id": "rn-local-audit",
            }
            client.client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
            policy = self._policy(config.PAPER_REASONING_DISABLED_MODEL)
            with patch("twinmarket_kr.llm.client._global_openrouter_slot", no_op_slot):
                # Duplicate-key, NaN, non-object and malformed provider
                # returns all remain auditable physical attempts, but none is
                # allowed to become an experiment acceptance.
                for phase_number in range(1, 5):
                    asyncio.run(
                        client.chat_strict_reasoning_off(
                            [{"role": "user", "content": f"invalid-{phase_number}"}],
                            policy=policy,
                            response_format=dict(RN_STRICT_RESPONSE_FORMAT),
                            max_tokens=1024,
                            audit_label="rn_ab_stage",
                            logical_call_id="same-logical-id",
                            phase_attempt_id=f"phase-{phase_number}",
                            seed=7,
                        )
                    )
                response = asyncio.run(
                    client.chat_strict_reasoning_off(
                        [{"role": "user", "content": "second"}],
                        policy=policy,
                        response_format=dict(RN_STRICT_RESPONSE_FORMAT),
                        max_tokens=1024,
                        audit_label="rn_ab_stage",
                        logical_call_id="same-logical-id",
                        phase_attempt_id="phase-5",
                        seed=7,
                    )
                )
            accepted = {"value": "experiment-accepted"}
            accepted_sha256 = hashlib.sha256(
                json.dumps(
                    accepted,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            self.assertEqual(
                json.loads(response.choices[0].message.content),
                accepted,
            )
            # Simulate a process restart after the durable response journal
            # accepted the response but before the acceptance event was
            # appended.  Recovery binds the existing provider row without a
            # new HTTP request and is idempotent.
            restarted = OpenRouterClient.__new__(OpenRouterClient)
            restarted.audit_path = audit_path
            restarted.audit_context = client.audit_context
            restarted.record_experiment_acceptance(
                logical_call_id="same-logical-id",
                phase_attempt_id="phase-5",
                accepted_response_sha256=accepted_sha256,
            )
            restarted.record_experiment_acceptance(
                logical_call_id="same-logical-id",
                phase_attempt_id="phase-replay",
                accepted_response_sha256=accepted_sha256,
            )
            rows = [
                json.loads(line)
                for line in audit_path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(
            [row["audit_event"] for row in rows],
            ["provider_attempt"] * 5 + ["experiment_acceptance"],
        )
        for invalid_row in rows[:4]:
            self.assertIsNone(invalid_row["accepted_response_sha256"])
            self.assertIsNone(invalid_row["provider_canonical_json_sha256"])
            self.assertIsInstance(invalid_row["provider_response_sha256"], str)
        self.assertEqual(rows[-1]["accepted_response_sha256"], accepted_sha256)
        self.assertEqual(
            validate_reasoning_audit(rows, policy=policy),
            {"attempt_count": 5, "success_count": 1},
        )

    def test_strict_call_requires_positive_max_tokens_and_forwards_it(self) -> None:
        client = OpenRouterClient.__new__(OpenRouterClient)
        client.offline = False
        client.chat = AsyncMock(return_value=SimpleNamespace(choices=[]))
        policy = self._policy(config.PAPER_REASONING_DISABLED_MODEL)

        asyncio.run(
            client.chat_strict_reasoning_off(
                [{"role": "user", "content": "test"}],
                policy=policy,
                response_format=dict(RN_STRICT_RESPONSE_FORMAT),
                max_tokens=3072,
                audit_label="test",
                logical_call_id="id",
                phase_attempt_id="phase",
            )
        )
        self.assertEqual(client.chat.await_args.kwargs["max_tokens"], 3072)
        self.assertEqual(
            client.chat.await_args.kwargs["response_format"],
            dict(RN_STRICT_RESPONSE_FORMAT),
        )

        for invalid in (0, -1, True, 1.5, "1024"):
            with self.subTest(max_tokens=invalid), self.assertRaisesRegex(
                ValueError, "positive integer"
            ):
                asyncio.run(
                    client.chat_strict_reasoning_off(
                        [{"role": "user", "content": "test"}],
                        policy=policy,
                        max_tokens=invalid,  # type: ignore[arg-type]
                        audit_label="test",
                        logical_call_id="id",
                        phase_attempt_id="phase",
                    )
                )

    def test_strict_call_cannot_omit_max_tokens(self) -> None:
        client = OpenRouterClient.__new__(OpenRouterClient)
        client.offline = False
        with self.assertRaisesRegex(TypeError, "max_tokens"):
            asyncio.run(
                client.chat_strict_reasoning_off(  # type: ignore[call-arg]
                    [{"role": "user", "content": "test"}],
                    policy=self._policy(config.PAPER_REASONING_DISABLED_MODEL),
                    audit_label="test",
                    logical_call_id="id",
                    phase_attempt_id="phase",
                )
            )

    def test_strict_call_rejects_unsealed_sampling_or_response_format(self) -> None:
        client = OpenRouterClient.__new__(OpenRouterClient)
        client.offline = False
        client.chat = AsyncMock(return_value=SimpleNamespace(choices=[]))
        policy = self._policy(config.PAPER_REASONING_DISABLED_MODEL)

        for kwargs, expected in (
            ({"response_format": None}, "JSON-object response format"),
            ({"response_format": {"type": "text"}}, "JSON-object response format"),
            (
                {"response_format": dict(RN_STRICT_RESPONSE_FORMAT), "temperature": 0.7},
                "sealed temperature",
            ),
        ):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, expected):
                asyncio.run(
                    client.chat_strict_reasoning_off(
                        [{"role": "user", "content": "test"}],
                        policy=policy,
                        max_tokens=1024,
                        audit_label="test",
                        logical_call_id="id",
                        phase_attempt_id="phase",
                        **kwargs,
                    )
                )
