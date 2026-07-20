from __future__ import annotations

import json
from typing import Any

import config
from twinmarket_kr.llm.analysis import parse_json_loose
from twinmarket_kr.llm.belief import load_prompt
from twinmarket_kr.llm.client import OpenRouterClient, response_content, stable_llm_seed


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
    for attempt in range(1, 5):
        response = await client.chat(
            [{"role": "user", "content": prompt}],
            model=config.OPENROUTER_COMMUNITY_MODEL,
            temperature=0.3 if attempt == 1 else 0.1,
            response_format={"type": "json_object"},
            seed=stable_llm_seed(seed or 0, "community_select_validation", attempt),
            audit_label="community_read_select",
        )
        raw = parse_json_loose(response_content(response) or "{}")
        raw_ids = raw.get("selected_post_ids")
        if not isinstance(raw_ids, list):
            continue
        candidate: list[int] = []
        valid = True
        for post_id in raw_ids:
            if isinstance(post_id, bool):
                valid = False
                break
            try:
                pid = int(post_id)
            except (TypeError, ValueError):
                valid = False
                break
            if pid not in available or pid in candidate:
                valid = False
                break
            candidate.append(pid)
        if valid and len(candidate) <= read_limit:
            selected = candidate
            break
    else:
        raise RuntimeError("community reading selection was invalid after 4 attempts")
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
    for attempt in range(1, 5):
        response = await client.chat(
            [{"role": "user", "content": prompt}],
            model=config.OPENROUTER_COMMUNITY_MODEL,
            temperature=0.2 if attempt == 1 else 0.1,
            response_format={"type": "json_object"},
            seed=stable_llm_seed(seed or 0, "community_react_validation", attempt),
            audit_label="community_read_react",
        )
        raw = parse_json_loose(response_content(response) or "{}")
        raw_reactions = raw.get("reactions")
        if not isinstance(raw_reactions, list):
            continue
        candidate: list[dict[str, Any]] = []
        valid = True
        for item in raw_reactions:
            if not isinstance(item, dict) or isinstance(item.get("post_id"), bool):
                valid = False
                break
            try:
                post_id = int(item.get("post_id"))
            except (TypeError, ValueError):
                valid = False
                break
            reaction = str(item.get("reaction") or "")
            if post_id not in available or reaction not in {"like", "unlike", "none"}:
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
    else:
        raise RuntimeError("community reactions were invalid after 4 attempts")
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
