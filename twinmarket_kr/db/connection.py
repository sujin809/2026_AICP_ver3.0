from __future__ import annotations

import sqlite3
from pathlib import Path

from twinmarket_kr.db.schema import (
    PAPER_ANALYSES_DDL,
    PAPER_RUNTIME_TABLES,
    SIM_SCHEMA_VERSION,
    create_agents_table_sql,
    create_paper_sim_tables_sql,
    create_sim_tables_sql,
)


class ManagedConnection(sqlite3.Connection):
    """A sqlite connection whose context manager also releases the file handle."""

    def __exit__(self, exc_type, exc_value, traceback):  # type: ignore[no-untyped-def]
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect(db_path: Path | str, *, read_only: bool = False) -> sqlite3.Connection:
    path = Path(db_path)
    if read_only:
        if not path.exists():
            raise FileNotFoundError(f"SQLite database not found: {path}")
        # sys_100.db is shared reference data. Never acquire a write journal for it.
        target: str | Path = f"file:{path.resolve().as_posix()}?mode=ro"
        conn = sqlite3.connect(
            target,
            uri=True,
            timeout=30.0,
            check_same_thread=False,
            factory=ManagedConnection,
        )
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=30.0, check_same_thread=False, factory=ManagedConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if read_only:
        conn.execute("PRAGMA query_only = ON")
    else:
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_sim_db(db_path: Path | str) -> None:
    with connect(db_path) as conn:
        for ddl in create_sim_tables_sql():
            conn.execute(ddl)


def init_paper_sim_db(db_path: Path | str) -> None:
    """Initialize only an RN paper-run database.

    This is deliberately separate from :func:`init_sim_db` so loading a legacy
    0720 database cannot create or migrate the new scientific tables.
    """
    with connect(db_path) as conn:
        analysis_table_preexisting = (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'paper_analyses'"
            ).fetchone()
            is not None
        )
        post_trace_table_preexisting = (
            conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'community_post_trace'"
            ).fetchone()
            is not None
        )
        for ddl in create_paper_sim_tables_sql():
            conn.execute(ddl)
        _upgrade_empty_analysis_contract_schema(
            conn, analysis_table_preexisting=analysis_table_preexisting
        )
        _upgrade_empty_human_log_contract_schema(conn)
        _upgrade_empty_post_trace_contract_schema(
            conn, post_trace_table_preexisting=post_trace_table_preexisting
        )
        _apply_additive_sim_migrations(conn)
        conn.execute(
            """
            INSERT INTO rn_schema_meta (schema_key, schema_value)
            VALUES ('schema_version', ?)
            ON CONFLICT(schema_key) DO UPDATE SET schema_value = excluded.schema_value
            """,
            (SIM_SCHEMA_VERSION,),
        )
        conn.commit()


def _apply_additive_sim_migrations(conn: sqlite3.Connection) -> None:
    """Keep existing local databases readable while scientific tables evolve.

    Only nullable/defaulted metadata columns are added here.  Scientific rows
    are never rewritten as part of schema initialization.
    """
    required_columns = {
        "short_term_belief_history": {
            "dimension_evidence_json": "TEXT NOT NULL DEFAULT '{}'",
        },
        "ltb_dimension_transitions": {
            "transaction_episode_json": "TEXT NOT NULL DEFAULT '{}'",
            "integration_evidence_by_dimension_json": "TEXT NOT NULL DEFAULT '{}'",
        },
        "paper_decisions": {
            "date": "TEXT NOT NULL DEFAULT ''",
            "subturn": "TEXT NOT NULL DEFAULT ''",
            # An existing local RN database cannot safely be backfilled with a
            # synthetic analysis row during initialization.  Leave the legacy
            # value explicitly empty; new rows are constrained by the fresh
            # schema and validated by the paper persistence boundary.
            "analysis_id": "TEXT NOT NULL DEFAULT ''",
            "input_sha256": "TEXT NOT NULL DEFAULT ''",
            "response_sha256": "TEXT NOT NULL DEFAULT ''",
        },
    }
    for table, columns in required_columns.items():
        existing = {
            str(row["name"])
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _upgrade_empty_analysis_contract_schema(
    conn: sqlite3.Connection, *, analysis_table_preexisting: bool
) -> None:
    """Install the v7 analysis shape only in a wholly empty paper database.

    The old four-field bridge discarded the nine-field legacy market analysis.
    SQLite cannot replace that table contract in place.  If *any* scientific
    paper table already has a row, changing the analysis shape would mutate a
    started experiment, so initialization fails closed and requires a new
    run-scoped database.
    """

    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'paper_analyses'"
    ).fetchone()
    if row is None:
        raise RuntimeError("RN paper schema did not create paper_analyses")
    columns = {
        str(item["name"]) for item in conn.execute("PRAGMA table_info(paper_analyses)")
    }
    expected_columns = {
        "analysis_id",
        "run_id",
        "condition_id",
        "manifest_sha256",
        "agent_id",
        "event_id",
        "turn",
        "date",
        "subturn",
        "source_ltb_id",
        "source_stb_id",
        "input_sha256",
        "response_sha256",
        "market_view",
        "valuation_view",
        "technical_view",
        "news_view",
        "portfolio_view",
        "key_risks_json",
        "opportunity_json",
        "caution_json",
        "confidence",
        "directional_stance",
        "evidence_references_json",
        "scientific_sha256",
        "created_at",
    }
    definition = str(row["sql"] or "").lower()
    if analysis_table_preexisting and columns == expected_columns and "'uncertain'" in definition:
        return
    populated = []
    for table in PAPER_RUNTIME_TABLES:
        count = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        if count:
            populated.append(f"{table}={count}")
    if populated:
        raise RuntimeError(
            "RN paper database has a pre-v7 analysis contract and scientific rows; "
            "create a new run-scoped database instead of migrating it: " + ", ".join(populated)
        )
    conn.execute("DROP TABLE paper_analyses")
    conn.execute(PAPER_ANALYSES_DDL)


def _upgrade_empty_human_log_contract_schema(conn: sqlite3.Connection) -> None:
    """Add the v8 human-log columns only before scientific work has started.

    The missing values cannot be reconstructed from a partially populated old
    database with sufficient certainty: an old LTB row did not persist the
    renderer identity or exact rendered bytes.  Therefore an existing
    scientific row makes this migration fail closed instead of synthesizing
    human text or silently changing a started experiment.
    """

    required_columns = {
        "paper_ltb_states": {
            "belief_summary": "TEXT NOT NULL DEFAULT ''",
            "view_change_json": "TEXT NOT NULL DEFAULT ''",
            "human_log_renderer_version": "TEXT NOT NULL DEFAULT ''",
            "human_log_renderer_sha256": "TEXT NOT NULL DEFAULT ''",
            "human_log_sha256": "TEXT NOT NULL DEFAULT ''",
        },
        "turn_belief_trace": {
            "human_log_json": "TEXT NOT NULL DEFAULT ''",
            "human_log_renderer_version": "TEXT NOT NULL DEFAULT ''",
            "human_log_renderer_sha256": "TEXT NOT NULL DEFAULT ''",
            "human_log_sha256": "TEXT NOT NULL DEFAULT ''",
        },
    }
    missing: dict[str, dict[str, str]] = {}
    for table, columns in required_columns.items():
        existing = {
            str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")
        }
        absent = {name: ddl for name, ddl in columns.items() if name not in existing}
        if absent:
            missing[table] = absent
    version_row = conn.execute(
        "SELECT schema_value FROM rn_schema_meta WHERE schema_key = 'schema_version'"
    ).fetchone()
    prior_version = None if version_row is None else str(version_row["schema_value"])
    if not missing and prior_version in {"rn_ab_v8", SIM_SCHEMA_VERSION}:
        return

    populated = []
    for table in PAPER_RUNTIME_TABLES:
        count = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        if count:
            populated.append(f"{table}={count}")
    if populated:
        raise RuntimeError(
            "RN paper database is not sealed as the v8 deterministic human-log "
            "contract and already has scientific rows; create a new run-scoped "
            "database instead of synthesizing or relabeling human logs: "
            + ", ".join(populated)
        )

    for table, columns in missing.items():
        for name, ddl in columns.items():
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _upgrade_empty_post_trace_contract_schema(
    conn: sqlite3.Connection,
    *,
    post_trace_table_preexisting: bool,
) -> None:
    """Install the v9 posting trace only before a paper run has started.

    A populated v8 database may already contain community posts and accepted
    posting responses that cannot be reconstructed with the exact prompt-value
    and response hashes required by v9.  Such a run must remain labelled v8
    and be restarted in a fresh run-scoped database.
    """

    columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(community_post_trace)")
    }
    required = {
        "trace_id",
        "run_id",
        "condition_id",
        "manifest_sha256",
        "phase_id",
        "event_id",
        "turn",
        "date",
        "author_agent_id",
        "eligibility_status",
        "posting_status",
        "post_id",
        "ltb_id",
        "ltb_sha256",
        "view_change_id",
        "view_change_sha256",
        "fill_id",
        "prompt_template_sha256",
        "prompt_values_sha256",
        "logical_call_id",
        "accepted_response_sha256",
        "title_sha256",
        "body_sha256",
        "trace_sha256",
        "created_at",
    }
    if columns != required:
        raise RuntimeError("RN v9 community_post_trace schema is incomplete")
    if post_trace_table_preexisting:
        return

    populated = []
    for table in PAPER_RUNTIME_TABLES:
        if table == "community_post_trace":
            continue
        count = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        if count:
            populated.append(f"{table}={count}")
    if populated:
        raise RuntimeError(
            "RN paper database predates the v9 private community-post trace "
            "contract and already has scientific rows; create a new run-scoped "
            "database instead of reconstructing post provenance: "
            + ", ".join(populated)
        )


def init_agents_db(db_path: Path | str) -> None:
    with connect(db_path) as conn:
        conn.execute("DROP TABLE IF EXISTS agents")
        conn.execute(create_agents_table_sql())
        conn.commit()
