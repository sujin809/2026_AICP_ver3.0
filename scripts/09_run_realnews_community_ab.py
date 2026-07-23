#!/usr/bin/env python3
"""Paper entrypoint for a *local-only* RN Community A/B preflight.

The requested experiment has paid-model and atomic-phase requirements that are
not safely satisfied by a convenience launcher.  This entrypoint therefore
accepts exactly one mode: it seals and validates the run inputs, creates two
isolated paper namespaces, and exits before any model client or network code
can be reached.  A future execution mode must be implemented as an atomic
phase runner and must not be added as a permissive flag to this script.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

# Direct ``python scripts/...`` execution sets ``sys.path[0]`` to this
# directory rather than the repository root.  Keep the entrypoint self-
# contained while avoiding an installation-only assumption in paper ops.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from twinmarket_kr.rn_ab.run_bundle import RunBundleError, prepare_preflight_bundle


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Seal and validate a no-network RN Community A/B preflight bundle."
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        required=True,
        help="Required safety gate; this entrypoint intentionally has no execution mode.",
    )
    parser.add_argument("--run-id", required=True, help="New non-legacy RN run identifier.")
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--study-spec", type=Path, required=True)
    parser.add_argument("--cohort-registry", type=Path, required=True)
    parser.add_argument(
        "--persona-snapshot-dir",
        type=Path,
        required=True,
        help="Verified RN persona snapshot directory under input-root.",
    )
    parser.add_argument(
        "--prompt-dir",
        type=Path,
        required=True,
        help="Verified RN STB/analysis/decision/LTB prompt directory under input-root.",
    )
    parser.add_argument("--calendar-event-registry", type=Path, required=True)
    parser.add_argument("--stage-input-registry", type=Path, required=True)
    parser.add_argument("--event-price-registry", type=Path, required=True)
    parser.add_argument("--real-news-bundle", type=Path, required=True)
    parser.add_argument("--known-injection-registry", type=Path, required=True)
    parser.add_argument("--article-version-leakage-review-manifest", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        bundle = prepare_preflight_bundle(
            run_id=args.run_id,
            input_root=args.input_root,
            output_root=args.output_root,
            study_spec_path=args.study_spec,
            cohort_registry_path=args.cohort_registry,
            persona_snapshot_dir=args.persona_snapshot_dir,
            prompt_dir=args.prompt_dir,
            calendar_event_registry_path=args.calendar_event_registry,
            stage_input_registry_path=args.stage_input_registry,
            event_price_registry_path=args.event_price_registry,
            real_news_bundle_path=args.real_news_bundle,
            known_injection_registry_path=args.known_injection_registry,
            article_version_leakage_review_manifest_path=(
                args.article_version_leakage_review_manifest
            ),
        )
    except RunBundleError as exc:
        parser.exit(2, f"RN preflight rejected: {exc}\n")
    print(
        json.dumps(
            {
                "mode": "preflight_only_no_network_no_paid_api",
                "execution_authorized": False,
                "run_dir": str(bundle.run_dir),
                "resolved_manifest": str(bundle.resolved_manifest_path),
                "evaluator_contract": str(bundle.evaluator_contract_path),
                "article_version_leakage_review": str(bundle.leakage_review_path),
                "persona_snapshot": str(bundle.persona_snapshot_dir),
                "prompt_bundle": str(bundle.prompt_bundle_dir),
                "generated_preflight_inputs": str(bundle.generated_inputs_dir),
                "generated_input_manifest": str(bundle.generated_input_manifest_path),
                "run_record": str(bundle.run_record_path),
                "run_record_markdown": str(bundle.run_record_markdown_path),
                "source_hashes": str(bundle.source_hashes_path),
                "condition_dbs": {key: str(value) for key, value in bundle.condition_db_paths.items()},
                "journals": {key: str(value) for key, value in bundle.journal_paths.items()},
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
