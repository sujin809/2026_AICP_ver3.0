from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from twinmarket_kr.db.connection import init_sim_db

import config


RUNTIME_TABLES = (
    "TradingDetails",
    "trade_log",
    "agent_system_messages",
    "community_interactions",
    "community_posts",
    "community_logs",
)

DETERMINISTIC_FAILURE_NAMES = (
    "AnalysisValidationError",
    "BeliefValidationError",
    "CommunityValidationError",
    "DecisionConstraintError",
    "DecisionValidationError",
    "LLMValidationError",
    "UnexpectedModelError",
)


class ParallelTaskError(RuntimeError):
    """Preserve every child failure so restart classification cannot miss a mixed error."""

    def __init__(self, message: str, errors: list[BaseException]) -> None:
        super().__init__(message)
        self.errors = tuple(errors)


def _exception_chain(error: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    pending: list[BaseException] = [error]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        chain.append(current)
        seen.add(id(current))
        nested = getattr(current, "errors", ())
        if isinstance(nested, (tuple, list)):
            pending.extend(item for item in nested if isinstance(item, BaseException))
        if current.__context__ is not None:
            pending.append(current.__context__)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
    return chain


def classify_restart_safety(error: BaseException) -> dict[str, Any]:
    """Permit process restarts only for clearly transient provider/transport failures."""
    chain = _exception_chain(error)
    chain_types = [type(item).__name__ for item in chain]
    combined_text = "\n".join(f"{type(item).__name__}: {item}" for item in chain)
    deterministic = sorted(
        name for name in DETERMINISTIC_FAILURE_NAMES if name in combined_text
    )
    if deterministic:
        return {
            "auto_restart_allowed": False,
            "failure_class": "deterministic_validation_or_model",
            "exception_chain": chain_types,
            "matched_markers": deterministic,
        }

    transient_markers: list[str] = []
    transient_type_names = {
        "APIConnectionError",
        "APITimeoutError",
        "ConnectError",
        "ConnectTimeout",
        "ConnectionError",
        "NetworkError",
        "ReadError",
        "ReadTimeout",
        "TimeoutError",
    }
    for item in chain:
        item_type = type(item).__name__
        if item_type in transient_type_names or isinstance(item, TimeoutError):
            transient_markers.append(item_type)
        status_code = getattr(item, "status_code", None)
        if status_code is None:
            response = getattr(item, "response", None)
            status_code = getattr(response, "status_code", None)
        try:
            code = int(status_code) if status_code is not None else None
        except (TypeError, ValueError):
            code = None
        if code in {408, 409, 429} or (code is not None and 500 <= code <= 599):
            transient_markers.append(f"http_{code}")
    if transient_markers:
        return {
            "auto_restart_allowed": True,
            "failure_class": "transient_provider_or_transport",
            "exception_chain": chain_types,
            "matched_markers": sorted(set(transient_markers)),
        }
    return {
        "auto_restart_allowed": False,
        "failure_class": "unknown_or_local_error",
        "exception_chain": chain_types,
        "matched_markers": [],
    }


def file_sha256(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup_database(source: Path | str, target: Path | str) -> None:
    source_path = Path(source)
    target_path = Path(target)
    if not source_path.exists():
        raise FileNotFoundError(f"Simulation source database not found: {source_path}")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(str(target_path) + ".tmp")
    for candidate in (
        temporary,
        Path(str(temporary) + "-wal"),
        Path(str(temporary) + "-shm"),
    ):
        if candidate.exists():
            candidate.unlink()
    with sqlite3.connect(source_path) as src:
        quick_check = str(src.execute("PRAGMA quick_check").fetchone()[0])
        if quick_check.lower() != "ok":
            raise RuntimeError(f"Source SQLite quick_check failed for {source_path}: {quick_check}")
        with sqlite3.connect(temporary) as dst:
            src.backup(dst)
    for companion in (Path(str(target_path) + "-wal"), Path(str(target_path) + "-shm")):
        if companion.exists():
            companion.unlink()
    temporary.replace(target_path)


def build_clean_experiment_base(
    source: Path | str,
    target: Path | str,
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Create an immutable-style base containing market data and turn-zero state only."""
    source_path = Path(source)
    target_path = Path(target)
    if source_path.resolve() == target_path.resolve():
        raise ValueError("Experiment base output must differ from the source simulation DB")
    if target_path.exists() and not overwrite:
        raise FileExistsError(f"Experiment base already exists: {target_path}")
    if target_path.exists():
        target_path.unlink()
    for companion in (Path(str(target_path) + "-wal"), Path(str(target_path) + "-shm")):
        if companion.exists():
            companion.unlink()
    backup_database(source_path, target_path)
    init_sim_db(target_path)
    with sqlite3.connect(target_path) as connection:
        for table in RUNTIME_TABLES:
            connection.execute(f"DELETE FROM {table}")
        connection.execute("DELETE FROM belief_history WHERE turn <> 0")
        connection.execute("DELETE FROM portfolio_state WHERE turn <> 0")
        sequence_tables = (
            "TradingDetails",
            "agent_system_messages",
            "community_interactions",
            "community_posts",
            "community_logs",
        )
        placeholders = ",".join("?" for _ in sequence_tables)
        connection.execute(
            f"DELETE FROM sqlite_sequence WHERE name IN ({placeholders})",
            sequence_tables,
        )
        connection.commit()
    report = validate_clean_experiment_base(target_path)
    report["source_db"] = str(source_path.resolve())
    report["base_db"] = str(target_path.resolve())
    report["sha256"] = file_sha256(target_path)
    return report


def validate_clean_experiment_base(path: Path | str) -> dict[str, Any]:
    db_path = Path(path)
    with sqlite3.connect(db_path) as connection:
        counts = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in RUNTIME_TABLES
        }
        belief_turns = connection.execute(
            "SELECT MIN(turn), MAX(turn), COUNT(*), COUNT(DISTINCT agent_id) FROM belief_history"
        ).fetchone()
        portfolio_turns = connection.execute(
            "SELECT MIN(turn), MAX(turn), COUNT(*), COUNT(DISTINCT agent_id) FROM portfolio_state"
        ).fetchone()
        portfolio_rows = connection.execute(
            """
            SELECT agent_id, cash, positions, total_value, realized_pnl, total_return_rate
            FROM portfolio_state
            WHERE turn = 0
            ORDER BY agent_id
            """
        ).fetchall()
        belief_rows = connection.execute(
            """
            SELECT agent_id, dim_1, dim_2, dim_3, dim_4, dim_5, dim_6,
                   belief_summary, view_change
            FROM belief_history
            WHERE turn = 0
            ORDER BY agent_id
            """
        ).fetchall()
        stock_rows = int(connection.execute("SELECT COUNT(*) FROM StockData").fetchone()[0])
    nonempty_runtime = {table: count for table, count in counts.items() if count}
    if nonempty_runtime:
        raise RuntimeError(f"Experiment base contains runtime rows: {nonempty_runtime}")
    if not belief_turns or belief_turns[0] != 0 or belief_turns[1] != 0:
        raise RuntimeError(f"Experiment base belief state is not turn zero only: {belief_turns}")
    if not portfolio_turns or portfolio_turns[0] != 0 or portfolio_turns[1] != 0:
        raise RuntimeError(f"Experiment base portfolio state is not turn zero only: {portfolio_turns}")
    if int(belief_turns[3]) != int(portfolio_turns[3]):
        raise RuntimeError(
            "Experiment base agent coverage differs between belief and portfolio state: "
            f"belief={belief_turns[3]} portfolio={portfolio_turns[3]}"
        )
    if int(belief_turns[2]) != int(belief_turns[3]):
        raise RuntimeError("Experiment base contains duplicate turn-zero belief rows")
    if int(portfolio_turns[2]) != int(portfolio_turns[3]):
        raise RuntimeError("Experiment base contains duplicate turn-zero portfolio rows")
    belief_agents = {str(row[0]) for row in belief_rows}
    portfolio_agents = {str(row[0]) for row in portfolio_rows}
    if belief_agents != portfolio_agents:
        raise RuntimeError("Experiment base belief and portfolio agent IDs differ")
    empty_beliefs = [
        str(row[0])
        for row in belief_rows
        if any(not str(value or "").strip() for value in row[1:])
    ]
    if empty_beliefs:
        raise RuntimeError(
            "Experiment base has incomplete initial belief dimensions/summary/view_change: "
            f"{empty_beliefs[:5]}"
        )
    invalid_positions: list[str] = []
    invalid_financial_state: list[str] = []
    for agent_id, cash, positions, total_value, realized_pnl, total_return_rate in portfolio_rows:
        try:
            decoded_positions = json.loads(positions or "[]")
        except (TypeError, json.JSONDecodeError):
            decoded_positions = None
        if not isinstance(decoded_positions, list) or any(
            not isinstance(position, dict) or int(position.get("quantity") or 0) != 0
            for position in (decoded_positions or [])
        ):
            invalid_positions.append(str(agent_id))
        if (
            float(cash) <= 0
            or abs(float(total_value) - float(cash)) > 1e-6
            or abs(float(realized_pnl)) > 1e-9
            or abs(float(total_return_rate)) > 1e-9
        ):
            invalid_financial_state.append(str(agent_id))
    if invalid_positions:
        raise RuntimeError(
            f"Experiment base turn-zero portfolios contain positions: {invalid_positions[:5]}"
        )
    if invalid_financial_state:
        raise RuntimeError(
            "Experiment base turn-zero cash/value/PnL is inconsistent: "
            f"{invalid_financial_state[:5]}"
        )
    if not config.SYS_100_DB.exists():
        raise FileNotFoundError(f"Agent source DB is missing: {config.SYS_100_DB}")
    with sqlite3.connect(config.SYS_100_DB) as agent_connection:
        initial_cash_by_agent = {
            str(agent_id): float(initial_cash)
            for agent_id, initial_cash in agent_connection.execute(
                "SELECT agent_id, ini_cash FROM agents"
            ).fetchall()
        }
    if set(initial_cash_by_agent) != portfolio_agents:
        raise RuntimeError(
            "Experiment base turn-zero cohort differs from sys_100.db: "
            f"base={len(portfolio_agents)} sys_100={len(initial_cash_by_agent)}"
        )
    cash_mismatches = [
        str(agent_id)
        for agent_id, cash, *_rest in portfolio_rows
        if abs(float(cash) - initial_cash_by_agent[str(agent_id)]) > 1e-6
    ]
    if cash_mismatches:
        raise RuntimeError(
            f"Experiment base initial cash differs from persona cohort: {cash_mismatches[:5]}"
        )
    if stock_rows <= 0:
        raise RuntimeError("Experiment base contains no StockData rows")
    return {
        "status": "pass",
        "stock_rows": stock_rows,
        "turn_zero_beliefs": int(belief_turns[2]),
        "turn_zero_portfolios": int(portfolio_turns[2]),
        "agent_count": int(belief_turns[3]),
        "initial_cash_values": sorted({float(row[1]) for row in portfolio_rows}),
        "initial_positions_empty": True,
        "runtime_counts": counts,
    }
