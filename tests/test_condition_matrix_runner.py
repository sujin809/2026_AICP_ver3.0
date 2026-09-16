from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_runner():
    path = PROJECT_ROOT / "scripts" / "08_run_six_conditions.py"
    spec = importlib.util.spec_from_file_location(
        "integrated_condition_matrix_runner",
        path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import runner: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ConditionMatrixRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = _load_runner()

    def _args(self, *extra: str):
        return self.runner.build_parser().parse_args(
            [
                "--start-date",
                "2026-02-27",
                "--end-date",
                "2026-05-04",
                *extra,
            ]
        )

    def test_default_matrix_contains_only_real_news_baseline(self) -> None:
        conditions = self.runner._selected_conditions(self._args())
        self.assertEqual(
            [condition.condition_id for condition in conditions],
            ["RN_COMM_OFF", "RN_COMM_ON"],
        )
        self.assertEqual(
            {condition.news_treatment for condition in conditions},
            {"real_only"},
        )

    def test_baseline_command_uses_only_canonical_05_runner(self) -> None:
        args = self._args(
            "--allow-paid-api",
            "--reasoning-off-canary-audit",
            "/private/tmp/canary.jsonl",
        )
        condition = self.runner.CONDITION_BY_ID["RN_COMM_ON"]
        command = self.runner._build_condition_command(
            condition=condition,
            profile_root=Path("/sealed/profile"),
            run_dir=Path("/runs/RN_COMM_ON"),
            args=args,
            resume=False,
        )
        self.assertEqual(
            Path(command[1]).name,
            "05_run_simulation.py",
        )
        self.assertNotIn("05_run_simulation_checkpointed.py", command)
        self.assertNotIn("--chunk-days", command)
        self.assertNotIn("--fake-news-mode", command)
        self.assertNotIn("--fake-news-variant", command)
        self.assertNotIn("--max-agents", command)
        self.assertIn("--allow-paid-api", command)
        self.assertIn("--reasoning-off-canary-audit", command)
        self.assertEqual(command.count("--start-date"), 1)
        start_index = command.index("--start-date")
        self.assertEqual(command[start_index + 1], args.start_date)

    def test_resume_reuses_same_canonical_command(self) -> None:
        args = self._args()
        condition = self.runner.CONDITION_BY_ID["RN_COMM_OFF"]
        command = self.runner._build_condition_command(
            condition=condition,
            profile_root=Path("/sealed/profile"),
            run_dir=Path("/runs/RN_COMM_OFF"),
            args=args,
            resume=True,
        )
        self.assertEqual(command.count("--resume"), 1)
        self.assertEqual(
            Path(command[1]).name,
            "05_run_simulation.py",
        )

    def test_fake_condition_requires_explicit_sealed_profile(self) -> None:
        args = self._args(
            "--conditions",
            "REAL_PLUS_BEARISH_FAKE_COMM_OFF",
        )
        condition = self.runner._selected_conditions(args)[0]
        with self.assertRaisesRegex(
            RuntimeError,
            "--bearish-profile-root",
        ):
            self.runner._profile_root(args, condition)

    def test_fake_profile_must_declare_expected_treatment(self) -> None:
        condition = self.runner.CONDITION_BY_ID[
            "REAL_PLUS_BULLISH_FAKE_COMM_ON"
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "study_spec.json").write_text(
                json.dumps(
                    {
                        "condition_treatments": {
                            "RN_COMM_ON": {
                                "community_mode": "on",
                                "news_treatment": "real_only",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                RuntimeError,
                "sealed news_treatment",
            ):
                self.runner._validate_condition_profile(
                    condition=condition,
                    root=root,
                    full_agents=[],
                )

    def test_matrix_restarts_only_checkpoint_classified_transient_pause(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paused = root / "paused.json"
            checkpoint = root / "checkpoint.json"
            paused.write_text(
                json.dumps(
                    {
                        "status": "paused",
                        "event_id": "2026-02-27/AM",
                        "error_type": "RuntimeError",
                        "restart_command": "rerun with --resume",
                    }
                ),
                encoding="utf-8",
            )
            checkpoint.write_text(
                json.dumps(
                    {
                        "status": "paused",
                        "last_error": {
                            "event_id": "2026-02-27/AM",
                            "auto_restart_allowed": True,
                            "failure_class": "transient_provider_or_transport",
                            "exception_chain": ["RuntimeError", "TimeoutError"],
                            "matched_markers": ["TimeoutError"],
                        },
                    }
                ),
                encoding="utf-8",
            )
            decision = self.runner._restart_decision_from_checkpoint(
                paused_path=paused,
                checkpoint_path=checkpoint,
                condition_id="RN_COMM_OFF",
            )
        self.assertTrue(decision["auto_restart_allowed"])
        self.assertEqual(
            decision["failure_class"],
            "transient_provider_or_transport",
        )

    def test_matrix_does_not_restart_deterministic_pause(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paused = root / "paused.json"
            checkpoint = root / "checkpoint.json"
            paused.write_text(
                json.dumps(
                    {
                        "status": "paused",
                        "event_id": "2026-02-27/PM",
                        "error_type": "AnalysisValidationError",
                        "restart_command": "rerun with --resume",
                    }
                ),
                encoding="utf-8",
            )
            checkpoint.write_text(
                json.dumps(
                    {
                        "status": "paused",
                        "last_error": {
                            "event_id": "2026-02-27/PM",
                            "auto_restart_allowed": False,
                            "failure_class": "deterministic_validation_or_model",
                            "exception_chain": ["AnalysisValidationError"],
                            "matched_markers": [
                                "AnalysisValidationError"
                            ],
                        },
                    }
                ),
                encoding="utf-8",
            )
            decision = self.runner._restart_decision_from_checkpoint(
                paused_path=paused,
                checkpoint_path=checkpoint,
                condition_id="RN_COMM_ON",
            )
        self.assertFalse(decision["auto_restart_allowed"])
        self.assertEqual(
            decision["failure_class"],
            "deterministic_validation_or_model",
        )

    def test_matrix_rejects_pause_not_bound_to_checkpoint_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paused = root / "paused.json"
            checkpoint = root / "checkpoint.json"
            paused.write_text(
                json.dumps(
                    {
                        "status": "paused",
                        "event_id": "2026-02-27/AM",
                        "error_type": "TimeoutError",
                        "restart_command": "rerun with --resume",
                    }
                ),
                encoding="utf-8",
            )
            checkpoint.write_text(
                json.dumps(
                    {
                        "status": "paused",
                        "last_error": {
                            "event_id": "2026-02-27/PM",
                            "auto_restart_allowed": True,
                            "failure_class": "transient_provider_or_transport",
                        },
                    }
                ),
                encoding="utf-8",
            )
            decision = self.runner._restart_decision_from_checkpoint(
                paused_path=paused,
                checkpoint_path=checkpoint,
                condition_id="RN_COMM_OFF",
            )
        self.assertFalse(decision["auto_restart_allowed"])
        self.assertEqual(
            decision["failure_class"],
            "invalid_or_unbound_canonical_pause",
        )

    def test_current_real_profile_is_accepted_for_both_arms(self) -> None:
        args = self._args()
        agents = self.runner.select_simulation_agents(None)
        for condition in self.runner._selected_conditions(args):
            root = self.runner._profile_root(args, condition)
            profile = self.runner._validate_condition_profile(
                condition=condition,
                root=root,
                full_agents=agents,
            )
            self.assertEqual(profile.required_agent_count, 100)
            self.assertEqual(profile.condition_id, condition.condition_id)


if __name__ == "__main__":
    unittest.main()
