"""Durable logical-response journal separated from rollback-prone run state."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


class JournalIntegrityError(RuntimeError):
    pass


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _load_canonical_response(
    raw_response: str,
    *,
    logical_call_id: str,
    status_label: str,
) -> tuple[dict[str, Any], str]:
    """Revalidate one durable response without leaking decoder/encoder errors.

    The model-facing parser rejects duplicate keys and non-standard numbers
    before a response reaches this journal.  This second boundary protects
    replay/finalization from direct database corruption as well: duplicate-key
    text cannot be canonical, while NaN, Infinity, numeric overflow, excessive
    nesting, and invalid Unicode must all become a journal integrity failure.
    """

    try:
        response = json.loads(
            raw_response,
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise JournalIntegrityError(
            f"{status_label} response JSON is not canonical for {logical_call_id}"
        ) from exc
    if not isinstance(response, dict):
        raise JournalIntegrityError(
            f"{status_label} response has invalid type for {logical_call_id}"
        )
    try:
        canonical = _canonical(response)
        response_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError) as exc:
        raise JournalIntegrityError(
            f"{status_label} response JSON is not canonical for {logical_call_id}"
        ) from exc
    if raw_response != canonical:
        raise JournalIntegrityError(
            f"{status_label} response JSON is not canonical for {logical_call_id}"
        )
    return response, response_sha256


def _load_canonical_request(
    raw_request: str | None,
    *,
    logical_call_id: str,
    status_label: str,
) -> tuple[dict[str, Any], str]:
    """Load the exact request object used for one durable logical call.

    ``request_sha256`` alone proves only that *some* byte-equivalent object was
    once hashed.  Final lineage needs the canonical request itself so prompt
    template/value identities can be checked against sealed inputs.  Older
    journals are migrated with a nullable column, but a NULL legacy request is
    deliberately not guessed or reconstructed after a response was accepted.
    """

    if raw_request is None:
        raise JournalIntegrityError(
            f"{status_label} request JSON is absent for legacy row {logical_call_id}"
        )
    try:
        request = json.loads(
            raw_request,
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise JournalIntegrityError(
            f"{status_label} request JSON is not canonical for {logical_call_id}"
        ) from exc
    if not isinstance(request, dict):
        raise JournalIntegrityError(
            f"{status_label} request has invalid type for {logical_call_id}"
        )
    try:
        canonical = _canonical(request)
        request_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError) as exc:
        raise JournalIntegrityError(
            f"{status_label} request JSON is not canonical for {logical_call_id}"
        ) from exc
    if raw_request != canonical:
        raise JournalIntegrityError(
            f"{status_label} request JSON is not canonical for {logical_call_id}"
        )
    return request, request_sha256


@dataclass(frozen=True)
class LogicalCallKey:
    run_id: str
    condition_id: str
    agent_id: str
    event_id: str
    stage: str
    schema_version: str

    def value(self) -> str:
        fields = (
            self.run_id,
            self.condition_id,
            self.agent_id,
            self.event_id,
            self.stage,
            self.schema_version,
        )
        if any(not field for field in fields):
            raise JournalIntegrityError("Every logical-call key component is required")
        return "|".join(fields)


class ResponseJournal:
    """Stores accepted responses once and preserves physical attempts separately."""

    def __init__(self, path: Path | str, *, manifest_sha256: str) -> None:
        self.path = Path(path)
        self.manifest_sha256 = manifest_sha256
        if not manifest_sha256:
            raise ValueError("manifest_sha256 is required")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS logical_responses (
                    logical_call_id TEXT PRIMARY KEY,
                    manifest_sha256 TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    request_json TEXT,
                    response_sha256 TEXT,
                    response_json TEXT,
                    validation_status TEXT NOT NULL CHECK(validation_status IN ('pending', 'accepted', 'rejected')),
                    commit_status TEXT NOT NULL CHECK(commit_status IN ('pending', 'committed', 'rolled_back')),
                    committed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS physical_attempts (
                    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    logical_call_id TEXT NOT NULL REFERENCES logical_responses(logical_call_id),
                    phase_attempt_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    response_sha256 TEXT,
                    status TEXT NOT NULL CHECK(status IN ('started', 'success', 'error')),
                    error_type TEXT,
                    error_text TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(logical_call_id, phase_attempt_id, attempt_number)
                );
                """
            )
            # Additive, migration-safe schema evolution.  A pre-column journal
            # remains readable for diagnostics, but accepted/committed legacy
            # rows cannot pass replay or finalization because their exact
            # request can no longer be proven.
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(logical_responses)"
                ).fetchall()
            }
            if "request_json" not in columns:
                connection.execute(
                    "ALTER TABLE logical_responses ADD COLUMN request_json TEXT"
                )
            self._assert_journal_manifest(connection)
            connection.commit()

    def get_accepted(self, key: LogicalCallKey, request: Mapping[str, Any]) -> dict[str, Any] | None:
        logical_call_id = key.value()
        request_sha = _sha(dict(request))
        with self._connect(read_only=True) as connection:
            row = connection.execute(
                "SELECT * FROM logical_responses WHERE logical_call_id = ?", (logical_call_id,)
            ).fetchone()
        if row is None:
            return None
        self._assert_row_manifest(row, logical_call_id)
        self._assert_request_identity(
            row,
            request_sha,
            logical_call_id,
            request_json=_canonical(dict(request)),
        )
        if str(row["commit_status"]) == "rolled_back":
            raise JournalIntegrityError(
                f"Rolled-back response may not be replayed: {logical_call_id}"
            )
        if str(row["validation_status"]) != "accepted":
            return None
        raw_response = str(row["response_json"] or "")
        response, response_sha256 = _load_canonical_response(
            raw_response,
            logical_call_id=logical_call_id,
            status_label="Accepted",
        )
        if str(row["response_sha256"] or "") != response_sha256:
            raise JournalIntegrityError(
                f"Accepted response hash does not match stored JSON for {logical_call_id}"
            )
        return response

    def begin_attempt(
        self,
        key: LogicalCallKey,
        request: Mapping[str, Any],
        *,
        phase_attempt_id: str,
        attempt_number: int,
    ) -> str:
        if not phase_attempt_id or int(attempt_number) < 1:
            raise JournalIntegrityError("phase_attempt_id and positive attempt_number are required")
        logical_call_id = key.value()
        request_payload = dict(request)
        request_json = _canonical(request_payload)
        request_sha = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
        now = _now()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM logical_responses WHERE logical_call_id = ?", (logical_call_id,)
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO logical_responses (
                        logical_call_id, manifest_sha256, request_sha256, request_json,
                        validation_status, commit_status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'pending', 'pending', ?, ?)
                    """,
                    (
                        logical_call_id,
                        self.manifest_sha256,
                        request_sha,
                        request_json,
                        now,
                        now,
                    ),
                )
            else:
                self._assert_row_manifest(row, logical_call_id)
                self._assert_request_identity(
                    row,
                    request_sha,
                    logical_call_id,
                    request_json=request_json,
                )
            connection.execute(
                """
                INSERT INTO physical_attempts (
                    logical_call_id, phase_attempt_id, attempt_number, request_sha256, status, created_at
                ) VALUES (?, ?, ?, ?, 'started', ?)
                """,
                (logical_call_id, phase_attempt_id, int(attempt_number), request_sha, now),
            )
            connection.commit()
        return logical_call_id

    def record_success(
        self,
        logical_call_id: str,
        response: Mapping[str, Any],
        *,
        phase_attempt_id: str,
        attempt_number: int,
    ) -> str:
        response_payload = dict(response)
        response_sha = _sha(response_payload)
        with self._connect() as connection:
            row = self._logical_row(connection, logical_call_id)
            previous_status = str(row["validation_status"])
            if previous_status == "accepted":
                if str(row["response_sha256"] or "") != response_sha:
                    raise JournalIntegrityError(
                        f"Different accepted response for replayed logical request {logical_call_id}"
                    )
            elif previous_status == "rejected":
                raise JournalIntegrityError(f"Rejected logical request cannot be revived: {logical_call_id}")
            else:
                connection.execute(
                    """
                    UPDATE logical_responses
                    SET response_sha256 = ?, response_json = ?, validation_status = 'accepted', updated_at = ?
                    WHERE logical_call_id = ?
                    """,
                    (response_sha, _canonical(response_payload), _now(), logical_call_id),
                )
            self._finish_attempt(
                connection,
                logical_call_id,
                phase_attempt_id,
                attempt_number,
                status="success",
                response_sha256=response_sha,
            )
            connection.commit()
        return response_sha

    def record_error(
        self,
        logical_call_id: str,
        *,
        phase_attempt_id: str,
        attempt_number: int,
        error: BaseException,
    ) -> None:
        with self._connect() as connection:
            self._logical_row(connection, logical_call_id)
            self._finish_attempt(
                connection,
                logical_call_id,
                phase_attempt_id,
                attempt_number,
                status="error",
                error_type=type(error).__name__,
                error_text=str(error),
            )
            connection.commit()

    def mark_committed(self, logical_call_ids: list[str]) -> None:
        if not logical_call_ids:
            return
        with self._connect() as connection:
            for logical_call_id in logical_call_ids:
                row = self._logical_row(connection, logical_call_id)
                if str(row["validation_status"]) != "accepted":
                    raise JournalIntegrityError(f"Cannot commit an unaccepted response: {logical_call_id}")
                commit_status = str(row["commit_status"])
                if commit_status == "committed":
                    # Idempotent retry of the same completed phase is safe.
                    continue
                if commit_status != "pending":
                    raise JournalIntegrityError(
                        f"Rolled-back response cannot be committed: {logical_call_id}"
                    )
                connection.execute(
                    """
                    UPDATE logical_responses
                    SET commit_status = 'committed', committed_at = ?, updated_at = ?
                    WHERE logical_call_id = ?
                    """,
                    (_now(), _now(), logical_call_id),
                )
            connection.commit()

    def mark_rolled_back(self, logical_call_ids: list[str]) -> None:
        """Retain the validated response, but never count it as scientific work."""
        if not logical_call_ids:
            return
        with self._connect() as connection:
            for logical_call_id in logical_call_ids:
                row = self._logical_row(connection, logical_call_id)
                commit_status = str(row["commit_status"])
                if commit_status == "rolled_back":
                    continue
                if commit_status != "pending":
                    raise JournalIntegrityError(
                        f"Committed response cannot be rolled back: {logical_call_id}"
                    )
                connection.execute(
                    "UPDATE logical_responses SET commit_status = 'rolled_back', updated_at = ? WHERE logical_call_id = ?",
                    (_now(), logical_call_id),
                )
            connection.commit()

    def committed_summary(self) -> dict[str, int]:
        with self._connect(read_only=True) as connection:
            self._assert_journal_manifest(connection)
            rows = connection.execute(
                """
                SELECT commit_status, COUNT(*) AS count
                FROM logical_responses
                WHERE manifest_sha256 = ?
                GROUP BY commit_status
                """,
                (self.manifest_sha256,),
            ).fetchall()
        result = {"pending": 0, "committed": 0, "rolled_back": 0}
        result.update({str(row["commit_status"]): int(row["count"]) for row in rows})
        return result

    def committed_accepted_response_digests(self) -> dict[str, str]:
        """Return committed logical-response IDs and revalidated hashes."""

        return {
            logical_call_id: str(record["response_sha256"])
            for logical_call_id, record in self.committed_request_response_records().items()
        }

    def committed_request_response_records(
        self,
    ) -> dict[str, dict[str, Any]]:
        """Return canonically revalidated committed request/response pairs.

        The API is intentionally strict enough for P0/finalization.  In
        particular, a migrated legacy row with ``request_json IS NULL`` is not
        silently trusted merely because its historical request hash is
        well-formed.
        """

        with self._connect(read_only=True) as connection:
            self._assert_journal_manifest(connection)
            rows = connection.execute(
                """
                SELECT logical_call_id, request_sha256, request_json,
                       response_sha256, response_json, validation_status
                FROM logical_responses
                WHERE manifest_sha256 = ? AND commit_status = 'committed'
                ORDER BY logical_call_id
                """,
                (self.manifest_sha256,),
            ).fetchall()
        committed: dict[str, dict[str, Any]] = {}
        for row in rows:
            logical_call_id = str(row["logical_call_id"])
            if str(row["validation_status"]) != "accepted":
                raise JournalIntegrityError(
                    f"Committed response is not accepted: {logical_call_id}"
                )
            request, expected_request_sha = _load_canonical_request(
                row["request_json"],
                logical_call_id=logical_call_id,
                status_label="Committed",
            )
            if str(row["request_sha256"] or "") != expected_request_sha:
                raise JournalIntegrityError(
                    f"Committed request hash does not match stored JSON for {logical_call_id}"
                )
            raw_response = str(row["response_json"] or "")
            response, expected_response_sha = _load_canonical_response(
                raw_response,
                logical_call_id=logical_call_id,
                status_label="Committed",
            )
            if str(row["response_sha256"] or "") != expected_response_sha:
                raise JournalIntegrityError(
                    f"Committed response hash does not match stored JSON for {logical_call_id}"
                )
            committed[logical_call_id] = {
                "request": request,
                "request_sha256": expected_request_sha,
                "response": response,
                "response_sha256": expected_response_sha,
            }
        return committed

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            uri = f"file:{self.path.resolve().as_posix()}?mode=ro"
            connection = sqlite3.connect(uri, uri=True)
        else:
            connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @staticmethod
    def _assert_request_identity(
        row: sqlite3.Row,
        request_sha: str,
        logical_call_id: str,
        *,
        request_json: str,
    ) -> None:
        if str(row["request_sha256"]) != request_sha:
            raise JournalIntegrityError(
                f"Different request payload for replayed logical request {logical_call_id}"
            )
        _stored_request, stored_request_sha = _load_canonical_request(
            row["request_json"],
            logical_call_id=logical_call_id,
            status_label="Stored",
        )
        if stored_request_sha != request_sha or str(row["request_json"]) != request_json:
            raise JournalIntegrityError(
                f"Different request payload for replayed logical request {logical_call_id}"
            )

    def _assert_journal_manifest(self, connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT manifest_sha256 FROM logical_responses WHERE manifest_sha256 != ? LIMIT 1",
            (self.manifest_sha256,),
        ).fetchone()
        if row is not None:
            raise JournalIntegrityError(
                "Response journal contains a logical response for a different manifest"
            )

    def _assert_row_manifest(self, row: sqlite3.Row, logical_call_id: str) -> None:
        if str(row["manifest_sha256"]) != self.manifest_sha256:
            raise JournalIntegrityError(f"Manifest changed for logical request {logical_call_id}")

    def _logical_row(self, connection: sqlite3.Connection, logical_call_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM logical_responses WHERE logical_call_id = ?", (logical_call_id,)
        ).fetchone()
        if row is None:
            raise JournalIntegrityError(f"Unknown logical request {logical_call_id}")
        self._assert_row_manifest(row, logical_call_id)
        return row

    @staticmethod
    def _finish_attempt(
        connection: sqlite3.Connection,
        logical_call_id: str,
        phase_attempt_id: str,
        attempt_number: int,
        *,
        status: str,
        response_sha256: str | None = None,
        error_type: str | None = None,
        error_text: str | None = None,
    ) -> None:
        cursor = connection.execute(
            """
            UPDATE physical_attempts
            SET status = ?, response_sha256 = ?, error_type = ?, error_text = ?
            WHERE logical_call_id = ? AND phase_attempt_id = ? AND attempt_number = ? AND status = 'started'
            """,
            (
                status,
                response_sha256,
                error_type,
                error_text,
                logical_call_id,
                phase_attempt_id,
                int(attempt_number),
            ),
        )
        if cursor.rowcount != 1:
            raise JournalIntegrityError("Physical attempt is missing or already finalized")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
