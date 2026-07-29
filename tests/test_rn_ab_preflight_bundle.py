from __future__ import annotations

import copy
import asyncio
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_rn_ab_spec import (
    _am_pm_calendar_registry,
    _cohort_registry,
    _stage_input_registry,
    _study_spec,
)
from twinmarket_kr.rn_ab.news import (
    article_payload_sha256,
    bundle_content_sha256,
    fake_registry_sha256,
)
from twinmarket_kr.rn_ab.resolver import canonical_sha256
from twinmarket_kr.rn_ab.persona_snapshot import (
    SealedPersonaSnapshot,
    build_persona_snapshot,
    persona_renderer_sha256,
)
from twinmarket_kr.rn_ab.preflight_inputs import (
    DEPTH2_FILENAME,
    PUBLIC_PROFILE_FILENAME,
    TRUTH_POLICY_FILENAME,
    PreflightInputError,
    load_generated_preflight_inputs,
)
from twinmarket_kr.rn_ab.prompt_registry import ALL_PROMPT_FILENAMES, RNPromptBundle
from twinmarket_kr.rn_ab.run_context import RNRunContext, RNRunContextError
from twinmarket_kr.rn_ab.evidence_provider import (
    RNEvidenceProviderError,
    RNRunContextEvidenceProvider,
)
from twinmarket_kr.rn_ab.community import PublicAuthorProfile
from twinmarket_kr.rn_ab.community_provider import build_journaled_community_lifecycle
from twinmarket_kr.rn_ab.execution import strict_policy_from_context
from twinmarket_kr.rn_ab.run_bundle import RunBundleError, prepare_preflight_bundle
from twinmarket_kr.db.schema import AGENTS_DDL


def _file_sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


class RNPreflightBundleTests(unittest.TestCase):
    """Offline-only checks for the sealed paper entrypoint's local preflight."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.inputs = self.root / "inputs"
        self.outputs = self.root / "outputs"
        self.inputs.mkdir()
        self.outputs.mkdir()
        self.project_root = Path(__file__).resolve().parents[1]
        self.cohort = _cohort_registry()
        # The paper evaluator deliberately requires an AM/PM pair for each
        # date, unlike the smaller resolver-only fixture.
        self.calendar = _am_pm_calendar_registry()
        self.stage_inputs = _stage_input_registry(self.calendar)
        self._write_inputs()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_inputs(self) -> None:
        self.cohort_path = self.inputs / "cohort.json"
        self.calendar_path = self.inputs / "calendar.json"
        self.stage_path = self.inputs / "stage-inputs.json"
        self.price_path = self.inputs / "prices.json"
        self.news_path = self.inputs / "news.json"
        self.injection_path = self.inputs / "known-injections.json"
        self.review_path = self.inputs / "article-review.json"
        self.spec_path = self.inputs / "study-spec.json"

        self.persona_source_path = self.inputs / "legacy-personas.sqlite"
        self.persona_snapshot_path = self.inputs / "persona-snapshot"
        self._write_persona_source()
        build_persona_snapshot(
            source_db_path=self.persona_source_path,
            snapshot_dir=self.persona_snapshot_path,
            expected_agent_count=3,
            expected_depth_counts={0: 1, 1: 1, 2: 1},
        )
        self.sealed_personas = SealedPersonaSnapshot.load(self.persona_snapshot_path)
        for member in self.cohort["agents"]:
            member["persona_sha256"] = self.sealed_personas.persona(member["agent_id"]).persona_sha256

        self.prompt_dir = self.inputs / "common-prompts"
        self.prompt_dir.mkdir()
        repository_prompts = self.project_root / "prompts" / "common"
        for filename in ALL_PROMPT_FILENAMES:
            shutil.copy2(repository_prompts / filename, self.prompt_dir / filename)
        self.prompt_bundle = RNPromptBundle.load(prompt_dir=self.prompt_dir)

        self._write_json(self.cohort_path, self.cohort)
        self._write_json(self.calendar_path, self.calendar)
        stage_bytes = _canonical_bytes(self.stage_inputs)
        self.stage_path.write_bytes(stage_bytes)
        self._write_json(self.price_path, self._price_registry())

        self.news = self._news_bundle()
        self._write_json(self.news_path, self.news)
        self.injections = {
            "artifact_type": "known_injection_registry",
            "version": "known-injections-v1",
            "entries": [
                {
                    "injection_id": "approved-fake-001",
                    "title": "Approved non-runtime counterfactual",
                    "row_sha256": _file_sha256(b"approved-fake-row"),
                }
            ],
        }
        self._write_json(self.injection_path, self.injections)
        self.review = self._review_manifest()
        self._write_json(self.review_path, self.review)

        payload = _study_spec(self.cohort, self.calendar)
        payload["baseline_commit"] = self._checked_out_commit()
        payload["cohort_registry_sha256"] = canonical_sha256(self.cohort)
        payload["persona_snapshot_manifest_sha256"] = self.sealed_personas.manifest_sha256
        payload["persona_depth_manifest_sha256"] = self.sealed_personas.depth_manifest_sha256
        payload["persona_renderer_sha256"] = persona_renderer_sha256()
        payload["prompt_bundle_sha256"] = self.prompt_bundle.canonical_sha256
        news_policy = {
            "version": "rn-news-exposure-policy-v1",
            "target_real_news_per_event": 1,
            "fake_news_per_event": 0,
            "shortage_policy": "accepted_shortage_no_synthetic_or_duplicate_v1",
        }
        payload["news_exposure_policy"] = news_policy
        payload["news_exposure_policy_sha256"] = canonical_sha256(news_policy)
        payload["real_news_bundle_manifest_sha256"] = self.news["bundle_sha256"]
        payload["known_injection_registry_sha256"] = canonical_sha256(self.injections)
        payload["article_version_leakage_review_manifest_sha256"] = canonical_sha256(self.review)
        payload["stage_input_registry_file_sha256"] = _file_sha256(stage_bytes)
        payload["stage_input_registry_canonical_sha256"] = canonical_sha256(self.stage_inputs)
        self.spec = payload
        self._write_json(self.spec_path, self.spec)

    def _write_persona_source(self) -> None:
        rows = []
        for member in self.cohort["agents"]:
            rows.append(
                (
                    member["agent_id"],
                    f"source-{member['agent_id']}",
                    "ordinary",
                    "female",
                    31,
                    "30대",
                    "서울",
                    "low",
                    "low",
                    "medium",
                    "medium",
                    "value",
                    0,
                    '["전기전자","반도체"]',
                    member["initial_cash"],
                    member["news_depth"],
                    "test-segment",
                    1,
                    "legacy prompt with an intentionally stale depth sentence",
                )
            )
        with sqlite3.connect(self.persona_source_path) as connection:
            connection.execute(AGENTS_DDL)
            connection.executemany(
                "INSERT INTO agents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            connection.commit()

    def _price_registry(self) -> dict:
        stage_by_event = {row["event_id"]: row for row in self.stage_inputs["events"]}
        events: list[dict] = []
        for date_row in self.calendar["dates"]:
            for event in date_row["decision_events"]:
                stage = stage_by_event[event["decision_event_id"]]
                events.append(
                    {
                        "decision_event_id": event["decision_event_id"],
                        "date": date_row["date"],
                        "subturn": event["subturn"],
                        "execution_price_field": event["execution_price_field"],
                        "execution_price": stage["market"]["reference_price"],
                    }
                )
        return {
            "artifact_type": "event_price_registry",
            "version": "prices-v1",
            "stock_code": "005930",
            "calendar_event_registry_sha256": canonical_sha256(self.calendar),
            "events": events,
        }

    def _news_bundle(self) -> dict:
        articles: list[dict] = []
        slots: list[dict] = []
        for index, stage in enumerate(self.stage_inputs["events"], start=1):
            day = stage["date"]
            article = {
                "article_id": f"real-article-{index}",
                "title": f"Samsung operating update {index}",
                "summary": f"A provenance-safe pre-session operating update {index}.",
                "published_at": f"{day}T07:00:00+09:00",
                "observed_at": f"{day}T07:20:00+09:00",
                "last_modified_at": f"{day}T07:10:00+09:00",
                "source_url": f"https://example.com/article/{index}",
                "source": "ExampleWire",
                "raw_body_sha256": _file_sha256(f"body-{index}".encode("utf-8")),
                "version_sha256": _file_sha256(f"version-{index}".encode("utf-8")),
                "cutoff_version_sha256": _file_sha256(
                    f"cutoff-version-{index}".encode("utf-8")
                ),
            }
            article["payload_sha256"] = article_payload_sha256(article)
            articles.append(article)
            slots.append(
                {
                    "event_id": stage["event_id"],
                    "slot_ordinal": 1,
                    "article_id": article["article_id"],
                    "payload_sha256": article["payload_sha256"],
                }
            )
        fake_hash = _file_sha256(b"approved-fake-row")
        bundle = {
            "artifact_type": "real_news_bundle_manifest",
            "bundle_sha256": "0" * 64,
            "stock_code": "005930",
            "target_real_news_per_event": 1,
            "fake_news_per_event": 0,
            "articles": articles,
            "slots": slots,
            "accepted_shortages": {},
            "known_fake_ids": ["approved-fake-001"],
            "known_fake_payload_hashes": [fake_hash],
            "fake_registry_sha256": fake_registry_sha256(
                known_fake_ids=["approved-fake-001"],
                known_fake_payload_hashes=[fake_hash],
            ),
        }
        bundle["bundle_sha256"] = bundle_content_sha256(bundle)
        return bundle

    def _review_manifest(self) -> dict:
        credential_sha = _file_sha256(b"offline-reviewer-credential")
        return {
            "artifact_type": "article_version_leakage_review_manifest",
            "version": "article-review-v2",
            "real_news_bundle_manifest_sha256": self.news["bundle_sha256"],
            "calendar_event_registry_sha256": canonical_sha256(self.calendar),
            "stage_input_registry_canonical_sha256": canonical_sha256(self.stage_inputs),
            "scanner": {
                "scanner_id": "offline-semantic-leakage-scanner",
                "scanner_version": "test-v1",
                "scanner_sha256": _file_sha256(b"offline-scanner"),
                "executed_at": "2026-03-04T09:00:00+09:00",
            },
            "runtime_reviews": [
                {
                    "article_id": article["article_id"],
                    "payload_sha256": article["payload_sha256"],
                    "version_sha256": article["version_sha256"],
                    "cutoff_version_sha256": article["cutoff_version_sha256"],
                    "decision": "allow",
                    "reason": "Automated scan plus blinded review approved the frozen version.",
                    "reviewer_id": "offline-test-reviewer",
                    "reviewer_credential_sha256": credential_sha,
                    "reviewed_at": "2026-03-04T10:00:00+09:00",
                }
                for article in self.news["articles"]
            ],
            "candidate_reviews": [
                {
                    "candidate_id": "news_20260427_섹터_0032",
                    "article_id": "news_20260427_섹터_0032",
                    "payload_sha256": _file_sha256(b"known-eod-payload"),
                    "version_sha256": _file_sha256(b"known-eod-version"),
                    "cutoff_version_sha256": _file_sha256(b"known-eod-cutoff-version"),
                    "decision": "reject",
                    "reason": "Documented same-day EOD close and investor-flow leakage.",
                    "reviewer_id": "offline-test-reviewer",
                    "reviewer_credential_sha256": credential_sha,
                    "reviewed_at": "2026-03-04T10:00:00+09:00",
                    "scanner_result": "eod_close_and_individual_flow_detected",
                }
            ],
        }

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        path.write_bytes(_canonical_bytes(value))

    def _checked_out_commit(self) -> str:
        completed = subprocess.run(
            ["git", "-C", str(self.project_root), "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            check=True,
        )
        return completed.stdout.strip()

    def _preflight(
        self,
        *,
        run_id: str = "rn-preflight-local",
        input_root: Path | None = None,
        output_root: Path | None = None,
    ):
        return prepare_preflight_bundle(
            run_id=run_id,
            input_root=input_root or self.inputs,
            output_root=output_root or self.outputs,
            study_spec_path=self.spec_path,
            cohort_registry_path=self.cohort_path,
            persona_snapshot_dir=self.persona_snapshot_path,
            prompt_dir=self.prompt_dir,
            calendar_event_registry_path=self.calendar_path,
            stage_input_registry_path=self.stage_path,
            event_price_registry_path=self.price_path,
            real_news_bundle_path=self.news_path,
            known_injection_registry_path=self.injection_path,
            article_version_leakage_review_manifest_path=self.review_path,
        )

    def test_local_preflight_creates_only_sealed_non_execution_artifacts(self) -> None:
        bundle = self._preflight()

        self.assertTrue(bundle.resolved_manifest_path.is_file())
        self.assertTrue(bundle.evaluator_contract_path.is_file())
        self.assertTrue(bundle.leakage_review_path.is_file())
        self.assertTrue(bundle.run_record_path.is_file())
        self.assertTrue(bundle.run_record_markdown_path.is_file())
        self.assertTrue(bundle.source_hashes_path.is_file())
        self.assertEqual(set(bundle.condition_db_paths), {"RN_COMM_OFF", "RN_COMM_ON"})
        self.assertTrue(all(path.is_file() for path in bundle.condition_db_paths.values()))
        self.assertTrue(all(path.is_file() for path in bundle.journal_paths.values()))
        record = json.loads(bundle.run_record_path.read_text(encoding="utf-8"))
        self.assertEqual(record["mode"], "preflight_only_no_network_no_paid_api")
        self.assertFalse(record["execution_authorized"])
        self.assertEqual(record["network_requests"], 0)
        self.assertEqual(record["paid_api_calls"], 0)
        self.assertEqual(record["source_hashes"]["path"], "source_hashes.json")
        source_hashes = json.loads(bundle.source_hashes_path.read_text(encoding="utf-8"))
        self.assertEqual(
            source_hashes["snapshot_sha256"],
            record["source_hashes"]["snapshot_sha256"],
        )
        self.assertRegex(source_hashes["source_tree_sha256"], r"^[0-9a-f]{64}$")
        rendered_record = bundle.run_record_markdown_path.read_text(encoding="utf-8")
        self.assertIn("PAID EXECUTION NOT AUTHORIZED", rendered_record)
        self.assertTrue((bundle.run_dir / "RN_COMM_OFF" / "RUN_RECORD.md").is_file())
        self.assertTrue((bundle.run_dir / "RN_COMM_ON" / "RUN_RECORD.md").is_file())
        self.assertEqual(set(record["clean_base"]), {"RN_COMM_OFF", "RN_COMM_ON"})
        for condition_id, base in record["clean_base"].items():
            self.assertEqual(base["initial_portfolios"], 3, condition_id)
            self.assertEqual(base["ltb0"], 3, condition_id)
            self.assertRegex(base["clean_base_scientific_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(record["baseline_commit"], self._checked_out_commit())
        self.assertEqual(record["checked_out_baseline_commit"], self._checked_out_commit())
        self.assertEqual(
            record["article_version_leakage_review_manifest_sha256"],
            canonical_sha256(self.review),
        )
        self.assertEqual(
            json.loads(bundle.leakage_review_path.read_text(encoding="utf-8")),
            self.review,
        )
        context = RNRunContext.load(bundle.run_dir)
        self.assertEqual(context.run_id, bundle.run_id)
        self.assertEqual(context.manifest_sha256, bundle.resolved_manifest_sha256)
        self.assertEqual(context.prompt_bundle.canonical_sha256, bundle.prompt_bundle_sha256)
        self.assertEqual(set(context.condition_db_paths), {"RN_COMM_OFF", "RN_COMM_ON"})
        self.assertTrue((bundle.runtime_inputs_dir / "real_news_bundle.json").is_file())
        self.assertTrue((bundle.generated_inputs_dir / DEPTH2_FILENAME).is_file())
        self.assertTrue((bundle.generated_inputs_dir / PUBLIC_PROFILE_FILENAME).is_file())
        self.assertTrue((bundle.generated_inputs_dir / TRUTH_POLICY_FILENAME).is_file())
        self.assertTrue(bundle.generated_input_manifest_path.is_file())
        generated = record["generated_preflight_inputs"]
        self.assertFalse(generated["human_approval_claimed"])
        self.assertFalse(generated["execution_authorized"])
        self.assertEqual(generated["network_requests"], 0)
        self.assertEqual(generated["paid_api_calls"], 0)
        with self.assertRaisesRegex(RunBundleError, "Cannot create sealed RN run directory"):
            self._preflight()

    def test_generated_inputs_are_deterministic_safe_projections_and_tamper_closed(self) -> None:
        bundle = self._preflight(run_id="rn-generated-inputs-local")
        context = RNRunContext.load(bundle.run_dir)
        generated = load_generated_preflight_inputs(
            bundle.generated_inputs_dir,
            resolved=context.resolved,
            personas=context.personas,
            stage_inputs=context.stage_inputs,
            news=context.news_registry,
        )
        self.assertEqual(set(generated.public_profiles), {
            member.agent_id
            for member in context.resolved.cohort.members
            if member.news_depth in {1, 2}
        })
        for profile in generated.public_profiles.values():
            self.assertEqual(profile["public_direction"], "neutral")
            self.assertEqual(profile["public_reliability_score"], 50)
            self.assertNotIn("portfolio", profile)
            self.assertNotIn("recent_trade", profile)
            self.assertNotIn("private_belief", profile)
        for row in generated.depth2_registry["events"]:
            self.assertFalse(
                set(row["candidate_article_ids"]) & set(row["excluded_current_event_article_ids"])
            )
            # 후보 풀 자체에는 상한이 없다. 상한은 에이전트 선택 단계에만 걸린다.
            self.assertEqual(
                len(row["candidate_article_ids"]), len(row["candidate_payload_sha256s"])
            )
            self.assertEqual(
                len(set(row["candidate_article_ids"])), len(row["candidate_article_ids"])
            )
        self.assertEqual(generated.depth2_registry["max_selected"], 5)
        d2_agent = next(
            member.agent_id
            for member in context.resolved.cohort.members
            if member.news_depth == 2
        )
        later_event = context.resolved.decision_events[1].decision_event_id
        evidence_provider = RNRunContextEvidenceProvider(context)
        packet = asyncio.run(
            evidence_provider.current_evidence(
                condition_id="RN_COMM_OFF",
                agent_id=d2_agent,
                event_id=later_event,
            )
        )
        self.assertTrue(packet.depth2_search_results)
        # 에이전트가 실제로 읽는 추가 기사는 봉인된 선택 상한을 넘지 못한다.
        self.assertLessEqual(
            len(packet.depth2_search_results), generated.depth2_registry["max_selected"]
        )
        pool_ids = {
            article_id
            for article_id, _projection in generated.depth2_candidate_pool(
                event_id=later_event, news=context.news_registry
            )
        }
        self.assertTrue(
            {item["article_id"] for item in packet.depth2_search_results} <= pool_ids
        )
        self.assertFalse(
            {item["article_id"] for item in packet.news}
            & {item["article_id"] for item in packet.depth2_search_results}
        )
        self.assertNotIn(
            "sealed_depth2_recent_search_registry_and_projection",
            evidence_provider.missing_full_run_dependencies(),
        )
        search_id = packet.depth2_search_results[0]["article_id"]
        event = context.event_schedule.event(later_event)
        store = context.open_store("RN_COMM_OFF")
        stb_id = store.save_stb(
            agent_id=d2_agent,
            event_id=later_event,
            turn=int(event["turn"]),
            date=str(event["date"]),
            dimensions={
                "dim_1": "검색 결과를 포함한 단기 전망",
                "dim_2": "현재 가격 평가는 중립",
                "dim_3": "거시 환경은 혼조",
                "dim_4": "시장 심리는 중립",
                "dim_5": "최근 검색 기사를 확인함",
                "dim_6": "판단을 계속 점검함",
            },
            dimension_evidence={
                dimension: {
                    "support": [search_id] if dimension == "dim_5" else [],
                    "contradict": [],
                }
                for dimension in (
                    "dim_1",
                    "dim_2",
                    "dim_3",
                    "dim_4",
                    "dim_5",
                    "dim_6",
                )
            },
            current_evidence=packet.as_dict(),
        )
        saved = store.current_stb(
            agent_id=d2_agent,
            event_id=later_event,
            turn=int(event["turn"]),
        )
        self.assertEqual(saved["stb_id"], stb_id)
        self.assertIn(search_id, {
            row["article_id"] for row in saved["evidence"] if row["kind"] == "news"
        })

        profile_path = bundle.generated_inputs_dir / PUBLIC_PROFILE_FILENAME
        payload = json.loads(profile_path.read_text(encoding="utf-8"))
        payload["profiles"][0]["profile"]["portfolio"] = {"cash": 999}
        content = {key: value for key, value in payload.items() if key != "registry_sha256"}
        payload["registry_sha256"] = canonical_sha256(content)
        profile_path.write_bytes(_canonical_bytes(payload))
        with self.assertRaises(PreflightInputError):
            load_generated_preflight_inputs(
                bundle.generated_inputs_dir,
                resolved=context.resolved,
                personas=context.personas,
                stage_inputs=context.stage_inputs,
                news=context.news_registry,
            )

    def test_checked_in_100_agent_candidate_keeps_source_hash_and_depth_map(self) -> None:
        source = self.project_root / "outputs" / "sys_100.db"
        candidate = (
            self.project_root
            / "preparation"
            / "rn_ab_source_candidate_v1"
            / "persona_snapshot"
        )
        before = _file_sha256(source.read_bytes())
        sealed = SealedPersonaSnapshot.load(candidate)
        after = _file_sha256(source.read_bytes())
        self.assertEqual(before, after)
        self.assertEqual(before, sealed.source_db_sha256)
        self.assertEqual(len(sealed.personas), 100)
        self.assertEqual(
            {
                str(depth): sum(
                    persona.news_depth == depth for persona in sealed.personas.values()
                )
                for depth in (0, 1, 2)
            },
            {"0": 30, "1": 55, "2": 15},
        )

    def test_generated_community_bindings_clear_only_artifact_gaps_and_reject_drift(self) -> None:
        bundle = self._preflight(run_id="rn-generated-community-local")
        context = RNRunContext.load(bundle.run_dir)

        class LocalModel:
            local_only = True

            async def complete(self, **kwargs):  # pragma: no cover - construction makes no call.
                raise AssertionError("community construction must not call a model")

        lifecycle = build_journaled_community_lifecycle(
            context,
            model=LocalModel(),
            call_policy=strict_policy_from_context(context),
        )
        provider = RNRunContextEvidenceProvider(
            context,
            community_lifecycle=lifecycle,
        )
        missing = provider.missing_full_run_dependencies()
        self.assertNotIn("sealed_public_author_profile_registry_and_manifest_hash", missing)
        self.assertNotIn("sealed_generated_community_post_truth_policy", missing)
        self.assertNotIn("sealed_generated_input_manifest_binding", missing)
        self.assertIn("approved_journaled_community_posting_and_read_provider", missing)
        self.assertIn(
            "approved_journaled_next_am_community_interpretation_provider",
            missing,
        )

        expected_hash = lifecycle.public_profile_registry_sha256
        lifecycle.public_profile_registry_sha256 = "f" * 64
        with self.assertRaisesRegex(
            RNEvidenceProviderError, "generated hashes differ"
        ):
            RNRunContextEvidenceProvider(context, community_lifecycle=lifecycle)
        lifecycle.public_profile_registry_sha256 = expected_hash

        replacement = PublicAuthorProfile.from_mapping(
            {
                "schema_version": "rn-public-profile-v1",
                "public_badges": ["registered-community-member"],
                "public_direction": "bullish",
                "public_reliability_score": 50,
            },
            label="test drift profile",
        )
        active_agent = next(iter(context.generated_inputs.public_profiles))
        for service in lifecycle.services.values():
            service.public_profiles[active_agent] = replacement
        with self.assertRaisesRegex(
            RNEvidenceProviderError, "public profiles differ"
        ):
            RNRunContextEvidenceProvider(context, community_lifecycle=lifecycle)

    def test_run_context_uses_the_run_local_input_copy_and_detects_copy_drift(self) -> None:
        bundle = self._preflight(run_id="rn-context-local")
        # An external input changing after preflight cannot affect the context:
        # it reconstructs only from inputs/runtime under the sealed run root.
        self.news_path.write_text('{"not":"the sealed bundle"}', encoding="utf-8")
        context = RNRunContext.load(bundle.run_dir)
        self.assertEqual(context.news_registry.bundle_sha256, self.news["bundle_sha256"])

        copied = bundle.runtime_inputs_dir / "real_news_bundle.json"
        copied.write_text('{"not":"the sealed bundle"}', encoding="utf-8")
        with self.assertRaisesRegex(RNRunContextError, "file hash"):
            RNRunContext.load(bundle.run_dir)

    def test_run_context_rejects_a_source_snapshot_hash_mismatch(self) -> None:
        bundle = self._preflight(run_id="rn-source-provenance-local")
        record = json.loads(bundle.run_record_path.read_text(encoding="utf-8"))
        record["source_hashes"]["source_tree_sha256"] = "0" * 64
        bundle.run_record_path.write_bytes(_canonical_bytes(record))

        with self.assertRaisesRegex(RNRunContextError, "source_tree_sha256 differs"):
            RNRunContext.load(bundle.run_dir)

    def test_preflight_rejects_a_pinned_but_masked_runtime_article(self) -> None:
        review = copy.deepcopy(self.review)
        review["runtime_reviews"][0]["decision"] = "reject"
        self._write_json(self.review_path, review)
        self.spec["article_version_leakage_review_manifest_sha256"] = canonical_sha256(review)
        self._write_json(self.spec_path, self.spec)

        with self.assertRaisesRegex(RunBundleError, "masked/rejected"):
            self._preflight(run_id="rn-masked-local")

    def test_preflight_rejects_review_without_the_documented_eod_counterexample(self) -> None:
        review = copy.deepcopy(self.review)
        review["candidate_reviews"][0]["candidate_id"] = "another-scanner-candidate"
        self._write_json(self.review_path, review)
        self.spec["article_version_leakage_review_manifest_sha256"] = canonical_sha256(review)
        self._write_json(self.spec_path, self.spec)

        with self.assertRaisesRegex(RunBundleError, "documented EOD counterexample"):
            self._preflight(run_id="rn-review-eod-gap")

    def test_preflight_rejects_a_rehashed_review_for_another_calendar(self) -> None:
        review = copy.deepcopy(self.review)
        review["calendar_event_registry_sha256"] = _file_sha256(b"another-calendar")
        self._write_json(self.review_path, review)
        self.spec["article_version_leakage_review_manifest_sha256"] = canonical_sha256(review)
        self._write_json(self.spec_path, self.spec)

        with self.assertRaisesRegex(RunBundleError, "different decision calendar"):
            self._preflight(run_id="rn-review-calendar-drift")

    def test_preflight_rejects_a_claimed_baseline_that_is_not_checked_out(self) -> None:
        self.spec["baseline_commit"] = "f" * 40
        self._write_json(self.spec_path, self.spec)

        with self.assertRaisesRegex(RunBundleError, "differs from the checked-out"):
            self._preflight(run_id="rn-baseline-drift")

    def test_preflight_rejects_current_and_symlink_artifact_roots(self) -> None:
        current_input_root = self.root / "current"
        current_input_root.mkdir()
        with self.assertRaisesRegex(RunBundleError, "forbidden RN namespace"):
            self._preflight(run_id="rn-input-root-test", input_root=current_input_root)

        current_root = self.outputs / "current"
        current_root.mkdir()
        with self.assertRaisesRegex(RunBundleError, "forbidden RN namespace"):
            self._preflight(run_id="rn-root-test", output_root=current_root)

        for forbidden_component in ("latest", "rn_c00_legacy"):
            forbidden_root = self.root / forbidden_component
            forbidden_root.mkdir()
            with self.assertRaisesRegex(RunBundleError, "forbidden RN namespace"):
                self._preflight(run_id="rn-namespace-test", output_root=forbidden_root)

        alias_root = self.root / "output-alias"
        alias_root.symlink_to(self.outputs, target_is_directory=True)
        with self.assertRaisesRegex(RunBundleError, "symbolic link"):
            self._preflight(run_id="rn-alias-test", output_root=alias_root)

    def test_preflight_rejects_a_rehashed_known_injection_closure_mutation(self) -> None:
        """A self-consistent edited fake registry must still match the sealed bundle closure."""

        injections = copy.deepcopy(self.injections)
        injections["entries"][0]["row_sha256"] = _file_sha256(b"replacement-fake-row")
        self._write_json(self.injection_path, injections)
        self.spec["known_injection_registry_sha256"] = canonical_sha256(injections)
        self._write_json(self.spec_path, self.spec)

        with self.assertRaisesRegex(RunBundleError, "fake-isolation closure"):
            self._preflight(run_id="rn-injection-drift")

    def test_preflight_rejects_a_self_consistent_bundle_missing_calendar_coverage(self) -> None:
        """No subset-of-days fallback is permitted when a frozen event disappears."""

        bundle = copy.deepcopy(self.news)
        missing_slot = bundle["slots"].pop()
        bundle["articles"] = [
            article for article in bundle["articles"] if article["article_id"] != missing_slot["article_id"]
        ]
        bundle["bundle_sha256"] = bundle_content_sha256(bundle)
        self.news = bundle
        self._write_json(self.news_path, bundle)
        self.review = self._review_manifest()
        self._write_json(self.review_path, self.review)
        self.spec["real_news_bundle_manifest_sha256"] = bundle["bundle_sha256"]
        self.spec["article_version_leakage_review_manifest_sha256"] = canonical_sha256(self.review)
        self._write_json(self.spec_path, self.spec)

        with self.assertRaisesRegex(RunBundleError, "event coverage differs"):
            self._preflight(run_id="rn-news-coverage-drift")

    def test_direct_cli_exposes_only_the_no_network_preflight_mode(self) -> None:
        command = [
            sys.executable,
            str(self.project_root / "scripts" / "09_run_realnews_community_ab.py"),
            "--preflight",
            "--run-id",
            "rn-cli-local",
            "--input-root",
            str(self.inputs),
            "--output-root",
            str(self.outputs),
            "--study-spec",
            str(self.spec_path),
            "--cohort-registry",
            str(self.cohort_path),
            "--persona-snapshot-dir",
            str(self.persona_snapshot_path),
            "--prompt-dir",
            str(self.prompt_dir),
            "--calendar-event-registry",
            str(self.calendar_path),
            "--stage-input-registry",
            str(self.stage_path),
            "--event-price-registry",
            str(self.price_path),
            "--real-news-bundle",
            str(self.news_path),
            "--known-injection-registry",
            str(self.injection_path),
            "--article-version-leakage-review-manifest",
            str(self.review_path),
        ]
        completed = subprocess.run(
            command,
            cwd=self.project_root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads(completed.stdout)
        self.assertEqual(output["mode"], "preflight_only_no_network_no_paid_api")
        self.assertFalse(output["execution_authorized"])


if __name__ == "__main__":
    unittest.main()
