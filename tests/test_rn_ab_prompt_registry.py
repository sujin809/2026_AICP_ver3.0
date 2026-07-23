from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from twinmarket_kr.rn_ab.prompt_registry import (
    ALL_PROMPT_FILENAMES,
    ANALYSIS_STAGE,
    COMMUNITY_INTERPRETATION_STAGE,
    COMMUNITY_POSTING_STAGE,
    COMMUNITY_READING_STAGE,
    DECISION_STAGE,
    LTB_STAGE,
    PAYLOAD_SENTINEL,
    RNPromptBundle,
    RNPromptError,
    STB_STAGE,
)
from twinmarket_kr.rn_ab.prompt_contracts import belief_output_contract


class RNPromptRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "prompts"
        self.root.mkdir()
        source = Path(__file__).resolve().parents[1] / "prompts" / "common"
        for name in ALL_PROMPT_FILENAMES:
            shutil.copy2(source / name, self.root / name)

    @staticmethod
    def _belief_payload(*, evidence_field: str, dim_1_limit: int = 150) -> dict:
        limits = {"dim_1": dim_1_limit, **{f"dim_{index}": 100 for index in range(2, 7)}}
        return {
            "schema_version": "test-belief-v1",
            "output_contract": belief_output_contract(
                limits=limits,
                evidence_field=evidence_field,
            ),
        }

    @staticmethod
    def _legacy_stage_payload(*, schema_version: str) -> dict:
        dimensions = {f"dim_{index}": f"belief {index}" for index in range(1, 7)}
        execution_state = {
            "available_cash": 1_000,
            "current_quantity": 0,
            "max_buy_quantity": 5,
            "max_sell_quantity": 0,
            "allowed_actions": ["buy"],
            "price_label": "sealed_open",
            "announced_price": 100,
        }
        return {
            "schema_version": schema_version,
            "persona": {"persona_sha256": "a" * 64, "persona_text": "테스트 페르소나"},
            "previous_ltb": dimensions,
            "current_stb": dimensions,
            "market": {
                "reference_price": 100,
                "previous_close": 99,
                "open_price": 100,
                "subturn": "am",
                "as_of_timestamp": "2026-01-01T09:00:00+09:00",
            },
            "execution_state": execution_state,
            "order_history": [],
            "analysis": {
                "market_view": "mixed",
                "valuation_view": "fair",
                "technical_view": "mixed",
                "news_view": "neutral",
                "portfolio_view": "cash available",
                "key_risks": ["volatility"],
                "opportunity": "limited entry",
                "caution": "avoid concentration",
                "confidence": "medium",
                "directional_stance": "buy",
                "evidence_references": [
                    {"source": "previous_ltb", "field": "dim_1"},
                    {"source": "current_stb", "field": "dim_1"},
                    {"source": "market", "field": "reference_price"},
                    {"source": "execution_state", "field": "max_buy_quantity"},
                ],
            },
        }

    def test_common_contains_the_complete_legacy_set_with_only_two_belief_role_files(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        common = project_root / "prompts" / "common"
        self.assertEqual(
            {path.name for path in common.glob("*.txt")},
            {
                "community_reading.txt",
                "community_thinking.txt",
                "initial_belief.txt",
                "make_decision.txt",
                "market_analysis.txt",
                "news_agent_post_search.txt",
                "news_agent_pre_search.txt",
                "news_interpretation.txt",
                "posting_decision.txt",
                "update_short_term_belief.txt",
                "update_long_term_belief.txt",
            },
        )
        # Four support roles retain the 0720 bytes. community_thinking changes
        # the output contract to the exact next-AM claim schema, while
        # community_reading changes only its public-profile privacy boundary
        # plus the required active select/react JSON contract, and
        # posting_decision makes both conditional JSON examples strict and
        # parseable.
        for filename in (
            "initial_belief.txt",
            "news_agent_post_search.txt",
            "news_agent_pre_search.txt",
            "news_interpretation.txt",
        ):
            with self.subTest(filename=filename):
                self.assertEqual(
                    (project_root / "prompts" / filename).read_bytes(),
                    (common / filename).read_bytes(),
                )

    def test_explicit_directory_bundle_hashes_and_renders_one_payload(self) -> None:
        bundle = RNPromptBundle.load(prompt_dir=self.root)
        payload = self._legacy_stage_payload(schema_version="test-v1")
        payload["nested"] = {"korean": "값"}
        rendered = bundle.render(DECISION_STAGE, payload)
        self.assertNotIn(PAYLOAD_SENTINEL, rendered)
        self.assertNotIn("{{", rendered)
        self.assertIn('"requested_quantity": 10', rendered)
        self.assertEqual(rendered.count('"schema_version":"test-v1"'), 1)
        self.assertEqual(bundle.manifest()["artifact_type"], "rn_prompt_bundle")
        self.assertEqual(
            [template["stage"] for template in bundle.manifest()["templates"]],
            ["stb", "analysis", "decision", "post_fill_ltb"],
        )
        self.assertEqual(len(bundle.manifest()["templates"]), 4)
        self.assertEqual(len(bundle.manifest()["support_templates"]), 7)
        self.assertEqual(bundle.manifest()["version"], "rn-prompt-bundle-v2")
        runtime_use = {
            item["stage"]: item["runtime_use"]
            for item in bundle.manifest()["support_templates"]
        }
        self.assertEqual(
            runtime_use[COMMUNITY_POSTING_STAGE],
            "conditional_post_pm_journaled_call",
        )
        self.assertIn("no_model_call", runtime_use["initial_belief"])
        self.assertIn("no_model_call", runtime_use["news_pre_search"])
        self.assertEqual(bundle.template(ANALYSIS_STAGE).filename, "market_analysis.txt")

    def test_support_prompts_render_only_their_exact_source_slots(self) -> None:
        bundle = RNPromptBundle.load(prompt_dir=self.root)
        posting = bundle.render_support(
            COMMUNITY_POSTING_STAGE,
            {
                "persona_prompt": "페르소나 {not_a_slot}",
                "ltb_dimensions": {
                    f"dim_{index}": f"장기 관점 {index}"
                    for index in range(1, 7)
                },
                "view_change": {"dim_1": {"before": "a", "after": "b"}},
                "committed_pm_fill": {"action": "buy"},
                "date": "2026-01-02",
                "post_types_guide": "guide",
            },
        )
        self.assertIn("페르소나 {not_a_slot}", posting)
        self.assertIn('"dim_6":"장기 관점 6"', posting)
        self.assertIn('\"action\":\"buy\"', posting)
        self.assertNotIn("belief_summary", posting)
        self.assertNotIn("trade_summary", posting)
        self.assertIn("완전히 일치하도록 억지로 맞추지 않아도 됩니다.", posting)
        self.assertIn('{\n  \"will_post\"', posting)
        reading = bundle.render_support(
            COMMUNITY_READING_STAGE,
            {
                "persona_prompt": "persona",
                "mode": "select",
                "post_list_str": [{"post_id": "post:1", "title": "t"}],
                "read_limit": 1,
                "posts_content_str": [],
            },
        )
        self.assertIn('"post_id":"post:1"', reading)
        self.assertIn('"selected_post_ids": []', reading)
        self.assertNotIn("### react 모드", reading)
        self.assertNotIn('"reactions"', reading)
        select_example = '{\n  "selected_post_ids": []\n}'
        self.assertEqual(json.loads(select_example), {"selected_post_ids": []})
        self.assertIn(select_example, reading)
        reacting = bundle.render_support(
            COMMUNITY_READING_STAGE,
            {
                "persona_prompt": "persona",
                "mode": "react",
                "post_list_str": [],
                "read_limit": 1,
                "posts_content_str": [{"post_id": "post:1", "body": "text"}],
            },
        )
        self.assertIn('"reactions": []', reacting)
        self.assertNotIn("### select 모드", reacting)
        self.assertNotIn('"selected_post_ids"', reacting)
        react_example = '{\n  "reactions": []\n}'
        self.assertEqual(json.loads(react_example), {"reactions": []})
        self.assertIn(react_example, reacting)
        interpretation = bundle.render_support(
            COMMUNITY_INTERPRETATION_STAGE,
            {
                "persona_prompt": "persona",
                "candidate_posts_summary": [
                    {
                        "content_level": "title_only",
                        "title": "candidate",
                        "reader_reaction": None,
                        "source_exposure_ids": ["exp:title"],
                    }
                ],
                "best_posts_summary": [{"source_exposure_ids": ["exp:1"]}],
                "posts_read_summary": [],
            },
        )
        self.assertIn('"source_exposure_ids":["exp:1"]', interpretation)
        self.assertIn('"source_exposure_ids":["exp:title"]', interpretation)
        self.assertIn("supporting_quote", interpretation)
        self.assertIn("title_only", interpretation)
        self.assertIn("observed_sentiment", interpretation)

    def test_requires_common_analysis_template(self) -> None:
        (self.root / "market_analysis.txt").unlink()
        with self.assertRaisesRegex(RNPromptError, "analysis prompt"):
            RNPromptBundle.load(prompt_dir=self.root)

    def test_requires_and_validates_every_support_template(self) -> None:
        target = self.root / "posting_decision.txt"
        original = target.read_text(encoding="utf-8")
        target.unlink()
        with self.assertRaisesRegex(RNPromptError, "community_posting support prompt"):
            RNPromptBundle.load(prompt_dir=self.root)
        target.write_text(original + "\n{unapproved_runtime_slot}\n", encoding="utf-8")
        with self.assertRaisesRegex(RNPromptError, "unapproved template field"):
            RNPromptBundle.load(prompt_dir=self.root)

    def test_rejects_community_reading_pseudo_json_example(self) -> None:
        target = self.root / "community_reading.txt"
        original = target.read_text(encoding="utf-8")
        target.write_text(
            original.replace('"selected_post_ids": []', '"selected_post_ids": [...]'),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(RNPromptError, "pseudo-JSON"):
            RNPromptBundle.load(prompt_dir=self.root)

    def test_rejects_template_with_missing_or_duplicate_sentinel(self) -> None:
        target = self.root / "make_decision.txt"
        target.write_text("no payload marker", encoding="utf-8")
        with self.assertRaisesRegex(RNPromptError, "exactly one"):
            RNPromptBundle.load(prompt_dir=self.root)
        target.write_text(f"{PAYLOAD_SENTINEL}\n{PAYLOAD_SENTINEL}\n", encoding="utf-8")
        with self.assertRaisesRegex(RNPromptError, "exactly one"):
            RNPromptBundle.load(prompt_dir=self.root)

    def test_runtime_text_may_contain_template_like_literals_without_becoming_tokens(self) -> None:
        bundle = RNPromptBundle.load(prompt_dir=self.root)
        payload = self._legacy_stage_payload(schema_version="sentinel-v1")
        payload["persona"]["persona_text"] = (
            f"문자열 {PAYLOAD_SENTINEL} 및 {{today_belief}}는 데이터일 뿐입니다."
        )
        payload["previous_ltb"]["dim_1"] = f"{{not_a_slot}} {PAYLOAD_SENTINEL}"
        rendered = bundle.render(DECISION_STAGE, payload)
        self.assertIn(f"문자열 {PAYLOAD_SENTINEL} 및 {{today_belief}}는 데이터일 뿐입니다.", rendered)
        self.assertIn(f"{{not_a_slot}} {PAYLOAD_SENTINEL}", rendered)
        self.assertEqual(rendered.count('"schema_version":"sentinel-v1"'), 1)

    def test_legacy_market_and_decision_slots_are_explicitly_rendered(self) -> None:
        bundle = RNPromptBundle.load(prompt_dir=self.root)
        for stage in (ANALYSIS_STAGE, DECISION_STAGE):
            with self.subTest(stage=stage):
                rendered = bundle.render(
                    stage,
                    self._legacy_stage_payload(schema_version=f"{stage}-slots-v1"),
                )
                self.assertIn("테스트 페르소나", rendered)
                self.assertNotRegex(
                    rendered,
                    r"\{(?:persona_prompt|today_belief|market_features|portfolio_summary|"
                    r"news_interpretation|market_analysis|order_history|trading_constraints|"
                    r"decision_space_instruction)\}",
                )

    def test_rejects_unapproved_legacy_template_slot(self) -> None:
        target = self.root / "market_analysis.txt"
        text = target.read_text(encoding="utf-8")
        target.write_text(text.replace("{market_features}", "{unapproved_slot}"), encoding="utf-8")
        with self.assertRaisesRegex(RNPromptError, "approved legacy template slots"):
            RNPromptBundle.load(prompt_dir=self.root)

    def test_rejects_malformed_identifier_like_template_tokens_at_load(self) -> None:
        target = self.root / "market_analysis.txt"
        text = target.read_text(encoding="utf-8")
        for token in ("{bad-slot}", "{persona_prompt!r}", "{unknown:>10}"):
            with self.subTest(token=token):
                target.write_text(text + "\n" + token + "\n", encoding="utf-8")
                with self.assertRaisesRegex(RNPromptError, "unapproved or malformed"):
                    RNPromptBundle.load(prompt_dir=self.root)

    def test_rejects_doubled_template_braces_at_load(self) -> None:
        target = self.root / "market_analysis.txt"
        text = target.read_text(encoding="utf-8")
        target.write_text(
            text.replace("{persona_prompt}", "{{persona_prompt}}"), encoding="utf-8"
        )
        with self.assertRaisesRegex(RNPromptError, "doubled template braces"):
            RNPromptBundle.load(prompt_dir=self.root)

    def test_belief_limit_tokens_are_explicitly_and_safely_rendered(self) -> None:
        bundle = RNPromptBundle.load(prompt_dir=self.root)
        rendered_stb = bundle.render(
            STB_STAGE,
            self._belief_payload(evidence_field="dimension_evidence", dim_1_limit=151),
        )
        rendered_ltb = bundle.render(
            LTB_STAGE,
            self._belief_payload(evidence_field="integration_evidence", dim_1_limit=152),
        )
        self.assertIn("151자 이내", rendered_stb)
        self.assertIn("152자 이내", rendered_ltb)
        self.assertNotIn("{dim_1_limit}", rendered_stb)
        self.assertNotIn("{dim_1_limit}", rendered_ltb)
        self.assertIn('"character_limit":151', rendered_stb)
        self.assertIn('"character_limit":152', rendered_ltb)

    def test_belief_limit_tokens_require_the_sealed_output_contract(self) -> None:
        bundle = RNPromptBundle.load(prompt_dir=self.root)
        with self.assertRaisesRegex(RNPromptError, "output_contract"):
            bundle.render(STB_STAGE, {"schema_version": "missing-contract"})

    def test_rejects_missing_or_duplicate_legacy_belief_limit_token(self) -> None:
        target = self.root / "update_short_term_belief.txt"
        text = target.read_text(encoding="utf-8")
        target.write_text(text.replace("{dim_1_limit}", "150", 1), encoding="utf-8")
        with self.assertRaisesRegex(RNPromptError, "belief limit token"):
            RNPromptBundle.load(prompt_dir=self.root)

    def test_rejects_unknown_legacy_belief_limit_token(self) -> None:
        target = self.root / "update_short_term_belief.txt"
        text = target.read_text(encoding="utf-8")
        target.write_text(text + "\n{dim_7_limit}\n", encoding="utf-8")
        with self.assertRaisesRegex(RNPromptError, "belief limit token"):
            RNPromptBundle.load(prompt_dir=self.root)

    def test_common_templates_retain_reviewed_legacy_and_hierarchical_contracts(self) -> None:
        bundle = RNPromptBundle.load(prompt_dir=self.root)
        required_markers = {
            STB_STAGE: (
                "Short-Term Belief",
                "【Belief란 무엇이며 왜 업데이트하는가】",
                "【오늘의 컨텍스트】",
                "dimension_evidence",
                "그날의 임시·현재 관점",
                "각 AM/PM 의사결정 직전",
                "current_evidence.depth2_search_results",
                "향후 1개월 삼성전자 주가 방향",
            ),
            LTB_STAGE: (
                "Long-Term Belief",
                "【Belief란 무엇이며 왜 업데이트하는가】",
                "integration_evidence",
                "다음 거래부터 쓰는 지속적 관점",
                "sanitized_evidence_registry",
                "정확히 한 번씩",
                "이전 문장을 그대로 복사",
            ),
            ANALYSIS_STAGE: (
                "분석의 목적:",
                "이전 Long-Term Belief",
                "오늘의 Short-Term Belief",
                "uncertain",
                "evidence_references",
            ),
            DECISION_STAGE: (
                "공시가 기반 체결 규칙",
                "전량 즉시 체결",
                "requested_quantity",
                "allowed_actions",
                "price, order_type, hold, market, limit",
            ),
        }
        for stage, markers in required_markers.items():
            with self.subTest(stage=stage):
                text = bundle.template(stage).text
                for marker in markers:
                    self.assertIn(marker, text)

    def test_belief_templates_keep_the_legacy_update_scaffold_in_order(self) -> None:
        bundle = RNPromptBundle.load(prompt_dir=self.root)
        common_scaffold = (
            "당신은 아래",
            "【Belief란 무엇이며 왜 업데이트하는가】",
            "【오늘의 컨텍스트】",
        )
        stage_scaffold = {
            STB_STAGE: (
                "오늘의 거래에 앞서, 아래 순서로 생각을 정리하세요.",
                "【Step 1: 오늘 어떤 새로운 정보를 접했는가?】",
                "【Step 2: 기존 Belief와 비교하면 무엇이 달라졌는가?】",
                "【Step 3: 오늘 거래에 임할 관점을 어떻게 정리할 것인가?】",
            ),
            LTB_STAGE: (
                "다음 거래에 앞서, 아래 순서로 생각을 정리하세요.",
                "【Step 1: 오늘 어떤 새로운 정보를 접했는가?】",
                "【Step 2: 기존 Belief와 비교하면 무엇이 달라졌는가?】",
                "【Step 3: 다음 거래에 임할 관점을 어떻게 정리할 것인가?】",
            ),
        }
        shared_tail = (
            "필수 JSON 키:",
            "거래 행동 지시는 쓰지 말고, 관점과 느낀 점만 작성하세요.",
        )
        original_dimension_descriptions = (
            "향후 1개월 삼성전자 주가 방향에 대한 나의 전망",
            "현재 삼성전자 주가의 밸류에이션에 대한 나의 관점",
            "삼성전자에 영향을 미치는 거시경제 환경에 대한 나의 판단",
            "현재 삼성전자를 둘러싼 시장 심리와 투자자 분위기에 대한 나의 감지",
            "오늘 뉴스와 커뮤니티를 접하고 느낀 것, 깨달은 것, 내 관점에서의 해석",
            "최근 나의 투자 판단들을 돌아본 자기 평가",
        )
        shared_anchor = "Belief는 당신이 삼성전자에 대해 지속적으로 유지하고 수정해 나가는 투자 관점입니다."
        stage_anchor_sentences = {
            STB_STAGE: (
                shared_anchor,
                "아래 여섯 차원 각각에 대해, 오늘 접한 정보가 현재 관점에 무엇을 시사하는지 반영하세요.",
                "강한 신호가 있는 차원은 명확히 쓰고, 약하거나 없는 차원은 현재 신호의 중립성·불확실성을 담으세요.",
            ),
            LTB_STAGE: (
                shared_anchor,
                "아래 여섯 차원 각각에 대해, 오늘 접한 정보가 기존 관점을 어떻게 바꾸는지 반영하세요.",
                "크게 바뀐 차원은 명확히 수정하고, 유지되는 차원은 그대로 두되 그 이유를 담으세요.",
            ),
        }
        for stage in (STB_STAGE, LTB_STAGE):
            with self.subTest(stage=stage):
                text = bundle.template(stage).text
                scaffold = (*common_scaffold, *stage_scaffold[stage], *shared_tail)
                positions = [text.index(marker) for marker in scaffold]
                self.assertEqual(positions, sorted(positions))
                for description in original_dimension_descriptions:
                    self.assertIn(description, text)
                for sentence in stage_anchor_sentences[stage]:
                    self.assertIn(sentence, text)
                for key in ("belief_summary", "view_change"):
                    self.assertNotIn(key, text)
                for dimension in range(1, 7):
                    self.assertIn(f"{{dim_{dimension}_limit}}", text)

    def test_belief_templates_keep_every_unaffected_legacy_line_in_order(self) -> None:
        """Keep the 0720 update prompt verbatim except declared stage boundaries.

        The two role files deliberately differ from the legacy updater only
        where their sealed inputs, visibility timing, or exact JSON output
        contract make the original line false.  This test makes an accidental
        prose rewrite visible in review without pretending that STB and
        post-fill LTB can share the same inputs.
        """

        project_root = Path(__file__).resolve().parents[1]
        legacy_lines = (project_root / "prompts" / "update_belief.txt").read_text(
            encoding="utf-8"
        ).splitlines()
        common = project_root / "prompts" / "common"
        shared_context_lines = {
            "{persona_prompt}",
            "{today_context}",
            "- market_features: 오늘 확인 가능한 시장 데이터 (최근 종가·거래량·기술적 지표 등)",
            "- news_interpretation: 오늘 뉴스를 내 페르소나 관점에서 해석한 결과",
            "  (감성, 단기/장기 영향, 느낀 점, 판단 신뢰도 등)",
            "- community_thinking: 오늘 아침 읽은 종목토론방 글들의 분위기와 논의 흐름",
            "  (값이 없거나 null이면 무시)",
            "- 이전 Belief: 어제까지 형성된 나의 투자 관점 전체",
            "- belief_summary: 위 여섯 차원을 통합한 1~3문장 핵심 요약. 오늘 거래에 임하는 나의 핵심 관점을 담으세요.",
            '- view_change: 이전 Belief 대비 오늘 달라진 점을 구체적으로 서술하세요. 변화가 없다면 "유지"와 그 이유를 쓰세요.',
            "위 사고 과정을 바탕으로 오늘의 삼성전자 투자 Belief를 JSON으로 작성하세요.",
        }
        stage_specific_changes = {
            STB_STAGE: {
                "매일 아침 오늘의 새로운 정보(뉴스, 시장 데이터, 커뮤니티 분위기)를 접한 뒤,",
                "기존에 가지고 있던 관점과 비교하여 무엇이 달라졌는지, 무엇이 확인됐는지를 정리합니다.",
                "- 시장 데이터(가격·거래량 등)는 어떤 흐름을 보이고 있는가?",
                "아래 여섯 차원 각각에 대해, 오늘 접한 정보가 기존 관점을 어떻게 바꾸는지 반영하세요.",
                "크게 바뀐 차원은 명확히 수정하고, 유지되는 차원은 그대로 두되 그 이유를 담으세요.",
            },
            LTB_STAGE: {
                "매일 아침 오늘의 새로운 정보(뉴스, 시장 데이터, 커뮤니티 분위기)를 접한 뒤,",
                "이 업데이트된 Belief가 오늘의 거래 결정(살지·팔지·얼마에·얼마나)의 기반이 됩니다.",
                "- 뉴스에서 무엇이 눈에 들어왔는가? 중요한 변화나 신호가 있었는가?",
                "- 시장 데이터(가격·거래량 등)는 어떤 흐름을 보이고 있는가?",
                "- 커뮤니티에서 다른 투자자들은 어떤 분위기였는가? 나와 비슷한가, 다른가?",
                "오늘의 거래에 앞서, 아래 순서로 생각을 정리하세요.",
                "【Step 3: 오늘 거래에 임할 관점을 어떻게 정리할 것인가?】",
            },
        }
        filenames = {STB_STAGE: "update_short_term_belief.txt", LTB_STAGE: "update_long_term_belief.txt"}
        for stage, filename in filenames.items():
            with self.subTest(stage=stage):
                text = (common / filename).read_text(encoding="utf-8")
                cursor = 0
                for line in legacy_lines:
                    if not line or line in shared_context_lines or line in stage_specific_changes[stage]:
                        continue
                    next_cursor = text.find(line, cursor)
                    self.assertNotEqual(
                        next_cursor,
                        -1,
                        f"{stage} changed an unaffected legacy line: {line!r}",
                    )
                    cursor = next_cursor + len(line)

    def test_analysis_and_decision_templates_keep_their_legacy_scaffolds_in_order(self) -> None:
        bundle = RNPromptBundle.load(prompt_dir=self.root)
        expected_scaffolds = {
            ANALYSIS_STAGE: (
                "오늘의 Belief:",
                "오늘의 시장 데이터:",
                "현재 포트폴리오:",
                "뉴스 해석:",
                "위 정보를 바탕으로 삼성전자(005930)에 대한 거래 전 시장 분석을 JSON으로 작성하세요.",
                "분석의 목적:",
                "분석 시 반드시 고려할 점:",
                "필수 JSON 키:",
                "작성 원칙:",
            ),
            DECISION_STAGE: (
                "오늘의 Belief:",
                "거래 전 시장 분석:",
                "현재 포트폴리오:",
                "최근 주문 이력:",
                "거래 제약:",
                "의사결정 공간:",
                "【공시가 기반 체결 규칙】",
                "【Step 1: 방향 결정 — 살 것인가, 팔 것인가?】",
                "【Step 2: 거래 가능 범위 확인 — 얼마나 거래할 수 있는가?】",
                "【Step 3: 최종 결정】",
                "필수 JSON 키:",
                "무효 출력 금지:",
            ),
        }
        for stage, scaffold in expected_scaffolds.items():
            with self.subTest(stage=stage):
                text = bundle.template(stage).text
                positions = [text.index(marker) for marker in scaffold]
                self.assertEqual(positions, sorted(positions))

    def test_analysis_keeps_all_legacy_keys_and_adds_only_typed_lineage(self) -> None:
        text = RNPromptBundle.load(prompt_dir=self.root).template(ANALYSIS_STAGE).text
        for key in (
            "market_view",
            "valuation_view",
            "technical_view",
            "news_view",
            "portfolio_view",
            "key_risks",
            "opportunity",
            "caution",
            "confidence",
            "directional_stance",
            "evidence_references",
        ):
            self.assertIn(f"- {key}:", text)
        self.assertNotIn("- summary:", text)

    def test_seals_an_independent_run_local_prompt_tree(self) -> None:
        source = RNPromptBundle.load(prompt_dir=self.root)
        sealed = source.seal_into(Path(self.temp.name) / "run-prompts")
        self.assertEqual(sealed.canonical_sha256, source.canonical_sha256)
        (self.root / "make_decision.txt").write_text("changed", encoding="utf-8")
        self.assertEqual(
            sealed.render(
                DECISION_STAGE,
                self._legacy_stage_payload(schema_version="sealed-v1"),
            ).count("sealed-v1"),
            1,
        )
        self.assertEqual(
            sealed.render(
                ANALYSIS_STAGE,
                self._legacy_stage_payload(schema_version="analysis-sealed-v1"),
            ).count("analysis-sealed-v1"),
            1,
        )
