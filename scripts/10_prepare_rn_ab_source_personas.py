#!/usr/bin/env python3
"""Build or validate the RN 100-agent source persona candidate.

This utility is local-only.  It never changes the source SQLite file and does
not import a network or model client.  Generated snapshots are candidates that
become run inputs only when their exact hashes are pinned by a StudySpec.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from twinmarket_kr.rn_ab.persona_snapshot import (
    PersonaSnapshotError,
    SealedPersonaSnapshot,
    build_persona_snapshot,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create/validate a no-network RN source persona snapshot."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build", help="Build a new immutable 100-agent candidate.")
    build.add_argument("--source-db", type=Path, required=True)
    build.add_argument("--destination", type=Path, required=True)
    validate = subparsers.add_parser("validate", help="Validate an existing candidate.")
    validate.add_argument("--snapshot-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            source_before = _sha256(args.source_db)
            artifacts = build_persona_snapshot(
                source_db_path=args.source_db,
                snapshot_dir=args.destination,
                expected_agent_count=100,
                expected_depth_counts={0: 30, 1: 55, 2: 15},
            )
            source_after = _sha256(args.source_db)
            if source_before != source_after:
                parser.exit(2, "RN persona preparation rejected: source DB changed during build\n")
            sealed = SealedPersonaSnapshot.load(artifacts.snapshot_dir)
        else:
            sealed = SealedPersonaSnapshot.load(args.snapshot_dir)
            counts = {depth: 0 for depth in (0, 1, 2)}
            for persona in sealed.personas.values():
                counts[persona.news_depth] += 1
            if len(sealed.personas) != 100 or counts != {0: 30, 1: 55, 2: 15}:
                parser.exit(2, "RN persona preparation rejected: expected 100 agents / 30-55-15\n")
        print(
            json.dumps(
                {
                    "mode": "local_source_snapshot_no_network_no_paid_api",
                    "source_db_sha256": sealed.source_db_sha256,
                    "snapshot_db_sha256": sealed.snapshot_db_sha256,
                    "snapshot_manifest_sha256": sealed.manifest_sha256,
                    "depth_manifest_sha256": sealed.depth_manifest_sha256,
                    "repair_manifest_sha256": sealed.repair_manifest_sha256,
                    "persona_prompt_map_sha256": sealed.prompt_map_sha256,
                    "agent_count": len(sealed.personas),
                    "depth_counts": {
                        str(depth): sum(
                            persona.news_depth == depth for persona in sealed.personas.values()
                        )
                        for depth in (0, 1, 2)
                    },
                    "human_approval_claimed": False,
                    "network_requests": 0,
                    "paid_api_calls": 0,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    except (OSError, PersonaSnapshotError) as exc:
        parser.exit(2, f"RN persona preparation rejected: {exc}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
