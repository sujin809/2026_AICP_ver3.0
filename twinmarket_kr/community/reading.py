from __future__ import annotations

import json
from typing import Any

import config
from twinmarket_kr.community.validation import CommunityValidationError
from twinmarket_kr.llm.analysis import parse_json_loose
from twinmarket_kr.llm.belief import load_prompt
from twinmarket_kr.llm.client import OpenRouterClient, response_content, stable_llm_seed
from twinmarket_kr.llm.validation import build_validation_retry_prompt, record_validation_failure


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
    prompt_template = load_prompt("community_reading.txt")
    prompt = prompt_template.format(
        mode="select",
        persona_prompt=agent.get("persona_prompt", ""),
        post_list_str=_format_post_list(post_list),
        read_limit=int(read_limit),
        posts_content_str="",
    )
    available = {int(post["post_id"]) for post in post_list}
    selected: list[int] = []
    current_prompt = prompt
    for attempt in range(1, 5):
        attempt_seed = stable_llm_seed(seed or 0, "community_select_validation", attempt)
        response = await client.chat(
            [{"role": "user", "content": current_prompt}],
            model=config.OPENROUTER_COMMUNITY_MODEL,
            temperature=0.3 if attempt == 1 else 0.1,
            response_format={"type": "json_object"},
            seed=attempt_seed,
            audit_label="community_read_select",
        )
        raw_content = response_content(response) or "{}"
        raw = parse_json_loose(raw_content)
        raw_ids = raw.get("selected_post_ids")
        errors: list[str] = []
        if not isinstance(raw_ids, list):
            errors.append("selected_post_ids:requires_list")
            candidate = []
            valid = False
        else:
            candidate = []
            valid = True
            for post_id in raw_ids:
                if isinstance(post_id, bool):
                    errors.append("selected_post_ids:boolean_not_allowed")
                    valid = False
                    break
                try:
                    pid = int(post_id)
                except (TypeError, ValueError):
                    errors.append("selected_post_ids:requires_integer_ids")
                    valid = False
                    break
                if pid not in available:
                    errors.append(f"selected_post_ids:unknown_id:{pid}")
                    valid = False
                    break
                if pid in candidate:
                    errors.append(f"selected_post_ids:duplicate_id:{pid}")
                    valid = False
                    break
                candidate.append(pid)
            if len(candidate) > read_limit:
                errors.append(f"selected_post_ids:exceeds_limit:{len(candidate)}>{read_limit}")
                valid = False
        if valid and len(candidate) <= read_limit:
            selected = candidate
            break
        record_validation_failure(
            label="community_read_select",
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
        raise CommunityValidationError("community reading selection was invalid after 4 attempts")
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
    prompt_template = load_prompt("community_reading.txt")
    prompt = prompt_template.format(
        mode="react",
        persona_prompt=agent.get("persona_prompt", ""),
        post_list_str="",
        read_limit=len(posts_content),
        posts_content_str=_format_posts_content(posts_content),
    )
    available = {int(post["post_id"]) for post in posts_content}
    validated: list[dict[str, Any]] = []
    current_prompt = prompt
    for attempt in range(1, 5):
        attempt_seed = stable_llm_seed(seed or 0, "community_react_validation", attempt)
        response = await client.chat(
            [{"role": "user", "content": current_prompt}],
            model=config.OPENROUTER_COMMUNITY_MODEL,
            temperature=0.2 if attempt == 1 else 0.1,
            response_format={"type": "json_object"},
            seed=attempt_seed,
            audit_label="community_read_react",
        )
        raw_content = response_content(response) or "{}"
        raw = parse_json_loose(raw_content)
        raw_reactions = raw.get("reactions")
        errors: list[str] = []
        if not isinstance(raw_reactions, list):
            errors.append("reactions:requires_list")
            candidate = []
            valid = False
        else:
            candidate = []
            valid = True
            for item in raw_reactions:
                if not isinstance(item, dict) or isinstance(item.get("post_id"), bool):
                    errors.append("reactions:item_requires_object_and_integer_id")
                    valid = False
                    break
                try:
                    post_id = int(item.get("post_id"))
                except (TypeError, ValueError):
                    errors.append("reactions:post_id_requires_integer")
                    valid = False
                    break
                reaction = str(item.get("reaction") or "")
                if post_id not in available:
                    errors.append(f"reactions:unknown_post_id:{post_id}")
                    valid = False
                    break
                if reaction not in {"like", "unlike", "none"}:
                    errors.append(f"reactions:invalid_reaction:{reaction}")
                    valid = False
                    break
                candidate.append({"post_id": post_id, "reaction": reaction})
        candidate_ids = [item["post_id"] for item in candidate]
        if (
            valid
            and len(candidate_ids) == len(set(candidate_ids))
            and set(candidate_ids) == available
        ):
            validated = candidate
            break
        if valid and len(candidate_ids) != len(set(candidate_ids)):
            errors.append("reactions:duplicate_post_ids")
        if valid and set(candidate_ids) != available:
            errors.append("reactions:must_cover_every_read_post")
        record_validation_failure(
            label="community_read_react",
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
        raise CommunityValidationError("community reactions were invalid after 4 attempts")
    return validated


def _format_post_list(post_list: list[dict[str, Any]]) -> str:
    lines = []
    for post in post_list:
        badges = ", ".join(post.get("author_badges") or []) or "없음"
        lines.append(
            f"[post_id={post['post_id']}] [{post.get('post_type', '')}] {post.get('title', '')} "
            f"| 작성자: {post.get('anonymous_code', '')} [{badges}] "
            f"| like {post.get('like_count', 0)} / unlike {post.get('unlike_count', 0)}"
        )
    return "\n".join(lines)


def _format_posts_content(posts_content: list[dict[str, Any]]) -> str:
    parts = []
    for post in posts_content:
        profile_text = ""
        if post.get("author_profile"):
            profile_text = "\n[작성자 프로필] " + json.dumps(
                post["author_profile"], ensure_ascii=False, default=str
            )[:800]
        parts.append(
            f"--- post_id={post['post_id']} [{post.get('post_type', '')}] ---\n"
            f"제목: {post.get('title', '')}\n"
            f"본문: {post.get('content', '')}"
            f"{profile_text}"
        )
    return "\n\n".join(parts)
