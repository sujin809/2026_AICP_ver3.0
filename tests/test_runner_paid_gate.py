from __future__ import annotations

import importlib.util
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_runner():
    path = PROJECT_ROOT / "scripts" / "05_run_simulation.py"
    spec = importlib.util.spec_from_file_location(
        "integrated_simulation_runner_paid_gate",
        path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import runner: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RunnerPaidGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = _load_runner()
        cls.profile = SimpleNamespace(per_arm_concurrency=8)

    @staticmethod
    def _args(
        *,
        allow_paid_api: bool = False,
        audit: Path | None = None,
    ) -> Namespace:
        return Namespace(
            allow_paid_api=allow_paid_api,
            reasoning_off_canary_audit=audit,
        )

    def test_offline_execution_needs_no_paid_authorization(self) -> None:
        result = self.runner._validate_live_execution_gate(
            self._args(),
            offline_llm=True,
            study_profile=self.profile,
        )
        self.assertIsNone(result)

    def test_offline_execution_rejects_live_canary_evidence(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "live execution only",
        ):
            self.runner._validate_live_execution_gate(
                self._args(audit=Path("unused.jsonl")),
                offline_llm=True,
                study_profile=self.profile,
            )

    def test_live_execution_rejects_missing_paid_authorization(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "--allow-paid-api",
        ):
            self.runner._validate_live_execution_gate(
                self._args(),
                offline_llm=False,
                study_profile=self.profile,
            )

    def test_live_execution_rejects_missing_reasoning_canary(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "--reasoning-off-canary-audit",
        ):
            self.runner._validate_live_execution_gate(
                self._args(allow_paid_api=True),
                offline_llm=False,
                study_profile=self.profile,
            )

    def test_invalid_canary_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            audit = Path(directory) / "canary.jsonl"
            audit.write_text(
                '{"status":"success","reasoning_tokens":1}\n',
                encoding="utf-8",
            )
            with self.assertRaises(Exception):
                self.runner._validate_live_execution_gate(
                    self._args(
                        allow_paid_api=True,
                        audit=audit,
                    ),
                    offline_llm=False,
                    study_profile=self.profile,
                )

    def test_parser_defaults_to_no_paid_authorization(self) -> None:
        args = self.runner.build_parser().parse_args([])
        self.assertFalse(args.allow_paid_api)
        self.assertIsNone(args.reasoning_off_canary_audit)
        self.assertIsNone(args.capture_reasoning_off_canary)


if __name__ == "__main__":
    unittest.main()
