"""Versioned, single-source prompt contracts for the RN stage pipeline.

The RN templates are deliberately Korean prose, but their field semantics must
not drift from the original 0720/TwinMarket belief and trading contracts.  This
module keeps the machine-relevant parts in one place so the prompt payload,
response validator, and tests use the same definitions.
"""
from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping


PROMPT_CONTRACT_VERSION = "rn-prompt-contract-v4"

BELIEF_DIMENSION_FIELDS = ("dim_1", "dim_2", "dim_3", "dim_4", "dim_5", "dim_6")
# These are server-only compatibility projections of an accepted LTB state.
# They are deliberately absent from every model input and model-output schema.
BELIEF_NARRATIVE_FIELDS = ("belief_summary", "view_change")

# These are the canonical meanings from the legacy runtime update prompt.  The
# limits remain run-scoped, because the resolved study manifest is the source
# of truth for the actual values used in a run.
BELIEF_DIMENSION_MEANINGS: Mapping[str, str] = MappingProxyType(
    {
        "dim_1": "향후 약 1개월 삼성전자 주가 방향 전망",
        "dim_2": "현재 삼성전자 valuation이 싸다·비싸다·적정하다에 대한 관점과 근거",
        "dim_3": "금리·환율·경기·반도체 업황 등 거시환경 판단",
        "dim_4": "삼성전자를 둘러싼 시장심리와 투자자 분위기",
        "dim_5": "이번 뉴스·community를 접한 해석과 깨달음",
        "dim_6": "최근 자기 투자 판단의 적절성과 반복 오류에 대한 자기평가",
    }
)

ANALYSIS_DIRECTIONAL_STANCES = frozenset({"buy", "sell", "uncertain"})
ANALYSIS_CONFIDENCE_LEVELS = frozenset({"low", "medium", "high"})
ANALYSIS_REFERENCE_FIELDS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "previous_ltb": frozenset(BELIEF_DIMENSION_FIELDS),
        "current_stb": frozenset(BELIEF_DIMENSION_FIELDS),
        "market": frozenset(
            {"reference_price", "previous_close", "open_price", "subturn", "as_of_timestamp"}
        ),
        "execution_state": frozenset(
            {
                "available_cash",
                "current_quantity",
                "max_buy_quantity",
                "max_sell_quantity",
                "allowed_actions",
                "price_label",
                "announced_price",
            }
        ),
    }
)
ANALYSIS_REQUIRED_REFERENCE_SOURCES = (
    "previous_ltb",
    "current_stb",
    "market",
    "execution_state",
)
ANALYSIS_LEGACY_TEXT_FIELDS = (
    "market_view",
    "valuation_view",
    "technical_view",
    "news_view",
    "portfolio_view",
)
ANALYSIS_LEGACY_FLEXIBLE_FIELDS = ("key_risks", "opportunity", "caution")
ANALYSIS_OUTPUT_FIELDS = (
    *ANALYSIS_LEGACY_TEXT_FIELDS,
    *ANALYSIS_LEGACY_FLEXIBLE_FIELDS,
    "confidence",
    "directional_stance",
    "evidence_references",
)


def belief_dimension_contract(limits: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Materialize the run-scoped six-dimension contract for a prompt payload."""

    if set(limits) != set(BELIEF_DIMENSION_FIELDS):
        raise ValueError("belief limits must contain exactly the six RN dimension fields")
    result: list[dict[str, Any]] = []
    for field in BELIEF_DIMENSION_FIELDS:
        limit = limits[field]
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError(f"belief limit for {field} must be a positive integer")
        result.append(
            {
                "field": field,
                "meaning": BELIEF_DIMENSION_MEANINGS[field],
                "character_limit": limit,
            }
        )
    return result


def belief_output_contract(*, limits: Mapping[str, Any], evidence_field: str) -> dict[str, Any]:
    """Return the exact scientific STB/LTB model-output shape.

    Human-readable compatibility fields are derived only after a validated LTB
    state is committed.  They must never appear in a model prompt or raw
    response, otherwise a free-form summary could become hidden causal state.
    """

    if evidence_field not in {"dimension_evidence", "integration_evidence"}:
        raise ValueError("belief evidence field must be dimension_evidence or integration_evidence")
    return {
        "contract_version": PROMPT_CONTRACT_VERSION,
        "required_top_level_keys": [
            *BELIEF_DIMENSION_FIELDS,
            evidence_field,
        ],
        "additional_top_level_keys_allowed": False,
        "dimension_contract": belief_dimension_contract(limits),
        "evidence": {
            "field": evidence_field,
            "required_dimension_keys": list(BELIEF_DIMENSION_FIELDS),
            "required_relation_keys": ["support", "contradict"],
            "ids_may_be_empty": True,
            "duplicate_ids_forbidden": True,
            "same_id_in_both_relations_forbidden": True,
        },
    }


def analysis_output_contract() -> dict[str, Any]:
    """Return the legacy analysis shape plus the two required RN additions."""

    return {
        "contract_version": PROMPT_CONTRACT_VERSION,
        "required_top_level_keys": list(ANALYSIS_OUTPUT_FIELDS),
        "additional_top_level_keys_allowed": False,
        "field_types": {
            **{field: "non_empty_string" for field in ANALYSIS_LEGACY_TEXT_FIELDS},
            **{
                field: "non_empty_string_or_non_empty_string_array"
                for field in ANALYSIS_LEGACY_FLEXIBLE_FIELDS
            },
        },
        "directional_stance_values": sorted(ANALYSIS_DIRECTIONAL_STANCES),
        "confidence_values": sorted(ANALYSIS_CONFIDENCE_LEVELS),
        "evidence_references": {
            "minimum_items": len(ANALYSIS_REQUIRED_REFERENCE_SOURCES),
            "item_exact_keys": ["source", "field"],
            "required_sources": list(ANALYSIS_REQUIRED_REFERENCE_SOURCES),
            "allowed_fields_by_source": {
                source: sorted(fields) for source, fields in ANALYSIS_REFERENCE_FIELDS.items()
            },
            "duplicate_source_field_pairs_forbidden": True,
        },
    }


def decision_output_contract() -> dict[str, Any]:
    """Return the one output shape that may authorize the deterministic fill."""

    return {
        "contract_version": PROMPT_CONTRACT_VERSION,
        "required_top_level_keys": ["action", "requested_quantity", "reason", "risk_control"],
        "additional_top_level_keys_allowed": False,
        "action_values": ["buy", "sell"],
        "requested_quantity": {"type": "positive_integer"},
        "text_max_characters": {"reason": 1_000, "risk_control": 1_000},
    }
