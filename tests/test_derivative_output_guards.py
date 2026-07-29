from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VALIDATION = load_module(
    "aicp_direction_validation",
    PROJECT_ROOT / "validation" / "validate_trading_direction.py",
)
RUN_REPORT = load_module(
    "aicp_run_report",
    SCRIPTS_DIR / "generate_run_report_pdf.py",
)
COMMUNITY_REPORT = load_module(
    "aicp_community_report",
    SCRIPTS_DIR / "generate_community_report_pdf.py",
)
RUN_VALIDATOR = load_module(
    "aicp_run_validator",
    SCRIPTS_DIR / "99_validate.py",
)


class DerivativeOutputGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.run_dir = self.root / "RN_COMM_OFF"
        self.run_dir.mkdir()
        self.derived_dir = self.root / "derived" / "RN_COMM_OFF"

    @staticmethod
    def _guards():
        return (
            VALIDATION.require_external_output_dir,
            RUN_REPORT.require_external_output,
            COMMUNITY_REPORT.require_external_output,
            RUN_VALIDATOR.require_external_output,
        )

    def test_external_derivative_destinations_are_allowed(self) -> None:
        for guard in self._guards():
            self.assertEqual(
                guard(self.derived_dir, self.run_dir),
                self.derived_dir.resolve(),
            )

    def test_run_local_derivative_destinations_are_rejected(self) -> None:
        for guard in self._guards():
            for target in (self.run_dir, self.run_dir / "reports" / "result.pdf"):
                with self.assertRaises((RuntimeError, ValueError)):
                    guard(target, self.run_dir)

    def test_symlink_into_run_is_rejected(self) -> None:
        alias = self.root / "run-alias"
        alias.symlink_to(self.run_dir, target_is_directory=True)
        for guard in self._guards():
            with self.assertRaises((RuntimeError, ValueError)):
                guard(alias / "report.pdf", self.run_dir)

    def test_direction_validator_gates_before_creating_derivative_output(self) -> None:
        args = Namespace(
            skip_initial_days=0,
            run_dir=self.run_dir,
        )
        with (
            patch.object(VALIDATION, "parse_args", return_value=args),
            patch.object(
                VALIDATION,
                "require_publication_ready_run",
                side_effect=RuntimeError("run is not publication ready"),
            ) as publication_gate,
            self.assertRaisesRegex(RuntimeError, "not publication ready"),
        ):
            VALIDATION.main()
        publication_gate.assert_called_once_with(self.run_dir)
        self.assertFalse(self.derived_dir.exists())

    def test_run_report_requires_initial_values_from_canonical_database(self) -> None:
        database = self.run_dir / "canonical.db"
        with sqlite3.connect(database) as connection:
            connection.executescript(
                """
                CREATE TABLE simulation_fills (
                    agent_id TEXT,
                    pre_portfolio_json TEXT,
                    turn INTEGER
                );
                INSERT INTO simulation_fills
                VALUES ('A001', '{"cash": 123456789}', 1);
                """
            )

        self.assertEqual(
            RUN_REPORT.load_initial_values(["A001"], run_db=database),
            {"A001": 123456789.0},
        )
        with self.assertRaisesRegex(RuntimeError, "A002"):
            RUN_REPORT.load_initial_values(["A001", "A002"], run_db=database)
        with self.assertRaisesRegex(RuntimeError, "canonical run-local database"):
            RUN_REPORT.load_initial_values(
                ["A001"],
                run_db=self.run_dir / "missing.db",
            )


if __name__ == "__main__":
    unittest.main()
