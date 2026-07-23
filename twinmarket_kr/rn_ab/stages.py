"""Typed, sealed stage packets for the RN STB → decision → fill → LTB DAG.

The public constructors deliberately accept registries/stores, not caller-made
"sealed" dictionaries.  This prevents an otherwise well-formed packet from
substituting a future price, swapped article, raw portfolio, or unowned
community claim before it reaches an LLM prompt.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date as calendar_date
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence

from twinmarket_kr.rn_ab.memory import (
    DIMENSION_KEYS,
    EventSchedule,
    PaperMemoryError,
    PaperMemoryStore,
    normalize_dimension_evidence,
    normalize_dimensions,
    scientific_sha256,
)
from twinmarket_kr.rn_ab.news import NewsBundleError, SealedNewsRegistry
from twinmarket_kr.rn_ab.stage_inputs import SealedStageInputRegistry, StageInputRegistryError


class StageContractError(ValueError):
    pass


class CommunityClaimVerifier(Protocol):
    """The isolated RN community service contract consumed by STB staging."""

    def validate_claims_for_agent(
        self,
        *,
        agent_id: str,
        event_id: str,
        claims: Sequence[Mapping[str, Any]],
    ) -> Sequence[Mapping[str, Any]]:
        ...


_FORBIDDEN_STB_KEYS = {
    "previous_ltb",
    "belief_summary",
    "view_change",
    "portfolio",
    "portfolio_summary",
    "order_history",
    "action_reason",
    "current_fill",
    "fill_id",
    "trade_outcome",
    "target_label",
    "individuals_flow",
}


def _reject_forbidden(value: Mapping[str, Any], *, stage: str, forbidden: set[str]) -> None:
    present = forbidden & set(value)
    if present:
        raise StageContractError(f"{stage} packet contains prohibited fields: {sorted(present)}")


def _exact_mapping(value: Any, *, required: set[str], stage: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise StageContractError(f"{stage} packet must be an object")
    missing = required - set(value)
    unknown = set(value) - required
    if missing or unknown:
        raise StageContractError(
            f"{stage} packet schema mismatch missing={sorted(missing)} unknown={sorted(unknown)}"
        )
    return dict(value)


@dataclass(frozen=True)
class CurrentEvidencePacket:
    """Current-only evidence after exact real-news and exposure verification.

    ``news_depth`` is execution metadata used to prove that D0 received the
    headline-only projection while D1/D2 received the sealed summary.  It is
    deliberately not copied into :meth:`as_dict`; the scientific evidence
    packet records the fields actually shown, and the stage adapter separately
    checks this metadata against the sealed persona before prompting.
    """

    event_id: str
    date: str
    subturn: str
    news: tuple[Mapping[str, str], ...]
    community_claims: tuple[Mapping[str, Any], ...]
    depth2_search_results: tuple[Mapping[str, str], ...] = ()
    news_depth: int = 1

    def __post_init__(self) -> None:
        depth = _validated_news_depth(self.news_depth)
        headline_fields = {"article_id", "payload_sha256", "title", "published_at"}
        expected_fields = headline_fields if depth == 0 else headline_fields | {"summary"}
        frozen_news: list[Mapping[str, str]] = []
        for index, item in enumerate(self.news):
            if not isinstance(item, Mapping) or set(item) != expected_fields:
                raise StageContractError(
                    f"STB news[{index}] violates the exact D{depth} projection schema"
                )
            frozen_news.append(MappingProxyType(dict(item)))
        frozen_claims: list[Mapping[str, Any]] = []
        for index, item in enumerate(self.community_claims):
            if not isinstance(item, Mapping):
                raise StageContractError(f"STB community_claims[{index}] must be an object")
            frozen_claims.append(MappingProxyType(dict(item)))
        if depth != 2 and self.depth2_search_results:
            raise StageContractError("Only Depth 2 may receive recent-search results")
        frozen_search: list[Mapping[str, str]] = []
        for index, item in enumerate(self.depth2_search_results):
            if not isinstance(item, Mapping) or set(item) != headline_fields | {"summary"}:
                raise StageContractError(
                    f"STB depth2_search_results[{index}] violates the exact D2 projection schema"
                )
            frozen_search.append(MappingProxyType(dict(item)))
        current_ids = {str(item["article_id"]) for item in frozen_news}
        search_ids = [str(item["article_id"]) for item in frozen_search]
        if len(search_ids) != len(set(search_ids)) or current_ids & set(search_ids):
            raise StageContractError(
                "Depth-2 recent-search results must be unique and exclude current-event news"
            )
        object.__setattr__(self, "news", tuple(frozen_news))
        object.__setattr__(self, "community_claims", tuple(frozen_claims))
        object.__setattr__(self, "depth2_search_results", tuple(frozen_search))

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        agent_id: str,
        event_schedule: EventSchedule,
        news_registry: SealedNewsRegistry,
        stage_input_registry: SealedStageInputRegistry,
        claim_verifier: CommunityClaimVerifier,
        expected_news_depth: int,
        expected_depth2_search_results: Sequence[Mapping[str, Any]] = (),
    ) -> "CurrentEvidencePacket":
        """Build a packet only from sealed deliveries and owned claim edges."""
        news_depth = _validated_news_depth(expected_news_depth)
        _reject_forbidden(value, stage="STB", forbidden=_FORBIDDEN_STB_KEYS)
        data = _exact_mapping(
            value,
            required={
                "event_id",
                "date",
                "subturn",
                "news",
                "depth2_search_results",
                "community_claims",
            },
            stage="STB",
        )
        event_id = _required_text(data["event_id"], "event_id")
        date = _parse_date(data["date"], "date")
        subturn = _required_text(data["subturn"], "subturn").lower()
        if subturn not in {"am", "pm"}:
            raise StageContractError("STB subturn must be am or pm")
        try:
            stage_input_registry.assert_matches_schedule(event_schedule)
            stage_input = stage_input_registry.event(event_id)
        except StageInputRegistryError as exc:
            raise StageContractError(f"STB stage inputs are invalid: {exc}") from exc
        frozen = event_schedule.event(event_id)
        if date != str(frozen["date"]) or subturn != str(frozen["subturn"]):
            raise StageContractError("STB event/date/subturn differs from the frozen event schedule")
        if (
            date != stage_input.date
            or subturn != stage_input.subturn
            or event_id != stage_input.event_id
        ):
            raise StageContractError("STB event differs from the sealed timestamp-level stage inputs")
        if (
            not isinstance(data["news"], list)
            or not isinstance(data["depth2_search_results"], list)
            or not isinstance(data["community_claims"], list)
        ):
            raise StageContractError(
                "STB news, depth2_search_results, and community_claims must be lists"
            )
        try:
            news = tuple(
                news_registry.validate_delivery(
                    event_id=event_id,
                    delivered=data["news"],
                    cutoff_timestamp=stage_input.news_cutoff_timestamp,
                    news_depth=news_depth,
                )
            )
        except NewsBundleError as exc:
            raise StageContractError(f"STB real-news delivery is invalid: {exc}") from exc
        expected_search = [dict(item) for item in expected_depth2_search_results]
        if news_depth != 2 and (data["depth2_search_results"] or expected_search):
            raise StageContractError("Only Depth 2 may receive recent-search results")
        if data["depth2_search_results"] != expected_search:
            raise StageContractError(
                "STB Depth-2 search results differ from the generated registry projection"
            )
        raw_claims = [_validate_claim(item) for item in data["community_claims"]]
        try:
            verified_claims = claim_verifier.validate_claims_for_agent(
                agent_id=_required_text(agent_id, "agent_id"),
                event_id=event_id,
                claims=raw_claims,
            )
        except Exception as exc:  # verifier errors must be fail-closed at the stage boundary
            raise StageContractError(f"STB community-claim provenance is invalid: {exc}") from exc
        claims = tuple(
            _validate_claim(
                item.stage_projection() if hasattr(item, "stage_projection") else item
            )
            for item in verified_claims
        )
        if list(claims) != raw_claims:
            raise StageContractError("Community verifier may validate claims but may not rewrite them")
        return cls(
            event_id=event_id,
            date=date,
            subturn=subturn,
            news=news,
            community_claims=claims,
            depth2_search_results=tuple(expected_search),
            news_depth=news_depth,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "date": self.date,
            "subturn": self.subturn,
            "news": [dict(item) for item in self.news],
            "depth2_search_results": [
                dict(item) for item in self.depth2_search_results
            ],
            "community_claims": [dict(item) for item in self.community_claims],
        }

    @property
    def sha256(self) -> str:
        return scientific_sha256(self.as_dict())


def build_decision_packet(
    *,
    event_schedule: EventSchedule,
    stage_input_registry: SealedStageInputRegistry,
    store: PaperMemoryStore,
    agent_id: str,
    event_id: str,
    previous_ltb: Mapping[str, Any],
    current_stb: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the sole decision input from sealed event/market registries.

    Neither a raw market mapping nor an as-of timestamp is accepted here.  The
    caller can choose an event ID only; the registry then supplies the exact
    timestamp-level field boundary and numeric market snapshot.
    """
    for label, belief in (("previous_ltb", previous_ltb), ("current_stb", current_stb)):
        _reject_forbidden(belief, stage=label, forbidden={"belief_summary", "view_change"})
    ltb = normalize_dimensions(previous_ltb, label="previous_ltb")
    stb = normalize_dimensions(current_stb, label="current_stb")
    requested_event_id = _required_text(event_id, "event_id")
    try:
        stage_input_registry.assert_matches_schedule(event_schedule)
        stage_input = stage_input_registry.event(requested_event_id)
    except StageInputRegistryError as exc:
        raise StageContractError(f"Decision stage inputs are invalid: {exc}") from exc
    frozen = event_schedule.event(requested_event_id)
    event_binding = {
        "event_id": str(frozen["event_id"]),
        "date": str(frozen["date"]),
        "subturn": str(frozen["subturn"]),
        "market_feature_as_of": stage_input.market_feature_as_of,
        "execution_price": _positive_number(frozen["execution_price"], "sealed execution_price"),
    }
    if (
        stage_input.date != event_binding["date"]
        or stage_input.subturn != event_binding["subturn"]
        or stage_input.event_id != event_binding["event_id"]
    ):
        raise StageContractError("Decision event differs from the sealed stage input registry")
    market_data = stage_input.market.stage_projection(subturn=stage_input.subturn)
    if _required_text(market_data["subturn"], "market.subturn").lower() != event_binding["subturn"]:
        raise StageContractError("Decision market subturn differs from the frozen event")
    if market_data["as_of_timestamp"] != event_binding["market_feature_as_of"]:
        raise StageContractError("Decision market as-of date differs from the frozen event")
    reference_price = _positive_number(market_data["reference_price"], "market.reference_price")
    if abs(reference_price - event_binding["execution_price"]) > 1e-9:
        raise StageContractError("Decision reference price differs from sealed execution price")
    _positive_number(market_data["previous_close"], "market.previous_close")
    _positive_number(market_data["open_price"], "market.open_price")

    if not isinstance(store, PaperMemoryStore):
        raise StageContractError("Decision stage requires the run-scoped PaperMemoryStore")
    if store.event_schedule is None or tuple(store.event_schedule.events) != tuple(event_schedule.events):
        raise StageContractError("Decision stage store is bound to a different frozen EventSchedule")
    try:
        execution = store.execution_state_for_event(
            agent_id=_required_text(agent_id, "agent_id"),
            event_id=requested_event_id,
        )
    except (PaperMemoryError, ValueError) as exc:
        raise StageContractError(f"Decision execution state is invalid: {exc}") from exc
    allowed_actions = execution["allowed_actions"]
    if (
        not isinstance(allowed_actions, list)
        or not allowed_actions
        or allowed_actions != [action for action in ("buy", "sell") if action in allowed_actions]
    ):
        raise StageContractError("RN decision packet requires a non-empty ordered feasible buy/sell subset")
    _nonnegative_number(execution["available_cash"], "execution_state.available_cash")
    for name in ("current_quantity", "max_buy_quantity", "max_sell_quantity"):
        _nonnegative_int(execution[name], f"execution_state.{name}")
    announced_price = _positive_number(execution["announced_price"], "execution_state.announced_price")
    if abs(announced_price - reference_price) > 1e-9:
        raise StageContractError("Execution announced price differs from the sealed reference price")
    if not isinstance(execution["price_label"], str) or not execution["price_label"].strip():
        raise StageContractError("Execution price_label must be a non-empty string")
    packet = {
        "event": event_binding,
        "previous_ltb": ltb,
        "current_stb": stb,
        "market": market_data,
        "execution_state": execution,
    }
    packet["input_sha256"] = scientific_sha256(packet)
    return packet


def build_post_fill_ltb_packet(
    *,
    store: PaperMemoryStore,
    agent_id: str,
    event_id: str,
    turn: int,
    parent_ltb_id: str,
    stb_id: str,
    fill_id: str,
) -> dict[str, Any]:
    """Build a post-fill packet solely by reading the canonical paper ledger."""
    context = store.post_fill_stage_inputs(
        agent_id=agent_id,
        event_id=event_id,
        turn=turn,
        parent_ltb_id=parent_ltb_id,
        stb_id=stb_id,
        fill_id=fill_id,
    )
    ltb = normalize_dimensions(context["previous_ltb"], label="previous_ltb")
    raw_stb = _exact_mapping(
        context["current_stb"],
        required={"dimensions", "dimension_evidence"},
        stage="post-fill current_stb",
    )
    stb = normalize_dimensions(raw_stb["dimensions"], label="current_stb")
    sanitized_registry = context["sanitized_evidence_registry"]
    if not isinstance(sanitized_registry, list):
        raise StageContractError("Post-fill sanitized_evidence_registry must be a list")
    registry_ids: set[str] = set()
    for item in sanitized_registry:
        entry = _exact_mapping(
            item,
            required={"evidence_id", "kind", "payload_sha256"}
            if isinstance(item, Mapping) and item.get("kind") == "news"
            else {"evidence_id", "kind", "lineage_sha256"},
            stage="post-fill sanitized evidence",
        )
        evidence_id = _required_text(entry["evidence_id"], "sanitized evidence_id")
        if evidence_id in registry_ids:
            raise StageContractError("Post-fill sanitized evidence IDs must be unique")
        registry_ids.add(evidence_id)
    stb_dimension_evidence = normalize_dimension_evidence(
        raw_stb["dimension_evidence"],
        label="post-fill current_stb.dimension_evidence",
        allowed_ids_by_dimension={key: set(registry_ids) for key in DIMENSION_KEYS},
    )
    episode = _validate_generated_episode(context["transaction_episode"])
    outcomes = [_validate_generated_outcome(item) for item in context["eligible_price_outcomes_dim_6_only"]]
    packet = {
        "event": dict(context["event"]),
        "lineage": {
            "parent_ltb_id": parent_ltb_id,
            "stb_id": stb_id,
            "decision_id": episode["decision_id"],
            "fill_id": fill_id,
        },
        "previous_ltb": ltb,
        "current_stb": {"dimensions": stb, "dimension_evidence": stb_dimension_evidence},
        "transaction_episode": episode,
        "eligible_price_outcomes_dim_6_only": outcomes,
        "sanitized_evidence_registry": [dict(item) for item in sanitized_registry],
    }
    packet["input_sha256"] = scientific_sha256(packet)
    return packet


def integration_evidence_from_post_fill_packet(
    packet: Mapping[str, Any],
) -> dict[str, dict[str, list[str]]]:
    """Return a valid deterministic local/mock LTB-evidence projection.

    A real model returns this same shape and may select an allowed subset.  The
    deterministic projection is useful only for local dry runs: it preserves
    each STB polarity and consumes every newly due outcome as dim_6 support.
    """
    data = _exact_mapping(
        packet,
        required={
            "event",
            "lineage",
            "previous_ltb",
            "current_stb",
            "transaction_episode",
            "eligible_price_outcomes_dim_6_only",
            "sanitized_evidence_registry",
            "input_sha256",
        },
        stage="post-fill LTB",
    )
    lineage = _exact_mapping(
        data["lineage"],
        required={"parent_ltb_id", "stb_id", "decision_id", "fill_id"},
        stage="post-fill lineage",
    )
    current_stb = _exact_mapping(
        data["current_stb"],
        required={"dimensions", "dimension_evidence"},
        stage="post-fill current_stb",
    )
    normalized = normalize_dimension_evidence(
        current_stb["dimension_evidence"],
        label="post-fill current_stb.dimension_evidence",
        allowed_ids_by_dimension={key: _packet_evidence_ids(data["sanitized_evidence_registry"]) for key in DIMENSION_KEYS},
    )
    result = {
        dimension: {
            "support": list(normalized[dimension]["support"]),
            "contradict": list(normalized[dimension]["contradict"]),
        }
        for dimension in DIMENSION_KEYS
    }
    for outcome in data["eligible_price_outcomes_dim_6_only"]:
        checked = _validate_generated_outcome(outcome)
        result["dim_6"]["support"].append(checked["outcome_id"])
    return result


def serialize_untrusted_text(value: str, *, source_kind: str) -> dict[str, str]:
    """Make source text explicit data, never an instruction-bearing role string."""
    return {
        "source_kind": _required_text(source_kind, "source_kind"),
        "untrusted_text": _required_text(value, "untrusted text"),
    }


def _validate_claim(value: Any) -> dict[str, Any]:
    data = _exact_mapping(
        value,
        required={"claim_id", "claim_text", "stance", "source_exposure_ids"},
        stage="community claim",
    )
    source_ids = data["source_exposure_ids"]
    if not isinstance(source_ids, list) or not source_ids:
        raise StageContractError("Community claim requires non-empty source exposure IDs")
    normalized_sources = [_required_text(item, "source_exposure_id") for item in source_ids]
    if len(normalized_sources) != len(set(normalized_sources)):
        raise StageContractError("Community claim cannot repeat one exposure ID")
    return {
        "claim_id": _required_text(data["claim_id"], "claim_id"),
        "claim_text": _required_text(data["claim_text"], "claim_text"),
        "stance": _required_text(data["stance"], "stance"),
        "source_exposure_ids": normalized_sources,
    }


def _packet_evidence_ids(value: Any) -> set[str]:
    if not isinstance(value, list):
        raise StageContractError("Post-fill sanitized_evidence_registry must be a list")
    result: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            raise StageContractError("Post-fill sanitized evidence entry must be an object")
        kind = item.get("kind")
        required = (
            {"evidence_id", "kind", "payload_sha256"}
            if kind == "news"
            else {"evidence_id", "kind", "lineage_sha256"}
            if kind == "community_claim"
            else set()
        )
        entry = _exact_mapping(item, required=required, stage="post-fill sanitized evidence")
        evidence_id = _required_text(entry["evidence_id"], "sanitized evidence_id")
        if evidence_id in result:
            raise StageContractError("Post-fill sanitized evidence IDs must be unique")
        result.add(evidence_id)
    return result


def _validate_generated_episode(value: Any) -> dict[str, Any]:
    data = _exact_mapping(
        value,
        required={
            "fill_id",
            "decision_id",
            "source_ltb_id",
            "source_stb_id",
            "action",
            "requested_quantity",
            "filled_quantity",
            "executed_price",
            "fee_amount",
            "pre_portfolio",
            "post_portfolio",
            "feasible_actions",
            "constraint_forced",
            "outcome_status",
        },
        stage="transaction_episode",
    )
    if data["action"] not in {"buy", "sell"}:
        raise StageContractError("Transaction episode action must be buy or sell")
    requested = _positive_int(data["requested_quantity"], "episode.requested_quantity")
    filled = _positive_int(data["filled_quantity"], "episode.filled_quantity")
    if requested != filled:
        raise StageContractError("Transaction episode must be a positive full fill")
    _positive_number(data["executed_price"], "episode.executed_price")
    if _finite_number(data["fee_amount"], "episode.fee_amount") != 0.0:
        raise StageContractError("Transaction episode fee must be zero")
    if not isinstance(data["pre_portfolio"], Mapping) or not isinstance(data["post_portfolio"], Mapping):
        raise StageContractError("Transaction episode portfolio snapshots must be structured mappings")
    feasible_actions = data["feasible_actions"]
    if (
        not isinstance(feasible_actions, list)
        or not feasible_actions
        or feasible_actions != [action for action in ("buy", "sell") if action in feasible_actions]
        or str(data["action"]) not in feasible_actions
    ):
        raise StageContractError("Transaction episode feasible_actions is invalid")
    if not isinstance(data["constraint_forced"], bool) or data["constraint_forced"] != (len(feasible_actions) == 1):
        raise StageContractError("Transaction episode constraint_forced is invalid")
    if data["outcome_status"] != "pending":
        raise StageContractError("Current fill episode must mark future outcomes as pending")
    return {
        "fill_id": _required_text(data["fill_id"], "episode.fill_id"),
        "decision_id": _required_text(data["decision_id"], "episode.decision_id"),
        "source_ltb_id": _required_text(data["source_ltb_id"], "episode.source_ltb_id"),
        "source_stb_id": _required_text(data["source_stb_id"], "episode.source_stb_id"),
        "action": str(data["action"]),
        "requested_quantity": requested,
        "filled_quantity": filled,
        "executed_price": _positive_number(data["executed_price"], "episode.executed_price"),
        "fee_amount": 0.0,
        "pre_portfolio": dict(data["pre_portfolio"]),
        "post_portfolio": dict(data["post_portfolio"]),
        "feasible_actions": list(feasible_actions),
        "constraint_forced": bool(data["constraint_forced"]),
        "outcome_status": "pending",
    }


def _validate_generated_outcome(value: Any) -> dict[str, Any]:
    data = _exact_mapping(
        value,
        required={
            "outcome_id",
            "fill_id",
            "horizon",
            "mark_price",
            "observed_event_id",
            "entry_action",
            "entry_price",
            "action_aligned_markout",
            "source_ltb_id",
            "source_stb_id",
        },
        stage="price outcome",
    )
    if data["horizon"] not in {"next_turn", "h1", "h5"}:
        raise StageContractError("Outcome horizon is invalid")
    entry_action = _required_text(data["entry_action"], "outcome.entry_action")
    if entry_action not in {"buy", "sell"}:
        raise StageContractError("Outcome entry_action must be buy or sell")
    entry_price = _positive_number(data["entry_price"], "outcome.entry_price")
    mark_price = _positive_number(data["mark_price"], "outcome.mark_price")
    expected_markout = (mark_price - entry_price) / entry_price
    if entry_action == "sell":
        expected_markout = -expected_markout
    if abs(_finite_number(data["action_aligned_markout"], "outcome.action_aligned_markout") - expected_markout) > 1e-12:
        raise StageContractError("Outcome action_aligned_markout differs from entry and mark prices")
    return {
        "outcome_id": _required_text(data["outcome_id"], "outcome.outcome_id"),
        "fill_id": _required_text(data["fill_id"], "outcome.fill_id"),
        "horizon": str(data["horizon"]),
        "mark_price": mark_price,
        "observed_event_id": _required_text(data["observed_event_id"], "outcome.observed_event_id"),
        "entry_action": entry_action,
        "entry_price": entry_price,
        "action_aligned_markout": expected_markout,
        "source_ltb_id": _required_text(data["source_ltb_id"], "outcome.source_ltb_id"),
        "source_stb_id": _required_text(data["source_stb_id"], "outcome.source_stb_id"),
    }


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StageContractError(f"{label} must be a non-empty string")
    return value.strip()


def _validated_news_depth(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in {0, 1, 2}:
        raise StageContractError("news_depth must be one of 0, 1, or 2")
    return value


def _parse_date(value: Any, label: str) -> str:
    text = _required_text(value, label)
    try:
        return calendar_date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise StageContractError(f"{label} must be YYYY-MM-DD") from exc


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StageContractError(f"{label} must be a numeric value")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise StageContractError(f"{label} must be finite")
    return numeric


def _positive_number(value: Any, label: str) -> float:
    numeric = _finite_number(value, label)
    if numeric <= 0:
        raise StageContractError(f"{label} must be finite and positive")
    return numeric


def _nonnegative_number(value: Any, label: str) -> float:
    numeric = _finite_number(value, label)
    if numeric < 0:
        raise StageContractError(f"{label} must be finite and non-negative")
    return numeric


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise StageContractError(f"{label} must be a positive integer")
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StageContractError(f"{label} must be a non-negative integer")
    return value
