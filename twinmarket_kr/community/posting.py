from __future__ import annotations

import json
from typing import Any

import config
from twinmarket_kr.community.validation import (
    COMMUNITY_POST_BODY_MAX_CHARS,
    CommunityValidationError,
    validate_post_body,
)
from twinmarket_kr.llm.analysis import parse_json_loose
from twinmarket_kr.llm.belief import belief_dimensions, render_prompt
from twinmarket_kr.llm.call_policy import (
    INTEGRATED_STAGE_MAX_TOKENS_V1,
    INTEGRATED_STAGE_SCHEMA_VERSIONS_V1,
)
from twinmarket_kr.llm.client import OpenRouterClient, response_content, stable_llm_seed
from twinmarket_kr.llm.response_journal import (
    ResponseJournalError,
    open_journal_call,
    parse_strict_json_object,
)
from twinmarket_kr.llm.validation import build_validation_retry_prompt, record_validation_failure


POST_TYPES = {"impression", "question", "trade_share", "profit_share", "analysis", "column"}

POST_TYPES_GUIDE = """
게시글 타입 6종 (하나를 선택):
- impression  : 짧은 감탄, 단상, 느낌 (1~3문장)
- question    : 다른 투자자에게 의견/정보 질문
- trade_share : 오늘 매수/매도 거래 소개 (반드시 실제 거래를 반영할 필요 없음)
- profit_share: 수익/손실 인증, 수익률 공유
- analysis    : 기술적/뉴스 분석, 시장 전망 (비교적 긴 글)
- column      : 한 편의 칼럼 형식, 긴 호흡의 의견
"""


async def posting_decision(
    agent: dict[str, Any],
    committed_ltb: dict[str, Any],
    committed_fill: dict[str, Any],
    date: str,
    client: OpenRouterClient | None = None,
    seed: int | None = None,
) -> dict[str, Any] | None:
    client = client or OpenRouterClient()
    ltb_dimensions = belief_dimensions(
        committed_ltb.get("dimensions", committed_ltb),
        label="community_posting.committed_ltb",
    )
    view_change = committed_ltb.get("view_change")
    if view_change is None:
        raise CommunityValidationError(
            "community posting requires the committed LTB-linked view_change"
        )
    required_fill_fields = {
        "fill_id",
        "decision_id",
        "action",
        "requested_quantity",
        "filled_quantity",
        "executed_price",
        "fee",
    }
    missing_fill_fields = required_fill_fields - set(committed_fill)
    if missing_fill_fields:
        raise CommunityValidationError(
            "community posting requires a structured committed fill; missing="
            + ",".join(sorted(missing_fill_fields))
        )
    prompt = render_prompt(
        "posting_decision.txt",
        persona_prompt=agent.get("persona_prompt", ""),
        ltb_dimensions=json.dumps(ltb_dimensions, ensure_ascii=False, indent=2),
        view_change=json.dumps(view_change, ensure_ascii=False, indent=2),
        committed_pm_fill=json.dumps(
            {key: committed_fill[key] for key in sorted(required_fill_fields)},
            ensure_ascii=False,
            indent=2,
        ),
        date=date,
        post_types_guide=POST_TYPES_GUIDE,
    )
    raw: dict[str, Any] = {}
    current_prompt = prompt
    stage = "community_posting"
    validation_attempts = 4
    temperatures = [0.7, 0.3, 0.3, 0.3]
    seeds = [
        stable_llm_seed(seed or 0, "community_posting_validation", attempt)
        for attempt in range(1, validation_attempts + 1)
    ]
    max_tokens = int(INTEGRATED_STAGE_MAX_TOKENS_V1[stage])
    journal_call = open_journal_call(
        stage=stage,
        schema_version=INTEGRATED_STAGE_SCHEMA_VERSIONS_V1[stage],
        base_prompt=prompt,
        client=client,
        model=config.OPENROUTER_COMMUNITY_MODEL,
        temperature_schedule=temperatures,
        seed_schedule=seeds,
        max_tokens=max_tokens,
        validation_attempts=validation_attempts,
        validation_procedure_version="community-posting-validator-v2",
        response_format={"type": "json_object"},
    )
    if journal_call is not None and journal_call.replay is not None:
        raw = dict(journal_call.replay.response)
        errors = _posting_errors(raw)
        if errors:
            raise ResponseJournalError(
                f"accepted replay no longer passes community posting: {errors}"
            )
        journal_call.repair_replay_acceptance(client=client)
    else:
        for attempt in range(1, validation_attempts + 1):
            attempt_seed = seeds[attempt - 1]
            if journal_call is not None:
                journal_call.begin(attempt)
            try:
                response = await client.chat(
                    [{"role": "user", "content": current_prompt}],
                    model=config.OPENROUTER_COMMUNITY_MODEL,
                    temperature=temperatures[attempt - 1],
                    response_format={"type": "json_object"},
                    seed=attempt_seed,
                    max_tokens=max_tokens,
                    audit_label=stage,
                    logical_call_id=(
                        journal_call.logical_call_id
                        if journal_call is not None
                        else None
                    ),
                    phase_attempt_id=(
                        journal_call.phase_attempt_id
                        if journal_call is not None
                        else None
                    ),
                )
            except BaseException as exc:
                if journal_call is not None:
                    journal_call.error(attempt, exc)
                raise
            raw_content = response_content(response) or "{}"
            parse_errors: list[str] = []
            parsed: dict[str, Any] | None
            try:
                parsed = (
                    parse_strict_json_object(raw_content)
                    if journal_call is not None
                    else parse_json_loose(raw_content)
                )
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                parsed = None
                parse_errors.append(f"strict_json_object:{exc}")
            raw = dict(parsed or {})
            errors = [*parse_errors, *_posting_errors(raw)]
            if not errors:
                if journal_call is not None:
                    if parsed is None:
                        raise AssertionError("validated response has no raw JSON object")
                    journal_call.accept(parsed, attempt, client=client)
                break
            if journal_call is not None:
                journal_call.rejected(
                    attempt,
                    response=parsed,
                    errors=errors,
                )
            record_validation_failure(
                label=stage,
                attempt=attempt,
                errors=errors,
                raw_content=raw_content,
                seed=attempt_seed,
            )
            current_prompt = build_validation_retry_prompt(
                prompt,
                errors=errors,
                schema_hint=(
                    '게시하면 {"will_post": true, "post_type": "impression|question|'
                    'trade_share|profit_share|analysis|column", "title": string, '
                    '"content": string} 네 key만, 게시하지 않으면 '
                    '{"will_post": false} 한 key만 출력하세요. 추가 key는 실패입니다.'
                ),
            )
        else:
            raise CommunityValidationError(
                "community posting did not return a valid decision after 4 attempts"
            )
    if raw["will_post"] is False:
        return None
    title = str(raw.get("title") or "").strip()
    content = str(raw.get("content") or "").strip()
    if not title or not content:
        return None
    post_type = str(raw.get("post_type") or "impression").strip()
    if post_type not in POST_TYPES:
        raise CommunityValidationError(f"invalid community post type after validation: {post_type}")
    return {
        "will_post": True,
        "post_type": post_type,
        "title": title,
        "content": validate_post_body(content),
        "source_ltb_id": str(committed_ltb.get("ltb_id") or ""),
        "source_view_change": view_change,
        "source_fill_id": str(committed_fill["fill_id"]),
        "source_decision_id": str(committed_fill["decision_id"]),
    }


def _posting_errors(raw: dict[str, Any]) -> list[str]:
    if not isinstance(raw.get("will_post"), bool):
        return ["will_post:requires_boolean"]
    # posting_decision.txt는 게시하지 않으면 {"will_post": false}만, 게시하면
    # 네 key만 출력하라고 요구한다. 다른 단계와 같은 강도로 extra key를 막는다.
    if raw["will_post"] is False:
        unknown = sorted(set(raw) - {"will_post"})
        return [f"top_level_keys:unknown={unknown}"] if unknown else []
    errors: list[str] = []
    unknown = sorted(set(raw) - {"will_post", "post_type", "title", "content"})
    if unknown:
        errors.append(f"top_level_keys:unknown={unknown}")
    if str(raw.get("post_type") or "").strip() not in POST_TYPES:
        errors.append("post_type:invalid")
    if not str(raw.get("title") or "").strip():
        errors.append("title:requires_nonempty_string")
    if not str(raw.get("content") or "").strip():
        errors.append("content:requires_nonempty_string")
    elif len(str(raw.get("content") or "").strip()) > COMMUNITY_POST_BODY_MAX_CHARS:
        errors.append(
            f"content:exceeds_{COMMUNITY_POST_BODY_MAX_CHARS}_characters"
        )
    return errors
