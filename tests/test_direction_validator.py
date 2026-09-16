from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "validation" / "validate_trading_direction.py"
SPEC = importlib.util.spec_from_file_location("validate_trading_direction", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


class DirectionValidatorTests(unittest.TestCase):
    def test_cli_help_resolves_project_imports_from_validation_directory(self) -> None:
        result = subprocess.run(
            [sys.executable, str(MODULE_PATH), "--help"],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--output-dir", result.stdout)

    def test_expected_calendar_does_not_silently_intersect(self) -> None:
        actual = {
            "2026-03-02": {
                "Individuals": 1.0,
                "Subtotal-Institutions": 0.0,
                "Total of foreign": 0.0,
                "Other corporations": 0.0,
            }
        }
        simulation = {
            "2026-03-02": {
                "llm_value": 1.0,
                "market_return": 0.0,
            }
        }
        with self.assertRaisesRegex(ValueError, "exact approved-date coverage"):
            validator.build_comparison_rows(
                label="value",
                actual=actual,
                simulation=simulation,
                expected_dates=["2026-03-02", "2026-03-03"],
            )

    def test_lag_baseline_is_built_before_burn_in_mask(self) -> None:
        full_rows = [
            {"date": "2026-03-02", "Individuals": -10.0, "market_return": -0.1},
            {"date": "2026-03-03", "Individuals": 20.0, "market_return": 0.1},
            {"date": "2026-03-04", "Individuals": -30.0, "market_return": -0.1},
            {"date": "2026-03-05", "Individuals": -40.0, "market_return": 0.1},
        ]
        evaluation_rows = [
            {
                **full_rows[-1],
                "llm_net": -1.0,
                "llm_direction": "net_sell",
                "llm_matches_individuals": 1,
            }
        ]
        baselines = validator._lag_baselines_for_evaluation(
            full_rows=full_rows,
            evaluation_rows=evaluation_rows,
        )
        previous_actual = baselines["previous_day_individual_direction"]
        self.assertEqual(previous_actual["direction_match_rate"], 1.0)
        self.assertEqual(
            previous_actual["predicted_direction_counts"],
            {"buy": 0, "sell": 1, "flat": 0},
        )


if __name__ == "__main__":
    unittest.main()
