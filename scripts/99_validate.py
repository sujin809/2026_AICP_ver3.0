#!/usr/bin/env python3
"""Validate one integrated run from its run-local canonical ledger."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from twinmarket_kr.run_integrity import (  # noqa: E402
    CanonicalRunValidationError,
    validate_canonical_run,
)


def require_external_output(output: Path, run_dir: Path) -> Path:
    """Reject derivative records that would mutate a signed run directory."""
    resolved_output = output.resolve()
    resolved_run_dir = run_dir.resolve()
    if resolved_output == resolved_run_dir or resolved_output.is_relative_to(
        resolved_run_dir
    ):
        raise ValueError(
            "--output must be outside --run-dir so the signed run artifact set "
            "remains immutable."
        )
    return resolved_output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate .runtime/committed.db, completion state, sealed-news "
            "coverage, lineage, outcomes, community state, and provenance logs."
        )
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        required=True,
        help="Integrated simulation run directory.",
    )
    parser.add_argument(
        "--allow-segment",
        action="store_true",
        help=(
            "Allow a segment_complete run for debugging. The result is "
            "explicitly not publication-ready."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        help=(
            "Optional path for the JSON validation record. It must be outside "
            "--run-dir because validation records are derivative artifacts."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        output = (
            require_external_output(args.output, args.run_dir)
            if args.output is not None
            else None
        )
        report = validate_canonical_run(
            args.run_dir,
            publication_ready=not args.allow_segment,
            verify_logs=True,
        )
    except (CanonicalRunValidationError, RuntimeError, ValueError) as exc:
        print(f"VALIDATION_FAILED: {exc}", file=sys.stderr)
        return 2

    rendered = json.dumps(
        report,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    )
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
