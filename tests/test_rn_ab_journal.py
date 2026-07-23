from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from twinmarket_kr.rn_ab.journal import JournalIntegrityError, LogicalCallKey, ResponseJournal


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


class ResponseJournalTests(unittest.TestCase):
    def test_committed_record_preserves_canonical_request_and_response(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = ResponseJournal(
                Path(directory) / "response_journal.sqlite",
                manifest_sha256=_digest("manifest"),
            )
            key = LogicalCallKey(
                "run-1", "RN_COMM_ON", "agent-1", "e1", "community_posting", "v1"
            )
            request = {"prompt_values": {"date": "2026-02-27"}, "seed": 7}
            response = {"will_post": False}
            logical_call_id = journal.begin_attempt(
                key,
                request,
                phase_attempt_id="phase-1",
                attempt_number=1,
            )
            journal.record_success(
                logical_call_id,
                response,
                phase_attempt_id="phase-1",
                attempt_number=1,
            )
            journal.mark_committed([logical_call_id])

            records = journal.committed_request_response_records()
            self.assertEqual(records[logical_call_id]["request"], request)
            self.assertEqual(records[logical_call_id]["response"], response)
            self.assertEqual(
                records[logical_call_id]["request_sha256"],
                _digest(
                    json.dumps(
                        request,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                ),
            )

    def test_migrated_committed_legacy_row_without_request_json_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "response_journal.sqlite"
            manifest = _digest("manifest")
            logical_call_id = "run-1|RN_COMM_ON|agent-1|e1|community_posting|v1"
            request_json = '{"input":"sealed"}'
            response_json = '{"will_post":false}'
            with sqlite3.connect(path) as connection:
                connection.execute(
                    """
                    CREATE TABLE logical_responses (
                        logical_call_id TEXT PRIMARY KEY,
                        manifest_sha256 TEXT NOT NULL,
                        request_sha256 TEXT NOT NULL,
                        response_sha256 TEXT,
                        response_json TEXT,
                        validation_status TEXT NOT NULL,
                        commit_status TEXT NOT NULL,
                        committed_at TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO logical_responses (
                        logical_call_id, manifest_sha256, request_sha256,
                        response_sha256, response_json, validation_status,
                        commit_status, committed_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'accepted', 'committed', 't', 't', 't')
                    """,
                    (
                        logical_call_id,
                        manifest,
                        _digest(request_json),
                        _digest(response_json),
                        response_json,
                    ),
                )
                connection.commit()

            journal = ResponseJournal(path, manifest_sha256=manifest)
            with sqlite3.connect(path) as connection:
                columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(logical_responses)"
                    ).fetchall()
                }
            self.assertIn("request_json", columns)
            with self.assertRaisesRegex(JournalIntegrityError, "legacy row"):
                journal.committed_request_response_records()

    def test_accepted_response_json_hash_is_revalidated_on_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "response_journal.sqlite"
            journal = ResponseJournal(path, manifest_sha256=_digest("manifest"))
            key = LogicalCallKey(
                run_id="run-1",
                condition_id="RN_COMM_OFF",
                agent_id="agent-1",
                event_id="e1",
                stage="stb",
                schema_version="v1",
            )
            request = {"input": "sealed"}
            logical_call_id = journal.begin_attempt(
                key, request, phase_attempt_id="phase-1", attempt_number=1
            )
            journal.record_success(
                logical_call_id,
                {"result": "accepted"},
                phase_attempt_id="phase-1",
                attempt_number=1,
            )
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "UPDATE logical_responses SET response_json = ? WHERE logical_call_id = ?",
                    ('{"result":"tampered"}', logical_call_id),
                )
                connection.commit()
            with self.assertRaisesRegex(JournalIntegrityError, "hash does not match"):
                journal.get_accepted(key, request)

    def test_accepted_response_must_remain_canonical_json_on_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "response_journal.sqlite"
            journal = ResponseJournal(path, manifest_sha256=_digest("manifest"))
            key = LogicalCallKey("run-1", "RN_COMM_OFF", "agent-1", "e1", "stb", "v1")
            request = {"input": "sealed"}
            logical_call_id = journal.begin_attempt(
                key, request, phase_attempt_id="phase-1", attempt_number=1
            )
            journal.record_success(
                logical_call_id,
                {"result": "accepted"},
                phase_attempt_id="phase-1",
                attempt_number=1,
            )
            # The parsed object would still be {"result": "accepted"}, but
            # duplicate keys are not an acceptable durable strict-JSON form.
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "UPDATE logical_responses SET response_json = ? WHERE logical_call_id = ?",
                    ('{"result":"tampered","result":"accepted"}', logical_call_id),
                )
                connection.commit()
            with self.assertRaisesRegex(JournalIntegrityError, "not canonical"):
                journal.get_accepted(key, request)

    def test_nonfinite_tampering_is_a_journal_integrity_error_everywhere(self) -> None:
        for raw_response in ('{"result":NaN}', '{"result":Infinity}', '{"result":-Infinity}'):
            with self.subTest(raw_response=raw_response), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "response_journal.sqlite"
                journal = ResponseJournal(path, manifest_sha256=_digest("manifest"))
                key = LogicalCallKey("run-1", "RN_COMM_OFF", "agent-1", "e1", "stb", "v1")
                request = {"input": "sealed"}
                logical_call_id = journal.begin_attempt(
                    key, request, phase_attempt_id="phase-1", attempt_number=1
                )
                journal.record_success(
                    logical_call_id,
                    {"result": "accepted"},
                    phase_attempt_id="phase-1",
                    attempt_number=1,
                )
                journal.mark_committed([logical_call_id])
                with sqlite3.connect(path) as connection:
                    connection.execute(
                        "UPDATE logical_responses SET response_json = ? WHERE logical_call_id = ?",
                        (raw_response, logical_call_id),
                    )
                    connection.commit()
                with self.assertRaisesRegex(JournalIntegrityError, "not canonical"):
                    journal.get_accepted(key, request)
                with self.assertRaisesRegex(JournalIntegrityError, "not canonical"):
                    journal.committed_accepted_response_digests()

    def test_rolled_back_response_can_never_be_revived_as_committed_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = ResponseJournal(
                Path(directory) / "response_journal.sqlite", manifest_sha256=_digest("manifest")
            )
            key = LogicalCallKey(
                run_id="run-1",
                condition_id="RN_COMM_OFF",
                agent_id="agent-1",
                event_id="e1",
                stage="stb",
                schema_version="v1",
            )
            logical_call_id = journal.begin_attempt(
                key, {"input": "sealed"}, phase_attempt_id="phase-1", attempt_number=1
            )
            journal.record_success(
                logical_call_id,
                {"result": "accepted"},
                phase_attempt_id="phase-1",
                attempt_number=1,
            )
            journal.mark_rolled_back([logical_call_id])
            with self.assertRaisesRegex(JournalIntegrityError, "Rolled-back response"):
                journal.get_accepted(key, {"input": "sealed"})
            with self.assertRaisesRegex(JournalIntegrityError, "Rolled-back"):
                journal.mark_committed([logical_call_id])
            self.assertEqual(
                journal.committed_summary(),
                {"pending": 0, "committed": 0, "rolled_back": 1},
            )

    def test_terminal_commit_state_is_monotonic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = ResponseJournal(
                Path(directory) / "response_journal.sqlite", manifest_sha256=_digest("manifest")
            )
            key = LogicalCallKey(
                run_id="run-1",
                condition_id="RN_COMM_OFF",
                agent_id="agent-1",
                event_id="e1",
                stage="decision",
                schema_version="v1",
            )
            call_id = journal.begin_attempt(
                key, {"input": "sealed"}, phase_attempt_id="phase-1", attempt_number=1
            )
            journal.record_success(
                call_id,
                {"result": "accepted"},
                phase_attempt_id="phase-1",
                attempt_number=1,
            )
            journal.mark_committed([call_id])
            journal.mark_committed([call_id])  # idempotent replay
            with self.assertRaisesRegex(JournalIntegrityError, "Committed"):
                journal.mark_rolled_back([call_id])

    def test_reopening_a_journal_with_another_manifest_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "response_journal.sqlite"
            journal = ResponseJournal(path, manifest_sha256=_digest("manifest-a"))
            key = LogicalCallKey(
                run_id="run-1",
                condition_id="RN_COMM_OFF",
                agent_id="agent-1",
                event_id="e1",
                stage="stb",
                schema_version="v1",
            )
            journal.begin_attempt(
                key, {"input": "sealed"}, phase_attempt_id="phase-1", attempt_number=1
            )

            with self.assertRaisesRegex(JournalIntegrityError, "different manifest"):
                ResponseJournal(path, manifest_sha256=_digest("manifest-b"))


if __name__ == "__main__":
    unittest.main()
