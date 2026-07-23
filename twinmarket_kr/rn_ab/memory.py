"""Append-only STB/LTB, fill, and outcome storage for the RN AB study path."""
from __future__ import annotations

import csv
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date as calendar_date
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from twinmarket_kr.db.connection import connect, init_paper_sim_db
from twinmarket_kr.rn_ab.belief_contract import has_rn_baseline_belief_limits
from twinmarket_kr.rn_ab.news import NewsBundleError, SealedNewsRegistry
from twinmarket_kr.rn_ab.prompt_contracts import (
    ANALYSIS_CONFIDENCE_LEVELS,
    ANALYSIS_DIRECTIONAL_STANCES,
    ANALYSIS_LEGACY_FLEXIBLE_FIELDS,
    ANALYSIS_LEGACY_TEXT_FIELDS,
    ANALYSIS_OUTPUT_FIELDS,
    ANALYSIS_REFERENCE_FIELDS,
    ANALYSIS_REQUIRED_REFERENCE_SOURCES,
    BELIEF_DIMENSION_FIELDS,
)
from twinmarket_kr.rn_ab.spec import TradePolicy

if TYPE_CHECKING:
    from twinmarket_kr.rn_ab.stage_inputs import SealedStageInputRegistry


DIMENSION_KEYS = BELIEF_DIMENSION_FIELDS

# This is the single schema-version registry for a model response that may be
# consumed by the scientific RN database.  ``stage_adapter`` uses the same
# mapping when it constructs a journal key, while ``PaperMemoryStore`` checks
# the key again at the persistence boundary.
RN_STAGE_SCHEMA_VERSIONS = {
    "stb": "rn-stb-v4",
    "analysis": "rn-analysis-v3",
    "decision": "rn-decision-v3",
    "post_fill_ltb": "rn-post-fill-ltb-v4",
}
RN_AUXILIARY_STAGE_SCHEMA_VERSIONS = {
    "community_posting": "rn-community-posting-response-v1",
    "community_read_select": "rn-community-read-select-response-v1",
    "community_read_react": "rn-community-read-react-response-v1",
    "community_interpretation": "rn-community-interpretation-response-v2",
}
RN_ALL_JOURNALED_STAGE_SCHEMA_VERSIONS = {
    **RN_STAGE_SCHEMA_VERSIONS,
    **RN_AUXILIARY_STAGE_SCHEMA_VERSIONS,
}

HUMAN_LOG_RENDERER_VERSION = "rn-human-log-v1"
HUMAN_LOG_RENDERER_CANONICAL_SOURCE = """\
before = normalize_dimensions(parent)
after = normalize_dimensions(current)
evidence = normalize_exact_ordered_dimension_evidence(integration_evidence_by_dimension)
belief_summary = LF.join(f"{dimension}: {after[dimension]}" for dimension in dim_1_to_dim_6)
view_change = ordered_dim_1_to_dim_6_objects(
    dimension,
    scientific_sha256(before[dimension]),
    scientific_sha256(after[dimension]),
    evidence[dimension],
)
output = {
    "renderer_version": "rn-human-log-v1",
    "renderer_sha256": sha256(this_canonical_source_utf8),
    "belief_summary": belief_summary,
    "view_change": view_change,
}
"""
HUMAN_LOG_RENDERER_CODE_SHA256 = hashlib.sha256(
    HUMAN_LOG_RENDERER_CANONICAL_SOURCE.encode("utf-8")
).hexdigest()
_HUMAN_LOG_HASH_DOMAIN = b"rn-human-log-sha256-v1\x00"

_MEMORY_EDGE_SOURCE_KINDS = {
    "news",
    "community_claim",
    "stb",
    "ltb",
    "decision",
    "fill",
    "trade_outcome",
}


class PaperMemoryError(RuntimeError):
    """Raised when a scientific memory fact is malformed, duplicated, or late."""


@dataclass(frozen=True)
class PhaseCallConsumption:
    """Exact durable-journal response consumed by one scientific stage write.

    The response body stays in the condition-scoped journal.  The rollback-
    aware runtime database stores only this logical ID and exact response
    digest, which is enough to prove which accepted response produced a
    committed scientific artifact.
    """

    stage: str
    logical_call_id: str
    response_sha256: str


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise PaperMemoryError("Scientific payload must be canonical JSON without non-finite values") from exc


def scientific_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def human_log_sha256(value: Any) -> str:
    """Hash only a non-causal human-log object in its dedicated namespace."""
    return hashlib.sha256(
        _HUMAN_LOG_HASH_DOMAIN + canonical_json(value).encode("utf-8")
    ).hexdigest()


def normalize_dimensions(value: Mapping[str, Any], *, label: str) -> dict[str, str]:
    unknown = set(value) - set(DIMENSION_KEYS)
    missing = set(DIMENSION_KEYS) - set(value)
    if unknown or missing:
        raise PaperMemoryError(
            f"{label} must contain exactly six dimensions; missing={sorted(missing)} "
            f"unknown={sorted(unknown)}"
        )
    normalized: dict[str, str] = {}
    for key in DIMENSION_KEYS:
        text = value[key]
        if not isinstance(text, str) or not text.strip():
            raise PaperMemoryError(f"{label}.{key} must be a non-empty string")
        normalized[key] = text.strip()
    return normalized


def normalize_belief_limits(
    value: Mapping[str, Any],
    *,
    label: str = "belief_limits",
) -> Mapping[str, int]:
    """Return an immutable exact six-dimension persistence contract."""
    if not isinstance(value, Mapping) or set(value) != set(DIMENSION_KEYS):
        raise PaperMemoryError(f"{label} must contain exactly dim_1 through dim_6")
    normalized: dict[str, int] = {}
    for dimension in DIMENSION_KEYS:
        limit = value[dimension]
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise PaperMemoryError(f"{label}.{dimension} must be a positive integer")
        normalized[dimension] = limit
    if not has_rn_baseline_belief_limits(normalized):
        raise PaperMemoryError(
            f"{label} must exactly match the approved RN baseline character limits"
        )
    return MappingProxyType(normalized)


def normalize_dimension_evidence(
    value: Mapping[str, Any],
    *,
    label: str,
    allowed_ids_by_dimension: Mapping[str, set[str]],
) -> dict[str, dict[str, list[str]]]:
    """Validate the exact per-dimension provenance shape used by RN belief calls.

    IDs are deliberately opaque server-owned references.  The model may choose
    a visible ID as support or contradiction, but cannot move it between
    dimensions or invent a new source identifier.  An empty list is valid:
    the six beliefs still have to be written when an event has no relevant
    source for a particular dimension.
    """
    if not isinstance(value, Mapping):
        raise PaperMemoryError(f"{label} must be an object")
    if set(value) != set(DIMENSION_KEYS):
        raise PaperMemoryError(f"{label} must contain exactly the six dimension keys")
    normalized: dict[str, dict[str, list[str]]] = {}
    for dimension in DIMENSION_KEYS:
        item = value[dimension]
        if not isinstance(item, Mapping) or set(item) != {"support", "contradict"}:
            raise PaperMemoryError(
                f"{label}.{dimension} must contain exactly support and contradict arrays"
            )
        support = _normalize_evidence_id_list(
            item["support"], label=f"{label}.{dimension}.support"
        )
        contradict = _normalize_evidence_id_list(
            item["contradict"], label=f"{label}.{dimension}.contradict"
        )
        if set(support) & set(contradict):
            raise PaperMemoryError(
                f"{label}.{dimension} cannot cite one ID as both support and contradict"
            )
        unknown = (set(support) | set(contradict)) - set(allowed_ids_by_dimension[dimension])
        if unknown:
            raise PaperMemoryError(
                f"{label}.{dimension} cites an unavailable or cross-dimension ID: {sorted(unknown)}"
            )
        normalized[dimension] = {"support": support, "contradict": contradict}
    return normalized


def _normalize_evidence_id_list(value: Any, *, label: str) -> list[str]:
    if not isinstance(value, list):
        raise PaperMemoryError(f"{label} must be an array")
    result: list[str] = []
    for raw in value:
        identifier = _required_text(raw, label)
        if identifier in result:
            raise PaperMemoryError(f"{label} may not repeat an evidence ID")
        result.append(identifier)
    return result


def normalize_human_log_evidence(
    value: Mapping[str, Any],
    *,
    label: str,
) -> dict[str, dict[str, list[str]]]:
    """Validate renderer evidence without granting any new evidence authority."""
    if not isinstance(value, Mapping) or set(value) != set(DIMENSION_KEYS):
        raise PaperMemoryError(f"{label} must contain exactly the six dimension keys")
    normalized: dict[str, dict[str, list[str]]] = {}
    for dimension in DIMENSION_KEYS:
        item = value[dimension]
        if not isinstance(item, Mapping) or set(item) != {"support", "contradict"}:
            raise PaperMemoryError(
                f"{label}.{dimension} must contain exactly support and contradict arrays"
            )
        support = _normalize_evidence_id_list(
            item["support"], label=f"{label}.{dimension}.support"
        )
        contradict = _normalize_evidence_id_list(
            item["contradict"], label=f"{label}.{dimension}.contradict"
        )
        if set(support) & set(contradict):
            raise PaperMemoryError(
                f"{label}.{dimension} cannot cite one ID as both support and contradict"
            )
        normalized[dimension] = {"support": support, "contradict": contradict}
    return normalized


def normalize_human_log(value: Mapping[str, Any], *, label: str) -> dict[str, Any]:
    """Validate the exact server-rendered, non-causal human-log object."""
    expected_fields = {
        "renderer_version",
        "renderer_sha256",
        "belief_summary",
        "view_change",
    }
    if not isinstance(value, Mapping) or set(value) != expected_fields:
        raise PaperMemoryError(f"{label} has an invalid human-log schema")
    if value["renderer_version"] != HUMAN_LOG_RENDERER_VERSION:
        raise PaperMemoryError(f"{label} has an unapproved renderer version")
    renderer_sha256 = _strict_sha256(
        value["renderer_sha256"], f"{label}.renderer_sha256"
    )
    if renderer_sha256 != HUMAN_LOG_RENDERER_CODE_SHA256:
        raise PaperMemoryError(f"{label} has an unapproved renderer code hash")
    summary = value["belief_summary"]
    if (
        not isinstance(summary, str)
        or not summary
        or summary != summary.strip()
    ):
        raise PaperMemoryError(f"{label}.belief_summary must be canonical non-empty text")
    raw_changes = value["view_change"]
    if not isinstance(raw_changes, list) or len(raw_changes) != len(DIMENSION_KEYS):
        raise PaperMemoryError(f"{label}.view_change must have one item per dimension")
    changes: list[dict[str, Any]] = []
    evidence: dict[str, Any] = {}
    expected_change_fields = {
        "dimension",
        "before_sha256",
        "after_sha256",
        "integration_evidence",
    }
    for dimension, raw in zip(DIMENSION_KEYS, raw_changes, strict=True):
        if not isinstance(raw, Mapping) or set(raw) != expected_change_fields:
            raise PaperMemoryError(f"{label}.view_change has an invalid item schema")
        if raw["dimension"] != dimension:
            raise PaperMemoryError(f"{label}.view_change is not in canonical dimension order")
        evidence[dimension] = raw["integration_evidence"]
        changes.append(
            {
                "dimension": dimension,
                "before_sha256": _strict_sha256(
                    raw["before_sha256"],
                    f"{label}.view_change.{dimension}.before_sha256",
                ),
                "after_sha256": _strict_sha256(
                    raw["after_sha256"],
                    f"{label}.view_change.{dimension}.after_sha256",
                ),
                "integration_evidence": raw["integration_evidence"],
            }
        )
    normalized_evidence = normalize_human_log_evidence(
        evidence, label=f"{label}.view_change.integration_evidence"
    )
    for item in changes:
        item["integration_evidence"] = normalized_evidence[item["dimension"]]
    return {
        "renderer_version": HUMAN_LOG_RENDERER_VERSION,
        "renderer_sha256": HUMAN_LOG_RENDERER_CODE_SHA256,
        "belief_summary": summary,
        "view_change": changes,
    }


def _human_log_state_columns(value: Mapping[str, Any]) -> dict[str, str]:
    human_log = normalize_human_log(value, label="human log persistence")
    return {
        "belief_summary": human_log["belief_summary"],
        "view_change_json": canonical_json(human_log["view_change"]),
        "human_log_renderer_version": human_log["renderer_version"],
        "human_log_renderer_sha256": human_log["renderer_sha256"],
        "human_log_sha256": human_log_sha256(human_log),
    }


def normalize_analysis_response(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the exact legacy analysis output plus two provenance fields."""
    if not isinstance(value, Mapping) or set(value) != set(ANALYSIS_OUTPUT_FIELDS):
        raise PaperMemoryError(
            "analysis response must contain exactly: " + ",".join(ANALYSIS_OUTPUT_FIELDS)
        )
    normalized: dict[str, Any] = {
        field: _required_text(value[field], f"analysis.{field}")
        for field in ANALYSIS_LEGACY_TEXT_FIELDS
    }
    for field in ANALYSIS_LEGACY_FLEXIBLE_FIELDS:
        item = value[field]
        if isinstance(item, str):
            normalized[field] = _required_text(item, f"analysis.{field}")
            continue
        if not isinstance(item, list) or not item:
            raise PaperMemoryError(
                f"analysis.{field} must be a non-empty string or non-empty string array"
            )
        normalized[field] = [
            _required_text(entry, f"analysis.{field}[{index}]")
            for index, entry in enumerate(item)
        ]

    stance = _required_text(value["directional_stance"], "analysis.directional_stance").lower()
    if stance not in ANALYSIS_DIRECTIONAL_STANCES:
        raise PaperMemoryError("analysis.directional_stance must be buy, sell, or uncertain")
    level = _required_text(value["confidence"], "analysis.confidence").lower()
    if level not in ANALYSIS_CONFIDENCE_LEVELS:
        raise PaperMemoryError("analysis.confidence must be low, medium, or high")
    evidence_references = value["evidence_references"]
    if not isinstance(evidence_references, list) or not evidence_references:
        raise PaperMemoryError("analysis.evidence_references must be a non-empty array")
    normalized_refs: list[dict[str, str]] = []
    for index, item in enumerate(evidence_references):
        if not isinstance(item, Mapping) or set(item) != {"source", "field"}:
            raise PaperMemoryError(
                f"analysis.evidence_references[{index}] must contain exactly source and field"
            )
        source = _required_text(item["source"], f"analysis.evidence_references[{index}].source")
        field = _required_text(item["field"], f"analysis.evidence_references[{index}].field")
        if source not in ANALYSIS_REFERENCE_FIELDS or field not in ANALYSIS_REFERENCE_FIELDS[source]:
            raise PaperMemoryError("analysis evidence reference is outside the typed analysis packet")
        reference = {"source": source, "field": field}
        if reference in normalized_refs:
            raise PaperMemoryError("analysis evidence references may not repeat one typed field")
        normalized_refs.append(reference)
    observed_sources = {reference["source"] for reference in normalized_refs}
    missing_sources = set(ANALYSIS_REQUIRED_REFERENCE_SOURCES) - observed_sources
    if missing_sources:
        raise PaperMemoryError(
            "analysis.evidence_references must cite every required source: "
            + ",".join(sorted(missing_sources))
        )
    normalized["confidence"] = level
    normalized["directional_stance"] = stance
    normalized["evidence_references"] = normalized_refs
    return normalized


def normalize_decision_response(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the one model schema allowed to authorize a deterministic fill."""
    if not isinstance(value, Mapping) or set(value) != {
        "action",
        "requested_quantity",
        "reason",
        "risk_control",
    }:
        raise PaperMemoryError("decision response must contain exactly action/requested_quantity/reason/risk_control")
    action = _required_text(value["action"], "decision.action").lower()
    if action not in {"buy", "sell"}:
        raise PaperMemoryError("decision.action must be buy or sell")
    quantity = _strict_positive_int(value["requested_quantity"], "decision.requested_quantity")
    reason = _required_text(value["reason"], "decision.reason")
    risk_control = _required_text(value["risk_control"], "decision.risk_control")
    if len(reason) > 1_000 or len(risk_control) > 1_000:
        raise PaperMemoryError("decision reason or risk_control exceeds the sealed maximum length")
    return {
        "action": action,
        "requested_quantity": quantity,
        "reason": reason,
        "risk_control": risk_control,
    }


@dataclass(frozen=True)
class SixDimensionBelief:
    """Scientific belief state; human summary/change fields intentionally do not fit here."""

    dim_1: str
    dim_2: str
    dim_3: str
    dim_4: str
    dim_5: str
    dim_6: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], *, label: str = "belief") -> "SixDimensionBelief":
        return cls(**normalize_dimensions(value, label=label))

    def as_dict(self) -> dict[str, str]:
        return {key: getattr(self, key) for key in DIMENSION_KEYS}


@dataclass(frozen=True)
class EventSchedule:
    """Frozen ordered event map used to time-gate post-fill outcomes."""

    events: tuple[dict[str, Any], ...]

    @classmethod
    def from_rows(cls, rows: Iterable[Mapping[str, Any]]) -> "EventSchedule":
        parsed: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        seen_turns: set[int] = set()
        for raw in rows:
            required = {"event_id", "turn", "date", "subturn", "execution_price"}
            if set(raw) != required:
                raise PaperMemoryError("Event schedule rows must contain exactly event_id/turn/date/subturn")
            event_id = _required_text(raw["event_id"], "event_id")
            turn = _strict_positive_int(raw["turn"], "event schedule turn")
            day = _parse_iso_date(raw["date"], "event schedule date")
            subturn = _required_text(raw["subturn"], "event schedule subturn").lower()
            if subturn not in {"am", "pm"}:
                raise PaperMemoryError("Event schedule subturn must be am or pm")
            if event_id in seen_ids or turn in seen_turns:
                raise PaperMemoryError("Event schedule IDs and turns must be unique")
            seen_ids.add(event_id)
            seen_turns.add(turn)
            parsed.append(
                {
                    "event_id": event_id,
                    "turn": turn,
                    "date": day,
                    "subturn": subturn,
                    "execution_price": _strict_positive_number(
                        raw["execution_price"], "event schedule execution_price"
                    ),
                }
            )
        if not parsed:
            raise PaperMemoryError("Event schedule must not be empty")
        parsed.sort(key=lambda item: int(item["turn"]))
        if [item["turn"] for item in parsed] != list(range(1, len(parsed) + 1)):
            raise PaperMemoryError("Event schedule turns must be contiguous and start at one")
        return cls(events=tuple(parsed))

    def event(self, event_id: str) -> dict[str, Any]:
        for event in self.events:
            if event["event_id"] == event_id:
                return dict(event)
        raise PaperMemoryError(f"Event is not in the frozen schedule: {event_id}")

    def due_event_id(self, *, fill_event_id: str, horizon: str) -> str | None:
        fill = self.event(fill_event_id)
        fill_turn = int(fill["turn"])
        if horizon == "next_turn":
            return self.events[fill_turn]["event_id"] if fill_turn < len(self.events) else None
        later_same_subturn_dates: list[str] = []
        for event in self.events:
            if int(event["turn"]) <= fill_turn or event["subturn"] != fill["subturn"]:
                continue
            if event["date"] not in later_same_subturn_dates:
                later_same_subturn_dates.append(str(event["date"]))
        offset = 0 if horizon == "h1" else 4 if horizon == "h5" else None
        if offset is None:
            raise PaperMemoryError(f"Unknown outcome horizon: {horizon}")
        if len(later_same_subturn_dates) <= offset:
            return None
        due_date = later_same_subturn_dates[offset]
        for event in self.events:
            if event["date"] == due_date and event["subturn"] == fill["subturn"]:
                return str(event["event_id"])
        raise PaperMemoryError("Frozen schedule is internally inconsistent")


class PaperMemoryStore:
    """Run/condition-scoped repository with idempotent identical replay only.

    The class never uses ``INSERT OR REPLACE``.  A duplicate key with identical
    scientific content is accepted as a journal replay; a different payload is
    an integrity error that must pause the run.
    """

    def __init__(
        self,
        db_path: Path | str,
        *,
        run_id: str,
        condition_id: str,
        manifest_sha256: str,
        event_schedule: EventSchedule | None = None,
        stock_code: str = "005930",
        news_registry: SealedNewsRegistry | None = None,
        stage_input_registry: "SealedStageInputRegistry | None" = None,
        initial_portfolios: Mapping[str, Mapping[str, Any]] | None = None,
        trade_policy: TradePolicy | Mapping[str, Any] | None = None,
        belief_limits: Mapping[str, Any] | None = None,
        depth2_search_registry: Mapping[str, Any] | None = None,
    ) -> None:
        if not run_id or not condition_id or not manifest_sha256:
            raise ValueError("run_id, condition_id, and manifest_sha256 are required")
        self.db_path = Path(db_path)
        self.run_id = run_id
        self.condition_id = condition_id
        self.manifest_sha256 = manifest_sha256
        self.event_schedule = event_schedule
        if stock_code != "005930":
            raise ValueError("RN baseline store is fixed to stock code 005930")
        self.stock_code = stock_code
        if event_schedule is None or news_registry is None or stage_input_registry is None:
            raise ValueError(
                "RN PaperMemoryStore requires EventSchedule, sealed news registry, and sealed stage input registry"
            )
        stage_input_registry.assert_matches_schedule(event_schedule)
        if initial_portfolios is None:
            raise ValueError("RN PaperMemoryStore requires sealed initial portfolios for every cohort agent")
        if isinstance(trade_policy, TradePolicy):
            normalized_trade_policy = trade_policy
        elif isinstance(trade_policy, Mapping):
            normalized_trade_policy = TradePolicy.from_mapping(trade_policy)
        else:
            raise ValueError("RN PaperMemoryStore requires the sealed RN TradePolicy")
        self.news_registry = news_registry
        self.stage_input_registry = stage_input_registry
        self.depth2_search_registry = (
            None
            if depth2_search_registry is None
            else json.loads(canonical_json(dict(depth2_search_registry)))
        )
        self.initial_portfolios = self._normalize_initial_portfolios(initial_portfolios)
        self.trade_policy = normalized_trade_policy
        self.trade_policy_sha256 = scientific_sha256(normalized_trade_policy.to_dict())
        self.belief_limits = (
            None
            if belief_limits is None
            else normalize_belief_limits(belief_limits)
        )
        init_paper_sim_db(self.db_path)
        self._seal_initial_portfolios()

    def _normalize_persisted_belief(
        self,
        value: Mapping[str, Any],
        *,
        label: str,
    ) -> dict[str, str]:
        """Validate shape and sealed character caps at the final write boundary."""
        belief = normalize_dimensions(value, label=label)
        if self.belief_limits is None:
            raise PaperMemoryError(
                f"{label} cannot be persisted without sealed belief_limits"
            )
        for dimension in DIMENSION_KEYS:
            limit = self.belief_limits[dimension]
            if len(belief[dimension]) > limit:
                raise PaperMemoryError(
                    f"{label}.{dimension} exceeds its sealed character limit {limit}"
                )
        return belief

    @staticmethod
    def _normalize_initial_portfolios(
        value: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        if not isinstance(value, Mapping) or not value:
            raise ValueError("initial_portfolios must be a non-empty agent mapping")
        result: dict[str, dict[str, Any]] = {}
        for raw_agent_id, raw_state in value.items():
            agent_id = _required_text(raw_agent_id, "initial portfolio agent_id")
            if agent_id in result:
                raise ValueError("initial_portfolios contains duplicate agent IDs")
            result[agent_id] = PaperMemoryStore._normalize_portfolio_state(
                raw_state,
                label=f"initial_portfolios.{agent_id}",
            )
        return result

    def _seal_initial_portfolios(self) -> None:
        """Persist and verify the clean-base portfolio ledger on every open.

        Keeping initial cash/quantity only in the Python constructor would let
        a restarted process quietly use a different cohort mapping.  The
        append-only ledger makes the run-local SQLite base independently
        inspectable and makes any changed portfolio map a deterministic
        conflict before a worker starts.
        """

        with connect(self.db_path) as connection:
            for agent_id, state in self.initial_portfolios.items():
                portfolio_id = self._initial_portfolio_id(agent_id)
                scientific = scientific_sha256(
                    {
                        "run_id": self.run_id,
                        "condition_id": self.condition_id,
                        "manifest_sha256": self.manifest_sha256,
                        "agent_id": agent_id,
                        "cash": state["cash"],
                        "quantity": state["quantity"],
                    }
                )
                self._insert_or_verify(
                    connection,
                    table="paper_initial_portfolios",
                    key_column="initial_portfolio_id",
                    key_value=portfolio_id,
                    values={
                        "initial_portfolio_id": portfolio_id,
                        "run_id": self.run_id,
                        "condition_id": self.condition_id,
                        "manifest_sha256": self.manifest_sha256,
                        "agent_id": agent_id,
                        "cash": float(state["cash"]),
                        "quantity": int(state["quantity"]),
                        "scientific_sha256": scientific,
                    },
                    compare_columns=("scientific_sha256", "manifest_sha256"),
                )
            self._assert_initial_portfolio_ledger(connection)
            connection.commit()

    def _assert_initial_portfolio_ledger(self, connection: Any) -> None:
        rows = connection.execute(
            """
            SELECT agent_id, cash, quantity, manifest_sha256
            FROM paper_initial_portfolios
            WHERE run_id = ? AND condition_id = ?
            ORDER BY agent_id
            """,
            (self.run_id, self.condition_id),
        ).fetchall()
        observed = {str(row["agent_id"]): row for row in rows}
        expected_ids = set(self.initial_portfolios)
        if set(observed) != expected_ids:
            raise PaperMemoryError("Initial portfolio ledger key set differs from the sealed cohort")
        for agent_id, state in self.initial_portfolios.items():
            row = observed[agent_id]
            if (
                str(row["manifest_sha256"]) != self.manifest_sha256
                or abs(float(row["cash"]) - float(state["cash"])) > 1e-6
                or int(row["quantity"]) != int(state["quantity"])
            ):
                raise PaperMemoryError("Initial portfolio ledger differs from the sealed cohort mapping")

    def assert_clean_base(self) -> dict[str, Any]:
        """Require the preflight base to contain only LTB₀ and portfolios."""

        with connect(self.db_path, read_only=True) as connection:
            self._assert_initial_portfolio_ledger(connection)
            ltb_rows = connection.execute(
                """
                SELECT ltb_id, agent_id, scientific_sha256 FROM paper_ltb_states
                WHERE run_id = ? AND condition_id = ? AND event_id = 'initial' AND turn = 0
                """,
                (self.run_id, self.condition_id),
            ).fetchall()
            ltb_agents = {str(row["agent_id"]) for row in ltb_rows}
            if ltb_agents != set(self.initial_portfolios) or len(ltb_rows) != len(ltb_agents):
                raise PaperMemoryError("Clean base lacks one deterministic LTB0 per sealed agent")
            for row in ltb_rows:
                self._reconstruct_human_log(
                    connection,
                    ltb_id=_required_text(row["ltb_id"], "clean-base LTB ID"),
                )
            for table in (
                "short_term_belief_history",
                "paper_analyses",
                "paper_decisions",
                "paper_fill_ledger",
                "ltb_dimension_transitions",
                "turn_belief_trace",
                "observation_events",
                "agent_exposures",
                "memory_evidence_edges",
                "phase_consumptions",
            ):
                count = connection.execute(
                    f"SELECT COUNT(*) AS count FROM {table} WHERE run_id = ? AND condition_id = ?",
                    (self.run_id, self.condition_id),
                ).fetchone()
                if count is None or int(count["count"]) != 0:
                    raise PaperMemoryError(f"Clean base has unexpected scientific rows in {table}")
            for table in ("trade_outcomes", "ltb_outcome_consumptions"):
                count = connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
                if count is None or int(count["count"]) != 0:
                    raise PaperMemoryError(f"Clean base has unexpected scientific rows in {table}")
            portfolio_rows = connection.execute(
                """
                SELECT agent_id, cash, quantity, scientific_sha256
                FROM paper_initial_portfolios
                WHERE run_id = ? AND condition_id = ?
                ORDER BY agent_id
                """,
                (self.run_id, self.condition_id),
            ).fetchall()
        digest = scientific_sha256(
            {
                "run_id": self.run_id,
                "condition_id": self.condition_id,
                "manifest_sha256": self.manifest_sha256,
                "initial_portfolios": [dict(row) for row in portfolio_rows],
                "ltb0": [dict(row) for row in ltb_rows],
            }
        )
        return {
            "initial_portfolios": len(self.initial_portfolios),
            "ltb0": len(ltb_rows),
            "clean_base_scientific_sha256": digest,
        }

    @staticmethod
    def _normalize_portfolio_state(value: Any, *, label: str) -> dict[str, Any]:
        if not isinstance(value, Mapping) or set(value) != {"cash", "quantity"}:
            raise PaperMemoryError(f"{label} must contain exactly cash and quantity")
        cash = _strict_finite_number(value["cash"], f"{label}.cash")
        if cash < 0:
            raise PaperMemoryError(f"{label}.cash must be non-negative")
        quantity = value["quantity"]
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0:
            raise PaperMemoryError(f"{label}.quantity must be a non-negative integer")
        return {"cash": cash, "quantity": quantity}

    @staticmethod
    def _same_portfolio(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
        return (
            int(left["quantity"]) == int(right["quantity"])
            and abs(float(left["cash"]) - float(right["cash"])) <= 1e-6
        )

    @staticmethod
    def _apply_fill_to_portfolio(
        state: Mapping[str, Any],
        *,
        action: str,
        quantity: int,
        price: float,
    ) -> dict[str, Any]:
        normalized = PaperMemoryStore._normalize_portfolio_state(state, label="portfolio state")
        if action not in {"buy", "sell"}:
            raise PaperMemoryError("Paper portfolio action must be buy or sell")
        notional = float(quantity) * float(price)
        if action == "buy":
            if notional > float(normalized["cash"]) + 1e-6:
                raise PaperMemoryError("Buy quantity exceeds the sealed available cash")
            return {"cash": float(normalized["cash"]) - notional, "quantity": int(normalized["quantity"]) + quantity}
        if quantity > int(normalized["quantity"]):
            raise PaperMemoryError("Sell quantity exceeds the sealed current holding")
        return {"cash": float(normalized["cash"]) + notional, "quantity": int(normalized["quantity"]) - quantity}

    def execution_state_for_event(self, *, agent_id: str, event_id: str) -> dict[str, Any]:
        """Derive the sole legal execution state from the append-only fill ledger."""
        if self.event_schedule is None:  # pragma: no cover - constructor enforces it.
            raise PaperMemoryError("Frozen EventSchedule is required for execution state")
        raw_scheduled = self.event_schedule.event(event_id)
        scheduled = self._assert_scheduled_event(
            event_id,
            turn=int(raw_scheduled["turn"]),
            date=str(raw_scheduled["date"]),
            subturn=str(raw_scheduled["subturn"]),
        )
        turn = int(scheduled["turn"])
        price = float(scheduled["execution_price"])
        with connect(self.db_path, read_only=True) as connection:
            state = self._portfolio_before_event(
                connection,
                agent_id=agent_id,
                event_id=event_id,
                turn=turn,
            )
        return self._execution_state_from_portfolio(scheduled, state)

    def _execution_state_from_portfolio(
        self, scheduled: Mapping[str, Any], state: Mapping[str, Any]
    ) -> dict[str, Any]:
        max_buy_quantity = int(
            math.floor(
                float(state["cash"])
                * self.trade_policy.max_single_trade_cash_ratio
                / float(scheduled["execution_price"])
            )
        )
        max_sell_quantity = int(state["quantity"])
        allowed_actions: list[str] = []
        if max_buy_quantity > 0:
            allowed_actions.append("buy")
        if max_sell_quantity > 0:
            allowed_actions.append("sell")
        return {
            "available_cash": state["cash"],
            "current_quantity": state["quantity"],
            "max_buy_quantity": max_buy_quantity,
            "max_sell_quantity": max_sell_quantity,
            "allowed_actions": allowed_actions,
            "price_label": f"sealed_{scheduled['subturn']}_execution_price",
            "announced_price": float(scheduled["execution_price"]),
        }

    def _portfolio_before_event(
        self,
        connection: Any,
        *,
        agent_id: str,
        event_id: str,
        turn: int,
    ) -> dict[str, Any]:
        if self.event_schedule is None:  # pragma: no cover - constructor enforces it.
            raise PaperMemoryError("Frozen EventSchedule is required for portfolio state")
        agent = _required_text(agent_id, "portfolio agent_id")
        if agent not in self.initial_portfolios:
            raise PaperMemoryError("Agent has no sealed initial portfolio")
        raw_event = self.event_schedule.event(event_id)
        expected_event = self._assert_scheduled_event(
            event_id,
            turn=turn,
            date=str(raw_event["date"]),
            subturn=str(raw_event["subturn"]),
        )
        prior_events = tuple(self.event_schedule.events[: int(expected_event["turn"]) - 1])
        rows = connection.execute(
            """
            SELECT event_id, turn, action, filled_quantity, executed_price, fee_amount,
                   pre_portfolio_json, post_portfolio_json
            FROM paper_fill_ledger
            WHERE run_id = ? AND condition_id = ? AND agent_id = ? AND turn < ?
            ORDER BY turn
            """,
            (self.run_id, self.condition_id, agent, turn),
        ).fetchall()
        if len(rows) != len(prior_events):
            raise PaperMemoryError("Portfolio lineage is missing a prior event fill")
        state = self._initial_portfolio_from_ledger(connection, agent)
        for expected, row in zip(prior_events, rows, strict=True):
            if str(row["event_id"]) != str(expected["event_id"]) or int(row["turn"]) != int(expected["turn"]):
                raise PaperMemoryError("Portfolio ledger event order differs from frozen EventSchedule")
            if float(row["fee_amount"]) != 0.0:
                raise PaperMemoryError("Portfolio ledger contains a non-zero RN fee")
            try:
                pre = self._normalize_portfolio_state(
                    json.loads(str(row["pre_portfolio_json"])), label="stored pre_portfolio"
                )
                post = self._normalize_portfolio_state(
                    json.loads(str(row["post_portfolio_json"])), label="stored post_portfolio"
                )
            except (json.JSONDecodeError, TypeError) as exc:
                raise PaperMemoryError("Stored portfolio ledger is malformed") from exc
            if not self._same_portfolio(pre, state):
                raise PaperMemoryError("Stored pre-portfolio does not chain from sealed prior state")
            computed = self._apply_fill_to_portfolio(
                state,
                action=str(row["action"]),
                quantity=int(row["filled_quantity"]),
                price=float(row["executed_price"]),
            )
            if not self._same_portfolio(post, computed):
                raise PaperMemoryError("Stored post-portfolio does not match its sealed fill transition")
            state = computed
        return state

    def _initial_portfolio_from_ledger(self, connection: Any, agent_id: str) -> dict[str, Any]:
        agent = _required_text(agent_id, "initial portfolio agent_id")
        row = connection.execute(
            """
            SELECT cash, quantity, manifest_sha256
            FROM paper_initial_portfolios
            WHERE run_id = ? AND condition_id = ? AND agent_id = ?
            """,
            (self.run_id, self.condition_id, agent),
        ).fetchone()
        if row is None or str(row["manifest_sha256"]) != self.manifest_sha256:
            raise PaperMemoryError("Initial portfolio ledger row is missing or cross-manifest")
        state = self._normalize_portfolio_state(
            {"cash": row["cash"], "quantity": row["quantity"]},
            label="stored initial_portfolio",
        )
        expected = self.initial_portfolios.get(agent)
        if expected is None or not self._same_portfolio(state, expected):
            raise PaperMemoryError("Stored initial portfolio differs from sealed run context")
        return state

    def bootstrap_ltb(
        self,
        *,
        agent_id: str,
        date: str,
        dimensions: Mapping[str, Any],
    ) -> str:
        belief = self._normalize_persisted_belief(dimensions, label="initial_ltb")
        ltb_id = self._ltb_id(agent_id, "initial")
        scientific = scientific_sha256(
            {
                "run_id": self.run_id,
                "condition_id": self.condition_id,
                "agent_id": agent_id,
                "event_id": "initial",
                "turn": 0,
                "dimensions": belief,
            }
        )
        human_log = self._render_human_log(
            parent=belief,
            current=belief,
            integration_evidence_by_dimension={
                dimension: {"support": [], "contradict": []}
                for dimension in DIMENSION_KEYS
            },
        )
        human_columns = _human_log_state_columns(human_log)
        with connect(self.db_path) as connection:
            self._insert_or_verify(
                connection,
                table="paper_ltb_states",
                key_column="ltb_id",
                key_value=ltb_id,
                values={
                    "ltb_id": ltb_id,
                    "run_id": self.run_id,
                    "condition_id": self.condition_id,
                    "manifest_sha256": self.manifest_sha256,
                    "agent_id": agent_id,
                    "event_id": "initial",
                    "turn": 0,
                    "visible_from_turn": 1,
                    "date": date,
                    "parent_ltb_id": None,
                    "current_stb_id": None,
                    **belief,
                    "scientific_sha256": scientific,
                    **human_columns,
                },
                compare_columns=(
                    "scientific_sha256",
                    "manifest_sha256",
                    "belief_summary",
                    "view_change_json",
                    "human_log_renderer_version",
                    "human_log_sha256",
                    "human_log_renderer_sha256",
                ),
            )
            connection.commit()
        return ltb_id

    def previous_ltb(self, *, agent_id: str, decision_turn: int) -> dict[str, Any]:
        with connect(self.db_path, read_only=True) as connection:
            row = connection.execute(
                """
                SELECT * FROM paper_ltb_states
                WHERE run_id = ? AND condition_id = ? AND agent_id = ?
                  AND visible_from_turn <= ?
                ORDER BY turn DESC, created_at DESC
                LIMIT 1
                """,
                (self.run_id, self.condition_id, agent_id, int(decision_turn)),
            ).fetchone()
        if row is None:
            raise PaperMemoryError(
                f"No previous visible LTB for agent={agent_id} turn={decision_turn}"
            )
        result = dict(row)
        result["dimensions"] = {key: str(result[key]) for key in DIMENSION_KEYS}
        return result

    def human_log_for_ltb(self, *, ltb_id: str) -> dict[str, Any]:
        """Reconstruct and verify one durable, server-generated LTB human log."""
        with connect(self.db_path, read_only=True) as connection:
            return self._reconstruct_human_log(connection, ltb_id=ltb_id)

    def human_log_for_trace(self, *, trace_id: str) -> dict[str, Any]:
        """Verify a trace snapshot against its authoritative LTB renderer output."""
        with connect(self.db_path, read_only=True) as connection:
            self._assert_scoped_reference(
                connection, "turn_belief_trace", "trace_id", trace_id
            )
            row = connection.execute(
                """
                SELECT ltb_id, human_log_json, human_log_renderer_version,
                       human_log_renderer_sha256, human_log_sha256
                FROM turn_belief_trace WHERE trace_id = ?
                """,
                (trace_id,),
            ).fetchone()
            if row is None:
                raise PaperMemoryError("Human-log trace row is missing")
            stored = normalize_human_log(
                _load_canonical_json(
                    row["human_log_json"],
                    label="stored trace human_log_json",
                    expected_type=dict,
                ),
                label="stored trace human log",
            )
            authoritative = self._reconstruct_human_log(
                connection, ltb_id=_required_text(row["ltb_id"], "trace ltb_id")
            )
            if canonical_json(stored) != canonical_json(authoritative):
                raise PaperMemoryError("Trace human log differs from its authoritative LTB")
            if (
                row["human_log_renderer_version"] != HUMAN_LOG_RENDERER_VERSION
                or _strict_sha256(
                    row["human_log_renderer_sha256"],
                    "trace human_log_renderer_sha256",
                )
                != HUMAN_LOG_RENDERER_CODE_SHA256
                or _strict_sha256(row["human_log_sha256"], "trace human_log_sha256")
                != human_log_sha256(authoritative)
            ):
                raise PaperMemoryError("Trace human-log identity or digest is invalid")
            return authoritative

    def _reconstruct_human_log(
        self,
        connection: Any,
        *,
        ltb_id: str,
    ) -> dict[str, Any]:
        self._assert_scoped_reference(
            connection, "paper_ltb_states", "ltb_id", ltb_id
        )
        row = connection.execute(
            """
            SELECT agent_id, event_id, turn, parent_ltb_id, current_stb_id,
                   dim_1, dim_2, dim_3, dim_4, dim_5, dim_6,
                   belief_summary, view_change_json,
                   human_log_renderer_version, human_log_renderer_sha256,
                   human_log_sha256
            FROM paper_ltb_states WHERE ltb_id = ?
            """,
            (ltb_id,),
        ).fetchone()
        if row is None:
            raise PaperMemoryError("LTB human-log state is missing")
        current = {dimension: str(row[dimension]) for dimension in DIMENSION_KEYS}
        if int(row["turn"]) == 0:
            if row["parent_ltb_id"] is not None or row["current_stb_id"] is not None:
                raise PaperMemoryError("Initial LTB human log has non-initial lineage")
            parent = current
            evidence = {
                dimension: {"support": [], "contradict": []}
                for dimension in DIMENSION_KEYS
            }
        else:
            parent_ltb_id = _required_text(
                row["parent_ltb_id"], "human-log parent_ltb_id"
            )
            self._assert_scoped_reference(
                connection, "paper_ltb_states", "ltb_id", parent_ltb_id
            )
            parent_row = connection.execute(
                """
                SELECT dim_1, dim_2, dim_3, dim_4, dim_5, dim_6
                FROM paper_ltb_states WHERE ltb_id = ?
                """,
                (parent_ltb_id,),
            ).fetchone()
            transition = connection.execute(
                """
                SELECT run_id, condition_id, agent_id, event_id, parent_ltb_id,
                       stb_id, integration_evidence_by_dimension_json
                FROM ltb_dimension_transitions WHERE ltb_id = ?
                """,
                (ltb_id,),
            ).fetchone()
            if parent_row is None or transition is None:
                raise PaperMemoryError("LTB human log cannot resolve its parent transition")
            if (
                str(transition["run_id"]) != self.run_id
                or str(transition["condition_id"]) != self.condition_id
                or str(transition["agent_id"]) != str(row["agent_id"])
                or str(transition["event_id"]) != str(row["event_id"])
                or str(transition["parent_ltb_id"]) != parent_ltb_id
                or str(transition["stb_id"]) != str(row["current_stb_id"])
            ):
                raise PaperMemoryError("LTB human-log transition is cross-scoped or inconsistent")
            parent = {
                dimension: str(parent_row[dimension]) for dimension in DIMENSION_KEYS
            }
            evidence = _load_canonical_json(
                transition["integration_evidence_by_dimension_json"],
                label="stored human-log integration evidence",
                expected_type=dict,
            )
        expected = self._render_human_log(
            parent=parent,
            current=current,
            integration_evidence_by_dimension=evidence,
        )
        stored = normalize_human_log(
            {
                "renderer_version": row["human_log_renderer_version"],
                "renderer_sha256": row["human_log_renderer_sha256"],
                "belief_summary": row["belief_summary"],
                "view_change": _load_canonical_json(
                    row["view_change_json"],
                    label="stored LTB view_change_json",
                    expected_type=list,
                ),
            },
            label="stored LTB human log",
        )
        if canonical_json(stored) != canonical_json(expected):
            raise PaperMemoryError("Stored LTB human log differs from deterministic reconstruction")
        if (
            _strict_sha256(row["human_log_sha256"], "LTB human_log_sha256")
            != human_log_sha256(expected)
        ):
            raise PaperMemoryError("Stored LTB human-log digest is invalid")
        return expected

    def current_stb(self, *, agent_id: str, event_id: str, turn: int) -> dict[str, Any]:
        """Read the committed current-turn STB and its validated provenance."""
        turn = _strict_positive_int(turn, "current STB turn")
        expected_id = self._stb_id(agent_id, event_id)
        with connect(self.db_path, read_only=True) as connection:
            self._assert_stb_for_event(
                connection, expected_id, agent_id=agent_id, event_id=event_id, turn=turn
            )
            row = connection.execute(
                """
                SELECT dim_1, dim_2, dim_3, dim_4, dim_5, dim_6,
                       evidence_json, dimension_evidence_json, scientific_sha256
                FROM short_term_belief_history WHERE stb_id = ?
                """,
                (expected_id,),
            ).fetchone()
        if row is None:
            raise PaperMemoryError("Committed current STB disappeared during retrieval")
        evidence_rows = _load_canonical_json(
            row["evidence_json"], label="stored STB evidence_json", expected_type=list
        )
        dimension_evidence = _load_canonical_json(
            row["dimension_evidence_json"],
            label="stored STB dimension_evidence_json",
            expected_type=dict,
        )
        visible_ids = self._evidence_ids(evidence_rows)
        return {
            "stb_id": expected_id,
            "dimensions": {key: str(row[key]) for key in DIMENSION_KEYS},
            "evidence": evidence_rows,
            "dimension_evidence": normalize_dimension_evidence(
                dimension_evidence,
                label="stored STB dimension_evidence",
                allowed_ids_by_dimension={key: set(visible_ids) for key in DIMENSION_KEYS},
            ),
            "scientific_sha256": _strict_sha256(row["scientific_sha256"], "stored STB scientific_sha256"),
        }

    def current_analysis(self, *, agent_id: str, event_id: str, turn: int) -> dict[str, Any]:
        """Read exactly the committed analysis that a same-turn decision may use."""
        turn = _strict_positive_int(turn, "current analysis turn")
        analysis_id = self._analysis_id(agent_id, event_id)
        with connect(self.db_path, read_only=True) as connection:
            self._assert_scoped_reference(connection, "paper_analyses", "analysis_id", analysis_id)
            row = connection.execute(
                """
                SELECT agent_id, event_id, turn,
                       market_view, valuation_view, technical_view, news_view, portfolio_view,
                       key_risks_json, opportunity_json, caution_json,
                       confidence, directional_stance,
                       evidence_references_json, input_sha256, response_sha256, scientific_sha256
                FROM paper_analyses WHERE analysis_id = ?
                """,
                (analysis_id,),
            ).fetchone()
        if row is None or (
            str(row["agent_id"]) != agent_id
            or str(row["event_id"]) != event_id
            or int(row["turn"]) != turn
        ):
            raise PaperMemoryError("Committed analysis does not match the requested agent/event/turn")
        references = _load_canonical_json(
            row["evidence_references_json"],
            label="stored analysis evidence_references_json",
            expected_type=list,
        )
        output = normalize_analysis_response(
            {
                **{field: row[field] for field in ANALYSIS_LEGACY_TEXT_FIELDS},
                **{
                    field: _load_canonical_json(
                        row[f"{field}_json"],
                        label=f"stored analysis {field}_json",
                        expected_type=(str, list),
                    )
                    for field in ANALYSIS_LEGACY_FLEXIBLE_FIELDS
                },
                "confidence": row["confidence"],
                "directional_stance": row["directional_stance"],
                "evidence_references": references,
            }
        )
        if scientific_sha256(output) != _strict_sha256(
            row["response_sha256"], "stored analysis response_sha256"
        ):
            raise PaperMemoryError("Stored analysis response hash does not match its typed output")
        return {
            "analysis_id": analysis_id,
            **output,
            "input_sha256": _strict_sha256(row["input_sha256"], "stored analysis input_sha256"),
            "response_sha256": _strict_sha256(
                row["response_sha256"], "stored analysis response_sha256"
            ),
            "scientific_sha256": _strict_sha256(
                row["scientific_sha256"], "stored analysis scientific_sha256"
            ),
        }

    def post_fill_stage_inputs(
        self,
        *,
        agent_id: str,
        event_id: str,
        turn: int,
        parent_ltb_id: str,
        stb_id: str,
        fill_id: str,
    ) -> dict[str, Any]:
        """Derive the only server-authoritative post-fill LTB input packet.

        Callers deliberately cannot provide their own transaction episode or
        price outcome mapping.  Both are reconstructed from the sealed ledger
        and frozen event schedule, which prevents a model-side or caller-side
        future-outcome/portfolio substitution.
        """
        turn = _strict_positive_int(turn, "post-fill input turn")
        scheduled = self._assert_scheduled_event(
            event_id,
            turn=turn,
            date=str(self.event_schedule.event(event_id)["date"]) if self.event_schedule else "",
        )
        with connect(self.db_path, read_only=True) as connection:
            self._assert_ltb_visible_for_agent(
                connection, parent_ltb_id, agent_id=agent_id, decision_turn=turn
            )
            self._assert_stb_for_event(
                connection, stb_id, agent_id=agent_id, event_id=event_id, turn=turn
            )
            self._assert_scoped_reference(connection, "paper_fill_ledger", "fill_id", fill_id)
            fill = connection.execute(
                """
                SELECT agent_id, event_id, turn, source_ltb_id, source_stb_id, decision_id
                FROM paper_fill_ledger WHERE fill_id = ?
                """,
                (fill_id,),
            ).fetchone()
            if fill is None or (
                str(fill["agent_id"]) != agent_id
                or str(fill["event_id"]) != event_id
                or int(fill["turn"]) != turn
                or str(fill["source_ltb_id"]) != parent_ltb_id
                or str(fill["source_stb_id"]) != stb_id
            ):
                raise PaperMemoryError("Post-fill stage IDs do not match the sealed current fill")
            parent = connection.execute(
                "SELECT dim_1, dim_2, dim_3, dim_4, dim_5, dim_6 FROM paper_ltb_states WHERE ltb_id = ?",
                (parent_ltb_id,),
            ).fetchone()
            stb = connection.execute(
                """
                SELECT dim_1, dim_2, dim_3, dim_4, dim_5, dim_6,
                       evidence_json, dimension_evidence_json
                FROM short_term_belief_history WHERE stb_id = ?
                """,
                (stb_id,),
            ).fetchone()
            if parent is None or stb is None:
                raise PaperMemoryError("Post-fill stage is missing its parent LTB or current STB")
            outcomes = connection.execute(
                """
                SELECT outcome.outcome_id, outcome.fill_id, outcome.horizon, outcome.mark_price,
                       outcome.observed_event_id, prior_fill.action AS entry_action,
                       prior_fill.executed_price AS entry_price, prior_fill.source_ltb_id,
                       prior_fill.source_stb_id
                FROM trade_outcomes AS outcome
                JOIN paper_fill_ledger AS prior_fill ON prior_fill.fill_id = outcome.fill_id
                WHERE prior_fill.run_id = ? AND prior_fill.condition_id = ?
                  AND prior_fill.agent_id = ?
                  AND outcome.status = 'matured' AND outcome.available_from_event_id = ?
                ORDER BY outcome.fill_id, outcome.horizon
                """,
                (self.run_id, self.condition_id, agent_id, event_id),
            ).fetchall()
            stb_evidence_rows = _load_canonical_json(
                stb["evidence_json"], label="stored STB evidence_json", expected_type=list
            )
            stb_dimension_evidence = _load_canonical_json(
                stb["dimension_evidence_json"],
                label="stored STB dimension_evidence_json",
                expected_type=dict,
            )
            visible_ids = self._evidence_ids(stb_evidence_rows)
            validated_dimension_evidence = normalize_dimension_evidence(
                stb_dimension_evidence,
                label="stored STB dimension_evidence",
                allowed_ids_by_dimension={key: set(visible_ids) for key in DIMENSION_KEYS},
            )
            sanitized_evidence = self._sanitized_stb_evidence_registry(stb_evidence_rows)
            eligible_outcomes: list[dict[str, Any]] = []
            for row in outcomes:
                entry_action = str(row["entry_action"])
                entry_price = _strict_positive_number(row["entry_price"], "outcome entry_price")
                mark_price = _strict_positive_number(row["mark_price"], "outcome mark_price")
                direction = 1.0 if entry_action == "buy" else -1.0
                eligible_outcomes.append(
                    {
                        "outcome_id": str(row["outcome_id"]),
                        "fill_id": str(row["fill_id"]),
                        "horizon": str(row["horizon"]),
                        "mark_price": mark_price,
                        "observed_event_id": str(row["observed_event_id"]),
                        "entry_action": entry_action,
                        "entry_price": entry_price,
                        "action_aligned_markout": direction * (mark_price - entry_price) / entry_price,
                        "source_ltb_id": str(row["source_ltb_id"]),
                        "source_stb_id": str(row["source_stb_id"]),
                    }
                )
            return {
                "event": {
                    "event_id": event_id,
                    "turn": turn,
                    "date": str(scheduled["date"]),
                    "subturn": str(scheduled["subturn"]),
                },
                "previous_ltb": {key: str(parent[key]) for key in DIMENSION_KEYS},
                "current_stb": {
                    "dimensions": {key: str(stb[key]) for key in DIMENSION_KEYS},
                    "dimension_evidence": validated_dimension_evidence,
                },
                "transaction_episode": self._transaction_episode(connection, fill_id),
                "eligible_price_outcomes_dim_6_only": eligible_outcomes,
                "sanitized_evidence_registry": sanitized_evidence,
            }

    def record_analysis(
        self,
        *,
        agent_id: str,
        event_id: str,
        turn: int,
        date: str,
        subturn: str,
        source_ltb_id: str,
        source_stb_id: str,
        analysis_packet: Mapping[str, Any],
        market_view: Any,
        valuation_view: Any,
        technical_view: Any,
        news_view: Any,
        portfolio_view: Any,
        key_risks: Any,
        opportunity: Any,
        caution: Any,
        directional_stance: Any,
        confidence: Any,
        evidence_references: Any,
        phase_call: PhaseCallConsumption | None = None,
    ) -> str:
        """Persist the typed analysis bridge between beliefs and a decision.

        Analysis is intentionally a separate stage.  It is validated against
        the same sealed LTB/STB/market/execution-state packet used by the
        decision, but it cannot introduce raw evidence or free-form historical
        references into that packet.
        """
        turn = _strict_positive_int(turn, "analysis turn")
        subturn = _required_text(subturn, "analysis subturn").lower()
        if subturn not in {"am", "pm"}:
            raise PaperMemoryError("Analysis subturn must be 'am' or 'pm'")
        self._assert_scheduled_event(event_id, turn=turn, date=date, subturn=subturn)
        response = normalize_analysis_response(
            {
                "market_view": market_view,
                "valuation_view": valuation_view,
                "technical_view": technical_view,
                "news_view": news_view,
                "portfolio_view": portfolio_view,
                "key_risks": key_risks,
                "opportunity": opportunity,
                "caution": caution,
                "confidence": confidence,
                "directional_stance": directional_stance,
                "evidence_references": evidence_references,
            }
        )
        response_sha256 = scientific_sha256(response)
        analysis_id = self._analysis_id(agent_id, event_id)
        with connect(self.db_path) as connection:
            self._assert_ltb_visible_for_agent(
                connection, source_ltb_id, agent_id=agent_id, decision_turn=turn
            )
            self._assert_stb_for_event(
                connection,
                source_stb_id,
                agent_id=agent_id,
                event_id=event_id,
                turn=turn,
            )
            input_sha256, _ = self._validate_sealed_decision_packet(
                connection,
                agent_id=agent_id,
                event_id=event_id,
                turn=turn,
                date=date,
                subturn=subturn,
                source_ltb_id=source_ltb_id,
                source_stb_id=source_stb_id,
                decision_packet=analysis_packet,
            )
            scientific = scientific_sha256(
                {
                    "run_id": self.run_id,
                    "condition_id": self.condition_id,
                    "agent_id": agent_id,
                    "event_id": event_id,
                    "turn": turn,
                    "date": date,
                    "subturn": subturn,
                    "source_ltb_id": source_ltb_id,
                    "source_stb_id": source_stb_id,
                    "input_sha256": input_sha256,
                    "response": response,
                }
            )
            self._insert_or_verify(
                connection,
                table="paper_analyses",
                key_column="analysis_id",
                key_value=analysis_id,
                values={
                    "analysis_id": analysis_id,
                    "run_id": self.run_id,
                    "condition_id": self.condition_id,
                    "manifest_sha256": self.manifest_sha256,
                    "agent_id": agent_id,
                    "event_id": event_id,
                    "turn": turn,
                    "date": date,
                    "subturn": subturn,
                    "source_ltb_id": source_ltb_id,
                    "source_stb_id": source_stb_id,
                    "input_sha256": input_sha256,
                    "response_sha256": response_sha256,
                    **{field: response[field] for field in ANALYSIS_LEGACY_TEXT_FIELDS},
                    **{
                        f"{field}_json": canonical_json(response[field])
                        for field in ANALYSIS_LEGACY_FLEXIBLE_FIELDS
                    },
                    "confidence": response["confidence"],
                    "directional_stance": response["directional_stance"],
                    "evidence_references_json": canonical_json(response["evidence_references"]),
                    "scientific_sha256": scientific,
                },
                compare_columns=("scientific_sha256", "manifest_sha256"),
            )
            self._consume_phase_call_in_transaction(
                connection,
                agent_id=agent_id,
                event_id=event_id,
                expected_stage="analysis",
                phase_call=phase_call,
            )
            connection.commit()
        return analysis_id

    def record_decision(
        self,
        *,
        agent_id: str,
        event_id: str,
        turn: int,
        date: str,
        subturn: str,
        action: str,
        requested_quantity: int,
        source_ltb_id: str,
        source_stb_id: str,
        analysis_id: str,
        decision_packet: Mapping[str, Any],
        decision_response: Mapping[str, Any],
        phase_call: PhaseCallConsumption | None = None,
    ) -> str:
        """Append the one sealed decision that may authorize a later fill.

        A paper fill is never allowed to invent an arbitrary decision ID.  The
        deterministic decision key binds the current STB and visible previous
        LTB to exact action/quantity/event fields before execution occurs.
        """
        turn = _strict_positive_int(turn, "decision turn")
        subturn = _required_text(subturn, "decision subturn").lower()
        if subturn not in {"am", "pm"}:
            raise PaperMemoryError("Decision subturn must be 'am' or 'pm'")
        action = _required_text(action, "decision action").lower()
        if action not in {"buy", "sell"}:
            raise PaperMemoryError("Paper decision action must be buy or sell")
        requested_quantity = _strict_positive_int(requested_quantity, "decision requested_quantity")
        analysis_id = _required_text(analysis_id, "analysis_id")
        response = normalize_decision_response(decision_response)
        if response["action"] != action or response["requested_quantity"] != requested_quantity:
            raise PaperMemoryError("Decision execution fields differ from the validated model response")
        response_sha256 = scientific_sha256(response)
        self._assert_scheduled_event(event_id, turn=turn, date=date, subturn=subturn)
        decision_id = self._decision_id(agent_id, event_id)
        with connect(self.db_path) as connection:
            self._assert_ltb_visible_for_agent(
                connection, source_ltb_id, agent_id=agent_id, decision_turn=turn
            )
            self._assert_stb_for_event(
                connection,
                source_stb_id,
                agent_id=agent_id,
                event_id=event_id,
                turn=turn,
            )
            self._assert_analysis_for_decision(
                connection,
                analysis_id=analysis_id,
                agent_id=agent_id,
                event_id=event_id,
                turn=turn,
                date=date,
                subturn=subturn,
                source_ltb_id=source_ltb_id,
                source_stb_id=source_stb_id,
            )
            input_sha256, execution_state = self._validate_sealed_decision_packet(
                connection,
                agent_id=agent_id,
                event_id=event_id,
                turn=turn,
                date=date,
                subturn=subturn,
                source_ltb_id=source_ltb_id,
                source_stb_id=source_stb_id,
                decision_packet=decision_packet,
            )
            if action not in execution_state["allowed_actions"]:
                raise PaperMemoryError("Decision action is not feasible under the sealed execution state")
            max_quantity = int(
                execution_state["max_buy_quantity"]
                if action == "buy"
                else execution_state["max_sell_quantity"]
            )
            if requested_quantity > max_quantity:
                raise PaperMemoryError("Decision quantity exceeds the sealed execution-state limit")
            scientific = scientific_sha256(
                {
                    "run_id": self.run_id,
                    "condition_id": self.condition_id,
                    "agent_id": agent_id,
                    "event_id": event_id,
                    "turn": turn,
                    "date": date,
                    "subturn": subturn,
                    "action": action,
                    "requested_quantity": requested_quantity,
                    "source_ltb_id": source_ltb_id,
                    "source_stb_id": source_stb_id,
                    "analysis_id": analysis_id,
                    "input_sha256": input_sha256,
                    "response_sha256": response_sha256,
                }
            )
            self._insert_or_verify(
                connection,
                table="paper_decisions",
                key_column="decision_id",
                key_value=decision_id,
                values={
                    "decision_id": decision_id,
                    "run_id": self.run_id,
                    "condition_id": self.condition_id,
                    "manifest_sha256": self.manifest_sha256,
                    "agent_id": agent_id,
                    "event_id": event_id,
                    "turn": turn,
                    "date": date,
                    "subturn": subturn,
                    "action": action,
                    "requested_quantity": requested_quantity,
                    "source_ltb_id": source_ltb_id,
                    "source_stb_id": source_stb_id,
                    "analysis_id": analysis_id,
                    "input_sha256": input_sha256,
                    "response_sha256": response_sha256,
                    "scientific_sha256": scientific,
                },
                compare_columns=("scientific_sha256", "manifest_sha256"),
            )
            self._consume_phase_call_in_transaction(
                connection,
                agent_id=agent_id,
                event_id=event_id,
                expected_stage="decision",
                phase_call=phase_call,
            )
            connection.commit()
        return decision_id

    def save_stb(
        self,
        *,
        agent_id: str,
        event_id: str,
        turn: int,
        date: str,
        dimensions: Mapping[str, Any],
        dimension_evidence: Mapping[str, Any],
        current_evidence: Mapping[str, Any],
        phase_call: PhaseCallConsumption | None = None,
    ) -> str:
        """Commit one STB from the complete sealed current-evidence packet.

        The old shape-only ``evidence`` + caller supplied hash API made it
        possible to bypass the news and community stage gates by writing a
        plausible-looking article/claim ID directly to SQLite.  This method is
        deliberately the persistence boundary: it reconstructs typed evidence
        from the sealed news registry and append-only community observations,
        then derives the request digest itself.
        """
        turn = _strict_positive_int(turn, "STB turn")
        scheduled = self._assert_scheduled_event(event_id, turn=turn, date=date)
        belief = self._normalize_persisted_belief(dimensions, label="stb")
        stb_id = self._stb_id(agent_id, event_id)
        with connect(self.db_path) as connection:
            evidence_rows, input_sha256 = self._sealed_stb_evidence(
                connection,
                agent_id=agent_id,
                event_id=event_id,
                scheduled=scheduled,
                current_evidence=current_evidence,
            )
            visible_ids = self._evidence_ids(evidence_rows)
            per_dimension_evidence = normalize_dimension_evidence(
                dimension_evidence,
                label="stb.dimension_evidence",
                allowed_ids_by_dimension={key: set(visible_ids) for key in DIMENSION_KEYS},
            )
            scientific = scientific_sha256(
                {
                    "run_id": self.run_id,
                    "condition_id": self.condition_id,
                    "agent_id": agent_id,
                    "event_id": event_id,
                    "turn": turn,
                    "dimensions": belief,
                    "evidence": evidence_rows,
                    "dimension_evidence": per_dimension_evidence,
                    "input_sha256": input_sha256,
                }
            )
            self._insert_or_verify(
                connection,
                table="short_term_belief_history",
                key_column="stb_id",
                key_value=stb_id,
                values={
                    "stb_id": stb_id,
                    "run_id": self.run_id,
                    "condition_id": self.condition_id,
                    "manifest_sha256": self.manifest_sha256,
                    "agent_id": agent_id,
                    "event_id": event_id,
                    "turn": turn,
                    "date": date,
                    **belief,
                    "evidence_json": canonical_json(evidence_rows),
                    "dimension_evidence_json": canonical_json(per_dimension_evidence),
                    "input_sha256": input_sha256,
                    "scientific_sha256": scientific,
                },
                compare_columns=("scientific_sha256", "manifest_sha256"),
            )
            self._write_stb_memory_edges(
                connection,
                agent_id=agent_id,
                event_id=event_id,
                stb_id=stb_id,
                evidence_rows=evidence_rows,
                dimension_evidence=per_dimension_evidence,
            )
            self._consume_phase_call_in_transaction(
                connection,
                agent_id=agent_id,
                event_id=event_id,
                expected_stage="stb",
                phase_call=phase_call,
            )
            connection.commit()
        return stb_id

    def record_fill(
        self,
        *,
        agent_id: str,
        event_id: str,
        turn: int,
        date: str,
        subturn: str,
        action: str,
        requested_quantity: int,
        source_ltb_id: str,
        source_stb_id: str,
        decision_id: str,
    ) -> str:
        if subturn not in {"am", "pm"}:
            raise PaperMemoryError("Fill subturn must be 'am' or 'pm'")
        if action not in {"buy", "sell"}:
            raise PaperMemoryError("Paper fill action must be buy or sell")
        requested_quantity = _strict_positive_int(requested_quantity, "requested_quantity")
        turn = _strict_positive_int(turn, "turn")
        scheduled = self._assert_scheduled_event(event_id, turn=turn, date=date, subturn=subturn)
        executed_price = float(scheduled["execution_price"])
        fill_id = self._fill_id(agent_id, event_id)
        with connect(self.db_path) as connection:
            self._assert_ltb_visible_for_agent(
                connection, source_ltb_id, agent_id=agent_id, decision_turn=int(turn)
            )
            self._assert_stb_for_event(
                connection,
                source_stb_id,
                agent_id=agent_id,
                event_id=event_id,
                turn=int(turn),
            )
            self._assert_decision_for_fill(
                connection,
                decision_id=decision_id,
                agent_id=agent_id,
                event_id=event_id,
                turn=turn,
                date=date,
                subturn=subturn,
                action=action,
                requested_quantity=requested_quantity,
                source_ltb_id=source_ltb_id,
                source_stb_id=source_stb_id,
            )
            pre_portfolio = self._portfolio_before_event(
                connection,
                agent_id=agent_id,
                event_id=event_id,
                turn=turn,
            )
            post_portfolio = self._apply_fill_to_portfolio(
                pre_portfolio,
                action=action,
                quantity=requested_quantity,
                price=executed_price,
            )
            pre_portfolio_json = canonical_json(pre_portfolio)
            post_portfolio_json = canonical_json(post_portfolio)
            scientific = scientific_sha256(
                {
                    "run_id": self.run_id,
                    "condition_id": self.condition_id,
                    "agent_id": agent_id,
                    "event_id": event_id,
                    "turn": turn,
                    "action": action,
                    "requested_quantity": requested_quantity,
                    "filled_quantity": requested_quantity,
                    "executed_price": executed_price,
                    "fee_amount": 0.0,
                    "source_ltb_id": source_ltb_id,
                    "source_stb_id": source_stb_id,
                    "decision_id": decision_id,
                    "pre_portfolio": pre_portfolio,
                    "post_portfolio": post_portfolio,
                }
            )
            self._insert_or_verify(
                connection,
                table="paper_fill_ledger",
                key_column="fill_id",
                key_value=fill_id,
                values={
                    "fill_id": fill_id,
                    "run_id": self.run_id,
                    "condition_id": self.condition_id,
                    "manifest_sha256": self.manifest_sha256,
                    "agent_id": agent_id,
                    "event_id": event_id,
                    "turn": turn,
                    "date": date,
                    "subturn": subturn,
                    "stock_code": self.stock_code,
                    "action": action,
                    "requested_quantity": requested_quantity,
                    "filled_quantity": requested_quantity,
                    "executed_price": executed_price,
                    "fee_amount": 0.0,
                    "source_ltb_id": source_ltb_id,
                    "source_stb_id": source_stb_id,
                    "decision_id": decision_id,
                    "pre_portfolio_json": pre_portfolio_json,
                    "post_portfolio_json": post_portfolio_json,
                    "scientific_sha256": scientific,
                },
                compare_columns=("scientific_sha256", "manifest_sha256"),
            )
            connection.commit()
        return fill_id

    def save_post_fill_ltb(
        self,
        *,
        agent_id: str,
        event_id: str,
        turn: int,
        date: str,
        parent_ltb_id: str,
        stb_id: str,
        fill_id: str,
        dimensions: Mapping[str, Any],
        integration_evidence_by_dimension: Mapping[str, Any],
        phase_call: PhaseCallConsumption | None = None,
    ) -> str:
        """Commit one recursive LTB with server-validated dimension evidence.

        The caller supplies only the model's six proposed texts and its
        support/contradict choices.  Parent belief, current STB provenance,
        current fill episode, due outcomes, and the human log are rebuilt by
        the store; none of them are caller authority.
        """
        turn = _strict_positive_int(turn, "LTB turn")
        self._assert_scheduled_event(event_id, turn=turn, date=date)
        belief = self._normalize_persisted_belief(dimensions, label="ltb")
        ltb_id = self._ltb_id(agent_id, event_id)
        transition_id = self._transition_id(agent_id, event_id)
        with connect(self.db_path) as connection:
            self._assert_ltb_visible_for_agent(
                connection, parent_ltb_id, agent_id=agent_id, decision_turn=turn
            )
            parent = connection.execute(
                "SELECT dim_1, dim_2, dim_3, dim_4, dim_5, dim_6 FROM paper_ltb_states WHERE ltb_id = ?",
                (parent_ltb_id,),
            ).fetchone()
            if parent is None:
                raise PaperMemoryError("Missing parent LTB after visibility validation")
            unchanged = [key for key in DIMENSION_KEYS if str(parent[key]) == belief[key]]
            if unchanged:
                raise PaperMemoryError(
                    "Post-fill LTB must rewrite all six dimensions; unchanged=" + ",".join(unchanged)
                )
            self._assert_stb_for_event(
                connection, stb_id, agent_id=agent_id, event_id=event_id, turn=turn
            )
            self._assert_scoped_reference(connection, "paper_fill_ledger", "fill_id", fill_id)
            fill_row = connection.execute(
                "SELECT agent_id, event_id, turn FROM paper_fill_ledger WHERE fill_id = ?", (fill_id,)
            ).fetchone()
            if fill_row is None or (
                str(fill_row["agent_id"]) != agent_id
                or str(fill_row["event_id"]) != event_id
                or int(fill_row["turn"]) != int(turn)
            ):
                raise PaperMemoryError("LTB current fill does not match its agent/event/turn")
            transaction_episode = self._transaction_episode(connection, fill_id)
            dimension_evidence, evidence, outcome_consumptions = self._validate_ltb_dimension_evidence(
                connection,
                integration_evidence_by_dimension=integration_evidence_by_dimension,
                agent_id=agent_id,
                event_id=event_id,
                stb_id=stb_id,
            )
            human_log = self._render_human_log(
                parent={key: str(parent[key]) for key in DIMENSION_KEYS},
                current=belief,
                integration_evidence_by_dimension=dimension_evidence,
            )
            human_columns = _human_log_state_columns(human_log)
            scientific = scientific_sha256(
                {
                    "run_id": self.run_id,
                    "condition_id": self.condition_id,
                    "agent_id": agent_id,
                    "event_id": event_id,
                    "turn": int(turn),
                    "parent_ltb_id": parent_ltb_id,
                    "stb_id": stb_id,
                    "fill_id": fill_id,
                    "dimensions": belief,
                    "transaction_episode": transaction_episode,
                    "integration_evidence": evidence,
                    "integration_evidence_by_dimension": dimension_evidence,
                }
            )
            self._insert_or_verify(
                connection,
                table="paper_ltb_states",
                key_column="ltb_id",
                key_value=ltb_id,
                values={
                    "ltb_id": ltb_id,
                    "run_id": self.run_id,
                    "condition_id": self.condition_id,
                    "manifest_sha256": self.manifest_sha256,
                    "agent_id": agent_id,
                    "event_id": event_id,
                    "turn": turn,
                    "visible_from_turn": int(turn) + 1,
                    "date": date,
                    "parent_ltb_id": parent_ltb_id,
                    "current_stb_id": stb_id,
                    **belief,
                    "scientific_sha256": scientific,
                    **human_columns,
                },
                compare_columns=(
                    "scientific_sha256",
                    "manifest_sha256",
                    "belief_summary",
                    "view_change_json",
                    "human_log_renderer_version",
                    "human_log_sha256",
                    "human_log_renderer_sha256",
                ),
            )
            self._insert_or_verify(
                connection,
                table="ltb_dimension_transitions",
                key_column="transition_id",
                key_value=transition_id,
                values={
                    "transition_id": transition_id,
                    "run_id": self.run_id,
                    "condition_id": self.condition_id,
                    "agent_id": agent_id,
                    "event_id": event_id,
                    "turn": turn,
                    "parent_ltb_id": parent_ltb_id,
                    "stb_id": stb_id,
                    "ltb_id": ltb_id,
                    "fill_id": fill_id,
                    "transaction_episode_json": canonical_json(transaction_episode),
                    "integration_evidence_json": canonical_json(evidence),
                    "integration_evidence_by_dimension_json": canonical_json(dimension_evidence),
                },
                compare_columns=(
                    "ltb_id",
                    "transaction_episode_json",
                    "integration_evidence_json",
                    "integration_evidence_by_dimension_json",
                ),
            )
            for outcome_fill_id, outcome_horizon in outcome_consumptions:
                self._consume_matured_outcome_in_transaction(
                    connection,
                    fill_id=outcome_fill_id,
                    horizon=outcome_horizon,
                    transition_id=transition_id,
                    consumed_at_event_id=event_id,
                )
            self._write_ltb_memory_edges(
                connection,
                agent_id=agent_id,
                event_id=event_id,
                ltb_id=ltb_id,
                parent_ltb_id=parent_ltb_id,
                stb_id=stb_id,
                decision_id=str(transaction_episode["decision_id"]),
                fill_id=fill_id,
                integration_evidence_by_dimension=dimension_evidence,
                flat_integration_evidence=evidence,
            )
            self._consume_phase_call_in_transaction(
                connection,
                agent_id=agent_id,
                event_id=event_id,
                expected_stage="post_fill_ltb",
                phase_call=phase_call,
            )
            connection.commit()
        return ltb_id

    def record_outcome(
        self,
        *,
        fill_id: str,
        horizon: str,
        available_from_event_id: str | None,
        observed_event_id: str | None,
        mark_price: float | None,
        status: str,
    ) -> str:
        if self.event_schedule is None:
            raise PaperMemoryError(
                "A frozen EventSchedule is required before recording price outcomes"
            )
        if horizon not in {"next_turn", "h1", "h5"}:
            raise PaperMemoryError("Unknown outcome horizon")
        if status == "pending":
            raise PaperMemoryError(
                "Pending belongs only in the immutable fill episode; trade_outcomes records final facts"
            )
        if status not in {"matured", "right_censored"}:
            raise PaperMemoryError("Unknown outcome status")
        expected_due_event = self._due_event_for_fill(fill_id, horizon)
        if status == "matured" and expected_due_event is None:
            raise PaperMemoryError("A right-censored outcome cannot be recorded as matured")
        if status == "right_censored" and expected_due_event is not None:
            raise PaperMemoryError("An available scheduled outcome cannot be marked right_censored")
        if status == "matured" and (not observed_event_id or mark_price is None):
            raise PaperMemoryError("A matured outcome requires event and positive mark price")
        if status == "matured":
            mark_price = _strict_positive_number(mark_price, "outcome mark_price")
            if available_from_event_id != expected_due_event or observed_event_id != expected_due_event:
                raise PaperMemoryError("Matured outcome event does not match the frozen horizon schedule")
            due_event = self.event_schedule.event(expected_due_event)
            if abs(mark_price - float(due_event["execution_price"])) > 1e-6:
                raise PaperMemoryError("Outcome mark price differs from the sealed due-event price")
        if status != "matured" and mark_price is not None:
            raise PaperMemoryError("Only a matured outcome may contain a mark price")
        if status == "right_censored" and (available_from_event_id is not None or observed_event_id is not None):
            raise PaperMemoryError("Right-censored outcome must not name a future observation event")
        outcome_id = f"outcome:{fill_id}:{horizon}"
        scientific = scientific_sha256(
            {
                "fill_id": fill_id,
                "horizon": horizon,
                "available_from_event_id": available_from_event_id,
                "observed_event_id": observed_event_id,
                "mark_price": mark_price,
                "status": status,
            }
        )
        with connect(self.db_path) as connection:
            self._assert_scoped_reference(connection, "paper_fill_ledger", "fill_id", fill_id)
            self._insert_or_verify(
                connection,
                table="trade_outcomes",
                key_column="outcome_id",
                key_value=outcome_id,
                values={
                    "outcome_id": outcome_id,
                    "fill_id": fill_id,
                    "horizon": horizon,
                    "available_from_event_id": available_from_event_id,
                    "observed_event_id": observed_event_id,
                    "mark_price": mark_price,
                    "status": status,
                    "scientific_sha256": scientific,
                },
                compare_columns=("scientific_sha256",),
            )
            connection.commit()
        return outcome_id

    def mature_outcomes_for_event(self, *, event_id: str) -> tuple[str, ...]:
        """Append every earlier outcome whose frozen horizon matures now.

        This is deterministic server work performed before the current event's
        STB phase.  It never receives a caller-supplied mark price and is safe
        to replay because :meth:`record_outcome` is insert-or-identical.
        """
        if self.event_schedule is None:
            raise PaperMemoryError("Frozen EventSchedule is required to mature outcomes")
        event = self.event_schedule.event(event_id)
        with connect(self.db_path, read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT fill_id FROM paper_fill_ledger
                WHERE run_id = ? AND condition_id = ?
                ORDER BY turn, agent_id
                """,
                (self.run_id, self.condition_id),
            ).fetchall()
        outcome_ids: list[str] = []
        for row in rows:
            fill_id = str(row["fill_id"])
            for horizon in ("next_turn", "h1", "h5"):
                if self._due_event_for_fill(fill_id, horizon) != event_id:
                    continue
                outcome_ids.append(
                    self.record_outcome(
                        fill_id=fill_id,
                        horizon=horizon,
                        available_from_event_id=event_id,
                        observed_event_id=event_id,
                        mark_price=float(event["execution_price"]),
                        status="matured",
                    )
                )
        return tuple(outcome_ids)

    def right_censor_unavailable_outcomes(self) -> tuple[str, ...]:
        """Close only horizons that never have a future event in the schedule."""
        if self.event_schedule is None:
            raise PaperMemoryError("Frozen EventSchedule is required to right-censor outcomes")
        with connect(self.db_path, read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT fill_id FROM paper_fill_ledger
                WHERE run_id = ? AND condition_id = ?
                ORDER BY turn, agent_id
                """,
                (self.run_id, self.condition_id),
            ).fetchall()
        outcome_ids: list[str] = []
        for row in rows:
            fill_id = str(row["fill_id"])
            for horizon in ("next_turn", "h1", "h5"):
                if self._due_event_for_fill(fill_id, horizon) is not None:
                    continue
                outcome_ids.append(
                    self.record_outcome(
                        fill_id=fill_id,
                        horizon=horizon,
                        available_from_event_id=None,
                        observed_event_id=None,
                        mark_price=None,
                        status="right_censored",
                    )
                )
        return tuple(outcome_ids)

    def outcome_status_counts(self) -> dict[str, int]:
        """Return a read-only terminal-outcome summary for progress reporting.

        A resumed runner must never call a write method merely to calculate its
        progress.  This small query keeps ``RNRunProgress`` observational and
        makes a second ``run_all()`` byte-for-byte idempotent after final
        censoring has already committed.
        """

        with connect(self.db_path, read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT outcome.status, COUNT(*) AS count
                FROM trade_outcomes AS outcome
                JOIN paper_fill_ledger AS fill ON fill.fill_id = outcome.fill_id
                WHERE fill.run_id = ? AND fill.condition_id = ?
                GROUP BY outcome.status
                """,
                (self.run_id, self.condition_id),
            ).fetchall()
        result = {"matured": 0, "right_censored": 0}
        for row in rows:
            status = str(row["status"])
            if status not in result:
                raise PaperMemoryError(f"Unexpected terminal outcome status: {status}")
            result[status] = int(row["count"])
        return result

    def consume_matured_outcome(
        self,
        *,
        fill_id: str,
        horizon: str,
        transition_id: str,
        consumed_at_event_id: str,
    ) -> str:
        with connect(self.db_path) as connection:
            consumption_id = self._consume_matured_outcome_in_transaction(
                connection,
                fill_id=fill_id,
                horizon=horizon,
                transition_id=transition_id,
                consumed_at_event_id=consumed_at_event_id,
            )
            connection.commit()
        return consumption_id

    def write_turn_trace(
        self,
        *,
        agent_id: str,
        event_id: str,
        turn: int,
        previous_ltb_id: str,
        stb_id: str,
        decision_id: str,
        fill_id: str,
        ltb_id: str,
        input_sha256: str,
        human_log: Mapping[str, Any],
    ) -> str:
        turn = _strict_positive_int(turn, "trace turn")
        scheduled = self.event_schedule.event(event_id) if self.event_schedule is not None else None
        if scheduled is None:
            raise PaperMemoryError("A frozen EventSchedule is required before writing a turn trace")
        self._assert_scheduled_event(event_id, turn=turn, date=str(scheduled["date"]))
        input_sha256 = _strict_sha256(input_sha256, "trace input_sha256")
        if not isinstance(human_log, Mapping):
            raise PaperMemoryError("Trace human_log must be a structured mapping")
        supplied_human_log = normalize_human_log(
            human_log, label="trace supplied human log"
        )
        trace_id = f"trace:{self.run_id}:{self.condition_id}:{agent_id}:{event_id}"
        scientific = scientific_sha256(
            {
                "previous_ltb_id": previous_ltb_id,
                "stb_id": stb_id,
                "decision_id": decision_id,
                "fill_id": fill_id,
                "ltb_id": ltb_id,
                "input_sha256": input_sha256,
            }
        )
        with connect(self.db_path) as connection:
            self._assert_exact_trace_lineage(
                connection,
                agent_id=agent_id,
                event_id=event_id,
                turn=turn,
                previous_ltb_id=previous_ltb_id,
                stb_id=stb_id,
                decision_id=decision_id,
                fill_id=fill_id,
                ltb_id=ltb_id,
                input_sha256=input_sha256,
            )
            authoritative_human_log = self._reconstruct_human_log(
                connection, ltb_id=ltb_id
            )
            if canonical_json(supplied_human_log) != canonical_json(
                authoritative_human_log
            ):
                raise PaperMemoryError(
                    "Trace human log differs from the server-rendered LTB human log"
            )
            human_log_json = canonical_json(authoritative_human_log)
            human_log_digest = human_log_sha256(authoritative_human_log)
            self._insert_or_verify(
                connection,
                table="turn_belief_trace",
                key_column="trace_id",
                key_value=trace_id,
                values={
                    "trace_id": trace_id,
                    "run_id": self.run_id,
                    "condition_id": self.condition_id,
                    "agent_id": agent_id,
                    "event_id": event_id,
                    "turn": int(turn),
                    "previous_ltb_id": previous_ltb_id,
                    "stb_id": stb_id,
                    "decision_id": decision_id,
                    "fill_id": fill_id,
                    "ltb_id": ltb_id,
                    "input_sha256": input_sha256,
                    "scientific_sha256": scientific,
                    "human_log_json": human_log_json,
                    "human_log_renderer_version": HUMAN_LOG_RENDERER_VERSION,
                    "human_log_renderer_sha256": HUMAN_LOG_RENDERER_CODE_SHA256,
                    "human_log_sha256": human_log_digest,
                },
                compare_columns=(
                    "scientific_sha256",
                    "human_log_json",
                    "human_log_renderer_version",
                    "human_log_renderer_sha256",
                    "human_log_sha256",
                ),
            )
            connection.commit()
        return trace_id

    def write_completed_turn_trace(self, *, agent_id: str, event_id: str) -> str:
        """Derive and write one complete turn trace from committed RN state.

        Callers deliberately provide no lineage IDs, input digest, or human-log
        payload.  Those values are all reconstructed from the immutable rows
        already committed by the STB → analysis → decision → fill → LTB path.
        This closes the otherwise easy-to-miss trace edge without making the
        stage adapter a second authority over scientific lineage.
        """

        if self.event_schedule is None:
            raise PaperMemoryError("A frozen EventSchedule is required before writing a turn trace")
        scheduled = self.event_schedule.event(event_id)
        turn = _strict_positive_int(scheduled["turn"], "trace event turn")
        stb_id = self._stb_id(agent_id, event_id)
        decision_id = self._decision_id(agent_id, event_id)
        fill_id = self._fill_id(agent_id, event_id)
        ltb_id = self._ltb_id(agent_id, event_id)
        transition_id = self._transition_id(agent_id, event_id)
        with connect(self.db_path, read_only=True) as connection:
            decision = connection.execute(
                """
                SELECT source_ltb_id, source_stb_id, input_sha256
                FROM paper_decisions WHERE decision_id = ?
                """,
                (decision_id,),
            ).fetchone()
            ltb = connection.execute(
                """
                SELECT dim_1, dim_2, dim_3, dim_4, dim_5, dim_6
                FROM paper_ltb_states WHERE ltb_id = ?
                """,
                (ltb_id,),
            ).fetchone()
            transition = connection.execute(
                """
                SELECT integration_evidence_by_dimension_json
                FROM ltb_dimension_transitions WHERE transition_id = ?
                """,
                (transition_id,),
            ).fetchone()
            if decision is None or ltb is None or transition is None:
                raise PaperMemoryError("Cannot trace an incomplete committed RN turn")
            previous_ltb_id = _required_text(decision["source_ltb_id"], "stored trace previous_ltb_id")
            if _required_text(decision["source_stb_id"], "stored trace stb_id") != stb_id:
                raise PaperMemoryError("Committed decision STB differs from deterministic turn STB")
            parent = connection.execute(
                """
                SELECT dim_1, dim_2, dim_3, dim_4, dim_5, dim_6
                FROM paper_ltb_states WHERE ltb_id = ?
                """,
                (previous_ltb_id,),
            ).fetchone()
            if parent is None:
                raise PaperMemoryError("Committed decision parent LTB is missing")
            evidence = _load_canonical_json(
                transition["integration_evidence_by_dimension_json"],
                label="stored trace integration evidence",
                expected_type=dict,
            )
            normalized_evidence = normalize_dimension_evidence(
                evidence,
                label="stored trace integration evidence",
                allowed_ids_by_dimension={
                    dimension: set(
                        evidence.get(dimension, {}).get("support", [])
                    )
                    | set(evidence.get(dimension, {}).get("contradict", []))
                    for dimension in DIMENSION_KEYS
                },
            )
            human_log = self._render_human_log(
                parent={dimension: str(parent[dimension]) for dimension in DIMENSION_KEYS},
                current={dimension: str(ltb[dimension]) for dimension in DIMENSION_KEYS},
                integration_evidence_by_dimension=normalized_evidence,
            )
            input_sha256 = _strict_sha256(decision["input_sha256"], "stored trace input_sha256")
        return self.write_turn_trace(
            agent_id=agent_id,
            event_id=event_id,
            turn=turn,
            previous_ltb_id=previous_ltb_id,
            stb_id=stb_id,
            decision_id=decision_id,
            fill_id=fill_id,
            ltb_id=ltb_id,
            input_sha256=input_sha256,
            human_log=human_log,
        )

    def assert_complete_lineage(
        self,
        expected_keys: Iterable[tuple[str, str]],
        *,
        require_finalized_outcomes: bool = True,
    ) -> dict[str, int]:
        """Require complete decision/fill/memory lineage and exact outcome use.

        ``require_finalized_outcomes=False`` is only for an interrupted live
        phase checkpoint.  A final evaluator/report must use the default and
        therefore reject pending/missing horizon records.
        """
        expected = {(str(agent_id), str(event_id)) for agent_id, event_id in expected_keys}
        if not expected:
            raise PaperMemoryError("Expected lineage key set must not be empty")
        with connect(self.db_path, read_only=True) as connection:
            observed: dict[str, set[tuple[str, str]]] = {}
            for table in (
                "short_term_belief_history",
                "paper_analyses",
                "paper_decisions",
                "paper_fill_ledger",
                "ltb_dimension_transitions",
                "turn_belief_trace",
            ):
                rows = connection.execute(
                    f"SELECT agent_id, event_id FROM {table} WHERE run_id = ? AND condition_id = ?",
                    (self.run_id, self.condition_id),
                ).fetchall()
                keys = {(str(row["agent_id"]), str(row["event_id"])) for row in rows}
                if len(rows) != len(keys):
                    raise PaperMemoryError(f"Duplicate scientific keys found in {table}")
                observed[table] = keys
            outcome_summary = self._assert_outcome_lineage(
                connection,
                require_finalized=require_finalized_outcomes,
            )
            memory_edge_summary = self._assert_memory_evidence_lineage(
                connection,
                expected=expected,
            )
            phase_call_summary = self._assert_phase_call_lineage(
                connection,
                expected=expected,
            )
            post_trace_summary = self._assert_community_post_trace_lineage(
                connection
            )
            self._assert_human_log_lineage(
                connection,
                expected=expected,
            )
            broken_analysis = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM paper_decisions AS decision
                LEFT JOIN paper_analyses AS analysis ON analysis.analysis_id = decision.analysis_id
                WHERE decision.run_id = ? AND decision.condition_id = ?
                  AND (
                    analysis.analysis_id IS NULL
                    OR analysis.run_id != decision.run_id
                    OR analysis.condition_id != decision.condition_id
                    OR analysis.agent_id != decision.agent_id
                    OR analysis.event_id != decision.event_id
                    OR analysis.turn != decision.turn
                    OR analysis.source_ltb_id != decision.source_ltb_id
                    OR analysis.source_stb_id != decision.source_stb_id
                  )
                """,
                (self.run_id, self.condition_id),
            ).fetchone()
            if broken_analysis is None or int(broken_analysis["count"]) != 0:
                raise PaperMemoryError("Decision-to-analysis lineage is incomplete or cross-scoped")
        for table, keys in observed.items():
            if keys != expected:
                raise PaperMemoryError(
                    f"{table} key-set mismatch missing={sorted(expected - keys)[:3]} "
                    f"extra={sorted(keys - expected)[:3]}"
                )
        return {
            "expected_keys": len(expected),
            **{table: len(keys) for table, keys in observed.items()},
            **memory_edge_summary,
            **phase_call_summary,
            **post_trace_summary,
            **outcome_summary,
        }

    def _assert_human_log_lineage(
        self,
        connection: Any,
        *,
        expected: set[tuple[str, str]],
    ) -> dict[str, int]:
        expected_ltb_ids = {
            self._ltb_id(agent_id, event_id) for agent_id, event_id in expected
        } | {
            self._ltb_id(agent_id, "initial")
            for agent_id in {agent_id for agent_id, _ in expected}
        }
        rows = connection.execute(
            """
            SELECT ltb_id FROM paper_ltb_states
            WHERE run_id = ? AND condition_id = ?
            """,
            (self.run_id, self.condition_id),
        ).fetchall()
        observed_ltb_ids = {str(row["ltb_id"]) for row in rows}
        if observed_ltb_ids != expected_ltb_ids:
            raise PaperMemoryError(
                "LTB human-log key-set mismatch "
                f"missing={sorted(expected_ltb_ids - observed_ltb_ids)[:3]} "
                f"extra={sorted(observed_ltb_ids - expected_ltb_ids)[:3]}"
            )
        for ltb_id in sorted(observed_ltb_ids):
            self._reconstruct_human_log(connection, ltb_id=ltb_id)

        trace_rows = connection.execute(
            """
            SELECT trace_id, ltb_id, human_log_json,
                   human_log_renderer_version, human_log_renderer_sha256,
                   human_log_sha256
            FROM turn_belief_trace
            WHERE run_id = ? AND condition_id = ?
            """,
            (self.run_id, self.condition_id),
        ).fetchall()
        for row in trace_rows:
            stored = normalize_human_log(
                _load_canonical_json(
                    row["human_log_json"],
                    label="lineage trace human_log_json",
                    expected_type=dict,
                ),
                label="lineage trace human log",
            )
            authoritative = self._reconstruct_human_log(
                connection, ltb_id=_required_text(row["ltb_id"], "lineage trace ltb_id")
            )
            if canonical_json(stored) != canonical_json(authoritative):
                raise PaperMemoryError(
                    "Turn trace human log differs from its deterministic LTB log"
                )
            if (
                row["human_log_renderer_version"] != HUMAN_LOG_RENDERER_VERSION
                or _strict_sha256(
                    row["human_log_renderer_sha256"],
                    "lineage trace renderer hash",
                )
                != HUMAN_LOG_RENDERER_CODE_SHA256
                or _strict_sha256(
                    row["human_log_sha256"], "lineage trace human-log hash"
                )
                != human_log_sha256(authoritative)
            ):
                raise PaperMemoryError(
                    "Turn trace human-log renderer identity or digest is invalid"
                )
        return {
            "ltb_human_logs_verified": len(observed_ltb_ids),
            "trace_human_logs_verified": len(trace_rows),
        }

    def phase_consumption_digests(self) -> dict[str, str]:
        """Return logical-response IDs and hashes consumed by this condition."""

        with connect(self.db_path, read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT source_kind, source_id, payload_sha256
                FROM phase_consumptions
                WHERE run_id = ? AND condition_id = ?
                ORDER BY source_id
                """,
                (self.run_id, self.condition_id),
            ).fetchall()
        consumed: dict[str, str] = {}
        for row in rows:
            if str(row["source_kind"]) != "logical_response":
                raise PaperMemoryError(
                    "Phase-call consumption has an unapproved source kind"
                )
            logical_call_id = _required_text(
                row["source_id"], "phase-call logical response ID"
            )
            response_sha256 = _strict_sha256(
                row["payload_sha256"], "phase-call response digest"
            )
            if logical_call_id in consumed:
                raise PaperMemoryError(
                    f"Duplicate phase-call logical response ID: {logical_call_id}"
                )
            consumed[logical_call_id] = response_sha256
        return consumed

    def record_auxiliary_phase_calls(
        self,
        calls: Iterable[PhaseCallConsumption],
    ) -> tuple[str, ...]:
        """Bind conditional community responses to their committed phase state.

        Core STB/analysis/decision/LTB calls are written inside their artifact
        transactions.  Conditional community calls have no one-row model
        artifact, so the lifecycle records their journal digest here after
        the corresponding post/exposure/claim batch succeeds and before the
        outer paired coordinator commits the journal.
        """

        typed = tuple(calls)
        if any(not isinstance(item, PhaseCallConsumption) for item in typed):
            raise PaperMemoryError("Auxiliary phase calls must be typed consumptions")
        ids: list[str] = []
        with connect(self.db_path) as connection:
            for item in typed:
                if item.stage not in RN_AUXILIARY_STAGE_SCHEMA_VERSIONS:
                    raise PaperMemoryError("Auxiliary phase call has an unapproved stage")
                components = item.logical_call_id.split("|")
                if len(components) != 6:
                    raise PaperMemoryError("Auxiliary phase logical ID has invalid components")
                run_id, condition_id, agent_id, event_id, stage, schema_version = components
                if (
                    run_id != self.run_id
                    or condition_id != self.condition_id
                    or stage != item.stage
                    or schema_version != RN_AUXILIARY_STAGE_SCHEMA_VERSIONS[item.stage]
                ):
                    raise PaperMemoryError("Auxiliary phase logical ID is outside this store")
                ids.append(
                    self._consume_phase_call_in_transaction(
                        connection,
                        agent_id=agent_id,
                        event_id=event_id,
                        expected_stage=item.stage,
                        phase_call=item,
                    )
                    or ""
                )
            connection.commit()
        if any(not value for value in ids) or len(ids) != len(set(ids)):
            raise PaperMemoryError("Auxiliary phase consumption IDs are invalid")
        return tuple(ids)

    def export_final_fill_ledger(self, path: Path | str) -> Path:
        """Export the raw database ledger for human forensic inspection.

        This preserves the database field names intentionally.  The paper
        evaluator consumes :meth:`export_canonical_final_fill_ledger` below,
        whose explicit projection distinguishes the resolved-run hash stored
        in SQLite from the evaluator-contract hash used to bind final CSVs.
        """
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with connect(self.db_path, read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT * FROM paper_fill_ledger
                WHERE run_id = ? AND condition_id = ?
                ORDER BY turn, agent_id
                """,
                (self.run_id, self.condition_id),
            ).fetchall()
        if not rows:
            raise PaperMemoryError("Cannot export an empty final fill ledger")
        fieldnames = list(rows[0].keys())
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(dict(row) for row in rows)
        temporary.replace(destination)
        return destination

    def export_canonical_final_fill_ledger(
        self,
        path: Path | str,
        *,
        evaluator_contract_sha256: str,
    ) -> Path:
        """Project immutable fills into the evaluator's reviewable CSV schema.

        ``paper_fill_ledger.manifest_sha256`` is the authoritative *resolved
        study* hash.  The evaluator, however, binds a final ledger to its own
        price-bearing contract envelope.  Both identities are retained under
        non-ambiguous names: ``manifest_hash`` is the evaluator contract for
        validator binding, while ``source_resolved_manifest_sha256`` preserves
        the source SQLite identity for reviewers.
        """
        evaluator_hash = _strict_sha256(
            evaluator_contract_sha256, "evaluator_contract_sha256"
        )
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with connect(self.db_path, read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT * FROM paper_fill_ledger
                WHERE run_id = ? AND condition_id = ?
                ORDER BY turn, agent_id
                """,
                (self.run_id, self.condition_id),
            ).fetchall()
        if not rows:
            raise PaperMemoryError("Cannot export an empty final fill ledger")
        fieldnames = [
            "fill_id",
            "condition_id",
            "manifest_hash",
            "source_resolved_manifest_sha256",
            "run_id",
            "agent_id",
            "event_id",
            "date",
            "subturn",
            "stock_code",
            "action",
            "fill_status",
            "requested_quantity",
            "filled_quantity",
            "fill_price",
            "fee_amount",
            "scientific_sha256",
        ]
        projected: list[dict[str, Any]] = []
        for row in rows:
            stored_hash = _strict_sha256(row["manifest_sha256"], "stored manifest_sha256")
            if stored_hash != self.manifest_sha256:
                raise PaperMemoryError("Stored fill does not belong to this resolved study manifest")
            projected.append(
                {
                    "fill_id": str(row["fill_id"]),
                    "condition_id": str(row["condition_id"]),
                    "manifest_hash": evaluator_hash,
                    "source_resolved_manifest_sha256": stored_hash,
                    "run_id": str(row["run_id"]),
                    "agent_id": str(row["agent_id"]),
                    "event_id": str(row["event_id"]),
                    "date": str(row["date"]),
                    "subturn": str(row["subturn"]).upper(),
                    "stock_code": str(row["stock_code"]),
                    "action": str(row["action"]).upper(),
                    "fill_status": "filled",
                    "requested_quantity": int(row["requested_quantity"]),
                    "filled_quantity": int(row["filled_quantity"]),
                    "fill_price": _csv_number(row["executed_price"]),
                    "fee_amount": _csv_number(row["fee_amount"]),
                    "scientific_sha256": str(row["scientific_sha256"]),
                }
            )
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(projected)
        temporary.replace(destination)
        return destination

    def _assert_outcome_lineage(
        self,
        connection: Any,
        *,
        require_finalized: bool,
    ) -> dict[str, int]:
        if self.event_schedule is None:
            raise PaperMemoryError("Frozen EventSchedule is required for outcome-lineage validation")
        fills = connection.execute(
            """
            SELECT fill_id, agent_id, event_id
            FROM paper_fill_ledger
            WHERE run_id = ? AND condition_id = ?
            """,
            (self.run_id, self.condition_id),
        ).fetchall()
        outcomes = connection.execute(
            """
            SELECT outcome.*
            FROM trade_outcomes AS outcome
            JOIN paper_fill_ledger AS fill ON fill.fill_id = outcome.fill_id
            WHERE fill.run_id = ? AND fill.condition_id = ?
            """,
            (self.run_id, self.condition_id),
        ).fetchall()
        rows_by_fill: dict[str, dict[str, Any]] = {}
        for row in outcomes:
            fill_rows = rows_by_fill.setdefault(str(row["fill_id"]), {})
            horizon = str(row["horizon"])
            if horizon in fill_rows:
                raise PaperMemoryError("Duplicate outcome horizon found for one fill")
            fill_rows[horizon] = row
        consumption_rows = connection.execute(
            """
            SELECT consumption.*, transition.agent_id, transition.event_id AS transition_event_id,
                   transition.run_id, transition.condition_id
            FROM ltb_outcome_consumptions AS consumption
            JOIN paper_fill_ledger AS fill ON fill.fill_id = consumption.fill_id
            JOIN ltb_dimension_transitions AS transition
              ON transition.transition_id = consumption.transition_id
            WHERE fill.run_id = ? AND fill.condition_id = ?
            """,
            (self.run_id, self.condition_id),
        ).fetchall()
        consumptions_by_key: dict[tuple[str, str], list[Any]] = {}
        for row in consumption_rows:
            key = (str(row["fill_id"]), str(row["horizon"]))
            consumptions_by_key.setdefault(key, []).append(row)

        finalized_count = 0
        for fill in fills:
            fill_id = str(fill["fill_id"])
            fill_event_id = str(fill["event_id"])
            fill_agent_id = str(fill["agent_id"])
            by_horizon = rows_by_fill.get(fill_id, {})
            if require_finalized and set(by_horizon) != {"next_turn", "h1", "h5"}:
                raise PaperMemoryError("Every final fill must have exactly next_turn/H1/H5 outcome rows")
            for horizon, outcome in by_horizon.items():
                due_event_id = self.event_schedule.due_event_id(
                    fill_event_id=fill_event_id,
                    horizon=horizon,
                )
                status = str(outcome["status"])
                consumption = consumptions_by_key.get((fill_id, horizon), [])
                if require_finalized:
                    if due_event_id is None:
                        if status != "right_censored" or consumption:
                            raise PaperMemoryError("Right-censored outcome must have no consumption edge")
                    else:
                        if status != "matured":
                            raise PaperMemoryError("Scheduled final outcome must be matured, never pending")
                        if len(consumption) != 1:
                            raise PaperMemoryError("Every mature outcome must have exactly one consumption edge")
                        edge = consumption[0]
                        if (
                            str(edge["run_id"]) != self.run_id
                            or str(edge["condition_id"]) != self.condition_id
                            or str(edge["agent_id"]) != fill_agent_id
                            or str(edge["transition_event_id"]) != due_event_id
                            or str(edge["consumed_at_event_id"]) != due_event_id
                        ):
                            raise PaperMemoryError("Outcome consumption is not bound to its owner and due transition")
                    finalized_count += 1
                elif status != "matured" and consumption:
                    raise PaperMemoryError("Only mature outcomes may have a consumption edge")
        if require_finalized:
            expected_consumption_keys = {
                (str(row["fill_id"]), str(row["horizon"]))
                for row in outcomes
                if str(row["status"]) == "matured"
            }
            if set(consumptions_by_key) != expected_consumption_keys:
                raise PaperMemoryError("Outcome-consumption key set contains an omitted or extra edge")
        return {
            "fills": len(fills),
            "outcomes": len(outcomes),
            "outcome_consumptions": len(consumption_rows),
            "finalized_outcomes": finalized_count,
        }

    def _assert_memory_evidence_lineage(
        self,
        connection: Any,
        *,
        expected: set[tuple[str, str]],
    ) -> dict[str, int]:
        """Rebuild and compare every current-evidence/STB/LTB causal edge."""

        expected_edges: set[tuple[str, str, str, str, str, str, str | None]] = set()
        outcome_rows = connection.execute(
            """
            SELECT outcome.outcome_id, outcome.status, outcome.available_from_event_id,
                   fill.agent_id
            FROM trade_outcomes AS outcome
            JOIN paper_fill_ledger AS fill ON fill.fill_id = outcome.fill_id
            WHERE fill.run_id = ? AND fill.condition_id = ?
            """,
            (self.run_id, self.condition_id),
        ).fetchall()
        outcome_by_id = {str(row["outcome_id"]): row for row in outcome_rows}

        def add(
            agent_id: str,
            event_id: str,
            target_kind: str,
            target_id: str,
            source_kind: str,
            source_id: str,
            dimension: str | None,
        ) -> None:
            expected_edges.add(
                (
                    agent_id,
                    event_id,
                    target_kind,
                    target_id,
                    source_kind,
                    source_id,
                    dimension,
                )
            )

        for agent_id, event_id in sorted(expected):
            stb = connection.execute(
                """
                SELECT stb_id, evidence_json, dimension_evidence_json
                FROM short_term_belief_history
                WHERE run_id = ? AND condition_id = ? AND agent_id = ? AND event_id = ?
                """,
                (self.run_id, self.condition_id, agent_id, event_id),
            ).fetchone()
            transition = connection.execute(
                """
                SELECT transition.ltb_id, transition.parent_ltb_id, transition.stb_id,
                       transition.fill_id, transition.integration_evidence_by_dimension_json,
                       fill.decision_id
                FROM ltb_dimension_transitions AS transition
                JOIN paper_fill_ledger AS fill ON fill.fill_id = transition.fill_id
                WHERE transition.run_id = ? AND transition.condition_id = ?
                  AND transition.agent_id = ? AND transition.event_id = ?
                """,
                (self.run_id, self.condition_id, agent_id, event_id),
            ).fetchone()
            if stb is None or transition is None:
                raise PaperMemoryError("Memory evidence lineage is missing its STB or LTB transition")
            stb_id = str(stb["stb_id"])
            if str(transition["stb_id"]) != stb_id:
                raise PaperMemoryError("Memory evidence lineage crosses two different current STBs")
            evidence_rows = _load_canonical_json(
                stb["evidence_json"],
                label="lineage STB evidence",
                expected_type=list,
            )
            dimension_evidence = _load_canonical_json(
                stb["dimension_evidence_json"],
                label="lineage STB dimension evidence",
                expected_type=dict,
            )
            source_kind_by_id: dict[str, str] = {}
            for raw in evidence_rows:
                kind = _required_text(raw.get("kind"), "lineage STB source kind")
                if kind == "news":
                    source_id = _required_text(raw.get("article_id"), "lineage article ID")
                elif kind == "community_claim":
                    source_id = _required_text(raw.get("claim_id"), "lineage claim ID")
                else:
                    raise PaperMemoryError("Lineage STB has an unsupported evidence source")
                if source_id in source_kind_by_id:
                    raise PaperMemoryError("Lineage STB has duplicate evidence source IDs")
                source_kind_by_id[source_id] = kind
                add(agent_id, event_id, "stb", stb_id, kind, source_id, None)
            normalized_stb_evidence = normalize_dimension_evidence(
                dimension_evidence,
                label="lineage STB dimension evidence",
                allowed_ids_by_dimension={
                    dimension: set(source_kind_by_id) for dimension in DIMENSION_KEYS
                },
            )
            for dimension in DIMENSION_KEYS:
                for relation in ("support", "contradict"):
                    for source_id in normalized_stb_evidence[dimension][relation]:
                        add(
                            agent_id,
                            event_id,
                            "stb",
                            stb_id,
                            source_kind_by_id[source_id],
                            source_id,
                            dimension,
                        )

            ltb_id = str(transition["ltb_id"])
            for source_kind, source_id in (
                ("ltb", str(transition["parent_ltb_id"])),
                ("stb", stb_id),
                ("decision", str(transition["decision_id"])),
                ("fill", str(transition["fill_id"])),
            ):
                add(agent_id, event_id, "ltb", ltb_id, source_kind, source_id, None)
            integration = _load_canonical_json(
                transition["integration_evidence_by_dimension_json"],
                label="lineage LTB integration evidence",
                expected_type=dict,
            )
            allowed_by_dimension = {
                dimension: set(source_kind_by_id) for dimension in DIMENSION_KEYS
            }
            allowed_by_dimension["dim_6"] |= set(outcome_by_id)
            normalized_integration = normalize_dimension_evidence(
                integration,
                label="lineage LTB integration evidence",
                allowed_ids_by_dimension=allowed_by_dimension,
            )
            for dimension in DIMENSION_KEYS:
                for relation in ("support", "contradict"):
                    for source_id in normalized_integration[dimension][relation]:
                        if source_id in outcome_by_id:
                            outcome = outcome_by_id[source_id]
                            if (
                                str(outcome["agent_id"]) != agent_id
                                or str(outcome["status"]) != "matured"
                                or str(outcome["available_from_event_id"]) != event_id
                                or dimension != "dim_6"
                            ):
                                raise PaperMemoryError(
                                    "LTB evidence edge uses a foreign, immature, or non-due outcome"
                                )
                            source_kind = "trade_outcome"
                        else:
                            source_kind = source_kind_by_id[source_id]
                        add(
                            agent_id,
                            event_id,
                            "ltb",
                            ltb_id,
                            source_kind,
                            source_id,
                            dimension,
                        )

        rows = connection.execute(
            """
            SELECT edge_id, agent_id, event_id, target_kind, target_id,
                   source_kind, source_id, dimension
            FROM memory_evidence_edges
            WHERE run_id = ? AND condition_id = ?
            """,
            (self.run_id, self.condition_id),
        ).fetchall()
        actual_edges: set[tuple[str, str, str, str, str, str, str | None]] = set()
        for row in rows:
            dimension = None if row["dimension"] is None else str(row["dimension"])
            edge = (
                str(row["agent_id"]),
                str(row["event_id"]),
                str(row["target_kind"]),
                str(row["target_id"]),
                str(row["source_kind"]),
                str(row["source_id"]),
                dimension,
            )
            values = {
                "run_id": self.run_id,
                "condition_id": self.condition_id,
                "agent_id": edge[0],
                "event_id": edge[1],
                "target_kind": edge[2],
                "target_id": edge[3],
                "source_kind": edge[4],
                "source_id": edge[5],
                "dimension": edge[6],
            }
            if str(row["edge_id"]) != "edge:" + scientific_sha256(values)[:40]:
                raise PaperMemoryError("Memory evidence edge has a non-canonical ID")
            if edge in actual_edges:
                raise PaperMemoryError("Duplicate memory evidence edge found")
            actual_edges.add(edge)
        if actual_edges != expected_edges:
            raise PaperMemoryError(
                "Memory evidence edge set is incomplete or contains an unapproved edge"
            )
        return {"memory_evidence_edges": len(actual_edges)}

    def _assert_phase_call_lineage(
        self,
        connection: Any,
        *,
        expected: set[tuple[str, str]],
    ) -> dict[str, int]:
        """Require the four core calls plus valid conditional community calls."""

        rows = connection.execute(
            """
            SELECT consumption_id, agent_id, event_id, stage, source_kind,
                   source_id, payload_sha256
            FROM phase_consumptions
            WHERE run_id = ? AND condition_id = ?
            """,
            (self.run_id, self.condition_id),
        ).fetchall()
        observed: set[tuple[str, str, str]] = set()
        observed_auxiliary: set[tuple[str, str, str]] = set()
        for row in rows:
            agent_id = str(row["agent_id"])
            event_id = str(row["event_id"])
            stage = str(row["stage"])
            key = (agent_id, event_id)
            if key not in expected or stage not in RN_ALL_JOURNALED_STAGE_SCHEMA_VERSIONS:
                raise PaperMemoryError("Phase-call consumption is outside the expected turn set")
            is_auxiliary = stage in RN_AUXILIARY_STAGE_SCHEMA_VERSIONS
            if is_auxiliary and self.condition_id != "RN_COMM_ON":
                raise PaperMemoryError("Community phase-call consumption exists in RN_COMM_OFF")
            if str(row["source_kind"]) != "logical_response":
                raise PaperMemoryError("Phase-call consumption has an unapproved source kind")
            expected_logical_id = "|".join(
                (
                    self.run_id,
                    self.condition_id,
                    agent_id,
                    event_id,
                    stage,
                    RN_ALL_JOURNALED_STAGE_SCHEMA_VERSIONS[stage],
                )
            )
            if str(row["source_id"]) != expected_logical_id:
                raise PaperMemoryError("Phase-call consumption logical ID is not canonical")
            _strict_sha256(row["payload_sha256"], "stored phase-call response digest")
            identity = {
                "run_id": self.run_id,
                "condition_id": self.condition_id,
                "agent_id": agent_id,
                "event_id": event_id,
                "stage": stage,
                "source_kind": "logical_response",
                "source_id": expected_logical_id,
            }
            if str(row["consumption_id"]) != "phase-call:" + scientific_sha256(identity)[:40]:
                raise PaperMemoryError("Phase-call consumption has a non-canonical ID")
            observed_key = (agent_id, event_id, stage)
            target = observed_auxiliary if is_auxiliary else observed
            if observed_key in target:
                raise PaperMemoryError("Duplicate phase-call consumption found")
            target.add(observed_key)
            if is_auxiliary:
                if self.event_schedule is None:
                    raise PaperMemoryError("Auxiliary phase call requires EventSchedule")
                subturn = str(self.event_schedule.event(event_id)["subturn"])
                expected_subturn = (
                    "am" if stage == "community_interpretation" else "pm"
                )
                if subturn != expected_subturn:
                    raise PaperMemoryError(
                        "Community phase-call stage occurs in the wrong event subturn"
                    )
        expected_consumptions = {
            (agent_id, event_id, stage)
            for agent_id, event_id in expected
            for stage in RN_STAGE_SCHEMA_VERSIONS
        }
        if observed != expected_consumptions:
            raise PaperMemoryError(
                "Phase-call consumption set is incomplete or contains an extra stage"
            )
        # Keep the public lineage-summary shape stable.  The total includes
        # every accepted response consumed by the phase, while the internal
        # validation above still distinguishes mandatory core calls from
        # conditional community calls.
        return {"phase_consumptions": len(observed) + len(observed_auxiliary)}

    def _assert_community_post_trace_lineage(
        self,
        connection: Any,
    ) -> dict[str, int]:
        """Require an exact private trace for every consumed posting response."""

        consumption_rows = connection.execute(
            """
            SELECT source_id, payload_sha256
            FROM phase_consumptions
            WHERE run_id = ? AND condition_id = ? AND stage = 'community_posting'
            """,
            (self.run_id, self.condition_id),
        ).fetchall()
        consumed = {
            _required_text(row["source_id"], "posting trace consumed logical ID"):
            _strict_sha256(
                row["payload_sha256"],
                "posting trace consumed response hash",
            )
            for row in consumption_rows
        }
        if len(consumed) != len(consumption_rows):
            raise PaperMemoryError("Duplicate consumed community-posting response")

        rows = connection.execute(
            """
            SELECT *
            FROM community_post_trace
            WHERE run_id = ? AND condition_id = ?
            ORDER BY phase_id, author_agent_id
            """,
            (self.run_id, self.condition_id),
        ).fetchall()
        board_rows = connection.execute(
            """
            SELECT stage
            FROM observation_events
            WHERE run_id = ? AND condition_id = ?
              AND stage LIKE 'community_posts:%'
            """,
            (self.run_id, self.condition_id),
        ).fetchall()
        board_phases = {
            str(row["stage"]).removeprefix("community_posts:")
            for row in board_rows
        }
        if len(board_phases) != len(board_rows):
            raise PaperMemoryError("Duplicate community public-board phase")
        if self.condition_id != "RN_COMM_ON":
            if rows or consumed or board_rows:
                raise PaperMemoryError(
                    "RN_COMM_OFF contains a community board, posting trace, or response"
                )
            return {
                "community_post_traces": 0,
                "community_posts_traced": 0,
                "community_post_skips_traced": 0,
            }

        traced: dict[str, Any] = {}
        posted = 0
        skipped = 0
        phase_posts: dict[str, dict[str, Mapping[str, Any]]] = {}
        traced_posted_authors: dict[str, set[str]] = {}
        for row in rows:
            logical_call_id = _required_text(
                row["logical_call_id"],
                "community post trace logical_call_id",
            )
            if logical_call_id in traced:
                raise PaperMemoryError(
                    "Duplicate community post trace logical response"
                )
            components = logical_call_id.split("|")
            if len(components) != 6:
                raise PaperMemoryError(
                    "Community post trace logical-call ID is malformed"
                )
            (
                run_id,
                condition_id,
                agent_id,
                event_id,
                stage,
                schema_version,
            ) = components
            if (
                run_id != self.run_id
                or condition_id != self.condition_id
                or agent_id != str(row["author_agent_id"])
                or event_id != str(row["event_id"])
                or stage != "community_posting"
                or schema_version
                != RN_AUXILIARY_STAGE_SCHEMA_VERSIONS["community_posting"]
            ):
                raise PaperMemoryError(
                    "Community post trace logical-call identity is cross-scoped"
                )
            if self.event_schedule is None:
                raise PaperMemoryError(
                    "Community post trace requires a frozen event schedule"
                )
            scheduled = self.event_schedule.event(event_id)
            if (
                str(scheduled["subturn"]) != "pm"
                or int(scheduled["turn"]) != int(row["turn"])
                or str(scheduled["date"]) != str(row["date"])
            ):
                raise PaperMemoryError(
                    "Community post trace differs from the frozen PM event"
                )
            accepted_sha = _strict_sha256(
                row["accepted_response_sha256"],
                "community post trace accepted response hash",
            )
            if consumed.get(logical_call_id) != accepted_sha:
                raise PaperMemoryError(
                    "Community post trace response hash differs from phase consumption"
                )
            if (
                str(row["manifest_sha256"]) != self.manifest_sha256
                or str(row["eligibility_status"]) != "eligible"
            ):
                raise PaperMemoryError(
                    "Community post trace manifest or eligibility is invalid"
                )
            for column in (
                "ltb_sha256",
                "view_change_sha256",
                "prompt_template_sha256",
                "prompt_values_sha256",
            ):
                _strict_sha256(row[column], f"community post trace {column}")

            ltb = connection.execute(
                """
                SELECT agent_id, event_id, turn, scientific_sha256
                FROM paper_ltb_states
                WHERE ltb_id = ? AND run_id = ? AND condition_id = ?
                """,
                (row["ltb_id"], self.run_id, self.condition_id),
            ).fetchone()
            fill = connection.execute(
                """
                SELECT agent_id, event_id, turn
                FROM paper_fill_ledger
                WHERE fill_id = ? AND run_id = ? AND condition_id = ?
                """,
                (row["fill_id"], self.run_id, self.condition_id),
            ).fetchone()
            if ltb is None or fill is None or any(
                (
                    str(source["agent_id"]) != agent_id
                    or str(source["event_id"]) != event_id
                    or int(source["turn"]) != int(row["turn"])
                )
                for source in (ltb, fill)
            ):
                raise PaperMemoryError(
                    "Community post trace LTB/fill lineage is missing or cross-scoped"
                )
            if _strict_sha256(
                ltb["scientific_sha256"],
                "community post trace stored LTB hash",
            ) != str(row["ltb_sha256"]):
                raise PaperMemoryError(
                    "Community post trace LTB hash differs from the stored LTB"
                )
            human_log = self._reconstruct_human_log(
                connection,
                ltb_id=str(row["ltb_id"]),
            )
            view_change_sha = scientific_sha256(human_log["view_change"])
            if (
                str(row["view_change_sha256"]) != view_change_sha
                or str(row["view_change_id"])
                != "view-change:" + view_change_sha[:40]
            ):
                raise PaperMemoryError(
                    "Community post trace view-change identity is invalid"
                )

            phase_id = _required_text(
                row["phase_id"], "community post trace phase_id"
            )
            if phase_id not in phase_posts:
                observation = connection.execute(
                    """
                    SELECT payload_json, payload_sha256
                    FROM observation_events
                    WHERE run_id = ? AND condition_id = ? AND event_id = ?
                      AND stage = ?
                    """,
                    (
                        self.run_id,
                        self.condition_id,
                        event_id,
                        f"community_posts:{phase_id}",
                    ),
                ).fetchone()
                if observation is None:
                    raise PaperMemoryError(
                        "Community post trace has no atomic public-board observation"
                    )
                payload = _load_canonical_json(
                    observation["payload_json"],
                    label="community post trace board observation",
                    expected_type=dict,
                )
                if scientific_sha256(payload) != _strict_sha256(
                    observation["payload_sha256"],
                    "community post trace board observation hash",
                ):
                    raise PaperMemoryError(
                        "Community post trace board observation hash is invalid"
                    )
                if (
                    payload.get("schema_version") != "rn-community-posts-v1"
                    or not isinstance(payload.get("phase"), Mapping)
                    or payload["phase"].get("phase_id") != phase_id
                    or not isinstance(payload.get("posts"), list)
                ):
                    raise PaperMemoryError(
                        "Community post trace board observation is malformed"
                    )
                by_author: dict[str, Mapping[str, Any]] = {}
                for post in payload["posts"]:
                    if not isinstance(post, Mapping):
                        raise PaperMemoryError(
                            "Community post board contains a malformed post"
                        )
                    if set(post) != {
                        "post_id",
                        "author_agent_id",
                        "title",
                        "body",
                        "body_sha256",
                        "content_version",
                        "content_version_sha256",
                        "post_type",
                        "score",
                        "like_count",
                    }:
                        raise PaperMemoryError(
                            "Community public post contains private or unknown fields"
                        )
                    author = _required_text(
                        post.get("author_agent_id"),
                        "community post board author",
                    )
                    if author in by_author:
                        raise PaperMemoryError(
                            "Community post board repeats an author"
                        )
                    by_author[author] = post
                phase_posts[phase_id] = by_author

            status = str(row["posting_status"])
            public_post = phase_posts[phase_id].get(agent_id)
            if status == "posted":
                if public_post is None:
                    raise PaperMemoryError(
                        "Posted community trace has no public post"
                    )
                title = _required_text(
                    public_post.get("title"),
                    "traced community post title",
                )
                body = _required_text(
                    public_post.get("body"),
                    "traced community post body",
                )
                if (
                    str(row["post_id"]) != public_post.get("post_id")
                    or _strict_sha256(
                        row["title_sha256"],
                        "community post trace title hash",
                    )
                    != hashlib.sha256(title.encode("utf-8")).hexdigest()
                    or _strict_sha256(
                        row["body_sha256"],
                        "community post trace body hash",
                    )
                    != hashlib.sha256(body.encode("utf-8")).hexdigest()
                ):
                    raise PaperMemoryError(
                        "Community post trace content hashes differ from the public post"
                    )
                traced_posted_authors.setdefault(phase_id, set()).add(agent_id)
                posted += 1
            elif status == "skipped":
                if public_post is not None or any(
                    row[column] is not None
                    for column in ("post_id", "title_sha256", "body_sha256")
                ):
                    raise PaperMemoryError(
                        "Skipped community post trace contains a public post identity"
                    )
                skipped += 1
            else:
                raise PaperMemoryError(
                    "Community post trace posting status is invalid"
                )

            values = {
                column: row[column]
                for column in (
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
                )
            }
            identity = {
                key: values[key]
                for key in (
                    "run_id",
                    "condition_id",
                    "phase_id",
                    "author_agent_id",
                )
            }
            if (
                str(row["trace_id"])
                != "post-trace:" + scientific_sha256(identity)[:40]
                or _strict_sha256(
                    row["trace_sha256"],
                    "community post trace scientific hash",
                )
                != scientific_sha256(values)
            ):
                raise PaperMemoryError(
                    "Community post trace deterministic identity/hash is invalid"
                )
            traced[logical_call_id] = row

        if set(traced) != set(consumed):
            raise PaperMemoryError(
                "Community post trace set differs from consumed posting responses"
            )
        if set(phase_posts) != board_phases:
            raise PaperMemoryError(
                "Community public-board phases differ from private post-trace phases"
            )
        for phase_id, posts_by_author in phase_posts.items():
            if set(posts_by_author) != traced_posted_authors.get(phase_id, set()):
                raise PaperMemoryError(
                    "Community public post set differs from posted private traces"
                )
        return {
            "community_post_traces": len(traced),
            "community_posts_traced": posted,
            "community_post_skips_traced": skipped,
        }

    def _sealed_stb_evidence(
        self,
        connection: Any,
        *,
        agent_id: str,
        event_id: str,
        scheduled: Mapping[str, Any],
        current_evidence: Mapping[str, Any],
    ) -> tuple[list[dict[str, Any]], str]:
        """Validate the full STB packet and return server-derived ledger rows.

        This is intentionally not a generic deserializer.  Every news item is
        re-validated against the registry and its event cutoff; every community
        claim is reloaded from the service's append-only claim observation and
        checked against reader-owned delivered exposures.
        """
        if not isinstance(current_evidence, Mapping):
            raise PaperMemoryError("STB current_evidence must be a structured packet")
        expected_keys = {
            "event_id",
            "date",
            "subturn",
            "news",
            "depth2_search_results",
            "community_claims",
        }
        if set(current_evidence) != expected_keys:
            raise PaperMemoryError("STB current_evidence has an invalid schema")
        packet_event_id = _required_text(current_evidence["event_id"], "STB packet event_id")
        packet_date = _parse_iso_date(current_evidence["date"], "STB packet date")
        packet_subturn = _required_text(current_evidence["subturn"], "STB packet subturn").lower()
        if (
            packet_event_id != event_id
            or packet_date != str(scheduled["date"])
            or packet_subturn != str(scheduled["subturn"])
        ):
            raise PaperMemoryError("STB current_evidence differs from the frozen event schedule")
        if packet_subturn not in {"am", "pm"}:
            raise PaperMemoryError("STB packet subturn must be am or pm")
        raw_news = current_evidence["news"]
        raw_depth2_search = current_evidence["depth2_search_results"]
        raw_claims = current_evidence["community_claims"]
        if (
            not isinstance(raw_news, list)
            or not isinstance(raw_depth2_search, list)
            or not isinstance(raw_claims, list)
        ):
            raise PaperMemoryError(
                "STB packet news, depth2_search_results, and community_claims must be arrays"
            )

        normalized_news: tuple[dict[str, str], ...]
        if self.news_registry is None:
            if raw_news:
                raise PaperMemoryError("STB news requires a sealed real-news registry")
            normalized_news = ()
        else:
            if self.stage_input_registry is None:
                raise PaperMemoryError("STB news requires a sealed timestamp-level stage input registry")
            try:
                stage_event = self.stage_input_registry.event(event_id)
                projection_depth = _news_projection_depth(raw_news)
                normalized_news = self.news_registry.validate_delivery(
                    event_id=event_id,
                    delivered=raw_news,
                    cutoff_timestamp=stage_event.news_cutoff_timestamp,
                    news_depth=projection_depth,
                )
            except (NewsBundleError, ValueError) as exc:
                raise PaperMemoryError(f"STB news does not match sealed delivery: {exc}") from exc

        normalized_search: tuple[dict[str, str], ...] = ()
        if raw_depth2_search:
            if self.depth2_search_registry is None or self.news_registry is None:
                raise PaperMemoryError(
                    "STB Depth-2 search results require a sealed generated registry"
                )
            rows = self.depth2_search_registry.get("events")
            if not isinstance(rows, list):
                raise PaperMemoryError("Sealed Depth-2 search registry has no event rows")
            matches = [
                row
                for row in rows
                if isinstance(row, Mapping) and row.get("event_id") == event_id
            ]
            if len(matches) != 1:
                raise PaperMemoryError(
                    "Sealed Depth-2 search registry does not cover this event exactly once"
                )
            row = matches[0]
            ids = row.get("result_article_ids")
            hashes = row.get("result_payload_sha256s")
            if (
                not isinstance(ids, list)
                or not isinstance(hashes, list)
                or len(ids) != len(hashes)
            ):
                raise PaperMemoryError("Sealed Depth-2 search identity/hash rows are invalid")
            projected: list[dict[str, str]] = []
            for article_id, expected_hash in zip(ids, hashes, strict=True):
                try:
                    article = self.news_registry.articles[article_id]
                except KeyError as exc:
                    raise PaperMemoryError(
                        "Depth-2 search article is absent from sealed clean news"
                    ) from exc
                if article.payload_sha256 != expected_hash:
                    raise PaperMemoryError(
                        "Depth-2 search article hash differs from sealed clean news"
                    )
                projected.append(dict(article.stage_projection(news_depth=2)))
            normalized_search = tuple(projected)
            if list(normalized_search) != raw_depth2_search:
                raise PaperMemoryError(
                    "STB Depth-2 search results differ from the generated registry"
                )

        normalized_claims = tuple(
            self._community_claim_stage_projection(
                connection,
                agent_id=agent_id,
                event_id=event_id,
                raw_claim=raw_claim,
            )
            for raw_claim in raw_claims
        )
        packet = {
            "event_id": event_id,
            "date": str(scheduled["date"]),
            "subturn": str(scheduled["subturn"]),
            "news": [dict(item) for item in normalized_news],
            "depth2_search_results": [dict(item) for item in normalized_search],
            "community_claims": [dict(item) for item in normalized_claims],
        }
        if packet != dict(current_evidence):
            raise PaperMemoryError("STB current_evidence differs from sealed news or community lineage")
        evidence_rows: list[dict[str, Any]] = [
            {
                "kind": "news",
                "article_id": item["article_id"],
                "payload_sha256": item["payload_sha256"],
            }
            for item in normalized_news
        ]
        evidence_rows.extend(
            {
                "kind": "news",
                "article_id": item["article_id"],
                "payload_sha256": item["payload_sha256"],
            }
            for item in normalized_search
        )
        evidence_rows.extend(
            {
                "kind": "community_claim",
                "claim_id": item["claim_id"],
                "source_exposure_ids": list(item["source_exposure_ids"]),
            }
            for item in normalized_claims
        )
        self._assert_stb_evidence_shape(evidence_rows)
        return evidence_rows, scientific_sha256(packet)

    def _community_claim_stage_projection(
        self,
        connection: Any,
        *,
        agent_id: str,
        event_id: str,
        raw_claim: Any,
    ) -> dict[str, Any]:
        expected_claim_fields = {"claim_id", "claim_text", "stance", "source_exposure_ids"}
        if not isinstance(raw_claim, Mapping) or set(raw_claim) != expected_claim_fields:
            raise PaperMemoryError("STB community claim has an invalid stage schema")
        claim_id = _required_text(raw_claim["claim_id"], "STB community claim ID")
        source_ids = raw_claim["source_exposure_ids"]
        if (
            not isinstance(source_ids, list)
            or not source_ids
            or any(not isinstance(item, str) or not item.strip() for item in source_ids)
            or len(source_ids) != len(set(source_ids))
        ):
            raise PaperMemoryError("STB community claim requires distinct source exposure IDs")
        row = connection.execute(
            """
            SELECT payload_json FROM observation_events
            WHERE run_id = ? AND condition_id = ? AND event_id = ? AND stage = ?
            """,
            (self.run_id, self.condition_id, event_id, f"community_claims:{agent_id}"),
        ).fetchone()
        if row is None:
            raise PaperMemoryError("STB community claim has no append-only claim observation")
        try:
            payload = json.loads(str(row["payload_json"]))
        except json.JSONDecodeError as exc:
            raise PaperMemoryError("Stored community claim observation is not JSON") from exc
        if canonical_json(payload) != str(row["payload_json"]):
            raise PaperMemoryError("Stored community claim observation is not canonical")
        if (
            not isinstance(payload, Mapping)
            or set(payload) != {"schema_version", "agent_id", "event_id", "claims"}
            or payload.get("schema_version") != "rn-community-claims-v1"
            or payload.get("agent_id") != agent_id
            or payload.get("event_id") != event_id
            or not isinstance(payload.get("claims"), list)
        ):
            raise PaperMemoryError("Stored community claim observation has an invalid schema")
        matches = [
            item
            for item in payload["claims"]
            if isinstance(item, Mapping) and item.get("claim_id") == claim_id
        ]
        if len(matches) != 1:
            raise PaperMemoryError("STB community claim does not exist exactly once in its observation")
        stored = matches[0]
        stored_fields = {
            "claim_id",
            "claim_text",
            "stance",
            "source_exposure_ids",
            "supporting_quote",
            "source_roots",
        }
        if set(stored) != stored_fields:
            raise PaperMemoryError("Stored community claim has an invalid schema")
        projection = {
            "claim_id": _required_text(stored["claim_id"], "stored community claim ID"),
            "claim_text": _required_text(stored["claim_text"], "stored community claim text"),
            "stance": _required_text(stored["stance"], "stored community claim stance"),
            "source_exposure_ids": list(stored["source_exposure_ids"]),
        }
        if projection != dict(raw_claim):
            raise PaperMemoryError("STB community claim differs from its append-only observation")
        visible_ids = self._visible_community_exposure_ids(
            connection,
            agent_id=agent_id,
            event_id=event_id,
        )
        if set(projection["source_exposure_ids"]) - visible_ids:
            raise PaperMemoryError("STB community claim cites an undelivered or foreign exposure")
        return projection

    def _visible_community_exposure_ids(
        self,
        connection: Any,
        *,
        agent_id: str,
        event_id: str,
    ) -> set[str]:
        rows = connection.execute(
            """
            SELECT exposure_id, channel, event_id, metadata_json
            FROM agent_exposures
            WHERE run_id = ? AND condition_id = ? AND agent_id = ? AND status = 'delivered'
            """,
            (self.run_id, self.condition_id, agent_id),
        ).fetchall()
        visible: set[str] = set()
        for row in rows:
            channel = str(row["channel"])
            if channel == "community_best" and str(row["event_id"]) == event_id:
                visible.add(str(row["exposure_id"]))
                continue
            if channel != "community_selected":
                continue
            try:
                metadata = json.loads(str(row["metadata_json"]))
            except json.JSONDecodeError as exc:
                raise PaperMemoryError("Stored community exposure metadata is not JSON") from exc
            if canonical_json(metadata) != str(row["metadata_json"]):
                raise PaperMemoryError("Stored community exposure metadata is not canonical")
            if isinstance(metadata, Mapping) and metadata.get("visible_from_event_id") == event_id:
                visible.add(str(row["exposure_id"]))
        trace_rows = connection.execute(
            """
            SELECT payload_json
            FROM observation_events
            WHERE run_id = ? AND condition_id = ?
              AND stage LIKE 'community_reader_trace:%'
            """,
            (self.run_id, self.condition_id),
        ).fetchall()
        matched_payloads = 0
        for row in trace_rows:
            try:
                payload = json.loads(str(row["payload_json"]))
            except json.JSONDecodeError as exc:
                raise PaperMemoryError(
                    "Stored community reader trace is not JSON"
                ) from exc
            if canonical_json(payload) != str(row["payload_json"]):
                raise PaperMemoryError(
                    "Stored community reader trace is not canonical"
                )
            if (
                not isinstance(payload, Mapping)
                or payload.get("visible_from_event_id") != event_id
            ):
                continue
            matched_payloads += 1
            readers = payload.get("readers")
            if not isinstance(readers, list):
                raise PaperMemoryError(
                    "Stored community reader trace has malformed readers"
                )
            reader_rows = [
                item
                for item in readers
                if isinstance(item, Mapping)
                and item.get("reader_agent_id") == agent_id
            ]
            if len(reader_rows) > 1:
                raise PaperMemoryError(
                    "Stored community reader trace repeats a reader"
                )
            if not reader_rows:
                continue
            candidates = reader_rows[0].get("candidates")
            if not isinstance(candidates, list):
                raise PaperMemoryError(
                    "Stored community reader trace has malformed candidates"
                )
            for candidate in candidates:
                if not isinstance(candidate, Mapping):
                    raise PaperMemoryError(
                        "Stored community candidate exposure is malformed"
                    )
                exposure_id = candidate.get("source_exposure_id")
                if (
                    candidate.get("content_level") != "title_only"
                    or not isinstance(exposure_id, str)
                    or not exposure_id
                ):
                    raise PaperMemoryError(
                        "Stored community title-only exposure is malformed"
                    )
                visible.add(exposure_id)
        if matched_payloads > 1:
            raise PaperMemoryError(
                "Multiple community reader traces target one event"
            )
        return visible

    def _assert_stb_evidence_shape(self, evidence: list[dict[str, Any]]) -> None:
        seen_ids: set[str] = set()
        for row in evidence:
            kind = row.get("kind")
            if kind == "news":
                expected = {"kind", "article_id", "payload_sha256"}
                identifier = row.get("article_id")
                payload_hash = row.get("payload_sha256")
                if not isinstance(payload_hash, str) or len(payload_hash) != 64:
                    raise PaperMemoryError("STB news evidence requires a SHA-256 payload hash")
            elif kind == "community_claim":
                expected = {"kind", "claim_id", "source_exposure_ids"}
                identifier = row.get("claim_id")
                source_ids = row.get("source_exposure_ids")
                if (
                    not isinstance(source_ids, list)
                    or not source_ids
                    or any(not isinstance(item, str) or not item for item in source_ids)
                ):
                    raise PaperMemoryError("STB community claim requires non-empty source exposure IDs")
            else:
                raise PaperMemoryError("STB evidence kind must be news or community_claim")
            if set(row) != expected:
                raise PaperMemoryError("STB evidence has fields outside its typed allowlist")
            if not isinstance(identifier, str) or not identifier.strip():
                raise PaperMemoryError("STB evidence ID must be a non-empty string")
            if identifier in seen_ids:
                raise PaperMemoryError("STB evidence IDs must be unique within an event")
            seen_ids.add(identifier)

    @staticmethod
    def _evidence_ids(evidence_rows: Iterable[Mapping[str, Any]]) -> set[str]:
        """Return the one server-owned identifier for each sealed STB source."""
        result: set[str] = set()
        for row in evidence_rows:
            kind = row.get("kind")
            if kind == "news":
                identifier = row.get("article_id")
            elif kind == "community_claim":
                identifier = row.get("claim_id")
            else:  # _assert_stb_evidence_shape must already have rejected this.
                raise PaperMemoryError("STB evidence kind is not recognized")
            if not isinstance(identifier, str) or not identifier or identifier in result:
                raise PaperMemoryError("STB evidence IDs are not a unique server-owned set")
            result.add(identifier)
        return result

    def _assert_scoped_reference(
        self, connection: Any, table: str, key_column: str, key_value: str
    ) -> None:
        row = connection.execute(
            f"SELECT run_id, condition_id FROM {table} WHERE {key_column} = ?", (key_value,)
        ).fetchone()
        if row is None:
            raise PaperMemoryError(f"Missing referenced {table}.{key_column}={key_value}")
        if str(row["run_id"]) != self.run_id or str(row["condition_id"]) != self.condition_id:
            raise PaperMemoryError(f"Cross-run/condition reference to {table}.{key_column}={key_value}")

    def _assert_ltb_visible_for_agent(
        self,
        connection: Any,
        ltb_id: str,
        *,
        agent_id: str,
        decision_turn: int,
    ) -> None:
        self._assert_scoped_reference(connection, "paper_ltb_states", "ltb_id", ltb_id)
        row = connection.execute(
            "SELECT agent_id, visible_from_turn, turn FROM paper_ltb_states WHERE ltb_id = ?", (ltb_id,)
        ).fetchone()
        if row is None:
            raise PaperMemoryError(f"Missing LTB {ltb_id}")
        if str(row["agent_id"]) != agent_id:
            raise PaperMemoryError("Decision/fill LTB belongs to another agent")
        if int(row["visible_from_turn"]) > int(decision_turn):
            raise PaperMemoryError("LTB is not yet visible for this decision turn")
        if int(row["turn"]) >= int(decision_turn):
            raise PaperMemoryError("Same-turn/future LTB cannot enter a decision or fill")

    def _assert_stb_for_event(
        self,
        connection: Any,
        stb_id: str,
        *,
        agent_id: str,
        event_id: str,
        turn: int,
    ) -> None:
        self._assert_scoped_reference(connection, "short_term_belief_history", "stb_id", stb_id)
        row = connection.execute(
            "SELECT agent_id, event_id, turn FROM short_term_belief_history WHERE stb_id = ?", (stb_id,)
        ).fetchone()
        if row is None:
            raise PaperMemoryError(f"Missing STB {stb_id}")
        if (
            str(row["agent_id"]) != agent_id
            or str(row["event_id"]) != event_id
            or int(row["turn"]) != int(turn)
        ):
            raise PaperMemoryError("STB does not match the fill/LTB agent event and turn")

    def _validate_sealed_decision_packet(
        self,
        connection: Any,
        *,
        agent_id: str,
        event_id: str,
        turn: int,
        date: str,
        subturn: str,
        source_ltb_id: str,
        source_stb_id: str,
        decision_packet: Mapping[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        """Rebuild the decision boundary before accepting a model action.

        ``input_sha256`` is not caller authority.  The method verifies the
        entire packet against the frozen timestamp/market registry, current
        ledger portfolio, and the six-dimension states already committed for
        this agent/event.
        """
        required = {
            "event",
            "previous_ltb",
            "current_stb",
            "market",
            "execution_state",
            "input_sha256",
        }
        if not isinstance(decision_packet, Mapping) or set(decision_packet) != required:
            raise PaperMemoryError("Decision packet has an invalid schema")
        input_sha256 = _strict_sha256(decision_packet["input_sha256"], "decision input_sha256")
        unsigned = {key: decision_packet[key] for key in required - {"input_sha256"}}
        if scientific_sha256(unsigned) != input_sha256:
            raise PaperMemoryError("Decision packet input_sha256 does not match its content")
        scheduled = self._assert_scheduled_event(event_id, turn=turn, date=date, subturn=subturn)
        stage_event = self.stage_input_registry.event(event_id)
        expected_event = {
            "event_id": event_id,
            "date": str(scheduled["date"]),
            "subturn": str(scheduled["subturn"]),
            "market_feature_as_of": stage_event.market_feature_as_of,
            "execution_price": float(scheduled["execution_price"]),
        }
        expected_market = stage_event.market.stage_projection(subturn=stage_event.subturn)
        if decision_packet["event"] != expected_event or decision_packet["market"] != expected_market:
            raise PaperMemoryError("Decision packet event or market differs from sealed stage inputs")
        parent = connection.execute(
            "SELECT dim_1, dim_2, dim_3, dim_4, dim_5, dim_6 FROM paper_ltb_states WHERE ltb_id = ?",
            (source_ltb_id,),
        ).fetchone()
        stb = connection.execute(
            "SELECT dim_1, dim_2, dim_3, dim_4, dim_5, dim_6 FROM short_term_belief_history WHERE stb_id = ?",
            (source_stb_id,),
        ).fetchone()
        if parent is None or stb is None:
            raise PaperMemoryError("Decision packet is missing sealed LTB/STB state")
        expected_ltb = {key: str(parent[key]) for key in DIMENSION_KEYS}
        expected_stb = {key: str(stb[key]) for key in DIMENSION_KEYS}
        if decision_packet["previous_ltb"] != expected_ltb or decision_packet["current_stb"] != expected_stb:
            raise PaperMemoryError("Decision packet belief state differs from sealed LTB/STB")
        state = self._portfolio_before_event(
            connection,
            agent_id=agent_id,
            event_id=event_id,
            turn=turn,
        )
        expected_execution = self._execution_state_from_portfolio(scheduled, state)
        if decision_packet["execution_state"] != expected_execution:
            raise PaperMemoryError("Decision packet execution state differs from sealed portfolio ledger")
        return input_sha256, expected_execution

    def _assert_decision_for_fill(
        self,
        connection: Any,
        *,
        decision_id: str,
        agent_id: str,
        event_id: str,
        turn: int,
        date: str,
        subturn: str,
        action: str,
        requested_quantity: int,
        source_ltb_id: str,
        source_stb_id: str,
    ) -> None:
        """Require a previously committed, exact decision authorization."""
        expected_id = self._decision_id(agent_id, event_id)
        if decision_id != expected_id:
            raise PaperMemoryError("Fill decision_id is not the deterministic decision for its agent/event")
        self._assert_scoped_reference(connection, "paper_decisions", "decision_id", decision_id)
        row = connection.execute(
            """
            SELECT manifest_sha256, agent_id, event_id, turn, date, subturn, action,
                   requested_quantity, source_ltb_id, source_stb_id
            FROM paper_decisions WHERE decision_id = ?
            """,
            (decision_id,),
        ).fetchone()
        expected = {
            "manifest_sha256": self.manifest_sha256,
            "agent_id": agent_id,
            "event_id": event_id,
            "turn": int(turn),
            "date": str(date),
            "subturn": str(subturn),
            "action": str(action),
            "requested_quantity": int(requested_quantity),
            "source_ltb_id": source_ltb_id,
            "source_stb_id": source_stb_id,
        }
        if row is None or any(row[key] != value for key, value in expected.items()):
            raise PaperMemoryError("Fill differs from its sealed decision authorization")

    def _assert_analysis_for_decision(
        self,
        connection: Any,
        *,
        analysis_id: str,
        agent_id: str,
        event_id: str,
        turn: int,
        date: str,
        subturn: str,
        source_ltb_id: str,
        source_stb_id: str,
    ) -> None:
        expected_id = self._analysis_id(agent_id, event_id)
        if analysis_id != expected_id:
            raise PaperMemoryError("Decision analysis_id is not the deterministic analysis for its agent/event")
        self._assert_scoped_reference(connection, "paper_analyses", "analysis_id", analysis_id)
        row = connection.execute(
            """
            SELECT manifest_sha256, agent_id, event_id, turn, date, subturn, source_ltb_id, source_stb_id
            FROM paper_analyses WHERE analysis_id = ?
            """,
            (analysis_id,),
        ).fetchone()
        expected = {
            "manifest_sha256": self.manifest_sha256,
            "agent_id": agent_id,
            "event_id": event_id,
            "turn": int(turn),
            "date": str(date),
            "subturn": str(subturn),
            "source_ltb_id": source_ltb_id,
            "source_stb_id": source_stb_id,
        }
        if row is None or any(row[key] != value for key, value in expected.items()):
            raise PaperMemoryError("Decision differs from its sealed analysis lineage")

    def _assert_exact_trace_lineage(
        self,
        connection: Any,
        *,
        agent_id: str,
        event_id: str,
        turn: int,
        previous_ltb_id: str,
        stb_id: str,
        decision_id: str,
        fill_id: str,
        ltb_id: str,
        input_sha256: str,
    ) -> None:
        """Bind every trace edge to the same agent/event/turn, never just its arm."""
        expected = {
            "stb_id": self._stb_id(agent_id, event_id),
            "decision_id": self._decision_id(agent_id, event_id),
            "fill_id": self._fill_id(agent_id, event_id),
            "ltb_id": self._ltb_id(agent_id, event_id),
        }
        supplied = {
            "stb_id": stb_id,
            "decision_id": decision_id,
            "fill_id": fill_id,
            "ltb_id": ltb_id,
        }
        if supplied != expected:
            raise PaperMemoryError("Trace IDs are not the deterministic IDs for its agent/event")
        self._assert_ltb_visible_for_agent(
            connection, previous_ltb_id, agent_id=agent_id, decision_turn=turn
        )
        self._assert_stb_for_event(
            connection, stb_id, agent_id=agent_id, event_id=event_id, turn=turn
        )
        self._assert_scoped_reference(connection, "paper_decisions", "decision_id", decision_id)
        decision = connection.execute(
            """
            SELECT agent_id, event_id, turn, source_ltb_id, source_stb_id, input_sha256
            FROM paper_decisions WHERE decision_id = ?
            """,
            (decision_id,),
        ).fetchone()
        self._assert_scoped_reference(connection, "paper_fill_ledger", "fill_id", fill_id)
        fill = connection.execute(
            """
            SELECT agent_id, event_id, turn, decision_id, source_ltb_id, source_stb_id
            FROM paper_fill_ledger WHERE fill_id = ?
            """,
            (fill_id,),
        ).fetchone()
        self._assert_scoped_reference(connection, "paper_ltb_states", "ltb_id", ltb_id)
        ltb = connection.execute(
            """
            SELECT agent_id, event_id, turn, parent_ltb_id, current_stb_id
            FROM paper_ltb_states WHERE ltb_id = ?
            """,
            (ltb_id,),
        ).fetchone()
        transition_id = self._transition_id(agent_id, event_id)
        self._assert_scoped_reference(
            connection, "ltb_dimension_transitions", "transition_id", transition_id
        )
        transition = connection.execute(
            """
            SELECT agent_id, event_id, turn, parent_ltb_id, stb_id, ltb_id, fill_id
            FROM ltb_dimension_transitions WHERE transition_id = ?
            """,
            (transition_id,),
        ).fetchone()
        if None in (decision, fill, ltb, transition):
            raise PaperMemoryError("Trace references missing scientific lineage state")
        if (
            str(decision["agent_id"]) != agent_id
            or str(decision["event_id"]) != event_id
            or int(decision["turn"]) != turn
            or str(decision["source_ltb_id"]) != previous_ltb_id
            or str(decision["source_stb_id"]) != stb_id
            or str(decision["input_sha256"]) != input_sha256
        ):
            raise PaperMemoryError("Trace decision does not match agent/event/turn/input lineage")
        if (
            str(fill["agent_id"]) != agent_id
            or str(fill["event_id"]) != event_id
            or int(fill["turn"]) != turn
            or str(fill["decision_id"]) != decision_id
            or str(fill["source_ltb_id"]) != previous_ltb_id
            or str(fill["source_stb_id"]) != stb_id
        ):
            raise PaperMemoryError("Trace fill does not match its decision/STB/LTB lineage")
        if (
            str(ltb["agent_id"]) != agent_id
            or str(ltb["event_id"]) != event_id
            or int(ltb["turn"]) != turn
            or str(ltb["parent_ltb_id"]) != previous_ltb_id
            or str(ltb["current_stb_id"]) != stb_id
        ):
            raise PaperMemoryError("Trace LTB does not match the current event lineage")
        if (
            str(transition["agent_id"]) != agent_id
            or str(transition["event_id"]) != event_id
            or int(transition["turn"]) != turn
            or str(transition["parent_ltb_id"]) != previous_ltb_id
            or str(transition["stb_id"]) != stb_id
            or str(transition["ltb_id"]) != ltb_id
            or str(transition["fill_id"]) != fill_id
        ):
            raise PaperMemoryError("Trace transition does not match the current event lineage")

    def _write_stb_memory_edges(
        self,
        connection: Any,
        *,
        agent_id: str,
        event_id: str,
        stb_id: str,
        evidence_rows: Iterable[Mapping[str, Any]],
        dimension_evidence: Mapping[str, Mapping[str, list[str]]],
    ) -> None:
        """Persist prompt-consumption and dimension evidence edges for an STB.

        A ``dimension IS NULL`` edge means that the source was present in the
        sealed current-evidence request.  A dimension-qualified edge means the
        validated STB output actually cited the source in that dimension.  The
        support/contradict relation remains losslessly recoverable from the
        target STB's immutable ``dimension_evidence_json``; duplicating it in
        this polymorphic edge table would create a second authority.
        """

        source_kind_by_id: dict[str, str] = {}
        for raw in evidence_rows:
            kind = _required_text(raw.get("kind"), "STB evidence edge source kind")
            if kind == "news":
                source_id = _required_text(raw.get("article_id"), "STB news edge source ID")
            elif kind == "community_claim":
                source_id = _required_text(
                    raw.get("claim_id"), "STB community edge source ID"
                )
            else:  # The sealed-evidence validator should already reject this.
                raise PaperMemoryError("STB evidence edge has an unsupported source kind")
            if source_id in source_kind_by_id:
                raise PaperMemoryError("STB evidence edge source IDs must be unique")
            source_kind_by_id[source_id] = kind
            self._insert_memory_evidence_edge(
                connection,
                agent_id=agent_id,
                event_id=event_id,
                target_kind="stb",
                target_id=stb_id,
                source_kind=kind,
                source_id=source_id,
                dimension=None,
            )

        for dimension in DIMENSION_KEYS:
            item = dimension_evidence[dimension]
            for relation in ("support", "contradict"):
                for source_id in item[relation]:
                    kind = source_kind_by_id.get(source_id)
                    if kind is None:
                        raise PaperMemoryError("STB dimension edge cites an unavailable source")
                    self._insert_memory_evidence_edge(
                        connection,
                        agent_id=agent_id,
                        event_id=event_id,
                        target_kind="stb",
                        target_id=stb_id,
                        source_kind=kind,
                        source_id=source_id,
                        dimension=dimension,
                    )

    def _write_ltb_memory_edges(
        self,
        connection: Any,
        *,
        agent_id: str,
        event_id: str,
        ltb_id: str,
        parent_ltb_id: str,
        stb_id: str,
        decision_id: str,
        fill_id: str,
        integration_evidence_by_dimension: Mapping[str, Mapping[str, list[str]]],
        flat_integration_evidence: Iterable[Mapping[str, Any]],
    ) -> None:
        """Persist the recursive LTB's structural and selected evidence DAG."""

        structural_sources = (
            ("ltb", parent_ltb_id),
            ("stb", stb_id),
            ("decision", decision_id),
            ("fill", fill_id),
        )
        for source_kind, source_id in structural_sources:
            self._insert_memory_evidence_edge(
                connection,
                agent_id=agent_id,
                event_id=event_id,
                target_kind="ltb",
                target_id=ltb_id,
                source_kind=source_kind,
                source_id=source_id,
                dimension=None,
            )

        stb_row = connection.execute(
            "SELECT evidence_json FROM short_term_belief_history WHERE stb_id = ?",
            (stb_id,),
        ).fetchone()
        if stb_row is None:
            raise PaperMemoryError("LTB evidence edges cannot resolve the current STB")
        stb_evidence = _load_canonical_json(
            stb_row["evidence_json"],
            label="LTB edge current STB evidence",
            expected_type=list,
        )
        source_kind_by_id: dict[str, str] = {}
        for raw in stb_evidence:
            kind = _required_text(raw.get("kind"), "LTB evidence edge source kind")
            source_id = _required_text(
                raw.get("article_id") if kind == "news" else raw.get("claim_id"),
                "LTB evidence edge source ID",
            )
            source_kind_by_id[source_id] = kind
        outcome_ids = {
            _required_text(item.get("outcome_id"), "LTB outcome edge source ID")
            for item in flat_integration_evidence
            if item.get("kind") == "trade_outcome"
        }

        for dimension in DIMENSION_KEYS:
            item = integration_evidence_by_dimension[dimension]
            for relation in ("support", "contradict"):
                for source_id in item[relation]:
                    if source_id in outcome_ids:
                        source_kind = "trade_outcome"
                    else:
                        source_kind = source_kind_by_id.get(source_id, "")
                    if source_kind not in {"news", "community_claim", "trade_outcome"}:
                        raise PaperMemoryError("LTB dimension edge cites an unavailable source")
                    self._insert_memory_evidence_edge(
                        connection,
                        agent_id=agent_id,
                        event_id=event_id,
                        target_kind="ltb",
                        target_id=ltb_id,
                        source_kind=source_kind,
                        source_id=source_id,
                        dimension=dimension,
                    )

    def _insert_memory_evidence_edge(
        self,
        connection: Any,
        *,
        agent_id: str,
        event_id: str,
        target_kind: str,
        target_id: str,
        source_kind: str,
        source_id: str,
        dimension: str | None,
    ) -> str:
        if target_kind not in {"stb", "ltb"}:
            raise PaperMemoryError("Memory evidence edge has an unsupported target kind")
        if source_kind not in _MEMORY_EDGE_SOURCE_KINDS:
            raise PaperMemoryError("Memory evidence edge has an unsupported source kind")
        if dimension is not None and dimension not in DIMENSION_KEYS:
            raise PaperMemoryError("Memory evidence edge has an invalid dimension")
        values_without_id = {
            "run_id": self.run_id,
            "condition_id": self.condition_id,
            "agent_id": _required_text(agent_id, "memory edge agent_id"),
            "event_id": _required_text(event_id, "memory edge event_id"),
            "target_kind": target_kind,
            "target_id": _required_text(target_id, "memory edge target_id"),
            "source_kind": source_kind,
            "source_id": _required_text(source_id, "memory edge source_id"),
            "dimension": dimension,
        }
        # This matches CommunityService.attach_claims_to_stb, so a legacy
        # caller that re-attaches the same validated claim is an idempotent
        # replay rather than a duplicate causal edge.
        edge_id = "edge:" + scientific_sha256(values_without_id)[:40]
        self._insert_or_verify(
            connection,
            table="memory_evidence_edges",
            key_column="edge_id",
            key_value=edge_id,
            values={"edge_id": edge_id, **values_without_id},
            compare_columns=tuple(values_without_id),
        )
        return edge_id

    def _consume_phase_call_in_transaction(
        self,
        connection: Any,
        *,
        agent_id: str,
        event_id: str,
        expected_stage: str,
        phase_call: PhaseCallConsumption | None,
    ) -> str | None:
        """Bind one accepted logical response to its scientific stage write."""

        if phase_call is None:
            return None
        if not isinstance(phase_call, PhaseCallConsumption):
            raise PaperMemoryError("phase_call must be a typed PhaseCallConsumption")
        if (
            phase_call.stage != expected_stage
            or expected_stage not in RN_ALL_JOURNALED_STAGE_SCHEMA_VERSIONS
        ):
            raise PaperMemoryError("Phase-call consumption stage differs from its artifact stage")
        logical_call_id = _required_text(
            phase_call.logical_call_id, "phase-call logical_call_id"
        )
        components = logical_call_id.split("|")
        expected_components = [
            self.run_id,
            self.condition_id,
            _required_text(agent_id, "phase-call agent_id"),
            _required_text(event_id, "phase-call event_id"),
            expected_stage,
            RN_ALL_JOURNALED_STAGE_SCHEMA_VERSIONS[expected_stage],
        ]
        if components != expected_components:
            raise PaperMemoryError(
                "Phase-call logical ID is outside its run/condition/agent/event/stage scope"
            )
        response_sha256 = _strict_sha256(
            phase_call.response_sha256, "phase-call response_sha256"
        )
        identity = {
            "run_id": self.run_id,
            "condition_id": self.condition_id,
            "agent_id": agent_id,
            "event_id": event_id,
            "stage": expected_stage,
            "source_kind": "logical_response",
            "source_id": logical_call_id,
        }
        consumption_id = "phase-call:" + scientific_sha256(identity)[:40]
        self._insert_or_verify(
            connection,
            table="phase_consumptions",
            key_column="consumption_id",
            key_value=consumption_id,
            values={
                "consumption_id": consumption_id,
                **identity,
                "payload_sha256": response_sha256,
            },
            compare_columns=(
                "run_id",
                "condition_id",
                "agent_id",
                "event_id",
                "stage",
                "source_kind",
                "source_id",
                "payload_sha256",
            ),
        )
        return consumption_id

    def _validate_ltb_dimension_evidence(
        self,
        connection: Any,
        *,
        integration_evidence_by_dimension: Mapping[str, Any],
        agent_id: str,
        event_id: str,
        stb_id: str,
    ) -> tuple[dict[str, dict[str, list[str]]], list[dict[str, Any]], list[tuple[str, str]]]:
        """Bind every LTB output reference to its same-dimension STB source.

        Only ``dim_6`` may add outcomes that became mature at this exact event.
        The returned legacy-flat list is retained as a concise ledger index;
        the full per-dimension map is the scientific provenance record.
        """
        row = connection.execute(
            "SELECT evidence_json, dimension_evidence_json FROM short_term_belief_history WHERE stb_id = ?",
            (stb_id,),
        ).fetchone()
        if row is None:
            raise PaperMemoryError("LTB cannot resolve dimension evidence for a missing STB")
        evidence_rows = _load_canonical_json(
            row["evidence_json"], label="stored STB evidence_json", expected_type=list
        )
        stb_dimension_evidence = _load_canonical_json(
            row["dimension_evidence_json"],
            label="stored STB dimension_evidence_json",
            expected_type=dict,
        )
        all_stb_ids = self._evidence_ids(evidence_rows)
        validated_stb_evidence = normalize_dimension_evidence(
            stb_dimension_evidence,
            label="stored STB dimension_evidence",
            allowed_ids_by_dimension={key: set(all_stb_ids) for key in DIMENSION_KEYS},
        )
        due_rows = connection.execute(
            """
            SELECT outcome.outcome_id, outcome.fill_id, outcome.horizon
            FROM trade_outcomes AS outcome
            JOIN paper_fill_ledger AS fill ON fill.fill_id = outcome.fill_id
            WHERE fill.run_id = ? AND fill.condition_id = ? AND fill.agent_id = ?
              AND outcome.status = 'matured' AND outcome.available_from_event_id = ?
            ORDER BY outcome.fill_id, outcome.horizon
            """,
            (self.run_id, self.condition_id, agent_id, event_id),
        ).fetchall()
        due_outcome_ids = {str(row["outcome_id"]) for row in due_rows}
        allowed: dict[str, set[str]] = {
            dimension: set(validated_stb_evidence[dimension]["support"])
            | set(validated_stb_evidence[dimension]["contradict"])
            for dimension in DIMENSION_KEYS
        }
        allowed["dim_6"] |= due_outcome_ids
        normalized = normalize_dimension_evidence(
            integration_evidence_by_dimension,
            label="ltb.integration_evidence",
            allowed_ids_by_dimension=allowed,
        )
        cited_outcomes = set(normalized["dim_6"]["support"]) | set(
            normalized["dim_6"]["contradict"]
        )
        cited_outcomes &= due_outcome_ids
        if cited_outcomes != due_outcome_ids:
            raise PaperMemoryError(
                "LTB dim_6 must consume every and only outcome mature at this event"
            )
        flat_evidence: list[dict[str, Any]] = [{"kind": "current_stb", "stb_id": stb_id}]
        flat_evidence.extend(
            {"kind": "trade_outcome", "outcome_id": str(row["outcome_id"]), "dimension": "dim_6"}
            for row in due_rows
        )
        consumptions = self._validate_ltb_integration_evidence(
            connection,
            flat_evidence,
            agent_id=agent_id,
            event_id=event_id,
            stb_id=stb_id,
        )
        return normalized, flat_evidence, consumptions

    @staticmethod
    def _sanitized_stb_evidence_registry(evidence_rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Return an ID/hash-only LTB provenance view without raw source text."""
        registry: list[dict[str, Any]] = []
        for row in evidence_rows:
            kind = row.get("kind")
            if kind == "news":
                item = {
                    "evidence_id": _required_text(row.get("article_id"), "news article_id"),
                    "kind": "news",
                    "payload_sha256": _strict_sha256(row.get("payload_sha256"), "news payload_sha256"),
                }
            elif kind == "community_claim":
                sources = row.get("source_exposure_ids")
                if not isinstance(sources, list) or not sources:
                    raise PaperMemoryError("Community evidence registry requires source exposure IDs")
                item = {
                    "evidence_id": _required_text(row.get("claim_id"), "community claim_id"),
                    "kind": "community_claim",
                    "lineage_sha256": scientific_sha256(dict(row)),
                }
            else:
                raise PaperMemoryError("STB evidence registry has an unsupported kind")
            registry.append(item)
        if len({str(item["evidence_id"]) for item in registry}) != len(registry):
            raise PaperMemoryError("STB evidence registry contains duplicate IDs")
        return registry

    @staticmethod
    def _render_human_log(
        *,
        parent: Mapping[str, str],
        current: Mapping[str, str],
        integration_evidence_by_dimension: Mapping[str, Mapping[str, list[str]]],
    ) -> dict[str, Any]:
        """Pure server renderer; never an LLM input or a caller-controlled value."""
        before = normalize_dimensions(parent, label="human-log parent")
        after = normalize_dimensions(current, label="human-log current")
        evidence = normalize_human_log_evidence(
            integration_evidence_by_dimension,
            label="human-log integration evidence",
        )
        summary = "\n".join(f"{dimension}: {after[dimension]}" for dimension in DIMENSION_KEYS)
        changes = [
            {
                "dimension": dimension,
                "before_sha256": scientific_sha256(before[dimension]),
                "after_sha256": scientific_sha256(after[dimension]),
                "integration_evidence": evidence[dimension],
            }
            for dimension in DIMENSION_KEYS
        ]
        return normalize_human_log({
            "renderer_version": HUMAN_LOG_RENDERER_VERSION,
            "renderer_sha256": HUMAN_LOG_RENDERER_CODE_SHA256,
            "belief_summary": summary,
            "view_change": changes,
        }, label="rendered human log")

    def _validate_ltb_integration_evidence(
        self,
        connection: Any,
        evidence: list[dict[str, Any]],
        *,
        agent_id: str,
        event_id: str,
        stb_id: str,
    ) -> list[tuple[str, str]]:
        """Allow only current-STB lineage and due, mature dim-6 outcomes.

        The current fill is intentionally absent: it is represented by the
        server-built transaction episode.  Accepting free-form evidence here
        would make future H1/H5 values or a second fill path smuggleable.
        """
        if not evidence:
            raise PaperMemoryError("LTB integration evidence must include the current STB")
        current_stb_count = 0
        outcome_ids: set[str] = set()
        outcomes_to_consume: list[tuple[str, str]] = []
        for item in evidence:
            kind = item.get("kind")
            if kind == "current_stb":
                if set(item) != {"kind", "stb_id"} or item.get("stb_id") != stb_id:
                    raise PaperMemoryError("LTB current_stb evidence must reference the exact current STB")
                current_stb_count += 1
                continue
            if kind != "trade_outcome" or set(item) != {"kind", "outcome_id", "dimension"}:
                raise PaperMemoryError("LTB integration evidence has an unapproved source schema")
            if item.get("dimension") != "dim_6":
                raise PaperMemoryError("Price outcomes may directly affect only LTB dim_6")
            outcome_id = item.get("outcome_id")
            if not isinstance(outcome_id, str) or not outcome_id or outcome_id in outcome_ids:
                raise PaperMemoryError("LTB trade-outcome IDs must be unique non-empty strings")
            outcome_ids.add(outcome_id)
            outcome = connection.execute(
                """
                SELECT outcome_id, fill_id, horizon, status, available_from_event_id
                FROM trade_outcomes WHERE outcome_id = ?
                """,
                (outcome_id,),
            ).fetchone()
            if outcome is None or str(outcome["status"]) != "matured":
                raise PaperMemoryError("LTB may use only server-recorded mature outcomes")
            if str(outcome["available_from_event_id"]) != event_id:
                raise PaperMemoryError("LTB may use an outcome only at its due event")
            fill = connection.execute(
                "SELECT agent_id FROM paper_fill_ledger WHERE fill_id = ?", (outcome["fill_id"],)
            ).fetchone()
            if fill is None or str(fill["agent_id"]) != agent_id:
                raise PaperMemoryError("LTB outcome belongs to a different agent")
            outcomes_to_consume.append((str(outcome["fill_id"]), str(outcome["horizon"])))
        if current_stb_count != 1:
            raise PaperMemoryError("LTB must consume the current STB exactly once")
        due_rows = connection.execute(
            """
            SELECT outcome.outcome_id
            FROM trade_outcomes AS outcome
            JOIN paper_fill_ledger AS fill ON fill.fill_id = outcome.fill_id
            WHERE fill.run_id = ? AND fill.condition_id = ? AND fill.agent_id = ?
              AND outcome.status = 'matured' AND outcome.available_from_event_id = ?
            """,
            (self.run_id, self.condition_id, agent_id, event_id),
        ).fetchall()
        due_outcome_ids = {str(row["outcome_id"]) for row in due_rows}
        if outcome_ids != due_outcome_ids:
            raise PaperMemoryError(
                "LTB must consume every and only mature outcome due at this event"
            )
        return outcomes_to_consume

    def _consume_matured_outcome_in_transaction(
        self,
        connection: Any,
        *,
        fill_id: str,
        horizon: str,
        transition_id: str,
        consumed_at_event_id: str,
    ) -> str:
        """Write/check the one allowed consumption edge inside an active transaction."""
        consumption_id = f"outcome-consumption:{fill_id}:{horizon}"
        outcome = connection.execute(
            "SELECT * FROM trade_outcomes WHERE fill_id = ? AND horizon = ?", (fill_id, horizon)
        ).fetchone()
        if outcome is None or str(outcome["status"]) != "matured":
            raise PaperMemoryError("Only matured outcomes may be consumed")
        if str(outcome["available_from_event_id"]) != consumed_at_event_id:
            raise PaperMemoryError("Outcome was consumed before/after its scheduled event")
        self._assert_scoped_reference(
            connection, "ltb_dimension_transitions", "transition_id", transition_id
        )
        fill = connection.execute(
            "SELECT agent_id FROM paper_fill_ledger WHERE fill_id = ?", (fill_id,)
        ).fetchone()
        transition = connection.execute(
            """
            SELECT agent_id, event_id FROM ltb_dimension_transitions
            WHERE transition_id = ?
            """,
            (transition_id,),
        ).fetchone()
        if fill is None or transition is None:
            raise PaperMemoryError("Outcome consumption references a missing fill or transition")
        if str(fill["agent_id"]) != str(transition["agent_id"]):
            raise PaperMemoryError("Outcome may only be consumed by the fill owner's LTB")
        if str(transition["event_id"]) != consumed_at_event_id:
            raise PaperMemoryError("Outcome must be consumed by the LTB transition at its due event")
        self._insert_or_verify(
            connection,
            table="ltb_outcome_consumptions",
            key_column="consumption_id",
            key_value=consumption_id,
            values={
                "consumption_id": consumption_id,
                "fill_id": fill_id,
                "horizon": horizon,
                "transition_id": transition_id,
                "consumed_at_event_id": consumed_at_event_id,
            },
            compare_columns=("transition_id", "consumed_at_event_id"),
        )
        return consumption_id

    def _transaction_episode(self, connection: Any, fill_id: str) -> dict[str, Any]:
        row = connection.execute(
            """
            SELECT fill_id, agent_id, event_id, turn, date, subturn, action,
                   requested_quantity, filled_quantity, executed_price, fee_amount,
                   source_ltb_id, source_stb_id, decision_id, pre_portfolio_json, post_portfolio_json
            FROM paper_fill_ledger
            WHERE fill_id = ?
            """,
            (fill_id,),
        ).fetchone()
        if row is None:
            raise PaperMemoryError(f"Missing current fill for transaction episode: {fill_id}")
        scheduled = self._assert_scheduled_event(
            str(row["event_id"]),
            turn=int(row["turn"]),
            date=str(row["date"]),
            subturn=str(row["subturn"]),
        )
        pre_portfolio = _load_canonical_json(
            row["pre_portfolio_json"], label="stored pre_portfolio_json", expected_type=dict
        )
        execution_state = self._execution_state_from_portfolio(scheduled, pre_portfolio)
        return {
            "fill_id": str(row["fill_id"]),
            "decision_id": str(row["decision_id"]),
            "source_ltb_id": str(row["source_ltb_id"]),
            "source_stb_id": str(row["source_stb_id"]),
            "action": str(row["action"]),
            "requested_quantity": int(row["requested_quantity"]),
            "filled_quantity": int(row["filled_quantity"]),
            "executed_price": float(row["executed_price"]),
            "fee_amount": float(row["fee_amount"]),
            "pre_portfolio": pre_portfolio,
            "post_portfolio": _load_canonical_json(
                row["post_portfolio_json"], label="stored post_portfolio_json", expected_type=dict
            ),
            "feasible_actions": list(execution_state["allowed_actions"]),
            "constraint_forced": len(execution_state["allowed_actions"]) == 1,
            "outcome_status": "pending",
        }

    def _due_event_for_fill(self, fill_id: str, horizon: str) -> str | None:
        if self.event_schedule is None:
            raise PaperMemoryError("Frozen event schedule is unavailable")
        with connect(self.db_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT event_id FROM paper_fill_ledger WHERE fill_id = ?", (fill_id,)
            ).fetchone()
        if row is None:
            raise PaperMemoryError(f"Missing fill while resolving outcome horizon: {fill_id}")
        return self.event_schedule.due_event_id(
            fill_event_id=str(row["event_id"]), horizon=horizon
        )

    def _assert_scheduled_event(
        self,
        event_id: str,
        *,
        turn: int,
        date: str,
        subturn: str | None = None,
    ) -> dict[str, Any]:
        if self.event_schedule is None:
            raise PaperMemoryError(
                "A frozen EventSchedule is required before writing scientific event state"
            )
        scheduled = self.event_schedule.event(event_id)
        if int(scheduled["turn"]) != int(turn) or str(scheduled["date"]) != str(date):
            raise PaperMemoryError("Event ID, turn, or date differs from the frozen schedule")
        if subturn is not None and str(scheduled["subturn"]) != str(subturn):
            raise PaperMemoryError("Event subturn differs from the frozen schedule")
        return scheduled

    @staticmethod
    def _insert_or_verify(
        connection: Any,
        *,
        table: str,
        key_column: str,
        key_value: str,
        values: Mapping[str, Any],
        compare_columns: tuple[str, ...],
    ) -> bool:
        existing = connection.execute(
            f"SELECT * FROM {table} WHERE {key_column} = ?", (key_value,)
        ).fetchone()
        if existing is not None:
            if all(existing[column] == values[column] for column in compare_columns):
                return False
            raise PaperMemoryError(f"Conflicting replay for {table}.{key_column}={key_value}")
        columns = list(values)
        placeholders = ", ".join("?" for _ in columns)
        connection.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
            [values[column] for column in columns],
        )
        return True

    def _stb_id(self, agent_id: str, event_id: str) -> str:
        return f"stb:{self.run_id}:{self.condition_id}:{agent_id}:{event_id}"

    def _initial_portfolio_id(self, agent_id: str) -> str:
        return f"initial-portfolio:{self.run_id}:{self.condition_id}:{agent_id}"

    def _ltb_id(self, agent_id: str, event_id: str) -> str:
        return f"ltb:{self.run_id}:{self.condition_id}:{agent_id}:{event_id}"

    def _fill_id(self, agent_id: str, event_id: str) -> str:
        return f"fill:{self.run_id}:{self.condition_id}:{agent_id}:{event_id}"

    def _decision_id(self, agent_id: str, event_id: str) -> str:
        return f"decision:{self.run_id}:{self.condition_id}:{agent_id}:{event_id}"

    def _analysis_id(self, agent_id: str, event_id: str) -> str:
        return f"analysis:{self.run_id}:{self.condition_id}:{agent_id}:{event_id}"

    def _transition_id(self, agent_id: str, event_id: str) -> str:
        return f"ltb-transition:{self.run_id}:{self.condition_id}:{agent_id}:{event_id}"


def _news_projection_depth(rows: list[Any]) -> int:
    """Infer only the serialized visibility tier, never an agent assignment.

    D1 and D2 deliberately share the event-feed projection.  The sealed
    persona check at the stage adapter proves which of those two tiers owns
    the packet; persistence re-validates that every row is consistently D0
    headline-only or D1/D2 title+summary.
    """
    headline_fields = {"article_id", "payload_sha256", "title", "published_at"}
    summary_fields = headline_fields | {"summary"}
    if not rows:
        raise PaperMemoryError("STB sealed news delivery may not be empty")
    field_sets: list[set[str]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise PaperMemoryError(f"STB news[{index}] must be an object")
        field_sets.append(set(row))
    if all(fields == headline_fields for fields in field_sets):
        return 0
    if all(fields == summary_fields for fields in field_sets):
        return 1
    raise PaperMemoryError("STB news mixes or violates sealed depth projections")


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PaperMemoryError(f"{label} must be a non-empty string")
    return value.strip()


def _strict_positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PaperMemoryError(f"{label} must be a positive integer")
    return value


def _strict_finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PaperMemoryError(f"{label} must be numeric")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise PaperMemoryError(f"{label} must be finite")
    return numeric


def _strict_positive_number(value: Any, label: str) -> float:
    numeric = _strict_finite_number(value, label)
    if numeric <= 0:
        raise PaperMemoryError(f"{label} must be positive")
    return numeric


def _csv_number(value: Any) -> str:
    """Stable human-readable numeric form without scientific notation drift."""
    numeric = _strict_finite_number(value, "CSV numeric field")
    return format(numeric, ".15g")


def _strict_sha256(value: Any, label: str) -> str:
    text = _required_text(value, label).lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise PaperMemoryError(f"{label} must be a SHA-256 hex digest")
    return text


def _load_canonical_json(
    value: Any, *, label: str, expected_type: type | tuple[type, ...]
) -> Any:
    if not isinstance(value, str):
        raise PaperMemoryError(f"{label} must be canonical JSON text")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise PaperMemoryError(f"{label} is not valid JSON") from exc
    if not isinstance(parsed, expected_type):
        raise PaperMemoryError(f"{label} has the wrong JSON type")
    if canonical_json(parsed) != value:
        raise PaperMemoryError(f"{label} is not canonical JSON")
    return parsed


def _parse_iso_date(value: Any, label: str) -> str:
    text = _required_text(value, label)
    try:
        return calendar_date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise PaperMemoryError(f"{label} must use YYYY-MM-DD") from exc
