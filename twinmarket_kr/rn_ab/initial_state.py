"""Deterministic, sealed initial RN long-term belief state."""
from __future__ import annotations

from twinmarket_kr.rn_ab.persona_snapshot import FrozenPersona, parse_persona_v1


def deterministic_initial_ltb(persona: FrozenPersona) -> dict[str, str]:
    """Render LTB₀ only from the frozen canonical persona.

    It intentionally makes no model call and contains no market, news,
    target, portfolio-history, or community information.  Keeping it separate
    lets preflight seal the clean base and runtime verify that exact base.
    """

    profile = parse_persona_v1(persona.persona_prompt)
    strategy = str(profile["strategy"])
    risk = str(profile["bh_lottery_preference_category"])
    disposition = str(profile["bh_disposition_effect_category"])
    depth = int(profile["news_depth"])
    return {
        "dim_1": f"초기 기준에서는 {strategy} 관점으로 새 정보가 확인될 때까지 삼성전자 1개월 방향을 조건부로 판단한다.",
        "dim_2": f"초기 가치 판단은 {strategy} 성향을 따르되 검증 가능한 실적·가치 정보가 들어올 때까지 확신을 제한한다.",
        "dim_3": "초기 거시·업황 판단은 새 cutoff-safe 신호가 없으므로 불확실성을 유지하고 이후 정보로 갱신한다.",
        "dim_4": f"초기 시장심리 평가는 정보 접근 단계 D{depth}의 허용 범위에서만 관찰하고 추정하지 않는다.",
        "dim_5": f"초기 사건 해석은 {strategy} 투자 전략과 현재 허용 정보 범위를 분리해 적용한다.",
        "dim_6": f"초기 위험·규율은 {risk} 위험선호와 {disposition} 처분효과 성향을 의식하되 아직 체결 성과를 평가하지 않는다.",
    }


__all__ = ["deterministic_initial_ltb"]
