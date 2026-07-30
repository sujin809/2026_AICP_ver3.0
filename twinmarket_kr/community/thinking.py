from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

import config
from twinmarket_kr.community.validation import CommunityValidationError
from twinmarket_kr.llm.analysis import parse_json_loose
from twinmarket_kr.llm.belief import render_prompt
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


async def community_thinking(
    agent: dict[str, Any],
    community_log: dict[str, Any],
    *,
    delivery_turn: int,
    delivery_date: str,
    client: OpenRouterClient | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    client = client or OpenRouterClient()
    source_turn = int(community_log.get("turn") or 0)
    source_date = str(community_log.get("date") or "")
    agent_id = str(agent["agent_id"])
    best_posts = community_log.get("best_posts_seen") or []
    posts_read = community_log.get("posts_read") or []
    selected_by_id = {
        int(post["post_id"]): post
        for post in posts_read
        if post.get("post_id") is not None
    }
    best_post_ids = {
        int(post["post_id"])
        for post in best_posts
        if post.get("post_id") is not None
    }
    best_summary, best_sources = _format_best_posts(
        best_posts,
        selected_by_id=selected_by_id,
        agent_id=agent_id,
        source_turn=source_turn,
        source_date=source_date,
        delivery_turn=delivery_turn,
    )
    read_summary, read_sources = _format_posts_read(
        posts_read,
        excluded_post_ids=best_post_ids,
        agent_id=agent_id,
        source_turn=source_turn,
        source_date=source_date,
        delivery_turn=delivery_turn,
    )
    allowed_sources = {
        **best_sources,
        **read_sources,
    }
    # 선택 화면의 title-only 후보 노출은 exposure 분석 로그 전용이며,
    # EXPERIMENT_DESIGN.md 8.4절에 따라 next-AM claim 입력에서 제외한다.
    # 따라서 이 프롬프트에는 full_body 노출 두 종류만 넣는다.
    prompt = render_prompt(
        "community_thinking.txt",
        persona_prompt=agent.get("persona_prompt", ""),
        best_posts_summary=best_summary,
        posts_read_summary=read_summary,
    )
    current_prompt = prompt
    stage = "community_thinking"
    validation_attempts = 10
    temperatures = [0.3 if a == 1 else 0.1 for a in range(1, validation_attempts + 1)]
    seeds = [
        stable_llm_seed(seed or 0, "community_thinking_validation", attempt)
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
        validation_procedure_version="community-thinking-validator-v2",
        response_format={"type": "json_object"},
        semantic_inputs={"allowed_sources": dict(sorted(allowed_sources.items()))},
    )
    if journal_call is not None and journal_call.replay is not None:
        content = dict(journal_call.replay.response)
        errors = _community_thinking_errors(content, allowed_sources=allowed_sources)
        if errors:
            raise ResponseJournalError(
                f"accepted replay no longer passes community thinking: {errors}"
            )
        journal_call.repair_replay_acceptance(client=client)
        content["source_turn"] = source_turn
        content["source_date"] = source_date
        content["delivery_turn"] = int(delivery_turn)
        content["delivery_date"] = str(delivery_date)
        return content
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
        raw: dict[str, Any] | None
        try:
            raw = (
                parse_strict_json_object(raw_content)
                if journal_call is not None
                else parse_json_loose(raw_content)
            )
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raw = None
            parse_errors.append(f"strict_json_object:{exc}")
        content = dict(raw or {})
        errors = [
            *parse_errors,
            *_community_thinking_errors(
                content,
                allowed_sources=allowed_sources,
            ),
        ]
        if not errors:
            if journal_call is not None:
                if raw is None:
                    raise AssertionError("validated response has no raw JSON object")
                journal_call.accept(raw, attempt, client=client)
            content["source_turn"] = source_turn
            content["source_date"] = source_date
            content["delivery_turn"] = int(delivery_turn)
            content["delivery_date"] = str(delivery_date)
            return content
        if journal_call is not None:
            journal_call.rejected(
                attempt,
                response=raw,
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
                "observed_sentiment, claims, agreement_disagreement, uncertainty만 "
                "가진 JSON object를 출력하세요."
            ),
        )
    raise CommunityValidationError(
        f"community thinking did not satisfy the structured claim contract after {validation_attempts} attempts"
    )


def _format_best_posts(
    best_posts: list[dict[str, Any]],
    *,
    selected_by_id: dict[int, dict[str, Any]] | None = None,
    agent_id: str,
    source_turn: int,
    source_date: str,
    delivery_turn: int,
) -> tuple[str, dict[str, str]]:
    if not best_posts:
        return "(어제 Best 게시글 없음)", {}
    selected_by_id = selected_by_id or {}
    lines: list[str] = []
    sources: dict[str, str] = {}
    for post in best_posts:
        post_id = int(post["post_id"]) if post.get("post_id") is not None else -1
        exposure_id = (
            f"community:{source_date}:t{source_turn}:post:{post_id}:"
            f"best_full_body:{agent_id}:delivered_t{delivery_turn}"
        )
        selected = selected_by_id.get(post_id)
        reaction = (
            f" | 전날 직접 읽음·내 반응: {selected.get('reaction', 'none')}"
            if selected is not None
            else ""
        )
        profile = _format_author_profile(post.get("author_profile"))
        title = str(post.get("title") or "")
        body = str(post.get("content") or "")
        body_text = f"{title}\n{body}"
        sources[exposure_id] = body_text
        exposure_lines = [f"- source_exposure_id: {exposure_id}"]
        if selected is not None:
            # 같은 글이 Best와 직접 선택 두 관계에 모두 해당하면 본문은 여기서 한 번만
            # 직렬화하되, 두 노출 관계 ID를 모두 인용 가능한 근거로 남긴다.
            replay_exposure_id = (
                f"community:{source_date}:t{source_turn}:post:{post_id}:"
                f"selected_full_body_replay:{agent_id}:delivered_t{delivery_turn}"
            )
            sources[replay_exposure_id] = body_text
            exposure_lines.append(
                f"- source_exposure_id: {replay_exposure_id} (같은 글을 전날 직접 선택해 읽은 관계)"
            )
        lines.append(
            "\n".join(exposure_lines) + "\n"
            f"  Best {post.get('rank', '')} [{post.get('post_type', '')}] "
            f"{title} (score: {post.get('score', 0)})\n"
            f"  작성자: {post.get('anonymous_code', '')}{reaction}\n"
            f"  본문 전체: {body}{profile}"
        )
    return "\n\n".join(lines), sources


def _format_posts_read(
    posts_read: list[dict[str, Any]],
    *,
    excluded_post_ids: set[int] | None = None,
    agent_id: str,
    source_turn: int,
    source_date: str,
    delivery_turn: int,
) -> tuple[str, dict[str, str]]:
    excluded_post_ids = excluded_post_ids or set()
    visible_posts = [
        post
        for post in posts_read
        if post.get("post_id") is None or int(post["post_id"]) not in excluded_post_ids
    ]
    if not visible_posts:
        return "(어제 직접 읽은 게시글 없음)", {}
    lines: list[str] = []
    sources: dict[str, str] = {}
    for post in visible_posts:
        post_id = int(post["post_id"])
        exposure_id = (
            f"community:{source_date}:t{source_turn}:post:{post_id}:"
            f"selected_full_body_replay:{agent_id}:delivered_t{delivery_turn}"
        )
        profile = _format_author_profile(post.get("author_profile"))
        title = str(post.get("title") or "")
        body = str(post.get("content") or "")
        sources[exposure_id] = f"{title}\n{body}"
        lines.append(
            f"- source_exposure_id: {exposure_id}\n"
            f"  [{post.get('post_type', '')}] {title} | "
            f"내 반응: {post.get('reaction', 'none')}\n"
            f"  작성자: {post.get('anonymous_code', '')}\n"
            f"  본문 전체: {body}{profile}"
        )
    return "\n\n".join(lines), sources


def _normalize_quote_text(value: str) -> str:
    """Compare quotes with whitespace removed.

    게시글 본문에는 ``21 만 6 천``처럼 띄어쓰기가 특이한 문장이 섞여 있고,
    모델은 인용하면서 공백을 자기 스타일로 통일하는 습성이 있다. 2일 유료
    실행에서 community_thinking 첫 시도 거부 100건이 발생했고, 실패 프롬프트를
    그대로 5회 재호출한 프로브에서 실패 인용문 전부가 공백만 다른
    verbatim 인용이었다(``16조나`` vs ``16 조나``). 공백을 제거한 뒤 부분
    문자열을 검사하면 인용 계약(인용문은 인용한 노출의 실제 문자열)은
    유지하면서 이 서식 차이만 흡수한다.
    """

    return re.sub(r"\s+", "", unicodedata.normalize("NFC", value))


def _community_thinking_errors(
    value: dict[str, Any],
    *,
    allowed_sources: dict[str, str],
) -> list[str]:
    required = {
        "observed_sentiment",
        "claims",
        "agreement_disagreement",
        "uncertainty",
    }
    errors: list[str] = []
    if set(value) != required:
        errors.append(
            "top_level_keys:"
            f"missing={sorted(required - set(value))}:"
            f"unknown={sorted(set(value) - required)}"
        )
    if value.get("observed_sentiment") not in {
        "optimistic",
        "pessimistic",
        "mixed",
        "overheated",
        "anxious",
        "uncertain",
    }:
        errors.append("observed_sentiment:invalid")
    for field in ("agreement_disagreement", "uncertainty"):
        if not isinstance(value.get(field), str) or not value[field].strip():
            errors.append(f"{field}:requires_nonempty_string")
    claims = value.get("claims")
    if not isinstance(claims, list):
        return [*errors, "claims:requires_array"]
    for index, claim in enumerate(claims):
        expected = {
            "claim_text",
            "claim_stance",
            "source_exposure_ids",
            "supporting_quote",
        }
        if not isinstance(claim, dict) or set(claim) != expected:
            errors.append(f"claims[{index}]:invalid_schema")
            continue
        if not isinstance(claim["claim_text"], str) or not claim["claim_text"].strip():
            errors.append(f"claims[{index}].claim_text:requires_nonempty_string")
        if claim["claim_stance"] not in {
            "bullish",
            "bearish",
            "neutral",
            "uncertain",
        }:
            errors.append(f"claims[{index}].claim_stance:invalid")
        source_ids = claim["source_exposure_ids"]
        if (
            not isinstance(source_ids, list)
            or not source_ids
            or any(not isinstance(item, str) or not item for item in source_ids)
            or len(source_ids) != len(set(source_ids))
        ):
            errors.append(
                f"claims[{index}].source_exposure_ids:requires_unique_nonempty_array"
            )
            continue
        unknown = set(source_ids) - set(allowed_sources)
        if unknown:
            errors.append(
                f"claims[{index}].source_exposure_ids:unknown:{sorted(unknown)}"
            )
        quote = claim["supporting_quote"]
        if not isinstance(quote, str) or not quote.strip():
            errors.append(f"claims[{index}].supporting_quote:requires_nonempty_string")
        elif not any(
            _normalize_quote_text(quote)
            in _normalize_quote_text(allowed_sources.get(source_id, ""))
            for source_id in source_ids
        ):
            errors.append(
                f"claims[{index}].supporting_quote:not_in_cited_exposure"
            )
    return errors


def _format_author_profile(profile: Any) -> str:
    if not isinstance(profile, dict) or not profile:
        return ""
    return "\n  동결된 공개 프로필: " + json.dumps(
        profile,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
