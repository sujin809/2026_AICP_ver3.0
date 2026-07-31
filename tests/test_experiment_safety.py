from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from twinmarket_kr.llm.analysis import (
    AnalysisValidationError,
    _nonempty_text_or_string_list,
    analyze_market,
    depth2_pre_search,
    normalize_depth2_post_search,
    normalize_depth2_pre_search,
    validate_depth2_post_search,
    validate_depth2_pre_search,
)
from twinmarket_kr.llm.decision import (
    DecisionConstraintError,
    DecisionValidationError,
    make_decision,
    parse_decision_json,
)
from twinmarket_kr.llm.client import UnexpectedModelError, _is_retryable_error
from twinmarket_kr.run_integrity import summarize_api_audit
from twinmarket_kr.experiment_runtime import (
    ParallelTaskError,
    build_clean_experiment_base,
    classify_restart_safety,
)


class _InvalidDecisionClient:
    async def chat(self, *args, **kwargs):
        return json.dumps(
            {
                "action": "hold",
                "requested_quantity": 0,
                "reason": "invalid hold response",
                "risk_control": "invalid hold response",
            }
        )


class _FailIfCalledClient:
    async def chat(self, *args, **kwargs):
        raise AssertionError("API must not be called")


class _SequenceClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []

    async def chat(self, messages, *args, **kwargs):
        self.prompts.append(messages[0]["content"])
        return self.responses.pop(0)


class PersonaPromptProjectionTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _dimensions(prefix: str) -> dict[str, str]:
        return {
            f"dim_{index}": f"{prefix} dimension {index}"
            for index in range(1, 7)
        }

    async def test_market_analysis_final_prompt_contains_persona_once(self) -> None:
        persona = "PERSONA_SENTINEL_MARKET_ANALYSIS"
        client = _SequenceClient(
            [
                json.dumps(
                    {
                        "market_view": "혼조 흐름이다.",
                        "valuation_view": "판단이 어렵다.",
                        "technical_view": "단기 변동성이 있다.",
                        "news_view": "긍정과 부정 요인이 혼재한다.",
                        "portfolio_view": "현금 여력을 유지할 수 있다.",
                        "key_risks": ["변동성"],
                        "opportunity": "제한적인 매수 기회가 있다.",
                        "caution": "추격 매수는 주의한다.",
                        "confidence": "medium",
                        "directional_stance": "uncertain",
                        "evidence_references": [
                            {"source": "previous_ltb", "field": "dim_1"},
                            {"source": "current_stb", "field": "dim_5"},
                            {"source": "market", "field": "reference_price"},
                            {
                                "source": "execution_state",
                                "field": "max_buy_quantity",
                            },
                        ],
                    },
                    ensure_ascii=False,
                )
            ]
        )

        await analyze_market(
            {"agent_id": "A001", "persona_prompt": persona},
            previous_ltb=self._dimensions("previous"),
            current_stb=self._dimensions("current"),
            market_features={"reference_price": 100_000},
            portfolio_summary="현금 1억원, 보유 0주",
            execution_state={"max_buy_quantity": 500},
            client=client,
            seed=2,
        )

        self.assertEqual(client.prompts[0].count(persona), 1)

    async def test_trading_decision_final_prompt_contains_persona_once(self) -> None:
        persona = "PERSONA_SENTINEL_TRADING_DECISION"
        client = _SequenceClient(
            [
                json.dumps(
                    {
                        "action": "buy",
                        "requested_quantity": 1,
                        "reason": "Belief와 시장 분석을 반영한 제한적 매수다.",
                        "risk_control": "현금 여력을 유지한다.",
                    },
                    ensure_ascii=False,
                )
            ]
        )
        constraints = {
            "available_cash": 100_000_000,
            "current_quantity": 0,
            "current_price": 100_000,
            "min_order_unit": 1,
            "max_buy_quantity": 500,
            "max_sell_quantity": 0,
            "allow_hold": False,
            "allowed_actions": ["buy"],
        }

        await make_decision(
            {"agent_id": "A001", "persona_prompt": persona},
            self._dimensions("previous"),
            self._dimensions("current"),
            {"directional_stance": "buy"},
            "현금 1억원, 보유 0주",
            constraints,
            client=client,
            seed=2,
        )

        self.assertEqual(client.prompts[0].count(persona), 1)


class DecisionSafetyTest(unittest.IsolatedAsyncioTestCase):
    def test_fractional_or_unexplained_order_is_invalid(self) -> None:
        constraints = {
            "current_price": 100_000,
            "min_order_unit": 1,
            "max_buy_quantity": 500,
            "max_sell_quantity": 0,
            "allow_hold": False,
            "allowed_actions": ["buy"],
        }
        parsed = parse_decision_json(
            json.dumps(
                {
                    "action": "buy",
                    "requested_quantity": 1.5,
                    "reason": "",
                    "risk_control": "",
                }
            ),
            constraints,
        )
        self.assertFalse(parsed["valid"])
        self.assertIn("invalid_quantity_format", parsed["validation_errors"])
        self.assertIn("missing_reason", parsed["validation_errors"])
        self.assertIn("missing_risk_control", parsed["validation_errors"])

    async def test_invalid_output_never_becomes_one_share_fallback(self) -> None:
        constraints = {
            "available_cash": 100_000_000,
            "current_quantity": 0,
            "current_price": 100_000,
            "min_order_unit": 1,
            "max_buy_quantity": 500,
            "max_sell_quantity": 0,
            "allow_hold": False,
            "allowed_actions": ["buy"],
        }
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(
                config,
                "OPENROUTER_AUDIT_LOG",
                Path(directory) / "openrouter_calls.jsonl",
            ):
                with self.assertRaises(DecisionValidationError):
                    dimensions = {
                        f"dim_{index}": f"test belief dimension {index}"
                        for index in range(1, 7)
                    }
                    await make_decision(
                        {"persona_prompt": "test"},
                        dimensions,
                        dimensions,
                        {"directional_stance": "uncertain"},
                        "test portfolio",
                        constraints,
                        client=_InvalidDecisionClient(),
                        seed=2,
                        validation_attempts=2,
                    )

    async def test_no_allowed_action_pauses_without_api_call(self) -> None:
        constraints = {
            "available_cash": 0,
            "current_quantity": 0,
            "current_price": 100_000,
            "min_order_unit": 1,
            "max_buy_quantity": 0,
            "max_sell_quantity": 0,
            "allow_hold": False,
            "allowed_actions": [],
        }
        with self.assertRaises(DecisionConstraintError):
            await make_decision(
                {"persona_prompt": "test"},
                {"belief_summary": "test"},
                {},
                "test portfolio",
                "no history",
                constraints,
                client=_FailIfCalledClient(),
                seed=2,
            )


class ApiAuditSafetyTest(unittest.TestCase):
    def test_unexpected_returned_model_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            audit_path = Path(directory) / "openrouter_calls.jsonl"
            audit_path.write_text(
                json.dumps(
                    {
                        "status": "success",
                        "requested_model": "qwen/qwen3.5-flash-02-23",
                        "returned_model": "another/model",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(RuntimeError):
                summarize_api_audit(
                    audit_path,
                    expected_model="qwen/qwen3.5-flash-02-23",
                )

    def test_model_mismatch_is_not_retried(self) -> None:
        self.assertFalse(_is_retryable_error(UnexpectedModelError("wrong model")))

    def test_unknown_local_error_is_not_retried_as_network_failure(self) -> None:
        self.assertFalse(_is_retryable_error(ValueError("invalid local request")))


class AnalysisSchemaSafetyTest(unittest.IsolatedAsyncioTestCase):
    def test_market_analysis_accepts_explanatory_text_or_string_list(self) -> None:
        self.assertTrue(_nonempty_text_or_string_list("명확한 기회 설명"))
        self.assertTrue(_nonempty_text_or_string_list(["위험 1", "위험 2"]))
        self.assertFalse(_nonempty_text_or_string_list(""))
        self.assertFalse(_nonempty_text_or_string_list([]))
        self.assertFalse(_nonempty_text_or_string_list([1]))

    def test_depth2_pre_accepts_historical_list_and_string_encodings(self) -> None:
        historical_list = normalize_depth2_pre_search(
            {
                "key_findings": ["핵심 1", "핵심 2"],
                "curiosity_points": ["쟁점"],
                "search_rationale": "추가 확인 필요",
                "search_keywords": ["HBM", "환율", "외국인"],
            }
        )
        self.assertEqual(validate_depth2_pre_search(historical_list), [])
        historical_string = normalize_depth2_pre_search(
            {
                "key_findings": "핵심 한 문장",
                "curiosity_points": "추가 쟁점",
                "search_rationale": "추가 확인 필요",
                "search_keywords": ["HBM", "환율", "외국인"],
            }
        )
        self.assertEqual(historical_string["key_findings"], ["핵심 한 문장"])
        self.assertEqual(historical_string["curiosity_points"], ["추가 쟁점"])
        self.assertEqual(validate_depth2_pre_search(historical_string), [])

    def test_depth2_post_accepts_observed_new_findings_encodings(self) -> None:
        value = normalize_depth2_post_search(
            {
                "new_findings": "새 내용",
                "view_change": "유지",
                "view_change_detail": "기존 판단을 유지한다.",
                "unresolved_questions": [],
            }
        )
        self.assertEqual(value["new_findings"], ["새 내용"])
        self.assertEqual(validate_depth2_post_search(value), [])
        value["new_findings"] = []
        self.assertEqual(validate_depth2_post_search(value), [])

    async def test_invalid_depth2_response_is_corrected_with_feedback_and_audited(self) -> None:
        client = _SequenceClient(
            [
                json.dumps(
                    {
                        "key_findings": [],
                        "curiosity_points": ["쟁점"],
                        "search_rationale": "추가 확인 필요",
                        "search_keywords": ["HBM", "환율", "외국인"],
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "key_findings": ["핵심"],
                        "curiosity_points": ["쟁점"],
                        "search_rationale": "추가 확인 필요",
                        "search_keywords": ["HBM", "환율", "외국인"],
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            audit = Path(directory) / "openrouter_calls.jsonl"
            with patch.object(config, "OPENROUTER_AUDIT_LOG", audit):
                result = await depth2_pre_search(
                    {"persona_prompt": "test persona"},
                    {},
                    client=client,
                    seed=2,
                )
            validation_rows = [
                json.loads(line)
                for line in (Path(directory) / "llm_validation_errors.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
        self.assertEqual(result["generation_attempts"], 2)
        self.assertIn("key_findings:requires_nonempty_string_list", client.prompts[1])
        self.assertEqual(len(validation_rows), 1)
        self.assertEqual(validation_rows[0]["label"], "depth2_pre_search")


class RestartSafetyTest(unittest.TestCase):
    def test_schema_failure_is_never_process_restarted(self) -> None:
        result = classify_restart_safety(AnalysisValidationError("invalid schema"))
        self.assertFalse(result["auto_restart_allowed"])
        self.assertEqual(result["failure_class"], "deterministic_validation_or_model")

    def test_wrapped_timeout_can_restart_from_checkpoint(self) -> None:
        timeout = TimeoutError("provider timed out")
        wrapped = RuntimeError("OpenRouter chat failed")
        wrapped.__cause__ = timeout
        result = classify_restart_safety(wrapped)
        self.assertTrue(result["auto_restart_allowed"])
        self.assertEqual(result["failure_class"], "transient_provider_or_transport")

    def test_unknown_local_failure_does_not_loop(self) -> None:
        result = classify_restart_safety(RuntimeError("local invariant failed"))
        self.assertFalse(result["auto_restart_allowed"])
        self.assertEqual(result["failure_class"], "unknown_or_local_error")

    def test_mixed_parallel_errors_do_not_hide_schema_failure(self) -> None:
        mixed = ParallelTaskError(
            "two agents failed",
            [TimeoutError("provider timed out"), AnalysisValidationError("invalid schema")],
        )
        result = classify_restart_safety(mixed)
        self.assertFalse(result["auto_restart_allowed"])
        self.assertEqual(result["failure_class"], "deterministic_validation_or_model")


class ExperimentBaseSafetyTest(unittest.TestCase):
    def test_source_database_cannot_be_overwritten_as_the_base(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "same.db"
            with self.assertRaises(ValueError):
                build_clean_experiment_base(path, path, overwrite=True)


if __name__ == "__main__":
    unittest.main()


def test_no_undefined_names_in_runtime_modules() -> None:
    """미정의 이름은 compileall이 못 잡고 유료 실행에서야 터진다.

    시드 원복 중 남은 ``attempt_salt`` 참조가 전체 테스트(200 passed)와
    compileall을 모두 통과한 뒤, 유료 실행 첫 턴에서 100 agent NameError로
    터졌다. 함수 안에서 전역으로 해석되는 이름이 모듈에도 builtins에도
    없으면 실행 시 NameError이므로 여기서 막는다.
    """

    import builtins
    import symtable
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    failures: list[str] = []
    for path in sorted((root / "twinmarket_kr").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        top = symtable.symtable(source, str(path), "exec")
        module_names = {
            symbol.get_name()
            for symbol in top.get_symbols()
            if symbol.is_assigned() or symbol.is_imported() or symbol.is_namespace()
        }

        def walk(table: symtable.SymbolTable) -> None:
            if table.get_type() == "function":
                for symbol in table.get_symbols():
                    name = symbol.get_name()
                    if (
                        symbol.is_global()
                        and symbol.is_referenced()
                        and name not in module_names
                        and not hasattr(builtins, name)
                    ):
                        failures.append(
                            f"{path.relative_to(root)} {table.get_name()}(): {name}"
                        )
            for child in table.get_children():
                walk(child)

        walk(top)
    assert not failures, "미정의 이름:\n" + "\n".join(sorted(set(failures)))
