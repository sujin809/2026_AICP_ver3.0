from __future__ import annotations

import json
from typing import Any

import config
from twinmarket_kr.community.validation import CommunityValidationError
from twinmarket_kr.llm.analysis import parse_json_loose
from twinmarket_kr.llm.belief import render_prompt
from twinmarket_kr.llm.call_policy import (
    INTEGRATED_STAGE_MAX_TOKENS_V1,
    INTEGRATED_STAGE_SCHEMA_VERSIONS_V1,
)

# community_reading.txt는 select와 react 두 단계가 함께 쓰는 한 파일이다.
# 두 모드의 설명이 모두 남아 있으면 모델이 마지막에 읽은 스키마를 따라가므로,
# 이번 호출에서 유효한 출력 계약만 프롬프트 끝에 주입한다.
SELECT_OUTPUT_CONTRACT = """필수 JSON 키는 `selected_post_ids` 하나뿐입니다.
`selected_post_ids`는 위 게시글 목록에 실제로 있는 post_id의 배열입니다.
위 선택 기준에 맞는 글을 고르세요. 상한까지 억지로 채울 필요는 없고,
읽을 만한 글이 없다고 판단하면 빈 배열도 유효합니다.
Markdown, 코드블록, 설명, 추가 키 없이 아래 형태의 JSON object 하나만 출력하세요.
아래 숫자는 형식 예시일 뿐이며 실제 post_id로 바꿔야 합니다.
{
  "selected_post_ids": [3, 7]
}"""

REACT_OUTPUT_CONTRACT = """필수 JSON 키는 `reactions` 하나뿐입니다.
`reactions`는 위 본문이 제공된 **모든** 글에 대해 각각 정확히 하나의 항목을
가져야 합니다. 하나라도 빠지거나 중복되면 실패입니다. 빈 배열은 허용되지 않습니다.
각 항목의 `post_id`는 위 본문에 실제로 있는 post_id여야 하고, `reaction`은
`like`, `unlike`, `none` 중 하나입니다.
Markdown, 코드블록, 설명, 추가 키 없이 아래 형태의 JSON object 하나만 출력하세요.
아래 숫자는 형식 예시일 뿐이며 실제 post_id로 바꿔야 합니다.
{
  "reactions": [
    {"post_id": 101, "reaction": "like"},
    {"post_id": 102, "reaction": "none"}
  ]
}"""
from twinmarket_kr.llm.client import OpenRouterClient, response_content, stable_llm_seed
from twinmarket_kr.llm.response_journal import (
    ResponseJournalError,
    open_journal_call,
    parse_strict_json_object,
)
from twinmarket_kr.llm.validation import build_validation_retry_prompt, record_validation_failure


def _validate_selection(
    raw: dict[str, Any],
    *,
    available: set[int],
    read_limit: int,
) -> tuple[list[int], list[str]]:
    raw_ids = raw.get("selected_post_ids")
    errors: list[str] = []
    # SELECT_OUTPUT_CONTRACT가 "추가 키 없이 ... selected_post_ids 하나뿐"이라고
    # 요구하므로 다른 단계와 같은 강도로 extra key를 거부한다.
    unknown_keys = sorted(set(raw) - {"selected_post_ids"})
    if unknown_keys:
        errors.append(f"top_level_keys:unknown={unknown_keys}")
    if not isinstance(raw_ids, list):
        return [], [*errors, "selected_post_ids:requires_list"]
    candidate: list[int] = []
    for post_id in raw_ids:
        if isinstance(post_id, bool):
            errors.append("selected_post_ids:boolean_not_allowed")
            break
        try:
            pid = int(post_id)
        except (TypeError, ValueError):
            errors.append("selected_post_ids:requires_integer_ids")
            break
        if pid not in available:
            errors.append(f"selected_post_ids:unknown_id:{pid}")
            break
        if pid in candidate:
            errors.append(f"selected_post_ids:duplicate_id:{pid}")
            break
        candidate.append(pid)
    if len(candidate) > read_limit:
        errors.append(
            f"selected_post_ids:exceeds_limit:{len(candidate)}>{read_limit}"
        )
    return candidate, errors


def _validate_reactions(
    raw: dict[str, Any],
    *,
    available: set[int],
) -> tuple[list[dict[str, Any]], list[str]]:
    raw_reactions = raw.get("reactions")
    # REACT_OUTPUT_CONTRACT도 reactions 한 key만 허용한다.
    unknown_keys = sorted(set(raw) - {"reactions"})
    key_errors = (
        [f"top_level_keys:unknown={unknown_keys}"] if unknown_keys else []
    )
    if not isinstance(raw_reactions, list):
        return [], [*key_errors, "reactions:requires_list"]
    candidate: list[dict[str, Any]] = []
    errors: list[str] = list(key_errors)
    for item in raw_reactions:
        if not isinstance(item, dict) or isinstance(item.get("post_id"), bool):
            errors.append("reactions:item_requires_object_and_integer_id")
            break
        try:
            post_id = int(item.get("post_id"))
        except (TypeError, ValueError):
            errors.append("reactions:post_id_requires_integer")
            break
        reaction = str(item.get("reaction") or "")
        if post_id not in available:
            errors.append(f"reactions:unknown_post_id:{post_id}")
            break
        if reaction not in {"like", "unlike", "none"}:
            errors.append(f"reactions:invalid_reaction:{reaction}")
            break
        candidate.append({"post_id": post_id, "reaction": reaction})
    candidate_ids = [item["post_id"] for item in candidate]
    if len(candidate_ids) != len(set(candidate_ids)):
        errors.append("reactions:duplicate_post_ids")
    if set(candidate_ids) != available:
        errors.append("reactions:must_cover_every_read_post")
    return candidate, errors


async def community_reading_select(
    agent: dict[str, Any],
    post_list: list[dict[str, Any]],
    read_limit: int,
    client: OpenRouterClient | None = None,
    seed: int | None = None,
) -> list[int]:
    client = client or OpenRouterClient()
    if not post_list:
        return []
    prompt = render_prompt(
        "community_reading.txt",
        mode="select",
        persona_prompt=agent.get("persona_prompt", ""),
        post_list_str=_format_post_list(post_list),
        read_limit=int(read_limit),
        posts_content_str="",
        output_contract=SELECT_OUTPUT_CONTRACT,
    )
    available = {int(post["post_id"]) for post in post_list}
    selected: list[int] = []
    current_prompt = prompt
    stage = "community_read_select"
    validation_attempts = 10
    temperatures = [0.3 if a == 1 else 0.1 for a in range(1, validation_attempts + 1)]
    seeds = [
        stable_llm_seed(seed or 0, "community_select_validation", attempt)
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
        validation_procedure_version="community-read-select-validator-v2",
        response_format={"type": "json_object"},
        semantic_inputs={
            "available_post_ids": sorted(available),
            "read_limit": int(read_limit),
        },
    )
    if journal_call is not None:
        # retry 계보가 열렸으면 시드가 변주된다. journal 요청 identity와
        # 실제 API 호출이 같은 스케줄을 쓰도록 여기서 덮어쓴다.
        seeds = list(journal_call.seed_schedule)
    if journal_call is not None and journal_call.replay is not None:
        selected, errors = _validate_selection(
            journal_call.replay.response,
            available=available,
            read_limit=read_limit,
        )
        if errors:
            raise ResponseJournalError(
                f"accepted replay no longer passes community selection: {errors}"
            )
        journal_call.repair_replay_acceptance(client=client)
        return selected
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
        candidate, validation_errors = _validate_selection(
            raw or {},
            available=available,
            read_limit=read_limit,
        )
        errors = [*parse_errors, *validation_errors]
        if not errors:
            selected = candidate
            if journal_call is not None:
                if raw is None:
                    raise AssertionError("validated response has no raw JSON object")
                journal_call.accept(raw, attempt, client=client)
            break
        if journal_call is not None:
            journal_call.rejected(
                attempt,
                response=raw,
                errors=errors,
            )
        record_validation_failure(
            label=stage,
            attempt=attempt,
            errors=errors or ["selected_post_ids:invalid"],
            raw_content=raw_content,
            seed=attempt_seed,
        )
        current_prompt = build_validation_retry_prompt(
            prompt,
            errors=errors or ["selected_post_ids:invalid"],
            schema_hint=(
                '{"selected_post_ids": [integer, ...]}. 후보에 있는 post_id만 중복 없이 '
                f"최대 {read_limit}개 출력하세요."
            ),
        )
    else:
        raise CommunityValidationError(f"community reading selection was invalid after {validation_attempts} attempts")
    return selected


async def community_reading_react(
    agent: dict[str, Any],
    posts_content: list[dict[str, Any]],
    client: OpenRouterClient | None = None,
    seed: int | None = None,
) -> list[dict[str, Any]]:
    client = client or OpenRouterClient()
    if not posts_content:
        return []
    prompt = render_prompt(
        "community_reading.txt",
        mode="react",
        persona_prompt=agent.get("persona_prompt", ""),
        post_list_str="",
        read_limit=len(posts_content),
        posts_content_str=_format_posts_content(posts_content),
        output_contract=REACT_OUTPUT_CONTRACT,
    )
    available = {int(post["post_id"]) for post in posts_content}
    validated: list[dict[str, Any]] = []
    current_prompt = prompt
    stage = "community_read_react"
    validation_attempts = 10
    temperatures = [0.2 if a == 1 else 0.1 for a in range(1, validation_attempts + 1)]
    seeds = [
        stable_llm_seed(seed or 0, "community_react_validation", attempt)
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
        validation_procedure_version="community-read-react-validator-v2",
        response_format={"type": "json_object"},
        semantic_inputs={"available_post_ids": sorted(available)},
    )
    if journal_call is not None:
        # retry 계보가 열렸으면 시드가 변주된다. journal 요청 identity와
        # 실제 API 호출이 같은 스케줄을 쓰도록 여기서 덮어쓴다.
        seeds = list(journal_call.seed_schedule)
    if journal_call is not None and journal_call.replay is not None:
        validated, errors = _validate_reactions(
            journal_call.replay.response,
            available=available,
        )
        if errors:
            raise ResponseJournalError(
                f"accepted replay no longer passes community reactions: {errors}"
            )
        journal_call.repair_replay_acceptance(client=client)
        return validated
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
        candidate, validation_errors = _validate_reactions(
            raw or {},
            available=available,
        )
        errors = [*parse_errors, *validation_errors]
        if not errors:
            validated = candidate
            if journal_call is not None:
                if raw is None:
                    raise AssertionError("validated response has no raw JSON object")
                journal_call.accept(raw, attempt, client=client)
            break
        if journal_call is not None:
            journal_call.rejected(
                attempt,
                response=raw,
                errors=errors,
            )
        record_validation_failure(
            label=stage,
            attempt=attempt,
            errors=errors or ["reactions:invalid"],
            raw_content=raw_content,
            seed=attempt_seed,
        )
        current_prompt = build_validation_retry_prompt(
            prompt,
            errors=errors or ["reactions:invalid"],
            schema_hint=(
                '{"reactions": [{"post_id": integer, "reaction": "like|unlike|none"}, ...]}. '
                "읽은 모든 post_id를 정확히 한 번씩 포함하세요."
            ),
        )
    else:
        raise CommunityValidationError(f"community reactions were invalid after {validation_attempts} attempts")
    return validated


def _format_post_list(post_list: list[dict[str, Any]]) -> str:
    lines = []
    for post in post_list:
        lines.append(
            f"[post_id={post['post_id']}] [{post.get('post_type', '')}] {post.get('title', '')} "
            f"| 작성자: {post.get('anonymous_code', '')} "
            f"| like {post.get('like_count', 0)} / unlike {post.get('unlike_count', 0)} "
            f"/ score {post.get('score', 0)}"
        )
    return "\n".join(lines)


def _format_posts_content(posts_content: list[dict[str, Any]]) -> str:
    parts = []
    for post in posts_content:
        profile_text = ""
        if post.get("author_profile"):
            profile_text = "\n[작성자 프로필] " + json.dumps(
                post["author_profile"], ensure_ascii=False, default=str
            )
        parts.append(
            f"--- post_id={post['post_id']} [{post.get('post_type', '')}] ---\n"
            f"제목: {post.get('title', '')}\n"
            f"작성자: {post.get('anonymous_code', '')}\n"
            f"본문: {post.get('content', '')}"
            f"{profile_text}"
        )
    return "\n\n".join(parts)
