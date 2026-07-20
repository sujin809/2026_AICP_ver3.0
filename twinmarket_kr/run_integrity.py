from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                rows.append(json.loads(stripped))
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Invalid JSONL at {path}:{line_number}") from exc
    return rows


def summarize_api_audit(path: Path | str, *, expected_model: str) -> dict[str, Any]:
    rows = _read_jsonl(Path(path))
    if not rows:
        raise RuntimeError(f"OpenRouter audit log is empty: {path}")
    requested_models = sorted({str(row.get("requested_model") or "") for row in rows})
    if requested_models != [expected_model]:
        raise RuntimeError(
            f"Unexpected requested models in API audit: {requested_models}, expected={expected_model}"
        )
    successful = [row for row in rows if str(row.get("status")) == "success"]
    if not successful:
        raise RuntimeError("OpenRouter audit contains no successful calls")
    returned_models = sorted(
        {str(row.get("returned_model")) for row in successful if row.get("returned_model")}
    )
    unexpected_returned_models = [
        model for model in returned_models if model != expected_model
    ]
    if unexpected_returned_models:
        raise RuntimeError(
            "OpenRouter returned a model other than the paper model: "
            f"{unexpected_returned_models}, expected={expected_model}"
        )
    return {
        "api_call_attempts": len(rows),
        "api_successes": len(successful),
        "api_errors": len(rows) - len(successful),
        "api_requested_model_set": requested_models,
        "api_returned_model_set": returned_models,
        "api_missing_returned_model": sum(
            not bool(row.get("returned_model")) for row in successful
        ),
        "api_provider_set": sorted(
            {str(row.get("provider")) for row in successful if row.get("provider")}
        ),
    }


def scheduled_fake_dates(daily_news_csv: Path | str, dates: Iterable[str]) -> set[str]:
    allowed = set(dates)
    result: set[str] = set()
    for row in _read_csv(Path(daily_news_csv)):
        day = str(row.get("date") or "")
        if day not in allowed:
            continue
        if _truthy(row.get("is_fake")) or str(row.get("synthetic_id") or "").strip():
            result.add(day)
    return result


def validate_news_inputs(
    *,
    processed_news_csv: Path | str,
    daily_news_csv: Path | str,
    baseline_processed_csv: Path | str,
    baseline_daily_csv: Path | str,
    sim_db_path: Path | str,
    dates: list[str],
    fake_news_mode: str,
    market_close_time: str,
) -> dict[str, Any]:
    """Audit feed identity, 10(+1) structure, and temporal eligibility before API calls."""
    processed = _read_csv(Path(processed_news_csv))
    daily = _read_csv(Path(daily_news_csv))
    if not processed or not daily:
        raise RuntimeError("News input CSV is empty or missing")

    processed_ids = [str(row.get("id") or "") for row in processed]
    daily_ids = [str(row.get("id") or "") for row in daily]
    if any(not value for value in processed_ids + daily_ids):
        raise RuntimeError("News input contains an empty public ID")
    if len(processed_ids) != len(set(processed_ids)):
        raise RuntimeError("Processed news contains duplicate public IDs")
    if len(daily_ids) != len(set(daily_ids)):
        raise RuntimeError("Daily news selection contains duplicate public IDs")
    missing_content = sorted(set(daily_ids) - set(processed_ids))
    if missing_content:
        raise RuntimeError(f"Daily feed IDs missing from processed news: {missing_content[:5]}")

    def is_fake(row: dict[str, Any]) -> bool:
        return _truthy(row.get("is_fake")) or bool(str(row.get("synthetic_id") or "").strip())

    processed_fake_ids = {str(row["id"]) for row in processed if is_fake(row)}
    daily_fake_ids = {str(row["id"]) for row in daily if is_fake(row)}
    if processed_fake_ids != daily_fake_ids:
        raise RuntimeError(
            "Fake-news IDs differ between processed and daily files: "
            f"processed={len(processed_fake_ids)} daily={len(daily_fake_ids)}"
        )
    if fake_news_mode == "off" and daily_fake_ids:
        raise RuntimeError("Fake-news rows are present in a fake-off feed")
    if fake_news_mode == "on":
        if len(daily_fake_ids) != 30:
            raise RuntimeError(f"Paper fake-on feed requires 30 stimuli, found {len(daily_fake_ids)}")
        baseline_processed = _read_csv(Path(baseline_processed_csv))
        baseline_daily = _read_csv(Path(baseline_daily_csv))
        public_processed_fields = ("id", "title", "date", "time", "category", "summary")
        public_daily_fields = ("id", "title", "date", "time", "category")

        def public_rows(rows: list[dict[str, str]], fields: tuple[str, ...]) -> list[tuple[str, ...]]:
            return sorted(
                tuple(str(row.get(field) or "") for field in fields)
                for row in rows
                if not is_fake(row)
            )

        if public_rows(processed, public_processed_fields) != public_rows(
            baseline_processed, public_processed_fields
        ):
            raise RuntimeError("Fake-on processed news does not preserve the baseline real-news pool")
        if public_rows(daily, public_daily_fields) != public_rows(baseline_daily, public_daily_fields):
            raise RuntimeError("Fake-on daily feed does not preserve the baseline real-news selection")

    with sqlite3.connect(sim_db_path) as connection:
        stock_dates = [
            str(row[0])
            for row in connection.execute(
                "SELECT date FROM StockData WHERE stock_id = '005930' ORDER BY date"
            ).fetchall()
        ]
    previous_by_date = {
        day: stock_dates[index - 1] for index, day in enumerate(stock_dates) if index > 0
    }
    rows_with_time: list[tuple[datetime, dict[str, str]]] = []
    for row in daily:
        try:
            timestamp = datetime.strptime(
                f"{str(row.get('date') or '')[:10]} {str(row.get('time') or '')[:5]}",
                "%Y-%m-%d %H:%M",
            )
        except ValueError:
            continue
        rows_with_time.append((timestamp, row))

    slot_report: list[dict[str, Any]] = []
    fake_dates: list[str] = []
    for day in dates:
        if day not in previous_by_date:
            raise RuntimeError(f"No prior trading date exists for news cutoff: {day}")
        windows = (
            (
                "am",
                datetime.strptime(
                    f"{previous_by_date[day]} {market_close_time}", "%Y-%m-%d %H:%M"
                ),
                datetime.strptime(f"{day} 08:59", "%Y-%m-%d %H:%M"),
            ),
            (
                "pm",
                datetime.strptime(f"{day} 08:59", "%Y-%m-%d %H:%M"),
                datetime.strptime(f"{day} {market_close_time}", "%Y-%m-%d %H:%M"),
            ),
        )
        for subturn, start, end in windows:
            slot_rows = [row for timestamp, row in rows_with_time if start < timestamp <= end]
            fake_count = sum(is_fake(row) for row in slot_rows)
            real_count = len(slot_rows) - fake_count
            if real_count not in {9, 10}:
                raise RuntimeError(
                    f"Real-news feed count must be 9 or 10 at {day}:{subturn}, found {real_count}"
                )
            if fake_count not in {0, 1}:
                raise RuntimeError(
                    f"At most one fake stimulus is allowed at {day}:{subturn}, found {fake_count}"
                )
            if fake_count and real_count != 10:
                raise RuntimeError(
                    f"Fake stimulus must append to 10 real items at {day}:{subturn}"
                )
            if fake_count:
                fake_dates.append(day)
            slot_report.append(
                {
                    "date": day,
                    "subturn": subturn,
                    "real_count": real_count,
                    "fake_count": fake_count,
                }
            )
    if fake_news_mode == "on":
        if len(fake_dates) != 30 or len(set(fake_dates)) != 30:
            raise RuntimeError(
                "Fake stimuli must occupy 30 distinct experiment dates/slots: "
                f"slots={len(fake_dates)} dates={len(set(fake_dates))}"
            )
    elif fake_dates:
        raise RuntimeError(f"Unexpected fake-news slots in fake-off mode: {fake_dates[:5]}")

    short_real_slots = [
        f"{row['date']}:{row['subturn']}"
        for row in slot_report
        if row["real_count"] == 9
    ]
    return {
        "status": "pass",
        "processed_rows": len(processed),
        "daily_rows": len(daily),
        "experiment_slot_count": len(slot_report),
        "fake_stimulus_count": len(daily_fake_ids),
        "fake_experiment_slots": len(fake_dates),
        "short_real_news_slots": short_real_slots,
        "slot_counts": slot_report,
    }


def assert_runtime_state(
    db_path: Path | str,
    *,
    agent_ids: list[str],
    expected_turn: int,
    phase: str,
) -> str:
    """Validate and fingerprint belief/portfolio state at a chunk boundary."""
    if not agent_ids:
        raise ValueError("agent_ids must not be empty")
    placeholders = ",".join("?" for _ in agent_ids)
    payload: list[dict[str, Any]] = []
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        for table in ("portfolio_state", "belief_history"):
            maximum = connection.execute(
                f"SELECT MAX(turn) FROM {table} WHERE agent_id IN ({placeholders})",
                agent_ids,
            ).fetchone()[0]
            if maximum is None or int(maximum) != expected_turn:
                raise RuntimeError(
                    f"{phase}: {table} max turn is {maximum}, expected {expected_turn}"
                )

        expected_state_rows = len(agent_ids) * (expected_turn + 1)
        for table in ("portfolio_state", "belief_history"):
            state_rows = int(
                connection.execute(
                    f"""
                    SELECT COUNT(*) FROM {table}
                    WHERE agent_id IN ({placeholders}) AND turn BETWEEN 0 AND ?
                    """,
                    [*agent_ids, expected_turn],
                ).fetchone()[0]
            )
            if state_rows != expected_state_rows:
                raise RuntimeError(
                    f"{phase}: {table} rows={state_rows}, expected={expected_state_rows}"
                )
            foreign_runtime_agents = connection.execute(
                f"""
                SELECT DISTINCT agent_id FROM {table}
                WHERE turn > 0 AND agent_id NOT IN ({placeholders})
                ORDER BY agent_id
                LIMIT 5
                """,
                agent_ids,
            ).fetchall()
            if foreign_runtime_agents:
                raise RuntimeError(
                    f"{phase}: {table} contains out-of-cohort runtime agents: "
                    f"{[str(row[0]) for row in foreign_runtime_agents]}"
                )
        trade_rows = int(
            connection.execute(
                f"""
                SELECT COUNT(*) FROM trade_log
                WHERE agent_id IN ({placeholders}) AND turn BETWEEN 1 AND ?
                """,
                [*agent_ids, expected_turn],
            ).fetchone()[0]
        )
        expected_trade_rows = len(agent_ids) * expected_turn
        if trade_rows != expected_trade_rows:
            raise RuntimeError(
                f"{phase}: trade_log rows={trade_rows}, expected={expected_trade_rows}"
            )
        invalid_trade_rows = int(
            connection.execute(
                f"""
                SELECT COUNT(*) FROM trade_log
                WHERE agent_id IN ({placeholders}) AND turn BETWEEN 1 AND ?
                  AND (status <> 'filled' OR filled_quantity <> quantity OR executed_price IS NULL)
                """,
                [*agent_ids, expected_turn],
            ).fetchone()[0]
        )
        if invalid_trade_rows:
            raise RuntimeError(
                f"{phase}: non-full or unresolved trade_log rows={invalid_trade_rows}"
            )
        trading_detail_rows = int(
            connection.execute(
                f"SELECT COUNT(*) FROM TradingDetails WHERE user_id IN ({placeholders})",
                agent_ids,
            ).fetchone()[0]
        )
        if trading_detail_rows != expected_trade_rows:
            raise RuntimeError(
                f"{phase}: TradingDetails rows={trading_detail_rows}, expected={expected_trade_rows}"
            )
        system_message_rows = int(
            connection.execute("SELECT COUNT(*) FROM agent_system_messages").fetchone()[0]
        )
        if system_message_rows:
            raise RuntimeError(
                f"{phase}: fabricated/system recovery messages detected: {system_message_rows}"
            )

        portfolios = connection.execute(
            f"""
            SELECT agent_id, turn, date, cash, positions, total_value,
                   realized_pnl, total_return_rate
            FROM portfolio_state
            WHERE turn = ? AND agent_id IN ({placeholders})
            ORDER BY agent_id
            """,
            [expected_turn, *agent_ids],
        ).fetchall()
        beliefs = connection.execute(
            f"""
            SELECT agent_id, turn, date, belief_summary, COALESCE(view_change, '') AS view_change
            FROM belief_history
            WHERE turn = ? AND agent_id IN ({placeholders})
            ORDER BY agent_id
            """,
            [expected_turn, *agent_ids],
        ).fetchall()

    portfolio_agents = {str(row["agent_id"]) for row in portfolios}
    belief_agents = {str(row["agent_id"]) for row in beliefs}
    expected_agents = set(agent_ids)
    if portfolio_agents != expected_agents:
        missing = sorted(expected_agents - portfolio_agents)
        extra = sorted(portfolio_agents - expected_agents)
        raise RuntimeError(f"{phase}: portfolio boundary mismatch missing={missing} extra={extra}")
    if belief_agents != expected_agents:
        missing = sorted(expected_agents - belief_agents)
        extra = sorted(belief_agents - expected_agents)
        raise RuntimeError(f"{phase}: belief boundary mismatch missing={missing} extra={extra}")

    portfolio_by_agent = {str(row["agent_id"]): dict(row) for row in portfolios}
    belief_by_agent = {str(row["agent_id"]): dict(row) for row in beliefs}
    for agent_id in sorted(expected_agents):
        portfolio = portfolio_by_agent[agent_id]
        if float(portfolio["cash"]) < -1e-6:
            raise RuntimeError(f"{phase}: negative cash for {agent_id}: {portfolio['cash']}")
        try:
            positions = json.loads(portfolio["positions"] or "[]")
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"{phase}: invalid positions JSON for {agent_id}") from exc
        if any(int(position.get("quantity") or 0) < 0 for position in positions):
            raise RuntimeError(f"{phase}: negative position for {agent_id}")
        payload.append(
            {
                "agent_id": agent_id,
                "portfolio": portfolio_by_agent[agent_id],
                "belief": belief_by_agent[agent_id],
            }
        )
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def validate_log_bundle(
    run_dir: Path | str,
    *,
    dates: list[str],
    agent_ids: list[str],
    active_community_agent_ids: list[str],
    turn_offset: int,
    fake_news_mode: str,
    daily_news_csv: Path | str,
    community_mode: str,
) -> dict[str, Any]:
    """Fail fast when a chunk or merged run is incomplete or temporally inconsistent."""
    root = Path(run_dir)
    errors: list[str] = []
    agent_turns = _read_csv(root / "agent_turns.csv")
    expected_agent_turns = len(dates) * 2 * len(agent_ids)
    if len(agent_turns) != expected_agent_turns:
        errors.append(f"agent_turns rows={len(agent_turns)} expected={expected_agent_turns}")

    expected_turn_by_date_subturn: dict[tuple[str, str], int] = {}
    for index, day in enumerate(dates):
        am_turn = turn_offset + index * 2 + 1
        expected_turn_by_date_subturn[(day, "am")] = am_turn
        expected_turn_by_date_subturn[(day, "pm")] = am_turn + 1

    seen_agent_turns: Counter[tuple[str, str, str]] = Counter()
    observed_agents = set()
    feed_signatures: dict[tuple[str, str], set[tuple[str, ...]]] = defaultdict(set)
    visible_counts: dict[tuple[str, str], set[int]] = defaultdict(set)
    fake_visible_counts: dict[tuple[str, str], int] = defaultdict(int)
    for row in agent_turns:
        day = str(row.get("date") or "")
        subturn = str(row.get("subturn") or "")
        agent_id = str(row.get("agent_id") or "")
        observed_agents.add(agent_id)
        seen_agent_turns[(day, subturn, agent_id)] += 1
        expected_turn = expected_turn_by_date_subturn.get((day, subturn))
        try:
            actual_turn = int(row.get("turn") or -1)
        except ValueError:
            actual_turn = -1
        if expected_turn is None or actual_turn != expected_turn:
            errors.append(
                f"unexpected turn mapping date={day} subturn={subturn} agent={agent_id} "
                f"actual={actual_turn} expected={expected_turn}"
            )
            if len(errors) >= 20:
                break
        action = str(row.get("action") or "").lower()
        try:
            quantity = int(float(row.get("quantity") or 0))
            decision_attempts = int(float(row.get("decision_attempts") or 0))
            belief_attempts = int(float(row.get("belief_generation_attempts") or 0))
            depth = int(float(row.get("news_depth") or 0))
            read_count = int(float(row.get("read_news_count") or 0))
            search_count = int(float(row.get("search_read_count") or 0))
        except ValueError:
            errors.append(f"invalid numeric decision/log fields agent={agent_id} date={day}")
            continue
        visible_ids = tuple(
            value.strip()
            for value in str(row.get("visible_news_ids") or "").split(",")
            if value.strip()
        )
        visible_count = len(visible_ids)
        feed_key = (day, subturn)
        feed_signatures[feed_key].add(visible_ids)
        visible_counts[feed_key].add(visible_count)
        if _truthy(row.get("fake_visible")):
            fake_visible_counts[feed_key] += 1
        if len(visible_ids) != len(set(visible_ids)):
            errors.append(f"duplicate visible news IDs date={day} subturn={subturn} agent={agent_id}")
        if action not in {"buy", "sell"} or quantity < 1:
            errors.append(
                f"invalid buy/sell-only decision agent={agent_id} date={day} "
                f"action={action} quantity={quantity}"
            )
        if decision_attempts < 1:
            errors.append(f"missing decision attempt audit agent={agent_id} date={day}")
        if belief_attempts < 1:
            errors.append(f"missing belief attempt audit agent={agent_id} date={day}")
        if _truthy(row.get("deterministic_fallback_used")):
            errors.append(f"deterministic fallback detected agent={agent_id} date={day}")
        if depth == 0 and (read_count != 0 or search_count != 0):
            errors.append(f"depth0 read/search violation agent={agent_id} date={day}")
        if depth == 1 and (read_count != visible_count or search_count != 0):
            errors.append(
                f"depth1 full-read/no-search violation agent={agent_id} date={day} "
                f"visible={visible_count} read={read_count} search={search_count}"
            )
        if depth >= 2 and (read_count != visible_count or search_count > 10):
            errors.append(
                f"depth2 full-read/search-limit violation agent={agent_id} date={day} "
                f"visible={visible_count} read={read_count} search={search_count}"
            )
    duplicates = [key for key, count in seen_agent_turns.items() if count != 1]
    if duplicates:
        errors.append(f"agent_turn duplicate/missing keys sample={duplicates[:5]}")
    if observed_agents != set(agent_ids):
        errors.append(
            f"agent cohort mismatch observed={sorted(observed_agents)} expected={sorted(agent_ids)}"
        )
    for feed_key, signatures in sorted(feed_signatures.items()):
        if len(signatures) != 1:
            errors.append(f"agents received different feed candidates at {feed_key}")
            continue
        count = next(iter(visible_counts[feed_key]))
        fake_rows_in_slot = fake_visible_counts.get(feed_key, 0)
        if fake_rows_in_slot not in {0, len(agent_ids)}:
            errors.append(
                f"partial fake feed exposure at {feed_key}: {fake_rows_in_slot}/{len(agent_ids)}"
            )
        if fake_rows_in_slot:
            if count != 11:
                errors.append(f"fake slot must contain 10 real + 1 fake at {feed_key}: count={count}")
        elif count not in {9, 10}:
            errors.append(f"real-news slot must contain 9 or 10 items at {feed_key}: count={count}")

    portfolio_updates = _read_jsonl(root / "portfolio_updates.jsonl")
    portfolio_keys = Counter(
        (str(row.get("date") or ""), int(row.get("turn") or -1), str(row.get("agent_id") or ""))
        for row in portfolio_updates
    )
    if len(portfolio_updates) != expected_agent_turns:
        errors.append(
            f"portfolio_updates rows={len(portfolio_updates)} expected={expected_agent_turns}"
        )
    duplicate_portfolios = [key for key, count in portfolio_keys.items() if count != 1]
    if duplicate_portfolios:
        errors.append(f"portfolio update duplicate keys sample={duplicate_portfolios[:5]}")
    for row in portfolio_updates:
        state = row.get("state") or {}
        if int(state.get("turn") or -1) != int(row.get("turn") or -1):
            errors.append(
                f"portfolio state/event turn mismatch agent={row.get('agent_id')} date={row.get('date')}"
            )
            break

    exchange_rows = _read_csv(root / "daily_exchange_summary.csv")
    expected_exchange_rows = len(dates) * 2
    exchange_keys = Counter(
        (str(row.get("date") or ""), str(row.get("turn") or ""), str(row.get("stock_code") or ""))
        for row in exchange_rows
    )
    if len(exchange_rows) != expected_exchange_rows:
        errors.append(
            f"daily_exchange_summary rows={len(exchange_rows)} expected={expected_exchange_rows}"
        )
    duplicate_exchange = [key for key, count in exchange_keys.items() if count != 1]
    if duplicate_exchange:
        errors.append(f"daily exchange duplicate keys sample={duplicate_exchange[:5]}")

    order_rows = _read_csv(root / "submitted_orders.csv")
    expected_orders = sum(_truthy(row.get("submitted_order")) for row in agent_turns)
    if len(order_rows) != expected_orders:
        errors.append(f"submitted_orders rows={len(order_rows)} expected={expected_orders}")
    order_quantity: dict[tuple[str, str, str, str], int] = defaultdict(int)
    for row in order_rows:
        key = (
            str(row.get("date") or ""),
            str(row.get("turn") or ""),
            str(row.get("agent_id") or ""),
            str(row.get("action") or ""),
        )
        order_quantity[key] += int(float(row.get("quantity") or 0))
    fill_rows = _read_csv(root / "exchange_fills.csv")
    fill_quantity: dict[tuple[str, str, str, str], int] = defaultdict(int)
    for row in fill_rows:
        key = (
            str(row.get("date") or ""),
            str(row.get("turn") or ""),
            str(row.get("agent_id") or ""),
            str(row.get("action") or ""),
        )
        filled = int(float(row.get("quantity") or 0))
        fill_quantity[key] += filled
        if key not in order_quantity or filled > order_quantity[key]:
            errors.append(f"fill has no matching order or exceeds it key={key} filled={filled}")
            break
    if len(fill_rows) != len(order_rows):
        errors.append(f"exchange_fills rows={len(fill_rows)} expected_orders={len(order_rows)}")
    if fill_quantity != order_quantity:
        mismatch_keys = [
            key
            for key in sorted(set(fill_quantity) | set(order_quantity))
            if order_quantity.get(key, 0) != fill_quantity.get(key, 0)
        ][:5]
        errors.append(
            "full-fill invariant failed sample="
            + str(
                [
                    (key, order_quantity.get(key, 0), fill_quantity.get(key, 0))
                    for key in mismatch_keys
                    if order_quantity.get(key, 0) != fill_quantity.get(key, 0)
                ]
            )
        )

    expected_fake = scheduled_fake_dates(daily_news_csv, dates)
    visible_by_date: dict[str, list[str]] = defaultdict(list)
    for row in agent_turns:
        if _truthy(row.get("fake_visible")):
            visible_by_date[str(row.get("date") or "")].append(str(row.get("agent_id") or ""))
    if fake_news_mode == "off":
        if visible_by_date:
            errors.append(f"fake feed visibility found while fake mode is off: {sorted(visible_by_date)}")
    else:
        actual_fake = set(visible_by_date)
        if actual_fake != expected_fake:
            errors.append(
                f"fake feed dates actual={sorted(actual_fake)} expected={sorted(expected_fake)}"
            )
        for day in sorted(expected_fake):
            visible = visible_by_date.get(day, [])
            if len(visible) != len(agent_ids) or set(visible) != set(agent_ids):
                errors.append(
                    f"fake feed cohort mismatch date={day} rows={len(visible)} "
                    f"unique={len(set(visible))} expected={len(agent_ids)}"
                )

    community_rows = _read_csv(root / "community_logs.csv")
    if community_mode == "off":
        if community_rows:
            errors.append(f"community logs found while community mode is off: rows={len(community_rows)}")
    else:
        expected_community_rows = len(dates) * len(active_community_agent_ids)
        if len(community_rows) != expected_community_rows:
            errors.append(
                f"community_logs rows={len(community_rows)} expected={expected_community_rows}"
            )
        community_keys = Counter(
            (str(row.get("date") or ""), str(row.get("agent_id") or ""))
            for row in community_rows
        )
        duplicate_community = [key for key, count in community_keys.items() if count != 1]
        if duplicate_community:
            errors.append(f"community log duplicate keys sample={duplicate_community[:5]}")

    agent_errors = _read_jsonl(root / "errors.jsonl")
    if agent_errors:
        errors.append(f"agent errors logged: {len(agent_errors)}")

    if errors:
        raise RuntimeError("Run integrity validation failed:\n- " + "\n- ".join(errors[:25]))

    return {
        "status": "pass",
        "run_dir": str(root),
        "date_count": len(dates),
        "agent_count": len(agent_ids),
        "turn_offset": turn_offset,
        "expected_agent_turns": expected_agent_turns,
        "scheduled_fake_dates": sorted(expected_fake),
        "community_agent_count": len(active_community_agent_ids),
        "visible_news_counts": {
            f"{day}:{subturn}": sorted(counts)
            for (day, subturn), counts in sorted(visible_counts.items())
        },
    }
