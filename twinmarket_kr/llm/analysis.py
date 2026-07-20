from __future__ import annotations

import json
from typing import Any, Callable

from twinmarket_kr.llm.belief import load_prompt
from twinmarket_kr.llm.client import OpenRouterClient, response_content, stable_llm_seed
from twinmarket_kr.llm.validation import (
    LLMValidationError,
    build_validation_retry_prompt,
    normalize_string_list,
    record_validation_failure,
    valid_string_list,
)


NEWS_INTERPRETATION_KEYS = (
    "selected_news",
    "news_sentiment",
    "short_term_impact",
    "long_term_impact",
    "persona_interpretation",
    "confidence",
    "reason",
)

MARKET_ANALYSIS_KEYS = (
    "market_view",
    "valuation_view",
    "technical_view",
    "news_view",
    "portfolio_view",
    "key_risks",
    "opportunity",
    "caution",
    "confidence",
)

DEPTH2_PRE_SEARCH_KEYS = (
    "key_findings",
    "curiosity_points",
    "search_rationale",
    "search_keywords",
)

DEPTH2_POST_SEARCH_KEYS = (
    "new_findings",
    "view_change",
    "view_change_detail",
    "unresolved_questions",
)


class AnalysisValidationError(LLMValidationError):
    pass


DEPTH2_PRE_SEARCH_SCHEMA = """
{
  "key_findings": ["비어 있지 않은 문자열", "..."],
  "curiosity_points": ["비어 있지 않은 문자열", "..."],
  "search_rationale": "비어 있지 않은 문자열",
  "search_keywords": ["3~8개의 검색 키워드", "...", "..."]
}
"""

DEPTH2_POST_SEARCH_SCHEMA = """
{
  "new_findings": ["새로 확인한 내용"],
  "view_change": "강화 또는 수정 또는 반전 또는 유지",
  "view_change_detail": "비어 있지 않은 문자열",
  "unresolved_questions": ["아직 불명확한 쟁점"]
}
새로운 내용이나 미해결 쟁점이 없으면 해당 배열은 []로 출력할 수 있습니다.
"""


def _nonempty_text_or_string_list(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value) and isinstance(value, list) and all(
        isinstance(item, str) and item.strip() for item in value
    )


def parse_json_object(content: str, required_keys: tuple[str, ...], label: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    data = json.loads(text)
    missing = [key for key in required_keys if key not in data]
    if missing:
        raise ValueError(f"{label} JSON missing keys: {missing}")
    return data


def parse_json_loose(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        data = json.loads(text or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


async def _required_json_response(
    *,
    client: OpenRouterClient,
    prompt: str,
    required_keys: tuple[str, ...],
    label: str,
    seed: int | None,
    validation_attempts: int = 4,
    validator: Callable[[dict[str, Any]], list[str]] | None = None,
    normalizer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    schema_hint: str = "요청된 모든 JSON 키와 자료형을 정확히 지키세요.",
) -> dict[str, Any]:
    if validation_attempts < 1:
        raise ValueError("validation_attempts must be at least 1")
    invalid_history: list[list[str]] = []
    current_prompt = prompt
    for attempt in range(1, validation_attempts + 1):
        attempt_seed = stable_llm_seed(seed or 0, label, attempt)
        response = await client.chat(
            [{"role": "user", "content": current_prompt}],
            response_format={"type": "json_object"},
            temperature=0.2 if attempt == 1 else 0.1,
            seed=attempt_seed,
            audit_label=label,
        )
        raw_content = response_content(response) or "{}"
        data = parse_json_loose(raw_content)
        if normalizer is not None:
            data = normalizer(data)
        missing = [key for key in required_keys if key not in data]
        validation_errors = validator(data) if not missing and validator is not None else []
        if not missing and not validation_errors:
            data["generation_attempts"] = attempt
            return data
        errors = [*[f"missing:{key}" for key in missing], *validation_errors]
        invalid_history.append(errors)
        record_validation_failure(
            label=label,
            attempt=attempt,
            errors=errors,
            raw_content=raw_content,
            seed=attempt_seed,
        )
        current_prompt = build_validation_retry_prompt(
            prompt,
            errors=errors,
            schema_hint=schema_hint,
        )
    raise AnalysisValidationError(
        f"{label} did not produce all required JSON keys after "
        f"{validation_attempts} attempts: {invalid_history}"
    )


def normalize_depth2_pre_search(value: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(value)
    for key in ("key_findings", "curiosity_points", "search_keywords"):
        if key in normalized:
            normalized[key] = normalize_string_list(normalized[key])
    if isinstance(normalized.get("search_keywords"), list) and all(
        isinstance(item, str) for item in normalized["search_keywords"]
    ):
        normalized["search_keywords"] = normalized["search_keywords"][:8]
    return normalized


def validate_depth2_pre_search(value: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for key in ("key_findings", "curiosity_points"):
        if not valid_string_list(value.get(key), allow_empty=False):
            errors.append(f"{key}:requires_nonempty_string_list")
    if not isinstance(value.get("search_rationale"), str) or not value[
        "search_rationale"
    ].strip():
        errors.append("search_rationale:requires_nonempty_string")
    keywords = value.get("search_keywords")
    if not valid_string_list(keywords, allow_empty=False) or not 3 <= len(keywords) <= 8:
        errors.append("search_keywords:requires_3_to_8_strings")
    return errors


def normalize_depth2_post_search(value: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(value)
    for key in ("new_findings", "unresolved_questions"):
        if key in normalized:
            normalized[key] = normalize_string_list(normalized[key])
    return normalized


def validate_depth2_post_search(value: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not valid_string_list(value.get("new_findings"), allow_empty=True):
        errors.append("new_findings:requires_string_list")
    if value.get("view_change") not in {"강화", "수정", "반전", "유지"}:
        errors.append("view_change:invalid")
    if not isinstance(value.get("view_change_detail"), str) or not value[
        "view_change_detail"
    ].strip():
        errors.append("view_change_detail:requires_nonempty_string")
    if not valid_string_list(value.get("unresolved_questions"), allow_empty=True):
        errors.append("unresolved_questions:requires_string_list")
    return errors


async def interpret_news(
    agent: dict[str, Any],
    news_context: dict[str, Any],
    *,
    client: OpenRouterClient | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    client = client or OpenRouterClient()
    prompt = load_prompt("news_interpretation.txt").format(
        persona_prompt=agent["persona_prompt"],
        news_context=json.dumps(news_context, ensure_ascii=False, indent=2),
    )
    data = await _required_json_response(
        client=client,
        prompt=prompt,
        required_keys=NEWS_INTERPRETATION_KEYS,
        label="news_interpretation",
        seed=seed,
        validator=lambda value: [
            *([] if isinstance(value.get("selected_news"), list) else ["selected_news:not_list"]),
            *(
                []
                if value.get("news_sentiment")
                in {"positive", "negative", "neutral", "mixed", "insufficient"}
                else ["news_sentiment:invalid"]
            ),
            *(
                []
                if value.get("confidence") in {"high", "medium", "low"}
                else ["confidence:invalid"]
            ),
            *[
                f"{key}:empty"
                for key in ("short_term_impact", "long_term_impact", "persona_interpretation", "reason")
                if not isinstance(value.get(key), str) or not value[key].strip()
            ],
        ],
    )
    return data


async def depth2_pre_search(
    agent: dict[str, Any],
    base_news_context: dict[str, Any],
    *,
    client: OpenRouterClient | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    client = client or OpenRouterClient()
    prompt = load_prompt("news_agent_pre_search.txt").format(
        persona_prompt=agent["persona_prompt"],
        base_news_context=json.dumps(base_news_context, ensure_ascii=False, indent=2),
    )
    data = await _required_json_response(
        client=client,
        prompt=prompt,
        required_keys=DEPTH2_PRE_SEARCH_KEYS,
        label="depth2_pre_search",
        seed=seed,
        normalizer=normalize_depth2_pre_search,
        validator=validate_depth2_pre_search,
        schema_hint=DEPTH2_PRE_SEARCH_SCHEMA,
    )
    return data


async def depth2_post_search(
    agent: dict[str, Any],
    base_news_context: dict[str, Any],
    search_results: list[dict[str, Any]],
    pre_thinking: dict[str, Any],
    *,
    client: OpenRouterClient | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    client = client or OpenRouterClient()
    prompt = load_prompt("news_agent_post_search.txt").format(
        persona_prompt=agent["persona_prompt"],
        base_news_context=json.dumps(base_news_context, ensure_ascii=False, indent=2),
        search_results=json.dumps(search_results, ensure_ascii=False, indent=2),
        pre_search_thinking=json.dumps(pre_thinking, ensure_ascii=False, indent=2),
    )
    data = await _required_json_response(
        client=client,
        prompt=prompt,
        required_keys=DEPTH2_POST_SEARCH_KEYS,
        label="depth2_post_search",
        seed=seed,
        normalizer=normalize_depth2_post_search,
        validator=validate_depth2_post_search,
        schema_hint=DEPTH2_POST_SEARCH_SCHEMA,
    )
    return data


async def analyze_market(
    agent: dict[str, Any],
    *,
    today_belief: dict[str, Any],
    market_features: dict[str, Any],
    portfolio_summary: str,
    news_interpretation: dict[str, Any],
    client: OpenRouterClient | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    client = client or OpenRouterClient()
    prompt = load_prompt("market_analysis.txt").format(
        persona_prompt=agent["persona_prompt"],
        today_belief=json.dumps(today_belief, ensure_ascii=False, indent=2),
        market_features=json.dumps(market_features, ensure_ascii=False, indent=2),
        portfolio_summary=portfolio_summary,
        news_interpretation=json.dumps(news_interpretation, ensure_ascii=False, indent=2),
    )
    return await _required_json_response(
        client=client,
        prompt=prompt,
        required_keys=MARKET_ANALYSIS_KEYS,
        label="market_analysis",
        seed=seed,
        validator=lambda value: [
            *(
                []
                if value.get("confidence") in {"high", "medium", "low"}
                else ["confidence:invalid"]
            ),
            *[
                f"{key}:empty"
                for key in (
                    "market_view",
                    "valuation_view",
                    "technical_view",
                    "news_view",
                    "portfolio_view",
                )
                if not isinstance(value.get(key), str) or not value[key].strip()
            ],
            *[
                f"{key}:requires_nonempty_text_or_string_list"
                for key in ("key_risks", "opportunity", "caution")
                if not _nonempty_text_or_string_list(value.get(key))
            ],
        ],
    )
