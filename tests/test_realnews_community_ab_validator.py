from __future__ import annotations

import copy
import csv
import hashlib
import json
import os
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from twinmarket_kr.experiment_runtime import file_sha256
from twinmarket_kr.rn_ab import canonical_sha256
from validation.validate_realnews_community_ab import (
    ContractError,
    _load_manifest,
    parse_ab_contract,
    validate_realnews_community_ab,
    write_evaluation_artifacts,
)


_UNSET = object()


@dataclass
class PaperRun:
    manifest: dict[str, Any]
    authoritative_manifest: dict[str, Any]
    target_registry: dict[str, Any]
    authoritative_hash: str
    price_registry_hash: str
    target_registry_hash: str
    off: Path
    on: Path
    finalization_record: Path
    final_fill_export_index: Path


class RealNewsCommunityABValidatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _digest(label: str) -> str:
        return hashlib.sha256(label.encode("utf-8")).hexdigest()

    @classmethod
    def _seal(cls, content: dict[str, Any]) -> dict[str, Any]:
        """Return a resolver-compatible evaluator envelope with its real hash."""

        sealed = copy.deepcopy(content)
        content_hash = canonical_sha256(sealed)
        sealed["manifest_hash"] = content_hash
        sealed["resolved_manifest_sha256"] = content_hash
        return sealed

    @classmethod
    def _resign(cls, envelope: dict[str, Any]) -> dict[str, Any]:
        return cls._seal(
            {
                key: value
                for key, value in envelope.items()
                if key not in {"manifest_hash", "resolved_manifest_sha256"}
            }
        )

    @classmethod
    def paper_material(cls) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str, str, str]:
        """Build separately hash-pinned resolver, evaluator, price, and target artifacts."""

        source_study_spec_hash = cls._digest("study-spec")
        authoritative = {
            "artifact_type": "resolved_study_manifest",
            "source_study_spec_sha256": source_study_spec_hash,
            "conditions": ["RN_COMM_OFF", "RN_COMM_ON"],
            "condition_treatments": {
                "RN_COMM_OFF": {"community_mode": "off", "news_treatment": "real_only"},
                "RN_COMM_ON": {"community_mode": "on", "news_treatment": "real_only"},
            },
            "agent_ids": ["A001", "A002"],
            "initial_cash_by_agent": {"A001": 100, "A002": 1_000},
            "burn_in_date_ids": ["2026-01-05"],
            "evaluation_date_ids": ["2026-01-06"],
            "decision_events": [
                {"decision_event_id": "2026-01-05/AM", "date": "2026-01-05", "subturn": "AM"},
                {"decision_event_id": "2026-01-05/PM", "date": "2026-01-05", "subturn": "PM"},
                {"decision_event_id": "2026-01-06/AM", "date": "2026-01-06", "subturn": "AM"},
                {"decision_event_id": "2026-01-06/PM", "date": "2026-01-06", "subturn": "PM"},
            ],
            "trade_policy": {"stock_code": "005930"},
        }
        authoritative_hash = canonical_sha256(authoritative)
        price_registry_hash = cls._digest("price-registry")
        pair_id = "rn-pair-001"
        pair_invariant_hash = cls._digest("all-non-community-inputs")
        shared_input_hashes = {
            "cohort": cls._digest("cohort"),
            "calendar": cls._digest("calendar"),
            "prices": price_registry_hash,
        }
        evaluator = cls._seal(
            {
                "artifact_type": "rn_ab_evaluator_contract",
                "contract_version": "2",
                "manifest_hash_algorithm": (
                    "sha256-utf8-compact-json-object-excluding-"
                    "manifest_hash-and-resolved_manifest_sha256"
                ),
                "authoritative_resolved_manifest_sha256": authoritative_hash,
                "source_study_spec_sha256": source_study_spec_hash,
                "condition_pair_id": pair_id,
                "pair_invariant_hash": pair_invariant_hash,
                "conditions": {
                    "RN_COMM_OFF": {
                        "condition_id": "RN_COMM_OFF",
                        "condition_pair_id": pair_id,
                        "community_mode": "off",
                        "news_treatment": "real_only",
                        "pair_invariant_hash": pair_invariant_hash,
                        "input_hashes": shared_input_hashes,
                    },
                    "RN_COMM_ON": {
                        "condition_id": "RN_COMM_ON",
                        "condition_pair_id": pair_id,
                        "community_mode": "on",
                        "news_treatment": "real_only",
                        "pair_invariant_hash": pair_invariant_hash,
                        "input_hashes": shared_input_hashes,
                    },
                },
                "cohort": {
                    "agent_ids": ["A001", "A002"],
                    "initial_cash_by_agent": {"A001": 100, "A002": 1_000},
                    "canonical_sha256": cls._digest("cohort-registry"),
                },
                "event_calendar": {
                    "events": [
                        {
                            "event_id": "2026-01-05/AM",
                            "date": "2026-01-05",
                            "session": "AM",
                            "execution_price_field": "actual_open",
                            "execution_price": 100,
                        },
                        {
                            "event_id": "2026-01-05/PM",
                            "date": "2026-01-05",
                            "session": "PM",
                            "execution_price_field": "actual_close",
                            "execution_price": 102,
                        },
                        {
                            "event_id": "2026-01-06/AM",
                            "date": "2026-01-06",
                            "session": "AM",
                            "execution_price_field": "actual_open",
                            "execution_price": 104,
                        },
                        {
                            "event_id": "2026-01-06/PM",
                            "date": "2026-01-06",
                            "session": "PM",
                            "execution_price_field": "actual_close",
                            "execution_price": 106,
                        },
                    ]
                },
                "burn_in_dates": ["2026-01-05"],
                "evaluation_dates": ["2026-01-06"],
                "price_registry": {
                    "version": "prices-v1",
                    "stock_code": "005930",
                    "calendar_event_registry_sha256": cls._digest("calendar-registry"),
                    "canonical_sha256": price_registry_hash,
                    "file_sha256": cls._digest("price-file"),
                },
            }
        )
        targets = {
            "artifact_type": "rn_ab_evaluator_target_registry",
            "version": "2",
            "authoritative_resolved_manifest_sha256": authoritative_hash,
            "price_registry_sha256": price_registry_hash,
            "input_dates": ["2026-01-05", "2026-01-06"],
            "evaluation_dates": ["2026-01-06"],
            "target_values": {"2026-01-05": -1, "2026-01-06": 1},
        }
        return (
            evaluator,
            authoritative,
            targets,
            authoritative_hash,
            price_registry_hash,
            canonical_sha256(targets),
        )

    @staticmethod
    def generic_manifest() -> dict[str, Any]:
        """Legacy-shaped input accepted only by the parser compatibility helper."""

        return {
            "manifest_hash": "generic-manifest-hash",
            "condition_pair_id": "rn-pair-001",
            "conditions": {
                "RN_COMM_OFF": {
                    "condition_id": "RN_COMM_OFF",
                    "condition_pair_id": "rn-pair-001",
                    "community_mode": "off",
                    "pair_invariant_hash": "all-non-community-inputs-sha",
                },
                "RN_COMM_ON": {
                    "condition_id": "RN_COMM_ON",
                    "condition_pair_id": "rn-pair-001",
                    "community_mode": "on",
                    "pair_invariant_hash": "all-non-community-inputs-sha",
                },
            },
            "cohort": {"agent_ids": ["A001", "A002"]},
            "event_calendar": {
                "events": [
                    {"event_id": "2026-01-05/AM", "date": "2026-01-05", "session": "AM", "open_price": 100},
                    {"event_id": "2026-01-05/PM", "date": "2026-01-05", "session": "PM", "close_price": 102},
                    {"event_id": "2026-01-06/AM", "date": "2026-01-06", "session": "AM", "open_price": 104},
                    {"event_id": "2026-01-06/PM", "date": "2026-01-06", "session": "PM", "close_price": 106},
                ]
            },
            "burn_in_dates": ["2026-01-05"],
            "evaluation_dates": ["2026-01-06"],
        }

    @staticmethod
    def ledger_rows(
        condition_id: str,
        *,
        buy_quantity: int,
        manifest_hash: str,
    ) -> list[dict[str, object]]:
        events = (
            ("2026-01-05/AM", "2026-01-05", "AM", 100),
            ("2026-01-05/PM", "2026-01-05", "PM", 102),
            ("2026-01-06/AM", "2026-01-06", "AM", 104),
            ("2026-01-06/PM", "2026-01-06", "PM", 106),
        )
        rows: list[dict[str, object]] = []
        for event_id, date, session, price in events:
            rows.append(
                {
                    "fill_id": f"{condition_id}-{event_id}-A001",
                    "condition_id": condition_id,
                    "manifest_hash": manifest_hash,
                    "agent_id": "A001",
                    "event_id": event_id,
                    "date": date,
                    "session": session,
                    "stock_code": "005930",
                    "action": "BUY",
                    "fill_status": "filled",
                    "requested_quantity": buy_quantity,
                    "filled_quantity": buy_quantity,
                    "fill_price": price,
                    "fee_amount": 0,
                }
            )
            rows.append(
                {
                    "fill_id": f"{condition_id}-{event_id}-A002",
                    "condition_id": condition_id,
                    "manifest_hash": manifest_hash,
                    "agent_id": "A002",
                    "event_id": event_id,
                    "date": date,
                    "session": session,
                    "stock_code": "005930",
                    "action": "SELL",
                    "fill_status": "filled",
                    "requested_quantity": 1,
                    "filled_quantity": 1,
                    "fill_price": price,
                    "fee_amount": 0,
                }
            )
        return rows

    def write_ledger(self, name: str, rows: list[dict[str, object]]) -> Path:
        path = self.root / name
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        return path

    def valid_run(self) -> PaperRun:
        (
            manifest,
            authoritative,
            targets,
            authoritative_hash,
            price_registry_hash,
            target_registry_hash,
        ) = self.paper_material()
        off = self.write_ledger(
            "off.csv",
            self.ledger_rows("RN_COMM_OFF", buy_quantity=2, manifest_hash=manifest["manifest_hash"]),
        )
        on = self.write_ledger(
            "on.csv",
            self.ledger_rows("RN_COMM_ON", buy_quantity=3, manifest_hash=manifest["manifest_hash"]),
        )
        exports = {
            "RN_COMM_OFF": {
                "path": off.name,
                "sha256": file_sha256(off),
                "row_count": 8,
                "format": "rn_canonical_final_fill_csv_v1",
            },
            "RN_COMM_ON": {
                "path": on.name,
                "sha256": file_sha256(on),
                "row_count": 8,
                "format": "rn_canonical_final_fill_csv_v1",
            },
        }
        export_index = self.root / "final_fill_export_index.json"
        export_index.write_text(
            json.dumps(
                {
                    "artifact_type": "rn_final_fill_export_index",
                    "version": "1",
                    "evaluator_contract_sha256": manifest["manifest_hash"],
                    "exports": exports,
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        finalization = self.root / "RUN_FINALIZATION.json"
        finalization.write_text(
            json.dumps(
                {
                    "artifact_type": "rn_ab_finalization_record",
                    "version": "1",
                    "run_id": "run-1",
                    "status": "local_integrity_passed_pending_evaluator_only_target_join",
                    "resolved_manifest_sha256": authoritative_hash,
                    "evaluator_contract_sha256": manifest["manifest_hash"],
                    "expected_agent_event_key_count": 8,
                    "lineage": {
                        condition: {"expected_keys": 8} for condition in ("RN_COMM_OFF", "RN_COMM_ON")
                    },
                    "journals": {
                        condition: {"pending": 0, "rolled_back": 0, "committed": 1}
                        for condition in ("RN_COMM_OFF", "RN_COMM_ON")
                    },
                    "final_fill_export_index": {
                        "path": export_index.name,
                        "sha256": file_sha256(export_index),
                        "exports": exports,
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return PaperRun(
            manifest=manifest,
            authoritative_manifest=authoritative,
            target_registry=targets,
            authoritative_hash=authoritative_hash,
            price_registry_hash=price_registry_hash,
            target_registry_hash=target_registry_hash,
            off=off,
            on=on,
            finalization_record=finalization,
            final_fill_export_index=export_index,
        )

    def validate(
        self,
        run: PaperRun,
        *,
        manifest: object = _UNSET,
        off: object = _UNSET,
        on: object = _UNSET,
        target_values: object = _UNSET,
        authoritative_manifest: object = _UNSET,
        authoritative_hash: object = _UNSET,
        price_registry_hash: object = _UNSET,
        target_registry_hash: object = _UNSET,
    ) -> dict[str, Any]:
        selected_off = run.off if off is _UNSET else off
        selected_on = run.on if on is _UNSET else on
        finalization_record = run.finalization_record
        export_index = run.final_fill_export_index
        if off is not _UNSET or on is not _UNSET:
            # Build an internally consistent handoff envelope so adversarial
            # ledger fixtures reach the row-level validator.  A separate test
            # below verifies that changing a CSV without updating this chain is
            # rejected before row parsing.
            off_path = Path(selected_off)  # type: ignore[arg-type]
            on_path = Path(selected_on)  # type: ignore[arg-type]
            suffix = self._digest(f"{off_path}:{file_sha256(off_path)}:{on_path}:{file_sha256(on_path)}")[:12]
            exports = {
                "RN_COMM_OFF": {
                    "path": off_path.name,
                    "sha256": file_sha256(off_path),
                    "row_count": 8,
                    "format": "rn_canonical_final_fill_csv_v1",
                },
                "RN_COMM_ON": {
                    "path": on_path.name,
                    "sha256": file_sha256(on_path),
                    "row_count": 8,
                    "format": "rn_canonical_final_fill_csv_v1",
                },
            }
            export_index = self.root / f"final_fill_export_index_{suffix}.json"
            export_index.write_text(
                json.dumps(
                    {
                        "artifact_type": "rn_final_fill_export_index",
                        "version": "1",
                        "evaluator_contract_sha256": run.manifest["manifest_hash"],
                        "exports": exports,
                    },
                    sort_keys=True,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            finalization_record = self.root / f"RUN_FINALIZATION_{suffix}.json"
            finalization_record.write_text(
                json.dumps(
                    {
                        "artifact_type": "rn_ab_finalization_record",
                        "version": "1",
                        "run_id": "run-1",
                        "status": "local_integrity_passed_pending_evaluator_only_target_join",
                        "resolved_manifest_sha256": run.authoritative_hash,
                        "evaluator_contract_sha256": run.manifest["manifest_hash"],
                        "expected_agent_event_key_count": 8,
                        "lineage": {
                            condition: {"expected_keys": 8}
                            for condition in ("RN_COMM_OFF", "RN_COMM_ON")
                        },
                        "journals": {
                            condition: {"pending": 0, "rolled_back": 0, "committed": 1}
                            for condition in ("RN_COMM_OFF", "RN_COMM_ON")
                        },
                        "final_fill_export_index": {
                            "path": export_index.name,
                            "sha256": file_sha256(export_index),
                            "exports": exports,
                        },
                    },
                    sort_keys=True,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        return validate_realnews_community_ab(
            run.manifest if manifest is _UNSET else manifest,  # type: ignore[arg-type]
            off_final_fills=selected_off,  # type: ignore[arg-type]
            on_final_fills=selected_on,  # type: ignore[arg-type]
            finalization_record_path=finalization_record,
            final_fill_export_index_path=export_index,
            target_values=run.target_registry if target_values is _UNSET else target_values,  # type: ignore[arg-type]
            authoritative_resolved_manifest=(
                run.authoritative_manifest if authoritative_manifest is _UNSET else authoritative_manifest
            ),  # type: ignore[arg-type]
            expected_authoritative_manifest_sha256=(
                run.authoritative_hash if authoritative_hash is _UNSET else authoritative_hash
            ),  # type: ignore[arg-type]
            expected_price_registry_sha256=(
                run.price_registry_hash if price_registry_hash is _UNSET else price_registry_hash
            ),  # type: ignore[arg-type]
            expected_target_registry_sha256=(
                run.target_registry_hash if target_registry_hash is _UNSET else target_registry_hash
            ),  # type: ignore[arg-type]
        )

    def test_valid_pair_reports_paired_effect_and_all_independent_provenance_hashes(self) -> None:
        run = self.valid_run()

        result = self.validate(run)

        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["stock_code"], "005930")
        self.assertEqual(result["authoritative_resolved_manifest_sha256"], run.authoritative_hash)
        self.assertEqual(result["price_registry_sha256"], run.price_registry_hash)
        self.assertEqual(result["target_registry_sha256"], run.target_registry_hash)
        self.assertEqual(result["expected_final_fill_rows_per_arm"], 8)
        self.assertEqual(result["burn_in_dates"], ["2026-01-05"])
        self.assertEqual(result["evaluation_dates"], ["2026-01-06"])
        self.assertEqual(len(result["daily_gross_signed_fill_value"]), 1)
        daily = result["daily_gross_signed_fill_value"][0]
        self.assertEqual(daily["date"], "2026-01-06")
        self.assertEqual(daily["RN_COMM_OFF_AM_gross_signed_fill_value"], 104.0)
        self.assertEqual(daily["RN_COMM_OFF_PM_gross_signed_fill_value"], 106.0)
        self.assertEqual(daily["RN_COMM_OFF_gross_signed_fill_value"], 210.0)
        self.assertEqual(daily["RN_COMM_OFF_direction"], "BUY")
        self.assertEqual(daily["Individuals_direction"], "BUY")
        self.assertEqual(daily["RN_COMM_OFF_direction_matches_Individuals"], 1)
        self.assertEqual(daily["RN_COMM_ON_gross_signed_fill_value"], 420.0)
        self.assertEqual(daily["community_effect_on_minus_off"], 210.0)
        self.assertEqual(result["paired_effect"]["sum"], 210.0)
        self.assertEqual(result["target_metrics"]["RN_COMM_OFF_direction_accuracy"], 1.0)
        self.assertEqual(
            result["target_metrics"]["RN_COMM_OFF"]["confusion_matrix"]["actual_buy"],
            {"predicted_buy": 1, "predicted_sell": 0, "predicted_flat": 0},
        )
        self.assertEqual(result["target_metrics"]["full_period_diagnostic"]["RN_COMM_OFF"]["balanced_accuracy"], 0.5)
        self.assertEqual(result["rq2_status"], "computed_from_sealed_agent_initial_cash_map")
        self.assertAlmostEqual(
            result["rq2_community_effect"]["mean_daily_community_effect"], 1.05
        )
        wealth = result["wealth_sensitivity_v1"]
        self.assertTrue(wealth["one_eok_only_rich_excluded_alias_byte_equal"])
        self.assertEqual(wealth["one_eok_only"], wealth["rich_excluded"])
        self.assertEqual(wealth["ten_eok_only"]["agent_ids"], ["A002"])
        self.assertEqual(len(wealth["leave_one_rich_out"]), 1)
        self.assertFalse(wealth["wealth_fragile"])
        self.assertIsNone(wealth["robust_p3b_pass"])

    def test_missing_extra_and_duplicate_canonical_keys_are_rejected_instead_of_intersected(self) -> None:
        run = self.valid_run()
        baseline = self.ledger_rows(
            "RN_COMM_OFF", buy_quantity=2, manifest_hash=run.manifest["manifest_hash"]
        )
        cases = {
            "missing": baseline[:-1],
            "duplicate": [*baseline, copy.deepcopy(baseline[0])],
            "extra": [
                *baseline,
                {
                    **copy.deepcopy(baseline[0]),
                    "fill_id": "extra-fill",
                    "agent_id": "OUT_OF_COHORT",
                },
            ],
        }
        for name, rows in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(ContractError):
                    self.validate(run, off=self.write_ledger(f"{name}.csv", rows))

    def test_price_action_status_stock_full_fill_and_fee_contracts_are_all_fail_closed(self) -> None:
        run = self.valid_run()
        baseline = self.ledger_rows(
            "RN_COMM_OFF", buy_quantity=2, manifest_hash=run.manifest["manifest_hash"]
        )
        mutations = {
            "wrong_am_price": {"fill_price": 101},
            "hold_action": {"action": "HOLD"},
            "partial_fill": {"filled_quantity": 1},
            "nonzero_fee": {"fee_amount": "0.01"},
            "wrong_stock": {"stock_code": "000660"},
            "unfilled_status": {"fill_status": "pending"},
        }
        for name, changes in mutations.items():
            with self.subTest(name=name):
                rows = copy.deepcopy(baseline)
                rows[0].update(changes)
                with self.assertRaises(ContractError):
                    self.validate(run, off=self.write_ledger(f"{name}.csv", rows))

    def test_pair_invariant_drift_is_rejected_even_when_a_tampered_envelope_is_resigned(self) -> None:
        run = self.valid_run()
        drifted = copy.deepcopy(run.manifest)
        drifted["conditions"]["RN_COMM_ON"]["pair_invariant_hash"] = self._digest("changed-inputs")

        with self.assertRaisesRegex(ContractError, "arm descriptors|invariant hashes"):
            self.validate(run, manifest=self._resign(drifted))

    def test_parser_compatibility_is_not_a_public_paper_validation_bypass(self) -> None:
        generic = self.generic_manifest()
        parsed = parse_ab_contract(generic)
        self.assertEqual(parsed.manifest_hash, "generic-manifest-hash")
        self.assertIsNone(parsed.initial_cash_by_agent)

        with self.assertRaisesRegex(ContractError, "artifact_type='rn_ab_evaluator_contract'"):
            self.validate(self.valid_run(), manifest=generic)

    def test_paper_contract_requires_external_authoritative_and_price_provenance(self) -> None:
        run = self.valid_run()
        tampered = copy.deepcopy(run.manifest)
        tampered["event_calendar"]["events"][0]["execution_price"] = 999
        with self.assertRaisesRegex(ContractError, "evaluator-contract verification failed"):
            self.validate(run, manifest=tampered)

        fabricated = copy.deepcopy(run.manifest)
        fabricated["authoritative_resolved_manifest_sha256"] = self._digest("fabricated-authority")
        with self.assertRaisesRegex(ContractError, "different authoritative manifest"):
            self.validate(run, manifest=self._resign(fabricated))

        with self.assertRaisesRegex(ContractError, "requires expected_price_registry_sha256"):
            self.validate(run, price_registry_hash=None)

        altered_authoritative = copy.deepcopy(run.authoritative_manifest)
        altered_authoritative["agent_ids"] = ["A001", "A999"]
        with self.assertRaisesRegex(ContractError, "trusted expected hash"):
            self.validate(run, authoritative_manifest=altered_authoritative)

    def test_paper_contract_hash_and_exact_two_arm_requirements_are_mandatory(self) -> None:
        run = self.valid_run()
        non_sha = copy.deepcopy(run.manifest)
        non_sha["manifest_hash"] = "not-a-sha256"
        non_sha["resolved_manifest_sha256"] = "not-a-sha256"
        with self.assertRaisesRegex(ContractError, "64-character SHA-256"):
            self.validate(run, manifest=non_sha)

        with_extra = copy.deepcopy(run.manifest)
        extra_arm = copy.deepcopy(with_extra["conditions"]["RN_COMM_OFF"])
        extra_arm["condition_id"] = "RN_FAKE"
        with_extra["conditions"]["RN_FAKE"] = extra_arm
        with self.assertRaisesRegex(ContractError, "exactly RN_COMM_OFF and RN_COMM_ON"):
            self.validate(run, manifest=self._resign(with_extra))

        legacy_v1 = copy.deepcopy(run.manifest)
        legacy_v1["contract_version"] = "1"
        with self.assertRaisesRegex(ContractError, "unsupported evaluator contract version"):
            self.validate(run, manifest=self._resign(legacy_v1))

    def test_evaluator_initial_cash_is_exactly_bound_to_authoritative_manifest(self) -> None:
        run = self.valid_run()
        changed = copy.deepcopy(run.manifest)
        changed["cohort"]["initial_cash_by_agent"]["A002"] = 2_000
        with self.assertRaisesRegex(ContractError, "differs from authoritative"):
            self.validate(run, manifest=self._resign(changed))

    def test_wealth_fragile_detects_leave_one_rich_rq2_sign_transition(self) -> None:
        run = self.valid_run()
        on_rows = self.ledger_rows(
            "RN_COMM_ON", buy_quantity=3, manifest_hash=run.manifest["manifest_hash"]
        )
        for row in on_rows:
            if row["agent_id"] == "A002" and row["date"] == "2026-01-06":
                row["requested_quantity"] = 20
                row["filled_quantity"] = 20
        result = self.validate(run, on=self.write_ledger("wealth-fragile-on.csv", on_rows))

        wealth = result["wealth_sensitivity_v1"]
        self.assertTrue(wealth["wealth_fragile"])
        self.assertIn(
            "A002:rq2_mean_effect_sign_or_zero_changed",
            wealth["wealth_fragile_reasons"],
        )
        self.assertLess(
            wealth["initial_capital_normalized_equal_agent"]["mean_daily_community_effect"],
            0,
        )
        self.assertGreater(
            wealth["leave_one_rich_out"][0]["initial_capital_normalized_equal_agent"][
                "mean_daily_community_effect"
            ],
            0,
        )

    def test_evaluator_only_target_registry_is_required_hash_pinned_and_exactly_bound(self) -> None:
        run = self.valid_run()
        with self.assertRaisesRegex(ContractError, "requires evaluator-only target_values registry"):
            self.validate(run, target_values=None)

        with self.assertRaisesRegex(ContractError, "invalid fields"):
            self.validate(run, target_values={"target_values": {"2026-01-06": 1}})

        tampered = copy.deepcopy(run.target_registry)
        tampered["target_values"]["2026-01-06"] = -1
        with self.assertRaisesRegex(ContractError, "target registry hash differs"):
            self.validate(run, target_values=tampered)

        wrong_dates = copy.deepcopy(run.target_registry)
        wrong_dates["evaluation_dates"] = ["2026-01-05"]
        with self.assertRaisesRegex(ContractError, "evaluation dates differ"):
            self.validate(
                run,
                target_values=wrong_dates,
                target_registry_hash=canonical_sha256(wrong_dates),
            )

        zero_target = copy.deepcopy(run.target_registry)
        zero_target["target_values"]["2026-01-06"] = 0
        with self.assertRaisesRegex(ContractError, "must be non-zero"):
            self.validate(
                run,
                target_values=zero_target,
                target_registry_hash=canonical_sha256(zero_target),
            )

    def test_human_readable_evaluation_bundle_is_hash_indexed_and_immutable(self) -> None:
        run = self.valid_run()
        result = self.validate(run)
        output_dir = self.root / "evaluation"

        artifacts = write_evaluation_artifacts(output_dir, result)

        with artifacts.daily_flow_csv.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["date"], "2026-01-06")
        self.assertEqual(rows[0]["RN_COMM_OFF_direction"], "BUY")
        index = json.loads(artifacts.artifact_index_json.read_text(encoding="utf-8"))
        self.assertEqual(index["artifacts"]["daily_flow_comparison"]["row_count"], 1)
        self.assertEqual(write_evaluation_artifacts(output_dir, result), artifacts)

        changed = copy.deepcopy(result)
        changed["daily_gross_signed_fill_value"][0]["RN_COMM_OFF_direction"] = "SELL"
        with self.assertRaisesRegex(ContractError, "different content"):
            write_evaluation_artifacts(output_dir, changed)

    def test_evaluator_rejects_final_fill_not_bound_to_integrity_handoff(self) -> None:
        run = self.valid_run()
        run.off.write_text(run.off.read_text(encoding="utf-8") + "\n", encoding="utf-8")

        with self.assertRaisesRegex(ContractError, "CSV hash differs from finalization index"):
            self.validate(run)

        with self.assertRaisesRegex(ContractError, "requires the integrity-gated"):
            validate_realnews_community_ab(
                run.manifest,
                off_final_fills=run.off,
                on_final_fills=run.on,
                finalization_record_path=None,
                final_fill_export_index_path=run.final_fill_export_index,
                target_values=run.target_registry,
                authoritative_resolved_manifest=run.authoritative_manifest,
                expected_authoritative_manifest_sha256=run.authoritative_hash,
                expected_price_registry_sha256=run.price_registry_hash,
                expected_target_registry_sha256=run.target_registry_hash,
            )

    def test_requested_quantity_cannot_be_defaulted_from_a_legacy_quantity_column(self) -> None:
        run = self.valid_run()
        rows = self.ledger_rows(
            "RN_COMM_OFF", buy_quantity=2, manifest_hash=run.manifest["manifest_hash"]
        )
        for row in rows:
            row.pop("requested_quantity")
            row["quantity"] = row["filled_quantity"]
        with self.assertRaisesRegex(ContractError, "requested_quantity"):
            self.validate(run, off=self.write_ledger("legacy_quantity.csv", rows))

    @unittest.skipUnless(hasattr(os, "symlink"), "platform has no symlink API")
    def test_cli_input_helpers_reject_leaf_and_parent_symlinked_manifest_and_final_fill(self) -> None:
        source_directory = self.root / "real-inputs"
        source_directory.mkdir()
        manifest_path = source_directory / "manifest.json"
        manifest_path.write_text(json.dumps(self.valid_run().manifest), encoding="utf-8")
        manifest_link = self.root / "manifest-link.json"
        parent_link = self.root / "input-link"
        try:
            manifest_link.symlink_to(manifest_path)
            parent_link.symlink_to(source_directory, target_is_directory=True)
        except OSError as exc:  # pragma: no cover - filesystem policy
            self.skipTest(f"symlink creation unavailable: {exc}")
        with self.assertRaisesRegex(ContractError, "symbolic link"):
            _load_manifest(manifest_link)
        with self.assertRaisesRegex(ContractError, "symbolic link"):
            _load_manifest(parent_link / "manifest.json")

        run = self.valid_run()
        fill_link = self.root / "off-link.csv"
        fill_link.symlink_to(run.off)
        with self.assertRaisesRegex(ContractError, "symbolic link"):
            self.validate(run, off=fill_link)


if __name__ == "__main__":
    unittest.main()
