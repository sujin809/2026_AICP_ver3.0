#!/usr/bin/env python3
"""Build/validate local no-go source-input candidates for RN Community AB.

This command never contacts a network or LLM provider and never changes a
source database or input CSV.  Its output is intentionally non-executable:
every candidate records ``execution_authorized=false`` and
``run_eligible=false`` until a separately approved, immutable as-of dataset
is available.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from twinmarket_kr.rn_ab.source_candidates import (  # noqa: E402
    CandidateBuildConfig,
    SourceCandidateError,
    SourceInputPaths,
    build_source_input_candidates,
    validate_source_input_candidates,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build/validate deterministic local-only RN source input candidates."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    default_destination = (
        PROJECT_ROOT / "preparation" / "rn_ab_source_candidate_v1" / "input_candidates"
    )

    build = subparsers.add_parser("build", help="Create a new explicit NO-GO candidate directory.")
    build.add_argument("--destination", type=Path, default=default_destination)
    build.add_argument("--start-date", default="2026-02-27")
    build.add_argument("--end-date", default="2026-05-04")
    build.add_argument(
        "--burn-in-date",
        action="append",
        dest="burn_in_dates",
        default=None,
        help="Repeat exactly for each ordered burn-in date; default is the documented 3-date set.",
    )

    validate = subparsers.add_parser("validate", help="Verify hashes and immutable no-go flags.")
    validate.add_argument("--candidate-dir", type=Path, default=default_destination)
    validate.add_argument(
        "--skip-source-hash-check",
        action="store_true",
        help="Only validate candidate bytes/self-hashes; normally source drift is rejected.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    source_paths = SourceInputPaths.from_project_root(PROJECT_ROOT)
    try:
        if args.command == "build":
            config = CandidateBuildConfig(
                start_date=args.start_date,
                end_date=args.end_date,
                burn_in_dates=tuple(args.burn_in_dates)
                if args.burn_in_dates is not None
                else ("2026-02-27", "2026-03-03", "2026-03-04"),
            )
            artifacts = build_source_input_candidates(
                destination_dir=args.destination,
                paths=source_paths,
                config=config,
            )
            payload = {
                "mode": "local_only_no_network_no_paid_api",
                "candidate_root": str(artifacts.root),
                "audit_path": str(artifacts.audit_path),
                "candidate_paths": {
                    kind: str(path) for kind, path in sorted(artifacts.candidate_paths.items())
                },
                "execution_authorized": artifacts.execution_authorized,
                "run_eligible": artifacts.run_eligible,
                "network_requests": 0,
                "paid_api_calls": 0,
            }
        else:
            payload = dict(
                validate_source_input_candidates(
                    root=args.candidate_dir,
                    project_root=PROJECT_ROOT,
                    check_source_files=not args.skip_source_hash_check,
                )
            )
            payload["mode"] = "local_only_no_network_no_paid_api"
            payload["network_requests"] = 0
            payload["paid_api_calls"] = 0
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    except (OSError, SourceCandidateError) as exc:
        parser.exit(2, f"RN source candidate preparation rejected: {exc}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
