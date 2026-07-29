from __future__ import annotations

import json
import unittest
from typing import Any

from twinmarket_kr.community.posting import posting_decision
from twinmarket_kr.llm.analysis import analyze_market
from twinmarket_kr.llm.belief import (
    generate_short_term_belief,
    update_long_term_belief,
)
from twinmarket_kr.llm.decision import (
    build_trading_constraints,
    make_decision,
)


def _dimensions(label: str) -> dict[str, str]:
    return {
        f"dim_{index}": f"{label} dimension {index}"
        for index in range(1, 7)
    }


def _evidence(
    *,
    dim_1_support: list[str] | None = None,
    dim_6_support: list[str] | None = None,
) -> dict[str, dict[str, list[str]]]:
    result = {
        f"dim_{index}": {"support": [], "contradict": []}
        for index in range(1, 7)
    }
    result["dim_1"]["support"] = list(dim_1_support or [])
    result["dim_6"]["support"] = list(dim_6_support or [])
    return result


def _stage_payload(prompt: str) -> dict[str, Any]:
    marker = "입력 정보(JSON):"
    if marker not in prompt:
        raise AssertionError("prompt does not contain the integrated stage payload marker")
    return json.loads(prompt.rsplit(marker, 1)[1].strip())


class _CaptureClient:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = json.dumps(response, ensure_ascii=False)
        self.prompts: list[str] = []
        self.kwargs: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[dict[str, str]],
        **kwargs: Any,
    ) -> str:
        self.prompts.append(messages[-1]["content"])
        self.kwargs.append(dict(kwargs))
        return self.response


class IntegratedMemoryPromptWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_stb_uses_current_only_top_level_prompt_and_six_dimensions(
        self,
    ) -> None:
        client = _CaptureClient(
            {
                **_dimensions("stb"),
                "dimension_evidence": _evidence(
                    dim_1_support=["news:current"],
                ),
            }
        )
        result = await generate_short_term_belief(
            {
                "agent_id": "agent-1",
                "news_depth": 1,
                "persona_prompt": "legacy persona",
            },
            event={
                "event_id": "2026-02-27/AM",
                "turn": 1,
                "date": "2026-02-27",
                "subturn": "am",
            },
            current_evidence={
                "news": [
                    {
                        "evidence_id": "news:current",
                        "title": "현재 턴 뉴스",
                    }
                ],
                "depth2_search_results": [],
                "community_claims": [],
            },
            allowed_evidence_ids={"news:current"},
            client=client,
            seed=7,
            validation_attempts=1,
        )

        self.assertEqual(client.kwargs[0]["audit_label"], "short_term_belief")
        self.assertIn(
            "이 Short-Term Belief 하나만으로 거래를 결정하지 않습니다.",
            client.prompts[0],
        )
        payload = _stage_payload(client.prompts[0])
        self.assertEqual(
            set(payload),
            {
                "schema_version",
                "persona",
                "event",
                "current_evidence",
                "sanitized_evidence_registry",
            },
        )
        forbidden = {
            "previous_ltb",
            "transaction_episode",
            "eligible_price_outcomes_dim_6_only",
            "belief_summary",
            "view_change",
        }
        self.assertTrue(forbidden.isdisjoint(payload))
        self.assertEqual(
            result["dimension_evidence"]["dim_1"]["support"],
            ["news:current"],
        )

    async def test_ltb_uses_recursive_post_fill_prompt_and_only_due_outcomes(
        self,
    ) -> None:
        due_outcome_id = "outcome:fill-previous:h1"
        integration = _evidence(
            dim_1_support=["news:current"],
            dim_6_support=[due_outcome_id],
        )
        client = _CaptureClient(
            {
                **_dimensions("post-fill ltb"),
                "integration_evidence": integration,
            }
        )
        result = await update_long_term_belief(
            {
                "agent_id": "agent-1",
                "news_depth": 1,
                "persona_prompt": "legacy persona",
            },
            event={
                "event_id": "2026-02-27/PM",
                "turn": 2,
                "date": "2026-02-27",
                "subturn": "pm",
            },
            previous_ltb={
                "dimensions": _dimensions("previous ltb"),
                "belief_summary": "HUMAN_SUMMARY_MUST_NOT_ENTER_LTB_PROMPT",
                "view_change": "HUMAN_CHANGE_MUST_NOT_ENTER_LTB_PROMPT",
            },
            current_stb={
                "dimensions": _dimensions("current stb"),
                "dimension_evidence": _evidence(
                    dim_1_support=["news:current"],
                ),
            },
            transaction_episode={
                "fill_id": "fill:current",
                "decision_id": "decision:current",
                "action": "buy",
                "requested_quantity": 2,
                "filled_quantity": 2,
                "executed_price": 100.0,
                "fee": 0.0,
            },
            eligible_price_outcomes_dim_6_only=[
                {
                    "outcome_id": due_outcome_id,
                    "horizon": "h1",
                    "observed_event_id": "2026-02-27/PM",
                    "action_aligned_markout": 0.05,
                }
            ],
            client=client,
            seed=8,
            validation_attempts=1,
        )

        self.assertEqual(
            client.kwargs[0]["audit_label"],
            "post_fill_long_term_belief",
        )
        self.assertIn(
            "핵심 관점이 이어지는 이유를 담은 새 문장",
            client.prompts[0],
        )
        payload = _stage_payload(client.prompts[0])
        self.assertEqual(
            set(payload),
            {
                "schema_version",
                "persona",
                "event",
                "previous_ltb",
                "current_stb",
                "transaction_episode",
                "eligible_price_outcomes_dim_6_only",
                "sanitized_evidence_registry",
            },
        )
        self.assertEqual(
            set(payload["previous_ltb"]),
            {f"dim_{index}" for index in range(1, 7)},
        )
        self.assertEqual(
            payload["transaction_episode"]["fill_id"],
            "fill:current",
        )
        self.assertEqual(
            [
                row["outcome_id"]
                for row in payload["eligible_price_outcomes_dim_6_only"]
            ],
            [due_outcome_id],
        )
        self.assertNotIn("HUMAN_SUMMARY_MUST_NOT_ENTER_LTB_PROMPT", client.prompts[0])
        self.assertNotIn("HUMAN_CHANGE_MUST_NOT_ENTER_LTB_PROMPT", client.prompts[0])
        self.assertEqual(
            result["integration_evidence"]["dim_6"]["support"],
            [due_outcome_id],
        )
        self.assertIn("belief_summary", result)
        self.assertIn("view_change", result)

    async def test_analysis_and_decision_compare_both_beliefs_without_human_logs(
        self,
    ) -> None:
        previous = {
            "dimensions": _dimensions("previous ltb"),
            "belief_summary": "HUMAN_SUMMARY_MUST_NOT_ENTER_DECISION",
            "view_change": "HUMAN_CHANGE_MUST_NOT_ENTER_DECISION",
        }
        current = {
            "dimensions": _dimensions("current stb"),
            "dimension_evidence": _evidence(),
            "belief_summary": "STB_SUMMARY_MUST_NOT_ENTER_DECISION",
        }
        analysis_client = _CaptureClient(
            {
                "market_view": "혼조",
                "valuation_view": "판단 어려움",
                "technical_view": "변동성 확인 필요",
                "news_view": "현재 신호가 엇갈림",
                "portfolio_view": "현금과 보유량을 함께 고려",
                "key_risks": ["정보 불확실성"],
                "opportunity": "제약 안의 제한적 대응",
                "caution": "과신 금지",
                "confidence": "medium",
                "directional_stance": "uncertain",
                "evidence_references": [
                    {"source": "previous_ltb", "field": "dim_1"},
                    {"source": "current_stb", "field": "dim_1"},
                    {"source": "market", "field": "close"},
                    {"source": "execution_state", "field": "available_cash"},
                ],
            }
        )
        constraints = build_trading_constraints(
            available_cash=1_000.0,
            current_quantity=5,
            current_price=100.0,
            max_single_trade_cash_ratio=0.5,
        )
        analysis = await analyze_market(
            {"agent_id": "agent-1", "persona_prompt": "legacy persona"},
            previous_ltb=previous,
            current_stb=current,
            market_features={"close": 100.0},
            portfolio_summary="cash=1000, quantity=5",
            execution_state=constraints,
            client=analysis_client,
            seed=9,
        )
        analysis_prompt = analysis_client.prompts[0]
        analysis_payload = _stage_payload(analysis_prompt)
        self.assertIn(
            "단순히 이어 붙이거나 어느 하나만 따르지 말고",
            analysis_prompt,
        )
        self.assertEqual(
            set(analysis_payload["previous_ltb"]),
            {f"dim_{index}" for index in range(1, 7)},
        )
        self.assertEqual(
            set(analysis_payload["current_stb"]),
            {f"dim_{index}" for index in range(1, 7)},
        )

        decision_client = _CaptureClient(
            {
                "action": "buy",
                "requested_quantity": 2,
                "reason": "LTB와 STB의 충돌을 시장 분석과 함께 종합했다.",
                "risk_control": "현금 제약 안에서 수량을 제한했다.",
            }
        )
        decision = await make_decision(
            {"agent_id": "agent-1", "persona_prompt": "legacy persona"},
            previous,
            current,
            analysis,
            "cash=1000, quantity=5",
            constraints,
            client=decision_client,
            seed=10,
            validation_attempts=1,
        )
        decision_prompt = decision_client.prompts[0]
        decision_payload = _stage_payload(decision_prompt)
        self.assertIn(
            "단순히 이어 붙이거나 Short-Term Belief만 따르지 말고",
            decision_prompt,
        )
        self.assertEqual(
            set(decision_payload["previous_ltb"]),
            {f"dim_{index}" for index in range(1, 7)},
        )
        self.assertEqual(
            set(decision_payload["current_stb"]),
            {f"dim_{index}" for index in range(1, 7)},
        )
        for sentinel in (
            "HUMAN_SUMMARY_MUST_NOT_ENTER_DECISION",
            "HUMAN_CHANGE_MUST_NOT_ENTER_DECISION",
            "STB_SUMMARY_MUST_NOT_ENTER_DECISION",
        ):
            self.assertNotIn(sentinel, analysis_prompt)
            self.assertNotIn(sentinel, decision_prompt)
        self.assertEqual((decision["action"], decision["quantity"]), ("buy", 2))

    async def test_posting_uses_post_fill_ltb_view_change_and_structured_pm_fill(
        self,
    ) -> None:
        client = _CaptureClient(
            {
                "will_post": True,
                "post_type": "impression",
                "title": "오늘 생각",
                "content": "장기 관점과 오늘 체결을 함께 돌아본 짧은 글입니다.",
            }
        )
        committed_ltb = {
            "ltb_id": "ltb:post-fill:pm",
            "dimensions": _dimensions("post-fill ltb"),
            "belief_summary": "HUMAN_SUMMARY_MUST_NOT_ENTER_POST",
            "view_change": {
                "dim_1": {
                    "before": "이전 전망",
                    "after": "체결 후 전망",
                }
            },
        }
        committed_fill = {
            "fill_id": "fill:pm",
            "decision_id": "decision:pm",
            "action": "sell",
            "requested_quantity": 2,
            "filled_quantity": 2,
            "executed_price": 105.0,
            "fee": 0.0,
            "private_extra": "PRIVATE_FILL_FIELD_MUST_NOT_ENTER_POST",
        }
        result = await posting_decision(
            {"agent_id": "agent-1", "persona_prompt": "legacy persona"},
            committed_ltb=committed_ltb,
            committed_fill=committed_fill,
            date="2026-02-27",
            client=client,
            seed=11,
        )

        prompt = client.prompts[0]
        self.assertIn(
            "현재 post-fill Long-Term Belief, 그 Belief와 연결된 관점 변화, "
            "오늘의 실제 PM 체결을 함께 활용",
            prompt,
        )
        self.assertIn("ltb:post-fill:pm", result["source_ltb_id"])
        self.assertEqual(result["source_fill_id"], "fill:pm")
        self.assertEqual(result["source_decision_id"], "decision:pm")
        self.assertEqual(result["source_view_change"], committed_ltb["view_change"])
        self.assertIn('"fill_id": "fill:pm"', prompt)
        self.assertIn('"after": "체결 후 전망"', prompt)
        self.assertNotIn("HUMAN_SUMMARY_MUST_NOT_ENTER_POST", prompt)
        self.assertNotIn("PRIVATE_FILL_FIELD_MUST_NOT_ENTER_POST", prompt)


if __name__ == "__main__":
    unittest.main()
