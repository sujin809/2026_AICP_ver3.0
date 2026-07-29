from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import config


def _load_script_module():
    path = config.PROJECT_ROOT / "scripts" / "01_build_persona.py"
    spec = importlib.util.spec_from_file_location("build_persona_script", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load script: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PersonaBuildScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = _load_script_module()

    def test_default_mode_validates_without_modifying_baseline(self) -> None:
        before = _sha256(config.SYS_100_DB)
        with contextlib.redirect_stdout(io.StringIO()):
            self.script.main([])
        self.assertEqual(_sha256(config.SYS_100_DB), before)

    def test_sealed_baseline_validation_detects_depth_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            drifted = Path(directory) / "sys_100.db"
            shutil.copyfile(config.SYS_100_DB, drifted)
            with sqlite3.connect(drifted) as connection:
                connection.execute(
                    "UPDATE agents SET news_depth = 2 WHERE agent_id = 'A001'"
                )
                connection.commit()
            with self.assertRaises(RuntimeError):
                self.script.validate_existing_baseline(drifted)

    def test_new_cohort_cannot_overwrite_frozen_baseline(self) -> None:
        with self.assertRaises(ValueError):
            self.script.build_new_cohort(config.SYS_100_DB)

    def test_explicit_new_cohort_uses_separate_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "new_cohort.db"
            result = self.script.build_new_cohort(output)
            report = output.with_suffix(".validation.json")

            self.assertTrue(output.exists())
            self.assertTrue(report.exists())
            self.assertEqual(result["database"], str(output.resolve()))
            self.assertTrue(result["distribution"]["distribution_pass"])
            self.assertEqual(
                sqlite3.connect(output)
                .execute("SELECT COUNT(*) FROM agents")
                .fetchone()[0],
                100,
            )


if __name__ == "__main__":
    unittest.main()
