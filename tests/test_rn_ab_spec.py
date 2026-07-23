from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from validation.validate_realnews_community_ab import parse_ab_contract

from twinmarket_kr.rn_ab import (
    RN_COMM_OFF,
    RN_COMM_ON,
    ArmPairValidationError,
    PathSafetyError,
    ResolutionError,
    StudySpec,
    StudySpecError,
    assert_only_community_mode_diff,
    canonical_json_bytes,
    canonical_sha256,
    ensure_safe_run_directory,
    resolve_study,
    verify_evaluator_contract_hash,
    write_resolved_manifest,
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _cohort_registry() -> dict:
    return {
        "artifact_type": "cohort_registry",
        "version": "cohort-v1",
        "agents": [
            {
                "ordinal": 1,
                "agent_id": "A001",
                "news_depth": 0,
                "initial_cash": 100,
                "persona_sha256": _digest("A001-persona"),
                "fixed_slot_sha256": _digest("A001-slot"),
            },
            {
                "ordinal": 2,
                "agent_id": "A002",
                "news_depth": 1,
                "initial_cash": 100,
                "persona_sha256": _digest("A002-persona"),
                "fixed_slot_sha256": _digest("A002-slot"),
            },
            {
                "ordinal": 3,
                "agent_id": "A003",
                "news_depth": 2,
                "initial_cash": 1_000,
                "persona_sha256": _digest("A003-persona"),
                "fixed_slot_sha256": _digest("A003-slot"),
            },
        ],
    }


def _event(day: str, ordinal: int, subturn: str) -> dict:
    previous_trading_day = {"2026-02-27": "2026-02-26", "2026-03-03": "2026-02-27"}[day]
    if subturn == "AM":
        decision_timestamp = f"{day}T09:00:00+09:00"
        news_window = {
            "start_exclusive": f"{previous_trading_day}T15:30:00+09:00",
            "end_inclusive": f"{day}T08:59:00+09:00",
        }
        market_feature_as_of = decision_timestamp
    else:
        decision_timestamp = f"{day}T15:30:00+09:00"
        news_window = {
            "start_exclusive": f"{day}T08:59:00+09:00",
            "end_inclusive": decision_timestamp,
        }
        market_feature_as_of = decision_timestamp
    return {
        "decision_event_id": f"{day}/{subturn}",
        "event_ordinal_in_date": ordinal,
        "subturn": subturn,
        "decision_timestamp": decision_timestamp,
        "news_window": news_window,
        "market_feature_as_of": market_feature_as_of,
        "execution_price_field": "actual_open" if subturn == "AM" else "actual_close",
        "consume_scheduled_community": subturn == "AM",
        "decision_enabled": True,
    }


def _calendar_registry() -> dict:
    return {
        "artifact_type": "calendar_event_registry",
        "version": "calendar-v1",
        "dates": [
            {
                "date": "2026-02-27",
                "timezone": "Asia/Seoul",
                "decision_events": [
                    _event("2026-02-27", 1, "AM"),
                    _event("2026-02-27", 2, "PM"),
                ],
                "post_decision_phases": [
                    {
                        "phase_id": "2026-02-27/community",
                        "after_event_id": "2026-02-27/PM",
                        "next_visible_event_rule": "next-approved-AM",
                    }
                ],
            },
            {
                "date": "2026-03-03",
                "timezone": "Asia/Seoul",
                "decision_events": [_event("2026-03-03", 1, "AM")],
                "post_decision_phases": [],
            },
        ],
    }


def _am_pm_calendar_registry() -> dict:
    """A full AM/PM calendar with one PM community phase per trading day."""

    calendar = _calendar_registry()
    calendar["dates"][1]["decision_events"].append(_event("2026-03-03", 2, "PM"))
    calendar["dates"][1]["post_decision_phases"].append(
        {
            "phase_id": "2026-03-03/community",
            "after_event_id": "2026-03-03/PM",
            "next_visible_event_rule": "next-approved-AM",
        }
    )
    return calendar


def _event_price_registry(calendar: dict) -> dict:
    events: list[dict] = []
    for date_row in calendar["dates"]:
        for offset, event in enumerate(date_row["decision_events"], start=1):
            events.append(
                {
                    "decision_event_id": event["decision_event_id"],
                    "date": date_row["date"],
                    "subturn": event["subturn"],
                    "execution_price_field": event["execution_price_field"],
                    "execution_price": 70_000 + len(events) * 100 + offset,
                }
            )
    return {
        "artifact_type": "event_price_registry",
        "version": "prices-v1",
        "stock_code": "005930",
        "calendar_event_registry_sha256": canonical_sha256(calendar),
        "events": events,
    }


def _stage_input_registry(calendar: dict) -> dict:
    events: list[dict] = []
    for date_row in calendar["dates"]:
        for event in date_row["decision_events"]:
            market_as_of = event["market_feature_as_of"]
            events.append(
                {
                    "event_id": event["decision_event_id"],
                    "date": date_row["date"],
                    "subturn": event["subturn"].lower(),
                    "news_cutoff_timestamp": event["news_window"]["end_inclusive"],
                    "market_feature_as_of": market_as_of,
                    "market": {
                        "reference_price": 70_000 + len(events) * 100,
                        "previous_close": 69_900 + len(events) * 100,
                        "open_price": 70_000 + len(events) * 100,
                        "as_of_timestamp": market_as_of,
                    },
                }
            )
    return {
        "artifact_type": "rn_stage_input_registry",
        "version": "stage-input-v1",
        "calendar_event_registry_sha256": canonical_sha256(calendar),
        "events": events,
    }


def _stage_input_file_sha256(registry: dict) -> str:
    payload = json.dumps(registry, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _study_spec(cohort: dict, calendar: dict) -> dict:
    news_exposure_policy = {
        "version": "rn-news-exposure-policy-v1",
        "target_real_news_per_event": 10,
        "fake_news_per_event": 0,
        "shortage_policy": "accepted_shortage_no_synthetic_or_duplicate_v1",
    }
    community_timing_policy = {
        "timezone": "Asia/Seoul",
        "pm_phase_not_before": "15:30:00",
        "pm_phase_not_after": "23:59:59",
        "next_am_delivery_not_before": "08:00:00",
        "next_am_delivery_not_after": "09:00:00",
    }
    stage_inputs = _stage_input_registry(calendar)
    return {
        "artifact_type": "study_spec",
        "study_id": "rn-community-ab-test",
        "design_version": "2.0.0-test",
        "baseline_commit": "a" * 40,
        "required_agent_count": 3,
        "cohort_registry_sha256": canonical_sha256(cohort),
        "persona_snapshot_manifest_sha256": _digest("persona-snapshot"),
        "persona_depth_manifest_sha256": _digest("persona-depth"),
        "persona_assignment_policy": "frozen-db-map-prompt-projection-only",
        "persona_renderer_sha256": _digest("persona-renderer"),
        "prompt_bundle_sha256": _digest("rn-prompt-bundle"),
        "belief_limits": {
            "dim_1": 150,
            "dim_2": 100,
            "dim_3": 100,
            "dim_4": 100,
            "dim_5": 100,
            "dim_6": 100,
        },
        "cohort_assertions": {
            "depth_counts": {"0": 1, "1": 1, "2": 1},
            "initial_cash_counts": {"100": 2, "1000": 1},
        },
        "condition_treatments": {
            "RN_COMM_OFF": {"community_mode": "off", "news_treatment": "real_only"},
            "RN_COMM_ON": {"community_mode": "on", "news_treatment": "real_only"},
        },
        "paired_condition_groups": [["RN_COMM_OFF", "RN_COMM_ON"]],
        "treatment_diff_allowlist": ["community_mode"],
        "calendar_event_registry_sha256": canonical_sha256(calendar),
        "burn_in_date_ids": ["2026-02-27"],
        "regime_policy_sha256": _digest("regime"),
        "real_news_bundle_manifest_sha256": _digest("real-news"),
        "known_injection_registry_sha256": _digest("known-injection-registry"),
        "article_version_leakage_review_manifest_sha256": _digest(
            "article-version-leakage-review-manifest"
        ),
        "news_exposure_policy_sha256": canonical_sha256(news_exposure_policy),
        "news_exposure_policy": news_exposure_policy,
        "community_policy": {
            "best_k": 5,
            "best_selection_policy": "top_k_or_fewer_available_no_forced_posting",
            "permissions_from_cohort_depth_map": True,
            "depth1_selective_read_cap": 5,
            "depth2_selective_read_cap": 10,
            "best_payload": "title_plus_full_frozen_body",
            "visibility": "next_approved_am_decision_event",
        },
        "community_timing_policy": community_timing_policy,
        "community_timing_policy_sha256": canonical_sha256(community_timing_policy),
        "context_window_policy": {
            "decision_historical_order_or_fill_direct_visibility": "forbidden",
            "community_public_author_private_portfolio_or_trade_visibility": "forbidden",
            "trade_memory_visibility": "postfill-ltb-only",
            "depth2_search_lookback": 7,
            "depth2_search_lookback_unit": "calendar_days",
            "depth2_search_top_k": 10,
            "news_category_targets": {"stock": 5, "sector": 3, "economy": 2},
            "market_feature_policy_sha256": _digest("market-features"),
        },
        "memory_policy": {
            "version": "stb-ltb-v4",
            "cadence": "each_manifest_decision_event",
            "trade_belief_blocks": "previous_ltb_plus_current_stb_separate_blocks",
            "ltb_update_timing": "after-fill-before-commit",
            "current_transaction_episode_input": "once-same-turn-dim6",
            "price_outcome_input": "eligible-earlier-dim6",
            "outcome_horizons": ["next-decision-event", "same-subturn-plus-1-trading-date"],
        },
        "trade_policy": {
            "stock_code": "005930",
            "decision_space": ["buy", "sell"],
            "allow_hold": False,
            "max_single_trade_cash_ratio": 0.5,
            "fill_policy": "full_fill_at_event_reference_price",
            "commission_rate": 0.0,
            "commission_applies_to": [],
            "sell_tax_rate": 0.0,
            "fee_policy": "zero_fee_baseline_all_fee_amounts_must_be_zero",
            "target_direction_notional": "gross_signed_fill_value",
        },
        "model_policy": {
            "model": "qwen/qwen3.5-flash-02-23",
            "provider": "alibaba",
            "reasoning": {"effort": "none", "exclude": True},
            "per_arm_max_concurrent_llm_calls": 8,
            "physical_http_attempts_per_phase_attempt": 1,
            "allow_provider_fallbacks": False,
            "require_parameters": True,
            "reasoning_off_canary_required": True,
            "reasoning_off_success_contract": "provider-model-match-reasoning-empty-tokens-zero",
        },
        "study_seed": 2,
        "seed_namespace": "study-agent-event-stage-attempt-v1",
        "retry_policy_sha256": _digest("retry"),
        "runtime_policy_sha256": _digest("runtime"),
        "evaluation_policy_sha256": _digest("evaluation"),
        "stage_input_registry_file_sha256": _stage_input_file_sha256(stage_inputs),
        "stage_input_registry_canonical_sha256": canonical_sha256(stage_inputs),
    }


class RnAbSpecResolverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.inputs = self.root / "inputs"
        self.outputs = self.root / "outputs"
        self.inputs.mkdir()
        self.outputs.mkdir()
        self.cohort = _cohort_registry()
        self.calendar = _calendar_registry()
        self.spec_payload = _study_spec(self.cohort, self.calendar)
        self.cohort_path = self.inputs / "cohort.json"
        self.calendar_path = self.inputs / "calendar.json"
        self.stage_input_path = self.inputs / "stage-inputs.json"
        self.cohort_path.write_text(json.dumps(self.cohort), encoding="utf-8")
        self.calendar_path.write_text(json.dumps(self.calendar), encoding="utf-8")
        self.stage_input_path.write_text(
            json.dumps(_stage_input_registry(self.calendar), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _resolve(self):
        return self._resolve_payload(self.spec_payload)

    def _resolve_payload(self, payload: dict, *, stage_input_path: Path | None = None):
        return resolve_study(
            StudySpec.from_dict(copy.deepcopy(payload)),
            cohort_registry_path=self.cohort_path,
            calendar_event_registry_path=self.calendar_path,
            stage_input_registry_path=stage_input_path or self.stage_input_path,
            input_root=self.inputs,
        )

    def _resolve_for_calendar(self, calendar: dict):
        calendar_path = self.inputs / "evaluator-calendar.json"
        stage_path = self.inputs / "evaluator-stage-inputs.json"
        calendar_path.write_text(json.dumps(calendar), encoding="utf-8")
        stage_path.write_text(
            json.dumps(_stage_input_registry(calendar), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        return resolve_study(
            StudySpec.from_dict(_study_spec(self.cohort, calendar)),
            cohort_registry_path=self.cohort_path,
            calendar_event_registry_path=calendar_path,
            stage_input_registry_path=stage_path,
            input_root=self.inputs,
        )

    def test_resolves_counts_keys_and_call_policy_without_turn_arithmetic(self) -> None:
        resolved = self._resolve()

        self.assertEqual(resolved.resolved_counts["agents"], 3)
        self.assertEqual(resolved.resolved_counts["trading_dates"], 2)
        self.assertEqual(resolved.resolved_counts["decision_events"], 3)
        self.assertEqual(resolved.resolved_counts["burn_in_dates"], 1)
        self.assertEqual(resolved.resolved_counts["primary_evaluation_dates"], 1)
        self.assertEqual(resolved.resolved_counts["agent_event_keys_per_arm"], 9)
        self.assertEqual(resolved.resolved_counts["agent_event_keys_all_arms"], 18)
        self.assertEqual(resolved.resolved_counts["community_phase_keys_per_arm"], 1)
        self.assertEqual(resolved.resolved_counts["target_real_news_article_slots"], 30)
        self.assertEqual(resolved.evaluation_date_ids, ("2026-03-03",))
        self.assertEqual(len(resolved.expected_keys("stb")), 18)
        self.assertEqual(resolved.expected_keys("stb"), resolved.expected_keys("ltb_transition"))
        self.assertEqual(len(resolved.expected_keys("fill", condition_id=RN_COMM_OFF)), 9)
        self.assertEqual(len(resolved.expected_keys("decision", condition_id=RN_COMM_ON)), 9)
        self.assertEqual(
            [event.global_ordinal for event in resolved.decision_events],
            [1, 2, 3],
        )
        # Registry serialization must remain the pinned authored shape.  In
        # particular, resolver-only date/global ordinal fields cannot leak
        # into the canonical calendar hash.
        self.assertEqual(
            canonical_sha256(resolved.calendar.to_dict()),
            self.spec_payload["calendar_event_registry_sha256"],
        )
        self.assertEqual(
            resolved.spec.call_policy.request_policy(),
            {
                "reasoning": {"effort": "none", "exclude": True},
                "provider": {
                    "only": ["alibaba"],
                    "order": ["alibaba"],
                    "allow_fallbacks": False,
                    "require_parameters": True,
                },
                "offline_llm": False,
            },
        )
        self.assertEqual(
            resolved.spec.call_policy.http_request_policy(),
            {
                "reasoning": {"effort": "none", "exclude": True},
                "provider": {
                    "only": ["alibaba"],
                    "order": ["alibaba"],
                    "allow_fallbacks": False,
                    "require_parameters": True,
                },
            },
        )
        self.assertNotIn("offline_llm", resolved.spec.call_policy.http_request_policy())
        self.assertEqual(
            resolved.expected_key_set_hashes["stb"],
            resolved.expected_key_set_hashes["ltb_transitions"],
        )
        resolved.assert_pair_integrity()

    def test_rejects_any_drift_from_the_inherited_0720_belief_character_limits(self) -> None:
        payload = copy.deepcopy(self.spec_payload)
        payload["belief_limits"]["dim_6"] = 101

        with self.assertRaisesRegex(StudySpecError, "exactly match the approved RN baseline"):
            StudySpec.from_dict(payload)

    def test_calendar_requires_explicit_kst_timestamps_and_causal_order(self) -> None:
        non_iso = copy.deepcopy(self.calendar)
        non_iso["dates"][0]["decision_events"][0]["news_window"]["end_inclusive"] = (
            "2026-02-27 08:59:00"
        )
        with self.assertRaisesRegex(ResolutionError, "ISO-8601"):
            self._resolve_for_calendar(non_iso)

        wrong_offset = copy.deepcopy(self.calendar)
        wrong_offset["dates"][0]["decision_events"][0]["decision_timestamp"] = (
            "2026-02-27T09:00:00+00:00"
        )
        with self.assertRaisesRegex(ResolutionError, "ISO-8601"):
            self._resolve_for_calendar(wrong_offset)

        future_cutoff = copy.deepcopy(self.calendar)
        event = future_cutoff["dates"][0]["decision_events"][0]
        event["news_window"]["end_inclusive"] = "2026-02-27T09:01:00+09:00"
        event["market_feature_as_of"] = "2026-02-27T09:01:00+09:00"
        with self.assertRaisesRegex(ResolutionError, "end_inclusive may not be later than decision_timestamp"):
            self._resolve_for_calendar(future_cutoff)

        reversed_cutoff_as_of = copy.deepcopy(self.calendar)
        reversed_cutoff_as_of["dates"][0]["decision_events"][0]["market_feature_as_of"] = (
            "2026-02-27T08:58:00+09:00"
        )
        with self.assertRaisesRegex(ResolutionError, "end_inclusive may not be later than market_feature_as_of"):
            self._resolve_for_calendar(reversed_cutoff_as_of)

    def test_news_slot_domain_comes_from_the_hash_pinned_policy(self) -> None:
        for target in (9, 11):
            with self.subTest(target=target):
                payload = copy.deepcopy(self.spec_payload)
                payload["news_exposure_policy"]["target_real_news_per_event"] = target
                payload["news_exposure_policy_sha256"] = canonical_sha256(
                    payload["news_exposure_policy"]
                )
                resolved = self._resolve_payload(payload)
                self.assertEqual(resolved.resolved_counts["target_real_news_articles_per_event"], target)
                self.assertEqual(resolved.resolved_counts["target_real_news_article_slots"], target * 3)
                self.assertEqual(len(resolved.planned_news_slot_keys), target * 3)
                self.assertEqual(
                    {key.slot_ordinal for key in resolved.planned_news_slot_keys},
                    set(range(1, target + 1)),
                )

        stale_hash = copy.deepcopy(self.spec_payload)
        stale_hash["news_exposure_policy"]["target_real_news_per_event"] = 9
        with self.assertRaisesRegex(StudySpecError, "news_exposure_policy_sha256"):
            StudySpec.from_dict(stale_hash)

    def test_timing_and_provenance_pins_propagate_to_resolved_contracts(self) -> None:
        resolved = self._resolve()
        arm = resolved.arm_execution_contract(RN_COMM_ON)
        manifest = resolved.to_dict()
        self.assertEqual(
            arm["community_timing_policy_sha256"],
            self.spec_payload["community_timing_policy_sha256"],
        )
        self.assertEqual(
            manifest["known_injection_registry_sha256"],
            self.spec_payload["known_injection_registry_sha256"],
        )
        self.assertEqual(
            manifest["article_version_leakage_review_manifest_sha256"],
            self.spec_payload["article_version_leakage_review_manifest_sha256"],
        )

        stale_timing_hash = copy.deepcopy(self.spec_payload)
        stale_timing_hash["community_timing_policy"]["pm_phase_not_after"] = "22:00:00"
        with self.assertRaisesRegex(StudySpecError, "community_timing_policy_sha256"):
            StudySpec.from_dict(stale_timing_hash)

        broad_timing = copy.deepcopy(self.spec_payload)
        broad_timing["community_timing_policy"]["pm_phase_not_before"] = "00:00:00"
        broad_timing["community_timing_policy_sha256"] = canonical_sha256(
            broad_timing["community_timing_policy"]
        )
        with self.assertRaisesRegex(StudySpecError, "may not begin before 15:30"):
            StudySpec.from_dict(broad_timing)

    def test_stage_input_registry_requires_file_and_canonical_pins(self) -> None:
        changed = _stage_input_registry(self.calendar)
        changed["events"][0]["market"]["reference_price"] += 1
        self.stage_input_path.write_text(
            json.dumps(changed, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ResolutionError, "file hash differs"):
            self._resolve()

        file_resealed_payload = copy.deepcopy(self.spec_payload)
        file_resealed_payload["stage_input_registry_file_sha256"] = _stage_input_file_sha256(changed)
        with self.assertRaisesRegex(ResolutionError, "canonical hash differs"):
            self._resolve_payload(file_resealed_payload)

        mismatched_calendar_facts = copy.deepcopy(changed)
        mismatched_calendar_facts["events"][0]["news_cutoff_timestamp"] = "2026-02-27T08:58:00+09:00"
        fully_resealed_payload = copy.deepcopy(self.spec_payload)
        fully_resealed_payload["stage_input_registry_file_sha256"] = _stage_input_file_sha256(
            mismatched_calendar_facts
        )
        fully_resealed_payload["stage_input_registry_canonical_sha256"] = canonical_sha256(
            mismatched_calendar_facts
        )
        self.stage_input_path.write_text(
            json.dumps(
                mismatched_calendar_facts,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ResolutionError, "news cutoff differs from resolved calendar"):
            self._resolve_payload(fully_resealed_payload)

    def test_rejects_authored_derived_fields_and_non_rn_conditions(self) -> None:
        payload = copy.deepcopy(self.spec_payload)
        payload["trading_days"] = 2
        with self.assertRaisesRegex(StudySpecError, "resolver-derived"):
            StudySpec.from_dict(payload)

        payload = copy.deepcopy(self.spec_payload)
        payload["condition_treatments"]["RN_COMM_ON"]["news_treatment"] = "fake_only"
        with self.assertRaisesRegex(StudySpecError, "RN_COMM_ON"):
            StudySpec.from_dict(payload)

        payload = copy.deepcopy(self.spec_payload)
        payload["treatment_diff_allowlist"] = ["community_mode", "seed"]
        with self.assertRaisesRegex(StudySpecError, "community_mode"):
            StudySpec.from_dict(payload)

        with self.assertRaisesRegex(StudySpecError, "study_spec must be an object"):
            StudySpec.from_dict([])  # type: ignore[arg-type]

    def test_rejects_fee_and_reasoning_policy_drift(self) -> None:
        payload = copy.deepcopy(self.spec_payload)
        payload["trade_policy"]["commission_rate"] = 0.0005
        with self.assertRaisesRegex(StudySpecError, "commission_rate"):
            StudySpec.from_dict(payload)

        payload = copy.deepcopy(self.spec_payload)
        payload["model_policy"]["reasoning"]["effort"] = "low"
        with self.assertRaisesRegex(StudySpecError, "reasoning.effort"):
            StudySpec.from_dict(payload)

        payload = copy.deepcopy(self.spec_payload)
        payload["model_policy"]["allow_provider_fallbacks"] = True
        with self.assertRaisesRegex(StudySpecError, "allow_provider_fallbacks"):
            StudySpec.from_dict(payload)

        payload = copy.deepcopy(self.spec_payload)
        payload["model_policy"]["model"] = "some-provider/other-model"
        with self.assertRaisesRegex(StudySpecError, "RN baseline requires model_policy.model"):
            StudySpec.from_dict(payload)

    def test_rejects_registry_content_drift_and_calendar_intersection_substitute(self) -> None:
        changed = copy.deepcopy(self.calendar)
        changed["dates"].pop()
        self.calendar_path.write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaisesRegex(ResolutionError, "calendar-event registry hash"):
            self._resolve()

    def test_pair_contract_rejects_non_treatment_difference(self) -> None:
        resolved = self._resolve()
        off = dict(resolved.arm_execution_contract(RN_COMM_OFF))
        on = dict(resolved.arm_execution_contract(RN_COMM_ON))
        on["study_seed"] = 999
        with self.assertRaisesRegex(ArmPairValidationError, "study_seed"):
            assert_only_community_mode_diff(off, on)

    def test_safe_output_seals_only_new_or_exact_resume_manifest(self) -> None:
        resolved = self._resolve()
        run_dir = self.outputs / "rn-community-ab-test"
        manifest_path = write_resolved_manifest(
            resolved,
            run_dir=run_dir,
            output_root=self.outputs,
        )
        self.assertTrue(manifest_path.is_file())
        self.assertEqual(manifest_path.read_bytes(), canonical_json_bytes(resolved.to_dict()))
        self.assertEqual(
            write_resolved_manifest(
                resolved,
                run_dir=run_dir,
                output_root=self.outputs,
                allow_exact_resume=True,
            ),
            manifest_path,
        )
        with self.assertRaises(PathSafetyError):
            ensure_safe_run_directory(self.outputs / "current", output_root=self.outputs)

    def test_evaluator_contract_is_hash_sealed_and_validator_compatible(self) -> None:
        calendar = _am_pm_calendar_registry()
        resolved = self._resolve_for_calendar(calendar)
        prices = _event_price_registry(calendar)
        price_path = self.inputs / "event-prices.json"
        price_path.write_text(json.dumps(prices), encoding="utf-8")
        authoritative_before = canonical_json_bytes(resolved.to_dict())

        contract = resolved.to_evaluator_contract(
            price_registry_path=price_path,
            input_root=self.inputs,
        )

        self.assertEqual(authoritative_before, canonical_json_bytes(resolved.to_dict()))
        self.assertEqual(contract["manifest_hash"], contract["resolved_manifest_sha256"])
        self.assertEqual(contract["contract_version"], "2")
        self.assertEqual(
            contract["cohort"]["initial_cash_by_agent"],
            {"A001": 100, "A002": 100, "A003": 1_000},
        )
        self.assertEqual(
            resolved.to_dict()["initial_cash_by_agent"],
            contract["cohort"]["initial_cash_by_agent"],
        )
        unhashed_content = {
            key: value
            for key, value in contract.items()
            if key not in {"manifest_hash", "resolved_manifest_sha256"}
        }
        self.assertEqual(contract["manifest_hash"], canonical_sha256(unhashed_content))
        self.assertEqual(
            contract["conditions"][RN_COMM_OFF]["pair_invariant_hash"],
            contract["conditions"][RN_COMM_ON]["pair_invariant_hash"],
        )
        self.assertEqual(
            contract["conditions"][RN_COMM_OFF]["community_mode"],
            "off",
        )
        self.assertEqual(
            contract["conditions"][RN_COMM_ON]["community_mode"],
            "on",
        )
        self.assertEqual(
            [event["execution_price"] for event in contract["event_calendar"]["events"]],
            [70_001, 70_102, 70_201, 70_302],
        )
        verify_evaluator_contract_hash(
            contract,
            expected_authoritative_manifest_sha256=resolved.sha256,
            expected_price_registry_sha256=canonical_sha256(prices),
        )
        with self.assertRaisesRegex(ResolutionError, "different authoritative manifest"):
            verify_evaluator_contract_hash(
                contract,
                expected_authoritative_manifest_sha256=_digest("other-authoritative-manifest"),
            )
        validator_contract = parse_ab_contract(contract)
        self.assertEqual(len(validator_contract.expected_keys), 12)
        self.assertEqual(validator_contract.initial_cash_by_agent["A003"], 1_000)
        self.assertEqual(validator_contract.burn_in_dates, ("2026-02-27",))
        self.assertEqual(validator_contract.evaluation_dates, ("2026-03-03",))

        tampered = copy.deepcopy(contract)
        tampered["event_calendar"]["events"][0]["execution_price"] = 999_999
        with self.assertRaisesRegex(ResolutionError, "content hash mismatch"):
            verify_evaluator_contract_hash(tampered)

        missing_cash = copy.deepcopy(contract)
        del missing_cash["cohort"]["initial_cash_by_agent"]["A003"]
        missing_cash = {
            **missing_cash,
            "manifest_hash": canonical_sha256(
                {
                    key: value
                    for key, value in missing_cash.items()
                    if key not in {"manifest_hash", "resolved_manifest_sha256"}
                }
            ),
        }
        missing_cash["resolved_manifest_sha256"] = missing_cash["manifest_hash"]
        with self.assertRaisesRegex(ResolutionError, "exactly cover"):
            verify_evaluator_contract_hash(missing_cash)

    def test_evaluator_contract_rejects_non_exact_price_registry_and_irregular_calendar(self) -> None:
        calendar = _am_pm_calendar_registry()
        resolved = self._resolve_for_calendar(calendar)
        prices = _event_price_registry(calendar)
        prices["events"].pop()
        price_path = self.inputs / "missing-event-prices.json"
        price_path.write_text(json.dumps(prices), encoding="utf-8")
        with self.assertRaisesRegex(ResolutionError, "event IDs differ"):
            resolved.to_evaluator_contract(
                price_registry_path=price_path,
                input_root=self.inputs,
            )

        prices = _event_price_registry(calendar)
        prices["events"][0]["execution_price"] = "70000"
        price_path.write_text(json.dumps(prices), encoding="utf-8")
        with self.assertRaisesRegex(ResolutionError, "numeric JSON"):
            resolved.to_evaluator_contract(
                price_registry_path=price_path,
                input_root=self.inputs,
            )

        irregular_prices = _event_price_registry(self.calendar)
        irregular_path = self.inputs / "irregular-event-prices.json"
        irregular_path.write_text(json.dumps(irregular_prices), encoding="utf-8")
        with self.assertRaisesRegex(ResolutionError, "exactly one AM and one PM"):
            self._resolve().to_evaluator_contract(
                price_registry_path=irregular_path,
                input_root=self.inputs,
            )

    @unittest.skipUnless(hasattr(os, "symlink"), "platform has no symlink API")
    def test_symlinked_registry_is_rejected_before_parse(self) -> None:
        link = self.inputs / "cohort-link.json"
        try:
            link.symlink_to(self.cohort_path)
        except OSError as exc:  # pragma: no cover - platform/filesystem policy
            self.skipTest(f"symlink creation unavailable: {exc}")
        with self.assertRaises(PathSafetyError):
            resolve_study(
                StudySpec.from_dict(copy.deepcopy(self.spec_payload)),
                cohort_registry_path=link,
                calendar_event_registry_path=self.calendar_path,
                stage_input_registry_path=self.stage_input_path,
                input_root=self.inputs,
            )


if __name__ == "__main__":
    unittest.main()
