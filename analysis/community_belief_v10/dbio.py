import sqlite3
from pathlib import Path

import pandas as pd

import guard  # noqa: F401  무과금 강제

guard.enforce_no_paid_api()


def connect(db_file: Path) -> sqlite3.Connection:
    """원본 DB를 읽기 전용으로 연다.

    run DB는 WAL 상태라 mode=ro로는 열리지 않는다(실측 2026-08-03).
    immutable=1을 써야 하며, 이는 원본을 절대 수정하지 않겠다는 보증이기도 하다.
    """
    if not db_file.exists():
        raise FileNotFoundError(f"DB가 없다: {db_file}")
    return sqlite3.connect(f"file:{db_file}?immutable=1", uri=True)


def read_table(conn: sqlite3.Connection, name: str) -> pd.DataFrame:
    return pd.read_sql_query(f"select * from {name}", conn)


def table_count(conn: sqlite3.Connection, name: str) -> int:
    return int(conn.execute(f"select count(*) from {name}").fetchone()[0])
