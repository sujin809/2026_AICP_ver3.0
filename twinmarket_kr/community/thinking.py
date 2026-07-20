from __future__ import annotations

from typing import Any

import config
from twinmarket_kr.community.validation import CommunityValidationError
from twinmarket_kr.llm.belief import load_prompt
from twinmarket_kr.llm.client import OpenRouterClient, response_content, stable_llm_seed
from twinmarket_kr.llm.validation import build_validation_retry_prompt, record_validation_failure


async def community_thinking(
    agent: dict[str, Any],
    community_log: dict[str, Any],
    client: OpenRouterClient | None = None,
    seed: int | None = None,
) -> str:
    client = client or OpenRouterClient()
    prompt_template = load_prompt("community_thinking.txt")
    prompt = prompt_template.format(
        persona_prompt=agent.get("persona_prompt", ""),
        best_posts_summary=_format_best_posts(community_log.get("best_posts_seen") or []),
        posts_read_summary=_format_posts_read(community_log.get("posts_read") or []),
        depth=int(agent.get("news_depth") or 0),
    )
    current_prompt = prompt
    for attempt in range(1, 5):
        attempt_seed = stable_llm_seed(seed or 0, "community_thinking_validation", attempt)
        response = await client.chat(
            [{"role": "user", "content": current_prompt}],
            model=config.OPENROUTER_COMMUNITY_MODEL,
            temperature=0.3 if attempt == 1 else 0.1,
            seed=attempt_seed,
            audit_label="community_thinking",
        )
        raw_content = response_content(response)
        content = raw_content.strip()
        if content:
            return content
        errors = ["community_thinking:requires_nonempty_text"]
        record_validation_failure(
            label="community_thinking",
            attempt=attempt,
            errors=errors,
            raw_content=raw_content,
            seed=attempt_seed,
        )
        current_prompt = build_validation_retry_prompt(
            prompt,
            errors=errors,
            schema_hint="커뮤니티 정보에 대한 비어 있지 않은 한국어 생각을 자유 형식 텍스트로 작성하세요.",
            json_only=False,
        )
    raise CommunityValidationError("community thinking was empty after 4 attempts")


def _format_best_posts(best_posts: list[dict[str, Any]]) -> str:
    if not best_posts:
        return "(어제 Best 게시글 없음)"
    return "\n".join(
        f"- [{post.get('post_type', '')}] {post.get('title', '')} (score: {post.get('score', 0)})"
        for post in best_posts
    )


def _format_posts_read(posts_read: list[dict[str, Any]]) -> str:
    if not posts_read:
        return "(어제 직접 읽은 게시글 없음)"
    lines = []
    for post in posts_read:
        badges = ", ".join(post.get("author_badges") or []) or "없음"
        content = str(post.get("content", ""))[:200]
        lines.append(
            f"- [{post.get('post_type', '')}] {post.get('title', '')} | "
            f"내 반응: {post.get('reaction', 'read')} | 작성자 뱃지: {badges}\n"
            f"  내용 요약: {content}"
        )
    return "\n\n".join(lines)
