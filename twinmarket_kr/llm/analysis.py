from __future__ import annotations

import json
from typing import Any, Callable

from twinmarket_kr.llm.belief import load_prompt
from twinmarket_kr.llm.client import OpenRouterClient, response_content, stable_llm_seed


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
    "search_needed",
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


class AnalysisValidationError(RuntimeError):
    pass


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


def with_defaults(data: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(data)
    for key, value in defaults.items():
        normalized.setdefault(key, value)
    return normalized


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
) -> dict[str, Any]:
    invalid_history: list[list[str]] = []
    for attempt in range(1, validation_attempts + 1):
        response = await client.chat(
            [{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.2 if attempt == 1 else 0.1,
            seed=stable_llm_seed(seed or 0, label, attempt),
            audit_label=label,
        )
        data = parse_json_loose(response_content(response) or "{}")
        missing = [key for key in required_keys if key not in data]
        validation_errors = validator(data) if not missing and validator is not None else []
        if not missing and not validation_errors:
            data["generation_attempts"] = attempt
            return data
        invalid_history.append([*missing, *validation_errors])
    raise AnalysisValidationError(
        f"{label} did not produce all required JSON keys after "
        f"{validation_attempts} attempts: {invalid_history}"
    )


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
    prompt = load_prompt("news_agent.txt").format(
        mode="pre_search",
        persona_prompt=agent["persona_prompt"],
        base_news_context=json.dumps(base_news_context, ensure_ascii=False, indent=2),
        search_results="[]",
        pre_search_thinking="{}",
    )
    data = await _required_json_response(
        client=client,
        prompt=prompt,
        required_keys=DEPTH2_PRE_SEARCH_KEYS,
        label="depth2_pre_search",
        seed=seed,
        validator=lambda value: [
            *(
                []
                if isinstance(value.get("search_needed"), bool)
                else ["search_needed:not_boolean"]
            ),
            *(
                []
                if isinstance(value.get("curiosity_points"), list)
                else ["curiosity_points:not_list"]
            ),
            *(
                []
                if isinstance(value.get("search_keywords"), list)
                and 3 <= len(value["search_keywords"]) <= 8
                and all(
                    isinstance(keyword, str) and keyword.strip()
                    for keyword in value["search_keywords"]
                )
                else ["search_keywords:requires_3_to_8_strings"]
            ),
            *[
                f"{key}:empty"
                for key in ("key_findings", "search_rationale")
                if not isinstance(value.get(key), str) or not value[key].strip()
            ],
        ],
    )
    data = with_defaults(data, {
        "search_needed": bool(data.get("search_keywords")),
        "key_findings": "",
        "curiosity_points": [],
        "search_rationale": "",
        "search_keywords": [],
    })
    data["search_needed"] = bool(data["search_needed"])
    data["search_keywords"] = [str(keyword).strip() for keyword in data["search_keywords"] if str(keyword).strip()][:8]
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
    prompt = load_prompt("news_agent.txt").format(
        mode="post_search",
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
        validator=lambda value: [
            *(
                []
                if value.get("view_change") in {"강화", "수정", "반전", "유지"}
                else ["view_change:invalid"]
            ),
            *(
                []
                if isinstance(value.get("unresolved_questions"), list)
                else ["unresolved_questions:not_list"]
            ),
            *[
                f"{key}:not_string"
                for key in ("new_findings", "view_change_detail")
                if not isinstance(value.get(key), str)
            ],
        ],
    )
    data = with_defaults(data, {
        "new_findings": "",
        "view_change": "유지",
        "view_change_detail": "",
        "unresolved_questions": [],
    })
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
