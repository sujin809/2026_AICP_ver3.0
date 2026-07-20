from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import config
from twinmarket_kr.agents.memory_agent import MemoryAgent
from twinmarket_kr.llm.client import OpenRouterClient, response_content, stable_llm_seed


BELIEF_KEYS = ("dim_1", "dim_2", "dim_3", "dim_4", "dim_5", "dim_6", "belief_summary", "view_change")


class BeliefValidationError(RuntimeError):
    pass


def load_prompt(name: str) -> str:
    return (config.PROMPT_DIR / name).read_text(encoding="utf-8")


def _limits() -> dict[str, int]:
    return {f"{key}_limit": value for key, value in config.BELIEF_LIMITS.items()}


def parse_belief_json(content: str) -> dict[str, str]:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        data = json.loads(text or "{}")
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    for key in BELIEF_KEYS:
        data.setdefault(key, "")
    return {
        key: str(data[key]) if isinstance(data[key], (str, int, float)) and not isinstance(data[key], bool) else ""
        for key in BELIEF_KEYS
    }


def offline_initial_belief(agent: dict[str, Any]) -> dict[str, str]:
    strategy = agent.get("strategy", "value")
    if strategy == "technical":
        dim_2 = "기술적 흐름과 거래량 신호가 확인되기 전까지 중립적으로 본다."
    else:
        dim_2 = "대형 우량주로서 장기 가치는 보지만 현재 가격의 저평가 여부를 확인해야 한다."
    return {
        "dim_1": "초기에는 삼성전자의 한 달 방향을 중립으로 보며 확인된 신호를 기다린다.",
        "dim_2": dim_2,
        "dim_3": "반도체 업황, 환율, 금리 흐름이 판단의 핵심 변수다.",
        "dim_4": "시장 심리가 과열되면 신중하고, 위축되면 기회를 찾는 태도다.",
        "dim_5": "뉴스는 제목과 핵심 내용을 확인하되 자신의 투자 성향에 맞게 해석한다.",
        "dim_6": "초기 판단에는 불확실성이 있어 현금 관리와 원칙 준수를 중시한다.",
        "belief_summary": "초기에는 삼성전자에 대해 중립적 관점을 유지한다. 페르소나상 투자 전략에 맞춰 시장 데이터와 뉴스를 확인한 뒤 방향성을 조정할 것이다.",
        "view_change": "initial",
    }


async def generate_initial_belief(
    agent: dict[str, Any],
    *,
    client: OpenRouterClient | None = None,
    memory: MemoryAgent | None = None,
    date: str = "t000",
    offline: bool = False,
) -> dict[str, Any]:
    if offline:
        parsed = offline_initial_belief(agent)
    else:
        client = client or OpenRouterClient()
        prompt = load_prompt("initial_belief.txt").format(
            persona_prompt=agent["persona_prompt"],
            **_limits(),
        )
        response = await client.chat(
            [{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.2,
            audit_label="initial_belief",
        )
        parsed = parse_belief_json(response_content(response) or "{}")
    belief = {"agent_id": agent["agent_id"], "turn": 0, "date": date, **parsed}
    if memory is not None:
        memory.save_belief(belief)
    return belief


async def update_belief(
    agent: dict[str, Any],
    today_context: dict[str, Any],
    *,
    client: OpenRouterClient | None = None,
    memory: MemoryAgent | None = None,
    seed: int | None = None,
    validation_attempts: int = 4,
) -> dict[str, Any]:
    client = client or OpenRouterClient()
    prompt = load_prompt("update_belief.txt").format(
        persona_prompt=agent["persona_prompt"],
        today_context=json.dumps(today_context, ensure_ascii=False, indent=2),
        **_limits(),
    )
    if validation_attempts < 1:
        raise ValueError("validation_attempts must be at least 1")
    parsed: dict[str, str] | None = None
    invalid_history: list[list[str]] = []
    for attempt in range(1, validation_attempts + 1):
        response = await client.chat(
            [{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.2 if attempt == 1 else 0.1,
            seed=stable_llm_seed(seed or 0, "belief_validation", attempt),
            audit_label="belief_update",
        )
        candidate = parse_belief_json(response_content(response) or "{}")
        errors = [key for key in BELIEF_KEYS if not candidate.get(key, "").strip()]
        if not errors:
            parsed = candidate
            break
        invalid_history.append(errors)
    if parsed is None:
        raise BeliefValidationError(
            "LLM did not produce a complete belief after "
            f"{validation_attempts} validation attempts; empty keys={invalid_history}"
        )
    belief = {
        "agent_id": agent["agent_id"],
        "turn": int(today_context["turn"]),
        "date": today_context["date"],
        "generation_attempts": len(invalid_history) + 1,
        **parsed,
    }
    if memory is not None:
        memory.save_belief(belief)
    return belief
