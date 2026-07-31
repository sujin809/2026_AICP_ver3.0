"""Durable validated-response replay for the numbered simulation pipeline.

The simulation database and output files are checkpointed per AM/PM event.
This journal is the matching durable boundary for paid model responses: a
provider return is reusable only after the stage validator accepted it, and an
event is committed only together with the exact accepted response digests.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping


JOURNAL_SCHEMA_VERSION = "integrated-response-journal-v1"


class ResponseJournalError(RuntimeError):
    """The response journal cannot safely replay or commit a model result."""


class ResponseJournalDriftError(ResponseJournalError):
    """A logical call was reused with different semantic request inputs."""


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def parse_strict_json_object(content: str) -> dict[str, Any]:
    """Parse exactly one standards-compliant JSON object.

    Markdown fences, duplicate keys, NaN and Infinity are rejected.  This
    matches the provider-audit canonical digest and prevents a locally
    normalized object from being accepted as a different provider response.
    The returned mapping is reparsed from that canonical representation, so a
    provider response and its later journal replay have the same key order
    when a downstream prompt renders nested JSON.
    """

    value = json.loads(
        content,
        object_pairs_hook=_strict_object,
        parse_constant=_reject_constant,
    )
    if not isinstance(value, dict):
        raise ValueError("response must be one JSON object")
    return json.loads(canonical_json(value))


@dataclass(frozen=True)
class LogicalCallKey:
    run_id: str
    condition_id: str
    agent_id: str
    event_id: str
    stage: str
    schema_version: str

    def __post_init__(self) -> None:
        for field, value in (
            ("run_id", self.run_id),
            ("condition_id", self.condition_id),
            ("agent_id", self.agent_id),
            ("event_id", self.event_id),
            ("stage", self.stage),
            ("schema_version", self.schema_version),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} must be a non-empty string")

    def value(self) -> str:
        payload = {
            "run_id": self.run_id,
            "condition_id": self.condition_id,
            "agent_id": self.agent_id,
            "event_id": self.event_id,
            "stage": self.stage,
            "schema_version": self.schema_version,
        }
        return "llm:" + canonical_sha256(payload)


@dataclass(frozen=True)
class JournalReplay:
    logical_call_id: str
    response: dict[str, Any]
    response_sha256: str
    validation_attempt: int


@dataclass(frozen=True)
class JournalEventContext:
    journal: "ResponseJournal"
    run_id: str
    condition_id: str
    event_id: str
    phase_attempt_id: str
    event_attempt_number: int
    agent_id: str


_EVENT_CONTEXT: contextvars.ContextVar[JournalEventContext | None] = (
    contextvars.ContextVar("integrated_response_journal_context", default=None)
)


@contextmanager
def response_journal_scope(
    *,
    journal: "ResponseJournal",
    run_id: str,
    condition_id: str,
    event_id: str,
    phase_attempt_id: str,
    event_attempt_number: int,
    agent_id: str,
) -> Iterator[JournalEventContext]:
    context = JournalEventContext(
        journal=journal,
        run_id=str(run_id),
        condition_id=str(condition_id),
        event_id=str(event_id),
        phase_attempt_id=str(phase_attempt_id),
        event_attempt_number=int(event_attempt_number),
        agent_id=str(agent_id),
    )
    if context.event_attempt_number < 1:
        raise ValueError("event_attempt_number must be positive")
    token = _EVENT_CONTEXT.set(context)
    try:
        yield context
    finally:
        _EVENT_CONTEXT.reset(token)


def current_journal_context() -> JournalEventContext | None:
    return _EVENT_CONTEXT.get()


class ResponseJournal:
    """SQLite-backed accepted-response store safe for concurrent agent tasks."""

    def __init__(self, path: Path | str, *, manifest_sha256: str) -> None:
        self.path = Path(path)
        if (
            not isinstance(manifest_sha256, str)
            or len(manifest_sha256) != 64
            or any(character not in "0123456789abcdef" for character in manifest_sha256)
        ):
            raise ValueError("manifest_sha256 must be a lowercase SHA-256")
        self.manifest_sha256 = manifest_sha256
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS journal_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS logical_responses (
                    logical_call_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    condition_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    manifest_sha256 TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    response_sha256 TEXT,
                    response_json TEXT,
                    accepted_validation_attempt INTEGER,
                    validation_status TEXT NOT NULL
                        CHECK (validation_status IN ('pending', 'accepted')),
                    commit_status TEXT NOT NULL
                        CHECK (commit_status IN ('pending', 'committed')),
                    created_at TEXT NOT NULL,
                    accepted_at TEXT,
                    committed_at TEXT
                );

                CREATE UNIQUE INDEX IF NOT EXISTS logical_response_key
                ON logical_responses (
                    run_id, condition_id, agent_id, event_id, stage, schema_version
                );

                CREATE TABLE IF NOT EXISTS physical_attempts (
                    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    logical_call_id TEXT NOT NULL,
                    phase_attempt_id TEXT NOT NULL,
                    event_attempt_number INTEGER NOT NULL,
                    validation_attempt INTEGER NOT NULL,
                    status TEXT NOT NULL
                        CHECK (status IN ('started', 'rejected', 'error', 'accepted')),
                    response_sha256 TEXT,
                    error_type TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (
                        logical_call_id, phase_attempt_id, validation_attempt
                    ),
                    FOREIGN KEY (logical_call_id)
                        REFERENCES logical_responses(logical_call_id)
                );
                """
            )
            meta = {
                "schema_version": JOURNAL_SCHEMA_VERSION,
                "manifest_sha256": self.manifest_sha256,
            }
            for key, expected in meta.items():
                row = connection.execute(
                    "SELECT value FROM journal_meta WHERE key = ?",
                    (key,),
                ).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO journal_meta(key, value) VALUES (?, ?)",
                        (key, expected),
                    )
                elif str(row["value"]) != expected:
                    raise ResponseJournalDriftError(
                        f"response journal {key} differs from this run"
                    )

    def _ensure_logical(
        self,
        connection: sqlite3.Connection,
        key: LogicalCallKey,
        request: Mapping[str, Any],
    ) -> tuple[str, str, str]:
        logical_id = key.value()
        request_json = canonical_json(dict(request))
        request_sha = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
        row = connection.execute(
            "SELECT * FROM logical_responses WHERE logical_call_id = ?",
            (logical_id,),
        ).fetchone()
        if row is None:
            connection.execute(
                """
                INSERT INTO logical_responses (
                    logical_call_id, run_id, condition_id, agent_id, event_id,
                    stage, schema_version, manifest_sha256, request_sha256,
                    request_json, validation_status, commit_status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 'pending', ?)
                """,
                (
                    logical_id,
                    key.run_id,
                    key.condition_id,
                    key.agent_id,
                    key.event_id,
                    key.stage,
                    key.schema_version,
                    self.manifest_sha256,
                    request_sha,
                    request_json,
                    _utc_now(),
                ),
            )
        elif (
            str(row["manifest_sha256"]) != self.manifest_sha256
            or str(row["request_sha256"]) != request_sha
            or str(row["request_json"]) != request_json
        ):
            raise ResponseJournalDriftError(
                f"semantic request drift for {key.stage} at {key.event_id}/{key.agent_id}"
            )
        return logical_id, request_sha, request_json

    def get_accepted(
        self,
        key: LogicalCallKey,
        request: Mapping[str, Any],
    ) -> JournalReplay | None:
        with self._connect() as connection:
            logical_id, _, _ = self._ensure_logical(connection, key, request)
            row = connection.execute(
                "SELECT * FROM logical_responses WHERE logical_call_id = ?",
                (logical_id,),
            ).fetchone()
            if row is None or str(row["validation_status"]) != "accepted":
                return None
            response_json = row["response_json"]
            response_sha = row["response_sha256"]
            validation_attempt = row["accepted_validation_attempt"]
            if (
                not isinstance(response_json, str)
                or not isinstance(response_sha, str)
                or not isinstance(validation_attempt, int)
            ):
                raise ResponseJournalError(
                    f"accepted journal row is incomplete: {logical_id}"
                )
            response = parse_strict_json_object(response_json)
            if canonical_sha256(response) != response_sha:
                raise ResponseJournalError(
                    f"accepted response digest mismatch: {logical_id}"
                )
            return JournalReplay(
                logical_call_id=logical_id,
                response=response,
                response_sha256=response_sha,
                validation_attempt=validation_attempt,
            )

    def begin_attempt(
        self,
        key: LogicalCallKey,
        request: Mapping[str, Any],
        *,
        phase_attempt_id: str,
        event_attempt_number: int,
        validation_attempt: int,
    ) -> str:
        if not phase_attempt_id:
            raise ValueError("phase_attempt_id must be non-empty")
        if event_attempt_number < 1 or validation_attempt < 1:
            raise ValueError("attempt numbers must be positive")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            logical_id, _, _ = self._ensure_logical(connection, key, request)
            row = connection.execute(
                """
                SELECT validation_status
                FROM logical_responses
                WHERE logical_call_id = ?
                """,
                (logical_id,),
            ).fetchone()
            if row is not None and str(row["validation_status"]) == "accepted":
                raise ResponseJournalError(
                    f"cannot start a provider attempt after acceptance: {logical_id}"
                )
            now = _utc_now()
            try:
                connection.execute(
                    """
                    INSERT INTO physical_attempts (
                        logical_call_id, phase_attempt_id, event_attempt_number,
                        validation_attempt, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'started', ?, ?)
                    """,
                    (
                        logical_id,
                        phase_attempt_id,
                        event_attempt_number,
                        validation_attempt,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ResponseJournalError(
                    "duplicate physical validation attempt for "
                    f"{logical_id}/{phase_attempt_id}/{validation_attempt}"
                ) from exc
            return logical_id

    def record_rejected(
        self,
        logical_call_id: str,
        *,
        phase_attempt_id: str,
        validation_attempt: int,
        response: Mapping[str, Any] | None,
        error: str,
    ) -> None:
        response_sha = (
            canonical_sha256(dict(response)) if response is not None else None
        )
        self._finish_attempt(
            logical_call_id,
            phase_attempt_id=phase_attempt_id,
            validation_attempt=validation_attempt,
            status="rejected",
            response_sha256=response_sha,
            error_type="ValidationRejected",
            error=error,
        )

    def record_error(
        self,
        logical_call_id: str,
        *,
        phase_attempt_id: str,
        validation_attempt: int,
        error: BaseException,
    ) -> None:
        self._finish_attempt(
            logical_call_id,
            phase_attempt_id=phase_attempt_id,
            validation_attempt=validation_attempt,
            status="error",
            response_sha256=None,
            error_type=type(error).__name__,
            error=str(error),
        )

    def record_success(
        self,
        logical_call_id: str,
        response: Mapping[str, Any],
        *,
        phase_attempt_id: str,
        validation_attempt: int,
    ) -> str:
        response_json = canonical_json(dict(response))
        response_sha = hashlib.sha256(response_json.encode("utf-8")).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT validation_status, response_sha256
                FROM logical_responses
                WHERE logical_call_id = ?
                """,
                (logical_call_id,),
            ).fetchone()
            if row is None:
                raise ResponseJournalError(
                    f"logical response is missing: {logical_call_id}"
                )
            if str(row["validation_status"]) == "accepted":
                if str(row["response_sha256"]) == response_sha:
                    return response_sha
                raise ResponseJournalError(
                    f"conflicting accepted response: {logical_call_id}"
                )
            cursor = connection.execute(
                """
                UPDATE physical_attempts
                SET status = 'accepted', response_sha256 = ?, error_type = NULL,
                    error = NULL, updated_at = ?
                WHERE logical_call_id = ? AND phase_attempt_id = ?
                  AND validation_attempt = ? AND status = 'started'
                """,
                (
                    response_sha,
                    _utc_now(),
                    logical_call_id,
                    phase_attempt_id,
                    validation_attempt,
                ),
            )
            if cursor.rowcount != 1:
                raise ResponseJournalError(
                    f"started attempt is missing for acceptance: {logical_call_id}"
                )
            connection.execute(
                """
                UPDATE logical_responses
                SET response_sha256 = ?, response_json = ?,
                    accepted_validation_attempt = ?,
                    validation_status = 'accepted', accepted_at = ?
                WHERE logical_call_id = ?
                """,
                (
                    response_sha,
                    response_json,
                    validation_attempt,
                    _utc_now(),
                    logical_call_id,
                ),
            )
        return response_sha

    def _finish_attempt(
        self,
        logical_call_id: str,
        *,
        phase_attempt_id: str,
        validation_attempt: int,
        status: str,
        response_sha256: str | None,
        error_type: str,
        error: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE physical_attempts
                SET status = ?, response_sha256 = ?, error_type = ?, error = ?,
                    updated_at = ?
                WHERE logical_call_id = ? AND phase_attempt_id = ?
                  AND validation_attempt = ? AND status = 'started'
                """,
                (
                    status,
                    response_sha256,
                    error_type,
                    error,
                    _utc_now(),
                    logical_call_id,
                    phase_attempt_id,
                    validation_attempt,
                ),
            )
            if cursor.rowcount != 1:
                raise ResponseJournalError(
                    f"started attempt is missing for {status}: {logical_call_id}"
                )

    def accepted_event_digests(self, event_id: str) -> dict[str, str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT logical_call_id, validation_status, commit_status,
                       response_sha256
                FROM logical_responses
                WHERE event_id = ?
                ORDER BY logical_call_id
                """,
                (event_id,),
            ).fetchall()
        unresolved = [
            str(row["logical_call_id"])
            for row in rows
            if str(row["validation_status"]) != "accepted"
        ]
        if unresolved:
            raise ResponseJournalError(
                f"event has unresolved logical responses: {unresolved[:10]}"
            )
        digests: dict[str, str] = {}
        for row in rows:
            digest = row["response_sha256"]
            if not isinstance(digest, str):
                raise ResponseJournalError(
                    f"accepted event response has no digest: {row['logical_call_id']}"
                )
            digests[str(row["logical_call_id"])] = digest
        return digests

    def assert_event_digests(
        self,
        event_id: str,
        expected: Mapping[str, str],
    ) -> None:
        observed = self.accepted_event_digests(event_id)
        if observed != dict(expected):
            raise ResponseJournalDriftError(
                f"accepted response set drifted for event {event_id}"
            )

    def mark_committed(self, logical_call_ids: Mapping[str, str]) -> None:
        expected = dict(logical_call_ids)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for logical_id, digest in expected.items():
                row = connection.execute(
                    """
                    SELECT validation_status, response_sha256, commit_status
                    FROM logical_responses
                    WHERE logical_call_id = ?
                    """,
                    (logical_id,),
                ).fetchone()
                if (
                    row is None
                    or str(row["validation_status"]) != "accepted"
                    or str(row["response_sha256"]) != digest
                ):
                    raise ResponseJournalDriftError(
                        f"cannot commit unresolved/drifted response: {logical_id}"
                    )
                if str(row["commit_status"]) == "committed":
                    continue
                connection.execute(
                    """
                    UPDATE logical_responses
                    SET commit_status = 'committed', committed_at = ?
                    WHERE logical_call_id = ?
                    """,
                    (_utc_now(), logical_id),
                )

    def summary(self) -> dict[str, Any]:
        with self._connect() as connection:
            logical = connection.execute(
                """
                SELECT validation_status, commit_status, COUNT(*) AS count
                FROM logical_responses
                GROUP BY validation_status, commit_status
                """
            ).fetchall()
            physical = connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM physical_attempts
                GROUP BY status
                """
            ).fetchall()
            labels = connection.execute(
                "SELECT DISTINCT stage FROM logical_responses ORDER BY stage"
            ).fetchall()
        return {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "path": str(self.path),
            "logical_counts": {
                f"{row['validation_status']}/{row['commit_status']}": int(row["count"])
                for row in logical
            },
            "physical_attempt_counts": {
                str(row["status"]): int(row["count"])
                for row in physical
            },
            "stage_labels": [str(row["stage"]) for row in labels],
        }

    def rejected_errors(self, logical_call_id: str) -> list[list[str]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT error
                FROM physical_attempts
                WHERE logical_call_id = ? AND status = 'rejected'
                ORDER BY validation_attempt
                """,
                (logical_call_id,),
            ).fetchall()
        result: list[list[str]] = []
        for row in rows:
            try:
                value = json.loads(str(row["error"]))
            except json.JSONDecodeError as exc:
                raise ResponseJournalError(
                    f"rejected-attempt diagnostics are corrupt: {logical_call_id}"
                ) from exc
            if not isinstance(value, list) or any(
                not isinstance(item, str) for item in value
            ):
                raise ResponseJournalError(
                    f"rejected-attempt diagnostics are invalid: {logical_call_id}"
                )
            result.append(list(value))
        return result


@dataclass
class JournalCall:
    context: JournalEventContext
    key: LogicalCallKey
    request: dict[str, Any]
    replay: JournalReplay | None

    @property
    def logical_call_id(self) -> str:
        return self.key.value()

    @property
    def phase_attempt_id(self) -> str:
        return self.context.phase_attempt_id

    def begin(self, validation_attempt: int) -> None:
        self.context.journal.begin_attempt(
            self.key,
            self.request,
            phase_attempt_id=self.context.phase_attempt_id,
            event_attempt_number=self.context.event_attempt_number,
            validation_attempt=validation_attempt,
        )

    def rejected(
        self,
        validation_attempt: int,
        *,
        response: Mapping[str, Any] | None,
        errors: list[str],
    ) -> None:
        self.context.journal.record_rejected(
            self.logical_call_id,
            phase_attempt_id=self.phase_attempt_id,
            validation_attempt=validation_attempt,
            response=response,
            error=canonical_json(errors),
        )

    def error(self, validation_attempt: int, error: BaseException) -> None:
        self.context.journal.record_error(
            self.logical_call_id,
            phase_attempt_id=self.phase_attempt_id,
            validation_attempt=validation_attempt,
            error=error,
        )

    def accept(
        self,
        response: Mapping[str, Any],
        validation_attempt: int,
        *,
        client: Any,
    ) -> str:
        response_sha = self.context.journal.record_success(
            self.logical_call_id,
            response,
            phase_attempt_id=self.phase_attempt_id,
            validation_attempt=validation_attempt,
        )
        _record_provider_acceptance(
            client,
            logical_call_id=self.logical_call_id,
            phase_attempt_id=self.phase_attempt_id,
            response_sha256=response_sha,
        )
        return response_sha

    def repair_replay_acceptance(self, *, client: Any) -> None:
        if self.replay is None:
            raise ResponseJournalError("repair requested without a replay response")
        _record_provider_acceptance(
            client,
            logical_call_id=self.logical_call_id,
            phase_attempt_id=self.phase_attempt_id,
            response_sha256=self.replay.response_sha256,
        )


def _record_provider_acceptance(
    client: Any,
    *,
    logical_call_id: str,
    phase_attempt_id: str,
    response_sha256: str,
) -> None:
    if bool(getattr(client, "is_offline", False)):
        return
    recorder = getattr(client, "record_experiment_acceptance", None)
    if recorder is None:
        return
    recorder(
        logical_call_id=logical_call_id,
        phase_attempt_id=phase_attempt_id,
        accepted_response_sha256=response_sha256,
    )


def open_journal_call(
    *,
    stage: str,
    schema_version: str,
    base_prompt: str,
    client: Any,
    temperature_schedule: list[float],
    seed_schedule: list[int],
    max_tokens: int,
    validation_attempts: int,
    validation_procedure_version: str,
    model: str | None = None,
    response_format: Mapping[str, Any] | None = None,
    semantic_inputs: Mapping[str, Any] | None = None,
) -> JournalCall | None:
    """Open a logical call using every response-affecting semantic input."""

    context = current_journal_context()
    if context is None:
        return None
    if validation_attempts < 1 or max_tokens < 1:
        raise ValueError("validation_attempts and max_tokens must be positive")
    if len(temperature_schedule) != validation_attempts:
        raise ValueError("temperature schedule length differs from validation attempts")
    if len(seed_schedule) != validation_attempts:
        raise ValueError("seed schedule length differs from validation attempts")
    request_policy_fn = getattr(client, "request_policy", None)
    request_policy = request_policy_fn() if callable(request_policy_fn) else {}
    resolved_model = str(model or getattr(client, "model", "") or "unspecified")
    key = LogicalCallKey(
        run_id=context.run_id,
        condition_id=context.condition_id,
        agent_id=context.agent_id,
        event_id=context.event_id,
        stage=stage,
        schema_version=schema_version,
    )
    request = {
        "base_prompt": base_prompt,
        "semantic_inputs": dict(semantic_inputs or {}),
        "model": resolved_model,
        "temperature_schedule": [float(value) for value in temperature_schedule],
        "seed_schedule": [int(value) for value in seed_schedule],
        "max_tokens": int(max_tokens),
        "response_format": dict(response_format or {"type": "json_object"}),
        "request_policy": request_policy,
        "validation_attempts": int(validation_attempts),
        "validation_procedure_version": validation_procedure_version,
    }
    replay = context.journal.get_accepted(key, request)
    return JournalCall(
        context=context,
        key=key,
        request=request,
        replay=replay,
    )


__all__ = [
    "JOURNAL_SCHEMA_VERSION",
    "JournalCall",
    "JournalEventContext",
    "JournalReplay",
    "LogicalCallKey",
    "ResponseJournal",
    "ResponseJournalDriftError",
    "ResponseJournalError",
    "canonical_sha256",
    "current_journal_context",
    "open_journal_call",
    "parse_strict_json_object",
    "response_journal_scope",
]
