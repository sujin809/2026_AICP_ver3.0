#!/usr/bin/env python3
"""Fail-closed validator for the ``RN_COMM_OFF`` / ``RN_COMM_ON`` pair.

This validator intentionally does not reuse the historical direction validator.
That validator accepts an actual/simulation date intersection and carries legacy
defaults; neither behaviour is safe for a sealed RealNews Community A/B run.

Accepted resolved-manifest contract
===================================

The input is one explicit resolved-study manifest.  Field aliases are accepted
to make the validator usable while the resolver is being modularised, but every
scientific input below remains required (there are no inferred dates, cohort
members, prices, fees, or treatment assignments):

* a manifest hash: ``manifest_hash`` (or ``resolved_manifest_hash``);
* two condition entries named ``RN_COMM_OFF`` and ``RN_COMM_ON`` under
  ``conditions`` / ``condition_specs`` / ``arms``;
* each arm's ``condition_id``, ``community_mode`` and a shared pair proof.
  A pair proof is either the same arm-level ``pair_invariant_hash`` (aliases
  ``non_treatment_input_hash`` and ``shared_input_hash``) or exactly equal
  arm-level invariant maps (``input_hashes``, ``shared_input_hashes``,
  ``pair_invariants`` or ``scientific_inputs``).  A root-level pair proof is
  also accepted when the arm descriptors differ only in condition/treatment
  metadata;
* an explicit cohort ID list;
* an event calendar with one explicit AM and PM event per trading date, each
  carrying an event ID and its execution price (AM open / PM close); and
* explicit ``burn_in_dates`` and ``evaluation_dates`` whose union is the
  calendar date set.

Canonical final-fill CSV contract
=================================

Pass one CSV for each arm.  Every row must carry a condition ID, manifest hash,
fill ID, event ID, agent ID, Samsung stock code ``005930``, BUY/SELL action,
``filled`` status, requested quantity, filled quantity, fill price, and fee.
Date/session columns are optional only when the event ID is present; if
supplied, they must agree with the manifest event.  The validator requires
exactly one final fill for every ``(agent_id, event_id)`` key in each arm.  It
rejects missing, extra, and duplicate keys rather than intersecting or silently
dropping them.

The public paper-validation entry point accepts only the resolver's sealed
``rn_ab_evaluator_contract`` envelope.  It verifies that envelope through
``twinmarket_kr.rn_ab.resolver.verify_evaluator_contract_hash`` against a
trusted resolved manifest and price-registry hash, requires a real
64-character SHA-256 manifest hash, and requires an externally hash-pinned,
evaluator-only target registry.  None of those facts is inferred from
agent-visible or runtime state.

The output is a JSON-serialisable dictionary containing all evaluation-date
daily gross signed fill values, AM/PM decomposition, the raw paired ``ON -
OFF`` effect, the preregistered per-arm direction metrics, and the sealed
agent-first initial-capital-normalised RQ2/wealth sensitivity outputs.
``--output-dir`` additionally writes immutable, hash-indexed reviewer-facing
CSV/JSON artifacts.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from itertools import product
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from twinmarket_kr.experiment_runtime import file_sha256
from twinmarket_kr.rn_ab import canonical_sha256 as rn_ab_canonical_sha256
from twinmarket_kr.rn_ab import resolver as rn_ab_resolver


class ContractError(RuntimeError):
    """Raised when a resolved manifest or final-fill ledger is not sealed-safe."""


_MISSING = object()
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


@dataclass(frozen=True)
class Event:
    event_id: str
    date: str
    session: str
    execution_price: Decimal


@dataclass(frozen=True)
class Fill:
    fill_id: str
    condition_id: str
    manifest_hash: str
    agent_id: str
    event_id: str
    date: str
    session: str
    stock_code: str
    action: str
    fill_status: str
    requested_quantity: int
    filled_quantity: int
    fill_price: Decimal
    fee_amount: Decimal

    @property
    def signed_value(self) -> Decimal:
        direction = Decimal(1) if self.action == "BUY" else Decimal(-1)
        return direction * Decimal(self.filled_quantity) * self.fill_price


@dataclass(frozen=True)
class ABContract:
    manifest_hash: str
    pair_id: str
    off_condition_id: str
    on_condition_id: str
    cohort_agent_ids: tuple[str, ...]
    events: tuple[Event, ...]
    burn_in_dates: tuple[str, ...]
    evaluation_dates: tuple[str, ...]
    target_values: Mapping[str, Decimal] | None
    # Optional only for the generic compatibility parser/direct legacy test
    # fixtures.  The sealed paper entry point requires a complete map.
    initial_cash_by_agent: Mapping[str, Decimal] | None = None

    @property
    def events_by_id(self) -> dict[str, Event]:
        return {event.event_id: event for event in self.events}

    @property
    def expected_keys(self) -> set[tuple[str, str]]:
        return set(product(self.cohort_agent_ids, (event.event_id for event in self.events)))

    @property
    def input_dates(self) -> tuple[str, ...]:
        """Return the manifest calendar in first-event order, without inference."""

        return tuple(dict.fromkeys(event.date for event in self.events))


@dataclass(frozen=True)
class EvaluationArtifacts:
    """Human-readable and machine-verifiable evaluator handoff artifacts."""

    daily_flow_csv: Path
    paired_summary_json: Path
    direction_validation_json: Path
    artifact_index_json: Path


def _normalise_key(value: object) -> str:
    return "".join(ch for ch in str(value).lower() if ch.isalnum())


def _is_present(value: object) -> bool:
    if value is None or value is _MISSING:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _lookup(value: object, dotted_path: str) -> object:
    current = value
    for part in dotted_path.split("."):
        if not isinstance(current, Mapping):
            return _MISSING
        indexed = {_normalise_key(key): item for key, item in current.items()}
        key = _normalise_key(part)
        if key not in indexed:
            return _MISSING
        current = indexed[key]
    return current


def _equivalent_values(values: Sequence[object]) -> bool:
    if not values:
        return True
    encoded = {
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
        for value in values
    }
    return len(encoded) == 1


def _required_value(source: Mapping[str, Any], paths: Sequence[str], label: str) -> object:
    found = [value for path in paths if _is_present(value := _lookup(source, path))]
    if not found:
        raise ContractError(f"resolved manifest is missing required {label}; accepted paths={list(paths)}")
    if not _equivalent_values(found):
        raise ContractError(f"resolved manifest has conflicting values for {label}")
    return found[0]


def _optional_value(source: Mapping[str, Any], paths: Sequence[str]) -> object:
    found = [value for path in paths if _is_present(value := _lookup(source, path))]
    if not found:
        return _MISSING
    if not _equivalent_values(found):
        raise ContractError(f"resolved manifest has conflicting alias values for {list(paths)}")
    return found[0]


def _first_present_value(source: Mapping[str, Any], paths: Sequence[str]) -> object:
    """Use the first documented representation when aliases nest each other.

    ``event_calendar.events`` and ``event_calendar`` are intentionally both
    supported, for example, but they cannot be compared as literal JSON values.
    Callers use this only for representation alternatives, not scientific values.
    """
    for path in paths:
        value = _lookup(source, path)
        if _is_present(value):
            return value
    return _MISSING


def _required_text(source: Mapping[str, Any], paths: Sequence[str], label: str) -> str:
    value = _required_value(source, paths, label)
    text = str(value).strip()
    if not text:
        raise ContractError(f"resolved manifest has an empty {label}")
    return text


def _parse_date(value: object, label: str) -> str:
    text = str(value).strip()
    try:
        return datetime.strptime(text, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError as exc:
        raise ContractError(f"{label} must be YYYY-MM-DD, got {value!r}") from exc


def _parse_decimal(value: object, label: str, *, nonnegative: bool = False) -> Decimal:
    text = str(value).strip()
    if not text:
        raise ContractError(f"{label} is empty")
    try:
        number = Decimal(text.replace(",", ""))
    except (InvalidOperation, ValueError) as exc:
        raise ContractError(f"{label} is not numeric: {value!r}") from exc
    if not number.is_finite():
        raise ContractError(f"{label} must be finite: {value!r}")
    if nonnegative and number < 0:
        raise ContractError(f"{label} must be non-negative: {value!r}")
    return number


def _parse_positive_integer(value: object, label: str) -> int:
    number = _parse_decimal(value, label, nonnegative=True)
    if number != number.to_integral_value() or number < 1:
        raise ContractError(f"{label} must be a positive integer: {value!r}")
    return int(number)


def _normalise_session(value: object, label: str) -> str:
    text = str(value).strip().upper()
    aliases = {"AM": "AM", "MORNING": "AM", "PM": "PM", "AFTERNOON": "PM"}
    if text not in aliases:
        raise ContractError(f"{label} must be AM or PM, got {value!r}")
    return aliases[text]


def _normalise_mode(value: object, label: str) -> str:
    text = str(value).strip().lower()
    aliases = {"off": "off", "on": "on"}
    if text not in aliases:
        raise ContractError(f"{label} must be 'off' or 'on', got {value!r}")
    return aliases[text]


def _normalise_action(value: object, label: str) -> str:
    text = str(value).strip().upper()
    aliases = {"BUY": "BUY", "SELL": "SELL"}
    if text not in aliases:
        raise ContractError(f"{label} must be BUY or SELL, got {value!r}")
    return aliases[text]


def _normalise_stock_code(value: object, label: str) -> str:
    stock_code = str(value).strip()
    if stock_code != "005930":
        raise ContractError(f"{label} must be the approved Samsung stock code '005930', got {value!r}")
    return stock_code


def _normalise_fill_status(value: object, label: str) -> str:
    text = str(value).strip().lower()
    if text != "filled":
        raise ContractError(f"{label} must be 'filled', got {value!r}")
    return text


def _coerce_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{label} must be an object")
    return value


def _coerce_sequence(value: object, label: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContractError(f"{label} must be a list")
    return value


def _extract_conditions(manifest: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    raw = _required_value(
        manifest,
        ("conditions", "condition_specs", "arms", "condition_pair.conditions"),
        "condition registry",
    )
    if isinstance(raw, Mapping) and "items" in raw:
        raw = raw["items"]
    entries: list[tuple[str | None, Mapping[str, Any]]] = []
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            if not isinstance(value, Mapping):
                continue
            entries.append((str(key), value))
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        for value in raw:
            if not isinstance(value, Mapping):
                raise ContractError("condition registry contains a non-object condition")
            entries.append((None, value))
    else:
        raise ContractError("condition registry must be an object or list")

    result: dict[str, Mapping[str, Any]] = {}
    for mapping_key, arm in entries:
        explicit = _optional_value(arm, ("condition_id", "id", "condition"))
        condition_id = str(explicit if explicit is not _MISSING else mapping_key or "").strip()
        if not condition_id:
            raise ContractError("condition registry entry is missing condition_id")
        if condition_id in result:
            raise ContractError(f"condition registry has duplicate condition_id={condition_id!r}")
        result[condition_id] = arm
    required = {"RN_COMM_OFF", "RN_COMM_ON"}
    actual = set(result)
    if actual != required:
        raise ContractError(
            "RealNews A/B manifest must declare exactly RN_COMM_OFF and RN_COMM_ON; "
            f"missing={sorted(required - actual)}, extra={sorted(actual - required)}"
        )
    return result


def _top_level_pair_id(manifest: Mapping[str, Any]) -> str | None:
    value = _optional_value(manifest, ("condition_pair_id", "pair_id", "condition_pair.pair_id"))
    return None if value is _MISSING else str(value).strip()


def _arm_pair_id(arm: Mapping[str, Any]) -> str | None:
    value = _optional_value(arm, ("condition_pair_id", "pair_id", "pair.pair_id"))
    return None if value is _MISSING else str(value).strip()


def _canonical_without_treatment(value: object) -> object:
    """Compare arm descriptors while allowing only operational treatment labels."""
    allowed = {
        "conditionid",
        "condition",
        "id",
        "name",
        "label",
        "communitymode",
        "treatment",
        "runid",
        "rundir",
        "runroot",
        "outputdir",
        "outputpath",
        "description",
    }
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            normalized_key = _normalise_key(key)
            if normalized_key in allowed:
                continue
            # A nested community descriptor may legitimately vary only by its
            # ``mode`` field.  Preserve every other nested field so changed
            # reading/posting/permission policies are still detected.
            if normalized_key == "community" and isinstance(item, Mapping):
                result[str(key)] = {
                    str(child_key): _canonical_without_treatment(child_value)
                    for child_key, child_value in item.items()
                    if _normalise_key(child_key) not in {"mode", "communitymode"}
                }
            elif normalized_key == "community":
                # Scalar ``community: off/on`` is the same treatment indicator
                # as ``community_mode``.
                continue
            else:
                result[str(key)] = _canonical_without_treatment(item)
        return result
    if isinstance(value, list):
        return [_canonical_without_treatment(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_canonical_without_treatment(item) for item in value)
    return value


def _arm_composite_proof(arm: Mapping[str, Any]) -> str | None:
    value = _optional_value(
        arm,
        (
            "pair_invariant_hash",
            "non_treatment_input_hash",
            "shared_input_hash",
            "pair.invariant_hash",
        ),
    )
    return None if value is _MISSING else str(value).strip()


def _arm_invariant_map(arm: Mapping[str, Any]) -> Mapping[str, Any] | None:
    value = _optional_value(
        arm,
        ("input_hashes", "shared_input_hashes", "pair_invariants", "scientific_inputs"),
    )
    if value is _MISSING:
        return None
    return _coerce_mapping(value, "condition invariant map")


def _root_pair_proof(manifest: Mapping[str, Any]) -> str | None:
    value = _optional_value(
        manifest,
        (
            "pair_invariant_hash",
            "condition_pair.pair_invariant_hash",
            "condition_pair.invariant_hash",
        ),
    )
    return None if value is _MISSING else str(value).strip()


def _root_invariant_map(manifest: Mapping[str, Any]) -> Mapping[str, Any] | None:
    value = _optional_value(
        manifest,
        (
            "pair_invariants",
            "condition_pair.invariants",
            "condition_pair.shared_input_hashes",
            "shared_input_hashes",
        ),
    )
    if value is _MISSING:
        return None
    mapping = _coerce_mapping(value, "root RN pair invariant map")
    if not mapping:
        raise ContractError("root RN pair invariant map is empty")
    return mapping


def _validate_pair_invariants(
    manifest: Mapping[str, Any],
    off_arm: Mapping[str, Any],
    on_arm: Mapping[str, Any],
) -> str:
    off_mode = _normalise_mode(
        _required_value(off_arm, ("community_mode", "community.mode"), "RN_COMM_OFF community_mode"),
        "RN_COMM_OFF community_mode",
    )
    on_mode = _normalise_mode(
        _required_value(on_arm, ("community_mode", "community.mode"), "RN_COMM_ON community_mode"),
        "RN_COMM_ON community_mode",
    )
    if off_mode != "off" or on_mode != "on":
        raise ContractError(
            "RN A/B arms have invalid treatment assignment: "
            f"RN_COMM_OFF={off_mode!r}, RN_COMM_ON={on_mode!r}"
        )

    root_pair_id = _top_level_pair_id(manifest)
    supplied_pair_ids = [item for item in (_arm_pair_id(off_arm), _arm_pair_id(on_arm), root_pair_id) if item]
    if not supplied_pair_ids:
        raise ContractError("RN pair has no explicit condition_pair_id")
    if len(set(supplied_pair_ids)) != 1:
        raise ContractError(f"RN pair IDs disagree: {supplied_pair_ids}")
    pair_id = supplied_pair_ids[0]

    # A descriptor-level comparison catches a changed input field even if a
    # caller accidentally copied a stale pair hash.  The only permitted arm
    # changes are condition/treatment metadata listed in the helper above.
    if not _equivalent_values(
        [_canonical_without_treatment(off_arm), _canonical_without_treatment(on_arm)]
    ):
        raise ContractError("RN pair arm descriptors differ beyond community treatment metadata")

    off_hash = _arm_composite_proof(off_arm)
    on_hash = _arm_composite_proof(on_arm)
    if off_hash is not None or on_hash is not None:
        if not off_hash or not on_hash or off_hash != on_hash:
            raise ContractError("RN pair composite invariant hashes are missing or unequal")
        return pair_id

    off_map = _arm_invariant_map(off_arm)
    on_map = _arm_invariant_map(on_arm)
    if off_map is not None or on_map is not None:
        if off_map is None or on_map is None or not _equivalent_values([off_map, on_map]):
            raise ContractError("RN pair invariant maps are missing or unequal")
        if not off_map:
            raise ContractError("RN pair invariant map is empty")
        return pair_id

    root_proof = _root_pair_proof(manifest)
    if root_proof:
        return pair_id
    if _root_invariant_map(manifest) is not None:
        return pair_id
    raise ContractError(
        "RN pair has no pair-invariant proof; supply equal arm pair_invariant_hash values "
        "or equal arm input_hashes maps"
    )


def _extract_agent_ids(manifest: Mapping[str, Any]) -> tuple[str, ...]:
    raw = _first_present_value(
        manifest,
        (
            "cohort.agent_ids",
            "cohort_registry.agent_ids",
            "cohort_manifest.agent_ids",
            "registries.cohort_manifest.agent_ids",
            "agent_ids",
            "cohort.agents",
            "cohort_registry.agents",
            "cohort_manifest.agents",
            "registries.cohort_manifest.agents",
        ),
    )
    if raw is _MISSING:
        raise ContractError("resolved manifest is missing required frozen cohort registry")
    values = _coerce_sequence(raw, "frozen cohort registry")
    agent_ids: list[str] = []
    for item in values:
        if isinstance(item, Mapping):
            agent_id = _required_text(item, ("agent_id", "id"), "cohort agent_id")
        else:
            agent_id = str(item).strip()
        if not agent_id:
            raise ContractError("frozen cohort contains an empty agent ID")
        agent_ids.append(agent_id)
    if not agent_ids or len(agent_ids) != len(set(agent_ids)):
        raise ContractError("frozen cohort must contain at least one unique agent ID")
    return tuple(agent_ids)


def _extract_initial_cash_by_agent(
    manifest: Mapping[str, Any],
    cohort_agent_ids: tuple[str, ...],
) -> Mapping[str, Decimal] | None:
    """Return an optional exact per-agent fixed-capital map.

    Generic resolver-shaped manifests historically omitted this field, so the
    compatibility parser keeps it optional.  The sealed paper entry point
    requires it separately after parsing.
    """

    raw = _optional_value(
        manifest,
        (
            "cohort.initial_cash_by_agent",
            "initial_cash_by_agent",
        ),
    )
    if raw is _MISSING:
        return None
    mapping = _coerce_mapping(raw, "initial_cash_by_agent")
    if any(not isinstance(key, str) for key in mapping):
        raise ContractError("initial_cash_by_agent keys must be agent-ID strings")
    expected = set(cohort_agent_ids)
    actual = set(mapping)
    if actual != expected or len(mapping) != len(expected):
        raise ContractError(
            "initial_cash_by_agent must exactly cover the frozen cohort; "
            f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
        )
    values: dict[str, Decimal] = {}
    for agent_id in cohort_agent_ids:
        capital = _parse_positive_integer(
            mapping[agent_id],
            f"initial_cash_by_agent[{agent_id!r}]",
        )
        values[agent_id] = Decimal(capital)
    return values


def _event_rows(manifest: Mapping[str, Any]) -> Sequence[object]:
    raw = _first_present_value(
        manifest,
        (
            "event_calendar.events",
            "calendar.events",
            "resolved.event_calendar.events",
            "registries.event_calendar.events",
            "event_calendar",
        ),
    )
    if raw is _MISSING:
        raise ContractError("resolved manifest is missing required event calendar")
    if isinstance(raw, Mapping) and "events" in raw:
        raw = raw["events"]
    return _coerce_sequence(raw, "event calendar")


def _event_price(row: Mapping[str, Any], session: str, event_id: str) -> Decimal:
    direct = _optional_value(
        row,
        ("execution_price", "expected_execution_price", "fill_price", "announced_price"),
    )
    if direct is _MISSING:
        price_path = ("open_price", "prices.open") if session == "AM" else ("close_price", "prices.close")
        direct = _required_value(row, price_path, f"{event_id} {session} execution price")
    price = _parse_decimal(direct, f"{event_id} {session} execution price", nonnegative=True)
    if price <= 0:
        raise ContractError(f"{event_id} {session} execution price must be positive")
    return price


def _extract_events(manifest: Mapping[str, Any]) -> tuple[Event, ...]:
    events: list[Event] = []
    seen_ids: set[str] = set()
    seen_date_sessions: set[tuple[str, str]] = set()
    for index, item in enumerate(_event_rows(manifest), start=1):
        row = _coerce_mapping(item, f"event calendar row {index}")
        event_id = _required_text(row, ("event_id", "id", "decision_event_id"), "event_id")
        date = _parse_date(
            _required_value(row, ("date", "trading_date", "decision_date"), f"{event_id} date"),
            f"{event_id} date",
        )
        session = _normalise_session(
            _required_value(row, ("session", "subturn"), f"{event_id} session"),
            f"{event_id} session",
        )
        if event_id in seen_ids:
            raise ContractError(f"event calendar has duplicate event_id={event_id!r}")
        if (date, session) in seen_date_sessions:
            raise ContractError(f"event calendar has duplicate date/session={date}:{session}")
        seen_ids.add(event_id)
        seen_date_sessions.add((date, session))
        events.append(Event(event_id, date, session, _event_price(row, session, event_id)))
    if not events:
        raise ContractError("event calendar is empty")

    dates = sorted({event.date for event in events})
    for date in dates:
        sessions = {event.session for event in events if event.date == date}
        if sessions != {"AM", "PM"}:
            raise ContractError(f"event calendar must contain exactly AM and PM for {date}; got {sorted(sessions)}")
    return tuple(events)


def _extract_date_set(manifest: Mapping[str, Any], *, kind: str) -> tuple[str, ...]:
    if kind == "burn_in":
        paths = (
            "burn_in_dates",
            "burnin_dates",
            "burn_in.excluded_dates",
            "burn_in.dates",
            "evaluation.burn_in_dates",
            "study.burn_in_dates",
        )
    elif kind == "evaluation":
        paths = (
            "evaluation_dates",
            "evaluation_date_ids",
            "evaluation.date_ids",
            "evaluation.dates",
            "study.evaluation_dates",
        )
    else:  # pragma: no cover - caller-controlled constant
        raise ValueError(kind)
    raw = _required_value(manifest, paths, f"{kind} date set")
    values = _coerce_sequence(raw, f"{kind} date set")
    parsed = tuple(_parse_date(item, f"{kind} date") for item in values)
    if len(parsed) != len(set(parsed)):
        raise ContractError(f"{kind} date set has duplicates")
    if not parsed:
        raise ContractError(f"{kind} date set is empty")
    return parsed


def _target_entries(raw: object) -> Iterable[tuple[str, object]]:
    if isinstance(raw, Mapping):
        normalized = {_normalise_key(key): value for key, value in raw.items()}
        for envelope_key in ("targetvalues", "targetlabels", "values", "rows"):
            if envelope_key in normalized:
                yield from _target_entries(normalized[envelope_key])
                return
        for key, value in raw.items():
            if isinstance(value, Mapping):
                date = _optional_value(value, ("date", "trading_date"))
                target = _optional_value(
                    value,
                    ("individual_net_value", "individuals", "target_value", "value"),
                )
                if date is _MISSING:
                    date = key
                if target is _MISSING:
                    raise ContractError(f"target entry for {key!r} has no target value")
                yield str(date), target
            else:
                yield str(key), value
        return
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        for item in raw:
            row = _coerce_mapping(item, "target row")
            yield (
                str(_required_value(row, ("date", "trading_date"), "target date")),
                _required_value(
                    row,
                    ("individual_net_value", "individuals", "target_value", "value"),
                    "target value",
                ),
            )
        return
    raise ContractError("target labels must be an object or list")


def _normalise_target_values(
    raw: object,
    expected_dates: tuple[str, ...],
    *,
    label: str,
    reject_zero: bool = False,
) -> Mapping[str, Decimal]:
    values: dict[str, Decimal] = {}
    for raw_date, raw_value in _target_entries(raw):
        date = _parse_date(raw_date, f"{label} target date")
        if date in values:
            raise ContractError(f"{label} has duplicate date={date}")
        parsed = _parse_decimal(raw_value, f"{label} value for {date}")
        if reject_zero and parsed == 0:
            raise ContractError(f"{label} value for {date} must be non-zero for direction evaluation")
        values[date] = parsed
    expected = set(expected_dates)
    actual = set(values)
    if actual != expected:
        raise ContractError(
            f"{label} must exactly cover the sealed date set; "
            f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
        )
    return values


def _extract_optional_targets(
    manifest: Mapping[str, Any], evaluation_dates: tuple[str, ...]
) -> Mapping[str, Decimal] | None:
    raw = _optional_value(
        manifest,
        (
            "target_labels",
            "targets",
            "evaluator.target_labels",
            "evaluation.target_labels",
            "registries.target_labels",
        ),
    )
    if raw is _MISSING:
        return None
    return _normalise_target_values(raw, evaluation_dates, label="manifest target labels")


def parse_ab_contract(manifest: Mapping[str, Any]) -> ABContract:
    """Normalise a resolved manifest into the strict contract used by this validator."""
    manifest_hash = _required_text(
        manifest,
        (
            "manifest_hash",
            "resolved_manifest_hash",
            "resolved_manifest_sha256",
            "resolved_study_manifest_hash",
            "resolved_study_manifest_sha256",
        ),
        "resolved manifest hash",
    )
    conditions = _extract_conditions(manifest)
    off_arm = conditions["RN_COMM_OFF"]
    on_arm = conditions["RN_COMM_ON"]
    pair_id = _validate_pair_invariants(manifest, off_arm, on_arm)
    cohort = _extract_agent_ids(manifest)
    events = _extract_events(manifest)
    calendar_dates = {event.date for event in events}
    burn_in_dates = _extract_date_set(manifest, kind="burn_in")
    evaluation_dates = _extract_date_set(manifest, kind="evaluation")
    burn_set = set(burn_in_dates)
    evaluation_set = set(evaluation_dates)
    if burn_set & evaluation_set:
        raise ContractError(f"burn-in and evaluation date sets overlap: {sorted(burn_set & evaluation_set)}")
    if burn_set | evaluation_set != calendar_dates:
        raise ContractError(
            "burn-in/evaluation dates must partition the manifest calendar; "
            f"calendar_only={sorted(calendar_dates - (burn_set | evaluation_set))}, "
            f"outside_calendar={sorted((burn_set | evaluation_set) - calendar_dates)}"
        )
    return ABContract(
        manifest_hash=manifest_hash,
        pair_id=pair_id,
        off_condition_id="RN_COMM_OFF",
        on_condition_id="RN_COMM_ON",
        cohort_agent_ids=cohort,
        events=events,
        burn_in_dates=burn_in_dates,
        evaluation_dates=evaluation_dates,
        initial_cash_by_agent=_extract_initial_cash_by_agent(manifest, cohort),
        target_values=_extract_optional_targets(manifest, evaluation_dates),
    )


def _required_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ContractError(f"{label} must be a 64-character SHA-256 hex digest")
    return value.lower()


def _parse_paper_evaluator_contract(
    manifest: Mapping[str, Any],
    *,
    expected_authoritative_manifest_sha256: str,
    expected_price_registry_sha256: str,
) -> ABContract:
    """Parse the one sealed envelope accepted by the paper-facing API.

    ``parse_ab_contract`` intentionally remains a generic normaliser for
    resolver-integration and migration tests.  It is *not* a paper-validation
    entry point: accepting its alias schema here would let an unsealed manifest
    bypass the evaluator-contract hash and exact top-level schema checks.
    """

    mapping = _coerce_mapping(manifest, "paper evaluator contract")
    artifact_type = mapping.get("artifact_type")
    if artifact_type != "rn_ab_evaluator_contract":
        raise ContractError(
            "paper validation requires artifact_type='rn_ab_evaluator_contract'; "
            "generic resolved manifests are not accepted"
        )
    expected_authoritative_hash = _required_sha256(
        expected_authoritative_manifest_sha256,
        "expected authoritative resolved manifest hash",
    )
    expected_price_hash = _required_sha256(
        expected_price_registry_sha256,
        "expected price-registry hash",
    )
    try:
        rn_ab_resolver.verify_evaluator_contract_hash(
            mapping,
            expected_authoritative_manifest_sha256=expected_authoritative_hash,
            expected_price_registry_sha256=expected_price_hash,
        )
    except Exception as exc:  # Resolver errors are external-contract failures here.
        raise ContractError(f"paper evaluator-contract verification failed: {exc}") from exc

    manifest_hash = mapping.get("manifest_hash")
    if not isinstance(manifest_hash, str) or not _SHA256_RE.fullmatch(manifest_hash):
        # The resolver performs this validation too.  Keep the explicit guard
        # local so the paper API's ledger binding requirement is unambiguous.
        raise ContractError("paper evaluator contract manifest_hash must be a 64-character SHA-256 hex digest")

    price_registry = _coerce_mapping(mapping.get("price_registry"), "paper evaluator price registry")
    _normalise_stock_code(price_registry.get("stock_code"), "paper evaluator price registry.stock_code")

    # ``_extract_conditions`` rejects both missing and additional arms.  This
    # second structural check is deliberately kept after cryptographic
    # verification: a resigned envelope with an unexpected third arm must not
    # become valid simply because its content hash is internally consistent.
    contract = parse_ab_contract(mapping)
    if contract.initial_cash_by_agent is None:
        raise ContractError(
            "paper evaluator contract requires sealed cohort.initial_cash_by_agent"
        )
    return contract


def _verify_authoritative_manifest_binding(
    authoritative_manifest: Mapping[str, Any],
    *,
    expected_authoritative_manifest_sha256: str,
    evaluator_contract: Mapping[str, Any],
    contract: ABContract,
) -> None:
    """Bind the evaluator envelope to an independently supplied resolver artifact.

    The evaluator envelope's self-hash protects against accidental mutation,
    not fabricated provenance.  Paper validation therefore receives a trusted
    resolved-study artifact plus its independently recorded hash and checks the
    cohort/calendar/study identity again before reading any ledger row.
    """

    mapping = _coerce_mapping(authoritative_manifest, "authoritative resolved manifest")
    if mapping.get("artifact_type") != "resolved_study_manifest":
        raise ContractError("authoritative manifest must have artifact_type='resolved_study_manifest'")
    expected_hash = _required_sha256(
        expected_authoritative_manifest_sha256,
        "expected authoritative resolved manifest hash",
    )
    actual_hash = rn_ab_canonical_sha256(mapping)
    if actual_hash != expected_hash:
        raise ContractError(
            "authoritative resolved manifest hash differs from the trusted expected hash: "
            f"expected={expected_hash} actual={actual_hash}"
        )
    evaluator_authoritative_hash = _required_sha256(
        evaluator_contract.get("authoritative_resolved_manifest_sha256"),
        "paper evaluator contract authoritative_resolved_manifest_sha256",
    )
    if evaluator_authoritative_hash != expected_hash:
        raise ContractError("paper evaluator contract is not bound to the trusted authoritative manifest")

    source_hash = _required_sha256(
        mapping.get("source_study_spec_sha256"),
        "authoritative manifest source_study_spec_sha256",
    )
    evaluator_source_hash = _required_sha256(
        evaluator_contract.get("source_study_spec_sha256"),
        "paper evaluator contract source_study_spec_sha256",
    )
    if source_hash != evaluator_source_hash:
        raise ContractError("paper evaluator contract source StudySpec hash differs from authoritative manifest")

    raw_conditions = _coerce_sequence(mapping.get("conditions"), "authoritative manifest conditions")
    if tuple(str(value) for value in raw_conditions) != ("RN_COMM_OFF", "RN_COMM_ON"):
        raise ContractError("authoritative manifest must declare exactly RN_COMM_OFF then RN_COMM_ON")

    raw_agent_ids = _coerce_sequence(mapping.get("agent_ids"), "authoritative manifest agent_ids")
    agent_ids = tuple(str(value).strip() for value in raw_agent_ids)
    if agent_ids != contract.cohort_agent_ids:
        raise ContractError("paper evaluator cohort differs from authoritative resolved manifest")
    authoritative_initial_cash = _extract_initial_cash_by_agent(mapping, agent_ids)
    if authoritative_initial_cash is None:
        raise ContractError(
            "authoritative resolved manifest is missing sealed initial_cash_by_agent"
        )
    if contract.initial_cash_by_agent is None:  # paper parser already rejects this
        raise ContractError("paper evaluator contract is missing sealed initial_cash_by_agent")
    if dict(authoritative_initial_cash) != dict(contract.initial_cash_by_agent):
        raise ContractError(
            "paper evaluator initial_cash_by_agent differs from authoritative resolved manifest"
        )

    raw_burn_in = _coerce_sequence(mapping.get("burn_in_date_ids"), "authoritative manifest burn_in_date_ids")
    if tuple(_parse_date(value, "authoritative burn-in date") for value in raw_burn_in) != contract.burn_in_dates:
        raise ContractError("paper evaluator burn-in dates differ from authoritative resolved manifest")
    raw_evaluation = _coerce_sequence(
        mapping.get("evaluation_date_ids"), "authoritative manifest evaluation_date_ids"
    )
    if tuple(_parse_date(value, "authoritative evaluation date") for value in raw_evaluation) != contract.evaluation_dates:
        raise ContractError("paper evaluator evaluation dates differ from authoritative resolved manifest")

    raw_events = _coerce_sequence(mapping.get("decision_events"), "authoritative manifest decision_events")
    authoritative_events: list[tuple[str, str, str]] = []
    for index, raw_event in enumerate(raw_events):
        event = _coerce_mapping(raw_event, f"authoritative manifest decision event {index}")
        authoritative_events.append(
            (
                _required_text(event, ("decision_event_id", "event_id"), "authoritative decision_event_id"),
                _parse_date(_required_value(event, ("date",), "authoritative event date"), "authoritative event date"),
                _normalise_session(
                    _required_value(event, ("subturn", "session"), "authoritative event subturn"),
                    "authoritative event subturn",
                ),
            )
        )
    evaluator_events = [(event.event_id, event.date, event.session) for event in contract.events]
    if tuple(authoritative_events) != tuple(evaluator_events):
        raise ContractError("paper evaluator event calendar differs from authoritative resolved manifest")

    trade_policy = _coerce_mapping(mapping.get("trade_policy"), "authoritative manifest trade_policy")
    _normalise_stock_code(trade_policy.get("stock_code"), "authoritative manifest trade_policy.stock_code")


_TARGET_REGISTRY_FIELDS = frozenset(
    {
        "artifact_type",
        "version",
        "authoritative_resolved_manifest_sha256",
        "price_registry_sha256",
        "input_dates",
        "evaluation_dates",
        "target_values",
    }
)


def _parse_evaluator_target_registry(
    target_registry: Mapping[str, Any] | Sequence[object],
    *,
    expected_target_registry_sha256: str,
    expected_authoritative_manifest_sha256: str,
    expected_price_registry_sha256: str,
    input_dates: tuple[str, ...],
    evaluation_dates: tuple[str, ...],
) -> Mapping[str, Decimal]:
    """Validate the evaluator-only target registry and its external hash pin.

    Version 2 contains target values for the complete sealed input calendar,
    not only the post-burn-in evaluation subset.  That makes missing burn-in
    targets visible and permits the required full-period diagnostic without
    silently rebuilding a date intersection.
    """

    registry = _coerce_mapping(target_registry, "evaluator-only target registry")
    if set(registry) != _TARGET_REGISTRY_FIELDS:
        raise ContractError(
            "evaluator-only target registry has invalid fields; "
            f"missing={sorted(_TARGET_REGISTRY_FIELDS - set(registry))}, "
            f"extra={sorted(set(registry) - _TARGET_REGISTRY_FIELDS)}"
        )
    if registry.get("artifact_type") != "rn_ab_evaluator_target_registry":
        raise ContractError("evaluator-only target registry artifact_type is invalid")
    if registry.get("version") != "2":
        raise ContractError("unsupported evaluator-only target registry version")

    expected_registry_hash = _required_sha256(
        expected_target_registry_sha256,
        "expected evaluator-only target registry hash",
    )
    actual_registry_hash = rn_ab_canonical_sha256(registry)
    if actual_registry_hash != expected_registry_hash:
        raise ContractError(
            "evaluator-only target registry hash differs from the trusted expected hash: "
            f"expected={expected_registry_hash} actual={actual_registry_hash}"
        )
    expected_authoritative_hash = _required_sha256(
        expected_authoritative_manifest_sha256,
        "expected authoritative resolved manifest hash",
    )
    if _required_sha256(
        registry.get("authoritative_resolved_manifest_sha256"),
        "evaluator-only target registry authoritative_resolved_manifest_sha256",
    ) != expected_authoritative_hash:
        raise ContractError("evaluator-only target registry is not bound to the trusted authoritative manifest")
    expected_price_hash = _required_sha256(expected_price_registry_sha256, "expected price-registry hash")
    if _required_sha256(
        registry.get("price_registry_sha256"),
        "evaluator-only target registry price_registry_sha256",
    ) != expected_price_hash:
        raise ContractError("evaluator-only target registry is not bound to the trusted price registry")

    raw_input_dates = _coerce_sequence(
        registry.get("input_dates"), "evaluator-only target registry input_dates"
    )
    registry_input_dates = tuple(
        _parse_date(value, "evaluator-only target registry input date")
        for value in raw_input_dates
    )
    if registry_input_dates != input_dates:
        raise ContractError("evaluator-only target registry input dates differ from the sealed evaluator contract")
    raw_dates = _coerce_sequence(registry.get("evaluation_dates"), "evaluator-only target registry evaluation_dates")
    registry_dates = tuple(_parse_date(value, "evaluator-only target registry evaluation date") for value in raw_dates)
    if registry_dates != evaluation_dates:
        raise ContractError("evaluator-only target registry evaluation dates differ from the sealed evaluator contract")
    return _normalise_target_values(
        registry["target_values"],
        input_dates,
        label="evaluator-only target values",
        reject_zero=True,
    )


def _load_handoff_json(path: Path | str, *, label: str) -> tuple[Path, Mapping[str, Any]]:
    safe_path = _require_regular_non_symlink_file(path, label=label)
    try:
        payload = json.loads(safe_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ContractError(f"{label} is not valid JSON: {safe_path}") from exc
    return safe_path, _coerce_mapping(payload, label)


def _verify_finalization_handoff(
    *,
    finalization_record_path: Path | str,
    final_fill_export_index_path: Path | str,
    off_final_fills: Path | str,
    on_final_fills: Path | str,
    contract: ABContract,
    expected_authoritative_manifest_sha256: str,
) -> dict[str, str]:
    """Bind evaluator inputs to the integrity-gated finalization export chain."""

    record_path, record = _load_handoff_json(
        finalization_record_path, label="RN finalization record"
    )
    index_path, index = _load_handoff_json(
        final_fill_export_index_path, label="RN final-fill export index"
    )
    if record.get("artifact_type") != "rn_ab_finalization_record" or record.get("version") != "1":
        raise ContractError("RN finalization record has an unsupported artifact type or version")
    if record.get("status") != "local_integrity_passed_pending_evaluator_only_target_join":
        raise ContractError("RN finalization record does not prove the local integrity gate passed")
    authoritative_hash = _required_sha256(
        expected_authoritative_manifest_sha256,
        "expected authoritative resolved manifest hash",
    )
    if _required_sha256(
        record.get("resolved_manifest_sha256"),
        "RN finalization record resolved_manifest_sha256",
    ) != authoritative_hash:
        raise ContractError("RN finalization record is bound to a different resolved manifest")
    if _required_sha256(
        record.get("evaluator_contract_sha256"),
        "RN finalization record evaluator_contract_sha256",
    ) != contract.manifest_hash:
        raise ContractError("RN finalization record is bound to a different evaluator contract")
    if record.get("expected_agent_event_key_count") != len(contract.expected_keys):
        raise ContractError("RN finalization record expected key count differs from evaluator contract")

    journals = _coerce_mapping(record.get("journals"), "RN finalization record journals")
    if set(journals) != {contract.off_condition_id, contract.on_condition_id}:
        raise ContractError("RN finalization record must contain exactly both arm journal summaries")
    for condition_id in (contract.off_condition_id, contract.on_condition_id):
        summary = _coerce_mapping(journals[condition_id], f"{condition_id} journal summary")
        if summary.get("pending") != 0 or summary.get("rolled_back") != 0:
            raise ContractError(f"{condition_id} finalization journal is not terminally committed")

    if index.get("artifact_type") != "rn_final_fill_export_index" or index.get("version") != "1":
        raise ContractError("RN final-fill export index has an unsupported artifact type or version")
    if _required_sha256(
        index.get("evaluator_contract_sha256"),
        "RN final-fill export index evaluator_contract_sha256",
    ) != contract.manifest_hash:
        raise ContractError("RN final-fill export index is bound to a different evaluator contract")
    record_index = _coerce_mapping(
        record.get("final_fill_export_index"), "RN finalization record final_fill_export_index"
    )
    recorded_index_hash = _required_sha256(
        record_index.get("sha256"), "RN finalization record export-index SHA-256"
    )
    actual_index_hash = file_sha256(index_path)
    if actual_index_hash != recorded_index_hash:
        raise ContractError(
            "RN final-fill export index hash differs from finalization record: "
            f"expected={recorded_index_hash} actual={actual_index_hash}"
        )
    index_exports = _coerce_mapping(index.get("exports"), "RN final-fill export index exports")
    record_exports = _coerce_mapping(
        record_index.get("exports"), "RN finalization record indexed exports"
    )
    expected_conditions = {contract.off_condition_id, contract.on_condition_id}
    if set(index_exports) != expected_conditions or record_exports != index_exports:
        raise ContractError("RN finalization record and export index do not name identical two-arm exports")

    actual_paths = {
        contract.off_condition_id: _require_regular_non_symlink_file(
            off_final_fills, label="RN_COMM_OFF canonical final-fill CSV"
        ),
        contract.on_condition_id: _require_regular_non_symlink_file(
            on_final_fills, label="RN_COMM_ON canonical final-fill CSV"
        ),
    }
    for condition_id, actual_path in actual_paths.items():
        entry = _coerce_mapping(index_exports[condition_id], f"{condition_id} final-fill export")
        if entry.get("format") != "rn_canonical_final_fill_csv_v1":
            raise ContractError(f"{condition_id} final-fill export format is unsupported")
        if entry.get("row_count") != len(contract.expected_keys):
            raise ContractError(f"{condition_id} indexed final-fill row count differs from expected key count")
        indexed_name = str(entry.get("path", "")).strip()
        if not indexed_name or Path(indexed_name).name != actual_path.name:
            raise ContractError(f"{condition_id} final-fill path differs from the finalization index")
        expected_csv_hash = _required_sha256(
            entry.get("sha256"), f"{condition_id} indexed final-fill SHA-256"
        )
        actual_csv_hash = file_sha256(actual_path)
        if actual_csv_hash != expected_csv_hash:
            raise ContractError(
                f"{condition_id} final-fill CSV hash differs from finalization index: "
                f"expected={expected_csv_hash} actual={actual_csv_hash}"
            )
    return {
        "finalization_record_sha256": file_sha256(record_path),
        "final_fill_export_index_sha256": actual_index_hash,
    }


_CSV_ALIASES = {
    "fill_id": ("fill_id", "ledger_id"),
    "condition_id": ("condition_id", "condition"),
    "manifest_hash": (
        "manifest_hash",
        "resolved_manifest_hash",
        "resolved_manifest_sha256",
        "resolved_study_manifest_hash",
        "resolved_study_manifest_sha256",
    ),
    "agent_id": ("agent_id", "user_id"),
    "event_id": ("event_id", "decision_event_id"),
    "stock_code": ("stock_code",),
    "action": ("action", "direction", "side"),
    "fill_status": ("fill_status", "status"),
    "requested_quantity": ("requested_quantity", "requested_qty", "order_quantity"),
    "filled_quantity": ("filled_quantity", "filled_qty", "executed_quantity"),
    "fill_price": ("fill_price", "executed_price", "price"),
    "fee_amount": ("fee_amount", "fee"),
}


def _require_regular_non_symlink_file(path: Path | str, *, label: str) -> Path:
    """Reject missing, aliased, and non-regular CLI input files before parsing.

    ``lstat`` on the leaf alone is insufficient: ``safe/manifest.json`` can
    itself be a regular file while ``safe`` is a caller-controlled symlink.
    Every lexical component is therefore checked.  macOS exposes ``/var`` and
    ``/tmp`` as OS-owned aliases; those two documented root aliases are
    tolerated so normal temporary paths remain usable, while every subsequent
    symlink remains forbidden.
    """

    raw_path = Path(path)
    if ".." in raw_path.parts:
        raise ContractError(f"{label} path must not contain '..': {raw_path}")
    candidate = Path(os.path.abspath(os.fspath(raw_path)))
    permitted_system_aliases = {
        Path("/var"): Path("/private/var"),
        Path("/tmp"): Path("/private/tmp"),
    }
    cursor = Path(candidate.anchor)
    for component in candidate.parts[1:]:
        cursor = cursor / component
        try:
            component_mode = cursor.lstat().st_mode
        except FileNotFoundError as exc:
            raise ContractError(f"{label} does not exist: {candidate}") from exc
        except OSError as exc:
            raise ContractError(f"cannot inspect {label}: {candidate}") from exc
        if stat.S_ISLNK(component_mode):
            allowed_target = permitted_system_aliases.get(cursor)
            if allowed_target is None or cursor.resolve() != allowed_target:
                raise ContractError(f"{label} must not contain a symbolic link: {cursor}")
    try:
        mode = candidate.lstat().st_mode
    except FileNotFoundError as exc:
        raise ContractError(f"{label} does not exist: {candidate}") from exc
    except OSError as exc:
        raise ContractError(f"cannot inspect {label}: {candidate}") from exc
    if stat.S_ISLNK(mode):
        raise ContractError(f"{label} must not be a symbolic link: {candidate}")
    if not stat.S_ISREG(mode):
        raise ContractError(f"{label} must be a regular file: {candidate}")
    return candidate


def _row_value(row: Mapping[str, str], aliases: Sequence[str], label: str, row_number: int) -> str:
    indexed = {_normalise_key(key): value for key, value in row.items() if key is not None}
    values = [indexed[_normalise_key(alias)] for alias in aliases if _normalise_key(alias) in indexed]
    values = [value for value in values if str(value).strip()]
    if not values:
        raise ContractError(f"final-fill CSV row {row_number} is missing {label}; accepted columns={list(aliases)}")
    if len({str(value).strip() for value in values}) != 1:
        raise ContractError(f"final-fill CSV row {row_number} has conflicting aliases for {label}")
    return str(values[0]).strip()


def _optional_row_value(row: Mapping[str, str], aliases: Sequence[str], label: str, row_number: int) -> str | None:
    indexed = {_normalise_key(key): value for key, value in row.items() if key is not None}
    values = [indexed[_normalise_key(alias)] for alias in aliases if _normalise_key(alias) in indexed]
    values = [value for value in values if str(value).strip()]
    if not values:
        return None
    if len({str(value).strip() for value in values}) != 1:
        raise ContractError(f"final-fill CSV row {row_number} has conflicting aliases for {label}")
    return str(values[0]).strip()


def _prices_match(actual: Decimal, expected: Decimal) -> bool:
    # A ledger price is a market input, so a tiny decimal serialisation difference
    # is acceptable; a one-won difference is not.
    return abs(actual - expected) <= Decimal("0.000001")


def read_and_validate_final_fills(
    csv_path: Path | str,
    *,
    contract: ABContract,
    condition_id: str,
) -> tuple[Fill, ...]:
    """Read one canonical final-fill CSV and enforce its complete ledger key set."""
    path = _require_regular_non_symlink_file(csv_path, label="canonical final-fill CSV")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ContractError(f"canonical final-fill CSV has no header: {path}")
        rows = list(reader)
    if not rows:
        raise ContractError(f"canonical final-fill CSV is empty: {path}")

    events = contract.events_by_id
    cohort = set(contract.cohort_agent_ids)
    fills: list[Fill] = []
    seen_keys: set[tuple[str, str]] = set()
    seen_fill_ids: set[str] = set()
    for row_number, row in enumerate(rows, start=2):
        fill_id = _row_value(row, _CSV_ALIASES["fill_id"], "fill_id", row_number)
        if fill_id in seen_fill_ids:
            raise ContractError(f"final-fill CSV has duplicate fill_id={fill_id!r}")
        seen_fill_ids.add(fill_id)
        row_condition = _row_value(row, _CSV_ALIASES["condition_id"], "condition_id", row_number)
        if row_condition != condition_id:
            raise ContractError(
                f"final-fill CSV row {row_number} has condition_id={row_condition!r}; expected {condition_id!r}"
            )
        row_manifest_hash = _row_value(row, _CSV_ALIASES["manifest_hash"], "manifest_hash", row_number)
        if row_manifest_hash != contract.manifest_hash:
            raise ContractError(
                f"final-fill CSV row {row_number} has manifest_hash={row_manifest_hash!r}; "
                f"expected {contract.manifest_hash!r}"
            )
        agent_id = _row_value(row, _CSV_ALIASES["agent_id"], "agent_id", row_number)
        event_id = _row_value(row, _CSV_ALIASES["event_id"], "event_id", row_number)
        if agent_id not in cohort:
            raise ContractError(f"final-fill CSV row {row_number} has out-of-cohort agent_id={agent_id!r}")
        event = events.get(event_id)
        if event is None:
            raise ContractError(f"final-fill CSV row {row_number} has event_id outside manifest={event_id!r}")
        key = (agent_id, event_id)
        if key in seen_keys:
            raise ContractError(f"final-fill CSV has duplicate canonical key agent/event={key!r}")
        seen_keys.add(key)

        optional_date = _optional_row_value(row, ("date", "trading_date"), "date", row_number)
        optional_session = _optional_row_value(row, ("session", "subturn"), "session", row_number)
        if (optional_date is None) != (optional_session is None):
            raise ContractError(
                f"final-fill CSV row {row_number} must provide both date and session or neither"
            )
        if optional_date is not None and optional_session is not None:
            if _parse_date(optional_date, f"final-fill row {row_number} date") != event.date:
                raise ContractError(f"final-fill CSV row {row_number} date disagrees with event_id={event_id!r}")
            if _normalise_session(optional_session, f"final-fill row {row_number} session") != event.session:
                raise ContractError(f"final-fill CSV row {row_number} session disagrees with event_id={event_id!r}")

        stock_code = _normalise_stock_code(
            _row_value(row, _CSV_ALIASES["stock_code"], "stock_code", row_number),
            f"final-fill row {row_number} stock_code",
        )
        action = _normalise_action(
            _row_value(row, _CSV_ALIASES["action"], "action", row_number),
            f"final-fill row {row_number} action",
        )
        fill_status = _normalise_fill_status(
            _row_value(row, _CSV_ALIASES["fill_status"], "fill_status", row_number),
            f"final-fill row {row_number} fill_status",
        )
        requested = _parse_positive_integer(
            _row_value(row, _CSV_ALIASES["requested_quantity"], "requested_quantity", row_number),
            f"final-fill row {row_number} requested_quantity",
        )
        filled = _parse_positive_integer(
            _row_value(row, _CSV_ALIASES["filled_quantity"], "filled_quantity", row_number),
            f"final-fill row {row_number} filled_quantity",
        )
        if requested != filled:
            raise ContractError(
                f"final-fill CSV row {row_number} is not full-filled: requested={requested}, filled={filled}"
            )
        fill_price = _parse_decimal(
            _row_value(row, _CSV_ALIASES["fill_price"], "fill_price", row_number),
            f"final-fill row {row_number} fill_price",
            nonnegative=True,
        )
        if fill_price <= 0 or not _prices_match(fill_price, event.execution_price):
            raise ContractError(
                f"final-fill CSV row {row_number} price={fill_price} does not match "
                f"{event.session} manifest price={event.execution_price} for {event_id}"
            )
        fee_amount = _parse_decimal(
            _row_value(row, _CSV_ALIASES["fee_amount"], "fee_amount", row_number),
            f"final-fill row {row_number} fee_amount",
            nonnegative=True,
        )
        if fee_amount != 0:
            raise ContractError(f"final-fill CSV row {row_number} has non-zero fee={fee_amount}")
        fills.append(
            Fill(
                fill_id=fill_id,
                condition_id=row_condition,
                manifest_hash=row_manifest_hash,
                agent_id=agent_id,
                event_id=event.event_id,
                date=event.date,
                session=event.session,
                stock_code=stock_code,
                action=action,
                fill_status=fill_status,
                requested_quantity=requested,
                filled_quantity=filled,
                fill_price=fill_price,
                fee_amount=fee_amount,
            )
        )

    expected = contract.expected_keys
    missing = expected - seen_keys
    extra = seen_keys - expected
    if missing or extra:
        raise ContractError(
            "canonical final-fill key set does not exactly match manifest; "
            f"missing={sorted(missing)[:10]}, extra={sorted(extra)[:10]}"
        )
    return tuple(fills)


def _sign(value: Decimal) -> int:
    return 1 if value > 0 else -1 if value < 0 else 0


def _direction_name(value: Decimal) -> str:
    return {1: "BUY", -1: "SELL", 0: "FLAT"}[_sign(value)]


def _daily_values(fills: Sequence[Fill], dates: Iterable[str]) -> dict[str, Decimal]:
    values = {date: Decimal(0) for date in dates}
    for fill in fills:
        if fill.date not in values:
            continue
        values[fill.date] += fill.signed_value
    return values


def _daily_session_values(
    fills: Sequence[Fill], dates: Iterable[str], session: str
) -> dict[str, Decimal]:
    values = {date: Decimal(0) for date in dates}
    for fill in fills:
        if fill.date in values and fill.session == session:
            values[fill.date] += fill.signed_value
    return values


def _daily_volumes(fills: Sequence[Fill], dates: Iterable[str]) -> dict[str, Decimal]:
    values = {date: Decimal(0) for date in dates}
    for fill in fills:
        if fill.date not in values:
            continue
        direction = Decimal(1) if fill.action == "BUY" else Decimal(-1)
        values[fill.date] += direction * Decimal(fill.filled_quantity)
    return values


def _agent_session_values(
    fills: Sequence[Fill],
    agent_ids: Sequence[str],
    dates: Sequence[str],
) -> dict[tuple[str, str, str], Decimal]:
    """Index exact fill notional before any cross-agent aggregation."""

    values = {
        (agent_id, date, session): Decimal(0)
        for agent_id in agent_ids
        for date in dates
        for session in ("AM", "PM")
    }
    for fill in fills:
        key = (fill.agent_id, fill.date, fill.session)
        if key in values:
            values[key] += fill.signed_value
    return values


def _group_raw_report(
    off_values: Mapping[tuple[str, str, str], Decimal],
    on_values: Mapping[tuple[str, str, str], Decimal],
    *,
    agent_ids: Sequence[str],
    dates: Sequence[str],
    targets: Mapping[str, Decimal],
) -> dict[str, Any]:
    if not agent_ids:
        raise ContractError("wealth sensitivity group must contain at least one agent")
    off_daily: dict[str, Decimal] = {}
    on_daily: dict[str, Decimal] = {}
    rows: list[dict[str, Any]] = []
    for date in dates:
        off_am = sum((off_values[(agent, date, "AM")] for agent in agent_ids), Decimal(0))
        off_pm = sum((off_values[(agent, date, "PM")] for agent in agent_ids), Decimal(0))
        on_am = sum((on_values[(agent, date, "AM")] for agent in agent_ids), Decimal(0))
        on_pm = sum((on_values[(agent, date, "PM")] for agent in agent_ids), Decimal(0))
        off_total = off_am + off_pm
        on_total = on_am + on_pm
        off_daily[date] = off_total
        on_daily[date] = on_total
        rows.append(
            {
                "date": date,
                "RN_COMM_OFF_AM": _serialise_decimal(off_am),
                "RN_COMM_OFF_PM": _serialise_decimal(off_pm),
                "RN_COMM_OFF_AM_PM": _serialise_decimal(off_total),
                "RN_COMM_OFF_direction": _direction_name(off_total),
                "RN_COMM_ON_AM": _serialise_decimal(on_am),
                "RN_COMM_ON_PM": _serialise_decimal(on_pm),
                "RN_COMM_ON_AM_PM": _serialise_decimal(on_total),
                "RN_COMM_ON_direction": _direction_name(on_total),
                "community_effect_ON_minus_OFF": _serialise_decimal(on_total - off_total),
            }
        )
    effects = [on_daily[date] - off_daily[date] for date in dates]
    return {
        "agent_count": len(agent_ids),
        "agent_ids": list(agent_ids),
        "daily": rows,
        "mean_daily_community_effect": _serialise_decimal(
            sum(effects, Decimal(0)) / Decimal(len(dates))
        ),
        "direction_metrics": {
            "RN_COMM_OFF": _direction_metrics(off_daily, targets, dates),
            "RN_COMM_ON": _direction_metrics(on_daily, targets, dates),
        },
    }


def _normalised_group_report(
    off_values: Mapping[tuple[str, str, str], Decimal],
    on_values: Mapping[tuple[str, str, str], Decimal],
    *,
    agent_ids: Sequence[str],
    dates: Sequence[str],
    initial_cash_by_agent: Mapping[str, Decimal],
) -> dict[str, Any]:
    """Compute AM+PM per agent, divide by fixed capital, then mean agents."""

    if not agent_ids:
        raise ContractError("normalised wealth sensitivity group must not be empty")
    rows: list[dict[str, Any]] = []
    effects: list[Decimal] = []
    divisor = Decimal(len(agent_ids))
    for date in dates:
        condition_values: dict[str, dict[str, Decimal]] = {}
        for condition, source in (("RN_COMM_OFF", off_values), ("RN_COMM_ON", on_values)):
            am_ratios: list[Decimal] = []
            pm_ratios: list[Decimal] = []
            am_pm_ratios: list[Decimal] = []
            for agent_id in agent_ids:
                capital = initial_cash_by_agent[agent_id]
                agent_am = source[(agent_id, date, "AM")]
                agent_pm = source[(agent_id, date, "PM")]
                # The ordering here is scientific: sum the same agent's two
                # subturns before normalising and before the equal-agent mean.
                am_ratios.append(agent_am / capital)
                pm_ratios.append(agent_pm / capital)
                am_pm_ratios.append((agent_am + agent_pm) / capital)
            condition_values[condition] = {
                "AM": sum(am_ratios, Decimal(0)) / divisor,
                "PM": sum(pm_ratios, Decimal(0)) / divisor,
                "AM_PM": sum(am_pm_ratios, Decimal(0)) / divisor,
            }
        effect = condition_values["RN_COMM_ON"]["AM_PM"] - condition_values["RN_COMM_OFF"]["AM_PM"]
        effects.append(effect)
        rows.append(
            {
                "date": date,
                "RN_COMM_OFF_AM": _serialise_decimal(condition_values["RN_COMM_OFF"]["AM"]),
                "RN_COMM_OFF_PM": _serialise_decimal(condition_values["RN_COMM_OFF"]["PM"]),
                "RN_COMM_OFF_AM_PM": _serialise_decimal(condition_values["RN_COMM_OFF"]["AM_PM"]),
                "RN_COMM_OFF_direction": _direction_name(condition_values["RN_COMM_OFF"]["AM_PM"]),
                "RN_COMM_ON_AM": _serialise_decimal(condition_values["RN_COMM_ON"]["AM"]),
                "RN_COMM_ON_PM": _serialise_decimal(condition_values["RN_COMM_ON"]["PM"]),
                "RN_COMM_ON_AM_PM": _serialise_decimal(condition_values["RN_COMM_ON"]["AM_PM"]),
                "RN_COMM_ON_direction": _direction_name(condition_values["RN_COMM_ON"]["AM_PM"]),
                "community_effect_ON_minus_OFF": _serialise_decimal(effect),
            }
        )
    mean_effect = sum(effects, Decimal(0)) / Decimal(len(effects))
    return {
        "agent_count": len(agent_ids),
        "agent_ids": list(agent_ids),
        "aggregation_version": "agent-first-am-pm-sum-fixed-initial-cap-normalize-then-mean-v1",
        "daily": rows,
        "mean_daily_community_effect": _serialise_decimal(mean_effect),
        "mean_daily_community_effect_direction": _direction_name(mean_effect),
    }


def _p3b_metric_gate(metrics: Mapping[str, Any], targets: Mapping[str, Decimal], dates: Sequence[str]) -> bool:
    """Performance-only portion of P3-B; run/news leakage is outside this evaluator."""

    always_buy_accuracy = sum(targets[date] > 0 for date in dates) / len(dates)
    always_sell_accuracy = sum(targets[date] < 0 for date in dates) / len(dates)
    return bool(
        metrics["date_count"] == len(dates)
        and metrics["buy_recall"] is not None
        and metrics["sell_recall"] is not None
        and metrics["buy_recall"] > 0.5
        and metrics["sell_recall"] > 0.5
        and metrics["balanced_accuracy"] is not None
        and metrics["balanced_accuracy"] > 0.5
        and metrics["accuracy"] > max(always_buy_accuracy, always_sell_accuracy)
        and metrics["mcc"] > 0
    )


def _wealth_sensitivity(
    off_fills: Sequence[Fill],
    on_fills: Sequence[Fill],
    *,
    contract: ABContract,
    targets: Mapping[str, Decimal],
    dates: Sequence[str],
) -> dict[str, Any]:
    initial_cash = contract.initial_cash_by_agent
    if initial_cash is None:
        raise ContractError("wealth sensitivity requires sealed initial_cash_by_agent")
    tiers = sorted(set(initial_cash.values()))
    if len(tiers) != 2:
        raise ContractError(
            "wealth_sensitivity_v1 requires exactly two fixed initial-capital tiers"
        )
    small_capital, rich_capital = tiers
    small_agents = tuple(agent for agent in contract.cohort_agent_ids if initial_cash[agent] == small_capital)
    rich_agents = tuple(agent for agent in contract.cohort_agent_ids if initial_cash[agent] == rich_capital)
    if not small_agents or not rich_agents:
        raise ContractError("wealth_sensitivity_v1 requires non-empty small and rich groups")

    off_values = _agent_session_values(off_fills, contract.cohort_agent_ids, dates)
    on_values = _agent_session_values(on_fills, contract.cohort_agent_ids, dates)
    raw_100 = _group_raw_report(
        off_values, on_values, agent_ids=contract.cohort_agent_ids, dates=dates, targets=targets
    )
    one_eok_only = _group_raw_report(
        off_values, on_values, agent_ids=small_agents, dates=dates, targets=targets
    )
    # Both public names intentionally reference identical scientific content.
    rich_excluded = one_eok_only
    ten_eok_only = _group_raw_report(
        off_values, on_values, agent_ids=rich_agents, dates=dates, targets=targets
    )
    normalised_100 = _normalised_group_report(
        off_values,
        on_values,
        agent_ids=contract.cohort_agent_ids,
        dates=dates,
        initial_cash_by_agent=initial_cash,
    )

    full_metric_gate = _p3b_metric_gate(
        raw_100["direction_metrics"]["RN_COMM_OFF"], targets, dates
    )
    direction_sign = {"BUY": 1, "SELL": -1, "FLAT": 0}
    full_effect_sign = direction_sign[
        normalised_100["mean_daily_community_effect_direction"]
    ]
    leave_one: list[dict[str, Any]] = []
    fragile_reasons: list[str] = []
    for rich_agent in rich_agents:
        retained = tuple(agent for agent in contract.cohort_agent_ids if agent != rich_agent)
        raw = _group_raw_report(
            off_values, on_values, agent_ids=retained, dates=dates, targets=targets
        )
        normalised = _normalised_group_report(
            off_values,
            on_values,
            agent_ids=retained,
            dates=dates,
            initial_cash_by_agent=initial_cash,
        )
        metric_gate = _p3b_metric_gate(raw["direction_metrics"]["RN_COMM_OFF"], targets, dates)
        effect_sign = direction_sign[normalised["mean_daily_community_effect_direction"]]
        metric_changed = metric_gate != full_metric_gate
        effect_sign_changed = effect_sign != full_effect_sign
        if metric_changed:
            fragile_reasons.append(f"{rich_agent}:p3b_metric_gate_changed")
        if effect_sign_changed:
            fragile_reasons.append(f"{rich_agent}:rq2_mean_effect_sign_or_zero_changed")
        leave_one.append(
            {
                "excluded_rich_agent_id": rich_agent,
                "raw": raw,
                "initial_capital_normalized_equal_agent": normalised,
                "p3b_metric_gate": metric_gate,
                "p3b_metric_gate_changed_from_full": metric_changed,
                "rq2_mean_effect_sign_or_zero_changed_from_full": effect_sign_changed,
            }
        )

    alias_bytes = json.dumps(one_eok_only, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    rich_alias_bytes = json.dumps(rich_excluded, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return {
        "version": "wealth_sensitivity_v1",
        "fixed_initial_capital_tiers": {
            "one_eok_group_value": _serialise_decimal(small_capital),
            "ten_eok_group_value": _serialise_decimal(rich_capital),
            "one_eok_agent_count": len(small_agents),
            "ten_eok_agent_count": len(rich_agents),
        },
        "raw_100": raw_100,
        "one_eok_only": one_eok_only,
        "rich_excluded": rich_excluded,
        "one_eok_only_rich_excluded_alias_byte_equal": alias_bytes == rich_alias_bytes,
        "ten_eok_only": ten_eok_only,
        "initial_capital_normalized_equal_agent": normalised_100,
        "leave_one_rich_out": leave_one,
        "full_100_p3b_metric_gate": full_metric_gate,
        "wealth_fragile": bool(fragile_reasons),
        "wealth_fragile_reasons": fragile_reasons,
        "wealth_fragile_semantics": {
            "algorithm": (
                "any leave-one-rich-out P3-B performance-metric gate change or "
                "RQ2 normalized mean-effect sign/zero transition"
            ),
            "policy": "blocks robust population-wide claim; does not invalidate run integrity",
            "limitation": (
                "the external news-leakage/coverage proof is not an evaluator input; "
                "p3b_metric_gate is the performance-only component, not full core_p3b_pass"
            ),
        },
        "robust_p3b_pass": None,
        "robust_p3b_pass_status": "not_computed_without_external_core_p3b_leakage_gate",
    }


def _serialise_decimal(value: Decimal) -> float:
    # JSON numeric output is convenient for reports; all validation comparisons
    # above remain Decimal-exact before this presentation conversion.
    result = float(value)
    if not math.isfinite(result):  # pragma: no cover - Decimal finite guards above
        raise ContractError("metric cannot be represented as a finite JSON number")
    return result


def _direction_metrics(
    values: Mapping[str, Decimal],
    targets: Mapping[str, Decimal],
    dates: Sequence[str],
) -> dict[str, Any]:
    """Compute the preregistered 2-by-3 daily-direction metric family.

    Actual labels must be BUY or SELL.  Predictions retain an exact zero as
    FLAT, which is counted as an error rather than omitted or coerced.
    """

    ordered_dates = tuple(dates)
    if not ordered_dates:
        raise ContractError("direction metrics require a non-empty sealed date set")
    confusion = {
        "actual_buy": {"predicted_buy": 0, "predicted_sell": 0, "predicted_flat": 0},
        "actual_sell": {"predicted_buy": 0, "predicted_sell": 0, "predicted_flat": 0},
    }
    actual_counts = {1: 0, -1: 0}
    predicted_counts = {1: 0, -1: 0, 0: 0}
    correct = 0
    for date in ordered_dates:
        if date not in values or date not in targets:
            raise ContractError(f"direction metrics are missing sealed date={date}")
        actual = _sign(targets[date])
        if actual == 0:
            raise ContractError(f"Individuals target is zero on {date}; direction is undefined")
        predicted = _sign(values[date])
        actual_counts[actual] += 1
        predicted_counts[predicted] += 1
        row = "actual_buy" if actual == 1 else "actual_sell"
        column = {
            1: "predicted_buy",
            -1: "predicted_sell",
            0: "predicted_flat",
        }[predicted]
        confusion[row][column] += 1
        correct += int(actual == predicted)

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
    balanced_accuracy = (
        (buy_recall + sell_recall) / 2
        if buy_recall is not None and sell_recall is not None
        else None
    )

    # Generalised multiclass MCC over BUY/SELL/FLAT.  Actual FLAT has zero
    # support, but keeping it in the shared label domain correctly penalises a
    # zero simulated daily sum without dropping the date.
    labels = (1, -1, 0)
    matrix = {actual: {predicted: 0 for predicted in labels} for actual in labels}
    for actual_name, row in confusion.items():
        actual = 1 if actual_name == "actual_buy" else -1
        matrix[actual][1] = row["predicted_buy"]
        matrix[actual][-1] = row["predicted_sell"]
        matrix[actual][0] = row["predicted_flat"]
    sample_count = len(ordered_dates)
    trace = sum(matrix[label][label] for label in labels)
    actual_totals = {label: sum(matrix[label].values()) for label in labels}
    predicted_totals = {
        label: sum(matrix[actual][label] for actual in labels) for label in labels
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
    mcc = numerator / math.sqrt(denominator_squared) if denominator_squared > 0 else 0.0
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
        "balanced_accuracy": balanced_accuracy,
        "mcc": mcc,
    }


def _daily_comparison_rows(
    off_fills: Sequence[Fill],
    on_fills: Sequence[Fill],
    targets: Mapping[str, Decimal],
    dates: Sequence[str],
) -> list[dict[str, Any]]:
    off_daily = _daily_values(off_fills, dates)
    on_daily = _daily_values(on_fills, dates)
    off_am = _daily_session_values(off_fills, dates, "AM")
    off_pm = _daily_session_values(off_fills, dates, "PM")
    on_am = _daily_session_values(on_fills, dates, "AM")
    on_pm = _daily_session_values(on_fills, dates, "PM")
    off_volume = _daily_volumes(off_fills, dates)
    on_volume = _daily_volumes(on_fills, dates)
    rows: list[dict[str, Any]] = []
    for date in dates:
        target = targets[date]
        off_session_abs = abs(off_am[date]) + abs(off_pm[date])
        on_session_abs = abs(on_am[date]) + abs(on_pm[date])
        rows.append(
            {
                "date": date,
                "Individuals_net_trading_value": _serialise_decimal(target),
                "Individuals_direction": _direction_name(target),
                "RN_COMM_OFF_AM_gross_signed_fill_value": _serialise_decimal(off_am[date]),
                "RN_COMM_OFF_AM_direction": _direction_name(off_am[date]),
                "RN_COMM_OFF_PM_gross_signed_fill_value": _serialise_decimal(off_pm[date]),
                "RN_COMM_OFF_PM_direction": _direction_name(off_pm[date]),
                "RN_COMM_OFF_gross_signed_fill_value": _serialise_decimal(off_daily[date]),
                "RN_COMM_OFF_gross_signed_fill_volume": _serialise_decimal(off_volume[date]),
                "RN_COMM_OFF_direction": _direction_name(off_daily[date]),
                "RN_COMM_OFF_AM_PM_direction_discordant": int(
                    _sign(off_am[date]) != _sign(off_pm[date])
                ),
                "RN_COMM_OFF_cancellation_ratio": (
                    _serialise_decimal(abs(off_daily[date]) / off_session_abs)
                    if off_session_abs
                    else None
                ),
                "RN_COMM_OFF_direction_matches_Individuals": int(
                    _sign(off_daily[date]) == _sign(target)
                ),
                "RN_COMM_ON_AM_gross_signed_fill_value": _serialise_decimal(on_am[date]),
                "RN_COMM_ON_AM_direction": _direction_name(on_am[date]),
                "RN_COMM_ON_PM_gross_signed_fill_value": _serialise_decimal(on_pm[date]),
                "RN_COMM_ON_PM_direction": _direction_name(on_pm[date]),
                "RN_COMM_ON_gross_signed_fill_value": _serialise_decimal(on_daily[date]),
                "RN_COMM_ON_gross_signed_fill_volume": _serialise_decimal(on_volume[date]),
                "RN_COMM_ON_direction": _direction_name(on_daily[date]),
                "RN_COMM_ON_AM_PM_direction_discordant": int(
                    _sign(on_am[date]) != _sign(on_pm[date])
                ),
                "RN_COMM_ON_cancellation_ratio": (
                    _serialise_decimal(abs(on_daily[date]) / on_session_abs)
                    if on_session_abs
                    else None
                ),
                "RN_COMM_ON_direction_matches_Individuals": int(
                    _sign(on_daily[date]) == _sign(target)
                ),
                "community_effect_on_minus_off": _serialise_decimal(
                    on_daily[date] - off_daily[date]
                ),
            }
        )
    return rows


def validate_realnews_community_ab(
    manifest: Mapping[str, Any],
    *,
    off_final_fills: Path | str,
    on_final_fills: Path | str,
    finalization_record_path: Path | str | None = None,
    final_fill_export_index_path: Path | str | None = None,
    target_values: Mapping[str, Any] | None = None,
    authoritative_resolved_manifest: Mapping[str, Any] | None = None,
    expected_authoritative_manifest_sha256: str | None = None,
    expected_price_registry_sha256: str | None = None,
    expected_target_registry_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate a sealed RN paper run and return its paired-effect metrics.

    ``target_values`` is an evaluator-only target *registry*, separate from
    the runtime-facing evaluator contract.  It is pinned by an independently
    recorded hash and must exactly cover the sealed evaluation-date set.
    Likewise, the evaluator envelope is checked against an independently
    supplied authoritative resolver artifact and price-registry hash.  None of
    those provenance inputs may be inferred from the evaluator's self-hash.
    """

    if authoritative_resolved_manifest is None:
        raise ContractError("paper validation requires a trusted authoritative_resolved_manifest")
    if expected_authoritative_manifest_sha256 is None:
        raise ContractError("paper validation requires expected_authoritative_manifest_sha256")
    if expected_price_registry_sha256 is None:
        raise ContractError("paper validation requires expected_price_registry_sha256")
    if expected_target_registry_sha256 is None:
        raise ContractError("paper validation requires expected_target_registry_sha256")
    if finalization_record_path is None:
        raise ContractError("paper validation requires the integrity-gated RN finalization record")
    if final_fill_export_index_path is None:
        raise ContractError("paper validation requires the final-fill export index")
    contract = _parse_paper_evaluator_contract(
        manifest,
        expected_authoritative_manifest_sha256=expected_authoritative_manifest_sha256,
        expected_price_registry_sha256=expected_price_registry_sha256,
    )
    _verify_authoritative_manifest_binding(
        authoritative_resolved_manifest,
        expected_authoritative_manifest_sha256=expected_authoritative_manifest_sha256,
        evaluator_contract=manifest,
        contract=contract,
    )
    if target_values is None:
        raise ContractError(
            "paper validation requires evaluator-only target_values registry with exact evaluation-date coverage"
        )
    evaluator_targets = _parse_evaluator_target_registry(
        target_values,
        expected_target_registry_sha256=expected_target_registry_sha256,
        expected_authoritative_manifest_sha256=expected_authoritative_manifest_sha256,
        expected_price_registry_sha256=expected_price_registry_sha256,
        input_dates=contract.input_dates,
        evaluation_dates=contract.evaluation_dates,
    )
    handoff_hashes = _verify_finalization_handoff(
        finalization_record_path=finalization_record_path,
        final_fill_export_index_path=final_fill_export_index_path,
        off_final_fills=off_final_fills,
        on_final_fills=on_final_fills,
        contract=contract,
        expected_authoritative_manifest_sha256=expected_authoritative_manifest_sha256,
    )
    off_fills = read_and_validate_final_fills(
        off_final_fills, contract=contract, condition_id=contract.off_condition_id
    )
    on_fills = read_and_validate_final_fills(
        on_final_fills, contract=contract, condition_id=contract.on_condition_id
    )
    input_dates = contract.input_dates
    primary_dates = contract.evaluation_dates
    full_rows = _daily_comparison_rows(off_fills, on_fills, evaluator_targets, input_dates)
    primary_row_by_date = {row["date"]: row for row in full_rows}
    daily_rows = [primary_row_by_date[date] for date in primary_dates]
    off_full = _daily_values(off_fills, input_dates)
    on_full = _daily_values(on_fills, input_dates)
    off_daily = {date: off_full[date] for date in primary_dates}
    on_daily = {date: on_full[date] for date in primary_dates}
    paired = [on_daily[date] - off_daily[date] for date in primary_dates]
    off_primary_metrics = _direction_metrics(off_daily, evaluator_targets, primary_dates)
    on_primary_metrics = _direction_metrics(on_daily, evaluator_targets, primary_dates)
    off_full_metrics = _direction_metrics(off_full, evaluator_targets, input_dates)
    on_full_metrics = _direction_metrics(on_full, evaluator_targets, input_dates)
    always_buy = {date: Decimal(1) for date in primary_dates}
    always_sell = {date: Decimal(-1) for date in primary_dates}
    target_metrics = {
        "primary_date_count": len(primary_dates),
        "full_input_date_count": len(input_dates),
        "primary_metric_name": "RN_COMM_OFF_balanced_accuracy",
        "RN_COMM_OFF": off_primary_metrics,
        "RN_COMM_ON": on_primary_metrics,
        "full_period_diagnostic": {
            "RN_COMM_OFF": off_full_metrics,
            "RN_COMM_ON": on_full_metrics,
        },
        "constant_baselines": {
            "always_buy": _direction_metrics(always_buy, evaluator_targets, primary_dates),
            "always_sell": _direction_metrics(always_sell, evaluator_targets, primary_dates),
        },
        # Compatibility aliases for existing downstream readers.  Both values
        # are computed from the same exact primary date set above.
        "RN_COMM_OFF_direction_accuracy": off_primary_metrics["accuracy"],
        "RN_COMM_ON_direction_accuracy": on_primary_metrics["accuracy"],
        "target_values": {
            date: _serialise_decimal(evaluator_targets[date]) for date in primary_dates
        },
    }
    wealth_sensitivity = _wealth_sensitivity(
        off_fills,
        on_fills,
        contract=contract,
        targets=evaluator_targets,
        dates=primary_dates,
    )
    return {
        "status": "pass",
        "validation_scope": "sealed_fill_integrity_rq1_direction_and_rq2_wealth_sensitivity",
        "validator": "validate_realnews_community_ab",
        "aggregation_contract": {
            "side": "BUY=+1; SELL=-1",
            "per_fill": "side * filled_quantity * fill_price",
            "per_day": "sum across every sealed cohort agent and every manifest AM/PM event",
            "prediction": "sign(per-day gross signed fill value)",
            "target": "sign(005930 Individuals final daily net trading value)",
            "comparison": "direction only for RQ1; raw values are retained for audit",
            "rq2": (
                "per agent and date, sum AM+PM signed notional; divide by sealed fixed "
                "initial capital; equal-weight mean agents; then RN_COMM_ON minus RN_COMM_OFF"
            ),
        },
        "manifest_hash": contract.manifest_hash,
        "authoritative_resolved_manifest_sha256": _required_sha256(
            expected_authoritative_manifest_sha256,
            "expected authoritative resolved manifest hash",
        ),
        "price_registry_sha256": _required_sha256(
            expected_price_registry_sha256,
            "expected price-registry hash",
        ),
        "target_registry_sha256": _required_sha256(
            expected_target_registry_sha256,
            "expected evaluator-only target registry hash",
        ),
        "finalization_handoff": handoff_hashes,
        "stock_code": "005930",
        "condition_pair_id": contract.pair_id,
        "conditions": {
            "off": contract.off_condition_id,
            "on": contract.on_condition_id,
        },
        "cohort_agent_count": len(contract.cohort_agent_ids),
        "event_count": len(contract.events),
        "expected_final_fill_rows_per_arm": len(contract.expected_keys),
        "burn_in_dates": list(contract.burn_in_dates),
        "input_dates": list(input_dates),
        "evaluation_dates": list(primary_dates),
        "daily_gross_signed_fill_value": daily_rows,
        "full_period_daily_gross_signed_fill_value": full_rows,
        "paired_effect": {
            "definition": "date-level raw gross signed fill value: RN_COMM_ON minus RN_COMM_OFF",
            "normalization": "none; this is not the preregistered initial-capital-normalized RQ2 estimand",
            "evaluation_date_count": len(paired),
            "sum": _serialise_decimal(sum(paired, Decimal(0))),
            "mean": _serialise_decimal(sum(paired, Decimal(0)) / Decimal(len(paired))),
            "positive_dates": sum(value > 0 for value in paired),
            "negative_dates": sum(value < 0 for value in paired),
            "zero_dates": sum(value == 0 for value in paired),
            "direction_discordant_dates": [
                date
                for date in primary_dates
                if _sign(off_daily[date]) != _sign(on_daily[date])
            ],
        },
        "rq2_status": "computed_from_sealed_agent_initial_cash_map",
        "rq2_community_effect": wealth_sensitivity[
            "initial_capital_normalized_equal_agent"
        ],
        "wealth_sensitivity_v1": wealth_sensitivity,
        "target_metrics": target_metrics,
    }


def _load_manifest(path: Path | str) -> Mapping[str, Any]:
    safe_path = _require_regular_non_symlink_file(path, label="resolved manifest")
    try:
        payload = json.loads(safe_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ContractError(f"resolved manifest is not valid JSON: {safe_path}") from exc
    return _coerce_mapping(payload, "resolved manifest")


def _load_evaluator_target_values(path: Path | str) -> Mapping[str, Any]:
    safe_path = _require_regular_non_symlink_file(path, label="evaluator-only target values")
    try:
        payload = json.loads(safe_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ContractError(f"evaluator-only target values are not valid JSON: {safe_path}") from exc
    return _coerce_mapping(payload, "evaluator-only target registry")


def _write_json_atomically(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _write_immutable_text(path: Path, content: str) -> None:
    """Create or idempotently verify one evaluator artifact."""

    if path.exists():
        if path.read_text(encoding="utf-8") != content:
            raise ContractError(f"evaluation artifact already exists with different content: {path}")
        return
    temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    with temporary.open("x", encoding="utf-8", newline="") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _json_text(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_evaluation_artifacts(
    output_dir: Path | str,
    result: Mapping[str, Any],
) -> EvaluationArtifacts:
    """Write the required reviewer-facing RQ1 CSV/JSON handoff bundle.

    This function consumes only an already validated result.  It never opens a
    global database or searches for a latest run.  Files are immutable and an
    adjacent index records exact SHA-256 hashes and CSV row counts.
    """

    if result.get("status") != "pass" or result.get("validation_scope") != (
        "sealed_fill_integrity_rq1_direction_and_rq2_wealth_sensitivity"
    ):
        raise ContractError("evaluation artifacts require a passed sealed RQ1/RQ2 validation result")
    rows = result.get("daily_gross_signed_fill_value")
    if not isinstance(rows, list) or not rows or not all(isinstance(row, Mapping) for row in rows):
        raise ContractError("validated result has no non-empty daily comparison rows")
    expected_dates = result.get("evaluation_dates")
    if not isinstance(expected_dates, list) or [row.get("date") for row in rows] != expected_dates:
        raise ContractError("daily comparison rows do not exactly match sealed evaluation-date order")

    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    daily_path = root / "daily_flow_comparison.csv"
    buffer = io.StringIO(newline="")
    fieldnames = list(rows[0])
    if any(list(row) != fieldnames for row in rows):
        raise ContractError("daily comparison rows have inconsistent ordered columns")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    _write_immutable_text(daily_path, buffer.getvalue())

    paired_path = root / "paired_condition_summary.json"
    paired_payload = {
        "artifact_type": "rn_ab_paired_condition_summary",
        "version": "2",
        "manifest_hash": result.get("manifest_hash"),
        "authoritative_resolved_manifest_sha256": result.get(
            "authoritative_resolved_manifest_sha256"
        ),
        "price_registry_sha256": result.get("price_registry_sha256"),
        "target_registry_sha256": result.get("target_registry_sha256"),
        "evaluation_dates": expected_dates,
        "aggregation_contract": result.get("aggregation_contract"),
        "target_metrics": result.get("target_metrics"),
        "paired_effect": result.get("paired_effect"),
        "rq2_status": result.get("rq2_status"),
        "rq2_community_effect": result.get("rq2_community_effect"),
        "wealth_sensitivity_v1": result.get("wealth_sensitivity_v1"),
    }
    _write_immutable_text(paired_path, _json_text(paired_payload))

    validation_path = root / "direction_validation.json"
    _write_immutable_text(validation_path, _json_text(result))
    artifacts = {
        "daily_flow_comparison": {
            "path": daily_path.name,
            "format": "csv",
            "row_count": len(rows),
            "sha256": file_sha256(daily_path),
        },
        "paired_condition_summary": {
            "path": paired_path.name,
            "format": "json",
            "sha256": file_sha256(paired_path),
        },
        "direction_validation": {
            "path": validation_path.name,
            "format": "json",
            "sha256": file_sha256(validation_path),
        },
    }
    index_path = root / "evaluation_artifact_index.json"
    index_payload = {
        "artifact_type": "rn_ab_evaluation_artifact_index",
        "version": "1",
        "manifest_hash": result.get("manifest_hash"),
        "artifacts": artifacts,
    }
    _write_immutable_text(index_path, _json_text(index_payload))
    return EvaluationArtifacts(
        daily_flow_csv=daily_path,
        paired_summary_json=paired_path,
        direction_validation_json=validation_path,
        artifact_index_json=index_path,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fail-closed validation for a sealed RN_COMM_OFF/RN_COMM_ON pair."
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="Sealed rn_ab_evaluator_contract JSON.",
    )
    parser.add_argument(
        "--authoritative-resolved-manifest",
        type=Path,
        required=True,
        help="Trusted resolver-produced resolved_study_manifest JSON.",
    )
    parser.add_argument(
        "--expected-authoritative-manifest-sha256",
        required=True,
        help="Externally recorded SHA-256 of the authoritative resolved manifest.",
    )
    parser.add_argument(
        "--expected-price-registry-sha256",
        required=True,
        help="Externally recorded canonical SHA-256 of the evaluator price registry.",
    )
    parser.add_argument(
        "--off-final-fills",
        type=Path,
        required=True,
        help="Canonical final-fill CSV for RN_COMM_OFF only.",
    )
    parser.add_argument(
        "--on-final-fills",
        type=Path,
        required=True,
        help="Canonical final-fill CSV for RN_COMM_ON only.",
    )
    parser.add_argument(
        "--finalization-record",
        type=Path,
        required=True,
        help="Integrity-gated RUN_FINALIZATION.json produced before evaluator target join.",
    )
    parser.add_argument(
        "--final-fill-export-index",
        type=Path,
        required=True,
        help="Hash-indexed final_fill_export_index.json referenced by the finalization record.",
    )
    parser.add_argument(
        "--target-values",
        type=Path,
        required=True,
        help="Evaluator-only sealed target registry JSON, with exactly one value for every evaluation date.",
    )
    parser.add_argument(
        "--expected-target-registry-sha256",
        required=True,
        help="Externally recorded canonical SHA-256 of the evaluator-only target registry.",
    )
    parser.add_argument("--output-json", type=Path, help="Optional run-local validation JSON output.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        help=(
            "Optional immutable evaluator bundle directory containing "
            "daily_flow_comparison.csv and hash-indexed JSON artifacts."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = validate_realnews_community_ab(
        _load_manifest(args.manifest),
        off_final_fills=args.off_final_fills,
        on_final_fills=args.on_final_fills,
        finalization_record_path=args.finalization_record,
        final_fill_export_index_path=args.final_fill_export_index,
        target_values=_load_evaluator_target_values(args.target_values),
        authoritative_resolved_manifest=_load_manifest(args.authoritative_resolved_manifest),
        expected_authoritative_manifest_sha256=args.expected_authoritative_manifest_sha256,
        expected_price_registry_sha256=args.expected_price_registry_sha256,
        expected_target_registry_sha256=args.expected_target_registry_sha256,
    )
    if args.output_json:
        _write_json_atomically(args.output_json, result)
    if args.output_dir:
        write_evaluation_artifacts(args.output_dir, result)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
