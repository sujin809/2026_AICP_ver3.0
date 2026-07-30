"""Deterministic human-log projection of committed belief state.

`EXPERIMENT_DESIGN.md` 7.3절은 `belief_summary`를 "저장된 여섯 차원에서
결정론적으로 렌더링하는 사람용 로그"로 고정한다. 모델 출력이 아니고, 다음
STB·analysis·decision 입력도 아니다.

이 규칙을 지키는 경로가 둘이라 renderer를 여기 한 곳에만 둔다.

- LLM 경로: post-fill LTB (`twinmarket_kr/llm/belief.py`)
- 무호출 경로: 결정론적 LTB₀ (`twinmarket_kr/experiment_runtime.py`)

두 경로가 각자 문장을 만들면 같은 필드가 서로 다른 규칙으로 채워진다.
의존성 없는 순수 모듈로 유지해서 base DB를 만드는 04가 LLM client를
끌어오지 않게 한다.
"""

from __future__ import annotations

from typing import Mapping

BELIEF_DIMENSION_KEYS = ("dim_1", "dim_2", "dim_3", "dim_4", "dim_5", "dim_6")


def render_belief_summary(dimensions: Mapping[str, str]) -> str:
    """Render the human belief_summary from the committed six dimensions."""

    missing = [key for key in BELIEF_DIMENSION_KEYS if not dimensions.get(key)]
    if missing:
        raise ValueError(
            "belief_summary requires every dimension: " + ",".join(missing)
        )
    return "\n".join(
        f"{dimension}: {dimensions[dimension]}"
        for dimension in BELIEF_DIMENSION_KEYS
    )
