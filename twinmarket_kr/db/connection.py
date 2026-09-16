from __future__ import annotations

import sqlite3
from pathlib import Path

from twinmarket_kr.db.schema import (
    create_agents_table_sql,
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
        _apply_main_sim_migrations(conn)
        conn.commit()


def _apply_main_sim_migrations(conn: sqlite3.Connection) -> None:
    """Apply additive columns required by the single integrated runtime."""

    required_columns = {
        "trade_log": {
            "order_type": "TEXT",
            "submitted_price": "REAL",
            "status": "TEXT NOT NULL DEFAULT 'pending'",
            "filled_quantity": "INTEGER NOT NULL DEFAULT 0",
            "analysis_id": "TEXT REFERENCES simulation_analyses(analysis_id)",
            "decision_id": "TEXT REFERENCES simulation_decisions(decision_id)",
            "source_ltb_id": "TEXT REFERENCES simulation_ltb_states(ltb_id)",
            "source_stb_id": "TEXT REFERENCES simulation_stb_states(stb_id)",
            "fill_id": "TEXT REFERENCES simulation_fills(fill_id)",
            "post_fill_ltb_id": "TEXT REFERENCES simulation_ltb_states(ltb_id)",
        },
        "simulation_ltb_states": {
            # Existing rows remain readable and retain their original
            # scientific hash. New rows always write the normalized six-dim
            # evidence object explicitly.
            "integration_evidence_json": "TEXT NOT NULL DEFAULT '{}'",
        },
        "simulation_decisions": {
            # Databases created before the integrated analysis lineage keep a
            # nullable compatibility column. New runtime writes reject NULL at
            # the MemoryAgent boundary and fresh schemas enforce NOT NULL.
            "analysis_id": "TEXT REFERENCES simulation_analyses(analysis_id)",
        },
        "community_logs": {
            "candidate_posts_seen": "TEXT NOT NULL DEFAULT '[]'",
        },
        "community_posts": {
            "source_ltb_id": "TEXT REFERENCES simulation_ltb_states(ltb_id)",
            "source_fill_id": "TEXT REFERENCES simulation_fills(fill_id)",
            "source_decision_id": "TEXT REFERENCES simulation_decisions(decision_id)",
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


def init_agents_db(db_path: Path | str) -> None:
    with connect(db_path) as conn:
        conn.execute("DROP TABLE IF EXISTS agents")
        conn.execute(create_agents_table_sql())
        conn.commit()
