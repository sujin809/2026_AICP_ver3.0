from __future__ import annotations

import hashlib
import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone

from tests.test_rn_ab_preflight_bundle import RNPreflightBundleTests
from twinmarket_kr.rn_ab.call_policy import RN_REASONING_AUDIT_FIELDS
from twinmarket_kr.rn_ab.execution import (
    RNExecutionFactoryError,
    _cli_main,
    build_local_test_runner,
    build_paired_runner,
    capture_reasoning_off_canary_evidence,
    execution_prerequisites,
    prepare_reasoning_off_canary,
    run_local_audit_path,
    strict_policy_from_context,
    validate_reasoning_off_canary_evidence,
    validate_reasoning_off_canary_plan,
    validate_final_run_reasoning_audits,
    validate_run_local_reasoning_audits,
)
from twinmarket_kr.rn_ab.finalization import inspect_final_handoff_readiness
from twinmarket_kr.rn_ab.journal import LogicalCallKey
from twinmarket_kr.rn_ab.memory import RN_STAGE_SCHEMA_VERSIONS
from twinmarket_kr.rn_ab.run_context import RNRunContext
from twinmarket_kr.rn_ab.spec import RN_COMM_OFF, RN_COMM_ON
from twinmarket_kr.rn_ab.stage_adapter import RN_STAGE_MAX_TOKENS_V1


def _audit_rows(
    context: RNRunContext,
    condition_id: str,
    *,
    logical_call_id: str,
    max_tokens: int,
    accepted_response: dict | None = None,
    phase_attempt_id: str = "phase-test",
    prepend_invalid_provider_return: bool = False,
) -> list[dict]:
    policy = strict_policy_from_context(context)
    request_policy = policy.as_request_policy()
    accepted_response = accepted_response or {"fixture": logical_call_id}
    canonical_response = json.dumps(
        accepted_response,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    response_sha256 = hashlib.sha256(canonical_response.encode()).hexdigest()
    provider_row = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "pid": 123,
        "label": "rn_ab_stage",
        "audit_event": "provider_attempt",
        "status": "provider_returned",
        "requested_model": policy.model,
        "returned_model": policy.model,
        "provider": policy.provider,
        "request_id": f"request-{hashlib.sha256(logical_call_id.encode()).hexdigest()[:16]}",
        "seed": 42,
        "attempt": 1,
        "latency_seconds": 0.1,
        "prompt_sha256": hashlib.sha256(logical_call_id.encode()).hexdigest(),
        "usage": {"reasoning_tokens": 0, "total_tokens": 10},
        "reasoning_tokens": 0,
        "response_reasoning_present": False,
        "finish_reason": "stop",
        "provider_response_sha256": hashlib.sha256(
            canonical_response.encode()
        ).hexdigest(),
        "provider_canonical_json_sha256": response_sha256,
        "accepted_response_sha256": None,
        "provider_attempt_sha256": None,
        "request_policy": request_policy,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "max_tokens": max_tokens,
        "request_policy_sha256": hashlib.sha256(
            json.dumps(request_policy, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest(),
        "logical_call_id": logical_call_id,
        "phase_attempt_id": phase_attempt_id,
        "audit_context": {
            "artifact": "rn_ab_strict_openrouter_attempt",
            "condition_id": condition_id,
            "manifest_sha256": context.manifest_sha256,
            "run_id": context.run_id,
        },
        "error_type": None,
        "error": None,
    }
    acceptance_row = {
        **provider_row,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "audit_event": "experiment_acceptance",
        "status": "accepted",
        "accepted_response_sha256": response_sha256,
        "provider_attempt_sha256": hashlib.sha256(
            json.dumps(
                provider_row,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
    }
    assert set(provider_row) == RN_REASONING_AUDIT_FIELDS
    assert set(acceptance_row) == RN_REASONING_AUDIT_FIELDS
    if not prepend_invalid_provider_return:
        return [provider_row, acceptance_row]
    invalid_raw = '{"duplicate":1,"duplicate":2}'
    invalid_provider_row = {
        **provider_row,
        "phase_attempt_id": f"{phase_attempt_id}-schema-invalid",
        "request_id": f"{provider_row['request_id']}-schema-invalid",
        "provider_response_sha256": hashlib.sha256(invalid_raw.encode()).hexdigest(),
        "provider_canonical_json_sha256": None,
    }
    return [invalid_provider_row, provider_row, acceptance_row]


class RNExecutionFactoryTests(RNPreflightBundleTests):
    """No-network checks for manifest-bound runner construction."""

    def _context(self) -> RNRunContext:
        return RNRunContext.load(self._preflight(run_id="rn-execution-local").run_dir)

    def test_strict_policy_is_derived_from_the_sealed_context(self) -> None:
        context = self._context()

        policy = strict_policy_from_context(context)

        self.assertEqual(policy.model, "qwen/qwen3.5-flash-02-23")
        self.assertEqual(policy.provider, "alibaba")
        self.assertEqual(policy.max_retries, 1)
        self.assertEqual(policy.concurrency, 8)
        self.assertEqual(
            policy.as_request_policy()["reasoning"],
            {"effort": "none", "exclude": True},
        )

    def test_paid_graph_stays_explicitly_no_go_without_approved_dependencies(self) -> None:
        context = self._context()

        prerequisites = execution_prerequisites(context, community_lifecycle=None)

        self.assertFalse(prerequisites.ready_for_paid_execution)
        self.assertIn("validated_reasoning_off_live_telemetry", prerequisites.missing)
        self.assertIn(
            "sealed_two_trading_day_p1_canary_spec_and_validated_community_boundary",
            prerequisites.missing,
        )
        self.assertIn("verified_run_local_community_lifecycle_for_RN_COMM_ON", prerequisites.missing)

        class LocalModel:
            local_only = True

        models = {RN_COMM_OFF: LocalModel(), RN_COMM_ON: LocalModel()}
        with self.assertRaisesRegex(RNExecutionFactoryError, "RNCommunityLifecycleAdapter"):
            build_paired_runner(context, models=models, community_lifecycle=None)  # type: ignore[arg-type]

    def test_local_graph_cannot_be_used_to_smuggle_in_an_unmarked_model(self) -> None:
        context = self._context()

        class UnmarkedModel:
            pass

        models = {RN_COMM_OFF: UnmarkedModel(), RN_COMM_ON: UnmarkedModel()}
        with self.assertRaisesRegex(RNExecutionFactoryError, "local_only=True"):
            build_local_test_runner(context, models=models, community_lifecycle=None)  # type: ignore[arg-type]

    def test_run_local_reasoning_audit_validates_both_arm_logs(self) -> None:
        context = self._context()
        event_id = str(context.event_schedule.events[0]["event_id"])
        for condition_id in (RN_COMM_OFF, RN_COMM_ON):
            logical_call_id = "|".join(
                (
                    context.run_id,
                    condition_id,
                    context.agent_ids[0],
                    event_id,
                    "stb",
                    RN_STAGE_SCHEMA_VERSIONS["stb"],
                )
            )
            rows = _audit_rows(
                context,
                condition_id,
                logical_call_id=logical_call_id,
                max_tokens=RN_STAGE_MAX_TOKENS_V1["stb"],
            )
            run_local_audit_path(context, condition_id).write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )

        self.assertEqual(
            validate_run_local_reasoning_audits(context),
            {
                RN_COMM_OFF: {"attempt_count": 1, "success_count": 1},
                RN_COMM_ON: {"attempt_count": 1, "success_count": 1},
            },
        )

    def test_run_local_reasoning_audit_does_not_use_a_global_log(self) -> None:
        context = self._context()
        # A plausible global/legacy log cannot substitute for the two files
        # owned by this sealed run and its exact arm namespaces.
        (context.run_dir / "openrouter_calls.jsonl").write_text(
            json.dumps({"status": "success"}) + "\n", encoding="utf-8"
        )

        with self.assertRaisesRegex(RNExecutionFactoryError, "audit is missing"):
            validate_run_local_reasoning_audits(context)

    def test_run_local_reasoning_audit_rejects_schema_and_policy_tampering(self) -> None:
        mutations = ("extra_key", "reasoning_token", "policy_hash")
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                context = RNRunContext.load(
                    self._preflight(run_id=f"rn-audit-tamper-{mutation}").run_dir
                )
                event_id = str(context.event_schedule.events[0]["event_id"])
                for condition_id in (RN_COMM_OFF, RN_COMM_ON):
                    logical_call_id = "|".join(
                        (
                            context.run_id,
                            condition_id,
                            context.agent_ids[0],
                            event_id,
                            "stb",
                            RN_STAGE_SCHEMA_VERSIONS["stb"],
                        )
                    )
                    rows = _audit_rows(
                        context,
                        condition_id,
                        logical_call_id=logical_call_id,
                        max_tokens=RN_STAGE_MAX_TOKENS_V1["stb"],
                    )
                    if condition_id == RN_COMM_ON:
                        if mutation == "extra_key":
                            rows[-1]["invented_telemetry"] = "not allowed"
                        elif mutation == "reasoning_token":
                            rows[-1]["reasoning_tokens"] = 1
                        else:
                            rows[-1]["request_policy_sha256"] = "0" * 64
                    run_local_audit_path(context, condition_id).write_text(
                        "".join(json.dumps(row) + "\n" for row in rows),
                        encoding="utf-8",
                    )
                with self.assertRaises(RNExecutionFactoryError):
                    validate_run_local_reasoning_audits(context)

    def test_offline_telemetry_plan_is_sealed_and_explicitly_not_final_p1(self) -> None:
        context = self._context()

        prepared = prepare_reasoning_off_canary(context)
        plan = validate_reasoning_off_canary_plan(context)

        self.assertEqual(prepared.expected_live_request_count, 24)
        self.assertEqual(plan["mode"], "plan_only_no_network_no_paid_api")
        self.assertFalse(plan["execution_authorized"])
        self.assertEqual(plan["readiness"]["network_requests_made_by_plan"], 0)
        self.assertEqual(plan["readiness"]["paid_api_calls_made_by_plan"], 0)
        self.assertEqual(
            plan["canary_scope"]["classification"],
            "reasoning_off_telemetry_only_not_final_two_trading_day_p1_canary",
        )
        self.assertIn("not_final", plan["canary_scope"]["classification"])
        output = io.StringIO()
        with redirect_stdout(output):
            status = _cli_main(
                [
                    "--run-dir",
                    str(context.run_dir),
                    "--validate-canary-plan",
                ]
            )
        self.assertEqual(status, 0)
        cli = json.loads(output.getvalue())
        self.assertEqual(cli["status"], "PLAN_VALID_NO_NETWORK_NO_PAID_API")
        self.assertFalse(cli["execution_ready"])
        self.assertEqual(cli["paid_api_calls"], 0)
        self.assertIn(
            "final_two_trading_day_P1_canary_is_a_separate_gate",
            cli["blocking_requirements"],
        )

    def test_live_evidence_requires_exact_real_rows_and_survives_later_append(self) -> None:
        context = self._context()
        prepare_reasoning_off_canary(context)
        event_id = str(context.event_schedule.events[0]["event_id"])
        for condition_id in (RN_COMM_OFF, RN_COMM_ON):
            rows = []
            for agent_id in context.agent_ids:
                for stage, schema_version in RN_STAGE_SCHEMA_VERSIONS.items():
                    logical_call_id = "|".join(
                        (
                            context.run_id,
                            condition_id,
                            agent_id,
                            event_id,
                            stage,
                            schema_version,
                        )
                    )
                    rows.extend(
                        _audit_rows(
                            context,
                            condition_id,
                            logical_call_id=logical_call_id,
                            max_tokens=RN_STAGE_MAX_TOKENS_V1[stage],
                        )
                    )
            run_local_audit_path(context, condition_id).write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )

        evidence = capture_reasoning_off_canary_evidence(context)
        validated = validate_reasoning_off_canary_evidence(context)
        self.assertEqual(validated.evidence_sha256, evidence.evidence_sha256)
        self.assertEqual(validated.summaries[RN_COMM_OFF]["success_count"], 12)

        # A later full-run row may append to the same run-local audit without
        # changing any row that the telemetry evidence already sealed.
        later_event_id = str(context.event_schedule.events[1]["event_id"])
        for condition_id in (RN_COMM_OFF, RN_COMM_ON):
            logical_call_id = "|".join(
                (
                    context.run_id,
                    condition_id,
                    context.agent_ids[0],
                    later_event_id,
                    "stb",
                    RN_STAGE_SCHEMA_VERSIONS["stb"],
                )
            )
            with run_local_audit_path(context, condition_id).open(
                "a", encoding="utf-8"
            ) as handle:
                handle.write(
                    "".join(
                        json.dumps(row) + "\n"
                        for row in _audit_rows(
                            context,
                            condition_id,
                            logical_call_id=logical_call_id,
                            max_tokens=RN_STAGE_MAX_TOKENS_V1["stb"],
                        )
                    )
                )
        self.assertEqual(
            validate_reasoning_off_canary_evidence(context).evidence_sha256,
            evidence.evidence_sha256,
        )

        prerequisites = execution_prerequisites(context, community_lifecycle=None)
        self.assertNotIn(
            "validated_reasoning_off_live_telemetry",
            prerequisites.missing,
        )
        self.assertIn(
            "sealed_two_trading_day_p1_canary_spec_and_validated_community_boundary",
            prerequisites.missing,
        )

    def test_live_evidence_is_not_created_from_missing_or_tampered_telemetry(self) -> None:
        context = self._context()
        prepared = prepare_reasoning_off_canary(context)
        with self.assertRaisesRegex(RNExecutionFactoryError, "audit is missing"):
            capture_reasoning_off_canary_evidence(context)
        evidence_path = context.run_dir / "REASONING_OFF_TELEMETRY_EVIDENCE.json"
        self.assertFalse(evidence_path.exists())

        # A plan remains an offline preparation artifact, not telemetry.
        plan = json.loads(prepared.plan_path.read_text(encoding="utf-8"))
        self.assertEqual(plan["readiness"]["status"], "NO_GO_LIVE_TELEMETRY_MISSING")

        handoff = inspect_final_handoff_readiness(context.run_dir)
        self.assertEqual(handoff.status, "NO_GO")
        self.assertFalse(handoff.reasoning_off_live_telemetry_validated)
        self.assertFalse(handoff.final_two_trading_day_p1_validated)
        self.assertIn(
            "validated_two_trading_day_U4_100_agent_P1_community_boundary_evidence",
            handoff.missing,
        )
        (context.run_dir / "P1_CANARY_EVIDENCE.json").write_text(
            json.dumps({"status": "PASS"}),
            encoding="utf-8",
        )
        still_blocked = inspect_final_handoff_readiness(context.run_dir)
        self.assertFalse(still_blocked.final_two_trading_day_p1_validated)
        self.assertIn(
            "validated_two_trading_day_U4_100_agent_P1_community_boundary_evidence",
            still_blocked.missing,
        )

    def test_final_reasoning_handoff_requires_exact_audit_to_committed_journal_set(self) -> None:
        context = RNRunContext.load(
            self._preflight(run_id="rn-final-reasoning-handoff").run_dir
        )
        for condition_id in (RN_COMM_OFF, RN_COMM_ON):
            journal = context.open_journal(condition_id)
            rows = []
            committed_ids = []
            for event in context.event_schedule.events:
                event_id = str(event["event_id"])
                for agent_id in context.agent_ids:
                    for stage, schema_version in RN_STAGE_SCHEMA_VERSIONS.items():
                        key = LogicalCallKey(
                            context.run_id,
                            condition_id,
                            agent_id,
                            event_id,
                            stage,
                            schema_version,
                        )
                        logical_call_id = key.value()
                        journal.begin_attempt(
                            key,
                            {"sealed": logical_call_id},
                            phase_attempt_id=f"phase-{len(committed_ids)}",
                            attempt_number=1,
                        )
                        journal.record_success(
                            logical_call_id,
                            {"accepted": logical_call_id},
                            phase_attempt_id=f"phase-{len(committed_ids)}",
                            attempt_number=1,
                        )
                        committed_ids.append(logical_call_id)
                        rows.extend(
                            _audit_rows(
                                context,
                                condition_id,
                                logical_call_id=logical_call_id,
                                max_tokens=RN_STAGE_MAX_TOKENS_V1[stage],
                                accepted_response={"accepted": logical_call_id},
                                phase_attempt_id=f"phase-{len(committed_ids) - 1}",
                                prepend_invalid_provider_return=(
                                    len(committed_ids) == 1
                                ),
                            )
                        )
            journal.mark_committed(committed_ids)
            run_local_audit_path(context, condition_id).write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )

        summary = validate_final_run_reasoning_audits(context)
        expected_per_arm = (
            len(context.agent_ids)
            * len(context.event_schedule.events)
            * len(RN_STAGE_SCHEMA_VERSIONS)
        )
        self.assertEqual(
            summary[RN_COMM_OFF]["committed_success_count"],
            expected_per_arm,
        )
        self.assertEqual(
            summary[RN_COMM_OFF]["attempt_count"],
            expected_per_arm + 1,
        )

        off_path = run_local_audit_path(context, RN_COMM_OFF)
        off_text = off_path.read_text(encoding="utf-8")
        off_rows = [json.loads(line) for line in off_text.splitlines()]
        target_logical_id = next(
            str(row["logical_call_id"])
            for row in off_rows
            if row["audit_event"] == "experiment_acceptance"
        )
        target_stage = target_logical_id.split("|")[4]
        replacement = _audit_rows(
            context,
            RN_COMM_OFF,
            logical_call_id=target_logical_id,
            max_tokens=RN_STAGE_MAX_TOKENS_V1[target_stage],
            accepted_response={"different": "but internally valid audit response"},
            phase_attempt_id="phase-digest-mismatch",
        )
        off_path.write_text(
            "".join(
                json.dumps(row) + "\n"
                for row in [
                    *(
                        row
                        for row in off_rows
                        if row["logical_call_id"] != target_logical_id
                    ),
                    *replacement,
                ]
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            RNExecutionFactoryError,
            "accepted-response digest differs from committed journal",
        ):
            validate_final_run_reasoning_audits(context)
        off_path.write_text(off_text, encoding="utf-8")

        on_path = run_local_audit_path(context, RN_COMM_ON)
        on_rows = on_path.read_text(encoding="utf-8").splitlines()
        on_path.write_text("\n".join(on_rows[:-1]) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(
            RNExecutionFactoryError,
            "live audit/journal mismatch",
        ):
            validate_final_run_reasoning_audits(context)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
