"""Finalize and evaluate one canonical real-news community OFF/ON pair.

This module reads only the common numbered-run artifacts produced by
``scripts/05_run_simulation.py``.  It deliberately has no dependency on the
retired ``twinmarket_kr.rn_ab`` runtime or its paper tables.

The actual Individuals target remains evaluator-only: it is never copied into
an agent-facing runtime database or prompt.  Its source file and the exact
calendar-bound target subset are instead pinned in the final JSON hash index.
"""
from __future__ import annotations

import csv
import json
import math
import sqlite3
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from twinmarket_kr.experiment_runtime import (
    atomic_write_json,
    canonical_sha256,
    file_sha256,
)


PAIR_ARTIFACT_TYPE = "integrated_realnews_community_pair_evaluation"
PAIR_ARTIFACT_VERSION = "1"
OFF_CONDITION = "RN_COMM_OFF"
ON_CONDITION = "RN_COMM_ON"
_PAIR_PARAMETER_ALLOWLIST = frozenset({"condition_id", "community_mode"})


class PairEvaluationError(RuntimeError):
    """The two canonical runs cannot support the paired scientific analysis."""


@dataclass(frozen=True)
class Fill:
    fill_id: str
    agent_id: str
    event_id: str
    date: str
    session: str
    action: str
    quantity: int
    price: Decimal

    @property
    def signed_value(self) -> Decimal:
        sign = Decimal(1) if self.action == "buy" else Decimal(-1)
        return sign * Decimal(self.quantity) * self.price


@dataclass(frozen=True)
class RunSource:
    condition_id: str
    run_dir: Path
    metadata: dict[str, Any]
    signature_payload: dict[str, Any]
    checkpoint: dict[str, Any]
    database: Path


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise PairEvaluationError(f"{label} must be a real JSON file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PairEvaluationError(f"{label} is not valid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise PairEvaluationError(f"{label} must be a JSON object: {path}")
    return value


def _sha256_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise PairEvaluationError(f"{label} is not a SHA-256 value")
    if any(character not in "0123456789abcdef" for character in value):
        raise PairEvaluationError(f"{label} is not a lowercase SHA-256 value")
    return value


def _decimal(value: Any, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PairEvaluationError(f"{label} is not numeric: {value!r}") from exc
    if not parsed.is_finite():
        raise PairEvaluationError(f"{label} is not finite")
    return parsed


def _decimal_text(value: Decimal) -> str:
    if value == value.to_integral():
        return str(value.quantize(Decimal(1)))
    return format(value.normalize(), "f")


def _load_run(run_dir: Path | str, expected_condition: str) -> RunSource:
    root = Path(run_dir).resolve()
    terminal = _read_json_object(root / "run_complete.json", "run completion")
    if (
        terminal.get("status") != "complete"
        or terminal.get("full_frozen_schedule") is not True
        or terminal.get("schedule_complete") is not True
    ):
        raise PairEvaluationError(
            f"{expected_condition} is not a complete full-schedule run"
        )

    metadata_path = root / "run_metadata.json"
    signature_path = root / "run_signature.json"
    checkpoint_path = root / ".runtime" / "checkpoint.json"
    metadata = _read_json_object(metadata_path, "run metadata")
    signature = _read_json_object(signature_path, "run signature")
    checkpoint = _read_json_object(checkpoint_path, "event checkpoint")
    payload = signature.get("signature_payload")
    if not isinstance(payload, dict):
        raise PairEvaluationError("run signature has no signature_payload")
    signature_sha = canonical_sha256(payload)
    if signature.get("signature_sha256") != signature_sha:
        raise PairEvaluationError("run signature payload hash is invalid")
    if (
        metadata.get("run_signature_sha256") != signature_sha
        or checkpoint.get("signature_sha256") != signature_sha
    ):
        raise PairEvaluationError(
            f"{expected_condition} metadata/checkpoint signature binding differs"
        )
    parameters = payload.get("parameters")
    if not isinstance(parameters, dict):
        raise PairEvaluationError("run signature parameters are missing")
    if parameters.get("condition_id") != expected_condition:
        raise PairEvaluationError(
            f"run condition is {parameters.get('condition_id')!r}, "
            f"expected {expected_condition!r}"
        )
    expected_mode = "off" if expected_condition == OFF_CONDITION else "on"
    if parameters.get("community_mode") != expected_mode:
        raise PairEvaluationError(
            f"{expected_condition} has community_mode="
            f"{parameters.get('community_mode')!r}"
        )
    if parameters.get("news_treatment") != "real_only":
        raise PairEvaluationError(
            f"{expected_condition} is not the real-only baseline"
        )
    seed = parameters.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise PairEvaluationError(f"{expected_condition} seed is not an integer")
    prompts = payload.get("prompts")
    if not isinstance(prompts, dict):
        raise PairEvaluationError("run signature has no prompt manifest")
    _sha256_text(prompts.get("tree_sha256"), "prompt tree SHA-256")
    call_policy = payload.get("call_policy")
    if not isinstance(call_policy, dict):
        raise PairEvaluationError("run signature has no model call policy")
    if not isinstance(call_policy.get("model"), str) or not call_policy["model"]:
        raise PairEvaluationError("model call policy has no pinned model")
    provider = call_policy.get("provider")
    reasoning = call_policy.get("reasoning")
    if (
        not isinstance(provider, dict)
        or provider.get("allow_fallbacks") is not False
        or provider.get("require_parameters") is not True
        or not isinstance(provider.get("only"), list)
        or len(provider["only"]) != 1
        or not isinstance(reasoning, dict)
        or reasoning.get("effort") != "none"
        or reasoning.get("exclude") is not True
    ):
        raise PairEvaluationError(
            "model/provider/reasoning-off policy is not publication-safe"
        )
    for field, value in parameters.items():
        if metadata.get(field) != value:
            raise PairEvaluationError(
                f"{expected_condition} metadata.{field} differs from its "
                "immutable signature"
            )

    event_ids = parameters.get("event_ids")
    agent_ids = parameters.get("agent_ids")
    if (
        not isinstance(event_ids, list)
        or not event_ids
        or len(event_ids) % 2
    ):
        raise PairEvaluationError(
            f"{expected_condition} must contain non-empty AM/PM event pairs"
        )
    if len(set(map(str, event_ids))) != len(event_ids):
        raise PairEvaluationError(f"{expected_condition} event IDs are not unique")
    if not isinstance(agent_ids, list) or not agent_ids:
        raise PairEvaluationError(
            f"{expected_condition} must contain a non-empty sealed cohort"
        )
    if len(set(map(str, agent_ids))) != len(agent_ids):
        raise PairEvaluationError(f"{expected_condition} agent IDs are not unique")
    if int(parameters.get("agent_count", -1)) != len(agent_ids):
        raise PairEvaluationError(
            f"{expected_condition} agent_count differs from agent_ids"
        )
    if (
        checkpoint.get("completed_events") != event_ids
        or metadata.get("completed_events") != event_ids
    ):
        raise PairEvaluationError(
            f"{expected_condition} completed-event prefix is not the full schedule"
        )

    database = root / ".runtime" / "committed.db"
    if not database.is_file() or database.is_symlink():
        raise PairEvaluationError(
            f"{expected_condition} committed database is missing: {database}"
        )
    database_sha = file_sha256(database)
    for label, recorded in (
        ("checkpoint", checkpoint.get("committed_database_sha256")),
        ("metadata", metadata.get("committed_database_sha256")),
    ):
        if recorded != database_sha:
            raise PairEvaluationError(
                f"{expected_condition} {label} committed DB hash differs"
            )
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
    if quick_check.lower() != "ok":
        raise PairEvaluationError(
            f"{expected_condition} committed DB quick_check failed"
        )
    return RunSource(
        condition_id=expected_condition,
        run_dir=root,
        metadata=metadata,
        signature_payload=payload,
        checkpoint=checkpoint,
        database=database,
    )


def _pair_invariant_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    normalized = json.loads(
        json.dumps(payload, ensure_ascii=False, allow_nan=False)
    )
    parameters = normalized.get("parameters")
    if not isinstance(parameters, dict):
        raise PairEvaluationError("signature parameters are missing")
    for field in _PAIR_PARAMETER_ALLOWLIST:
        parameters.pop(field, None)
    return normalized


def _validate_pair_invariants(off: RunSource, on: RunSource) -> str:
    off_invariant = _pair_invariant_payload(off.signature_payload)
    on_invariant = _pair_invariant_payload(on.signature_payload)
    if off_invariant != on_invariant:
        changed_sections = sorted(
            key
            for key in set(off_invariant) | set(on_invariant)
            if off_invariant.get(key) != on_invariant.get(key)
        )
        raise PairEvaluationError(
            "OFF/ON immutable inputs differ outside community_mode; "
            f"changed_sections={changed_sections}"
        )
    return canonical_sha256(off_invariant)


def _sealed_input(source: RunSource, label: str) -> tuple[Path, str]:
    inputs = source.signature_payload.get("sealed_inputs")
    if not isinstance(inputs, dict) or not isinstance(inputs.get(label), dict):
        raise PairEvaluationError(f"signature has no sealed input {label!r}")
    entry = inputs[label]
    path = Path(str(entry.get("path") or "")).resolve()
    expected_sha = _sha256_text(entry.get("sha256"), f"{label} SHA-256")
    if not path.is_file() or path.is_symlink():
        raise PairEvaluationError(f"sealed input is missing: {path}")
    if file_sha256(path) != expected_sha:
        raise PairEvaluationError(f"sealed input hash differs: {label}")
    return path, expected_sha


def _event_contract(
    source: RunSource,
) -> tuple[list[dict[str, Any]], dict[str, Decimal], str]:
    path, price_sha = _sealed_input(source, "price_registry")
    registry = _read_json_object(path, "sealed price registry")
    rows = registry.get("events")
    if not isinstance(rows, list):
        raise PairEvaluationError("price registry events must be an array")
    event_ids = [
        str(value)
        for value in source.signature_payload["parameters"]["event_ids"]
    ]
    if len(rows) != len(event_ids):
        raise PairEvaluationError("price registry event count differs from run")
    normalized: list[dict[str, Any]] = []
    prices: dict[str, Decimal] = {}
    for turn, (expected_id, row) in enumerate(zip(event_ids, rows), start=1):
        if not isinstance(row, dict):
            raise PairEvaluationError("price registry event must be an object")
        event_id = str(row.get("decision_event_id") or "")
        session = str(row.get("subturn") or "").upper()
        expected_session = "AM" if expected_id.endswith("/AM") else "PM"
        expected_field = "actual_open" if expected_session == "AM" else "actual_close"
        if (
            event_id != expected_id
            or session != expected_session
            or row.get("execution_price_field") != expected_field
        ):
            raise PairEvaluationError(
                f"price registry AM/open or PM/close binding differs at {expected_id}"
            )
        price = _decimal(row.get("execution_price"), f"{event_id} price")
        if price <= 0:
            raise PairEvaluationError(f"{event_id} price must be positive")
        date = expected_id.split("/", 1)[0]
        normalized.append(
            {
                "event_id": expected_id,
                "turn": turn,
                "date": date,
                "session": expected_session,
                "execution_price_field": expected_field,
                "execution_price": _decimal_text(price),
            }
        )
        prices[expected_id] = price
    return normalized, prices, price_sha


def _validate_calendar_contract(
    source: RunSource,
    events: Sequence[Mapping[str, Any]],
) -> str:
    path, calendar_sha = _sealed_input(source, "calendar_registry")
    registry = _read_json_object(path, "sealed calendar registry")
    rows = registry.get("dates")
    if not isinstance(rows, list):
        raise PairEvaluationError("calendar registry dates must be an array")
    flattened: list[tuple[str, str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise PairEvaluationError("calendar date row must be an object")
        date = str(row.get("date") or "")
        decision_events = row.get("decision_events")
        if not isinstance(decision_events, list):
            raise PairEvaluationError("calendar decision_events must be an array")
        for event in decision_events:
            if not isinstance(event, dict):
                raise PairEvaluationError("calendar event must be an object")
            flattened.append(
                (
                    str(event.get("decision_event_id") or ""),
                    date,
                    str(event.get("execution_price_field") or ""),
                )
            )
    expected = [
        (
            str(event["event_id"]),
            str(event["date"]),
            str(event["execution_price_field"]),
        )
        for event in events
    ]
    if flattened != expected:
        raise PairEvaluationError(
            "sealed calendar order/AM-open/PM-close fields differ from price "
            "registry and run signature"
        )
    return calendar_sha


def _cohort_contract(
    source: RunSource,
) -> tuple[dict[str, Decimal], str]:
    path, cohort_sha = _sealed_input(source, "sealed_cohort")
    registry = _read_json_object(path, "sealed cohort registry")
    rows = registry.get("agents")
    if not isinstance(rows, list):
        raise PairEvaluationError("cohort registry agents must be an array")
    expected_ids = [
        str(value)
        for value in source.signature_payload["parameters"]["agent_ids"]
    ]
    observed_ids: list[str] = []
    initial_cash: dict[str, Decimal] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise PairEvaluationError("cohort agent must be an object")
        agent_id = str(row.get("agent_id") or "")
        if not agent_id or agent_id in initial_cash:
            raise PairEvaluationError("cohort agent IDs must be unique")
        cash = _decimal(row.get("initial_cash"), f"{agent_id} initial_cash")
        if cash <= 0:
            raise PairEvaluationError(f"{agent_id} initial_cash must be positive")
        observed_ids.append(agent_id)
        initial_cash[agent_id] = cash
    if observed_ids != expected_ids:
        raise PairEvaluationError(
            "sealed cohort order/IDs differ from the run signature"
        )
    return initial_cash, cohort_sha


def _validate_initial_assets(
    source: RunSource,
    expected: Mapping[str, Decimal],
) -> str:
    with sqlite3.connect(f"file:{source.database}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT agent_id, cash, positions, total_value
            FROM portfolio_state
            WHERE turn = 0
            ORDER BY agent_id
            """
        ).fetchall()
    observed: dict[str, Decimal] = {}
    canonical_rows: list[dict[str, str]] = []
    for row in rows:
        agent_id = str(row["agent_id"])
        if agent_id in observed:
            raise PairEvaluationError(
                f"{source.condition_id} has duplicate turn-zero portfolio"
            )
        total_value = _decimal(row["total_value"], f"{agent_id} turn-zero value")
        if agent_id not in expected or total_value != expected[agent_id]:
            raise PairEvaluationError(
                f"{source.condition_id} turn-zero assets differ from sealed cohort "
                f"for {agent_id}"
            )
        try:
            positions = json.loads(str(row["positions"]))
        except json.JSONDecodeError as exc:
            raise PairEvaluationError(
                f"{source.condition_id} has invalid turn-zero positions"
            ) from exc
        if not isinstance(positions, list):
            raise PairEvaluationError("turn-zero positions must be an array")
        observed[agent_id] = total_value
        canonical_rows.append(
            {
                "agent_id": agent_id,
                "initial_cash": _decimal_text(expected[agent_id]),
                "turn_zero_total_value": _decimal_text(total_value),
            }
        )
    if set(observed) != set(expected) or len(rows) != len(expected):
        raise PairEvaluationError(
            f"{source.condition_id} turn-zero portfolio does not cover the cohort"
        )
    return canonical_sha256(canonical_rows)


def _validated_fill_rows(
    database: Path,
    *,
    condition_id: str,
    agent_ids: Sequence[str],
    events: Sequence[Mapping[str, Any]],
    prices: Mapping[str, Decimal],
    stock_code: str,
) -> tuple[tuple[Fill, ...], str]:
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT
                fill_id, agent_id, turn, date, subturn, stock_code, action,
                requested_quantity, filled_quantity, executed_price, fee,
                source_ltb_id, source_stb_id, decision_id, scientific_sha256
            FROM simulation_fills
            ORDER BY turn, agent_id
            """
        ).fetchall()
    event_by_turn = {int(event["turn"]): event for event in events}
    expected_keys = {
        (str(agent_id), str(event["event_id"]))
        for agent_id in agent_ids
        for event in events
    }
    observed_keys: set[tuple[str, str]] = set()
    observed_ids: set[str] = set()
    fills: list[Fill] = []
    canonical_rows: list[dict[str, Any]] = []
    for row in rows:
        fill_id = str(row["fill_id"])
        if fill_id in observed_ids:
            raise PairEvaluationError(f"{condition_id} has duplicate fill_id")
        observed_ids.add(fill_id)
        turn = int(row["turn"])
        event = event_by_turn.get(turn)
        if event is None:
            raise PairEvaluationError(
                f"{condition_id} fill turn is outside the schedule: {turn}"
            )
        agent_id = str(row["agent_id"])
        event_id = str(event["event_id"])
        key = (agent_id, event_id)
        if key in observed_keys:
            raise PairEvaluationError(
                f"{condition_id} has duplicate agent/event fill: {key}"
            )
        observed_keys.add(key)
        session = str(row["subturn"]).upper()
        action = str(row["action"]).lower()
        requested = int(row["requested_quantity"])
        filled = int(row["filled_quantity"])
        price = _decimal(row["executed_price"], f"{fill_id} executed_price")
        fee = _decimal(row["fee"], f"{fill_id} fee")
        if (
            agent_id not in set(agent_ids)
            or str(row["date"]) != event["date"]
            or session != event["session"]
        ):
            raise PairEvaluationError(
                f"{condition_id} fill key/date/session differs from schedule"
            )
        if str(row["stock_code"]) != stock_code:
            raise PairEvaluationError(f"{condition_id} fill stock code differs")
        if action not in {"buy", "sell"}:
            raise PairEvaluationError(f"{condition_id} fill action is not buy/sell")
        if requested <= 0 or requested != filled:
            raise PairEvaluationError(f"{condition_id} fill is not a positive full fill")
        if price != prices[event_id]:
            raise PairEvaluationError(
                f"{condition_id} {event_id} fill price is not the sealed "
                f"{session} execution price"
            )
        if fee != 0:
            raise PairEvaluationError(f"{condition_id} fill fee is not zero")
        scientific_sha = _sha256_text(
            str(row["scientific_sha256"]),
            f"{fill_id} scientific_sha256",
        )
        lineage_ids = {
            "source_ltb_id": str(row["source_ltb_id"] or ""),
            "source_stb_id": str(row["source_stb_id"] or ""),
            "decision_id": str(row["decision_id"] or ""),
        }
        if any(not value for value in lineage_ids.values()):
            raise PairEvaluationError(f"{condition_id} fill lineage is incomplete")
        fill = Fill(
            fill_id=fill_id,
            agent_id=agent_id,
            event_id=event_id,
            date=str(event["date"]),
            session=session,
            action=action,
            quantity=filled,
            price=price,
        )
        fills.append(fill)
        canonical_rows.append(
            {
                "fill_id": fill_id,
                "agent_id": agent_id,
                "event_id": event_id,
                "action": action,
                "requested_quantity": requested,
                "filled_quantity": filled,
                "executed_price": _decimal_text(price),
                "fee": _decimal_text(fee),
                **lineage_ids,
                "scientific_sha256": scientific_sha,
            }
        )
    missing = expected_keys - observed_keys
    extra = observed_keys - expected_keys
    if missing or extra or len(rows) != len(expected_keys):
        raise PairEvaluationError(
            f"{condition_id} fill key set is not exact; "
            f"missing={sorted(missing)[:10]} extra={sorted(extra)[:10]}"
        )
    return tuple(fills), canonical_sha256(canonical_rows)


def _normalize_target_date(value: str) -> str:
    normalized = value.strip().replace("/", "-")
    parts = normalized.split("-")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        raise PairEvaluationError(f"invalid target date: {value!r}")
    return f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"


def _load_targets(
    path: Path | str,
    input_dates: Sequence[str],
) -> tuple[dict[str, Decimal], dict[str, str]]:
    source = Path(path).resolve()
    if not source.is_file() or source.is_symlink():
        raise PairEvaluationError(
            f"Individuals evaluator target must be a real CSV file: {source}"
        )
    try:
        with source.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or "Date" not in reader.fieldnames:
                raise PairEvaluationError("target CSV must contain Date")
            if "Individuals" not in reader.fieldnames:
                raise PairEvaluationError("target CSV must contain Individuals")
            rows = list(reader)
    except OSError as exc:
        raise PairEvaluationError(f"cannot read target CSV: {source}") from exc
    all_targets: dict[str, Decimal] = {}
    for row in rows:
        date = _normalize_target_date(str(row.get("Date") or ""))
        if date in all_targets:
            raise PairEvaluationError(f"target CSV has duplicate date={date}")
        value = _decimal(row.get("Individuals"), f"{date} Individuals")
        all_targets[date] = value
    selected: dict[str, Decimal] = {}
    for date in input_dates:
        if date not in all_targets:
            raise PairEvaluationError(f"target CSV is missing date={date}")
        if all_targets[date] == 0:
            raise PairEvaluationError(
                f"Individuals direction is undefined because target is zero: {date}"
            )
        selected[date] = all_targets[date]
    target_rows = [
        {"date": date, "Individuals": _decimal_text(selected[date])}
        for date in input_dates
    ]
    return selected, {
        "path": str(source),
        "file_sha256": file_sha256(source),
        "selected_rows_sha256": canonical_sha256(target_rows),
    }


def _sign(value: Decimal) -> int:
    return 1 if value > 0 else -1 if value < 0 else 0


def _direction(value: Decimal) -> str:
    return {1: "BUY", -1: "SELL", 0: "FLAT"}[_sign(value)]


def _direction_metrics(
    values: Mapping[str, Decimal],
    targets: Mapping[str, Decimal],
    dates: Sequence[str],
) -> dict[str, Any]:
    confusion = {
        "actual_buy": {"predicted_buy": 0, "predicted_sell": 0, "predicted_flat": 0},
        "actual_sell": {"predicted_buy": 0, "predicted_sell": 0, "predicted_flat": 0},
    }
    correct = 0
    actual_counts = {1: 0, -1: 0}
    predicted_counts = {1: 0, -1: 0, 0: 0}
    observed: list[tuple[int, int]] = []
    for date in dates:
        actual = _sign(targets[date])
        predicted = _sign(values[date])
        if actual == 0:
            raise PairEvaluationError(f"zero Individuals target on {date}")
        actual_counts[actual] += 1
        predicted_counts[predicted] += 1
        confusion["actual_buy" if actual == 1 else "actual_sell"][
            {1: "predicted_buy", -1: "predicted_sell", 0: "predicted_flat"}[
                predicted
            ]
        ] += 1
        correct += int(actual == predicted)
        observed.append((actual, predicted))
    buy_recall = (
        confusion["actual_buy"]["predicted_buy"] / actual_counts[1]
        if actual_counts[1]
        else None
    )
    sell_recall = (
        confusion["actual_sell"]["predicted_sell"] / actual_counts[-1]
        if actual_counts[-1]
        else None
    )
    balanced = (
        (buy_recall + sell_recall) / 2
        if buy_recall is not None and sell_recall is not None
        else None
    )
    labels = (1, -1, 0)
    matrix = {
        actual: {predicted: 0 for predicted in labels}
        for actual in labels
    }
    for actual, predicted in observed:
        matrix[actual][predicted] += 1
    sample_count = len(dates)
    trace = sum(matrix[label][label] for label in labels)
    actual_totals = {
        label: sum(matrix[label].values()) for label in labels
    }
    predicted_totals = {
        label: sum(matrix[actual][label] for actual in labels)
        for label in labels
    }
    numerator = trace * sample_count - sum(
        actual_totals[label] * predicted_totals[label] for label in labels
    )
    denominator_squared = (
        sample_count * sample_count
        - sum(value * value for value in predicted_totals.values())
    ) * (
        sample_count * sample_count
        - sum(value * value for value in actual_totals.values())
    )
    mcc = (
        numerator / math.sqrt(denominator_squared)
        if denominator_squared > 0
        else 0.0
    )
    return {
        "date_count": sample_count,
        "actual_buy_dates": actual_counts[1],
        "actual_sell_dates": actual_counts[-1],
        "predicted_buy_dates": predicted_counts[1],
        "predicted_sell_dates": predicted_counts[-1],
        "predicted_flat_dates": predicted_counts[0],
        "confusion_matrix": confusion,
        "accuracy": correct / sample_count,
        "buy_recall": buy_recall,
        "sell_recall": sell_recall,
        "balanced_accuracy": balanced,
        "mcc": mcc,
    }


def _agent_session_values(
    fills: Iterable[Fill],
    agent_ids: Sequence[str],
    dates: Sequence[str],
) -> dict[tuple[str, str, str], Decimal]:
    values = {
        (agent_id, date, session): Decimal(0)
        for agent_id in agent_ids
        for date in dates
        for session in ("AM", "PM")
    }
    for fill in fills:
        values[(fill.agent_id, fill.date, fill.session)] += fill.signed_value
    return values


def _daily_for_agents(
    values: Mapping[tuple[str, str, str], Decimal],
    agent_ids: Sequence[str],
    dates: Sequence[str],
) -> dict[str, Decimal]:
    return {
        date: sum(
            (
                values[(agent_id, date, "AM")]
                + values[(agent_id, date, "PM")]
                for agent_id in agent_ids
            ),
            Decimal(0),
        )
        for date in dates
    }


def _group_report(
    off_values: Mapping[tuple[str, str, str], Decimal],
    on_values: Mapping[tuple[str, str, str], Decimal],
    *,
    agent_ids: Sequence[str],
    dates: Sequence[str],
    evaluation_dates: Sequence[str],
    targets: Mapping[str, Decimal],
) -> dict[str, Any]:
    off_daily = _daily_for_agents(off_values, agent_ids, dates)
    on_daily = _daily_for_agents(on_values, agent_ids, dates)
    effects = [on_daily[date] - off_daily[date] for date in evaluation_dates]
    return {
        "agent_count": len(agent_ids),
        "agent_ids_sha256": canonical_sha256(list(agent_ids)),
        "direction_metrics": {
            OFF_CONDITION: _direction_metrics(
                off_daily, targets, evaluation_dates
            ),
            ON_CONDITION: _direction_metrics(
                on_daily, targets, evaluation_dates
            ),
        },
        "evaluation_mean_daily_effect_on_minus_off": _decimal_text(
            sum(effects, Decimal(0)) / Decimal(len(effects))
        ),
    }


def _wealth_report(
    off_fills: Sequence[Fill],
    on_fills: Sequence[Fill],
    *,
    agent_ids: Sequence[str],
    dates: Sequence[str],
    evaluation_dates: Sequence[str],
    targets: Mapping[str, Decimal],
    initial_cash: Mapping[str, Decimal],
) -> dict[str, Any]:
    off_values = _agent_session_values(off_fills, agent_ids, dates)
    on_values = _agent_session_values(on_fills, agent_ids, dates)
    tiers = sorted(set(initial_cash.values()))
    tier_reports: dict[str, Any] = {}
    for tier in tiers:
        members = [
            agent_id for agent_id in agent_ids if initial_cash[agent_id] == tier
        ]
        tier_reports[_decimal_text(tier)] = _group_report(
            off_values,
            on_values,
            agent_ids=members,
            dates=dates,
            evaluation_dates=evaluation_dates,
            targets=targets,
        )

    normalized_daily: list[dict[str, str]] = []
    normalized_effects: list[Decimal] = []
    off_normalized: dict[str, Decimal] = {}
    on_normalized: dict[str, Decimal] = {}
    divisor = Decimal(len(agent_ids))
    for date in dates:
        off_value = sum(
            (
                (
                    off_values[(agent_id, date, "AM")]
                    + off_values[(agent_id, date, "PM")]
                )
                / initial_cash[agent_id]
                for agent_id in agent_ids
            ),
            Decimal(0),
        ) / divisor
        on_value = sum(
            (
                (
                    on_values[(agent_id, date, "AM")]
                    + on_values[(agent_id, date, "PM")]
                )
                / initial_cash[agent_id]
                for agent_id in agent_ids
            ),
            Decimal(0),
        ) / divisor
        effect = on_value - off_value
        off_normalized[date] = off_value
        on_normalized[date] = on_value
        if date in set(evaluation_dates):
            normalized_effects.append(effect)
        normalized_daily.append(
            {
                "date": date,
                "off": _decimal_text(off_value),
                "on": _decimal_text(on_value),
                "on_minus_off": _decimal_text(effect),
            }
        )
    return {
        "initial_cash_by_agent_sha256": canonical_sha256(
            {
                agent_id: _decimal_text(initial_cash[agent_id])
                for agent_id in agent_ids
            }
        ),
        "initial_cash_tiers": {
            _decimal_text(tier): sum(
                initial_cash[agent_id] == tier for agent_id in agent_ids
            )
            for tier in tiers
        },
        "raw_by_initial_cash_tier": tier_reports,
        "equal_agent_initial_cash_normalized": {
            "aggregation": (
                "per agent/date sum AM+PM signed notional, divide by fixed "
                "turn-zero capital, then equal-weight mean across agents"
            ),
            "daily": normalized_daily,
            "evaluation_mean_effect_on_minus_off": _decimal_text(
                sum(normalized_effects, Decimal(0))
                / Decimal(len(normalized_effects))
            ),
            "direction_metrics": {
                OFF_CONDITION: _direction_metrics(
                    off_normalized, targets, evaluation_dates
                ),
                ON_CONDITION: _direction_metrics(
                    on_normalized, targets, evaluation_dates
                ),
            },
        },
    }


def _daily_rows(
    off_fills: Sequence[Fill],
    on_fills: Sequence[Fill],
    *,
    agent_ids: Sequence[str],
    dates: Sequence[str],
    targets: Mapping[str, Decimal],
) -> tuple[list[dict[str, Any]], dict[str, Decimal], dict[str, Decimal]]:
    off_values = _agent_session_values(off_fills, agent_ids, dates)
    on_values = _agent_session_values(on_fills, agent_ids, dates)
    off_daily = _daily_for_agents(off_values, agent_ids, dates)
    on_daily = _daily_for_agents(on_values, agent_ids, dates)
    rows: list[dict[str, Any]] = []
    for date in dates:
        off_am = sum(
            (off_values[(agent_id, date, "AM")] for agent_id in agent_ids),
            Decimal(0),
        )
        off_pm = sum(
            (off_values[(agent_id, date, "PM")] for agent_id in agent_ids),
            Decimal(0),
        )
        on_am = sum(
            (on_values[(agent_id, date, "AM")] for agent_id in agent_ids),
            Decimal(0),
        )
        on_pm = sum(
            (on_values[(agent_id, date, "PM")] for agent_id in agent_ids),
            Decimal(0),
        )
        rows.append(
            {
                "date": date,
                "Individuals": _decimal_text(targets[date]),
                "Individuals_direction": _direction(targets[date]),
                "off_am": _decimal_text(off_am),
                "off_pm": _decimal_text(off_pm),
                "off_am_pm": _decimal_text(off_daily[date]),
                "off_direction": _direction(off_daily[date]),
                "on_am": _decimal_text(on_am),
                "on_pm": _decimal_text(on_pm),
                "on_am_pm": _decimal_text(on_daily[date]),
                "on_direction": _direction(on_daily[date]),
                "on_minus_off": _decimal_text(
                    on_daily[date] - off_daily[date]
                ),
            }
        )
    return rows, off_daily, on_daily


def finalize_realnews_community_pair(
    *,
    off_run_dir: Path | str,
    on_run_dir: Path | str,
    target_csv: Path | str,
    output_path: Path | str,
) -> dict[str, Any]:
    """Validate two canonical runs and write one deterministic pair artifact."""

    off = _load_run(off_run_dir, OFF_CONDITION)
    on = _load_run(on_run_dir, ON_CONDITION)
    pair_invariant_sha = _validate_pair_invariants(off, on)
    parameters = off.signature_payload["parameters"]
    agent_ids = [str(value) for value in parameters["agent_ids"]]
    stock_code = str(parameters.get("stock_code") or "")
    if not stock_code:
        raise PairEvaluationError("run signature has no stock_code")
    events, prices, price_sha = _event_contract(off)
    calendar_sha = _validate_calendar_contract(off, events)
    dates = list(dict.fromkeys(str(event["date"]) for event in events))
    expected_event_ids = [
        event_id
        for date in dates
        for event_id in (f"{date}/AM", f"{date}/PM")
    ]
    if [str(event["event_id"]) for event in events] != expected_event_ids:
        raise PairEvaluationError(
            "event schedule is not ordered AM/PM pairs for each input date"
        )
    initial_cash, cohort_sha = _cohort_contract(off)
    off_assets_sha = _validate_initial_assets(off, initial_cash)
    on_assets_sha = _validate_initial_assets(on, initial_cash)
    if off_assets_sha != on_assets_sha:
        raise PairEvaluationError("OFF/ON turn-zero asset maps differ")

    off_fills, off_fills_sha = _validated_fill_rows(
        off.database,
        condition_id=OFF_CONDITION,
        agent_ids=agent_ids,
        events=events,
        prices=prices,
        stock_code=stock_code,
    )
    on_fills, on_fills_sha = _validated_fill_rows(
        on.database,
        condition_id=ON_CONDITION,
        agent_ids=agent_ids,
        events=events,
        prices=prices,
        stock_code=stock_code,
    )
    off_keys = {(fill.agent_id, fill.event_id) for fill in off_fills}
    on_keys = {(fill.agent_id, fill.event_id) for fill in on_fills}
    if off_keys != on_keys:
        raise PairEvaluationError("OFF/ON final fill key sets differ")

    study_path, study_sha = _sealed_input(off, "sealed_study_spec")
    study = _read_json_object(study_path, "sealed study spec")
    burn_in_raw = study.get("burn_in_date_ids")
    if not isinstance(burn_in_raw, list):
        raise PairEvaluationError("study spec has no burn_in_date_ids")
    burn_in_dates = [str(value) for value in burn_in_raw]
    if dates[: len(burn_in_dates)] != burn_in_dates:
        raise PairEvaluationError(
            "burn-in dates must be the ordered prefix of the run calendar"
        )
    evaluation_dates = [
        date for date in dates if date not in set(burn_in_dates)
    ]
    if not evaluation_dates:
        raise PairEvaluationError(
            "burn-in policy leaves no evaluation dates"
        )
    if parameters.get("study_spec_sha256") != study_sha:
        raise PairEvaluationError(
            "run parameter study_spec_sha256 differs from sealed input"
        )
    if parameters.get("cohort_sha256") != cohort_sha:
        raise PairEvaluationError(
            "run parameter cohort_sha256 differs from sealed input"
        )
    if (
        parameters.get("prompt_bundle_sha256")
        != off.signature_payload["prompts"]["tree_sha256"]
    ):
        raise PairEvaluationError(
            "run prompt_bundle_sha256 differs from prompt tree"
        )
    targets, target_lineage = _load_targets(target_csv, dates)
    daily, off_daily, on_daily = _daily_rows(
        off_fills,
        on_fills,
        agent_ids=agent_ids,
        dates=dates,
        targets=targets,
    )
    wealth = _wealth_report(
        off_fills,
        on_fills,
        agent_ids=agent_ids,
        dates=dates,
        evaluation_dates=evaluation_dates,
        targets=targets,
        initial_cash=initial_cash,
    )

    news_path, news_sha = _sealed_input(off, "news_bundle")
    pair_effects = [
        on_daily[date] - off_daily[date] for date in evaluation_dates
    ]
    body: dict[str, Any] = {
        "artifact_type": PAIR_ARTIFACT_TYPE,
        "version": PAIR_ARTIFACT_VERSION,
        "status": "pass",
        "conditions": {"off": OFF_CONDITION, "on": ON_CONDITION},
        "source_runs": {
            OFF_CONDITION: {
                "run_dir": str(off.run_dir),
                "run_id": off.metadata.get("run_id"),
                "run_signature_sha256": off.metadata[
                    "run_signature_sha256"
                ],
            },
            ON_CONDITION: {
                "run_dir": str(on.run_dir),
                "run_id": on.metadata.get("run_id"),
                "run_signature_sha256": on.metadata[
                    "run_signature_sha256"
                ],
            },
        },
        "invariant_contract": {
            "only_allowed_signature_parameter_difference": [
                "condition_id",
                "community_mode",
            ],
            "pair_invariant_sha256": pair_invariant_sha,
            "cohort_agent_count": len(agent_ids),
            "event_count": len(events),
            "expected_fill_rows_per_arm": len(agent_ids) * len(events),
            "fill_key_definition": ["agent_id", "event_id"],
            "off_on_fill_key_set_equal": True,
            "stock_code": stock_code,
            "fee_policy": "all simulation_fills.fee == 0",
            "execution_price_policy": "AM=actual_open; PM=actual_close",
        },
        "aggregation_contract": {
            "per_fill": (
                "BUY=+filled_quantity*executed_price; "
                "SELL=-filled_quantity*executed_price"
            ),
            "daily_prediction": (
                "sign(sum across the exact cohort and both AM+PM sessions)"
            ),
            "target": (
                f"sign({stock_code} Individuals final daily net trading value)"
            ),
            "primary_dates": (
                "all sealed input dates minus the sealed burn-in prefix"
            ),
            "primary_metrics": [
                "accuracy",
                "buy_recall",
                "sell_recall",
                "balanced_accuracy",
                "mcc",
            ],
            "wealth_sensitivity": (
                "fixed turn-zero capital tiers plus equal-agent, "
                "initial-capital-normalized AM+PM response"
            ),
        },
        "schedule": {
            "input_dates": dates,
            "burn_in_dates": burn_in_dates,
            "evaluation_dates": evaluation_dates,
        },
        "daily_gross_signed_fill_value": daily,
        "direction_metrics": {
            "primary_evaluation": {
                OFF_CONDITION: _direction_metrics(
                    off_daily, targets, evaluation_dates
                ),
                ON_CONDITION: _direction_metrics(
                    on_daily, targets, evaluation_dates
                ),
            },
            "full_schedule_diagnostic": {
                OFF_CONDITION: _direction_metrics(off_daily, targets, dates),
                ON_CONDITION: _direction_metrics(on_daily, targets, dates),
            },
        },
        "paired_effect": {
            "definition": (
                "daily gross signed fill value RN_COMM_ON minus RN_COMM_OFF"
            ),
            "evaluation_date_count": len(pair_effects),
            "sum": _decimal_text(sum(pair_effects, Decimal(0))),
            "mean": _decimal_text(
                sum(pair_effects, Decimal(0)) / Decimal(len(pair_effects))
            ),
            "positive_dates": sum(value > 0 for value in pair_effects),
            "negative_dates": sum(value < 0 for value in pair_effects),
            "zero_dates": sum(value == 0 for value in pair_effects),
        },
        "wealth_sensitivity": wealth,
        "hash_index": {
            "shared": {
                "pair_invariant_sha256": pair_invariant_sha,
                "sealed_study_spec_sha256": study_sha,
                "sealed_cohort_sha256": cohort_sha,
                "sealed_news_bundle_path": str(news_path),
                "sealed_news_bundle_sha256": news_sha,
                "sealed_calendar_registry_sha256": calendar_sha,
                "sealed_price_registry_sha256": price_sha,
                "prompt_tree_sha256": off.signature_payload["prompts"][
                    "tree_sha256"
                ],
                "target_source_path": target_lineage["path"],
                "target_source_sha256": target_lineage["file_sha256"],
                "selected_target_rows_sha256": target_lineage[
                    "selected_rows_sha256"
                ],
                "turn_zero_assets_sha256": off_assets_sha,
            },
            OFF_CONDITION: {
                "run_signature_file_sha256": file_sha256(
                    off.run_dir / "run_signature.json"
                ),
                "run_metadata_file_sha256": file_sha256(
                    off.run_dir / "run_metadata.json"
                ),
                "checkpoint_file_sha256": file_sha256(
                    off.run_dir / ".runtime" / "checkpoint.json"
                ),
                "committed_database_sha256": file_sha256(off.database),
                "canonical_fill_rows_sha256": off_fills_sha,
            },
            ON_CONDITION: {
                "run_signature_file_sha256": file_sha256(
                    on.run_dir / "run_signature.json"
                ),
                "run_metadata_file_sha256": file_sha256(
                    on.run_dir / "run_metadata.json"
                ),
                "checkpoint_file_sha256": file_sha256(
                    on.run_dir / ".runtime" / "checkpoint.json"
                ),
                "committed_database_sha256": file_sha256(on.database),
                "canonical_fill_rows_sha256": on_fills_sha,
            },
        },
    }
    artifact = {
        **body,
        "content_sha256": canonical_sha256(body),
    }
    destination = Path(output_path).resolve()
    if destination.exists():
        existing = _read_json_object(destination, "pair evaluation artifact")
        if existing != artifact:
            raise PairEvaluationError(
                f"pair artifact already exists with different content: {destination}"
            )
        return existing
    atomic_write_json(destination, artifact)
    return artifact


__all__ = [
    "OFF_CONDITION",
    "ON_CONDITION",
    "PAIR_ARTIFACT_TYPE",
    "PairEvaluationError",
    "finalize_realnews_community_pair",
]
