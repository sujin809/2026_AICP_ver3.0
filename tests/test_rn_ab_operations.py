from __future__ import annotations

import asyncio
import io
import json
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.test_rn_ab_preflight_bundle import RNPreflightBundleTests
from twinmarket_kr.rn_ab.execution import (
    RNReasoningOffTelemetryRunner,
    prepare_reasoning_off_canary,
)
from twinmarket_kr.rn_ab.operations import (
    RNOperationalError,
    _require_incomplete_run_state,
    main,
)
from twinmarket_kr.rn_ab.p1 import (
    RNP1ValidationError,
    _validate_recovery_evidence,
    assert_p1_candidate_context,
    write_p1_recovery_evidence,
)
from twinmarket_kr.rn_ab.run_context import RNRunContext


class _FakePaidRunner:
    def __init__(self, *, on_run: object | None = None) -> None:
        self.on_run = on_run
        self.calls = 0

    async def run(self) -> object:
        self.calls += 1
        if callable(self.on_run):
            self.on_run()
        return SimpleNamespace(
            evidence_path=Path("/tmp/fake-evidence.json"),
            evidence_sha256="a" * 64,
            summaries={"RN_COMM_OFF": {}, "RN_COMM_ON": {}},
        )

    async def run_all(self) -> object:
        self.calls += 1
        return SimpleNamespace(
            completed_phases=(object(), object()),
            right_censored_outcome_count=4,
        )


class RNOperationsTests(RNPreflightBundleTests):
    def _context(self, run_id: str) -> RNRunContext:
        return RNRunContext.load(self._preflight(run_id=run_id).run_dir)

    def test_prepare_command_cannot_construct_a_live_client(self) -> None:
        context = self._context("rn-ops-prepare")
        output = io.StringIO()
        with patch(
            "twinmarket_kr.rn_ab.operations.build_live_stage_models",
            side_effect=AssertionError("read-only command reached live client construction"),
        ), redirect_stdout(output):
            status = main(
                [
                    "prepare-telemetry",
                    "--run-dir",
                    str(context.run_dir),
                ]
            )
        self.assertEqual(status, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "PREPARED_NO_NETWORK_NO_PAID_API")
        self.assertEqual(result["network_requests"], 0)
        self.assertEqual(result["paid_api_calls"], 0)

    def test_paid_command_requires_flag_and_exact_run_id_before_live_construction(self) -> None:
        context = self._context("rn-ops-auth")
        prepare_reasoning_off_canary(context)
        cases = (
            ["--confirm-run-id", context.run_id],
            [
                "--authorize-paid-api-calls",
                "--confirm-run-id",
                "another-run",
            ],
        )
        for suffix in cases:
            with self.subTest(arguments=suffix):
                output = io.StringIO()
                with patch(
                    "twinmarket_kr.rn_ab.operations.build_live_stage_models",
                    side_effect=AssertionError("authorization failure reached live construction"),
                ), redirect_stdout(output):
                    status = main(
                        [
                            "telemetry",
                            "--run-dir",
                            str(context.run_dir),
                            *suffix,
                        ]
                    )
                self.assertEqual(status, 2)
                self.assertEqual(json.loads(output.getvalue())["status"], "NO_GO")

    def test_telemetry_bootstraps_before_first_event(self) -> None:
        context = self._context("rn-ops-bootstrap")
        plan = prepare_reasoning_off_canary(context)
        events: list[str] = []

        class UnderlyingRunner:
            async def _bootstrap_phase(self) -> None:
                events.append("bootstrap")

            async def run_event(self, *, event_id: str) -> None:
                events.append(f"event:{event_id}")

        expected_evidence = SimpleNamespace(evidence_sha256="b" * 64)
        runner = RNReasoningOffTelemetryRunner(
            _context=context,
            _runner=UnderlyingRunner(),  # type: ignore[arg-type]
            plan_path=plan.plan_path,
            event_id=str(context.event_schedule.events[0]["event_id"]),
        )
        with patch(
            "twinmarket_kr.rn_ab.execution.capture_reasoning_off_canary_evidence",
            return_value=expected_evidence,
        ):
            actual = asyncio.run(runner.run())
        self.assertIs(actual, expected_evidence)
        self.assertEqual(
            events,
            [
                "bootstrap",
                f"event:{context.event_schedule.events[0]['event_id']}",
            ],
        )

    def test_successful_telemetry_prefix_can_continue_into_p1_without_network(self) -> None:
        context = self._context("rn-ops-prefix")
        prepare_reasoning_off_canary(context)
        first_phase = f"{context.event_schedule.events[0]['event_id']}:scientific_turn"

        def write_prefix() -> None:
            (context.run_dir / "rn_phase_checkpoint.json").write_text(
                json.dumps(
                    {
                        "version": "rn-paired-phase-v1",
                        "manifest_sha256": context.manifest_sha256,
                        "completed_phases": [
                            {"phase_id": "bootstrap_ltb0"},
                            {"phase_id": first_phase},
                        ],
                        "inflight_phase": None,
                    }
                ),
                encoding="utf-8",
            )

        telemetry_runner = _FakePaidRunner(on_run=write_prefix)
        output = io.StringIO()
        with patch(
            "twinmarket_kr.rn_ab.operations._live_dependencies",
            return_value=({}, object()),
        ), patch(
            "twinmarket_kr.rn_ab.operations.build_reasoning_off_telemetry_runner",
            return_value=telemetry_runner,
        ), redirect_stdout(output):
            status = main(
                [
                    "telemetry",
                    "--run-dir",
                    str(context.run_dir),
                    "--authorize-paid-api-calls",
                    "--confirm-run-id",
                    context.run_id,
                ]
            )
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "LIVE_TELEMETRY_VALIDATED")

        p1_runner = _FakePaidRunner()
        output = io.StringIO()
        with patch(
            "twinmarket_kr.rn_ab.operations.assert_p1_candidate_context"
        ), patch(
            "twinmarket_kr.rn_ab.operations.validate_reasoning_off_canary_evidence"
        ), patch(
            "twinmarket_kr.rn_ab.operations._live_dependencies",
            return_value=({}, object()),
        ), patch(
            "twinmarket_kr.rn_ab.operations.build_p1_canary_runner",
            return_value=p1_runner,
        ), redirect_stdout(output):
            status = main(
                [
                    "run-p1",
                    "--run-dir",
                    str(context.run_dir),
                    "--authorize-paid-api-calls",
                    "--confirm-run-id",
                    context.run_id,
                ]
            )
        self.assertEqual(status, 0)
        self.assertEqual(
            json.loads(output.getvalue())["status"],
            "P1_RUN_COMPLETE_LOCAL_FINALIZATION_REQUIRED",
        )
        self.assertEqual(p1_runner.calls, 1)

    def test_resume_rejects_missing_and_completed_checkpoints(self) -> None:
        context = self._context("rn-ops-resume-state")
        with self.assertRaisesRegex(RNOperationalError, "existing phase checkpoint"):
            _require_incomplete_run_state(context, None)
        completed = {
            "completed_phases": [{"phase_id": "finalize:right_censor_outcomes"}],
            "inflight_phase": None,
        }
        with self.assertRaisesRegex(RNOperationalError, "already complete"):
            _require_incomplete_run_state(context, completed)

    def test_three_agent_fixture_cannot_pose_as_p1(self) -> None:
        context = self._context("rn-ops-not-p1")
        with self.assertRaisesRegex(RNP1ValidationError, "exactly 100"):
            assert_p1_candidate_context(context)

    def test_p1_shape_and_recovery_proof_are_derived_from_real_checkpoints(self) -> None:
        context = self._context("rn-ops-recovery-proof")
        phase_ids = [
            "bootstrap_ltb0",
            *[
                f"{event['event_id']}:scientific_turn"
                for event in context.event_schedule.events
            ],
            "finalize:right_censor_outcomes",
        ]
        completed = [
            {
                "phase_id": phase_id,
                "work_item_count": (
                    2 if phase_id == "finalize:right_censor_outcomes"
                    else 2 * len(context.agent_ids)
                ),
            }
            for phase_id in phase_ids
        ]
        final_checkpoint = {
            "version": "rn-paired-phase-v1",
            "manifest_sha256": context.manifest_sha256,
            "completed_phases": completed,
            "inflight_phase": None,
        }
        (context.run_dir / "rn_phase_checkpoint.json").write_text(
            json.dumps(final_checkpoint, sort_keys=True),
            encoding="utf-8",
        )
        pre_checkpoint = {
            "version": "rn-paired-phase-v1",
            "manifest_sha256": context.manifest_sha256,
            "completed_phases": completed[:2],
            "inflight_phase": None,
            "paused_phase": {
                "phase_id": phase_ids[2],
                "phase_attempt_id": "attempt-paused",
                "attempt_number": 1,
            },
        }
        with patch("twinmarket_kr.rn_ab.p1.P1_REQUIRED_AGENT_COUNT", 3):
            assert_p1_candidate_context(context)
            path = write_p1_recovery_evidence(
                context,
                pre_resume_checkpoint=pre_checkpoint,
            )
            self.assertTrue(path.is_file())
            self.assertEqual(
                _validate_recovery_evidence(context),
                "retry_paused_phase",
            )

        final_checkpoint["completed_phases"] = completed[:-1]
        (context.run_dir / "rn_phase_checkpoint.json").write_text(
            json.dumps(final_checkpoint, sort_keys=True),
            encoding="utf-8",
        )
        with self.assertRaises(RNP1ValidationError):
            _validate_recovery_evidence(context)

    def test_p1_rejects_a_missing_final_pm_community_phase(self) -> None:
        """The final PM phase is authored even though its Best is right-censored."""

        context = self._context("rn-ops-final-pm-community-phase")
        truncated_calendar = replace(
            context.resolved.calendar,
            community_phases=context.resolved.calendar.community_phases[:-1],
        )
        truncated_context = replace(
            context,
            resolved=replace(context.resolved, calendar=truncated_calendar),
        )
        with patch("twinmarket_kr.rn_ab.p1.P1_REQUIRED_AGENT_COUNT", 3):
            with self.assertRaisesRegex(RNP1ValidationError, "follow every PM"):
                assert_p1_candidate_context(truncated_context)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
