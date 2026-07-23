from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from twinmarket_kr.rn_ab.source_candidates import (
    SourceCandidateError,
    validate_source_input_candidates,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_ROOT = (
    PROJECT_ROOT / "preparation" / "rn_ab_source_candidate_v1" / "input_candidates"
)


class SourceInputCandidateTests(unittest.TestCase):
    def test_checked_in_candidate_is_explicit_no_go_and_quarantines_documented_leak(self) -> None:
        result = validate_source_input_candidates(
            root=CANDIDATE_ROOT,
            project_root=PROJECT_ROOT,
            check_source_files=True,
        )
        self.assertFalse(result["execution_authorized"])
        self.assertFalse(result["run_eligible"])
        self.assertEqual(result["candidate_file_count"], 8)

        audit = json.loads((CANDIDATE_ROOT / "SOURCE_INPUT_CANDIDATE_AUDIT.json").read_text())
        self.assertEqual(audit["network_requests"], 0)
        self.assertEqual(audit["paid_api_calls"], 0)
        self.assertIn(
            "immutable_article_version_and_as_of_provenance_missing",
            audit["blocking_reasons"],
        )
        quarantine = json.loads((CANDIDATE_ROOT / "leakage_quarantine_report.json").read_text())
        documented = quarantine["proposed_runtime_shape"]["confirmed_counterexamples"]
        self.assertTrue(
            any(
                item["article_id"] == "news_20260427_섹터_0032"
                and item["decision"] == "quarantine"
                for item in documented
            )
        )

    def test_candidate_byte_tamper_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            copied = Path(temp_dir) / "input_candidates"
            shutil.copytree(CANDIDATE_ROOT, copied)
            target = copied / "calendar_candidate.json"
            payload = json.loads(target.read_text())
            payload["findings"]["stock_calendar_date_count"] = 999
            target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(SourceCandidateError, "self hash|file hash"):
                validate_source_input_candidates(
                    root=copied,
                    project_root=PROJECT_ROOT,
                    check_source_files=False,
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
