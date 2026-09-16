from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import re
import sqlite3
from typing import Any


SYSTEM_USERS = {"INSTITUTIONAL"}
HIERARCHICAL_TABLES = {
    "stb": "simulation_stb_states",
    "ltb": "simulation_ltb_states",
    "analysis": "simulation_analyses",
    "decision": "simulation_decisions",
    "fill": "simulation_fills",
    "outcome": "simulation_trade_outcomes",
    "outcome_consumption": "simulation_outcome_consumptions",
}
OUTCOME_HORIZONS = ("next_turn", "h1", "h5")


def num(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def row_agent_id(row: dict[str, Any]) -> str:
    return str(row.get("agent_id") or row.get("user_id") or "")


def row_quantity(row: dict[str, Any]) -> float:
    return num(row.get("quantity") or row.get("executed_quantity") or row.get("filled_quantity"))


def row_price(row: dict[str, Any]) -> float:
    return num(row.get("price") or row.get("executed_price") or row.get("announced_price") or row.get("close_price"))


def is_reaction_row(row: dict[str, Any]) -> bool:
    """Count only the original PM full-body read, not title exposure or AM replay."""
    delivery_status = str(row.get("delivery_status") or "")
    if delivery_status:
        return delivery_status == "read_pm" and bool(row.get("reaction"))
    return bool(row.get("reaction"))


def csv_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(
                    f"{path}:{line_number} must contain a JSON object"
                )
            rows.append(value)
    return rows


def _run_local_path(run_dir: Path, value: Any) -> Path | None:
    """Resolve evidence only when it is physically inside the explicit run."""
    if value in (None, ""):
        return None
    candidate = Path(str(value))
    if not candidate.is_absolute():
        candidate = run_dir / candidate
    try:
        resolved = candidate.resolve()
        resolved.relative_to(run_dir.resolve())
    except (OSError, ValueError):
        return None
    return resolved if resolved.is_file() else None


def _resolve_sim_db(run_dir: Path, metadata: dict[str, Any]) -> Path | None:
    """Resolve the immutable run-local DB before any mutable/legacy path.

    The integrated ``05`` runner checkpoints each committed event into
    ``.runtime/committed.db``.  Reports must prefer that durable prefix over
    ``runtime_db`` (the mutable working copy).  ``sim_db`` is retained only as
    a compatibility key for older, explicitly supplied report fixtures.
    """

    run_root = run_dir.resolve()
    candidates: list[Path] = [run_dir / ".runtime" / "committed.db"]
    for key in ("runtime_db", "sim_db"):
        value = metadata.get(key)
        if value in (None, ""):
            continue
        candidate = Path(str(value))
        if not candidate.is_absolute():
            candidate = run_dir / candidate
        candidates.append(candidate)
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            resolved.relative_to(run_root)
        except (OSError, ValueError):
            continue
        if resolved.is_file():
            return resolved
    return None


def _readonly_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row["name"])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _scope(
    metadata: dict[str, Any],
    agent_ids: list[str],
    turn_rows: list[dict[str, Any]],
) -> tuple[list[str], list[int], int]:
    agents = sorted(
        {
            str(value)
            for value in agent_ids
            if value not in (None, "")
        }
    )
    turns = sorted(
        {
            int(row["turn"])
            for row in turn_rows
            if row.get("turn") not in (None, "")
        }
    )
    if not turns:
        start = int(metadata.get("global_turn_start") or 1)
        end = int(
            metadata.get("global_turn_end")
            or (start + int(metadata.get("turn_count") or 0) - 1)
        )
        if end >= start:
            turns = list(range(start, end + 1))
    expected = len(agents) * len(turns)
    return agents, turns, expected


def _where_agent_turn(
    alias: str,
    agent_ids: list[str],
    turns: list[int],
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    values: list[Any] = []
    if agent_ids:
        clauses.append(
            f"{alias}.agent_id IN ({','.join('?' for _ in agent_ids)})"
        )
        values.extend(agent_ids)
    if turns:
        clauses.append(f"{alias}.turn IN ({','.join('?' for _ in turns)})")
        values.extend(turns)
    return (" AND ".join(clauses) or "1 = 1"), values


def summarize_canonical_lineage(
    run_dir: Path,
    *,
    metadata: dict[str, Any],
    agent_ids: list[str],
    turn_rows: list[dict[str, Any]],
    community_posts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Summarize the canonical recursive-memory chain without mutating it.

    Scientific state is read from the isolated simulation DB, including the
    explicit analysis row between STB and decision.  The existing
    ``agent_turns.jsonl`` analysis export is counted separately so the report
    catches a DB/export mismatch.  Community post linkage is checked against
    both the CSV export and the DB chain.
    """
    agents, turns, expected = _scope(metadata, agent_ids, turn_rows)
    analysis_log_count = sum(
        1
        for row in turn_rows
        if str((row.get("agent") or {}).get("agent_id") or row.get("agent_id") or "")
        in agents
        and int(row.get("turn") or 0) in turns
        and isinstance(row.get("market_analysis"), dict)
        and bool(row.get("market_analysis"))
    )
    base: dict[str, Any] = {
        "status": "missing_db",
        "db_path": None,
        "agent_count": len(agents),
        "event_count": len(turns),
        "expected_event_rows": expected,
        "initial_ltb_count": 0,
        "stb_count": 0,
        "analysis_count": 0,
        "analysis_log_count": analysis_log_count,
        "decision_count": 0,
        "fill_count": 0,
        "post_fill_ltb_count": 0,
        "complete_chain_count": 0,
        "outcome_matured_count": 0,
        "outcome_right_censored_count": 0,
        "outcome_consumed_count": 0,
        "outcome_consumption_linked_count": 0,
        "outcome_total_count": 0,
        "expected_terminal_outcome_count": 0,
        "outcome_horizon_counts": {
            horizon: {
                "matured_count": 0,
                "right_censored_count": 0,
                "consumed_count": 0,
                "consumption_linked_count": 0,
                "total_count": 0,
                "expected_terminal_count": 0,
            }
            for horizon in OUTCOME_HORIZONS
        },
        "community_post_count": len(community_posts or []),
        "community_post_linked_count": 0,
        "missing_tables": [],
    }
    if not agents or not turns:
        base["status"] = "missing_scope"
        return base
    db_path = _resolve_sim_db(run_dir, metadata)
    if db_path is None:
        return base
    base["db_path"] = str(db_path)
    with _readonly_connection(db_path) as connection:
        available = _table_names(connection)
        missing = sorted(set(HIERARCHICAL_TABLES.values()) - available)
        if missing:
            base["status"] = "missing_tables"
            base["missing_tables"] = missing
            return base

        where, values = _where_agent_turn("s", agents, turns)
        base["stb_count"] = int(
            connection.execute(
                f"SELECT COUNT(*) AS n FROM simulation_stb_states AS s WHERE {where}",
                values,
            ).fetchone()["n"]
        )
        where, values = _where_agent_turn("a", agents, turns)
        base["analysis_count"] = int(
            connection.execute(
                f"SELECT COUNT(*) AS n FROM simulation_analyses AS a WHERE {where}",
                values,
            ).fetchone()["n"]
        )
        where, values = _where_agent_turn("d", agents, turns)
        base["decision_count"] = int(
            connection.execute(
                f"SELECT COUNT(*) AS n FROM simulation_decisions AS d WHERE {where}",
                values,
            ).fetchone()["n"]
        )
        where, values = _where_agent_turn("f", agents, turns)
        base["fill_count"] = int(
            connection.execute(
                f"SELECT COUNT(*) AS n FROM simulation_fills AS f WHERE {where}",
                values,
            ).fetchone()["n"]
        )
        where, values = _where_agent_turn("l", agents, turns)
        base["post_fill_ltb_count"] = int(
            connection.execute(
                f"""
                SELECT COUNT(*) AS n
                FROM simulation_ltb_states AS l
                WHERE l.turn > 0 AND {where}
                """,
                values,
            ).fetchone()["n"]
        )
        if agents:
            base["initial_ltb_count"] = int(
                connection.execute(
                    f"""
                    SELECT COUNT(*) AS n
                    FROM simulation_ltb_states
                    WHERE turn = 0
                      AND agent_id IN ({','.join('?' for _ in agents)})
                    """,
                    agents,
                ).fetchone()["n"]
            )

        where, values = _where_agent_turn("s", agents, turns)
        base["complete_chain_count"] = int(
            connection.execute(
                f"""
                SELECT COUNT(*) AS n
                FROM simulation_stb_states AS s
                JOIN simulation_analyses AS a
                  ON a.agent_id = s.agent_id
                 AND a.turn = s.turn
                 AND a.source_stb_id = s.stb_id
                JOIN simulation_decisions AS d
                  ON d.agent_id = a.agent_id
                 AND d.turn = a.turn
                 AND d.analysis_id = a.analysis_id
                 AND d.source_stb_id = s.stb_id
                 AND d.source_ltb_id = a.source_ltb_id
                JOIN simulation_fills AS f
                  ON f.agent_id = d.agent_id
                 AND f.turn = d.turn
                 AND f.decision_id = d.decision_id
                 AND f.source_stb_id = d.source_stb_id
                 AND f.source_ltb_id = d.source_ltb_id
                JOIN simulation_ltb_states AS l
                  ON l.agent_id = f.agent_id
                 AND l.turn = f.turn
                 AND l.parent_ltb_id = f.source_ltb_id
                 AND l.source_stb_id = f.source_stb_id
                 AND l.source_decision_id = f.decision_id
                 AND l.source_fill_id = f.fill_id
                WHERE {where}
                """,
                values,
            ).fetchone()["n"]
        )

        where, values = _where_agent_turn("f", agents, turns)
        outcome_rows = connection.execute(
            f"""
            SELECT outcome.horizon, outcome.status, COUNT(*) AS n
            FROM simulation_trade_outcomes AS outcome
            JOIN simulation_fills AS f ON f.fill_id = outcome.fill_id
            WHERE {where}
            GROUP BY outcome.horizon, outcome.status
            """,
            values,
        ).fetchall()
        horizon_counts = base["outcome_horizon_counts"]
        for row in outcome_rows:
            horizon = str(row["horizon"])
            status = str(row["status"])
            count = int(row["n"])
            counts = horizon_counts.setdefault(
                horizon,
                {
                    "matured_count": 0,
                    "right_censored_count": 0,
                    "consumed_count": 0,
                    "consumption_linked_count": 0,
                    "total_count": 0,
                    "expected_terminal_count": 0,
                },
            )
            if status == "matured":
                counts["matured_count"] += count
            elif status == "right_censored":
                counts["right_censored_count"] += count
            counts["total_count"] += count

        consumed_rows = connection.execute(
            f"""
            SELECT consumed.horizon, COUNT(*) AS n
            FROM simulation_outcome_consumptions AS consumed
            JOIN simulation_fills AS f ON f.fill_id = consumed.fill_id
            WHERE {where}
            GROUP BY consumed.horizon
            """,
            values,
        ).fetchall()
        for row in consumed_rows:
            horizon = str(row["horizon"])
            counts = horizon_counts.setdefault(
                horizon,
                {
                    "matured_count": 0,
                    "right_censored_count": 0,
                    "consumed_count": 0,
                    "consumption_linked_count": 0,
                    "total_count": 0,
                    "expected_terminal_count": 0,
                },
            )
            counts["consumed_count"] = int(row["n"])

        linked_rows = connection.execute(
            f"""
            SELECT outcome.horizon, COUNT(*) AS n
            FROM simulation_outcome_consumptions AS consumed
            JOIN simulation_trade_outcomes AS outcome
              ON outcome.outcome_id = consumed.outcome_id
             AND outcome.fill_id = consumed.fill_id
             AND outcome.horizon = consumed.horizon
            JOIN simulation_fills AS f
              ON f.fill_id = outcome.fill_id
            JOIN simulation_ltb_states AS l
              ON l.ltb_id = consumed.ltb_id
             AND l.agent_id = f.agent_id
            WHERE {where}
            GROUP BY outcome.horizon
            """,
            values,
        ).fetchall()
        for row in linked_rows:
            horizon = str(row["horizon"])
            horizon_counts[horizon]["consumption_linked_count"] = int(row["n"])

        expected_per_horizon = int(base["fill_count"])
        for horizon in OUTCOME_HORIZONS:
            horizon_counts[horizon][
                "expected_terminal_count"
            ] = expected_per_horizon
        base["outcome_matured_count"] = sum(
            int(counts["matured_count"])
            for counts in horizon_counts.values()
        )
        base["outcome_right_censored_count"] = sum(
            int(counts["right_censored_count"])
            for counts in horizon_counts.values()
        )
        base["outcome_total_count"] = sum(
            int(counts["total_count"]) for counts in horizon_counts.values()
        )
        base["expected_terminal_outcome_count"] = (
            expected_per_horizon * len(OUTCOME_HORIZONS)
        )
        base["outcome_consumed_count"] = sum(
            int(counts["consumed_count"])
            for counts in horizon_counts.values()
        )
        base["outcome_consumption_linked_count"] = sum(
            int(counts["consumption_linked_count"])
            for counts in horizon_counts.values()
        )

        posts = list(community_posts or [])
        if posts:
            where, values = _where_agent_turn("l", agents, turns)
            valid_links = {
                (
                    str(row["ltb_id"]),
                    str(row["fill_id"]),
                    str(row["decision_id"]),
                )
                for row in connection.execute(
                    f"""
                    SELECT
                        l.ltb_id,
                        f.fill_id,
                        d.decision_id
                    FROM simulation_ltb_states AS l
                    JOIN simulation_fills AS f
                      ON f.fill_id = l.source_fill_id
                    JOIN simulation_decisions AS d
                      ON d.decision_id = l.source_decision_id
                    WHERE l.agent_id = f.agent_id
                      AND l.agent_id = d.agent_id
                      AND l.turn = f.turn
                      AND l.turn = d.turn
                      AND {where}
                    """,
                    values,
                ).fetchall()
            }
            base["community_post_linked_count"] = sum(
                (
                    str(post.get("source_ltb_id") or ""),
                    str(post.get("source_fill_id") or ""),
                    str(post.get("source_decision_id") or ""),
                )
                in valid_links
                for post in posts
            )

    full_chain = (
        expected > 0
        and base["initial_ltb_count"] == len(agents)
        and all(
            base[key] == expected
            for key in (
                "stb_count",
                "analysis_count",
                "analysis_log_count",
                "decision_count",
                "fill_count",
                "post_fill_ltb_count",
                "complete_chain_count",
            )
        )
    )
    posts_linked = (
        base["community_post_count"] == base["community_post_linked_count"]
    )
    base["status"] = "complete" if full_chain and posts_linked else "incomplete"
    return base


def summarize_community_exposures(
    interactions: list[dict[str, Any]],
    best_posts: list[dict[str, Any]],
    posts: list[dict[str, Any]],
) -> dict[str, Any]:
    title_only = [
        row for row in interactions if row.get("exposure_level") == "title_only"
    ]
    full_body = [
        row for row in interactions if row.get("exposure_level") == "full_body"
    ]
    pm_reads = [row for row in full_body if is_reaction_row(row)]
    best_deliveries = [
        row
        for row in full_body
        if row.get("delivery_status") == "delivered_am"
        and csv_bool(row.get("is_best"))
    ]
    selected_replays = [
        row
        for row in full_body
        if row.get("delivery_status") == "delivered_am"
        and csv_bool(row.get("replay"))
    ]
    author_by_post: dict[str, str] = {}
    for row in [*posts, *best_posts]:
        post_id = str(row.get("post_id") or "")
        author = str(
            row.get("author_agent_id") or row.get("agent_id") or ""
        )
        if post_id and author:
            author_by_post[post_id] = author
    violations = [
        row
        for row in best_deliveries
        if str(row.get("agent_id") or "")
        == author_by_post.get(str(row.get("post_id") or ""))
    ]
    provenance_ids = [
        str(row.get("provenance_id") or "")
        for row in interactions
        if row.get("provenance_id")
    ]
    missing_full_body = sum(
        not str(row.get("content") or "").strip() for row in full_body
    )
    missing_body_hash = sum(
        re.fullmatch(r"[0-9a-f]{64}", str(row.get("body_sha256") or ""))
        is None
        for row in full_body
    )
    known_post_ids = {
        str(row.get("post_id") or "")
        for row in [*posts, *best_posts]
        if row.get("post_id") not in (None, "")
    }
    return {
        "title_only_count": len(title_only),
        "full_body_count": len(full_body),
        "pm_selected_full_body_count": len(pm_reads),
        "best_full_body_delivery_count": len(best_deliveries),
        "selected_full_body_replay_count": len(selected_replays),
        "self_excluded_count": sum(
            int(num(row.get("self_excluded_count")))
            for row in best_posts
        ),
        "self_delivery_violation_count": len(violations),
        "missing_full_body_count": missing_full_body,
        "missing_body_hash_count": missing_body_hash,
        "title_only_body_leak_count": sum(
            bool(str(row.get("content") or "").strip()) for row in title_only
        ),
        "orphan_exposure_count": sum(
            str(row.get("post_id") or "") not in known_post_ids
            for row in interactions
        ),
        "provenance_count": len(provenance_ids),
        "duplicate_provenance_count": len(provenance_ids)
        - len(set(provenance_ids)),
        "best_delivery_statuses": dict(
            sorted(
                Counter(
                    str(row.get("delivery_status") or "unknown")
                    for row in best_posts
                ).items()
            )
        ),
    }


def _reasoning_audit_paths(
    run_dir: Path,
    metadata: dict[str, Any],
) -> list[Path]:
    paths: set[Path] = set()
    for key in (
        "openrouter_audit_log",
        "openrouter_audit_path",
        "api_audit_path",
        "reasoning_audit_log",
        "reasoning_audit_path",
    ):
        path = _run_local_path(run_dir, metadata.get(key))
        if path is not None:
            paths.add(path)
    for relative in (
        "openrouter_calls.jsonl",
        "reasoning_audit.jsonl",
        "traces/openrouter_calls.jsonl",
        "traces/reasoning_audit.jsonl",
    ):
        path = _run_local_path(run_dir, relative)
        if path is not None:
            paths.add(path)
    return sorted(paths)


def summarize_reasoning_off(
    run_dir: Path,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Describe strict-off policy and run-local provider telemetry.

    A process-wide audit is deliberately ignored: it cannot prove which run
    produced a response.  ``verified`` is used only when every successful
    provider return has zero reasoning tokens, no reasoning payload, the pinned
    model/provider, and a strict no-fallback request policy.
    """
    configured = (
        metadata.get("openrouter_call_policy")
        or metadata.get("call_policy")
        or {}
    )
    reasoning = configured.get("reasoning") or {
        "effort": configured.get("reasoning_effort"),
        "exclude": configured.get("reasoning_exclude"),
    }
    raw_provider = configured.get("provider")
    if isinstance(raw_provider, str):
        provider_policy = {
            "only": [raw_provider],
            "order": [raw_provider],
            "allow_fallbacks": configured.get("allow_fallbacks"),
            "require_parameters": configured.get("require_parameters"),
        }
    else:
        provider_policy = raw_provider or {}
    providers = list(provider_policy.get("only") or [])
    policy_strict = (
        reasoning == {"effort": "none", "exclude": True}
        and len(providers) == 1
        and list(provider_policy.get("order") or []) == providers
        and provider_policy.get("allow_fallbacks") is False
        and provider_policy.get("require_parameters") is True
        and bool(configured.get("model"))
    )
    paths = _reasoning_audit_paths(run_dir, metadata)
    rows = [row for path in paths for row in read_jsonl(path)]
    provider_rows = [
        row
        for row in rows
        if row.get("audit_event", "provider_attempt") == "provider_attempt"
    ]
    returned = [
        row
        for row in provider_rows
        if row.get("status") in {"success", "provider_returned"}
    ]
    errors = [row for row in provider_rows if row.get("status") == "error"]
    accepted = [
        row
        for row in rows
        if row.get("audit_event") == "experiment_acceptance"
        and row.get("status") == "accepted"
    ]
    expected_model = str(configured.get("model") or "")
    expected_provider = providers[0] if len(providers) == 1 else ""
    expected_request_policy = {
        "reasoning": reasoning,
        "provider": provider_policy,
    }
    compliant = [
        row
        for row in returned
        if row.get("reasoning_tokens") == 0
        and row.get("response_reasoning_present") is False
        and str(row.get("requested_model") or "") == expected_model
        and str(row.get("returned_model") or "") == expected_model
        and str(row.get("provider") or "") == expected_provider
        and row.get("request_policy") == expected_request_policy
    ]
    if not policy_strict:
        status = "invalid_policy"
    elif not paths:
        status = "missing_run_local_telemetry"
    elif not returned:
        status = "no_successful_provider_return"
    elif len(compliant) != len(returned):
        status = "telemetry_mismatch"
    else:
        status = "verified"
    return {
        "status": status,
        "policy_strict": policy_strict,
        "audit_paths": [str(path) for path in paths],
        "audit_row_count": len(rows),
        "provider_attempt_count": len(provider_rows),
        "provider_return_count": len(returned),
        "provider_error_count": len(errors),
        "acceptance_count": len(accepted),
        "reasoning_zero_count": sum(
            row.get("reasoning_tokens") == 0 for row in returned
        ),
        "reasoning_nonzero_count": sum(
            isinstance(row.get("reasoning_tokens"), int)
            and row.get("reasoning_tokens") != 0
            for row in returned
        ),
        "reasoning_missing_count": sum(
            row.get("reasoning_tokens") is None for row in returned
        ),
        "response_reasoning_present_count": sum(
            row.get("response_reasoning_present") is True for row in returned
        ),
        "models": sorted(
            {
                str(row.get("returned_model") or row.get("requested_model") or "")
                for row in returned
                if row.get("returned_model") or row.get("requested_model")
            }
        ),
        "providers": sorted(
            {
                str(row.get("provider") or "")
                for row in returned
                if row.get("provider")
            }
        ),
    }


def pick_representative_agents(
    agent_ids: list[str],
    *,
    final_states: dict[str, dict[str, Any]] | None = None,
    order_rows: list[dict[str, Any]] | None = None,
    fill_rows: list[dict[str, Any]] | None = None,
    community_posts: list[dict[str, Any]] | None = None,
    community_interactions: list[dict[str, Any]] | None = None,
    limit: int = 4,
) -> tuple[list[str], dict[str, str]]:
    """Pick a compact set of agents that still explains most of the run."""
    final_states = final_states or {}
    order_rows = order_rows or []
    fill_rows = fill_rows or []
    community_posts = community_posts or []
    community_interactions = community_interactions or []

    if not agent_ids or limit <= 0:
        return [], {}

    selected: list[str] = []
    reasons: dict[str, str] = {}

    def add(agent_id: str, reason: str) -> None:
        if agent_id and agent_id in agent_ids and agent_id not in selected and len(selected) < limit:
            selected.append(agent_id)
            reasons[agent_id] = reason

    ranked_returns = sorted(
        (
            (
                agent_id,
                num(
                    final_states.get(agent_id, {}).get(
                        "return_rate_marked_final",
                        final_states.get(agent_id, {}).get("total_return_rate", 0),
                    )
                ),
            )
            for agent_id in agent_ids
        ),
        key=lambda item: (-item[1], item[0]),
    )
    for agent_id, value in ranked_returns[:2]:
        add(agent_id, f"최종 평가 수익률 상위권({value * 100:.2f}%)")

    impact = Counter()
    for row in order_rows:
        agent_id = row_agent_id(row)
        if agent_id in SYSTEM_USERS:
            continue
        impact[agent_id] += abs(row_quantity(row)) * max(row_price(row), 1)
    for row in fill_rows:
        agent_id = row_agent_id(row)
        if agent_id in SYSTEM_USERS:
            continue
        impact[agent_id] += abs(row_quantity(row)) * max(row_price(row), 1) * 2
    if impact:
        agent_id, value = sorted(impact.items(), key=lambda item: (-item[1], item[0]))[0]
        add(agent_id, f"주문/체결 금액 영향도 최상위({value:,.0f})")

    community_activity = Counter()
    for row in community_posts:
        community_activity[str(row.get("agent_id") or "")] += 2
    for row in community_interactions:
        if row.get("post_id") and is_reaction_row(row):
            community_activity[str(row.get("agent_id") or "")] += 1
    if community_activity:
        agent_id, value = sorted(community_activity.items(), key=lambda item: (-item[1], item[0]))[0]
        add(agent_id, f"커뮤니티 참여도 최상위({int(value)}점)")

    combined = Counter()
    for agent_id, value in ranked_returns:
        combined[agent_id] += value * 100
    for agent_id, value in impact.items():
        combined[agent_id] += value / 10_000_000
    for agent_id, value in community_activity.items():
        combined[agent_id] += value
    for agent_id in agent_ids:
        combined[agent_id] += 0

    for agent_id, value in sorted(combined.items(), key=lambda item: (-item[1], item[0])):
        add(agent_id, f"복합 점수 보완 선정({value:.2f})")

    return selected, reasons
